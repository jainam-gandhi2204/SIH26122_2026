"""Tests for Planner Review Queue (app/review_queue.py and API endpoints).

All tests are pure unit tests — mock DB sessions, no external dependencies.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app
from app.database import get_db
from app.review_queue import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    _determine_review_reason,
    _extract_activity_name,
    _find_suggested_match,
    _parse_model_response,
    get_review_queue,
)


def _row(**kwargs):
    """Return a simple namespace object representing a database query row."""
    return types.SimpleNamespace(**kwargs)


class ReviewQueueHelpersTests(unittest.TestCase):
    """Unit tests for internal helper functions in review_queue.py."""

    def test_parse_model_response_dict(self):
        d = {"activity": "Grading", "matched_source_task_id": "T101"}
        self.assertEqual(_parse_model_response(d), d)

    def test_parse_model_response_json_string(self):
        s = '{"activity": "Pipelaying", "matched_source_task_id": "T105"}'
        parsed = _parse_model_response(s)
        self.assertEqual(parsed["activity"], "Pipelaying")
        self.assertEqual(parsed["matched_source_task_id"], "T105")

    def test_parse_model_response_invalid_or_none(self):
        self.assertEqual(_parse_model_response(None), {})
        self.assertEqual(_parse_model_response("not-json"), {})
        self.assertEqual(_parse_model_response(123), {})

    def test_determine_review_reason(self):
        # Unmatched + low confidence
        self.assertEqual(
            _determine_review_reason(None, 40.0, 70.0),
            "unmatched_and_low_confidence",
        )
        # Unmatched + high confidence
        self.assertEqual(
            _determine_review_reason(None, 85.0, 70.0),
            "unmatched",
        )
        # Matched + low confidence
        self.assertEqual(
            _determine_review_reason("task-uuid-1", 50.0, 70.0),
            "low_confidence",
        )

    def test_extract_activity_name_from_model_response(self):
        resp = {"activity": "Foundation Excavation"}
        self.assertEqual(_extract_activity_name(resp, None), "Foundation Excavation")

    def test_extract_activity_name_from_raw_json(self):
        resp = {"raw_json": {"activity": "Pipe Welding"}}
        self.assertEqual(_extract_activity_name(resp, None), "Pipe Welding")

    def test_extract_activity_name_fallback_to_suggested_match(self):
        suggested = {"activity": "Electrical Wiring"}
        self.assertEqual(_extract_activity_name({}, suggested), "Electrical Wiring")

    def test_extract_activity_name_none_when_empty(self):
        self.assertIsNone(_extract_activity_name({}, None))

    def test_find_suggested_match_from_direct_match(self):
        match = _find_suggested_match(
            matched_st_id="uuid-1",
            matched_source_task_id="T101",
            matched_activity="Site Prep",
            matched_location="Well Pad A",
            matched_planned_start="2026-09-01",
            matched_planned_end="2026-09-05",
            model_resp={},
            location="Well Pad A",
            raw_update="Site prep ongoing.",
            all_tasks=[],
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["task_id"], "uuid-1")
        self.assertEqual(match["source_task_id"], "T101")

    def test_find_suggested_match_from_model_response_source_task_id(self):
        all_tasks = [
            {
                "task_id": "uuid-102",
                "source_task_id": "T102",
                "activity": "Foundation Concreting",
                "location": "Well Pad A",
                "planned_start": "2026-09-06",
                "planned_end": "2026-09-15",
            }
        ]
        match = _find_suggested_match(
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
            model_resp={"matched_source_task_id": "T102"},
            location="Well Pad A",
            raw_update="Concreting is underway.",
            all_tasks=all_tasks,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["source_task_id"], "T102")

    def test_find_suggested_match_from_keyword_heuristic(self):
        all_tasks = [
            {
                "task_id": "uuid-103",
                "source_task_id": "T103",
                "activity": "Drainage Trench Excavation",
                "location": "Well Pad A",
                "planned_start": "2026-09-16",
                "planned_end": "2026-09-20",
            }
        ]
        match = _find_suggested_match(
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
            model_resp={},
            location="Well Pad A",
            raw_update="Drainage work started today.",
            all_tasks=all_tasks,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["source_task_id"], "T103")

    def test_find_suggested_match_none_when_unmatched_and_no_keywords(self):
        all_tasks = [
            {
                "task_id": "uuid-101",
                "source_task_id": "T101",
                "activity": "Earthmoving",
                "location": "Well Pad A",
                "planned_start": "2026-09-01",
                "planned_end": "2026-09-05",
            }
        ]
        match = _find_suggested_match(
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
            model_resp={},
            location="Different Location",
            raw_update="Safety meeting held.",
            all_tasks=all_tasks,
        )
        self.assertIsNone(match)


class ReviewQueueQueryUnitTests(unittest.TestCase):
    """Unit tests for get_review_queue query and formatting."""

    def test_empty_review_queue(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = []

        items = get_review_queue(mock_db, threshold=70.0)
        self.assertEqual(items, [])
        self.assertEqual(mock_db.execute.call_count, 1)

    def test_review_queue_returns_unmatched_and_low_confidence_items(self):
        # Row 1: unmatched update (matched_task_id is None, confidence 20%)
        row_unmatched = _row(
            processed_id="p-111",
            site_update_id="su-111",
            source_update_id="U005",
            reported_on="2026-09-10",
            location="Well Pad B",
            raw_update="Heavy rainfall halted all operations.",
            source_reference="daily_report.csv",
            matched_task_id=None,
            progress_percent=None,
            status="blocked",
            delay_days=1,
            delay_reason="Heavy rainfall",
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=20.0,
            model_name="mock-keyword-v1",
            model_response='{"matched_source_task_id": null}',
            processed_at="2026-09-10 10:00:00",
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
        )

        # Row 2: low-confidence matched update (matched to T102, confidence 40%)
        row_low_conf = _row(
            processed_id="p-222",
            site_update_id="su-222",
            source_update_id="U002",
            reported_on="2026-09-08",
            location="Well Pad A",
            raw_update="Excavation and concreting is roughly 40% done.",
            source_reference=None,
            matched_task_id="st-uuid-102",
            progress_percent=40.0,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=40.0,
            model_name="mock-keyword-v1",
            model_response={},
            processed_at="2026-09-08 12:00:00",
            matched_st_id="st-uuid-102",
            matched_source_task_id="T102",
            matched_activity="Excavation and Concreting",
            matched_location="Well Pad A",
            matched_planned_start="2026-09-06",
            matched_planned_end="2026-09-15",
        )

        task_row_102 = _row(
            id="st-uuid-102",
            source_task_id="T102",
            activity="Excavation and Concreting",
            location="Well Pad A",
            planned_start="2026-09-06",
            planned_end="2026-09-15",
        )

        mock_db = MagicMock()
        mock_db.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[row_unmatched, row_low_conf])),
            MagicMock(fetchall=MagicMock(return_value=[task_row_102])),
        ]

        items = get_review_queue(mock_db, threshold=70.0)
        self.assertEqual(len(items), 2)

        # Verify unmatched item
        u_item = items[0]
        self.assertEqual(u_item["review_id"], "p-111")
        self.assertEqual(u_item["source_update_id"], "U005")
        self.assertEqual(u_item["confidence_score"], 20.0)
        self.assertEqual(u_item["review_reason"], "unmatched_and_low_confidence")
        self.assertEqual(u_item["raw_update"], "Heavy rainfall halted all operations.")
        self.assertIsNone(u_item["suggested_match"])

        # Verify low-confidence item with suggested match
        l_item = items[1]
        self.assertEqual(l_item["review_id"], "p-222")
        self.assertEqual(l_item["source_update_id"], "U002")
        self.assertEqual(l_item["confidence_score"], 40.0)
        self.assertEqual(l_item["review_reason"], "low_confidence")
        self.assertEqual(l_item["progress_percent"], 40.0)
        self.assertEqual(l_item["status"], "in_progress")
        self.assertIsNotNone(l_item["suggested_match"])
        self.assertEqual(l_item["suggested_match"]["source_task_id"], "T102")
        self.assertEqual(l_item["extracted_activity"], "Excavation and Concreting")

    def test_read_only_guarantee_no_mutations(self):
        """Review queue must NEVER execute mutating UPDATE, INSERT, or DELETE statements."""
        import re
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = []

        get_review_queue(mock_db)

        # Check all SQL executed
        for call_args in mock_db.execute.call_args_list:
            sql = str(call_args[0][0])
            self.assertIsNone(re.search(r"\bUPDATE\s+[a-zA-Z_]", sql, re.IGNORECASE))
            self.assertIsNone(re.search(r"\bINSERT\s+INTO\b", sql, re.IGNORECASE))
            self.assertIsNone(re.search(r"\bDELETE\s+FROM\b", sql, re.IGNORECASE))


class ReviewQueueApiEndpointTests(unittest.TestCase):
    """API endpoint tests for /review/queue and /planner/review-queue."""

    def setUp(self):
        self.client = TestClient(app)

    def test_get_review_queue_200(self):
        mock_item = {
            "review_id": "rev-1",
            "site_update_id": "su-1",
            "source_update_id": "U001",
            "reported_on": "2026-09-05",
            "location": "Well Pad A",
            "raw_update": "Work ongoing.",
            "source_reference": None,
            "extracted_activity": "Grading",
            "progress_percent": 30.0,
            "status": "in_progress",
            "delay_days": None,
            "delay_reason": None,
            "actual_start_date": None,
            "actual_end_date": None,
            "confidence_score": 45.0,
            "review_reason": "low_confidence",
            "suggested_match": None,
            "processed_at": "2026-09-05 10:00:00",
        }

        with patch("app.review_queue.get_review_queue", return_value=[mock_item]):
            resp = self.client.get("/review/queue")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["review_id"], "rev-1")
            self.assertEqual(data[0]["confidence_score"], 45.0)

    def test_get_planner_review_queue_alias_200(self):
        """Both /review/queue and /planner/review-queue routes return 200."""
        with patch("app.review_queue.get_review_queue", return_value=[]):
            resp = self.client.get("/planner/review-queue")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), [])

    def test_get_review_queue_passes_threshold_param(self):
        with patch("app.review_queue.get_review_queue", return_value=[]) as mock_get:
            resp = self.client.get("/review/queue?threshold=55.5")
            self.assertEqual(resp.status_code, 200)
            mock_get.assert_called_once()
            self.assertEqual(mock_get.call_args[1]["threshold"], 55.5)

    def test_get_review_queue_db_error_returns_503(self):
        mock_db = MagicMock()
        mock_db.execute.side_effect = OperationalError("db error", None, Exception("orig"))

        def _get_db_override():
            yield mock_db

        app.dependency_overrides[get_db] = _get_db_override
        try:
            resp = self.client.get("/review/queue")
            self.assertEqual(resp.status_code, 503)
            self.assertIn("Database is unavailable", resp.json()["detail"])
        finally:
            app.dependency_overrides.pop(get_db, None)


if __name__ == "__main__":
    unittest.main()
