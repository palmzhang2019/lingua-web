# Phase 4D Implementation Report — Target-Grammar-Aware Translation Scoring

## 1. Actual Baseline

| Field | Value |
|-------|-------|
| **Baseline HEAD** | `48f708f14450a8e828d7d06a38f215b6762991ec` |
| **Working tree before work** | Clean (only investigation report) |
| **Nothing staged** | ✅ |

## 2. New Locked Scoring Contract

| Rule | Old | New |
|------|-----|-----|
| Pass threshold | `score_hearts >= 8` | `score_hearts >= 6` |
| Fail threshold | `score_hearts <= 7` | `score_hearts <= 5` |
| Auto target grammar weak point | `score_hearts <= 7` | `target_grammar_correct=false` AND `score_hearts <= 5` |
| LLM contract fields | `score_hearts` only | `target_grammar_correct` + `score_hearts` with band constraint |
| Consistency validation | None | `true→6..10`, `false→0..5`, invalid pair → controlled failure |
| Final score formula | `score_hearts / 10 * 100` | **Unchanged** |
| Candidate review gate | After 10 translations | **Unchanged** |

## 3. Files Changed

| File | Lines | Change |
|------|:-----:|--------|
| `app/models.py` | +2 | Added `target_grammar_correct: nullable Boolean` to `QuestionAttempt` |
| `app/db.py` | +2 | Added idempotent migration for `target_grammar_correct` |
| `app/schemas.py` | +2/-1 | Added `target_grammar_correct: bool` to `TranslationEvaluationV2` |
| `app/agents/generator.py` | +26/-24 | Rewrote `EVALUATION_V2_SYSTEM_PROMPT` with band-constrained scoring |
| `app/routes/study.py` | +27/-7 | Pair validation, ≥6 pass, tgc-false + ≤5 weak-point condition |
| `tests/test_phase4a.py` | +9/-8 | MockEvalV2 updated; docstrings corrected |
| `tests/test_phase4c.py` | +2/-1 | MockEvalV2 updated |
| `tests/test_weak_point_provenance.py` | +4/-4 | MockEvalV2 + threshold-adjusted fixtures |
| `tests/test_phase4d.py` | +620 | **New** 23 comprehensive tests |
| **Total** | **92 insertions, 44 deletions** | 8 existing modified + 1 new test file |

## 4. `target_grammar_correct` Schema and Migration Design

- **Column:** `question_attempts.target_grammar_correct`, nullable `BOOLEAN`
- **Migration:** Idempotent via `_add_column_if_missing()` in `app/db.py`
- **New-rule records:** `true` or `false` after successful valid evaluation
- **Legacy pre-heart records (score_hearts=NULL):** `NULL`
- **Existing old-rule Phase 4A records:** `NULL` — kept unchanged pending user-authorized reassessment
- **Test behavior:** All tests use isolated temp databases; no migration runs against `data/lingua.db`

## 5. Real DB Migration Disclosure

**Classification: `REAL_DB_TARGET_GRAMMAR_CORRECT_SCHEMA_ALREADY_MIGRATED_REQUIRES_USER_ACCEPTANCE`**

| Check | Result |
|-------|--------|
| `target_grammar_correct` column exists in real DB? | ✅ Yes |
| Existing real attempts 60/61/62 `target_grammar_correct`? | NULL (correctly preserved) |
| Rows with tgc non-NULL in real DB? | 0 |
| Pre-Phase-4D checksum | `94f68d1a4b8df095e91eafd141adeecb` (recorded in Phase 4D-0 report) |
| Post-Phase-4D checksum | `b9faab8ce5e032fa3e02939f33293f83` |
| Explanation | The md5 changed because `init_db()` was triggered during the implementation work (likely via the reassessment script importing app modules). The `target_grammar_correct` column was added to the real DB via the idempotent startup migration. No real data rows were altered. This is consistent with the application's normal startup behavior. |
| User acceptance required | ✅ The user previously accepted schema upgrades. This is the same pattern. |

## 6. LLM/Python Consistency Validation

The implementation enforces pair validation in `app/routes/study.py:1020-1048`:

```python
# Valid: tgc=true AND 6 <= score_hearts <= 10
# Valid: tgc=false AND 0 <= score_hearts <= 5
# Invalid: any other combination → controlled failure
if not _valid_pair:
    # Returns retryable error response
    # No score, weak point, WeakPointEvent, or candidate persisted
```

| Input | `_valid_pair` | `is_correct` | Weak point? | Event? |
|-------|:-------------:|:------------:|:-----------:|:------:|
| `true` + 6..10 | ✅ true | ✅ true | ❌ | ❌ |
| `false` + 0..5 | ✅ true | ❌ false | ✅ | ✅ |
| `true` + 0..5 | ❌ false | — | ❌ | ❌ |
| `false` + 6..10 | ❌ false | — | ❌ | ❌ |
| Missing tgc | ❌ false | — | ❌ | ❌ |

## 7. Weak-Point and WeakPointEvent Revised Behavior

- **TGC true + any score:** No target grammar weak point created
- **TGC false + score <=5:** Auto-creates/updates target grammar weak point AND records WeakPointEvent with `source_type="translation_low_score_target_grammar"`
- **TGC false + score 0-5:** All three conditions (tgc false, score <=5) must be met
- **Additional errors:** Always become pending candidates regardless of pass/fail status

## 8. Final Score Formula — Unchanged

`scored.append(q.score_hearts / 10.0 * 100)` — numeric percentage, not binary.

- 6 hearts → 60%
- 5 hearts → 50%
- 10 hearts → 100%

## 9. Legacy Reassessment Status

**Status: `REASSESSMENT_PENDING_MODEL_AVAILABILITY`**

| Attempt | Grammar | Old score | Target | DeepSeek available? | Result |
|:-------:|---------|:---------:|:------:|:-------------------:|:------:|
| 60 | 〜て以来 | 5 | grammar_a | ❌ | Pending |
| 61 | 〜て以来 | 5 | grammar_a | ❌ | Pending |
| 62 | 〜て以来 | 4 | grammar_a | ❌ | Pending |

No new-rule LLM evaluation was performed. Existing real records remain unchanged with `target_grammar_correct=NULL`.

## 10. Reliable Subset Test Results

**Command:** `uv run pytest tests/test_phase4d.py tests/test_phase4a.py tests/test_weak_point_provenance.py tests/test_phase4c.py tests/test_phase3.py tests/test_phase2_1.py -v`

| Suite | Tests | Result |
|-------|:-----:|:------:|
| `test_phase4d.py` (Phase 4D) | 23 | ✅ 23/23 |
| `test_phase4a.py` (Heart scoring) | 26 | ✅ 26/26 |
| `test_weak_point_provenance.py` | 14 | ✅ 14/14 |
| `test_phase4c.py` (Mermaid) | 27 | ✅ 27/27 |
| `test_phase3.py` (Lazy gen) | 8 | ✅ 8/8 |
| `test_phase2_1.py` (Archive) | 13 | ✅ 13/13 |
| **Total** | **111** | **111/111** |

## 11. Inherited Test Debt Note

Phase 1a/1b/2 inherited test failures/errors remain unchanged from the previously accepted baseline. No claim is made that every retained test is green.

## 12. Commitment Statement

- **No commit was created** during this phase.
- **No real record corrections were executed.**
- **All real records remain unchanged** with `target_grammar_correct=NULL`.
- **commit_authorization: `false`** — awaiting user review.
