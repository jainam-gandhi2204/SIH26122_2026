"""SQLAlchemy configuration for the backend PostgreSQL connection."""

from __future__ import annotations

import os
import ssl
from collections.abc import Generator
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when the backend has no PostgreSQL connection string."""


load_dotenv()


def build_engine(raw_url: str) -> Engine:
    """Build a robust SQLAlchemy Engine for PostgreSQL.

    If the connection URL uses `postgresql` or `postgresql+psycopg`, adapts it to
    use `pg8000` (pure-Python DBAPI driver) to avoid Windows Application Control
    policies blocking the unsigned native C-extension DLL (_psycopg.pyd).
    """
    url = make_url(raw_url)
    connect_args: dict[str, Any] = {}

    drivername = url.drivername
    if drivername in ("postgresql", "postgresql+psycopg", "postgresql+psycopg2"):
        query = dict(url.query)
        sslmode = query.pop("sslmode", None)

        # In pg8000, SSL is configured via ssl_context rather than sslmode in query
        if sslmode or (url.host and "supabase" in url.host):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            connect_args["ssl_context"] = ctx

        url = url.set(drivername="postgresql+pg8000", query=query)

    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


@lru_cache
def get_engine() -> Engine:
    """Create the shared PostgreSQL engine on first database use."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise DatabaseNotConfiguredError("DATABASE_URL is not configured")

    return build_engine(database_url)


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
