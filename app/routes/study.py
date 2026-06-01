"""Study-cycle routes — Day 2 runtime + Day 3 weak points, resume, module actions."""

import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Material,
    GrammarPoint,
    StudyCycle,
    CycleMaterial,
    QuestionAttempt,
    SessionState,
    WeakPoint,
    WeakPointEvent,
    UsageLog,
    TranslationErrorCandidate,
)
from app.schemas import TranslationExercise, MultipleChoiceQuestion, QuestionPayload, TranslationEvaluationV2
from app.agents.generator import (
    generate_explanation,
    generate_translation_exercises,
    generate_multiple_choice,
    generate_one_translation,
    generate_one_multiple_choice,
    evaluate_translation_answer_v2,
)
from app.llm import get_and_clear_usage, is_available

router = APIRouter(prefix="/study", tags=["study"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


MODULE_ORDER = ["grammar_a_translation", "grammar_b_translation", "multiple_choice"]
MODULE_LABELS = {
    "grammar_a_translation": "语法 A 翻译练习",
    "grammar_b_translation": "语法 B 翻译练习",
    "multiple_choice": "选择题练习",
}


def _get_or_create_session_state(db: Session) -> SessionState:
    """Get the singleton session state, creating it if needed."""
    state = db.query(SessionState).first()
    if not state:
        state = SessionState(
            current_cycle_id=None,
            current_module=None,
            current_question_index=0,
            updated_at=datetime.datetime.utcnow(),
        )
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _get_sorted_cycle_questions(db: Session, cycle_id: int) -> list[QuestionAttempt]:
    """Get all questions for a cycle, ordered by id."""
    return (
        db.query(QuestionAttempt)
        .filter(QuestionAttempt.cycle_id == cycle_id)
        .order_by(QuestionAttempt.id)
        .all()
    )


def _get_module_questions(all_qs: list[QuestionAttempt], module: str) -> list[QuestionAttempt]:
    """Filter questions by module type."""
    return [q for q in all_qs if q.module_type == module]


def _find_next_module(
    all_qs: list[QuestionAttempt], current_module: str | None
) -> str | None:
    """Find the next module after current_module that has any unanswered questions."""
    start = 0
    if current_module and current_module in MODULE_ORDER:
        start = MODULE_ORDER.index(current_module) + 1
    for mod in MODULE_ORDER[start:]:
        module_qs = _get_module_questions(all_qs, mod)
        pending = [q for q in module_qs if q.status in ("pending", "planned", "generation_failed")]
        if pending:
            return mod
    return None


def _first_pending_question_index(all_qs: list[QuestionAttempt]) -> int | None:
    """Return the index (in all_qs) of the first pending/planned question."""
    for i, q in enumerate(all_qs):
        if q.status in ("pending", "planned", "generation_failed"):
            return i
    return None


def _compute_cycle_completion(db: Session, cycle: StudyCycle) -> dict:
    """Compute completion stats for a cycle. Also sets completed_at + is_valid_completion if fully done."""
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    total = len(all_qs)
    answered = sum(1 for q in all_qs if q.status == "answered")
    skipped_count = sum(1 for q in all_qs if q.status == "skipped")
    studied_count = sum(1 for q in all_qs if q.status == "studied")
    cancelled_mastered_count = sum(1 for q in all_qs if q.status == "cancelled_mastered")
    pending = sum(1 for q in all_qs if q.status in ("pending", "planned", "generating", "generation_failed"))
    correct = sum(1 for q in all_qs if q.is_correct)

    # Module-level analysis
    module_statuses = {}
    had_skipped = False
    for mod in MODULE_ORDER:
        mod_qs = _get_module_questions(all_qs, mod)
        mod_pending = sum(1 for q in mod_qs if q.status in ("pending", "planned", "generating", "generation_failed"))
        mod_skipped = sum(1 for q in mod_qs if q.status == "skipped")
        if mod_skipped > 0:
            had_skipped = True
        module_statuses[mod] = {
            "total": len(mod_qs),
            "pending": mod_pending,
            "skipped": mod_skipped,
            "studied": sum(1 for q in mod_qs if q.status == "studied"),
            "cancelled_mastered": sum(1 for q in mod_qs if q.status == "cancelled_mastered"),
            "answered": sum(1 for q in mod_qs if q.status == "answered"),
            "done": mod_pending == 0,
        }

    is_done = pending == 0
    if is_done and not cycle.completed_at:
        cycle.completed_at = datetime.datetime.utcnow()
        cycle.is_valid_completion = is_done and not had_skipped
        db.commit()

    return {
        "total": total,
        "answered": answered,
        "skipped": skipped_count,
        "studied": studied_count,
        "cancelled_mastered": cancelled_mastered_count,
        "pending": pending,
        "correct": correct,
        "accuracy": round(correct / answered * 100, 1) if answered > 0 else 0,
        "is_done": is_done,
        "had_skipped_module": had_skipped,
        "is_valid_completion": cycle.is_valid_completion if is_done else False,
        "module_statuses": module_statuses,
    }


def _record_weak_point(
    db: Session, grammar_point_name: str,
    cycle_id: int | None = None,
    source_type: str | None = None,
    attempt_id: int | None = None,
    candidate_id: int | None = None,
) -> str:
    """Record or increment a grammar weak point from a wrong answer.

    Returns event_type: 'created' or 'hit_existing'.
    When cycle_id and source_type are provided, also records a
    WeakPointEvent for provenance-based per-cycle statistics.
    """
    wp = (
        db.query(WeakPoint)
        .filter(
            WeakPoint.point_type == "grammar",
            WeakPoint.point_reference == grammar_point_name,
        )
        .first()
    )
    if wp:
        wp.error_count = (wp.error_count or 0) + 1
        wp.last_error_at = datetime.datetime.utcnow()
        if wp.error_count >= 2:
            wp.is_active = True
        event_type = "hit_existing"
    else:
        wp = WeakPoint(
            point_type="grammar",
            point_reference=grammar_point_name,
            error_count=1,
            last_error_at=datetime.datetime.utcnow(),
            is_active=False,
        )
        db.add(wp)
        db.commit()
        db.refresh(wp)
        event_type = "created"

    # Record provenance event if cycle context is available
    if cycle_id is not None and source_type is not None:
        ev = WeakPointEvent(
            cycle_id=cycle_id,
            weak_point_id=wp.id,
            source_type=source_type,
            event_type=event_type,
            source_attempt_id=attempt_id,
            source_candidate_id=candidate_id,
            created_at=datetime.datetime.utcnow(),
        )
        db.add(ev)
    db.commit()
    return event_type


def _persist_usage_logs(db: Session, cycle_id: int | None = None) -> None:
    """Flush accumulated usage records from llm.py into the DB."""
    records = get_and_clear_usage()
    for r in records:
        log = UsageLog(
            call_purpose=r["purpose"],
            cycle_id=cycle_id,
            prompt_tokens=r["prompt_tokens"],
            completion_tokens=r["completion_tokens"],
            total_tokens=r["total_tokens"],
            called_at=datetime.datetime.utcnow(),
        )
        db.add(log)
    if records:
        db.commit()


def _cancel_mastered_cycle_questions(
    db: Session, grammar_point_name: str, grammar_point_id: int
) -> None:
    """Mark pending questions targeting a newly-mastered grammar as cancelled_mastered.

    Called when the user toggles mastered=True during an active study cycle.
    Answered questions are left unchanged. Pending translation and MC questions
    whose associated grammar_point matches the newly-mastered grammar are cancelled.
    Advances the session if the current question was cancelled.
    """
    from app.models import SessionState

    state = db.query(SessionState).first()
    if not state or not state.current_cycle_id:
        return

    all_qs = _get_sorted_cycle_questions(db, state.current_cycle_id)
    grammar_point_name_lower = grammar_point_name.lower().strip().lstrip("〜")
    changed = False

    for i, q in enumerate(all_qs):
        if q.status not in ("pending", "planned", "generating", "generation_failed"):
            continue
        payload = q.question_payload_json or {}
        q_gp_name = (payload.get("grammar_point") or "").lower().strip().lstrip("〜")
        # Check grammar_point field, target_grammar_id, or grammar_point_id match
        if (q_gp_name == grammar_point_name_lower
            or q_gp_name == str(grammar_point_id)
            or (not q_gp_name and q.target_grammar_id == grammar_point_id)):
            q.status = "cancelled_mastered"
            q.answered_at = datetime.datetime.utcnow()
            changed = True

    if changed:
        db.commit()

    # If the current question was cancelled, advance to next pending
    if state.current_question_index < len(all_qs):
        current_q = all_qs[state.current_question_index]
        if current_q.status == "cancelled_mastered":
            # Find next pending
            next_pending = _first_pending_question_index(all_qs)
            if next_pending is not None:
                state.current_question_index = next_pending
                state.current_module = all_qs[next_pending].module_type
            else:
                state.current_question_index = len(all_qs)
                # Complete the cycle
                cycle = db.query(StudyCycle).filter(
                    StudyCycle.id == state.current_cycle_id
                ).first()
                if cycle:
                    _compute_cycle_completion(db, cycle)
            state.updated_at = datetime.datetime.utcnow()
            db.commit()


def _validate_mc_against_mastered(
    mc_questions: list,
    mastered_names: set[str],
) -> list[bool]:
    """Validate that no generated MC question contains mastered grammar.

    Checks prompt text, all choices (A/B/C/D), and grammar_point field.
    Returns a list of booleans (True=valid, False=contains mastered grammar).
    """
    normalized_mastered = {
        name.lower().strip().lstrip("〜")
        for name in mastered_names
    }
    results = []
    for q in mc_questions:
        texts_to_check = [
            getattr(q, "prompt", "") or "",
            getattr(q, "A", "") or "",
            getattr(q, "B", "") or "",
            getattr(q, "C", "") or "",
            getattr(q, "D", "") or "",
            getattr(q, "grammar_point", "") or "",
        ]
        combined = " ".join(texts_to_check).lower()
        # Check if any mastered grammar name appears as a substring in the content
        contaminated = any(
            name in combined
            for name in normalized_mastered
            if name  # skip empty strings
        )
        results.append(not contaminated)
    return results


def _normalize_grammar_name(name: str) -> str:
    """Normalize a grammar expression for cross-material comparison.

    Strips whitespace and leading ``〜``, lowercases.
    Returns a clean key for deduplication and mastered-exclusion lookups.
    """
    return name.lower().strip().lstrip("〜")


def _build_combined_grammar_pool(
    db: Session, material_ids: list[int]
) -> tuple[list[GrammarPoint], set[str]]:
    """Build a unified eligible grammar pool from multiple materials.

    Returns (eligible_candidates, globally_mastered_names) where:
    - eligible_candidates: GrammarPoint rows suitable for A/B/review selection,
      deduplicated by normalized name. The first-occurring row (by id) is the
      representative for each unique grammar expression.
    - globally_mastered_names: normalized names of ALL mastered grammar
      expressions across ALL materials (cross-material scope).
    """
    if not material_ids:
        return [], set()

    # ---- Step 1: Collect all grammar and mastered info from selected materials ----
    raw_points = (
        db.query(GrammarPoint)
        .filter(GrammarPoint.material_id.in_(material_ids))
        .order_by(GrammarPoint.id)  # deterministic: first by id
        .all()
    )

    # ---- Step 2: Build the global mastered set from ALL materials ----
    all_mastered_gp = (
        db.query(GrammarPoint)
        .filter(GrammarPoint.mastered == True)
        .all()
    )
    globally_mastered_names: set[str] = set()
    for gp in all_mastered_gp:
        globally_mastered_names.add(_normalize_grammar_name(gp.point_name))

    # ---- Step 3: Deduplicate by normalized name, exclude mastered ----
    seen: set[str] = set()
    eligible: list[GrammarPoint] = []
    for gp in raw_points:
        key = _normalize_grammar_name(gp.point_name)
        if key in seen:
            continue
        seen.add(key)
        if key in globally_mastered_names:
            # Not eligible — exclude (even if this particular row has mastered=False)
            continue
        eligible.append(gp)

    return eligible, globally_mastered_names


def _generate_slot_content(
    db: Session,
    slot: QuestionAttempt,
    grammar_a: GrammarPoint,
    grammar_b: GrammarPoint,
    review_points: list[GrammarPoint],
    globally_mastered_names: set[str],
) -> bool:
    """Generate content for one planned/generation_failed slot.

    Returns True on success (slot status → pending).
    Sets generation_failed + error message on failure.
    """
    payload = slot.question_payload_json or {}
    gtype = payload.get("type", "translation")
    module = slot.module_type

    if module in ("grammar_a_translation", "grammar_b_translation"):
        target_gp_id = slot.target_grammar_id or (grammar_a.id if "grammar_a" in module else grammar_b.id)
        target_gp = (grammar_a if target_gp_id == grammar_a.id
                     else grammar_b if target_gp_id == grammar_b.id
                     else db.query(GrammarPoint).filter(GrammarPoint.id == target_gp_id).first())
        if not target_gp:
            slot.status = "generation_failed"
            slot.generation_error = "Target grammar not found"
            db.commit()
            return False

        result = generate_one_translation(target_gp)
        if result is None:
            slot.status = "generation_failed"
            slot.generation_error = "Failed to generate translation question"
            db.commit()
            return False

        qp = QuestionPayload(
            type="translation",
            prompt_zh=result.prompt_zh,
            reference_answer_ja=result.reference_answer_ja,
            grading_notes=result.grading_notes,
            grammar_point=result.grammar_point,
        ).model_dump()
        slot.question_payload_json = qp
        slot.correct_answer = result.reference_answer_ja
        slot.status = "pending"
        db.commit()

    else:  # multiple_choice
        # Determine slot index within MC module
        all_qs = _get_sorted_cycle_questions(db, slot.cycle_id)
        mc_qs = [q for q in all_qs if q.module_type == "multiple_choice"]
        slot_idx = next((i for i, q in enumerate(mc_qs) if q.id == slot.id), 0)

        result = generate_one_multiple_choice(
            grammar_a, grammar_b, review_points, slot_idx
        )
        if result is None:
            slot.status = "generation_failed"
            slot.generation_error = "Failed to generate multiple-choice question"
            db.commit()
            return False

        mc_name = _normalize_grammar_name(result.grammar_point)
        mnames_clean = {_normalize_grammar_name(n) for n in globally_mastered_names}
        texts = [result.prompt, result.A, result.B, result.C, result.D]
        contaminated = any(
            mname in " ".join(texts).lower()
            for mname in mnames_clean if mname
            for t2 in [texts]
        )
        if contaminated:
            slot.status = "generation_failed"
            slot.generation_error = "Generated content contains mastered grammar"
            db.commit()
            return False

        qp = QuestionPayload(
            type="multiple_choice",
            choices={"A": result.A, "B": result.B, "C": result.C, "D": result.D},
            prompt=result.prompt,
            expected=result.expected,
            grammar_point=result.grammar_point,
            question_role=result.question_role,
        ).model_dump()
        slot.question_payload_json = qp
        slot.correct_answer = result.expected
        slot.status = "pending"
        db.commit()

    return True


def _get_cycle_grammar_points(
    db: Session, cycle: StudyCycle
) -> tuple[GrammarPoint | None, GrammarPoint | None, list[GrammarPoint]]:
    """Get grammar_a, grammar_b, and eligible review points for a cycle."""
    ga = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_a_id).first()
    gb = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_b_id).first()

    # Build review list from the cycle's materials
    cms = db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == cycle.id
    ).all()
    material_ids = [cm.material_id for cm in cms]
    eligible_candidates, _ = _build_combined_grammar_pool(db, material_ids)
    review_points = [
        gp for gp in eligible_candidates
        if gp.id not in (cycle.grammar_a_id, cycle.grammar_b_id)
    ]

    return ga, gb, review_points


