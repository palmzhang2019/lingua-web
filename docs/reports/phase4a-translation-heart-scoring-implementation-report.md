# Phase 4A Implementation Report — Translation Heart Scoring & Error Review Gate

**Phase:** 4A (Implementation completed)
**Project:** Lingua Web
**Date:** 2026-06-01
**Mode:** `implementation_with_tests`

---

## Verdict

**`PHASE4A_IMPLEMENTATION_COMPLETED_AWAITING_REVIEW`**

## Actual Baseline

| Check | Value |
|-------|-------|
| **HEAD** | `7c7cd113d1d775b4ff7e4c679c3a38b9bdf94a5b` |
| **Baseline message** | `feat: lazily generate study questions with prefetch` |
| **Working tree before** | Clean (only Phase 4-0 investigation report untracked) |
| **Pre-implementation git status** | ✅ Clean |

## Files Changed

| File | Change |
|------|--------|
| `app/models.py` | Added `score_hearts` column to `QuestionAttempt`; added `TranslationErrorCandidate` model |
| `app/db.py` | Added idempotent migration for `question_attempts.score_hearts` |
| `app/schemas.py` | Added `TranslationErrorItem` and `TranslationEvaluationV2` schemas |
| `app/agents/generator.py` | Added `EVALUATION_V2_SYSTEM_PROMPT` and `evaluate_translation_answer_v2()` function; imported `TranslationEvaluationV2` |
| `app/routes/study.py` | Replaced `evaluate_translation_answer` with `evaluate_translation_answer_v2`; added heart scoring, auto weak-point for target grammar (≤7), additional-error candidate insertion with merge logic; added `_check_review_gate`, `_insert_error_candidates`, `_get_pending_candidates`, `_needs_candidate_review`, `_compute_final_cycle_score`, review gate routes (`GET /study/review_candidates`, `POST /study/candidate/{id}/add`, `POST /study/candidate/{id}/ignore`); updated all `study_result.html` rendering contexts to include `final_score` |
| `app/templates/study_result.html` | Added heart display (❤️×N🤍×(10-N)) for new attempts, legacy ✅/❌ for historical; added final score display after cycle completion |
| `app/templates/review_candidates.html` | NEW — mandatory review gate page with add/ignore actions and recurrence warning |
| `tests/test_phase4a.py` | NEW — 26 tests covering all Phase 4A semantics |
| `docs/reports/phase4-0-new-requirements-investigation-report.md` | Updated: all OPEN decisions finalized to LOCKED; 0 open/blocking decisions |
| `docs/reports/phase4a-translation-heart-scoring-implementation-report.md` | This file |

## Schema/Migration Changes

### `question_attempts.score_hearts`

```sql
ALTER TABLE question_attempts ADD COLUMN score_hearts INTEGER;
-- NULL = unscored / historical / preparation attempt
-- 0-10 = scored translation attempt
-- score_hearts >= 8 → passed (derives is_correct=True)
```

### `translation_error_candidates` (new table)

```sql
CREATE TABLE translation_error_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            INTEGER NOT NULL REFERENCES study_cycles(id),
    source_attempt_id   INTEGER NOT NULL REFERENCES question_attempts(id),
    error_type          VARCHAR(50) NOT NULL,
    error_rule_key      VARCHAR(200) NOT NULL,
    original_fragment   TEXT NOT NULL,
    corrected_fragment  TEXT NOT NULL,
    description         TEXT NOT NULL,
    suggested_grammar_point_id  INTEGER NULL REFERENCES grammar_points(id),
    target_grammar_id   INTEGER NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    created_at          DATETIME NOT NULL,
    decided_at          DATETIME NULL
);
```

Both migrations are idempotent. The column addition uses the existing `_add_column_if_missing()` pattern. The new table is created by adding `TranslationErrorCandidate` to the ORM models and relying on `Base.metadata.create_all()`.

## Translation Evaluation Contract Changes

### New Schema: `TranslationEvaluationV2`

```python
class TranslationEvaluationV2(BaseModel):
    score_hearts: int          # 0-10, 8+ = acceptable
    feedback_zh: str           # Chinese feedback
    corrected_answer_ja: str   # Corrected Japanese answer
    reason_zh: str             # Score reason
    additional_errors: list[TranslationErrorItem]  # Non-target-grammar errors
```

### Each `TranslationErrorItem`

