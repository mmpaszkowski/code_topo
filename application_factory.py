"""Construct concrete tools and storage at the application's entry boundary."""

from application import Application
from clang_ast import ClangAstStep, Extractor
from clangd_import import ClangdYamlReader, ClangdYamlStep, LocalSourceReader
from clangd_lsp import ClangdClient, ClangdLspStep
from clangd_lsp.step import IndexSourceProvider, SymbolIdsProvider
from clangd_lsp.static_index import StaticIndexSource
from index_pipeline import Step
from progress import NO_PROGRESS, ProgressReporter
from pipeline_config import ClangAstStepConfig, ClangdLspStepConfig, ClangdYamlStepConfig, PipelineConfig, StepConfig
from sqlite_model_store import SQLiteModelStore
from snapshot_steps import RunInputs, PrepareSnapshotStep, FinalizeSnapshotStep, CollectorCheckpointStore


def create_step(config: StepConfig, *, symbol_ids_provider: SymbolIdsProvider | None = None,
                index_source_provider: IndexSourceProvider | None = None,
                progress: ProgressReporter = NO_PROGRESS) -> Step:
    if isinstance(config, ClangdYamlStepConfig):
        reader = ClangdYamlReader(config.path, recover_known_errors=config.recover_known_errors)
        return ClangdYamlStep(reader, LocalSourceReader(), progress=progress)
    if isinstance(config, ClangdLspStepConfig):
        return ClangdLspStep(ClangdClient(config.compile_commands_dir, executable=config.executable,
                                        timeout_seconds=config.timeout_seconds,
                                        remove_compile_flags=config.remove_compile_flags),
                             exclude_paths=config.exclude_paths, symbol_ids_provider=symbol_ids_provider,
                             index_source_provider=index_source_provider, progress=progress)
    if isinstance(config, ClangAstStepConfig):
        return ClangAstStep(Extractor(config.executable, config.timeout_seconds), config.compile_commands_dir,
                            symbol_ids_provider=symbol_ids_provider,
                            remove_compile_flags=config.remove_compile_flags, exclude_paths=config.exclude_paths,
                            progress=progress)
    raise TypeError(f"Unsupported step configuration: {type(config).__name__}")


def create_application(config: PipelineConfig, *, progress: ProgressReporter = NO_PROGRESS) -> Application:
    store = None
    database = config.database
    if database is not None and database.save_after_each_step:
        store = SQLiteModelStore(database.path, progress=progress)
        store.check_destination(str(config.project_root))
    inputs = RunInputs(config, progress=progress)
    preparation = PrepareSnapshotStep(inputs, progress=progress)
    steps = [preparation]
    if store is not None:
        store = CollectorCheckpointStore(store, preparation)
    symbol_ids_provider = None
    index_source_provider = None
    for step_config in config.steps:
        step = create_step(step_config, symbol_ids_provider=symbol_ids_provider,
                           index_source_provider=index_source_provider, progress=progress)
        steps.append(step)
        if isinstance(step, ClangdYamlStep):
            # Capture this importer, but read its current report only at run time.
            symbol_ids_provider = lambda importer=step: importer.report.symbol_ids if importer.report else {}
            def index_source_provider(importer=step):
                if importer.report is None:
                    raise ValueError("Static index requires a completed YAML import")
                return StaticIndexSource(importer.report.yaml_path, importer.report.yaml_sha256)
    steps.append(FinalizeSnapshotStep(inputs, progress=progress))
    return Application(config.project_root, steps, store=store, progress=progress)