def _build_answer_feedback_html(
    is_correct: bool, expected: str, user_answer: str, explanation: str | None = None
) -> str:
    """Build HTML feedback for an answer."""
    icon = "✅" if is_correct else "❌"
    result_text = "正确！" if is_correct else "不正确"
    html = (
        f'<div class="card">'
        f'<h3>{icon} {result_text}</h3>'
        f'<p><strong>你的答案：</strong>{user_answer}</p>'
        f'<p><strong>参考答案：</strong>{expected}</p>'
    )
    if explanation:
        html += f'<p style="color: #555; background: #f9f9f9; padding: 0.5rem; border-radius: 4px;">{explanation}</p>'
    html += '</div>'
    return html


# =============================================================================
# POST /study/start_cycle — start a new study cycle for a material
# =============================================================================

@router.post("/start_cycle")
async def start_cycle(
    request: Request,
    material_id: int = Form(None),
    material_ids: list[int] = Form([]),
    db: Session = Depends(get_db),
):
    """Start a new study cycle from one or more materials.

    Legacy single-material callers can use ``material_id``.
    Multi-material callers pass ``material_ids`` as repeated form values.
    """
    # ---- Normalise input: combine single + multi into a deduplicated list ----
    ids: set[int] = set()
    if material_id is not None:
        ids.add(material_id)
    for mid in material_ids:
        if mid > 0:
            ids.add(mid)
    sorted_ids = sorted(ids)
    if not sorted_ids:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<div class='card flash-error'>"
                    "<strong>无法开始学习：</strong>请至少选择一份素材后再开始学习。</div>"
                    "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
                )
            },
            status_code=400,
        )

    # ---- Verify every selected material exists ----
    materials = (
        db.query(Material)
        .filter(Material.id.in_(sorted_ids))
        .all()
    )
    if len(materials) != len(sorted_ids):
        return HTMLResponse("One or more materials not found", status_code=404)

    # Reject archived materials
    archived_ids = [m.id for m in materials if m.archived_at is not None]
    if archived_ids:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<div class='card flash-error'>"
                    "<strong>无法开始学习：</strong>选中的素材包含已被删除或隐藏的素材，"
                    "无法用于新的学习循环。</div>"
                    "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
                )
            },
            status_code=400,
        )

    # ---- Build combined eligible grammar pool --------------------------------
    eligible_candidates, globally_mastered_names = _build_combined_grammar_pool(db, sorted_ids)

    if len(eligible_candidates) < 2:
        total_gp = db.query(GrammarPoint).filter(
            GrammarPoint.material_id.in_(sorted_ids)
        ).count()
        if len(eligible_candidates) == 1:
            msg = (
                "<div class='card flash-error'>"
                "<strong>无法开始学习：</strong>所选素材需要至少 2 个未掌握的语法点才能开始学习。"
                f"共 {total_gp} 个语法点，仅剩 1 个未掌握，仍需至少 2 个。</div>"
                "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
            )
        else:
            msg = (
                "<div class='card flash-error'>"
                "<strong>无法开始学习：</strong>所选素材中没有未掌握的可学习语法点。"
                f"共 {total_gp} 个语法点，均已掌握。</div>"
                "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
            )
        return templates.TemplateResponse(
            request, "base.html",
            {"content": msg},
            status_code=400,
        )

    # ---- Select grammar A and B from eligible pool deterministically ---------
    n2_points = [gp for gp in eligible_candidates if gp.difficulty_level == "N2"]
    if len(n2_points) >= 2:
        grammar_a = n2_points[0]
        grammar_b = n2_points[1]
    else:
        grammar_a = eligible_candidates[0]
        grammar_b = eligible_candidates[1]

    # ---- Remaining eligible points for review --------------------------------
    review_points = [
        gp for gp in eligible_candidates
        if gp.id not in (grammar_a.id, grammar_b.id)
    ]

    # ---- Prioritize active weak points (but only from the surviving pool) ----
    active_weak_points = (
        db.query(WeakPoint)
        .filter(WeakPoint.point_type == "grammar", WeakPoint.is_active == True)
        .all()
    )
    weak_point_names = {wp.point_reference for wp in active_weak_points}
    weak_review = [gp for gp in review_points if gp.point_name in weak_point_names]
    other_review = [gp for gp in review_points if gp.point_name not in weak_point_names]
    prioritized_review = weak_review + other_review

    # ---- Create study cycle with 19 planned slots (lazy generation) --------
    cycle = StudyCycle(
        started_at=datetime.datetime.utcnow(),
        completed_at=None,
        grammar_a_id=grammar_a.id,
        grammar_b_id=grammar_b.id,
        is_valid_completion=False,
    )
    db.add(cycle)
    db.commit()
    db.refresh(cycle)

    # ---- Persist cycle_materials association rows ---------------------------
    for mid in sorted_ids:
        db.add(CycleMaterial(cycle_id=cycle.id, material_id=mid))
    db.commit()

    # ---- Create 19 planned question slots (lazy generation) -----------------
    # Slots 1-5: Grammar A translation
    for i in range(5):
        slot = QuestionAttempt(
            cycle_id=cycle.id,
            module_type="grammar_a_translation",
            question_payload_json={"type": "translation"},
            correct_answer="",
            is_correct=False,
            status="planned",
            target_grammar_id=grammar_a.id,
        )
        db.add(slot)

    # Slots 6-10: Grammar B translation
    for i in range(5):
        slot = QuestionAttempt(
            cycle_id=cycle.id,
            module_type="grammar_b_translation",
            question_payload_json={"type": "translation"},
            correct_answer="",
            is_correct=False,
            status="planned",
            target_grammar_id=grammar_b.id,
        )
        db.add(slot)

    # Slots 11-19: Multiple choice
    for i in range(9):
        slot = QuestionAttempt(
            cycle_id=cycle.id,
            module_type="multiple_choice",
            question_payload_json={"type": "multiple_choice"},
            correct_answer="",
            is_correct=False,
            status="planned",
        )
        db.add(slot)
    db.commit()

    # ---- Phase 5A: Do not synchronously generate Q1.
    #          All 19 slots remain planned. /study/current shows loading +
    #          async POST /study/generate_module for the initial translation module.
    #          This eliminates the synchronous LLM wait during start_cycle and
    #          lets the batch-generation loading UI handle first-question visibility.
    # ---- Initialize session state -------------------------------------------
    state = _get_or_create_session_state(db)
    state.current_cycle_id = cycle.id
    state.current_module = "grammar_a_translation"
    state.current_question_index = 0
    state.updated_at = datetime.datetime.utcnow()
    db.commit()

    return RedirectResponse(url="/study/current", status_code=303)


