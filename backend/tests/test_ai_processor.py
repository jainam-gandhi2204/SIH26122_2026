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

from app.ai_provider import (
    AIProvider,
    ActivityObservation,
    AnalysisResult,
    GeminiAIProvider,
    MockAIProvider,
    _extract_json,
    _safe_float,
    _safe_int,
    _safe_iso_date,
    _safe_str,
    _unknown_result,
    get_provider,
    sanitize_error_message,
)
from app.ai_processor import (
    SiteUpdateNotFoundError,
    _UPDATE_TASK_ACTUALS,
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


def _make_result_row(
    result_id: str = SAMPLE_RESULT_ID,
    actual_start_date: str | None = None,
    actual_end_date: str | None = None,
):
    row = MagicMock()
    row.id = uuid.UUID(result_id)
    row.site_update_id = uuid.UUID(SAMPLE_UPDATE_ID)
    row.matched_task_id = uuid.UUID(SAMPLE_TASK_ID)
    row.progress_percent = 100.0
    row.status = "completed"
    row.delay_days = None
    row.delay_reason = None
    row.actual_start_date = actual_start_date
    row.actual_end_date = actual_end_date
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

    def _side_effect(*args, **kwargs):
        if results:
            return results.pop(0)
        return MagicMock()

    mock_db.execute.side_effect = _side_effect
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

    def test_around_qualifier_percent_extracted(self):
        """Regression for U002: 'around X%' must be treated as an explicitly
        stated FACT, not an estimate to be discarded."""
        result = self._analyse("Concreting is around 40% complete.")
        self.assertEqual(result.progress_percent, 40.0)

    def test_approximately_qualifier_percent_extracted(self):
        """'approximately X%' must also be extracted as a FACT."""
        result = self._analyse("Foundation work is approximately 60% done.")
        self.assertEqual(result.progress_percent, 60.0)

    def test_roughly_qualifier_percent_extracted(self):
        result = self._analyse("Roughly 25% of the steelwork is installed.")
        self.assertEqual(result.progress_percent, 25.0)

    def test_about_qualifier_percent_extracted(self):
        result = self._analyse("Grading is about 80% complete.")
        self.assertEqual(result.progress_percent, 80.0)

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
        assert result.delay_reason is not None
        self.assertIn("delay", result.delay_reason.lower())

    def test_actual_dates_unknown_when_not_stated(self):
        """When no dates are mentioned, actual dates remain None (UNKNOWN)."""
        result = self._analyse("Site preparation completed successfully.")
        self.assertIsNone(result.actual_start_date)
        self.assertIsNone(result.actual_end_date)

    def test_actual_start_date_extracted_iso(self):
        """Explicit 'started on YYYY-MM-DD' is extracted as actual_start_date."""
        result = self._analyse("Excavation started on 2026-09-01 and is ongoing.")
        self.assertEqual(result.actual_start_date, "2026-09-01")
        self.assertIsNone(result.actual_end_date)

    def test_actual_start_date_extracted_dd_mon_yyyy(self):
        """Explicit 'started on DD-Mon-YYYY' is converted to ISO YYYY-MM-DD."""
        result = self._analyse("Work started on 02-Sep-2026.")
        self.assertEqual(result.actual_start_date, "2026-09-02")
        self.assertIsNone(result.actual_end_date)

    def test_actual_end_date_extracted_iso(self):
        """Explicit 'completed on YYYY-MM-DD' is extracted as actual_end_date."""
        result = self._analyse("Site preparation completed on 2026-09-05.")
        self.assertIsNone(result.actual_start_date)
        self.assertEqual(result.actual_end_date, "2026-09-05")

    def test_actual_end_date_extracted_dd_mon_yyyy(self):
        """Explicit 'completed on DD-Mon-YYYY' is converted to ISO YYYY-MM-DD."""
        result = self._analyse("Completed on 01-Sep-2026.")
        self.assertIsNone(result.actual_start_date)
        self.assertEqual(result.actual_end_date, "2026-09-01")

    def test_both_actual_dates_extracted(self):
        """Both start and end dates extracted when both are explicitly stated."""
        result = self._analyse(
            "Site preparation started on 2026-09-01 and completed on 2026-09-05."
        )
        self.assertEqual(result.actual_start_date, "2026-09-01")
        self.assertEqual(result.actual_end_date, "2026-09-05")

    def test_actual_start_alternative_phrasings(self):
        """Test commenced, began, start date, and actual start phrasings."""
        r1 = self._analyse("Piling commenced on 03-Sep-2026.")
        self.assertEqual(r1.actual_start_date, "2026-09-03")

        r2 = self._analyse("Foundation work began on 2026-09-04.")
        self.assertEqual(r2.actual_start_date, "2026-09-04")

        r3 = self._analyse("Start date: 2026-09-01. Work is progressing.")
        self.assertEqual(r3.actual_start_date, "2026-09-01")

        r4 = self._analyse("Actual start: 2026-09-02.")
        self.assertEqual(r4.actual_start_date, "2026-09-02")

    def test_actual_end_alternative_phrasings(self):
        """Test finished, ended, completion date, and actual end phrasings."""
        r1 = self._analyse("Grading finished on 05-Sep-2026.")
        self.assertEqual(r1.actual_end_date, "2026-09-05")

        r2 = self._analyse("Activity ended on 2026-09-06.")
        self.assertEqual(r2.actual_end_date, "2026-09-06")

        r3 = self._analyse("Completion date: 2026-09-07.")
        self.assertEqual(r3.actual_end_date, "2026-09-07")

        r4 = self._analyse("Actual end: 2026-09-08.")
        self.assertEqual(r4.actual_end_date, "2026-09-08")

    def test_planned_dates_not_extracted_as_actual(self):
        """Planned dates should not be confused with actual start dates."""
        result = self._analyse(
            "Planned start date: 2026-09-01. Actual start date: 2026-09-03."
        )
        self.assertEqual(result.actual_start_date, "2026-09-03")

    def test_progress_percent_does_not_trigger_end_date(self):
        """Statements like '40% complete' must NOT set actual_end_date."""
        result = self._analyse(
            "Concreting is around 40% complete. Delayed by 2 days due to rain."
        )
        self.assertIsNone(result.actual_start_date)
        self.assertIsNone(result.actual_end_date)
        self.assertEqual(result.progress_percent, 40.0)

    def test_relative_date_words_remain_unknown(self):
        """Relative terms like 'today' or 'yesterday' never invent dates."""
        result = self._analyse("Work started today and finished yesterday.")
        self.assertIsNone(result.actual_start_date)
        self.assertIsNone(result.actual_end_date)

    def test_invalid_calendar_date_returns_none(self):
        """Impossible calendar dates (e.g. Feb 31) are rejected as None."""
        result = self._analyse("Work started on 2026-02-31.")
        self.assertIsNone(result.actual_start_date)

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

    def _make_stub_provider(
        self,
        task_id=SAMPLE_TASK_ID,
        status="completed",
        progress=100.0,
        actual_start_date=None,
        actual_end_date=None,
    ):
        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=task_id,
            progress_percent=progress,
            status=status,
            delay_days=None,
            delay_reason=None,
            actual_start_date=actual_start_date,
            actual_end_date=actual_end_date,
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

    def test_process_persists_actual_dates_to_database(self):
        update_row = _make_update_row(
            raw_update="Site preparation started on 2026-09-01 and completed on 2026-09-05."
        )
        task_row = _make_task_row()
        result_row = _make_result_row(
            actual_start_date="2026-09-01",
            actual_end_date="2026-09-05",
        )
        mock_db = _mock_db_for_process(update_row, [task_row], result_row)
        provider = self._make_stub_provider(
            actual_start_date="2026-09-01",
            actual_end_date="2026-09-05",
        )

        result = process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # Check DB insert parameters received actual dates
        insert_call = mock_db.execute.call_args_list[3]
        params = insert_call[0][1]
        self.assertEqual(params["actual_start_date"], "2026-09-01")
        self.assertEqual(params["actual_end_date"], "2026-09-05")

        # Check return dict
        self.assertEqual(result["actual_start_date"], "2026-09-01")
        self.assertEqual(result["actual_end_date"], "2026-09-05")

    def test_process_automatically_updates_schedule_tasks_actuals(self):
        """When an update is linked to a task with HIGH confidence (>= 80.0), schedule_tasks is updated."""
        update_row = _make_update_row(raw_update="Foundation work is 60% done. 2 days delay.")
        task_row = _make_task_row()
        result_row = _make_result_row()
        mock_db = _mock_db_for_process(update_row, [task_row], result_row)

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=SAMPLE_TASK_ID,
            progress_percent=60.0,
            status="delayed",
            delay_days=2,
            delay_reason="Rain",
            actual_start_date="2026-09-02",
            actual_end_date=None,
            confidence_score=82.0,   # >= 80.0 → auto-link threshold
            model_name="mock-keyword-v1",
            model_response={},
        )

        process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # Call 4 must be the schedule_tasks actuals update
        self.assertEqual(mock_db.execute.call_count, 5)
        update_call = mock_db.execute.call_args_list[4]
        params = update_call[0][1]

        self.assertEqual(params["task_id"], SAMPLE_TASK_ID)
        self.assertEqual(params["reported_on"], "2026-09-05")
        self.assertEqual(params["progress_percent"], 60.0)
        self.assertEqual(params["status"], "delayed")
        self.assertEqual(params["delay_days"], 2)
        self.assertEqual(params["delay_reason"], "Rain")
        self.assertEqual(params["actual_start_date"], "2026-09-02")
        self.assertIsNone(params["actual_end_date"])

        # Planned dates must NEVER be overwritten / in the parameters
        self.assertNotIn("planned_start", params)
        self.assertNotIn("planned_end", params)


    def test_process_unmatched_task_does_not_update_schedule_tasks(self):
        """When an update does not match any planned task, schedule_tasks is not updated."""
        update_row = _make_update_row(raw_update="Workers arrived on site.")
        result_row = _make_result_row()
        mock_db = _mock_db_for_process(update_row, [], result_row)
        provider = self._make_stub_provider(task_id=None)

        process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # 4 execute calls: fetch update, fetch tasks, expire current, insert new (no update to schedule_tasks)
        self.assertEqual(mock_db.execute.call_count, 4)

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


