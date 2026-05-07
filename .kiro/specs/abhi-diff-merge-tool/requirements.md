# Requirements Document

## Introduction

Waggle's `.abhi` format is a portable, deterministic memory graph snapshot. The project already has skeleton `diff` and `merge` commands, but they are shallow: diff returns only node/edge IDs with no content detail, conflict reporting is a bare string `"Conflict on {id}"`, and there is no merge preview, no conflict resolution workflow, and no boundary enforcement for dangling `RELATES_TO` edges.

This feature hardens the `.abhi` diff/merge tool into a first-class, production-quality workflow. It covers four core areas plus supporting concerns:

1. **Rich field-level diff** — show exactly what changed in each node and edge, not just which IDs changed.
2. **Structured conflict detection and reporting** — surface conflicts with full before/after values so a human or agent can make an informed resolution decision.
3. **Merge preview and conflict resolution** — allow a dry-run preview before writing output, and support per-conflict resolution choices.
4. **Export boundary enforcement** — detect and report edges whose target nodes are absent from the export, preventing silent dangling references.
5. **Performance contracts** — diff and merge must complete within defined time bounds for graphs up to 1000 nodes.
6. **Schema versioning interaction** — define behavior when diff/merge is called across schema versions.
7. **Identity matching** — specify the algorithm for determining whether two nodes/edges are the same, modified, or distinct.
8. **Test corpus** — a checked-in set of canonical `.abhi` fixtures that all diff/merge tests run against.
9. **CLI ↔ MCP equivalence** — every CLI operation has an equivalent MCP tool with identical semantics.

## Glossary

- **Diff_Tool**: The component responsible for comparing two `.abhi` documents and producing a structured diff result.
- **Merge_Tool**: The component responsible for combining a base, left, and right `.abhi` document into a merged output document.
- **Two_Way_Diff**: A diff between two documents (left, right) with no base. Produces additions, removals, and modifications only — no conflict concept applies.
- **Three_Way_Diff**: A diff among three documents (base, left, right). Identifies which changes came from left, which from right, and which fields both sides changed (conflicts).
- **Conflict**: A situation where both the left and right documents diverge from the base on the same node or edge field. Only meaningful in a Three_Way_Diff.
- **Field_Delta**: A record of a single field change: the field name, the old value, and the new value.
- **Conflict_Record**: A structured object describing a conflict: the object ID, the conflicting field, the base value, the left value, and the right value.
- **Merge_Preview**: A dry-run merge result that reports what the merged document would contain and which conflicts exist, without writing an output file.
- **Dangling_Edge**: An edge whose `source_id` or `target_id` references a node ID that is not present in the same `.abhi` document. Aliases and other string-valued fields are NOT considered dangling — only edges that reference node IDs by UUID are subject to boundary enforcement.
- **Export_Boundary**: The set of node IDs present in a given `.abhi` document.
- **Abhi_Document**: A parsed, in-memory representation of a `.abhi` file (v1 JSON or v2 ZIP format).
- **Schema_Version**: The `integrity.abhi_spec_version` (v1) or `manifest.schema_version` (v2) field that identifies the format version of an `.abhi` document.
- **Round_Trip**: The property that export → import → export of the same logical graph produces a byte-identical `.abhi` archive.
- **Content_Hash**: The `sha256` hash stored in `integrity.content_hash`, computed from the document's nodes, edges, and transcripts.
- **DIFFED_FIELDS**: The set of node fields included in field-level diff comparison: `{ label, content, node_type, tags, valid_from, valid_to, aliases, metadata.* }`.
- **IGNORED_FIELDS**: The set of node fields excluded from field-level diff comparison: `{ updated_at, access_count, embedding, embedding_model_id }`.
- **Contradict_Strategy**: A merge strategy that retains both conflicting values by creating a `CONTRADICTS` edge between the two nodes, rather than selecting one value.
- **Merge_Strategy_Config**: A per-user YAML file at `~/.waggle/merge-strategies.yaml` that specifies per-field or per-node-type strategy overrides.

