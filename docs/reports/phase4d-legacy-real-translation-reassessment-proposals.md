# Phase 4D: Legacy Real Translation Reassessment Proposals

```
status: REASSESSMENT_PROPOSALS_COMPLETED_WITH_NEW_RULE_OUTPUT
generated_at: 2026-06-01T14:45+08:00
model: deepseek-v4-flash
reassessment_contract: Phase 4D target-grammar-aware scoring
```

## DeepSeek 可用性诊断结果

| 检查 | 结果 |
|:----|:----:|
| 初始化顺序 | ✅ `load_dotenv()` → `import app.llm` → `is_available()` |
| `DEEPSEEK_API_KEY` 存在 | ✅ 是 |
| `DEEPSEEK_BASE_URL` 存在 | ✅ 是 |
| `DEEPSEEK_MODEL` 存在 | ✅ 是 |
| `is_available()` 返回值 | ✅ **True** |
| 3 次调用均成功返回 | ✅ 全部通过 Pydantic 验证 |

## 真实数据库安全

| 检查 | 结果 |
|:----|:----:|
| 读取模式 | ✅ `mode=ro` (只读) |
| 预检查校验和 | ✅ `b9faab8ce5e032fa3e02939f33293f83` |
| 后检查校验和 | ✅ `b9faab8ce5e032fa3e02939f33293f83` |
| 变更执行 | ❌ 未执行任何变更 |
| `target_grammar_correct` 修改 | ❌ 未修改（保持 NULL） |

---

## 评估概述

全部 3 条旧记录（attempts 60, 61, 62）的**目标语法「〜て以来」在 Phase 4D 规则下均被判定为正确使用**。

| Attempt | 旧分数 | 旧判定 | 新 tgc | 新分数 | 新判定 | 旧 WP 事件 |
|:-------:|:------:|:------:|:------:|:------:|:------:|:----------:|
| **60** | 5 | ❌ 失败 | ✅ **True** | **6** | ✅ **通过** | `hit_existing` (ID=1) |
| **61** | 5 | ❌ 失败 | ✅ **True** | **7** | ✅ **通过** | `hit_existing` (ID=2) |
| **62** | 4 | ❌ 失败 | ✅ **True** | **6** | ✅ **通过** | `hit_existing` (ID=3) |

### 关键发现

所有 3 次作答中，用户都正确使用了 `〜て以来` 的 て形结构（`旅行して以来`）。旧规则（Phase 4A）将分数压低至 ≤7 是因为用户作答中**其他非目标语法错误**（助词误用、词汇选择、动词时态），而非目标语法本身使用错误。在 Phase 4D 规则下：

- `target_grammar_correct = true`（目标语法正确）
- `score_hearts` 在 6-7 之间（因其他错误扣分）
- 原本因 `score_hearts ≤ 7` 自动插入的目标语法薄弱点事件现在是**错误的**——目标语法本身不应该被标记为弱点

---

## 逐条提案

### Proposal A — Attempt 60

| 字段 | 值 |
|:----|:----|
| **attempt_id** | 60 |
| **cycle_id** | 4 |
| **target_grammar** | 〜て以来 |
| **user_answer_fragment** | その旅行して以来、撮影を深い興味があります |
| **correct_answer_fragment** | その旅行をして以来、写真に深い興味があります。 |
| **old_score_hearts** | 5 |
| **old_is_correct** | false |
| **old_target_grammar_correct** | NULL |
| **old_weak_point_event** | ✅ 存在 (id=1, `translation_low_score_target_grammar`, `hit_existing`) |
| **new_proposed_target_grammar_correct** | ✅ **true** |
| **new_proposed_score_hearts** | **6** |
| **new_proposed_pass_or_fail** | ✅ **PASS** |
| **new_detected_additional_errors** | 3 项 |
| | 1. `particle`: 旅行して → 旅行**を**して（缺少宾格助词「を」） |
| | 2. `vocabulary`: 撮影 → **写真**（用词不当） |
| | 3. `particle`: 撮影**を**深い興味 → 写真**に**深い興味（兴趣对象应用「に」） |
| **proposed WP action** | `propose_reverse_or_compensate` |
| **proposed WPE action** | `propose_void_or_compensate` |
| **理由** | 「〜て以来」的 て形使用正确，其他错误不应触发目标语法薄弱点。建议撤销或补偿此次的 WP 事件。 |

### Proposal B — Attempt 61