# ---------------------------------------------------------------------------
# GetProviderTests – extended to cover gemini path
# ---------------------------------------------------------------------------

class GetProviderGeminiTests(unittest.TestCase):
    """Tests for get_provider() gemini path (mocked to avoid a real key)."""

    def test_get_provider_gemini_returns_gemini_provider(self):
        """get_provider('gemini') returns a GeminiAIProvider when key is set."""
        with patch.dict("os.environ", {"AI_PROVIDER": "gemini", "GEMINI_API_KEY": "fake-key"}):
            with patch("app.ai_provider.GeminiAIProvider.__init__", return_value=None):
                provider = get_provider()
                self.assertIsInstance(provider, GeminiAIProvider)

    def test_get_provider_unknown_raises(self):
        with patch.dict("os.environ", {"AI_PROVIDER": "chatgpt"}):
            with self.assertRaises(ValueError) as ctx:
                get_provider()
            self.assertIn("chatgpt", str(ctx.exception))


# ---------------------------------------------------------------------------
# GeminiAIProvider – unit tests (all mock the genai client)
# ---------------------------------------------------------------------------

def _make_gemini_response(json_body: dict) -> MagicMock:
    """Build a mock genai response whose .text is json.dumps(json_body)."""
    resp = MagicMock()
    resp.text = json.dumps(json_body)
    return resp


def _make_gemini_provider() -> GeminiAIProvider:
    """Instantiate GeminiAIProvider with a mocked genai.Client."""
    with patch("google.genai.Client") as MockClient:
        provider = GeminiAIProvider.__new__(GeminiAIProvider)
        provider._client = MockClient.return_value
        provider._types = MagicMock()
        provider._types.GenerateContentConfig = MagicMock()
    return provider


SAMPLE_TASKS_FOR_GEMINI = [
    {
        "id": SAMPLE_TASK_ID,
        "source_task_id": "T101",
        "activity": "Site Preparation",
        "location": "Well Pad A",
        "planned_start": "2026-09-01",
        "planned_end": "2026-09-05",
    }
]


class GeminiAIProviderTests(unittest.TestCase):
    """Unit tests for GeminiAIProvider.analyse() with all genai calls mocked."""

    def setUp(self):
        self.provider = _make_gemini_provider()

    def _call_with_response(self, json_body: dict, tasks=None) -> AnalysisResult:
        """Call provider.analyse() with a mocked API response."""
        self.provider._client.models.generate_content.return_value = (
            _make_gemini_response(json_body)
        )
        return self.provider.analyse(
            raw_update="Site preparation completed successfully.",
            location="Well Pad A",
            reported_on="2026-09-05",
            candidate_tasks=tasks if tasks is not None else SAMPLE_TASKS_FOR_GEMINI,
        )

    # --- Happy path ---

    def test_completed_status_parsed(self):
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "completed",
            "progress_percent": 100,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 85,
            "reasoning": "Update clearly states completion.",
        })
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.progress_percent, 100.0)
        # Composite confidence (LLM + deterministic + extraction + location) will be
        # higher than 80 for a clear match — not necessarily the raw LLM score of 85.
        self.assertGreaterEqual(result.confidence_score, 80.0)
        self.assertLessEqual(result.confidence_score, 100.0)
        self.assertEqual(result.matched_task_id, SAMPLE_TASK_ID)

    def test_task_id_resolved_from_source_task_id(self):
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "in_progress",
            "progress_percent": 60,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 70,
            "reasoning": "Task matched by location and activity.",
        })
        self.assertEqual(result.matched_task_id, SAMPLE_TASK_ID)

    def test_unknown_source_task_id_gives_none_match(self):
        result = self._call_with_response({
            "matched_source_task_id": "T999",  # not in task list
            "status": "in_progress",
            "progress_percent": None,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 40,
            "reasoning": "Unknown task.",
        })
        self.assertIsNone(result.matched_task_id)

    def test_delay_fields_extracted(self):
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "delayed",
            "progress_percent": None,
            "delay_days": 3,
            "delay_reason": "Heavy rainfall blocked access",
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 75,
            "reasoning": "Delay explicitly mentioned.",
        })
        self.assertEqual(result.status, "delayed")
        self.assertEqual(result.delay_days, 3)
        self.assertEqual(result.delay_reason, "Heavy rainfall blocked access")

    def test_valid_actual_dates_parsed(self):
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "completed",
            "progress_percent": 100,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": "2026-09-01",
            "actual_end_date": "2026-09-05",
            "confidence_score": 90,
            "reasoning": "Explicit dates stated.",
        })
        self.assertEqual(result.actual_start_date, "2026-09-01")
        self.assertEqual(result.actual_end_date, "2026-09-05")

    def test_model_name_is_gemini(self):
        result = self._call_with_response({
            "matched_source_task_id": None,
            "status": None,
            "progress_percent": None,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 20,
            "reasoning": "Vague update.",
        })
        self.assertEqual(result.model_name, "gemini-3.6-flash")

    def test_model_response_contains_raw_json(self):
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "in_progress",
            "progress_percent": 50,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 65,
            "reasoning": "In progress.",
        })
        self.assertIn("raw_json", result.model_response)
        self.assertIn("reasoning", result.model_response)

    # --- Validation / safety ---

    def test_invalid_status_rejected(self):
        """A status not in the DB enum is silently dropped to None."""
        result = self._call_with_response({
            "matched_source_task_id": None,
            "status": "partially_done",      # not a valid DB status
            "progress_percent": None,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 30,
            "reasoning": "Unknown status from model.",
        })
        self.assertIsNone(result.status)

    def test_out_of_range_progress_rejected(self):
        """progress_percent > 100 is silently dropped to None."""
        result = self._call_with_response({
            "matched_source_task_id": None,
            "status": None,
            "progress_percent": 150,    # invalid
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 50,
            "reasoning": "Test.",
        })
        self.assertIsNone(result.progress_percent)

    def test_negative_delay_days_rejected(self):
        """Negative delay_days is silently dropped to None."""
        result = self._call_with_response({
            "matched_source_task_id": None,
            "status": "delayed",
            "progress_percent": None,
            "delay_days": -5,           # invalid
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 50,
            "reasoning": "Test.",
        })
        self.assertIsNone(result.delay_days)

    def test_malformed_date_rejected(self):
        """Dates not in YYYY-MM-DD format are dropped to None."""
        result = self._call_with_response({
            "matched_source_task_id": None,
            "status": "completed",
            "progress_percent": 100,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": "05-Sep-2026",    # wrong format
            "actual_end_date": "2026/09/05",        # wrong format
            "confidence_score": 80,
            "reasoning": "Test.",
        })
        self.assertIsNone(result.actual_start_date)
        self.assertIsNone(result.actual_end_date)

    def test_confidence_clamped_to_100(self):
        """Confidence scores above 100 are clamped so result never exceeds 100.0."""
        result = self._call_with_response({
            "matched_source_task_id": None,
            "status": None,
            "progress_percent": None,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 999,    # out of range
            "reasoning": "Test.",
        })
        # The composite formula clamps LLM confidence to 100 internally.
        # With no match, deterministic/location scores are 0, so composite < 100.
        # The key invariant: result must never be > 100 and never be 999.
        self.assertLessEqual(result.confidence_score, 100.0)
        self.assertNotEqual(result.confidence_score, 999)
        self.assertGreater(result.confidence_score, 0.0)

    def test_no_tasks_matched_task_id_is_none(self):
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "in_progress",
            "progress_percent": None,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 50,
            "reasoning": "Test.",
        }, tasks=[])  # no tasks provided → map is empty → matched_task_id is None
        self.assertIsNone(result.matched_task_id)

    # --- U002-style regression: approximation-qualified percentages (FACT) ---

    def test_progress_with_around_qualifier_extracted(self):
        """Regression for U002: 'concreting is around 40% complete' must yield
        progress_percent=40.0.  The word 'around' is an approximation qualifier,
        not an invented estimate — the reporter explicitly stated the figure."""
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "in_progress",
            "progress_percent": 40,       # model should now extract this
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 75,
            "reasoning": "Update states 'around 40% complete'.",
        })
        self.assertEqual(result.progress_percent, 40.0)
        self.assertEqual(result.status, "in_progress")

    def test_progress_with_approximately_qualifier_extracted(self):
        """'approximately 60% done' should yield progress_percent=60.0."""
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "in_progress",
            "progress_percent": 60,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 72,
            "reasoning": "Update states 'approximately 60% done'.",
        })
        self.assertEqual(result.progress_percent, 60.0)

    def test_progress_null_when_no_percentage_stated(self):
        """When no percentage figure is mentioned, progress_percent must stay null.
        Ensures the hallucination guard is preserved."""
        result = self._call_with_response({
            "matched_source_task_id": "T101",
            "status": "in_progress",
            "progress_percent": None,     # no percentage in text
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 55,
            "reasoning": "Work is ongoing; no percentage stated.",
        })
        self.assertIsNone(result.progress_percent)

    # --- Error / fallback handling ---

    def test_api_exception_returns_unknown_result(self):
        """Any API exception returns a zero-confidence all-None result."""
        from google.genai.errors import APIError
        self.provider._client.models.generate_content.side_effect = Exception("Network timeout")
        result = self.provider.analyse(
            raw_update="Test update.",
            location="Zone A",
            reported_on="2026-09-05",
            candidate_tasks=[],
        )
        self.assertIsNone(result.status)
        self.assertIsNone(result.matched_task_id)
        self.assertEqual(result.confidence_score, 0.0)
        self.assertIn("error", result.model_response)

    def test_invalid_json_response_returns_unknown_result(self):
        """A non-JSON response string triggers the fallback."""
        resp = MagicMock()
        resp.text = "Sorry, I cannot process this request."
        self.provider._client.models.generate_content.return_value = resp
        result = self.provider.analyse(
            raw_update="Test update.",
            location="Zone A",
            reported_on="2026-09-05",
            candidate_tasks=[],
        )
        self.assertEqual(result.confidence_score, 0.0)
        self.assertIsNone(result.status)

    def test_missing_key_raises_value_error(self):
        """GeminiAIProvider.__init__ raises ValueError with no API key."""
        with patch.dict("os.environ", {}, clear=True):
            # Patch google.genai.Client to avoid actually hitting network
            with patch("google.genai.Client"):
                with self.assertRaises(ValueError) as ctx:
                    GeminiAIProvider(api_key=None)
                self.assertIn("GEMINI_API_KEY", str(ctx.exception))