---

## Requirements

### Requirement 1: Field-Level Diff Output

**User Story:** As a developer reviewing two memory snapshots, I want to see exactly which fields changed on each node and edge, so that I can understand what the AI remembered differently between two sessions without manually inspecting raw JSON.

#### Acceptance Criteria

1. WHEN `diff(left, right)` is called with two valid `.abhi` documents and no base document, THE Diff_Tool SHALL perform a Two_Way_Diff and SHALL produce a `FieldLevelDiffResult` containing `nodes_added`, `nodes_removed`, and `nodes_modified` — with no `conflict_records` field, because the two-way mode has no conflict concept.
2. WHEN `diff(base, left, right)` is called with three valid `.abhi` documents, THE Diff_Tool SHALL perform a Three_Way_Diff and SHALL produce a `FieldLevelDiffResult` that additionally includes `conflict_records` for any field where both left and right diverge from base.
3. WHEN a node is present in document A but absent in document B, THE Diff_Tool SHALL include that node ID in `nodes_removed` with a `Field_Delta` representing the full node content as the old value and `null` as the new value.
4. WHEN a node is present in document B but absent in document A, THE Diff_Tool SHALL include that node ID in `nodes_added` with a `Field_Delta` representing `null` as the old value and the full node content as the new value.
5. THE Diff_Tool SHALL compare only DIFFED_FIELDS (`label`, `content`, `node_type`, `tags`, `valid_from`, `valid_to`, `aliases`, `metadata.*`) when computing per-node field deltas.
6. THE Diff_Tool SHALL exclude IGNORED_FIELDS (`updated_at`, `access_count`, `embedding`, `embedding_model_id`) from all field delta comparisons, so that operational noise does not appear in diff output.
7. THE Diff_Tool SHALL include `relationship`, `weight`, `source_id`, `target_id`, and `metadata` fields in the per-edge delta comparison.
8. WHEN the two documents have different `Schema_Version` values, THE Diff_Tool SHALL include a `schema_version_mismatch` warning in the result and SHALL still produce a best-effort diff.
9. THE Diff_Tool SHALL ignore the `export_context` field and embedding vectors when computing field deltas, because those fields are intentionally outside the portable hash.

### Requirement 2: Structured Conflict Detection and Merge Strategies

**User Story:** As a developer merging two divergent memory graphs, I want to see structured conflict records with before/after values for each conflicting field and control how conflicts are resolved, so that I can decide which version to keep without guessing.

#### Acceptance Criteria

1. WHEN `merge(base, left, right)` detects that both left and right diverge from base on the same field of the same node or edge, THE Merge_Tool SHALL produce a `Conflict_Record` containing: the object ID, the object type (`node` or `edge`), the field name, the base value, the left value, and the right value.
2. THE Merge_Tool SHALL produce one `Conflict_Record` per conflicting field, not one record per conflicting object.
3. WHEN `--strategy=ours` (`prefer_left`) is specified, THE Merge_Tool SHALL resolve each conflict by selecting the left value and SHALL record the resolution in the `Conflict_Record` as `resolved_by: "prefer_left"`.
4. WHEN `--strategy=theirs` (`prefer_right`) is specified, THE Merge_Tool SHALL resolve each conflict by selecting the right value and SHALL record the resolution in the `Conflict_Record` as `resolved_by: "prefer_right"`.
5. WHEN `--strategy=newer` (`last_write_wins`) is specified, THE Merge_Tool SHALL resolve each conflict by selecting the value with the later `updated_at` timestamp and SHALL record the resolution in the `Conflict_Record` as `resolved_by: "last_write_wins"`.
6. WHEN two conflicting values have identical `updated_at` timestamps under `--strategy=newer`, THE Merge_Tool SHALL select the right value and SHALL record the resolution as `resolved_by: "last_write_wins_tie_right"`.
7. WHEN `--strategy=contradict` is specified or no strategy flag is provided (default), THE Merge_Tool SHALL retain both conflicting node values by creating a `CONTRADICTS` edge between the two nodes and SHALL record the resolution in the `Conflict_Record` as `resolved_by: "contradict"`.
8. WHEN a `Merge_Strategy_Config` file exists at `~/.waggle/merge-strategies.yaml`, THE Merge_Tool SHALL apply per-field or per-node-type strategy overrides from that file, taking precedence over the global `--strategy` flag for the fields and types specified.
9. THE `waggle resolve <conflict-id>` command SHALL allow post-merge resolution of a single conflict by ID, updating the merged document in place.
10. THE `AbhiMergeResult` model SHALL expose `conflict_records` as a list of `Conflict_Record` objects in addition to the existing `conflicts` string list, which SHALL be retained for backward compatibility.
11. Interactive per-conflict resolution (TUI, side-by-side diff, save-and-resume) is explicitly OUT OF SCOPE for this requirement and SHALL be addressed in a separate spec.

