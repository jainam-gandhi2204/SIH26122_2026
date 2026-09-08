"""Tests for Planner Review Queue & Exception Handling (app/review_queue.py and API endpoints).

All tests are pure unit tests — mock DB sessions, no external dependencies.
"""

from __future__ import annotations

import json
import types
import unittest
from unittest.mock import MagicMock, call, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.database import get_db
from app.main import app
from app.review_queue import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    InvalidTaskMatchError,
    ReviewActionError,
    ReviewItemNotFoundError,
    _determine_review_reason,
    _extract_activity_name,
    _find_candidate_tasks,
    _find_suggested_match,
    _parse_model_response,
    approve_match,
    change_matched_task,
    get_review_queue,
    reject_match,
    resolve_review_item,
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
        # Ambiguous match
        self.assertEqual(
            _determine_review_reason(None, 40.0, 70.0, is_ambiguous=True),
            "ambiguous_match",
        )
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

    def test_find_candidate_tasks_ranks_by_location_and_keywords(self):
        tasks = [
            {
                "task_id": "uuid-1",
                "source_task_id": "T101",
                "activity": "Foundation Concreting",
                "location": "Well Pad A",
                "planned_start": "2026-09-01",
            },
            {
                "task_id": "uuid-2",
                "source_task_id": "T102",
                "activity": "Drainage Trenching",
                "location": "Well Pad A",
                "planned_start": "2026-09-05",
            },
            {
                "task_id": "uuid-3",
                "source_task_id": "T103",
                "activity": "Foundation Piling",
                "location": "Other Site",
                "planned_start": "2026-09-02",
            },
        ]
        # Querying with update at Well Pad A mentioning "concreting"
        candidates = _find_candidate_tasks(
            location="Well Pad A",
            raw_update="Concreting work started.",
            all_tasks=tasks,
        )
        self.assertEqual(len(candidates), 2)
        # T101 should rank first due to matching keyword 'concreting'
        self.assertEqual(candidates[0]["source_task_id"], "T101")
        self.assertEqual(candidates[1]["source_task_id"], "T102")

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


class ReviewQueueQueryUnitTests(unittest.TestCase):
    """Unit tests for get_review_queue query, candidate tasks, and formatting."""

    def test_empty_review_queue(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = []

        items = get_review_queue(mock_db, threshold=70.0)
        self.assertEqual(items, [])
        self.assertEqual(mock_db.execute.call_count, 1)

    def test_review_queue_returns_enriched_items(self):
        row_unmatched = _row(
            processed_id="p-111",
            site_update_id="su-111",
            source_update_id="U005",
            reported_on="2026-09-10",
            location="Well Pad B",
            raw_update="PCC pouring or reinforced concrete work in progress.",
            source_reference="daily_report.csv",
            matched_task_id=None,
            progress_percent=None,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=30.0,
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

        task_cand1 = _row(
            id="cand-1",
            source_task_id="T201",
            activity="PCC Pouring Foundation",
            location="Well Pad B",
            planned_start="2026-09-08",
            planned_end="2026-09-15",
        )
        task_cand2 = _row(
            id="cand-2",
            source_task_id="T202",
            activity="Reinforced Concrete Structure",
            location="Well Pad B",
            planned_start="2026-09-16",
            planned_end="2026-09-25",
        )

        mock_db = MagicMock()
        mock_db.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[row_unmatched])),
            MagicMock(fetchall=MagicMock(return_value=[task_cand1, task_cand2])),
        ]

        items = get_review_queue(mock_db, threshold=70.0)
        self.assertEqual(len(items), 1)

        item = items[0]
        self.assertEqual(item["review_id"], "p-111")
        self.assertIsNone(item["current_matched_task"])
        self.assertEqual(len(item["candidate_tasks"]), 2)
        # Multiple candidate tasks at the location matched keywords -> ambiguous match
        self.assertEqual(item["review_reason"], "ambiguous_match")

    def test_review_queue_filters_by_status(self):
        row_pending = _row(
            processed_id="p-1",
            site_update_id="su-1",
            source_update_id="U001",
            reported_on="2026-09-05",
            location="Well Pad A",
            raw_update="Update 1",
            source_reference=None,
            matched_task_id=None,
            progress_percent=None,
            status=None,
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=40.0,
            model_name="mock",
            model_response='{"review_status": "pending"}',
            processed_at=None,
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
        )
        row_resolved = _row(
            processed_id="p-2",
            site_update_id="su-2",
            source_update_id="U002",
            reported_on="2026-09-05",
            location="Well Pad A",
            raw_update="Update 2",
            source_reference=None,
            matched_task_id="tid-2",
            progress_percent=50.0,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date=None,
            actual_end_date=None,
            confidence_score=100.0,
            model_name="mock",
            model_response='{"review_status": "approved"}',
            processed_at=None,
            matched_st_id="tid-2",
            matched_source_task_id="T102",
            matched_activity="Activity 2",
            matched_location="Well Pad A",
            matched_planned_start=None,
            matched_planned_end=None,
        )

        mock_db = MagicMock()
        mock_db.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[row_pending, row_resolved])),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        # Default 'pending' returns only row_pending
        pending_items = get_review_queue(mock_db, status="pending")
        self.assertEqual(len(pending_items), 1)
        self.assertEqual(pending_items[0]["review_id"], "p-1")


