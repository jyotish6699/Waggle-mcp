<p align="center">
  <img src="https://raw.githubusercontent.com/Abhigyan-Shekhar/graph-memory-mcp/main/assets/banner.png" alt="waggle-mcp" width="720"/>
</p>

<p align="center">
  <strong>Your AI forgets everything between sessions. Waggle gives it a graph-backed brain.</strong><br/>
  Persistent, structured memory for AI agents — typically lower-token than chunk-based retrieval, often 2-4× on factual lookups.
</p>

<p align="center">
  <em>Waggle is not a code indexer. It's a conversational memory engine — it remembers what you decided, why, and what changed, across every session.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/waggle-mcp"><img src="https://img.shields.io/pypi/v/waggle-mcp?color=39d5cf&label=pypi" alt="PyPI"/></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/MCP-compatible-brightgreen" alt="MCP compatible"/>
  <img src="https://img.shields.io/badge/embeddings-local%2C%20no%20API%20key-orange" alt="Local embeddings"/>
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT"/>
</p>

<p align="center">
  <a href="https://glama.ai/mcp/servers/Abhigyan-Shekhar/Waggle-mcp"><img src="https://glama.ai/mcp/servers/Abhigyan-Shekhar/Waggle-mcp/badges/card.svg" alt="Waggle-mcp MCP server"/></a>
  <a href="https://glama.ai/mcp/servers/Abhigyan-Shekhar/Waggle-mcp"><img src="https://glama.ai/mcp/servers/Abhigyan-Shekhar/Waggle-mcp/badges/score.svg" alt="Waggle-mcp MCP server score"/></a>
</p>

---

## What's New — v0.1.7

- **Benchmark harness**: end-to-end `WaggleAdapter` connecting the graph engine to ConvoMem / MemBench runners with automated exact-match scoring and latency logging.
- **LongMemEval integration**: CLI-driven retrieval evaluation against the official LongMemEval split (`97.4% R@5` / `88.2% Exact@5` in `graph_raw`, `96.4%` / `85.6%` in `graph_hybrid`).
- **Logging utilities**: structured log helpers (`logging_utils`) for consistent, level-aware output across all subsystems.
- **Evidence tracking**: new `evidence.py` module records source provenance on stored nodes so reasoning chains are fully traceable.
- **Observability stack**: Grafana dashboard, Prometheus config, and Docker Compose overlay in `deploy/observability/`.
- **Kubernetes manifests**: production-grade `deployment.yaml`, network policy, external-secret, and certificate templates under `deploy/kubernetes/`.
- **Operational runbooks**: incident response, secret management, API-key rotation, and onboarding guides added to `docs/runbooks/`.

---

## Who is this for?

**→ Individual developer** extending Claude, Codex, Cursor, or Antigravity with persistent memory:
`pip install waggle-mcp && waggle-mcp init` and you're done. SQLite + local embeddings, zero infra.

**→ Team running a shared memory service:** Waggle ships with a Docker image, Kubernetes manifests, Prometheus metrics, and multi-tenant auth. See [deploy/kubernetes/](./deploy/kubernetes/) and [docs/runbooks/](./docs/runbooks/).

Both paths share the same MCP tool surface — the difference is only the backend and transport.

---

## Why waggle-mcp?

`waggle-mcp` is a local-first memory layer for MCP-compatible AI clients, built on a persistent knowledge graph. It gives your AI a persistent knowledge graph it can read and write through any MCP-compatible client (Claude Desktop, Cursor, Codex, Antigravity, etc.).

| Stuffed context | Structured retrieval |
|-----------------|----------------------|
| Huge prompts every session | Compact subgraph retrieved at query time |
| Session-local memory | Persistent multi-session memory |
| Flat notes and chunks | Typed nodes and edges: decisions, reasons, contradictions |
| "What changed?" requires replaying logs | Temporal queries and diffs are first-class |

Waggle often uses materially fewer tokens than naive chunked retrieval on factual lookups, while graph-traversal queries intentionally spend more context to include reasoning chains such as updates, contradictions, and dependencies.

---

## Architecture

```mermaid
flowchart LR
  C["MCP Client\n(Claude/Cursor/Codex/Antigravity)"] --> S["waggle.server\nMCP tool surface"]
  S --> G["Graph Engine\nMemoryGraph / Neo4jMemoryGraph"]
  G --> DB["SQLite (local default)\nor Neo4j (service mode)"]
  G --> E["Embeddings\n(sentence-transformers or deterministic fallback)"]
```

