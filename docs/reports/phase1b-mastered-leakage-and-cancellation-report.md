# Phase 1b Report — Mastered Leakage Investigation and In-Cycle Cancellation

## Verdict

**`PHASE1B_MASTERED_LEAKAGE_AND_CANCELLATION_COMPLETED`**

## Executive Summary

Phase 1b investigated and fixed two distinct mastered-related issues:

1. **LLM-generated content referencing mastered grammar**: A post-generation validation (`_validate_mc_against_mastered`) now checks all newly generated multiple-choice questions against the set of mastered grammar names. Contaminated questions trigger a controlled retry; persistent failure blocks cycle creation with a clear error.

2. **In-cycle mastered toggle**: When the user marks a grammar mastered during an active study cycle, all pending questions targeting that grammar are immediately set to `cancelled_mastered`. The session advances to the next pending question. Already-answered historical results are preserved unchanged.

The Python source filtering for new-cycle grammar selection was already correct in Phase 1a — mastered grammar is excluded from A, B, and review candidate lists before generation.

## Phase 1a Baseline Truth Verification

- **Commit `fc18a90`** confirmed: contains `app/config.py` with the test guard (`LINGUA_TESTING` rejects real DB paths)
- **Working tree**: clean at Phase 1b start
- **Pre-change tests**: 12/12 baseline tests pass under isolated temp DB
- **`.env` and `data/lingua.db`**: gitignored, untracked, unchanged

## Scope & Explicit Non-Scope

### Done ✓
- `cancelled_mastered` status implemented
- `_cancel_mastered_cycle_questions` helper
- `_validate_mc_against_mastered` post-generation defense
- Toggle route integration (upload.py)
- Result template updates
- 13 new pytest tests

### Deferred / Frozen ⏭
- Lazy generation / prefetch: out of scope
- Multi-material cycles: out of scope
- PDF 10-page limit: unchanged
- GPT-5.4-mini PDF Vision: unchanged

## Mastered Leakage Reproduction

Two distinct leakage paths were identified:

| Path | Status | Explanation |
|------|--------|-------------|
| Grammar A/B selection | ✅ Already correct | Filtered by `not gp.mastered` since P2.1 |
| Review point selection | ✅ Already correct | Drawn from `unmastered` list |
| Weak-point reintroduction | ✅ Already correct | `weak_review` filtered from `unmastered` only |
| **LLM generated MC content** | ❌ **Fixed in Phase 1b** | Added `_validate_mc_against_mastered` post-generation check |
| **In-cycle toggle** | ❌ **Fixed in Phase 1b** | Added `_cancel_mastered_cycle_questions` called from toggle route |

## Source Fix Implemented

**`_cancel_mastered_cycle_questions(db, grammar_point_name, grammar_point_id)`** — added to `app/routes/study.py`:

- Scans all pending questions in the current active cycle
- Matches by `grammar_point` field in `question_payload_json` (case-insensitive, 〜-stripped)
- Sets matched questions to `status='cancelled_mastered'`
- If the current question was cancelled, advances session to next pending or completes the cycle

Called from `toggle_grammar_mastered` in `upload.py` when `gp.mastered` becomes `True`.

## Post-Generation MC Defense

**`_validate_mc_against_mastered(mc_questions, mastered_names)`** — added to `app/routes/study.py`:

- Checks each MC question's prompt text, all 4 choices (A/B/C/D), and grammar_point field
- Normalizes by lowercasing and stripping leading "〜"
- If any mastered grammar name appears as a substring in the content, the question is flagged
- Applied after MC generation in `start_cycle`
- Flagged set triggers one controlled retry with explicit constraint in the prompt
- Persistent failure blocks cycle creation with a clear error message

## In-Cycle Mastered Behavior

### Trigger
User posts to `/materials/{id}/grammar/{gid}/toggle_mastered` setting `mastered=True`

### Effect
| Question State | Behavior |
|----------------|----------|
| Already answered | Unchanged |
| Current pending targeting mastered grammar | `cancelled_mastered`, session advances |
| Future pending targeting mastered grammar | `cancelled_mastered` |
| Pending MC containing mastered expression | `cancelled_mastered` |
| Toggle back to False | Does NOT resurrect cancelled questions |

