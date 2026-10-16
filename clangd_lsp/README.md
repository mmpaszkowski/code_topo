# clangd LSP enrichment

This optional second step adds information missing from the clangd-indexer YAML
import: full declaration/definition ranges and resolved file-to-file includes.
It uses existing files, symbols, and name locations from the model. It does not
scan the project or collect signatures, documentation, references, calls, or metrics.

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
- `ClangdLspStep(client, exclude_paths=())` selects existing project files, matches observations,
  and stages changes with `IndexBuilder.from_model()`. Each run has fresh staging
  state. The input snapshot is never changed.
- The configuration loader and application factory construct the adapter. The
  pipeline, CLI, domain model, and SQLite writer need no LSP-specific behavior.

For each existing file under `project_root`, the step checks its SHA-256 against
the imported hash, opens its exact UTF-8 text in clangd, waits for parsing
diagnostics, requests document symbols and links, and closes the document. It
reuses the process across files and disables clangd background indexing. Header
compile commands may be inferred by clangd from the compilation database; results
describe that active context, not every possible build configuration. Compilation
inputs and dependencies are not independently verified against the YAML export.

## Matching and coverage

Document symbols supply a name `selectionRange` and a full `range`. The adapter
converts UTF-8 or UTF-16 LSP positions to the model's UTF-8 byte columns and matches
the name range exactly against existing declaration/definition locations,
including corresponding reference records. Only a unique symbol match is accepted.
Roles come from the imported model. Full ranges are never assumed to be body ranges.
Unmatched outlines, ambiguous matches, invalid ranges, and known occurrences not
returned in the outline produce diagnostics and partial source-range coverage.

Resolved local-file document links become include edges. A new target gets a file
record with no content hash and all stages `not_requested`. Neither new targets
nor files outside the project are opened during that run. Repeated targets and
existing ranges/locations reuse existing IDs. A discovered target's absence on disk
does not erase clangd's reported edge; an unresolved or non-local target is reported
without an edge. There is no fuzzy location or symbol-name matching.

The step reports valid partial results:

- Missing, changed, unverifiable, or non-UTF-8 project sources fail both requested
  stages for that file; they are not sent to clangd.
- A rejected request or invalid result fails that collection stage. The other
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

Diagnostics distinguish `missing_outline`, `ambiguous_match`, `unmatched_outline`,
`invalid_range`, `source_failure`, `request_failure`, `invalid_result`,
`compiler_error`, and `unresolved_include`. Each unmatched known occurrence includes
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
configuration, compile-command filtering/cleanup, exclusions, detailed coverage
diagnostics, and a fake subprocess with fragmented messages and failed writes.
When clangd is installed, the suite also creates a temporary C++ header/source
fixture, imports YAML records, runs real LSP enrichment through the CLI, and checks
SQLite foreign keys and integrity. No full Monogon LSP run is part of the normal suite.
