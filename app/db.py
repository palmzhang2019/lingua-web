"""Database engine and session management for Lingua Web."""

import os
from pathlib import Path

from sqlalchemy import create_engine
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
    """Create all tables. Safe to call repeatedly (idempotent)."""
    import app.models  # noqa: F401 — ensure models are registered

    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency — yields a session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
