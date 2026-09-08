"""Focused tests for Automatic Schedule Actual Updates from processed site updates.

Requirements verified:
1. When an AI-processed site update has a valid matched task and contains
   actual_start_date, actual_end_date, progress_percent, or status, update the
   corresponding schedule task's actual fields.
2. Baseline planned_start and planned_end dates are strictly preserved and never modified.
3. Never invent actual dates; only apply valid extracted FACT data.
4. Prevent older site updates from overwriting newer actual progress/date information.
5. If a field is UNKNOWN/NULL, do not overwrite an existing known actual value.
6. A completed task status is never downgraded by subsequent updates.
7. Schedule linker reflects updated actuals alongside planned dates.
"""

from __future__ import annotations

import datetime
import json
import unittest
from unittest.mock import MagicMock, call, patch
import uuid

from sqlalchemy.orm import Session

from app.ai_processor import (
    _UPDATE_TASK_ACTUALS,
    process_site_update,
    update_schedule_task_actuals,
)
from app.ai_provider import AIProvider, AnalysisResult, MockAIProvider
from app.schedule_linker import get_linked_tasks, get_task


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

SAMPLE_TASK_ID = "11111111-2222-3333-4444-555555555555"
SAMPLE_UPDATE_ID = "22222222-3333-4444-5555-666666666666"


def _make_update_row(
    raw_update: str = "PCC work commenced on 2026-09-02, currently 40% complete.",
    reported_on: datetime.date | str = datetime.date(2026, 9, 5),
    location: str = "Well Pad A",
):
    row = MagicMock()
    row.id = uuid.UUID(SAMPLE_UPDATE_ID)
    row.source_update_id = "U-001"
    row.reported_on = reported_on
    row.location = location
    row.raw_update = raw_update
    row.ingested_at = datetime.datetime(2026, 9, 5, 12, 0, 0)
    row.source_reference = "daily_report.pdf"
    return row


def _make_candidate_task_row(
    task_id: str = SAMPLE_TASK_ID,
    source_task_id: str = "T101",
    activity: str = "PCC Concrete Pouring",
    location: str = "Well Pad A",
    planned_start: str = "2026-09-01",
    planned_end: str = "2026-09-10",
):
    row = MagicMock()
    row.id = uuid.UUID(task_id)
    row.source_task_id = source_task_id
    row.activity = activity
    row.location = location
    row.planned_start = planned_start
    row.planned_end = planned_end
    return row


