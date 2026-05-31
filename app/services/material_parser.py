"""
Material parser for Lingua Web.

Handles TXT, Markdown, and PDF (including OCR fallback) material ingestion.
Follows the pattern: text extraction first, then feed plain text to the
existing DeepSeek grammar/vocabulary extraction pipeline.
"""

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pypdf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum pages to process from a PDF (prototype guard against runaway cost)
MAX_PDF_PAGES = 30

# Minimum non-whitespace characters to consider embedded text "sufficient"
MIN_EMBEDDED_TEXT_CHARS = 200

# Temporary directory for OCR rendering output
TEMP_DIR = Path(tempfile.gettempdir()) / "lingua_web_ocr"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ParsedMaterial:
    """Result of parsing an uploaded file."""

    source_type: str  # "txt" | "md" | "pdf"
    content_text: str
    parse_method: str  # "text" | "markdown" | "pdf_text" | "pdf_ocr"
    warnings: list[str] = field(default_factory=list)
    page_count: int | None = None
    filename: str = ""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def parse_uploaded_material(filename: str, content: bytes) -> ParsedMaterial:
    """
    Parse an uploaded file and return the extracted plain text.

    Dispatches to the correct parser based on file extension.
    The returned content_text is always plain UTF-8 text suitable for
    the existing DeepSeek extraction pipeline.
    """
    suffix = Path(filename).suffix.lower()

    if suffix in (".txt", ".md"):
        return _parse_text(content, suffix)
    elif suffix == ".pdf":
        return _parse_pdf(content, filename)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


def is_supported_extension(suffix: str) -> bool:
    """Check if a file extension is supported."""
    return suffix.lower() in {".txt", ".md", ".pdf"}


def supported_extensions_display() -> str:
    """Human-readable list of supported extensions."""
    return ".txt, .md, .pdf"


# ---------------------------------------------------------------------------
# Text / Markdown parser
# ---------------------------------------------------------------------------

def _parse_text(content: bytes, suffix: str) -> ParsedMaterial:
    """Decode text file content as UTF-8."""
    text = content.decode("utf-8")
    source_type = "md" if suffix == ".md" else "txt"
    parse_method = "markdown" if suffix == ".md" else "text"
    return ParsedMaterial(
        source_type=source_type,
        content_text=text,
        parse_method=parse_method,
        filename="",
    )


# ---------------------------------------------------------------------------
# PDF parser (embedded text + OCR fallback)
# ---------------------------------------------------------------------------

def _parse_pdf(content: bytes, filename: str) -> ParsedMaterial:
    """
    Extract text from a PDF.

    1. Attempt embedded-text extraction via pypdf.
    2. If insufficient text, fall back to OCR via tesseract.
    """
    from io import BytesIO

    warnings: list[str] = []

    try:
        reader = pypdf.PdfReader(BytesIO(content))
    except Exception as exc:
        return ParsedMaterial(
            source_type="pdf",
            content_text="",
            parse_method="pdf_text",
            warnings=[f"无法读取 PDF 文件：{exc}"],
            page_count=None,
            filename=filename,
        )

    page_count = len(reader.pages)

    # Enforce page limit
    if page_count > MAX_PDF_PAGES:
        # Offer the first N pages
        warnings.append(
            f"该 PDF 共 {page_count} 页，仅处理前 {MAX_PDF_PAGES} 页。"
        )

    pages_to_process = min(page_count, MAX_PDF_PAGES)

    # --- Embedded text extraction ---
    embedded_text_parts: list[str] = []
    for i in range(pages_to_process):
        try:
            page = reader.pages[i]
            text = page.extract_text() or ""
            embedded_text_parts.append(text)
        except Exception:
            embedded_text_parts.append("")

    embedded_text = "\n".join(embedded_text_parts).strip()
    non_whitespace_chars = len(re.sub(r"\s+", "", embedded_text))

    if non_whitespace_chars >= MIN_EMBEDDED_TEXT_CHARS:
        # Sufficient embedded text — done
        return ParsedMaterial(
            source_type="pdf",
            content_text=embedded_text,
            parse_method="pdf_text",
            warnings=warnings,
            page_count=page_count,
            filename=filename,
        )

    # --- OCR fallback ---
    warnings.append(
        f"PDF 嵌入文本不足（{non_whitespace_chars} 有效字符），"
        f"启动 OCR 识别（共 {pages_to_process} 页）。"
    )

    ocr_text = _ocr_pdf(content, pages_to_process, warnings)

    if not ocr_text.strip():
        return ParsedMaterial(
            source_type="pdf",
            content_text="",
            parse_method="pdf_ocr",
            warnings=warnings
            + ["无法从该 PDF 中提取可学习的文本。系统已尝试文本读取与 OCR，请确认文件清晰且未加密后重试。"],
            page_count=page_count,
            filename=filename,
        )

    # Cleanup temp files
    _cleanup_temp()
    return ParsedMaterial(
        source_type="pdf",
        content_text=ocr_text,
        parse_method="pdf_ocr",
        warnings=warnings,
        page_count=page_count,
        filename=filename,
    )


def _ocr_pdf(content: bytes, page_count: int, warnings: list[str]) -> str:
    """
    Perform OCR on PDF pages using tesseract.

    Renders each page as an image (via pdf2image/poppler), then runs
    tesseract OCR with Japanese language model.
    """
    from io import BytesIO

    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        warnings.append("OCR 依赖 pdf2image 未安装，跳过 OCR 回退。")
        return ""

    try:
        import pytesseract
    except ImportError:
        warnings.append("OCR 依赖 pytesseract 未安装，跳过 OCR 回退。")
        return ""

    try:
        images = convert_from_bytes(
            content,
            first_page=1,
            last_page=page_count,
            dpi=300,
            fmt="png",
            output_folder=str(TEMP_DIR),
        )
    except Exception as exc:
        warnings.append(f"PDF 页面渲染失败（OCR 步骤）：{exc}")
        return ""

    page_texts: list[str] = []
    for i, img in enumerate(images):
        try:
            text = pytesseract.image_to_string(img, lang="jpn+eng")
            page_texts.append(text.strip())
        except Exception as exc:
            warnings.append(f"第 {i+1} 页 OCR 失败：{exc}")
            page_texts.append("")

    return "\n".join(page_texts)


def _cleanup_temp():
    """Clean up temporary OCR image files."""
    try:
        for f in TEMP_DIR.iterdir():
            if f.is_file():
                f.unlink()
    except Exception:
        pass
