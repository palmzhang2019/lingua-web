# P2 · PDF/OCR 材料导入与空状态上传入口修复报告

- **报告 ID:** lingua-web-p2-pdf-ocr-and-upload-entry
- **日期:** 2026-05-31
- **基线提交:** `f15c2ed` (Day 3 原型关闭)

---

## 判定

**P2_PDF_OCR_AND_UPLOAD_ENTRY_COMPLETED**

---

## 执行摘要

本次 P2 对 Lingua Web 进行了两项关键改进：

1. **修复空状态上传入口** — 无素材时直接渲染上传表单，不再需要两次点击才能上传。
2. **PDF/OCR 材料导入** — 支持 PDF 上传（含扫描件 OCR 回退），提取的文本自动接入现有 DeepSeek 语法/词汇提取管线。

所有验证条件均满足：TXT/MD 回归通过、嵌入式文本 PDF 通过、OCR 回退 PDF 通过、PDF 衍生的真实 DeepSeek 提取通过（5 语法点 + 24 词汇），现有学习流程可正常使用。

---

## 基线提交与 Git 状态

- HEAD: `7d05eee`（bilingual README 文档更新）
- Day 3 关闭提交 `f15c2ed` 存在 ✅
- 无未提交的应用代码冲突
- `data/lingua.db` 由 `.gitignore` 正确排除 ✅
- 无 .env、密钥或用户 PDF 材料被暂存

---

## 参考 OCR Skill 检查

读取了 `ocr-and-documents` Skill（`~/.hermes/skills/productivity/ocr-and-documents/SKILL.md`）：

- 推荐 pymupdf（轻量文本 PDF）+ marker-pdf（扫描件 OCR，需 3-5GB）
- 本实现选择了 `pypdf`（纯 Python，轻量，等效于 pymupdf 的文本提取）配合 `pytesseract + pdf2image + poppler-utils` 作为 OCR 回退
- 不选用 marker-pdf 的原因：依赖体积过大（~3-5GB 含 PyTorch 模型）、当前 OCR 方案已验证可行

---

## 空状态上传入口根因与修复

**根因**：原有设计需两次点击才能上传：点击「点击这里上传第一篇素材」→ 导航到 `?show_upload=1` → 表单才出现。用户点击后无足够视觉反馈。

**修复**：空状态直接内嵌可工作的上传表单（含文件输入和提交按钮），无需额外点击。非空状态保留原有的「＋ 上传新素材」链接逻辑。

---

## PDF 文本提取与 OCR 回退设计

| 层 | 工具 | 行为 |
|----|------|------|
| 嵌入式文本 | `pypdf.PdfReader.extract_text()` | 优先尝试；≥200 非空白字符即认为足够 |
| OCR 回退 | `pdf2image` 渲染 → `pytesseract` (jpn+eng) | 嵌入文本不足时自动切换 |
| 失败处理 | 清空文本 + 中文错误信息 | 告知用户无法提取、建议换文件 |

---

## 依赖与系统前置条件

### Python 依赖（通过 uv 管理）

| 包 | 用途 |
|----|------|
| `pypdf` | 嵌入式 PDF 文本提取 |
| `pytesseract` | Python tesseract 绑定（OCR） |
| `pdf2image` | PDF 页面→图像渲染 |
| `fpdf2` | 仅用于测试夹具生成 |

### 系统依赖（uv 无法安装）

- `tesseract-ocr` + `tesseract-ocr-jpn`（日语 OCR 语言包）
- `poppler-utils`（供 pdf2image 使用）

---

## PDF 安全限制

- 最多处理前 **30 页**，超出时给出警告
- 超过 30 页的 PDF：仅处理前 30 页，其余忽略
- 嵌入式文本阈值：≥200 非空白字符即跳过 OCR

---

## Schema 变更状态

**无 schema 变更。** 仅扩展已有 `materials.source_type` 字段接受 `"pdf"` 值（`String(20)` 已有足够容量）。

---

## 运行时产物与 Git 安全检查

| 检查项 | 结果 |
|--------|------|
| `data/lingua.db` | `.gitignore` 排除，未暂存 ✅ |
| `.env` / 密钥 | 未暂存 ✅ |
| 上传的用户 PDF | 仅本地 /tmp 下的合成夹具，未提交 ✅ |
| OCR 临时图片 | `TEMP_DIR = /tmp/lingua_web_ocr`，未提交 ✅ |
| `Untitled` 无关文件 | 未暂存 ✅ |

---

## 创建/修改的文件

| 文件 | 操作 |
|------|------|
| `app/services/__init__.py` | 新增 |
| `app/services/material_parser.py` | 新增 — PDF/OCR 文本解析 |
| `app/routes/upload.py` | 修改 — 整合解析器，接受 PDF |
| `app/templates/materials.html` | 修改 — 空状态直接渲染表单 |
| `app/templates/base.html` | 修改 — 文件类型标签样式 |
| `pyproject.toml` | 修改 — 新增依赖 |
| `uv.lock` | 修改 — 自动更新 |
| `README.md` | 修改 — 中英文双语更新 |
| `tests/test_p2_pdf_ocr.py` | 新增 — 解析器单元测试 |
| `tests/test_p2_e2e.py` | 新增 — 端到端 HTTP 测试 |
| `tests/test_p2_regression.py` | 新增 — 回归检查 |
| `tests/test_p2_deepseek_pdf.py` | 新增 — DeepSeek PDF 提取验证 |
| `docs/reports/p2-pdf-ocr-and-upload-entry-report.md` | 新增 — 本报告 |