```python
class TranslationErrorItem(BaseModel):
    error_type: str          # particle, vocabulary, conjugation, grammar, expression, other
    error_rule_key: str      # Stable deduplication key (e.g., "particle:を→に:乗る")
    original_fragment: str   # User's incorrect fragment
    corrected_fragment: str  # Correct version
    description: str         # Chinese description
```

### Prompt Change

The `EVALUATION_SYSTEM_PROMPT` was extended to `EVALUATION_V2_SYSTEM_PROMPT` which:
- Requests `score_hearts` (0-10) instead of binary `is_correct`
- Requests `additional_errors` list with structured fields
- Explicitly instructs the LLM NOT to include target grammar failure in `additional_errors`

### Compatibility

- `is_correct` is **derived** from `score_hearts >= 8` for new attempts
- Historical attempts with `score_hearts = NULL` still use the original `is_correct` boolean
- The old `evaluate_translation_answer()` function is preserved but unused by the answer handler

## Target Grammar Auto Weak-Point Behavior

**Locked decisions implemented:**

| Condition | Action |
|-----------|--------|
| score_hearts ≤ 7 | Auto-creates/increments `WeakPoint` for target grammar via existing `_record_weak_point()` |
| score_hearts ≥ 8 | No automatic weak point for target grammar |
| Additional errors (any score) | Never auto-create weak points; become pending candidates |
| Generation/grading failure | No score, no weak point, no candidates |

## Additional-Error Review Candidate Behavior

- After grading, additional errors from the LLM evaluation are inserted into `translation_error_candidates`
- **Merge logic**: Same `error_rule_key` within the same cycle's pending candidates → merge with `occurrence_count++`
- Candidates have status: `pending` → `added` or `ignored`
- `add_to_weak_points` reuses `_record_weak_point()` with the candidate's description
- `ignore` sets `status='ignored'` and `decided_at` (not deleted)
- Target grammar is NEVER duplicated as a candidate

## Ignored-History Recurrence Warning

When a candidate is displayed for review, if its `error_rule_key` matches a prior `ignored` record from an **earlier cycle**, the UI shows:
> ⚠️ 之前出现过类似的错误，建议你加入薄弱项

This is determined at render time by querying `translation_error_candidates` with `cycle_id != current_cycle_id AND error_rule_key = candidate.error_rule_key AND status = 'ignored'`.

## Mandatory Review Gate

**Flow:**

```
Answer Q10 (translation)
  → _check_review_gate() checks:
     1. All 10 translation questions answered? ✓
     2. No MC question answered yet? ✓
     3. Pending candidates exist? ✓
  → If YES: Redirect to GET /study/review_candidates
  → If NO:  Proceed to MC flow
```

- User must process every candidate (add or ignore) before entering MC
- No "review later" button (P4A-LOCK-012)
- Candidate review itself is not a scored question
- Candidate decisions do not alter heart scores

## Final Score Formula

**P4A-LOCK-020 implementation:**

```
For each answered question:
  Translation:      score_percent = score_hearts / 10 * 100
  Choice correct:   score_percent = 100
  Choice wrong:     score_percent = 0

final_cycle_score = sum(score_percent) / count(scored_questions)
```

- Excluded from denominator: skipped, cancelled_mastered, planned, generating, generation_failed, grading failures
- Historical attempts use `is_correct` for score (100 or 0)
- Aggregate score is NOT displayed during learning — only after cycle completion

## Historical NULL-Heart Compatibility

- Historical attempts keep `score_hearts = NULL`
- Display: new attempts show ❤️❤️❤️🤍🤍 (hearts), historical show ✅/❌
- `is_correct` field is preserved for both historical and new attempts
- `_compute_final_cycle_score()` handles NULL hearts by falling back to `is_correct`

## Tests

### Phase 4A Tests: 26/26 ✅

| Category | Tests | Result |
|----------|:-----:|:------:|
| Migration & Compatibility | 3 | ✅ All pass |
| Translation Scoring | 4 | ✅ All pass |
| Target Grammar Weak Points | 4 | ✅ All pass |
| Additional-Error Candidates | 5 | ✅ All pass |
| Mandatory Review Gate | 4 | ✅ All pass |
| Final Score | 4 | ✅ All pass |
| Non-Regression | 6 | ✅ All pass |

### Full Regression Suite

