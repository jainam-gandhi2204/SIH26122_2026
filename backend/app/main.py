from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import DatabaseNotConfiguredError, check_database_connection, get_db
from app import importer as schedule_importer
from app import site_updates
from app.site_updates import SiteUpdateCreate
from app import ai_processor
from app.ai_processor import SiteUpdateNotFoundError
from app import spreadsheet_ingestor
from app import schedule_linker


app = FastAPI(
    title="SIH26122 API",
    version="0.1.0",
    description="Backend foundation for the SIH 2026 prototype.",
)


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Return service health for local development and deployment checks."""
    return {"status": "ok"}


@app.get("/health/database", tags=["system"])
def database_health_check() -> dict[str, str]:
    """Verify that the API can establish a connection to PostgreSQL."""
    try:
        check_database_connection()
    except DatabaseNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not configured",
        ) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error

    return {"status": "ok", "database": "reachable"}


_SCHEDULE_TASKS_QUERY = text(
    """
    SELECT source_task_id, activity, location, planned_start, planned_end
    FROM schedule_tasks
    ORDER BY planned_start, source_task_id
    """
)


@app.get("/schedule/tasks", tags=["schedule"])
def list_schedule_tasks(db: Session = Depends(get_db)) -> list[dict]:
    """Return all schedule tasks with their core planning fields."""
    try:
        rows = db.execute(_SCHEDULE_TASKS_QUERY).fetchall()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error

    return [
        {
            "source_task_id": row.source_task_id,
            "activity": row.activity,
            "location": row.location,
            "planned_start": row.planned_start.isoformat(),
            "planned_end": row.planned_end.isoformat(),
        }
        for row in rows
    ]


@app.post("/schedule/import", tags=["schedule"], status_code=status.HTTP_201_CREATED)
async def import_schedule_csv(
    file: UploadFile,
    db: Session = Depends(get_db),
) -> dict:
    """Accept a CSV file and import its tasks into the schedule tables.

    Returns a summary of the import on success.
    Returns HTTP 422 if the CSV contains validation errors.
    Returns HTTP 503 on database failure.
    """
    content = await file.read()
    filename = file.filename or "upload.csv"

    parsed_rows, errors = schedule_importer.parse_and_validate(content, filename)

    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {
                    "row": e.row_number,
                    "task_id": e.task_id,
                    "message": e.message,
                }
                for e in errors
            ],
        )

    try:
        result = schedule_importer.import_to_db(db, parsed_rows, filename)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error during import",
        ) from error

    return {
        "import_id": result.import_id,
        "filename": result.filename,
        "tasks_imported": result.tasks_imported,
        "dependencies_imported": result.dependencies_imported,
    }


@app.post("/site-updates", tags=["site-updates"], status_code=status.HTTP_201_CREATED)
def create_site_update(
    payload: SiteUpdateCreate,
    db: Session = Depends(get_db),
) -> dict:
    """Ingest a new site update."""
    try:
        return site_updates.create_site_update(db, payload)
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error


@app.get("/site-updates", tags=["site-updates"])
def list_site_updates(db: Session = Depends(get_db)) -> list[dict]:
    """Return stored site updates, newest reported date first."""
    try:
        return site_updates.list_site_updates(db)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error


@app.post(
    "/site-updates/{update_id}/process",
    tags=["site-updates"],
    status_code=status.HTTP_201_CREATED,
)
def process_site_update(
    update_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Run AI processing for a stored site update and persist the result.

    Re-running this endpoint replaces the previous current result (is_current
    is set to false on the old record and a fresh row is inserted).
    Returns the new ai_processed_updates record.
    """
    try:
        return ai_processor.process_site_update(db, update_id)
    except SiteUpdateNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error


@app.get("/site-updates/{update_id}/process", tags=["site-updates"])
def get_site_update_result(
    update_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Return the current AI-processed result for a site update.

    Returns 404 if the update does not exist or has not been processed yet.
    """
    try:
        result = ai_processor.get_current_result(db, update_id)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No processed result found for update '{update_id}'.",
        )
    return result


@app.post(
    "/site-updates/import-spreadsheet",
    tags=["site-updates"],
    status_code=status.HTTP_201_CREATED,
)
async def import_spreadsheet(
    file: UploadFile,
    db: Session = Depends(get_db),
) -> dict:
    """Ingest a CSV or XLSX spreadsheet of site updates.

    Each data row is validated and stored as a site_updates record, with
    source_reference set to the uploaded filename.  The response includes
    per-row detail so callers can see exactly which rows were accepted or
    rejected without re-submitting the whole file.

    Accepted column names (case-insensitive, punctuation-tolerant):
    - Update ID / update_id / id / uid
    - Date / reported on / report date / update date
    - Location / loc / site / area / zone
    - Description / raw update / remarks / notes / observation / work done

    Returns HTTP 422 if the file itself is unreadable or all rows fail.
    Returns HTTP 201 otherwise (partial success is noted in row_errors).
    """
    content = await file.read()
    filename = file.filename or "upload"

    parse_result = spreadsheet_ingestor.parse_spreadsheet(content, filename)

    # Full failure: nothing accepted at all
    if parse_result.accepted_count == 0 and parse_result.error_count > 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "No rows could be imported.",
                "row_errors": [
                    {
                        "row": e.row_number,
                        "source_update_id": e.source_update_id,
                        "message": e.message,
                    }
                    for e in parse_result.row_errors
                ],
            },
        )

    created: list[dict] = []
    db_errors: list[dict] = []

    for payload in parse_result.rows_accepted:
        try:
            record = site_updates.create_site_update(db, payload)
            created.append({
                "id": record["id"],
                "source_update_id": record["source_update_id"],
            })
        except SQLAlchemyError as exc:
            db.rollback()
            db_errors.append({
                "source_update_id": payload.source_update_id,
                "message": f"Database error: {exc}",
            })

    return {
        "filename": filename,
        "accepted": parse_result.accepted_count,
        "stored": len(created),
        "row_errors": [
            {
                "row": e.row_number,
                "source_update_id": e.source_update_id,
                "message": e.message,
            }
            for e in parse_result.row_errors
        ],
        "db_errors": db_errors,
        "created": created,
    }


# ---------------------------------------------------------------------------
# Schedule-linking + downstream impact
# ---------------------------------------------------------------------------

@app.get("/schedule/tasks/linked", tags=["schedule"])
def list_linked_tasks(db: Session = Depends(get_db)) -> list[dict]:
    """Return all schedule tasks joined with their latest AI-processed status.

    Tasks that have not been AI-processed yet are included with null actual
    fields so the caller can see the full plan regardless of AI coverage.

    Fields per task:
    - task_id, source_task_id, activity, location
    - planned_start, planned_end   (planned dates from CSV import)
    - status, progress_percent, delay_days, delay_reason  (from AI)
    - actual_start_date, actual_end_date, confidence_score, processed_at
    """
    try:
        return schedule_linker.get_linked_tasks(db)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error


@app.get("/schedule/tasks/{task_id}/impact", tags=["schedule"])
def get_task_impact(
    task_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Return a single task's plan+actual data and its downstream impact chain.

    Uses task_dependencies to traverse directly and transitively affected
    downstream tasks via BFS.  Downstream tasks are marked with
    ``at_risk=true`` and a ``risk_reason`` when the upstream task is delayed,
    blocked, or has less-than-complete progress.

    Downstream tasks are NOT given an invented delay — the prototype
    flags them as at-risk and explains why, which is honest and auditable.

    Returns 404 if task_id does not exist.
    """
    try:
        result = schedule_linker.get_task_impact(db, task_id)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )
    return result
