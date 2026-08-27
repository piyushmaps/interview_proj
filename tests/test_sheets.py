"""Large tables get described; small reference sheets get enumerated.

Enumerating every row of a 900k-row export is the failure this guards against:
those rows embed to nearly the same point, so retrieval over them is noise and
the embedding bill is real.
"""

import csv

import pytest

from src.connectors.city_map import CityMapConnector
from src.ingestion.extractors import profile_sheet, render_profile


def _write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture
def big_data_table(tmp_path):
    """1,200 rows of numbers and codes — no prose anywhere."""
    rows = [[i, i * 1.5, "THEFT" if i % 2 else "ASSAULT"] for i in range(1200)]
    return _write_csv(tmp_path / "crime.csv", ["id", "value", "type"], rows)


@pytest.fixture
def big_text_table(tmp_path):
    """1,200 rows whose `text` column holds an article."""
    rows = [[i, f"Headline {i}", "word " * 120] for i in range(1200)]
    return _write_csv(tmp_path / "articles.csv", ["id", "title", "text"], rows)


def test_data_table_is_profiled_not_enumerated(big_data_table):
    docs = CityMapConnector(path=big_data_table.parent).load()
    assert len(docs) == 1, "a numeric table should collapse to a single profile"
    doc = docs[0]
    assert doc.metadata["format"] == "sheet_profile"
    assert doc.metadata["row_count"] == 1200
    # The profile is what makes the dataset findable at all.
    assert "Rows: 1,200" in doc.body
    assert "id" in doc.body and "type" in doc.body


def test_profile_summarises_columns(big_data_table):
    profile = profile_sheet(big_data_table)
    assert profile.text_column is None
    body = render_profile(big_data_table, profile).text
    assert "numeric" in body            # id / value summarised as ranges
    assert "most common" in body        # type summarised as categories


def test_text_bearing_table_also_indexes_rows(big_text_table):
    docs = CityMapConnector(path=big_text_table.parent).load()
    formats = [d.metadata["format"] for d in docs]
    assert formats.count("sheet_profile") == 1
    # Rows carry prose, so they are worth retrieving individually — up to the cap.
    assert formats.count("sheet") > 0
    assert profile_sheet(big_text_table).text_column == "text"


def test_small_sheet_still_becomes_one_document_per_row(tmp_path):
    path = _write_csv(tmp_path / "owners.csv", ["capability", "owner_team"],
                      [["Payments", "money-movement"], ["Identity", "auth"]])
    docs = CityMapConnector(path=path.parent).load()
    assert len(docs) == 2
    assert all(d.metadata["format"] == "sheet" for d in docs)
    assert any("Payments" in d.title for d in docs)


def test_applestyle_sidecar_files_are_skipped(tmp_path):
    _write_csv(tmp_path / "owners.csv", ["capability"], [["Payments"]])
    (tmp_path / "._owners.csv").write_bytes(b"\x00\x05\x16\x07binary junk")
    docs = CityMapConnector(path=tmp_path).load()
    assert len(docs) == 1