# =============================================================================
# GET /study — home / resume entry point
# =============================================================================

@router.get("", response_class=HTMLResponse)
async def study_home(
    request: Request,
    db: Session = Depends(get_db),
):
    """Show study home: resume if unfinished, or prompt to start new."""
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id:
        # No active cycle — show start page
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<h1>📚 学习</h1>"
                    "<p>还没有开始学习。请先选择一个素材开始。</p>"
                    "<a href='/materials' class='btn btn-primary'>去素材列表</a>"
                )
            },
        )

    cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
    if not cycle:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<h1>📚 学习</h1>"
                    "<p>会话状态异常，请重新开始。</p>"
                    "<a href='/materials' class='btn btn-primary'>去素材列表</a>"
                )
            },
        )

    all_qs = _get_sorted_cycle_questions(db, cycle.id)

    # Check if cycle is fully completed
    if cycle.completed_at:
        # Completed — show results, do NOT resume
        stats = _compute_cycle_completion(db, cycle)
        return templates.TemplateResponse(
            request, "study_result.html",
            {
                "cycle": cycle,
                "total": stats["total"],
                "answered": stats["answered"],
                "correct": stats["correct"],
                "skipped": stats["skipped"],
                "studied": stats["studied"],
                "cancelled_mastered": stats["cancelled_mastered"],
                "accuracy": stats["accuracy"],
                "questions": all_qs,
                "in_progress": False,
                "had_skipped_module": stats["had_skipped_module"],
                "is_valid_completion": stats["is_valid_completion"],
                "module_statuses": stats["module_statuses"],
                "final_score": _compute_final_cycle_score(db, cycle),
            },
        )

    # Unfinished — redirect to current question
    return RedirectResponse(url="/study/current", status_code=303)


# =============================================================================
# GET /study/current — show current unanswered question
# =============================================================================