# ---------------------------------------------------------------------------
# _extract_json unit tests  (covers the exact U002 failure scenario)
# ---------------------------------------------------------------------------

class ExtractJsonTests(unittest.TestCase):
    """Unit tests for the _extract_json helper.

    This function is the fix for the reported 'Unterminated string starting
    at line 5 column 3' error: previously json.loads was called directly on
    the raw model text without any pre-processing.
    """

    # --- Happy path: plain JSON (MIME type honoured by model) ---

    def test_plain_json_parsed(self):
        result = _extract_json('{"status": "completed", "progress_percent": 100}')
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress_percent"], 100)

    def test_plain_json_with_leading_trailing_whitespace(self):
        result = _extract_json('  \n{"status": "in_progress"}\n  ')
        self.assertEqual(result["status"], "in_progress")

    def test_plain_json_all_null_fields(self):
        payload = json.dumps({
            "matched_source_task_id": None,
            "status": None,
            "progress_percent": None,
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 25,
            "reasoning": "Vague update.",
        })
        result = _extract_json(payload)
        self.assertIsNone(result["status"])
        self.assertEqual(result["confidence_score"], 25)

    # --- Fenced JSON (model ignores response_mime_type) ---

    def test_fenced_json_with_language_tag(self):
        text = '```json\n{"status": "delayed", "delay_days": 3}\n```'
        result = _extract_json(text)
        self.assertEqual(result["status"], "delayed")
        self.assertEqual(result["delay_days"], 3)

    def test_fenced_json_without_language_tag(self):
        text = '```\n{"status": "completed"}\n```'
        result = _extract_json(text)
        self.assertEqual(result["status"], "completed")

    def test_fenced_json_with_surrounding_prose(self):
        text = (
            "Here is the analysis:\n"
            "```json\n"
            '{"status": "in_progress", "confidence_score": 70}\n'
            "```\n"
            "Let me know if you need more detail."
        )
        result = _extract_json(text)
        self.assertEqual(result["status"], "in_progress")

    def test_fenced_json_uppercase_JSON_tag(self):
        text = '```JSON\n{"status": "blocked"}\n```'
        result = _extract_json(text)
        self.assertEqual(result["status"], "blocked")

    # --- Brace-extraction fallback (prose wrapping the JSON) ---

    def test_json_embedded_in_prose_extracted_by_braces(self):
        text = 'The answer is: {"status": "not_started", "confidence_score": 30} — end.'
        result = _extract_json(text)
        self.assertEqual(result["status"], "not_started")

    # --- Error cases ---

    def test_empty_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            _extract_json("")

    def test_whitespace_only_raises_value_error(self):
        with self.assertRaises(ValueError):
            _extract_json("   \n  ")

    def test_plain_prose_with_no_json_raises_value_error(self):
        """Reproduces the exact class of failure: model returned non-JSON prose."""
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            _extract_json("I cannot process this request.")

    def test_truncated_json_raises_parse_error(self):
        """Reproduces the EXACT U002 failure: 'Unterminated string starting at
        line 5 column 3' — caused by max_output_tokens truncating mid-string.

        When the JSON is truncated before the closing brace, _extract_json raises
        ValueError (no JSON object found) or JSONDecodeError depending on whether
        a closing brace is accidentally present. Both mean 'could not parse', and
        the caller (analyse()) handles both by returning a zero-confidence result.
        """
        truncated = '{"status": "in_progress", "reasoning": "Work is ongoing at the site and crews are'
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            _extract_json(truncated)

    def test_fenced_truncated_json_raises_json_decode_error(self):
        """Fenced response that was also truncated mid-string."""
        truncated_fenced = '```json\n{"status": "delayed", "reasoning": "Equipment broke'
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            _extract_json(truncated_fenced)


# ---------------------------------------------------------------------------
# GeminiAIProviderTests – parse robustness (end-to-end via analyse())
# ---------------------------------------------------------------------------

