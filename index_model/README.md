# Python model

`index_model` represents the whole version 3 schema using only the Python 3.11+
standard library. It does not open databases, parse source files, or depend on an
indexing tool. Run the example from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m examples.two_functions
```

## Build a snapshot

```python
from index_model import (
    GLOBAL_SCOPE_ID, IndexBuilder, IndexMetadata, LanguageId, SymbolId,
)

builder = IndexBuilder()
builder.set_metadata(IndexMetadata(project_root="/example"))
builder.add_language(language_id=LanguageId(1), name="C++")
builder.add_symbol(symbol_id=SymbolId(10))
builder.resolve_symbol(
    symbol_id=SymbolId(10), name="process", scope_id=GLOBAL_SCOPE_ID,
    language_id=LanguageId(1),
)
index = builder.build()
symbol = index.symbol(SymbolId(10))
assert symbol.scope is index.scope(GLOBAL_SCOPE_ID)
assert symbol.scope_name == ""
```

The builder accepts IDs so records can arrive in any order. `build()` resolves
links iteratively and returns canonical nested objects: every relationship to a
particular symbol points to the same `Symbol` instance within that snapshot.
Known owners may be supplied as forward references; unknown owners remain None.
Missing referenced IDs are errors. Create placeholders explicitly when a symbol
is known to exist but its attributes are unavailable.

Only global scope 1 is seeded. Supply other lookup rows explicitly, including any
reference-bit description records you wish to retain. Call `set_metadata()` once.
IDs are supplied by the caller, are local to a snapshot, and fit SQLite's signed
64-bit integer range. Family-specific `NewType` annotations support static typing;
they do not create different runtime integer classes. Booleans are rejected as
IDs and counts. Positions use zero-based lines and UTF-8 byte columns.

Duplicate IDs are rejected at insertion; duplicate natural keys and relationships
are rejected during the build. `resolve_symbol()` replaces only an existing empty
placeholder and can resolve it once. `add_*()` methods never overwrite records;
enrichment and status changes use the explicit methods below.
Builder reuse creates new entity objects and cannot alter an earlier snapshot.

## Enrich an existing snapshot

```python
builder = IndexBuilder.from_model(index)
builder.enrich_symbol(symbol_id=SymbolId(10), signature="()", return_type="int")
enriched = builder.build()
assert index.symbol(SymbolId(10)).signature is None
assert enriched.symbol(SymbolId(10)).signature == "()"
```

`from_model()` copies every collection into independent staging records and keeps
the existing IDs and metadata. `build()` reconstructs all entity links within the
new snapshot, including links to enriched symbols. Immutable scalar values and
metadata may be shared safely. No source access or tool calls occur.

`enrich_symbol()` fills optional details of an existing resolved symbol. `None`
means leave unchanged, and repeating the same value is allowed. Conflicting known
values reject the entire enrichment call without changing the builder. Resolve
placeholders with `resolve_symbol()` first. Names, scope membership, and language
are established during symbol creation or resolution and are not rewritten by
enrichment. Missing referenced IDs and invalid field values fail at `build()`.

`update_collection_status(file_id=..., stage=..., status=..., error=...)` replaces
an existing stage outcome. It replaces the error too, with `None` by default.
Partial and failed outcomes still require an error. New files still need all
five statuses explicitly; neither cloning nor enrichment invents coverage.

`update_metadata(metadata)` explicitly replaces existing metadata. Use
`dataclasses.replace(index.metadata, ...)` to retain provenance while adding a
metrics version or setting completion status and time. Cloning and pipeline
execution do not automatically mark the snapshot complete.

The separate [`index_pipeline`](../index_pipeline.py) module runs steps in order;
see the [pipeline example](../examples/sequential_steps.py).

## Records and methods

All entity dataclasses are frozen, slotted, keyword-only, and use object identity
for equality. `Position` and `BodyMetrics` are immutable value objects with value
equality. Entities from different builds are not equal merely because IDs match.

| SQL relation | Python record | Builder method |
| --- | --- | --- |
| `index_metadata` | `IndexMetadata` | `set_metadata`, `update_metadata` |
| `libraries` | `Library` | `add_library` |
| `files` | `File` | `add_file` |
| `locations` | `Location` | `add_location` |
| `symbol_kinds` | `SymbolKind` | `add_symbol_kind` |
| `languages` | `Language` | `add_language` |
| `access_specifiers` | `AccessSpecifier` | `add_access_specifier` |
| `reference_kind_bits` | `ReferenceKindBit` | `add_reference_kind_bit` |
| `scopes` | `Scope` | `add_scope` |
| `symbols` | `Symbol` | `add_symbol`, `resolve_symbol`, `enrich_symbol` |
| `file_includes` | `FileInclude` | `add_file_include` |
| `symbol_references` | `SymbolReference` | `add_symbol_reference` |
| `class_inheritance` | `ClassInheritance` | `add_class_inheritance` |
| `method_overrides` | `MethodOverride` | `add_method_override` |
| `symbol_source_ranges` | `SymbolSourceRange` | `add_symbol_source_range` |
| `symbol_bodies` | `SymbolBody` and `BodyMetrics` | `add_symbol_body` |
| `collection_status` | `CollectionStatus` | `add_collection_status`, `update_collection_status` |
| `generated_documentation` | `GeneratedDocumentation` | `add_generated_documentation` |

Relationships are object references: `Location.file`, `File.library`,
`Symbol.scope`, `Scope.owner`, `Symbol.kind`, `Symbol.language`,
`Symbol.declaration`, `Symbol.definition`, and the endpoints of relationship
records. Symbols do not contain collections of incoming/outgoing edges.
Recursive calls are represented by separate `SymbolReference` records.

`Symbol.scope_name`, `Symbol.parent`, and `Symbol.resolution_state` correspond to
the calculated columns of `symbol_details`. `SymbolBody.line_count` corresponds
to `symbol_body_metrics`. None of those values can be written independently.
`BodyMetrics` requires every count explicitly; absent analysis must not become
invented zero counts. Use no `SymbolBody` row when metrics were not collected.

Use `SnapshotStatus`, `CollectionStage`, `CollectionState`,
`DocumentationStatus`, `SourceRangeRole`, and `ReferenceKind` members, not raw
strings/integers. Reference flags combine with `|`. Kinds and languages use
extensible lookup records rather than a fixed enumeration.

`IndexModel` exposes tuples named after their SQL relations, with plural
`collection_statuses` for collection records. Metadata is a single object.
Collections have deterministic key order. Lookups include `symbol(id)`,
`scope(id)`, `file(id)`, `library(id)`, `location(id)`, `kind(id)`, `language(id)`,
`access_specifier(id)`, `reference_kind_bit(bit)`, `reference(id)`, `include(id)`,
`inheritance(id)`, `override(id)`, `body(symbol_id)`, `documentation(symbol_id)`,
`collection_status(file_id, stage)`, and `source_range(symbol_id, location_id, role)`.
Missing lookups raise `KeyError`. There is no implicit graph loading or traversal.

## Validation boundary

Record constructors check local fields and scalar constraints. Builder `build()`
checks references, canonical identity, natural uniqueness, ownership cycles,
coverage records, metric/body consistency, and required documentation inputs.
Direct `IndexModel(...)` construction validates too, and copies collection inputs
into tuples so caller-owned lists cannot change the snapshot. The standalone
`validate(index)` returns a tuple of `ValidationIssue` objects.

Failures raise `ModelValidationError`, whose `.issues` contains immutable records
with `entity`, `field`, and `message`. Independent invalid records can be reported
together; dependent records may report that their dependencies were invalid.
Cycles are detected without recursive construction or mutable published shells.

Each file needs an explicit status for all five collection stages. This is a
structural requirement; a recorded `partial` or `failed` stage is still valid
data. A model may represent a building or failed snapshot. Publication readiness
is a separate concern handled by `validate_snapshot.py` for database snapshots.

SQL NULL becomes None. URI and timestamp strings are preserved without conversion;
callers follow the documented URI/UTC conventions. Hash checks validate digest
shape, not source freshness. The model performs no file access or semantic proof.
It is intentionally stricter than SQLite's input coercion: normalize enum values,
integer strings, and booleans at an external adapter boundary.

Persistence, migrations, indexing-tool adapters, and cross-snapshot identity are
outside this package.
