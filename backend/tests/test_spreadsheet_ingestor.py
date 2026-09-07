"""Tests for app/spreadsheet_ingestor.py.

All tests are pure unit tests — no database, no network.
CSV bytes are built in-memory; XLSX bytes are built with openpyxl.
"""

from __future__ import annotations

import csv
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import openpyxl

from app.spreadsheet_ingestor import (
    RowError,
    SpreadsheetImportResult,
    _build_column_map,
    _check_required_columns,
    _normalise,
    _resolve_header,
    parse_spreadsheet,
)
from app.site_updates import SiteUpdateCreate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_csv(rows: list[list[str]], header: list[str] | None = None) -> bytes:
    """Build CSV bytes from a header + rows."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    if header is not None:
        writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode()


_STANDARD_HEADER = ["Update ID", "Date", "Location", "Description"]


def _valid_row(uid="U001", date="05-Sep-2026", loc="Well Pad A", desc="Work done."):
    return [uid, date, loc, desc]


def _make_xlsx(
    rows: list[list[str]],
    header: list[str] | None = None,
    sheet_name: str = "Sheet1",
) -> bytes:
    """Build XLSX bytes from a header + rows using openpyxl."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    if header:
        ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _normalise and _resolve_header
# ---------------------------------------------------------------------------

class NormaliseTests(unittest.TestCase):
    def test_strips_and_lowercases(self):
        self.assertEqual(_normalise("  Update ID  "), "update id")

    def test_collapses_punctuation(self):
        self.assertEqual(_normalise("update_id"), "update id")

    def test_collapses_mixed_punct(self):
        self.assertEqual(_normalise("raw-update/text"), "raw update text")


class ResolveHeaderTests(unittest.TestCase):
    def test_standard_update_id(self):
        self.assertEqual(_resolve_header("Update ID"), "source_update_id")

    def test_uid_alias(self):
        self.assertEqual(_resolve_header("uid"), "source_update_id")

    def test_date_alias(self):
        self.assertEqual(_resolve_header("Date"), "reported_on")

    def test_report_date_alias(self):
        self.assertEqual(_resolve_header("Report Date"), "reported_on")

    def test_location_alias(self):
        self.assertEqual(_resolve_header("loc"), "location")

    def test_site_alias(self):
        self.assertEqual(_resolve_header("site"), "location")

    def test_description_alias(self):
        self.assertEqual(_resolve_header("description"), "raw_update")

    def test_remarks_alias(self):
        self.assertEqual(_resolve_header("Remarks"), "raw_update")

    def test_notes_alias(self):
        self.assertEqual(_resolve_header("Notes"), "raw_update")

    def test_observation_alias(self):
        self.assertEqual(_resolve_header("Observation"), "raw_update")

    def test_work_done_alias(self):
        self.assertEqual(_resolve_header("Work Done"), "raw_update")

    def test_unknown_header_returns_none(self):
        self.assertIsNone(_resolve_header("Foo Bar Baz"))


# ---------------------------------------------------------------------------
# _build_column_map / _check_required_columns
# ---------------------------------------------------------------------------

class BuildColumnMapTests(unittest.TestCase):
    def test_standard_headers_all_resolved(self):
        col_map = _build_column_map(_STANDARD_HEADER)
        self.assertIn("Update ID", col_map)
        self.assertEqual(col_map["Update ID"], "source_update_id")
        self.assertIn("Date", col_map)
        self.assertIn("Location", col_map)
        self.assertIn("Description", col_map)

    def test_no_required_missing(self):
        col_map = _build_column_map(_STANDARD_HEADER)
        missing = _check_required_columns(col_map)
        self.assertEqual(missing, set())

    def test_missing_raw_update_column(self):
        col_map = _build_column_map(["Update ID", "Date", "Location"])
        missing = _check_required_columns(col_map)
        self.assertIn("raw_update", missing)

    def test_duplicate_canonical_first_wins(self):
        # Two columns that both resolve to raw_update
        col_map = _build_column_map(["Update ID", "Date", "Location", "Description", "Remarks"])
        vals = list(col_map.values())
        self.assertEqual(vals.count("raw_update"), 1)


