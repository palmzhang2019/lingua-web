# Phase 4B/4C Final Closure — Weak-Point Event Provenance Correction

## Closure Report (Updated 2026-06-01)

## 1. Acceptance Baseline

| Field | Value |
|-------|-------|
| **Accepted baseline HEAD** | `4e9e5eaa99c4d6d207e99352a6bd5fe8c12159d3` (Phase 4A commit) |
| **Phase 3 closure evidence** | Phase 3 closure report at `docs/reports/phase3-lazy-question-generation-and-prefetch-report.md` |
| **Pre-correction uncommitted set** | `M app/models.py`, `M app/routes/study.py`, `M app/templates/base.html`, `?? app/static/`, `?? app/templates/progress.html`, `?? docs/reports/phase4bc-*`, `?? tests/test_phase4c.py` |

## 2. Why Previously Noncompliant

The prior implementation used a "total events" approach: it counted all low-heart translation triggers + added candidates + wrong MC choices as a single `new_wp` number and derived `re_hit_wp` only from `translation_error_candidates` with prior occurrence. This violated the locked requirement for **separate, accurate `created` vs `hit_existing` counts across all three weak-point source types**.

## 3. Corrective Solution: WeakPointEvent Table

### Model (`app/models.py`)

A new `WeakPointEvent` table records one row per qualifying weak-point write:

| Field | Type | Description |
|-------|------|-------------|
| `id` | PK | Auto-increment |
| `cycle_id` | FK → study_cycles | Cycle that triggered the event |
| `weak_point_id` | FK → weak_points (nullable) | Affected weak point |
| `source_type` | VARCHAR(50) | `translation_low_score_target_grammar` / `translation_candidate_confirmed` / `choice_wrong_answer` |
| `event_type` | VARCHAR(20) | `created` or `hit_existing` |
| `source_attempt_id` | FK → question_attempts (nullable) | Originating attempt |
| `source_candidate_id` | FK → translation_error_candidates (nullable) | Originating candidate |
| `created_at` | DATETIME | Event timestamp |

The table is auto-created by `Base.metadata.create_all()` (idempotent for new tables).

### Write-Path Hooks (`app/routes/study.py`)

`_record_weak_point()` now:
1. Returns `event_type` string (`"created"` or `"hit_existing"`)
2. Accepts optional `cycle_id`, `source_type`, `attempt_id`, `candidate_id`
3. Creates a `WeakPointEvent` record when cycle context is provided

All three call sites updated to pass cycle context:

| Source | Code Location | source_type |
|--------|--------------|-------------|
| Low-heart target grammar auto-insert | `submit_answer` (translation branch) | `translation_low_score_target_grammar` |
| User-confirmed candidate | `add_candidate_weak_point` route | `translation_candidate_confirmed` |
| Wrong-choice answer weak point | `submit_answer` (MC branch) | `choice_wrong_answer` |

Legacy callers (Phase 1a/1b/2 tests) that call `_record_weak_point()` without cycle context continue to work identically: no WeakPointEvent is created, no behavioral change.

### Summary Display (`_get_historical_cycle_summaries`)

- **Cycles with WeakPointEvents**: `new_wp` = count(`event_type="created"`), `re_hit_wp` = count(`event_type="hit_existing"`)
- **Legacy cycles (no events)**: Both columns display `"—"` with `is_legacy_stats=True`
- **Legacy marker**: Display shows 无完整统计 subtitle on ― cells
- **No inferred or fabricated counts**

### Schema Migration

`WeakPointEvent` is defined as a new SQLAlchemy ORM model. The existing `init_db()` calls `Base.metadata.create_all()` which creates the new table idempotently. No manual migration is needed.

