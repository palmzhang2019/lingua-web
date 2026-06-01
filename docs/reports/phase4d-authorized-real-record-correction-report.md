# Phase 4D: Authorized Real-Record Correction Report

```
report_status: REAL_RECORD_CORRECTION_EXECUTED_WITH_USER_AUTHORIZATION
executed_at: 2026-06-01T14:55+08:00
correction_basis: docs/reports/phase4d-legacy-real-translation-reassessment-proposals.md
```

## User Authorization Summary

- **Approved correction basis:** DeepSeek Phase 4D reassessment proved attempts 60/61/62 had correct target grammar usage
- **Authorized scope:** Correct attempts, void erroneous WeakPointEvents, restore weak_point_id=2 counter
- **Prohibited:** Deleting entire weak point, guessing pre-misjudgment is_active, modifying candidates

## Accepted Commit Baseline

| Field | Value |
|:------|:------|
| **HEAD** | `a68cd4be45598db9bb03a9b1ddcb27d63cd36704` |
| **Phase 4D feature** | target-grammar-aware translation scoring committed |
| **Real DB schema already accepted** | `score_hearts`, `target_grammar_correct`, `translation_error_candidates`, `weak_point_events` |

## Database Backup

| Field | Value |
|:------|:------|
| **Original checksum** | `b9faab8ce5e032fa3e02939f33293f83` |
| **Backup path** | `data/backups/lingua.pre-phase4d-record-correction-20260601-145538.db` |
| **Backup checksum** | `b9faab8ce5e032fa3e02939f33293f83` ✅ |
| **Backup ignored/staged** | Untracked (safe — `data/*.db` in `.gitignore`) |

## Pre-Correction Snapshot

### Question Attempts

| ID | Cycle | Grammar | Old Hearts | Old Correct | Old TGC |
|:--:|:-----:|:--------|:----------:|:-----------:|:-------:|
| 60 | 4 | 〜て以来 | **5** | 0 (false) | NULL |
| 61 | 4 | 〜て以来 | **5** | 0 (false) | NULL |
| 62 | 4 | 〜て以来 | **4** | 0 (false) | NULL |

### WeakPointEvents

| ID | WP ID | Source Type | Event Type | Attempt |
|:--:|:-----:|:------------|:----------:|:-------:|
| 1 | 2 | `translation_low_score_target_grammar` | `hit_existing` | 60 |
| 2 | 2 | `translation_low_score_target_grammar` | `hit_existing` | 61 |
| 3 | 2 | `translation_low_score_target_grammar` | `hit_existing` | 62 |

### weak_points (id=2)

| Field | Pre-Correction |
|:------|:--------------:|
| point_reference | 〜て以来 |
| error_count | **6** |
| is_active | **1** (true) |
| last_error_at | 2026-06-01 04:22:00.040599 |

## Event Invalidation Mechanism

**Mechanism used:** `event_type` was changed from `"hit_existing"` to `"voided"`.

**Why this is safe:**
- `event_type` is `VARCHAR(20) NOT NULL` with **no CHECK constraint** → `"voided"` is a valid value
- Progress summary (`_get_historical_cycle_summaries`) counts events by filtering: `event_type == "created"` and `event_type == "hit_existing"` (lines 1537-1538)
- `"voided"` events are **silently excluded** from both counts — correct display semantics
- No other code in the codebase reads or processes `event_type` values

**Recommended alternative if schema were enhanced in future:** A `voided_at` nullable datetime column or an `event_type` enum constraint would provide stronger schema-level guarantees.

## Weak Point Counter Restoration

| Field | Pre | Post | Delta | Logic |
|:------|:---:|:----:|:-----:|:------|
| `error_count` | **6** | **3** | **−3** | 3 known `hit_existing` events from old-rule false positives |
| `is_active` | **1** | **1** | **0** | Not provably different: 3 ≥ 2 threshold still met |

**Counter safety proof:**
- `_record_weak_point` increments `error_count` by exactly 1 per event
- Events 1/2/3 are the only WeakPointEvent records for wp_id=2
- Subtracting exactly 3 restores the counter to the pre-misjudgment level (3 legitimate pre-provenance hits)
- Error_count ≥ 2 threshold still met → `is_active` remains correct without guessing

## Executed SQL Updates (atomic transaction)

The following updates were executed inside a single SQLite transaction with
pre-condition validation before each write. On any failure, the transaction
was rolled back.

```sql
-- Attempt corrections
UPDATE question_attempts SET score_hearts=6, is_correct=1, target_grammar_correct=1 WHERE id=60;
UPDATE question_attempts SET score_hearts=7, is_correct=1, target_grammar_correct=1 WHERE id=61;
UPDATE question_attempts SET score_hearts=6, is_correct=1, target_grammar_correct=1 WHERE id=62;

-- Event invalidation
UPDATE weak_point_events SET event_type='voided' WHERE id=1;
UPDATE weak_point_events SET event_type='voided' WHERE id=2;
UPDATE weak_point_events SET event_type='voided' WHERE id=3;

-- Counter restoration
UPDATE weak_points SET error_count = error_count - 3 WHERE id=2 AND error_count >= 3;
```

## Post-Correction Values

### Attempts

| ID | Hearts | Correct | TGC | Status |
|:--:|:------:|:-------:|:---:|:------|
| 60 | **6** | **1** (true) | **1** (true) | ✅ PASS |
| 61 | **7** | **1** (true) | **1** (true) | ✅ PASS |
| 62 | **6** | **1** (true) | **1** (true) | ✅ PASS |

### Events

| ID | Old Type | New Type | Counting Impact |
|:--:|:---------|:---------|:----------------|
| 1 | `hit_existing` | **`voided`** | Excluded from `re_hit_count` |
| 2 | `hit_existing` | **`voided`** | Excluded from `re_hit_count` |
| 3 | `hit_existing` | **`voided`** | Excluded from `re_hit_count` |

### weak_point_id=2

| Field | Post-Correction |
|:------|:---------------:|
| error_count | **3** (6 − 3) |
| is_active | **1** (unchanged — ≥2 threshold still met) |

### Additional-Error Candidates Preserved

| Count | Status |
|:-----:|:-------|
| **9** | All `pending`, unchanged |

## Final Database Checksum

| Checksum | Value |
|:---------|:------|
| Pre-correction | `b9faab8ce5e032fa3e02939f33293f83` |
| Post-correction | **`f44e7346b0a06fd809eaaa05ee2e096d`** (expected change) |
| Backup | `b9faab8ce5e032fa3e02939f33293f83` (restorable) |

## Scope Confirmation

- ✅ **Unrelated real-record changes:** **none**
- ✅ **Application code changes:** **none**
- ✅ **Test changes:** **none**
- ✅ **Data/backups not staged**
- ✅ **No git add or commit executed**
- ✅ **Backup available for manual rollback**

## Rollback Instructions

To restore the pre-correction state manually:

```bash
cp data/backups/lingua.pre-phase4d-record-correction-20260601-145538.db data/lingua.db
```

Then restart the Lingua Web application to refresh any in-memory state.