class GeminiParseRobustnessTests(unittest.TestCase):
    """End-to-end tests for the new parsing paths in GeminiAIProvider.analyse().

    All calls go through the real analyse() code but with the genai client mocked.
    """

    def setUp(self):
        self.provider = _make_gemini_provider()

    def _set_response_text(self, text: str) -> None:
        resp = MagicMock()
        resp.text = text
        self.provider._client.models.generate_content.return_value = resp

    def test_fenced_json_response_is_parsed_correctly(self):
        """Model wraps its output in ```json fences — must still be handled."""
        self._set_response_text(
            '```json\n'
            '{"matched_source_task_id": "T101", "status": "completed", '
            '"progress_percent": 100, "delay_days": null, "delay_reason": null, '
            '"actual_start_date": null, "actual_end_date": null, '
            '"confidence_score": 85, "reasoning": "Clearly done."}\n'
            '```'
        )
        result = self.provider.analyse(
            raw_update="Site preparation completed.",
            location="Well Pad A",
            reported_on="2026-09-07",
            candidate_tasks=SAMPLE_TASKS_FOR_GEMINI,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.progress_percent, 100.0)
        self.assertEqual(result.matched_task_id, SAMPLE_TASK_ID)
        self.assertGreaterEqual(result.confidence_score, 80.0)
        self.assertLessEqual(result.confidence_score, 100.0)


    def test_fenced_json_no_language_tag_parsed(self):
        """Model uses ``` without json tag — must still be handled."""
        self._set_response_text(
            '```\n{"status": "in_progress", "confidence_score": 60, '
            '"matched_source_task_id": null, "progress_percent": null, '
            '"delay_days": null, "delay_reason": null, '
            '"actual_start_date": null, "actual_end_date": null, '
            '"reasoning": "Ongoing."}\n```'
        )
        result = self.provider.analyse(
            raw_update="Work is ongoing.",
            location="Well Pad A",
            reported_on="2026-09-07",
            candidate_tasks=[],
        )
        self.assertEqual(result.status, "in_progress")
        # Composite confidence with no candidates and no deterministic match will be
        # lower than raw LLM score of 60 — the key invariant is it's within [0, 100].
        self.assertGreater(result.confidence_score, 0.0)
        self.assertLessEqual(result.confidence_score, 100.0)

    def test_truncated_json_returns_unknown_result(self):
        """The exact U002 failure: truncated JSON → parse error → zero-confidence fallback,
        NOT a crash. Previously this would raise and surface as a 500 error."""
        truncated = (
            '{"status": "in_progress", "matched_source_task_id": "T101", '
            '"reasoning": "Work is ongoing at the site and crews are'
            # truncated here — no closing quote, brace, etc.
        )
        self._set_response_text(truncated)
        result = self.provider.analyse(
            raw_update="Work ongoing.",
            location="Well Pad A",
            reported_on="2026-09-07",
            candidate_tasks=SAMPLE_TASKS_FOR_GEMINI,
        )
        # Must not crash; must return a safe fallback
        self.assertEqual(result.confidence_score, 0.0)
        self.assertIsNone(result.status)
        self.assertIsNone(result.matched_task_id)
        self.assertIn("error", result.model_response)

    def test_empty_response_returns_unknown_result(self):
        """Empty response text → ValueError in _extract_json → fallback."""
        self._set_response_text("")
        result = self.provider.analyse(
            raw_update="Site update.", location="X", reported_on="2026-09-07",
            candidate_tasks=[],
        )
        self.assertEqual(result.confidence_score, 0.0)
        self.assertIn("error", result.model_response)

    def test_api_error_returns_unknown_result(self):
        """Network error during API call → fallback (unchanged from before)."""
        self.provider._client.models.generate_content.side_effect = Exception("Timeout")
        result = self.provider.analyse(
            raw_update="Update.", location="X", reported_on="2026-09-07",
            candidate_tasks=[],
        )
        self.assertEqual(result.confidence_score, 0.0)
        self.assertIn("error", result.model_response)


# ---------------------------------------------------------------------------
# Gemini failure diagnostic and multi-activity regression tests (Tests A-F)
# ---------------------------------------------------------------------------