@router.get("/current", response_class=HTMLResponse)
async def current_question(
    request: Request,
    db: Session = Depends(get_db),
):
    """Render the current unanswered question (or results if completed)."""
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<h1>📚 学习</h1>"
                    "<p>还没有开始学习。请先选择一个素材开始学习。</p>"
                    "<a href='/materials' class='btn btn-primary'>去素材列表</a>"
                )
            },
        )

    cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
    if not cycle:
        return HTMLResponse("Cycle not found", status_code=404)

    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    total = len(all_qs)

    # If cycle already completed (completed_at set), show results
    if cycle.completed_at:
        stats = _compute_cycle_completion(db, cycle)
        return templates.TemplateResponse(
            request, "study_result.html",
            {
                "cycle": cycle,
                "total": stats["total"],
                "answered": stats["answered"],
                "correct": stats["correct"],
                "skipped": stats["skipped"],
                "studied": stats["studied"],
                "cancelled_mastered": stats["cancelled_mastered"],
                "accuracy": stats["accuracy"],
                "questions": all_qs,
                "in_progress": False,
                "had_skipped_module": stats["had_skipped_module"],
                "is_valid_completion": stats["is_valid_completion"],
                "module_statuses": stats["module_statuses"],
            },
        )

    # --- Day 3: Resume support — find first pending question ---
    first_pending_idx = _first_pending_question_index(all_qs)
    if first_pending_idx is None:
        # No pending questions — all done or all skipped/studied
        # Trigger completion
        stats = _compute_cycle_completion(db, cycle)
        return templates.TemplateResponse(
            request, "study_result.html",
            {
                "cycle": cycle,
                "total": stats["total"],
                "answered": stats["answered"],
                "correct": stats["correct"],
                "accuracy": stats["accuracy"],
                "questions": all_qs,
                "in_progress": False,
                "had_skipped_module": stats["had_skipped_module"],
                "is_valid_completion": stats["is_valid_completion"],
                "module_statuses": stats["module_statuses"],
                "final_score": _compute_final_cycle_score(db, cycle),
            },
        )

    # Fix session state to point to the actual first pending question
    state.current_question_index = first_pending_idx
    current_q = all_qs[first_pending_idx]
    state.current_module = current_q.module_type
    state.updated_at = datetime.datetime.utcnow()
    db.commit()

    # Phase 4A: Review gate — if about to enter MC but candidates pending, redirect
    if current_q.module_type == "multiple_choice" and _check_review_gate(db, cycle.id):
        return RedirectResponse(url="/study/review_candidates", status_code=303)

    # Phase 5A: Translation module entry — if current question not ready, show loading UI
    if current_q.module_type in ("grammar_a_translation", "grammar_b_translation"):
        if current_q.status not in ("pending",):
            progress_label = MODULE_LABELS.get(state.current_module, "")
            return templates.TemplateResponse(
                request, "study.html",
                {
                    "loading": True,
                    "module_name": progress_label,
                    "question": {
                        "index": state.current_question_index + 1,
                        "total": total,
                        "answered": sum(1 for q in all_qs if q.status == "answered"),
                        "correct": sum(1 for q in all_qs if q.is_correct),
                    },
                },
            )

    # Phase 5B: MC generation failure — show retry feedback instead of blank question
    if current_q.module_type == "multiple_choice" and current_q.status == "generation_failed":
        return templates.TemplateResponse(
            request, "study.html",
            {
                "mc_retry": True,
                "mc_failed": True,
                "module_name": MODULE_LABELS.get(state.current_module, ""),
                "question": {
                    "index": state.current_question_index + 1,
                    "total": total,
                    "answered": sum(1 for q in all_qs if q.status == "answered"),
                    "correct": sum(1 for q in all_qs if q.is_correct),
                },
            },
        )

    current_q = all_qs[state.current_question_index]
    answered_count = sum(1 for q in all_qs if q.status == "answered")
    correct_count = sum(1 for q in all_qs if q.is_correct)

    # Build the view payload (strip hidden answers)
    payload = current_q.question_payload_json
    view_data = {
        "question_id": current_q.id,
        "module_type": current_q.module_type,
        "index": state.current_question_index + 1,
        "total": total,
        "answered": answered_count,
        "correct": correct_count,
    }

    if payload.get("type") == "translation":
        view_data["prompt_zh"] = payload.get("prompt_zh", "")
        view_data["grammar_point"] = payload.get("grammar_point", "")
        view_data["mode"] = "translation"
    else:
        view_data["prompt"] = payload.get("prompt", "")
        view_data["choices"] = payload.get("choices", {})
        view_data["grammar_point"] = payload.get("grammar_point", "")
        view_data["question_role"] = payload.get("question_role", "")
        view_data["mode"] = "multiple_choice"

    # Show grammar explanation when entering a new module
    grammar_a = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_a_id).first()
    grammar_b = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_b_id).first()

    explanation_html = None
    if state.current_question_index == 0 and state.current_module == "grammar_a_translation":
        exp_a = generate_explanation(grammar_a) if grammar_a else None
        if exp_a:
            examples_html = "".join(f"<li>{s}</li>" for s in (exp_a.example_sentences or []))
            explanation_html = (
                f"<div class='card' style='background:#e8f4fd;'>"
                f"<h3>📖 语法 A：{exp_a.point_name}</h3>"
                f"<p><strong>含义：</strong>{exp_a.meaning_zh}</p>"
                f"<p><strong>用法：</strong>{exp_a.usage_notes_zh}</p>"
                f"<ul>{examples_html}</ul>"
                f"</div>"
            )
    elif state.current_question_index == 5 and state.current_module == "grammar_b_translation":
        exp_b = generate_explanation(grammar_b) if grammar_b else None
        if exp_b:
            examples_html = "".join(f"<li>{s}</li>" for s in (exp_b.example_sentences or []))
            explanation_html = (
                f"<div class='card' style='background:#e8f4fd;'>"
                f"<h3>📖 语法 B：{exp_b.point_name}</h3>"
                f"<p><strong>含义：</strong>{exp_b.meaning_zh}</p>"
                f"<p><strong>用法：</strong>{exp_b.usage_notes_zh}</p>"
                f"<ul>{examples_html}</ul>"
                f"</div>"
            )

    # --- Determine if module is in a skip-able/studied-able state ---
    mod_qs = _get_module_questions(all_qs, state.current_module)
    mod_pending = sum(1 for q in mod_qs if q.status == "pending")
    can_skip_or_study = mod_pending > 0

    return templates.TemplateResponse(
        request, "study.html",
        {
            "question": view_data,
            "explanation_html": explanation_html,
            "module_name": MODULE_LABELS.get(state.current_module, state.current_module or ""),
            "can_skip_or_study": can_skip_or_study,
            "current_module": state.current_module,
            # Grammar point info for "mark as mastered" during study
            "grammar_a_info": {
                "id": grammar_a.id,
                "name": grammar_a.point_name,
                "mastered": grammar_a.mastered,
                "material_id": grammar_a.material_id,
            } if grammar_a else None,
            "grammar_b_info": {
                "id": grammar_b.id,
                "name": grammar_b.point_name,
                "mastered": grammar_b.mastered,
                "material_id": grammar_b.material_id,
            } if grammar_b else None,
        },
    )


# =============================================================================
# POST /study/answer — submit answer for current question
# =============================================================================

