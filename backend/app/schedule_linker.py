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
import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

# All schedule tasks joined with their current AI result (LEFT JOIN so tasks
# without any AI result still appear). Uses DISTINCT ON to ensure 1:1 join per task.
_LINKED_TASKS_QUERY = text(
    """
    SELECT
        st.id                           AS task_id,
        st.source_task_id,
        st.activity,
        st.location,
        st.planned_start::TEXT          AS planned_start,
        st.planned_end::TEXT            AS planned_end,
        COALESCE(st.progress_percent, ap.progress_percent) AS progress_percent,
        COALESCE(st.status, ap.status)                     AS status,
        COALESCE(st.delay_days, ap.delay_days)             AS delay_days,
        COALESCE(st.delay_reason, ap.delay_reason)         AS delay_reason,
        COALESCE(st.actual_start_date::TEXT, ap.actual_start_date::TEXT) AS actual_start_date,
        COALESCE(st.actual_end_date::TEXT, ap.actual_end_date::TEXT)     AS actual_end_date,
        ap.confidence_score,
        ap.processed_at::TEXT           AS processed_at,
        COALESCE(ap.matched_task_id, st.id)              AS ai_matched_task_id
    FROM schedule_tasks st
    LEFT JOIN (
        SELECT DISTINCT ON (matched_task_id)
            matched_task_id,
            progress_percent,
            status,
            delay_days,
            delay_reason,
            actual_start_date,
            actual_end_date,
            confidence_score,
            processed_at
        FROM ai_processed_updates
        WHERE is_current = true
          AND matched_task_id IS NOT NULL
        ORDER BY matched_task_id, processed_at DESC
    ) ap ON ap.matched_task_id = st.id
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
        COALESCE(st.progress_percent, ap.progress_percent) AS progress_percent,
        COALESCE(st.status, ap.status)                     AS status,
        COALESCE(st.delay_days, ap.delay_days)             AS delay_days,
        COALESCE(st.delay_reason, ap.delay_reason)         AS delay_reason,
        COALESCE(st.actual_start_date::TEXT, ap.actual_start_date::TEXT) AS actual_start_date,
        COALESCE(st.actual_end_date::TEXT, ap.actual_end_date::TEXT)     AS actual_end_date,
        ap.confidence_score,
        ap.processed_at::TEXT           AS processed_at
    FROM schedule_tasks st
    LEFT JOIN (
        SELECT DISTINCT ON (matched_task_id)
            matched_task_id,
            progress_percent,
            status,
            delay_days,
            delay_reason,
            actual_start_date,
            actual_end_date,
            confidence_score,
            processed_at
        FROM ai_processed_updates
        WHERE is_current = true
          AND matched_task_id IS NOT NULL
        ORDER BY matched_task_id, processed_at DESC
    ) ap ON ap.matched_task_id = st.id
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
        COALESCE(st.progress_percent, ap.progress_percent) AS progress_percent,
        COALESCE(st.status, ap.status)                     AS status,
        COALESCE(st.delay_days, ap.delay_days)             AS delay_days,
        COALESCE(st.actual_start_date::TEXT, ap.actual_start_date::TEXT) AS actual_start_date,
        COALESCE(st.actual_end_date::TEXT, ap.actual_end_date::TEXT)     AS actual_end_date,
        ap.confidence_score,
        ap.processed_at::TEXT           AS processed_at
    FROM schedule_tasks st
    LEFT JOIN (
        SELECT DISTINCT ON (matched_task_id)
            matched_task_id,
            progress_percent,
            status,
            delay_days,
            delay_reason,
            actual_start_date,
            actual_end_date,
            confidence_score,
            processed_at
        FROM ai_processed_updates
        WHERE is_current = true
          AND matched_task_id IS NOT NULL
        ORDER BY matched_task_id, processed_at DESC
    ) ap ON ap.matched_task_id = st.id
    """
)


# ---------------------------------------------------------------------------
# Schedule deviation calculation
# ---------------------------------------------------------------------------

def _parse_date_obj(val: Any) -> datetime.date | None:
    """Safely parse a date/datetime/ISO-string into a datetime.date object."""
    if val is None:
        return None
    if isinstance(val, datetime.datetime):
        return val.date()
    if isinstance(val, datetime.date):
        return val
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned:
            return None
        try:
            return datetime.date.fromisoformat(cleaned[:10])
        except ValueError:
            return None
    return None


