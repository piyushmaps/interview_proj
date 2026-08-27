"""Base connector interface.

`iter_documents` is the real contract; `load` is a convenience for tests and
small sources. Streaming is the primary shape because a connector may sit in
front of a 200 GB export, and nothing downstream should require the whole
source to fit in memory at once.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator

from src.models.document import Document


class BaseConnector(ABC):
    """Pulls raw content from a source and emits normalized Documents."""

    @abstractmethod
    def iter_documents(self) -> Iterator[Document]:
        """Yield normalized documents lazily, one at a time."""

    def load(self) -> list[Document]:
        """Materialize every document. Only safe for sources known to be small."""
        return list(self.iter_documents())
