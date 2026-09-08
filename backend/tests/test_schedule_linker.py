"""Tests for app/schedule_linker.py.

The dataset mirrors the requirement spec:
  T102 -> T103 -> T104
  T102 -> T105 -> T106
  T104 + T106 -> T107   (T107 depends on both T104 and T106)

T102 has status="delayed" with delay_days=1 and progress_percent=40.

All tests are pure unit tests — no real DB, no network.
A lightweight mock Row class stands in for SQLAlchemy Row objects.
"""

from __future__ import annotations

import types
import unittest
from collections import namedtuple
from unittest.mock import MagicMock, call, patch

from app.schedule_linker import (
    _fmt_linked,
    _fmt_task,
    _is_concerning,
    get_linked_tasks,
    get_task_impact,
)


# ---------------------------------------------------------------------------
# Mock row helper
# ---------------------------------------------------------------------------

def _row(**kwargs):
    """Return a simple namespace object that looks like a SQLAlchemy Row."""
    return types.SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# UUID constants for the test dataset
# ---------------------------------------------------------------------------

T102_ID = "aaaaaaaa-0000-0000-0000-000000000102"
T103_ID = "aaaaaaaa-0000-0000-0000-000000000103"
T104_ID = "aaaaaaaa-0000-0000-0000-000000000104"
T105_ID = "aaaaaaaa-0000-0000-0000-000000000105"
T106_ID = "aaaaaaaa-0000-0000-0000-000000000106"
T107_ID = "aaaaaaaa-0000-0000-0000-000000000107"
T101_ID = "aaaaaaaa-0000-0000-0000-000000000101"

# Convenience: all task IDs in topological order
ALL_TASK_IDS = [T101_ID, T102_ID, T103_ID, T104_ID, T105_ID, T106_ID, T107_ID]

# Dependency edges: (downstream, upstream) — mirrors task_dependencies schema
DEPS = [
    (T103_ID, T102_ID),  # T103 depends on T102
    (T104_ID, T103_ID),  # T104 depends on T103
    (T105_ID, T102_ID),  # T105 depends on T102
    (T106_ID, T105_ID),  # T106 depends on T105
    (T107_ID, T104_ID),  # T107 depends on T104
    (T107_ID, T106_ID),  # T107 also depends on T106
]


def _make_task_row(tid, source_id, status=None, progress=None, delay=None):
    """Helper: build a row for _ALL_TASKS_QUERY / _SINGLE_TASK_QUERY."""
    return _row(
        task_id=tid,
        source_task_id=source_id,
        activity=f"Activity {source_id}",
        location="Well Pad A",
        planned_start="2026-09-01",
        planned_end="2026-09-10",
        progress_percent=progress,
        status=status,
        delay_days=delay,
        actual_start_date=None,
        actual_end_date=None,
        confidence_score=80.0 if status else None,
        processed_at="2026-09-08T00:00:00",
    )


def _make_dep_row(downstream_id, upstream_id):
    return _row(downstream_task_id=downstream_id, upstream_task_id=upstream_id)


def _make_linked_row(tid, source_id, status=None, progress=None, delay=None):
    """Helper: build a row for _LINKED_TASKS_QUERY."""
    row = _make_task_row(tid, source_id, status, progress, delay)
    row.delay_reason = "Heavy rain" if status == "delayed" else None
    row.ai_matched_task_id = tid if status else None
    return row


# ---------------------------------------------------------------------------
# Mock DB session helpers
# ---------------------------------------------------------------------------