---

## 执行的测试与命令

```bash
# 解析器单元测试
python3 tests/test_p2_pdf_ocr.py

# 端到端 HTTP 测试
python3 tests/test_p2_e2e.py

# 回归检查
python3 tests/test_p2_regression.py

# DeepSeek PDF 提取验证（加载 ~/.hermes/.env）
python3 tests/test_p2_deepseek_pdf.py
```

---

## TXT/MD 回归结果

| 测试 | 结果 |
|------|------|
| TXT 解析 | ✅ `source_type=txt` |
| MD 解析 | ✅ `source_type=md` |
| TXT HTTP 上传 | ✅ 303 重定向 |
| MD HTTP 上传 | ✅ 303 重定向 |
| 学习页面 | ✅ 200 |
| 薄弱点页面 | ✅ 200 |

---

## 嵌入式文本 PDF 验证

- 文件：合成夹具 `test_embedded_pdf.pdf`（1 页，英文 N2 语法说明）
- 解析方法：`pdf_text`（pypdf 直接提取）
- 提取文本：423 字符
- 上传状态：200（重定向至详情页）
- 结论：✅ 通过

---

## OCR 回退 PDF 验证

- 文件：合成夹具 `test_ocr_pdf.pdf`（1 页，基于 PIL 图像渲染的英文+一些可识别字符）
- 解析方法：`pdf_ocr` ✓（触发了 OCR 回退）
- 提取文本：80 字符
- OCR 引擎：tesseract v5.3.4（jpn+eng）
- 上传状态：200
- 结论：✅ 通过

---

## 真实 DeepSeek PDF 提取证据

### 主验证（OCR 衍生 PDF）

使用含 8 个 N2 语法点的合成扫描 PDF（`test_ocr_n2_pdf.pdf`，2 页图像，pypdf 嵌入文本 = 0 字符）：

| 指标 | 值 |
|------|-----|
| Material ID | 4 |
| 解析方法 | `pdf_ocr` |
| OCR 提取字符 | ~634 字符（jpn+eng） |
| DeepSeek 原始语法点 | 7 |
| 校验通过语法点 | **5** |
| 校验通过的语法点 | てはいられない(N2)、ざるを得ない(N2)、に限って(N2)、たきり(N2)、つつある(N2) |
| 被过滤语法点（示例不匹配OCR） | ものの、がち |
| DeepSeek 词汇 | 24（全部通过校验） |
| 可进入学习流程 | ✅（5 ≥ 2 语法点） |
| DeepSeek API | 可用 ✅ |

**结论**：PDF 上传 → OCR 回退 → DeepSeek 语法/词汇提取 → 持久化并进入学习流程，全链路验证通过。

### 补充说明

- 2 个语法点被过滤器拒绝是因为 OCR 文本与原始排版不完全一致，DeepSeek 生成的部分示例在 OCR 输出中无法精确匹配。这是验证层的预期行为。
- DeepSeek 凭据从 `~/.hermes/.env` 加载，未在报告中暴露。

---

## 现有学习流程回归结果

| 检查 | 结果 |
|------|------|
| `/study` 页面正常加载 | ✅ 200 |
| `/weak_points` 正常加载 | ✅ 200 |
| 已有材料详情页正常 | ✅ 包含语法/词汇分区 |
| 可点击「开始学习」 | ✅（≥2 语法点显示绿色按钮） |

---

## README 更新状态

中英文双语 README 已同步更新：

- 支持格式：TXT、Markdown、PDF（含 OCR）
- PDF 行为：直接文本提取优先，扫描件自动 OCR
- 30 页安全边界说明
- 系统依赖：tesseract-ocr、tesseract-ocr-jpn、poppler-utils
- 路线图：PDF/OCR 已从 P3 移至 P2 ✅，空状态修复同上 ✅
- 新增 P2 报告链接

---

## 已知限制

1. **OCR 质量依赖文档清晰度** — 扫描件模糊、字体特殊、手写等情况可能影响识别效果
2. **不超过 30 页** — PDF 页数保护限制，长文档需自行拆分
3. **嵌入式文本阈值固定** — 200 非空白字符的阈值不可配置，对混合型 PDF（少量文本+扫描图）可能不够智能
4. **无批量导入** — 暂不支持文件夹/多文件批量导入
5. **OCR 未保存在 DB** — 是否通过 OCR 提取的信息不持久化到数据库（仅运行时日志可见）
6. **示例验证严格** — OCR 文本因排版差异可能导致部分语法点的源示例验证失败

---

## 下一步建议

1. 让 PDF 提取方法信息（pdf_text / pdf_ocr）在材料详情页用户可见
2. 支持用户手动选择页面范围（而非固定前 30 页）
3. 对测试环境和评估流程做本地 .env 解决方案或独立的测试凭据管理
4. 增加混合型 PDF（部分文本+部分扫描）的智能检测策略
5. 稳定后将本次 P2 生成的测试夹具脚本常驻化