class GeminiProductionDiagnosticRegressionTests(unittest.TestCase):
    """Regression tests covering production UPD-959491 failure diagnostics,
    secret redaction, and multi-activity isolation.
    """

    def setUp(self):
        self.provider = _make_gemini_provider()

    @patch("time.sleep")
    def test_gemini_quota_rate_limit_diagnostic_and_secret_redaction(self, mock_sleep):
        """Test A: Exact reproduction of UPD-959491 production 429 quota failure.
        - Mocks Gemini returning 429 RESOURCE_EXHAUSTED with sensitive API key in error text.
        - Verifies structured failure diagnostic is emitted.
        - Verifies secrets are sanitized/redacted from both error and diagnostic.
        - Verifies retry count is 3 and error_class is recorded.
        """
        raw_error = (
            "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 20, model: gemini-3.6-flash. Please retry in 42.447211746s. "
            "url: https://generativelanguage.googleapis.com/v1beta/models?key=AIzaSyD-secretKey9876543210"
        )
        self.provider._client.models.generate_content.side_effect = Exception(raw_error)

        result = self.provider.analyse(
            raw_update="Daily pipeline construction report: Route survey complete.",
            location="Pipeline Section Y",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        self.assertEqual(result.confidence_score, 0.0)
        self.assertIsNone(result.matched_task_id)
        self.assertIsNone(result.status)
        self.assertEqual(len(result.additional_observations), 0)

        diag = result.model_response.get("failure_diagnostic")
        self.assertIsNotNone(diag, "failure_diagnostic must be present in model_response")
        self.assertEqual(diag["failure_type"], "rate_limit")
        self.assertEqual(diag["provider"], "gemini")
        # Quota delay of 42.45s exceeds sensible cap (10s), so retry loop aborts immediately
        # without blocking the request, leaving retry_attempts at 1
        self.assertEqual(diag["retry_attempts"], 1)
        self.assertAlmostEqual(diag.get("retry_delay", 0.0), 42.447, places=2)
        mock_sleep.assert_not_called()
        self.assertEqual(diag["error_class"], "Exception")
        self.assertFalse(diag["raw_response_available"])

        # Secret redaction check
        self.assertNotIn("AIzaSyD-secretKey9876543210", diag["error_message"])
        self.assertNotIn("secretKey9876543210", diag["error_message"])
        self.assertIn("[REDACTED]", diag["error_message"])
        self.assertNotIn("AIzaSyD-secretKey9876543210", result.model_response.get("error", ""))
        self.assertIn("[REDACTED]", result.model_response.get("error", ""))
        self.assertIn("429 RESOURCE_EXHAUSTED", diag["error_message"])

    @patch("time.sleep")
    def test_gemini_transient_rate_limit_with_short_retry_delay_retries(self, mock_sleep):
        """Short provider-advised retry delay (<= 10s) is respected and retried."""
        short_err = "429 RESOURCE_EXHAUSTED. Rate limit exceeded. Please retry in 1.5s."
        self.provider._client.models.generate_content.side_effect = Exception(short_err)

        result = self.provider.analyse(
            raw_update="Survey underway.",
            location="Zone B",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        self.assertEqual(result.confidence_score, 0.0)
        diag = result.model_response.get("failure_diagnostic")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["failure_type"], "rate_limit")
        self.assertEqual(diag["retry_attempts"], 3)
        self.assertAlmostEqual(diag.get("retry_delay", 0.0), 1.5, places=1)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(1.5)

    @patch("time.sleep")
    def test_gemini_retry_after_header_respected(self, mock_sleep):
        """Retry-After header from response object is parsed and respected."""
        mock_exc = Exception("429 Too Many Requests")
        mock_resp = MagicMock()
        mock_resp.headers = {"Retry-After": "2.5"}
        mock_exc.response = mock_resp
        self.provider._client.models.generate_content.side_effect = mock_exc

        result = self.provider.analyse(
            raw_update="Survey underway.",
            location="Zone B",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        diag = result.model_response.get("failure_diagnostic")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["failure_type"], "rate_limit")
        self.assertAlmostEqual(diag.get("retry_delay", 0.0), 2.5, places=1)
        mock_sleep.assert_called_with(2.5)

    @patch("time.sleep")
    def test_gemini_rpc_retry_info_respected(self, mock_sleep):
        """google.rpc.RetryInfo details dictionary is parsed and respected."""
        mock_exc = Exception("429 Quota Exceeded")
        mock_exc.details = {
            "error": {
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3.5s"}
                ]
            }
        }
        self.provider._client.models.generate_content.side_effect = mock_exc

        result = self.provider.analyse(
            raw_update="Survey underway.",
            location="Zone B",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        diag = result.model_response.get("failure_diagnostic")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["failure_type"], "rate_limit")
        self.assertAlmostEqual(diag.get("retry_delay", 0.0), 3.5, places=1)
        mock_sleep.assert_called_with(3.5)

    def test_gemini_model_resolution_and_default(self):
        """GeminiAIProvider resolves model from parameter, environment, or default gemini-3.6-flash."""
        p_default = _make_gemini_provider()
        self.assertEqual(getattr(p_default, "_model_name", None), "gemini-3.6-flash")

        p_custom = GeminiAIProvider.__new__(GeminiAIProvider)
        p_custom._model_name = "gemini-3.8-flash"
        self.assertEqual(p_custom._model_name, "gemini-3.8-flash")

    @patch("time.sleep")
    def test_gemini_network_timeout_produces_structured_diagnostic(self, mock_sleep):
        """Test B: Mock Gemini returning network timeout.
        - Verifies failure_type is 'timeout'.
        - Verifies retry count is 3.
        - Verifies error details are populated.
        """
        self.provider._client.models.generate_content.side_effect = TimeoutError("Request timed out after 30000ms")

        result = self.provider.analyse(
            raw_update="Trench excavation progressing.",
            location="Pipeline Section Y",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        self.assertEqual(result.confidence_score, 0.0)
        diag = result.model_response.get("failure_diagnostic")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["failure_type"], "timeout")
        self.assertEqual(diag["retry_attempts"], 3)
        self.assertEqual(diag["error_class"], "TimeoutError")
        self.assertIn("timed out", diag["error_message"].lower())

    def test_gemini_empty_response_produces_structured_diagnostic(self):
        """Test C: Mock Gemini returning empty string / blank response.
        - Verifies failure_type is 'empty_response'.
        - Verifies raw_response_available is False.
        """
        resp = MagicMock()
        resp.text = "   \n  "
        self.provider._client.models.generate_content.return_value = resp

        result = self.provider.analyse(
            raw_update="Pipe stringing complete.",
            location="Pipeline Section Y",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        self.assertEqual(result.confidence_score, 0.0)
        diag = result.model_response.get("failure_diagnostic")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["failure_type"], "empty_response")
        self.assertIn("empty response", diag["error_message"].lower())

    def test_gemini_truncated_malformed_json_preserves_raw_response(self):
        """Test D: Mock Gemini returning truncated / malformed JSON.
        - Verifies raw_response is preserved.
        - Verifies failure_type is 'invalid_json'.
        - Verifies raw_response_available is True.
        """
        malformed_raw = '{"observations": [{"activity": "Route Survey", "progress_percent": 100, "status": "comp'
        resp = MagicMock()
        resp.text = malformed_raw
        self.provider._client.models.generate_content.return_value = resp

        result = self.provider.analyse(
            raw_update="Route survey complete.",
            location="Pipeline Section Y",
            reported_on="2026-11-18",
            candidate_tasks=[],
        )

        self.assertEqual(result.confidence_score, 0.0)
        diag = result.model_response.get("failure_diagnostic")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["failure_type"], "invalid_json")
        self.assertTrue(diag["raw_response_available"])
        self.assertEqual(result.model_response.get("raw_response"), malformed_raw)

    def test_gemini_valid_multi_activity_seven_plus_observations_preserved(self):
        """Test E: Valid multi-activity response with 7+ observations from Gemini.
        - Verifies all observations are parsed and retained.
        - Verifies none are lost before matching.
        """
        nine_obs_payload = {
            "observations": [
                {
                    "activity": "Route survey and marking",
                    "status": "completed",
                    "progress_percent": 100,
                    "actual_end_date": "2026-11-07",
                    "confidence_score": 95,
                    "matched_source_task_id": "PX101",
                },
                {
                    "activity": "Right of way clearing",
                    "status": "completed",
                    "progress_percent": 100,
                    "actual_end_date": "2026-11-11",
                    "confidence_score": 95,
                    "matched_source_task_id": "PX102",
                },
                {
                    "activity": "Trench excavation",
                    "status": "completed",
                    "progress_percent": 100,
                    "actual_end_date": "2026-11-17",
                    "confidence_score": 95,
                    "matched_source_task_id": "PX103",
                },
                {
                    "activity": "Sand bedding",
                    "status": "in_progress",
                    "progress_percent": 80,
                    "confidence_score": 90,
                    "matched_source_task_id": "PX104",
                },
                {
                    "activity": "Pipe stringing",
                    "status": "completed",
                    "progress_percent": 100,
                    "confidence_score": 95,
                    "matched_source_task_id": "PX105",
                },
                {
                    "activity": "Pipeline welding",
                    "status": "in_progress",
                    "progress_percent": 35,
                    "delay_days": 1,
                    "delay_reason": "heavy rainfall",
                    "confidence_score": 90,
                    "matched_source_task_id": "PX106",
                },
                {
                    "activity": "Weld inspection and NDT",
                    "status": "not_started",
                    "progress_percent": None,
                    "confidence_score": 90,
                    "matched_source_task_id": "PX107",
                },
                {
                    "activity": "Hydrotesting",
                    "status": "not_started",
                    "progress_percent": None,
                    "confidence_score": 85,
                    "matched_source_task_id": "PX108",
                },
                {
                    "activity": "Backfilling and restoration",
                    "status": "not_started",
                    "progress_percent": None,
                    "confidence_score": 85,
                    "matched_source_task_id": "PX109",
                },
            ]
        }
        candidate_tasks = [
            {"id": f"task-uuid-{i}", "source_task_id": f"PX10{i}", "activity": f"Activity {i}", "location": "Pipeline Section Y"}
            for i in range(1, 10)
        ]

        resp = MagicMock()
        resp.text = json.dumps(nine_obs_payload)
        self.provider._client.models.generate_content.return_value = resp

        result = self.provider.analyse(
            raw_update="Daily pipeline construction report covering 9 activities.",
            location="Pipeline Section Y",
            reported_on="2026-11-18",
            candidate_tasks=candidate_tasks,
        )

        all_observations = [result] + list(result.additional_observations)
        self.assertEqual(len(all_observations), 9, "All 9 observations must be preserved")

        self.assertEqual(all_observations[0].status, "completed")
        self.assertEqual(all_observations[0].progress_percent, 100.0)
        self.assertEqual(all_observations[0].actual_end_date, "2026-11-07")
        self.assertEqual(all_observations[0].matched_task_id, "task-uuid-1")

        self.assertEqual(all_observations[3].status, "in_progress")
        self.assertEqual(all_observations[3].progress_percent, 80.0)
        self.assertEqual(all_observations[3].candidate_source_task_id, "PX104")

        self.assertEqual(all_observations[5].delay_days, 1)
        self.assertEqual(all_observations[5].delay_reason, "heavy rainfall")
        self.assertEqual(all_observations[5].candidate_source_task_id, "PX106")

        self.assertEqual(all_observations[6].status, "not_started")
        self.assertIsNone(all_observations[6].progress_percent)
        self.assertEqual(all_observations[6].candidate_source_task_id, "PX107")

    def test_gemini_one_ambiguous_observation_does_not_block_high_confidence(self):
        """Test F: Multi-activity response where 1 observation is ambiguous or missing task match.
        - Verifies other observations continue to match and persist.
        - Verifies no single failure causes all observations to disappear or fail.
        """
        payload = {
            "observations": [
                {
                    "activity": "Foundation Reinforcement",
                    "status": "completed",
                    "progress_percent": 100,
                    "confidence_score": 95,
                    "matched_source_task_id": "MC104",
                },
                {
                    "activity": "Random unidentifiable maintenance work",
                    "status": "in_progress",
                    "progress_percent": 10,
                    "confidence_score": 30,
                    "matched_source_task_id": None,
                },
                {
                    "activity": "Equipment Foundation Concrete",
                    "status": "in_progress",
                    "progress_percent": 60,
                    "confidence_score": 90,
                    "matched_source_task_id": "MC105",
                },
            ]
        }
        candidate_tasks = [
            {"id": "uuid-mc104", "source_task_id": "MC104", "activity": "Foundation Reinforcement", "location": "Unit 1"},
            {"id": "uuid-mc105", "source_task_id": "MC105", "activity": "Equipment Foundation Concrete", "location": "Unit 1"},
        ]

        resp = MagicMock()
        resp.text = json.dumps(payload)
        self.provider._client.models.generate_content.return_value = resp

        result = self.provider.analyse(
            raw_update="Foundation reinforcement 100%, random work 10%, equipment foundation concrete 60%.",
            location="Unit 1",
            reported_on="2026-11-18",
            candidate_tasks=candidate_tasks,
        )

        all_obs = [result] + list(result.additional_observations)
        self.assertEqual(len(all_obs), 3)

        # Observation 0 (primary AnalysisResult): High confidence, matched to MC104
        self.assertEqual(all_obs[0].matched_task_id, "uuid-mc104")
        self.assertEqual(all_obs[0].status, "completed")
        self.assertGreaterEqual(all_obs[0].confidence_score, 80.0)

        # Observation 1 (ActivityObservation): Unmatched / low confidence, but does NOT kill obs 0 or 2
        self.assertIsNone(all_obs[1].candidate_source_task_id)
        self.assertLess(all_obs[1].extraction_confidence, 50.0)

        # Observation 2 (ActivityObservation): High confidence, matched candidate MC105
        self.assertEqual(all_obs[2].candidate_source_task_id, "MC105")
        self.assertEqual(all_obs[2].status, "in_progress")
        self.assertEqual(all_obs[2].progress_percent, 60.0)
        self.assertGreaterEqual(all_obs[2].extraction_confidence, 80.0)



