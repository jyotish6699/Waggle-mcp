# Design Document: `.abhi` Diff/Merge Tool

## Overview

This document describes the design for hardening the `.abhi` diff/merge tool from its current shallow skeleton into a production-quality workflow. The existing implementation in `src/waggle/abhi.py` provides `diff_abhi_documents` and `merge_abhi_documents` functions that operate on node/edge IDs only — they detect which IDs changed but produce no field-level detail, report conflicts as bare strings like `"Conflict on {id}"`, and have no dry-run mode, no boundary enforcement, and no structured conflict resolution.

The design covers four primary areas:

1. **Field-level diff engine** — replace ID-only comparison with per-field deltas restricted to `DIFFED_FIELDS`, with explicit two-way vs. three-way API separation.
2. **Structured conflict detection and merge strategies** — replace string conflicts with typed `MergeConflictRecord` objects and implement four strategies (`ours`, `theirs`, `newer`, `contradict`).
3. **Merge preview and exit codes** — add `dry_run` mode and a well-defined exit code matrix.
4. **Export boundary enforcement** — detect dangling edges at import/export time with strict-by-default import and advisory export.

Supporting concerns include round-trip integrity, human-readable output, performance contracts, schema versioning, identity matching, a test corpus, and CLI ↔ MCP equivalence.


## Architecture

The feature is implemented entirely within `src/waggle/abhi.py` and `src/waggle/models.py`, with CLI surface in `src/waggle/server.py`. No new top-level modules are introduced; instead, the existing functions are replaced or extended in place.

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI / MCP Layer                          │
│  waggle diff  │  waggle merge  │  waggle resolve  │  waggle upgrade │
│  diff_abhi    │  merge_abhi    │  resolve_conflict │               │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                     Public API (abhi.py)                        │
│  diff_abhi_files()   merge_abhi_files()   validate_abhi_document() │
│  upgrade_abhi_document()   resolve_abhi_conflict()              │
└──────────┬────────────────────┬────────────────────┬────────────┘
           │                    │                    │
┌──────────▼──────────┐ ┌───────▼────────┐ ┌────────▼───────────┐
│   Field-Level Diff  │ │  Merge Engine  │ │ Boundary Validator │
│  diff_abhi_documents│ │merge_abhi_docs │ │validate_boundaries │
│  _compute_node_delta│ │_merge_records  │ │_find_dangling_edges│
│  _compute_edge_delta│ │_apply_strategy │ │                    │
│  _classify_identity │ │_contradict_merge│                    │
└──────────┬──────────┘ └───────┬────────┘ └────────────────────┘
           │                    │
┌──────────▼────────────────────▼────────────────────────────────┐
│                     Data Models (models.py)                     │
│  FieldDelta  MergeConflictRecord  FieldLevelDiffResult          │
│  AbhiMergeResult (extended)  AbhiValidationResult (extended)    │
│  MergeStrategyConfig                                            │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

**No new files.** All logic lives in `abhi.py` and `models.py`. This keeps the import graph simple and avoids circular dependencies.

**Backward compatibility.** `AbhiDiffResult` is replaced by `FieldLevelDiffResult` for new callers. `AbhiMergeResult` gains new fields (`conflict_records`, `dry_run`, `hash_verified`) while retaining the existing `conflicts: list[str]` field. Existing callers that only read `conflicts` continue to work.

**Strategy as enum string.** Merge strategies are plain strings (`"prefer_left"`, `"prefer_right"`, `"last_write_wins"`, `"contradict"`) rather than an enum, matching the existing `merge_strategy` parameter convention.

**Dry-run is a parameter, not a subcommand.** `merge_abhi_files(dry_run=True)` skips the `write_abhi_document` call and returns `output_path=""`. This keeps the function signature stable.


## Components and Interfaces

### 1. Field-Level Diff Engine

#### Constants

```python
# src/waggle/abhi.py

DIFFED_FIELDS: frozenset[str] = frozenset({
    "label", "content", "node_type", "tags",
    "valid_from", "valid_to", "aliases", "metadata",
})

IGNORED_FIELDS: frozenset[str] = frozenset({
    "updated_at", "access_count", "embedding_b64",
    "embedding_model_id", "embedding_dim",
})

EDGE_DIFFED_FIELDS: frozenset[str] = frozenset({
    "relationship", "weight", "source_id", "target_id", "metadata",
})
```

#### Identity Classification

```python
def _classify_node_identity(
    node_a: dict[str, Any],
    node_b: dict[str, Any],
) -> Literal["identical", "modified", "separate"]:
    """
    Classify the relationship between two nodes.

    Returns:
        "identical"  — same id, same content hash (no DIFFED_FIELDS differ)
        "modified"   — same id, at least one DIFFED_FIELD differs
        "separate"   — different ids (treated as add + remove, never merged)
    """
```

```python
def _classify_edge_identity(
    edge_a: dict[str, Any],
    edge_b: dict[str, Any],
) -> Literal["identical", "modified", "separate"]:
    """
    Classify the relationship between two edges.

    Returns:
        "identical"  — same id, same source_id/target_id/relationship
        "modified"   — same id, at least one EDGE_DIFFED_FIELD differs
        "separate"   — different ids
    """
```

#### Field Delta Computation

