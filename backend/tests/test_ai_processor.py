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
)
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
        self.assertEqual(result.confidence_score, 85.0)
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
        """Confidence scores above 100 are clamped to 100."""
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
        self.assertEqual(result.confidence_score, 100.0)

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
        self.assertGreater(result.confidence_score, 0)

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
        self.assertEqual(result.confidence_score, 60.0)

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


if __name__ == "__main__":
    unittest.main()