# ---------------------------------------------------------------------------
# Safe parsing helper tests
# ---------------------------------------------------------------------------

class SafeParsingHelperTests(unittest.TestCase):
    """Unit tests for the parsing utility functions."""

    def test_safe_str_strips_whitespace(self):
        self.assertEqual(_safe_str("  hello  "), "hello")

    def test_safe_str_none_input(self):
        self.assertIsNone(_safe_str(None))

    def test_safe_str_null_string(self):
        self.assertIsNone(_safe_str("null"))

    def test_safe_str_empty_string(self):
        self.assertIsNone(_safe_str(""))

    def test_safe_float_valid(self):
        self.assertEqual(_safe_float("75.5"), 75.5)

    def test_safe_float_invalid(self):
        self.assertIsNone(_safe_float("not-a-number"))

    def test_safe_float_none(self):
        self.assertIsNone(_safe_float(None))

    def test_safe_int_valid(self):
        self.assertEqual(_safe_int("3"), 3)

    def test_safe_int_invalid(self):
        self.assertIsNone(_safe_int("three"))

    def test_safe_int_none(self):
        self.assertIsNone(_safe_int(None))

    def test_safe_iso_date_valid(self):
        self.assertEqual(_safe_iso_date("2026-09-05"), "2026-09-05")

    def test_safe_iso_date_wrong_format(self):
        self.assertIsNone(_safe_iso_date("05-Sep-2026"))

    def test_safe_iso_date_none(self):
        self.assertIsNone(_safe_iso_date(None))

    def test_safe_iso_date_slash_format(self):
        self.assertIsNone(_safe_iso_date("2026/09/05"))


# ---------------------------------------------------------------------------
# Task actuals accumulation & planned dates protection tests
# ---------------------------------------------------------------------------

class TaskActualsAccumulationTests(unittest.TestCase):
    """Verify that multiple updates on a task accumulate actuals properly:
    1. A field with UNKNOWN/NULL does NOT overwrite an existing known value.
    2. Planned start/end baseline dates are NEVER modified.
    3. New valid non-null values update progress, status, and actual dates.
    """

    def test_null_actual_date_does_not_overwrite_existing(self):
        """If update 2 mentions no start date, the existing start date must be preserved."""
        update_row = _make_update_row(raw_update="Foundation work is 70% complete. 2 days delay.")
        result_row = _make_result_row()
        mock_db = _mock_db_for_process(update_row, [], result_row)

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=SAMPLE_TASK_ID,
            progress_percent=70.0,
            status="delayed",
            delay_days=2,
            delay_reason="Rain",
            actual_start_date=None,  # UNKNOWN in this update
            actual_end_date=None,    # UNKNOWN in this update
            confidence_score=82.0,   # >= 80.0 → schedule update triggered
            model_name="mock-keyword-v1",
            model_response={},
        )

        process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        # Verify the update query parameters
        update_call = mock_db.execute.call_args_list[4]
        params = update_call[0][1]

        # COALESCE(:actual_start_date, actual_start_date): param is None so DB retains existing
        self.assertIsNone(params["actual_start_date"])
        self.assertEqual(params["progress_percent"], 70.0)
        self.assertEqual(params["status"], "delayed")
        self.assertEqual(params["delay_days"], 2)

        # Baseline planned dates are NEVER in the update params
        self.assertNotIn("planned_start", params)
        self.assertNotIn("planned_end", params)


    def test_completion_update_sets_end_date_and_preserves_start_date(self):
        """When completion date is reported, actual_end_date is set while start date is retained."""
        update_row = _make_update_row(raw_update="Foundation work completed on 2026-09-08.")
        result_row = _make_result_row()
        mock_db = _mock_db_for_process(update_row, [], result_row)

        provider = MagicMock(spec=AIProvider)
        provider.analyse.return_value = AnalysisResult(
            matched_task_id=SAMPLE_TASK_ID,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date="2026-09-08",
            confidence_score=85.0,
            model_name="mock-keyword-v1",
            model_response={},
        )

        process_site_update(mock_db, SAMPLE_UPDATE_ID, provider=provider)

        update_call = mock_db.execute.call_args_list[4]
        params = update_call[0][1]

        self.assertEqual(params["status"], "completed")
        self.assertEqual(params["progress_percent"], 100.0)
        self.assertEqual(params["actual_end_date"], "2026-09-08")
        self.assertIsNone(params["actual_start_date"])
        self.assertNotIn("planned_start", params)
        self.assertNotIn("planned_end", params)

    def test_update_task_actuals_sql_structure(self):
        """_UPDATE_TASK_ACTUALS SQL must contain protections for date order, progress, and completed status."""
        sql_str = str(_UPDATE_TASK_ACTUALS)

        # Baseline planned dates must NEVER be modified
        self.assertNotIn("planned_start =", sql_str)
        self.assertNotIn("planned_end =", sql_str)

        # Monotonic progress: incoming only updates if strictly higher
        self.assertIn(":progress_percent > progress_percent", sql_str)

        # Completed status protection: completed status is NEVER downgraded
        self.assertIn("WHEN status = 'completed' THEN 'completed'", sql_str)

        # Date order protection: older updates cannot overwrite newer status or delay
        self.assertIn("CAST(:reported_on AS DATE) >= last_reported_on", sql_str)
        self.assertIn("CAST(:reported_on AS DATE) < last_reported_on", sql_str)

        # Earliest actual start date preservation
        self.assertIn("CAST(:actual_start_date AS DATE) < actual_start_date", sql_str)

    def test_older_update_logic_simulation(self):
        """Verify the exact update rules when an older update is processed after a newer update."""
        def apply_update(existing, incoming):
            # Implements the CASE semantics of _UPDATE_TASK_ACTUALS in Python
            res = dict(existing)
            rep_incoming = incoming.get("reported_on")
            rep_existing = existing.get("last_reported_on")

            is_older = (
                rep_existing is not None
                and rep_incoming is not None
                and rep_incoming < rep_existing
            )

            # last_reported_on
            if rep_existing is None or (rep_incoming and rep_incoming >= rep_existing):
                res["last_reported_on"] = rep_incoming

            # progress_percent (monotonic, higher progress wins)
            p_in = incoming.get("progress_percent")
            p_ex = existing.get("progress_percent")
            if p_in is not None:
                if p_ex is None or p_in > p_ex:
                    res["progress_percent"] = p_in

            # status
            s_in = incoming.get("status")
            s_ex = existing.get("status")
            if s_ex == "completed":
                res["status"] = "completed"
            elif is_older and s_ex is not None:
                res["status"] = s_ex
            elif s_in is not None:
                res["status"] = s_in

            # delay_days & delay_reason
            if not is_older or existing.get("delay_days") is None:
                if incoming.get("delay_days") is not None:
                    res["delay_days"] = incoming.get("delay_days")
            if not is_older or existing.get("delay_reason") is None:
                if incoming.get("delay_reason") is not None:
                    res["delay_reason"] = incoming.get("delay_reason")

            # actual_start_date (earliest wins, fills if null)
            start_in = incoming.get("actual_start_date")
            start_ex = existing.get("actual_start_date")
            if start_ex is None:
                res["actual_start_date"] = start_in
            elif start_in is not None and start_in < start_ex:
                res["actual_start_date"] = start_in

            # actual_end_date
            end_in = incoming.get("actual_end_date")
            end_ex = existing.get("actual_end_date")
            if end_ex is not None:
                res["actual_end_date"] = end_ex
            elif not is_older and end_in is not None:
                res["actual_end_date"] = end_in

            return res

        # Existing task state from update on 2026-09-08: 40% progress, delayed 1 day
        current_state = {
            "last_reported_on": "2026-09-08",
            "progress_percent": 40.0,
            "status": "delayed",
            "delay_days": 1,
            "delay_reason": "Rain",
            "actual_start_date": None,
            "actual_end_date": None,
        }

        # Incoming older update from 2026-09-02: 20% progress, in_progress, 0 delay, start date 2026-09-02
        older_update = {
            "reported_on": "2026-09-02",
            "progress_percent": 20.0,
            "status": "in_progress",
            "delay_days": 0,
            "delay_reason": None,
            "actual_start_date": "2026-09-02",
            "actual_end_date": None,
        }

        result = apply_update(current_state, older_update)

        # 1. Higher progress is preserved (40% is NOT overwritten by 20%)
        self.assertEqual(result["progress_percent"], 40.0)

        # 2. Newer status is preserved ('delayed' is NOT overwritten by 'in_progress')
        self.assertEqual(result["status"], "delayed")

        # 3. Newer delay info is preserved (1 day is NOT overwritten by 0)
        self.assertEqual(result["delay_days"], 1)
        self.assertEqual(result["delay_reason"], "Rain")

        # 4. last_reported_on remains the newer date
        self.assertEqual(result["last_reported_on"], "2026-09-08")

        # 5. Missing start date is safely populated from the historical update
        self.assertEqual(result["actual_start_date"], "2026-09-02")

    def test_lower_progress_does_not_overwrite_higher_progress_simulation(self):
        """An update on a newer date reporting lower progress does not regress task progress."""
        current_state = {
            "last_reported_on": "2026-09-08",
            "progress_percent": 70.0,
            "status": "in_progress",
        }
        # Erroneously reported 30% on 2026-09-10
        newer_lower = {
            "reported_on": "2026-09-10",
            "progress_percent": 30.0,
            "status": "in_progress",
        }
        p_in = newer_lower["progress_percent"]
        p_ex = current_state["progress_percent"]
        progress_after = p_in if (p_ex is None or p_in > p_ex) else p_ex
        self.assertEqual(progress_after, 70.0, "Progress must never regress to a lower value")

    def test_completed_task_not_downgraded_simulation(self):
        """A completed task cannot be downgraded to in_progress or not_started."""
        current_status = "completed"
        incoming_status = "in_progress"
        status_after = "completed" if current_status == "completed" else incoming_status
        self.assertEqual(status_after, "completed")