```python
def _compute_node_delta(
    node_a: dict[str, Any] | None,
    node_b: dict[str, Any] | None,
) -> list[FieldDelta]:
    """
    Compute per-field deltas for a node pair.
    node_a=None means the node was added; node_b=None means it was removed.
    Only DIFFED_FIELDS are compared. IGNORED_FIELDS are skipped.
    metadata.* sub-keys are each compared individually.
    """
```

```python
def _compute_edge_delta(
    edge_a: dict[str, Any] | None,
    edge_b: dict[str, Any] | None,
) -> list[FieldDelta]:
    """
    Compute per-field deltas for an edge pair.
    Only EDGE_DIFFED_FIELDS are compared.
    """
```

#### Two-Way and Three-Way Diff

```python
def diff_abhi_documents(
    document_a: dict[str, Any],
    document_b: dict[str, Any],
    *,
    input_path_a: str | Path,
    input_path_b: str | Path,
    base_document: dict[str, Any] | None = None,
) -> FieldLevelDiffResult:
    """
    Two-way diff when base_document is None.
    Three-way diff when base_document is provided.

    Schema version mismatch: if document_a and document_b have different
    schema_version values, a schema_version_mismatch warning is added to
    the result and a best-effort diff is still produced.

    Cross-version diff (different major versions): raises SchemaVersionError
    with a message directing the caller to run `waggle upgrade` first.
    """
```

```python
def diff_abhi_files(
    *,
    input_path_a: str | Path,
    input_path_b: str | Path,
    input_path_base: str | Path | None = None,
    passphrase: str = "",
) -> FieldLevelDiffResult:
    """File-level wrapper. Loads documents then calls diff_abhi_documents."""
```

### 2. Merge Engine

#### Strategy Application

```python
def _apply_merge_strategy(
    item_id: str,
    object_type: Literal["node", "edge"],
    base_item: dict[str, Any] | None,
    left_item: dict[str, Any],
    right_item: dict[str, Any],
    *,
    strategy: str,
    strategy_config: MergeStrategyConfig | None,
    conflict_records: list[MergeConflictRecord],
) -> dict[str, Any]:
    """
    Apply the merge strategy to a single conflicting item.

    For each field that both left and right changed relative to base,
    a MergeConflictRecord is appended to conflict_records.

    Strategy dispatch:
        "prefer_left"     → select left value for all conflicts
        "prefer_right"    → select right value for all conflicts
        "last_write_wins" → select value with later updated_at; tie → right
        "contradict"      → select right value AND schedule a CONTRADICTS edge

    strategy_config overrides take precedence over the global strategy
    for the specific fields/node_types they cover.
    """
```

```python
def _merge_records(
    base_items: list[dict[str, Any]],
    left_items: list[dict[str, Any]],
    right_items: list[dict[str, Any]],
    *,
    object_type: Literal["node", "edge"],
    merge_strategy: str,
    strategy_config: MergeStrategyConfig | None,
    conflict_records: list[MergeConflictRecord],
    contradict_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Three-way merge for a list of records (nodes, edges, transcripts, etc.).
    Appends MergeConflictRecord objects to conflict_records.
    Appends synthetic CONTRADICTS edges to contradict_edges when strategy="contradict".
    """
```

#### Main Merge Function

```python
def merge_abhi_documents(
    base_document: dict[str, Any],
    left_document: dict[str, Any],
    right_document: dict[str, Any],
    *,
    base_input_path: str | Path,
    left_input_path: str | Path,
    right_input_path: str | Path,
    output_path: str | Path,
    merge_strategy: str = "contradict",
    strategy_config: MergeStrategyConfig | None = None,
    passphrase: str = "",
    dry_run: bool = False,
) -> AbhiMergeResult:
    """
    Three-way merge. Raises SchemaVersionError if schema versions differ.

    When dry_run=True:
        - Computes the full merge result including conflict detection
        - Does NOT call write_abhi_document
        - Returns AbhiMergeResult with dry_run=True and output_path=""

    When dry_run=False:
        - Calls write_abhi_document
        - Verifies the content_hash of the output document
        - Returns AbhiMergeResult with hash_verified=True if hash matches
        - Raises HashVerificationError if hash does not match (no file written)
    """
```

```python
def merge_abhi_files(
    *,
    base_input_path: str | Path,
    left_input_path: str | Path,
    right_input_path: str | Path,
    output_path: str | Path,
    merge_strategy: str = "contradict",
    passphrase: str = "",
    dry_run: bool = False,
) -> AbhiMergeResult:
    """File-level wrapper. Loads documents then calls merge_abhi_documents."""
```

#### Conflict Resolution

```python
def resolve_abhi_conflict(
    *,
    merged_path: str | Path,
    conflict_id: str,
    resolution: Literal["ours", "theirs", "value"],
    value: Any = None,
    passphrase: str = "",
) -> AbhiMergeResult:
    """
    Post-merge resolution of a single conflict by ID.
    Loads the merged document, applies the resolution, rewrites the file,
    and recomputes the content_hash.

    conflict_id: the MergeConflictRecord.conflict_id field
    resolution:
        "ours"   → use the left value stored in the conflict record
        "theirs" → use the right value stored in the conflict record
        "value"  → use the caller-supplied value parameter
    """
```

