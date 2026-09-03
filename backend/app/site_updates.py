"""Site updates ingestion: validation, queries, and database persistence.

Kept separate from main.py so validation and persistence can be unit-tested cleanly.
"""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

DATE_FORMAT = "%d-%b-%Y"  # e.g. 05-Sep-2026


class SiteUpdateCreate(BaseModel):
    source_update_id: str
    reported_on: str
    location: str
    raw_update: str
    source_reference: str | None = None

    @field_validator("source_update_id", "location", "raw_update")
    @classmethod
    def validate_non_empty(cls, value: str, info: Any) -> str:
        if not value or not value.strip():
            raise ValueError(f"{info.field_name} cannot be empty")
        return value.strip()

    @field_validator("reported_on")
    @classmethod
    def validate_reported_on(cls, value: str) -> str:
        stripped = value.strip() if value else ""
        if not stripped:
            raise ValueError("reported_on cannot be empty")
        try:
            datetime.datetime.strptime(stripped, DATE_FORMAT)
        except ValueError:
            raise ValueError(
                f"reported_on '{stripped}' is not a valid date (expected DD-Mon-YYYY, e.g. 05-Sep-2026)"
            )
        return stripped

    @field_validator("source_reference")
    @classmethod
    def validate_source_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped if stripped else None


def format_site_update(row: Any) -> dict[str, Any]:
    """Format a database row or mock object into a standardized site update dictionary."""
    reported_on_val = row.reported_on
    if hasattr(reported_on_val, "isoformat"):
        reported_on_str = reported_on_val.isoformat()
    else:
        reported_on_str = str(reported_on_val)

    ingested_at_val = getattr(row, "ingested_at", None)
    if hasattr(ingested_at_val, "isoformat"):
        ingested_at_str = ingested_at_val.isoformat()
    elif ingested_at_val is not None:
        ingested_at_str = str(ingested_at_val)
    else:
        ingested_at_str = None

    return {
        "id": str(row.id),
        "source_update_id": row.source_update_id,
        "reported_on": reported_on_str,
        "location": row.location,
        "raw_update": row.raw_update,
        "ingested_at": ingested_at_str,
        "source_reference": getattr(row, "source_reference", None),
    }


_INSERT_SITE_UPDATE_QUERY = text(
    """
    INSERT INTO site_updates (
        source_update_id,
        reported_on,
        location,
        raw_update,
        source_reference
    )
    VALUES (
        :source_update_id,
        :reported_on,
        :location,
        :raw_update,
        :source_reference
    )
    RETURNING id, source_update_id, reported_on, location, raw_update, ingested_at, source_reference
    """
)

_SELECT_SITE_UPDATES_QUERY = text(
    """
    SELECT id, source_update_id, reported_on, location, raw_update, ingested_at, source_reference
    FROM site_updates
    ORDER BY reported_on DESC, ingested_at DESC
    """
)


def create_site_update(db: Session, payload: SiteUpdateCreate) -> dict[str, Any]:
    """Insert a new site update record into PostgreSQL and return the created record."""
    reported_date = datetime.datetime.strptime(payload.reported_on, DATE_FORMAT).date()
    row = db.execute(
        _INSERT_SITE_UPDATE_QUERY,
        {
            "source_update_id": payload.source_update_id,
            "reported_on": reported_date,
            "location": payload.location,
            "raw_update": payload.raw_update,
            "source_reference": payload.source_reference,
        },
    ).fetchone()
    db.commit()
    return format_site_update(row)


def list_site_updates(db: Session) -> list[dict[str, Any]]:
    """Return all stored site updates, ordered with newest reported date first."""
    rows = db.execute(_SELECT_SITE_UPDATES_QUERY).fetchall()
    return [format_site_update(row) for row in rows]
