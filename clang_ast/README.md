# Clang AST enrichment

`ClangAstStep` adds callable definition/body ranges, body metrics, and explicit
compiler-resolved calls, symbol details, scope owners, inheritance, and overrides.
Existing placeholders can be resolved through verified compiler identities. It does not change the
model or database schema. The factory passes the current YAML importer identity
map through a callback, just as it does for LSP enrichment.

## Build and run

Install LLVM 21 development packages (`llvm-21-dev`, `libclang-21-dev`, and
`libclang-cpp21-dev` on Ubuntu). Build with a matching compiler:

```sh
cmake -S native/clang_ast -B build/clang_ast \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=clang++-21 \
  -DLLVM_DIR=/usr/lib/llvm-21/lib/cmake/llvm
cmake --build build/clang_ast -j2
```

Append a step after `clangd_yaml`, optionally after `clangd_lsp`:

```yaml
  - type: clang_ast
    compile_commands_dir: ../monogon/build
    executable: ./build/clang_ast/indexer-clang-ast
    timeout_seconds: 120
    remove_compile_flags: [-fprofile-abs-path]
    exclude_paths: [build/_deps/*]
```

Paths are relative to the configuration file. The default executable is
`indexer-clang-ast` on PATH. Exclusions are project-relative glob patterns.
The main `config.yaml` includes this step after LSP enrichment; `examples/ast.yaml`
runs Monogon into a separate database.

```sh
python3 run_pipeline.py --config examples/ast.yaml
INDEXER_AST_EXECUTABLE="$PWD/build/clang_ast/indexer-clang-ast" \
  python3 -W error::ResourceWarning -m unittest discover -s tests -q
```

Native integration tests skip with an explicit reason if the executable is not
configured. Adapter/protocol/configuration tests always run.

## Compiler boundary

The C++ executable uses LibTooling, syntax-only argument adjustment, and no shell
execution. Python runs every distinct selected compilation command sequentially,
preserving its working directory and semantic flags. Output/dependency options
are removed. Headers are observed through the translation units that include
them; headers without an observed context do not acquire complete coverage.

Protocol 2 takes a JSON request with `protocol`, a single normalized compilation
`command`, the selected indexed `files`, allowed `metadata_files`, and existing clangd `ids`. It returns JSON Lines:

- `version`: protocol number and LLVM major version (currently 2 and 21).
- `file`: absolute path and SHA-256 of the compiler's source buffer.
- `declaration`: USR, semantic parent USR, name/kind/language, verified name
  locations, signature/return type/template arguments/source comment, and body eligibility.
- `inheritance` / `override`: direct endpoint USRs and inheritance access/virtual attributes.
  Inheritance's optional boolean `existing_edge_only` defaults to false. When true,
  the observation can enrich only an edge present in the input snapshot.
- `body`: declaration USR, body/definition byte extents, and metrics.
- `call`: target/container USRs and spelled callee/operator byte extent.
- `issue` or `diagnostic`: path, message, and diagnostic error severity.
- `complete`: explicit success or failure of this compilation context.

Raw offsets, compiler identities, context hashes, and reports stay inside this
adapter. Python hashes USRs using clangd's truncated SHA-1 convention, accepts
unique mappings to existing symbols, verifies indexed/current/compiler
source hashes, and converts byte offsets to model positions. No matching by
name/signature and no new-symbol discovery occur. Selected declarations and needed
parent/base/override/call endpoints may resolve existing placeholders, including
endpoint declarations in excluded dependencies. Dependency bodies remain excluded.
Source buffers are checked again before publication.

No semantic observations from a failed, timed-out, crashed, or malformed-output
context are merged. Other contexts continue. A failed context conservatively
marks all selected indexed files partial because its include inventory may be
incomplete. Earlier coverage errors are retained. Diagnostics affect their relevant
stages: call gaps affect references, body gaps affect metrics/ranges, and declaration
or relationship gaps affect symbols. Compiler warnings remain in the report without
making coverage partial. Expected absent bodies (declaration-only, defaulted,
deleted, implicit) are distinguished from unsupported or unobserved written bodies.
Diagnostics and eligibility counts are available through `step.report`.
Setup/version errors stop the step; no result is published.

## Metrics version: clang-ast-body-v1

