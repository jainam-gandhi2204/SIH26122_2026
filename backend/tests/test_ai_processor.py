"""Unit tests for the AI processing layer.

All tests use mocks – no database connection or API calls needed.
"""

import datetime
import json
import unittest
from unittest.mock import MagicMock, patch
import uuid

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.ai_provider import AIProvider, AnalysisResult, MockAIProvider, get_provider
from app.ai_processor import (
    SiteUpdateNotFoundError,
    _format_result,
    get_current_result,
    process_site_update,
)
from app.main import (
    process_site_update as process_endpoint,
    get_site_update_result as get_result_endpoint,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SAMPLE_UPDATE_ID = "11111111-1111-1111-1111-111111111111"
SAMPLE_TASK_ID = "22222222-2222-2222-2222-222222222222"
SAMPLE_RESULT_ID = "33333333-3333-3333-3333-333333333333"


def _make_update_row(
    update_id: str = SAMPLE_UPDATE_ID,
    location: str = "Well Pad A",
    raw_update: str = "Site preparation completed successfully.",
    reported_on: datetime.date = datetime.date(2026, 9, 5),
):
    row = MagicMock()
    row.id = uuid.UUID(update_id)
    row.location = location
    row.raw_update = raw_update
    row.reported_on = reported_on
    row.source_update_id = "U001"
    return row


def _make_task_row(
    task_id: str = SAMPLE_TASK_ID,
    activity: str = "Site Preparation",
    location: str = "Well Pad A",
    planned_start: str = "2026-09-01",
    planned_end: str = "2026-09-05",
):
    row = MagicMock()
    row.id = uuid.UUID(task_id)
    row.source_task_id = "T101"
    row.activity = activity
    row.location = location
    row.planned_start = planned_start
    row.planned_end = planned_end
    return row


def _make_result_row(result_id: str = SAMPLE_RESULT_ID):
    row = MagicMock()
    row.id = uuid.UUID(result_id)
    row.site_update_id = uuid.UUID(SAMPLE_UPDATE_ID)
    row.matched_task_id = uuid.UUID(SAMPLE_TASK_ID)
    row.progress_percent = 100.0
    row.status = "completed"
    row.delay_days = None
    row.delay_reason = None
    row.actual_start_date = None
    row.actual_end_date = None
    row.confidence_score = 40.0
    row.model_name = "mock-keyword-v1"
    row.model_response = json.dumps({"note": "test"})
    row.processed_at = datetime.datetime(2026, 9, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
    row.is_current = True
    return row


def _mock_db_for_process(update_row=None, task_rows=None, result_row=None):
    """Build a mock DB session that returns specified rows in sequence."""
    mock_db = MagicMock()
    results = []

    # fetchone for FETCH_SITE_UPDATE
    r1 = MagicMock()
    r1.fetchone.return_value = update_row
    results.append(r1)

    # fetchall for FETCH_TASKS_BY_LOCATION
    r2 = MagicMock()
    r2.fetchall.return_value = task_rows or []
    results.append(r2)

    # EXPIRE_CURRENT (no fetch needed, just execute)
    r3 = MagicMock()
    results.append(r3)

    # fetchone for INSERT_PROCESSED
    r4 = MagicMock()
    r4.fetchone.return_value = result_row
    results.append(r4)

    mock_db.execute.side_effect = results
    return mock_db


# ---------------------------------------------------------------------------
# MockAIProvider tests
# ---------------------------------------------------------------------------

class MockAIProviderTests(unittest.TestCase):

    def setUp(self):
        self.provider = MockAIProvider()

    def _analyse(self, text: str, tasks=None):
        return self.provider.analyse(
            raw_update=text,
            location="Well Pad A",
            reported_on="2026-09-05",
            candidate_tasks=tasks or [],
        )

    def test_completed_status_inferred_from_keyword(self):
        result = self._analyse("Site preparation completed successfully.")
        self.assertEqual(result.status, "completed")

    def test_in_progress_status_inferred_from_keyword(self):
        result = self._analyse("Foundation work is in progress and ongoing.")
        self.assertEqual(result.status, "in_progress")

    def test_delayed_status_inferred_from_keyword(self):
        result = self._analyse("Work has been delayed due to heavy rainfall.")
        self.assertEqual(result.status, "delayed")

    def test_blocked_status_inferred_from_keyword(self):
        result = self._analyse("Operations are blocked due to equipment failure.")
        self.assertEqual(result.status, "blocked")

    def test_no_keyword_gives_unknown_status(self):
        result = self._analyse("Workers were on site today.")
        self.assertIsNone(result.status)

    def test_explicit_percent_extracted(self):
        result = self._analyse("Equipment installation is 75% complete.")
        self.assertEqual(result.progress_percent, 75.0)

    def test_completed_implies_100_percent(self):
        result = self._analyse("Site preparation completed successfully.")
        self.assertEqual(result.progress_percent, 100.0)

    def test_no_percent_in_non_completed_update(self):
        result = self._analyse("Workers arrived on site today.")
        self.assertIsNone(result.progress_percent)

    def test_task_matched_by_location_fallback(self):
        tasks = [
            {"id": SAMPLE_TASK_ID, "source_task_id": "T101", "activity": "Site Preparation",
             "location": "Well Pad A", "planned_start": "2026-09-01", "planned_end": "2026-09-05"},
        ]
        result = self._analyse("General update.", tasks=tasks)
        self.assertEqual(result.matched_task_id, SAMPLE_TASK_ID)

    def test_task_matched_by_activity_keyword(self):
        other_id = "44444444-4444-4444-4444-444444444444"
        tasks = [
            {"id": other_id, "source_task_id": "T100", "activity": "Clearing",
             "location": "Well Pad A", "planned_start": "2026-08-25", "planned_end": "2026-08-31"},
            {"id": SAMPLE_TASK_ID, "source_task_id": "T101", "activity": "Foundation Construction",
             "location": "Well Pad A", "planned_start": "2026-09-01", "planned_end": "2026-09-10"},
        ]
        result = self._analyse("Foundation work is in progress.", tasks=tasks)
        self.assertEqual(result.matched_task_id, SAMPLE_TASK_ID)

    def test_no_tasks_gives_no_match(self):
        result = self._analyse("Site update text.", tasks=[])
        self.assertIsNone(result.matched_task_id)

    def test_confidence_lower_with_no_match(self):
        result_no_task = self._analyse("Site update.", tasks=[])
        result_with_task = self._analyse(
            "Site update.",
            tasks=[{"id": SAMPLE_TASK_ID, "source_task_id": "T1",
                    "activity": "Work", "location": "Well Pad A",
                    "planned_start": "2026-09-01", "planned_end": "2026-09-05"}]
        )
        self.assertLess(result_no_task.confidence_score, result_with_task.confidence_score)

    def test_delay_days_extracted_from_text(self):
        result = self._analyse("Work is 3 days delay due to rain.")
        self.assertEqual(result.delay_days, 3)

    def test_delay_reason_extracted_when_delayed(self):
        result = self._analyse("Operations delayed due to heavy rainfall.")
        self.assertIsNotNone(result.delay_reason)
        self.assertIn("delay", result.delay_reason.lower())

    def test_actual_dates_always_unknown(self):
        result = self._analyse("Completed on 01-Sep-2026.")
        # Mock provider never extracts actual dates (too complex / risky)
        self.assertIsNone(result.actual_start_date)
        self.assertIsNone(result.actual_end_date)

    def test_model_name_is_correct(self):
        result = self._analyse("Test.")
        self.assertEqual(result.model_name, "mock-keyword-v1")

    def test_model_response_is_dict(self):
        result = self._analyse("Test.")
        self.assertIsInstance(result.model_response, dict)

    def test_confidence_within_valid_range(self):
        result = self._analyse("Test.", tasks=[{"id": SAMPLE_TASK_ID, "source_task_id": "T1",
                                               "activity": "Work", "location": "Well Pad A",
                                               "planned_start": "2026-09-01", "planned_end": "2026-09-05"}])
        self.assertGreaterEqual(result.confidence_score, 0.0)
        self.assertLessEqual(result.confidence_score, 100.0)


# ---------------------------------------------------------------------------
# get_provider tests
# ---------------------------------------------------------------------------

class GetProviderTests(unittest.TestCase):

    def test_default_is_mock(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AI_PROVIDER", None)
            provider = get_provider()
            self.assertIsInstance(provider, MockAIProvider)

    def test_mock_explicit(self):
        with patch.dict("os.environ", {"AI_PROVIDER": "mock"}):
            provider = get_provider()
            self.assertIsInstance(provider, MockAIProvider)

    def test_unknown_provider_raises(self):
        with patch.dict("os.environ", {"AI_PROVIDER": "unknown_model"}):
            with self.assertRaises(ValueError):
                get_provider()


# ---------------------------------------------------------------------------
# process_site_update (orchestration) tests
# ---------------------------------------------------------------------------

class ProcessSiteUpdateTests(unittest.TestCase):

    def _make_stub_provider(self, task_id=SAMPLE_TASK_ID, status="completed", progress=100.0):
        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=task_id,
            progress_percent=progress,
            status=status,
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=40.0,
            model_name="mock-keyword-v1",
            model_response={"note": "stub"},
        )
        return provider

    def test_process_success_returns_formatted_result(self):
        update_row = _make_update_row()
        task_row = _make_task_row()
        result_row = _make_result_row()
        mock_db = _mock_db_for_process(update_row, [task_row], result_row)
        provider = self._make_stub_provider()

        result = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        self.assertEqual(result["id"], SAMPLE_RESULT_ID)
        self.assertEqual(result["site_update_id"], SAMPLE_UPDATE_ID)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["is_current"])
        mock_db.commit.assert_called_once()

    def test_process_site_update_not_found_raises(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = None
        provider = self._make_stub_provider()

        with self.assertRaises(SiteUpdateNotFoundError):
            process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

    def test_process_calls_expire_then_insert(self):
        update_row = _make_update_row()
        result_row = _make_result_row()
        mock_db = _mock_db_for_process(update_row, [], result_row)
        provider = self._make_stub_provider(task_id=None)

        process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # 4 execute calls: fetch update, fetch tasks, expire current, insert new
        self.assertEqual(mock_db.execute.call_count, 4)

    def test_db_error_propagates(self):
        mock_db = MagicMock()
        mock_db.execute.side_effect = OperationalError("fail", None, None)
        provider = self._make_stub_provider()

        with self.assertRaises(OperationalError):
            process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)


# ---------------------------------------------------------------------------
# get_current_result tests
# ---------------------------------------------------------------------------

class GetCurrentResultTests(unittest.TestCase):

    def test_returns_formatted_result_when_present(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = _make_result_row()

        result = get_current_result(mock_db, SAMPLE_UPDATE_ID)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], SAMPLE_RESULT_ID)

    def test_returns_none_when_not_processed(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = None

        result = get_current_result(mock_db, SAMPLE_UPDATE_ID)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------

class ProcessEndpointTests(unittest.TestCase):

    def _make_stub_provider_result(self):
        return {
            "id": SAMPLE_RESULT_ID,
            "site_update_id": SAMPLE_UPDATE_ID,
            "matched_task_id": SAMPLE_TASK_ID,
            "progress_percent": 100.0,
            "status": "completed",
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 40.0,
            "model_name": "mock-keyword-v1",
            "model_response": {"note": "stub"},
            "processed_at": "2026-09-05T12:00:00+00:00",
            "is_current": True,
        }

    @patch("app.main.ai_processor.process_site_update")
    def test_process_endpoint_success(self, mock_process):
        mock_process.return_value = self._make_stub_provider_result()
        mock_db = MagicMock()

        result = process_endpoint(SAMPLE_UPDATE_ID, db=mock_db)

        mock_process.assert_called_once_with(mock_db, SAMPLE_UPDATE_ID)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["id"], SAMPLE_RESULT_ID)

    @patch("app.main.ai_processor.process_site_update", side_effect=SiteUpdateNotFoundError("not found"))
    def test_process_endpoint_not_found_raises_404(self, _mock):
        mock_db = MagicMock()
        with self.assertRaises(HTTPException) as ctx:
            process_endpoint(SAMPLE_UPDATE_ID, db=mock_db)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.ai_processor.process_site_update", side_effect=OperationalError("db fail", None, None))
    def test_process_endpoint_db_error_raises_503(self, _mock):
        mock_db = MagicMock()
        with self.assertRaises(HTTPException) as ctx:
            process_endpoint(SAMPLE_UPDATE_ID, db=mock_db)
        self.assertEqual(ctx.exception.status_code, 503)
        mock_db.rollback.assert_called_once()

    @patch("app.main.ai_processor.get_current_result")
    def test_get_result_endpoint_success(self, mock_get):
        mock_get.return_value = self._make_stub_provider_result()
        mock_db = MagicMock()

        result = get_result_endpoint(SAMPLE_UPDATE_ID, db=mock_db)
        self.assertEqual(result["id"], SAMPLE_RESULT_ID)

    @patch("app.main.ai_processor.get_current_result", return_value=None)
    def test_get_result_endpoint_not_processed_raises_404(self, _mock):
        mock_db = MagicMock()
        with self.assertRaises(HTTPException) as ctx:
            get_result_endpoint(SAMPLE_UPDATE_ID, db=mock_db)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.ai_processor.get_current_result", side_effect=OperationalError("fail", None, None))
    def test_get_result_endpoint_db_error_raises_503(self, _mock):
        mock_db = MagicMock()
        with self.assertRaises(HTTPException) as ctx:
            get_result_endpoint(SAMPLE_UPDATE_ID, db=mock_db)
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