### 3. Boundary Validator

```python
def _find_dangling_edges(document: dict[str, Any]) -> list[str]:
    """
    Return a list of edge IDs whose source_id or target_id is not present
    in the document's node set. Only UUID node references are checked;
    string-valued alias fields are not considered.
    """
```

```python
def validate_abhi_document(
    document: dict[str, Any],
    *,
    input_path: str | Path,
    allow_dangling: bool = False,
    skip_verify: bool = False,
) -> AbhiValidationResult:
    """
    Extended validation. Now also checks for dangling edges.

    allow_dangling=True: dangling edges are reported as warnings, not errors.
    skip_verify=True: hash verification is skipped (warning logged).
    """
```

### 4. Schema Upgrade

```python
def upgrade_abhi_document(
    document: dict[str, Any],
    *,
    input_path: str | Path,
    target_version: str,
    output_path: str | Path,
    passphrase: str = "",
) -> AbhiExportResult:
    """
    Promote a .abhi document from an older schema version to target_version.
    Rewrites the file with the new schema_version and a recomputed content_hash.
    If the document is already at target_version, returns without modifying the file.
    """
```

### 5. Output Serializers

```python
def serialize_abhi_diff(
    result: FieldLevelDiffResult,
    *,
    fmt: Literal["human", "json"] = "human",
    max_chars: int = 4000,
) -> str:
    """
    Serialize a FieldLevelDiffResult to a human-readable or JSON string.

    human format: git-diff-style colored output with node labels and old→new values.
    json format: JSON serialization of result.model_dump(mode="json").

    When fmt="human" and the diff contains more than 50 changed objects,
    per-field detail is truncated and a summary line is appended.
    Total output is capped at max_chars characters.
    """
```

```python
def serialize_abhi_merge(
    result: AbhiMergeResult,
    *,
    fmt: Literal["human", "json"] = "human",
) -> str:
    """
    Serialize an AbhiMergeResult to a human-readable or JSON string.

    human format: summary of merged counts, then per-conflict detail
    (object ID, field, left value, right value, resolution applied).
    json format: JSON serialization of result.model_dump(mode="json").
    """
```


## Data Models

All new models are added to `src/waggle/models.py` as Pydantic `BaseModel` subclasses.

### FieldDelta

```python
class FieldDelta(BaseModel):
    field: str                  # e.g. "content", "tags", "metadata.source_app"
    old_value: Any              # None when the object was added
    new_value: Any              # None when the object was removed
```

### NodeDiffRecord

```python
class NodeDiffRecord(BaseModel):
    node_id: str
    classification: Literal["added", "removed", "modified", "identical"]
    label: str = ""             # label or content[:60] for display
    deltas: list[FieldDelta] = Field(default_factory=list)
```

### EdgeDiffRecord

```python
class EdgeDiffRecord(BaseModel):
    edge_id: str
    classification: Literal["added", "removed", "modified", "identical"]
    deltas: list[FieldDelta] = Field(default_factory=list)
```

### FieldLevelDiffResult

Replaces `AbhiDiffResult` for new callers. `AbhiDiffResult` is retained for backward compatibility but deprecated.

```python
class FieldLevelDiffResult(BaseModel):
    input_path_a: str
    input_path_b: str
    input_path_base: str = ""           # empty string for two-way diff
    abhi_spec_version_a: str = ""
    abhi_spec_version_b: str = ""
    diff_mode: Literal["two_way", "three_way"] = "two_way"

    # Node-level summary (IDs only, for backward compat)
    nodes_added: list[str] = Field(default_factory=list)
    nodes_removed: list[str] = Field(default_factory=list)
    nodes_updated: list[str] = Field(default_factory=list)

    # Edge-level summary (IDs only)
    edges_added: list[str] = Field(default_factory=list)
    edges_removed: list[str] = Field(default_factory=list)
    edges_updated: list[str] = Field(default_factory=list)

    # Field-level detail
    node_records: list[NodeDiffRecord] = Field(default_factory=list)
    edge_records: list[EdgeDiffRecord] = Field(default_factory=list)

    # Three-way only
    conflict_records: list[MergeConflictRecord] = Field(default_factory=list)

    # Warnings
    warnings: list[str] = Field(default_factory=list)
    schema_version_mismatch: bool = False
```

### MergeConflictRecord

```python
class MergeConflictRecord(BaseModel):
    conflict_id: str = Field(default_factory=lambda: str(uuid4()))
    object_id: str              # node or edge ID
    object_type: Literal["node", "edge"]
    field: str                  # e.g. "content", "tags"
    base_value: Any             # value in the base document (None if absent)
    left_value: Any             # value in the left document
    right_value: Any            # value in the right document
    resolved_by: str = ""       # set after resolution: "prefer_left", "prefer_right",
                                # "last_write_wins", "last_write_wins_tie_right", "contradict"
    resolved_value: Any = None  # the value selected after resolution
```

### AbhiMergeResult (extended)

The existing `AbhiMergeResult` gains new fields. All existing fields are preserved.

