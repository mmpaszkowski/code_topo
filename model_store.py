"""Storage boundary for immutable index snapshots."""

from abc import ABC, abstractmethod

from index_model import IndexModel


class ModelStore(ABC):
    @abstractmethod
    def save(self, model: IndexModel) -> None:
        """Save the model, preserving the previous saved state on failure."""
        raise NotImplementedError
