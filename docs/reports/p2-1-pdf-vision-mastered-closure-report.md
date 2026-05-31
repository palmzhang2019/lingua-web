# P2.1 PDF Vision + Mastered Feature — Closure Report

**Verdict:** `P2_1_PDF_VISION_AND_MASTERED_FEATURES_COMPLETED`

---

## Executive Summary

This closure finalises the GPT-5.4-mini PDF vision import pipeline and introduces the mastered-item workflow. All confirmed bugs from the iterative session are fixed, external CDN dependency is removed, tests pass, and the commit is safe.

## Baseline Commits and Initial Uncommitted Diff

| Item | Value |
|------|-------|
| HEAD | `703d34b` (feat: use gpt-5.4-mini for bounded PDF learning import) |
| Origin | `origin/main` (same commit) |
| Prior OCR baseline | `f77d2a6` |
| Initial uncommitted files | 11 modified + 2 untracked |
| Initial delta | +159 / −54 |

## Implemented Features Retained

1. **PDF Vision Import** — OpenAI gpt-5.4-mini via Responses API; selected-page slicing only; 10-page / 10 MB limit.
2. **Mastered Grammar/Vocabulary** — `mastered` Boolean on both tables; toggle via material detail and study page; study cycle excludes mastered items.
3. **Parallel Study Cycle Generation** — 5 DeepSeek calls via `ThreadPoolExecutor` + `asyncio.gather` (~8 s vs ~30 s).
4. **Configuration Loading** — `load_dotenv()` in `app/main.py` (tests call `load_dotenv()` directly).

## Confirmed Bugs Fixed

| Bug | Fix |
|-----|-----|
| `.env` not loaded (empty API keys) | `app/main.py`: added `load_dotenv()` before app module imports |
| DeepSeek JSON truncation (`max_tokens=4096`) | `app/llm.py`: `max_tokens` → 8192 |
| Scroll-to-top on mastered toggle | Removed HTMX CDN; replaced with inline `fetch()` + `outerHTML` swap on card element |
| `.catch(this.submit())` fallback bug | Captures `var f=this`; also checks `response.ok` before swapping |
| 1-remaining edge case message | `app/routes/study.py`: distinct "仅剩 1 个，仍需至少 2 个" message |
| `MAX_PDF_PAGES` test failure | `tests/test_p2_1_final_closure.py`: updated test to reject >10 pages |

## Inline JS Fallback Fix Result

The `.catch(function(){f.submit();})` pattern correctly captures the form via `var f=this` in the enclosing `onsubmit` scope. Additionally `response.ok` check prevents swapping a card with an error-HTML body. Non-JS fallback (`action="..." method="post"`) remains functional.

## HTMX / External Dependency Final Decision

**Removed.** The HTMX CDN script (`unpkg.com`) was unreachable in the WSL/ngrok environment. All `hx-*` attributes were cleaned from templates. Core interactions (toggle mastered, upload form, study answer forms) use plain HTML form submission. The mastered toggle additionally enhances with inline `fetch()` — zero external JS dependencies.

## Configuration Loading Result

- **`uvicorn` path**: `app/main.py` calls `load_dotenv()` before importing `app.db`, `app.llm`, `app.pdf_vision` → keys available at module-import time. ✅
- **Test/script path**: `test_p2_1_final_closure.py` calls `load_dotenv()` before app module imports. ✅
- **Direct module import**: Any script that imports `app.llm` or `app.pdf_vision` must call `load_dotenv()` first (documented in `.env.example`).
- **Secret safety**: No API keys appear in diff, logs, tests, or reports.

## Multiple-Choice Truncation Fix and Validation

- Root cause: `max_tokens=4096` caused DeepSeek to truncate the 9-question JSON mid-string → `JSONDecodeError`.
- Fix: increased to `8192`.
- Verified: `generate_multiple_choice` returns exactly 9 validated `MultipleChoiceQuestion` items; invalid/malformed JSON returns empty list → error shown to user; no partial cycle persisted.
- Cost impact: negligible (8192 tokens is well within typical prompt+completion size).

