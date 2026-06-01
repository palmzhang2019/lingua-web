# Phase 4 Final State — Planning Input for LLM C

```
generated_for: LLM C (next development plan generator)
based_on_head: 497d0925f010d9c742ab765dc69355908207ee7a (2026-06-01)
```

## 1. Purpose of This Planning Input

This report describes the current accepted product state of Lingua Web through
Phase 4D. It is **not** a development plan. It provides precise constraints,
evidence, technical debt, and candidate next directions so that LLM C can produce
an informed, minimal, testable next-phase plan without guessing or re‑investigating.

---

## 2. Repository Baseline and Accepted Commit Chain

| Commit | Message | Capability |
|:-------|:--------|:-----------|
| `4e9e5ea` | `feat: add translation heart scoring and error review gate` | Phase 4A — heart scoring (`score_hearts` 0‑10, ≥8 pass), additional‑error candidates, mandatory review gate, final score formula |
| `48f708f` | `feat: add learning progress page with Mermaid and weak-point provenance` | Phase 4B/4C — Mermaid progress page (local vendor), `WeakPointEvent` table, material‑nav consolidation, MC pre‑generation scope‑bug fix |
| `a68cd4b` | `feat: make translation scoring target-grammar aware` | Phase 4D — `target_grammar_correct` field, ≥6 pass / ≤5 fail, contradictory pair rejection, tgc‑aware weak‑point triggers |
| `497d092` | `docs: record Phase 4D legacy correction and voided-event semantics` | Phase 4D closure — real‑record correction (attempts 60/61/62), `event_type=voided` semantics, test for voided exclusion |

**Latest accepted HEAD:** `497d0925f010d9c742ab765dc69355908207ee7a`

---

## 3. Current Product Architecture and Major Routes/Modules

**Stack:** Python 3.11 + FastAPI + SQLite + SQLAlchemy 2.x + Jinja2 (no external CDN)

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI |
| Database | SQLite (`data/lingua.db`) |
| ORM | SQLAlchemy 2.x |
| Templates | Jinja2 (server‑rendered) |
| LLM | DeepSeek API (OpenAI‑compatible SDK) |
| PDF Vision | OpenAI gpt‑5.4‑mini |
| Charts | Mermaid (local `app/static/vendor/mermaid.min.js`) |

**Key source files:**

| File | Responsibility |
|------|:--------------|
| `app/main.py` | FastAPI entry point, route mounts, `load_dotenv()`, startup migration |
| `app/db.py` | SQLAlchemy engine, session, idempotent column migrations |
| `app/models.py` | ORM models (Material, GrammarPoint, QuestionAttempt, WeakPoint, WeakPointEvent, TranslationErrorCandidate, etc.) |
| `app/schemas.py` | Pydantic schemas (extraction, translation evaluation v2, question payloads) |
| `app/llm.py` | DeepSeek adapter, `is_available()`, `structured_extraction()`, usage tracking |
| `app/agents/extractor.py` | Grammar/vocab extraction from materials |
| `app/agents/generator.py` | Cycle generation, translation evaluation prompt |
| `app/routes/upload.py` | Material upload, delete, display |
| `app/routes/study.py` | All study‑cycle runtime: answer submission, heart scoring, candidate review gate, progress page, cycle completion, weak‑point recording |

**Tests per phase:**

| Suite | Tests | File |
|:------|:-----:|:-----|
| Phase 4D (scoring v2) | 23 | `tests/test_phase4d.py` |
| Phase 4A (heart scoring) | 26 | `tests/test_phase4a.py` |
| Phase 4C (Mermaid progress) | 27 | `tests/test_phase4c.py` |
| WeakPointEvent provenance | 16 | `tests/test_weak_point_provenance.py` |
| Phase 3 (lazy generation) | 8 | `tests/test_phase3.py` |
| Phase 2.1 (archive/delete) | 13 | `tests/test_phase2_1.py` |

---

## 4. Accepted End-to-End User Learning Flow

1. **Material upload** (`/materials`) — TXT/MD via DeepSeek extraction; PDF via gpt‑5.4‑mini Vision (≤10 selected pages)
2. **Start cycle** (`POST /study/start_cycle`) — picks two unmastered grammar points
3. **Translation stage** (10 questions, 5 per grammar) — each submitted and graded
4. **Candidate review gate** (`GET /study/review_candidates`) — if pending candidates exist, user processes each as `add_to_weak_points` or `ignore` before proceeding to choices
5. **Choice stage** (9 questions: 4 distinction + 5 review) — deterministic grading
6. **Cycle complete** — final score displayed; `/study/progress` shows Mermaid flowchart + history

