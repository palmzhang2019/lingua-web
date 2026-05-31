# Lingua Web — Day 3 Prototype Closure Report

**文件路径：** `/home/pompeo_z/workspace/lingua-web/docs/reports/day3-prototype-closure-report.md`

---

## 最终判定

**DAY3_COMPLETED_THREE_DAY_PROTOTYPE_CLOSED**

所有 Day 3 验收标准已通过。三个有效学习循环已完成实时验证。

---

## Executive Summary

Day 3 实现了 Lingua Web 原型的三天收尾工作：
1. **薄弱点追踪** — 基于错误答案自动记录语法薄弱点，错误 ≥ 2 次自动激活
2. **复习优先** — 新循环的复习选择题优先从活跃薄弱点选取
3. **会话恢复** — 未完成循环可通过 GET /study 精确恢复
4. **模块操作** — 跳过当前模块（无效完成）与标记已学过（有效完成）
5. **Token 用量追踪** — 完整记录所有 DeepSeek API 调用的 token 消耗
6. **成本测量** — 基于实际用量计算近似成本

---

## 基线提交与 Day 2 报告修正

| 项目 | 状态 |
|------|------|
| Git 基线提交 | ✅ `f5922b2` (Day 1+Day 2) + `dcfdb41` (typo 修正) |
| Day 2 报告 "17 次翻译" typo | ✅ 已修正为 "10 次翻译" |
| Day 2 内容保护 | ✅ 未修改 Day 1/Day 2 业务逻辑 |

---

## 实现范围与非范围

**实现范围：**
- [x] 薄弱点持久化（答错自动计数）
- [x] error_count ≥ 2 时激活薄弱点
- [x] 新循环复习题优先使用活跃薄弱点
- [x] `/weak_points` 页面
- [x] 中断会话恢复（GET /study + GET /study/current）
- [x] 跳过当前模块（POST /study/skip_module）
- [x] 标记已学过（POST /study/mark_studied）
- [x] 有效完成判定（is_valid_completion）
- [x] Token usage 追踪与成本测量
- [x] 三个完整有效循环的实时验证

**非范围（明确排除）：**
- 听力/音频练习
- PDF 上传和 OCR
- 间隔重复（SRS）
- 多用户认证
- 外部框架（LangGraph、CrewAI 等）

---

## 创建/修改的文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `app/models.py` | 修改 | QuestionAttempt 新增 `status` 字段；新增 `UsageLog` 表 |
| `app/llm.py` | 修改 | 新增用量累加器 `record_usage()`/`get_and_clear_usage()` |
| `app/routes/study.py` | 重写 | 新增薄弱点记录、恢复逻辑、跳过/学过模块、有效完成、复习优先 |
| `app/main.py` | 修改 | 新增 `GET /weak_points` 路由 |
| `app/templates/base.html` | 修改 | 导航栏添加"薄弱点"和"学习"链接 |
| `app/templates/weak_points.html` | 创建 | 薄弱点展示页面 |
| `app/templates/study.html` | 修改 | 添加「跳过当前模块」「标记已学过」按钮 |
| `app/templates/study_result.html` | 修改 | 显示有效完成/跳过标记状态 |
| `docs/reports/day3-prototype-closure-report.md` | 创建 | 本报告 |
| `README.md` | 修改 | 更新成本信息和 P2/P3 任务 |

---

## Schema 变更及理由

### QuestionAttempt.status
- **字段：** `status VARCHAR(20) NOT NULL DEFAULT 'pending'`
- **值：** `pending` | `answered` | `skipped` | `studied`
- **理由：** 跟踪每道题目的状态，支持跳过/学过操作，以及有效完成判定
- **迁移：** 已通过 SQLite ALTER TABLE 为现有数据设置状态（已回答的行设为 `answered`）

### UsageLog 表
- **字段：** `id`, `call_purpose`, `cycle_id`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `called_at`
- **理由：** 记录所有 LLM API 调用的 token 用量，用于成本分析
- **注意：** 不含 API key 或敏感请求内容

---

## 薄弱点合同与幂等性

