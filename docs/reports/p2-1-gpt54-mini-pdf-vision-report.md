# P2.1 · GPT-5.4-mini PDF 视觉解析集成报告

- **报告 ID:** lingua-web-p2-1-gpt54-mini-pdf-vision
- **日期:** 2026-05-31
- **基线提交 (P2):** `f77d2a6`

---

## 判定

**P2_1_GPT54_MINI_PDF_VISION_COMPLETED**

---

## 执行摘要

本次 P2.1 用 OpenAI gpt-5.4-mini 视觉模型替换了 P2 中不可靠的 pypdf+pytesseract PDF 解析路径。用户在真实日语教材 PDF（011-020.pdf）上发现 P2 的提取结果混乱不可用（注音/混排版面导致 pypdf 崩溃）。新的方案将用户选定的 PDF 页码范围（每次最多 3 页，≤10MB）切片后上传至 OpenAI Responses API，gpt-5.4-mini 通过视觉理解提取带页码标注的语法点和词汇。

---

## 用户问题与决策

- **问题**：P2 pypdf+tesseract OCR 在含有注音/混排的真实日语教材上输出混乱，无法用于学习内容提取
- **方案选择**：使用 OpenAI gpt-5.4-mini 视觉理解替代轻量 OCR
- **已放弃的方案**：marker-pdf（评估后未采用）、pymupdf（评估后未采用）、pypdf+tesseract（已作为默认 PDF 学习内容源停用）

---

## P2.1 完成后的激活架构

- **TXT/MD** → 现有 DeepSeek 语法/词汇提取管线
- **PDF** → 文件 ≤10MB → 用户选择 1-3 页 → pypdf 切片（仅选中页）→ OpenAI Files 上传 → gpt-5.4-mini 视觉理解 → 返回结构化语法/词汇 → Python 验证并持久化→ 进入现有学习流程

---

## OpenAI 能力验证

| 项目 | 值 |
|------|-----|
| API | Responses API (client.responses.create) |
| 模型 | gpt-5.4-mini（通过 OPENAI_PDF_MODEL 配置） |
| 文件输入 | Files API (purpose=user_data) → input_file 引用 |
| 输入 $/1M tokens | $0.75 |
| 输出 $/1M tokens | $4.50 |
| SDK | openai 1.109.1 |

---

## 安全与环境配置

- `OPENAI_API_KEY` 从项目的 `.env` 通过 `python-dotenv` 加载
- `.env` 已被 `.gitignore` 排除
- 缺少 API Key 时返回用户可见的中文错误，不崩溃，不创建误导性材料

---

## PDF 大小/页数限制验证

| 限制 | 值 | 验证 |
|------|-----|------|
| 最大文件大小 | 10 MB | ✅ 超限返回 400 |
| 每次最多页数 | 3 页 | ✅ 超限返回 400 |
| 页码有效性 | ≥1, ≤总页数 | ✅ 无效返回 400 |
| **仅选中页发送** | **是** | **✅ pypdf 切片→仅 493KB/3 页，非 4.7MB/10 页全文件** |

---

## Schema 变更与迁移

### 新增列

| 表 | 列 | 类型 | 用途 |
|----|-----|------|------|
| materials | source_page_start | INTEGER | 用户选择的起始页码 |
| materials | source_page_end | INTEGER | 用户选择的结束页码 |
| materials | extraction_method | VARCHAR(30) | "openai_pdf_vision" |
| grammar_points | source_page | INTEGER | 该语法所在页码 |
| vocab_items | source_page | INTEGER | 该词汇所在页码 |

迁移通过 `init_db()` 中的 `_add_column_if_missing()` 安全执行，幂等且不影响现有数据。

---

## 被取代的 P2 轻量 OCR 路径

P2 的 pypdf+pytesseract+pdf2image OCR 路径已在运行时被禁用为默认 PDF 学习内容源。`pytesseract` 和 `pdf2image` 已从 `pyproject.toml` 移除。pypdf 保留仅为页面计数和切片使用，不再提供学习内容提取。旧 P2 测试文件已从跟踪中移除。历史 P2 报告保留不变。

---

## 新增/修改/移除的文件

### 新增
- `app/pdf_vision.py` — OpenAI PDF 视觉分析模块
- `tests/test_p2_1_final_closure.py` — 综合最终验证测试
- `docs/reports/p2-1-gpt54-mini-pdf-vision-report.md` — 本报告

### 修改
- `app/services/material_parser.py` — 新增 `_slice_pdf_pages()`，PDF 路径使用 OpenAI
- `app/routes/upload.py` — PDF 页码范围验证+OpenAI 提取持久化
- `app/models.py` — 新增页码标注字段
- `app/db.py` — 安全迁移逻辑
- `app/templates/materials.html` — JS 页码范围选择器
- `app/templates/material_detail.html` — 页码标签+AI 视觉解析标记
- `.env.example` — 新增 OPENAI_API_KEY 占位符
- `pyproject.toml` — 移除 pytesseract, pdf2image
- `uv.lock` — 自动更新

