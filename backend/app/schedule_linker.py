"""Schedule linking and downstream impact analysis.

Responsibilities
----------------
1. get_linked_tasks(db) — return all schedule tasks joined with their latest
   AI-processed status/progress from ai_processed_updates (is_current=true).
   Tasks without any AI result are included with null actuals.

2. get_task_impact(db, task_id) — return a single task's plan+actual data,
   then traverse task_dependencies downstream (BFS) to identify every task
   that is directly or transitively blocked by the upstream task's delay or
   incomplete status.

Downstream impact model
-----------------------
We deliberately do NOT propagate delay_days to downstream tasks — doing so
would require replanning logic that is out of scope for an SIH prototype and
would hallucinate exact dates.  Instead each downstream task receives:

  "at_risk": true
  "risk_reason": "<source_task_id> is <status>/incomplete, which may affect
                  this task's start date"

This is honest, auditable, and enough to demonstrate the capability for the
SIH judges.

No FastAPI imports — this module is fully unit-testable with mock DB sessions.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

# All schedule tasks joined with their current AI result (LEFT JOIN so tasks
# without any AI result still appear).
_LINKED_TASKS_QUERY = text(
    """
    SELECT
        st.id                           AS task_id,
        st.source_task_id,
        st.activity,
        st.location,
        st.planned_start::TEXT          AS planned_start,
        st.planned_end::TEXT            AS planned_end,
        ap.progress_percent,
        ap.status,
        ap.delay_days,
        ap.delay_reason,
        ap.actual_start_date::TEXT      AS actual_start_date,
        ap.actual_end_date::TEXT        AS actual_end_date,
        ap.confidence_score,
        ap.processed_at::TEXT           AS processed_at,
        ap.matched_task_id              AS ai_matched_task_id
    FROM schedule_tasks st
    LEFT JOIN ai_processed_updates ap
           ON ap.matched_task_id = st.id
          AND ap.is_current = true
    ORDER BY st.planned_start, st.source_task_id
    """
)

# All task_dependencies rows — fetched once and filtered in Python for BFS.
_ALL_DEPENDENCIES_QUERY = text(
    """
    SELECT
        td.task_id              AS downstream_task_id,
        td.depends_on_task_id   AS upstream_task_id
    FROM task_dependencies td
    """
)

# Single task lookup by UUID.
_SINGLE_TASK_QUERY = text(
    """
    SELECT
        st.id                           AS task_id,
        st.source_task_id,
        st.activity,
        st.location,
        st.planned_start::TEXT          AS planned_start,
        st.planned_end::TEXT            AS planned_end,
        ap.progress_percent,
        ap.status,
        ap.delay_days,
        ap.delay_reason,
        ap.actual_start_date::TEXT      AS actual_start_date,
        ap.actual_end_date::TEXT        AS actual_end_date,
        ap.confidence_score,
        ap.processed_at::TEXT           AS processed_at
    FROM schedule_tasks st
    LEFT JOIN ai_processed_updates ap
           ON ap.matched_task_id = st.id
          AND ap.is_current = true
    WHERE st.id = :task_id
    """
)

# All schedule tasks (id + source_task_id + basics) for BFS node lookup.
_ALL_TASKS_QUERY = text(
    """
    SELECT
        st.id                           AS task_id,
        st.source_task_id,
        st.activity,
        st.location,
        st.planned_start::TEXT          AS planned_start,
        st.planned_end::TEXT            AS planned_end,
        ap.progress_percent,
        ap.status,
        ap.delay_days,
        ap.actual_start_date::TEXT      AS actual_start_date,
        ap.actual_end_date::TEXT        AS actual_end_date,
        ap.confidence_score,
        ap.processed_at::TEXT           AS processed_at
    FROM schedule_tasks st
    LEFT JOIN ai_processed_updates ap
           ON ap.matched_task_id = st.id
          AND ap.is_current = true
    """
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_linked(row: Any) -> dict[str, Any]:
    """Serialise a linked-tasks query row."""
    return {
        "task_id": str(row.task_id),
        "source_task_id": row.source_task_id,
        "activity": row.activity,
        "location": row.location,
        "planned_start": row.planned_start,
        "planned_end": row.planned_end,
        # Actual / AI fields (null when no AI result exists yet)
        "progress_percent": (
            float(row.progress_percent) if row.progress_percent is not None else None
        ),
        "status": row.status,
        "delay_days": row.delay_days,
        "delay_reason": row.delay_reason,
        "actual_start_date": row.actual_start_date,
        "actual_end_date": row.actual_end_date,
        "confidence_score": (
            float(row.confidence_score) if row.confidence_score is not None else None
        ),
        "processed_at": row.processed_at,
    }


def _fmt_task(row: Any) -> dict[str, Any]:
    """Serialise a single task row (from _ALL_TASKS_QUERY or _SINGLE_TASK_QUERY)."""
    return {
        "task_id": str(row.task_id),
        "source_task_id": row.source_task_id,
        "activity": row.activity,
        "location": row.location,
        "planned_start": row.planned_start,
        "planned_end": row.planned_end,
        "progress_percent": (
            float(row.progress_percent) if row.progress_percent is not None else None
        ),
        "status": row.status,
        "delay_days": row.delay_days,
        "actual_start_date": row.actual_start_date,
        "actual_end_date": row.actual_end_date,
        "confidence_score": (
            float(row.confidence_score) if row.confidence_score is not None else None
        ),
        "processed_at": row.processed_at,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_linked_tasks(db: Session) -> list[dict[str, Any]]:
    """Return all schedule tasks joined with their latest AI result.

    Tasks that have never been AI-processed are included with null actual fields.
    """
    rows = db.execute(_LINKED_TASKS_QUERY).fetchall()
    return [_fmt_linked(r) for r in rows]


def get_task_impact(db: Session, task_id: str) -> dict[str, Any] | None:
    """Return plan+actual data for one task plus its downstream impact chain.

    Parameters
    ----------
    db:       Open SQLAlchemy session.
    task_id:  UUID string of the schedule_tasks row.

    Returns
    -------
    Dict with keys:
      task          – the requested task's plan+actual data
      downstream    – list of directly and transitively affected tasks;
                      each entry has an ``at_risk`` flag and ``risk_reason``
    Or None if the task_id does not exist.
    """
    root_row = db.execute(_SINGLE_TASK_QUERY, {"task_id": task_id}).fetchone()
    if root_row is None:
        return None

    root = _fmt_task(root_row)

    # Determine whether the root task should flag downstream tasks as at-risk.
    # A task is "concerning" if it is delayed, blocked, or incomplete (<100%)
    # in status; tasks with no AI data or fully completed are not at risk.
    root_concerning = _is_concerning(root)

    # Load all tasks and all dependency edges from the DB in two flat queries.
    # Using Python BFS rather than a recursive CTE keeps the code simple and
    # compatible with any PostgreSQL version without needing WITH RECURSIVE.
    all_task_rows = db.execute(_ALL_TASKS_QUERY).fetchall()
    all_dep_rows = db.execute(_ALL_DEPENDENCIES_QUERY).fetchall()

    # Build lookup: task_id -> formatted dict
    task_lookup: dict[str, dict] = {str(r.task_id): _fmt_task(r) for r in all_task_rows}

    # Build adjacency: upstream_task_id -> list[downstream_task_id]
    downstream_adj: dict[str, list[str]] = {}
    for dep in all_dep_rows:
        up = str(dep.upstream_task_id)
        down = str(dep.downstream_task_id)
        downstream_adj.setdefault(up, []).append(down)

    # BFS from the root task to collect all downstream tasks
    downstream: list[dict[str, Any]] = []
    visited: set[str] = {task_id}
    queue: deque[str] = deque(downstream_adj.get(task_id, []))
    while queue:
        tid = queue.popleft()
        if tid in visited:
            continue
        visited.add(tid)

        task_data = task_lookup.get(tid, {})
        enriched = dict(task_data)
        enriched["at_risk"] = root_concerning
        enriched["risk_reason"] = (
            f"Upstream task {root['source_task_id']} is "
            f"{root['status'] or 'incomplete'} "
            f"(progress {root['progress_percent']}%), "
            f"which may delay the start of this task."
            if root_concerning
            else "No upstream delay detected."
        )
        downstream.append(enriched)

        # Enqueue this task's own downstream tasks
        for next_tid in downstream_adj.get(tid, []):
            if next_tid not in visited:
                queue.append(next_tid)

    return {
        "task": root,
        "downstream_affected_count": len(downstream),
        "downstream": downstream,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_INCOMPLETE_STATUSES = frozenset({"delayed", "blocked", "in_progress", "not_started"})


def _is_concerning(task: dict[str, Any]) -> bool:
    """Return True if a task should be flagged as at-risk for downstream tasks.

    A task is concerning when it has AI data and its status is delayed, blocked,
    or in-progress/not-started with less than 100% progress.
    Tasks with no AI data (status None) are treated as not yet assessed and are
    NOT flagged — we do not want to hallucinate risk.
    """
    status = task.get("status")
    if status is None:
        # No AI assessment yet — do not invent risk
        return False
    if status in ("delayed", "blocked"):
        return True
    if status in ("in_progress", "not_started"):
        progress = task.get("progress_percent")
        # Concerning if progress is unknown or less than 100
        return progress is None or progress < 100.0
    # "completed" with 100% — not concerning
    return False
