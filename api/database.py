"""Database configuration and session management."""

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from api.core.config import settings
from api.models import Base

# Database URL - can be configured via environment variable
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./roster.db")

# Create engine with SQLite optimizations
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {
        "check_same_thread": False,
        "timeout": 30,  # Increase timeout to 30 seconds for better concurrency
    }
    if "mode=memory" in DATABASE_URL or ":memory:" in DATABASE_URL:
        # Enable URI parsing for shared memory named DBs
        connect_args["uri"] = True

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,
    pool_pre_ping=True,  # Verify connections before using them
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Install the cross-tenant query guard. The listener is registered against
# the global Session class so it covers SessionLocal as well as any test
# sessionmakers spun up against in-memory SQLite (e.g., tests/api/conftest.py).
from api.utils.tenancy_guard import install_tenancy_guard  # noqa: E402

install_tenancy_guard(Session)


def _resolve_sqlite_path(db_url: str) -> Path | None:
    """Translate SQLite URLs into filesystem paths."""
    # FORCE DISABLE FILE RESOLUTION
    # return None

    if not db_url.startswith("sqlite"):
        return None
    if ":memory:" in db_url or "mode=memory" in db_url:
        return None
    url = make_url(db_url)
    database = url.database or ""
    path = Path(database)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _prepare_sqlite_file(sqlite_path: Path, db_url: str) -> None:
    """Ensure the SQLite file and parent directories exist with write access."""
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.touch(exist_ok=True)
    # Always set permissions (touch doesn't update existing file permissions)
    sqlite_path.chmod(0o666)  # rw-rw-rw- for maximum compatibility in tests
    if settings.TESTING or os.getenv("TESTING"):
        stats = sqlite_path.stat()
        print(
            f"[init_db] ensuring sqlite database at {sqlite_path} (from {db_url}) "
            f"(mode={oct(stats.st_mode & 0o777)} owner={stats.st_uid}:{stats.st_gid})"
        )


def init_db() -> None:
    """Initialize database tables."""
    import logging
    import sys

    logging.getLogger("rostio").fatal(f"DEBUG: init_db engine id: {id(engine)}")
    for k in sorted(sys.modules.keys()):
        if "api.database" in k:
            logging.getLogger("rostio").fatal(
                f"DEBUG: IN THREAD sys.modules[{k}] id: {id(sys.modules[k])}"
            )

    logging.getLogger("rostio").fatal(f"DEBUG: init_db tables: {list(Base.metadata.tables.keys())}")

    sqlite_path = _resolve_sqlite_path(DATABASE_URL)
    if sqlite_path:
        _prepare_sqlite_file(sqlite_path, DATABASE_URL)
    Base.metadata.create_all(bind=engine)

    # Enable WAL mode for better concurrency in SQLite unless running tests
    if DATABASE_URL.startswith("sqlite"):
        env_testing = os.getenv("TESTING", "").lower() in {"1", "true", "yes", "on"}
        with engine.connect() as conn:
            if settings.TESTING or env_testing:
                conn.execute(text("PRAGMA journal_mode=DELETE"))
            else:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.execute(
                    text("PRAGMA synchronous=NORMAL")
                )  # Balance between safety and performance
            conn.commit()


def get_db() -> Generator[Session, None, None]:
    """
    Dependency to get database session.

    Usage in FastAPI route:
        @router.get("/items")
        def get_items(db: Session = Depends(get_db)):
            ...
    """
    import logging

    logging.getLogger("rostio").fatal(f"DEBUG: get_db engine id: {id(engine)}")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
