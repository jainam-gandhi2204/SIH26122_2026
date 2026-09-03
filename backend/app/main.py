from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import DatabaseNotConfiguredError, check_database_connection, get_db
from app import importer as schedule_importer
from app import site_updates
from app.site_updates import SiteUpdateCreate


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

