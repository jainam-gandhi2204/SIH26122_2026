from fastapi import FastAPI, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from app.database import DatabaseNotConfiguredError, check_database_connection


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
