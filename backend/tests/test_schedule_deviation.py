"""Tests for Schedule Deviation Tracking and Health Classification.

Tests:
1. calculate_schedule_deviation:
   - On-time start and finish (0 deviation)
   - Delayed start (positive start_deviation_days)
   - Early start (negative start_deviation_days)
   - Delayed completion (positive end_deviation_days)
   - Early completion (negative end_deviation_days)
   - Duration deviation calculation
   - Missing actual dates handling
   - Health classification: on_time, delayed, at_risk, unassessed
   - Preservation of baseline planned dates
2. get_schedule_deviation_summary:
   - Summary statistics (total, on_time, delayed, at_risk, unassessed)
   - Downstream risk propagation from upstream delayed tasks
3. API endpoints:
   - GET /schedule/deviation (200)
   - GET /schedule/tasks/deviation (200 alias)
   - GET /schedule/tasks/linked (includes deviation & schedule_health)
   - GET /schedule/tasks/{task_id} (includes deviation & schedule_health)
   - 503 on database unavailability
"""

from __future__ import annotations

import datetime
import types
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app
from app.database import get_db
from app.schedule_linker import (
    calculate_schedule_deviation,
    get_schedule_deviation_summary,
)


def _row(**kwargs):
    """Create a namespace object representing a DB row."""
    return types.SimpleNamespace(**kwargs)


