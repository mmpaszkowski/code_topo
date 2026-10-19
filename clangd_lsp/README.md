# clangd LSP enrichment

This optional second step adds information missing from the clangd-indexer YAML
import: declaration/definition locations and extents, resolved file-to-file
includes, references, verified reference containers, and direct inheritance.
It uses existing files, symbols, and name locations from the model. It does not
discover new symbols, collect hover text, or request ASTs or metrics.

```yaml
steps:
  - type: clangd_yaml
    path: monogon-index.yaml
  - type: clangd_lsp
    compile_commands_dir: ../monogon/build
    executable: clangd
    timeout_seconds: 60
    remove_compile_flags:
      - -fprofile-abs-path
    exclude_paths:
      - build/_deps/*
```

The compilation database directory is required and must contain
`compile_commands.json`. `executable` defaults to `clangd`, found on `PATH`.
An executable containing `/` is a path relative to the configuration directory,
as is `compile_commands_dir`. The positive, finite timeout defaults to 60 seconds
per request, document-open parsing wait, or notification write. The adapter uses
only Python's standard library and POSIX pipes. It is tested with clangd 21.1.8.

`remove_compile_flags` and `exclude_paths` default to empty lists. Flag removal
matches exact standalone argument tokens; it does not remove prefixes or paired
option values. Use it for flags such as `-fprofile-abs-path`, not options such as
`-I` that take a separate argument. A `CompilationDatabase` helper reads either
`arguments` arrays or POSIX-quoted `command` strings and writes a temporary copy
for clangd. It never executes command strings, modifies the original database,
or expands response files. Entries, working directories, and remaining arguments
are preserved. The temporary copy is removed when clangd closes, including on
startup failure. With an empty removal list the original database is used directly.

Exclusion patterns use case-sensitive `fnmatchcase` against normalized,
project-relative paths with `/` separators. `*` matches path separators too, so
`build/_deps/*` excludes all nested vendored files. Excluded files keep their
imported facts and existing collection statuses. Includes pointing to them are
still recorded. Exclusions are counted separately from source failures.

Run through the existing CLI:

```sh
python3 run_pipeline.py --config config.yaml
```

`database.save_after_each_step` applies normally: the YAML result is saved before
the LSP step starts, and the enriched result is saved when the step succeeds.
Omit the `clangd_lsp` entry to retain the original YAML-only workflow.

## Responsibilities

- `ClangdClient` owns one clangd process and decodes protocol responses into
  immutable records. Its private JSON-RPC transport handles framing, request
  correlation, server messages, deadlines, stderr draining, and shutdown.
- `ClangdLspStep(client, exclude_paths=(), symbol_ids_provider=None)` selects existing project files, matches observations,
  and stages changes with `IndexBuilder.from_model()`. Each run has fresh staging
  state. The input snapshot is never changed.
- The configuration loader and application factory construct the adapter. The
  pipeline, CLI, domain model, and SQLite writer need no LSP-specific behavior.

The optional `symbol_ids_provider` is a callable returning a mapping from external
clangd IDs to local model symbol IDs for the current input snapshot. The application
factory supplies it from the preceding YAML step's existing import report. It is
called once per run, then copied and checked against the input model. Invalid
mappings fail before clangd starts. The provider must belong to the current import;
membership checks cannot detect a stale mapping that happens to reuse local IDs.
Producer identities remain in the adapters, without model, schema, or generic
pipeline changes. Standalone use can omit the provider and retain unique exact
location matching. There is no new configuration setting.

The application factory also supplies `index_source_provider`, a callback returning
`StaticIndexSource(path, sha256)` from the current import report. This enables
structural enrichment. Standalone steps without this provider keep the ranges and
includes workflow. No tool data is placed in the model or generic pipeline.

The client checks the export digest and creates a temporary static index. Only the
importer's known malformed `Kind: ... Lang: C` pattern is normalized, to separate
`Kind: Unknown` and `Lang: C` fields. The original export is never modified. Clangd
loads the copy with `--index-file`, `--sync`, `--background-index=false`, and unlimited
reference/result counts. Before any document is opened, a workspace-symbol lookup
must match a known exported project symbol's name and location. Failure to verify
the index aborts the step. The temporary index is cleaned up with the process.

`document_symbols()` and `document_links()` return immutable `CollectionResult`
values with `items` and item-level `issues` (response path and message).
`symbol_info(uri, position)` returns immutable symbol details, including optional
clangd IDs and nullable containers. Positions use the negotiated LSP encoding.

For each existing file under `project_root`, the step checks its SHA-256 against
the imported hash, opens its exact UTF-8 text in clangd, waits for parsing
diagnostics, requests document symbols and links, and closes the document. It
reuses the process across files and disables clangd background indexing. Header
compile commands may be inferred by clangd from the compilation database; results
describe that active context, not every possible build configuration. Compilation
inputs and dependencies are not independently verified against the YAML export.
Document versions increase within each session. Diagnostics for closed documents
or mismatched versions are ignored. Unversioned diagnostics remain accepted for
compatibility, so their freshness cannot be checked by version.

## Matching and coverage

Document symbols supply a name `selectionRange` and a full `range`. The adapter
converts UTF-8 or UTF-16 LSP positions to the model's UTF-8 byte columns and matches
the name range exactly against existing declaration/definition locations,
including corresponding reference records. A unique exact-location match needs
no further requests. For ambiguous locations with an identity mapping, the step
requests `textDocument/symbolInfo` once per position per file attempt, caching both
results and recoverable errors. Returned IDs are matched case-insensitively to the
import mapping. Recovery requires every returned identity to be mapped and all of
them to identify the same candidate. Missing IDs, conflicting or unmapped IDs,
unsupported requests, and malformed identity responses leave the match partial.
Only the verified candidate receives ranges; the other candidates remain unresolved.
Roles come from the imported model. Full ranges are never assumed to be body ranges.
Unmatched outlines, ambiguous matches, invalid ranges, and known occurrences not
returned in the outline produce diagnostics and partial source-range coverage.
Missing outlines do not trigger AST requests or symbol discovery.