```python
class AbhiMergeResult(BaseModel):
    # --- existing fields (unchanged) ---
    base_input_path: str
    left_input_path: str
    right_input_path: str
    output_path: str
    merge_strategy: str = "contradict"
    abhi_spec_version: str = ""
    nodes_merged: int = 0
    edges_merged: int = 0
    conflicts: list[str] = Field(default_factory=list)   # retained for compat
    content_hash: str = ""
    embedding_count: int = 0
    encrypted: bool = False
    encryption_algorithm: str = ""
    executed_actions: list[str] = Field(default_factory=list)

    # --- new fields ---
    conflict_records: list[MergeConflictRecord] = Field(default_factory=list)
    dry_run: bool = False
    hash_verified: bool = False
    dangling_edges_dropped: list[str] = Field(default_factory=list)
    contradict_edges_added: int = 0
```

### AbhiValidationResult (extended)

```python
class AbhiValidationResult(BaseModel):
    # --- existing fields (unchanged) ---
    input_path: str
    valid: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    content_hash: str = ""
    abhi_spec_version: str = ""
    embedding_count: int = 0
    encrypted: bool = False
    encryption_algorithm: str = ""

    # --- new fields ---
    dangling_edges: list[str] = Field(default_factory=list)
    dangling_edge_count: int = 0
    boundary_warning: str = ""
```

### MergeStrategyConfig

```python
class MergeStrategyFieldOverride(BaseModel):
    field: str                  # e.g. "tags", "metadata.source_app"
    strategy: str               # "prefer_left" | "prefer_right" | "last_write_wins" | "contradict"

class MergeStrategyTypeOverride(BaseModel):
    node_type: str              # e.g. "decision", "fact"
    strategy: str

class MergeStrategyConfig(BaseModel):
    default_strategy: str = "contradict"
    field_overrides: list[MergeStrategyFieldOverride] = Field(default_factory=list)
    type_overrides: list[MergeStrategyTypeOverride] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "MergeStrategyConfig":
        """
        Load from path, or from ~/.waggle/merge-strategies.yaml if path is None.
        Returns a default config if the file does not exist.
        """
```


## CLI and MCP Interface

### CLI Commands

#### `waggle diff`

```
waggle diff <file_a> <file_b> [--base <base_file>] [--format human|json] [--passphrase-env VAR]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--base` | (none) | Base document for three-way diff |
| `--format` | `human` | Output format: `human` or `json` |
| `--passphrase-env` | `""` | Env var holding passphrase |

Exit codes: `0` always (diff never fails; schema mismatch is a warning).

#### `waggle merge`

```
waggle merge <left> <right> --base <base> --output <out> [--strategy ours|theirs|newer|contradict]
             [--dry-run] [--allow-dangling] [--skip-verify] [--force]
             [--include-deps] [--strict-export] [--format human|json] [--passphrase-env VAR]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--strategy` | `contradict` | Conflict resolution strategy |
| `--dry-run` | `False` | Preview without writing |
| `--allow-dangling` | `False` | Drop dangling edges instead of rejecting |
| `--skip-verify` | `False` | Skip hash verification on import |
| `--force` | `False` | Equivalent to `--allow-dangling --skip-verify` |
| `--include-deps` | `False` | Walk dangling edge targets and include them |
| `--strict-export` | `False` | Refuse to export if dangling edges exist |
| `--format` | `human` | Output format |

Exit codes:
- `0` — clean merge, no conflicts
- `1` — merge completed with unresolved conflicts
- `2` — merge impossible (schema mismatch, corrupt input, hash failure)
- `3` — reserved (future interactive abort)

#### `waggle resolve`

```
waggle resolve <merged_file> <conflict_id> --resolution ours|theirs|value [--value <json>]
```

#### `waggle upgrade`

```
waggle upgrade <file> --to <version> [--output <out>] [--passphrase-env VAR]
```

### MCP Tools

All MCP tools are defined in `src/waggle/server.py`. Each tool mirrors its CLI counterpart exactly.

#### `diff_abhi`

```python
# Input schema
{
    "input_path_a": str,
    "input_path_b": str,
    "input_path_base": str,   # optional, default ""
    "passphrase": str,        # optional, default ""
    "format": str,            # optional, default "json"
}

# Returns: CallToolResult with
#   content: [TextContent(serialize_abhi_diff(result, fmt=format))]
#   structuredContent: result.model_dump(mode="json")
```

#### `merge_abhi`

```python
# Input schema
{
    "base_input_path": str,
    "left_input_path": str,
    "right_input_path": str,
    "output_path": str,
    "merge_strategy": str,    # optional, default "contradict"
    "dry_run": bool,          # optional, default False
    "allow_dangling": bool,   # optional, default False
    "skip_verify": bool,      # optional, default False
    "passphrase": str,        # optional, default ""
}

# Returns: CallToolResult with
#   content: [TextContent(serialize_abhi_merge(result, fmt="json"))]
#   structuredContent: result.model_dump(mode="json")
```

#### `resolve_conflict`

```python
# Input schema
{
    "merged_path": str,
    "conflict_id": str,
    "resolution": str,        # "ours" | "theirs" | "value"
    "value": Any,             # optional, used when resolution="value"
    "passphrase": str,        # optional, default ""
}
```

### CLI ↔ MCP Equivalence Table