def _make_db(
    linked_rows=None,
    single_task_row=None,
    all_task_rows=None,
    dep_rows=None,
):
    """Return a MagicMock Session whose execute() returns appropriate results."""
    db = MagicMock()
    call_count = [0]

    # Store the sequence of results to return on successive execute() calls
    results = []
    if single_task_row is not None:
        # get_task_impact: first call = single task, second = all tasks, third = deps
        results = [single_task_row, all_task_rows or [], dep_rows or []]
    elif linked_rows is not None:
        results = [linked_rows]

    result_iter = iter(results)

    def side_effect(query, *args, **kwargs):
        mock_result = MagicMock()
        try:
            data = next(result_iter)
        except StopIteration:
            data = []
        if isinstance(data, list):
            mock_result.fetchall.return_value = data
            mock_result.fetchone.return_value = data[0] if data else None
        else:
            # Single row
            mock_result.fetchone.return_value = data
            mock_result.fetchall.return_value = [data]
        return mock_result

    db.execute.side_effect = side_effect
    return db


# ---------------------------------------------------------------------------
# _is_concerning
# ---------------------------------------------------------------------------

class IsConcerningTests(unittest.TestCase):
    def test_delayed_is_concerning(self):
        self.assertTrue(_is_concerning({"status": "delayed", "progress_percent": 40}))

    def test_blocked_is_concerning(self):
        self.assertTrue(_is_concerning({"status": "blocked", "progress_percent": None}))

    def test_in_progress_less_than_100_is_concerning(self):
        self.assertTrue(_is_concerning({"status": "in_progress", "progress_percent": 60}))

    def test_in_progress_100_percent_is_NOT_concerning(self):
        self.assertFalse(_is_concerning({"status": "in_progress", "progress_percent": 100.0}))

    def test_completed_is_NOT_concerning(self):
        self.assertFalse(_is_concerning({"status": "completed", "progress_percent": 100.0}))

    def test_no_status_is_NOT_concerning(self):
        """Tasks with no AI assessment must not generate false risk signals."""
        self.assertFalse(_is_concerning({"status": None, "progress_percent": None}))

    def test_not_started_is_concerning(self):
        self.assertTrue(_is_concerning({"status": "not_started", "progress_percent": 0}))

    def test_in_progress_with_none_progress_is_concerning(self):
        self.assertTrue(_is_concerning({"status": "in_progress", "progress_percent": None}))


# ---------------------------------------------------------------------------
# _fmt_linked / _fmt_task
# ---------------------------------------------------------------------------

class FmtLinkedTests(unittest.TestCase):
    def test_all_fields_present(self):
        row = _make_linked_row(T102_ID, "T102", "delayed", 40.0, 1)
        result = _fmt_linked(row)
        self.assertEqual(result["source_task_id"], "T102")
        self.assertEqual(result["status"], "delayed")
        self.assertEqual(result["progress_percent"], 40.0)
        self.assertEqual(result["delay_days"], 1)
        self.assertIsNone(result["actual_start_date"])

    def test_null_ai_fields_when_no_ai_data(self):
        row = _make_linked_row(T101_ID, "T101")
        result = _fmt_linked(row)
        self.assertIsNone(result["status"])
        self.assertIsNone(result["progress_percent"])
        self.assertIsNone(result["delay_days"])

    def test_task_id_is_string(self):
        row = _make_linked_row(T102_ID, "T102")
        result = _fmt_linked(row)
        self.assertIsInstance(result["task_id"], str)


class FmtTaskTests(unittest.TestCase):
    def test_progress_is_float(self):
        row = _make_task_row(T102_ID, "T102", "delayed", 40, 1)
        result = _fmt_task(row)
        self.assertIsInstance(result["progress_percent"], float)

    def test_null_progress_stays_none(self):
        row = _make_task_row(T103_ID, "T103")
        result = _fmt_task(row)
        self.assertIsNone(result["progress_percent"])


# ---------------------------------------------------------------------------
# get_linked_tasks
# ---------------------------------------------------------------------------

