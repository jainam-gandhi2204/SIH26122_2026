"""CSV schedule importer: parsing, validation, and database insertion.

Kept separate from main.py so the logic can be unit-tested without FastAPI.
"""

from __future__ import annotations

import csv
import datetime
import io
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
    dependency: str  # raw string from CSV; "-" means none


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
    """Strip whitespace from all keys and values in a CSV row dict."""
    return {k.strip(): (v.strip() if v else "") for k, v in row.items()}


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

        dependency = row.get("Dependency", "").strip() or NO_DEPENDENCY

        rows.append(ParsedRow(
            source_task_id=task_id,
            activity=activity,
            location=location,
            planned_start=start,
            planned_end=end,
            dependency=dependency,
        ))

    # --- dependency reference check (only against tasks that passed row validation) ---
    valid_ids = {r.source_task_id for r in rows}
    for row_index, parsed in enumerate(rows, start=1):
        dep = parsed.dependency
        if dep != NO_DEPENDENCY and dep not in valid_ids:
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
) -> ImportResult:
    """Insert schedule_imports + schedule_tasks + task_dependencies.

    Raises SQLAlchemyError on any database failure (caller handles rollback
    because the session is managed by FastAPI's get_db() generator).
    """
    import_id = str(uuid.uuid4())

    # 1. Insert the schedule_imports parent record
    db.execute(
        text(
            """
            INSERT INTO schedule_imports (id, source_filename, row_count)
            VALUES (:id, :filename, :row_count)
            """
        ),
        {"id": import_id, "filename": filename, "row_count": len(rows)},
    )

    # 2. Insert schedule_tasks; collect source_task_id -> db UUID mapping
    task_id_map: dict[str, str] = {}  # source_task_id -> DB UUID
    for row in rows:
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

    # 3. Insert task_dependencies
    deps_inserted = 0
    for row in rows:
        if row.dependency != NO_DEPENDENCY and row.dependency in task_id_map:
            db.execute(
                text(
                    """
                    INSERT INTO task_dependencies (task_id, depends_on_task_id)
                    VALUES (:task_id, :depends_on_task_id)
                    """
                ),
                {
                    "task_id": task_id_map[row.source_task_id],
                    "depends_on_task_id": task_id_map[row.dependency],
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