### Requirement 3: Merge Preview (Dry Run) and Exit Codes

**User Story:** As a developer or AI agent, I want to preview what a merge would produce before writing the output file, and receive a machine-readable exit code that distinguishes clean merges from conflicts from impossible merges, so that I can integrate `waggle merge` into CI/CD pipelines reliably.

#### Acceptance Criteria

1. WHEN `merge_abhi_files` is called with `dry_run=True`, THE Merge_Tool SHALL compute the full merge result including conflict detection and SHALL return an `AbhiMergeResult` with `dry_run=True` and `output_path=""`.
2. WHEN `dry_run=True`, THE Merge_Tool SHALL NOT write any file to disk.
3. WHEN `dry_run=True`, THE Merge_Tool SHALL populate `nodes_merged`, `edges_merged`, `conflicts`, and `conflict_records` in the result as if the merge had been written.
4. THE `merge_abhi` MCP tool SHALL accept an optional `dry_run` boolean parameter, defaulting to `False`.
5. THE `waggle merge` CLI command SHALL accept an optional `--dry-run` flag that sets `dry_run=True`.
6. THE Merge_Tool SHALL exit with the following codes:
   - **0** — clean merge completed, no conflicts present
   - **1** — merge completed with unresolved conflicts (resolvable via strategy flag or `waggle resolve`)
   - **2** — merge impossible (e.g., schema version mismatch that cannot be reconciled, corrupt input, hash verification failure)
   - **3** — operation aborted (e.g., user cancelled; reserved for future interactive mode)
7. WHEN `--dry-run` is passed to the CLI, THE Merge_Tool SHALL print the merge preview to stdout and SHALL exit with code 0 if there are no conflicts, or code 1 if there are unresolved conflicts.

### Requirement 4: Export Boundary Enforcement for Dangling Edges

**User Story:** As a developer importing a `.abhi` file, I want strict validation of dangling edges by default so that corrupt or incomplete graphs are rejected at the boundary, with explicit opt-out flags when I know the file is safe to import as-is.

#### Acceptance Criteria

1. WHEN `validate_abhi_document` is called on a document that contains an edge whose `source_id` or `target_id` is not present in the document's node set, THE Diff_Tool SHALL include that edge ID in a `dangling_edges` list in the validation result.
2. WHEN `export_abhi` produces a document, THE Diff_Tool SHALL run boundary validation and SHALL include a `dangling_edge_count` field in the `AbhiExportResult`.
3. IF a document contains one or more `Dangling_Edge` records, THEN THE Diff_Tool SHALL include a `boundary_warning` string in the validation result describing the count and the affected edge IDs.
4. THE boundary validation SHALL treat all edge types with equal scrutiny — no edge type is exempt from boundary enforcement.
5. THE boundary validation SHALL NOT flag aliases or other string-valued fields as dangling, because aliases are self-contained strings and not node references.

**Default import behavior (strict-by-default):**

