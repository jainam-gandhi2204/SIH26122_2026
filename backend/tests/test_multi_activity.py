"""Tests for Phase 2 multi-activity observation processing.

Verifies:
- Multiple observations persist correctly from a single site update / DPR.
- Primary observation receives observation_index = 0.
- Secondary observations receive deterministic indexes (1, 2, 3...).
- Re-processing expires the old batch once and creates a fresh current batch without duplicates.
- Historical observations remain available in the database with is_current = false.
- Error isolation: one malformed or failing secondary observation does not abort remaining valid observations.
- Multiple valid high-confidence observations independently update schedule actuals.
- Low / medium confidence secondary observations respect thresholds and planner review routing.
- Unmatched observations do not mutate schedule_tasks.
- ObservationList provides backwards compatibility for dict-style access while behaving as a list.
"""

from __future__ import annotations

import datetime
import json
import unittest
from typing import Any
from unittest.mock import MagicMock, call, patch

from app.ai_processor import (
    ObservationList,
    _format_result,
    get_current_result,
    process_site_update,
)
from app.ai_provider import (
    ActivityObservation,
    AIProvider,
    AnalysisResult,
    MockAIProvider,
)

SAMPLE_UPDATE_ID = "11111111-1111-1111-1111-111111111111"
TASK_CS101_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TASK_CS102_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TASK_CS103_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _make_update_row(
    raw_update: str = "Survey 100% complete and clearing 50% complete.",
    reported_on: datetime.date = datetime.date(2026, 9, 21),
    location: str = "Pipeline Section B",
) -> MagicMock:
    row = MagicMock()
    row.id = SAMPLE_UPDATE_ID
    row.source_update_id = "UPD-001"
    row.reported_on = reported_on
    row.location = location
    row.raw_update = raw_update
    row.source_reference = "Daily Field Report"
    return row


def _make_candidate_tasks() -> list[MagicMock]:
    tasks = [
        MagicMock(
            id=TASK_CS101_ID,
            source_task_id="CS101",
            activity="Site survey and route marking",
            location="Pipeline Section B",
            planned_start="2026-09-15",
            planned_end="2026-09-20",
            status="not_started",
        ),
        MagicMock(
            id=TASK_CS102_ID,
            source_task_id="CS102",
            activity="Right of way clearing",
            location="Pipeline Section B",
            planned_start="2026-09-18",
            planned_end="2026-09-25",
            status="not_started",
        ),
        MagicMock(
            id=TASK_CS103_ID,
            source_task_id="CS103",
            activity="Trench excavation work",
            location="Pipeline Section B",
            planned_start="2026-09-22",
            planned_end="2026-09-30",
            status="not_started",
        ),
    ]
    return tasks


