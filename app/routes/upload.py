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
    is_supported_extension,
    supported_extensions_display,
)

router = APIRouter(prefix="/materials", tags=["materials"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf"}


def _validate_material(text: str) -> bool:
    """Basic validation: non-empty text readable as UTF-8."""
    return bool(text and text.strip())


@router.get("", response_class=HTMLResponse)
async def list_materials(request: Request, db: Session = Depends(get_db)):
    """Display uploaded materials list with extraction status."""
    materials = db.query(Material).order_by(Material.uploaded_at.desc()).all()
    # Pass grammar point count for each material
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
    db: Session = Depends(get_db),
):
    """Upload a TXT/MD/PDF material file, persist it, and trigger extraction."""
    # --- Validate file extension ---
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return HTMLResponse(
            f"Unsupported file type '{suffix}'. "
            f"Only {', '.join(sorted(ALLOWED_EXTENSIONS))} are accepted.",
            status_code=400,
        )

    # --- Read file content ---
    try:
        raw = await file.read()
    except Exception as exc:
        return HTMLResponse(f"Failed to read uploaded file: {exc}", status_code=400)

    # --- Parse content (TXT/MD as text, PDF with OCR fallback) ---
    try:
        parsed = parse_uploaded_material(file.filename or "untitled", raw)
    except UnicodeDecodeError:
        return HTMLResponse(
            "File encoding error: could not read as UTF-8 text.",
            status_code=400,
        )
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)

    if not parsed.content_text.strip():
        error_msg = "无法从该文件提取可学习的文本。"
        if parsed.warnings:
            error_msg += " " + " ".join(parsed.warnings)
        return HTMLResponse(error_msg, status_code=400)

    # --- Persist material ---
    source_type = parsed.source_type
    material = Material(
        filename=file.filename,
        content_text=parsed.content_text,
        source_type=source_type,
        language_code="ja",
        uploaded_at=datetime.datetime.utcnow(),
    )
    db.add(material)
    db.commit()
    db.refresh(material)

    # --- Automatic extraction ---
    extraction_errors: list[str] = []

    # Grammar extraction
    grammar_items = extract_grammar_points(parsed.content_text)
    for g in grammar_items:
        db.add(
            GrammarPoint(
                material_id=material.id,
                point_name=g.point_name,
                explanation_jp=g.explanation_jp,
                example_from_material=g.example_from_material,
                difficulty_level=g.difficulty_level,
                extracted_at=datetime.datetime.utcnow(),
            )
        )

    # Vocab extraction
    vocab_items = extract_vocab(parsed.content_text)
    for v in vocab_items:
        db.add(
            VocabItem(
                material_id=material.id,
                word=v.word,
                reading=v.reading,
                meaning_zh=v.meaning_zh,
                example_from_material=v.example_from_material,
                difficulty_level=v.difficulty_level,
                extracted_at=datetime.datetime.utcnow(),
            )
        )

    db.commit()

    # --- Redirect to material detail page ---
    response = RedirectResponse(
        url=f"/materials/{material.id}", status_code=303
    )
    return response