def calculate_schedule_deviation(
    planned_start: Any,
    planned_end: Any,
    actual_start: Any = None,
    actual_end: Any = None,
    status: str | None = None,
    progress_percent: float | None = None,
    delay_days: int | None = None,
    at_risk: bool = False,
) -> dict[str, Any]:
    """Calculate actual-vs-planned schedule deviation and health classification.

    Parameters
    ----------
    planned_start:    Baseline planned start date (date or ISO string).
    planned_end:      Baseline planned end date (date or ISO string).
    actual_start:     Actual start date if known (date or ISO string).
    actual_end:       Actual completion date if known (date or ISO string).
    status:           Current task status string (e.g. 'completed', 'delayed').
    progress_percent: Current progress percentage (0-100).
    delay_days:       Explicitly reported delay in days, if any.
    at_risk:          True if affected by an upstream dependency delay.

    Returns
    -------
    dict with:
      start_deviation_days     – (actual_start - planned_start). Positive = late start.
      end_deviation_days       – (actual_end - planned_end). Positive = late finish.
      duration_planned_days    – planned duration in calendar days.
      duration_actual_days     – actual duration in calendar days.
      duration_deviation_days  – (duration_actual - duration_planned).
      effective_delay_days     – consolidated delay in days.
      schedule_health          – 'on_time' | 'delayed' | 'at_risk' | 'unassessed'.
    """
    p_start = _parse_date_obj(planned_start)
    p_end = _parse_date_obj(planned_end)
    a_start = _parse_date_obj(actual_start)
    a_end = _parse_date_obj(actual_end)

    start_dev: int | None = None
    if p_start and a_start:
        start_dev = (a_start - p_start).days

    end_dev: int | None = None
    if p_end and a_end:
        end_dev = (a_end - p_end).days

    p_duration: int | None = None
    if p_start and p_end:
        p_duration = (p_end - p_start).days + 1

    a_duration: int | None = None
    if a_start and a_end:
        a_duration = (a_end - a_start).days + 1

    dur_dev: int | None = None
    if p_duration is not None and a_duration is not None:
        dur_dev = a_duration - p_duration

    # Effective delay in days
    effective_delay: int | None = None
    if end_dev is not None and end_dev > 0:
        effective_delay = end_dev
    elif delay_days is not None and delay_days > 0:
        effective_delay = delay_days
    elif start_dev is not None and start_dev > 0 and status != "completed":
        effective_delay = start_dev
    elif (end_dev is not None and end_dev <= 0) or delay_days == 0 or (start_dev is not None and start_dev <= 0 and status == "completed"):
        effective_delay = 0

    # Classification: on_time | delayed | at_risk | unassessed
    clean_status = (status or "").strip().lower()

    if clean_status == "delayed" or (effective_delay is not None and effective_delay > 0) or (end_dev is not None and end_dev > 0):
        health = "delayed"
    elif clean_status == "completed":
        health = "delayed" if (end_dev is not None and end_dev > 0) else "on_time"
    elif clean_status == "blocked" or at_risk:
        health = "at_risk"
    elif clean_status == "in_progress":
        health = "on_time"
    elif clean_status == "not_started":
        if start_dev is not None and start_dev > 0:
            health = "delayed"
        else:
            health = "on_time"
    elif p_start and a_start is None and a_end is None and clean_status == "" and delay_days is None:
        health = "unassessed"
    else:
        health = "on_time"

    return {
        "start_deviation_days": start_dev,
        "end_deviation_days": end_dev,
        "duration_planned_days": p_duration,
        "duration_actual_days": a_duration,
        "duration_deviation_days": dur_dev,
        "effective_delay_days": effective_delay,
        "schedule_health": health,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_linked(row: Any) -> dict[str, Any]:
    """Serialise a linked-tasks query row with schedule deviation metrics."""
    dev = calculate_schedule_deviation(
        planned_start=row.planned_start,
        planned_end=row.planned_end,
        actual_start=row.actual_start_date,
        actual_end=row.actual_end_date,
        status=row.status,
        progress_percent=float(row.progress_percent) if row.progress_percent is not None else None,
        delay_days=row.delay_days,
        at_risk=False,
    )
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
        "start_deviation_days": dev["start_deviation_days"],
        "end_deviation_days": dev["end_deviation_days"],
        "schedule_health": dev["schedule_health"],
        "deviation": dev,
    }