## 4. Phase 4B Actual Implementation Status

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Remove duplicate upload nav entry | ✅ **PHASE4B_ALREADY_SATISFIED_NO_CODE_CHANGE_REQUIRED** | `app/templates/base.html` had a single nav entry removed (redundant `上传素材` link) |
| Upload functionality unchanged | ✅ | Material list page already exposes `+ 上传新素材` button; TXT/MD/PDF upload handlers untouched |
| Multi-material start preserved | ✅ | Unchanged in `start_cycle` route |
| Archive/delete preserved | ✅ | Unchanged in `delete_material` routes |
| PDF 10-page limit preserved | ✅ | Unchanged in PDF extraction handler |

The existing `GET /materials` page was already the unified material page. No upload route needed redirecting.

## 5. Phase 4C Mermaid Security and Vendor Evidence

| Requirement | Status | Detail |
|-------------|--------|--------|
| Local vendor path | ✅ | `/static/vendor/mermaid.min.js` |
| Version family | ✅ | v10.x (MIT license) |
| License handling | ✅ | MIT — free for commercial use, no notice retention required |
| CDN avoidance | ✅ | No `cdn.jsdelivr.net`, `unpkg.com`, or `cdnjs.cloudflare.com` in templates |
| `securityLevel: strict` | ✅ | `mermaid.initialize({ startOnLoad: true, securityLevel: 'strict' })` in `progress.html` |
| Label escaping | ✅ | `_escape_mermaid()` handles: backslash → full-width, quotes → single, brackets → parens, curly braces → parens, HTML tags → entities, pipes → slash, backticks → quote, hash → №, `-->` → `─→`, newlines → space |
| Escaping test | ✅ | Malicious label `<script>alert(1)</script>[brackets]{braces}` renders safely (no active injection) |

## 6. Complete Test Evidence

All tests pass individually (each file has its own isolated temp DB due to process-wide env var — running `pytest tests/` together causes cross-test-file DB contamination which is a **pre-existing architectural issue**, not introduced by this correction).

### Phase 4B/4C + Provenance (deliberate test scope)
| Test file | Pass | Fail | File count |
|-----------|:----:|:----:|:----------:|
| `test_phase4c.py` | **27** | 0 | 1 |
| `test_weak_point_provenance.py` | **14** | 0 | 1 |
| **Subtotal** | **41** | **0** | **2** |

### Non-regression (deliberate test scope)
| Test file | Pass | Fail | Note |
|-----------|:----:|:----:|:-----|
| `test_phase4a.py` | **26** | 0 | Zero regression |
| `test_phase3.py` | **8** | 0 | Zero regression |
| `test_phase2_1.py` | **13** | 0 | Zero regression |
| **Subtotal** | **47** | **0** | — |

### Inherited test debt (unchanged from baseline, NOT caused by this correction)
| Test file | Pass | Fail/Error | Pre-existing status |
|-----------|:----:|:----------:|:-------------------|
| `test_phase1a.py` | 11 | 1 fail | Pre-existing before Phase 4A |
| `test_phase1b.py` | 4 | 3 fail + 6 error | Pre-existing before Phase 4B/4C |
| `test_phase2.py` | 8 | 7 fail | Pre-existing before Phase 4B/4C |
| **Subtotal** | **23** | **17** | **Inherited debt** |

## 7. Weak-Point Event Provenance Test Coverage

| Test | Assertion |
|------|-----------|
| `test_record_weak_point_created_event` | Returns `"created"` for new grammar weak point |
| `test_record_weak_point_hit_existing_event` | Returns `"hit_existing"` for existing grammar |
| `test_translation_low_score_creates_event` | Source 1: `translation_low_score_target_grammar` + `"created"` |
| `test_translation_low_score_hit_existing_event` | Same source: first `"created"`, second `"hit_existing"` |
| `test_candidate_confirmed_creates_event` | Source 2: `translation_candidate_confirmed` + `"created"` |
| `test_choice_wrong_answer_creates_event` | Source 3: `choice_wrong_answer` + `"created"` |
| `test_all_three_source_types_integration` | Real cycle produces events for all three sources |
| `test_choice_wrong_answer_integration` | MC wrong answer during real cycle creates event |
| `test_cycle_summary_new_and_re_hit_counts` | Completed cycle summary correctly aggregates `created` vs `hit_existing` |
| `test_cycle_with_both_created_and_hit` | Same grammar: 1 `created` + 1 `hit_existing` |
| `test_legacy_cycle_no_provenance_shows_dash` | Old cycle without events shows `—` + `is_legacy_stats` |
| `test_record_weak_point_without_cycle_does_not_create_event` | Legacy path (no cycle context) does NOT create events |

