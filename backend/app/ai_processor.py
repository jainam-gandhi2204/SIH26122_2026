"""AI processing orchestration layer.

Responsibilities
----------------
1. Fetch the site_update row from the database.
2. Find candidate schedule_tasks for the same location.
3. Call the configured AI provider to produce an AnalysisResult.
4. Mark any existing current ai_processed_updates row as is_current = false.
5. Insert the new ai_processed_updates row.
6. Return a formatted dict suitable for the HTTP response.

This module does NOT import FastAPI; all database errors propagate to the
caller (main.py) which converts them to HTTP responses.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

from sqlalchemy import bindparam, Float, Integer, String, text
from sqlalchemy.orm import Session

from app.ai_provider import AIProvider, AnalysisResult, get_provider


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

_FETCH_TASKS_BY_LOCATION = text(
    """
    SELECT id, source_task_id, activity, location,
           planned_start::TEXT AS planned_start,
           planned_end::TEXT   AS planned_end
    FROM schedule_tasks
    WHERE location = :location
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

    # 2. Candidate tasks at the same location
    task_rows = db.execute(
        _FETCH_TASKS_BY_LOCATION, {"location": update_row.location}
    ).fetchall()
    candidate_tasks = [
        {
            "id": str(row.id),
            "source_task_id": row.source_task_id,
            "activity": row.activity,
            "location": row.location,
            "planned_start": row.planned_start,
            "planned_end": row.planned_end,
        }
        for row in task_rows
    ]

    # 3. Call provider
    reported_on_str = (
        update_row.reported_on.isoformat()
        if hasattr(update_row.reported_on, "isoformat")
        else str(update_row.reported_on)
    )
    result: AnalysisResult = provider.analyse(
        raw_update=update_row.raw_update,
        location=update_row.location,
        reported_on=reported_on_str,
        candidate_tasks=candidate_tasks,
    )

    # 4. Expire the previous current record for this site update
    db.execute(_EXPIRE_CURRENT, {"site_update_id": update_id})

    # 5. Insert new row
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

    # 6. If matched to a planned task, automatically update schedule_tasks actuals
    #    (baseline planned dates planned_start/planned_end are NEVER overwritten;
    #     existing known values are never overwritten with NULL/UNKNOWN;
    #     older/lower-progress updates do not overwrite newer/higher-progress actuals)
    if result.matched_task_id:
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