## Structural facts

Structural queries start at a selected, resolved symbol's definition or declaration.
The step verifies its clangd identity, queries missing canonical locations and
references, and requests call/type hierarchies for the corresponding symbol kinds.
Requests and identity lookups are cached during the run. Returned source locations
must belong to an existing selected file, match its content hash, and convert to
valid model coordinates. Out-of-scope targets are skipped and reported; unknown
files and identities are not manufactured. A nested document open may be needed
to verify the identity of a newly returned location.

Missing canonical locations are filled only when there is one verified result.
Definition navigation can fall back to a declaration: an explicit matching
`symbolInfo.definitionRange` is required before filling a definition. Navigation
name ranges are never stored as full source extents. Existing canonical locations,
symbol details, and inheritance attributes are preserved.

References reuse existing IDs and locations. A verified caller can fill a missing
reference container; incompatible containers remain separate observations with a
diagnostic. Immediate type supertypes supply direct inheritance edges, with access
and virtual-inheritance fields left unknown. No extra override edges are inferred.

**Call hierarchy does not prove a call expression.** With clangd 21.1.8, both incoming
and outgoing hierarchy responses include address-taking such as `&target` as well
as `target()`. These observations therefore populate ordinary references and
verified containers, never newly set `ReferenceKind.CALL`. Existing call flags are
preserved. The adapter reports distinct `call_candidates` per attempt; precise call
classification is deferred to AST work. This behavior is covered by a real-clangd
address-taking fixture.

Reference enrichment stages immutable replacement records in the adapter and
publishes a validated snapshot, preserving existing IDs without adding a builder
update API. Repeated enrichment does not duplicate facts. Symbol/reference coverage
retains earlier gaps and records new failures. Hierarchy query/partial counts live
in the adapter report because the model has no separate hierarchy stage.

Resolved local-file document links become include edges. A new target gets a file
record with no content hash and all stages `not_requested`. Neither new targets
nor files outside the project are opened during that run. Repeated targets and
existing ranges/locations reuse existing IDs. A discovered target's absence on disk
does not erase clangd's reported edge; an unresolved or non-local target is reported
without an edge. There is no fuzzy location or symbol-name matching.

The step reports valid partial results:

- Missing, changed, unverifiable, or non-UTF-8 project sources fail both requested
  range/include stages for that file; they are not sent to clangd. Structural
  enrichment also marks its symbol/reference attempt partial without dropping facts.
- Malformed individual outline/link entries produce decoding issues and partial
  coverage. Valid siblings, valid parents with malformed children, and independently
  valid children of malformed parents remain usable.
- A rejected collection request or malformed top-level result fails that stage. The other
  stage and other files are still processed.
- Compiler errors make successful collections partial. Warnings do not do so.
- A timeout, malformed JSON-RPC traffic, or process failure aborts the entire step
  and cleans up clangd. The previous database checkpoint remains intact.

Collection status describes this attempt. Existing facts remain in the enriched
model even if the latest attempt has failed or partial coverage. Unrelated stages
are unchanged. `complete` means collection finished for the configured scope and
active context; it is not a claim of exhaustive C++ semantics. Snapshot status
remains `building`.

Progress and diagnostics are logged by the adapter. `step.report` holds immutable
counts and diagnostics after a successful run, and `summary()` exposes the generic
CLI values, including selected name occurrences matched/total and diagnostic
counts by category. An occurrence is identified by file, exact name range, symbol
ID, and declaration/definition role; one site can have both roles. Compiler errors
affect both stages and are counted once in each stage.
`occurrences_identity_resolved` counts distinct matched occurrences recovered from
ambiguous selections during this attempt, including ranges already present in the
input. Repeated outline entries do not inflate this count.
The optional `structural` report contains query counts, filled locations, added and
enriched references, call candidates, added inheritance edges, failed requests, and
hierarchy coverage. The report also includes normalized-index record counts and
elapsed seconds. Progress is logged per file and every 25 structural symbols.

Diagnostics distinguish `missing_outline`, `ambiguous_match`, `unmatched_outline`,
`invalid_range`, `source_failure`, `request_failure`, `invalid_result`,
`compiler_error`, `unresolved_include`, `decode_error`, `unresolved_identity`,
`conflict`, and `out_of_scope`. Each unmatched known occurrence includes
its symbol ID, name, role, and exact location; ambiguous matches also list candidate
IDs. No macro origin is inferred from names alone. Compiler diagnostic ranges are
preserved and converted to model coordinates. Records use zero-based lines and
UTF-8 byte columns; logs explicitly label their one-based display coordinates.
Collection-status errors store concise category counts. Full details remain in
the report and logs, without a database schema change.

Before a run or after fatal failure, the report is `None` and the
summary is empty. Reading the summary does not repeat diagnostic logging.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Tests cover exact matching, Unicode, includes, partial results, repeated runs,
configuration, identity recovery and run-time mapping refresh, partial response
decoding, diagnostic versions, compile-command filtering/cleanup, exclusions, detailed coverage
diagnostics, and a fake subprocess with fragmented messages and failed writes.
When clangd is installed, the suite also creates a temporary C++ header/source
fixture, imports YAML records, runs real LSP enrichment through the CLI, and checks
SQLite foreign keys and integrity. Fixtures also cover static-index references
from an unopened translation unit, definition recovery, direct inheritance, and
address-taking in call hierarchy. No full Monogon LSP run is part of the normal suite.