def _fmt_task(row: Any) -> dict[str, Any]:
    """Serialise a single task row with schedule deviation metrics."""
    dev = calculate_schedule_deviation(
        planned_start=row.planned_start,
        planned_end=row.planned_end,
        actual_start=row.actual_start_date,
        actual_end=row.actual_end_date,
        status=row.status,
        progress_percent=float(row.progress_percent) if row.progress_percent is not None else None,
        delay_days=row.delay_days,
        at_risk=False,
    )
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
        "start_deviation_days": dev["start_deviation_days"],
        "end_deviation_days": dev["end_deviation_days"],
        "schedule_health": dev["schedule_health"],
        "deviation": dev,
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


def get_schedule_deviation_summary(db: Session) -> dict[str, Any]:
    """Return overall deviation summary and all tasks with planned vs actual deviation.

    Evaluates:
    - start_deviation_days, end_deviation_days, duration_deviation_days
    - health classification (on_time, delayed, at_risk, unassessed)
    - downstream dependency risk propagation
    """
    tasks = get_linked_tasks(db)

    # Evaluate dependency graph to identify at-risk downstream tasks
    all_dep_rows = db.execute(_ALL_DEPENDENCIES_QUERY).fetchall()
    downstream_adj: dict[str, list[str]] = {}
    for dep in all_dep_rows:
        up = str(dep.upstream_task_id)
        down = str(dep.downstream_task_id)
        downstream_adj.setdefault(up, []).append(down)

    concerning_ids = {
        t["task_id"] for t in tasks if _is_concerning(t)
    }

    at_risk_ids: set[str] = set()
    for cid in concerning_ids:
        queue = deque(downstream_adj.get(cid, []))
        while queue:
            tid = queue.popleft()
            if tid not in at_risk_ids:
                at_risk_ids.add(tid)
                for next_tid in downstream_adj.get(tid, []):
                    if next_tid not in at_risk_ids:
                        queue.append(next_tid)

    # Propagate at_risk to downstream tasks if they are not already delayed or completed
    for t in tasks:
        if t["task_id"] in at_risk_ids and t.get("status") != "completed" and t.get("schedule_health") != "delayed":
            dev = calculate_schedule_deviation(
                planned_start=t["planned_start"],
                planned_end=t["planned_end"],
                actual_start=t["actual_start_date"],
                actual_end=t["actual_end_date"],
                status=t["status"],
                progress_percent=t["progress_percent"],
                delay_days=t["delay_days"],
                at_risk=True,
            )
            t["schedule_health"] = dev["schedule_health"]
            t["start_deviation_days"] = dev["start_deviation_days"]
            t["end_deviation_days"] = dev["end_deviation_days"]
            t["deviation"] = dev

    summary = {
        "total_tasks": len(tasks),
        "on_time": sum(1 for t in tasks if t["schedule_health"] == "on_time"),
        "delayed": sum(1 for t in tasks if t["schedule_health"] == "delayed"),
        "at_risk": sum(1 for t in tasks if t["schedule_health"] == "at_risk"),
        "unassessed": sum(1 for t in tasks if t["schedule_health"] == "unassessed"),
    }

    return {
        "summary": summary,
        "tasks": tasks,
    }


def get_task(db: Session, task_id: str) -> dict[str, Any] | None:
    """Return plan+actual data for a single schedule task, or None if not found."""
    row = db.execute(_SINGLE_TASK_QUERY, {"task_id": task_id}).fetchone()
    if row is None:
        return None
    return _fmt_task(row)


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
        if root_concerning and enriched.get("schedule_health") not in ("delayed", "completed"):
            enriched["schedule_health"] = "at_risk"
            if "deviation" in enriched and isinstance(enriched["deviation"], dict):
                dev_dict = dict(enriched["deviation"])
                dev_dict["schedule_health"] = "at_risk"
                enriched["deviation"] = dev_dict
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