class ReviewQueueResolutionActionsTests(unittest.TestCase):
    """Tests for planner actions: approve_match, change_matched_task, reject_match, resolve_review_item."""

    def _make_review_row(self, matched_task_id=None, confidence=40.0):
        return _row(
            processed_id="rev-123",
            site_update_id="su-123",
            source_update_id="U001",
            reported_on="2026-09-05",
            location="Well Pad A",
            raw_update="Concreting is 40% complete.",
            source_reference="daily.pdf",
            matched_task_id=matched_task_id,
            progress_percent=40.0,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date="2026-09-01",
            actual_end_date=None,
            confidence_score=confidence,
            model_name="mock-keyword-v1",
            model_response='{"provider": "mock"}',
            processed_at="2026-09-05 12:00:00",
            is_current=True,
            matched_st_id=matched_task_id,
            matched_source_task_id="T101" if matched_task_id else None,
            matched_activity="Concreting" if matched_task_id else None,
            matched_location="Well Pad A" if matched_task_id else None,
            matched_planned_start="2026-09-01" if matched_task_id else None,
            matched_planned_end="2026-09-10" if matched_task_id else None,
        )

    @patch("app.review_queue.update_schedule_task_actuals")
    def test_approve_match_with_existing_matched_task(self, mock_update_actuals):
        mock_db = MagicMock()
        mock_row = self._make_review_row(matched_task_id="task-uuid-101")
        mock_db.execute.return_value.fetchone.return_value = mock_row

        res = approve_match(mock_db, "rev-123", notes="Looks good")

        self.assertEqual(res["action"], "approve")
        self.assertEqual(res["matched_task_id"], "task-uuid-101")
        self.assertEqual(res["confidence_score"], 100.0)
        self.assertEqual(res["review_status"], "approved")

        # Must call update_schedule_task_actuals
        mock_update_actuals.assert_called_once_with(
            db=mock_db,
            task_id="task-uuid-101",
            reported_on="2026-09-05",
            progress_percent=40.0,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date="2026-09-01",
            actual_end_date=None,
        )
        self.assertEqual(mock_db.commit.call_count, 1)

    @patch("app.review_queue.update_schedule_task_actuals")
    def test_approve_match_with_suggested_match(self, mock_update_actuals):
        mock_db = MagicMock()
        # Row has matched_task_id = None, but model_response contains suggested T101
        row = self._make_review_row(matched_task_id=None)
        row.model_response = '{"matched_source_task_id": "T101"}'

        task_101 = _row(
            id="task-uuid-101",
            source_task_id="T101",
            activity="Concreting",
            location="Well Pad A",
            planned_start="2026-09-01",
            planned_end="2026-09-10",
        )

        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: row),          # _FETCH_REVIEW_ITEM_QUERY
            MagicMock(fetchall=lambda: [task_101]),   # _ALL_SCHEDULE_TASKS_QUERY
            MagicMock(),                              # _UPDATE_PROCESSED_MATCH_QUERY
        ]

        res = approve_match(mock_db, "rev-123")
        self.assertEqual(res["action"], "approve")
        self.assertEqual(res["matched_task_id"], "task-uuid-101")
        mock_update_actuals.assert_called_once()

    def test_approve_match_not_found_raises_error(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = None

        with self.assertRaises(ReviewItemNotFoundError):
            approve_match(mock_db, "non-existent")

    def test_approve_match_without_any_match_raises_error(self):
        mock_db = MagicMock()
        row = self._make_review_row(matched_task_id=None)
        row.model_response = "{}"

        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: row),
            MagicMock(fetchall=lambda: []),
        ]

        with self.assertRaises(ReviewActionError):
            approve_match(mock_db, "rev-123")

    @patch("app.review_queue.update_schedule_task_actuals")
    def test_change_matched_task_success(self, mock_update_actuals):
        mock_db = MagicMock()
        mock_row = self._make_review_row(matched_task_id="old-uuid")
        new_task_row = _row(
            id="new-uuid-202",
            source_task_id="T202",
            activity="Foundation Reinforcement",
            location="Well Pad A",
            planned_start="2026-09-05",
            planned_end="2026-09-15",
        )

        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: mock_row),      # _FETCH_REVIEW_ITEM_QUERY
            MagicMock(fetchone=lambda: new_task_row),  # _LOOKUP_TASK_QUERY
            MagicMock(),                               # _UPDATE_PROCESSED_MATCH_QUERY
        ]

        res = change_matched_task(mock_db, "rev-123", new_task_id="T202", notes="Corrected by planner")

        self.assertEqual(res["action"], "change_match")
        self.assertEqual(res["matched_task_id"], "new-uuid-202")
        self.assertEqual(res["source_task_id"], "T202")
        self.assertEqual(res["review_status"], "manually_matched")

        mock_update_actuals.assert_called_once_with(
            db=mock_db,
            task_id="new-uuid-202",
            reported_on="2026-09-05",
            progress_percent=40.0,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date="2026-09-01",
            actual_end_date=None,
        )

    def test_change_matched_task_invalid_target_raises_error(self):
        mock_db = MagicMock()
        mock_row = self._make_review_row()
        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: mock_row),
            MagicMock(fetchone=lambda: None),  # target task not found
        ]

        with self.assertRaises(InvalidTaskMatchError):
            change_matched_task(mock_db, "rev-123", new_task_id="INVALID_TASK")

    @patch("app.review_queue.update_schedule_task_actuals")
    def test_reject_match_clears_task_and_does_not_update_actuals(self, mock_update_actuals):
        mock_db = MagicMock()
        mock_row = self._make_review_row(matched_task_id="old-uuid")
        mock_db.execute.return_value.fetchone.return_value = mock_row

        res = reject_match(mock_db, "rev-123", reason="Unrelated site observation")

        self.assertEqual(res["action"], "reject")
        self.assertIsNone(res["matched_task_id"])
        self.assertEqual(res["review_status"], "rejected")

        # schedule actuals must NOT be updated on rejection
        mock_update_actuals.assert_not_called()
        self.assertEqual(mock_db.commit.call_count, 1)

    def test_resolve_review_item_dispatcher(self):
        mock_db = MagicMock()
        with patch("app.review_queue.approve_match", return_value={"action": "approve"}) as mock_approve:
            res = resolve_review_item(mock_db, "rev-1", action="approve", notes="OK")
            self.assertEqual(res["action"], "approve")
            mock_approve.assert_called_once_with(mock_db, "rev-1", notes="OK")

        with patch("app.review_queue.change_matched_task", return_value={"action": "change_match"}) as mock_change:
            res = resolve_review_item(mock_db, "rev-1", action="change_match", task_id="T101")
            self.assertEqual(res["action"], "change_match")
            mock_change.assert_called_once_with(mock_db, "rev-1", new_task_id="T101", notes=None)

        with patch("app.review_queue.reject_match", return_value={"action": "reject"}) as mock_reject:
            res = resolve_review_item(mock_db, "rev-1", action="reject", notes="Dismissed")
            self.assertEqual(res["action"], "reject")
            mock_reject.assert_called_once_with(mock_db, "rev-1", reason="Dismissed")

        with self.assertRaises(ReviewActionError):
            resolve_review_item(mock_db, "rev-1", action="invalid_action")


