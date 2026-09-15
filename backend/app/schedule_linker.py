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
    WHERE (CAST(st.id AS TEXT) = :task_id OR st.source_task_id = :task_id)
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
      duration_planned_days    – planned duration in calendar days (inclusive).
      duration_actual_days     – actual duration in calendar days (inclusive).
      duration_deviation_days  – (duration_actual - duration_planned).
      effective_delay_days     – consolidated delay in days.
      slip_days                – confirmed schedule slip (never negative, 0 for on-time).
      remaining_work_percent   – 0.0 for completed, 100 - progress for in-progress.
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

    clean_status = (status or "").strip().lower()
    is_completed = (
        clean_status == "completed"
        or (progress_percent is not None and float(progress_percent) >= 100.0)
    )

    # Remaining work percentage
    if is_completed:
        remaining_work_percent: float | None = 0.0
    elif progress_percent is not None:
        remaining_work_percent = max(0.0, round(100.0 - float(progress_percent), 1))
    else:
        remaining_work_percent = None

    # Effective delay in days
    effective_delay: int | None = None
    if end_dev is not None and end_dev > 0:
        effective_delay = end_dev
    elif delay_days is not None and delay_days > 0:
        effective_delay = delay_days
    elif start_dev is not None and start_dev > 0 and not is_completed:
        effective_delay = start_dev
    elif (
        (end_dev is not None and end_dev <= 0)
        or delay_days == 0
        or (start_dev is not None and start_dev <= 0 and is_completed)
        or is_completed
    ):
        effective_delay = 0

    # Confirmed schedule slip (never negative; 0 if on-time or early)
    slip_days: int = max(0, effective_delay) if effective_delay is not None else 0

    # Classification: on_time | delayed | at_risk | unassessed
    if clean_status == "delayed" or (effective_delay is not None and effective_delay > 0) or (end_dev is not None and end_dev > 0):
        health = "delayed"
    elif is_completed:
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
        "slip_days": slip_days,
        "remaining_work_percent": remaining_work_percent,
        "schedule_health": health,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_linked(row: Any) -> dict[str, Any]:
    """Serialise a linked-tasks query row with schedule deviation metrics."""
    raw_status = row.status
    raw_prog = float(row.progress_percent) if row.progress_percent is not None else None
    if raw_status == "completed" and (raw_prog is None or raw_prog < 100.0):
        raw_prog = 100.0
    if raw_prog is not None and raw_prog >= 100.0:
        raw_status = "completed"

    dev = calculate_schedule_deviation(
        planned_start=row.planned_start,
        planned_end=row.planned_end,
        actual_start=row.actual_start_date,
        actual_end=row.actual_end_date,
        status=raw_status,
        progress_percent=raw_prog,
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
        "progress_percent": raw_prog,
        "status": raw_status,
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
        "slip_days": dev["slip_days"],
        "remaining_work_percent": dev["remaining_work_percent"],
        "schedule_health": dev["schedule_health"],
        "deviation": dev,
    }


def _fmt_task(row: Any) -> dict[str, Any]:
    """Serialise a single task row with schedule deviation metrics."""
    raw_status = row.status
    raw_prog = float(row.progress_percent) if row.progress_percent is not None else None
    if raw_status == "completed" and (raw_prog is None or raw_prog < 100.0):
        raw_prog = 100.0
    if raw_prog is not None and raw_prog >= 100.0:
        raw_status = "completed"

    dev = calculate_schedule_deviation(
        planned_start=row.planned_start,
        planned_end=row.planned_end,
        actual_start=row.actual_start_date,
        actual_end=row.actual_end_date,
        status=raw_status,
        progress_percent=raw_prog,
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
        "progress_percent": raw_prog,
        "status": raw_status,
        "delay_days": row.delay_days,
        "delay_reason": getattr(row, "delay_reason", None),
        "actual_start_date": row.actual_start_date,
        "actual_end_date": row.actual_end_date,
        "confidence_score": (
            float(row.confidence_score) if row.confidence_score is not None else None
        ),
        "processed_at": row.processed_at,
        "start_deviation_days": dev["start_deviation_days"],
        "end_deviation_days": dev["end_deviation_days"],
        "slip_days": dev["slip_days"],
        "remaining_work_percent": dev["remaining_work_percent"],
        "schedule_health": dev["schedule_health"],
        "deviation": dev,
    }


# ---------------------------------------------------------------------------
# CPM Forward-Pass Helpers
# ---------------------------------------------------------------------------

