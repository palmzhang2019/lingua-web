# Phase 4D-0 Investigation — Translation Scoring Rule Revision and Real Learning Record Audit

## 1. Actual Baseline and Clean-Tree Check

| Check | Value |
|-------|-------|
| **Git HEAD** | `48f708f14450a8e828d7d06a38f215b6762991ec` ✅ 符合预期 |
| **Working tree** | 完全干净 — 无已修改/未跟踪文件 |
| **DB md5 before audit** | `94f68d1a4b8df095e91eafd141adeecb` |
| **DB md5 after audit** | `94f68d1a4b8df095e91eafd141adeecb` ✅ 未变更 |

---

## 2. New User-Locked Scoring Rules

| ID | 规则 |
|----|------|
| P4D-LOCK-001 | 满分仍为 10 颗心 |
| P4D-LOCK-002 | **目标语法使用正确时，最少给 6 颗心**（即使有其他错误） |
| P4D-LOCK-003 | **目标语法使用错误时，最多给 5 颗心**（即使其他部分良好） |
| P4D-LOCK-004 | **score_hearts >= 6 视为通过** |
| P4D-LOCK-005 | **score_hearts <= 5 视为未通过** |
| P4D-LOCK-006 | **仅 score_hearts <= 5 自动创建/更新目标语法薄弱点** |
| P4D-LOCK-007 | 非目标语法的额外错误继续作为候选，通过已有审核流程处理 |

### 设计含义

由于新规则锚定了"目标语法正确 ↔ ≥6 心，目标语法错误 ↔ ≤5 心"，**未来记录可以从 `score_hearts` 直接推导目标语法是否正确**，无需额外的布尔字段。但为了审计可追溯性，**建议保留显式的评分理由**（`reason_zh` 字段已存在于 `TranslationEvaluationV2` 中），若需机器可读的证明，可考虑新增 `target_grammar_correct: bool`。

---

## 3. Current Implemented Scoring Rule and Exact Code Paths

### 3.1 LLM Grading Prompt

**文件：** `app/agents/generator.py:87-112`

当前提示：
```
score_hearts: A score from 0 to 10. 8+ means the answer is semantically acceptable.
   - 10: Perfect. Natural, correct, uses target grammar flawlessly.
   - 8-9: Acceptable. Conveys meaning, target grammar used correctly.
   - 5-7: Partially correct but has meaningful errors.
   - 1-4: Significant errors.
   - 0: Completely wrong or unrelated.
```

**需要修改：** 提示必须强制要求：
- 目标语法正确 → 最低 6 分
- 目标语法错误 → 最高 5 分

### 3.2 Python Pass/Fail Threshold

**文件：** `app/routes/study.py:1024`

```python
is_correct = score_hearts >= 8
```

**需要修改为：**
```python
is_correct = score_hearts >= 6
```

### 3.3 Schema Field Description

**文件：** `app/schemas.py:111`

```python
score_hearts: int = Field(description="Score from 0 to 10, where 8+ means acceptable")
```

**需要修改为：** `6+ means acceptable`

### 3.4 Display Wording

**文件：** `app/routes/study.py:1049`

```python
result_text = "正确！" if is_correct else "不正确"
```
is_correct 由阈值推导，阈值修改后自动更新显示。

### 3.5 Final Cycle Score Formula

**文件：** `app/routes/study.py:1764-1766`

```python
scored.append(q.score_hearts / 10.0 * 100)
```

**不受影响。** 最终得分公式将每个翻译题的 heart 百分比（0-100%）纳入等权平均。例如 6 心贡献 60%，5 心贡献 50%。这与新规则兼容，无需修改。

---

## 4. Current Auto Weak-Point Threshold and Exact Code Paths

### 4.1 Weak-Point Creation Condition

**文件：** `app/routes/study.py:1034-1037`

```python
# Phase 4A: auto weak point for target grammar if score_hearts <= 7
if grammar_point_name and score_hearts <= 7:
    _record_weak_point(...)
```

**需要修改为：** `score_hearts <= 5`

### 4.2 WeakPointEvent Source Recording

**文件：** `app/routes/study.py:1037-1042`

WeakPointEvent 的 `source_type="translation_low_score_target_grammar"` 在调用 `_record_weak_point` 时传递。阈值修改后，WeakPointEvent 将自动仅在 `<=5` 时创建。

### 4.3 MC Wrong-Answer Weak-Point Path

**文件：** `app/routes/study.py:1089-1094`

```python
if module_type == "multiple_choice":
    grammar_point_name = payload.get("grammar_point", "")
    if grammar_point_name and not is_correct:
        _record_weak_point(...)
```

**不受影响。** MC 的薄弱点路径独立于翻译阈值。

---

