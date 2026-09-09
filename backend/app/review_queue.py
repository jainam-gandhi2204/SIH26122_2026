"""Planner Review Queue & Exception Handling for unmatched and low-confidence site updates.

Responsibilities
----------------
1. Identify AI-processed updates where matched_task_id IS NULL, confidence_score
   is below a given threshold (default: 70.0), or multiple candidate matches cause ambiguity.
2. Retrieve these items for planner review with complete context:
   - raw site update text, reported date, location, and source reference
   - extracted activity, progress_percent, status, delays, and actual dates
   - current matched task (if any)
   - candidate schedule tasks where useful to assist planner decision making
   - confidence score and review reason (unmatched, low_confidence, ambiguous_match, etc.)
   - suggested match if available (from model response or location/activity keywords)
3. Allow a planner to resolve review items:
   - approve the AI match (or suggested match)
   - change the matched task to another valid schedule task
   - mark the update as intentionally unmatched/rejected
4. When a match is approved or corrected, safely apply schedule actual updates
   via existing update_schedule_task_actuals (preserving baseline planned dates,
   monotonic progress, stale update protections, and FACT date guarantees).

No FastAPI imports — this module is fully unit-testable with mock DB sessions.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai_processor import update_schedule_task_actuals

DEFAULT_CONFIDENCE_THRESHOLD: float = 70.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReviewItemNotFoundError(Exception):
    """Raised when the specified review item (ai_processed_updates id) does not exist."""


class InvalidTaskMatchError(Exception):
    """Raised when the specified task_id does not exist in schedule_tasks."""


class ReviewActionError(Exception):
    """Raised when a review resolution action is invalid or cannot be applied."""


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

_FETCH_REVIEW_ITEM_QUERY = text(
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
        ap.is_current,
        st.id                      AS matched_st_id,
        st.source_task_id          AS matched_source_task_id,
        st.activity                AS matched_activity,
        st.location                AS matched_location,
        st.planned_start::TEXT     AS matched_planned_start,
        st.planned_end::TEXT       AS matched_planned_end
    FROM ai_processed_updates ap
    JOIN site_updates su ON ap.site_update_id = su.id
    LEFT JOIN schedule_tasks st ON ap.matched_task_id = st.id
    WHERE ap.id = :review_id
    """
)

_LOOKUP_TASK_QUERY = text(
    """
    SELECT
        id,
        source_task_id,
        activity,
        location,
        planned_start::TEXT AS planned_start,
        planned_end::TEXT   AS planned_end
    FROM schedule_tasks
    WHERE id::TEXT = :task_id OR UPPER(source_task_id) = UPPER(:task_id)
    LIMIT 1
    """
)