class RegressionScenario1MatchingTests(unittest.TestCase):
    """Specific regression tests for Scenario 1:
    Schedule:
    - P101: Land Clearing
    - P102: Excavation Work
    - P103: Foundation Casting
    - P104: Equipment Installation
    All at 'Test Site'.
    Update: 'Excavation work at Test Site is approximately 60% complete. Heavy rainfall caused a two-day delay.'
    Expected:
    - P102 is identified with high confidence (>= 80%)
    - Auto-linked and updated (progress=60%, status='delayed', delay_days=2, delay_reason extracted)
    - Not left as pending review
    """

    def setUp(self):
        self.p101_id = "11111111-1111-1111-1111-111111111101"
        self.p102_id = "11111111-1111-1111-1111-111111111102"
        self.p103_id = "11111111-1111-1111-1111-111111111103"
        self.p104_id = "11111111-1111-1111-1111-111111111104"

        self.candidate_tasks = [
            {
                "id": self.p101_id,
                "source_task_id": "P101",
                "activity": "Land Clearing",
                "location": "Test Site",
                "planned_start": "2026-09-01",
                "planned_end": "2026-09-05",
            },
            {
                "id": self.p102_id,
                "source_task_id": "P102",
                "activity": "Excavation Work",
                "location": "Test Site",
                "planned_start": "2026-09-06",
                "planned_end": "2026-09-12",
            },
            {
                "id": self.p103_id,
                "source_task_id": "P103",
                "activity": "Foundation Casting",
                "location": "Test Site",
                "planned_start": "2026-09-13",
                "planned_end": "2026-09-20",
            },
            {
                "id": self.p104_id,
                "source_task_id": "P104",
                "activity": "Equipment Installation",
                "location": "Test Site",
                "planned_start": "2026-09-21",
                "planned_end": "2026-09-30",
            },
        ]
        self.provider = MockAIProvider()
        self.raw_update = "Excavation work at Test Site is approximately 60% complete. Heavy rainfall caused a two-day delay."

    def test_p102_matched_with_high_confidence_and_extracted_facts(self):
        """P102 Excavation Work is matched with high confidence (>= 80%), 60% progress, 2-day delay, delayed status."""
        result = self.provider.analyse(
            raw_update=self.raw_update,
            location="Test Site",
            reported_on="2026-09-08",
            candidate_tasks=self.candidate_tasks,
        )

        self.assertEqual(result.matched_task_id, self.p102_id)
        self.assertGreaterEqual(result.confidence_score, 80.0)
        self.assertEqual(result.progress_percent, 60.0)
        self.assertEqual(result.status, "delayed")
        self.assertEqual(result.delay_days, 2)
        self.assertIsNotNone(result.delay_reason)
        assert result.delay_reason is not None
        self.assertTrue(
            "rainfall" in result.delay_reason.lower() or "rain" in result.delay_reason.lower() or "delay" in result.delay_reason.lower()
        )
        self.assertEqual(result.model_response.get("confidence_tier"), "high")

    def test_location_tolerance_when_default_location_passed(self):
        """Even if location was ingested as 'Well Pad A', the text contains 'Test Site' and matches P102."""
        result = self.provider.analyse(
            raw_update=self.raw_update,
            location="Well Pad A",
            reported_on="2026-09-08",
            candidate_tasks=self.candidate_tasks,
        )
        self.assertEqual(result.matched_task_id, self.p102_id)
        self.assertGreaterEqual(result.confidence_score, 80.0)

    def test_process_site_update_auto_links_and_updates_actuals(self):
        """High confidence match triggers update_schedule_task_actuals in process_site_update."""
        import types
        db = MagicMock()
        update_row = MagicMock()
        update_row.id = uuid.UUID("33333333-3333-3333-3333-333333333333")
        update_row.location = "Test Site"
        update_row.raw_update = self.raw_update
        update_row.reported_on = datetime.date(2026, 9, 8)

        inserted_mock = MagicMock()
        inserted_mock.id = "44444444-4444-4444-4444-444444444444"
        inserted_mock.site_update_id = str(update_row.id)
        inserted_mock.matched_task_id = self.p102_id
        inserted_mock.progress_percent = 60.0
        inserted_mock.status = "delayed"
        inserted_mock.delay_days = 2
        inserted_mock.delay_reason = "Heavy rainfall caused a two-day delay."
        inserted_mock.actual_start_date = None
        inserted_mock.actual_end_date = None
        inserted_mock.confidence_score = 95.0
        inserted_mock.model_name = "mock-keyword-v1"
        inserted_mock.model_response = json.dumps({"confidence_tier": "high"})
        inserted_mock.processed_at = "2026-09-08T10:00:00"
        inserted_mock.is_current = True

        task_mock_rows = [
            types.SimpleNamespace(
                id=t["id"],
                source_task_id=t["source_task_id"],
                activity=t["activity"],
                location=t["location"],
                planned_start=t["planned_start"],
                planned_end=t["planned_end"],
                status="not_started",
            )
            for t in self.candidate_tasks
        ]

        def db_execute(stmt, params=None):
            sql_str = str(stmt)
            mock_res = MagicMock()
            if "FROM site_updates" in sql_str:
                mock_res.fetchone.return_value = update_row
            elif "FROM schedule_tasks" in sql_str:
                mock_res.fetchall.return_value = task_mock_rows
            elif "INSERT INTO ai_processed_updates" in sql_str:
                mock_res.fetchone.return_value = inserted_mock
            return mock_res

        db_execute_mock = db_execute
        db.execute.side_effect = db_execute_mock

        res = process_site_update(db, str(update_row.id), provider=self.provider)
        self.assertEqual(res["matched_task_id"], self.p102_id)
        self.assertGreaterEqual(res["confidence_score"], 80.0)

        # Check that UPDATE schedule_tasks actuals was executed
        executed_sqls = [str(call_args[0][0]) for call_args in db.execute.call_args_list]
        update_task_calls = [s for s in executed_sqls if "UPDATE schedule_tasks" in s]
        self.assertEqual(len(update_task_calls), 1)



