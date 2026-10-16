"""Construct concrete tools and storage at the application's entry boundary."""

from application import Application
from clangd_import import ClangdYamlReader, ClangdYamlStep, LocalSourceReader
from index_pipeline import Step
from pipeline_config import ClangdYamlStepConfig, PipelineConfig
from sqlite_model_store import SQLiteModelStore


def create_step(config: ClangdYamlStepConfig) -> Step:
    reader = ClangdYamlReader(config.path, recover_known_errors=config.recover_known_errors)
    return ClangdYamlStep(reader, LocalSourceReader())


def create_application(config: PipelineConfig) -> Application:
    store = None
    database = config.database
    if database is not None and database.save_after_each_step:
        store = SQLiteModelStore(database.path)
        store.check_destination(str(config.project_root))
    steps = tuple(create_step(step) for step in config.steps)
    return Application(config.project_root, steps, store=store)