# ---------------------------------------------------------------------------
# CSV parsing — happy path
# ---------------------------------------------------------------------------

class CsvHappyPathTests(unittest.TestCase):
    def test_single_valid_row(self):
        content = _make_csv([_valid_row()], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "updates.csv")
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.error_count, 0)
        row = result.rows_accepted[0]
        self.assertEqual(row.source_update_id, "U001")
        self.assertEqual(row.location, "Well Pad A")
        self.assertEqual(row.source_reference, "updates.csv")

    def test_multiple_valid_rows(self):
        content = _make_csv([
            _valid_row("U001"),
            _valid_row("U002", "06-Sep-2026", "Well Pad B", "Concreting 40% done."),
            _valid_row("U003", "07-Sep-2026", "Well Pad A", "Site inspection complete."),
        ], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "bulk.csv")
        self.assertEqual(result.accepted_count, 3)

    def test_source_reference_set_to_filename(self):
        content = _make_csv([_valid_row()], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "my_report.csv")
        self.assertEqual(result.rows_accepted[0].source_reference, "my_report.csv")

    def test_alternative_column_names(self):
        """Aliases for all four fields must be resolved correctly."""
        header = ["uid", "report date", "site", "remarks"]
        row = ["U010", "08-Sep-2026", "Zone B", "Foundations poured."]
        content = _make_csv([row], header)
        result = parse_spreadsheet(content, "alt_cols.csv")
        self.assertEqual(result.accepted_count, 1, result.row_errors)
        self.assertEqual(result.rows_accepted[0].source_update_id, "U010")

    def test_whitespace_in_cells_is_stripped(self):
        content = _make_csv([["  U001 ", "  05-Sep-2026  ", "  Well Pad A  ", "  Work done.  "]], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "test.csv")
        self.assertEqual(result.accepted_count, 1)
        row = result.rows_accepted[0]
        self.assertEqual(row.source_update_id, "U001")
        self.assertEqual(row.location, "Well Pad A")

    def test_blank_rows_are_skipped_silently(self):
        content = _make_csv([
            _valid_row("U001"),
            ["", "", "", ""],
            _valid_row("U002", "06-Sep-2026"),
        ], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "with_blanks.csv")
        self.assertEqual(result.accepted_count, 2)
        self.assertEqual(result.error_count, 0)

    def test_utf8_bom_is_handled(self):
        """Some Excel CSV exports prepend a BOM."""
        content = b"\xef\xbb\xbf" + _make_csv([_valid_row()], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "bom.csv")
        self.assertEqual(result.accepted_count, 1)

    def test_case_insensitive_header_matching(self):
        """UPPER CASE column names must still be matched."""
        header = ["UPDATE ID", "DATE", "LOCATION", "DESCRIPTION"]
        content = _make_csv([_valid_row()], header)
        result = parse_spreadsheet(content, "upper.csv")
        self.assertEqual(result.accepted_count, 1)


# ---------------------------------------------------------------------------
# CSV parsing — error cases
# ---------------------------------------------------------------------------