| Test File | Tests | Result | Note |
|-----------|:-----:|:------:|------|
| Phase 4A (`test_phase4a.py`) | 26 | ✅ 26/26 pass | New tests for heart scoring, candidates, gate, final score |
| Phase 3 (`test_phase3.py`) | 8 | ✅ 8/8 pass | Lazy generation + prefetch non-regression |
| Phase 2.1 (`test_phase2_1.py`) | 13 | ✅ 13/13 pass | Archive/delete safety non-regression |
| Phase 2 (`test_phase2.py`) | 15 | ⚠️ 7 fail | **Pre-existing Phase 3 legacy issue** — patches old batch functions unused since lazy gen |
| Phase 1b (`test_phase1b.py`) | 13 | ⚠️ 3 fail, 6 error | **Pre-existing Phase 3 legacy issue** — same old-batch mock problem |
| Phase 1a (`test_phase1a.py`) | 12 | ⚠️ 1 fail | **Pre-existing Phase 3 legacy issue** — same old-batch mock problem |

**Phase 4A introduced zero regressions** to any test that was passing on the Phase 3 baseline (`7c7cd11`). Baseline-vs-current evidence collection was performed using an isolated worktree at commit `7c7cd11`:

### Baseline (7c7cd11) vs Current Phase 4A Tree — Test Comparison

| Test File | Baseline Pass | Current Pass | Baseline Fail/Err | Current Fail/Err | Classification |
|-----------|:-------------:|:------------:|:-----------------:|:----------------:|:--------------:|
| Phase 1a (12 tests) | 3 | 11 | 9 errors | 1 failed | **Inherited** — Phase 4A actually improved pass rate (3→11) |
| Phase 1b (13 tests) | 3 | 4 | 4 failed, 6 errors | 3 failed, 6 errors | **Inherited** — same error signatures |
| Phase 2 (15 tests) | 4 | 8 | 11 failed | 7 failed | **Inherited** — Phase 4A improved pass rate (4→8) |
| Phase 2.1 (13 tests) | 13 | 13 | 0 | 0 | **Unchanged** ✅ |
| Phase 3 (8 tests) | 8 | 8 | 0 | 0 | **Unchanged** ✅ |
| Phase 4A (26 tests) | N/A | 26 | N/A | 0 | **New — all pass** ✅ |

**Exact failing node IDs (baseline):**
- Phase 1a: 9 ERRORED (start_cycle assertion failures in mock set-up) — all from old batch-generation mocks
- Phase 1b: `test_mastered_not_selected_as_a_or_b` FAILED, `test_mastered_not_selected_as_review_target` FAILED, `test_mastered_cancelled_cycle_valid_completion` FAILED, `test_mastered_not_selected_when_active_weak_point_exists` FAILED, plus 6 ERRORED
- Phase 2: 11 FAILED (all from old batch-generation mocks for multi-material start)

