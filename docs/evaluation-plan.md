# Implementation Plan: Dedup → Context Assembly → Corpus Hardening

> **Execute in sequence.** Each phase changes the numbers the next phase measures.
> Do not run phases in parallel.

```
Phase 1: Dedup improvement       → fixes product-blocking bug; changes graph density
Phase 2: Context assembly depth  → changes what Waggle returns per query; depends on cleaner graph
Phase 3: Corpus hardening        → re-measures everything; must run against the improved system
```

**Estimated total:** 2–3 focused weeks.

---

## Phase 1 — Dedup Improvement

**Current state:** 12/22 = 55% at threshold 0.82. fp=0 across all thresholds.
Remaining 10 false-negatives are pure-paraphrase pairs with no entity anchor and other edge cases.

**Target:** ≥90% on the existing 22-pair fixture without regressing false-friend rejection (maintain fp=0).

### Step 1A — Expand the fixture set

22 pairs is enough to develop against but marginal for validation.

- Add 8–10 more pairs targeting the specific remaining failure modes:
  - Pure-paraphrase true-dups with no entity anchor ("user prefers dark mode" / "user wants dark mode UI")
  - Cross-type pairs (decision vs fact about the same topic)
  - Temporal near-duplicates ("chose PostgreSQL in April" vs "still using PostgreSQL in May" — these should **not** merge)
- **Target: 30–35 pairs minimum before declaring victory**

### Step 1B — Paraphrase detection pre-filter

The 4 remaining false-negatives are pairs where:
- No named entity exists to anchor the merge
- Cosine similarity is below threshold despite semantic equivalence

Implement a lightweight paraphrase pre-score on top of the existing Jaccard path:

```python
# Proposed scoring path for entity-less pairs
paraphrase_score = 0.6 * cosine_similarity + 0.4 * jaccard_overlap
if paraphrase_score >= ENTITY_LESS_THRESHOLD:  # tune on fixture set, start at 0.72
    return existing_node, "paraphrase_merge", paraphrase_score
```

This is distinct from the existing Jaccard-boosted path (which requires jaccard ≥ 0.35 first). The combined score catches cases where neither metric alone is strong enough.

**Tune on fixture set. Commit threshold values with the fixture results that justify them.**

### Step 1C — Measure and ship

- Run full dedup benchmark: report old accuracy vs new accuracy on the same fixture set
- If ≥90%: update product default threshold, merge to main
- If <90%: document what's still failing, decide whether to pursue bi-encoder fine-tuning

**Exit criteria:**
- [ ] Dedup accuracy ≥90% on ≥30 fixture pairs
- [ ] fp=0 maintained (no false-friend regressions)
- [ ] Product default threshold updated with fixture-backed justification
- [ ] README dedup collapsible updated with new numbers

---

## Phase 2 — Context Assembly Depth

**Current state:** 91% Hit@k, 74% exact support. Worst case: `cross_scenario_synthesis` — 8/8 hit, 1/8 exact.  
Waggle finds the right topic but doesn't assemble enough surrounding context.

**Target:** Exact support ≥85% on current 66-query corpus; mean tokens stay under 55 (preserving ≥2.5× advantage over RAG).

### Step 2A — Diagnose the failure pattern first

Before writing a line of code, manually inspect every case where Hit@k = true but exact support = false.

For each failing query, record:
- Which node was retrieved (the "hit")
- What supporting information was missing
- Where that missing info lives in the graph (adjacent node? two hops away? different session?)
- Whether the missing info is connected by a typed edge to the retrieved node

**Deliverable:** `docs/context_assembly_failure_analysis.json` — feed this into Step 2B.

### Step 2B — 1-hop subgraph expansion

Based on the expected diagnosis (missing adjacent context), add selective expansion:

```
query_graph("database decision")
  → finds node: "Chose PostgreSQL over MySQL"
  → current:  returns just that node
  → proposed: also returns nodes connected by [depends_on, updates, contradicts, derived_from] (1 hop)
  → skip:     relates_to, similar_to (too broad — would blow up token count)
```

**Implementation:**
- Add `expand_depth` parameter to `query_graph` (default: `0` for backward compatibility)
- When `expand_depth=1`: for each retrieved node, fetch edges of types `[depends_on, updates, contradicts, derived_from]` and include connected nodes
- Deduplicate expanded results
- Cap total returned nodes at `top_k * 2`

**Hard constraint:** If expansion pushes mean tokens above 60, the 4× claim erodes to ~2.5×. That's still defensible but the README table must be updated honestly.

### Step 2C — Selective expansion based on query type

Not every query needs expansion. Auto-trigger expansion only when the query contains relational or temporal signals:

```python
EXPANSION_TRIGGERS = [
    "how did * affect",
    "what changed",
    "relationship between",
    "connected to",
    "depends on",
    "because of",
    "before and after",
    "why did we",
]
```

Start simple: literal substring matching. Refine later if needed.

### Step 2D — Measure and validate

- Re-run comparative benchmark with expansion enabled
- Report exact support improvement per task family
- Verify token cost stays within the "~4×" claim range
- If token cost increases significantly: report the new ratio honestly and update all tables

**Exit criteria:**
- [ ] Exact support ≥85% on current 66-query corpus
- [ ] Mean tokens ≤55 (preserving ≥2.5× advantage)
- [ ] `cross_scenario_synthesis` exact support improves from 1/8
- [ ] No regression on Hit@k
- [ ] Token efficiency table in README updated

---

