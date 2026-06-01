"""Centralized application configuration for Lingua Web.

Environment-variable driven. All database paths, API keys, and mode flags
are resolved here so that tests and the real app use consistent settings.

Environment variables:
  LINGUA_DATABASE_URL  — SQLAlchemy database URL (default: sqlite:///./data/lingua.db)
  LINGUA_TESTING       — "1" when running automated / verification tests
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Upload limits
# ---------------------------------------------------------------------------
# Maximum single-file upload size (in MB). Shared across all supported file types.
MAX_UPLOAD_SIZE_MB = 10
MAX_UPLOAD_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DEFAULT_DATABASE_URL = f"sqlite:///{DATA_DIR / 'lingua.db'}"
DATABASE_URL = os.getenv("LINGUA_DATABASE_URL", DEFAULT_DATABASE_URL)

# Test mode guard — refuse to open the real learning DB when testing
LINGUA_TESTING = os.getenv("LINGUA_TESTING", "0") == "1"
_PROHIBITED_PATTERNS = ["data/lingua.db", "./data/lingua.db"]

if LINGUA_TESTING:
    url_lower = DATABASE_URL.lower()
    for pattern in _PROHIBITED_PATTERNS:
        if pattern in url_lower:
            raise RuntimeError(
                f"Tests must not use the real learning database.\n"
                f"Set LINGUA_DATABASE_URL to a temporary path before importing app modules.\n"
                f"Current URL: {DATABASE_URL} (matches prohibited pattern: {pattern})"
            )

# ---------------------------------------------------------------------------
# API Keys (read from environment; validated at point of use)
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