class GetLinkedTasksTests(unittest.TestCase):
    def _run(self, rows):
        db = _make_db(linked_rows=rows)
        return get_linked_tasks(db)

    def test_returns_all_tasks(self):
        rows = [
            _make_linked_row(T101_ID, "T101"),
            _make_linked_row(T102_ID, "T102", "delayed", 40.0, 1),
        ]
        result = self._run(rows)
        self.assertEqual(len(result), 2)

    def test_task_with_ai_result_has_status(self):
        rows = [_make_linked_row(T102_ID, "T102", "delayed", 40.0, 1)]
        result = self._run(rows)
        self.assertEqual(result[0]["status"], "delayed")
        self.assertEqual(result[0]["progress_percent"], 40.0)
        self.assertEqual(result[0]["delay_days"], 1)

    def test_task_without_ai_result_has_null_status(self):
        rows = [_make_linked_row(T101_ID, "T101")]
        result = self._run(rows)
        self.assertIsNone(result[0]["status"])
        self.assertIsNone(result[0]["progress_percent"])

    def test_empty_schedule_returns_empty_list(self):
        result = self._run([])
        self.assertEqual(result, [])

    def test_each_task_has_required_keys(self):
        rows = [_make_linked_row(T102_ID, "T102", "in_progress", 60.0)]
        result = self._run(rows)
        for key in ("task_id", "source_task_id", "activity", "location",
                    "planned_start", "planned_end", "status", "progress_percent",
                    "delay_days", "delay_reason", "actual_start_date",
                    "actual_end_date", "confidence_score", "processed_at"):
            self.assertIn(key, result[0], f"Missing key: {key}")


# ---------------------------------------------------------------------------
# get_task_impact — happy path and T102 scenario
# ---------------------------------------------------------------------------