class MultiActivityProcessingTests(unittest.TestCase):
    """Unit tests for Phase 2 multi-activity processing pipeline."""

    def test_observation_list_backwards_compatibility(self):
        """ObservationList behaves as both a list and a dict for primary observation access."""
        obs0 = {"id": "res-0", "matched_task_id": "t-0", "progress_percent": 100.0, "status": "completed"}
        obs1 = {"id": "res-1", "matched_task_id": "t-1", "progress_percent": 50.0, "status": "in_progress"}

        ol = ObservationList([obs0, obs1])

        # List behavior
        self.assertIsInstance(ol, list)
        self.assertEqual(len(ol), 2)
        self.assertEqual(ol[0], obs0)
        self.assertEqual(ol[1], obs1)
        self.assertEqual([x["id"] for x in ol], ["res-0", "res-1"])

        # Dict behavior (delegated to primary observation)
        self.assertEqual(ol["id"], "res-0")
        self.assertEqual(ol["matched_task_id"], "t-0")
        self.assertEqual(ol.get("status"), "completed")
        self.assertEqual(ol.get("nonexistent", "default"), "default")
        self.assertTrue("matched_task_id" in ol)
        self.assertFalse("nonexistent" in ol)
        self.assertIn("id", list(ol.keys()))

    def test_primary_single_activity_receives_index_zero(self):
        """A single-activity update produces exactly 1 observation with observation_index=0."""
        mock_db = MagicMock()
        update_row = _make_update_row("Route survey completed.")
        tasks = _make_candidate_tasks()

        inserted_mock = MagicMock()
        inserted_mock.id = "proc-primary-uuid"
        inserted_mock.site_update_id = SAMPLE_UPDATE_ID
        inserted_mock.matched_task_id = TASK_CS101_ID
        inserted_mock.progress_percent = 100.0
        inserted_mock.status = "completed"
        inserted_mock.delay_days = 0
        inserted_mock.delay_reason = None
        inserted_mock.actual_start_date = None
        inserted_mock.actual_end_date = "2026-09-21"
        inserted_mock.confidence_score = 90.0
        inserted_mock.model_name = "mock-keyword-v1"
        inserted_mock.model_response = "{}"
        inserted_mock.observation_index = 0
        inserted_mock.processed_at = "2026-09-21T10:00:00"
        inserted_mock.is_current = True

        def db_execute(stmt: Any, params: Any = None) -> Any:
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "INSERT INTO ai_processed_updates" in sql:
                res.fetchone.return_value = inserted_mock
            return res

        mock_db.execute.side_effect = db_execute

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=TASK_CS101_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date="2026-09-21",
            confidence_score=90.0,
            model_name="mock-keyword-v1",
            model_response={},
            additional_observations=[],  # No secondary observations
        )

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        self.assertIsInstance(res, ObservationList)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["observation_index"], 0)
        self.assertEqual(res["matched_task_id"], TASK_CS101_ID)

    def test_multi_observations_persisted_with_deterministic_indexes(self):
        """A multi-activity update produces primary (index 0) and secondaries (index 1, 2...)."""
        mock_db = MagicMock()
        update_row = _make_update_row(
            "Survey 100% complete. ROW clearing is 100% done. Trenching is 75% underway."
        )
        tasks = _make_candidate_tasks()

        inserted_rows = []

        def make_inserted_row(params):
            r = MagicMock()
            r.id = params["id"]
            r.site_update_id = params["site_update_id"]
            r.matched_task_id = params["matched_task_id"]
            r.progress_percent = params["progress_percent"]
            r.status = params["status"]
            r.delay_days = params["delay_days"]
            r.delay_reason = params["delay_reason"]
            r.actual_start_date = params["actual_start_date"]
            r.actual_end_date = params["actual_end_date"]
            r.confidence_score = params["confidence_score"]
            r.model_name = params["model_name"]
            r.model_response = params["model_response"]
            r.observation_index = params["observation_index"]
            r.processed_at = "2026-09-21T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "INSERT INTO ai_processed_updates" in sql:
                row = make_inserted_row(params)
                inserted_rows.append(row)
                res.fetchone.return_value = row
            return res

        mock_db.execute.side_effect = db_execute

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=TASK_CS101_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date="2026-09-21",
            confidence_score=95.0,
            model_name="mock-keyword-v1",
            model_response={},
            additional_observations=[
                ActivityObservation(
                    activity_description="ROW clearing is 100% done",
                    location_mentioned="Pipeline Section B",
                    progress_percent=100.0,
                    status="completed",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=92.0,
                    candidate_source_task_id="CS102",
                ),
                ActivityObservation(
                    activity_description="Trenching is 75% underway",
                    location_mentioned="Pipeline Section B",
                    progress_percent=75.0,
                    status="in_progress",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=88.0,
                    candidate_source_task_id="CS103",
                ),
            ],
        )

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # 3 observations persisted in order
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0]["observation_index"], 0)
        self.assertEqual(res[0]["matched_task_id"], TASK_CS101_ID)
        self.assertEqual(res[1]["observation_index"], 1)
        self.assertEqual(res[1]["matched_task_id"], TASK_CS102_ID)
        self.assertEqual(res[2]["observation_index"], 2)
        self.assertEqual(res[2]["matched_task_id"], TASK_CS103_ID)

    def test_single_bulk_expire_called_once_before_inserts(self):
        """The previous current observations batch is expired ONCE before any inserts."""
        mock_db = MagicMock()
        update_row = _make_update_row()
        tasks = _make_candidate_tasks()

        calls_sequence = []

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                calls_sequence.append("FETCH_UPDATE")
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                calls_sequence.append("FETCH_TASKS")
                res.fetchall.return_value = tasks
            elif "UPDATE ai_processed_updates" in sql and "is_current = false" in sql:
                calls_sequence.append("EXPIRE_CURRENT")
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                calls_sequence.append(f"INSERT_OBS_{p.get('observation_index')}")
                r = MagicMock()
                r.id = p.get("id")
                r.site_update_id = p.get("site_update_id")
                r.matched_task_id = p.get("matched_task_id")
                r.progress_percent = p.get("progress_percent")
                r.status = p.get("status")
                r.delay_days = None
                r.delay_reason = None
                r.actual_start_date = None
                r.actual_end_date = None
                r.confidence_score = p.get("confidence_score")
                r.model_name = "mock"
                r.model_response = "{}"
                r.observation_index = p.get("observation_index")
                r.processed_at = "2026-09-21T10:00:00"
                r.is_current = True
                res.fetchone.return_value = r
            return res

        mock_db.execute.side_effect = db_execute

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=TASK_CS101_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=90.0,
            model_name="mock",
            model_response={},
            additional_observations=[
                ActivityObservation(
                    activity_description="Secondary task",
                    location_mentioned="Pipeline Section B",
                    progress_percent=50.0,
                    status="in_progress",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=80.0,
                    candidate_source_task_id="CS102",
                )
            ],
        )

        process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # Check call order: EXPIRE_CURRENT must happen once, strictly before INSERT_OBS_0 and INSERT_OBS_1
        self.assertEqual(calls_sequence.count("EXPIRE_CURRENT"), 1)
        expire_idx = calls_sequence.index("EXPIRE_CURRENT")
        insert0_idx = calls_sequence.index("INSERT_OBS_0")
        insert1_idx = calls_sequence.index("INSERT_OBS_1")
        self.assertLess(expire_idx, insert0_idx)
        self.assertLess(insert0_idx, insert1_idx)

    def test_one_bad_observation_does_not_abort_siblings(self):
        """A failure/exception in secondary observation 1 does not abort observation 0 or observation 2."""
        mock_db = MagicMock()
        update_row = _make_update_row()
        tasks = _make_candidate_tasks()

        def make_row(params):
            r = MagicMock()
            r.id = params["id"]
            r.site_update_id = params["site_update_id"]
            r.matched_task_id = params["matched_task_id"]
            r.progress_percent = params["progress_percent"]
            r.status = params["status"]
            r.delay_days = None
            r.delay_reason = None
            r.actual_start_date = None
            r.actual_end_date = None
            r.confidence_score = params["confidence_score"]
            r.model_name = "mock"
            r.model_response = "{}"
            r.observation_index = params["observation_index"]
            r.processed_at = "2026-09-21T10:00:00"
            r.is_current = True
            return r

        inserted_indices = []

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "INSERT INTO ai_processed_updates" in sql:
                # Simulate a DB error specifically on observation 1
                p = params or {}
                if p.get("observation_index") == 1:
                    raise RuntimeError("Simulated transient error in observation 1")
                inserted_indices.append(p.get("observation_index"))
                res.fetchone.return_value = make_row(p)
            return res

        mock_db.execute.side_effect = db_execute

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=TASK_CS101_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=90.0,
            model_name="mock",
            model_response={},
            additional_observations=[
                ActivityObservation(
                    activity_description="Failing observation",
                    location_mentioned=None,
                    progress_percent=10.0,
                    status="in_progress",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=60.0,
                    candidate_source_task_id="CS102",
                ),
                ActivityObservation(
                    activity_description="Successful third observation",
                    location_mentioned="Pipeline Section B",
                    progress_percent=75.0,
                    status="in_progress",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=85.0,
                    candidate_source_task_id="CS103",
                ),
            ],
        )

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # Observation 0 (primary) and observation 2 (secondary) succeeded; observation 1 was safely skipped
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["observation_index"], 0)
        self.assertEqual(res[1]["observation_index"], 2)
        self.assertEqual(inserted_indices, [0, 2])

    def test_multiple_valid_observations_independently_update_schedule_actuals(self):
        """High-confidence primary and secondary observations independently update schedule_tasks."""
        mock_db = MagicMock()
        update_row = _make_update_row()
        tasks = _make_candidate_tasks()

        actuals_updated_tasks = []

        def make_row(params):
            r = MagicMock()
            r.id = params["id"]
            r.site_update_id = params["site_update_id"]
            r.matched_task_id = params["matched_task_id"]
            r.progress_percent = params["progress_percent"]
            r.status = params["status"]
            r.delay_days = params["delay_days"]
            r.delay_reason = params["delay_reason"]
            r.actual_start_date = None
            r.actual_end_date = None
            r.confidence_score = params["confidence_score"]
            r.model_name = "mock"
            r.model_response = "{}"
            r.observation_index = params["observation_index"]
            r.processed_at = "2026-09-21T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                res.fetchone.return_value = make_row(p)
            return res

        mock_db.execute.side_effect = db_execute

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=TASK_CS101_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date="2026-09-21",
            confidence_score=92.0,  # >= 80 -> auto-link
            model_name="mock",
            model_response={},
            additional_observations=[
                ActivityObservation(
                    activity_description="Right of way clearing completed",
                    location_mentioned="Pipeline Section B",
                    progress_percent=100.0,
                    status="completed",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=90.0,
                    candidate_source_task_id="CS102",
                ),
            ],
        )

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        self.assertEqual(len(res), 2)
        # Both tasks received actuals updates
        self.assertIn(TASK_CS101_ID, actuals_updated_tasks)
        self.assertIn(TASK_CS102_ID, actuals_updated_tasks)
        self.assertEqual(len(actuals_updated_tasks), 2)

    def test_unmatched_secondary_observation_does_not_modify_schedule(self):
        """A secondary observation with no matched task does not mutate schedule_tasks."""
        mock_db = MagicMock()
        update_row = _make_update_row()
        tasks = _make_candidate_tasks()

        actuals_updated_tasks = []

        def make_row(params):
            r = MagicMock()
            r.id = params["id"]
            r.site_update_id = params["site_update_id"]
            r.matched_task_id = params["matched_task_id"]
            r.progress_percent = params["progress_percent"]
            r.status = params["status"]
            r.delay_days = params["delay_days"]
            r.delay_reason = params["delay_reason"]
            r.actual_start_date = None
            r.actual_end_date = None
            r.confidence_score = params["confidence_score"]
            r.model_name = "mock"
            r.model_response = "{}"
            r.observation_index = params["observation_index"]
            r.processed_at = "2026-09-21T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                res.fetchone.return_value = make_row(p)
            return res

        mock_db.execute.side_effect = db_execute

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=TASK_CS101_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date="2026-09-21",
            confidence_score=92.0,  # >= 80 -> auto-link
            model_name="mock",
            model_response={},
            additional_observations=[
                ActivityObservation(
                    activity_description="Canteen construction started",
                    location_mentioned="Base Camp",
                    progress_percent=10.0,
                    status="in_progress",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=30.0,  # < 50 -> low confidence / unmatched
                    candidate_source_task_id=None,
                ),
            ],
        )

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        self.assertEqual(len(res), 2)
        # Secondary observation has no matched_task_id
        self.assertIsNone(res[1]["matched_task_id"])
        # Only CS101 was updated; no second task was touched
        self.assertEqual(actuals_updated_tasks, [TASK_CS101_ID])

    def test_get_current_result_returns_ordered_observations(self):
        """get_current_result returns all observations ordered by observation_index ascending."""
        mock_db = MagicMock()

        row0 = MagicMock()
        row0.id = "p-0"
        row0.site_update_id = SAMPLE_UPDATE_ID
        row0.matched_task_id = TASK_CS101_ID
        row0.progress_percent = 100.0
        row0.status = "completed"
        row0.delay_days = None
        row0.delay_reason = None
        row0.actual_start_date = None
        row0.actual_end_date = "2026-09-21"
        row0.confidence_score = 95.0
        row0.model_name = "mock"
        row0.model_response = "{}"
        row0.observation_index = 0
        row0.processed_at = "2026-09-21T10:00:00"
        row0.is_current = True

        row1 = MagicMock()
        row1.id = "p-1"
        row1.site_update_id = SAMPLE_UPDATE_ID
        row1.matched_task_id = TASK_CS102_ID
        row1.progress_percent = 50.0
        row1.status = "in_progress"
        row1.delay_days = None
        row1.delay_reason = None
        row1.actual_start_date = None
        row1.actual_end_date = None
        row1.confidence_score = 85.0
        row1.model_name = "mock"
        row1.model_response = "{}"
        row1.observation_index = 1
        row1.processed_at = "2026-09-21T10:00:00"
        row1.is_current = True

        mock_db.execute.return_value.fetchall.return_value = [row0, row1]

        res = get_current_result(mock_db, SAMPLE_UPDATE_ID)

        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["observation_index"], 0)
        self.assertEqual(res[1]["observation_index"], 1)
        self.assertEqual(res[0]["matched_task_id"], TASK_CS101_ID)
        self.assertEqual(res[1]["matched_task_id"], TASK_CS102_ID)
        # Check dict access delegates to primary
        self.assertEqual(res["matched_task_id"], TASK_CS101_ID)

    def test_reprocessing_idempotency_simulation(self):
        """Re-processing an update sets previous batch to is_current=False and creates fresh is_current=True batch."""
        # Simulated database store
        stored_rows: list[dict[str, Any]] = []

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = _make_update_row()
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = _make_candidate_tasks()
            elif "UPDATE ai_processed_updates" in sql and "is_current = false" in sql:
                p = params or {}
                for row in stored_rows:
                    if row["site_update_id"] == p.get("site_update_id"):
                        row["is_current"] = False
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                row_data: dict[str, Any] = dict(p)
                row_data["is_current"] = True
                stored_rows.append(row_data)
                mock_row = MagicMock()
                for k, v in row_data.items():
                    setattr(mock_row, str(k), v)
                mock_row.processed_at = "2026-09-21T10:00:00"
                res.fetchone.return_value = mock_row
            return res

        mock_db = MagicMock()
        mock_db.execute.side_effect = db_execute

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=TASK_CS101_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=90.0,
            model_name="mock",
            model_response={},
            additional_observations=[
                ActivityObservation(
                    activity_description="Secondary task",
                    location_mentioned="Pipeline Section B",
                    progress_percent=50.0,
                    status="in_progress",
                    delay_days=None,
                    delay_reason=None,
                    extraction_confidence=85.0,
                    candidate_source_task_id="CS102",
                )
            ],
        )

        # First run: inserts batch 1 (2 observations: index 0 and 1)
        res1 = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)
        self.assertEqual(len(res1), 2)
        current_after_run1 = [r for r in stored_rows if r["is_current"]]
        self.assertEqual(len(current_after_run1), 2)
        self.assertEqual([r["observation_index"] for r in current_after_run1], [0, 1])

        # Second run (re-processing): expires batch 1 and inserts fresh batch 2
        res2 = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)
        self.assertEqual(len(res2), 2)

        # Total rows in DB is 4 (2 old + 2 new)
        self.assertEqual(len(stored_rows), 4)

        # Exactly 2 rows remain current (the fresh batch)
        current_after_run2 = [r for r in stored_rows if r["is_current"]]
        self.assertEqual(len(current_after_run2), 2)
        self.assertEqual([r["observation_index"] for r in current_after_run2], [0, 1])

        # Historical rows remain with is_current=False
        historical_rows = [r for r in stored_rows if not r["is_current"]]
        self.assertEqual(len(historical_rows), 2)
        self.assertEqual([r["observation_index"] for r in historical_rows], [0, 1])

    def test_mock_provider_detects_additional_activities_typed(self):
        """MockAIProvider produces typed ActivityObservation objects in AnalysisResult.additional_observations."""
        provider = MockAIProvider()
        candidate_tasks = [
            {
                "id": "t1",
                "source_task_id": "CS101",
                "activity": "Route survey and marking",
                "location": "Pipeline Section B",
            },
            {
                "id": "t2",
                "source_task_id": "CS102",
                "activity": "Right of way clearing",
                "location": "Pipeline Section B",
            },
            {
                "id": "t3",
                "source_task_id": "CS103",
                "activity": "Trench excavation",
                "location": "Pipeline Section B",
            },
        ]
        raw_text = "Route survey complete and right of way clearing is 50% done at Pipeline Section B."

        result = provider.analyse(
            raw_update=raw_text,
            location="Pipeline Section B",
            reported_on="2026-09-21",
            candidate_tasks=candidate_tasks,
        )

        self.assertIsInstance(result, AnalysisResult)
        self.assertIsInstance(result.additional_observations, list)
        self.assertGreaterEqual(len(result.additional_observations), 1)

        obs = result.additional_observations[0]
        self.assertIsInstance(obs, ActivityObservation)
        self.assertEqual(obs.candidate_source_task_id, "CS101")
        self.assertEqual(result.matched_task_id, "t2")

    def test_observation_index_deterministic_mock(self):
        """Repeated analysis on the same multi-activity text produces identical deterministic observations."""
        provider = MockAIProvider()
        candidate_tasks = [
            {
                "id": "t1",
                "source_task_id": "CS101",
                "activity": "Route survey and marking",
                "location": "Pipeline Section B",
            },
            {
                "id": "t2",
                "source_task_id": "CS102",
                "activity": "Right of way clearing",
                "location": "Pipeline Section B",
            },
            {
                "id": "t3",
                "source_task_id": "CS103",
                "activity": "Trench excavation",
                "location": "Pipeline Section B",
            },
        ]
        raw_text = "Route survey complete and right of way clearing is 50% done at Pipeline Section B."

        res1 = provider.analyse(
            raw_update=raw_text,
            location="Pipeline Section B",
            reported_on="2026-09-21",
            candidate_tasks=candidate_tasks,
        )
        res2 = provider.analyse(
            raw_update=raw_text,
            location="Pipeline Section B",
            reported_on="2026-09-21",
            candidate_tasks=candidate_tasks,
        )

        self.assertEqual(res1.matched_task_id, res2.matched_task_id)
        self.assertEqual(len(res1.additional_observations), len(res2.additional_observations))
        for o1, o2 in zip(res1.additional_observations, res2.additional_observations):
            self.assertEqual(o1.candidate_source_task_id, o2.candidate_source_task_id)
            self.assertEqual(o1.progress_percent, o2.progress_percent)
            self.assertEqual(o1.extraction_confidence, o2.extraction_confidence)

    def test_production_observed_report_extraction(self):
        """Production regression test: exact problem report extracts MC104, MC105, and MC106 independently."""
        provider = MockAIProvider()
        tasks = [
            {"id": "t101", "source_task_id": "MC101", "activity": "Site Survey and Setting Out", "location": "Compressor Station C"},
            {"id": "t102", "source_task_id": "MC102", "activity": "Site Clearing and Grading", "location": "Compressor Station C"},
            {"id": "t103", "source_task_id": "MC103", "activity": "Foundation Excavation", "location": "Compressor Station C"},
            {"id": "t104", "source_task_id": "MC104", "activity": "Foundation Reinforcement", "location": "Compressor Station C"},
            {"id": "t105", "source_task_id": "MC105", "activity": "Equipment Foundation Concrete", "location": "Compressor Station C"},
            {"id": "t106", "source_task_id": "MC106", "activity": "Equipment Base Plate Installation", "location": "Compressor Station C"},
            {"id": "t107", "source_task_id": "MC107", "activity": "Equipment Installation", "location": "Compressor Station C"},
            {"id": "t108", "source_task_id": "MC108", "activity": "Equipment Alignment and Inspection", "location": "Compressor Station C"},
            {"id": "t109", "source_task_id": "MC109", "activity": "Electrical Cable Installation", "location": "Compressor Station C"},
            {"id": "t110", "source_task_id": "MC110", "activity": "Electrical Testing", "location": "Compressor Station C"},
            {"id": "t111", "source_task_id": "MC111", "activity": "Final Commissioning", "location": "Compressor Station C"},
        ]
        raw_text = (
            "Daily construction update: Foundation reinforcement has been "
            "completed at 100%. Equipment foundation concrete has reached 60% "
            "completion and is progressing normally. Equipment base plate "
            "installation has not started yet."
        )

        result = provider.analyse(
            raw_update=raw_text,
            location="Compressor Station C",
            reported_on="2026-09-26",
            candidate_tasks=tasks,
        )

        # Observation 0 (Foundation Reinforcement -> MC104)
        self.assertEqual(result.matched_task_id, "t104")
        self.assertEqual(result.model_response.get("matched_source_task_id"), "MC104")
        self.assertEqual(result.progress_percent, 100.0)
        self.assertEqual(result.status, "completed")
        self.assertGreaterEqual(result.confidence_score, 80.0)
        self.assertFalse(result.model_response.get("is_ambiguous", False))

        # Two secondary observations extracted
        self.assertEqual(len(result.additional_observations), 2)

        # Observation 1 (Equipment Foundation Concrete -> MC105)
        obs1 = result.additional_observations[0]
        self.assertEqual(obs1.candidate_source_task_id, "MC105")
        self.assertEqual(obs1.progress_percent, 60.0)
        self.assertEqual(obs1.status, "in_progress")
        self.assertGreaterEqual(obs1.extraction_confidence, 80.0)
        self.assertFalse(obs1.is_ambiguous)

        # Observation 2 (Equipment Base Plate Installation -> MC106)
        obs2 = result.additional_observations[1]
        self.assertEqual(obs2.candidate_source_task_id, "MC106")
        self.assertIsNone(obs2.progress_percent)
        self.assertEqual(obs2.status, "not_started")
        self.assertGreaterEqual(obs2.extraction_confidence, 80.0)
        self.assertFalse(obs2.is_ambiguous)

    def test_scenario_a_three_high_confidence_observations_all_auto_process(self):
        """Scenario A: 3 high-confidence observations all auto-process and update schedule actuals."""
        mock_db = MagicMock()
        update_row = _make_update_row(
            raw_update=(
                "Daily construction update: Foundation reinforcement has been "
                "completed at 100%. Equipment foundation concrete has reached 60% "
                "completion and is progressing normally. Equipment base plate "
                "installation has not started yet."
            ),
            location="Compressor Station C",
        )
        tasks = [
            MagicMock(id="uuid-104", source_task_id="MC104", activity="Foundation Reinforcement", location="Compressor Station C", planned_start="2026-09-11", planned_end="2026-09-20", status="not_started"),
            MagicMock(id="uuid-105", source_task_id="MC105", activity="Equipment Foundation Concrete", location="Compressor Station C", planned_start="2026-09-21", planned_end="2026-09-30", status="not_started"),
            MagicMock(id="uuid-106", source_task_id="MC106", activity="Equipment Base Plate Installation", location="Compressor Station C", planned_start="2026-10-01", planned_end="2026-10-10", status="not_started"),
        ]

        actuals_updated_tasks = []
        inserted_rows = []

        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-09-26T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                row = make_row(p)
                inserted_rows.append(row)
                res.fetchone.return_value = row
            return res

        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        # All 3 observations inserted
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0]["matched_task_id"], "uuid-104")
        self.assertEqual(res[1]["matched_task_id"], "uuid-105")
        self.assertEqual(res[2]["matched_task_id"], "uuid-106")

        # All 3 observations have confidence >= 80.0
        self.assertGreaterEqual(res[0]["confidence_score"], 80.0)
        self.assertGreaterEqual(res[1]["confidence_score"], 80.0)
        self.assertGreaterEqual(res[2]["confidence_score"], 80.0)

        # All 3 tasks independently updated their actuals in schedule_tasks
        self.assertIn("uuid-104", actuals_updated_tasks)
        self.assertIn("uuid-105", actuals_updated_tasks)
        self.assertIn("uuid-106", actuals_updated_tasks)
        self.assertEqual(len(actuals_updated_tasks), 3)

        # Requirements B & D: Each observation retains its own independent confidence and model/evidence data
        for i in range(3):
            mr = res[i]["model_response"]
            self.assertIsNotNone(mr)
            self.assertIsInstance(mr, dict)
            self.assertIn("composite_confidence", mr)
            self.assertIn("extraction_confidence", mr)
            self.assertIn("match_confidence", mr)
            self.assertIn("reasoning", mr)
            self.assertIsNotNone(mr["reasoning"])

    def test_production_observed_report_all_three_auto_link_with_location_variation(self):
        """Production regression test: exact production input with Compressor Station D location

        Tests Requirements A-D:
        A) exact 3-activity input
        B) each observation gets independent confidence
        C) clear observations auto-link when actual signals are strong
        D) secondary observations retain model/evidence data
        """
        mock_db = MagicMock()
        update_row = _make_update_row(
            raw_update=(
                "Daily construction update: Foundation reinforcement has been "
                "completed at 100%. Equipment foundation concrete has reached 60% "
                "completion and is progressing normally. Equipment base plate "
                "installation has not started yet."
            ),
            location="Compressor Station D",
        )
        tasks = [
            MagicMock(id="uuid-104", source_task_id="MC104", activity="Foundation Reinforcement", location="Compressor Station C", planned_start="2026-09-11", planned_end="2026-09-20", status="not_started"),
            MagicMock(id="uuid-105", source_task_id="MC105", activity="Equipment Foundation Concrete", location="Compressor Station C", planned_start="2026-09-21", planned_end="2026-09-30", status="not_started"),
            MagicMock(id="uuid-106", source_task_id="MC106", activity="Equipment Base Plate Installation", location="Compressor Station C", planned_start="2026-10-01", planned_end="2026-10-10", status="not_started"),
        ]

        actuals_updated_tasks = []
        inserted_rows = []

        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-09-26T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                row = make_row(p)
                inserted_rows.append(row)
                res.fetchone.return_value = row
            return res

        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        # All 3 observations inserted and auto-linked
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0]["matched_task_id"], "uuid-104")
        self.assertEqual(res[1]["matched_task_id"], "uuid-105")
        self.assertEqual(res[2]["matched_task_id"], "uuid-106")

        # Independent extraction and status
        self.assertEqual(res[0]["progress_percent"], 100.0)
        self.assertEqual(res[0]["status"], "completed")

        self.assertEqual(res[1]["progress_percent"], 60.0)
        self.assertEqual(res[1]["status"], "in_progress")

        self.assertIsNone(res[2]["progress_percent"])
        self.assertEqual(res[2]["status"], "not_started")

        # All 3 observations score >= 80.0 even with location variation
        self.assertGreaterEqual(res[0]["confidence_score"], 80.0)
        self.assertGreaterEqual(res[1]["confidence_score"], 80.0)
        self.assertGreaterEqual(res[2]["confidence_score"], 80.0)

        # All 3 tasks independently updated actuals in schedule_tasks
        self.assertIn("uuid-104", actuals_updated_tasks)
        self.assertIn("uuid-105", actuals_updated_tasks)
        self.assertIn("uuid-106", actuals_updated_tasks)
        self.assertEqual(len(actuals_updated_tasks), 3)

        # Secondary observations retain model/evidence data
        for i in range(3):
            mr = res[i]["model_response"]
            self.assertIsNotNone(mr, f"model_response is None for observation {i}")
            self.assertIsInstance(mr, dict)
            self.assertIn("composite_confidence", mr)
            self.assertIn("extraction_confidence", mr)
            self.assertIn("match_confidence", mr)
            self.assertIn("reasoning", mr)
            self.assertIsNotNone(mr["reasoning"])
        # Secondary observations specifically retain raw_json
        for i in (1, 2):
            self.assertIn("raw_json", res[i]["model_response"])

    def test_scenario_b_two_high_confidence_one_ambiguous(self):
        """Scenario B: 2 high-confidence auto-process, only the ambiguous one enters review."""
        mock_db = MagicMock()
        update_row = _make_update_row(
            raw_update=(
                "Foundation reinforcement has been completed at 100%. "
                "Equipment foundation concrete has reached 60% completion. "
                "Electrical work has reached 30% progress."
            ),
            location="Compressor Station C",
        )
        tasks = [
            MagicMock(id="uuid-104", source_task_id="MC104", activity="Foundation Reinforcement", location="Compressor Station C", planned_start="2026-09-11", planned_end="2026-09-20", status="not_started"),
            MagicMock(id="uuid-105", source_task_id="MC105", activity="Equipment Foundation Concrete", location="Compressor Station C", planned_start="2026-09-21", planned_end="2026-09-30", status="not_started"),
            MagicMock(id="uuid-109", source_task_id="MC109", activity="Electrical Cable Installation", location="Compressor Station C", planned_start="2026-10-21", planned_end="2026-10-31", status="not_started"),
            MagicMock(id="uuid-110", source_task_id="MC110", activity="Electrical Testing", location="Compressor Station C", planned_start="2026-11-01", planned_end="2026-11-10", status="not_started"),
        ]

        actuals_updated_tasks = []
        inserted_rows = []

        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-09-26T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                row = make_row(p)
                inserted_rows.append(row)
                res.fetchone.return_value = row
            return res

        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        self.assertEqual(len(res), 3)

        # Observations 0 and 1 are high confidence and auto-processed
        self.assertGreaterEqual(res[0]["confidence_score"], 80.0)
        self.assertGreaterEqual(res[1]["confidence_score"], 80.0)
        self.assertIn("uuid-104", actuals_updated_tasks)
        self.assertIn("uuid-105", actuals_updated_tasks)

        # Observation 2 is medium confidence (< 80.0) and routes to review (not auto-linked)
        self.assertLess(res[2]["confidence_score"], 80.0)
        self.assertNotIn("uuid-109", actuals_updated_tasks)
        self.assertNotIn("uuid-110", actuals_updated_tasks)
        self.assertEqual(len(actuals_updated_tasks), 2)

    def test_scenario_c_one_unmatched_observation_does_not_mutate_schedule(self):
        """Scenario C: Unmatched observation does not mutate any schedule task."""
        mock_db = MagicMock()
        update_row = _make_update_row(
            raw_update="Foundation reinforcement has been completed at 100%. Catering tent setup completed at dining hall.",
            location="Compressor Station C",
        )
        tasks = [
            MagicMock(id="uuid-104", source_task_id="MC104", activity="Foundation Reinforcement", location="Compressor Station C", planned_start="2026-09-11", planned_end="2026-09-20", status="not_started"),
            MagicMock(id="uuid-105", source_task_id="MC105", activity="Equipment Foundation Concrete", location="Compressor Station C", planned_start="2026-09-21", planned_end="2026-09-30", status="not_started"),
        ]

        actuals_updated_tasks = []

        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-09-26T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                res.fetchone.return_value = make_row(p)
            return res

        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        self.assertGreaterEqual(len(res), 1)
        # MC104 auto-linked
        self.assertIn("uuid-104", actuals_updated_tasks)
        # No dining/catering task mutated (only uuid-104 touched)
        self.assertEqual(actuals_updated_tasks, ["uuid-104"])

    def test_scenario_d_repeated_processing_no_duplicate_current_observations(self):
        """Scenario D: Repeated processing leaves exactly one set of current observations (no duplicates)."""
        stored_rows: list[dict[str, Any]] = []

        update_row = _make_update_row(
            raw_update=(
                "Daily construction update: Foundation reinforcement has been "
                "completed at 100%. Equipment foundation concrete has reached 60% "
                "completion and is progressing normally. Equipment base plate "
                "installation has not started yet."
            ),
            location="Compressor Station C",
        )
        tasks = [
            MagicMock(id="uuid-104", source_task_id="MC104", activity="Foundation Reinforcement", location="Compressor Station C", planned_start="2026-09-11", planned_end="2026-09-20", status="not_started"),
            MagicMock(id="uuid-105", source_task_id="MC105", activity="Equipment Foundation Concrete", location="Compressor Station C", planned_start="2026-09-21", planned_end="2026-09-30", status="not_started"),
            MagicMock(id="uuid-106", source_task_id="MC106", activity="Equipment Base Plate Installation", location="Compressor Station C", planned_start="2026-10-01", planned_end="2026-10-10", status="not_started"),
        ]

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE ai_processed_updates" in sql and "is_current = false" in sql:
                p = params or {}
                for row in stored_rows:
                    if row["site_update_id"] == p.get("site_update_id"):
                        row["is_current"] = False
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                row_data: dict[str, Any] = dict(p)
                row_data["is_current"] = True
                stored_rows.append(row_data)
                mock_row = MagicMock()
                for k, v in row_data.items():
                    setattr(mock_row, str(k), v)
                mock_row.processed_at = "2026-09-26T10:00:00"
                res.fetchone.return_value = mock_row
            return res

        mock_db = MagicMock()
        mock_db.execute.side_effect = db_execute

        # Run 1
        res1 = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())
        self.assertEqual(len(res1), 3)
        current_run1 = [r for r in stored_rows if r["is_current"]]
        self.assertEqual(len(current_run1), 3)

        # Run 2 (re-processing)
        res2 = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())
        self.assertEqual(len(res2), 3)

        # Total 6 rows in DB (3 historical + 3 current)
        self.assertEqual(len(stored_rows), 6)

        # Exactly 3 current rows
        current_run2 = [r for r in stored_rows if r["is_current"]]
        self.assertEqual(len(current_run2), 3)
        self.assertEqual([r["observation_index"] for r in current_run2], [0, 1, 2])

        # Exactly 3 historical rows
        historical = [r for r in stored_rows if not r["is_current"]]
        self.assertEqual(len(historical), 3)
        self.assertEqual([r["observation_index"] for r in historical], [0, 1, 2])

    def test_generalization_pipeline_section_x_all_seven_activities_auto_linked(self):
        """Test A: Pipeline Section X report with 7 activities all auto-link (>80%) independently.

        Verifies:
        1. Route survey and marking -> 100% completed on 2026-11-07, matches PX101
        2. Right of way clearing -> 100% completed on 2026-11-11, matches PX102
        3. Trench excavation -> 100% completed on 2026-11-17, matches PX103
        4. Sand bedding -> 80% in_progress, matches PX104
        5. Pipe stringing -> 100% completed, matches PX105
        6. Pipeline welding -> 35% in_progress, delay_days=1, delay_reason='heavy rainfall', matches PX106
        7. Weld inspection and NDT -> not_started, progress is None, matches PX107
        All 7 have composite confidence >= 80.0 and update schedule_tasks actuals.
        """
        raw_update = (
            "Daily pipeline construction report: Route survey and marking was "
            "completed on 7 November. Right of way clearing was completed on "
            "11 November. Trench excavation reached 100% completion on 17 November. "
            "Sand bedding is currently 80% complete and progressing normally. "
            "Pipe stringing has been completed. Pipeline welding has started and "
            "is approximately 35% complete. Welding activities were delayed by "
            "heavy rainfall for one working day. Weld inspection and NDT has not "
            "started yet because welding is still in progress."
        )
        update_row = _make_update_row(raw_update=raw_update, location="Pipeline Section X", reported_on=datetime.date(2026, 11, 18))
        tasks = [
            MagicMock(id="u-px101", source_task_id="PX101", activity="Route Survey and Marking", location="Pipeline Section X", planned_start="2026-11-01", planned_end="2026-11-10", status="not_started"),
            MagicMock(id="u-px102", source_task_id="PX102", activity="Right of Way Clearing", location="Pipeline Section X", planned_start="2026-11-05", planned_end="2026-11-15", status="not_started"),
            MagicMock(id="u-px103", source_task_id="PX103", activity="Trench Excavation", location="Pipeline Section X", planned_start="2026-11-10", planned_end="2026-11-20", status="not_started"),
            MagicMock(id="u-px104", source_task_id="PX104", activity="Sand Bedding", location="Pipeline Section X", planned_start="2026-11-15", planned_end="2026-11-25", status="not_started"),
            MagicMock(id="u-px105", source_task_id="PX105", activity="Pipe Stringing", location="Pipeline Section X", planned_start="2026-11-20", planned_end="2026-11-30", status="not_started"),
            MagicMock(id="u-px106", source_task_id="PX106", activity="Pipeline Welding", location="Pipeline Section X", planned_start="2026-11-25", planned_end="2026-12-05", status="not_started"),
            MagicMock(id="u-px107", source_task_id="PX107", activity="Weld Inspection and NDT", location="Pipeline Section X", planned_start="2026-12-01", planned_end="2026-12-10", status="not_started"),
        ]

        actuals_updated_tasks: list[str] = []
        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-11-18T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                res.fetchone.return_value = make_row(p)
            return res

        mock_db = MagicMock()
        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        self.assertEqual(len(res), 7)
        # Verify all 7 observations received indexes 0..6
        self.assertEqual([obs["observation_index"] for obs in res], list(range(7)))

        # Verify all 7 observations have confidence >= 80.0
        for i, obs in enumerate(res):
            self.assertGreaterEqual(
                obs["confidence_score"],
                80.0,
                f"Observation {i} ({obs.get('activity_description')}) did not reach 80% threshold",
            )

        # 1. Route Survey and Marking -> PX101
        self.assertEqual(res[0]["matched_task_id"], "u-px101")
        self.assertEqual(res[0]["progress_percent"], 100.0)
        self.assertEqual(res[0]["status"], "completed")
        self.assertEqual(res[0]["actual_end_date"], "2026-11-07")

        # 2. Right of Way Clearing -> PX102
        self.assertEqual(res[1]["matched_task_id"], "u-px102")
        self.assertEqual(res[1]["progress_percent"], 100.0)
        self.assertEqual(res[1]["status"], "completed")
        self.assertEqual(res[1]["actual_end_date"], "2026-11-11")

        # 3. Trench Excavation -> PX103
        self.assertEqual(res[2]["matched_task_id"], "u-px103")
        self.assertEqual(res[2]["progress_percent"], 100.0)
        self.assertEqual(res[2]["status"], "completed")
        self.assertEqual(res[2]["actual_end_date"], "2026-11-17")

        # 4. Sand Bedding -> PX104
        self.assertEqual(res[3]["matched_task_id"], "u-px104")
        self.assertEqual(res[3]["progress_percent"], 80.0)
        self.assertEqual(res[3]["status"], "in_progress")

        # 5. Pipe Stringing -> PX105
        self.assertEqual(res[4]["matched_task_id"], "u-px105")
        self.assertEqual(res[4]["progress_percent"], 100.0)
        self.assertEqual(res[4]["status"], "completed")

        # 6. Pipeline Welding -> PX106
        self.assertEqual(res[5]["matched_task_id"], "u-px106")
        self.assertEqual(res[5]["progress_percent"], 35.0)
        self.assertEqual(res[5]["status"], "in_progress")
        self.assertEqual(res[5]["delay_days"], 1)
        self.assertIsNotNone(res[5]["delay_reason"])
        self.assertIn("heavy rainfall", res[5]["delay_reason"].lower())

        # 7. Weld Inspection and NDT -> PX107
        self.assertEqual(res[6]["matched_task_id"], "u-px107")
        self.assertIsNone(res[6]["progress_percent"])
        self.assertEqual(res[6]["status"], "not_started")

        # Verify all 7 tasks had actuals updated in schedule
        self.assertEqual(
            actuals_updated_tasks,
            ["u-px101", "u-px102", "u-px103", "u-px104", "u-px105", "u-px106", "u-px107"],
        )

    def test_generalization_pipeline_section_x_alternative_wording_report_b(self):
        """Test B: Same 7 activities with alternative phrasing all extract and match correctly."""
        raw_update_b = (
            "Daily field summary: Route survey and line marking was finished on 7 Nov. "
            "ROW clearing was completed on 11 Nov. "
            "Trenching reached 100% completion on 17 Nov. "
            "Sand bedding is 80% complete and continuing. "
            "Pipe stringing work is done. "
            "Pipeline welding has commenced and is around 35% complete with 1 day delay due to heavy rainfall. "
            "Inspection and NDT has not started yet."
        )
        tasks = [
            {"id": "u-px101", "source_task_id": "PX101", "activity": "Route Survey and Marking", "location": "Pipeline Section X"},
            {"id": "u-px102", "source_task_id": "PX102", "activity": "Right of Way Clearing", "location": "Pipeline Section X"},
            {"id": "u-px103", "source_task_id": "PX103", "activity": "Trench Excavation", "location": "Pipeline Section X"},
            {"id": "u-px104", "source_task_id": "PX104", "activity": "Sand Bedding", "location": "Pipeline Section X"},
            {"id": "u-px105", "source_task_id": "PX105", "activity": "Pipe Stringing", "location": "Pipeline Section X"},
            {"id": "u-px106", "source_task_id": "PX106", "activity": "Pipeline Welding", "location": "Pipeline Section X"},
            {"id": "u-px107", "source_task_id": "PX107", "activity": "Weld Inspection and NDT", "location": "Pipeline Section X"},
        ]

        provider = MockAIProvider()
        result = provider.analyse(
            raw_update=raw_update_b,
            location="Pipeline Section X",
            reported_on="2026-11-18",
            candidate_tasks=tasks,
        )

        all_obs = [result] + list(result.additional_observations)
        self.assertEqual(len(all_obs), 7)

        # Verify task mappings
        matched_source_ids = [
            result.matched_task_id,  # "u-px101"
            *[obs.candidate_source_task_id for obs in result.additional_observations],
        ]
        self.assertEqual(matched_source_ids, ["u-px101", "PX102", "PX103", "PX104", "PX105", "PX106", "PX107"])

        # Check extracted dates on alternative phrasings
        self.assertEqual(result.actual_end_date, "2026-11-07")
        self.assertEqual(result.additional_observations[0].actual_end_date, "2026-11-11")
        self.assertEqual(result.additional_observations[1].actual_end_date, "2026-11-17")

    def test_generalization_mixed_confidence_one_ambiguous_does_not_block_high_confidence(self):
        """Test C: 1 ambiguous or low-confidence observation does not block 2 high-confidence observations."""
        raw_update = (
            "Route survey and marking was completed on 7 November. "
            "Right of way clearing was completed on 11 November. "
            "General construction work is proceeding."
        )
        update_row = _make_update_row(raw_update=raw_update, location="Pipeline Section X", reported_on=datetime.date(2026, 11, 18))
        tasks = [
            MagicMock(id="u-px101", source_task_id="PX101", activity="Route Survey and Marking", location="Pipeline Section X", planned_start="2026-11-01", planned_end="2026-11-10", status="not_started"),
            MagicMock(id="u-px102", source_task_id="PX102", activity="Right of Way Clearing", location="Pipeline Section X", planned_start="2026-11-05", planned_end="2026-11-15", status="not_started"),
            MagicMock(id="u-px103", source_task_id="PX103", activity="Trench Excavation", location="Pipeline Section X", planned_start="2026-11-10", planned_end="2026-11-20", status="not_started"),
        ]

        actuals_updated_tasks: list[str] = []
        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-11-18T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated_tasks.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                res.fetchone.return_value = make_row(p)
            return res

        mock_db = MagicMock()
        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        self.assertEqual(len(res), 3)
        # High confidence observations auto-link
        self.assertGreaterEqual(res[0]["confidence_score"], 80.0)
        self.assertGreaterEqual(res[1]["confidence_score"], 80.0)
        self.assertEqual(res[0]["matched_task_id"], "u-px101")
        self.assertEqual(res[1]["matched_task_id"], "u-px102")

        # Ambiguous / unlinked observation routes to review
        self.assertLess(res[2]["confidence_score"], 80.0)
        self.assertIsNone(res[2]["matched_task_id"])

        # High confidence observations updated actuals; ambiguous one did NOT
        self.assertEqual(actuals_updated_tasks, ["u-px101", "u-px102"])

    def test_generalization_all_high_confidence_observations_auto_process(self):
        """Test D: All high-confidence observations auto-process and update schedule actuals."""
        raw_update = (
            "Route survey and marking completed 100%. "
            "Right of way clearing completed 100%. "
            "Trench excavation reached 100% completion."
        )
        update_row = _make_update_row(raw_update=raw_update, location="Pipeline Section X", reported_on=datetime.date(2026, 11, 18))
        tasks = [
            MagicMock(id="u-px101", source_task_id="PX101", activity="Route Survey and Marking", location="Pipeline Section X", planned_start="2026-11-01", planned_end="2026-11-10", status="not_started"),
            MagicMock(id="u-px102", source_task_id="PX102", activity="Right of Way Clearing", location="Pipeline Section X", planned_start="2026-11-05", planned_end="2026-11-15", status="not_started"),
            MagicMock(id="u-px103", source_task_id="PX103", activity="Trench Excavation", location="Pipeline Section X", planned_start="2026-11-10", planned_end="2026-11-20", status="not_started"),
        ]

        actuals_updated: list[str] = []
        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-11-18T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                res.fetchone.return_value = make_row(p)
            return res

        mock_db = MagicMock()
        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        self.assertEqual(len(res), 3)
        for obs in res:
            self.assertGreaterEqual(obs["confidence_score"], 80.0)
        self.assertEqual(actuals_updated, ["u-px101", "u-px102", "u-px103"])

    def test_generalization_unmatched_observation_does_not_mutate_schedule(self):
        """Test E: An unmatched observation never mutates any schedule tasks."""
        raw_update = (
            "Route survey and marking completed 100%. "
            "Catering tent setup completed at dining hall."
        )
        update_row = _make_update_row(raw_update=raw_update, location="Pipeline Section X", reported_on=datetime.date(2026, 11, 18))
        tasks = [
            MagicMock(id="u-px101", source_task_id="PX101", activity="Route Survey and Marking", location="Pipeline Section X", planned_start="2026-11-01", planned_end="2026-11-10", status="not_started"),
            MagicMock(id="u-px102", source_task_id="PX102", activity="Right of Way Clearing", location="Pipeline Section X", planned_start="2026-11-05", planned_end="2026-11-15", status="not_started"),
        ]

        actuals_updated: list[str] = []
        def make_row(params):
            r = MagicMock()
            for k, v in params.items():
                setattr(r, k, v)
            r.processed_at = "2026-11-18T10:00:00"
            r.is_current = True
            return r

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE schedule_tasks" in sql:
                p = params or {}
                actuals_updated.append(p.get("task_id"))
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                res.fetchone.return_value = make_row(p)
            return res

        mock_db = MagicMock()
        mock_db.execute.side_effect = db_execute

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())

        self.assertGreaterEqual(len(res), 2)
        # Only u-px101 was updated
        self.assertEqual(actuals_updated, ["u-px101"])
        # Unmatched observation has None matched_task_id
        self.assertIsNone(res[1]["matched_task_id"])

    def test_generalization_reprocessing_expires_previous_observations_no_duplicates(self):
        """Test F: Reprocessing a 7-activity update expires old rows and retains exactly 7 current rows."""
        stored_rows: list[dict[str, Any]] = []
        raw_update = (
            "Daily pipeline construction report: Route survey and marking was "
            "completed on 7 November. Right of way clearing was completed on "
            "11 November. Trench excavation reached 100% completion on 17 November. "
            "Sand bedding is currently 80% complete and progressing normally. "
            "Pipe stringing has been completed. Pipeline welding has started and "
            "is approximately 35% complete. Welding activities were delayed by "
            "heavy rainfall for one working day. Weld inspection and NDT has not "
            "started yet because welding is still in progress."
        )
        update_row = _make_update_row(raw_update=raw_update, location="Pipeline Section X", reported_on=datetime.date(2026, 11, 18))
        tasks = [
            MagicMock(id="u-px101", source_task_id="PX101", activity="Route Survey and Marking", location="Pipeline Section X", planned_start="2026-11-01", planned_end="2026-11-10", status="not_started"),
            MagicMock(id="u-px102", source_task_id="PX102", activity="Right of Way Clearing", location="Pipeline Section X", planned_start="2026-11-05", planned_end="2026-11-15", status="not_started"),
            MagicMock(id="u-px103", source_task_id="PX103", activity="Trench Excavation", location="Pipeline Section X", planned_start="2026-11-10", planned_end="2026-11-20", status="not_started"),
            MagicMock(id="u-px104", source_task_id="PX104", activity="Sand Bedding", location="Pipeline Section X", planned_start="2026-11-15", planned_end="2026-11-25", status="not_started"),
            MagicMock(id="u-px105", source_task_id="PX105", activity="Pipe Stringing", location="Pipeline Section X", planned_start="2026-11-20", planned_end="2026-11-30", status="not_started"),
            MagicMock(id="u-px106", source_task_id="PX106", activity="Pipeline Welding", location="Pipeline Section X", planned_start="2026-11-25", planned_end="2026-12-05", status="not_started"),
            MagicMock(id="u-px107", source_task_id="PX107", activity="Weld Inspection and NDT", location="Pipeline Section X", planned_start="2026-12-01", planned_end="2026-12-10", status="not_started"),
        ]

        def db_execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "FROM site_updates" in sql:
                res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql:
                res.fetchall.return_value = tasks
            elif "UPDATE ai_processed_updates" in sql and "is_current = false" in sql:
                p = params or {}
                for row in stored_rows:
                    if row["site_update_id"] == p.get("site_update_id"):
                        row["is_current"] = False
            elif "INSERT INTO ai_processed_updates" in sql:
                p = params or {}
                row_data: dict[str, Any] = dict(p)
                row_data["is_current"] = True
                stored_rows.append(row_data)
                mock_row = MagicMock()
                for k, v in row_data.items():
                    setattr(mock_row, str(k), v)
                mock_row.processed_at = "2026-11-18T10:00:00"
                res.fetchone.return_value = mock_row
            return res

        mock_db = MagicMock()
        mock_db.execute.side_effect = db_execute

        # Run 1
        res1 = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())
        self.assertEqual(len(res1), 7)
        self.assertEqual(len([r for r in stored_rows if r["is_current"]]), 7)

        # Run 2 (Reprocessing)
        res2 = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=MockAIProvider())
        self.assertEqual(len(res2), 7)

        # Total 14 rows: 7 expired historical + 7 active current
        self.assertEqual(len(stored_rows), 14)
        current_rows = [r for r in stored_rows if r["is_current"]]
        self.assertEqual(len(current_rows), 7)
        self.assertEqual([r["observation_index"] for r in current_rows], list(range(7)))

        historical_rows = [r for r in stored_rows if not r["is_current"]]
        self.assertEqual(len(historical_rows), 7)
        self.assertEqual([r["observation_index"] for r in historical_rows], list(range(7)))

    def test_generalization_decoupled_extraction_independent_of_schedule_matching(self):
        """Test G: Observation extraction works independently of schedule task IDs or names."""
        raw_update = (
            "Daily pipeline construction report: Route survey and marking was "
            "completed on 7 November. Right of way clearing was completed on "
            "11 November. Trench excavation reached 100% completion on 17 November. "
            "Sand bedding is currently 80% complete and progressing normally. "
            "Pipe stringing has been completed. Pipeline welding has started and "
            "is approximately 35% complete. Welding activities were delayed by "
            "heavy rainfall for one working day. Weld inspection and NDT has not "
            "started yet because welding is still in progress."
        )
        # Pass empty tasks or unrelated tasks
        result = MockAIProvider().analyse(
            raw_update=raw_update,
            location="Pipeline Section X",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        all_obs = [result] + list(result.additional_observations)
        self.assertEqual(len(all_obs), 7)

        # 1. Route survey
        self.assertEqual(all_obs[0].progress_percent, 100.0)
        self.assertEqual(all_obs[0].status, "completed")
        self.assertEqual(all_obs[0].actual_end_date, "2026-11-07")

        # 2. ROW clearing
        self.assertEqual(all_obs[1].progress_percent, 100.0)
        self.assertEqual(all_obs[1].status, "completed")
        self.assertEqual(all_obs[1].actual_end_date, "2026-11-11")

        # 3. Trench excavation
        self.assertEqual(all_obs[2].progress_percent, 100.0)
        self.assertEqual(all_obs[2].status, "completed")
        self.assertEqual(all_obs[2].actual_end_date, "2026-11-17")

        # 4. Sand bedding
        self.assertEqual(all_obs[3].progress_percent, 80.0)
        self.assertEqual(all_obs[3].status, "in_progress")

        # 5. Pipe stringing
        self.assertEqual(all_obs[4].progress_percent, 100.0)
        self.assertEqual(all_obs[4].status, "completed")

        # 6. Pipeline welding
        self.assertEqual(all_obs[5].progress_percent, 35.0)
        self.assertEqual(all_obs[5].status, "in_progress")
        self.assertEqual(all_obs[5].delay_days, 1)
        self.assertIsNotNone(all_obs[5].delay_reason)
        self.assertIn("heavy rainfall", all_obs[5].delay_reason.lower())

        # 7. Weld inspection
        self.assertIsNone(all_obs[6].progress_percent)
        self.assertEqual(all_obs[6].status, "not_started")


if __name__ == "__main__":
    unittest.main()
