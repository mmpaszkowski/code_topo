"""Sequential, tool-independent steps over immutable index snapshots."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
import logging

from index_model import IndexModel
from model_store import ModelStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SummaryItem:
    """One labeled value a step exposes for display, independent of its tools."""

    label: str
    value: str | int


class Step(ABC):
    @abstractmethod
    def run(self, model: IndexModel) -> IndexModel:
        """Return an enriched, validated snapshot without changing the input.

        Concrete steps receive their tools and configuration through constructors.
        Check prerequisites, collect data, and return a local builder's build().
        Raise an exception when execution cannot continue.
        """
        raise NotImplementedError

    def summary(self) -> tuple[SummaryItem, ...]:
        """Return summary values from the latest run; reporting is optional."""
        return ()


class Pipeline:
    def __init__(self, steps: Sequence[Step], *, store: ModelStore | None = None) -> None:
        self._steps = tuple(steps)
        self._store = store

    def run(self, model: IndexModel) -> IndexModel:
        """Run and optionally save each result before starting the next step."""
        if not isinstance(model, IndexModel):
            raise TypeError("Pipeline.run requires an IndexModel")
        for number, step in enumerate(self._steps, 1):
            name = type(step).__name__
            logger.info("Starting step %d/%d: %s", number, len(self._steps), name)
            try:
                result = step.run(model)
                if not isinstance(result, IndexModel):
                    raise TypeError(f"{name}.run must return an IndexModel")
                if self._store is not None:
                    self._store.save(result)
            except Exception:
                logger.exception("Failed step %d/%d: %s", number, len(self._steps), name)
                raise
            model = result
            logger.info("Completed step %d/%d: %s", number, len(self._steps), name)
        return model