6. WHEN `import_abhi` is called on a document with dangling edges and no opt-out flag is provided, THE Merge_Tool SHALL reject the import and SHALL return a validation error listing the dangling edge IDs.
7. WHEN `import_abhi` is called on a document with a `Schema_Version` that does not match the current runtime version and no opt-out flag is provided, THE Merge_Tool SHALL reject the import and SHALL return a schema version mismatch error.
8. WHEN `import_abhi` is called on a document whose `Content_Hash` does not match the recomputed hash and no opt-out flag is provided, THE Merge_Tool SHALL reject the import and SHALL return a hash verification failure error.

**Opt-out flags:**

9. WHEN `import_abhi` is called with `--allow-dangling`, THE Merge_Tool SHALL drop dangling edges, log a warning listing the dropped edge IDs, and continue with the import.
10. WHEN `import_abhi` is called with `--skip-verify`, THE Merge_Tool SHALL skip hash verification and SHALL log a warning that hash verification was bypassed.
11. WHEN `import_abhi` is called with `--force`, THE Merge_Tool SHALL apply the behavior of both `--allow-dangling` and `--skip-verify` simultaneously.

**Default export behavior (advisory):**

12. WHEN `export_abhi` produces a document that contains dangling edges in the source graph, THE Diff_Tool SHALL log a warning listing the dangling edge IDs and SHALL allow the export to proceed.
13. WHEN `export_abhi` is called with `--include-deps`, THE Diff_Tool SHALL walk all dangling edge targets and SHALL include the referenced nodes in the export.
14. WHEN `export_abhi` is called with `--strict-export`, THE Diff_Tool SHALL refuse to export if any dangling edges exist and SHALL return a validation error.

### Requirement 5: Round-Trip Integrity After Merge

**User Story:** As a developer, I want the merged `.abhi` file to satisfy the same round-trip guarantee as a normal export, so that I can trust the merged artifact is a valid, portable memory snapshot.

#### Acceptance Criteria

1. FOR ALL valid merged `.abhi` documents produced by `merge_abhi_files`, THE Merge_Tool SHALL produce a document whose `integrity.content_hash` matches the hash recomputed from the document's nodes, edges, and transcripts.
2. WHEN a merged document is exported and then re-imported and re-exported, THE Merge_Tool SHALL produce a byte-identical archive (Round_Trip property).
3. WHEN `merge_abhi_files` completes, THE Merge_Tool SHALL verify the `Content_Hash` of the output document and SHALL include `hash_verified: true` in the `AbhiMergeResult` if verification passes.
4. IF the post-merge hash verification fails, THEN THE Merge_Tool SHALL return an error and SHALL NOT write the output file.

### Requirement 6: Human-Readable CLI and MCP Output

**User Story:** As a developer using the CLI or an AI agent using the MCP tool, I want diff and merge results presented in a readable format that highlights what changed and what conflicted, so that I can act on the information without parsing raw JSON.

#### Acceptance Criteria

1. WHEN `serialize_abhi_diff` formats a `FieldLevelDiffResult`, THE Diff_Tool SHALL include, for each updated node, the node label or content prefix and the list of changed fields with old → new values.
2. WHEN `serialize_abhi_merge` formats an `AbhiMergeResult` that contains conflicts, THE Merge_Tool SHALL include, for each `Conflict_Record`, the object ID, the field name, the left value, the right value, and the resolution applied.
3. THE serialized diff output SHALL be no longer than 4000 characters when the diff contains 50 or fewer changed objects; for larger diffs, THE Diff_Tool SHALL truncate the per-field detail and SHALL append a summary line stating the total number of omitted changes.
4. WHEN `diff_abhi` is called as an MCP tool, THE Diff_Tool SHALL return both the human-readable string and the structured `FieldLevelDiffResult` as `structuredContent`.
5. WHEN `merge_abhi` is called as an MCP tool, THE Merge_Tool SHALL return both the human-readable string and the structured `AbhiMergeResult` as `structuredContent`.

