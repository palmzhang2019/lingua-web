# Phase 4D Legacy Real-Translation Reassessment Proposals

**Status: `REASSESSMENT_PENDING_MODEL_AVAILABILITY`**

## Safety Declaration

- Real database was opened **read-only** (read-only URI mode).
- **No records were modified** — no score_hearts, is_correct, target_grammar_correct,
  weak_points, WeakPointEvents, or translation_error_candidates were altered.
- These proposals are informational only. Corrections require explicit user approval.
- DeepSeek LLM was unavailable during proposal generation. Each record is marked
  `requires_manual_review` pending a successful new-rule LLM re-evaluation or
  a separately authorized manual user decision.

---

## Reassessment Results

**Total attempts identified for reassessment: 3**

### Attempt 60 — 〜て以来 (score_hearts=5)

| Field | Current Value |
|-------|---------------|
| **Cycle ID** | 4 |
| **User answer** | その旅行して以来、撮影を深い興味があります... |
| **Reference** | その旅行をして以来、写真に深い興味があります。 |
| **Old score_hearts** | 5 |
| **Old is_correct/pass** | False |
| **Existing weak-point event** | id=1, type=hit_existing (translation_low_score_target_grammar) |
| **Existing weak point** | 〜て以来, count=6, active=True |
| **New proposed target_grammar_correct** | `PENDING — requires_manual_review` |
| **New proposed score_hearts** | `PENDING — requires_manual_review` |
| **New proposed pass/fail** | `PENDING — requires_manual_review` |
| **Recommended weak-point action** | `requires_manual_review` |
| **Recommended WP-Event action** | `requires_manual_review` |
| **Candidate impact** | Unknown without new evaluation |

**Explanation:** DeepSeek LLM was not available during this audit. The answer appears to use
「〜て以来」correctly syntactically, but contains particle errors (「旅行して以来」vs
「旅行をして以来」) and vocabulary issues. A new-rule evaluation is needed to determine
target_grammar_correct and the appropriate heart score.

---

### Attempt 61 — 〜て以来 (score_hearts=5)

| Field | Current Value |
|-------|---------------|
| **Cycle ID** | 4 |
| **User answer** | 前回の日本の旅行して以来、ずっと日本の文化を興味が持ちます... |
| **Reference** | 前回日本に旅行して以来、ずっと日本の文化に興味を持っています。 |
| **Old score_hearts** | 5 |
| **Old is_correct/pass** | False |
| **Existing weak-point event** | id=2, type=hit_existing |
| **Existing weak point** | 〜て以来, count=6, active=True |
| **New proposed target_grammar_correct** | `PENDING — requires_manual_review` |
| **New proposed score_hearts** | `PENDING — requires_manual_review` |
| **Recommended weak-point action** | `requires_manual_review` |

**Explanation:** DeepSeek not available. Same grammar target with different sentence errors.

---

### Attempt 62 — 〜て以来 (score_hearts=4)

| Field | Current Value |
|-------|---------------|
| **Cycle ID** | 4 |
| **User answer** | 前回の日本の旅行して以来、ずっと日本の文化を興味が持ちます... |
| **Reference** | 前回日本に旅行して以来、ずっと日本の文化に興味を持っています。 |
| **Old score_hearts** | 4 |
| **Old is_correct/pass** | False |
| **Existing weak-point event** | id=3, type=hit_existing |
| **Existing weak point** | 〜て以来, count=6, active=True |
| **New proposed target_grammar_correct** | `PENDING — requires_manual_review` |
| **New proposed score_hearts** | `PENDING — requires_manual_review` |
| **Recommended weak-point action** | `requires_manual_review` |

**Explanation:** DeepSeek not available. Same user answer as attempt 61 (duplicate submission).

---

## Instructions

No automatic correction has been executed. To proceed:

1. **Option A:** Wait for DeepSeek to be available, then re-run the reassessment script.
2. **Option B:** Manually review each answer and decide target_grammar_correct.
3. **Option C:** Accept the existing old-rule scores as-is (no correction).

Do not execute any correction until per-attempt authorization is received.**Total attempts reassessed:** 3

### Attempt 60 — 〜て以来

| Field | Value |
|-------|-------|
| **Cycle ID** | 4 |
| **Target Grammar** | 〜て以来 |
| **Old score_hearts** | 5 |
| **Old is_correct/pass** | False |
| **New proposed target_grammar_correct** | MANUAL_REVIEW_REQUIRED |
| **New proposed score_hearts** | MANUAL_REVIEW_REQUIRED |
| **New proposed pass/fail** | MANUAL_REVIEW_REQUIRED |
| **Recommended weak-point action** | requires_manual_review |
| **Recommended WP-Event action** | requires_manual_review |
| **Candidate impact** | Cannot assess |

**Explanation:** LLM evaluation unavailable for this attempt.

---

### Attempt 61 — 〜て以来

| Field | Value |
|-------|-------|
| **Cycle ID** | 4 |
| **Target Grammar** | 〜て以来 |
| **Old score_hearts** | 5 |
| **Old is_correct/pass** | False |
| **New proposed target_grammar_correct** | MANUAL_REVIEW_REQUIRED |
| **New proposed score_hearts** | MANUAL_REVIEW_REQUIRED |
| **New proposed pass/fail** | MANUAL_REVIEW_REQUIRED |
| **Recommended weak-point action** | requires_manual_review |
| **Recommended WP-Event action** | requires_manual_review |
| **Candidate impact** | Cannot assess |

**Explanation:** LLM evaluation unavailable for this attempt.

---

### Attempt 62 — 〜て以来

| Field | Value |
|-------|-------|
| **Cycle ID** | 4 |
| **Target Grammar** | 〜て以来 |
| **Old score_hearts** | 4 |
| **Old is_correct/pass** | False |
| **New proposed target_grammar_correct** | MANUAL_REVIEW_REQUIRED |
| **New proposed score_hearts** | MANUAL_REVIEW_REQUIRED |
| **New proposed pass/fail** | MANUAL_REVIEW_REQUIRED |
| **Recommended weak-point action** | requires_manual_review |
| **Recommended WP-Event action** | requires_manual_review |
| **Candidate impact** | Cannot assess |

**Explanation:** LLM evaluation unavailable for this attempt.

---

## Instructions

For each attempt above, decide one of:
- **Accept proposed correction** — authorize Hermes to update the real records
- **Reject proposed correction** — keep historical values as-is
- **Request manual review** — user wants to personally review the original answer

Do not execute any correction until per-attempt approval is received.
