"""DevPortal connector — loads Markdown docs."""

from collections.abc import Iterator
from pathlib import Path

from src.config import SAMPLE_DIR
from src.connectors.base import BaseConnector
from src.models.document import Document, SourceType


class DevPortalConnector(BaseConnector):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SAMPLE_DIR / "devportal"

    def iter_documents(self) -> Iterator[Document]:
        for md_file in sorted(self.path.glob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            title = _first_heading(text) or md_file.stem.replace("-", " ").title()
            yield Document(
                id=f"devportal:{md_file.stem}",
                source=SourceType.DEVPORTAL,
                title=title,
                body=text,
                url=f"https://devportal.example.com/docs/{md_file.stem}",
                metadata={"path": str(md_file.name)},
            )


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None
