"""Async SQLAlchemy engine and session management (SQLite via aiosqlite).

Configuration is read from the environment so the module works before the
central settings object (``app.core.config``) exists:

* ``DATABASE_URL`` - defaults to ``sqlite+aiosqlite:///./data/expense.db``.
  A plain ``sqlite:///...`` URL is upgraded to the async driver automatically.
* ``SQL_ECHO`` - set to ``1`` / ``true`` to log every SQL statement.

Typical usage::

    # FastAPI dependency
    async def list_items(db: AsyncSession = Depends(get_db)): ...

    # Scripts / background tasks (commits on success, rolls back on error)
    async with session_scope() as session: ...
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final, Optional

from sqlalchemy import MetaData, event, inspect, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL: Final[str] = "sqlite+aiosqlite:///./data/expense.db"
SQLITE_BUSY_TIMEOUT_SECONDS: Final[int] = 30

#: Deterministic constraint / index names keep future Alembic migrations stable.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base class shared by every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean flag from the environment.

    Args:
        name: Environment variable name.
        default: Value used when the variable is unset.

    Returns:
        ``True`` for ``1``, ``true``, ``yes`` or ``on`` (case-insensitive).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_database_url(database_url: Optional[str] = None) -> URL:
    """Return the effective, async-capable database URL.

    Args:
        database_url: Explicit URL. Falls back to ``DATABASE_URL`` and then to
            :data:`DEFAULT_DATABASE_URL`.

    Returns:
        A parsed SQLAlchemy URL (``sqlite`` is upgraded to ``sqlite+aiosqlite``).

    Raises:
        ValueError: If the URL cannot be parsed.
    """
    candidate = (database_url or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL).strip()
    try:
        url = make_url(candidate)
    except ArgumentError as exc:
        raise ValueError(f"Invalid database URL: {candidate!r}") from exc
    if url.drivername == "sqlite":
        url = url.set(drivername="sqlite+aiosqlite")
    return url


def _is_sqlite_memory(url: URL) -> bool:
    """Return ``True`` for in-memory SQLite URLs."""
    if url.get_backend_name() != "sqlite":
        return False
    database = url.database or ""
    return database == "" or ":memory:" in database or url.query.get("mode") == "memory"


def _ensure_sqlite_directory(url: URL) -> None:
    """Create the parent directory of a file-based SQLite database.

    Raises:
        RuntimeError: If the directory cannot be created.
    """
    if url.get_backend_name() != "sqlite" or _is_sqlite_memory(url) or not url.database:
        return
    parent = Path(url.database).expanduser().parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Cannot create database directory '{parent}': {exc}") from exc


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------
def build_engine(
    database_url: Optional[str] = None,
    *,
    echo: Optional[bool] = None,
) -> AsyncEngine:
    """Create an :class:`AsyncEngine`.

    SQLite connections get ``foreign_keys=ON``, a busy timeout and (for file
    databases) WAL journaling so that readers do not block the writer.

    Args:
        database_url: Explicit URL; defaults to the environment / built-in default.
        echo: Log SQL statements. Defaults to the ``SQL_ECHO`` environment flag.

    Returns:
        A configured async engine (no connection is opened yet).

    Raises:
        ValueError: If the URL is invalid.
        RuntimeError: If the engine cannot be created (e.g. driver missing).
    """
    url = _resolve_database_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    in_memory = _is_sqlite_memory(url)

    engine_kwargs: dict[str, Any] = {
        "echo": _env_flag("SQL_ECHO") if echo is None else echo,
    }
    if is_sqlite:
        _ensure_sqlite_directory(url)
        engine_kwargs["connect_args"] = {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
        if in_memory:
            # One shared connection, otherwise every session sees an empty DB.
            engine_kwargs["poolclass"] = StaticPool
    else:
        engine_kwargs["pool_pre_ping"] = True

    try:
        engine = create_async_engine(url, **engine_kwargs)
    except (ArgumentError, ImportError) as exc:
        raise RuntimeError(
            f"Could not create database engine for "
            f"'{url.render_as_string(hide_password=True)}': {exc}"
        ) from exc

    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
            """Apply per-connection SQLite PRAGMAs."""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
                if not in_memory:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    logger.info("Database engine ready: %s", url.render_as_string(hide_password=True))
    return engine


engine: AsyncEngine = build_engine()

#: Session factory. ``expire_on_commit=False`` is required for async code,
#: otherwise attributes would be lazily reloaded (and fail) after a commit.
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------
async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one :class:`AsyncSession` per request.

    The caller commits explicitly. Any exception raised while handling the
    request rolls the transaction back, and the session is always closed.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for code running outside a request.

    Commits when the block finishes cleanly and rolls back on any exception.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _add_missing_columns(connection: Connection) -> None:
    """Add columns introduced after a database file was first created.

    ``create_all`` never alters existing tables, so an older ``expense.db`` (for
    example in a Docker volume) would miss newer columns. This tiny step stands in
    for a migration tool until one is needed: it only ever *adds* nullable columns.
    """
    inspector = inspect(connection)
    for table in Base.metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            column_type = column.type.compile(dialect=connection.dialect)
            connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'))
            if column.index:
                connection.execute(
                    text(f'CREATE INDEX IF NOT EXISTS "ix_{table.name}_{column.name}" ON "{table.name}" ("{column.name}")')
                )
            logger.info("Added missing column %s.%s", table.name, column.name)


def _backfill_merchant_keys(connection: Connection) -> None:
    """Give rows saved before merchant keys existed their key (idempotent, only fills ``NULL``)."""
    from app.services.merchant import merchant_key  # local import: keeps this module free of service imports

    rows = connection.execute(
        text("SELECT id, merchant_name FROM expenses WHERE merchant_key IS NULL AND merchant_name IS NOT NULL")
    ).all()
    for row_id, name in rows:
        key = merchant_key(name)
        if key:
            connection.execute(text("UPDATE expenses SET merchant_key = :key WHERE id = :id"), {"key": key, "id": row_id})
    if rows:
        logger.info("Backfilled merchant keys for %d rows", len(rows))


async def init_db(bind: Optional[AsyncEngine] = None) -> None:
    """Create all tables that do not exist yet.

    Args:
        bind: Engine to use; defaults to the module-level :data:`engine`.

    Raises:
        SQLAlchemyError: If table creation fails.
    """
    # Imported here so the models register on ``Base.metadata`` without a
    # circular import at module load time.
    from app.db import models  # noqa: F401

    target = bind or engine
    try:
        async with target.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(_add_missing_columns)
            await connection.run_sync(_backfill_merchant_keys)
    except SQLAlchemyError:
        logger.exception("Database initialisation failed")
        raise
    logger.info("Database tables are ready")


async def check_db_connection() -> bool:
    """Return ``True`` if the database answers a trivial query (health check)."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, OSError) as exc:
        logger.error("Database health check failed: %s", exc)
        return False


async def dispose_engine() -> None:
    """Close all pooled connections (call on application shutdown)."""
    await engine.dispose()
    logger.info("Database engine disposed")