@router.post("/answer")
async def submit_answer(
    request: Request,
    answer: str = Form(...),
    db: Session = Depends(get_db),
):
    """Submit an answer for the current question."""
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id:
        return HTMLResponse("No active cycle", status_code=400)

    all_qs = _get_sorted_cycle_questions(db, state.current_cycle_id)

    if state.current_question_index >= len(all_qs):
        return HTMLResponse("All questions already answered", status_code=400)

    current_q = all_qs[state.current_question_index]
    if current_q.answered_at is not None:
        return HTMLResponse("This question was already answered", status_code=400)
    if current_q.status != "pending":
        return HTMLResponse("This question was skipped or studied", status_code=400)

    payload = current_q.question_payload_json
    module_type = current_q.module_type
    is_correct = False

    if module_type in ("grammar_a_translation", "grammar_b_translation"):
        # Translation grading via DeepSeek (Phase 4A: heart scoring)
        ex = TranslationExercise(
            prompt_zh=payload.get("prompt_zh", ""),
            reference_answer_ja=payload.get("reference_answer_ja", ""),
            grammar_point=payload.get("grammar_point", ""),
            grading_notes=payload.get("grading_notes", ""),
        )
        evaluation = evaluate_translation_answer_v2(ex, answer)

        if evaluation is None:
            # Evaluation failed — do not advance, show error
            return templates.TemplateResponse(
                request, "base.html",
                {
                    "content": (
                        "<div class='card flash-error'>"
                        "<strong>评分失败：</strong>无法评估您的答案，请重试。</div>"
                        "<a href='/study/current' class='btn btn-primary'>重试</a>"
                    )
                },
            )

        # Phase 4D: validate LLM contract (target_grammar_correct + score_hearts pair)
        score_hearts = evaluation.score_hearts
        score_hearts = max(0, min(10, score_hearts))
        tgc = getattr(evaluation, "target_grammar_correct", None)

        # Validate pair: true ↔ 6-10, false ↔ 0-5, missing/non-bool → failure
        _valid_pair = False
        if isinstance(tgc, bool):
            if tgc is True and 6 <= score_hearts <= 10:
                _valid_pair = True
            elif tgc is False and 0 <= score_hearts <= 5:
                _valid_pair = True

        if not _valid_pair:
            # Contradictory/malformed evaluation — do not persist anything
            # Show retryable feedback using existing safe failure pattern
            return templates.TemplateResponse(
                request, "base.html",
                {
                    "content": (
                        "<div class='card flash-error'>"
                        "<strong>评分结果不一致，请重试。</strong>"
                        "<p style='margin-top: 0.5rem; color: #555;'>"
                        "评估返回了矛盾的结果。请重新提交答案。</p></div>"
                        "<a href='/study/current' class='btn btn-primary'>重试</a>"
                    )
                },
            )

        # Phase 4D: assess pass/fail using new >=6 / <=5 threshold
        is_correct = score_hearts >= 6

        current_q.user_answer = answer
        current_q.score_hearts = score_hearts
        current_q.target_grammar_correct = tgc
        current_q.is_correct = is_correct
        current_q.answered_at = datetime.datetime.utcnow()
        current_q.status = "answered"
        current_q.correct_answer = evaluation.corrected_answer_ja
        db.commit()

        # Phase 4D: auto weak point for target grammar only if target_grammar_correct is false and score <= 5
        grammar_point_name = payload.get("grammar_point", "")
        if grammar_point_name and not tgc and score_hearts <= 5:
            _record_weak_point(
                db, grammar_point_name,
                cycle_id=state.current_cycle_id,
                source_type="translation_low_score_target_grammar",
                attempt_id=current_q.id,
            )

        # Phase 4A: insert additional-error candidates (if any)
        if evaluation.additional_errors:
            _insert_error_candidates(
                db, state.current_cycle_id, current_q.id,
                evaluation.additional_errors,
            )

        # Build heart-display feedback
        feedback = evaluation.feedback_zh
        hearts_display = "❤️" * score_hearts + "🤍" * (10 - score_hearts)
        result_text = "正确！" if is_correct else "不正确"
        feedback_html = (
            f'<div class="card">'
            f'<h3>{hearts_display} ({score_hearts}/10) — {result_text}</h3>'
            f'<p><strong>你的答案：</strong>{answer}</p>'
            f'<p><strong>参考答案：</strong>{evaluation.corrected_answer_ja}</p>'
            f'<p style="color: #555; background: #f9f9f9; padding: 0.5rem; border-radius: 4px;">{feedback}</p>'
            f'</div>'
        )

    else:
        # Multiple choice — deterministic Python grading
        normalized = answer.strip().upper()
        number_map = {"1": "A", "2": "B", "3": "C", "4": "D"}
        if normalized in number_map:
            normalized = number_map[normalized]

        expected = current_q.correct_answer.upper()
        is_correct = normalized == expected

        current_q.user_answer = answer
        current_q.is_correct = is_correct
        current_q.answered_at = datetime.datetime.utcnow()
        current_q.status = "answered"
        db.commit()

        choices = payload.get("choices", {})
        correct_text = choices.get(expected, expected)
        result_text = f"✅ 正确！" if is_correct else f"❌ 不正确。正确答案是 {expected}：{correct_text}"
        feedback_html = _build_answer_feedback_html(
            is_correct, f"{expected}：{correct_text}", answer
        )

    # --- Phase 4A: Record weak point for wrong answers (MC only) ---
    # Translation weak points are handled inside the translation branch above.
    if module_type == "multiple_choice":
        grammar_point_name = payload.get("grammar_point", "")
        if grammar_point_name and not is_correct:
            _record_weak_point(
                db, grammar_point_name,
                cycle_id=state.current_cycle_id,
                source_type="choice_wrong_answer",
                attempt_id=current_q.id,
            )

    # --- Persist usage for evaluation call ---
    _persist_usage_logs(db, state.current_cycle_id)

    # Advance session state to next unanswered/pending question
    next_index = state.current_question_index + 1
    if next_index < len(all_qs):
        next_q = all_qs[next_index]
        state.current_question_index = next_index
        if next_q.module_type != module_type:
            state.current_module = next_q.module_type
        state.updated_at = datetime.datetime.utcnow()
    else:
        # Past the last question — check if any pending remain
        first_pending = _first_pending_question_index(all_qs)
        if first_pending is not None:
            state.current_question_index = first_pending
            state.current_module = all_qs[first_pending].module_type
        else:
            state.current_question_index = len(all_qs)  # past last
            # Mark cycle completed
            cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
            if cycle:
                _compute_cycle_completion(db, cycle)
        state.updated_at = datetime.datetime.utcnow()

    db.commit()

    # ---- Phase 3: Ensure next question is available before returning --------
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id
    ).first()
    if cycle:
        _ensure_next_question_generated(db, state, cycle)

    # ---- Phase 4A: Review gate check — redirect if candidates pending after Q10 ----
    if cycle and _check_review_gate(db, cycle.id):
        return RedirectResponse(url="/study/review_candidates", status_code=303)

    # Check if finished
    remaining_pending = _first_pending_question_index(all_qs)
    if remaining_pending is None:
        return RedirectResponse(url="/study/current", status_code=303)

    # Show feedback and link to next question
    return templates.TemplateResponse(
        request, "base.html",
        {
            "content": (
                f"{feedback_html}"
                "<a href='/study/current' class='btn btn-primary'>下一题</a>"
            )
        },
    )


# =============================================================================
# POST /study/skip_module — skip the current module
# =============================================================================

@router.post("/skip_module")
async def skip_current_module(
    request: Request,
    db: Session = Depends(get_db),
):
    """Skip all unanswered questions in the current module."""
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id or not state.current_module:
        return HTMLResponse("No active cycle or module", status_code=400)

    all_qs = _get_sorted_cycle_questions(db, state.current_cycle_id)
    mod_qs = _get_module_questions(all_qs, state.current_module)
    unresolved_in_mod = [q for q in mod_qs if q.status in ("pending", "planned", "generating", "generation_failed")]

    if not unresolved_in_mod:
        return RedirectResponse(url="/study/current", status_code=303)

    # Mark all unresolved questions in this module as skipped
    for q in unresolved_in_mod:
        q.status = "skipped"
        q.answered_at = datetime.datetime.utcnow()
    db.commit()

    # Advance to next module with pending questions
    next_mod = _find_next_module(all_qs, state.current_module)
    if next_mod is not None:
        state.current_module = next_mod
        first_idx = _first_pending_question_index(all_qs)
        state.current_question_index = first_idx if first_idx is not None else len(all_qs)
    else:
        # No more modules with pending questions — all done
        first_pending = _first_pending_question_index(all_qs)
        if first_pending is not None:
            state.current_question_index = first_pending
        else:
            state.current_question_index = len(all_qs)
            cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
            if cycle:
                _compute_cycle_completion(db, cycle)
    state.updated_at = datetime.datetime.utcnow()
    db.commit()

    return RedirectResponse(url="/study/current", status_code=303)


# =============================================================================
# POST /study/mark_studied — mark current module as already studied
# =============================================================================

@router.post("/mark_studied")
async def mark_current_module_studied(
    request: Request,
    db: Session = Depends(get_db),
):
    """Mark all unanswered questions in the current module as studied."""
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id or not state.current_module:
        return HTMLResponse("No active cycle or module", status_code=400)

    all_qs = _get_sorted_cycle_questions(db, state.current_cycle_id)
    mod_qs = _get_module_questions(all_qs, state.current_module)
    unresolved_in_mod = [q for q in mod_qs if q.status in ("pending", "planned", "generating", "generation_failed")]

    if not unresolved_in_mod:
        return RedirectResponse(url="/study/current", status_code=303)

    # Mark all unresolved questions in this module as studied
    for q in unresolved_in_mod:
        q.status = "studied"
        q.answered_at = datetime.datetime.utcnow()
        # Do NOT set user_answer or is_correct — studied unanswered questions
        # must not create weak points
    db.commit()

    # Advance to next module with pending questions
    next_mod = _find_next_module(all_qs, state.current_module)
    if next_mod is not None:
        state.current_module = next_mod
        first_idx = _first_pending_question_index(all_qs)
        state.current_question_index = first_idx if first_idx is not None else len(all_qs)
    else:
        first_pending = _first_pending_question_index(all_qs)
        if first_pending is not None:
            state.current_question_index = first_pending
        else:
            state.current_question_index = len(all_qs)
            cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
            if cycle:
                _compute_cycle_completion(db, cycle)
    state.updated_at = datetime.datetime.utcnow()
    db.commit()

    return RedirectResponse(url="/study/current", status_code=303)


# =============================================================================
# POST /study/prefetch_next — generate the next needed question (idempotent)
# =============================================================================

@router.post("/prefetch_next")
async def prefetch_next(
    request: Request,
    db: Session = Depends(get_db),
):
    """Generate at most one next planned question for the current cycle."""
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id:
        return {"ok": False, "reason": "no_active_cycle"}

    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id
    ).first()
    if not cycle or cycle.completed_at:
        return {"ok": False, "reason": "cycle_completed"}

    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    # Find the first PLANNED question (skip already-pending ones)
    planned_idx = None
    for i, q in enumerate(all_qs):
        if q.status == "planned":
            planned_idx = i
            break
    if planned_idx is None:
        return {"ok": False, "reason": "no_planned"}

    next_q = all_qs[planned_idx]
    if next_q.status != "planned":
        return {"ok": False, "reason": f"not_planned_{next_q.status}"}

    # Atomic claim: planned → generating
    rows = db.query(QuestionAttempt).filter(
        QuestionAttempt.id == next_q.id,
        QuestionAttempt.status == "planned",
    ).update({"status": "generating", "generation_started_at": datetime.datetime.utcnow()})
    if rows == 0:
        return {"ok": False, "reason": "claim_failed"}
    db.commit()

    ga, gb, review_points = _get_cycle_grammar_points(db, cycle)
    material_ids = [cm.material_id for cm in db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == cycle.id).all()]
    _, globally_mastered_names = _build_combined_grammar_pool(db, material_ids)

    success = _generate_slot_content(db, next_q, ga, gb, review_points, globally_mastered_names)
    return {"ok": success, "reason": "generated" if success else "generation_failed"}


