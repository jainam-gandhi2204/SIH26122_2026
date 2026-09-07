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

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai_provider import AIProvider, AnalysisResult, get_provider


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

_FETCH_SITE_UPDATE = text(
    """
    SELECT id, source_update_id, reported_on, location, raw_update, ingested_at, source_reference
    FROM site_updates
    WHERE source_update_id = :update_id
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
    FROM ai_processed_updates
    WHERE site_update_id = :site_update_id
      AND is_current = true
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

    db.commit()
    return _format_result(inserted_row)


def get_current_result(
    db: Session,
    update_id: str,
) -> dict[str, Any] | None:
    """Return the current AI-processed result for a site update, or None."""
    row = db.execute(_FETCH_CURRENT_RESULT, {"site_update_id": update_id}).fetchone()
    if row is None:
        return None
    return _format_result(row)