def _get_task_forecast_finish(t_data: dict[str, Any]) -> datetime.date | None:
    """Determine the forecast or actual finish date for a task."""
    p_end = _parse_date_obj(t_data.get("planned_end"))
    p_start = _parse_date_obj(t_data.get("planned_start"))
    a_end = _parse_date_obj(t_data.get("actual_end_date"))
    status = (t_data.get("status") or "").strip().lower()
    progress = t_data.get("progress_percent")
    delay_days = t_data.get("delay_days")

    if a_end:
        return a_end

    is_completed = status == "completed" or (progress is not None and float(progress) >= 100.0)
    if is_completed:
        return p_end

    if p_end and delay_days is not None and delay_days > 0:
        return p_end + datetime.timedelta(days=delay_days)

    a_start = _parse_date_obj(t_data.get("actual_start_date"))
    if p_start and p_end and a_start and a_start > p_start:
        start_slip = (a_start - p_start).days
        return p_end + datetime.timedelta(days=start_slip)

    return p_end


def _calculate_earliest_start_from_pred(
    u_finish: datetime.date | None,
    u_plan_end: datetime.date | None,
    v_plan_start: datetime.date | None,
) -> datetime.date | None:
    """Calculate the earliest feasible start date that predecessor u allows for successor v.

    Unified finish-to-start rule:
    - If baseline had contiguous or positive buffer (v_plan_start >= u_plan_end + 1 day):
        ES = u_finish + 1 day
    - If baseline had lead time / overlap (v_plan_start < u_plan_end + 1 day):
        delay_u = max(0, (u_finish - u_plan_end).days)
        ES = v_plan_start + delay_u
    """
    if u_finish is None or v_plan_start is None:
        return v_plan_start or (u_finish + datetime.timedelta(days=1) if u_finish else None)

    if u_plan_end and v_plan_start >= u_plan_end + datetime.timedelta(days=1):
        return u_finish + datetime.timedelta(days=1)
    elif u_plan_end:
        delay_u = max(0, (u_finish - u_plan_end).days)
        return v_plan_start + datetime.timedelta(days=delay_u)
    else:
        return u_finish + datetime.timedelta(days=1)


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
    - downstream dependency risk propagation using deterministic CPM
    """
    tasks = get_linked_tasks(db)
    all_dep_rows = db.execute(_ALL_DEPENDENCIES_QUERY).fetchall()

    downstream_adj: dict[str, list[str]] = {}
    upstream_adj: dict[str, list[str]] = {}
    for dep in all_dep_rows:
        up = str(dep.upstream_task_id)
        down = str(dep.downstream_task_id)
        downstream_adj.setdefault(up, []).append(down)
        upstream_adj.setdefault(down, []).append(up)

    task_map = {t["task_id"]: t for t in tasks}

    # Topologically sort all tasks
    in_degree = {t["task_id"]: 0 for t in tasks}
    for t in tasks:
        tid = t["task_id"]
        for u in upstream_adj.get(tid, []):
            if u in task_map:
                in_degree[tid] += 1

    topo_queue = deque(
        sorted(
            [tid for tid, deg in in_degree.items() if deg == 0],
            key=lambda x: str(task_map.get(x, {}).get("planned_start") or ""),
        )
    )

    topo_order: list[str] = []
    while topo_queue:
        curr = topo_queue.popleft()
        topo_order.append(curr)
        for nxt in downstream_adj.get(curr, []):
            if nxt in in_degree:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    topo_queue.append(nxt)

    for t in tasks:
        if t["task_id"] not in topo_order:
            topo_order.append(t["task_id"])

    # Forward pass to detect tasks genuinely pushed by upstream delays
    forecast_finish_map: dict[str, datetime.date | None] = {}
    at_risk_ids: set[str] = set()

    for tid in topo_order:
        t_data = task_map.get(tid, {})
        v_plan_start = _parse_date_obj(t_data.get("planned_start"))
        v_plan_end = _parse_date_obj(t_data.get("planned_end"))
        v_duration = (
            (v_plan_end - v_plan_start).days + 1
            if (v_plan_start and v_plan_end)
            else 1
        )
        clean_status = (t_data.get("status") or "").strip().lower()
        is_completed = (
            clean_status == "completed"
            or (t_data.get("progress_percent") is not None and float(t_data["progress_percent"]) >= 100.0)
        )

        base_finish = _get_task_forecast_finish(t_data)
        preds = upstream_adj.get(tid, [])

        if preds and not is_completed:
            max_es: datetime.date | None = None
            for u in preds:
                u_data = task_map.get(u, {})
                u_plan_end = _parse_date_obj(u_data.get("planned_end"))
                u_finish = forecast_finish_map.get(u) or _get_task_forecast_finish(u_data)
                es_cand = _calculate_earliest_start_from_pred(u_finish, u_plan_end, v_plan_start)
                if max_es is None or (es_cand is not None and es_cand > max_es):
                    max_es = es_cand

            if max_es and v_plan_start and max_es > v_plan_start:
                at_risk_ids.add(tid)
                forecast_start = max_es
                base_finish = forecast_start + datetime.timedelta(days=max(0, v_duration - 1))

        if clean_status == "blocked":
            at_risk_ids.add(tid)

        forecast_finish_map[tid] = base_finish

    # Apply at_risk to tasks that are not already completed or delayed
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
            t["slip_days"] = dev["slip_days"]
            t["remaining_work_percent"] = dev["remaining_work_percent"]
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

    Uses deterministic Critical Path Method (CPM) forward-pass analysis.
    For each reachable downstream task, calculates:
    - Earliest feasible start date based on all predecessor constraints
    - Controlling predecessor
    - Schedule buffer/slack absorption
    - Exact cascade slip days
    """
    root_row = db.execute(_SINGLE_TASK_QUERY, {"task_id": task_id}).fetchone()
    if root_row is None:
        return None

    root = _fmt_task(root_row)
    root_uuid = str(root_row.task_id)

    # Load all tasks and all dependency edges from DB
    all_task_rows = db.execute(_ALL_TASKS_QUERY).fetchall()
    all_dep_rows = db.execute(_ALL_DEPENDENCIES_QUERY).fetchall()

    task_lookup: dict[str, dict] = {str(r.task_id): _fmt_task(r) for r in all_task_rows}
    task_lookup[root_uuid] = root

    downstream_adj: dict[str, list[str]] = {}
    upstream_adj: dict[str, list[str]] = {}
    for dep in all_dep_rows:
        up = str(dep.upstream_task_id)
        down = str(dep.downstream_task_id)
        downstream_adj.setdefault(up, []).append(down)
        upstream_adj.setdefault(down, []).append(up)

    # Find all reachable descendants of root_uuid via BFS
    descendant_ids: set[str] = set()
    queue = deque(downstream_adj.get(root_uuid, []))
    while queue:
        curr = queue.popleft()
        if curr not in descendant_ids and curr != root_uuid:
            descendant_ids.add(curr)
            for nxt in downstream_adj.get(curr, []):
                if nxt not in descendant_ids and nxt != root_uuid:
                    queue.append(nxt)

    # Calculate root forecast finish and root slip days
    root_plan_end = _parse_date_obj(root.get("planned_end"))
    root_finish = _get_task_forecast_finish(root)
    if root_plan_end and root_finish:
        root_slip = max(0, (root_finish - root_plan_end).days)
    else:
        root_slip = root.get("deviation", {}).get("slip_days", 0)

    if not descendant_ids:
        return {
            "task": root,
            "slip_days": root_slip,
            "total_descendants_count": 0,
            "downstream_affected_count": 0,
            "downstream": [],
        }

    # Topologically sort descendant_ids using Kahn's algorithm
    in_degree: dict[str, int] = {v: 0 for v in descendant_ids}
    for v in descendant_ids:
        for u in upstream_adj.get(v, []):
            if u in descendant_ids:
                in_degree[v] += 1

    topo_queue = deque(
        sorted(
            [v for v, deg in in_degree.items() if deg == 0],
            key=lambda tid: str(task_lookup.get(tid, {}).get("planned_start") or ""),
        )
    )

    topo_order: list[str] = []
    while topo_queue:
        curr = topo_queue.popleft()
        topo_order.append(curr)
        for nxt in downstream_adj.get(curr, []):
            if nxt in descendant_ids:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    topo_queue.append(nxt)

    for v in sorted(
        descendant_ids,
        key=lambda tid: str(task_lookup.get(tid, {}).get("planned_start") or ""),
    ):
        if v not in topo_order:
            topo_order.append(v)

    # Forward pass: maintain forecast finish map
    forecast_finish_map: dict[str, datetime.date | None] = {}
    forecast_finish_map[root_uuid] = root_finish
    for tid, tdata in task_lookup.items():
        if tid not in descendant_ids and tid != root_uuid:
            forecast_finish_map[tid] = _get_task_forecast_finish(tdata)

    downstream: list[dict[str, Any]] = []
    for v in topo_order:
        t_data = task_lookup.get(v, {})
        v_plan_start = _parse_date_obj(t_data.get("planned_start"))
        v_plan_end = _parse_date_obj(t_data.get("planned_end"))
        v_duration = (
            (v_plan_end - v_plan_start).days + 1
            if (v_plan_start and v_plan_end)
            else 1
        )

        preds = upstream_adj.get(v, [])
        max_es: datetime.date | None = None
        controlling_pred_id: str | None = None
        max_push: int = -999999

        for u in preds:
            u_data = task_lookup.get(u, {})
            u_plan_end = _parse_date_obj(u_data.get("planned_end"))
            u_finish = forecast_finish_map.get(u) or _get_task_forecast_finish(u_data)
            es_cand = _calculate_earliest_start_from_pred(u_finish, u_plan_end, v_plan_start)

            push = (es_cand - v_plan_start).days if (es_cand and v_plan_start) else 0
            if (
                max_es is None
                or (es_cand is not None and es_cand > max_es)
                or (es_cand is not None and es_cand == max_es and push > max_push)
            ):
                max_es = es_cand
                controlling_pred_id = u
                max_push = push

        es_date = max_es or v_plan_start

        cascade_slip = 0
        if v_plan_start and es_date and es_date > v_plan_start:
            cascade_slip = (es_date - v_plan_start).days

        forecast_start = (
            max(v_plan_start, es_date)
            if (v_plan_start and es_date)
            else (v_plan_start or es_date)
        )
        forecast_end = (
            forecast_start + datetime.timedelta(days=max(0, v_duration - 1))
            if forecast_start
            else v_plan_end
        )
        forecast_finish_map[v] = forecast_end

        controlling_task = task_lookup.get(controlling_pred_id, {}) if controlling_pred_id else {}
        controlling_source_id = controlling_task.get("source_task_id", controlling_pred_id)

        root_source = root.get("source_task_id") or "upstream task"
        root_concerning = _is_concerning(root)

        at_risk = (cascade_slip > 0) or (root_concerning and t_data.get("status") != "completed")
        impact_type = "delayed_start" if cascade_slip > 0 else ("pending_upstream" if at_risk else "on_track")

        if cascade_slip > 0:
            risk_reason = (
                f"Upstream task {root_source} ({root.get('status') or 'delayed'}) cascades through "
                f"{controlling_source_id or root_source}, pushing earliest feasible start "
                f"to {es_date.isoformat() if es_date else 'TBD'} (+{cascade_slip}d slip)."
            )
        elif at_risk:
            risk_reason = (
                f"Upstream task {root_source} is {root.get('status') or 'incomplete'} "
                f"(progress {root.get('progress_percent')}%), which may delay the start of this task."
            )
        elif controlling_source_id:
            risk_reason = (
                f"Predecessor constraints satisfied. Upstream task {root_source} variance "
                f"absorbed by schedule buffer; on track for {v_plan_start.isoformat() if v_plan_start else 'planned start'}."
            )
        else:
            risk_reason = f"Upstream task {root_source} completed. No delay detected; on schedule."

        current_health = t_data.get("schedule_health", "on_time")
        if at_risk and current_health not in ("delayed", "completed") and t_data.get("status") != "completed":
            final_health = "at_risk"
        else:
            final_health = current_health

        enriched = dict(t_data)
        enriched["earliest_feasible_start"] = es_date.isoformat() if es_date else None
        enriched["forecast_start"] = forecast_start.isoformat() if forecast_start else None
        enriched["forecast_end"] = forecast_end.isoformat() if forecast_end else None
        enriched["controlling_predecessor"] = controlling_source_id
        enriched["cascade_slip_days"] = cascade_slip
        enriched["at_risk"] = at_risk
        enriched["impact_type"] = impact_type
        enriched["assessment_type"] = "estimated"
        enriched["risk_reason"] = risk_reason
        enriched["schedule_health"] = final_health
        if "deviation" in enriched and isinstance(enriched["deviation"], dict):
            dev_copy = dict(enriched["deviation"])
            dev_copy["schedule_health"] = final_health
            dev_copy["cascade_slip_days"] = cascade_slip
            dev_copy["at_risk"] = at_risk
            enriched["deviation"] = dev_copy

        downstream.append(enriched)

    downstream_affected_count = sum(
        1 for d in downstream if d["cascade_slip_days"] > 0 or d["at_risk"]
    )

    return {
        "task": root,
        "slip_days": root_slip,
        "total_descendants_count": len(downstream),
        "downstream_affected_count": downstream_affected_count,
        "downstream": downstream,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_INCOMPLETE_STATUSES = frozenset({"delayed", "blocked", "in_progress", "not_started"})


def _is_concerning(task: dict[str, Any]) -> bool:
    """Return True if a task has active delays, blockages, or incomplete progress."""
    status = task.get("status")
    if status is None:
        return False
    if status in ("delayed", "blocked"):
        return True
    if status in ("in_progress", "not_started"):
        progress = task.get("progress_percent")
        return progress is None or progress < 100.0
    return False
