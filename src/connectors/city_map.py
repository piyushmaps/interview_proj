"""City map connector — multi-format source.

The city map "source" is really a folder of heterogeneous artifacts:
- capabilities.json  → the capability graph (nodes + edges)
- *.csv              → Google Sheet exports (e.g. ownership roster)
- *.html             → portal pages
- *.pdf              → architecture docs
- *.eml              → long-tail formats (announcements, memos)

Everything converges to the same normalized Document schema; downstream
chunking/indexing never knows which format a document came from.
"""

import json
from collections import defaultdict
from pathlib import Path

from src.config import SAMPLE_DIR
from src.connectors.base import BaseConnector
from src.ingestion.extractors import extract_file
from src.models.document import Document, SourceType

GRAPH_FILE = "capabilities.json"


class CityMapConnector(BaseConnector):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SAMPLE_DIR / "city_map"

    def load(self) -> list[Document]:
        docs = self._load_graph(self.path / GRAPH_FILE)
        for file in sorted(self.path.iterdir()):
            if file.name == GRAPH_FILE or not file.is_file():
                continue
            docs.extend(self._load_file(file))
        return docs

    # -- capability graph (JSON) --------------------------------------------

    def _load_graph(self, path: Path) -> list[Document]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in raw.get("nodes") or []}
        deps: dict[str, list[str]] = defaultdict(list)
        for edge in raw.get("edges") or []:
            deps[edge["from"]].append(f"{edge['type']} → {edge['to']}")

        docs: list[Document] = []
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
            docs.append(
                Document(
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
            )
        return docs

    # -- extracted formats (pdf / html / csv / eml / ...) --------------------

    def _load_file(self, file: Path) -> list[Document]:
        extracted = extract_file(file)
        if extracted is None or not extracted.text.strip():
            return []

        # Sheets: one document per row so a query matches the relevant row,
        # not the whole spreadsheet.
        if extracted.metadata.get("format") == "sheet":
            return self._sheet_row_docs(file, extracted.text)

        title = (
            extracted.metadata.get("html_title")
            or extracted.metadata.get("subject")
            or file.stem.replace("-", " ").title()
        )
        return [
            Document(
                id=f"city_map:{file.stem}",
                source=SourceType.CITY_MAP,
                title=f"City Map: {title}",
                body=extracted.text,
                url=f"https://citymap.example.com/files/{file.name}",
                metadata={"path": file.name, **extracted.metadata},
            )
        ]

    def _sheet_row_docs(self, file: Path, text: str) -> list[Document]:
        sheet_name = file.stem.replace("-", " ").title()
        docs: list[Document] = []
        for i, line in enumerate(text.splitlines(), start=1):
            # First "header: value" pair labels the row (e.g. "capability: Payments")
            first_value = line.split(" | ", 1)[0].split(": ", 1)[-1]
            docs.append(
                Document(
                    id=f"city_map:{file.stem}:row-{i}",
                    source=SourceType.CITY_MAP,
                    title=f"City Map: {sheet_name} — {first_value}",
                    body=line,
                    url=f"https://citymap.example.com/files/{file.name}#row={i}",
                    metadata={"path": file.name, "format": "sheet", "row": i},
                )
            )
        return docs