---

## 5. Locked Product Semantics That Future Plans Must Preserve

- A full cycle is 10 translation + 9 choice questions (19 total)
- Translation questions are graded first; candidate review gate **must** process before choices
- `skipped`, `cancelled_mastered`, `planned`, `generating`, `generation_failed` states never enter scoring denominator
- Session resume restores exact position (works at question‑level granularity)
- Mastered grammar points are excluded from future cycles
- Hard‑delete removes only unused materials; used materials are archived for history
- Lazy generation/prefetch (Phase 3) must not bypass the mandatory review gate

---

## 6. Translation Scoring and Final Score Contract

### Phase 4D validity matrix

| `target_grammar_correct` | `score_hearts` range | Result | Weak‑point action |
|:------------------------:|:--------------------:|:------:|:-----------------:|
| `true` | 6‑10 | ✅ **Pass** | None |
| `false` | 0‑5 | ❌ **Fail** | Auto WP for target grammar |
| `true` | 0‑5 | ⛔ **Rejected** | No side effects |
| `false` | 6‑10 | ⛔ **Rejected** | No side effects |

### Final score formula

- Translation contribution = `score_hearts / 10 × 100` (e.g., 6 hearts → 60%)
- Choice contribution = 100 (correct) or 0 (wrong)
- Final score = equal‑weight average across all answered/attempted questions only
- Excluded from denominator: `skipped`, `cancelled_mastered`, `planned`, `generating`, `generation_failed`, and historical attempts with NULL hearts
- Final score displayed only **after** cycle completion (never mid‑cycle)

---

## 7. Additional-Error Candidate and Review-Gate Contract

- Non‑target grammar problems (particle, vocabulary, conjugation, expression) are collected as `TranslationErrorCandidate` objects
- Each candidate has: `error_type`, `error_rule_key` (for dedup), `original_fragment`, `corrected_fragment`, `description`, `status` (`pending`/`added`/`ignored`), `occurrence_count`
- Same `error_rule_key` within one batch is merged with incremented `occurrence_count`
- **Must** process all pending candidates before entering the choice module
- Adding a candidate writes a `WeakPointEvent` with `source_type=translation_candidate_confirmed`
- Ignoring a candidate does **not** create an event but retains the record
- If a previously‑ignored `error_rule_key` appears again, the UI shows a recurrence warning

---

## 8. WeakPointEvent Provenance and Voided-Event Contract

Three source types for events:

| Source type | When created |
|:------------|:-------------|
| `translation_low_score_target_grammar` | When a translation answer triggers `target_grammar_correct=false` + hearts ≤5 |
| `translation_candidate_confirmed` | When a user confirms `add_to_weak_points` for an error candidate |
| `choice_wrong_answer` | When a wrong multiple‑choice answer triggers a weak‑point write |

Event type values:

| Value | Meaning | Counted? |
|:------|:--------|:---------|
| `created` | New weak‑point record | ✅ Included in `new_wp` count |
| `hit_existing` | Existing weak point triggered again | ✅ Included in `re_hit_wp` count |
| `voided` | Event later proven invalid; preserved for audit | ❌ Excluded from both counts |

`voided` is a documented value used in the Phase 4D real‑record correction. The
existing progress‑summary code counts events by filtering `event_type == "created"`
and `event_type == "hit_existing"`; `voided` events are automatically excluded.

---

## 9. Learning Progress / Mermaid and Material Navigation Current State

- `/study/progress` renders an in‑progress Mermaid flowchart + historical cycle summaries
- Mermaid is loaded from local `app/static/vendor/mermaid.min.js` (no CDN), with `securityLevel: 'strict'`
- Dynamic labels are escaped via a dedicated `_escape_mermaid()` function
- Historical summaries show `new_wp` and `re_hit_wp` counts for reliably tracked cycles; legacy cycles show a `—` marker
- Material list page (`/materials`) is the unified upload/management entry point
- Duplicate upload‑navigation entry has been removed and replaced with the Learning Progress tab

