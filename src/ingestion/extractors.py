"""Per-format text extractors: raw files → clean text for normalization.

Dispatch table at the bottom maps file suffixes to extractors. Each extractor
returns plain text; format-specific metadata (page numbers, row counts) is
returned alongside so connectors can preserve it for citations.
"""

from __future__ import annotations

import csv
import logging
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pypdf import PdfReader

logger = logging.getLogger(__name__)

# HTML elements that are boilerplate, never content
_HTML_NOISE_TAGS = ("nav", "footer", "script", "style", "header", "aside", "button")

# A single cell can hold a whole article; the stdlib default (128 KB) rejects those.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# A cell this long means the column carries prose, not a category or a number.
TEXT_COLUMN_MIN_CHARS = 200
# Rows scanned to infer schema and statistics without reading the whole file.
PROFILE_SAMPLE_ROWS = 500


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


@dataclass
class SheetProfile:
    """What a table *is*, learned from a streamed sample rather than the whole file."""

    columns: list[str]
    row_count: int
    text_column: str | None
    samples: list[dict[str, str]]
    stats: dict[str, str]

    @property
    def is_text_bearing(self) -> bool:
        """True when rows carry prose worth embedding individually."""
        return self.text_column is not None


def _summarize(column: str, values: list[str]) -> str:
    """One line per column: numeric range, or the categories that dominate it."""
    present = [v for v in values if v not in ("", None)]
    if not present:
        return "all empty"
    try:
        nums = [float(v) for v in present]
    except ValueError:
        counts = Counter(present)
        top = ", ".join(f"{v} ({n})" for v, n in counts.most_common(4))
        return f"{len(counts)} distinct; most common: {top}"
    return (
        f"numeric, min={min(nums):g} max={max(nums):g} "
        f"mean={statistics.fmean(nums):.2f}"
    )


def profile_sheet(path: Path, sample_rows: int = PROFILE_SAMPLE_ROWS) -> SheetProfile:
    """Stream a table once to learn its shape. Never holds the file in memory —
    a 200 MB export profiles in the same footprint as a 2 KB one."""
    sample: list[dict[str, str]] = []
    columns: list[str] = []
    row_count = 0

    with path.open(encoding="utf-8", newline="", errors="replace") as f:
        reader = csv.DictReader(f)
        columns = [c for c in (reader.fieldnames or []) if c]
        for row in reader:
            row_count += 1
            if len(sample) < sample_rows:
                sample.append(row)

    by_column = {c: [str(r.get(c) or "") for r in sample] for c in columns}
    text_column = max(
        (c for c in columns
         if by_column[c] and statistics.fmean(len(v) for v in by_column[c]) >= TEXT_COLUMN_MIN_CHARS),
        key=lambda c: statistics.fmean(len(v) for v in by_column[c]),
        default=None,
    )
    return SheetProfile(
        columns=columns,
        row_count=row_count,
        text_column=text_column,
        samples=sample[:3],
        stats={c: _summarize(c, by_column[c]) for c in columns},
    )


def render_profile(path: Path, profile: SheetProfile) -> Extracted:
    """A searchable description of a dataset, standing in for its rows.

    A 900k-row table of codes and coordinates is not something semantic search
    can answer over — every row embeds to nearly the same point. What people
    actually ask is "what data do we have about crime?", which a schema, column
    statistics, and a few sample rows answer in one chunk instead of 900k.
    """
    lines = [
        f"# Dataset: {path.name}",
        f"Rows: {profile.row_count:,}",
        f"Columns ({len(profile.columns)}): {', '.join(profile.columns)}",
        "",
        "## Column summary",
    ]
    lines += [f"- {c}: {profile.stats[c]}" for c in profile.columns]
    if profile.samples:
        lines += ["", "## Sample rows"]
        for row in profile.samples:
            cells = " | ".join(f"{k}: {str(v)[:60]}" for k, v in row.items() if v)
            lines.append(f"- {cells}")
    return Extracted(
        text="\n".join(lines),
        metadata={
            "format": "sheet_profile",
            "row_count": profile.row_count,
            "column_count": len(profile.columns),
        },
    )


def iter_sheet_rows(path: Path, limit: int | None = None) -> Iterator[tuple[int, str]]:
    """Yield (row_number, rendered_row) lazily, so row count never drives memory."""
    with path.open(encoding="utf-8", newline="", errors="replace") as f:
        for i, row in enumerate(csv.DictReader(f), start=1):
            if limit is not None and i > limit:
                return
            yield i, " | ".join(f"{k}: {v}" for k, v in row.items() if k and v)


EXTRACTORS: dict[str, Callable[[Path], Extracted]] = {
    ".pdf": extract_pdf,
    ".html": extract_html,
    ".htm": extract_html,
    ".csv": extract_sheet,
    ".eml": extract_eml,
}


def iter_pdf_parts(path: Path, pages_per_part: int) -> Iterator[Extracted]:
    """Stream a PDF as page-range parts instead of one document.

    A 1,000-page book held as a single Document means a multi-megabyte string in
    memory and thousands of chunks that all cite the same title. Emitting parts
    bounds memory regardless of page count and makes citations specific — the
    reader is pointed at pages 101-150, not at "the book".

    pypdf reads pages lazily, so only the current part is ever resident.
    """
    reader = PdfReader(str(path))
    total = len(reader.pages)
    for start in range(0, total, pages_per_part):
        end = min(start + pages_per_part, total)
        pages: list[str] = []
        for number in range(start, end):
            try:
                text = (reader.pages[number].extract_text() or "").strip()
            except Exception as exc:  # noqa: BLE001 - a broken page must not lose the part
                logger.debug("page %d of %s unreadable: %s", number + 1, path.name, exc)
                continue
            if text:
                pages.append(f"[page {number + 1}]\n{text}")
        if not pages:
            continue
        yield Extracted(
            text="\n\n".join(pages),
            metadata={
                "format": "pdf",
                "page_count": total,
                "page_start": start + 1,
                "page_end": end,
            },
        )


def register_extractor(suffix: str, extractor: Callable[[Path], Extracted]) -> None:
    """Teach the pipeline a new format without touching any connector.

    Adding .docx is one call here plus the parsing function — the connector,
    chunker, and index stay untouched, because they only ever see Documents.
    """
    EXTRACTORS[suffix.lower()] = extractor


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in EXTRACTORS


def extract_file(path: Path) -> Extracted | None:
    """Dispatch on suffix; None means unsupported format (caller skips)."""
    extractor = EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        return None
    return extractor(path)