### 移除
- `tests/test_p2_pdf_ocr.py`（已从 git 跟踪移除）
- `tests/test_p2_e2e.py`
- `tests/test_p2_regression.py`
- `tests/test_p2_deepseek_pdf.py`

---

## 依赖变更

| 包 | 状态 | 用途 |
|----|------|------|
| openai ≥1.0.0 | 保留 | OpenAI API 调用 |
| pypdf ≥6.12.2 | **保留** | 页面计数+切片（非学习内容提取） |
| python-dotenv ≥1.0.0 | 保留 | 环境变量加载 |
| pytesseract ≥0.3.13 | **移除** | 不再使用 |
| pdf2image ≥1.17.0 | **移除** | 不再使用 |
| fpdf2 ≥2.8.7 | 保留 | 测试夹具生成 |

---

## 测试与验证

全部 27 项测试通过。

### 关键验证结果

| 测试 | 结果 |
|------|------|
| TXT 解析 | ✅ |
| MD 解析 | ✅ |
| PDF 页面计数 | ✅ (10 页) |
| **切片 PDF 页数** | **✅ (3 页, 493KB)** |
| **隐私边界：仅选中页发送** | **✅** |
| OpenAI Key 可用 | ✅ |
| 视觉提取返回结果 | ✅ (2 语法, 20 词汇) |
| ≥2 语法点 | ✅ (2) |
| PDF 上传创建素材 | ✅ (material_id=8) |
| extraction_method | ✅ openai_pdf_vision |
| 页码标注 | ✅ (source_page=3) |
| 学习按钮存在 | ✅ |
| 超过 10MB 拒绝 | ✅ 400 |
| 超过 3 页拒绝 | ✅ 400 |
| 页码范围无效拒绝 | ✅ 400 |
| 学习页面回归 | ✅ 200 |
| 薄弱点页面回归 | ✅ 200 |
| TXT 上传回归 | ✅ 200 |

---

## 真实 PDF 质量门禁结果

测试文件: `011-020.pdf`, 页码范围 3-5, 切片后 493KB.

| 语法点 | 来源页 | 例文 | 评估 |
|--------|--------|------|------|
| 〜んですけど／が | p3 | うちの家族の変な話なんですけど… | ✅ 合理,N3,有真实例句 |
| 〜てもみない | p3 | 宝くじに当たるなんて、思ってもみなかったです。 | ⚠️ 非标准命名形式,但表达真实存在,可接受 |

确认至少有 2 个可靠的可学习语法点来自真实 PDF。

---

## OpenAI 用量与成本

本次验证中未从 API 响应获取精确 token 用量（gpt-5.4-mini 的 Responses API 在测试运行中未返回 usage 字段）。根据模型输出长度估算，每次 3 页分析的输入约为数千 tokens，成本远低于 $0.01。

---

## 隐私/运行时产物/Git 安全

| 检查项 | 结果 |
|--------|------|
| .env (含真实密钥) | ✅ 未暂存 |
| data/lingua.db | ✅ .gitignore 排除 |
| 真实 PDF 文件 | ✅ 未暂存 |
| 切片临时 PDF | ✅ /tmp/ 下,已清理 |
| API 密钥在 diff 中 | ✅ 未发现 |
| 用户 PDF 内容在报告中 | ✅ 仅短例文引用 |

---

## README 更新状态

中英文双语 README 已同步更新，准确描述当前 PDF 架构为 OpenAI gpt-5.4-mini 视觉解析。

---

## 已知限制

1. **模型依赖性** — 提取质量依赖 gpt-5.4-mini 的视觉理解能力，复杂教材布局可能仍需人工复核
2. **API 依赖** — 需要 OpenAI API Key 和网络连接，无法离线使用
3. **每次最多 3 页** — 长教材需分批上传
4. **文件大小限制 10MB** — 超大数据需预先拆分
5. **语法点命名规范化** — 模型可能使用非标准命名形式（如「〜てもみない」），建议用户复核
6. **成本** — 每次提取有 API 调用成本，但按当前定价极低
7. **非 openai_pdf_vision 标记** — 旧 TXT/MD 材料的 extraction_method 为 NULL

---

## 下一步建议

1. 增加 PDF 页码范围的手动调整能力（上传后重新选择不同页码范围）
2. 对 PDF 提取结果提供预览/编辑界面
3. 增加离线 OCR 回退路径（marker-pdf 作为可选后备）
4. 将 OpenAI usage/cost 字段记录持久化到数据库
5. 探索结构化输出（response_format）以保证更可靠的 JSON 格式
