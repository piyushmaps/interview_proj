"""City map connector — multi-format source.

The city map "source" is really a folder of heterogeneous artifacts:
- capabilities.json  → the capability graph (nodes + edges)
- *.csv / *.tsv      → sheet exports, from a 5-row roster to a 900k-row dump
- *.html             → portal pages
- *.pdf              → architecture docs, textbooks
- *.eml              → long-tail formats (announcements, memos)

Everything converges to the same normalized Document schema; downstream
chunking/indexing never knows which format a document came from.

Two production properties matter more than the format list:

* **Nothing accumulates.** Documents are yielded, never collected, so a folder
  holding hundreds of gigabytes streams through in constant memory.
* **One bad file cannot end the run.** Extraction failures are logged and
  skipped. A corrupt PDF discovered nine hours into ingestion should cost that
  file, not the job.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from src.config import SAMPLE_DIR, get_settings
from src.connectors.base import BaseConnector
from src.ingestion.extractors import (
    extract_file,
    is_supported,
    iter_pdf_parts,
    iter_sheet_rows,
    profile_sheet,
    render_profile,
)
from src.models.document import Document, SourceType

logger = logging.getLogger(__name__)

GRAPH_FILE = "capabilities.json"
SHEET_SUFFIXES = {".csv", ".tsv"}


class CityMapConnector(BaseConnector):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SAMPLE_DIR / "city_map"
        self.failures: list[tuple[str, str]] = []

    def iter_documents(self) -> Iterator[Document]:
        yield from self._iter_graph(self.path / GRAPH_FILE)
        for file in sorted(self.path.rglob("*")):
            if not self._should_read(file):
                continue
            try:
                yield from self._iter_file(file)
            except Exception as exc:  # noqa: BLE001 - one file must not end the run
                self.failures.append((file.name, f"{type(exc).__name__}: {exc}"))
                logger.warning("skipping %s — %s: %s", file.name, type(exc).__name__, exc)

    def _should_read(self, file: Path) -> bool:
        if not file.is_file() or file.name == GRAPH_FILE:
            return False
        # macOS AppleDouble sidecars carry a real extension but binary content.
        if file.name.startswith("._"):
            return False
        return file.suffix.lower() in SHEET_SUFFIXES or is_supported(file)

    # -- capability graph (JSON) ----------------------------------------------

    def _iter_graph(self, path: Path) -> Iterator[Document]:
        # The graph is one artifact among many in this folder, not a precondition
        # for the rest — a drop of PDFs and sheets should still ingest.
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in raw.get("nodes") or []}
        deps: dict[str, list[str]] = defaultdict(list)
        for edge in raw.get("edges") or []:
            deps[edge["from"]].append(f"{edge['type']} → {edge['to']}")

        for node_id, node in nodes.items():
            dep_lines = deps.get(node_id) or ["(none)"]
            body = (
                f"Capability: {node['name']} ({node_id})\n"
                f"Owner team: {node.get('owner_team')}\n"
                f"Owner lead: {node.get('owner_lead')}\n"
                f"Status: {node.get('status')}\n"
                f"SLA: {node.get('sla')}\n"
                f"Description: {node.get('description')}\n"
                f"Dependencies:\n- " + "\n- ".join(dep_lines)
            )
            yield Document(
                id=f"city_map:{node_id}",
                source=SourceType.CITY_MAP,
                title=f"City Map: {node['name']}",
                body=body,
                url=f"https://citymap.example.com/nodes/{node_id}",
                author=node.get("owner_lead"),
                metadata={
                    "node_id": node_id,
                    "owner_team": node.get("owner_team"),
                    "status": node.get("status"),
                    "format": "graph",
                },
            )

    # -- extracted formats (pdf / html / eml / ...) ----------------------------

    def _iter_file(self, file: Path) -> Iterator[Document]:
        if file.suffix.lower() in SHEET_SUFFIXES:
            yield from self._iter_sheet(file)
            return
        if file.suffix.lower() == ".pdf":
            yield from self._iter_pdf(file)
            return

        extracted = extract_file(file)
        if extracted is None or not extracted.text.strip():
            return

        title = (
            extracted.metadata.get("html_title")
            or extracted.metadata.get("subject")
            or file.stem.replace("-", " ").title()
        )
        yield Document(
            id=f"city_map:{self._key(file)}",
            source=SourceType.CITY_MAP,
            title=f"City Map: {title}",
            body=extracted.text,
            url=f"https://citymap.example.com/files/{file.name}",
            metadata={"path": file.name, **extracted.metadata},
        )

    def _iter_pdf(self, file: Path) -> Iterator[Document]:
        """Large PDFs become one document per page range, not one giant blob."""
        key = self._key(file)
        title = file.stem.replace("-", " ").replace("_", " ").strip()
        for part in iter_pdf_parts(file, get_settings().pdf_pages_per_document):
            start, end = part.metadata["page_start"], part.metadata["page_end"]
            single = part.metadata["page_count"] <= get_settings().pdf_pages_per_document
            yield Document(
                id=f"city_map:{key}" if single else f"city_map:{key}:p{start}-{end}",
                source=SourceType.CITY_MAP,
                title=f"City Map: {title}" if single
                else f"City Map: {title} (pages {start}-{end})",
                body=part.text,
                url=f"https://citymap.example.com/files/{file.name}#page={start}",
                metadata={"path": file.name, **part.metadata},
            )

    def _key(self, file: Path) -> str:
        """Stable id fragment. Uses the path relative to the source root so two
        files with the same name in different folders do not collide."""
        return file.relative_to(self.path).as_posix().replace("/", ":")

    # -- sheets ----------------------------------------------------------------

    def _iter_sheet(self, file: Path) -> Iterator[Document]:
        """Small reference sheets are enumerated; large tables are described.

        Enumerating every row only makes sense when rows are individually
        meaningful — an ownership roster, where "who owns Payments" should match
        one row. A 900k-row export of codes and coordinates is the opposite: the
        rows embed to nearly the same point, so retrieval over them is noise, and
        embedding them costs hours for nothing. Those get a profile instead:
        schema, column statistics, and sample rows, in a single searchable
        document. Rows that genuinely carry prose are indexed too, up to a cap.
        """
        settings = get_settings()
        profile = profile_sheet(file)
        sheet_name = file.stem.replace("-", " ").title()

        if profile.row_count <= settings.csv_small_sheet_rows:
            yield from self._iter_rows(file, sheet_name, limit=None)
            return

        yield Document(
            id=f"city_map:{self._key(file)}:profile",
            source=SourceType.CITY_MAP,
            title=f"City Map: {sheet_name} (dataset profile)",
            body=render_profile(file, profile).text,
            url=f"https://citymap.example.com/files/{file.name}",
            metadata={
                "path": file.name,
                "format": "sheet_profile",
                "row_count": profile.row_count,
            },
        )
        if profile.is_text_bearing:
            yield from self._iter_rows(file, sheet_name, limit=settings.csv_max_text_rows)

    def _iter_rows(self, file: Path, sheet_name: str, limit: int | None) -> Iterator[Document]:
        key = self._key(file)
        for i, line in iter_sheet_rows(file, limit=limit):
            if not line.strip():
                continue
            # First "header: value" pair labels the row (e.g. "capability: Payments")
            first_value = line.split(" | ", 1)[0].split(": ", 1)[-1]
            yield Document(
                id=f"city_map:{key}:row-{i}",
                source=SourceType.CITY_MAP,
                title=f"City Map: {sheet_name} — {first_value[:60]}",
                body=line,
                url=f"https://citymap.example.com/files/{file.name}#row={i}",
                metadata={"path": file.name, "format": "sheet", "row": i},
            )