class ReviewQueueApiEndpointTests(unittest.TestCase):
    """API endpoint tests for review queue queries and resolution actions."""

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
            "review_status": "pending",
            "current_matched_task": None,
            "candidate_tasks": [],
            "suggested_match": None,
            "processed_at": "2026-09-05 10:00:00",
        }

        with patch("app.review_queue.get_review_queue", return_value=[mock_item]):
            resp = self.client.get("/review/queue")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["review_id"], "rev-1")

    def test_post_approve_endpoint_200(self):
        expected = {"review_id": "rev-1", "action": "approve", "matched_task_id": "tid-1"}
        with patch("app.review_queue.approve_match", return_value=expected):
            resp = self.client.post("/planner/review-queue/rev-1/approve", json={"notes": "Approved"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["action"], "approve")

    def test_post_approve_endpoint_alias_200(self):
        expected = {"review_id": "rev-1", "action": "approve"}
        with patch("app.review_queue.approve_match", return_value=expected):
            resp = self.client.post("/review/queue/rev-1/approve")
            self.assertEqual(resp.status_code, 200)

    def test_post_change_match_endpoint_200(self):
        expected = {"review_id": "rev-1", "action": "change_match", "matched_task_id": "new-tid"}
        with patch("app.review_queue.change_matched_task", return_value=expected):
            resp = self.client.post(
                "/planner/review-queue/rev-1/change-match",
                json={"task_id": "T102", "notes": "Reassigned"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["matched_task_id"], "new-tid")

    def test_post_reject_endpoint_200(self):
        expected = {"review_id": "rev-1", "action": "reject", "matched_task_id": None}
        with patch("app.review_queue.reject_match", return_value=expected):
            resp = self.client.post(
                "/planner/review-queue/rev-1/reject",
                json={"reason": "Not related to project"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["action"], "reject")

    def test_post_resolve_unified_endpoint_200(self):
        expected = {"review_id": "rev-1", "action": "approve"}
        with patch("app.review_queue.resolve_review_item", return_value=expected):
            resp = self.client.post(
                "/planner/review-queue/rev-1/resolve",
                json={"action": "approve", "notes": "Approved via resolve"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["action"], "approve")

    def test_post_approve_not_found_returns_404(self):
        with patch("app.review_queue.approve_match", side_effect=ReviewItemNotFoundError("Item not found")):
            resp = self.client.post("/planner/review-queue/missing-id/approve")
            self.assertEqual(resp.status_code, 404)
            self.assertIn("Item not found", resp.json()["detail"])

    def test_post_change_match_invalid_task_returns_404(self):
        with patch("app.review_queue.change_matched_task", side_effect=InvalidTaskMatchError("Task invalid")):
            resp = self.client.post("/planner/review-queue/rev-1/change-match", json={"task_id": "INVALID"})
            self.assertEqual(resp.status_code, 404)
            self.assertIn("Task invalid", resp.json()["detail"])

    def test_post_resolve_invalid_action_returns_422(self):
        with patch("app.review_queue.resolve_review_item", side_effect=ReviewActionError("Unknown action")):
            resp = self.client.post("/planner/review-queue/rev-1/resolve", json={"action": "unknown_act"})
            self.assertEqual(resp.status_code, 422)
            self.assertIn("Unknown action", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
