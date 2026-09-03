"""SQLAlchemy configuration for the backend PostgreSQL connection."""

from __future__ import annotations

import os
from collections.abc import Generator
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when the backend has no PostgreSQL connection string."""


load_dotenv()


@lru_cache
def get_engine() -> Engine:
    """Create the shared PostgreSQL engine on first database use."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise DatabaseNotConfiguredError("DATABASE_URL is not configured")

    return create_engine(database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return the shared session factory for future application use."""
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a database session and always close it after the request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_database_connection() -> None:
    """Raise if PostgreSQL cannot be reached; otherwise return normally."""
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