### 合同
- `point_type` = `"grammar"`（仅语法薄弱点，Day 3 范围内）
- `point_reference` = 语法点名（字符串，如 `〜てはいられない`）
- 同一语法点的错误计数使用 `point_type + point_reference` 作为自然键
- 活跃判定：`error_count >= 2 → is_active = True`

### 幂等性
- 每道题只能提交一次答案（`answered_at` 守卫 + `status != 'pending'` 二次检查）
- 因此同一个 question_attempt 的弱项记录最多发生一次
- 页面刷新/重试不会导致重复计数

---

## 复习优先行为

### 机制
在 `start_cycle` 中：
1. 查询所有 `is_active=True` 的 `weak_points` 记录
2. 提取活跃薄弱点的语法点名
3. 将 `review_points` 数组按"活跃薄弱点先行，其余在后"排序
4. 将排序后的数组传给 `generate_multiple_choice`

### 验证结果
- Cycle 6 的复习选择题（5 题）中，第一题使用 `〜てみる`（已激活的薄弱点）
- 验证数据覆盖：`〜てみる` 在 Cycle 5 中 error_count 达到 2 并被激活，Cycle 6 中优先出现在复习题中

---

## 会话恢复行为

### 机制
- `GET /study` 检查 `session_state.current_cycle_id`
- 如果存在且 `completed_at IS NULL`，重定向到 `/study/current`
- `/study/current` 查找第一个 `status='pending'` 的题目
- 如果所有题目均已回答/跳过/学过，显示结果页

### 验证
- Cycle 9 中回答 2 题后中断（current_question_index=2）
- GET /study → 303 重定向到 /study/current
- current_question_index 保持在 2，未重置
- 已完成 Cycle（completed_at 不为空）不会被恢复

---

## 跳过与已学过模块行为

### 跳过模块 (POST /study/skip_module)
- 将当前模块所有 `status='pending'` 的题目设为 `status='skipped'`
- 设置 `answered_at` 但不设置 `user_answer` 或 `is_correct`
- 前进到下一个仍有待答题的模块
- **不产生薄弱点**

### 标记已学过 (POST /study/mark_studied)
- 将当前模块所有 `status='pending'` 的题目设为 `status='studied'`
- 设置 `answered_at` 但不设置 `user_answer` 或 `is_correct`
- 前进到下一个仍有待答题的模块
- **不产生薄弱点**

### 验证
- Cycle 7（跳过）：is_valid_completion=False，5 道 skipped 题目无 user_answer/is_correct ✅
- Cycle 8（标记学过）：is_valid_completion=True，5 道 studied 题目无 user_answer/is_correct ✅

---

## 有效完成规则

### 规则
一个循环在以下条件全部满足时视为**有效完成**：
- `grammar_a_translation` 模块所有题目不处于 pending 状态
- `grammar_b_translation` 模块所有题目不处于 pending 状态
- `multiple_choice` 模块所有题目不处于 pending 状态
- 没有任何模块存在 `status='skipped'` 的题目

### 验证
| Cycle | 结果 | 有效？ |
|-------|------|--------|
| 2 | 19 answered | ✅ 是 |
| 3 | 19 answered | ✅ 是 |
| 5 | 19 answered | ✅ 是 |
| 7 | 14 answered + 5 skipped | ❌ 否 |
| 8 | 14 answered + 5 studied | ✅ 是 |

---

## 路由与 UI

| 路由 | 方法 | 用途 | 状态 |
|------|------|------|------|
| `GET /study` | GET | 学习首页/恢复入口 | ✅ |
| `GET /study/current` | GET | 当前未答题 | ✅ |
| `POST /study/start_cycle` | POST | 开始新循环 | ✅ |
| `POST /study/answer` | POST | 提交答案 | ✅ |
| `GET /study/progress` | GET | 查看进度 | ✅ |
| `POST /study/skip_module` | POST | 跳过当前模块 | ✅ 新增 |
| `POST /study/mark_studied` | POST | 标记已学过 | ✅ 新增 |
| `GET /weak_points` | GET | 薄弱点列表 | ✅ 新增 |

