from __future__ import annotations

import numpy as np

from waggle.benchmark_harness import (
    build_markdown_summary,
    choose_best_dedup_threshold,
    load_benchmark_fixtures,
    run_benchmarks,
)


class FakeEmbeddingModel:
    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def test_fixture_loading_is_auditable() -> None:
    fixtures = load_benchmark_fixtures()

    assert len(fixtures["extraction_cases"]) == 12
    assert len(fixtures["retrieval_cases"]["nodes"]) == 18
    assert len(fixtures["retrieval_cases"]["queries"]) == 18
    assert len(fixtures["dedup_cases"]) == 22
    assert len(fixtures["comparative_eval"]["scenarios"]) >= 20
    assert len(fixtures["comparative_eval"]["queries"]) == 66
    assert any(not case["should_dedup"] for case in fixtures["dedup_cases"])
    assert any(case["should_dedup"] for case in fixtures["dedup_cases"])


def test_benchmark_report_includes_backend_labels_and_case_counts() -> None:
    report = run_benchmarks(
        extraction_backend="regex",
        embedding_model=FakeEmbeddingModel(),
        systems=["waggle", "rag_naive", "rag_tuned"],
    )

    extraction = next(metric for metric in report.metrics if metric.metric == "extraction")
    retrieval = next(metric for metric in report.metrics if metric.metric == "retrieval")
    dedup = next(metric for metric in report.metrics if metric.metric == "deduplication")

    assert extraction.backend == "regex"
    assert extraction.case_count == 12
    assert retrieval.backend == "semantic-query"
    assert retrieval.case_count == 18
    assert "corpus_nodes" in retrieval.metadata
    assert dedup.backend == "semantic-dedup"
    assert dedup.case_count == 22
    assert "threshold" in dedup.metadata
    assert report.comparative["corpus"]["scenario_count"] >= 20
    assert report.comparative["corpus"]["query_count"] == 66
    assert set(report.comparative["systems"]) == {"waggle", "rag_naive", "rag_tuned"}
    assert len(report.comparative["per_case"]) == 198


def test_markdown_summary_includes_comparative_systems() -> None:
    report = run_benchmarks(
        extraction_backend="regex",
        embedding_model=FakeEmbeddingModel(),
        systems=["waggle", "rag_naive"],
    )

    markdown = build_markdown_summary(report)

    assert "# Waggle Comparative Evaluation" in markdown
    assert "| waggle |" in markdown
    assert "| rag_naive |" in markdown
    assert "Failure Protocol" in markdown


def test_llm_benchmark_failure_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr("waggle.benchmark_harness.extract_with_llm", lambda *args, **kwargs: None)

    report = run_benchmarks(extraction_backend="llm", embedding_model=FakeEmbeddingModel())

    assert report.errors
    assert "LLM extraction backend unavailable" in report.errors[0]
    assert all(
        not (metric.metric == "extraction" and metric.backend == "llm")
        for metric in report.metrics
    )


def test_dedup_threshold_sweep_tracks_positive_and_negative_cases() -> None:
    fixtures = load_benchmark_fixtures()

    best, sweep = choose_best_dedup_threshold(
        fixtures["dedup_cases"],
        embedding_model=FakeEmbeddingModel(),
    )

    assert sweep
    assert best.metadata["positive_cases"] == 11
    assert best.metadata["negative_cases"] == 11
    assert best.metadata["true_negatives"] + best.metadata["false_positives"] == 11
    assert best.metadata["true_positives"] + best.metadata["false_negatives"] == 11