**Output format precedence:**

6. WHEN `--format=human` is specified or no `--format` flag is provided (default), THE Diff_Tool and Merge_Tool SHALL produce colored, indented, git-diff-style output.
7. WHEN `--format=json` is specified, THE Diff_Tool and Merge_Tool SHALL produce structured JSON output suitable for piping and machine consumption.
8. THE `--format=patch` value is reserved for future use (`waggle apply`) and SHALL NOT be implemented in v1; WHEN `--format=patch` is specified in v1, THE Diff_Tool SHALL return an error stating the format is not yet supported.

### Requirement 7: Performance Contracts

**User Story:** As a developer running diff and merge in CI/CD pipelines or interactive workflows, I want guaranteed time bounds on diff and merge operations so that the tool does not become a bottleneck for large memory graphs.

#### Acceptance Criteria

1. WHEN `diff(left, right)` or `diff(base, left, right)` is called on two or three `.abhi` documents each containing up to 1000 nodes and up to 5000 edges, THE Diff_Tool SHALL complete and return a result within 500 milliseconds on reference hardware (a single-core 2GHz CPU with 1GB available RAM).
2. WHEN `merge(base, left, right)` is called on three `.abhi` documents each containing up to 1000 nodes and up to 5000 edges, THE Merge_Tool SHALL complete and write the output document within 2 seconds on reference hardware.
3. THE performance benchmarks in Requirement 7 SHALL be validated by automated tests in the test corpus (see Requirement 10) using the `linear-history.abhi` and `branched.abhi` fixtures scaled to 1000 nodes.
4. IF a diff or merge operation exceeds its time bound, THEN THE Diff_Tool or Merge_Tool SHALL log a performance warning including the elapsed time and the node/edge counts of the input documents.

### Requirement 8: Schema Versioning Interaction

**User Story:** As a developer working with memory graphs across schema versions, I want clear, predictable behavior when diff or merge is called on documents with different schema versions, so that I am never silently operating on incompatible data.

#### Acceptance Criteria

1. WHEN `diff` or `merge` is called on documents that share the same `Schema_Version`, THE Diff_Tool and Merge_Tool SHALL proceed normally without any schema-related warnings.
2. WHEN `diff` is called on documents with different `Schema_Version` values, THE Diff_Tool SHALL refuse to proceed and SHALL return a schema version mismatch error identifying the versions of each input document.
3. WHEN `merge` is called on documents with different `Schema_Version` values, THE Merge_Tool SHALL refuse to proceed and SHALL return a schema version mismatch error with exit code 2.
4. THE `waggle upgrade <file> --to <version>` command SHALL promote a `.abhi` document from an older schema version to a newer one, producing a new file with the upgraded schema version and a recomputed `Content_Hash`.
5. WHEN `waggle upgrade` is called on a document that is already at the target version, THE Merge_Tool SHALL return without modifying the file and SHALL log an informational message stating the file is already at the target version.
6. WHEN cross-version diff is required, THE Diff_Tool SHALL require the caller to first run `waggle upgrade` on the older document before calling `diff`; THE Diff_Tool SHALL include the upgrade command in the error message to guide the user.
7. THE schema versioning behavior described in this requirement addresses concern #17 from AGENTS.md.

### Requirement 9: Identity Matching Algorithm

**User Story:** As a developer running diff or merge, I want a well-defined algorithm for determining whether two nodes are the same, modified, or entirely separate, so that the diff output is predictable and does not conflate deduplication with modification detection.

#### Acceptance Criteria