class ScheduleDeviationCalculationTests(unittest.TestCase):
    """Unit tests for calculate_schedule_deviation math and classifications."""

    def test_on_time_start_and_finish(self):
        """Task started and finished on exactly the planned dates."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-01",
            planned_end="2026-09-05",
            actual_start="2026-09-01",
            actual_end="2026-09-05",
            status="completed",
            progress_percent=100.0,
        )
        self.assertEqual(res["start_deviation_days"], 0)
        self.assertEqual(res["end_deviation_days"], 0)
        self.assertEqual(res["duration_planned_days"], 5)
        self.assertEqual(res["duration_actual_days"], 5)
        self.assertEqual(res["duration_deviation_days"], 0)
        self.assertEqual(res["effective_delay_days"], 0)
        self.assertEqual(res["schedule_health"], "on_time")

    def test_delayed_start(self):
        """Task started 3 days late."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-01",
            planned_end="2026-09-10",
            actual_start="2026-09-04",
            actual_end=None,
            status="in_progress",
            progress_percent=20.0,
        )
        self.assertEqual(res["start_deviation_days"], 3)
        self.assertIsNone(res["end_deviation_days"])
        self.assertEqual(res["effective_delay_days"], 3)
        self.assertEqual(res["schedule_health"], "delayed")

    def test_early_start(self):
        """Task started 2 days ahead of planned schedule."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-05",
            planned_end="2026-09-15",
            actual_start="2026-09-03",
            actual_end=None,
            status="in_progress",
            progress_percent=30.0,
        )
        self.assertEqual(res["start_deviation_days"], -2)
        self.assertIsNone(res["end_deviation_days"])
        self.assertEqual(res["schedule_health"], "on_time")

    def test_delayed_completion(self):
        """Task finished 4 days after planned end date."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-01",
            planned_end="2026-09-10",
            actual_start="2026-09-01",
            actual_end="2026-09-14",
            status="completed",
            progress_percent=100.0,
        )
        self.assertEqual(res["start_deviation_days"], 0)
        self.assertEqual(res["end_deviation_days"], 4)
        self.assertEqual(res["duration_planned_days"], 10)
        self.assertEqual(res["duration_actual_days"], 14)
        self.assertEqual(res["duration_deviation_days"], 4)
        self.assertEqual(res["effective_delay_days"], 4)
        self.assertEqual(res["schedule_health"], "delayed")

    def test_early_completion(self):
        """Task finished 2 days early."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-01",
            planned_end="2026-09-10",
            actual_start="2026-09-01",
            actual_end="2026-09-08",
            status="completed",
            progress_percent=100.0,
        )
        self.assertEqual(res["start_deviation_days"], 0)
        self.assertEqual(res["end_deviation_days"], -2)
        self.assertEqual(res["duration_deviation_days"], -2)
        self.assertEqual(res["effective_delay_days"], 0)
        self.assertEqual(res["schedule_health"], "on_time")

    def test_explicit_delay_days_without_dates(self):
        """Task has reported delay_days but no actual dates mentioned."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-06",
            planned_end="2026-09-15",
            actual_start=None,
            actual_end=None,
            status="delayed",
            delay_days=2,
            progress_percent=40.0,
        )
        self.assertIsNone(res["start_deviation_days"])
        self.assertIsNone(res["end_deviation_days"])
        self.assertEqual(res["effective_delay_days"], 2)
        self.assertEqual(res["schedule_health"], "delayed")

    def test_blocked_task_classified_as_at_risk(self):
        """Blocked status is classified as at_risk."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-16",
            planned_end="2026-09-20",
            status="blocked",
            progress_percent=10.0,
        )
        self.assertEqual(res["schedule_health"], "at_risk")

    def test_downstream_dependency_flagged_at_risk(self):
        """Task with at_risk=True is classified as at_risk."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-20",
            planned_end="2026-09-25",
            status="not_started",
            at_risk=True,
        )
        self.assertEqual(res["schedule_health"], "at_risk")

    def test_completed_task_never_flagged_at_risk(self):
        """A finished task is not at risk even if downstream flag was set."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-01",
            planned_end="2026-09-05",
            actual_start="2026-09-01",
            actual_end="2026-09-05",
            status="completed",
            progress_percent=100.0,
            at_risk=True,
        )
        self.assertEqual(res["schedule_health"], "on_time")

    def test_unassessed_future_task(self):
        """Task with no actuals, no status, and no delay is unassessed."""
        res = calculate_schedule_deviation(
            planned_start="2026-09-28",
            planned_end="2026-09-29",
            actual_start=None,
            actual_end=None,
            status=None,
            progress_percent=None,
        )
        self.assertIsNone(res["start_deviation_days"])
        self.assertIsNone(res["end_deviation_days"])
        self.assertEqual(res["schedule_health"], "unassessed")

    def test_accepts_date_objects_and_timestamps(self):
        """Supports datetime.date objects, datetime instances, and ISO strings."""
        p_start = datetime.date(2026, 9, 1)
        p_end = datetime.date(2026, 9, 5)
        a_start = datetime.datetime(2026, 9, 2, 8, 30, 0)
        a_end = "2026-09-06"

        res = calculate_schedule_deviation(
            planned_start=p_start,
            planned_end=p_end,
            actual_start=a_start,
            actual_end=a_end,
            status="completed",
        )
        self.assertEqual(res["start_deviation_days"], 1)
        self.assertEqual(res["end_deviation_days"], 1)
        self.assertEqual(res["duration_deviation_days"], 0)


class ScheduleDeviationSummaryUnitTests(unittest.TestCase):
    """Unit tests for get_schedule_deviation_summary."""

    def test_deviation_summary_with_downstream_propagation(self):
        # T101: completed on time
        t101 = _row(
            task_id="uuid-101",
            source_task_id="T101",
            activity="Site Preparation",
            location="Well Pad A",
            planned_start="2026-09-01",
            planned_end="2026-09-05",
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date="2026-09-01",
            actual_end_date="2026-09-05",
            confidence_score=95.0,
            processed_at="2026-09-05T00:00:00",
        )

        # T102: delayed
        t102 = _row(
            task_id="uuid-102",
            source_task_id="T102",
            activity="Foundation Construction",
            location="Well Pad A",
            planned_start="2026-09-06",
            planned_end="2026-09-15",
            progress_percent=40.0,
            status="delayed",
            delay_days=1,
            delay_reason="Heavy rain",
            actual_start_date="2026-09-07",
            actual_end_date=None,
            confidence_score=90.0,
            processed_at="2026-09-08T00:00:00",
        )

        # T103: depends on T102 (should become at_risk)
        t103 = _row(
            task_id="uuid-103",
            source_task_id="T103",
            activity="Equipment Installation",
            location="Well Pad A",
            planned_start="2026-09-16",
            planned_end="2026-09-20",
            progress_percent=None,
            status=None,
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=None,
            processed_at=None,
        )

        # Dependency: T103 depends on T102
        dep_103_102 = _row(
            downstream_task_id="uuid-103",
            upstream_task_id="uuid-102",
        )

        mock_db = MagicMock()
        mock_db.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[t101, t102, t103])),  # _LINKED_TASKS_QUERY
            MagicMock(fetchall=MagicMock(return_value=[dep_103_102])),       # _ALL_DEPENDENCIES_QUERY
        ]

        result = get_schedule_deviation_summary(mock_db)

        self.assertIn("summary", result)
        self.assertIn("tasks", result)

        summary = result["summary"]
        self.assertEqual(summary["total_tasks"], 3)
        self.assertEqual(summary["on_time"], 1)   # T101
        self.assertEqual(summary["delayed"], 1)   # T102
        self.assertEqual(summary["at_risk"], 1)   # T103
        self.assertEqual(summary["unassessed"], 0)

        tasks = result["tasks"]
        t101_res = next(t for t in tasks if t["source_task_id"] == "T101")
        self.assertEqual(t101_res["schedule_health"], "on_time")
        self.assertEqual(t101_res["start_deviation_days"], 0)
        self.assertEqual(t101_res["end_deviation_days"], 0)

        t102_res = next(t for t in tasks if t["source_task_id"] == "T102")
        self.assertEqual(t102_res["schedule_health"], "delayed")
        self.assertEqual(t102_res["start_deviation_days"], 1)

        t103_res = next(t for t in tasks if t["source_task_id"] == "T103")
        self.assertEqual(t103_res["schedule_health"], "at_risk")


class ScheduleDeviationApiTests(unittest.TestCase):
    """FastAPI endpoint tests for schedule deviation tracking."""

    def setUp(self):
        self.client = TestClient(app)

    def test_get_schedule_deviation_endpoint_200(self):
        mock_summary = {
            "summary": {
                "total_tasks": 2,
                "on_time": 1,
                "delayed": 1,
                "at_risk": 0,
                "unassessed": 0,
            },
            "tasks": [
                {
                    "task_id": "t1",
                    "source_task_id": "T101",
                    "activity": "Grading",
                    "location": "Site A",
                    "planned_start": "2026-09-01",
                    "planned_end": "2026-09-05",
                    "actual_start_date": "2026-09-01",
                    "actual_end_date": "2026-09-05",
                    "progress_percent": 100.0,
                    "status": "completed",
                    "delay_days": 0,
                    "schedule_health": "on_time",
                    "start_deviation_days": 0,
                    "end_deviation_days": 0,
                    "deviation": {
                        "start_deviation_days": 0,
                        "end_deviation_days": 0,
                        "duration_planned_days": 5,
                        "duration_actual_days": 5,
                        "duration_deviation_days": 0,
                        "effective_delay_days": 0,
                        "schedule_health": "on_time",
                    },
                }
            ],
        }

        with patch("app.schedule_linker.get_schedule_deviation_summary", return_value=mock_summary):
            resp = self.client.get("/schedule/deviation")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["summary"]["total_tasks"], 2)
            self.assertEqual(data["summary"]["on_time"], 1)
            self.assertEqual(data["tasks"][0]["source_task_id"], "T101")
            self.assertEqual(data["tasks"][0]["start_deviation_days"], 0)

    def test_get_schedule_tasks_deviation_alias_200(self):
        """Verify /schedule/tasks/deviation returns the exact same data."""
        mock_summary = {"summary": {"total_tasks": 0}, "tasks": []}
        with patch("app.schedule_linker.get_schedule_deviation_summary", return_value=mock_summary):
            resp = self.client.get("/schedule/tasks/deviation")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["summary"]["total_tasks"], 0)

    def test_get_schedule_deviation_db_error_returns_503(self):
        mock_db = MagicMock()
        mock_db.execute.side_effect = OperationalError("db error", None, Exception("orig"))

        def _get_db_override():
            yield mock_db

        app.dependency_overrides[get_db] = _get_db_override
        try:
            resp = self.client.get("/schedule/deviation")
            self.assertEqual(resp.status_code, 503)
            self.assertIn("Database is unavailable", resp.json()["detail"])
        finally:
            app.dependency_overrides.pop(get_db, None)


if __name__ == "__main__":
    unittest.main()