| CLI command | MCP tool | Shared core function |
|-------------|----------|---------------------|
| `waggle diff` | `diff_abhi` | `diff_abhi_files()` |
| `waggle merge` | `merge_abhi` | `merge_abhi_files()` |
| `waggle resolve` | `resolve_conflict` | `resolve_abhi_conflict()` |
| `waggle upgrade` | *(future)* | `upgrade_abhi_document()` |

Both interfaces call the same underlying functions. The CLI formats output for humans by default; MCP returns JSON by default. Validation rules, conflict detection, and exit semantics are identical.


## Identity Matching Algorithm

The identity matching algorithm is the foundation of both diff and merge. It is implemented as a standalone function with explicit inputs, outputs, and three classification outcomes.

```python
def _classify_node_identity(
    node_a: dict[str, Any],
    node_b: dict[str, Any],
) -> Literal["identical", "modified", "separate"]:
    """
    Inputs:
        node_a: a node dict from document A (must have "id" key)
        node_b: a node dict from document B (must have "id" key)

    Outputs:
        "identical"  — same id AND no DIFFED_FIELDS differ
        "modified"   — same id AND at least one DIFFED_FIELD differs
        "separate"   — different ids (treated as independent add + remove)

    Algorithm:
        1. If node_a["id"] != node_b["id"]: return "separate"
        2. For each field in DIFFED_FIELDS:
               if field == "metadata": compare sub-keys individually
               else: compare canonical JSON representations
           If any field differs: return "modified"
        3. return "identical"

    Note: IGNORED_FIELDS (updated_at, access_count, embedding_b64,
    embedding_model_id, embedding_dim) are never compared.
    """
```

```python
def _classify_edge_identity(
    edge_a: dict[str, Any],
    edge_b: dict[str, Any],
) -> Literal["identical", "modified", "separate"]:
    """
    Inputs:
        edge_a: an edge dict from document A (must have "id" key)
        edge_b: an edge dict from document B (must have "id" key)

    Outputs:
        "identical"  — same id AND source_id, target_id, relationship all match
        "modified"   — same id AND at least one EDGE_DIFFED_FIELD differs
        "separate"   — different ids

    Algorithm:
        1. If edge_a["id"] != edge_b["id"]: return "separate"
        2. For each field in EDGE_DIFFED_FIELDS:
               compare canonical JSON representations
           If any field differs: return "modified"
        3. return "identical"
    """
```

### Deduplication is Out of Scope

When two nodes have different `id` values but identical `content`, the diff tool treats them as one addition and one removal. It does NOT attempt to merge or deduplicate them. Deduplication is handled by a separate subsystem (`graph.py` dedup logic) and is explicitly outside the scope of diff/merge.


## Export Boundary Enforcement

### Validation Flow

```
import_abhi(path, allow_dangling, skip_verify, force)
    │
    ├─ load_abhi_document(path)
    │
    ├─ [unless skip_verify or force]
    │   validate hash → reject on mismatch
    │
    ├─ [unless skip_verify or force]
    │   validate schema version → reject on mismatch
    │
    ├─ _find_dangling_edges(document)
    │   │
    │   ├─ [if dangling and not allow_dangling and not force]
    │   │   → reject with ValidationError listing edge IDs
    │   │
    │   └─ [if dangling and (allow_dangling or force)]
    │       → drop dangling edges, log warning, continue
    │
    └─ return AbhiImportResult
```

### Export Flow

```
export_abhi(snapshot, output_path, include_deps, strict_export)
    │
    ├─ build_abhi_document(snapshot)
    │
    ├─ _find_dangling_edges(document)
    │   │
    │   ├─ [if dangling and strict_export]
    │   │   → raise ValidationError
    │   │
    │   ├─ [if dangling and include_deps]
    │   │   → walk dangling targets, add referenced nodes, rebuild document
    │   │
    │   └─ [if dangling, no flags]
    │       → log warning with edge IDs, continue
    │
    └─ write_abhi_document → return AbhiExportResult(dangling_edge_count=N)
```

### Flag Semantics

| Flag | Import | Export |
|------|--------|--------|
| `--allow-dangling` | Drop dangling edges, warn, continue | N/A |
| `--skip-verify` | Skip hash + schema version check | N/A |
| `--force` | `--allow-dangling` + `--skip-verify` | N/A |
| `--include-deps` | N/A | Walk dangling targets, include them |
| `--strict-export` | N/A | Refuse if any dangling edges exist |


## Schema Versioning

### Version Detection

Schema version is read from:
- v1 JSON format: `document["integrity"]["abhi_spec_version"]`
- v2 ZIP format: `document["manifest"]["schema_version"]`

The existing `_assert_supported_schema_version()` function enforces that the major version matches `ABHI_MAJOR_VERSION`. This is extended to support cross-version detection for diff/merge.

### Cross-Version Behavior

```python
def _check_schema_version_compatibility(
    version_a: str,
    version_b: str,
    operation: Literal["diff", "merge"],
) -> None:
    """
    Raises SchemaVersionError if the major versions differ.
    The error message includes the versions of both documents and
    the `waggle upgrade` command to run.

    For diff: raises SchemaVersionError (requirement 8.2)
    For merge: raises SchemaVersionError with exit code 2 (requirement 8.3)
    """
```

### `waggle upgrade` Command