---

## 10. Accepted Real Database Schema and Corrected Real-Record State

### Schema additions accepted as idempotent startup migrations

| Addition | Type | Phase |
|:---------|:-----|:------|
| `question_attempts.score_hearts` | nullable INTEGER (0‑10) | Phase 4A |
| `translation_error_candidates` | new table | Phase 4A |
| `weak_point_events` | new table | Phase 4B |
| `question_attempts.target_grammar_correct` | nullable BOOLEAN | Phase 4D |

Old records preserve NULL for both `score_hearts` and `target_grammar_correct`.

### Authorized real-record correction (executed and closed)

Per the accepted Phase 4D correction report (`docs/reports/phase4d-authorized-real-record-correction-report.md`, committed at `497d092`):

| Attempt ID | Grammar | Old hearts | New hearts | Old correct | New correct | Old TGC | New TGC |
|:----------:|:--------|:----------:|:----------:|:-----------:|:-----------:|:-------:|:-------:|
| 60 | 〜て以来 | 5 | **6** | false | **true** | NULL | **true** |
| 61 | 〜て以来 | 5 | **7** | false | **true** | NULL | **true** |
| 62 | 〜て以来 | 4 | **6** | false | **true** | NULL | **true** |

- WeakPointEvents 1/2/3 (old‑rule false‑positive `translation_low_score_target_grammar`) changed to `event_type=voided`
- `weak_point_id=2.error_count` restored from 6 to 3; `is_active` remains true (3 ≥ 2 threshold)
- **9 genuine `pending` additional‑error candidates remain for these attempts** — this is an active user‑facing state

Backup: `data/backups/lingua.pre-phase4d-record-correction-20260601-145538.db` (local, git‑ignored)

**Do not alter these records without explicit user authorization and a new backup.**

---

## 11. Test Evidence and Inherited Technical Debt

### Verified passing subsets

| Suite | Count | Status |
|:------|:-----:|:------:|
| Phase 4D (scoring v2 + voided) | 23 | ✅ All pass |
| Phase 4A (heart scoring) | 26 | ✅ All pass |
| Phase 4C (Mermaid progress) | 27 | ✅ All pass |
| WeakPointEvent provenance | 16 | ✅ All pass (incl. voided exclusion) |
| Phase 3 (lazy gen + prefetch) | 8 | ✅ All pass |
| Phase 2.1 (archive/delete) | 13 | ✅ Pass in isolation |

**Phase 4D combined‑run baseline comparison** confirmed 0 new regressions.

### Inherited debt (pre‑existing, not caused by Phase 4)

1. **Phase 1a/1b/2 tests** contain failing/erroring tests related to test‑DB isolation and mastered‑leakage semantics. Never claimed green.
2. **Phase 2.1 combined‑run isolation defect:** `test_deleted_unused_mistaken_material_does_not_leave_mastery_side_effect` fails only in full‑suite runs due to SQLAlchemy identity‑map collision, not related to Phase 4 changes. Proven identical on baseline and after Phase 4D.

---

## 12. Known Operational Issue: CLI DeepSeek Initialization Order

`app/llm.py` reads `os.getenv("DEEPSEEK_API_KEY", "")` at **module‑import time**. The
normal `uvicorn` path calls `load_dotenv()` in `app/main.py` before any app imports,
so the web app always initialises correctly. Any CLI script that imports `app.llm`
directly must call `load_dotenv()` first.

**Affects:** Legacy record reassessment tools or any CLI invocation of the LLM layer.
**Fix:** Called `load_dotenv(dotenv_path="...")` before importing `app.llm` in CLI scripts.
**Does not affect** the running web application.

---

## 13. Pending User-Facing State Requiring Consideration

Based on the accepted Phase 4D correction report:

| Item | Status | Impact |
|:-----|:-------|:-------|
| 9 `pending` additional‑error candidates | ⏳ Awaiting user processing | These are genuine errors from attempts 60‑62; user must review at next cycle |
| Weak point 〜て以来 (`wp_id=2`) | ✅ `is_active=true`, `error_count=3` | Remaining 3 hits from earlier legitimate errors keep it active |
| Events 1/2/3 (voided) | ✅ Excluded from progress counts | Audit trail intact, won't affect statistics |
| Mastered grammar states | ⏳ Set per‑user during study | Part of normal operation |

