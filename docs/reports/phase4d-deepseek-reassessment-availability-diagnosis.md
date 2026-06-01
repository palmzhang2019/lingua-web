# Phase 4D: DeepSeek Reassessment Availability Diagnosis

## Summary

DeepSeek `is_available()` returned `false` during the legacy-record reassessment
attempt in Phase 4D-0 because of a **module-load-timing dependency** between
`load_dotenv()` and `app/llm.py`'s module-level constant `API_KEY`. This is a
known architectural constraint documented in `app/main.py` lines 10-12.

---

## 1. Where is `is_available()` implemented?

**File:** `app/llm.py` line 174-176

```python
def is_available() -> bool:
    """Check if LLM credentials are configured."""
    return bool(API_KEY)
```

where `API_KEY` is defined at module level on line 47:

```python
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
```

## 2. Required configuration variables

| Variable | Default | Source |
|----------|---------|--------|
| `DEEPSEEK_API_KEY` | `""` | `.env` or process environment |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | `.env` or process environment |
| `DEEPSEEK_MODEL` | `deepseek-chat` (llm.py) / `deepseek-v4-flash` (config.py) | `.env` or process environment |

## 3. Configuration presence (boolean only)

| Variable | Present? |
|----------|:--------:|
| `DEEPSEEK_API_KEY` | ✅ **true** — set in `.env` and current Hermes session |
| `DEEPSEEK_BASE_URL` | ✅ true |
| `DEEPSEEK_MODEL` | ✅ true |
| `.env` file exists | ✅ true at `~/workspace/lingua-web/.env` |

## 4. Why `is_available()` returned `false`

**Root cause: Module-import-time evaluation of `os.getenv()`.**

- `app/llm.py` reads `os.getenv("DEEPSEEK_API_KEY", "")` **at import time** (line 47).
- `load_dotenv()` MUST be called **before** `import app.llm` to populate environment variables from `.env`.
- The Phase 4D-0 reassessment code path imported `app.llm` (or a module that transitively imports it) **without** first calling `load_dotenv()` from the Lingua Web project directory.
- At that point, `API_KEY` was bound to `""` as a module-level string constant. Calling `load_dotenv()` later does **not** update the already-imported `API_KEY` variable.

**This is a known architectural constraint**, explicitly documented in `app/main.py` lines 10-12:

```python
# MUST be called before any app modules are imported, because db.py, llm.py,
# and pdf_vision.py read os.getenv() at module-import time.
load_dotenv()
```

## 5. Load-path comparison: Web app vs CLI vs Hermes agent

| Path | `load_dotenv()` timing | Result |
|------|------------------------|:------:|
| `uvicorn app.main:app` | ✅ Called at module scope before any app import | ✅ Works |
| `uv run pytest tests/...` | ✅ Each test file calls `load_dotenv()` at module scope | ✅ Works |
| Hermes agent `uv run python -c "from app.llm import ..."` | ❌ No `load_dotenv()` call before import | ❌ Fails |
| Hermes agent script with `os.putenv` | ✅ If key set before import | ✅ Works |

## 6. Relationship to OpenCode DeepSeek configuration

**Independent.** OpenCode stores DeepSeek provider config in its own configuration
files (e.g. `~/.opencode/` or OpenCode's provider registry), using environment
variables like `DEEPSEEK_API_KEY` that may or may not be the same variable name or
value as Lingua Web's `DEEPSEEK_API_KEY`. The two systems:

- Read from **different config files**
- Use **different provider registries**
- **Share** the `DEEPSEEK_API_KEY` environment variable name by convention only
- Lingua Web additionally reads `DEEPSEEK_BASE_URL` and `DEEPSEEK_MODEL` from `.env`

The availability of DeepSeek via OpenCode does **not** imply availability via
Lingua Web's `app.llm`, and vice versa.

## 7. Minimal next action to enable future reassessment

**Option A — Use the web app runtime (recommended):**
Start the Lingua Web app (`uv run uvicorn app.main:app`) so that `load_dotenv()`
runs before any modules are loaded. The reassessment endpoint would then have
access to DeepSeek via the normal application path.

**Option B — Load `.env` explicitly before importing `app.llm`:**
```python
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path="/home/pompeo_z/workspace/lingua-web/.env")
# THEN import app.llm:
from app.llm import structured_extraction
```

The `dotenv_path` keyword argument is critical when running from a different
working directory, because `load_dotenv()` defaults to looking for `.env` in
the current working directory.

**Option C — Set the environment variable at the process level:**
```python
import os
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-...")
```
before any import of `app.llm`. (Requires handling the secret value.)

## 8. Security note

No API key values were read, logged, or disclosed in this diagnosis.
Only boolean presence/absence was verified.

---

## Verification Update (2026-06-01T14:45)

After the Phase 4D commit (`a68cd4b`), the correct initialization order was applied
and DeepSeek `is_available()` returned **`true`**. All 3 legacy attempts (60, 61, 62)
were successfully reassessed via `structured_extraction()` using the Lingua Web
`TranslationEvaluationV2` schema.

**Confirmed resolution:** The previously observed `false` was entirely caused by
module-import-timing of `load_dotenv()`. The fix documented in options above
(either call `load_dotenv()` before `import app.llm`, or use the web app runtime)
works correctly.
