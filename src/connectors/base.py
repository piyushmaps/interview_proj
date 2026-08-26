"""Base connector interface."""

from abc import ABC, abstractmethod

from src.models.document import Document


class BaseConnector(ABC):
    """Pulls raw content from a source and emits normalized Documents."""

    @abstractmethod
    def load(self) -> list[Document]:
        """Return normalized documents for indexing."""