class GetTaskImpactTests(unittest.TestCase):

    def _build_full_dataset(self, root_tid=T102_ID, root_status="delayed", root_progress=40.0, root_delay=1):
        """Build the full 7-task dataset with T102 as the delayed root."""
        all_tasks = [
            _make_task_row(T101_ID, "T101", "completed", 100.0),
            _make_task_row(T102_ID, "T102", root_status, root_progress, root_delay),
            _make_task_row(T103_ID, "T103"),
            _make_task_row(T104_ID, "T104"),
            _make_task_row(T105_ID, "T105"),
            _make_task_row(T106_ID, "T106"),
            _make_task_row(T107_ID, "T107"),
        ]
        dep_rows = [_make_dep_row(down, up) for down, up in DEPS]

        # Single task row has extra delay_reason field
        root_single = _make_task_row(root_tid, "T102", root_status, root_progress, root_delay)
        root_single.delay_reason = "Rainfall"

        db = MagicMock()
        call_results = iter([root_single, all_tasks, dep_rows])

        def execute_side(query, *args, **kwargs):
            m = MagicMock()
            data = next(call_results)
            if isinstance(data, list):
                m.fetchall.return_value = data
                m.fetchone.return_value = data[0] if data else None
            else:
                m.fetchone.return_value = data
                m.fetchall.return_value = [data]
            return m

        db.execute.side_effect = execute_side
        return db

    def test_returns_none_for_unknown_task(self):
        db = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        db.execute.return_value = mock_result
        result = get_task_impact(db, "nonexistent-uuid")
        self.assertIsNone(result)

    def test_result_structure_has_required_keys(self):
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        self.assertIsNotNone(result)
        self.assertIn("task", result)
        self.assertIn("downstream", result)
        self.assertIn("downstream_affected_count", result)

    # --- T102 scenario: delayed task with full dependency chain ---

    def test_t102_root_task_has_delay_info(self):
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        task = result["task"]
        self.assertEqual(task["source_task_id"], "T102")
        self.assertEqual(task["status"], "delayed")
        self.assertEqual(task["progress_percent"], 40.0)
        self.assertEqual(task["delay_days"], 1)

    def test_t102_all_five_downstream_tasks_identified(self):
        """T103, T104, T105, T106, T107 must all appear in downstream."""
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        downstream_source_ids = {t["source_task_id"] for t in result["downstream"]}
        for expected in ("T103", "T104", "T105", "T106", "T107"):
            self.assertIn(expected, downstream_source_ids,
                          f"{expected} missing from downstream impact")

    def test_t101_not_in_downstream_of_t102(self):
        """T101 is upstream of T102 — it must NOT appear as a downstream impact."""
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        downstream_source_ids = {t["source_task_id"] for t in result["downstream"]}
        self.assertNotIn("T101", downstream_source_ids)

    def test_t102_itself_not_in_downstream(self):
        """The root task must not list itself as a downstream impact."""
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        downstream_source_ids = {t["source_task_id"] for t in result["downstream"]}
        self.assertNotIn("T102", downstream_source_ids)

    def test_downstream_count_is_five(self):
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        self.assertEqual(result["downstream_affected_count"], 5)

    def test_all_downstream_tasks_are_at_risk_when_t102_delayed(self):
        """Every downstream task must have at_risk=True when root is delayed."""
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        for task in result["downstream"]:
            self.assertTrue(
                task["at_risk"],
                f"{task['source_task_id']} should be at_risk"
            )

    def test_downstream_tasks_have_risk_reason(self):
        """Each at-risk task must have a non-empty risk_reason string."""
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        for task in result["downstream"]:
            self.assertIn("risk_reason", task)
            self.assertIsInstance(task["risk_reason"], str)
            self.assertGreater(len(task["risk_reason"]), 0)

    def test_downstream_delay_not_propagated_as_exact_days(self):
        """Downstream tasks must NOT have an invented delay_days value
        equal to the upstream delay — that would be hallucination."""
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        # Downstream tasks in our mock have no AI data, so delay_days is None
        # (the BFS adds at_risk/risk_reason but must NOT copy upstream delay_days)
        for task in result["downstream"]:
            # delay_days must not be artificially set to 1 (the upstream value)
            # It may be None (no AI data) or its own assessed value.
            # In the mock dataset all downstream tasks have no AI data -> None.
            self.assertIsNone(task.get("delay_days"),
                              f"{task['source_task_id']} should not have delay_days set")

    def test_risk_reason_mentions_upstream_task_id(self):
        """The risk reason must identify the upstream task causing the risk."""
        db = self._build_full_dataset()
        result = get_task_impact(db, T102_ID)
        for task in result["downstream"]:
            self.assertIn("T102", task["risk_reason"],
                          f"risk_reason for {task['source_task_id']} should mention T102")

    # --- completed task: no downstream risk ---

    def _build_completed_t102_dataset(self):
        all_tasks = [
            _make_task_row(T102_ID, "T102", "completed", 100.0, None),
            _make_task_row(T103_ID, "T103"),
        ]
        dep_rows = [_make_dep_row(T103_ID, T102_ID)]
        root_single = _make_task_row(T102_ID, "T102", "completed", 100.0, None)
        root_single.delay_reason = None

        db = MagicMock()
        call_results = iter([root_single, all_tasks, dep_rows])

        def execute_side(query, *args, **kwargs):
            m = MagicMock()
            data = next(call_results)
            if isinstance(data, list):
                m.fetchall.return_value = data
                m.fetchone.return_value = data[0] if data else None
            else:
                m.fetchone.return_value = data
                m.fetchall.return_value = [data]
            return m

        db.execute.side_effect = execute_side
        return db

    def test_completed_task_downstream_not_at_risk(self):
        """When T102 is completed, its downstream tasks must NOT be flagged at_risk."""
        db = self._build_completed_t102_dataset()
        result = get_task_impact(db, T102_ID)
        self.assertIsNotNone(result)
        for task in result["downstream"]:
            self.assertFalse(task["at_risk"],
                             f"{task['source_task_id']} should not be at_risk")

    # --- no-dependency task ---

    def test_task_with_no_dependencies_has_empty_downstream(self):
        root_single = _make_task_row(T101_ID, "T101", "completed", 100.0)
        root_single.delay_reason = None
        all_tasks = [root_single]
        dep_rows = []  # no deps

        db = MagicMock()
        call_results = iter([root_single, all_tasks, dep_rows])

        def execute_side(query, *args, **kwargs):
            m = MagicMock()
            data = next(call_results)
            if isinstance(data, list):
                m.fetchall.return_value = data
                m.fetchone.return_value = data[0] if data else None
            else:
                m.fetchone.return_value = data
                m.fetchall.return_value = [data]
            return m

        db.execute.side_effect = execute_side
        result = get_task_impact(db, T101_ID)
        self.assertEqual(result["downstream"], [])
        self.assertEqual(result["downstream_affected_count"], 0)

    # --- in_progress with incomplete progress ---

    def test_in_progress_40_percent_flags_downstream_at_risk(self):
        """in_progress with 40% must also flag downstream as at_risk."""
        db = self._build_full_dataset(root_status="in_progress", root_progress=40.0, root_delay=None)
        result = get_task_impact(db, T102_ID)
        for task in result["downstream"]:
            self.assertTrue(task["at_risk"])

    # --- no AI data: no risk flagged ---

    def test_no_ai_data_task_does_not_flag_downstream(self):
        """A task with no AI assessment (status=None) must not generate false risk."""
        root_single = _make_task_row(T101_ID, "T101", status=None)  # no AI data
        root_single.delay_reason = None
        all_tasks = [
            _make_task_row(T101_ID, "T101"),
            _make_task_row(T102_ID, "T102"),
        ]
        dep_rows = [_make_dep_row(T102_ID, T101_ID)]

        db = MagicMock()
        call_results = iter([root_single, all_tasks, dep_rows])

        def execute_side(query, *args, **kwargs):
            m = MagicMock()
            data = next(call_results)
            if isinstance(data, list):
                m.fetchall.return_value = data
                m.fetchone.return_value = data[0] if data else None
            else:
                m.fetchone.return_value = data
                m.fetchall.return_value = [data]
            return m

        db.execute.side_effect = execute_side
        result = get_task_impact(db, T101_ID)
        for task in result["downstream"]:
            self.assertFalse(task["at_risk"])