class CsvErrorTests(unittest.TestCase):
    def test_missing_required_column_returns_file_error(self):
        """If location is missing the whole file is rejected at header level."""
        content = _make_csv([_valid_row()], ["Update ID", "Date", "Description"])
        result = parse_spreadsheet(content, "bad_cols.csv")
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.error_count, 1)
        self.assertIn("location", result.row_errors[0].message.lower())

    def test_invalid_date_format_produces_row_error(self):
        content = _make_csv([
            _valid_row("U001", "2026-09-05"),  # wrong format
        ], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "bad_date.csv")
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.error_count, 1)
        self.assertEqual(result.row_errors[0].source_update_id, "U001")
        self.assertEqual(result.row_errors[0].row_number, 2)

    def test_empty_update_id_produces_row_error(self):
        content = _make_csv([_valid_row("")], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "empty_id.csv")
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.error_count, 1)

    def test_empty_raw_update_produces_row_error(self):
        content = _make_csv([["U001", "05-Sep-2026", "Well Pad A", ""]], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "empty_desc.csv")
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.error_count, 1)

    def test_one_bad_row_does_not_block_good_rows(self):
        """Partial import: row 2 bad, rows 3 and 4 good."""
        content = _make_csv([
            _valid_row("U001", "BAD-DATE-FORMAT"),
            _valid_row("U002", "06-Sep-2026"),
            _valid_row("U003", "07-Sep-2026"),
        ], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "mixed.csv")
        self.assertEqual(result.accepted_count, 2)
        self.assertEqual(result.error_count, 1)
        self.assertEqual(result.row_errors[0].row_number, 2)
        accepted_ids = [r.source_update_id for r in result.rows_accepted]
        self.assertIn("U002", accepted_ids)
        self.assertIn("U003", accepted_ids)

    def test_empty_file_produces_error(self):
        result = parse_spreadsheet(b"", "empty.csv")
        self.assertEqual(result.accepted_count, 0)
        self.assertGreater(result.error_count, 0)


# ---------------------------------------------------------------------------
# XLSX parsing — happy path
# ---------------------------------------------------------------------------

class XlsxHappyPathTests(unittest.TestCase):
    def test_single_valid_row_xlsx(self):
        content = _make_xlsx([_valid_row()], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "updates.xlsx")
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.error_count, 0)
        row = result.rows_accepted[0]
        self.assertEqual(row.source_update_id, "U001")
        self.assertEqual(row.source_reference, "updates.xlsx")

    def test_multiple_valid_rows_xlsx(self):
        content = _make_xlsx([
            _valid_row("U001"),
            _valid_row("U002", "06-Sep-2026", "Zone B", "Concrete poured."),
        ], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "multi.xlsx")
        self.assertEqual(result.accepted_count, 2)

    def test_alias_columns_xlsx(self):
        header = ["uid", "report date", "area", "notes"]
        row = ["SH01", "09-Sep-2026", "Substation A", "Cable laying done."]
        content = _make_xlsx([row], header)
        result = parse_spreadsheet(content, "aliases.xlsx")
        self.assertEqual(result.accepted_count, 1, result.row_errors)
        self.assertEqual(result.rows_accepted[0].source_update_id, "SH01")

    def test_xlsx_source_reference_is_filename(self):
        content = _make_xlsx([_valid_row()], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "discipline_a.xlsx")
        self.assertEqual(result.rows_accepted[0].source_reference, "discipline_a.xlsx")

    def test_blank_rows_skipped_xlsx(self):
        content = _make_xlsx([
            _valid_row("U001"),
            ["", "", "", ""],
            _valid_row("U002", "06-Sep-2026"),
        ], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "blanks.xlsx")
        self.assertEqual(result.accepted_count, 2)
        self.assertEqual(result.error_count, 0)


# ---------------------------------------------------------------------------
# XLSX parsing — error cases
# ---------------------------------------------------------------------------

class XlsxErrorTests(unittest.TestCase):
    def test_missing_required_column_xlsx(self):
        content = _make_xlsx([_valid_row()], ["Update ID", "Date", "Description"])
        result = parse_spreadsheet(content, "bad_cols.xlsx")
        self.assertEqual(result.accepted_count, 0)
        self.assertIn("location", result.row_errors[0].message.lower())

    def test_invalid_date_format_xlsx(self):
        content = _make_xlsx([["U001", "2026/09/05", "Zone A", "Work done."]], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "bad_date.xlsx")
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.error_count, 1)

    def test_one_bad_row_does_not_block_xlsx(self):
        content = _make_xlsx([
            _valid_row("U001", "BAD"),
            _valid_row("U002", "06-Sep-2026"),
        ], _STANDARD_HEADER)
        result = parse_spreadsheet(content, "partial.xlsx")
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.error_count, 1)

    def test_corrupt_xlsx_bytes_returns_error(self):
        result = parse_spreadsheet(b"this is not xlsx", "broken.xlsx")
        self.assertEqual(result.accepted_count, 0)
        self.assertGreater(result.error_count, 0)


