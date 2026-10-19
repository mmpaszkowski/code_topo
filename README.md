# Code index database

`schema.sql` defines a SQLite schema for one project/build snapshot. SQLite 3.37
or later is required for `STRICT` tables. Schema version 3 is a rebuild-only
change; earlier databases, including version 2, must be rebuilt.

The [Python model](index_model/README.md) represents the complete schema as an
immutable graph with nested relationships, typed builder methods, and validation.
It uses only the Python 3.11+ standard library and performs no database access.

## Run the pipeline

Install the YAML dependency, then run the CLI from this directory:

```sh
python3 -m pip install -r requirements-clangd.txt
python3 run_pipeline.py
python3 run_pipeline.py --config path/to/config.yaml
```

### Progress and time estimates

The CLI writes plain progress lines to stderr, including phase transitions and
a heartbeat every five seconds while a phase is active:

```text
[4/5] ClangAstStep | compiling | 8/14 contexts | elapsed 18s | remaining ~14s | tests/layer.cpp
[4/5] ClangAstStep | merging and validating | elapsed 7s | remaining unknown
```

Use `--progress-interval 2` to change the interval or `--no-progress` to disable
phase reporting and its heartbeat thread. Lifecycle logs, warnings, errors, and
the final stdout summary remain available. Intervals must be finite and positive.

YAML progress counts imported bytes without a preliminary parsing pass. LSP counts
selected project files; AST counts selected compilation contexts. Handled failures
and skipped items count as processed, with their diagnostic outcomes reported
separately. A finished counter does not establish semantic completeness.

Elapsed time is for the current phase. Its approximate ETA uses the average rate
after at least three completion updates and five seconds of work. Estimates reset
on phase changes; an expired estimate becomes unknown until work advances again.
Setup, merging, validation, shutdown, and database operations show elapsed time
without an ETA. Step completion includes database commit time. No timing history
is stored and no whole-pipeline estimate is calculated.

Library callers retain no-op reporting by default. To enable it, construct a
`LoggingProgressReporter` from `progress.py`, use it as a context manager, and pass
it as `progress=` to `create_application()`. The reporter thread only reads its
own synchronized state; indexing and database access stay on the calling thread.

### Configuration

The default is `config.yaml` in the current working directory. The included
configuration imports the local `monogon-index.yaml` export:

```yaml
project_root: ../monogon

database:
  path: index.sqlite3
  save_after_each_step: true

steps:
  - type: clangd_yaml
    path: monogon-index.yaml
    recover_known_errors: true

  - type: clangd_lsp
    compile_commands_dir: ../monogon/build
    remove_compile_flags:
      - -fprofile-abs-path
    exclude_paths:
      - build/_deps/*
```

Edit the paths for your project. Paths inside the configuration are relative to
its directory, regardless of where the command is launched. Absolute paths also
work. The export must already exist; the CLI does not run clangd-indexer.

Steps run in list order. `clangd_yaml` must appear once, first, because it requires
an empty initial model. The optional `clangd_lsp` step follows it once and adds
declaration/definition locations and ranges, include dependencies, references,
verified reference containers, and direct inheritance. It needs
clangd on `PATH` and an existing `compile_commands.json` in the configured directory.
It inspects existing project files and matches existing symbols by exact locations,
using imported clangd identities to resolve ambiguous matches when possible;
it does not repeat symbol discovery or extract ASTs. The factory provides the
current export as clangd's static project index. Call-hierarchy results enrich
reference containers; they do not set call flags because clangd also reports
non-call uses such as address-taking. See the
[LSP adapter documentation](clangd_lsp/README.md) for configuration and coverage.
The supplied configuration removes the unsupported coverage flag from a temporary
compilation-database copy and excludes vendored files from LSP inspection. Both
settings default to empty lists in other configurations. Existing build inputs
and imported dependency symbols are preserved.
`recover_known_errors` defaults to `true`; setting it to `false` disables the
importer's narrow recovery of malformed symbol kinds. The `database` section is
optional. Unknown settings, duplicate keys, incorrect types, and empty
step lists are configuration errors and are rejected before execution.

The CLI prints model counts, snapshot status, and diagnostic counts to stdout.
Progress, database saves, and detailed diagnostics go to stderr. The model's status
remains `building`; saving it does not mark it ready for publication. Current
source files are assumed to match the export, as described in
the [importer documentation](clangd_import/README.md). Recoverable diagnostics
allow success with partial coverage. Exit codes are `0` for success, `1` for an
execution failure, and `2` for a configuration or argument error. A failed step
stops execution and logs its traceback.

[`pipeline_config.py`](pipeline_config.py) loads immutable settings.
[`application_factory.py`](application_factory.py) constructs concrete steps and
storage. [`Application`](application.py) receives those objects through its
constructor and runs them; it has no dependency on clangd, YAML, or SQLite.
[`run_pipeline.py`](run_pipeline.py) handles arguments and displays results using
the common `Step` interface.

