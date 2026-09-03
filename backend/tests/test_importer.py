"""Tests for the CSV schedule importer (app/importer.py).

All tests are pure unit tests: no database connection, no network.
"""

import datetime
import unittest
from unittest.mock import MagicMock, call, patch

from sqlalchemy.exc import OperationalError

from app.importer import (
    NO_DEPENDENCY,
    ParsedRow,
    ValidationError,
    import_to_db,
    parse_and_validate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csv(*lines: str) -> bytes:
    """Build CSV bytes from a header + data lines."""
    header = "Task ID,Activity,Location,Planned Start,Planned End,Dependency"
    return "\n".join([header] + list(lines)).encode()


GOOD_ROW = "T101,Site Preparation,Well Pad A,01-Sep-2026,05-Sep-2026,-"
ROW_T102 = "T102,Foundation,Well Pad A,06-Sep-2026,15-Sep-2026,T101"
ROW_T103 = "T103,Equipment Install,Well Pad A,16-Sep-2026,20-Sep-2026,T102"


# ---------------------------------------------------------------------------
# parse_and_validate – happy-path tests
# ---------------------------------------------------------------------------

class ParseValidateSuccessTests(unittest.TestCase):

    def test_single_valid_row(self):
        rows, errors = parse_and_validate(_csv(GOOD_ROW), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.source_task_id, "T101")
        self.assertEqual(r.activity, "Site Preparation")
        self.assertEqual(r.location, "Well Pad A")
        self.assertEqual(r.planned_start, datetime.date(2026, 9, 1))
        self.assertEqual(r.planned_end, datetime.date(2026, 9, 5))
        self.assertEqual(r.dependency, NO_DEPENDENCY)

    def test_multiple_valid_rows_all_returned(self):
        rows, errors = parse_and_validate(_csv(GOOD_ROW, ROW_T102, ROW_T103), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.source_task_id for r in rows], ["T101", "T102", "T103"])

    def test_dependency_string_preserved(self):
        rows, errors = parse_and_validate(_csv(GOOD_ROW, ROW_T102), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(rows[1].dependency, "T101")

    def test_whitespace_around_values_is_stripped(self):
        padded = "  T101 , Site Preparation , Well Pad A , 01-Sep-2026 , 05-Sep-2026 , - "
        rows, errors = parse_and_validate(_csv(padded), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0].source_task_id, "T101")

    def test_same_start_and_end_date_is_valid(self):
        row = "T101,One Day Task,Zone A,01-Sep-2026,01-Sep-2026,-"
        rows, errors = parse_and_validate(_csv(row), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0].planned_start, rows[0].planned_end)


# ---------------------------------------------------------------------------
# parse_and_validate – validation-error tests
# ---------------------------------------------------------------------------