_UPDATE_PROCESSED_MATCH_QUERY = text(
    """
    UPDATE ai_processed_updates
    SET
        matched_task_id = :matched_task_id,
        confidence_score = :confidence_score,
        model_response = :model_response
    WHERE id = :review_id
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


def _find_candidate_tasks(
    location: str,
    raw_update: str,
    all_tasks: list[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return candidate schedule tasks relevant to the site update to aid planner review."""
    loc_clean = location.strip().lower()
    raw_lower = raw_update.lower()

    # Prioritise tasks at same location
    same_loc_tasks = [
        t for t in all_tasks
        if t.get("location", "").strip().lower() == loc_clean
    ]
    pool = same_loc_tasks if same_loc_tasks else all_tasks

    # Rank tasks by keyword relevance against update text
    def score_task(t: dict[str, Any]) -> int:
        act_words = [
            w for w in re.findall(r"\w+", t.get("activity", "").lower())
            if len(w) > 3
        ]
        matches = sum(1 for w in act_words if w in raw_lower)
        return matches

    scored = [(score_task(t), t) for t in pool]
    # Sort descending by score, then ascending by planned_start
    scored.sort(key=lambda item: (-item[0], item[1].get("planned_start") or ""))

    return [t for _, t in scored[:limit]]


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


def _determine_review_reason(
    matched_task_id: Any,
    confidence: float,
    threshold: float,
    is_ambiguous: bool = False,
) -> str:
    """Classify why this item was placed in the review queue."""
    if is_ambiguous:
        return "ambiguous_match"
    is_unmatched = matched_task_id is None
    is_low_conf = confidence < threshold

    if is_unmatched and is_low_conf:
        return "unmatched_and_low_confidence"
    if is_unmatched:
        return "unmatched"
    return "low_confidence"


# ---------------------------------------------------------------------------
# Public Query API
# ---------------------------------------------------------------------------

def get_review_queue(
    db: Session,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    status: str = "pending",
) -> list[dict[str, Any]]:
    """Return AI-processed updates that require planner review.

    An item is queued for review if:
    - matched_task_id IS NULL (no schedule task could be linked), OR
    - confidence_score < threshold (AI was uncertain about the match/extraction), OR
    - ambiguous candidate tasks were found at the location.

    Parameters
    ----------
    db:         Open SQLAlchemy session.
    threshold:  Confidence threshold (0-100) below which an update is flagged.
    status:     Filter by review status: 'pending' (default), 'resolved', or 'all'.

    Returns
    -------
    List of review queue items, ordered newest reported_on date first.
    Each item contains raw update text, extracted fields, current match,
    candidate schedule tasks, suggested match, confidence, and review reason.
    """
    rows = db.execute(_REVIEW_QUEUE_QUERY, {"threshold": threshold}).fetchall()
    if not rows:
        return []

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

    clean_status = (status or "pending").strip().lower()

    items: list[dict[str, Any]] = []
    for r in rows:
        model_resp = _parse_model_response(r.model_response)
        review_state = model_resp.get("review_status", "pending")

        # Filter by review status
        if clean_status == "pending" and review_state != "pending":
            continue
        elif clean_status == "resolved" and review_state in ("pending", "historical"):
            continue
        elif clean_status == "historical" and review_state != "historical":
            continue

        conf = float(r.confidence_score) if r.confidence_score is not None else 0.0

        current_match = (
            {
                "task_id": str(r.matched_st_id),
                "source_task_id": str(r.matched_source_task_id),
                "activity": str(r.matched_activity),
                "location": str(r.matched_location),
                "planned_start": str(r.matched_planned_start) if r.matched_planned_start else None,
                "planned_end": str(r.matched_planned_end) if r.matched_planned_end else None,
            }
            if r.matched_st_id is not None
            else None
        )

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

        candidates = _find_candidate_tasks(
            location=r.location,
            raw_update=r.raw_update,
            all_tasks=all_tasks,
            limit=10,
        )

        # Ambiguity detection: check if multiple tasks at the location matched keywords
        raw_lower = r.raw_update.lower()
        keyword_matches = 0
        for cand in candidates:
            words = [w for w in re.findall(r"\w+", cand["activity"].lower()) if len(w) > 3]
            if any(w in raw_lower for w in words):
                keyword_matches += 1
        is_ambiguous = (r.matched_task_id is None and keyword_matches >= 2)

        extracted_act = _extract_activity_name(model_resp, suggested)
        reason = _determine_review_reason(r.matched_task_id, conf, threshold, is_ambiguous=is_ambiguous)

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
            "review_status": review_state,
            "current_matched_task": current_match,
            "suggested_match": suggested,
            "candidate_tasks": candidates,
            "processed_at": str(r.processed_at) if r.processed_at else None,
        })

    return items


# ---------------------------------------------------------------------------
# Planner Resolution Actions
# ---------------------------------------------------------------------------