def _ensure_next_question_generated(
    db: Session, state: SessionState, cycle: StudyCycle
) -> bool:
    """Ensure the next unresolved question has content, generating sync if needed."""
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    # Find first non-pending unresolved question
    next_idx = None
    for i, q in enumerate(all_qs):
        if q.status in ("planned", "generation_failed", "generating"):
            next_idx = i
            break
    if next_idx is None:
        return True  # No more questions needing generation

    next_q = all_qs[next_idx]
    # Phase 5A: Translation slots are generated asynchronously via
    # POST /study/generate_module. /study/current shows loading + retry UI.
    if next_q.module_type in ("grammar_a_translation", "grammar_b_translation"):
        return True  # Don't block — async flow handles this
    if next_q.status in ("planned", "generation_failed"):
        ga, gb, review_points = _get_cycle_grammar_points(db, cycle)
        material_ids = [cm.material_id for cm in db.query(CycleMaterial).filter(
            CycleMaterial.cycle_id == cycle.id).all()]
        _, globally_mastered_names = _build_combined_grammar_pool(db, material_ids)
        return _generate_slot_content(db, next_q, ga, gb, review_points, globally_mastered_names)

    if next_q.status == "generating":
        # Brief wait for prefetch to complete
        import time
        for _ in range(10):
            time.sleep(0.1)
            db.refresh(next_q)
            if next_q.status == "pending":
                return True
            if next_q.status != "generating":
                break
        next_q.status = "generation_failed"
        next_q.generation_error = "Generation timed out"
        db.commit()
        return False

    return False


# =============================================================================
# Phase 4C: GET /study/progress — Mermaid progress page
# =============================================================================

def _escape_mermaid(text: str) -> str:
    """Escape text for safe embedding in Mermaid node labels.
    
    Replaces characters that could break Mermaid syntax or inject directives.
    """
    if not text:
        return ""
    text = text.replace("\\", "＼")  # Backslash → full-width
    text = text.replace('"', "'").replace('"', "'")
    text = text.replace('[', '(').replace(']', ')')
    text = text.replace('{', '(').replace('}', ')')
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    text = text.replace('|', '/')
    text = text.replace('`', "'")
    text = text.replace('#', '№')   # Mermaid comments
    text = text.replace('-->', '─→')  # Mermaid arrow syntax
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text


def _build_current_cycle_mermaid(db: Session, cycle: StudyCycle) -> str:
    """Build a Mermaid flowchart string for the current in-progress cycle."""
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    state = db.query(SessionState).first()
    
    # Determine material names
    cms = db.query(CycleMaterial).filter(CycleMaterial.cycle_id == cycle.id).all()
    material_names = []
    for cm in cms:
        mat = db.query(Material).filter(Material.id == cm.material_id).first()
        if mat:
            material_names.append(_escape_mermaid(mat.filename))
    materials_label = f"素材: {', '.join(material_names[:3])}" if material_names else "素材"
    
    # Target grammars
    ga = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_a_id).first()
    gb = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_b_id).first()
    ga_name = _escape_mermaid(ga.point_name) if ga else "语法A"
    gb_name = _escape_mermaid(gb.point_name) if gb else "语法B"
    
    # Question statuses
    ta_qs = [q for q in all_qs if q.module_type == "grammar_a_translation"]
    tb_qs = [q for q in all_qs if q.module_type == "grammar_b_translation"]
    mc_qs = [q for q in all_qs if q.module_type == "multiple_choice"]
    
    ta_done = all(q.status == "answered" for q in ta_qs) if ta_qs else True
    tb_done = all(q.status == "answered" for q in tb_qs) if tb_qs else True
    mc_done = all(q.status == "answered" for q in mc_qs) if mc_qs else True
    cycle_done = cycle.completed_at is not None
    
    # Candidate state
    from app.models import TranslationErrorCandidate
    pending = db.query(TranslationErrorCandidate).filter(
        TranslationErrorCandidate.cycle_id == cycle.id,
        TranslationErrorCandidate.status == "pending",
    ).count()
    
    review_state = "未开始"
    if pending > 0:
        review_state = "待处理"
    elif ta_done and tb_done:
        # Check if any candidates were ever created
        total_candidates = db.query(TranslationErrorCandidate).filter(
            TranslationErrorCandidate.cycle_id == cycle.id
        ).count()
        if total_candidates > 0:
            review_state = "已处理"
        else:
            review_state = "无需处理"
    
    # Current module for "进行中" state
    current_mod = state.current_module if state else None
    
    # Build node states
    def node_state(mod_type: str, is_done: bool) -> str:
        if is_done:
            return "已完成"
        if current_mod == mod_type:
            return "进行中"
        return "未开始"
    
    ta_state = "已完成" if ta_done else (node_state("grammar_a_translation", False))
    tb_state = "已完成" if tb_done else (node_state("grammar_b_translation", False))
    mc_state = "已完成" if mc_done else (node_state("multiple_choice", False))
    
    # Mermaid flowchart (using escaped labels)
    lines = ["flowchart LR"]
    lines.append(f'    M[\"📁 {materials_label}\"] --> GA["📝 {ga_name} ({ta_state})"]')
    lines.append(f'    GA --> GB["📝 {gb_name} ({tb_state})"]')
    lines.append(f'    GB --> REV["🔍 附加错误审查 ({_escape_mermaid(review_state)})"]')
    lines.append(f'    REV --> MC["🎯 选择题模块 ({_escape_mermaid(mc_state)})"]')
    lines.append(f'    MC --> END["🏁 Cycle 完成"]')
    
    # Style nodes
    lines.append(f'    style M fill:#e8f4fd,stroke:#3498db')
    if ta_done:
        lines.append(f'    style GA fill:#d4edda,stroke:#27ae60')
    elif current_mod == "grammar_a_translation":
        lines.append(f'    style GA fill:#fff3cd,stroke:#f39c12')
    if tb_done:
        lines.append(f'    style GB fill:#d4edda,stroke:#27ae60')
    elif current_mod == "grammar_b_translation":
        lines.append(f'    style GB fill:#fff3cd,stroke:#f39c12')
    if review_state in ("已处理", "无需处理"):
        lines.append(f'    style REV fill:#d4edda,stroke:#27ae60')
    elif review_state == "待处理":
        lines.append(f'    style REV fill:#f8d7da,stroke:#e74c3c')
    if mc_done:
        lines.append(f'    style MC fill:#d4edda,stroke:#27ae60')
    elif current_mod == "multiple_choice":
        lines.append(f'    style MC fill:#fff3cd,stroke:#f39c12')
    if cycle_done:
        lines.append(f'    style END fill:#d4edda,stroke:#27ae60')
    
    return "\n".join(lines)


def _get_historical_cycle_summaries(db: Session) -> list[dict]:
    """Get summary data for all completed cycles (excluding current unfinished)."""
    state = db.query(SessionState).first()
    current_cycle_id = state.current_cycle_id if state else None
    
    cycles = db.query(StudyCycle).filter(
        StudyCycle.completed_at.isnot(None)
    ).order_by(StudyCycle.id.desc()).all()
    
    summaries = []
    for cycle in cycles:
        if cycle.id == current_cycle_id and not cycle.completed_at:
            continue  # Don't show current unfinished cycle in historical list
        
        cms = db.query(CycleMaterial).filter(CycleMaterial.cycle_id == cycle.id).all()
        mats = []
        for cm in cms:
            m = db.query(Material).filter(Material.id == cm.material_id).first()
            if m:
                mats.append(m.filename)
        
        ga = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_a_id).first()
        gb = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_b_id).first()
        
        # Final score
        score = _compute_final_cycle_score(db, cycle)
        has_hearts = False
        if score:
            has_hearts = any(
                q.score_hearts is not None
                for q in _get_sorted_cycle_questions(db, cycle.id)
                if q.status == "answered"
            )
        
        # Weak-point counts: use WeakPointEvent provenance for reliable cycles
        all_qs = _get_sorted_cycle_questions(db, cycle.id)
        
        # Query WeakPointEvent records for this cycle
        events = db.query(WeakPointEvent).filter(
            WeakPointEvent.cycle_id == cycle.id
        ).all()
        
        if events:
            # Reliable provenance exists
            new_count = sum(1 for e in events if e.event_type == "created")
            re_hit_count = sum(1 for e in events if e.event_type == "hit_existing")
        else:
            # Legacy cycle without provenance
            new_count = None
            re_hit_count = None
        
        summary = {
            "id": cycle.id,
            "completed_at": cycle.completed_at.strftime("%Y-%m-%d %H:%M") if cycle.completed_at else "-",
            "materials": ", ".join(mats) if mats else "-",
            "grammar_a": ga.point_name if ga else "-",
            "grammar_b": gb.point_name if gb else "-",
            "has_hearts": has_hearts,
            "score_display": (
                f"{score['final_score_percent']}%" if score else
                ("旧版记录" if not has_hearts else "-")
            ),
            "new_wp": new_count if new_count is not None else "—",
            "re_hit_wp": re_hit_count if re_hit_count is not None else "—",
            "is_valid_completion": cycle.is_valid_completion,
            "is_legacy_stats": new_count is None,
        }
        summaries.append(summary)
    
    return summaries


