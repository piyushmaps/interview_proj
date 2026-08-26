"""Per-format text extractors: raw files → clean text for normalization.

Dispatch table at the bottom maps file suffixes to extractors. Each extractor
returns plain text; format-specific metadata (page numbers, row counts) is
returned alongside so connectors can preserve it for citations.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pypdf import PdfReader

# HTML elements that are boilerplate, never content
_HTML_NOISE_TAGS = ("nav", "footer", "script", "style", "header", "aside", "button")


@dataclass
class Extracted:
    """Clean text plus format-specific metadata."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def extract_pdf(path: Path) -> Extracted:
    """Digital-PDF text extraction, page markers kept for citation metadata."""
    reader = PdfReader(str(path))
    pages: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[page {i}]\n{text}")
    return Extracted(
        text="\n\n".join(pages),
        metadata={"format": "pdf", "page_count": len(reader.pages)},
    )
    

def extract_html(path: Path) -> Extracted:
    """Main-content extraction: strips nav/footer/script boilerplate,
    preserves heading structure as Markdown-style markers so the chunker
    can split on sections."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup(_HTML_NOISE_TAGS):
        tag.decompose()
    for banner in soup.select(".cookie-banner, .ad, .promo"):
        banner.decompose()

    lines: list[str] = []
    root = soup.body or soup
    for el in root.find_all(["h1", "h2", "h3", "p", "li"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name == "h1":
            lines.append(f"# {text}")
        elif el.name == "h2":
            lines.append(f"## {text}")
        elif el.name == "h3":
            lines.append(f"### {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)

    title = soup.title.get_text(strip=True) if soup.title else ""
    return Extracted(
        text="\n\n".join(lines),
        metadata={"format": "html", "html_title": title},
    )


def extract_sheet(path: Path) -> Extracted:
    """CSV / exported Google Sheet: render each row with headers attached
    ("col: value | col: value") so a row stays meaningful as a standalone
    chunk instead of a bare comma-separated line."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    lines = [
        " | ".join(f"{k}: {v}" for k, v in row.items() if v)
        for row in rows
    ]
    return Extracted(
        text="\n".join(lines),
        metadata={"format": "sheet", "row_count": len(rows)},
    )


def extract_eml(path: Path) -> Extracted:
    """Long-tail example: email files via the stdlib parser."""
    with path.open("rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    header = (
        f"From: {msg['From']}\n"
        f"To: {msg['To']}\n"
        f"Subject: {msg['Subject']}\n"
        f"Date: {msg['Date']}"
    )
    return Extracted(
        text=f"{header}\n\n{body}".strip(),
        metadata={"format": "eml", "subject": str(msg["Subject"] or "")},
    )


EXTRACTORS: dict[str, Callable[[Path], Extracted]] = {
    ".pdf": extract_pdf,
    ".html": extract_html,
    ".htm": extract_html,
    ".csv": extract_sheet,
    ".eml": extract_eml,
}


def extract_file(path: Path) -> Extracted | None:
    """Dispatch on suffix; None means unsupported format (caller skips)."""
    extractor = EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        return None
    return extractor(path)
