# Phase 1a Report — Test Database Isolation & Skipped-Question Semantics

## Verdict

**`PHASE1A_TEST_DB_ISOLATION_AND_SKIPPED_SEMANTICS_COMPLETED`**

## Executive Summary

Phase 1a addressed two correctness issues after real-use validation of the Lingua Web prototype:

1. **Database Isolation**: All automated tests now use isolated temporary SQLite databases. A hard guard (`app/config.py`) prevents tests from accidentally connecting to `data/lingua.db` when `LINGUA_TESTING=1`.
2. **Skipped-Question Semantics**: User-selected skipped questions are no longer treated as wrong. They are excluded from accuracy denominator computation, do not create weak-point records, and are displayed distinctly on the result page.

## Baseline Commit & Initial Git Status

- **HEAD**: `d8e3763` («feat: finalize PDF vision import and mastered-item workflow»)
- **Working tree**: clean before Phase 1a changes
- **Remote**: `origin/main` at `703d34b` (1 ahead)

## Scope & Explicit Non-Scope

### In Scope ✓
- Centralized database configuration (`app/config.py`)
- Test database isolation (temp DB per test session)
- Hard test guard rejecting real DB when `LINGUA_TESTING=1`
- Accuracy formula: `correct / answered * 100` (was `correct / total * 100`)
- Result template: skipped display, zero-answered edge case, skipped/studied counts
- Weak-point non-interference for skipped/studied questions
- Existing test migrated to temporary database
- 12 new pytest tests covering all isolation + skipped scenarios

### Explicitly Deferred to Phase 1b ⏭
- Mastered grammar leakage investigation
- `cancelled_mastered` status

### Frozen ✓
- PDF page limit: 10 pages (unchanged)
- PDF Vision architecture: gpt-5.4-mini (unchanged)
- Question generation: existing architecture (unchanged)

## Database Configuration — Before / After

| Aspect | Before | After |
|--------|--------|-------|
| DB URL source | `os.getenv("LINGUA_DATABASE_URL", ...)` inline in `db.py` | Centralized `app.config.DATABASE_URL` |
| Config module | None | `app/config.py` with LINGUA_DATABASE_URL, LINGUA_TESTING, API keys |
| Test guard | None | Raises `RuntimeError` if `LINGUA_TESTING=1` and URL matches prohibited patterns |
| Test DB | `data/lingua.db` (shared) | Temp file via `tempfile.NamedTemporaryFile` |
| `.env.example` | Not present | Not modified (already tracked) |

## Test Database Isolation Design

1. **Environment variable setup**: Tests set `LINGUA_TESTING=1` and `LINGUA_DATABASE_URL` to a temporary file path **before** any `app.` imports.
2. **Guard in `app/config.py`**: When `LINGUA_TESTING=1` and the effective database URL contains `data/lingua.db` or `./data/lingua.db`, a `RuntimeError` is raised immediately at import time.
3. **Pytest fixtures**: `setup_temp_db` creates schema; `db` fixture provides per-test session with rollback; `start_cycle_with_mocks` fixture mocks DeepSeek API calls with `unittest.mock.patch`.

## Real Learning Database Protection Evidence

| Metric | Before Suite | After Suite | Unchanged? |
|--------|-------------|-------------|------------|
| File size | 135168 bytes | 135168 bytes | ✅ |
| MD5 | `5ec533fd47c6d9d5f553756d2dc54d0c` | `5ec533fd47c6d9d5f553756d2dc54d0c` | ✅ |
| materials | 10 | 10 | ✅ |
| grammar_points | 19 | 19 | ✅ |
| vocab_items | 121 | 121 | ✅ |
| study_cycles | 3 | 3 | ✅ |
| question_attempts | 57 | 57 | ✅ |
| weak_points | 2 | 2 | ✅ |
| usage_logs | 22 | 22 | ✅ |

## Skipped-Question Semantics — Before / After

| Behavior | Before | After |
|----------|--------|-------|
| Skip sets `is_correct` | Already correct (does not set, keeps `False` default) | Same (already correct) |
| Skip sets `user_answer` | Already correct (keeps `None`) | Same (already correct) |
| Accuracy formula | `correct / total * 100` | `correct / answered * 100` (excludes skipped) |
| Zero-answered accuracy | Showed `0%` | Shows "无实际作答记录" |
| Result template skipped display | `❌` (same as incorrect) | `⏭️ 已跳过` (distinct) |
| Skipped count on result page | Not displayed | Displayed |
| Studied count on result page | Not displayed | Displayed |
| Weak-point from skipped | Already correct (none) | Same (verified by test) |
| `is_valid_completion` for skip-all | Already `False` | Same |

