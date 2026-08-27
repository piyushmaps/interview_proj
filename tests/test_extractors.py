"""Extraction-quality smoke tests for the multi-format City Map source."""

from pathlib import Path

from src.connectors import CityMapConnector
from src.ingestion.extractors import extract_eml, extract_html, extract_pdf, extract_sheet

# A fixed, small corpus: tests must not depend on whatever is sitting in
# data/sample, which is real user data and can be hundreds of megabytes.
CITY_MAP_DIR = Path(__file__).parent / "fixtures" / "city_map"


def test_html_strips_boilerplate_keeps_content():
    result = extract_html(CITY_MAP_DIR / "platform-overview.html")
    # Content survives, with heading markers for the chunker
    assert "# City Map Platform Overview" in result.text
    assert "owner_team" in result.text
    # Nav, footer, cookie banner, script are gone
    assert "Sign in" not in result.text
    assert "cookies" not in result.text.lower()
    assert "Privacy" not in result.text
    assert "analytics beacon" not in result.text


def test_sheet_rows_carry_headers():
    result = extract_sheet(CITY_MAP_DIR / "capability-owners.csv")
    lines = result.text.splitlines()
    assert result.metadata["row_count"] == 5
    # Every row is self-describing: header attached to each value
    assert all("owner_team:" in line for line in lines)
    assert any("capability: Payments" in line and "owner_lead: alex.chen" in line for line in lines)


def test_eml_extracts_headers_and_body():
    result = extract_eml(CITY_MAP_DIR / "ledger-core-deprecation.eml")
    assert "Ledger Core v1 API deprecation" in result.metadata["subject"]
    assert "idempotency key" in result.text


def test_pdf_extracts_pages():
    pdf_path = CITY_MAP_DIR / "architecture-overview.pdf"
    assert pdf_path.exists(), "Run: python scripts/make_sample_pdf.py"
    result = extract_pdf(pdf_path)
    assert result.metadata["page_count"] == 2
    assert "[page 1]" in result.text and "[page 2]" in result.text
    assert "DynamoDB" in result.text


def test_city_map_connector_loads_all_formats():
    docs = CityMapConnector(path=CITY_MAP_DIR).load()
    formats = {d.metadata.get("format") for d in docs}
    assert {"graph", "sheet", "html", "pdf", "eml"} <= formats
    # Sheet becomes one doc per row
    row_docs = [d for d in docs if d.metadata.get("format") == "sheet"]
    assert len(row_docs) == 5
    assert all(d.body for d in docs)