## 5. Real Database Read-Only Safety Evidence

| Check | Result |
|-------|--------|
| **打开方式** | `file:data/lingua.db?mode=ro` (只读 URI) |
| **md5 审计前** | `94f68d1a4b8df095e91eafd141adeecb` |
| **md5 审计后** | `94f68d1a4b8df095e91eafd141adeecb` |
| **行变更** | 0 — 未插入/更新/删除任何行 |
| **结论** | ✅ 只读审计安全完成 |

---

## 6. Real Translation Attempt Summary

| 范围 | 统计 |
|------|------|
| 总回答翻译题（有 score_hearts） | **3 题**（均来自 cycle 4，用户真实练习） |
| score_hearts 分布 | 4 (1 题), 5 (2 题) |
| 6 或 7 心记录 | **0 题** |
| WeakPointEvent (translation_low_score) | 3 条（均为 `hit_existing`） |
| 薄弱点 | 2 条：`〜ている` (count=1, inactive), `〜て以来` (count=6, active) |
| 候选记录 | 9 条（均 `pending`） |

---

## 7. Focused Audit of Existing 6-Heart and 7-Heart Attempts

**结论：不存在 6 或 7 心的翻译记录。**

用户的真实练习产生了 3 个已评分的翻译回答（score=4 或 5），全部 <=5。这些记录在新旧规则下都会触发目标语法薄弱点自动创建。**因此，无需对历史记录进行任何修正。**

---

## 8. Whether Existing Stored Data Can Prove Target-Grammar Correctness

| 问题 | 答案 |
|------|------|
| 数据库是否存储了"目标语法是否正确"的布尔值？ | ❌ **否** — 无此类字段 |
| 能否从 `score_hearts` 推导？ | 旧规则下：≥8 隐含语法正确，≤7 无法断定。新规则下：≥6 隐含语法正确，≤5 隐含语法错误 |
| 是否存储了评分理由（`reason_zh`）？ | DeepSeek 返回了此字段（`TranslationEvaluationV2`），但 **数据库未存储** `reason_zh`、`feedback_zh` 或 `corrected_answer_ja` |
| 能否不重调用 LLM 就证明目标语法正确性？ | ❌ 不能 — 旧记录没有保留足够信息 |

**设计建议：** 可考虑新增 `target_grammar_correct: bool` 字段（或等效 JSON），以便未来审计可追溯。也可依赖新规则的评分约束（≥6 = 语法正确），但后者假设提示被严格遵守。

---

## 9. Potential Real-Record Correction Decision

**结论：无需修正历史记录。**（注意：此结论仅限于 6/7 心记录的存在性检查。
完整的修正决定需在 Phase 4D 重评估后确定。）

| 条件检查 | 结果 |
|----------|------|
| 存在 6/7 心翻译题？ | ❌ 不存在 |
| 旧规则分数（4/5 心）能证明目标语法错误？ | ❌ 不能—旧规则评分提示未以目标语法正确性为锚点 |
| 需要自动撤销薄弱点？ | ❌ 不自动执行 |
| 需要用户判断的历史记录？ | ✅ 3 条记录需要重评估（attempts 60/61/62） |
| 重评估方式 | 使用新评分规则重新评估后，逐条提交用户审批 |

**重要更正：** Phase 4D-0 调查报告原先声称"因为没有 6/7 心记录所以无需修正历史记录"，
这一结论低估了旧规则 4/5 分记录可能也存在误评的情况。旧规则的评分提示将 5-7 分描述为
"部分正确但有重大错误"，并未将阈值锚定到目标语法正确性。因此 4 分或 5 分的记录
**不能证明目标语法错误**，需要单独重评估。

具体重评估提案见 `docs/reports/phase4d-legacy-real-translation-reassessment-proposals.md`。

---

## 10. Future Implementation Impact Map

### 10.1 需要修改的文件

| 文件 | 修改内容 |
|------|----------|
| `app/agents/generator.py:90-95` | 更新 `EVALUATION_V2_SYSTEM_PROMPT`：6+ pass, 5- fail，目标语法正确/错误约束 |
| `app/schemas.py:111` | 更新 `score_hearts` 字段描述 "6+ means acceptable" |
| `app/routes/study.py:1024` | `is_correct = score_hearts >= 8` → `>= 6` |
| `app/routes/study.py:1036` | `score_hearts <= 7` → `<= 5` |
| `app/routes/study.py:1049-1057` | `result_text` 等显示取决于 `is_correct`，阈值修改后自动更新 |

