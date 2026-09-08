"""Planner Review Queue for unmatched and low-confidence site updates.

Responsibilities
----------------
1. Identify AI-processed updates where matched_task_id IS NULL or
   confidence_score is below a given threshold (default: 70.0).
2. Retrieve these items for planner review with full context:
   - raw site update text, reported date, location, and source reference
   - extracted activity, progress_percent, status, delays, and actual dates
   - confidence score and review reason (unmatched, low_confidence, etc.)
   - suggested match if available (from model response or location/activity keywords)
3. Never automatically attach or mutate uncertain matches in the database.
   The review queue is strictly read-only; existing matching logic is unchanged.

No FastAPI imports — this module is fully unit-testable with mock DB sessions.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_CONFIDENCE_THRESHOLD: float = 70.0


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

_REVIEW_QUEUE_QUERY = text(
    """
    SELECT
        ap.id                      AS processed_id,
        ap.site_update_id,
        su.source_update_id,
        su.reported_on::TEXT       AS reported_on,
        su.location,
        su.raw_update,
        su.source_reference,
        ap.matched_task_id,
        ap.progress_percent,
        ap.status,
        ap.delay_days,
        ap.delay_reason,
        ap.actual_start_date::TEXT AS actual_start_date,
        ap.actual_end_date::TEXT   AS actual_end_date,
        ap.confidence_score,
        ap.model_name,
        ap.model_response,
        ap.processed_at::TEXT      AS processed_at,
        st.id                      AS matched_st_id,
        st.source_task_id          AS matched_source_task_id,
        st.activity                AS matched_activity,
        st.location                AS matched_location,
        st.planned_start::TEXT     AS matched_planned_start,
        st.planned_end::TEXT       AS matched_planned_end
    FROM ai_processed_updates ap
    JOIN site_updates su ON ap.site_update_id = su.id
    LEFT JOIN schedule_tasks st ON ap.matched_task_id = st.id
    WHERE ap.is_current = true
      AND (ap.matched_task_id IS NULL OR ap.confidence_score < :threshold)
    ORDER BY su.reported_on DESC, ap.processed_at DESC
    """
)

_ALL_SCHEDULE_TASKS_QUERY = text(
    """
    SELECT
        id,
        source_task_id,
        activity,
        location,
        planned_start::TEXT AS planned_start,
        planned_end::TEXT   AS planned_end
    FROM schedule_tasks
    ORDER BY planned_start, source_task_id
    """
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_model_response(raw_resp: Any) -> dict[str, Any]:
    """Parse model_response JSON into a python dict if stored as a string."""
    if isinstance(raw_resp, dict):
        return raw_resp
    if isinstance(raw_resp, str):
        try:
            val = json.loads(raw_resp)
            return val if isinstance(val, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _find_suggested_match(
    matched_st_id: Any,
    matched_source_task_id: Any,
    matched_activity: Any,
    matched_location: Any,
    matched_planned_start: Any,
    matched_planned_end: Any,
    model_resp: dict[str, Any],
    location: str,
    raw_update: str,
    all_tasks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find a suggested schedule task match without attaching it.

    1. If the AI tentatively matched a task but with low confidence,
       that task is the primary suggestion for planner review.
    2. Otherwise, check if model_response proposed a matched_source_task_id.
    3. Fallback: match activity keywords against tasks at the same location.
    4. Return None if no plausible match is found.
    """
    # 1. Direct low-confidence match
    if matched_st_id is not None:
        return {
            "task_id": str(matched_st_id),
            "source_task_id": str(matched_source_task_id),
            "activity": str(matched_activity),
            "location": str(matched_location),
            "planned_start": str(matched_planned_start) if matched_planned_start else None,
            "planned_end": str(matched_planned_end) if matched_planned_end else None,
        }

    # 2. Suggested source task in model_response (e.g. from Gemini raw_json)
    suggested_source: str | None = None
    if isinstance(model_resp, dict):
        suggested_source = model_resp.get("matched_source_task_id")
        if not suggested_source and isinstance(model_resp.get("raw_json"), dict):
            suggested_source = model_resp["raw_json"].get("matched_source_task_id")

    if suggested_source:
        suggested_clean = str(suggested_source).strip().upper()
        for t in all_tasks:
            if t.get("source_task_id", "").upper() == suggested_clean:
                return dict(t)

    # 3. Fallback heuristic: keyword matching for tasks at same location
    raw_lower = raw_update.lower()
    loc_clean = location.strip().lower()
    loc_candidates = [
        t for t in all_tasks
        if t.get("location", "").strip().lower() == loc_clean
    ]

    for t in loc_candidates:
        act_words = [
            w for w in re.findall(r"\w+", t.get("activity", "").lower())
            if len(w) > 3
        ]
        if any(w in raw_lower for w in act_words):
            return dict(t)

    return None