@router.get("/progress", response_class=HTMLResponse)
async def study_progress_mermaid(
    request: Request,
    db: Session = Depends(get_db),
):
    """Display learning progress with Mermaid flowchart and historical summaries."""
    state = _get_or_create_session_state(db)
    current_cycle = None
    current_cycle_mermaid = None
    supporting_text = None
    historical_summaries = []
    final_score = None
    
    if state and state.current_cycle_id:
        current_cycle = db.query(StudyCycle).filter(
            StudyCycle.id == state.current_cycle_id
        ).first()
        if current_cycle and not current_cycle.completed_at:
            current_cycle_mermaid = _build_current_cycle_mermaid(db, current_cycle)
            
            # Supporting text
            ga = db.query(GrammarPoint).filter(GrammarPoint.id == current_cycle.grammar_a_id).first()
            gb = db.query(GrammarPoint).filter(GrammarPoint.id == current_cycle.grammar_b_id).first()
            cms = db.query(CycleMaterial).filter(
                CycleMaterial.cycle_id == current_cycle.id
            ).all()
            mat_names = []
            for cm in cms:
                m = db.query(Material).filter(Material.id == cm.material_id).first()
                if m:
                    mat_names.append(m.filename)
            
            from app.models import TranslationErrorCandidate
            pending_count = db.query(TranslationErrorCandidate).filter(
                TranslationErrorCandidate.cycle_id == current_cycle.id,
                TranslationErrorCandidate.status == "pending",
            ).count()
            
            supporting_text = {
                "materials": ", ".join(mat_names) if mat_names else "-",
                "grammar_a": ga.point_name if ga else "-",
                "grammar_b": gb.point_name if gb else "-",
                "current_module": state.current_module or "-",
                "pending_candidates": pending_count,
            }
    
    # Historical summaries (all completed cycles)
    historical_summaries = _get_historical_cycle_summaries(db)
    
    return templates.TemplateResponse(
        request, "progress.html",
        {
            "current_cycle": current_cycle,
            "current_cycle_mermaid": current_cycle_mermaid,
            "supporting_text": supporting_text,
            "historical_summaries": historical_summaries,
        },
    )


# =============================================================================
# Phase 4A: Helper functions
# =============================================================================


def _insert_error_candidates(
    db: Session,
    cycle_id: int,
    attempt_id: int,
    additional_errors: list,
) -> None:
    """Insert additional-error candidates, merging duplicates by error_rule_key."""
    for err in additional_errors:
        rule_key = getattr(err, "error_rule_key", "") or err.get("error_rule_key", "")
        if not rule_key:
            continue
        existing = (
            db.query(TranslationErrorCandidate)
            .filter(
                TranslationErrorCandidate.cycle_id == cycle_id,
                TranslationErrorCandidate.status == "pending",
                TranslationErrorCandidate.error_rule_key == rule_key,
            )
            .first()
        )
        if existing:
            existing.occurrence_count = (existing.occurrence_count or 1) + 1
        else:
            candidate = TranslationErrorCandidate(
                cycle_id=cycle_id,
                source_attempt_id=attempt_id,
                error_type=getattr(err, "error_type", "") or err.get("error_type", "other"),
                error_rule_key=rule_key,
                original_fragment=getattr(err, "original_fragment", "") or err.get("original_fragment", ""),
                corrected_fragment=getattr(err, "corrected_fragment", "") or err.get("corrected_fragment", ""),
                description=getattr(err, "description", "") or err.get("description", ""),
                status="pending",
                occurrence_count=1,
                created_at=datetime.datetime.utcnow(),
            )
            db.add(candidate)
    db.commit()


def _get_pending_candidates(db: Session, cycle_id: int):
    return (
        db.query(TranslationErrorCandidate)
        .filter(
            TranslationErrorCandidate.cycle_id == cycle_id,
            TranslationErrorCandidate.status == "pending",
        )
        .order_by(TranslationErrorCandidate.id)
        .all()
    )


def _needs_candidate_review(db: Session, cycle_id: int) -> bool:
    return (
        db.query(TranslationErrorCandidate)
        .filter(
            TranslationErrorCandidate.cycle_id == cycle_id,
            TranslationErrorCandidate.status == "pending",
        )
        .count() > 0
    )


def _check_review_gate(db: Session, cycle_id: int) -> bool:
    """Check if both translation modules done with pending candidates.
    
    Returns True if all 10 translation questions are answered,
    NO MC question has been answered yet, and pending candidates exist.
    """
    all_qs = _get_sorted_cycle_questions(db, cycle_id)
    ta_qs = [q for q in all_qs if q.module_type == "grammar_a_translation"]
    tb_qs = [q for q in all_qs if q.module_type == "grammar_b_translation"]
    if not all(q.status == "answered" for q in ta_qs + tb_qs):
        return False
    mc_qs = [q for q in all_qs if q.module_type == "multiple_choice"]
    # Gate is active only if NO MC question has been answered yet
    if any(q.status == "answered" for q in mc_qs):
        return False
    return _needs_candidate_review(db, cycle_id)


@router.get("/review_candidates", response_class=HTMLResponse)
async def review_candidates_page(request: Request, db: Session = Depends(get_db)):
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id:
        return RedirectResponse(url="/materials")
    candidates = _get_pending_candidates(db, state.current_cycle_id)
    for c in candidates:
        prior = (
            db.query(TranslationErrorCandidate)
            .filter(
                TranslationErrorCandidate.cycle_id != state.current_cycle_id,
                TranslationErrorCandidate.error_rule_key == c.error_rule_key,
                TranslationErrorCandidate.status == "ignored",
            )
            .first()
        )
        c._has_prior_ignored = prior is not None
    if not candidates:
        return RedirectResponse(url="/study/current", status_code=303)
    return templates.TemplateResponse(
        request, "review_candidates.html", {"candidates": candidates},
    )


# =============================================================================
# Phase 5A: Duplicate guard for translation prompts
# =============================================================================

def _is_exact_duplicate(
    module_type: str, prompt_zh: str, db: Session, cycle_id: int,
) -> bool:
    """Check if *prompt_zh* already exists in another same-module, same-cycle slot.

    Only compares exact string equality within the same 5-question translation
    module. Does not compare across modules, cycles, or use semantic similarity.
    """
    if not prompt_zh:
        return False
    all_qs = _get_sorted_cycle_questions(db, cycle_id)
    for q in all_qs:
        if q.module_type != module_type:
            continue
        if q.status not in ("pending", "answered"):
            continue
        payload = q.question_payload_json or {}
        existing = (payload.get("prompt_zh") or "").strip()
        if existing == prompt_zh.strip():
            return True
    return False


# =============================================================================
# Phase 5A: Batch-generate missing translation questions for current module
# =============================================================================

