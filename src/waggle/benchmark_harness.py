from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from waggle.embeddings import EmbeddingModel
from waggle.extractor import EXTRACT_MODEL, OLLAMA_TIMEOUT_SECONDS, OLLAMA_URL, extract_with_llm
from waggle.graph import MemoryGraph
from waggle.intelligence import extract_conversation_candidates, infer_temporal_hints
from waggle.models import NodeType

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES_DIR = ROOT / "benchmarks" / "fixtures"
DEFAULT_DEDUP_THRESHOLDS = [0.82, 0.85, 0.88, 0.9, 0.92, 0.95, 0.97]


class BenchmarkRuntimeError(RuntimeError):
    """Raised when a requested benchmark cannot be executed honestly."""


@dataclass
class MetricSummary:
    metric: str
    backend: str
    passed: int
    total: int
    accuracy: float
    case_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComparativeCaseResult:
    query_id: str
    system: str
    task_family: str
    hit_at_k: bool
    exact_support: bool
    context_tokens: int
    retrieved_ids: list[str]
    gold_support_ids: list[str]


@dataclass
class BenchmarkReport:
    fixtures: dict[str, Any]
    metrics: list[MetricSummary]
    errors: list[str] = field(default_factory=list)
    threshold_sweep: list[MetricSummary] = field(default_factory=list)
    comparative: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixtures": self.fixtures,
            "metrics": [asdict(metric) for metric in self.metrics],
            "errors": list(self.errors),
            "threshold_sweep": [asdict(metric) for metric in self.threshold_sweep],
            "comparative": self.comparative,
        }


@dataclass
class RagChunk:
    chunk_id: str
    scenario_id: str
    session_id: str
    text: str
    timestamp: str
    support_fact_ids: list[str]


def load_benchmark_fixtures(fixtures_dir: Path | str = DEFAULT_FIXTURES_DIR) -> dict[str, Any]:
    base = Path(fixtures_dir)
    extraction_cases = json.loads((base / "extraction_cases.json").read_text(encoding="utf-8"))
    retrieval_cases = json.loads((base / "retrieval_cases.json").read_text(encoding="utf-8"))
    dedup_cases = json.loads((base / "dedup_cases.json").read_text(encoding="utf-8"))
    comparative_eval = json.loads((base / "comparative_eval.json").read_text(encoding="utf-8"))

    # Compute repo-relative path
    try:
        relative_dir = os.path.relpath(base, ROOT)
    except ValueError:
        # If relpath fails (e.g., different drives on Windows), fall back to absolute
        relative_dir = str(base)

    return {
        "base_dir": relative_dir,
        "extraction_cases": extraction_cases,
        "retrieval_cases": retrieval_cases,
        "dedup_cases": dedup_cases,
        "comparative_eval": comparative_eval,
    }


def _embedding_benchmark_error(exc: Exception, embedding_model: Any) -> BenchmarkRuntimeError:
    model_name = getattr(embedding_model, "model_name", "all-MiniLM-L6-v2")
    return BenchmarkRuntimeError(
        "Embedding-backed benchmarks require a locally available sentence-transformer model "
        f"('{model_name}'). Pre-cache the model before running retrieval/dedup benchmarks. "
        f"Original error: {exc}"
    )


def _graph(
    embedding_model: Any,
    *,
    dedup_similarity_threshold: float = 0.97,
    dedup_same_label_threshold: float = 0.9,
) -> MemoryGraph:
    tmpdir = tempfile.TemporaryDirectory()
    graph = MemoryGraph(
        Path(tmpdir.name) / "benchmark.db",
        embedding_model,
        dedup_similarity_threshold=dedup_similarity_threshold,
        dedup_same_label_threshold=dedup_same_label_threshold,
    )
    setattr(graph, "_benchmark_tmpdir", tmpdir)
    return graph


def _normalize_node_type(value: Any) -> str:
    if isinstance(value, NodeType):
        return value.value
    return str(value)


