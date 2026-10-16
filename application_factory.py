"""Construct concrete tools and storage at the application's entry boundary."""

from application import Application
from clangd_import import ClangdYamlReader, ClangdYamlStep, LocalSourceReader
from clangd_lsp import ClangdClient, ClangdLspStep
from index_pipeline import Step
from pipeline_config import ClangdLspStepConfig, ClangdYamlStepConfig, PipelineConfig, StepConfig
from sqlite_model_store import SQLiteModelStore


def create_step(config: StepConfig) -> Step:
    if isinstance(config, ClangdYamlStepConfig):
        reader = ClangdYamlReader(config.path, recover_known_errors=config.recover_known_errors)
        return ClangdYamlStep(reader, LocalSourceReader())
    if isinstance(config, ClangdLspStepConfig):
        return ClangdLspStep(ClangdClient(config.compile_commands_dir, executable=config.executable,
                                        timeout_seconds=config.timeout_seconds,
                                        remove_compile_flags=config.remove_compile_flags),
                             exclude_paths=config.exclude_paths)
    raise TypeError(f"Unsupported step configuration: {type(config).__name__}")


def create_application(config: PipelineConfig) -> Application:
    store = None
    database = config.database
    if database is not None and database.save_after_each_step:
        store = SQLiteModelStore(database.path)
        store.check_destination(str(config.project_root))
    steps = tuple(create_step(step) for step in config.steps)
    return Application(config.project_root, steps, store=store)