UI 变更：
- 导航栏添加 "薄弱点" 和 "学习" 链接
- 答题页添加 [⏭ 跳过当前模块] 和 [📖 我已学过] 按钮
- 结果页显示 "有效完成" 或 "跳过不计入" 状态

---

## 执行的测试与命令

### 自动化检测
| 检测项 | 结果 |
|--------|------|
| 一次错误回答创建一条薄弱点记录 | ✅ |
| 页面刷新/重试不重复计数 | ✅（answered_at 守卫） |
| 同语法点 2 次错误后 is_active=True | ✅ |
| 后续循环优先使用活跃薄弱点作为复习题 | ✅（〜てみる 示例） |
| 未完成会话恢复至相同题目 | ✅ |
| 已完成循环不被恢复 | ✅ |
| skip_module 前进到下一模块 | ✅ |
| skip_module 导致 is_valid_completion=False | ✅ |
| skip_module 不产生薄弱点 | ✅ |
| mark_studied 导致 is_valid_completion=True | ✅ |
| Day 1 上传/提取可用 | ✅ |
| Day 2 19 题生成可用 | ✅ |

---

## 三有效循环 E2E 证据

| 度量 | Cycle 2 | Cycle 3 | Cycle 5 |
|------|---------|---------|---------|
| 素材 | sample-n2.txt | sample-n2.txt | sample-n2.txt |
| 语法 A | 〜てはいられない | 〜てはいられない | 〜てはいられない |
| 语法 B | 〜ざるを得ない | 〜ざるを得ない | 〜ざるを得ない |
| 总题数 | 19 | 19 | 19 |
| 已答 | 19 | 19 | 19 |
| 正确 | 7 | 3 | 2 |
| 正确率 | 36.8% | 15.8% | 10.5% |
| 有效完成 | ✅ | ✅ | ✅ |

---

## 附加跳过与标记学过验证

| 度量 | Cycle 7 (跳过) | Cycle 8 (标记学过) |
|------|----------------|-------------------|
| 已答 | 14 | 14 |
| 跳过 | 5 | 0 |
| 标记学过 | 0 | 5 |
| 有效完成 | ❌ False | ✅ True |
| 跳过产生薄弱点 | ❌ 否 | N/A |

---

## DeepSeek 用量与成本测量

### 定价来源
- 来源：https://api-docs.deepseek.com/quick_start/pricing
- 模型：deepseek-v4-flash（`deepseek-chat` 解析结果）
- 输入：$0.14/1M tokens（cache miss）
- 输出：$0.28/1M tokens
- 检索日期：2026-05-31

### 材料提取（单次）
| 用途 | Token 总量 |
|------|-----------|
| 语法提取 | ~3,000（估算，Day 1 未追踪） |
| 词汇提取 | ~2,500（估算，Day 1 未追踪） |
| **提取合计** | **~5,500 tokens ≈ $0.0011** |

### 全周期学习循环（生成 + 10 次评估）
| 用途 | 调用次数 | 输入 Tokens | 输出 Tokens | 总计 |
|------|---------|------------|------------|------|
| 语法解释生成 (×2) | 2 | 625 | 330 | 955 |
| 翻译题生成 (×2) | 2 | 655 | 1,203 | 1,858 |
| 选择题生成 (×1) | 1 | 686 | 852 | 1,538 |
| 翻译评估 (×10) | 10 | 4,363 | 935 | 5,298 |
| **单循环合计** | **15** | **6,329** | **3,320** | **9,649** |

### 单循环成本
- 输入：6,329 / 1M × $0.14 = $0.00089
- 输出：3,320 / 1M × $0.28 = $0.00093
- **单循环 ≈ $0.0018**

### 全 Day 3 验证总成本
| 指标 | 值 |
|------|-----|
| 总 API 调用次数 | 84 |
| 总输入 Tokens | 34,611 |
| 总输出 Tokens | 23,606 |
| 总 Tokens | 58,217 |
| 总成本 | **~$0.011** |