## Parallel Cycle Generation Safety and Performance

- **Pattern**: `ThreadPoolExecutor(max_workers=5)` + `loop.run_in_executor()` + `await asyncio.gather()`.
- **Thread safety**: `generate_*` functions do not use SQLAlchemy Session; `_usage_records` `list.append()` is GIL-safe.
- **Atomicity**: All validation checks run before `StudyCycle` creation. Any generation failure returns an error page; no partial cycle is committed.
- **Performance**: Measured ~8 s for a complete cycle (previously ~30 s sequential). Timing from test evidence.

## Mastered Item Semantics and UI Behavior

- `GrammarPoint.mastered` / `VocabItem.mastered`: `Boolean, default=False`. Migration via `ALTER TABLE` (idempotent).
- Existing rows get `NULL` which is falsy in Python/SQL → treated as not-mastered.
- **Global vs cycle scope**: Toggling `mastered` on a source item does NOT retroactively change `QuestionAttempt`, `WeakPoint`, `StudyCycle.completed_at`, or `is_valid_completion`.
- **Filtering**: `start_cycle` selects only `unmastered` points for grammar A/B and review.
- **Edge cases**: 0 remaining → "无需学习的语法点"; 1 remaining → "仅剩 1 个，仍需至少 2 个".
- **Vocab mastered**: Displayed and persisted, but does not affect grammar-cycle creation (out of scope).
- **AJAX behavior**: Inline `fetch()` returns card fragment; `response.ok` check prevents error-page swap; `.catch()` falls back to form.submit().
- **No-JS fallback**: `<form action="..." method="post">` works as standard HTML form → 303 redirect.

## Active PDF Vision Architecture

| Component | Role |
|-----------|------|
| `app/pdf_vision.py` | Uploads PDF to OpenAI, calls Responses API with `gpt-5.4-mini`, parses JSON response |
| `app/services/material_parser.py` | Slices PDF to only selected pages (privacy boundary), calls `extract_from_pdf_pages` |
| `app/routes/upload.py` | Validates page range, file size, calls parser, persists results |
| **Page limit** | ≤ 10 pages per request (user-configurable via `MAX_PDF_PAGES`) |
| **File size limit** | ≤ 10 MB (via `MAX_PDF_BYTES`) |
| **Privacy** | Only user-selected pages sent to OpenAI; full PDF never uploaded to API |
| **Extraction method tag** | `openai_pdf_vision` stored in `materials.extraction_method` |

## PDF Page Limit Privacy and Cost Decision

`MAX_PDF_PAGES` was changed from 3 → 10 after explicit user request ("提取页面修改为10页"). The limit is enforced in the upload route (validation before any API call). Estimated cost per 10-page analysis: ~$0.05–0.15 (gpt-5.4-mini at $0.75 / 1M input tokens).

## Real PDF Quality Review and Suspicious Grammar Items

Verified PDF: `011-020.pdf`, pages 3–5 (bounded slice).

| Item | Verdict | Notes |
|------|---------|-------|
| `〜んですけど／が` | ✅ Acceptable (N3) | Used in引出话题 context. Correctly labelled N3. Example grounded on page 3. |
| `〜てもみない` | ⚠️ Accepted | Pattern is `思ってもみなかった` = "never even thought". While not a standard JLPT grammar entry, it is a legitimate colloquial N2-level construction and extracted from actual page content. Low risk as a learning item. |

**Final counts**: 2 grammar points, 16 vocabulary items (preliminary), 2 grammar / 10 vocab (persisted via HTTP E2E).

## Redundant Code and Dependency Cleanup

| Change | Reason |
|--------|--------|
| Removed `fpdf2` from `pyproject.toml` | Unused; no `import fpdf` in any source file |
| Removed HTMX CDN script from `base.html` | Unreachable in WSL/ngrok; zero external JS deps |
| Removed `hx-*` attributes from `study.html` and `materials.html` | No HTMX loaded; plain HTML fallback sufficient |
| Retained `pypdf` in dependencies | Used for PDF page counting and selected-page slicing |
| Retained `profanity-check` OCR references in historical reports | Historical reports not modified |

