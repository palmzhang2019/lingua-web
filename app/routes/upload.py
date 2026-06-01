"""Material upload and listing routes for Lingua Web."""

import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Material, GrammarPoint, VocabItem, CycleMaterial, StudyCycle
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
    # Exclude archived materials from the active list
    active_materials = [m for m in materials if m.archived_at is None]
    material_list = []
    for m in active_materials:
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


# =============================================================================
# POST /materials/<id>/grammar/<gid>/toggle_mastered
# =============================================================================

@router.post("/{material_id}/grammar/{grammar_id}/toggle_mastered")
async def toggle_grammar_mastered(
    request: Request,
    material_id: int,
    grammar_id: int,
    db: Session = Depends(get_db),
):
    gp = db.query(GrammarPoint).filter(
        GrammarPoint.id == grammar_id,
        GrammarPoint.material_id == material_id,
    ).first()
    if not gp:
        return HTMLResponse("Grammar point not found", status_code=404)

    # Phase 5D-1: If toggling TO mastered AND grammar is a current cycle target,
    # attempt replacement transaction. Otherwise use existing simple toggle.
    from app.routes.study import (
        _is_current_cycle_target,
        _find_eligible_replacement_grammar,
        _execute_manual_mastered_replacement,
        _build_no_replacement_html,
        _build_replacement_html,
    )

    toggle_to_mastered = not gp.mastered
    is_target, cycle, position = _is_current_cycle_target(db, gp.id) if toggle_to_mastered else (False, None, None)

    if toggle_to_mastered and is_target and cycle:
        # ---- Current cycle target: attempt replacement ----
        replacement = _find_eligible_replacement_grammar(db, cycle, gp.id)
        if replacement is None:
            # No eligible replacement — reject the toggle, unchanged state
            return templates.TemplateResponse(
                request, "base.html",
                {
                    "content": _build_no_replacement_html(gp.point_name),
                },
                status_code=400,
            )

        # Execute the atomic replacement transaction
        _execute_manual_mastered_replacement(db, cycle, gp, replacement)
        pos_name: str = position or "grammar_a"

        # Return replacement confirmation
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            # AJAX: return minimal HTML fragment
            return HTMLResponse(
                f"<div class='flash-success'>"
                f"「{gp.point_name}」已标记掌握，已替换为「{replacement.point_name}」。</div>"
            )

        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": _build_replacement_html(
                    gp.point_name, replacement.point_name, pos_name
                ),
            },
        )

    # ---- Original simple toggle for non-target or toggle-to-not-mastered ----
    gp.mastered = not gp.mastered
    db.commit()

    # Phase 1b: if mastered was just set to True, cancel related pending cycle questions
    if gp.mastered:
        from app.routes.study import _cancel_mastered_cycle_questions
        _cancel_mastered_cycle_questions(db, gp.point_name, gp.id)

    # AJAX (inline JS fetch): return just the card fragment — no body swap, no scroll jump
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return templates.TemplateResponse(
            request, "partials/grammar_card.html",
            {"gp": gp},
        )

    return RedirectResponse(url=f"/materials/{material_id}", status_code=303)


# =============================================================================
# POST /materials/<id>/vocab/<vid>/toggle_mastered
# =============================================================================

@router.post("/{material_id}/vocab/{vocab_id}/toggle_mastered")
async def toggle_vocab_mastered(
    request: Request,
    material_id: int,
    vocab_id: int,
    db: Session = Depends(get_db),
):
    v = db.query(VocabItem).filter(
        VocabItem.id == vocab_id,
        VocabItem.material_id == material_id,
    ).first()
    if not v:
        return HTMLResponse("Vocab item not found", status_code=404)
    v.mastered = not v.mastered
    db.commit()

    # AJAX (inline JS fetch): return just the card fragment — no body swap, no scroll jump
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return templates.TemplateResponse(
            request, "partials/vocab_card.html",
            {"v": v},
        )

    return RedirectResponse(url=f"/materials/{material_id}", status_code=303)


# =============================================================================
# POST /materials/delete_selected — delete or archive selected materials
# =============================================================================

@router.post("/delete_selected")
async def delete_selected_materials(
    request: Request,
    material_ids: list[int] = Form([]),
    db: Session = Depends(get_db),
):
    """Delete or archive selected materials.

    Unused materials (no cycle_materials relation) are hard-deleted.
    Used materials are soft-deleted (archived_at set) to preserve history.
    """
    deduped_ids = sorted(set(mid for mid in material_ids if mid > 0))
    if not deduped_ids:
        return HTMLResponse(
            "请至少选择一份需要删除的素材。", status_code=400
        )

    hard_deleted = 0
    archived = 0

    for mid in deduped_ids:
        material = db.query(Material).filter(
            Material.id == mid, Material.archived_at.is_(None)
        ).first()
        if not material:
            continue

        # Check if this material is referenced by any study cycle
        # (via cycle_materials OR via its grammar points being used as A/B)
        cm_count = db.query(CycleMaterial).filter(
            CycleMaterial.material_id == mid
        ).count()

        gp_ids = [
            row[0] for row in db.query(GrammarPoint.id).filter(
                GrammarPoint.material_id == mid
            ).all()
        ]
        cycle_gp_count = 0
        if gp_ids:
            cycle_gp_count = db.query(StudyCycle).filter(
                StudyCycle.grammar_a_id.in_(gp_ids)
            ).count() + db.query(StudyCycle).filter(
                StudyCycle.grammar_b_id.in_(gp_ids)
            ).count()

        if cm_count == 0 and cycle_gp_count == 0:
            # Unused material: hard delete
            db.query(GrammarPoint).filter(
                GrammarPoint.material_id == mid
            ).delete()
            db.query(VocabItem).filter(
                VocabItem.material_id == mid
            ).delete()
            db.delete(material)
            hard_deleted += 1
        else:
            # Used material: archive (set archived_at)
            material.archived_at = datetime.datetime.utcnow()
            archived += 1

    db.commit()

    # Build result message
    if hard_deleted > 0 and archived > 0:
        msg = (
            f"已删除 {hard_deleted} 份未使用素材；"
            f"{archived} 份已有学习记录的素材已从列表隐藏并保留历史。"
        )
    elif hard_deleted > 0:
        msg = f"已删除 {hard_deleted} 份素材。"
    elif archived > 0:
        msg = "所选素材已有学习记录，已从素材库隐藏并保留历史。"
    else:
        msg = "没有需要处理的素材。"

    return templates.TemplateResponse(
        request, "base.html",
        {
            "content": (
                f"<div class='card flash' style='background:#d4edda;color:#155724;'>"
                f"{msg}</div>"
                "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
            )
        },
    )
