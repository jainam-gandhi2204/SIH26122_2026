"""CSV schedule importer: parsing, validation, and database insertion.

Kept separate from main.py so the logic can be unit-tested without FastAPI.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {"Task ID", "Activity", "Location", "Planned Start", "Planned End", "Dependency"}
DATE_FORMAT = "%d-%b-%Y"  # e.g. 01-Sep-2026
NO_DEPENDENCY = "-"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    source_task_id: str
    activity: str
    location: str
    planned_start: datetime.date
    planned_end: datetime.date
    dependencies: list[str]  # list of upstream source_task_ids; empty means none


@dataclass
class ValidationError:
    row_number: int  # 1-based, counting data rows (header = row 0)
    task_id: str
    message: str


@dataclass
class ImportResult:
    import_id: str
    filename: str
    tasks_imported: int
    dependencies_imported: int


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: str, field_name: str, row_number: int, task_id: str) -> datetime.date | ValidationError:
    """Parse a date string; return a ValidationError on failure."""
    stripped = raw.strip()
    try:
        return datetime.datetime.strptime(stripped, DATE_FORMAT).date()
    except ValueError:
        return ValidationError(
            row_number=row_number,
            task_id=task_id,
            message=f"{field_name} '{stripped}' is not a valid date (expected DD-Mon-YYYY, e.g. 01-Sep-2026)",
        )


def _strip_row(row: dict[str, Any]) -> dict[str, str]:
    """Strip whitespace from all keys and values in a CSV row dict.

    csv.DictReader uses None as the key for any overflow values when a row
    has more columns than the header.  We skip those safely here.
    """
    return {k.strip(): (v.strip() if v else "") for k, v in row.items() if k is not None}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_and_validate(content: bytes, filename: str) -> tuple[list[ParsedRow], list[ValidationError]]:
    """Parse CSV bytes and return (valid_rows, errors).

    All rows are inspected; errors are collected rather than failing fast so
    the caller can return a complete list of problems to the client.
    """
    try:
        text_content = content.decode("utf-8-sig")  # handle optional BOM
    except UnicodeDecodeError:
        return [], [ValidationError(0, "", "File could not be decoded as UTF-8")]

    reader = csv.DictReader(io.StringIO(text_content))

    # --- column check ---
    if reader.fieldnames is None:
        return [], [ValidationError(0, "", "CSV file is empty or has no header row")]

    present = {h.strip() for h in reader.fieldnames}
    missing = REQUIRED_COLUMNS - present
    if missing:
        return [], [
            ValidationError(0, "", f"Missing required columns: {', '.join(sorted(missing))}")
        ]

    rows: list[ParsedRow] = []
    errors: list[ValidationError] = []
    seen_task_ids: set[str] = set()

    for row_index, raw_row in enumerate(reader, start=1):
        row = _strip_row(raw_row)

        task_id = row.get("Task ID", "").strip()
        if not task_id:
            errors.append(ValidationError(row_index, "", "Task ID is empty"))
            continue

        if task_id in seen_task_ids:
            errors.append(ValidationError(row_index, task_id, f"Duplicate Task ID '{task_id}'"))
            continue
        seen_task_ids.add(task_id)

        activity = row.get("Activity", "").strip()
        if not activity:
            errors.append(ValidationError(row_index, task_id, "Activity is empty"))
            continue

        location = row.get("Location", "").strip()
        if not location:
            errors.append(ValidationError(row_index, task_id, "Location is empty"))
            continue

        start = _parse_date(row.get("Planned Start", ""), "Planned Start", row_index, task_id)
        if isinstance(start, ValidationError):
            errors.append(start)
            continue

        end = _parse_date(row.get("Planned End", ""), "Planned End", row_index, task_id)
        if isinstance(end, ValidationError):
            errors.append(end)
            continue

        if end < start:
            errors.append(ValidationError(
                row_index, task_id,
                f"Planned End ({end}) must be >= Planned Start ({start})"
            ))
            continue

        # Parse dependency field: split on comma, strip, drop "-" and blanks
        raw_dep = row.get("Dependency", "").strip()
        if not raw_dep or raw_dep == NO_DEPENDENCY:
            dep_list: list[str] = []
        else:
            dep_list = [d.strip() for d in raw_dep.split(",") if d.strip() and d.strip() != NO_DEPENDENCY]

        rows.append(ParsedRow(
            source_task_id=task_id,
            activity=activity,
            location=location,
            planned_start=start,
            planned_end=end,
            dependencies=dep_list,
        ))

    # --- dependency reference check (only against tasks that passed row validation) ---
    valid_ids = {r.source_task_id for r in rows}
    for row_index, parsed in enumerate(rows, start=1):
        for dep in parsed.dependencies:
            if dep not in valid_ids:
                errors.append(ValidationError(
                    row_index, parsed.source_task_id,
                    f"Dependency '{dep}' does not reference a known Task ID in this CSV"
                ))

    # Remove any rows whose task_id has a dependency error so we never insert partial data
    if errors:
        error_task_ids = {e.task_id for e in errors if e.task_id}
        rows = [r for r in rows if r.source_task_id not in error_task_ids]

    return rows, errors


def import_to_db(
    db: Session,
    rows: list[ParsedRow],
    filename: str,
    replace: bool = False,
) -> ImportResult:
    """Insert or update schedule_imports + schedule_tasks + task_dependencies.

    Idempotency strategy
    --------------------
    1. A new schedule_imports record is inserted to preserve the audit trail
       of each import attempt.
    2. If replace is True:
       - Clears existing task_dependencies and schedule_tasks (and detaches
         existing matched_task_id in ai_processed_updates) to establish a clean
         new project schedule baseline.
    3. If replace is False (default):
       - For each incoming row:
         - If a task with that source_task_id already exists in schedule_tasks,
           it is UPDATED in place. Its existing primary key (UUID) is preserved,
           ensuring that foreign-key relationships (such as
           ai_processed_updates.matched_task_id) remain intact.
         - If it does not exist, a new schedule_tasks row is INSERTED with a
           fresh UUID.
         - If duplicate rows exist for the same source_task_id (e.g. from prior
           buggy imports), the canonical task (prioritizing tasks already
           referenced by ai_processed_updates) is preserved and redundant
           duplicates are removed.
    4. Existing task_dependencies for the imported tasks are refreshed so
       that re-importing the same CSV does not duplicate dependency edges,
       while fully supporting single and multiple dependencies (e.g. T107 ->
       T104 and T106).

    Raises SQLAlchemyError on any database failure (caller handles rollback
    because the session is managed by FastAPI's get_db() generator).
    """
    import_id = str(uuid.uuid4())

    # 1. Insert the schedule_imports parent record (audit trail)
    db.execute(
        text(
            """
            INSERT INTO schedule_imports (id, source_filename, row_count)
            VALUES (:id, :filename, :row_count)
            """
        ),
        {"id": import_id, "filename": filename, "row_count": len(rows)},
    )

    if replace:
        db.execute(text("DELETE FROM task_dependencies"))
        # Archive pending review items and preserve historical audit trail
        current_ai_rows = db.execute(
            text(
                """
                SELECT id, matched_task_id, model_response
                FROM ai_processed_updates
                WHERE is_current = true
                """
            )
        ).fetchall()

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for ai_row in current_ai_rows:
            row_id = str(ai_row.id)
            raw_resp = ai_row.model_response
            model_resp: dict[str, Any] = {}
            if isinstance(raw_resp, dict):
                model_resp = dict(raw_resp)
            elif isinstance(raw_resp, str):
                try:
                    parsed = json.loads(raw_resp)
                    if isinstance(parsed, dict):
                        model_resp = parsed
                except (ValueError, TypeError):
                    pass

            old_status = model_resp.get("review_status")
            if old_status in (None, "pending"):
                model_resp["review_status"] = "historical"
            model_resp["archived_reason"] = "Schedule baseline was replaced"
            model_resp["archived_at"] = now_iso
            if ai_row.matched_task_id:
                model_resp["historical_matched_task_id"] = str(ai_row.matched_task_id)

            db.execute(
                text(
                    """
                    UPDATE ai_processed_updates
                    SET matched_task_id = NULL,
                        model_response = :model_response
                    WHERE id = :id
                    """
                ),
                {"id": row_id, "model_response": json.dumps(model_resp)},
            )

        # Nullify any remaining non-current references before clearing tasks
        db.execute(
            text(
                "UPDATE ai_processed_updates SET matched_task_id = NULL WHERE matched_task_id IS NOT NULL"
            )
        )
        db.execute(text("DELETE FROM schedule_tasks"))

    if not rows:
        db.commit()
        return ImportResult(
            import_id=import_id,
            filename=filename,
            tasks_imported=0,
            dependencies_imported=0,
        )

    # 2. Look up existing schedule_tasks by source_task_id to preserve UUIDs
    source_ids = [r.source_task_id for r in rows]
    placeholders = ", ".join(f":sid_{i}" for i in range(len(source_ids)))
    params = {f"sid_{i}": sid for i, sid in enumerate(source_ids)}

    existing_rows = db.execute(
        text(
            f"""
            SELECT st.id, st.source_task_id, count(ap.id) AS ai_count
            FROM schedule_tasks st
            LEFT JOIN ai_processed_updates ap ON ap.matched_task_id = st.id
            WHERE st.source_task_id IN ({placeholders})
            GROUP BY st.id, st.source_task_id, st.created_at
            ORDER BY count(ap.id) DESC, st.created_at ASC
            """
        ),
        params,
    ).fetchall()

    existing_map: dict[str, str] = {}
    duplicate_ids_to_clean: list[str] = []

    for r in existing_rows:
        sid = r.source_task_id
        tid = str(r.id)
        if sid not in existing_map:
            existing_map[sid] = tid
        else:
            duplicate_ids_to_clean.append(tid)

    # Clean up redundant duplicate rows if any existed from prior imports
    if duplicate_ids_to_clean:
        dup_placeholders = ", ".join(f":dup_{i}" for i in range(len(duplicate_ids_to_clean)))
        dup_params = {f"dup_{i}": did for i, did in enumerate(duplicate_ids_to_clean)}
        db.execute(
            text(f"DELETE FROM schedule_tasks WHERE id IN ({dup_placeholders})"),
            dup_params,
        )

    # 3. Upsert schedule_tasks (UPDATE existing to preserve UUID, INSERT new)
    task_id_map: dict[str, str] = {}  # source_task_id -> DB UUID
    for row in rows:
        if row.source_task_id in existing_map:
            task_uuid = existing_map[row.source_task_id]
            task_id_map[row.source_task_id] = task_uuid
            db.execute(
                text(
                    """
                    UPDATE schedule_tasks
                    SET schedule_import_id = :import_id,
                        activity = :activity,
                        location = :location,
                        planned_start = :planned_start,
                        planned_end = :planned_end,
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": task_uuid,
                    "import_id": import_id,
                    "activity": row.activity,
                    "location": row.location,
                    "planned_start": row.planned_start,
                    "planned_end": row.planned_end,
                },
            )
        else:
            task_uuid = str(uuid.uuid4())
            task_id_map[row.source_task_id] = task_uuid
            db.execute(
                text(
                    """
                    INSERT INTO schedule_tasks
                        (id, schedule_import_id, source_task_id, activity,
                         location, planned_start, planned_end)
                    VALUES
                        (:id, :import_id, :source_task_id, :activity,
                         :location, :planned_start, :planned_end)
                    """
                ),
                {
                    "id": task_uuid,
                    "import_id": import_id,
                    "source_task_id": row.source_task_id,
                    "activity": row.activity,
                    "location": row.location,
                    "planned_start": row.planned_start,
                    "planned_end": row.planned_end,
                },
            )

    # 4. Refresh task_dependencies for the imported tasks without duplication
    task_uuids = list(task_id_map.values())
    if task_uuids:
        tid_placeholders = ", ".join(f":tid_{i}" for i in range(len(task_uuids)))
        tid_params = {f"tid_{i}": tid for i, tid in enumerate(task_uuids)}
        db.execute(
            text(f"DELETE FROM task_dependencies WHERE task_id IN ({tid_placeholders})"),
            tid_params,
        )

    deps_inserted = 0
    for row in rows:
        downstream_uuid = task_id_map[row.source_task_id]
        for dep_source_id in row.dependencies:
            if dep_source_id in task_id_map:
                upstream_uuid = task_id_map[dep_source_id]
                db.execute(
                    text(
                        """
                        INSERT INTO task_dependencies (task_id, depends_on_task_id)
                        VALUES (:task_id, :depends_on_task_id)
                        ON CONFLICT (task_id, depends_on_task_id) DO NOTHING
                        """
                    ),
                    {
                        "task_id": downstream_uuid,
                        "depends_on_task_id": upstream_uuid,
                    },
                )
                deps_inserted += 1

    db.commit()

    return ImportResult(
        import_id=import_id,
        filename=filename,
        tasks_imported=len(rows),
        dependencies_imported=deps_inserted,
    )