# ---------------------------------------------------------------------------
# Realistic Matching Regression Tests
# ---------------------------------------------------------------------------

class RealisticMatchingTests(unittest.TestCase):
    """Regression tests for the 8 messy real-world inputs from the approved
    implementation plan.  Uses MockAIProvider with a representative schedule.

    Tests verify routing (auto-link / planner-review / unmatched) and basic
    extraction accuracy.  Exact confidence values are NOT asserted — only
    tier and directional correctness.
    """

    REPORTED_ON = "2026-09-10"

    TASKS = [
        {"id": "t1", "source_task_id": "P101", "activity": "Right of Way Clearance",  "location": "Well Pad A",           "planned_start": "2026-08-01", "planned_end": "2026-08-20"},
        {"id": "t2", "source_task_id": "P102", "activity": "Excavation Work",          "location": "Well Pad A",           "planned_start": "2026-08-21", "planned_end": "2026-09-10"},
        {"id": "t3", "source_task_id": "P103", "activity": "Pipeline Laying",          "location": "Well Pad A",           "planned_start": "2026-09-11", "planned_end": "2026-09-30"},
        {"id": "t4", "source_task_id": "P104", "activity": "Welding",                 "location": "Well Pad A",           "planned_start": "2026-09-15", "planned_end": "2026-10-05"},
        {"id": "t5", "source_task_id": "P105", "activity": "Site Preparation",        "location": "Compressor Station B", "planned_start": "2026-08-15", "planned_end": "2026-09-05"},
        {"id": "t6", "source_task_id": "P106", "activity": "Foundation Construction", "location": "Compressor Station B", "planned_start": "2026-09-06", "planned_end": "2026-09-20"},
    ]

    def setUp(self):
        self.provider = MockAIProvider()

    def _analyse(self, text: str, location: str = "Well Pad A") -> AnalysisResult:
        return self.provider.analyse(
            raw_update=text,
            location=location,
            reported_on=self.REPORTED_ON,
            candidate_tasks=self.TASKS,
        )

    def test_welding_informal_matches_p104(self):
        """'guys finished the welding today at well pad A' → P104 Welding, high confidence."""
        r = self._analyse("guys finished the welding today at well pad A")
        self.assertEqual(r.matched_task_id, "t4")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.model_response.get("confidence_tier"), "high")
        self.assertEqual(r.status, "completed")
        self.assertEqual(r.progress_percent, 100.0)

    def test_row_abbreviation_matches_p101(self):
        """'ROW clearing 80% done, WPA' → P101 Right of Way, high confidence with alias."""
        r = self._analyse("ROW clearing 80% done, WPA")
        self.assertEqual(r.matched_task_id, "t1")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.progress_percent, 80.0)
        self.assertEqual(r.status, "in_progress")

    def test_typo_excavation_matches_p102(self):
        """'excvation work complet at WPA' → P102 Excavation (alias + typo fix)."""
        r = self._analyse("excvation work complet at WPA")
        self.assertEqual(r.matched_task_id, "t2")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.model_response.get("confidence_tier"), "high")
        self.assertEqual(r.status, "completed")
        self.assertEqual(r.progress_percent, 100.0)

    def test_multi_activity_ambiguous_routes_to_review(self):
        """'pipeline laid + welding done at WPA section 2' → matched but not auto-linked (medium tier)."""
        r = self._analyse("pipeline laid + welding done at WPA section 2")
        self.assertEqual(r.model_response.get("confidence_tier"), "medium")
        self.assertIn(r.matched_task_id, ("t3", "t4"))
        add_obs = r.model_response.get("additional_observations") or []
        self.assertGreaterEqual(len(add_obs), 1)

    def test_irrelevant_update_gives_no_match(self):
        """'lunch break at 1pm' → no match (not a construction activity)."""
        r = self._analyse("lunch break at 1pm", location="")
        self.assertIsNone(r.matched_task_id)
        self.assertLess(r.confidence_score, 50.0)

    def test_row_delay_matched_with_delay_extracted(self):
        """'ROW clearing delayed by 2 days due to rain' → P101, delay=2 extracted."""
        r = self._analyse("ROW clearing delayed by 2 days due to rain")
        self.assertEqual(r.matched_task_id, "t1")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.delay_days, 2)
        self.assertEqual(r.status, "delayed")

    def test_hinglish_excavation_matched(self):
        """'aaj excavation almost complete hai, kal bedding start karenge' → P102 in_progress with no hallucinated %."""
        r = self._analyse("aaj excavation almost complete hai, kal bedding start karenge")
        self.assertEqual(r.matched_task_id, "t2")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.status, "in_progress")
        self.assertIsNone(r.progress_percent)

    def test_alias_normalization_row_in_raw_text(self):
        """Verify ROW → Right of Way Clearance alias is active in matching pipeline."""
        r = self._analyse("ROW is 90% complete", location="Well Pad A")
        self.assertEqual(r.matched_task_id, "t1")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.progress_percent, 90.0)

    def test_multi_sentence_progress_report(self):
        """Multi-sentence field report: narrative surrounding extraction facts."""
        text = (
            "Crews arrived at Well Pad A at 7:00 AM. Excavation work proceeded steadily "
            "throughout the morning shift. As of 3 PM, excavation is 60% complete with no safety incidents."
        )
        r = self._analyse(text, location="Well Pad A")
        self.assertEqual(r.matched_task_id, "t2")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.status, "in_progress")
        self.assertEqual(r.progress_percent, 60.0)

    def test_location_alias_in_reported_metadata(self):
        """Update metadata uses location abbreviation 'WPA' matching canonical 'Well Pad A' task."""
        r = self._analyse("Welding finished for the day.", location="WPA")
        self.assertEqual(r.matched_task_id, "t4")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.status, "completed")
        self.assertEqual(r.progress_percent, 100.0)

    def test_vague_progress_does_not_hallucinate_percentage(self):
        """'nearly complete' sets status in_progress and does NOT invent a percentage number."""
        r = self._analyse("Right of way clearing is nearly complete at Well Pad A.", location="Well Pad A")
        self.assertEqual(r.matched_task_id, "t1")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.status, "in_progress")
        self.assertIsNone(r.progress_percent)

    def test_explicit_completion_infers_100_percent(self):
        """Explicit completion statement without percentage infers 100% progress."""
        r = self._analyse("Pipeline laying completed today at Well Pad A.", location="Well Pad A")
        self.assertEqual(r.matched_task_id, "t3")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.status, "completed")
        self.assertEqual(r.progress_percent, 100.0)

    def test_explicit_actual_start_date_extracted(self):
        """Explicit start date is parsed into actual_start_date."""
        r = self._analyse("Welding started on 2026-09-02 at Well Pad A.", location="Well Pad A")
        self.assertEqual(r.matched_task_id, "t4")
        self.assertGreaterEqual(r.confidence_score, 80.0)
        self.assertEqual(r.actual_start_date, "2026-09-02")
        self.assertEqual(r.status, "in_progress")

    def test_unrelated_activity_routes_to_unmatched(self):
        """Unrelated non-construction activity with no matching schedule task yields low confidence and no match."""
        r = self._analyse("Catering crew set up the dining tent.", location="")
        self.assertIsNone(r.matched_task_id)
        self.assertLess(r.confidence_score, 50.0)

    def test_multi_activity_three_tasks_detection(self):
        """Update mentions excavation, bedding, and pipe stringing: routes to planner review with additional observations."""
        text = "excavation is complete, bedding is 80 percent and pipe stringing has started"
        r = self._analyse(text, location="Well Pad A")
        self.assertEqual(r.model_response.get("confidence_tier"), "medium")
        self.assertEqual(r.matched_task_id, "t2")
        add_obs = r.model_response.get("additional_observations") or []
        self.assertGreaterEqual(len(add_obs), 1)


if __name__ == "__main__":
    unittest.main()