*注：实际成本可能因 cache hit（$0.0028/1M 输入）而更低。*

---

## 验收标准检查清单

| 标准 | 状态 |
|------|------|
| 薄弱点持久化 | ✅ |
| 2 次错误后激活 | ✅ |
| 后续循环优先激活薄弱点 | ✅ |
| /weak_points 页面 | ✅ |
| 会话恢复 | ✅ |
| 跳过模块 | ✅ |
| 标记已学过 | ✅ |
| 有效完成判定 | ✅ |
| 三个有效实时循环 | ✅（Cycle 2, 3, 5） |
| 跳过验证（额外循环） | ✅（Cycle 7） |
| 标记学过验证（额外循环） | ✅（Cycle 8） |
| Token 用量收集 | ✅ |
| 成本测量 | ✅（~$0.011 总量，~$0.0018/循环） |
| Day 1/Day 2 未破坏 | ✅ |
| 无密钥泄露 | ✅ |

---

## 已修复关键 Bug

1. **weak_points.html datetime 错误** — `{{ wp.last_error_at[:16] }}` 抛 TypeError（datetime 不支持切片），改为 `.strftime('%Y-%m-%d %H:%M')`

---

## P2/P3 待办

### P2（建议在下一次迭代中修复）
1. **语法解释预生成** — 当前在 GET /study/current 中按需生成解释，若 API 超时则无法展示。建议在 start_cycle 时预生成并持久化。
2. **复习优先级提升** — 当前策略将活跃薄弱点排到 review_points 数组前端，但 LLM 并不总是遵循排序。可改为直接提示 LLM 必须包含哪些语法点。
3. **薄弱点降级** — 当前没有降级机制（错误从不减少）。建议引入时间衰减或多轮正确后降级。
4. **Model alias 迁移** — `app/llm.py` 中默认模型为 `deepseek-chat`（兼容别名），该别名解析为 `deepseek-v4-flash`。建议改为显式指定 `deepseek-v4-flash`，因为兼容性别名可能在未来被废弃。

### P3（不作当前原型范围）
1. **听力练习** — Whisper/TTS 集成
2. **PDF 上传** — PDF 文本提取
3. **间隔重复（SRS）** — 遗忘曲线
4. **多用户认证** — 用户系统
5. **生产部署** — Docker/Celery/PostgreSQL
6. **UI 美化** — 进度条、图表、移动优化
7. **LLM 调用重试与超时** — 更健壮的 API 错误处理

---

## 三天原型收尾声明

Lingua Web 三天自用原型已完成全部核心流程：

```
上传 TXT/MD
  ↓
语法 + 词汇提取（DeepSeek）
  ↓
选择语法 A/B → 生成 19 题学习循环（DeepSeek）
  ↓
逐题作答（翻译通过 LLM 评估，选择通过 Python 判定）
  ↓
薄弱点自动追踪 + 后续循环复习优先
  ↓
结果展示（正确率 + 明细 + 有效完成状态）
  ↓
会话恢复 / 跳过 / 标记学过 支持
```

**原型已完成范围：**
- ✅ 素材上传与提取（TXT/MD）
- ✅ 19 题学习循环（10 翻译 + 9 选择题）
- ✅ 翻译语义评估（LLM）
- ✅ 选择题确定性评分（Python）
- ✅ 薄弱点追踪 + 复习优先
- ✅ 会话恢复 + 模块操作
- ✅ Token 用量与成本测量

**Day 1+2**: `f5922b2` — 素材上传提取 + 19 题学习循环运行时
**Day 3**: `f15c2ed` — 薄弱点、恢复、模块操作、成本测量

**总开发成本（含实测与估算）**
- Day 3 实测验证（84 次 API 调用，58,217 tokens）：**~$0.011**
- Day 1 提取（约 5,500 tokens，未记录）：**~$0.001（估算）**
- 合计三天验证成本：**约 $0.012**（基于已记录 token 与 Day 1 未追踪调用的估算）
- *注：单循环学习成本约 $0.002，三次完整原型验证不到两美分。*
