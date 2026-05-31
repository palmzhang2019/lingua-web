"""Material upload and listing routes for Lingua Web."""

import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Material, GrammarPoint, VocabItem
from app.agents.extractor import extract_grammar_points, extract_vocab
from app.services.material_parser import (
    parse_uploaded_material,
    parse_pdf_with_pages,
    is_supported_extension,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    _get_pdf_page_count,
)

router = APIRouter(prefix="/materials", tags=["materials"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf"}


@router.get("", response_class=HTMLResponse)
async def list_materials(request: Request, db: Session = Depends(get_db)):
    """Display uploaded materials list with extraction status."""
    materials = db.query(Material).order_by(Material.uploaded_at.desc()).all()
    material_list = []
    for m in materials:
        gp_count = db.query(GrammarPoint).filter(GrammarPoint.material_id == m.id).count()
        material_list.append({"material": m, "grammar_count": gp_count})
    return templates.TemplateResponse(
        request, "materials.html",
        {"materials": material_list},
    )


@router.get("/{material_id}", response_class=HTMLResponse)
async def material_detail(
    request: Request,
    material_id: int,
    db: Session = Depends(get_db),
):
    """Display a single material with extracted grammar points and vocab."""
    material = db.query(Material).filter(Material.id == material_id).first()
    if not material:
        return HTMLResponse("Material not found", status_code=404)

    grammar_points = (
        db.query(GrammarPoint)
        .filter(GrammarPoint.material_id == material_id)
        .order_by(GrammarPoint.extracted_at)
        .all()
    )
    vocab_items = (
        db.query(VocabItem)
        .filter(VocabItem.material_id == material_id)
        .order_by(VocabItem.extracted_at)
        .all()
    )

    return templates.TemplateResponse(
        request, "material_detail.html",
        {
            "material": material,
            "grammar_points": grammar_points,
            "vocab_items": vocab_items,
        },
    )


@router.post("/upload")
async def upload_material(
    request: Request,
    file: UploadFile = File(...),
    start_page: int = Form(1),
    end_page: int = Form(1),
    db: Session = Depends(get_db),
):
    """Upload a TXT/MD/PDF material file, persist it, and trigger extraction."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return HTMLResponse(
            f"Unsupported file type '{suffix}'. "
            f"Only {', '.join(sorted(ALLOWED_EXTENSIONS))} are accepted.",
            status_code=400,
        )

    try:
        raw = await file.read()
    except Exception as exc:
        return HTMLResponse(f"Failed to read uploaded file: {exc}", status_code=400)

    # =====================================================================
    # TXT / Markdown path (unchanged)
    # =====================================================================
    if suffix in (".txt", ".md"):
        try:
            parsed = parse_uploaded_material(file.filename or "untitled", raw)
        except UnicodeDecodeError:
            return HTMLResponse(
                "File encoding error: could not read as UTF-8 text.",
                status_code=400,
            )

        if not parsed.content_text.strip():
            return HTMLResponse("无法从该文件提取可学习的文本。", status_code=400)

        material = Material(
            filename=file.filename,
            content_text=parsed.content_text,
            source_type=parsed.source_type,
            language_code="ja",
            uploaded_at=datetime.datetime.utcnow(),
        )
        db.add(material)
        db.commit()
        db.refresh(material)

        _run_deepseek_extraction(db, material.id, parsed.content_text)

        return RedirectResponse(url=f"/materials/{material.id}", status_code=303)

    # =====================================================================
    # PDF path (OpenAI gpt-5.4-mini vision)
    # =====================================================================
    if suffix == ".pdf":
        # File size check
        if len(raw) > MAX_PDF_BYTES:
            return HTMLResponse(
                f"PDF 文件超过 {MAX_PDF_BYTES // (1024*1024)} MB。"
                f"当前版本请上传较小文件，或先截取需要学习的页面。",
                status_code=400,
            )

        # Page count
        page_count = _get_pdf_page_count(raw)
        if page_count is None:
            return HTMLResponse("无法读取该 PDF 的页数。请确认文件可读取。", status_code=400)

        # Page range validation
        if start_page < 1 or end_page > page_count or end_page < start_page:
            return HTMLResponse(
                f"页码范围无效。文件共 {page_count} 页，"
                f"每次最多分析 {MAX_PDF_PAGES} 页。",
                status_code=400,
            )

        if (end_page - start_page + 1) > MAX_PDF_PAGES:
            return HTMLResponse(
                f"每次最多分析 {MAX_PDF_PAGES} 页。请缩小页码范围。",
                status_code=400,
            )

        # Run OpenAI PDF vision extraction
        parsed = parse_pdf_with_pages(raw, file.filename or "untitled", start_page, end_page)

        if parsed.warnings:
            return HTMLResponse(" ".join(parsed.warnings), status_code=400)

        if not parsed.content_text.strip():
            return HTMLResponse("无法从所选 PDF 页面提取可学习的日语内容。", status_code=400)

        # Persist material
        material = Material(
            filename=file.filename,
            content_text=parsed.content_text,
            source_type="pdf",
            language_code="ja",
            uploaded_at=datetime.datetime.utcnow(),
            source_page_start=start_page,
            source_page_end=end_page,
            extraction_method="openai_pdf_vision",
        )
        db.add(material)
        db.commit()
        db.refresh(material)

        # Persist grammar/vocab from OpenAI vision result
        _persist_pdf_vision_items(db, material.id, parsed.grammar_items, parsed.vocab_items)

        return RedirectResponse(url=f"/materials/{material.id}", status_code=303)

    return HTMLResponse("Unsupported file type.", status_code=400)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _run_deepseek_extraction(db: Session, material_id: int, text: str):
    """Run existing DeepSeek grammar/vocab extraction (TXT/MD path)."""
    grammar_items = extract_grammar_points(text)
    for g in grammar_items:
        db.add(GrammarPoint(
            material_id=material_id,
            point_name=g.point_name,
            explanation_jp=g.explanation_jp,
            example_from_material=g.example_from_material,
            difficulty_level=g.difficulty_level,
            extracted_at=datetime.datetime.utcnow(),
        ))

    vocab_items = extract_vocab(text)
    for v in vocab_items:
        db.add(VocabItem(
            material_id=material_id,
            word=v.word,
            reading=v.reading,
            meaning_zh=v.meaning_zh,
            example_from_material=v.example_from_material,
            difficulty_level=v.difficulty_level,
            extracted_at=datetime.datetime.utcnow(),
        ))
    db.commit()


def _persist_pdf_vision_items(
    db: Session,
    material_id: int,
    grammar_dicts: list[dict],
    vocab_dicts: list[dict],
):
    """Persist grammar/vocab items from OpenAI PDF vision extraction."""
    now = datetime.datetime.utcnow()
    for g in grammar_dicts:
        db.add(GrammarPoint(
            material_id=material_id,
            point_name=g.get("point_name", ""),
            explanation_jp=g.get("explanation_zh", ""),
            example_from_material=g.get("example_from_page", ""),
            difficulty_level=g.get("difficulty_level", "N2"),
            extracted_at=now,
            source_page=g.get("source_page"),
        ))

    for v in vocab_dicts:
        db.add(VocabItem(
            material_id=material_id,
            word=v.get("word", ""),
            reading=v.get("reading") or None,
            meaning_zh=v.get("meaning_zh") or None,
            example_from_material=v.get("example_from_page") or None,
            difficulty_level=v.get("difficulty_level", "N2"),
            extracted_at=now,
            source_page=v.get("source_page"),
        ))
    db.commit()