def approve_match(
    db: Session,
    review_id: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Approve the AI or suggested match for a site update.

    - Sets confidence_score = 100.0.
    - Records review_status = 'approved' in model_response.
    - Safely applies extracted actuals to schedule_tasks via update_schedule_task_actuals.
    """
    row = db.execute(_FETCH_REVIEW_ITEM_QUERY, {"review_id": review_id}).fetchone()
    if row is None:
        raise ReviewItemNotFoundError(f"Review item '{review_id}' not found.")

    target_task_id: str | None = str(row.matched_task_id) if row.matched_task_id else None

    # If matched_task_id is None, attempt to resolve from suggested_match
    if target_task_id is None:
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
        model_resp = _parse_model_response(row.model_response)
        suggested = _find_suggested_match(
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
            model_resp=model_resp,
            location=row.location,
            raw_update=row.raw_update,
            all_tasks=all_tasks,
        )
        if suggested and suggested.get("task_id"):
            target_task_id = str(suggested["task_id"])
        else:
            raise ReviewActionError(
                "No match exists to approve. Please specify a task_id to link this update."
            )

    model_resp = _parse_model_response(row.model_response)
    model_resp["review_status"] = "approved"
    model_resp["reviewed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if notes:
        model_resp["planner_notes"] = notes

    # 1. Update ai_processed_updates
    db.execute(
        _UPDATE_PROCESSED_MATCH_QUERY,
        {
            "review_id": review_id,
            "matched_task_id": target_task_id,
            "confidence_score": 100.0,
            "model_response": json.dumps(model_resp),
        },
    )

    # 2. Apply schedule actuals using safe existing engine
    update_schedule_task_actuals(
        db=db,
        task_id=target_task_id,
        reported_on=str(row.reported_on),
        progress_percent=float(row.progress_percent) if row.progress_percent is not None else None,
        status=row.status,
        delay_days=row.delay_days,
        delay_reason=row.delay_reason,
        actual_start_date=str(row.actual_start_date) if row.actual_start_date else None,
        actual_end_date=str(row.actual_end_date) if row.actual_end_date else None,
    )

    db.commit()

    return {
        "review_id": review_id,
        "action": "approve",
        "matched_task_id": target_task_id,
        "review_status": "approved",
        "confidence_score": 100.0,
        "notes": notes,
    }


def change_matched_task(
    db: Session,
    review_id: str,
    new_task_id: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Reassign the site update to a different schedule task.

    - Validates that new_task_id exists in schedule_tasks.
    - Updates ai_processed_updates.matched_task_id and confidence_score = 100.0.
    - Records audit trail in model_response.
    - Safely applies extracted actuals to the new task via update_schedule_task_actuals.
    """
    row = db.execute(_FETCH_REVIEW_ITEM_QUERY, {"review_id": review_id}).fetchone()
    if row is None:
        raise ReviewItemNotFoundError(f"Review item '{review_id}' not found.")

    target_task = db.execute(_LOOKUP_TASK_QUERY, {"task_id": new_task_id.strip()}).fetchone()
    if target_task is None:
        raise InvalidTaskMatchError(f"Task '{new_task_id}' does not exist in schedule_tasks.")

    target_uuid = str(target_task.id)

    model_resp = _parse_model_response(row.model_response)
    model_resp["review_status"] = "manually_matched"
    model_resp["previous_matched_task_id"] = str(row.matched_task_id) if row.matched_task_id else None
    model_resp["reviewed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if notes:
        model_resp["planner_notes"] = notes

    # 1. Update ai_processed_updates
    db.execute(
        _UPDATE_PROCESSED_MATCH_QUERY,
        {
            "review_id": review_id,
            "matched_task_id": target_uuid,
            "confidence_score": 100.0,
            "model_response": json.dumps(model_resp),
        },
    )

    # 2. Apply schedule actuals to the newly matched task
    update_schedule_task_actuals(
        db=db,
        task_id=target_uuid,
        reported_on=str(row.reported_on),
        progress_percent=float(row.progress_percent) if row.progress_percent is not None else None,
        status=row.status,
        delay_days=row.delay_days,
        delay_reason=row.delay_reason,
        actual_start_date=str(row.actual_start_date) if row.actual_start_date else None,
        actual_end_date=str(row.actual_end_date) if row.actual_end_date else None,
    )

    db.commit()

    return {
        "review_id": review_id,
        "action": "change_match",
        "matched_task_id": target_uuid,
        "source_task_id": target_task.source_task_id,
        "review_status": "manually_matched",
        "confidence_score": 100.0,
        "notes": notes,
    }


def reject_match(
    db: Session,
    review_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark the site update as intentionally unmatched/rejected.

    - Clears matched_task_id to NULL.
    - Sets confidence_score = 0.0.
    - Records review_status = 'rejected' and reason in model_response.
    - Does NOT apply any updates to schedule_tasks.
    """
    row = db.execute(_FETCH_REVIEW_ITEM_QUERY, {"review_id": review_id}).fetchone()
    if row is None:
        raise ReviewItemNotFoundError(f"Review item '{review_id}' not found.")

    model_resp = _parse_model_response(row.model_response)
    model_resp["review_status"] = "rejected"
    model_resp["rejection_reason"] = reason or "Intentionally marked unmatched by planner"
    model_resp["reviewed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    db.execute(
        _UPDATE_PROCESSED_MATCH_QUERY,
        {
            "review_id": review_id,
            "matched_task_id": None,
            "confidence_score": 0.0,
            "model_response": json.dumps(model_resp),
        },
    )

    db.commit()

    return {
        "review_id": review_id,
        "action": "reject",
        "matched_task_id": None,
        "review_status": "rejected",
        "reason": reason,
    }


def resolve_review_item(
    db: Session,
    review_id: str,
    action: str,
    task_id: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Unified dispatcher for planner review actions."""
    act = (action or "").strip().lower()
    if act == "approve":
        return approve_match(db, review_id, notes=notes)
    elif act in ("change_match", "match", "change"):
        if not task_id:
            raise ReviewActionError("task_id is required when changing the matched task.")
        return change_matched_task(db, review_id, new_task_id=task_id, notes=notes)
    elif act in ("reject", "dismiss", "unmatch"):
        return reject_match(db, review_id, reason=notes)
    else:
        raise ReviewActionError(
            f"Unknown resolution action '{action}'. Allowed actions: 'approve', 'change_match', 'reject'."
        )