# ---------------------------------------------------------------------------
# Endpoint integration tests (mocked DB)
# ---------------------------------------------------------------------------

class LinkedTasksEndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    @patch("app.main.schedule_linker.get_linked_tasks")
    def test_linked_tasks_returns_200(self, mock_fn):
        mock_fn.return_value = [
            {
                "task_id": T102_ID, "source_task_id": "T102",
                "activity": "Foundation", "location": "Well Pad A",
                "planned_start": "2026-09-06", "planned_end": "2026-09-15",
                "progress_percent": 40.0, "status": "delayed",
                "delay_days": 1, "delay_reason": "Rain",
                "actual_start_date": None, "actual_end_date": None,
                "confidence_score": 75.0, "processed_at": "2026-09-08T00:00:00",
            }
        ]
        resp = self.client.get("/schedule/tasks/linked")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["source_task_id"], "T102")
        self.assertEqual(body[0]["status"], "delayed")

    @patch("app.main.schedule_linker.get_task_impact")
    def test_impact_returns_200(self, mock_fn):
        mock_fn.return_value = {
            "task": {"task_id": T102_ID, "source_task_id": "T102",
                     "status": "delayed", "progress_percent": 40.0,
                     "delay_days": 1, "delay_reason": "Rain",
                     "activity": "Foundation", "location": "Well Pad A",
                     "planned_start": "2026-09-06", "planned_end": "2026-09-15",
                     "actual_start_date": None, "actual_end_date": None,
                     "confidence_score": 75.0, "processed_at": "2026-09-08T00:00:00"},
            "downstream_affected_count": 5,
            "downstream": [
                {"source_task_id": "T103", "at_risk": True,
                 "risk_reason": "T102 is delayed", "task_id": T103_ID,
                 "status": None, "progress_percent": None, "delay_days": None,
                 "activity": "X", "location": "Well Pad A",
                 "planned_start": "2026-09-16", "planned_end": "2026-09-20",
                 "actual_start_date": None, "actual_end_date": None,
                 "confidence_score": None, "processed_at": None},
            ],
        }
        resp = self.client.get(f"/schedule/tasks/{T102_ID}/impact")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["downstream_affected_count"], 5)
        self.assertTrue(body["downstream"][0]["at_risk"])

    @patch("app.main.schedule_linker.get_task_impact")
    def test_impact_returns_404_for_unknown_task(self, mock_fn):
        mock_fn.return_value = None
        resp = self.client.get("/schedule/tasks/nonexistent-uuid/impact")
        self.assertEqual(resp.status_code, 404)

    @patch("app.main.schedule_linker.get_linked_tasks")
    def test_linked_tasks_db_error_returns_503(self, mock_fn):
        from sqlalchemy.exc import OperationalError
        mock_fn.side_effect = OperationalError("fail", None, None)
        resp = self.client.get("/schedule/tasks/linked")
        self.assertEqual(resp.status_code, 503)

    @patch("app.main.schedule_linker.get_task")
    def test_get_task_endpoint_returns_200(self, mock_fn):
        mock_fn.return_value = {
            "task_id": T101_ID,
            "source_task_id": "T101",
            "activity": "Site Preparation",
            "location": "Well Pad A",
            "planned_start": "2026-09-01",
            "planned_end": "2026-09-05",
            "progress_percent": 100.0,
            "status": "completed",
            "delay_days": None,
            "actual_start_date": "2026-09-01",
            "actual_end_date": "2026-09-05",
            "confidence_score": 80.0,
            "processed_at": "2026-09-05T12:00:00",
        }
        resp = self.client.get(f"/schedule/tasks/{T101_ID}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source_task_id"], "T101")
        self.assertEqual(body["planned_start"], "2026-09-01")
        self.assertEqual(body["actual_start_date"], "2026-09-01")
        self.assertEqual(body["actual_end_date"], "2026-09-05")
        self.assertEqual(body["progress_percent"], 100.0)
        self.assertEqual(body["status"], "completed")

    @patch("app.main.schedule_linker.get_task")
    def test_get_task_endpoint_returns_404_when_not_found(self, mock_fn):
        mock_fn.return_value = None
        resp = self.client.get(f"/schedule/tasks/{T101_ID}")
        self.assertEqual(resp.status_code, 404)

    @patch("app.main.schedule_linker.get_task")
    def test_get_task_endpoint_db_error_returns_503(self, mock_fn):
        from sqlalchemy.exc import OperationalError
        mock_fn.side_effect = OperationalError("fail", None, Exception("fail"))
        resp = self.client.get(f"/schedule/tasks/{T101_ID}")
        self.assertEqual(resp.status_code, 503)


