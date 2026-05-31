"""Database engine and session management for Lingua Web."""

import os
from pathlib import Path

from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# SQLite database lives under data/ and is gitignored.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE_URL = os.getenv(
    "LINGUA_DATABASE_URL",
    f"sqlite:///{DATA_DIR / 'lingua.db'}",
)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # FastAPI uses multiple threads
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create all tables and apply safe schema migrations. Idempotent."""
    import app.models  # noqa: F401 — ensure models are registered

    Base.metadata.create_all(bind=engine)

    # Safe column additions (SQLite ignores IF NOT EXISTS for ALTER)
    _add_column_if_missing("materials", "source_page_start", "INTEGER")
    _add_column_if_missing("materials", "source_page_end", "INTEGER")
    _add_column_if_missing("materials", "extraction_method", "VARCHAR(30)")
    _add_column_if_missing("grammar_points", "source_page", "INTEGER")
    _add_column_if_missing("vocab_items", "source_page", "INTEGER")


def _add_column_if_missing(table: str, column: str, coltype: str):
    """Add a column to a table if it does not already exist."""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa_text(f"PRAGMA table_info({table})")
            )
            existing = {row.name for row in result}
            if column not in existing:
                conn.execute(
                    sa_text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                )
                conn.commit()
    except Exception:
        pass  # Table might not exist yet — safe to ignore


def get_db():
    """FastAPI dependency — yields a session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