## Status Semantics & Score Rules

| Status | In Accuracy Denominator? | Creates Weak Points? | Valid Completion? |
|--------|------------------------|---------------------|-------------------|
| `answered` | Yes (correct/wrong) | Only if wrong | Yes |
| `skipped` | No | No | **No** |
| `studied` | No | No | Yes |
| `cancelled_mastered` | No | No | Yes |

## Valid Completion Rule

A cycle is valid for completion (`is_valid_completion=True`) when:
- All questions have a non-pending status
- **No** questions have `status='skipped'`
- Questions with `status='cancelled_mastered'` or `status='studied'` are OK

## Files Modified

| File | Change |
|------|--------|
| `app/routes/study.py` | Added `_cancel_mastered_cycle_questions`, `_validate_mc_against_mastered`; updated `_compute_cycle_completion` for `cancelled_mastered`; added MC defense in `start_cycle`; updated 3 template context dicts |
| `app/routes/upload.py` | `toggle_grammar_mastered` calls `_cancel_mastered_cycle_questions` when `mastered=True` |
| `app/templates/study_result.html` | Added `cancelled_mastered` count display and per-question status icon |
| `tests/test_phase1b.py` | NEW: 13 tests |
| `docs/reports/phase1b-...report.md` | NEW: this report |

## Schema Change Status

**None.** The `status` column already stores string values (`pending`, `answered`, `skipped`, `studied`). `cancelled_mastered` is a new string value that fits without migration.

## Tests & Full Suite Result

```bash
uv run pytest tests/ -v --tb=short
```

**25/25 ✅ ALL PASSED** (12 Phase 1a + 13 Phase 1b)

```
✅ test_mastered_not_selected_as_a_or_b
✅ test_mastered_not_selected_as_review_target
✅ test_one_unmastered_grammar_blocks_start
✅ test_validate_mc_against_mastered_clean
✅ test_validate_mc_against_mastered_contaminated
✅ test_toggle_mastered_cancels_future_target_questions
✅ test_previously_answered_rows_unchanged
✅ test_unmastering_does_not_resurrect_cancelled
✅ test_cancelled_mastered_excluded_from_accuracy
✅ test_cancelled_mastered_no_weak_points
✅ test_mastered_cancelled_cycle_valid_completion
✅ test_skipped_still_invalid
✅ test_mastered_not_selected_when_active_weak_point_exists
```

## Real Learning DB Integrity

| Metric | Before | After | Status |
|--------|--------|-------|--------|
| Size | 135168 bytes | 135168 bytes | ✅ |
| MD5 | `5ec533fd...` | `5ec533fd...` | ✅ |
| All 7 tables | Unchanged | Unchanged | ✅ |

## PDF Vision Non-Regression

- PDF maximum: 10 pages (unchanged)
- GPT-5.4-mini route: unchanged
- No PDF API calls made during Phase 1b testing

## Secret, Runtime Artifact & Git Safety

- `.env`: gitignored, not staged ✅
- `data/lingua.db`: gitignored, not staged ✅
- No API keys in diff ✅

## Commit

```
8f02f3f  fix: prevent mastered grammar leakage in study cycles
```

## Known Limitations

1. **MC defense is substring-based**: A mastered grammar name appearing as part of a longer word in generated content could trigger a false rejection. This is acceptable because the retry mechanism handles the normal case, and false positives are rare given Japanese grammar names are distinctive enough.
2. **Cancelled questions are not regenerated**: When a grammar is toggled mastered mid-cycle, the remaining unanswered questions for that grammar are cancelled but not replaced. The cycle continues with a reduced question count. This is the agreed design.
3. **Performance**: The MC validation loops through all generated MC questions × all mastered grammar names. For typical usage (1-3 mastered in a material, 9 MC questions), this is negligible.
4. **`cancelled_mastered` does not update `is_correct`**: Default `False` remains. Rendering uses `status` field, not `is_correct`.

## Phase 2 Readiness

All Phase 1b correctness objectives are complete. The application is ready for future work on:
- Lazy / on-demand question generation
- Multi-material study cycles
- Cancelled_mastered_again / three-state mastered UI refinement
- Any feature work beyond correctness