```python
def upgrade_abhi_document(
    document: dict[str, Any],
    *,
    input_path: str | Path,
    target_version: str,
    output_path: str | Path,
    passphrase: str = "",
) -> AbhiExportResult:
    """
    Upgrade a .abhi document to target_version.

    If document is already at target_version:
        - Log informational message
        - Return without modifying the file

    Upgrade path (v1 JSON → v2 ZIP):
        1. Parse v1 JSON structure
        2. Map fields to v2 ZIP member layout
        3. Recompute content_hash
        4. Write new .abhi file at output_path

    Only the v1→v2 upgrade path is implemented in v1 of this feature.
    """
```


## Round-Trip Integrity

After every merge, the output document's `content_hash` is verified before the file is returned to the caller.

```python
# Inside merge_abhi_documents, after write_abhi_document:
actual_hash = compute_abhi_hash(output_document)
expected_hash = output_document["manifest"]["content_hash"].removeprefix("sha256:")
if actual_hash != expected_hash:
    # Remove the partially-written file
    Path(output_path).unlink(missing_ok=True)
    raise HashVerificationError(
        f"Post-merge hash verification failed: expected {expected_hash}, got {actual_hash}"
    )
result.hash_verified = True
```

The `compute_abhi_hash()` function already exists and covers nodes, edges, transcripts, and the manifest (excluding `content_hash`, `created_at`, and `export_context`). No changes to the hash algorithm are needed.

### Round-Trip Property

A merged document satisfies the round-trip property if:

```
load_abhi_document(write_abhi_document(snapshot)) == snapshot
```

This is guaranteed by the existing `write_abhi_document` / `load_abhi_document` pair, which uses deterministic ZIP layout (`_deterministic_zip_info`) and canonical JSON serialization (`_canonical_json`). The merge engine produces a `merged_snapshot` dict that is passed directly to `write_abhi_document`, so the round-trip guarantee is inherited.


## Performance Contracts

### Targets

| Operation | Input size | Time bound |
|-----------|-----------|------------|
| `diff_abhi_files` (two-way) | 1000 nodes, 5000 edges each | < 500 ms |
| `diff_abhi_files` (three-way) | 1000 nodes, 5000 edges each | < 500 ms |
| `merge_abhi_files` | 1000 nodes, 5000 edges each | < 2 s |

Reference hardware: single-core 2 GHz CPU, 1 GB available RAM.

### Implementation Approach

The current `_merge_records` function iterates over `set(base_map) | set(left_map) | set(right_map)` — O(N) per collection. The new field-level diff adds per-field comparison within each record, but `DIFFED_FIELDS` is a fixed small set (8 fields), so the per-record cost is O(1). Total complexity remains O(N).

The `_find_dangling_edges` function builds a node ID set once (O(N)) then checks each edge (O(E)), giving O(N + E) total.

No caching or parallelism is needed to meet the time bounds for the specified input sizes.

### Performance Warning

```python
# Inside diff_abhi_documents and merge_abhi_documents:
import time
_start = time.monotonic()
# ... operation ...
elapsed_ms = (time.monotonic() - _start) * 1000
_DIFF_WARN_MS = 500
_MERGE_WARN_MS = 2000
if elapsed_ms > threshold:
    logger.warning(
        "Performance warning: %s took %.0f ms for %d nodes / %d edges",
        operation_name, elapsed_ms, node_count, edge_count,
    )
```


## Test Corpus

### Fixture Files

All fixtures live in `tests/fixtures/abhi/`. They are static, version-controlled `.abhi` files — not generated at test time.

| File | Description | Valid? |
|------|-------------|--------|
| `empty.abhi` | Zero nodes, zero edges | Yes |
| `single-node.abhi` | One node, zero edges | Yes |
| `linear-history.abhi` | Linear sequence of node additions (10 nodes, 9 edges) | Yes |
| `branched.abhi` | Graph with branching structure (20 nodes, 25 edges) | Yes |
| `with-contradictions.abhi` | Contains at least one `CONTRADICTS` edge | Yes |
| `with-dangling-edges.abhi` | Contains at least one edge whose target node is absent | **No** (intentionally invalid) |

The `with-dangling-edges.abhi` fixture is documented in a `README.md` in the same directory explaining that it is intentionally invalid and used only for boundary enforcement tests.

### Fixture Generation

Fixtures are generated once using `write_abhi_document()` and committed. A helper script `scripts/generate_abhi_fixtures.py` is provided for regeneration if the format changes, but it is not run at test time.

### Test Coverage Requirements

Each fixture must have at least one test that exercises:
1. `diff_abhi_files` (two-way) against the fixture
2. `merge_abhi_files` using the fixture as base, left, and right
3. `validate_abhi_document` against the fixture

The `with-dangling-edges.abhi` fixture must have tests for:
- Rejection on import without flags
- Acceptance with `--allow-dangling`
- Acceptance with `--force`


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: IGNORED_FIELDS produce no field deltas

*For any* pair of nodes (or edges) that differ only in `IGNORED_FIELDS` (`updated_at`, `access_count`, `embedding_b64`, `embedding_model_id`, `embedding_dim`, `export_context`), the diff engine SHALL produce zero `FieldDelta` records for those nodes.

**Validates: Requirements 1.5, 1.6, 1.9**

### Property 2: Added and removed nodes are correctly classified