class ParseValidateErrorTests(unittest.TestCase):

    def _assert_single_error_contains(self, errors, text_fragment):
        self.assertEqual(len(errors), 1, f"Expected 1 error, got: {errors}")
        self.assertIn(text_fragment, errors[0].message)

    def test_missing_required_column_returns_error(self):
        bad_csv = b"Task ID,Activity,Location,Planned Start,Planned End\nT101,X,Y,01-Sep-2026,05-Sep-2026"
        _, errors = parse_and_validate(bad_csv, "test.csv")
        self._assert_single_error_contains(errors, "Dependency")

    def test_multiple_missing_columns(self):
        bad_csv = b"Task ID,Activity\nT101,X"
        _, errors = parse_and_validate(bad_csv, "test.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("Location", errors[0].message)

    def test_empty_csv_no_header(self):
        _, errors = parse_and_validate(b"", "test.csv")
        self.assertEqual(len(errors), 1)

    def test_empty_task_id_is_rejected(self):
        row = ",Site Preparation,Well Pad A,01-Sep-2026,05-Sep-2026,-"
        _, errors = parse_and_validate(_csv(row), "test.csv")
        self._assert_single_error_contains(errors, "Task ID")

    def test_invalid_start_date_format(self):
        row = "T101,Activity,Zone A,2026-09-01,05-Sep-2026,-"
        _, errors = parse_and_validate(_csv(row), "test.csv")
        self._assert_single_error_contains(errors, "Planned Start")

    def test_invalid_end_date_format(self):
        row = "T101,Activity,Zone A,01-Sep-2026,NOTADATE,-"
        _, errors = parse_and_validate(_csv(row), "test.csv")
        self._assert_single_error_contains(errors, "Planned End")

    def test_end_before_start_is_rejected(self):
        row = "T101,Activity,Zone A,10-Sep-2026,05-Sep-2026,-"
        _, errors = parse_and_validate(_csv(row), "test.csv")
        self._assert_single_error_contains(errors, "Planned End")

    def test_duplicate_task_id_is_rejected(self):
        dup = "T101,Activity,Zone A,01-Sep-2026,05-Sep-2026,-"
        _, errors = parse_and_validate(_csv(GOOD_ROW, dup), "test.csv")
        self._assert_single_error_contains(errors, "Duplicate Task ID")

    def test_unknown_dependency_is_rejected(self):
        row = "T101,Activity,Zone A,01-Sep-2026,05-Sep-2026,TXXX"
        _, errors = parse_and_validate(_csv(row), "test.csv")
        self._assert_single_error_contains(errors, "TXXX")

    def test_multiple_errors_all_collected(self):
        bad1 = ",Activity,Zone,01-Sep-2026,05-Sep-2026,-"       # empty task id
        bad2 = "T102,Activity,Zone,BADDATE,05-Sep-2026,-"        # bad start
        _, errors = parse_and_validate(_csv(bad1, bad2), "test.csv")
        self.assertEqual(len(errors), 2)

    def test_non_utf8_file_returns_error(self):
        bad_bytes = b"\xff\xfe garbage"
        _, errors = parse_and_validate(bad_bytes, "test.csv")
        self.assertEqual(len(errors), 1)


# ---------------------------------------------------------------------------
# import_to_db – unit tests (mock session)
# ---------------------------------------------------------------------------

class ImportToDbTests(unittest.TestCase):

    def _make_rows(self, include_dep=False):
        rows = [
            ParsedRow("T101", "Site Prep", "Zone A",
                      datetime.date(2026, 9, 1), datetime.date(2026, 9, 5),
                      dependency=NO_DEPENDENCY),
        ]
        if include_dep:
            rows.append(
                ParsedRow("T102", "Foundation", "Zone A",
                          datetime.date(2026, 9, 6), datetime.date(2026, 9, 15),
                          dependency="T101")
            )
        return rows

    def _mock_db(self):
        db = MagicMock()
        db.execute.return_value = MagicMock()
        return db

    def test_successful_import_single_task(self):
        db = self._mock_db()
        result = import_to_db(db, self._make_rows(), "test.csv")
        self.assertEqual(result.tasks_imported, 1)
        self.assertEqual(result.dependencies_imported, 0)
        self.assertEqual(result.filename, "test.csv")
        self.assertIsNotNone(result.import_id)
        db.commit.assert_called_once()

    def test_successful_import_with_dependency(self):
        db = self._mock_db()
        result = import_to_db(db, self._make_rows(include_dep=True), "test.csv")
        self.assertEqual(result.tasks_imported, 2)
        self.assertEqual(result.dependencies_imported, 1)
        db.commit.assert_called_once()

    def test_correct_number_of_db_execute_calls(self):
        """1 schedule_imports + N schedule_tasks + D dependencies = total execute calls."""
        db = self._mock_db()
        import_to_db(db, self._make_rows(include_dep=True), "test.csv")
        # 1 (import record) + 2 (tasks) + 1 (dependency) = 4
        self.assertEqual(db.execute.call_count, 4)

    def test_database_error_propagates(self):
        db = self._mock_db()
        db.execute.side_effect = OperationalError("fail", None, None)
        with self.assertRaises(OperationalError):
            import_to_db(db, self._make_rows(), "test.csv")

    def test_import_id_is_valid_uuid(self):
        import uuid
        db = self._mock_db()
        result = import_to_db(db, self._make_rows(), "test.csv")
        # Should not raise
        uuid.UUID(result.import_id)

    def test_empty_rows_list_still_commits(self):
        db = self._mock_db()
        result = import_to_db(db, [], "empty.csv")
        self.assertEqual(result.tasks_imported, 0)
        self.assertEqual(result.dependencies_imported, 0)
        db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