To support another step, add its settings and validation to the loader and its
construction to `create_step()`. Implement `run(model)` and optionally override
`summary()` to return immutable `SummaryItem(label, value)` records. The default
summary is empty. Each adapter owns its summary values and diagnostic logging;
the CLI does not inspect tool-specific reports or branch on concrete step types.
The model, application, and pipeline need no YAML dependency.

The new LSP adapter has no dependency on the YAML adapter or its report. The
application factory constructs its client and step; all LSP communication and
interpretation remain inside `clangd_lsp`.

## Save after each step

`database.save_after_each_step: true` saves the resulting model after every
successful step and before the next step starts. `database.path` is required
when the section is present and is relative to the configuration file. Its
parent directory must already exist. Omitting the section or setting
`save_after_each_step: false` leaves execution entirely in memory; the boolean
defaults to `false`. The supplied configuration enables saving to `index.sqlite3`.

Each save replaces the complete model in one SQLite transaction. IDs, unknown
values, metadata, and partial collection statuses are preserved. The existing
database remains unchanged if writing, validation, or commit fails, and the
pipeline stops with exit code `1`. A missing database is created exclusively on
the first save; a failed initial save removes only that newly created output.

Existing databases must match the bundled `schema.sql` definitions (including
tables, views, and indexes), schema version, and project metadata. Compatibility
is checked before steps run and again under the write lock. Unrelated databases
and modified schemas are rejected without migration. The writer validates model
structure, enables foreign keys, and checks SQLite integrity before committing.

The small [`ModelStore`](model_store.py) interface exposes `save(model)`;
[`SQLiteModelStore`](sqlite_model_store.py) implements it using standard-library
`sqlite3` and explicit column mappings. The application factory creates the store
and checks its destination; `Application` passes the injected store to
`Pipeline(steps, store=store)`. Steps do not perform persistence themselves.
Without a store, `Pipeline` behaves as before. An empty pipeline does not save.

The database contains the latest saved model, including metrics and generated
documentation when present. Importer-specific diagnostics and external ID maps
remain in the import report. Database loading, resume, incremental writes, and
concurrent pipeline writers are not implemented. Intermediate databases may fail
the publication validator because their status is `building` or coverage is partial.

## Sequential enrichment

[`index_pipeline.py`](index_pipeline.py) provides a `Step` abstract base class
with `run(model: IndexModel) -> IndexModel` and a `Pipeline` that executes steps
in the supplied order. Each step receives its tools and configuration through
its constructor, reads the previous snapshot, and returns a validated snapshot
using `IndexBuilder.from_model(model)` and `build()`.

```python
from index_pipeline import Pipeline

model = Pipeline([collect_symbols, collect_source_ranges, calculate_metrics]).run(initial_model)
```