Only explicit function/method definitions with source-backed bodies are analyzed.
Constructors/destructors with written bodies are included. Coroutine bodies are
deferred with diagnostics. Implicit functions and
duplicate implicit template instantiations are excluded. Template patterns are
analyzed as written, including dependent constructs. Macro-expanded statements
count semantically, but bodies whose boundary tokens are macro-generated are
omitted with diagnostics. Local class method bodies are separate analyses.

- `statement_count`: explicit non-expression statement nodes, excluding compound
  blocks; includes labels, case/default labels, handlers, and declaration
  statements. An expression counts once when used as a statement (including a
  classic for-loop initializer), not once for each expression subtree.
- `if_count`: every `if`, including `else if`; excludes conditional expressions.
- `switch_count`, `case_count`: switch statements and case labels; default labels
  do not count as cases.
- `loop_count`: for, range-for, while, and do statements. Desugared range-for
  variables, loops, and calls do not contribute.
- `max_nesting_depth`: nested if/switch/loop/try constructs; zero without them.
- `cyclomatic_complexity`: 1 + ifs + loops + cases + catches + built-in `&&` +
  built-in `||` + conditional expressions. Overloaded logical calls are not
  short-circuit decisions.
- Remaining control counters count written return/goto/break/continue/try/catch/
  throw constructs.
- `parameter_count`: parameters of the owning function. `local_variable_count`:
  explicit local variable declarations, including catch variables and range-for
  variables, excluding parameters and compiler-generated temporaries.
- `lambda_count`: lambda expressions. Lambda bodies contribute to enclosing
  metrics as required by the schema; their parameters do not contribute to the
  enclosing parameter count. Local-class method statements do not contribute.
- Metrics describe syntax, including unevaluated expressions and both written
  constexpr branches; they do not estimate runtime execution or reachability.

Identical body/metrics observations merge across contexts. Conflicting body
locations or metrics leave new `SymbolBody` facts unfilled, retain independently
valid source ranges, and produce partial coverage. Existing facts are preserved
and disagreements reported. Different metrics versions cannot be mixed.

## Call semantics

`CALL | REFERENCE` denotes an explicit ordinary/member/operator call expression
with a known statically selected target, not proof of runtime execution. A
virtual call targets the selected declaration, not every possible override.
The location is the callee name or operator token, not the entire expression.

Address-taking, unevaluated operands, discarded constexpr branches, constructor
expressions, implicit default arguments/initializers, and lowered range-for
operations do not become calls. Unresolved indirect/dependent calls, conversion
calls, typeid operand analysis, and macro-generated callee locations are deferred
with diagnostics. Calls in lambdas use the lambda call operator as container;
when that symbol is absent from the index the container stays unknown.

Matching references retain their IDs and existing flags. Conflicting targets or
containers across contexts are not classified. A call present only in one build
configuration can be recorded as an observed fact. The model does not claim it
executes in all configurations.

The step reports counts and coverage, not exhaustive indexing. Construction calls,
expanded conversion/macro calls, and runtime call-target analysis remain later work.

## Structural enrichment

Semantic parent identities link scopes, including out-of-line methods. Scopes with
one verified owner are consolidated to their lowest reusable existing ID; names
alone never merge anonymous namespaces or overload-local scopes. Symbol/reference
IDs remain stable. The adapter replays changed rows through public builder methods
to preserve canonical immutable records without extending domain APIs.

Missing signatures use Clang's printed parameter types/names, variadic marker,
method cv/ref qualifiers, and exception specification. Default argument expressions
and bodies are excluded. Return type is separate and stays NULL for constructors
and destructors. Source comments and template arguments fill existing fields;
generated documentation is not touched. Existing non-NULL facts are retained;
alternate/conflicting renderings and context disagreements are reported. Individual
field conflicts do not discard unrelated facts.

Direct bases include effective access (including class/struct defaults) and virtual
inheritance. Overrides come from compiler-established direct override relationships.
For a dependent named class-template base such as `Derived<T> : Base<T>`, the
compiler's template declaration identity may enrich an existing imported edge.
This fills access and virtuality without choosing a specialization or creating a
new relationship. Unmatched pairs and genuinely unresolved bases such as `D : T`
retain diagnostics. Dependency exclusions still apply to derived classes.
There is no matching by name, transitive inheritance, or runtime-dispatch inference.