*For any* pair of documents A and B, every node ID present in B but absent in A SHALL appear in `nodes_added`, and every node ID present in A but absent in B SHALL appear in `nodes_removed`. The union of `nodes_added`, `nodes_removed`, and `nodes_updated` SHALL equal the symmetric difference of the two node ID sets plus the set of IDs present in both with differing DIFFED_FIELDS.

**Validates: Requirements 1.3, 1.4, 9.1, 9.2, 9.3**

### Property 3: Conflict records are per-field, not per-object

*For any* three-way merge where N distinct fields on the same node or edge are changed by both left and right relative to base, the merge engine SHALL produce exactly N `MergeConflictRecord` objects for that node or edge — one per conflicting field.

**Validates: Requirements 2.1, 2.2**

### Property 4: Strategy `ours` always selects the left value

*For any* set of conflicts resolved with `--strategy=ours`, every `MergeConflictRecord.resolved_value` SHALL equal the corresponding `left_value`, and `resolved_by` SHALL equal `"prefer_left"`.

**Validates: Requirements 2.3**

### Property 5: Strategy `theirs` always selects the right value

*For any* set of conflicts resolved with `--strategy=theirs`, every `MergeConflictRecord.resolved_value` SHALL equal the corresponding `right_value`, and `resolved_by` SHALL equal `"prefer_right"`.

**Validates: Requirements 2.4**

### Property 6: Strategy `newer` selects the value with the later timestamp

*For any* conflict resolved with `--strategy=newer`, the `resolved_value` SHALL equal the value from the side with the strictly later `updated_at` timestamp. When timestamps are equal, the right value is selected and `resolved_by` is `"last_write_wins_tie_right"`.

**Validates: Requirements 2.5, 2.6**

### Property 7: Dry-run produces identical results without writing files

*For any* merge inputs, calling `merge_abhi_files(dry_run=True)` SHALL return an `AbhiMergeResult` with `dry_run=True`, `output_path=""`, and the same `nodes_merged`, `edges_merged`, `conflicts`, and `conflict_records` as a non-dry-run call on the same inputs. No file SHALL be written to disk.

**Validates: Requirements 3.1, 3.2, 3.3**

### Property 8: Dangling edge detection is complete

*For any* document, `_find_dangling_edges` SHALL return exactly the set of edge IDs whose `source_id` or `target_id` is not present in the document's node ID set. No false positives (edges with valid endpoints) and no false negatives (edges with missing endpoints) SHALL occur.

**Validates: Requirements 4.1, 4.4**

### Property 9: `--allow-dangling` import drops all dangling edges

*For any* document containing dangling edges, importing with `--allow-dangling` SHALL produce an imported graph that contains zero dangling edges — all edges with missing endpoints SHALL be dropped, and all edges with valid endpoints SHALL be retained.

**Validates: Requirements 4.9**

### Property 10: Post-merge hash consistency

*For any* valid merge result, the `integrity.content_hash` stored in the output document SHALL equal the hash recomputed by `compute_abhi_hash()` applied to that same document. `AbhiMergeResult.hash_verified` SHALL be `True`.

**Validates: Requirements 5.1, 5.3**

### Property 11: Merge round-trip produces byte-identical output

*For any* merged `.abhi` document, exporting it and then re-importing and re-exporting SHALL produce a byte-identical archive. This follows from the deterministic ZIP layout and canonical JSON serialization used by `write_abhi_document`.

**Validates: Requirements 5.2**

### Property 12: Serialized diff output length is bounded

*For any* `FieldLevelDiffResult` containing 50 or fewer changed objects (nodes + edges), `serialize_abhi_diff(result, fmt="human")` SHALL return a string of at most 4000 characters.

**Validates: Requirements 6.3**

### Property 13: Diff performance contract

*For any* pair of `.abhi` documents each containing up to 1000 nodes and 5000 edges, `diff_abhi_files` SHALL complete within 500 milliseconds on reference hardware.

**Validates: Requirements 7.1**

### Property 14: Merge performance contract

*For any* triple of `.abhi` documents each containing up to 1000 nodes and 5000 edges, `merge_abhi_files` SHALL complete within 2000 milliseconds on reference hardware.

**Validates: Requirements 7.2**


## Error Handling

### Error Types

```python
# src/waggle/errors.py (additions)

class SchemaVersionError(ValidationFailure):
    """Raised when diff or merge is called on documents with incompatible schema versions."""

class HashVerificationError(ValidationFailure):
    """Raised when post-merge hash verification fails."""

class DanglingEdgeError(ValidationFailure):
    """Raised when import is called on a document with dangling edges and no opt-out flag."""

class ConflictResolutionError(ValidationFailure):
    """Raised when resolve_abhi_conflict is called with an unknown conflict_id."""
```

### Error → Exit Code Mapping

| Exception | CLI exit code | Description |
|-----------|--------------|-------------|
| `SchemaVersionError` | 2 | Schema versions incompatible |
| `HashVerificationError` | 2 | Post-merge hash mismatch |
| `DanglingEdgeError` | 2 | Dangling edges on strict import |
| `ValidationFailure` (other) | 2 | Corrupt input |
| Unresolved conflicts present | 1 | Merge completed with conflicts |
| No errors | 0 | Clean merge |

