"""AI processing orchestration layer.

Responsibilities
----------------
1. Fetch the site_update row from the database.
2. Shortlist candidate schedule_tasks using multi-signal scoring
   (location, activity keywords, date proximity, alias normalization).
3. Call the configured AI provider to produce an AnalysisResult.
4. Mark any existing current ai_processed_updates row as is_current = false.
5. Insert the new ai_processed_updates row.
6. If confidence >= 80.0 (HIGH tier), auto-link and update schedule_tasks actuals.
7. Return a formatted dict suitable for the HTTP response.

Candidate Generation
--------------------
Rather than sending ALL schedule tasks to the AI provider,
`shortlist_candidates()` scores each task on multiple deterministic signals:
- Location match (alias-aware): 0–30 points
- Activity keyword overlap (alias-normalized): 0–50 points
- Schedule state compatibility (not already completed): 0–10 points
- Date plausibility (update date near task window): 0–10 points

The top-N candidates (default 30) are sent to the provider.  The shortlister
always preserves a minimum set of candidates for recall: if no high-scoring
tasks exist, the top 5 by planned date are included anyway.

Confidence Tiers
----------------
  HIGH  >= 80.0 → auto-link, update schedule actuals
  MEDIUM 50.0–79.9 → planner review (matched_task_id stored, not auto-updated)
  LOW   < 50.0 → unmatched review (matched_task_id = None; no false match stored)

This module does NOT import FastAPI; all database errors propagate to the
caller (main.py) which converts them to HTTP responses.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
from typing import Any

from sqlalchemy import bindparam, Float, Integer, String, text
from sqlalchemy.orm import Session

from app.ai_provider import (
    AIProvider,
    AnalysisResult,
    get_provider,
    normalize_with_aliases,
    _ALL_ALIAS_PAIRS,
    _ACTIVITY_SYNONYMS,
    _STOP_WORDS,
    _location_match_score,
)


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

_FETCH_SITE_UPDATE = text(
    """
    SELECT id, source_update_id, reported_on, location, raw_update, ingested_at, source_reference
    FROM site_updates
    WHERE CAST(id AS TEXT) = :update_id OR source_update_id = :update_id
    LIMIT 1
    """
)

_FETCH_ALL_SCHEDULE_TASKS = text(
    """
    SELECT id, source_task_id, activity, location,
           planned_start::TEXT AS planned_start,
           planned_end::TEXT   AS planned_end,
           COALESCE(status, 'not_started') AS status
    FROM schedule_tasks
    ORDER BY planned_start
    """
)

_EXPIRE_CURRENT = text(
    """
    UPDATE ai_processed_updates
    SET is_current = false
    WHERE site_update_id = :site_update_id
      AND is_current = true
    """
)

_UPDATE_TASK_ACTUALS = text(
    """
    UPDATE schedule_tasks
    SET
        last_reported_on = CASE
            WHEN last_reported_on IS NULL OR CAST(:reported_on AS DATE) >= last_reported_on THEN CAST(:reported_on AS DATE)
            ELSE last_reported_on
        END,
        progress_percent = CASE
            WHEN :progress_percent IS NULL THEN progress_percent
            WHEN progress_percent IS NULL THEN :progress_percent
            WHEN :progress_percent > progress_percent THEN :progress_percent
            ELSE progress_percent
        END,
        status = CASE
            WHEN status = 'completed' THEN 'completed'
            WHEN last_reported_on IS NOT NULL AND CAST(:reported_on AS DATE) < last_reported_on AND status IS NOT NULL THEN status
            ELSE COALESCE(:status, status)
        END,
        delay_days = CASE
            WHEN last_reported_on IS NOT NULL AND CAST(:reported_on AS DATE) < last_reported_on AND delay_days IS NOT NULL THEN delay_days
            ELSE COALESCE(:delay_days, delay_days)
        END,
        delay_reason = CASE
            WHEN last_reported_on IS NOT NULL AND CAST(:reported_on AS DATE) < last_reported_on AND delay_reason IS NOT NULL THEN delay_reason
            ELSE COALESCE(:delay_reason, delay_reason)
        END,
        actual_start_date = CASE
            WHEN actual_start_date IS NULL THEN CAST(:actual_start_date AS DATE)
            WHEN :actual_start_date IS NULL THEN actual_start_date
            WHEN CAST(:actual_start_date AS DATE) < actual_start_date THEN CAST(:actual_start_date AS DATE)
            ELSE actual_start_date
        END,
        actual_end_date = CASE
            WHEN actual_end_date IS NOT NULL THEN actual_end_date
            WHEN last_reported_on IS NOT NULL AND CAST(:reported_on AS DATE) < last_reported_on THEN actual_end_date
            ELSE COALESCE(CAST(:actual_end_date AS DATE), actual_end_date)
        END,
        updated_at = now()
    WHERE id = :task_id
    """
).bindparams(
    bindparam("progress_percent", type_=Float),
    bindparam("status", type_=String),
    bindparam("delay_days", type_=Integer),
    bindparam("delay_reason", type_=String),
    bindparam("actual_start_date", type_=String),
    bindparam("actual_end_date", type_=String),
    bindparam("reported_on", type_=String),
)

_INSERT_PROCESSED = text(
    """
    INSERT INTO ai_processed_updates (
        id,
        site_update_id,
        matched_task_id,
        progress_percent,
        status,
        delay_days,
        delay_reason,
        actual_start_date,
        actual_end_date,
        confidence_score,
        model_name,
        model_response,
        is_current
    )
    VALUES (
        :id,
        :site_update_id,
        :matched_task_id,
        :progress_percent,
        :status,
        :delay_days,
        :delay_reason,
        :actual_start_date,
        :actual_end_date,
        :confidence_score,
        :model_name,
        :model_response,
        true
    )
    RETURNING
        id,
        site_update_id,
        matched_task_id,
        progress_percent,
        status,
        delay_days,
        delay_reason,
        actual_start_date,
        actual_end_date,
        confidence_score,
        model_name,
        model_response,
        processed_at,
        is_current
    """
)

_FETCH_CURRENT_RESULT = text(
    """
    SELECT
        ap.id,
        ap.site_update_id,
        ap.matched_task_id,
        ap.progress_percent,
        ap.status,
        ap.delay_days,
        ap.delay_reason,
        ap.actual_start_date,
        ap.actual_end_date,
        ap.confidence_score,
        ap.model_name,
        ap.model_response,
        ap.processed_at,
        ap.is_current
    FROM ai_processed_updates ap
    JOIN site_updates su ON su.id = ap.site_update_id
    WHERE (CAST(ap.site_update_id AS TEXT) = :site_update_id OR su.source_update_id = :site_update_id)
      AND ap.is_current = true
    LIMIT 1
    """
)


# ---------------------------------------------------------------------------
# Formatting helper
# ---------------------------------------------------------------------------

def _format_result(row: Any) -> dict[str, Any]:
    """Serialize an ai_processed_updates row to a JSON-safe dict."""

    def _date(val: Any) -> str | None:
        if val is None:
            return None
        return val.isoformat() if hasattr(val, "isoformat") else str(val)

    model_resp = row.model_response
    if isinstance(model_resp, str):
        try:
            model_resp = json.loads(model_resp)
        except (ValueError, TypeError):
            pass

    return {
        "id": str(row.id),
        "site_update_id": str(row.site_update_id),
        "matched_task_id": str(row.matched_task_id) if row.matched_task_id else None,
        "progress_percent": float(row.progress_percent) if row.progress_percent is not None else None,
        "status": row.status,
        "delay_days": row.delay_days,
        "delay_reason": row.delay_reason,
        "actual_start_date": _date(row.actual_start_date),
        "actual_end_date": _date(row.actual_end_date),
        "confidence_score": float(row.confidence_score) if row.confidence_score is not None else None,
        "model_name": row.model_name,
        "model_response": model_resp,
        "processed_at": _date(row.processed_at),
        "is_current": row.is_current,
    }


# ---------------------------------------------------------------------------
# Multi-signal candidate shortlisting
# ---------------------------------------------------------------------------

_MAX_CANDIDATES = 30    # Maximum candidates sent to AI provider
_MIN_CANDIDATES = 5     # Minimum candidates always included (recall preservation)
_DATE_PROXIMITY_DAYS = 60   # Window (days) for date plausibility scoring


def _shortlist_score(
    task: dict[str, Any],
    raw_update: str,
    location: str,
    reported_on_str: str,
) -> float:
    """Score a single task for shortlisting using deterministic multi-signal heuristics.

    Signals and weights:
    - Location match (alias-aware):           0–30 points
    - Activity keyword overlap (normalized):  0–50 points
    - Schedule state compatibility:           0–10 points
    - Date plausibility:                      0–10 points

    Total max: 100 points.

    These are PRIORITY signals, not hard gates.  A task with score 0 may still
    appear in the candidate list if it's in the top-MIN_CANDIDATES by planned date.
    """
    norm_raw = normalize_with_aliases(raw_update, _ALL_ALIAS_PAIRS)
    norm_loc = normalize_with_aliases(location, _ALL_ALIAS_PAIRS)
    raw_lower = norm_raw.lower()
    loc_lower = norm_loc.lower()

    task_loc = str(task.get("location", "")).strip().lower()
    task_act = normalize_with_aliases(str(task.get("activity", "")), _ALL_ALIAS_PAIRS).strip().lower()

    # 1. Location match score (0–30)
    loc_signal = _location_match_score(task_loc, loc_lower, raw_update.lower())
    location_points = loc_signal * 10.0  # 0, 10, 20, 30

    # 2. Activity keyword overlap (0–50)
    act_words = [w for w in re.findall(r"\w+", task_act) if len(w) > 2]
    distinctive = [w for w in act_words if w not in _STOP_WORDS] or act_words
    keyword_points = 0.0
    if distinctive:
        matched = 0
        for tok in distinctive:
            if re.search(r"\b" + re.escape(tok) + r"\b", raw_lower):
                matched += 1
                continue
            # Check synonyms
            for root, syns in _ACTIVITY_SYNONYMS.items():
                if tok.startswith(root) or any(s in tok for s in syns):
                    if any(re.search(r"\b" + re.escape(s) + r"\b", raw_lower) for s in syns):
                        matched += 1
                        break
        keyword_points = (matched / len(distinctive)) * 50.0

    # 3. Schedule state compatibility (0–10)
    # Don't penalize hard if status not available — only give bonus for clearly non-completed
    task_status = str(task.get("status", "") or "").lower()
    state_points = 10.0 if task_status != "completed" else 2.0

    # 4. Date plausibility (0–10): update reported_on within ±DATE_PROXIMITY_DAYS of task window
    date_points = 5.0  # default neutral
    try:
        rep_date = datetime.date.fromisoformat(str(reported_on_str)[:10])
        plan_start_str = task.get("planned_start") or ""
        plan_end_str = task.get("planned_end") or ""
        if plan_start_str and plan_end_str:
            plan_start = datetime.date.fromisoformat(str(plan_start_str)[:10])
            plan_end = datetime.date.fromisoformat(str(plan_end_str)[:10])
            # Within or near the planned window
            earliest = plan_start - datetime.timedelta(days=_DATE_PROXIMITY_DAYS)
            latest = plan_end + datetime.timedelta(days=_DATE_PROXIMITY_DAYS)
            if earliest <= rep_date <= latest:
                # More points if update is actually within the task window
                if plan_start <= rep_date <= plan_end:
                    date_points = 10.0
                else:
                    date_points = 7.0
            else:
                date_points = 0.0
    except (ValueError, TypeError):
        date_points = 5.0  # can't parse → neutral

    return location_points + keyword_points + state_points + date_points


def shortlist_candidates(
    all_tasks: list[dict[str, Any]],
    raw_update: str,
    location: str,
    reported_on: str,
    max_candidates: int = _MAX_CANDIDATES,
    min_candidates: int = _MIN_CANDIDATES,
) -> list[dict[str, Any]]:
    """Select the most plausible candidate tasks for a site update.

    Uses multi-signal deterministic scoring to reduce a large schedule to the
    most relevant candidates before sending to the AI provider.

    Design principles:
    - Location, activity keywords, date proximity are PRIORITY SIGNALS, not
      hard gates.  A task with no location match is still a valid candidate
      if it scores high on activity keywords.
    - Recall preservation: always includes at least min_candidates tasks so
      that borderline matches are not discarded.
    - For small schedules (≤ max_candidates tasks), all tasks are returned.

    Parameters
    ----------
    all_tasks:      Full list of schedule tasks from the DB.
    raw_update:     Raw site update text.
    location:       Reported location from the site update.
    reported_on:    ISO date string of the update (YYYY-MM-DD).
    max_candidates: Maximum number of tasks to return (default 30).
    min_candidates: Minimum tasks always included (default 5).

    Returns
    -------
    Shortlisted list of task dicts, length between min_candidates and max_candidates.
    """
    if len(all_tasks) <= max_candidates:
        # Small schedule: no shortlisting needed; return all
        return all_tasks

    # Score each task
    scored: list[tuple[float, dict[str, Any]]] = []
    for task in all_tasks:
        score = _shortlist_score(task, raw_update, location, reported_on)
        scored.append((score, task))

    # Sort by score descending, then by planned_start for tie-breaking
    scored.sort(key=lambda x: (-x[0], x[1].get("planned_start") or ""))

    top_n = scored[:max_candidates]

    # Always guarantee min_candidates from the full list (recall preservation)
    # If we already have enough, just return top_n
    if len(top_n) >= min_candidates:
        return [t for _, t in top_n]

    # Fill to min_candidates from date-sorted remainder (shouldn't normally happen)
    included_ids = {t["id"] for _, t in top_n}
    date_sorted_remainder = sorted(
        [t for _, t in scored if t["id"] not in included_ids],
        key=lambda t: (t.get("planned_start") or ""),
    )
    for task in date_sorted_remainder[:min_candidates - len(top_n)]:
        top_n.append((0.0, task))

    return [t for _, t in top_n]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SiteUpdateNotFoundError(LookupError):
    """Raised when the requested site_update does not exist."""


def process_site_update(
    db: Session,
    update_id: str,
    provider: AIProvider | None = None,
) -> dict[str, Any]:
    """Run AI processing for a site update and persist the result.

    Parameters
    ----------
    db:         Open SQLAlchemy session.
    update_id:  UUID string of the site_updates row.
    provider:   Optional override for testing.  If None, uses get_provider().

    Returns
    -------
    Formatted dict of the newly inserted ai_processed_updates row.

    Raises
    ------
    SiteUpdateNotFoundError  – if the update_id does not exist.
    SQLAlchemyError          – propagated to caller on DB failure.
    """
    if provider is None:
        provider = get_provider()

    # 1. Fetch the site update
    update_row = db.execute(_FETCH_SITE_UPDATE, {"update_id": update_id}).fetchone()
    if update_row is None:
        raise SiteUpdateNotFoundError(f"Site update '{update_id}' not found")
    update_id = str(update_row.id)

    # 2. Fetch all schedule tasks from DB
    task_rows = db.execute(_FETCH_ALL_SCHEDULE_TASKS).fetchall()
    all_candidates = [
        {
            "id": str(row.id),
            "source_task_id": row.source_task_id,
            "activity": row.activity,
            "location": row.location,
            "planned_start": row.planned_start,
            "planned_end": row.planned_end,
            "status": getattr(row, "status", "not_started"),
        }
        for row in task_rows
    ]

    # 3. Shortlist candidates using multi-signal scoring
    reported_on_str = (
        update_row.reported_on.isoformat()
        if hasattr(update_row.reported_on, "isoformat")
        else str(update_row.reported_on)
    )
    candidate_tasks = shortlist_candidates(
        all_tasks=all_candidates,
        raw_update=update_row.raw_update or "",
        location=update_row.location or "",
        reported_on=reported_on_str,
    )

    # 4. Call provider with shortlisted candidates
    result: AnalysisResult = provider.analyse(
        raw_update=update_row.raw_update,
        location=update_row.location,
        reported_on=reported_on_str,
        candidate_tasks=candidate_tasks,
    )

    # 5. Expire the previous current record for this site update
    db.execute(_EXPIRE_CURRENT, {"site_update_id": update_id})

    # 6. Insert new row
    new_id = str(uuid.uuid4())
    inserted_row = db.execute(
        _INSERT_PROCESSED,
        {
            "id": new_id,
            "site_update_id": update_id,
            "matched_task_id": result.matched_task_id,
            "progress_percent": result.progress_percent,
            "status": result.status,
            "delay_days": result.delay_days,
            "delay_reason": result.delay_reason,
            "actual_start_date": result.actual_start_date,
            "actual_end_date": result.actual_end_date,
            "confidence_score": result.confidence_score,
            "model_name": result.model_name,
            "model_response": json.dumps(result.model_response),
        },
    ).fetchone()

    # 7. Auto-link: if matched with HIGH confidence (>= 80.0), update schedule_tasks actuals.
    #    MEDIUM (50–79.9): match stored for planner review, but schedule NOT auto-updated.
    #    LOW (< 50.0): matched_task_id is None (set by provider); no schedule update.
    #    (baseline planned_start/planned_end are NEVER overwritten;
    #     existing known values are never overwritten with NULL/UNKNOWN;
    #     older/lower-progress updates do not overwrite newer/higher-progress actuals)
    if result.matched_task_id and result.confidence_score >= 80.0:
        update_schedule_task_actuals(
            db=db,
            task_id=result.matched_task_id,
            reported_on=reported_on_str,
            progress_percent=result.progress_percent,
            status=result.status,
            delay_days=result.delay_days,
            delay_reason=result.delay_reason,
            actual_start_date=result.actual_start_date,
            actual_end_date=result.actual_end_date,
        )

    db.commit()
    return _format_result(inserted_row)



def update_schedule_task_actuals(
    db: Session,
    task_id: str,
    reported_on: str | datetime.date,
    progress_percent: float | None = None,
    status: str | None = None,
    delay_days: int | None = None,
    delay_reason: str | None = None,
    actual_start_date: str | datetime.date | None = None,
    actual_end_date: str | datetime.date | None = None,
) -> None:
    """Update schedule_tasks actual tracking columns from an AI-processed site update.

    Guarantees:
    - Baseline planned dates (planned_start, planned_end) are NEVER modified.
    - Never invents dates: only applies extracted FACT values.
    - Existing known values are preserved when incoming value is NULL/UNKNOWN.
    - Monotonic progress: lower progress does not overwrite higher progress.
    - Date order protection: older site update cannot overwrite newer status,
      delays, or end dates.
    - Completed status cannot be downgraded.
    - Earliest confirmed actual start date is preserved.
    """
    reported_on_str = (
        reported_on.isoformat()
        if isinstance(reported_on, (datetime.date, datetime.datetime))
        else str(reported_on)
    )
    start_str = (
        actual_start_date.isoformat()
        if isinstance(actual_start_date, (datetime.date, datetime.datetime))
        else actual_start_date
    )
    end_str = (
        actual_end_date.isoformat()
        if isinstance(actual_end_date, (datetime.date, datetime.datetime))
        else actual_end_date
    )

    db.execute(
        _UPDATE_TASK_ACTUALS,
        {
            "task_id": task_id,
            "reported_on": reported_on_str,
            "progress_percent": progress_percent,
            "status": status,
            "delay_days": delay_days,
            "delay_reason": delay_reason,
            "actual_start_date": start_str,
            "actual_end_date": end_str,
        },
    )


def get_current_result(
    db: Session,
    update_id: str,
) -> dict[str, Any] | None:
    """Return the current AI-processed result for a site update, or None."""
    row = db.execute(_FETCH_CURRENT_RESULT, {"site_update_id": update_id}).fetchone()
    if row is None:
        return None
    return _format_result(row)