# ---------------------------------------------------------------------------
# SpreadsheetImportResult properties
# ---------------------------------------------------------------------------

class ImportResultTests(unittest.TestCase):
    def test_accepted_count_property(self):
        r = SpreadsheetImportResult(filename="x.csv")
        r.rows_accepted.append(MagicMock())
        r.rows_accepted.append(MagicMock())
        self.assertEqual(r.accepted_count, 2)

    def test_error_count_property(self):
        r = SpreadsheetImportResult(filename="x.csv")
        r.row_errors.append(RowError(2, "U001", "bad date"))
        self.assertEqual(r.error_count, 1)


# ---------------------------------------------------------------------------
# Endpoint integration tests (mocked DB + ingestor)
# ---------------------------------------------------------------------------

class SpreadsheetEndpointTests(unittest.TestCase):
    """Test the FastAPI endpoint layer only — ingestor and DB are mocked."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def _post(self, content: bytes, filename: str) -> object:
        return self.client.post(
            "/site-updates/import-spreadsheet",
            files={"file": (filename, content, "text/csv")},
        )

    @patch("app.main.site_updates.create_site_update")
    @patch("app.main.spreadsheet_ingestor.parse_spreadsheet")
    def test_all_rows_accepted_returns_201(self, mock_parse, mock_create):
        payload = SiteUpdateCreate(
            source_update_id="U001",
            reported_on="05-Sep-2026",
            location="Zone A",
            raw_update="Work done.",
            source_reference="f.csv",
        )
        mock_parse.return_value = SpreadsheetImportResult(
            filename="f.csv",
            rows_accepted=[payload],
            row_errors=[],
        )
        mock_create.return_value = {
            "id": "uuid-1", "source_update_id": "U001",
            "reported_on": "2026-09-05", "location": "Zone A",
            "raw_update": "Work done.", "ingested_at": None,
            "source_reference": "f.csv",
        }
        resp = self._post(b"dummy", "f.csv")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["stored"], 1)
        self.assertEqual(len(body["row_errors"]), 0)

    @patch("app.main.spreadsheet_ingestor.parse_spreadsheet")
    def test_all_rows_failed_returns_422(self, mock_parse):
        mock_parse.return_value = SpreadsheetImportResult(
            filename="bad.csv",
            rows_accepted=[],
            row_errors=[RowError(2, "U001", "bad date")],
        )
        resp = self._post(b"dummy", "bad.csv")
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertIn("row_errors", body["detail"])

    @patch("app.main.site_updates.create_site_update")
    @patch("app.main.spreadsheet_ingestor.parse_spreadsheet")
    def test_partial_success_returns_201_with_errors(self, mock_parse, mock_create):
        payload_good = SiteUpdateCreate(
            source_update_id="U002",
            reported_on="06-Sep-2026",
            location="Zone B",
            raw_update="OK.",
            source_reference="f.csv",
        )
        mock_parse.return_value = SpreadsheetImportResult(
            filename="f.csv",
            rows_accepted=[payload_good],
            row_errors=[RowError(2, "U001", "bad date")],
        )
        mock_create.return_value = {
            "id": "uuid-2", "source_update_id": "U002",
            "reported_on": "2026-09-06", "location": "Zone B",
            "raw_update": "OK.", "ingested_at": None, "source_reference": "f.csv",
        }
        resp = self._post(b"dummy", "f.csv")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["stored"], 1)
        self.assertEqual(len(body["row_errors"]), 1)


if __name__ == "__main__":
    unittest.main()