def _extract_activity_name(
    model_resp: dict[str, Any],
    suggested_match: dict[str, Any] | None,
) -> str | None:
    """Extract a descriptive activity name from model output or suggested task."""
    if isinstance(model_resp, dict):
        act = model_resp.get("activity")
        if act:
            return str(act)
        raw_json = model_resp.get("raw_json")
        if isinstance(raw_json, dict) and raw_json.get("activity"):
            return str(raw_json["activity"])

    if suggested_match and suggested_match.get("activity"):
        return str(suggested_match["activity"])

    return None


def _determine_review_reason(matched_task_id: Any, confidence: float, threshold: float) -> str:
    """Classify why this item was placed in the review queue."""
    is_unmatched = matched_task_id is None
    is_low_conf = confidence < threshold

    if is_unmatched and is_low_conf:
        return "unmatched_and_low_confidence"
    if is_unmatched:
        return "unmatched"
    return "low_confidence"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_review_queue(
    db: Session,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Return all AI-processed updates that require planner review.

    An item is queued for review if:
    - matched_task_id IS NULL (no schedule task could be linked), OR
    - confidence_score < threshold (AI was uncertain about the match/extraction).

    Parameters
    ----------
    db:         Open SQLAlchemy session.
    threshold:  Confidence threshold (0-100) below which an update is flagged.

    Returns
    -------
    List of review queue items, ordered newest reported_on date first.
    Each item contains raw update text, extracted fields, confidence, review reason,
    and suggested schedule task match (without attaching it).
    """
    # 1. Fetch review queue candidates
    rows = db.execute(_REVIEW_QUEUE_QUERY, {"threshold": threshold}).fetchall()
    if not rows:
        return []

    # 2. Fetch all schedule tasks once for suggested match resolution
    task_rows = db.execute(_ALL_SCHEDULE_TASKS_QUERY).fetchall()
    all_tasks = [
        {
            "task_id": str(t.id),
            "source_task_id": str(t.source_task_id),
            "activity": str(t.activity),
            "location": str(t.location),
            "planned_start": str(t.planned_start) if t.planned_start else None,
            "planned_end": str(t.planned_end) if t.planned_end else None,
        }
        for t in task_rows
    ]

    # 3. Build formatted review queue items
    items: list[dict[str, Any]] = []
    for r in rows:
        model_resp = _parse_model_response(r.model_response)
        conf = float(r.confidence_score) if r.confidence_score is not None else 0.0

        suggested = _find_suggested_match(
            matched_st_id=r.matched_st_id,
            matched_source_task_id=r.matched_source_task_id,
            matched_activity=r.matched_activity,
            matched_location=r.matched_location,
            matched_planned_start=r.matched_planned_start,
            matched_planned_end=r.matched_planned_end,
            model_resp=model_resp,
            location=r.location,
            raw_update=r.raw_update,
            all_tasks=all_tasks,
        )

        extracted_act = _extract_activity_name(model_resp, suggested)
        reason = _determine_review_reason(r.matched_task_id, conf, threshold)

        items.append({
            "review_id": str(r.processed_id),
            "site_update_id": str(r.site_update_id),
            "source_update_id": r.source_update_id,
            "reported_on": str(r.reported_on) if r.reported_on else None,
            "location": r.location,
            "raw_update": r.raw_update,
            "source_reference": r.source_reference,
            "extracted_activity": extracted_act,
            "progress_percent": (
                float(r.progress_percent) if r.progress_percent is not None else None
            ),
            "status": r.status,
            "delay_days": r.delay_days,
            "delay_reason": r.delay_reason,
            "actual_start_date": str(r.actual_start_date) if r.actual_start_date else None,
            "actual_end_date": str(r.actual_end_date) if r.actual_end_date else None,
            "confidence_score": conf,
            "review_reason": reason,
            "suggested_match": suggested,
            "processed_at": str(r.processed_at) if r.processed_at else None,
        })

    return items