### 10.2 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `app/routes/study.py:1764-1766`（最终得分公式） | 心脏百分比作为数值贡献不受阈值影响 |
| `app/templates/progress.html` | 显示的是 final_score% 和薄弱点计数，不直接引用 pass/fail 阈值 |
| `app/templates/study_result.html` | 显示 heart 数量，不编码 pass/fail |
| `app/routes/study.py:1089-1094`（MC 薄弱点） | 独立路径 |
| WeakPointEvent (study.py:1037-1042) | 阈值修改后自动跟随 |
| Review gate (study.py:1652-1667) | 独立于评分阈值 |
| Candidate review (study.py:1694-1726) | 独立于评分阈值 |

### 10.3 需要修改的测试

| 测试文件 | 测试名称 | 需修改原因 |
|----------|----------|------------|
| `test_phase4a.py` | `test_8_hearts_is_passed` | 阈值变为 ≥6，测试名和断言需更新 |
| `test_phase4a.py` | `test_7_hearts_is_not_passed` | 7 心现在应通过（≥6），需修改 |
| `test_phase4a.py` | 所有引用 `mock_deepseek_low_score`（score=4） 的测试 | 4 ≤ 5 仍应失败/触发薄弱点 **不变** |
| `test_phase4a.py` | `test_low_score_auto_creates_target_weak_point` | 4 ≤ 5 仍触发 **不变** |
| `test_phase4a.py` | `test_low_score_updates_existing_weak_point` | 同上 **不变** |
| `test_phase4a.py` | `test_high_score_does_not_create_target_weak_point` | 10 ≥ 6 仍不触发 **不变** |
| `test_phase4a.py` | `MockEvalV2` 默认 `score_hearts=8` | 8 ≥ 6 仍通过 **不变** |
| `test_phase4a.py` | `mock_deepseek_edge_score`（8 和 7 交替） | 7 现在通过，需调整 |
| `test_phase4c.py` | `MockEvalV2` 默认 `score_hearts=8` | 不变 |
| `test_weak_point_provenance.py` | `mock_low_score`（score=4） | 4 ≤ 5 仍触发 **不变** |
| `test_weak_point_provenance.py` | `mock_low_with_errors`（score=7） | 7 ≥ 6 不再触发薄弱点，需调整 |

---

## 11. Required Future Tests

| 测试描述 | 断言 |
|----------|------|
| 目标语法正确且无其他错误 | 可通过高分 |
| 目标语法正确但有额外错误 | **至少 6 心**，不自动创建目标语法薄弱点 |
| 目标语法错误但句子自然 | **最多 5 心**，自动创建目标语法薄弱点 |
| 6 心视为通过（is_correct=True） | 6 ≥ 6 → True |
| 5 心视为未通过（is_correct=False） | 5 ≤ 5 → False |
| 6 心贡献 60% 至最终成绩 | `score_hearts / 10.0 * 100` |
| 5 心贡献 50% 至最终成绩 | 同上 |
| 额外错误在 6 心通过时仍创建候选 | 候选插入逻辑独立于阈值 |
| 审核关卡不变 | 复习关卡独立 |
| WeakPointEvent 在新阈值下仅记录 ≤5 | 事件仅当 `score_hearts <= 5` |
| 旧 score_hearts NULL 记录保持旧版 | 不修改 |
| 测试不修改 data/lingua.db | 使用临时隔离数据库 |

---

## 12. Risks, Unknowns, and Recommendation

### 风险

| 风险 | 等级 | 缓解 |
|------|:----:|------|
| LLM 提示更新后可能不一致地应用"目标语法正确↔≥6心"约束 | **中** | 在提示中明确列出边界条件；通过测试验证 |
| 旧记录（无 `target_grammar_correct`）不可证明目标语法正确性 | **低** | 无 6/7 心历史记录，无需修正 |
| "6 心通过" 可能让用户感觉太宽松 | **低** | 用户主动提出此规则，属故意设计 |

### 未知

- LLM 是否严格遵守新的评分约束需要实际测试
- `target_grammar_correct` 是否值得新增持久化字段

### 推荐实施顺序

1. 更新 `app/agents/generator.py` 中的评分提示
2. 更新 `app/schemas.py` 中的字段描述
3. 更新 `app/routes/study.py` 中的阈值
4. 更新测试（名称、断言、mock 配置）
5. 运行完整测试套件验证
6. 提名：是否新增 `target_grammar_correct: bool` 字段到 `question_attempts`（建议添加，以便审计）

---

## 13. Git Safety After Investigation

| Check | Status |
|-------|--------|
| git status | ✅ 干净 — 无修改或未跟踪文件 |
| 唯一创建的文件 | `docs/reports/phase4d-0-translation-scoring-rule-and-real-record-investigation-report.md` |
| 禁用产物 | ✅ 未创建/修改/暂存数据文件、临时 DB 或上传资料 |
| 未执行 git add/commit | ✅ |