1. WHEN two nodes in the left and right documents share the same `id` and the same `content_hash`, THE Diff_Tool SHALL classify them as **identical** and SHALL produce no `Field_Delta` for those nodes.
2. WHEN two nodes share the same `id` but have different `content_hash` values, THE Diff_Tool SHALL classify them as **modification candidates** and SHALL produce `Field_Delta` records for each DIFFED_FIELD that differs between them.
3. WHEN two nodes have different `id` values but identical content, THE Diff_Tool SHALL treat them as **separate nodes** — one added, one removed — and SHALL NOT attempt to merge or deduplicate them; deduplication is a separate concern outside the scope of diff/merge.
4. WHEN two edges share the same `id` and the same `source_id`, `target_id`, and `relationship` values, THE Diff_Tool SHALL classify them as **identical** and SHALL produce no `Field_Delta` for those edges.
5. WHEN two edges share the same `id` but differ on any of `source_id`, `target_id`, `relationship`, or `weight`, THE Diff_Tool SHALL classify them as **modification candidates** and SHALL produce `Field_Delta` records for each differing field.
6. THE identity matching algorithm SHALL be documented in the codebase as a standalone function with explicit inputs, outputs, and the three classification outcomes: `identical`, `modified`, `separate`.

### Requirement 10: Test Corpus

**User Story:** As a developer maintaining the diff/merge tool, I want a checked-in set of canonical `.abhi` fixture files that represent known scenarios, so that all diff/merge tests run against a stable, reproducible baseline and regressions are caught immediately.

#### Acceptance Criteria

1. THE repository SHALL contain a `tests/fixtures/abhi/` directory with the following canonical `.abhi` files:
   - `empty.abhi` — a valid document with zero nodes and zero edges
   - `single-node.abhi` — a valid document with exactly one node and zero edges
   - `linear-history.abhi` — a document representing a linear sequence of node additions
   - `branched.abhi` — a document representing a graph with branching structure
   - `with-contradictions.abhi` — a document containing at least one `CONTRADICTS` edge
   - `with-dangling-edges.abhi` — a document containing at least one edge whose target node is absent
2. WHEN a new bug is found in the diff/merge tool that is caused by a specific graph structure, THE development team SHALL add a new fixture file to `tests/fixtures/abhi/` representing that structure before closing the bug.
3. THE diff/merge test suite SHALL include at least one test case per fixture file that exercises the primary diff and merge code paths against that fixture.
4. ALL fixture files SHALL be valid `.abhi` documents that pass schema validation, with the exception of `with-dangling-edges.abhi`, which is intentionally invalid and SHALL be documented as such.
5. THE fixture files SHALL be version-controlled and SHALL NOT be generated at test time; they are static, human-reviewable artifacts.

### Requirement 11: CLI ↔ MCP Equivalence Guarantee

**User Story:** As an AI agent or developer using either the CLI or the MCP interface, I want every diff/merge/resolve operation to be available through both interfaces with identical semantics, so that I can switch between human-driven and agent-driven workflows without behavioral differences.

#### Acceptance Criteria

1. FOR EACH CLI command in `{ waggle diff, waggle merge, waggle resolve }`, THE system SHALL provide an equivalent MCP tool with the same input parameters, the same validation rules, and the same output semantics.
2. WHEN a `waggle diff` CLI call and an equivalent `diff_abhi` MCP tool call are given identical inputs, THE Diff_Tool SHALL produce logically equivalent results regardless of which interface was used.
3. WHEN a `waggle merge` CLI call and an equivalent `merge_abhi` MCP tool call are given identical inputs, THE Merge_Tool SHALL produce logically equivalent results regardless of which interface was used.
4. WHEN a `waggle resolve` CLI call and an equivalent `resolve_conflict` MCP tool call are given identical inputs, THE Merge_Tool SHALL produce logically equivalent results regardless of which interface was used.
5. WHEN called via MCP, THE Diff_Tool and Merge_Tool SHALL return machine-readable JSON as the primary output format.
6. WHEN called via CLI with `--format=human` (default), THE Diff_Tool and Merge_Tool SHALL return human-readable, colored output as the primary output format.
7. THE MCP tool definitions SHALL be kept in sync with the CLI command definitions; WHEN a new flag is added to a CLI command, THE corresponding MCP tool SHALL be updated in the same pull request.