The 9 pending candidates are the most actionable live state. Any next plan should
consider whether to process them first or add new features on top of the current flow.

---

## 14. Candidate Directions for LLM C to Plan

The following are candidate directions **for LLM C to choose from and produce a plan**.
This report does not select a direction.

### Candidate A: Live‑Flow Acceptance and Pending‑Candidate Processing

- Manually walk through a complete cycle on the real app, processing the 9 pending candidates
- Validate that the full flow works end‑to‑end (upload → translations → review gate → choices → final score → progress page)
- No code changes; pure QA and acceptance

| Aspect | Details |
|--------|:--------|
| **Motivation** | Confirms the existing flow works in production before adding new features |
| **Risk** | Low; no code changes |
| **Dependencies** | Running app instance with DeepSeek API key |
| **Evidence needed** | Manual test log, screenshots, or automated acceptance test |

### Candidate B: Phase 2.1 Test‑Isolation Debt Repair

- Fix the flaky `test_deleted_unused_mistaken_material_does_not_leave_mastery_side_effect` test
- Root cause: SQLAlchemy identity‑map collision in combined runs due to shared auto‑increment IDs
- Likely fix: unique grammar‑point names per test, or explicit session cleanup before the flaky test

| Aspect | Details |
|--------|:--------|
| **Motivation** | Cleaner CI/CD; eliminates one known false‑positive failure |
| **Risk** | Very low; tests use isolated temp DBs |
| **Dependencies** | None |
| **Evidence needed** | Test passes in combined run after fix |

### Candidate C: CLI/Diagnostic LLM Robustness

- Add `load_dotenv()` call before `import app.llm` in any CLI‑facing entry point
- Or make `app.llm` lazily load configuration instead of at module‑import time
- Enables reliable CLI reassessment tools without the import‑order pitfall

| Aspect | Details |
|--------|:--------|
| **Motivation** | Prevents future debugging frustration; enables tool reuse |
| **Risk** | Low; well‑understood fix |
| **Dependencies** | None |
| **Evidence needed** | `is_available()` returns true from any import path |

### Candidate D: Next Product Enhancement (after live‑flow acceptance)

- After Candidate A confirms the flow works, add a new product feature
- Possible ideas (not commitments): listening practice, SRS/forgetting curve, weak‑point demotion, stronger MC validation, grammar‑explanation pre‑generation
- Should be planned as a minimal, testable, self‑contained phase

| Aspect | Details |
|--------|:--------|
| **Motivation** | Real user value |
| **Risk** | Medium; depends on Candidate A passing first |
| **Dependencies** | Candidate A completed |
| **Evidence needed** | Depends on chosen feature |

---

## 15. Hard Constraints for Any Next Plan

1. **Do not alter** accepted real learning records without explicit user authorization and a new byte‑for‑byte backup
2. **Do not commit** `data/lingua.db` or `data/backups/**`
3. **Do not regress** Phase 4D target‑grammar‑aware scoring contract (the validity matrix is non‑negotiable)
4. **Do not bypass** the mandatory candidate review gate before multiple‑choice questions
5. **Do not fabricate** historical scoring precision or weak‑point counts (NULL hearts = historical, not re‑evaluated)
6. **Do not claim** inherited failing tests as newly green without equivalent baseline‑comparison proof
7. Any implementation plan should be **minimal, phased, testable**, and maintain separate **product‑feature** vs **technical‑debt** scopes
8. All new tests must use **isolated temporary databases**, never `data/lingua.db`

---

## 16. Evidence Sources Reviewed

This planning input was produced by reading the following committed documents:

- `README.md` (updated with Phase 4 state)
- `docs/reports/phase4-0-new-requirements-investigation-report.md`
- `docs/reports/phase4a-translation-heart-scoring-implementation-report.md`
- `docs/reports/phase4bc-progress-and-material-navigation-implementation-report.md`
- `docs/reports/phase4d-target-grammar-aware-scoring-implementation-report.md`
- `docs/reports/phase4d-legacy-real-translation-reassessment-proposals.md`
- `docs/reports/phase4d-deepseek-reassessment-availability-diagnosis.md`
- `docs/reports/phase4d-authorized-real-record-correction-report.md`
- Git log: `4e9e5ea`, `48f708f`, `a68cd4b`, `497d092`
