"""Slack connector — one document per thread."""

import json
from pathlib import Path

from src.config import SAMPLE_DIR
from src.connectors.base import BaseConnector
from src.models.document import Document, SourceType


class SlackConnector(BaseConnector):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SAMPLE_DIR / "slack" / "threads.json"

    def load(self) -> list[Document]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        docs: list[Document] = []
        for thread in raw:
            messages = thread.get("messages") or []
            if not messages:
                continue
            body = "\n".join(f"{m['user']}: {m['text']}" for m in messages)
            root = messages[0]
            title = f"#{thread['channel']} — {root['text'][:80]}"
            docs.append(
                Document(
                    id=f"slack:{thread['thread_ts']}",
                    source=SourceType.SLACK,
                    title=title,
                    body=body,
                    url=thread.get("url") or "",
                    author=root.get("user"),
                    metadata={
                        "channel": thread.get("channel"),
                        "thread_ts": thread.get("thread_ts"),
                        "message_count": len(messages),
                    },
                )
            )
        return docs