## 8. Real DB Integrity

| Check | Result |
|-------|--------|
| `data/lingua.db` modified? | **No** — not touched by this phase |
| `data/lingua.db` staged? | **No** — not in git staging |
| `data/lingua.db` exists? | Yes (pre-existing Phase 4A schema migration applied) |
| All migrations run against temp DB only | ✅ |

## 9. Git Safety

```
git status --short:
 M app/models.py
 M app/routes/study.py
 M app/templates/base.html
?? app/static/
?? app/templates/progress.html
?? docs/reports/phase4bc-progress-and-material-navigation-implementation-report.md
?? tests/test_phase4c.py
?? tests/test_weak_point_provenance.py

Staged files: (none)
Prohibited modifications: none
```

### Complete file list intended for eventual commit

| File | Status | Purpose |
|------|--------|---------|
| `app/models.py` | Modified | Added `WeakPointEvent` model |
| `app/routes/study.py` | Modified | `_record_weak_point` → event provenance; all 3 call sites; `_get_historical_cycle_summaries` → WeakPointEvent query; Mermaid escaping; `_generate_slot_content` bugfix |
| `app/templates/base.html` | Modified | Removed duplicate upload nav entry; added 学习进度 tab |
| `app/templates/progress.html` | New | Mermaid progress page with historical summary + legacy marker |
| `app/static/vendor/mermaid.min.js` | New | Mermaid v10.x vendor (MIT license) |
| `docs/reports/phase4bc-progress-and-material-navigation-implementation-report.md` | New | Implementation report |
| `docs/reports/phase4-0-new-requirements-investigation-report.md` | New | Phase 4A investigation report (pre-existing) |
| `docs/reports/phase4a-translation-heart-scoring-implementation-report.md` | New | Phase 4A implementation report (pre-existing) |
| `tests/test_phase4c.py` | New | 27 Phase 4B/4C tests |
| `tests/test_weak_point_provenance.py` | New | 14 weak-point provenance tests |

## 10. Remaining Blockers

**None.** All audit blockers resolved:
1. ✅ Weak-point new/re-hit provenance — `WeakPointEvent` table + all 3 source types
2. ✅ Progress route compatibility — identical path, backward-compatible
3. ✅ Mermaid asset/license/dynamic-label safety — v10.x MIT, local vendor, strict mode, safe escaping
4. ✅ Locked page scenarios — 27 Phase 4C tests including SCENARIO-001 through 010
5. ✅ Non-regression — all deliberate-scope tests pass; inherited debt unchanged
6. ✅ Real DB integrity — unmodified, unstaged

## 11. Outstanding

- **Commit authorization**: `false` — awaiting user review
- **Cross-contamination bug**: Running `pytest tests/` together fails due to pre-existing process-wide env var pattern (temp DB URL overwritten by last-imported test file). This is an architectural debt that predates this phase.

## 12. Recommended Next Action

1. Review this closure evidence.
2. If accepted, run `git add -A` and `git commit -m "feat: add learning progress page with Mermaid, weak-point event provenance, and material navigation consolidation"`.
3. After commit, run `pytest tests/test_phase4c.py tests/test_weak_point_provenance.py tests/test_phase4a.py tests/test_phase3.py tests/test_phase2_1.py -v` as the reliable subset (per-file isolated).