---

## Quick start

```bash
pip install waggle-mcp
waggle-mcp init
# Restart your MCP client. Done.
```

`init` detects your MCP client, writes its config, and creates the local database directory. Default mode is local SQLite with on-device embeddings. Antigravity and manual configuration details are in [docs/reference.md](./docs/reference.md).

Manual MCP setup examples for **Codex**, **Claude Code**, **Cursor**, and **Antigravity** are in [docs/reference.md](./docs/reference.md#manual-client-configuration).

> **⚠️ Edges are what make graph memory work.**
> `observe_conversation` and `decompose_and_store` create edges automatically.
> If you only call `store_node`, you get isolated facts — not a connected graph.
> Always prefer `observe_conversation` for conversational ingestion.

---

## Setting Up waggle as an MCP Server

> **One-time install:** `pip install waggle-mcp` — no API key, no cloud account, no Docker required for local use.

Use this shared JSON config shape for clients that accept `mcpServers` JSON:

```json
{
  "mcpServers": {
    "waggle": {
      "command": "python",
      "args": ["-m", "waggle.server"],
      "env": {
        "WAGGLE_TRANSPORT": "stdio",
        "WAGGLE_BACKEND": "sqlite",
        "WAGGLE_DB_PATH": "~/.waggle/memory.db",
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

<details>
<summary>Claude Desktop / Antigravity / Cursor / Claude Code setup details</summary>

**Claude Desktop config file location**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Antigravity**
- Open agent panel → `···` → **Manage MCP Servers** → **View raw config**
- Paste the same JSON block above.

**Cursor**
- `Cursor Settings -> Features -> MCP Servers -> + Add`
- Command: `python`
- Args: `-m waggle.server`
- Env vars: same keys as the JSON block above.

**Claude Code**
```bash
claude mcp add waggle \
  --env WAGGLE_TRANSPORT=stdio \
  --env WAGGLE_BACKEND=sqlite \
  --env WAGGLE_DB_PATH=~/.waggle/memory.db \
  --env WAGGLE_DEFAULT_TENANT_ID=local-default \
  --env WAGGLE_MODEL=all-MiniLM-L6-v2 \
  -- python -m waggle.server
```

</details>

### Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.waggle]
command = "python"
args    = ["-m", "waggle.server"]
env     = {
  WAGGLE_TRANSPORT         = "stdio",
  WAGGLE_BACKEND           = "sqlite",
  WAGGLE_DB_PATH           = "~/.waggle/memory.db",
  WAGGLE_DEFAULT_TENANT_ID = "local-default",
  WAGGLE_MODEL             = "all-MiniLM-L6-v2"
}
```

### `python` not on PATH?

Replace `"command": "python"` with the full interpreter path:

```bash
which python3   # macOS / Linux
where python    # Windows
```

e.g. `/usr/local/bin/python3` or `C:\Python311\python.exe`.

### Verify it works

After restarting your client, ask the agent:

> *"Store a note: we're using PostgreSQL for this project."*

Then open a **fresh session** and ask:

> *"What database are we using?"*

Expected result (example):

```text
You decided on PostgreSQL for this project.
Reason captured: MySQL replication had been painful.
```

If you see that kind of recall in a new session, you're live.

### Quick-reference tool table

| Ask the agent… | Tool called |
|---|---|
| *"Remember that…"* | `observe_conversation` |
| *"What do you know about X?"* | `query_graph` |
| *"What changed recently?"* | `graph_diff` |
| *"Summarize context for a new session"* | `prime_context` |
| *"Show all stored topics"* | `get_topics` |
| *"Export my memory to a file"* | `export_graph_backup` |

For the full tool surface and environment variable reference see [docs/reference.md](./docs/reference.md).

---

## Using It In MCP Clients

Once installed, you usually do not run `waggle-mcp` commands by hand during daily work. Talk to the agent normally, and it calls Waggle MCP tools to store and retrieve memory.

- **Codex / Claude Code**: `observe_conversation`, `query_graph`, and `prime_context` are called automatically during normal threads.
- **Cursor**: decisions and facts can be persisted as graph memory instead of getting lost in old chat windows.
- **Antigravity**: conversation turns can be extracted via `observe_conversation`; context can be exported with `export_context_bundle`.

For a built-in CLI explanation of the feature surface, run:

```bash
waggle-mcp features
```

---

## See it in action

![Waggle init demo](./assets/demo.svg)

### How It Works (Interaction Flow)

```text
User  -> Agent -> observe_conversation(...) -> Graph stores typed nodes + edges
User  -> Agent -> query_graph("database")    -> Subgraph returned -> Agent answers with linked rationale
```

**Session 1** — April 10
```text
User:  Let's use PostgreSQL. MySQL replication has been painful.
Agent: [calls observe_conversation()]
       → stores decision node: "Chose PostgreSQL over MySQL"
       → stores reason node:   "MySQL replication painful"
       → links them with a depends_on edge
```

**Session 2** — April 12 (fresh context window, no history)
```text
User:  What did we decide about the database?
Agent: [calls query_graph("database decision")]
       → retrieves the decision node + linked reason from April 10

       "You decided on PostgreSQL on April 10. The reason recorded was
        that MySQL replication had been painful."
```

**Session 3** — April 14
```text
User:  Actually, let's reconsider — the team is more familiar with MySQL.
Agent: [calls store_node() + store_edge(new_node → old_node, "contradicts")]
       → both positions are preserved, and the contradiction is explicit
```

### Knowledge graph visual (example)

```mermaid
graph TD
  D1["Decision: Use PostgreSQL"]
  R1["Reason: MySQL replication pain"]
  D2["Decision update: reconsider MySQL"]
  P1["Preference: dark mode UI"]
  N1["Note: add integration tests"]

  D1 -- "depends_on" --> R1
  D2 -- "contradicts" --> D1
  N1 -- "relates_to" --> D2
  P1 -- "part_of project context" --> D1
```

---

## Key Features

- **Automatic Extraction**: `observe_conversation` ingests facts into the graph without manual schema work.
- **Portable Context**: `export_context_bundle` generates Markdown/JSON context packs for another AI.
- **Vault Round-trip**: `export_markdown_vault` / `import_markdown_vault` for Obsidian-style node editing.
- **Conflict Resolution**: `list_conflicts` / `resolve_conflict` to manage contradictions without losing history.
- **Deterministic Fallback**: Stable SHA-256 hashing for reliable, reproducible offline operation when transformer models are unavailable.

---

## Security & Privacy

By default, data stays local on your machine (`sqlite` backend, local database path such as `~/.waggle/memory.db`).  
Waggle does not require telemetry or cloud calls for core local operation.  
Your conversation memory only leaves your machine if you explicitly configure a remote backend or remote infrastructure.

---

## Graph Data Model

### Node Types

`fact`, `entity`, `concept`, `preference`, `decision`, `question`, `note`

### Edge Types

`relates_to`, `contradicts`, `depends_on`, `part_of`, `updates`, `derived_from`, `similar_to`

---

## Model Support

Waggle currently uses a local `sentence-transformers` embedding model selected by `WAGGLE_MODEL`.

- Default: `all-MiniLM-L6-v2`
- Any locally available `sentence-transformers` model name can be used.
- If the selected model is unavailable locally, Waggle falls back to deterministic embeddings for portability.

Set model in env:

```bash
WAGGLE_MODEL=all-mpnet-base-v2 waggle-mcp serve
```

Set model in MCP client config (example):

```json
{
  "mcpServers": {
    "waggle": {
      "command": "python",
      "args": ["-m", "waggle.server"],
      "env": {
        "WAGGLE_MODEL": "all-mpnet-base-v2"
      }
    }
  }
}
```

Notes:
- Waggle does not currently route to hosted embedding providers directly; embedding inference is local to the runtime.
- Deterministic mode is useful for offline/testing portability, but semantic retrieval quality is lower than transformer mode.

---

## Performance Snapshot

| Operation | Time | Notes |
|---|---:|---|
| `observe_conversation` | ~1.54 ms (mean) | Single conversation turn ingestion, local `sqlite` + deterministic embeddings |
| `query_graph` | ~1.60 ms (mean) | Subgraph retrieval (`max_nodes=12`, `max_depth=2`) |
| `graph_diff` | ~0.80 ms (mean) | Temporal diff over local graph |
| Context tokens (comparative mean) | `56.3` vs `150.2` | Waggle vs naive RAG baseline (`~2.7x` lower-token) |

Sources: [performance_snapshot.md](./tests/artifacts/verification/2026-04-20-performance-snapshot/performance_snapshot.md), [benchmark_current.md](./tests/artifacts/benchmark_current.md)

> **Example:** Retrieving a database decision stored days ago uses about `56` tokens from a Waggle subgraph vs about `150` tokens from naive context replay (`~2.7x` lower-token).

---

## Benchmarks & Verification

### External Benchmark — LongMemEval

LongMemEval session-retrieval results (500 questions):

| Method | R@5 | Exact@5 | Notes |
|------|-----|---------|-------|
| `graph_raw` | `97.4%` | `88.2%` | Full split, no second-stage reranking (`13/500` misses). Source: [`results_graph_raw.json`](./benchmarks/longmemeval/results_graph_raw.json) |
| `graph_hybrid` | `96.4%` | `85.6%` | Full split with hybrid reranking (`18/500` misses). Source: [`results_graph_hybrid.json`](./benchmarks/longmemeval/results_graph_hybrid.json) |

`Exact@5` is stricter than R@5 and is included here to show precision on support-session retrieval, not just any top-5 hit.

**Important:** on the current saved artifacts, raw retrieval outperforms hybrid reranking on both R@5 and Exact@5. We are treating this as a tuning target for `v0.1.8` rather than changing defaults to a weaker mode.

### Cross-Project Context (LongMemEval, session retrieval)

| System | R@5 | Notes |
|------|-----|-------|
| Waggle (`graph_raw`) | `97.4%` | Graph traversal, no reranking |
| MemPalace | `96.6%` | Semantic search, no LLM |
| Supermemory | `81.6%` | Self-reported |
| Vektori | `73%` | LongMemEval-S, gemini-flash-lite |

⚠️ Methodologies differ across projects. We encourage running your own evals. See [tests/artifacts/README.md](./tests/artifacts/README.md) for our setup.

### Internal Fixtures

| Area | Corpus | Result |
|------|--------|--------|
| Extraction | 25-case deterministic fixture | `100.0%` (source: [`benchmark_current.md`](./tests/artifacts/benchmark_current.md)) |
| Retrieval | 18-query retrieval fixture | `83.3% Hit@k` (source: [`benchmark_current.md`](./tests/artifacts/benchmark_current.md)) |
| Query stress | 40 adversarial retrieval-only cases | `97.5% Hit@k`, `97.5% exact support` (source: [`benchmark_current.md`](./tests/artifacts/benchmark_current.md)) |
| Deduplication | 22 cases (semi-semantic) | `0` false merges at threshold; `77.3%` overall (source: [`benchmark_current.md`](./tests/artifacts/benchmark_current.md)) |
| Automated tests | Infrastructure & logic | `91 passed` (source: [`pytest_test_benchmark_harness.txt`](./tests/artifacts/verification/2026-04-18-readme-claims/pytest_test_benchmark_harness.txt)) |

**Deduplication note:** Zero false-positive merges is the safety invariant. The 77.3% overall accuracy is intentionally conservative — the system prefers a missed merge over a wrong merge. Improving recall without introducing false positives is the active work for 0.1.8.

Detailed artifacts and methodology: **[Benchmark Methodology](./docs/benchmark-methodology.md)** · [tests/artifacts/README.md](./tests/artifacts/README.md)

---

## Known Limitations

- **Best on structured recall, weaker on answer synthesis**: Waggle is strongest at "retrieve the right facts and relationships" — not at emitting a single benchmark-formatted final answer from memory.
- **Edges are load-bearing**: `observe_conversation` and `decompose_and_store` create them automatically. Raw `store_node` calls without follow-up edges produce disconnected nodes with no traversal value.
- **Graph retrieval trades tokens for reasoning context**: factual lookups are often cheaper than chunked RAG; graph-expansion queries intentionally spend more tokens to carry update chains and contradictions.
- **Deduplication recall is conservative (77.3%)**: zero false-positive merges is maintained, but recall will improve in 0.1.8.

For operational details, scaling considerations, tool-level behavior, and the full MCP feature surface, see [docs/reference.md](./docs/reference.md).

---

## Contributing

PRs and issues are welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md).

---

## Reference & Docs

Detailed reference material lives in external documentation:

- **[docs/reference.md](./docs/reference.md)**: Environment variables, admin commands, Docker setup, and full tool surface.
- **[deploy/kubernetes/README.md](./deploy/kubernetes/README.md)**: Production deployment.
- **[docs/runbooks/](./docs/runbooks/)**: Operations and troubleshooting.
- **[tests/artifacts/README.md](./tests/artifacts/README.md)**: Benchmark artifacts and traceability.

---

## License

MIT — see [LICENSE](./LICENSE).