The variables above are application-supplied step instances. A runnable example
with two injected fixture tools is available without external dependencies:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m examples.sequential_steps
```

The pipeline logs step start, completion, and failure through the standard
`logging` module under `index_pipeline`; the application configures logging.
Step and storage exceptions propagate unchanged and prevent later steps from running. Earlier
snapshots remain unchanged; tool side effects are not rolled back. Steps check
their own prerequisites and explicitly report partial coverage when they can
continue. A wrong return type is an error. An empty pipeline returns its input.

Each returned snapshot must satisfy the model's structural rules, including
explicit coverage for all files. Steps retain snapshot-local IDs and normalize
tool-specific identities and coordinates at their boundary. Successful execution
does not automatically change metadata status or establish publication readiness.

The first real step is the [clangd YAML importer](clangd_import/README.md). It reads
an existing export, creates an immutable model, and exposes a separate import report
with external ID mappings and diagnostics. Its PyYAML dependency is listed in
`requirements-clangd.txt`.

## Normalization contract

Stored relations are designed for 3NF under the following dependencies. Read-only
views expose derived fields without introducing additional stored copies.

| Relation | Determinants and dependent facts |
| --- | --- |
| Lookup tables | ID and unique name are alternative keys. |
| `files` | File ID or URI determines ownership and indexed content hash. |
| `locations` | Location ID or the full file/range tuple determines the location. |
| `scopes` | Scope ID determines its display name and optional owner; a known owner uniquely identifies its scope. Display names are not keys. |
| `symbols` | Symbol ID determines its attributes and containing scope ID. |
| `symbol_bodies` | Symbol ID determines the selected body and analysis metrics. Physical line count is derived from its location. |
| `collection_status` | `(file_id, stage)` determines status and error. |
| `symbol_source_ranges` | `(symbol_id, location_id, role)` is the complete key. |
| Relationship tables | An edge ID or its unique endpoint pair identifies the edge and any attributes. |
| `symbol_references` | Reference ID or the normalized target/location/flags/container tuple identifies an observation. |
| `generated_documentation` | Symbol ID determines the current generation result and provenance. |
| `index_metadata` | One row describes the snapshot. |

Scope membership and semantic parentage are stored only once: a symbol references
a scope, which optionally references its owning symbol. `symbol_details` derives
`scope`, `parent_symbol_id`, and `resolution_state`. `symbol_body_metrics` derives
`line_count`. Neither view is writable.

The reference bitmask remains an application-owned scalar flag value; it does not
introduce a non-key functional dependency. No dependency from an occurrence's
location to a single target or container is assumed, because source expansion
and semantic contexts can overlap. Likewise, other body metrics belong to the
analyzed symbol: template instantiation or compilation context can produce
different analysis results for a shared source range. Only physical line count
is determined solely by that range.

## Import boundary

The database stores application concepts, independently of the indexing tool.
The importer owns external IDs, raw records, enum mappings, and coordinate
conversion. It maintains an in-memory mapping from external identity to integer
`symbol_id`. IDs are local to a snapshot: do not use them as cross-build bookmarks
or match symbols by name/signature. Each producer needs an explicit mapping to
the application's kind and language names (for example `function` and `C++`).

When a reference arrives before its symbol, insert a placeholder and reuse its
ID when resolving it. A placeholder has no symbol attributes. Resolve it by
setting `name`, `scope_id`, and `language_id` together. Source locations may remain
unknown. `symbol_details.resolution_state` is derived from the presence of those
attributes, so it cannot contradict them. Remaining placeholders are allowed in
a published snapshot, and consumers must display them as unresolved.

The schema creates the global scope with `scope_id = 1`, an empty display name,
and no owner. A resolved symbol in global scope references that row. NULL
`symbols.scope_id` is reserved for placeholders; it never means global scope.
For a known non-global scope, insert a `scopes` row with a nonempty qualified
display name (for example `ns::Widget::`) and an optional `owner_symbol_id`.
The owner is the semantic containing symbol, not a lexical source container.
Use NULL when the owner is unknown; filling it later resolves parentage for all
members without updating those symbols. Self-ownership and longer cycles are
rejected by snapshot validation.

An owner introduces at most one scope. Distinct scopes may share a display name,
including anonymous namespaces in different files or scopes under overloaded
functions. The importer maps scope identities just as it maps symbol identities;
do not merge scopes solely by display name. Insert an owner's symbol or placeholder
before linking it from a scope. For nested scopes, create the enclosing scope,
its member/owner symbol, and then the scope that symbol introduces.

Consumers that need the previous display fields can query the view:

```sql
SELECT symbol_id, name, scope, parent_symbol_id, resolution_state
FROM symbol_details;
```

The application owns these reference flags; map source meanings explicitly:

| Bit | Meaning |
| --- | --- |
| 1 | Declaration occurrence |
| 2 | Definition occurrence |
| 4 | Reference/use occurrence |
| 8 | The symbol name is spelled at the occurrence |
| 16 | Call occurrence |

Flags may be combined. Exact duplicates are prohibited, including occurrences
without a container. Importers should deduplicate normalized observations, using
`ON CONFLICT DO NOTHING` for repeated inserts. Different flag sets remain distinct
records: this schema does not automatically merge their meaning. Do not count
records as unique source sites without grouping by their locations. A call is
selected with `(kind & 16) <> 0`; a call container may equal its target (recursion).
Observed calls do not guarantee complete runtime dispatch information.

## Source evidence

All locations use zero-based lines, **UTF-8 byte columns**, and exclusive ends.
The importer converts source coordinates against the exact indexed bytes before
inserting a location. For example, in `é😀foo()`, `foo` occupies byte columns
6 through 9, although its UTF-16 start column is 3. Line breaks are counted by LF;
CR in CRLF remains a byte on the preceding line. Tabs are bytes, not visual stops.
Never relabel UTF-16 offsets as byte offsets. If required bytes are unavailable,
omit the unavailable location and record partial collection rather than guessing.

`symbols.declaration_location_id` and `definition_location_id` identify preferred
name locations for navigation. Additional name occurrences belong in
`symbol_references`. `symbol_source_ranges` stores complete declaration,
definition, and body extents, including multiple observed declarations. Body
availability does not require metrics. A `symbol_bodies` row selects one body
and must have a corresponding `body` source range.

Store only analyzed metrics in `symbol_bodies`; read physical `line_count` from
`symbol_body_metrics`. A range ending at column zero excludes that final line.
An empty range yields zero lines and is rejected as a body by snapshot validation.
For the usual brace-to-brace range, line count is `end_line - start_line + 1`.
Symbols sharing a body range therefore always expose the same physical line count.

`files.content_hash` is the lowercase SHA-256 digest of the indexed file bytes.
Use NULL when the source was unavailable. Before extracting source for display
or documentation, compare the current file hash; refuse stale or unverifiable
source rather than presenting it as the indexed version. The structural validator
does not read source files or perform these hash comparisons.

`index_metadata` has exactly one row, with `metadata_id = 1`. Store an absolute
project root and, when available, source revision and a SHA-256 fingerprint of
the normalized build inputs. Build inputs include compilation commands and
relevant configuration; record NULL when unavailable. The importer must document
its deterministic serialization of these inputs. Timestamps use UTC ISO 8601.

If metrics are present, set `metrics_version` to the application-owned algorithm
version. That version must document treatment of lambdas, macros, implicit
statements, and templates in addition to the counting rules in the schema.

## Coverage and publication

Each file needs a `collection_status` row for every stage: `symbols`, `references`,
`includes`, `source_ranges`, and `metrics`. Use `not_requested` explicitly when
a stage was skipped. `complete` means the configured collection finished; it
does not establish exhaustive semantic coverage. `partial` and `failed` require
error details. A discovered external file may have all stages `not_requested`.
An empty reference result is meaningful only together with collection coverage;
it must not automatically establish that a function has no dependencies.

Build a fresh database beside the current snapshot:

1. Execute `schema.sql` and insert metadata with status `building`.
2. Enable `PRAGMA foreign_keys = ON` on **every writer connection**, before any
   transaction. Insert normalized records and collection statuses transactionally.
3. After all requested stages finish, set metadata status to `complete`, set
   `completed_at`, commit, and close writers. A failed build keeps status `failed`
   with an error. Do not silently change a failed stage to `not_requested`.
4. Run `python3 validate_snapshot.py path/to/candidate.sqlite3`. Exit status 0
   means structural checks and declared collection statuses passed. Missing
   metadata/status rows, partial/failed stages, parent cycles, foreign-key errors,
   missing metric body ranges, empty body ranges, a missing global scope, and
   unsupported schema versions block publication.
5. Only the publishing application replaces the current snapshot after successful
   validation. For a single-file replacement, checkpoint and close any WAL-mode
   database first. A failed candidate must leave the current snapshot intact.

The validator is read-only and never creates a missing database. The configurable
CLI can save intermediate models to SQLite, but snapshot publication remains a
separate application responsibility. Use a separate working database if you need
to retain a previously published complete snapshot while building a new one.

`libraries` represents logical ownership; one file has at most one owner.
Build-target membership is deferred. Includes are resolved graph edges, not a
complete record of directives or conditional preprocessing.

## Generated documentation

Keep source comments in `symbols.documentation`. `generated_documentation`
stores one current result per symbol with `pending`, `running`, `complete`,
`failed`, or `stale` status. Completed results require a comment, model identifier,
prompt version, source-input and dependency-context SHA-256 hashes, and completion
timestamp. Hash the exact inputs given to generation, including an empty dependency
context; do not use NULL to mean an empty context. Failed results require an error.

Generation and source-hash verification belong to the application. It must mark
results stale when their inputs change. Pending, failed, or stale documentation
does not prevent publication of a valid code index. Full rebuilds start without
generated rows; cross-snapshot identity and result reuse are deferred.

## Tests

After installing the optional YAML adapter dependency, run all tests with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

The schema/model/pipeline/storage tests themselves need only the standard library. See the
[adapter verification instructions](clangd_import/README.md#verification) for the
optional full Monogon import and SQLite persistence check.

### AST enrichment

The optional `clang_ast` step uses a small LLVM 21 LibTooling executable and a
Python adapter to add verified callable body ranges, versioned metrics, and
explicit statically resolved calls. It runs after YAML import and optional LSP
enrichment, retaining compiler data in the adapter and preserving the domain
model/schema. See [build instructions and semantics](clang_ast/README.md) and
[the separate-database example](examples/ast.yaml).


### Snapshot lifecycle and provenance

The configured application wraps collectors with `PrepareSnapshotStep` and
`FinalizeSnapshotStep`. Preparation populates the five reference-kind bits and
three access specifiers, records an available Git revision (with a dirty marker),
and fingerprints the YAML digest, effective compile commands/response files,
relevant configuration, and tool versions. Finalization verifies those inputs
again and sets `complete` plus the completion time after every collector returns.
Partial/failed collection outcomes remain explicit and are printed separately;
`complete` means a finished configured run, not exhaustive semantic knowledge.
Preparation alone is not checkpointed, so an import failure cannot replace an
existing index with an empty seed. A later thrown step leaves the last successful
collector checkpoint with `building` status.

AST protocol 2 additionally fills verified scope ownership, declaration details,
inheritance attributes, and override edges, resolving existing placeholders only
for observed declarations and necessary endpoints. No domain records, schema
fields, or generic pipeline semantics were added for compiler state.
