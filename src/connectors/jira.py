"""Jira connector — loads sample tickets (swap for Jira REST API later)."""

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from src.config import SAMPLE_DIR
from src.connectors.base import BaseConnector
from src.models.document import Document, SourceType


class JiraConnector(BaseConnector):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SAMPLE_DIR / "jira" / "tickets.json"

    def iter_documents(self) -> Iterator[Document]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for ticket in raw:
            comments = ticket.get("comments") or []
            comment_text = "\n".join(f"- {c['author']}: {c['body']}" for c in comments)
            body_parts = [
                ticket.get("description") or "",
                f"Status: {ticket.get('status')}",
                f"Labels: {', '.join(ticket.get('labels') or [])}",
                f"Assignee: {ticket.get('assignee')}",
            ]
            if comment_text:
                body_parts.append("Comments:\n" + comment_text)

            yield Document(
                id=f"jira:{ticket['key']}",
                source=SourceType.JIRA,
                title=f"{ticket['key']}: {ticket['summary']}",
                body="\n\n".join(p for p in body_parts if p),
                url=ticket.get("url") or f"https://jira.example.com/browse/{ticket['key']}",
                author=ticket.get("reporter"),
                updated_at=_parse_dt(ticket.get("updated")),
                metadata={
                    "key": ticket["key"],
                    "status": ticket.get("status"),
                    "labels": ticket.get("labels") or [],
                },
            )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # Python 3.11+ parses the trailing "Z" natively.
    return datetime.fromisoformat(value)
