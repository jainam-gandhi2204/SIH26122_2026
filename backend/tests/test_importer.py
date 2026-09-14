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
    parse_dependencies,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import csv as _csv_mod
import io as _io_mod


def _csv(*data_rows: str | list, header: str = "Task ID,Activity,Location,Planned Start,Planned End,Dependency") -> bytes:
    """Build CSV bytes.

    Each argument may be a raw comma-separated string (simple rows) OR a list
    of field values (which will be properly quoted by csv.writer — needed for
    dependency cells containing commas, e.g. ["T107","Commissioning","Zone A",
    "26-Sep-2026","30-Sep-2026","T104,T106"]).
    """
    buf = _io_mod.StringIO()
    writer = _csv_mod.writer(buf)
    # Write header
    writer.writerow(header.split(","))
    for row in data_rows:
        if isinstance(row, list):
            writer.writerow(row)
        else:
            # Simple string rows: parse as CSV to get the fields, then re-write
            # so they are properly escaped/quoted.
            fields = next(_csv_mod.reader([row]))
            writer.writerow(fields)
    return buf.getvalue().encode()


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
        self.assertEqual(r.dependencies, [])

    def test_multiple_valid_rows_all_returned(self):
        rows, errors = parse_and_validate(_csv(GOOD_ROW, ROW_T102, ROW_T103), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.source_task_id for r in rows], ["T101", "T102", "T103"])

    def test_dependency_string_preserved(self):
        rows, errors = parse_and_validate(_csv(GOOD_ROW, ROW_T102), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(rows[1].dependencies, ["T101"])

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

    def test_no_dependency_yields_empty_list(self):
        rows, errors = parse_and_validate(_csv(GOOD_ROW), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0].dependencies, [])

    def test_multi_dependency_t104_t106(self):
        """T107 depends on both T104 and T106 — the spec's exact scenario."""
        row_t104 = "T104,Equip Install,Zone A,16-Sep-2026,20-Sep-2026,-"
        row_t106 = "T106,Cable Laying,Zone A,21-Sep-2026,25-Sep-2026,-"
        # List form: the Dependency cell is "T104,T106" (a single quoted field)
        row_t107 = ["T107", "Commissioning", "Zone A", "26-Sep-2026", "30-Sep-2026", "T104,T106"]
        rows, errors = parse_and_validate(_csv(row_t104, row_t106, row_t107), "test.csv")
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(len(rows), 3)
        t107 = next(r for r in rows if r.source_task_id == "T107")
        self.assertIn("T104", t107.dependencies)
        self.assertIn("T106", t107.dependencies)
        self.assertEqual(len(t107.dependencies), 2)

    def test_multi_dependency_with_whitespace(self):
        """Spaces around IDs in 'T104 , T106' must be trimmed."""
        row_t104 = "T104,Equip Install,Zone A,16-Sep-2026,20-Sep-2026,-"
        row_t106 = "T106,Cable Laying,Zone A,21-Sep-2026,25-Sep-2026,-"
        row_t107 = ["T107", "Commissioning", "Zone A", "26-Sep-2026", "30-Sep-2026", " T104 , T106 "]
        rows, errors = parse_and_validate(_csv(row_t104, row_t106, row_t107), "test.csv")
        self.assertEqual(errors, [])
        t107 = next(r for r in rows if r.source_task_id == "T107")
        self.assertEqual(sorted(t107.dependencies), ["T104", "T106"])

    def test_single_dependency_still_works(self):
        rows, errors = parse_and_validate(_csv(GOOD_ROW, ROW_T102), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(rows[1].dependencies, ["T101"])

    def test_parse_dependencies_exhaustive(self):
        """Regression tests for parse_dependencies helper handling all required formats."""
        # single dependency works
        self.assertEqual(parse_dependencies("CSB105"), ["CSB105"])
        # two dependencies separated by comma work
        self.assertEqual(parse_dependencies("CSB114,CSB115"), ["CSB114", "CSB115"])
        self.assertEqual(parse_dependencies("CSB114, CSB115"), ["CSB114", "CSB115"])
        # two dependencies separated by semicolon work
        self.assertEqual(parse_dependencies("CSB114;CSB115"), ["CSB114", "CSB115"])
        # whitespace is handled
        self.assertEqual(parse_dependencies("  CSB114  ;  CSB115  "), ["CSB114", "CSB115"])
        self.assertEqual(parse_dependencies("  CSB114  ,  CSB115  "), ["CSB114", "CSB115"])
        # "-" produces zero dependencies
        self.assertEqual(parse_dependencies("-"), [])
        self.assertEqual(parse_dependencies(" - "), [])
        # empty dependency produces zero dependencies
        self.assertEqual(parse_dependencies(""), [])
        self.assertEqual(parse_dependencies("   "), [])
        self.assertEqual(parse_dependencies(None), [])
        # mixed delimiters
        self.assertEqual(parse_dependencies("CSB110, CSB111; CSB112"), ["CSB110", "CSB111", "CSB112"])
        # duplicate handling
        self.assertEqual(parse_dependencies("CSB114;CSB114"), ["CSB114"])

    def test_semicolon_delimited_dependencies_in_csv(self):
        """CSV with semicolon-separated dependencies like CSB114;CSB115 produces multiple dependencies."""
        row_1 = "CSB114,Power Cable,Station B,28-Oct-2026,31-Oct-2026,-"
        row_2 = "CSB115,Instrumentation,Station B,28-Oct-2026,02-Nov-2026,-"
        row_3 = "CSB116,Control Panel,Station B,29-Oct-2026,31-Oct-2026,CSB114;CSB115"
        rows, errors = parse_and_validate(_csv(row_1, row_2, row_3), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 3)
        csb116 = next(r for r in rows if r.source_task_id == "CSB116")
        self.assertEqual(csb116.dependencies, ["CSB114", "CSB115"])

    def test_mixed_delimiter_dependencies_in_csv(self):
        """CSV with mixed comma and semicolon dependencies like 'CSB110, CSB111; CSB112'."""
        row_0 = "CSB110,Lube Oil,Station B,13-Oct-2026,17-Oct-2026,-"
        row_1 = "CSB111,Cooling Water,Station B,18-Oct-2026,22-Oct-2026,-"
        row_2 = "CSB112,Process Piping,Station B,23-Oct-2026,27-Oct-2026,-"
        row_3 = ["CSB117", "Inspection", "Station B", "06-Nov-2026", "08-Nov-2026", "CSB110, CSB111; CSB112"]
        rows, errors = parse_and_validate(_csv(row_0, row_1, row_2, row_3), "test.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 4)
        csb117 = next(r for r in rows if r.source_task_id == "CSB117")
        self.assertEqual(csb117.dependencies, ["CSB110", "CSB111", "CSB112"])


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

    def test_one_unknown_id_in_multi_dep_rejected(self):
        """If one ID in 'T104,TXXX' is unknown, the row is rejected."""
        row_t104 = "T104,Equip Install,Zone A,16-Sep-2026,20-Sep-2026,-"
        row_t107 = ["T107", "Commissioning", "Zone A", "26-Sep-2026", "30-Sep-2026", "T104,TXXX"]
        _, errors = parse_and_validate(_csv(row_t104, row_t107), "test.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("TXXX", errors[0].message)
        self.assertEqual(errors[0].task_id, "T107")

    def test_invalid_dependency_in_semicolon_list_rejected(self):
        """If one ID in 'CSB114;NONEXISTENT' is unknown, a ValidationError is produced."""
        row_1 = "CSB114,Power Cable,Station B,28-Oct-2026,31-Oct-2026,-"
        row_2 = "CSB116,Control Panel,Station B,29-Oct-2026,31-Oct-2026,CSB114;NONEXISTENT"
        rows, errors = parse_and_validate(_csv(row_1, row_2), "test.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("NONEXISTENT", errors[0].message)
        self.assertEqual(errors[0].task_id, "CSB116")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_task_id, "CSB114")


# ---------------------------------------------------------------------------
# import_to_db – unit tests (mock session)
# ---------------------------------------------------------------------------

class ImportToDbTests(unittest.TestCase):

    def _make_rows(self, include_dep=False):
        rows = [
            ParsedRow("T101", "Site Prep", "Zone A",
                      datetime.date(2026, 9, 1), datetime.date(2026, 9, 5),
                      dependencies=[]),
        ]
        if include_dep:
            rows.append(
                ParsedRow("T102", "Foundation", "Zone A",
                          datetime.date(2026, 9, 6), datetime.date(2026, 9, 15),
                          dependencies=["T101"])
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
        """1 import + 1 select existing + 2 task inserts + 1 delete deps + 1 dep insert = 6 calls."""
        db = self._mock_db()
        import_to_db(db, self._make_rows(include_dep=True), "test.csv")
        self.assertEqual(db.execute.call_count, 6)

    def test_database_error_propagates(self):
        db = self._mock_db()
        db.execute.side_effect = OperationalError("fail", {}, Exception("db error"))
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

    def test_multi_dependency_t104_t106_inserts_two_dep_rows(self):
        """T107 with dependencies=[T104, T106] must insert 2 task_dependency rows."""
        rows = [
            ParsedRow("T104", "Equip Install", "Zone A",
                      datetime.date(2026, 9, 16), datetime.date(2026, 9, 20),
                      dependencies=[]),
            ParsedRow("T106", "Cable Laying", "Zone A",
                      datetime.date(2026, 9, 21), datetime.date(2026, 9, 25),
                      dependencies=[]),
            ParsedRow("T107", "Commissioning", "Zone A",
                      datetime.date(2026, 9, 26), datetime.date(2026, 9, 30),
                      dependencies=["T104", "T106"]),
        ]
        db = self._mock_db()
        result = import_to_db(db, rows, "test.csv")
        self.assertEqual(result.tasks_imported, 3)
        self.assertEqual(result.dependencies_imported, 2)

    def test_multi_dependency_execute_call_count(self):
        """1 import + 1 select existing + 3 task inserts + 1 delete deps + 2 dep inserts = 8 calls."""
        rows = [
            ParsedRow("T104", "Equip Install", "Zone A",
                      datetime.date(2026, 9, 16), datetime.date(2026, 9, 20),
                      dependencies=[]),
            ParsedRow("T106", "Cable Laying", "Zone A",
                      datetime.date(2026, 9, 21), datetime.date(2026, 9, 25),
                      dependencies=[]),
            ParsedRow("T107", "Commissioning", "Zone A",
                      datetime.date(2026, 9, 26), datetime.date(2026, 9, 30),
                      dependencies=["T104", "T106"]),
        ]
        db = self._mock_db()
        import_to_db(db, rows, "test.csv")
        self.assertEqual(db.execute.call_count, 8)



class IdempotencyTests(unittest.TestCase):
    """Verify that re-importing the same CSV does not create duplicates and preserves task UUIDs."""

    def _mock_db(self):
        db = MagicMock()
        db.execute.return_value = MagicMock()
        return db

    def _full_schedule(self):
        """Return rows for T101 through T107 — the spec's full dependency chain."""
        return [
            ParsedRow("T101", "Site Prep",       "Zone A",
                      datetime.date(2026, 9, 1),  datetime.date(2026, 9, 5),  dependencies=[]),
            ParsedRow("T102", "Foundation",       "Zone A",
                      datetime.date(2026, 9, 6),  datetime.date(2026, 9, 15), dependencies=["T101"]),
            ParsedRow("T103", "Equip Install",    "Zone A",
                      datetime.date(2026, 9, 16), datetime.date(2026, 9, 20), dependencies=["T102"]),
            ParsedRow("T104", "Cable Laying A",   "Zone A",
                      datetime.date(2026, 9, 21), datetime.date(2026, 9, 25), dependencies=["T103"]),
            ParsedRow("T105", "Structural Work",  "Zone A",
                      datetime.date(2026, 9, 6),  datetime.date(2026, 9, 15), dependencies=["T102"]),
            ParsedRow("T106", "Cable Laying B",   "Zone A",
                      datetime.date(2026, 9, 16), datetime.date(2026, 9, 20), dependencies=["T105"]),
            ParsedRow("T107", "Commissioning",    "Zone A",
                      datetime.date(2026, 9, 26), datetime.date(2026, 9, 30), dependencies=["T104", "T106"]),
        ]

    def _sql_texts(self, db):
        """Return the list of SQL string fragments from every db.execute call."""
        texts = []
        for c in db.execute.call_args_list:
            arg = c.args[0] if c.args else None
            texts.append(str(arg) if arg is not None else "")
        return texts

    def test_first_import_issues_inserts_for_all_tasks(self):
        """On initial import, schedule_tasks are inserted with fresh UUIDs."""
        db = self._mock_db()
        db.execute.return_value.fetchall.return_value = []
        result = import_to_db(db, self._full_schedule(), "schedule.csv")

        self.assertEqual(result.tasks_imported, 7)
        self.assertEqual(result.dependencies_imported, 7)
        sql_calls = self._sql_texts(db)
        insert_task_calls = [s for s in sql_calls if "INSERT INTO schedule_tasks" in s]
        self.assertEqual(len(insert_task_calls), 7)
        update_task_calls = [s for s in sql_calls if "UPDATE schedule_tasks" in s]
        self.assertEqual(len(update_task_calls), 0)

    def test_reimport_preserves_task_uuids_and_updates_in_place(self):
        """When tasks already exist, re-import updates them in place using their existing UUIDs."""
        db = self._mock_db()
        existing_uuids = {f"T10{i}": f"00000000-0000-0000-0000-00000000010{i}" for i in range(1, 8)}

        class MockRow:
            def __init__(self, sid, tid):
                self.source_task_id = sid
                self.id = tid

        mock_rows = [MockRow(sid, tid) for sid, tid in existing_uuids.items()]
        db.execute.return_value.fetchall.return_value = mock_rows

        rows = self._full_schedule()
        result = import_to_db(db, rows, "schedule.csv")

        self.assertEqual(result.tasks_imported, 7)
        self.assertEqual(result.dependencies_imported, 7)

        sql_calls = self._sql_texts(db)
        delete_task_calls = [s for s in sql_calls if "DELETE FROM schedule_tasks" in s]
        self.assertEqual(len(delete_task_calls), 0, "schedule_tasks should NOT be deleted on re-import")

        insert_task_calls = [s for s in sql_calls if "INSERT INTO schedule_tasks" in s]
        self.assertEqual(len(insert_task_calls), 0, "No new tasks should be inserted for existing tasks")

        update_task_calls = [c for c in db.execute.call_args_list if "UPDATE schedule_tasks" in str(c.args[0])]
        self.assertEqual(len(update_task_calls), 7)

        updated_uuids = {c.args[1]["id"] for c in update_task_calls}
        self.assertEqual(updated_uuids, set(existing_uuids.values()))

    def test_reimport_refreshes_dependencies_without_duplication(self):
        """Dependencies for imported tasks are cleared and re-inserted cleanly."""
        db = self._mock_db()
        import_to_db(db, self._full_schedule(), "schedule.csv")

        sql_calls = self._sql_texts(db)
        dep_delete_calls = [s for s in sql_calls if "DELETE FROM task_dependencies" in s]
        self.assertEqual(len(dep_delete_calls), 1, "Expected exactly one DELETE on task_dependencies")

        dep_insert_calls = [s for s in sql_calls if "INSERT INTO task_dependencies" in s]
        self.assertEqual(len(dep_insert_calls), 7, "Expected 7 dependency inserts for T101-T107")

    def test_multi_dependency_t107_to_t104_and_t106_preserved(self):
        """T107 with dependencies [T104, T106] generates 2 distinct dependency inserts."""
        db = self._mock_db()
        rows = [
            ParsedRow("T104", "Cable Laying A", "Zone A",
                      datetime.date(2026, 9, 21), datetime.date(2026, 9, 25), dependencies=[]),
            ParsedRow("T106", "Cable Laying B", "Zone A",
                      datetime.date(2026, 9, 16), datetime.date(2026, 9, 20), dependencies=[]),
            ParsedRow("T107", "Commissioning",  "Zone A",
                      datetime.date(2026, 9, 26), datetime.date(2026, 9, 30), dependencies=["T104", "T106"]),
        ]
        result = import_to_db(db, rows, "test.csv")
        self.assertEqual(result.tasks_imported, 3)
        self.assertEqual(result.dependencies_imported, 2)

    def test_stateful_simulated_db_import_twice_no_duplicates_and_references_preserved(self):
        """Stateful test: import twice into a simulated DB store.

        Verifies:
        1. After 1st import: exactly 7 tasks and 7 dependencies.
        2. T102 UUID is recorded and simulated as referenced by ai_processed_updates.
        3. After 2nd import:
           - Still exactly 7 tasks in DB (no duplicates).
           - Still exactly 7 dependencies in DB (no duplicates).
           - T102's UUID is UNCHANGED.
           - The AI update's foreign key reference to T102 remains valid.
        """
        tasks_table: dict[str, dict] = {}
        deps_table: set[tuple[str, str]] = set()

        class SimulatedRow:
            def __init__(self, id, source_task_id):
                self.id = id
                self.source_task_id = source_task_id

        def fake_execute(query, params=None):
            sql = str(query)
            params = params or {}
            mock_res = MagicMock()

            if "SELECT st.id, st.source_task_id" in sql:
                matching = [
                    SimulatedRow(t["id"], t["source_task_id"])
                    for t in tasks_table.values()
                    if t["source_task_id"] in params.values()
                ]
                mock_res.fetchall.return_value = matching
                return mock_res

            if "INSERT INTO schedule_tasks" in sql:
                tid = params["id"]
                tasks_table[tid] = {
                    "id": tid,
                    "source_task_id": params["source_task_id"],
                    "activity": params["activity"],
                    "location": params["location"],
                    "planned_start": params["planned_start"],
                    "planned_end": params["planned_end"],
                }
                return mock_res

            if "UPDATE schedule_tasks" in sql:
                tid = params["id"]
                if tid in tasks_table:
                    tasks_table[tid]["activity"] = params["activity"]
                    tasks_table[tid]["location"] = params["location"]
                    tasks_table[tid]["planned_start"] = params["planned_start"]
                    tasks_table[tid]["planned_end"] = params["planned_end"]
                return mock_res

            if "DELETE FROM task_dependencies" in sql:
                del_ids = set(params.values())
                deps_table.difference_update({(d, u) for (d, u) in deps_table if d in del_ids})
                return mock_res

            if "INSERT INTO task_dependencies" in sql:
                deps_table.add((params["task_id"], params["depends_on_task_id"]))
                return mock_res

            if "DELETE FROM schedule_tasks" in sql:
                del_ids = set(params.values())
                for did in del_ids:
                    tasks_table.pop(did, None)
                return mock_res

            mock_res.fetchall.return_value = []
            return mock_res

        db = MagicMock()
        db.execute.side_effect = fake_execute

        rows = self._full_schedule()

        # === 1st Import ===
        res1 = import_to_db(db, rows, "schedule.csv")
        self.assertEqual(res1.tasks_imported, 7)
        self.assertEqual(res1.dependencies_imported, 7)
        self.assertEqual(len(tasks_table), 7)
        self.assertEqual(len(deps_table), 7)

        task_uuids_first_import = {t["source_task_id"]: t["id"] for t in tasks_table.values()}
        self.assertEqual(set(task_uuids_first_import.keys()), {"T101", "T102", "T103", "T104", "T105", "T106", "T107"})

        t102_original_uuid = task_uuids_first_import["T102"]
        ai_processed_update_matched_task_id = t102_original_uuid

        # === 2nd Import of the EXACT SAME CSV ===
        res2 = import_to_db(db, rows, "schedule.csv")
        self.assertEqual(res2.tasks_imported, 7)
        self.assertEqual(res2.dependencies_imported, 7)

        # 1. No duplicate schedule_tasks
        self.assertEqual(len(tasks_table), 7, "Exactly 7 schedule_tasks must exist after re-import")
        tasks_by_sid = {t["source_task_id"]: t for t in tasks_table.values()}
        self.assertEqual(set(tasks_by_sid.keys()), {"T101", "T102", "T103", "T104", "T105", "T106", "T107"})

        # 2. Existing task UUIDs are 100% PRESERVED
        for sid, orig_uuid in task_uuids_first_import.items():
            self.assertEqual(tasks_by_sid[sid]["id"], orig_uuid, f"UUID for {sid} must be preserved across re-imports")

        # 3. ai_processed_updates reference to T102 is still valid in the database
        self.assertIn(ai_processed_update_matched_task_id, tasks_table, "AI processed update reference to T102 must remain valid")
        self.assertEqual(tasks_table[ai_processed_update_matched_task_id]["source_task_id"], "T102")

        # 4. No duplicate dependencies
        self.assertEqual(len(deps_table), 7, "Exactly 7 dependency edges must exist after re-import")

        # 5. Verify T107 has multiple dependencies (to T104 and T106)
        t107_id = task_uuids_first_import["T107"]
        t104_id = task_uuids_first_import["T104"]
        t106_id = task_uuids_first_import["T106"]
        t107_deps = {u for (d, u) in deps_table if d == t107_id}
        self.assertEqual(t107_deps, {t104_id, t106_id}, "T107 must depend on both T104 and T106")

    def test_empty_import_skips_processing(self):
        """An empty rows list commits the import record and skips task processing."""
        db = self._mock_db()
        result = import_to_db(db, [], "empty.csv")
        self.assertEqual(result.tasks_imported, 0)
        self.assertEqual(result.dependencies_imported, 0)
        sql_calls = self._sql_texts(db)
        self.assertEqual(len(sql_calls), 1)
        self.assertIn("schedule_imports", sql_calls[0])

    def test_import_to_db_replace_clears_old_schedule_and_imports_new(self):
        """When replace=True, task_dependencies and schedule_tasks are cleared before inserting."""
        db = self._mock_db()
        rows = [
            ParsedRow("P101", "Survey", "Site 1",
                      datetime.date(2026, 10, 1), datetime.date(2026, 10, 5), dependencies=[]),
            ParsedRow("P102", "Excavation", "Site 1",
                      datetime.date(2026, 10, 6), datetime.date(2026, 10, 10), dependencies=["P101"]),
        ]
        result = import_to_db(db, rows, "new_project_schedule.csv", replace=True)
        self.assertEqual(result.tasks_imported, 2)
        self.assertEqual(result.dependencies_imported, 1)

        sql_calls = self._sql_texts(db)
        self.assertTrue(any("DELETE FROM task_dependencies" in s for s in sql_calls))
        self.assertTrue(any("DELETE FROM schedule_tasks" in s for s in sql_calls))
        self.assertTrue(any("UPDATE ai_processed_updates SET matched_task_id = NULL" in s for s in sql_calls))
        self.assertTrue(any("UPDATE site_updates" in s and "archived_at = :archived_at" in s for s in sql_calls))

    def test_import_to_db_replace_archives_site_updates_while_non_replace_does_not(self):
        """replace=True archives active site_updates with archived_at timestamp, whereas replace=False does not touch site_updates."""
        db_replace = self._mock_db()
        rows = [
            ParsedRow("P101", "Survey", "Site 1",
                      datetime.date(2026, 10, 1), datetime.date(2026, 10, 5), dependencies=[]),
        ]
        import_to_db(db_replace, rows, "new_schedule.csv", replace=True)
        sql_calls_replace = self._sql_texts(db_replace)
        self.assertTrue(any("UPDATE site_updates" in s and "SET archived_at = :archived_at" in s and "WHERE archived_at IS NULL" in s for s in sql_calls_replace))

        db_normal = self._mock_db()
        import_to_db(db_normal, rows, "same_schedule.csv", replace=False)
        sql_calls_normal = self._sql_texts(db_normal)
        self.assertFalse(any("UPDATE site_updates" in s for s in sql_calls_normal))


class CSBScheduleDependencyImportTests(unittest.TestCase):
    """Regression tests for importing CSB101–CSB120 schedule with single and multi-dependencies."""

    def test_import_csb101_to_csb120_schedule_creates_correct_dependency_records(self):
        """Verify:
        - CSB101–CSB120 (20 tasks) imported
        - Correct total number of dependency records (23)
        - Specifically verify:
            CSB116 -> CSB114
            CSB116 -> CSB115
        - Verify other multi-dependency tasks:
            CSB117 -> CSB110, CSB111, CSB112
            CSB119 -> CSB117, CSB118
            CSB120 -> CSB119
        """
        csb_rows = [
            # Task ID, Activity, Location, Start, End, Dependency
            ["CSB101", "Site Survey and Setting Out", "Compressor Station B", "10-Sep-2026", "12-Sep-2026", "-"],
            ["CSB102", "Site Clearing and Grading", "Compressor Station B", "13-Sep-2026", "16-Sep-2026", "CSB101"],
            ["CSB103", "Excavation for Compressor Foundation", "Compressor Station B", "17-Sep-2026", "21-Sep-2026", "CSB102"],
            ["CSB104", "Foundation Reinforcement Installation", "Compressor Station B", "22-Sep-2026", "25-Sep-2026", "CSB103"],
            ["CSB105", "Compressor Foundation Installation", "Compressor Station B", "26-Sep-2026", "30-Sep-2026", "CSB104"],
            ["CSB106", "Foundation Concrete Curing", "Compressor Station B", "01-Oct-2026", "05-Oct-2026", "CSB105"],
            ["CSB107", "Equipment Base Plate Installation", "Compressor Station B", "06-Oct-2026", "08-Oct-2026", "CSB106"],
            ["CSB108", "Compressor Skid Installation", "Compressor Station B", "09-Oct-2026", "12-Oct-2026", "CSB107"],
            ["CSB109", "Compressor Alignment", "Compressor Station B", "13-Oct-2026", "15-Oct-2026", "CSB108"],
            ["CSB110", "Lube Oil System Installation", "Compressor Station B", "13-Oct-2026", "17-Oct-2026", "CSB109"],
            ["CSB111", "Cooling Water Piping Installation", "Compressor Station B", "18-Oct-2026", "22-Oct-2026", "CSB110"],
            ["CSB112", "Process Piping Connection", "Compressor Station B", "23-Oct-2026", "27-Oct-2026", "CSB111"],
            ["CSB113", "Electrical Cable Tray Installation", "Compressor Station B", "23-Oct-2026", "27-Oct-2026", "CSB112"],
            ["CSB114", "Power Cable Installation", "Compressor Station B", "28-Oct-2026", "31-Oct-2026", "CSB113"],
            ["CSB115", "Instrumentation Installation", "Compressor Station B", "28-Oct-2026", "02-Nov-2026", "CSB114"],
            # Multi-dependency with semicolon:
            ["CSB116", "Control Panel Installation", "Compressor Station B", "29-Oct-2026", "31-Oct-2026", "CSB114;CSB115"],
            # Multi-dependency with comma:
            ["CSB117", "Mechanical Inspection", "Compressor Station B", "06-Nov-2026", "08-Nov-2026", "CSB110, CSB111, CSB112"],
            ["CSB118", "Electrical and Instrumentation Testing", "Compressor Station B", "09-Nov-2026", "11-Nov-2026", "CSB116"],
            # Multi-dependency with comma:
            ["CSB119", "Compressor Trial Run", "Compressor Station B", "12-Nov-2026", "14-Nov-2026", "CSB117, CSB118"],
            # Single dependency:
            ["CSB120", "Final Commissioning and Handover", "Compressor Station B", "15-Nov-2026", "17-Nov-2026", "CSB119"],
        ]

        csv_bytes = _csv(*csb_rows)
        parsed_rows, errors = parse_and_validate(csv_bytes, "csb_schedule.csv")
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(len(parsed_rows), 20)

        # Verify parsed dependencies on individual tasks
        csb116_parsed = next(r for r in parsed_rows if r.source_task_id == "CSB116")
        self.assertEqual(csb116_parsed.dependencies, ["CSB114", "CSB115"])

        csb117_parsed = next(r for r in parsed_rows if r.source_task_id == "CSB117")
        self.assertEqual(csb117_parsed.dependencies, ["CSB110", "CSB111", "CSB112"])

        csb119_parsed = next(r for r in parsed_rows if r.source_task_id == "CSB119")
        self.assertEqual(csb119_parsed.dependencies, ["CSB117", "CSB118"])

        csb120_parsed = next(r for r in parsed_rows if r.source_task_id == "CSB120")
        self.assertEqual(csb120_parsed.dependencies, ["CSB119"])

        # Now test import_to_db creates all 23 dependency records in PostgreSQL
        db = MagicMock()
        db.execute.return_value = MagicMock()
        result = import_to_db(db, parsed_rows, "csb_schedule.csv")

        self.assertEqual(result.tasks_imported, 20)
        self.assertEqual(result.dependencies_imported, 23)

        # Inspect DB executed calls to map UUIDs back to source_task_id
        uuid_to_source: dict[str, str] = {}
        for call_args in db.execute.call_args_list:
            sql_text = str(call_args[0][0])
            if "INSERT INTO schedule_tasks" in sql_text:
                p = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
                uuid_to_source[p["id"]] = p["source_task_id"]

        inserted_deps: list[tuple[str, str]] = []
        for call_args in db.execute.call_args_list:
            sql_text = str(call_args[0][0])
            if "INSERT INTO task_dependencies" in sql_text:
                p = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
                downstream = uuid_to_source[p["task_id"]]
                upstream = uuid_to_source[p["depends_on_task_id"]]
                inserted_deps.append((downstream, upstream))

        self.assertEqual(len(inserted_deps), 23)

        # Specifically verify CSB116 -> CSB114 and CSB116 -> CSB115
        self.assertIn(("CSB116", "CSB114"), inserted_deps)
        self.assertIn(("CSB116", "CSB115"), inserted_deps)

        # Specifically verify CSB117 -> CSB110, CSB111, CSB112
        self.assertIn(("CSB117", "CSB110"), inserted_deps)
        self.assertIn(("CSB117", "CSB111"), inserted_deps)
        self.assertIn(("CSB117", "CSB112"), inserted_deps)

        # Specifically verify CSB119 -> CSB117, CSB118
        self.assertIn(("CSB119", "CSB117"), inserted_deps)
        self.assertIn(("CSB119", "CSB118"), inserted_deps)

        # Specifically verify CSB120 -> CSB119
        self.assertIn(("CSB120", "CSB119"), inserted_deps)


if __name__ == "__main__":
    unittest.main()
