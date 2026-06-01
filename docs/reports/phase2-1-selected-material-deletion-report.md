# Phase 2.1 Report — Safe Selected-Material Deletion

## Verdict

**`PHASE2_1_SELECTED_MATERIAL_DELETION_COMPLETED`**

## Executive Summary

Added the ability to select and remove mistakenly uploaded materials. Unused materials are hard-deleted; materials with learning history are soft-deleted (archived) to preserve historical provenance, scores, weak points, and mastered semantics.

## Baseline

- **HEAD**: `a156ead` (Phase 2 completion), clean working tree
- **Phase 2 tests**: 15/15 ✅ pass before changes

## Material Relationship Investigation

| Relationship | References Material? | Historical Importance | Safe on Hard Delete? |
|---|---|---|---|
| `grammar_points.material_id` | Direct | High (provenance) | Only if unused |
| `vocab_items.material_id` | Direct | Low (display) | Only if unused |
| `cycle_materials.material_id` | Direct | High (cycle provenance) | No — forces archive |
| `study_cycles.grammar_a/b_id` | Indirect via GP | High (cycle data) | Protected by grammar FK |
| `question_attempts` | Indirect via cycle | High (scores) | Protected |
| `weak_points` | None (by name string) | Medium | Unaffected |
| `session_state` | Indirect via cycle | Active state | Protected |

## Deletion vs Archival Product Rule

| Status | Condition | Action |
|---|---|---|
| **Unused material** | No `cycle_materials` rows | Hard delete + delete grammar/vocab children |
| **Used material** | Has any `cycle_materials` row | Set `archived_at` timestamp, preserve all data |

## Schema Change

- **Table**: `materials`
- **Added**: `archived_at DATETIME nullable` (null = active)
- **Migration**: Idempotent via `_add_column_if_missing` in `init_db()`

## Route

- `POST /materials/delete_selected` — accepts `material_ids` (repeated form values)
- Processes each active material: hard delete if unused, archive if used
- Returns success message with counts

## UI

- Purple "使用所选素材开始学习" + red "删除所选素材" buttons
- Browser `confirm()` dialog explaining hard-delete vs archive behavior
- Checkboxes belong to a single `action-form` with `formaction` routing

## Archived Material Behavior

| Situation | Behavior |
|---|---|
| Active materials list | Archived hidden from list |
| Single-material start | Rejected with clear message |
| Multi-material start | Rejected with clear message |
| Existing historical cycles | Unaffected; render normally |
| Active unfinished cycles | Unaffected; resumeable |
| Global mastered set | Mastered from archived materials still excluded from new cycles |

## Mastered Semantics After Deletion/Archive

- **Hard-deleted unused material**: Mastery flags deleted with the material. The source was mistaken — no knowledge signal to preserve.
- **Archived used material**: Mastery flags preserved. `_build_combined_grammar_pool` queries `mastered=True` across ALL materials (including archived), so global mastered exclusion remains correct.

## Files Modified

| File | Change |
|---|---|
| `app/models.py` | Added `archived_at` to `Material` |
| `app/db.py` | Added migration for `archived_at` |
| `app/routes/upload.py` | Added `delete_selected` route; filter archived from materials list |
| `app/routes/study.py` | Reject archived materials in `start_cycle` |
| `app/templates/materials.html` | Delete button, confirmation, unified action form |
| `tests/test_phase2_1.py` | NEW — 11 tests |
| `README.md` | Updated (both CN/EN) |

## Test Results

**11/11 ✅ ALL PASS**

## Real DB

Schema upgraded (archived_at added). No data mutated. Zero row count changes.

## PDF Vision

Unchanged — 10-page limit preserved. No PDF API calls during Phase 2.1.

## Commit

```
f2efc14  feat: add safe selected-material deletion
```
