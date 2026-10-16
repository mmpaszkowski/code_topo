# clangd YAML import

The adapter reads an existing clangd-indexer export into an in-memory model. It
uses Python 3.11+ and PyYAML 6. The model and pipeline remain standard-library-only.

```sh
python3 -m pip install -r requirements-clangd.txt
```

## Use the step

```python
from pathlib import Path
from clangd_import import ClangdYamlReader, ClangdYamlStep, LocalSourceReader
from index_model import IndexBuilder, IndexMetadata
from index_pipeline import Pipeline

builder = IndexBuilder()
builder.set_metadata(IndexMetadata(project_root="/home/noname/Workspace/monogon"))
step = ClangdYamlStep(ClangdYamlReader(Path("monogon-index.yaml")), LocalSourceReader())
model = Pipeline([step]).run(builder.build())
report = step.report

symbol_id = report.symbol_ids["44EDD91B88949A8D"]
symbol = model.symbol(symbol_id)
original = report.exported_symbols[symbol_id].symbol
```

The runnable example prints progress and a JSON summary:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m examples.import_clangd_yaml monogon-index.yaml \
  --project-root /home/noname/Workspace/monogon
```

It does not persist a database or report. Applications retain the model and report
together. Later steps use the report's ID map to associate clangd IDs with model IDs.
The [configurable CLI](../README.md#save-after-each-step) can save the resulting
model to SQLite after each step; importer reports remain in memory.

## Responsibilities and lifecycle

| Class | Responsibility |
| --- | --- |
| `ClangdYamlReader` | Stream YAML documents into immutable export records. |
| `LocalSourceReader` | Read local file URIs, decoding percent escapes. |
| `_ClangdImport` | Translate records and own one run's builder, caches and diagnostics. |
| `ClangdYamlStep` | Run the import, log diagnostics, and expose its report and summary after model validation. |

The readers are constructor dependencies. Stateless helpers such as
`utf8_position()` are ordinary functions. Field mapping and dispatch are explicit.

Every run uses fresh working state. The seed must have `building` metadata and
only the global scope; lookup rows are allowed, indexed entities are not. Failure
propagates its original exception, closes the YAML stream, leaves the seed unchanged,
and leaves `step.report` as `None`. Previously retained snapshots and reports remain
immutable. Concurrent calls on the same step instance are not supported.

`summary()` returns the common pipeline `SummaryItem` records for display, or an
empty tuple before a run or after an import failure. The detailed `report` remains
available to callers that need clangd-specific data. Diagnostics are logged once
at the end of a successful import, before the pipeline saves the model; reading
the summary does not log them again. Applications configure standard logging.

## Mapping rules

- Allocate local IDs in first-encounter order; retain opaque external IDs in the
  report. Missing symbols become placeholders. A zero ID means an unknown reference
  container and is rejected elsewhere.
- Map languages and kinds explicitly. Unknown kinds remain `None` with diagnostics;
  unsupported languages or predicates fail. Empty optional strings become `None`.
  `ReturnType` populates the model only for callable symbol kinds; other type and
  completion information remains in the original symbol records in the report.
- Give non-global scope labels separate unknown-owner records. Matching labels do
  not prove identity or ownership. Later scope resolution may need explicit builder
  operations beyond the current symbol-enrichment API.
- Import preferred name locations, references, inheritance and overrides. Name
  locations are not full declaration/body ranges. Include suggestions remain in
  the report and do not become file-include edges.
- Map reference bits 1, 2, 4 and 8 explicitly. LLVM 21 sets bit 16 based on callable
  target kind, including declarations. Preserve its original value in the report,
  but never set the model's `CALL` bit. Require an occurrence bit and reject unknown
  bits. Repeated normalized references share one model record while every original
  occurrence retains a report entry.
- Predicate 0 maps subject to base and object to derived; predicate 1 maps subject
  to overridden method and object to overriding method. Access and virtuality stay
  unknown. Defer identical endpoints to the report with diagnostics; deduplicate
  repeated representable edges and count them.
- Recover only the observed `SymInfo` line containing `Kind: ... Lang: ... C`.
  Keep language C and leave kind unknown. `recover_known_errors=False` on the reader
  disables this recovery. Other malformed YAML, duplicate keys, recursive aliases,
  invalid IDs, and unknown document tags fail the import.

Every discovered file gets all five coverage statuses. Symbols/references coverage
describes importing this export, not exhaustive semantics. Missing sources, invalid
coordinates, unknown/recovered kinds and deferred relations make affected stages
partial where a file is known. Unattributable issues remain in the report. Includes,
full source ranges and metrics stay `not_requested`; metadata stays `building`.

LLVM 21 sources defining these mappings:
[SymbolCollector.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-21.1.8/clang-tools-extra/clangd/index/SymbolCollector.cpp),
[SymbolLocation.h](https://github.com/llvm/llvm-project/blob/llvmorg-21.1.8/clang-tools-extra/clangd/index/SymbolLocation.h),
[Symbol.h](https://github.com/llvm/llvm-project/blob/llvmorg-21.1.8/clang-tools-extra/clangd/index/Symbol.h),
[Relation.h](https://github.com/llvm/llvm-project/blob/llvmorg-21.1.8/clang-tools-extra/clangd/index/Relation.h).

## Source assumption and report

The YAML has no indexed source hashes. As agreed, current files are assumed to match
the export. The importer records this assumption, reads each URI once per run, hashes
those bytes, and converts UTF-16 columns to UTF-8 byte columns against the same bytes.
These hashes are not independent proof of freshness. Paths come from the export,
including external headers; `project_root` is metadata, not a read restriction or remap.

Missing files, invalid UTF-8 at an endpoint, split surrogates, out-of-range positions,
and saturated LLVM coordinates make the affected location unavailable. Retain symbol
metadata, omit references requiring that location, and record diagnostics and partial
coverage. LF separates lines; CR and tabs remain bytes.

The immutable report holds input path/hash and typed counts, external-to-local ID
maps, original parsed symbols, source hashes, deferred relations, diagnostics, and
one `ReferenceImport` per exported occurrence. Reference entries preserve original
flags, the model reference ID (or `None` when omitted), and YAML positions. Original
symbol records preserve empty strings, include suggestions, flags and completion data.

Document ordinals and reference indexes are zero-based; YAML lines are one-based.
Parse errors identify exact offending lines; import diagnostics identify document
starts and fields. Unknown fields remain in the original YAML. Keep that artifact
and verify its hash before inspecting raw data using report positions; the report
does not retain all raw YAML bytes.

## Verification

Run the regression suite after installing the adapter dependency:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

The optional acceptance test requires the supplied export and its source files:

```sh
RUN_MONOGON_IMPORT_TEST=1 PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning \
  -m unittest discover -s tests -p test_monogon_import.py -v
```

Expected: 28,551 symbols (8,416 placeholders), 195,945 references, 112,106 locations,
647 files, 682 inheritance edges, 380 overrides, 71 recovered kinds and one deferred
self-inheritance relation.