| 字段 | 值 |
|:----|:----|
| **attempt_id** | 61 |
| **cycle_id** | 4 |
| **target_grammar** | 〜て以来 |
| **user_answer_fragment** | 前回の日本の旅行して以来、ずっと日本の文化を興味が持ちます |
| **correct_answer_fragment** | 前回日本に旅行して以来、ずっと日本の文化に興味を持っています。 |
| **old_score_hearts** | 5 |
| **old_is_correct** | false |
| **old_target_grammar_correct** | NULL |
| **old_weak_point_event** | ✅ 存在 (id=2, `translation_low_score_target_grammar`, `hit_existing`) |
| **new_proposed_target_grammar_correct** | ✅ **true** |
| **new_proposed_score_hearts** | **7** |
| **new_proposed_pass_or_fail** | ✅ **PASS** |
| **new_detected_additional_errors** | 2 项 |
| | 1. `particle`: 前回**の**日本の旅行→前回日本**に**旅行（多余的「の」+ 缺少「に」） |
| | 2. `grammar`: 文化**を**興味**が**持ちます→文化**に**興味を**持っています（助词+持续体）|
| **proposed WP action** | `propose_reverse_or_compensate` |
| **proposed WPE action** | `propose_void_or_compensate` |
| **理由** | 「旅行して以来」结构正确。其他错误（助词、时态）不应触发目标语法弱点。评分最高（7），说明目标语法以外的部分也相对较好。建议撤销 WP 事件。 |

### Proposal C — Attempt 62

| 字段 | 值 |
|:----|:----|
| **attempt_id** | 62 |
| **cycle_id** | 4 |
| **target_grammar** | 〜て以来 |
| **user_answer_fragment** | 前回の日本の旅行して以来、ずっと日本の文化を興味が持ちます |
| **correct_answer_fragment** | 前回日本に旅行して以来、ずっと日本の文化に興味を持っています。 |
| **old_score_hearts** | 4 |
| **old_is_correct** | false |
| **old_target_grammar_correct** | NULL |
| **old_weak_point_event** | ✅ 存在 (id=3, `translation_low_score_target_grammar`, `hit_existing`) |
| **new_proposed_target_grammar_correct** | ✅ **true** |
| **new_proposed_score_hearts** | **6** |
| **new_proposed_pass_or_fail** | ✅ **PASS** |
| **new_detected_additional_errors** | 2 项 |
| | 1. `particle`: 前回**の**日本の旅行→前回日本**に**旅行 |
| | 2. `particle`: 文化**を**興味→文化**に**興味**を**持っています |
| **proposed WP action** | `propose_reverse_or_compensate` |
| **proposed WPE action** | `propose_void_or_compensate` |
| **理由** | 与 Attempt 61 相同的答案，目标语法正确。建议撤销 WP 事件。 |

---

## 旧薄弱点状态

| WP ID | point_reference | 事件关联 |
|:-----:|:----------------|:---------|
| 2 | ~て以来 | 3 次 `hit_existing` events (1, 2, 3) |

当前 weak_point_id=2 的 `error_count` 和 `is_active` 状态需要用户决定是否修正。

## 实施选项供用户选择

### 选项 A：全部撤销
- 将 3 个 WP events 标记为 `event_type = 'voided'`
- 减少 weak_point_id=2 的 `error_count` 3 次（如果降至 0，可设为 `is_active=False`）
- 保持 `target_grammar_correct = NULL`（历史记录不变）

### 选项 B：仅撤销事件，保留 WP
- 将 3 个 WP events 标记为 `event_type = 'voided'`
- 但不修改 weak_point_id=2 的计数/状态
- 保持 `target_grammar_correct = NULL`

### 选项 C：不动
- 保持全部旧记录原样
- 仅在新评分中应用 Phase 4D 规则

### 选项 D：全部撤销 + 更新评分
- 撤销 WP events
- 更新 `score_hearts` 为 6/7/6
- 设置 `target_grammar_correct = true`
- 更新 `is_correct = true`
- **此选项风险最高**，因为会修改真实学习历史

---

## 状态汇总

| Attempt | 提案状态 |
|:-------:|:---------:|
| 60 | ✅ `PROPOSAL_READY_FOR_USER_REVIEW` |
| 61 | ✅ `PROPOSAL_READY_FOR_USER_REVIEW` |
| 62 | ✅ `PROPOSAL_READY_FOR_USER_REVIEW` |

未产生 `PROPOSAL_INVALID_REQUIRES_RETRY`。全部 3 条合法有效的提案。

---

## 执行状态 (2026-06-01T14:55)

| 项目 | 状态 |
|:----|:----:|
| 用户授权 | ✅ 已批准 |
| 修正已执行 | ✅ 是 |
| Attempts 60/61/62 | ✅ 已更正为 6/7/6, `target_grammar_correct=true`, `is_correct=true` |
| Events 1/2/3 | ✅ `event_type` → `voided`（保留审计历史，不计入统计） |
| weak_point_id=2 | ✅ `error_count` 6→3, `is_active` 保持 true |
| Candidates 1-9 | ✅ 保留 pending，未变更 |
| 备份 | `data/backups/lingua.pre-phase4d-record-correction-20260601-145538.db` |

详细执行报告：`docs/reports/phase4d-authorized-real-record-correction-report.md`
