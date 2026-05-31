"""
Material parser for Lingua Web.

Handles TXT, Markdown (via UTF-8 decode -> DeepSeek), and PDF
(via gpt-5.4-mini vision) material ingestion.

For PDF: uses OpenAI Responses API with model gpt-5.4-mini for
layout-aware understanding of Japanese textbook pages.
Only selected pages (bounded: max 3 per request, up to 10 MB) are sent.
"""

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from io import BytesIO

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum upload size for PDF (10 MB)
MAX_PDF_BYTES = 10 * 1024 * 1024

# Maximum pages per analysis request
MAX_PDF_PAGES = 3

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ParsedMaterial:
    """Result of parsing an uploaded file."""

    source_type: str  # "txt" | "md" | "pdf"
    content_text: str
    parse_method: str  # "text" | "markdown" | "openai_pdf_vision"
    warnings: list[str] = field(default_factory=list)
    page_count: int | None = None
    filename: str = ""
    source_page_start: int | None = None
    source_page_end: int | None = None
    # Structured items from PDF vision (populated only for PDF path)
    grammar_items: list[dict] = field(default_factory=list)
    vocab_items: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def parse_uploaded_material(filename: str, content: bytes) -> ParsedMaterial:
    """Parse a TXT or Markdown file."""
    suffix = Path(filename).suffix.lower()

    if suffix in (".txt", ".md"):
        return _parse_text(content, suffix)
    elif suffix == ".pdf":
        raise ValueError("PDF requires page range. Use parse_pdf_with_pages().")
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


def parse_pdf_with_pages(
    content: bytes,
    filename: str,
    start_page: int,
    end_page: int,
) -> ParsedMaterial:
    """Parse specific PDF pages using gpt-5.4-mini vision understanding."""
    from app.pdf_vision import extract_from_pdf_pages, is_available

    if not is_available():
        return ParsedMaterial(
            source_type="pdf",
            content_text="",
            parse_method="openai_pdf_vision",
            warnings=["尚未配置 OpenAI API Key，无法分析 PDF 页面。"],
            page_count=None,
            filename=filename,
        )

    page_count = _get_pdf_page_count(content)
    if page_count and end_page > page_count:
        end_page = page_count

    # Slice PDF to ONLY selected pages (privacy/cost boundary)
    sliced = _slice_pdf_pages(content, start_page - 1, end_page - 1)
    if sliced is None:
        return ParsedMaterial(
            source_type="pdf",
            content_text="",
            parse_method="openai_pdf_vision",
            warnings=["无法提取所选页码范围。请确认文件可读取。"],
            page_count=page_count,
            filename=filename,
        )

    # Write sliced PDF (selected pages only) to temp file for OpenAI upload
    temp_dir = Path(tempfile.gettempdir()) / "lingua_web_pdf"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_pdf = temp_dir / f"pdf_{uuid.uuid4().hex}.pdf"
    temp_pdf.write_bytes(sliced)

    try:
        result = extract_from_pdf_pages(str(temp_pdf), start_page, end_page)
    finally:
        try:
            temp_pdf.unlink()
        except Exception:
            pass

    if result is None or (not result.grammar_items and not result.vocab_items):
        return ParsedMaterial(
            source_type="pdf",
            content_text="",
            parse_method="openai_pdf_vision",
            warnings=["AI 无法从所选页面提取可学习的日语内容。"],
            page_count=page_count,
            filename=filename,
            source_page_start=start_page,
            source_page_end=end_page,
        )

    # Build structured content text
    content_lines = []
    for g in result.grammar_items:
        content_lines.append(f"【文法】{g.point_name}")
        content_lines.append(f"  説明：{g.explanation_zh}")
        content_lines.append(f"  例文：{g.example_from_page}")
    for v in result.vocab_items:
        entry = f"【語彙】{v.word}"
        if v.reading:
            entry += f"（{v.reading}）"
        if v.meaning_zh:
            entry += f"：{v.meaning_zh}"
        if v.example_from_page:
            entry += f"｜例：{v.example_from_page}"
        content_lines.append(entry)

    content_text = "\n".join(content_lines)

    # Convert dataclass items to dicts for route persistence
    grammar_dicts = [
        {
            "point_name": g.point_name,
            "explanation_zh": g.explanation_zh,
            "example_from_page": g.example_from_page,
            "source_page": g.source_page,
            "difficulty_level": g.difficulty_level,
        }
        for g in result.grammar_items
    ]
    vocab_dicts = [
        {
            "word": v.word,
            "reading": v.reading or "",
            "meaning_zh": v.meaning_zh or "",
            "example_from_page": v.example_from_page or "",
            "source_page": v.source_page,
            "difficulty_level": v.difficulty_level,
        }
        for v in result.vocab_items
    ]

    return ParsedMaterial(
        source_type="pdf",
        content_text=content_text,
        parse_method="openai_pdf_vision",
        warnings=[],
        page_count=page_count,
        filename=filename,
        source_page_start=start_page,
        source_page_end=end_page,
        grammar_items=grammar_dicts,
        vocab_items=vocab_dicts,
    )


def is_supported_extension(suffix: str) -> bool:
    """Check if a file extension is supported."""
    return suffix.lower() in {".txt", ".md", ".pdf"}


def supported_extensions_display() -> str:
    """Human-readable list of supported extensions."""
    return ".txt, .md, .pdf"


# ---------------------------------------------------------------------------
# Internal helpers
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


def _get_pdf_page_count(content: bytes) -> int | None:
    """Get the number of pages in a PDF (lightweight, for validation only)."""
    try:
        import pypdf
        reader = pypdf.PdfReader(BytesIO(content))
        return len(reader.pages)
    except Exception:
        return None


def _slice_pdf_pages(content: bytes, start_idx: int, end_idx: int) -> bytes | None:
    """Extract only selected pages from a PDF. 0-indexed inclusive."""
    try:
        import pypdf
        from io import BytesIO
        reader = pypdf.PdfReader(BytesIO(content))
        writer = pypdf.PdfWriter()
        for i in range(start_idx, end_idx + 1):
            if i < len(reader.pages):
                writer.add_page(reader.pages[i])
        buf = BytesIO()
        writer.write(buf)
        buf.seek(0)
        return buf.getvalue()
    except Exception as exc:
        print(f"[parser] PDF slicing failed: {exc}")
        return None