# ---------------------------------------------------------------------------
# get_task (planned vs actual state) tests
# ---------------------------------------------------------------------------

class GetTaskUnitTests(unittest.TestCase):

    def test_get_task_returns_planned_vs_actual(self):
        """get_task returns both planned baseline dates and actual state."""
        row = _make_task_row(
            tid=T101_ID,
            source_id="T101",
            status="in_progress",
            progress=40.0,
            delay=None,
        )
        row.actual_start_date = "2026-09-01"
        row.actual_end_date = None

        db = MagicMock()
        db.execute.return_value.fetchone.return_value = row

        from app.schedule_linker import get_task
        task = get_task(db, T101_ID)

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task["task_id"], T101_ID)
        self.assertEqual(task["source_task_id"], "T101")
        # Baseline planned dates preserved
        self.assertEqual(task["planned_start"], "2026-09-01")
        self.assertEqual(task["planned_end"], "2026-09-10")
        # Actual state reflected
        self.assertEqual(task["actual_start_date"], "2026-09-01")
        self.assertIsNone(task["actual_end_date"])
        self.assertEqual(task["progress_percent"], 40.0)
        self.assertEqual(task["status"], "in_progress")

    def test_get_task_returns_none_when_task_not_found(self):
        """get_task returns None when the task UUID is not found."""
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None

        from app.schedule_linker import get_task
        task = get_task(db, "nonexistent-id")
        self.assertIsNone(task)


if __name__ == "__main__":
    unittest.main()