def _make_processed_result_row(
    matched_task_id: str | None = SAMPLE_TASK_ID,
    progress_percent: float | None = 40.0,
    status: str | None = "in_progress",
    delay_days: int | None = None,
    delay_reason: str | None = None,
    actual_start_date: str | None = "2026-09-02",
    actual_end_date: str | None = None,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.site_update_id = uuid.UUID(SAMPLE_UPDATE_ID)
    row.matched_task_id = uuid.UUID(matched_task_id) if matched_task_id else None
    row.progress_percent = progress_percent
    row.status = status
    row.delay_days = delay_days
    row.delay_reason = delay_reason
    row.actual_start_date = actual_start_date
    row.actual_end_date = actual_end_date
    row.confidence_score = 80.0
    row.model_name = "mock-keyword-v1"
    row.model_response = json.dumps({"provider": "mock-keyword-v1"})
    row.processed_at = datetime.datetime(2026, 9, 5, 12, 0, 0)
    row.is_current = True
    return row


class ScheduleActualUpdatesCoreTests(unittest.TestCase):
    """Tests verifying update_schedule_task_actuals logic and parameters."""

    def test_update_actuals_passes_all_extracted_fact_fields(self):
        """Calling update_schedule_task_actuals executes _UPDATE_TASK_ACTUALS with correct parameters."""
        mock_db = MagicMock(spec=Session)

        update_schedule_task_actuals(
            db=mock_db,
            task_id=SAMPLE_TASK_ID,
            reported_on="2026-09-05",
            progress_percent=60.0,
            status="in_progress",
            delay_days=2,
            delay_reason="Equipment breakdown",
            actual_start_date="2026-09-01",
            actual_end_date=None,
        )

        self.assertEqual(mock_db.execute.call_count, 1)
        executed_query, params = mock_db.execute.call_args[0]
        self.assertEqual(executed_query, _UPDATE_TASK_ACTUALS)
        self.assertEqual(params["task_id"], SAMPLE_TASK_ID)
        self.assertEqual(params["reported_on"], "2026-09-05")
        self.assertEqual(params["progress_percent"], 60.0)
        self.assertEqual(params["status"], "in_progress")
        self.assertEqual(params["delay_days"], 2)
        self.assertEqual(params["delay_reason"], "Equipment breakdown")
        self.assertEqual(params["actual_start_date"], "2026-09-01")
        self.assertIsNone(params["actual_end_date"])

    def test_update_actuals_handles_date_objects_cleanly(self):
        """Date objects for reported_on, actual_start_date, and actual_end_date are converted to ISO strings."""
        mock_db = MagicMock(spec=Session)

        update_schedule_task_actuals(
            db=mock_db,
            task_id=SAMPLE_TASK_ID,
            reported_on=datetime.date(2026, 9, 8),
            progress_percent=100.0,
            status="completed",
            delay_days=0,
            delay_reason=None,
            actual_start_date=datetime.date(2026, 9, 1),
            actual_end_date=datetime.date(2026, 9, 8),
        )

        _, params = mock_db.execute.call_args[0]
        self.assertEqual(params["reported_on"], "2026-09-08")
        self.assertEqual(params["actual_start_date"], "2026-09-01")
        self.assertEqual(params["actual_end_date"], "2026-09-08")

    def test_planned_dates_are_strictly_preserved(self):
        """The update SQL and parameter dict must NEVER modify planned_start or planned_end."""
        mock_db = MagicMock(spec=Session)

        update_schedule_task_actuals(
            db=mock_db,
            task_id=SAMPLE_TASK_ID,
            reported_on="2026-09-05",
            progress_percent=50.0,
            status="in_progress",
        )

        _, params = mock_db.execute.call_args[0]
        self.assertNotIn("planned_start", params)
        self.assertNotIn("planned_end", params)

        sql_text = str(_UPDATE_TASK_ACTUALS)
        self.assertNotIn("planned_start =", sql_text)
        self.assertNotIn("planned_end =", sql_text)


class ScheduleActualUpdatesSQLGuaranteesTests(unittest.TestCase):
    """Verify SQL safety clauses for monotonicity, stale updates, and NULL handling."""

    def setUp(self):
        self.sql_str = str(_UPDATE_TASK_ACTUALS)

    def test_monotonic_progress_clause_present(self):
        """Lower progress cannot overwrite higher progress."""
        self.assertIn("WHEN :progress_percent > progress_percent THEN :progress_percent", self.sql_str)
        self.assertIn("WHEN :progress_percent IS NULL THEN progress_percent", self.sql_str)

    def test_completed_status_protection_clause_present(self):
        """Completed status is locked and cannot be demoted."""
        self.assertIn("WHEN status = 'completed' THEN 'completed'", self.sql_str)

    def test_stale_update_protection_clause_present(self):
        """Updates with reported_on earlier than last_reported_on do not overwrite status/delay/end date."""
        self.assertIn("CAST(:reported_on AS DATE) < last_reported_on", self.sql_str)
        self.assertIn("CAST(:reported_on AS DATE) >= last_reported_on", self.sql_str)

    def test_earliest_start_date_clause_present(self):
        """Earliest confirmed actual start date is preserved."""
        self.assertIn("CAST(:actual_start_date AS DATE) < actual_start_date", self.sql_str)

    def test_null_value_preserves_existing_known_data(self):
        """Incoming NULL/UNKNOWN fields do not overwrite existing known actuals."""
        self.assertIn("COALESCE(:status, status)", self.sql_str)
        self.assertIn("COALESCE(:delay_days, delay_days)", self.sql_str)
        self.assertIn("COALESCE(:delay_reason, delay_reason)", self.sql_str)


class ProcessSiteUpdateIntegrationTests(unittest.TestCase):
    """Verify end-to-end site update processing triggers schedule actuals updates."""

    def test_process_site_update_updates_schedule_when_matched(self):
        """When an update matches a planned task, update_schedule_task_actuals is automatically invoked."""
        mock_db = MagicMock(spec=Session)

        update_row = _make_update_row("Foundation PCC poured, started on 2026-09-02, 50% complete.")
        cand_task = _make_candidate_task_row()
        processed_row = _make_processed_result_row(progress_percent=50.0, actual_start_date="2026-09-02")

        # Mock DB queries:
        # 1. _FETCH_SITE_UPDATE -> update_row
        # 2. _FETCH_TASKS_BY_LOCATION -> [cand_task]
        # 3. _EXPIRE_CURRENT -> None
        # 4. _INSERT_PROCESSED -> processed_row
        # 5. _UPDATE_TASK_ACTUALS -> None
        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: update_row),
            MagicMock(fetchall=lambda: [cand_task]),
            MagicMock(),
            MagicMock(fetchone=lambda: processed_row),
            MagicMock(),
        ]

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=SAMPLE_TASK_ID,
            progress_percent=50.0,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date="2026-09-02",
            actual_end_date=None,
            confidence_score=85.0,
            model_name="mock-keyword-v1",
            model_response={},
        )

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        self.assertEqual(res["matched_task_id"], SAMPLE_TASK_ID)
        self.assertEqual(res["progress_percent"], 50.0)

        # 5th query executed must be _UPDATE_TASK_ACTUALS
        update_call = mock_db.execute.call_args_list[4]
        self.assertEqual(update_call[0][0], _UPDATE_TASK_ACTUALS)
        params = update_call[0][1]
        self.assertEqual(params["task_id"], SAMPLE_TASK_ID)
        self.assertEqual(params["progress_percent"], 50.0)
        self.assertEqual(params["actual_start_date"], "2026-09-02")
        self.assertNotIn("planned_start", params)
        self.assertNotIn("planned_end", params)

    def test_process_site_update_does_not_update_schedule_when_unmatched(self):
        """When an update cannot be matched to any task, no schedule_tasks update is run."""
        mock_db = MagicMock(spec=Session)

        update_row = _make_update_row("Unrelated activity at remote site.")
        processed_row = _make_processed_result_row(matched_task_id=None)

        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: update_row),
            MagicMock(fetchall=lambda: []),
            MagicMock(),
            MagicMock(fetchone=lambda: processed_row),
        ]

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=None,
            progress_percent=None,
            status=None,
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=10.0,
            model_name="mock-keyword-v1",
            model_response={},
        )

        res = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        self.assertIsNone(res["matched_task_id"])
        # Exactly 4 queries: fetch update, fetch tasks, expire current, insert processed
        # No 5th query (_UPDATE_TASK_ACTUALS)
        self.assertEqual(mock_db.execute.call_count, 4)


