# Waggle Comparative Evaluation

- Scenarios: 24
- Queries: 66
- Task families: adversarial_paraphrase, cross_scenario_synthesis, decision_delta, factual_recall, multi_session_change, temporal_latest, temporal_original

| System | Hit@k | Exact support | Mean tokens | Median tokens | p95 tokens |
|--------|-------|---------------|-------------|---------------|------------|
| waggle | 91% | 74% | 36.9 | 37.0 | 42.0 |
| rag_naive | 100% | 100% | 152.8 | 155.0 | 162.8 |

## Failure Protocol

- If Waggle token reduction is under 15 percent, inspect whether graph serialization or context assembly is offsetting compression gains.
- If the tuned baseline matches Waggle on retrieval quality, frame the result as efficiency and structure first rather than retrieval superiority.
- If temporal queries do not separate systems, audit whether the corpus actually requires temporal reasoning before expanding claims.
- If multi-session change queries are inconclusive, expand that slice before broadening the whole pilot corpus.