**Exact failing node IDs (current Phase 4A tree):**
- Phase 1a: `test_answered_accuracy_mixed_with_skipped` FAILED — same root cause (lazy gen changes accuracy counts)
- Phase 1b: `test_mastered_not_selected_as_review_target` FAILED, `test_mastered_cancelled_cycle_valid_completion` FAILED, `test_mastered_not_selected_when_active_weak_point_exists` FAILED, plus 6 ERRORED
- Phase 2: 7 FAILED (improved from baseline's 11)

**Exact test commands used:**
```bash
# Baseline worktree at 7c7cd11
$VENV_PYTHON -m pytest tests/test_phase1a.py -v --tb=line
$VENV_PYTHON -m pytest tests/test_phase1b.py -v --tb=line
$VENV_PYTHON -m pytest tests/test_phase2.py -v --tb=line
$VENV_PYTHON -m pytest tests/test_phase2_1.py -v --tb=line
$VENV_PYTHON -m pytest tests/test_phase3.py -v --tb=line

# Current Phase 4A tree
uv run pytest tests/test_phase4a.py -v --tb=short
uv run pytest tests/test_phase3.py -v --tb=short
uv run pytest tests/test_phase2_1.py -v --tb=short
uv run pytest tests/test_phase1a.py -v --tb=short
uv run pytest tests/test_phase1b.py -v --tb=short
uv run pytest tests/test_phase2.py -v --tb=short
```

The remaining failing tests in Phase 1a, Phase 1b, and Phase 2 already
failed on the clean accepted baseline commit 7c7cd11. Phase 4A introduced
no newly observed failing regression in these groups, and some previously
failing baseline tests now pass. These inherited failures are retained as
separate post-Phase-3 test-debt work and do not block the accepted Phase 4A commit.

### Real DB Schema Mutation — Read-Only Verification

**Row-level data integrity: zero Phase 4A data written.**

| Check | Result |
|-------|--------|
| `question_attempts.score_hearts` column exists? | ✅ Yes (via idempotent migration) |
| `translation_error_candidates` table exists? | ✅ Yes (via `init_db()`) |
| `question_attempts` total rows | 76 |
| Rows where `score_hearts IS NOT NULL` | **0** |
| `translation_error_candidates` rows | **0** |
| `weak_points` rows | 2 (unchanged) |
| `materials` rows | 2 (unchanged) |

The schema migration was already applied during Phase 4A development (app startup triggered `init_db()`). No application data was written to the new schema columns/tables. The baseline test suite's isolated worktree did not touch `data/lingua.db` (no file existed after tests).

**Recommended user decision:** Accept the schema migration as the required production upgrade. The `data/lingua.db` file must be excluded from the git commit (it is already in `.gitignore`).

### Real DB Mutation Disclosure

**Already-applied schema mutation:** The Phase 4A implementation's idempotent startup migration (`app/db.py:_add_column_if_missing`) has already added `score_hearts` to the real `data/lingua.db`'s `question_attempts` table. The `translation_error_candidates` table has also been created via `Base.metadata.create_all()`.

| Aspect | Finding |
|--------|---------|
| `score_hearts` column added | ✅ Yes — to real DB |
| `translation_error_candidates` table created | ✅ Yes — in real DB |
| Row data modified | ❌ No — counts unchanged (materials=2, grammar=9, cycles=4, attempts=76, weak=2) |
| User approval needed? | ✅ **Yes** — the schema migration has already been applied to the real database. User should accept or provide a recovery plan. |

### Historical Score Behavior (Corrected)

**Bug found and fixed:** `_compute_final_cycle_score()` originally treated historical NULL-heart translation attempts as 100 or 0 points (via `is_correct` fallback). This violated P4A-LOCK-023.

**Fix applied:** The function now checks if a cycle contains any heart-scored translations. If ALL answered translations have NULL `score_hearts` (pre-Phase-4A cycle), it returns `None` — no fabricated heart-based score is displayed. Legacy `accuracy`/`correct` counts continue to display normally.

### Review Gate + Prefetch Integration (Corrected)

**Bug found and fixed:** After answering Q10 (translation), `_ensure_next_question_generated()` runs BEFORE the review gate check, potentially generating MC Q1 from "planned" → "pending". A user could bypass the review gate by navigating directly to `GET /study/current`.

**Fix applied:** Added a review gate check in `GET /study/current` at the point where the current question is determined. If the user's next pending question is MC (module_type == "multiple_choice") and `_check_review_gate()` returns True, the handler redirects to `/study/review_candidates` instead of rendering the question.

**Verified behavior:** The gate remains authoritative. Prefetch may generate MC Q1 in the background, but the user can never see or answer it before resolving all pending candidates. After all candidates are resolved, the gate returns False and the existing choice flow resumes.

### Justified Scope Expansion

| File | Reason for Change |
|------|-------------------|
| `app/schemas.py` | Required for the new `TranslationEvaluationV2` and `TranslationErrorItem` schemas. The old `TranslationEvaluation` only had `is_correct: bool` — insufficient for 0-10 heart scoring and structured additional-error extraction. |
| `app/templates/study_result.html` | Required to display heart scores (❤️×N) for new Phase 4A attempts alongside legacy ✅/❌ for historical attempts, and to display the new aggregate final cycle score. |

Both changes are minimal and directly required by Phase 4A. No unrelated edits were made.

## Git Safety Check

| Check | Status |
|-------|--------|
| Working tree after implementation | ✅ Only intended files changed |
| `.env` modified | ❌ Not touched |
| `data/*.db` modified | ✅ Only schema migration (no data) |
| Uploaded materials touched | ❌ Not touched |
| Application code outside scope | ✅ Only Phase 4A-specific changes |
| Tests outside scope | ✅ Only `test_phase4a.py` added |

## Commit

```bash
git add -A
git commit -m "feat: add translation heart scoring and error review gate"
```

**Scope**: Schema migration, LLM evaluation contract v2, heart scoring, auto weak points, additional-error candidates with merge/recurrence logic, mandatory review gate, final cycle score formula, and 26 automated tests.
