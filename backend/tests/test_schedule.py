"""Tests for the /schedule/tasks endpoint."""

import datetime
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.main import list_schedule_tasks


def _make_row(source_task_id, activity, location, planned_start, planned_end):
    """Return a mock row whose attributes match the schedule_tasks projection."""
    row = MagicMock()
    row.source_task_id = source_task_id
    row.activity = activity
    row.location = location
    row.planned_start = planned_start
    row.planned_end = planned_end
    return row


class ListScheduleTasksTests(unittest.TestCase):
    def _call(self, rows):
        """Call list_schedule_tasks with a mock DB session that returns *rows*."""
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = rows
        return list_schedule_tasks(db=mock_db)

    def test_empty_table_returns_empty_list(self):
        result = self._call([])
        self.assertEqual(result, [])

    def test_single_row_is_serialised_correctly(self):
        row = _make_row(
            source_task_id="T-001",
            activity="Site Clearance",
            location="Zone A",
            planned_start=datetime.date(2026, 1, 10),
            planned_end=datetime.date(2026, 1, 20),
        )
        result = self._call([row])
        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0],
            {
                "source_task_id": "T-001",
                "activity": "Site Clearance",
                "location": "Zone A",
                "planned_start": "2026-01-10",
                "planned_end": "2026-01-20",
            },
        )

    def test_multiple_rows_are_all_returned(self):
        rows = [
            _make_row("T-001", "Clearing", "Zone A",
                      datetime.date(2026, 1, 1), datetime.date(2026, 1, 5)),
            _make_row("T-002", "Excavation", "Zone B",
                      datetime.date(2026, 1, 6), datetime.date(2026, 1, 15)),
        ]
        result = self._call(rows)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["source_task_id"], "T-001")
        self.assertEqual(result[1]["source_task_id"], "T-002")

    def test_only_required_fields_are_present(self):
        row = _make_row("T-001", "Piling", "Zone C",
                        datetime.date(2026, 2, 1), datetime.date(2026, 2, 28))
        result = self._call([row])
        self.assertEqual(
            set(result[0].keys()),
            {"source_task_id", "activity", "location", "planned_start", "planned_end"},
        )

    def test_database_error_raises_503(self):
        mock_db = MagicMock()
        mock_db.execute.side_effect = OperationalError(
            "connection refused", None, None
        )
        with self.assertRaises(HTTPException) as ctx:
            list_schedule_tasks(db=mock_db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Database is unavailable")


if __name__ == "__main__":
    unittest.main()
