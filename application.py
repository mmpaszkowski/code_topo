"""Run supplied steps from a fresh model, with optional persistence."""

from collections.abc import Sequence
from pathlib import Path

from index_model import IndexBuilder, IndexMetadata, IndexModel
from index_pipeline import Pipeline, Step
from model_store import ModelStore
from progress import NO_PROGRESS, ProgressReporter


class Application:
    def __init__(self, project_root: Path, steps: Sequence[Step], *, store: ModelStore | None = None,
                 progress: ProgressReporter = NO_PROGRESS) -> None:
        self._project_root = project_root
        self._steps = tuple(steps)
        self._pipeline = Pipeline(self._steps, store=store, progress=progress)

    @property
    def steps(self) -> tuple[Step, ...]:
        return self._steps

    def run(self) -> IndexModel:
        builder = IndexBuilder()
        builder.set_metadata(IndexMetadata(project_root=str(self._project_root)))
        return self._pipeline.run(builder.build())