## Phase 3 — Corpus Hardening

**Current state:** RAG baseline hits 100% Hit@k on all 7 families.  
Root cause: at `top_k=5` every fact is independently retrievable from its own session chunk.

**Target:** At least 2 task families where naive RAG Hit@k drops below 90%, creating real separation between systems.

### Step 3A — New query types that break chunk boundaries

The structural weakness of chunked RAG: if the answer requires combining information from two different sessions (stored in different chunks), a single-chunk retrieval can't surface both pieces. Design queries that exploit this:

| Query type | Example | Why RAG struggles |
|---|---|---|
| **Cross-session synthesis** | "What's the relationship between the auth timeout fix and the deployment delay?" | Auth info in session 3 chunk, deploy info in session 7 — need both simultaneously |
| **Contradiction resolution** | "What's our current position on MySQL?" | Two chunks say opposite things; RAG returns both without resolving |
| **Temporal sequence** | "List the database decisions in chronological order" | Facts spread across many chunks; RAG returns them unordered |
| **Negation queries** | "What did we decide NOT to do?" | Negations semantically similar to assertions; cosine can't distinguish |
| **Implicit reference** | "That thing from the security review" | Vague query; graph edges provide disambiguation that raw chunks can't |

### Step 3B — Expand scenario count and depth

Current: 24 scenarios, ~2–3 turns each.  
**Target: 35–40 scenarios** with:
- ≥10 scenarios with 5+ turns (deeper conversation history)
- ≥8 scenarios that reference facts from other scenarios (cross-scenario dependencies)
- ≥5 scenarios with explicit contradictions or reversals
- Facts that are only inferrable by combining nodes from 2+ sessions

### Step 3C — Expand query count per family

| Task family | Current n | Target n | Priority |
|---|---|---|---|
| `factual_recall` | 18 | 18 | Sufficient — both systems ace it |
| `temporal_original` | 19 | 19 | Sufficient |
| `multi_session_change` | 11 | 15 | Add reversals and updates |
| `cross_scenario_synthesis` | 8 | 15 | **Priority** — where Waggle should win |
| `decision_delta` | 4 | 10 | Too small; raise to be statistically meaningful |
| `adversarial_paraphrase` | 4 | 10 | Too small |
| `temporal_latest` | 2 | 8 | Far too small |
| **`negation`** (new) | 0 | 6 | "What did we reject?" queries |
| **`implicit_reference`** (new) | 0 | 6 | Vague, context-dependent queries |
| **Total** | **66** | **~107** | |

### Step 3D — Validate that RAG actually drops (do this first)

After building the new corpus, **run naive RAG baseline first**, before running Waggle.

If RAG still hits ≥95% overall, the corpus isn't hard enough yet. Iterate on scenarios until RAG drops before investing time in a full comparison. This is the validation gate.

### Step 3E — Full comparison run and reporting

- Run Waggle + rag_naive on the hardened corpus
- Produce new `tests/artifacts/benchmark_current.json`
- Update all README tables; preserve old corpus as `benchmarks/fixtures/v1/` for historical comparison
- If Waggle beats RAG on Hit@k in any family: report it clearly, label it as the hardened-corpus result
- If Waggle still trails: report honestly and identify next architectural gap

**Exit criteria:**
- [ ] Corpus has ≥100 queries across ≥8 task families (all families n≥6)
- [ ] Naive RAG Hit@k drops below 95% overall
- [ ] At least 2 task families show meaningful Waggle advantage on Hit@k
- [ ] `benchmark_current.json` regenerated; old corpus preserved as `v1`
- [ ] README tables fully updated

---

## Dependency chain

```
Phase 1 (Dedup)
  └─ cleaner graph
       └─ Phase 2 (Context Assembly)
            └─ better exact support numbers
                 └─ Phase 3 (Corpus Hardening)
                      └─ stress test on both improvements
                           └─ README update with final revised benchmarks
```

## What not to do during these phases

- **No MCP tool surface changes.** No new tools, no API changes.
- **No extraction accuracy work.** 75% LLM extraction is not the bottleneck.
- **No new features.** No new node types, no new edge types.
- **No new README claims until Phase 3 numbers are in.** The README is honest as-is.

---

## Checklist

### Phase 1 — Dedup
- [ ] 1A: Expand fixture set to ≥30 pairs (add paraphrase + temporal near-dup + cross-type pairs)
- [ ] 1B: Implement paraphrase pre-score for entity-less pairs; tune threshold on fixture
- [ ] 1C: Run dedup benchmark; achieve ≥90%; update product default; update README

### Phase 2 — Context Assembly
- [ ] 2A: Failure analysis — inspect all Hit@k=true / exact=false cases; write analysis JSON
- [ ] 2B: Implement `expand_depth=1` in `query_graph`; expansion over typed edges only
- [ ] 2C: Add query-type trigger for selective expansion
- [ ] 2D: Re-run benchmark; verify exact support ≥85% and mean tokens ≤55; update README

### Phase 3 — Corpus Hardening
- [ ] 3A: Add 5 new query types (cross-session synthesis, contradiction, temporal sequence, negation, implicit reference)
- [ ] 3B: Expand to 35–40 scenarios with deeper histories and cross-scenario dependencies
- [ ] 3C: Expand per-family counts to targets in table above (total ≥107 queries)
- [ ] 3D: Run RAG baseline first; confirm it drops below 95% before running Waggle
- [ ] 3E: Full comparison run; update all tables; preserve v1 corpus