def _batch_generate_module_translations(
    db: Session, cycle: StudyCycle, module_type: str,
) -> dict:
    """Async-friendly batch generation of missing translation slots.

    Returns dict with:
      ok: bool       — True if at least one answerable slot was produced
      generated: int — number of slots successfully made pending
      total: int     — number of eligible (missing) slots found
      error: str|None
    """
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    # Eligible = slots in this module not yet valid
    eligible = [
        q for q in all_qs
        if q.module_type == module_type
        and q.status in ("planned", "generation_failed")
    ]
    if not eligible:
        return {"ok": True, "generated": 0, "total": 0, "error": None}

    # Determine target grammar point
    ga = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_a_id).first()
    gb = db.query(GrammarPoint).filter(GrammarPoint.id == cycle.grammar_b_id).first()
    target_gp = ga if "grammar_a" in module_type else gb
    if not target_gp:
        for q in eligible:
            q.status = "generation_failed"
            q.generation_error = "Target grammar not found"
        db.commit()
        return {"ok": False, "generated": 0, "total": len(eligible),
                "error": "Target grammar not found"}

    # Call LLM for exactly the number of missing slots.
    # Use the module-level reference (patched by test mocks).
    import app.routes.study as _rs
    results = []
    for _ in range(len(eligible)):
        try:
            ex = _rs.generate_one_translation(target_gp)
        except Exception:
            break
        if ex:
            results.append(ex)
        else:
            break

    if not results:
        # Complete failure
        for q in eligible:
            q.status = "generation_failed"
            q.generation_error = "Batch generation failed"
        db.commit()
        return {"ok": False, "generated": 0, "total": len(eligible),
                "error": "Generation returned no results"}

    from app.schemas import QuestionPayload
    accepted = 0
    result_iter = iter(results)

    for slot in eligible:
        try:
            ex = next(result_iter)
        except StopIteration:
            # Fewer results than eligible slots — remaining stay planned
            break

        # Exact duplicate check
        if _is_exact_duplicate(module_type, ex.prompt_zh, db, cycle.id):
            slot.generation_error = f"Duplicate prompt: {ex.prompt_zh[:60]}"
            # Slot stays planned — eligible for retry
            continue

        # Write valid question
        qp = QuestionPayload(
            type="translation",
            prompt_zh=ex.prompt_zh,
            reference_answer_ja=ex.reference_answer_ja,
            grading_notes=ex.grading_notes,
            grammar_point=ex.grammar_point,
        ).model_dump()
        slot.question_payload_json = qp
        slot.correct_answer = ex.reference_answer_ja
        slot.status = "pending"
        slot.generation_error = None
        db.commit()
        accepted += 1

    db.commit()
    return {"ok": accepted > 0, "generated": accepted, "total": len(eligible),
            "error": None}


# =============================================================================
# Phase 5A: POST /study/generate_module — async batch generation endpoint
# =============================================================================

@router.post("/generate_module")
async def generate_module(
    request: Request,
    db: Session = Depends(get_db),
):
    """Generate all missing translation questions for the current module.

    Called asynchronously from the loading page.
    Returns JSON: {ok, generated, total, error}.
    """
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id or not state.current_module:
        return {"ok": False, "generated": 0, "total": 0,
                "error": "no_active_cycle"}

    module = state.current_module
    if module not in ("grammar_a_translation", "grammar_b_translation"):
        return {"ok": False, "generated": 0, "total": 0,
                "error": "not_a_translation_module"}

    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id
    ).first()
    if not cycle or cycle.completed_at:
        return {"ok": False, "generated": 0, "total": 0,
                "error": "cycle_not_active"}

    result = _batch_generate_module_translations(db, cycle, module)
    return result


# =============================================================================
# Phase 5B: POST /study/regenerate_mc — retry current failed MC slot only
# =============================================================================

@router.post("/regenerate_mc")
async def regenerate_current_mc(
    request: Request,
    db: Session = Depends(get_db),
):
    """Regenerate only the current failed multiple-choice slot.

    Only accepts slots in generation_failed status.
    Returns JSON: {ok, error?}.
    """
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id or state.current_module != "multiple_choice":
        return {"ok": False, "error": "not_in_mc_module"}

    all_qs = _get_sorted_cycle_questions(db, state.current_cycle_id)
    next_idx = state.current_question_index
    if next_idx >= len(all_qs):
        return {"ok": False, "error": "index_out_of_range"}

    current_q = all_qs[next_idx]
    if current_q.module_type != "multiple_choice":
        return {"ok": False, "error": "not_current_mc_slot"}
    if current_q.status not in ("generation_failed",):
        return {"ok": False, "error": "not_retryable"}
    if current_q.answered_at is not None:
        return {"ok": False, "error": "already_answered"}

    # Atomic claim: generation_failed → generating (prevents concurrent retry)
    rows = db.query(QuestionAttempt).filter(
        QuestionAttempt.id == current_q.id,
        QuestionAttempt.status == "generation_failed",
    ).update({"status": "generating", "generation_started_at": datetime.datetime.utcnow()})
    if rows == 0:
        return {"ok": False, "error": "claim_failed"}
    db.commit()

    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id
    ).first()
    if not cycle or cycle.completed_at:
        # Restore generation_failed if cycle became invalid
        db.query(QuestionAttempt).filter(
            QuestionAttempt.id == current_q.id
        ).update({"status": "generation_failed"})
        db.commit()
        return {"ok": False, "error": "cycle_not_active"}

    ga, gb, review_points = _get_cycle_grammar_points(db, cycle)
    material_ids = [cm.material_id for cm in db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == cycle.id).all()]
    _, globally_mastered_names = _build_combined_grammar_pool(db, material_ids)

    # _generate_slot_content transitions to pending (ok) or generation_failed (fail)
    success = _generate_slot_content(db, current_q, ga, gb, review_points, globally_mastered_names)
    return {"ok": success}


@router.post("/candidate/{candidate_id}/add")
async def add_candidate_weak_point(
    request: Request, candidate_id: int, db: Session = Depends(get_db),
):
    candidate = db.query(TranslationErrorCandidate).filter(
        TranslationErrorCandidate.id == candidate_id
    ).first()
    if not candidate or candidate.status != "pending":
        return HTMLResponse("Not found or already processed", status_code=400)
    candidate.status = "added"
    candidate.decided_at = datetime.datetime.utcnow()
    _record_weak_point(
        db, candidate.description,
        cycle_id=candidate.cycle_id,
        source_type="translation_candidate_confirmed",
        attempt_id=candidate.source_attempt_id,
        candidate_id=candidate.id,
    )
    db.commit()
    if not _get_pending_candidates(db, candidate.cycle_id):
        return RedirectResponse(url="/study/current", status_code=303)
    return RedirectResponse(url="/study/review_candidates", status_code=303)


@router.post("/candidate/{candidate_id}/ignore")
async def ignore_candidate_route(
    request: Request, candidate_id: int, db: Session = Depends(get_db),
):
    candidate = db.query(TranslationErrorCandidate).filter(
        TranslationErrorCandidate.id == candidate_id
    ).first()
    if not candidate or candidate.status != "pending":
        return HTMLResponse("Not found or already processed", status_code=400)
    candidate.status = "ignored"
    candidate.decided_at = datetime.datetime.utcnow()
    db.commit()
    if not _get_pending_candidates(db, candidate.cycle_id):
        return RedirectResponse(url="/study/current", status_code=303)
    return RedirectResponse(url="/study/review_candidates", status_code=303)


def _compute_final_cycle_score(db: Session, cycle: StudyCycle) -> dict | None:
    """Compute final cycle score (P4A-LOCK-020).
    
    For new Phase 4A cycles: translation = score_hearts/10*100, choice correct=100, wrong=0.
    For pre-Phase-4A cycles (any answered translation with NULL score_hearts):
    does NOT compute a heart-based score — returns None so legacy statistics display.
    Excluded: skipped, cancelled_mastered, planned, generating, generation_failed.
    """
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    scored = []
    excluded = 0
    has_new_hearts = False
    
    for q in all_qs:
        if q.status == "answered":
            if q.module_type in ("grammar_a_translation", "grammar_b_translation"):
                if q.score_hearts is not None:
                    # Phase 4A heart-scored translation
                    scored.append(q.score_hearts / 10.0 * 100)
                    has_new_hearts = True
                else:
                    # Historical translation with only is_correct — skip from heart-based score
                    # (P4A-LOCK-023: no backfill of artificial heart values)
                    excluded += 1
            else:
                # Multiple choice — always counted
                scored.append(100.0 if q.is_correct else 0.0)
        else:
            excluded += 1
    
    # If no Phase 4A heart-scored translations exist in this cycle, don't fabricate a score
    if not scored or (not has_new_hearts and any(
        q.module_type in ("grammar_a_translation", "grammar_b_translation")
        for q in all_qs if q.status == "answered" and q.score_hearts is None
    )):
        # Check: if all translations are historical (NULL hearts), return None
        # so legacy accuracy display is used instead
        all_translations_historical = all(
            q.score_hearts is None
            for q in all_qs
            if q.status == "answered" and q.module_type in ("grammar_a_translation", "grammar_b_translation")
        )
        if all_translations_historical:
            return None
    
    if not scored:
        return None
    
    return {
        "final_score_percent": round(sum(scored) / len(scored), 1),
        "scored_count": len(scored),
        "excluded_count": excluded,
    }