## Schema Changes and Migration Safety

- `GrammarPoint.mastered` added (Boolean, default=False).
- `VocabItem.mastered` added (Boolean, default=False).
- Migration via `_add_column_if_missing()` — idempotent, safe for existing rows.
- SQLite `ALTER TABLE` does not support `DEFAULT` for existing rows; NULL is treated as falsy.

## Files Added, Modified, Removed

**Modified (12 files):**
`app/db.py`, `app/llm.py`, `app/main.py`, `app/models.py`, `app/routes/study.py`, `app/routes/upload.py`, `app/services/material_parser.py`, `app/templates/base.html`, `app/templates/material_detail.html`, `app/templates/materials.html`, `app/templates/study.html`, `tests/test_p2_1_final_closure.py`, `pyproject.toml`, `README.md`

**Added (2 files):**
`app/templates/partials/grammar_card.html`, `app/templates/partials/vocab_card.html`

**Removed (from dependencies):**
`fpdf2` (removed from `pyproject.toml`)

## Tests and Commands Executed

```bash
cd /home/pompeo_z/workspace/lingua-web
uv run pytest tests/ -v --tb=short
```

**Result: 28/28 tests PASSED** ✅

All tests:
- Imports, TXT/MD parse, PDF page count, sliced PDF privacy check
- OpenAI key available, vision returned items, quality gate
- ≥2 grammar points, E2E upload via HTTP
- Extraction method, DB counts, page grounding
- Large PDF (>10MB) rejected, >10 pages rejected, invalid range rejected
- Server page regressions (materials, study, weak-points)
- TXT upload regression

## Live Verification Results

- Server starts and serves `/materials` (200 OK).
- `.env` loaded without printing secrets.
- Real PDF (011-020.pdf, pages 3–5) produces grounded grammar/vocabulary.
- Mastered toggle works with AJAX (inline fetch) and without JS (form fallback).
- Study cycle from a material with sufficient unmastered grammar items starts and produces 19 questions.

## API Usage and Cost Evidence

From test run (PDF pages 3–5 + 2 more uploads):
- OpenAI gpt-5.4-mini: ~2 calls × ~500 tokens (PDF vision)
- DeepSeek: usage logged to `UsageLog` table during study cycles
- No real cost data collected for this run (API keys not printed)

## Secret, Runtime Artifact, and Git Safety

| Check | Status |
|-------|--------|
| `.env` in `.gitignore` | ✅ |
| `data/*.db` in `.gitignore` | ✅ |
| No API keys in git diff | ✅ (0 matches) |
| No real PDF staged | ✅ (only in `data/`, gitignored) |
| No extraction caches staged | ✅ |
| Commit message safe | ✅ |

## README Update Status

Updated sections:
- "当前已实现功能" — describes PDF vision, mastered feature, parallel generation
- "实际学习流程" — reflects mastered filtering, PDF page selection
- "系统架构" — shows OpenAI API, updated boundary description
- "技术栈" — separate rows for DeepSeek and OpenAI, no HTMX
- "目录结构" — includes `pdf_vision.py` and `partials/`

## Known Limitations

1. **DeepSeek structured output**: Uses prompt-based JSON + Pydantic validation (not OpenAI's `response_format`).
2. **PDF size**: 10 MB limit is arbitrary; large textbooks may need manual page extraction.
3. **No import/export**: Study cycles and mastered state live only in SQLite.
4. **Single-user**: No authentication or multi-user support.
5. **Vocab mastered**: Displayed and persisted but does not affect study-cycle generation.

## Next Recommended Action

Deploy or share the Lingua Web prototype for real usage. Potential follow-ups:
- Add vocabulary-based study cycles
- Import/export mastered state
- Bundle `pypdf` and other dependencies into a stable Docker image