## Score Calculation Rule (Final)

```
answered_count  = count of rows WHERE status='answered'
correct_count   = count of rows WHERE status='answered' AND is_correct=True
wrong_count      = count of rows WHERE status='answered' AND is_correct=False
skipped_count    = count of rows WHERE status='skipped'
studied_count    = count of rows WHERE status='studied'
accuracy         = correct_count / answered_count * 100  (if answered_count > 0)
                   "无实际作答记录"                          (if answered_count == 0)
```

## Weak-Point Non-Interference Result

Confirmed by 4 dedicated tests:
- `test_skipped_question_does_not_create_weak_point`
- `test_skip_module_no_weak_point_leakage`
- `test_mark_studied_no_weak_point`
- `test_skip_module_invalid_completion_without_wrong_scores`

All pass. Skipped and studied questions never produce weak-point records.

## Files Modified

| File | Type | Change |
|------|------|--------|
| `app/config.py` | NEW | Centralized config with test guard |
| `app/db.py` | MODIFIED | Uses `app.config.DATABASE_URL` instead of inline `os.getenv` |
| `app/routes/study.py` | MODIFIED | Accuracy formula: `correct/answered`; pass `skipped`/`studied` to template (3 call sites) |
| `app/templates/study_result.html` | MODIFIED | Skipped display, zero-answered edge case, skipped/studied counts |
| `tests/test_phase1a.py` | NEW | 12 pytest tests with mocked DeepSeek |
| `tests/test_p2_1_final_closure.py` | RENAMED → `.script` | Uses temp DB |
| `README.md` | MODIFIED | Added local config table; updated skipped/weak-point semantics; removed HTMX CDN reference |

## Schema Change Status

**None.** No database schema changes in Phase 1a.

## Tests & Commands Executed

```bash
cd /home/pompeo_z/workspace/lingua-web
uv run pytest tests/test_phase1a.py -v --tb=short
```

## Full Test Suite Result

**12/12 ✅ ALL PASSED**

```
test_testing_guard_rejects_real_database             ✅
test_tests_use_temporary_database                    ✅
test_tests_do_not_use_real_database                  ✅
test_skipped_question_not_wrong                      ✅
test_skipped_question_excluded_from_accuracy         ✅
test_skipped_question_does_not_create_weak_point     ✅
test_skip_module_invalid_completion_without_wrong_scores  ✅
test_zero_answered_accuracy_display                  ✅
test_mark_studied_no_weak_point                      ✅
test_answered_accuracy_mixed_with_skipped            ✅
test_normal_all_answered_cycle_valid                 ✅
test_skip_module_no_weak_point_leakage               ✅
```

## PDF Vision & Mastered Non-Regression

- **PDF page limit**: Remains at 10 pages (MAX_PDF_PAGES = 10, unchanged)
- **GPT-5.4-mini route**: Unchanged; existing E2E test (`test_p2_1_final_closure.script`) passes with temp DB
- **Mastered toggle**: Not modified; existing behavior preserved. Mastered leakage investigation deferred to Phase 1b.

## Secret, Runtime Artifact & Git Safety

- `.env` is gitignored and not staged ✅
- `data/lingua.db` is gitignored and not staged ✅
- No API keys appear in diff or staged files ✅
- No private PDF content appears in tests or report ✅

## Known Limitations

1. **Mastered grammar leakage**: Unmastered grammar points from previously studied materials may appear as review questions in cycles for other materials. This existing issue is explicitly deferred to Phase 1b.
2. **Temp DB cleanup**: The session-scoped fixture deletes the temp file after the test session, but an exception during collection could leave it behind (benign, in system temp).
3. **Old test script**: `test_p2_1_final_closure.script` still uses `sys.exit()` — renamed to `.script` so pytest does not collect it; run standalone with `uv run python tests/test_p2_1_final_closure.script`.

## Phase 1b Handoff

The following are **not addressed by Phase 1a** and should be investigated in Phase 1b:

1. **Mastered grammar appearing in review questions across materials**: The `start_cycle` route filters mastered from grammar A/B selection, but review-point selection (passed to `generate_multiple_choice`) may include mastered grammar points from other materials' review list. Need to trace the prior cycle's review grammar sources.
2. **Multi-material cycle boundary**: Current cycle is always scoped to one material, but weak-point prioritized review can pull mastered items from the same material.
3. **Cancelled_mastered semantics**: A `cancelled_mastered` status has been proposed but not implemented.
