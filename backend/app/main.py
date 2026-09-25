from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
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
from app import review_queue
from app.review_queue import (
    InvalidTaskMatchError,
    ReviewActionError,
    ReviewItemNotFoundError,
)


from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(
    title="SIH26122 API",
    version="0.1.0",
    description="Backend foundation for the SIH 2026 prototype.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    replace: bool = Query(False, description="If true, replace existing schedule tasks and dependencies before importing"),
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
        result = schedule_importer.import_to_db(db, parsed_rows, filename, replace=replace)
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
def list_site_updates(
    status_filter: str = Query(
        "active",
        alias="status",
        description="Filter site updates: 'active' (default), 'historical', or 'all'",
    ),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Return stored site updates, newest reported date first."""
    raw_status = getattr(status_filter, "default", status_filter)
    status_str = raw_status if isinstance(raw_status, str) else "active"
    try:
        return site_updates.list_site_updates(db, status=status_str)
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
) -> list[dict[str, Any]]:
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
) -> list[dict[str, Any]]:
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

    if not result:
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


@app.get("/schedule/deviation", tags=["schedule"])
@app.get("/schedule/tasks/deviation", tags=["schedule"])
def get_schedule_deviation(db: Session = Depends(get_db)) -> dict:
    """Return actual-vs-planned schedule deviation and health classifications.

    Calculates start, end, and duration deviations for all tasks, and classifies
    tasks as on_time, delayed, at_risk, or unassessed.
    """
    try:
        return schedule_linker.get_schedule_deviation_summary(db)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error


@app.get("/schedule/tasks/{task_id}", tags=["schedule"])
def get_schedule_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Return a single schedule task with its planned vs actual state.

    Returns 404 if task_id does not exist.
    """
    try:
        result = schedule_linker.get_task(db, task_id)
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


# ---------------------------------------------------------------------------
# Planner Review Queue & Resolution Endpoints
# ---------------------------------------------------------------------------

class ReviewApproveRequest(BaseModel):
    notes: str | None = Field(None, description="Optional planner notes")


class ReviewMatchRequest(BaseModel):
    task_id: str = Field(..., description="Target schedule task UUID or source_task_id (e.g. T101)")
    notes: str | None = Field(None, description="Optional planner notes")


class ReviewRejectRequest(BaseModel):
    reason: str | None = Field(None, description="Optional reason for marking update as unmatched/rejected")


class ReviewResolveRequest(BaseModel):
    action: str = Field(..., description="Action to take: 'approve', 'change_match', or 'reject'")
    task_id: str | None = Field(None, description="Target task ID for 'change_match'")
    notes: str | None = Field(None, description="Optional notes or rejection reason")


@app.get("/review/queue", tags=["review"])
@app.get("/planner/review-queue", tags=["review"])
def get_planner_review_queue(
    threshold: float = review_queue.DEFAULT_CONFIDENCE_THRESHOLD,
    status_filter: str = Query("pending", alias="status", description="Filter by review status: 'pending', 'resolved', or 'all'"),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Retrieve site updates requiring planner review.

    Includes all AI-processed updates where:
    - matched_task_id is null (no schedule task could be linked), or
    - confidence_score is below the threshold (default: 80.0), or
    - multiple candidate matches caused ambiguity.

    Returns raw update text, extracted activity/progress/status, confidence,
    review reason, current match, candidate schedule tasks, and suggested match.
    """
    try:
        return review_queue.get_review_queue(db, threshold=threshold, status=status_filter)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error


@app.post("/planner/review-queue/{review_id}/approve", tags=["review"])
@app.post("/review/queue/{review_id}/approve", tags=["review"])
def approve_review_item(
    review_id: str,
    payload: ReviewApproveRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Approve the AI or suggested match for an update in the review queue.

    Confirms the match, sets confidence to 100%, and safely updates schedule task
    actuals using the existing schedule actual engine.
    """
    notes = payload.notes if payload else None
    try:
        return review_queue.approve_match(db, review_id=review_id, notes=notes)
    except ReviewItemNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ReviewActionError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is unavailable") from error


@app.post("/planner/review-queue/{review_id}/change-match", tags=["review"])
@app.post("/review/queue/{review_id}/change-match", tags=["review"])
def change_review_item_match(
    review_id: str,
    payload: ReviewMatchRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Reassign the site update to a different schedule task.

    Validates that the target task exists, updates the matched task, and safely
    applies schedule task actuals to the newly matched task.
    """
    try:
        return review_queue.change_matched_task(db, review_id=review_id, new_task_id=payload.task_id, notes=payload.notes)
    except ReviewItemNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except InvalidTaskMatchError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ReviewActionError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is unavailable") from error


@app.post("/planner/review-queue/{review_id}/reject", tags=["review"])
@app.post("/review/queue/{review_id}/reject", tags=["review"])
def reject_review_item(
    review_id: str,
    payload: ReviewRejectRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Mark the site update as intentionally unmatched/rejected.

    Sets matched_task_id to NULL, records the rejection audit trail, and does NOT
    modify any schedule tasks.
    """
    reason = payload.reason if payload else None
    try:
        return review_queue.reject_match(db, review_id=review_id, reason=reason)
    except ReviewItemNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is unavailable") from error


@app.post("/planner/review-queue/{review_id}/resolve", tags=["review"])
@app.post("/review/queue/{review_id}/resolve", tags=["review"])
def resolve_review_item(
    review_id: str,
    payload: ReviewResolveRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Unified resolution endpoint for review queue items ('approve', 'change_match', or 'reject')."""
    try:
        return review_queue.resolve_review_item(
            db,
            review_id=review_id,
            action=payload.action,
            task_id=payload.task_id,
            notes=payload.notes,
        )
    except ReviewItemNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except InvalidTaskMatchError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ReviewActionError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is unavailable") from error