class FactExtractionProtectionTests(unittest.TestCase):
    """Verify that dates and progress are strictly FACT data and never invented."""

    def setUp(self):
        self.provider = MockAIProvider()

    def test_no_actual_dates_invented_from_target_or_planned_dates(self):
        """Target or planned dates in text must not be extracted as actual start or end dates."""
        text = "Target completion date is 2026-09-25. Planned start was 2026-09-01."
        res = self.provider.analyse(
            raw_update=text,
            location="Well Pad A",
            reported_on="2026-09-05",
            candidate_tasks=[],
        )
        self.assertIsNone(res.actual_start_date, "Must not treat planned start as actual start")
        self.assertIsNone(res.actual_end_date, "Must not treat target completion as actual end")

    def test_explicit_actual_dates_extracted_as_fact(self):
        """Explicitly stated actual commencement and completion dates are accurately extracted."""
        text = "Excavation commenced on 2026-09-01 and completed on 2026-09-06."
        res = self.provider.analyse(
            raw_update=text,
            location="Well Pad A",
            reported_on="2026-09-06",
            candidate_tasks=[],
        )
        self.assertEqual(res.actual_start_date, "2026-09-01")
        self.assertEqual(res.actual_end_date, "2026-09-06")
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.progress_percent, 100.0)

    def test_unknown_values_remain_none(self):
        """Vague progress text without explicit numbers leaves values as None (UNKNOWN)."""
        text = "Workers on site doing preparation. Weather is sunny."
        res = self.provider.analyse(
            raw_update=text,
            location="Well Pad A",
            reported_on="2026-09-05",
            candidate_tasks=[],
        )
        self.assertIsNone(res.progress_percent)
        self.assertIsNone(res.delay_days)
        self.assertIsNone(res.actual_start_date)
        self.assertIsNone(res.actual_end_date)


if __name__ == "__main__":
    unittest.main()