def _score_extraction_case(case: dict[str, Any], found_types: set[str]) -> bool:
    expected_types = set(case.get("expected_node_types", []))
    min_type_matches = int(case.get("min_type_matches", len(expected_types)))
    forbidden_types = set(case.get("forbidden_node_types", []))

    if expected_types:
        if len(found_types & expected_types) < min_type_matches:
            return False
    elif found_types:
        return False

    return not bool(found_types & forbidden_types)


def _estimate_tokens(text: str) -> int:
    normalized = text.strip()
    if not normalized:
        return 0
    return max(1, math.ceil(len(normalized) / 4))


def _percentile(values: list[int], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    weight = rank - lower
    return (ordered[lower] * (1 - weight)) + (ordered[upper] * weight)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _set_node_timestamp(graph: MemoryGraph, node_id: str, timestamp: str) -> None:
    with graph._lock, graph._connect() as connection:  # noqa: SLF001 - benchmark helper
        connection.execute(
            """
            UPDATE nodes
            SET created_at = ?, updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (timestamp, timestamp, node_id, graph.tenant_id),
        )


def _extract_fact_id(tags: list[str]) -> str | None:
    for tag in tags:
        if tag.startswith("fact_id:"):
            return tag.split(":", 1)[1]
    return None


def run_extraction_benchmark(
    cases: list[dict[str, Any]],
    *,
    backend: Literal["regex", "llm"],
    model: str = EXTRACT_MODEL,
    ollama_url: str = OLLAMA_URL,
    timeout_seconds: float = OLLAMA_TIMEOUT_SECONDS,
) -> MetricSummary:
    passed = 0
    for case in cases:
        if backend == "regex":
            candidates = extract_conversation_candidates(
                user_message=case["user_message"],
                assistant_response=case["assistant_response"],
            )
        else:
            candidates = extract_with_llm(
                case["user_message"],
                case["assistant_response"],
                model=model,
                ollama_url=ollama_url,
                timeout_seconds=timeout_seconds,
            )
            if candidates is None:
                raise BenchmarkRuntimeError(
                    f"LLM extraction backend unavailable at {ollama_url} using model '{model}' "
                    f"with timeout {timeout_seconds}s."
                )

        found_types = {_normalize_node_type(candidate["node_type"]) for candidate in candidates}
        if _score_extraction_case(case, found_types):
            passed += 1

    total = len(cases)
    return MetricSummary(
        metric="extraction",
        backend=backend,
        passed=passed,
        total=total,
        accuracy=passed / total if total else 0.0,
        case_count=total,
        metadata={
            "model": model if backend == "llm" else None,
            "ollama_url": ollama_url if backend == "llm" else None,
            "timeout_seconds": timeout_seconds if backend == "llm" else None,
        },
    )


def run_retrieval_benchmark(
    retrieval_fixtures: dict[str, Any],
    *,
    embedding_model: Any,
) -> MetricSummary:
    try:
        graph = _graph(embedding_model)
        for node in retrieval_fixtures["nodes"]:
            graph.add_node(
                label=node["label"],
                content=node["content"],
                node_type=NodeType(node["node_type"]),
                tags=["benchmark"],
            )

        passed = 0
        queries = retrieval_fixtures["queries"]
        for case in queries:
            result = graph.query(query=case["query"], max_nodes=5, max_depth=0)
            labels = [node.label for node in result.nodes]
            if any(case["expected_label_contains"].lower() in label.lower() for label in labels):
                passed += 1
    except Exception as exc:
        raise _embedding_benchmark_error(exc, embedding_model) from exc

    total = len(queries)
    return MetricSummary(
        metric="retrieval",
        backend="semantic-query",
        passed=passed,
        total=total,
        accuracy=passed / total if total else 0.0,
        case_count=total,
        metadata={"corpus_nodes": len(retrieval_fixtures["nodes"]), "top_k": 5},
    )


def run_dedup_benchmark(
    cases: list[dict[str, Any]],
    *,
    embedding_model: Any,
    dedup_threshold: float,
) -> MetricSummary:
    try:
        passed = 0
        true_positives = 0
        true_negatives = 0
        false_positives = 0
        false_negatives = 0

        for case in cases:
            graph = _graph(
                embedding_model,
                dedup_similarity_threshold=dedup_threshold,
                dedup_same_label_threshold=dedup_threshold,
            )
            first = case["first"]
            second = case["second"]
            node_type = NodeType(case["node_type"])
            graph.add_node(label=first["label"], content=first["content"], node_type=node_type)
            second_result = graph.add_node(label=second["label"], content=second["content"], node_type=node_type)
            did_dedup = not second_result.created
            expected = bool(case["should_dedup"])

            if did_dedup == expected:
                passed += 1
                if expected:
                    true_positives += 1
                else:
                    true_negatives += 1
            elif expected:
                false_negatives += 1
            else:
                false_positives += 1
    except Exception as exc:
        raise _embedding_benchmark_error(exc, embedding_model) from exc

    total = len(cases)
    return MetricSummary(
        metric="deduplication",
        backend="semantic-dedup",
        passed=passed,
        total=total,
        accuracy=passed / total if total else 0.0,
        case_count=total,
        metadata={
            "threshold": dedup_threshold,
            "positive_cases": sum(1 for case in cases if case["should_dedup"]),
            "negative_cases": sum(1 for case in cases if not case["should_dedup"]),
            "true_positives": true_positives,
            "true_negatives": true_negatives,
            "false_positives": false_positives,
            "false_negatives": false_negatives,
        },
    )


def choose_best_dedup_threshold(
    cases: list[dict[str, Any]],
    *,
    embedding_model: Any,
    thresholds: list[float] | None = None,
) -> tuple[MetricSummary, list[MetricSummary]]:
    candidates = thresholds or DEFAULT_DEDUP_THRESHOLDS
    sweep = [
        run_dedup_benchmark(cases, embedding_model=embedding_model, dedup_threshold=threshold)
        for threshold in candidates
    ]
    best = max(
        sweep,
        key=lambda summary: (
            summary.accuracy,
            summary.metadata["true_negatives"],
            summary.metadata["true_positives"],
            summary.metadata["threshold"],
        ),
    )
    return best, sweep


def _build_rag_chunks(
    comparative_eval: dict[str, Any],
    *,
    chunk_size_words: int,
    overlap_words: int,
) -> list[RagChunk]:
    chunks: list[RagChunk] = []
    for scenario in comparative_eval["scenarios"]:
        for session in scenario["sessions"]:
            words = session["transcript"].split()
            step = max(1, chunk_size_words - overlap_words)
            for index, start in enumerate(range(0, len(words), step)):
                end = start + chunk_size_words
                chunk_words = words[start:end]
                if not chunk_words:
                    continue
                chunks.append(
                    RagChunk(
                        chunk_id=f"{session['id']}::chunk{index}",
                        scenario_id=scenario["id"],
                        session_id=session["id"],
                        text=" ".join(chunk_words),
                        timestamp=session["timestamp"],
                        support_fact_ids=list(session.get("support_fact_ids", [])),
                    )
                )
                if end >= len(words):
                    break
    return chunks


def _score_rag_chunk(
    query: str,
    chunk: RagChunk,
    query_embedding: Any,
    chunk_embedding: Any,
    embedding_model: Any,
    *,
    lexical_weight: float,
    temporal_weight: float,
) -> float:
    similarity = max(embedding_model.cosine_similarity(query_embedding, chunk_embedding), 0.0)
    lexical_tokens = set(query.lower().split())
    chunk_tokens = set(chunk.text.lower().split())
    lexical_overlap = len(lexical_tokens & chunk_tokens) / max(len(lexical_tokens), 1)
    temporal_hints = infer_temporal_hints(query)
    timestamp = _parse_iso(chunk.timestamp)
    temporal_score = 0.0
    if temporal_hints.recency_mode == "latest":
        temporal_score = timestamp.timestamp()
    elif temporal_hints.recency_mode == "oldest":
        temporal_score = -timestamp.timestamp()
    return (0.72 * similarity) + (lexical_weight * lexical_overlap) + (temporal_weight * temporal_score)


def _rank_rag_chunks(
    query: str,
    chunks: list[RagChunk],
    embedding_model: Any,
    *,
    top_k: int,
    lexical_weight: float,
    temporal_weight: float,
) -> list[RagChunk]:
    chunk_embeddings = [embedding_model.embed(chunk.text) for chunk in chunks]
    query_embedding = embedding_model.embed(query)
    scored = [
        (
            _score_rag_chunk(
                query,
                chunk,
                query_embedding,
                chunk_embedding,
                embedding_model,
                lexical_weight=lexical_weight,
                temporal_weight=temporal_weight,
            ),
            chunk.timestamp,
            chunk.chunk_id,
            chunk,
        )
        for chunk, chunk_embedding in zip(chunks, chunk_embeddings, strict=True)
    ]
    return [
        item[3]
        for item in sorted(scored, key=lambda item: (-item[0], item[1], item[2]))[:top_k]
    ]


def _aggregate_case_results(results: list[ComparativeCaseResult]) -> dict[str, Any]:
    hit_scores = [1 if result.hit_at_k else 0 for result in results]
    exact_scores = [1 if result.exact_support else 0 for result in results]
    token_costs = [result.context_tokens for result in results]
    by_family: dict[str, dict[str, Any]] = {}

    for family in sorted({result.task_family for result in results}):
        family_results = [result for result in results if result.task_family == family]
        by_family[family] = {
            "case_count": len(family_results),
            "hit_at_k": sum(1 if result.hit_at_k else 0 for result in family_results) / len(family_results),
            "exact_support": sum(1 if result.exact_support else 0 for result in family_results) / len(family_results),
        }

    return {
        "case_count": len(results),
        "hit_at_k": sum(hit_scores) / len(hit_scores) if hit_scores else 0.0,
        "exact_support": sum(exact_scores) / len(exact_scores) if exact_scores else 0.0,
        "context_tokens": {
            "mean": statistics.mean(token_costs) if token_costs else 0.0,
            "median": statistics.median(token_costs) if token_costs else 0.0,
            "p95": _percentile(token_costs, 0.95),
        },
        "by_task_family": by_family,
    }


def _run_waggle_system(
    comparative_eval: dict[str, Any],
    *,
    embedding_model: Any,
    top_k: int,
) -> tuple[dict[str, Any], list[ComparativeCaseResult]]:
    graph = _graph(embedding_model)
    for scenario in comparative_eval["scenarios"]:
        for fact in scenario["facts"]:
            result = graph.add_node(
                label=fact["label"],
                content=fact["content"],
                node_type=NodeType(fact["node_type"]),
                tags=["benchmark", f"fact_id:{fact['id']}", f"scenario:{scenario['id']}"],
            )
            _set_node_timestamp(graph, result.node.id, fact["timestamp"])

    results: list[ComparativeCaseResult] = []
    for case in comparative_eval["queries"]:
        subgraph = graph.query(query=case["query"], max_nodes=top_k, max_depth=0)
        retrieved_fact_ids = [
            fact_id
            for node in subgraph.nodes
            for fact_id in [_extract_fact_id(node.tags)]
            if fact_id is not None
        ]
        union_ids = set(retrieved_fact_ids)
        gold_ids = set(case["gold_support_ids"])
        context_text = "\n".join(f"{node.label}: {node.content}" for node in subgraph.nodes)
        results.append(
            ComparativeCaseResult(
                query_id=case["id"],
                system="waggle",
                task_family=case["task_family"],
                hit_at_k=bool(union_ids & gold_ids),
                exact_support=gold_ids.issubset(union_ids),
                context_tokens=_estimate_tokens(context_text),
                retrieved_ids=retrieved_fact_ids,
                gold_support_ids=list(case["gold_support_ids"]),
            )
        )

    return {"system": "waggle", "parameters": {"top_k": top_k}}, results


def _run_rag_system(
    comparative_eval: dict[str, Any],
    *,
    embedding_model: Any,
    system: Literal["rag_naive", "rag_tuned"],
) -> tuple[dict[str, Any], list[ComparativeCaseResult]]:
    if system == "rag_naive":
        config = {"chunk_size_words": 120, "overlap_words": 20, "top_k": 5, "lexical_weight": 0.0, "temporal_weight": 0.0}
    else:
        config = {"chunk_size_words": 80, "overlap_words": 10, "top_k": 8, "lexical_weight": 0.18, "temporal_weight": 0.000000001}

    chunks = _build_rag_chunks(
        comparative_eval,
        chunk_size_words=config["chunk_size_words"],
        overlap_words=config["overlap_words"],
    )
    results: list[ComparativeCaseResult] = []
    for case in comparative_eval["queries"]:
        ranked = _rank_rag_chunks(
            case["query"],
            chunks,
            embedding_model,
            top_k=config["top_k"],
            lexical_weight=config["lexical_weight"],
            temporal_weight=config["temporal_weight"],
        )
        retrieved_ids = [fact_id for chunk in ranked for fact_id in chunk.support_fact_ids]
        union_ids = set(retrieved_ids)
        gold_ids = set(case["gold_support_ids"])
        context_text = "\n".join(chunk.text for chunk in ranked)
        results.append(
            ComparativeCaseResult(
                query_id=case["id"],
                system=system,
                task_family=case["task_family"],
                hit_at_k=bool(union_ids & gold_ids),
                exact_support=gold_ids.issubset(union_ids),
                context_tokens=_estimate_tokens(context_text),
                retrieved_ids=retrieved_ids,
                gold_support_ids=list(case["gold_support_ids"]),
            )
        )
    return {"system": system, "parameters": config}, results


def run_comparative_evaluation(
    comparative_eval: dict[str, Any],
    *,
    embedding_model: Any,
    systems: list[str],
) -> dict[str, Any]:
    try:
        system_summaries: dict[str, Any] = {}
        all_case_results: list[dict[str, Any]] = []
        for system in systems:
            if system == "waggle":
                info, results = _run_waggle_system(comparative_eval, embedding_model=embedding_model, top_k=5)
            elif system in {"rag_naive", "rag_tuned"}:
                info, results = _run_rag_system(
                    comparative_eval,
                    embedding_model=embedding_model,
                    system=system,
                )
            else:
                raise BenchmarkRuntimeError(f"Unsupported comparative system: {system}")

            summary = _aggregate_case_results(results)
            summary["parameters"] = info["parameters"]
            system_summaries[system] = summary
            all_case_results.extend(asdict(result) for result in results)

        return {
            "corpus": {
                "scenario_count": len(comparative_eval["scenarios"]),
                "query_count": len(comparative_eval["queries"]),
                "task_families": sorted({case["task_family"] for case in comparative_eval["queries"]}),
            },
            "systems": system_summaries,
            "per_case": all_case_results,
            "failure_protocol": list(comparative_eval.get("failure_protocol", [])),
        }
    except BenchmarkRuntimeError:
        raise
    except Exception as exc:
        raise _embedding_benchmark_error(exc, embedding_model) from exc


def _format_metric(metric: MetricSummary) -> str:
    extras = []
    if metric.metric == "extraction":
        extras.append(f"backend={metric.backend}")
        if metric.metadata.get("model"):
            extras.append(f"model={metric.metadata['model']}")
        if metric.metadata.get("timeout_seconds") is not None:
            extras.append(f"timeout={metric.metadata['timeout_seconds']}s")
    elif metric.metric == "retrieval":
        extras.append(f"backend={metric.backend}")
        extras.append(f"corpus_nodes={metric.metadata['corpus_nodes']}")
    elif metric.metric == "deduplication":
        extras.append(f"backend={metric.backend}")
        extras.append(f"threshold={metric.metadata['threshold']:.2f}")
        extras.append(
            f"positives={metric.metadata['positive_cases']}, negatives={metric.metadata['negative_cases']}"
        )
    extras.append(f"cases={metric.case_count}")
    return (
        f"{metric.metric:<14} {metric.passed}/{metric.total} = {metric.accuracy:.0%} "
        f"({' | '.join(extras)})"
    )


def build_markdown_summary(report: BenchmarkReport) -> str:
    if not hasattr(report, "comparative") or report.comparative is None:
        lines = ["# Waggle Benchmark Report", ""]
        if hasattr(report, "error") and report.error:
            lines.append(f"**Error:** {report.error}")
        if hasattr(report, "status") and report.status:
            lines.append(f"**Status:** {report.status}")
        if hasattr(report, "failure_protocol") and report.failure_protocol:
            lines.extend(["", "## Failure Protocol", ""])
            for item in report.failure_protocol:
                lines.append(f"- {item}")
        return "\n".join(lines) + "\n"

    lines = [
        "# Waggle Comparative Evaluation",
        "",
        f"- Scenarios: {report.comparative['corpus']['scenario_count']}",
        f"- Queries: {report.comparative['corpus']['query_count']}",
        f"- Task families: {', '.join(report.comparative['corpus']['task_families'])}",
        "",
        "| System | Hit@k | Exact support | Mean tokens | Median tokens | p95 tokens |",
        "|--------|-------|---------------|-------------|---------------|------------|",
    ]
    for system, metrics in report.comparative["systems"].items():
        lines.append(
            f"| {system} | {metrics['hit_at_k']:.0%} | {metrics['exact_support']:.0%} | "
            f"{metrics['context_tokens']['mean']:.1f} | {metrics['context_tokens']['median']:.1f} | "
            f"{metrics['context_tokens']['p95']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Failure Protocol",
            "",
        ]
    )
    for item in report.comparative.get("failure_protocol", []):
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def run_benchmarks(
    *,
    extraction_backend: Literal["regex", "llm", "both"] = "both",
    model: str = EXTRACT_MODEL,
    ollama_url: str = OLLAMA_URL,
    ollama_timeout_seconds: float = OLLAMA_TIMEOUT_SECONDS,
    fixtures_dir: Path | str = DEFAULT_FIXTURES_DIR,
    embedding_model: Any | None = None,
    dedup_threshold: float | None = None,
    systems: list[str] | None = None,
) -> BenchmarkReport:
    fixtures = load_benchmark_fixtures(fixtures_dir)
    model_instance = embedding_model or EmbeddingModel()
    selected_systems = systems or ["waggle", "rag_naive", "rag_tuned"]
    report = BenchmarkReport(
        fixtures={
            "directory": fixtures["base_dir"],
            "extraction_cases": len(fixtures["extraction_cases"]),
            "retrieval_nodes": len(fixtures["retrieval_cases"]["nodes"]),
            "retrieval_queries": len(fixtures["retrieval_cases"]["queries"]),
            "dedup_cases": len(fixtures["dedup_cases"]),
            "comparative_scenarios": len(fixtures["comparative_eval"]["scenarios"]),
            "comparative_queries": len(fixtures["comparative_eval"]["queries"]),
        },
        metrics=[],
    )

    if extraction_backend in ("regex", "both"):
        report.metrics.append(
            run_extraction_benchmark(fixtures["extraction_cases"], backend="regex")
        )

    if extraction_backend in ("llm", "both"):
        try:
            report.metrics.append(
                run_extraction_benchmark(
                    fixtures["extraction_cases"],
                    backend="llm",
                    model=model,
                    ollama_url=ollama_url,
                    timeout_seconds=ollama_timeout_seconds,
                )
            )
        except BenchmarkRuntimeError as exc:
            report.errors.append(str(exc))

    embedding_ready = True

    try:
        report.metrics.append(
            run_retrieval_benchmark(fixtures["retrieval_cases"], embedding_model=model_instance)
        )
    except BenchmarkRuntimeError as exc:
        report.errors.append(str(exc))
        embedding_ready = False

    if embedding_ready:
        try:
            if dedup_threshold is None:
                dedup_result, sweep = choose_best_dedup_threshold(
                    fixtures["dedup_cases"],
                    embedding_model=model_instance,
                )
                report.threshold_sweep.extend(sweep)
            else:
                dedup_result = run_dedup_benchmark(
                    fixtures["dedup_cases"],
                    embedding_model=model_instance,
                    dedup_threshold=dedup_threshold,
                )
            report.metrics.append(dedup_result)
        except BenchmarkRuntimeError as exc:
            report.errors.append(str(exc))

        try:
            report.comparative = run_comparative_evaluation(
                fixtures["comparative_eval"],
                embedding_model=model_instance,
                systems=selected_systems,
            )
        except BenchmarkRuntimeError as exc:
            report.errors.append(str(exc))
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reproducible local benchmark harness for waggle-mcp.")
    parser.add_argument(
        "--extraction-backend",
        choices=["regex", "llm", "both"],
        default=os.getenv("WAGGLE_BENCHMARK_EXTRACTION_BACKEND", "both"),
        help="Which extraction benchmark(s) to run.",
    )
    parser.add_argument(
        "--systems",
        nargs="+",
        choices=["waggle", "rag_naive", "rag_tuned", "all"],
        default=["all"],
        help="Comparative systems to run. 'all' expands to waggle, rag_naive, rag_tuned.",
    )
    parser.add_argument(
        "--ollama-model",
        default=os.getenv("WAGGLE_EXTRACT_MODEL", EXTRACT_MODEL),
        help="Ollama model used for the LLM extraction benchmark.",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("WAGGLE_OLLAMA_URL", OLLAMA_URL),
        help="Base URL for the local Ollama instance used in the LLM extraction benchmark.",
    )
    parser.add_argument(
        "--ollama-timeout-seconds",
        type=float,
        default=float(os.getenv("WAGGLE_OLLAMA_TIMEOUT_SECONDS", str(OLLAMA_TIMEOUT_SECONDS))),
        help="Timeout for each Ollama extraction request.",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=DEFAULT_FIXTURES_DIR,
        help="Directory containing checked-in benchmark fixture JSON files.",
    )
    parser.add_argument(
        "--dedup-threshold",
        type=float,
        default=None,
        help="Optional fixed dedup threshold. If omitted, the harness sweeps checked-in thresholds and picks the best score.",
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("WAGGLE_MODEL", "all-MiniLM-L6-v2"),
        help="Embedding model shared by Waggle retrieval and the eval baselines.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path. When provided, a Markdown summary is also written beside it.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    selected_systems = ["waggle", "rag_naive", "rag_tuned"] if "all" in args.systems else args.systems
    report = run_benchmarks(
        extraction_backend=args.extraction_backend,
        model=args.ollama_model,
        ollama_url=args.ollama_url,
        ollama_timeout_seconds=args.ollama_timeout_seconds,
        fixtures_dir=args.fixtures_dir,
        dedup_threshold=args.dedup_threshold,
        embedding_model=EmbeddingModel(args.embedding_model),
        systems=selected_systems,
    )

    print("=" * 72)
    print("waggle-mcp benchmark harness")
    print("=" * 72)
    print(
        f"fixtures: extraction={report.fixtures['extraction_cases']} "
        f"retrieval_nodes={report.fixtures['retrieval_nodes']} "
        f"retrieval_queries={report.fixtures['retrieval_queries']} "
        f"dedup_cases={report.fixtures['dedup_cases']} "
        f"comparative_scenarios={report.fixtures['comparative_scenarios']} "
        f"comparative_queries={report.fixtures['comparative_queries']}"
    )
    for metric in report.metrics:
        print(_format_metric(metric))

    if report.threshold_sweep:
        print("dedup threshold sweep:")
        for metric in report.threshold_sweep:
            print(f"  {_format_metric(metric)}")

    if report.comparative:
        print("comparative systems:")
        for system, metrics in report.comparative["systems"].items():
            print(
                f"  {system:<10} hit@k={metrics['hit_at_k']:.0%} "
                f"exact={metrics['exact_support']:.0%} "
                f"tokens(mean/median/p95)={metrics['context_tokens']['mean']:.1f}/"
                f"{metrics['context_tokens']['median']:.1f}/{metrics['context_tokens']['p95']:.1f}"
            )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        markdown_path = args.output.with_suffix(".md")
        markdown_path.write_text(build_markdown_summary(report), encoding="utf-8")
        print(f"wrote JSON report to {args.output}")
        print(f"wrote Markdown summary to {markdown_path}")

    if report.errors:
        for error in report.errors:
            print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())