### Logging

All warnings and errors are logged via `logger = logging.getLogger(__name__)` in `abhi.py`. No new logging infrastructure is introduced.

Key log messages:
- `WARNING: Dropping %d dangling edges: %s` (allow-dangling import)
- `WARNING: Hash verification bypassed (--skip-verify)` 
- `WARNING: Performance warning: %s took %.0f ms for %d nodes / %d edges`
- `INFO: Document already at target version %s, no upgrade needed`


## Testing Strategy

### Dual Testing Approach

Unit tests cover specific examples, edge cases, and error conditions. Property-based tests verify universal properties across many generated inputs. Both are needed for comprehensive coverage.

### Property-Based Testing Library

Use **[Hypothesis](https://hypothesis.readthedocs.io/)** for Python property-based testing. Each property test runs a minimum of 100 iterations.

Tag format for each property test:
```python
@settings(max_examples=100)
@given(...)
def test_property_N_description():
    # Feature: abhi-diff-merge-tool, Property N: <property_text>
    ...
```

### Property Test Implementations

**Property 1 — IGNORED_FIELDS produce no field deltas**
```python
# Generate: random node pair with same id, differing only in IGNORED_FIELDS
# Assert: len(result.node_records[0].deltas) == 0
```

**Property 2 — Added and removed nodes are correctly classified**
```python
# Generate: random document A, then B = A with random nodes added/removed
# Assert: nodes_added == set(B.nodes) - set(A.nodes)
#         nodes_removed == set(A.nodes) - set(B.nodes)
```

**Property 3 — Conflict records are per-field**
```python
# Generate: base node, left = base with N fields changed, right = base with same N fields changed differently
# Assert: len(conflict_records for that node) == N
```

**Property 4 — Strategy `ours` selects left**
```python
# Generate: random conflict set
# Assert: all(r.resolved_value == r.left_value for r in conflict_records)
```

**Property 5 — Strategy `theirs` selects right**
```python
# Generate: random conflict set
# Assert: all(r.resolved_value == r.right_value for r in conflict_records)
```

**Property 6 — Strategy `newer` selects later timestamp**
```python
# Generate: random conflicts with varying timestamps (including ties)
# Assert: resolved_value matches the side with the later updated_at
#         tie-breaking: right value selected
```

**Property 7 — Dry-run produces identical results without writing**
```python
# Generate: random merge inputs
# Assert: dry_run result fields == non-dry-run result fields (except output_path, dry_run flag)
#         no file written to disk
```

**Property 8 — Dangling edge detection is complete**
```python
# Generate: random document with random subset of edges pointing to absent nodes
# Assert: set(_find_dangling_edges(doc)) == expected_dangling_set
```

**Property 9 — `--allow-dangling` drops all dangling edges**
```python
# Generate: random document with dangling edges
# Assert: _find_dangling_edges(imported_doc) == []
#         all non-dangling edges are retained
```

**Property 10 — Post-merge hash consistency**
```python
# Generate: random merge inputs
# Assert: compute_abhi_hash(output_doc) == output_doc["manifest"]["content_hash"].removeprefix("sha256:")
#         result.hash_verified == True
```

**Property 11 — Merge round-trip**
```python
# Generate: random merge inputs
# Assert: write_abhi_document(load_abhi_document(merged_path)) produces byte-identical output
```

**Property 12 — Serialized diff length bounded**
```python
# Generate: FieldLevelDiffResult with 1..50 changed objects
# Assert: len(serialize_abhi_diff(result, fmt="human")) <= 4000
```

**Properties 13 & 14 — Performance contracts**
```python
# Generate: documents with exactly 1000 nodes and 5000 edges (using fixture or generator)
# Assert: elapsed time < 500ms (diff) / 2000ms (merge)
# Note: run with @settings(max_examples=5) to avoid excessive CI time
```

### Unit Tests

Unit tests cover:
- Each fixture file: diff, merge, validate
- Schema version mismatch: error raised, message contains upgrade command
- `--allow-dangling`: dangling edges dropped, non-dangling retained
- `--skip-verify`: hash check bypassed, warning logged
- `--force`: both behaviors applied
- `--include-deps`: dangling targets included in export
- `--strict-export`: export refused when dangling edges exist
- `waggle resolve`: conflict resolved by ID, file updated, hash recomputed
- `waggle upgrade`: v1→v2 upgrade, already-at-version no-op
- Exit code matrix: 0, 1, 2 for clean/conflicts/impossible
- `--format=patch`: error returned stating not yet supported
- `serialize_abhi_diff` truncation: >50 changed objects → summary line appended
- `MergeStrategyConfig.load()`: file not found → default config returned
- Per-field strategy override: config file overrides global strategy for specified fields

### Test File Location

```
tests/
  test_abhi_diff_merge.py      # new file: all diff/merge property and unit tests
  fixtures/
    abhi/
      empty.abhi
      single-node.abhi
      linear-history.abhi
      branched.abhi
      with-contradictions.abhi
      with-dangling-edges.abhi
      README.md
```

Existing tests in `tests/test_graph.py` and `tests/test_server.py` that exercise `diff_abhi_files` and `merge_abhi_files` are updated to use `FieldLevelDiffResult` and the new `conflict_records` field.

