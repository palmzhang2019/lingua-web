"""Study-cycle routes — Day 2 runtime: start cycle, answer questions, view progress."""

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
    QuestionAttempt,
    SessionState,
    WeakPoint,
)
from app.schemas import TranslationExercise, MultipleChoiceQuestion, QuestionPayload
from app.agents.generator import (
    generate_explanation,
    generate_translation_exercises,
    generate_multiple_choice,
    evaluate_translation_answer,
)

router = APIRouter(prefix="/study", tags=["study"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


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


# =============================================================================
# POST /study/start_cycle — start a new study cycle for a material
# =============================================================================

@router.post("/start_cycle")
async def start_cycle(
    request: Request,
    material_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Start a new study cycle for the given material."""
    material = db.query(Material).filter(Material.id == material_id).first()
    if not material:
        return HTMLResponse("Material not found", status_code=404)

    # Load grammar points ordered by id (prefer N2 first)
    grammar_points = (
        db.query(GrammarPoint)
        .filter(GrammarPoint.material_id == material_id)
        .order_by(GrammarPoint.id)
        .all()
    )

    if len(grammar_points) < 2:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<div class='card flash-error'>"
                    "<strong>无法开始学习：</strong>该素材至少需要 2 个语法点才能开始学习。"
                    f"当前只有 {len(grammar_points)} 个语法点。</div>"
                    "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
                )
            },
            status_code=400,
        )

    # Select grammar A and B deterministically: first two N2 points by id,
    # fallback to earliest available
    n2_points = [gp for gp in grammar_points if gp.difficulty_level == "N2"]
    if len(n2_points) >= 2:
        grammar_a = n2_points[0]
        grammar_b = n2_points[1]
    else:
        grammar_a = grammar_points[0]
        grammar_b = grammar_points[1]

    # Remaining points for review questions
    review_points = [
        gp for gp in grammar_points
        if gp.id not in (grammar_a.id, grammar_b.id)
    ]

    # ---------- Generate explanations ----------
    explanation_a = generate_explanation(grammar_a)
    explanation_b = generate_explanation(grammar_b)

    if not explanation_a or not explanation_b:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<div class='card flash-error'>"
                    "<strong>生成语法解释失败：</strong>DeepSeek 无法生成语法解释。"
                    "请检查 API 配置后重试。</div>"
                    "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
                )
            },
            status_code=500,
        )

    # ---------- Generate translation exercises ----------
    trans_a = generate_translation_exercises(grammar_a, 5)
    trans_b = generate_translation_exercises(grammar_b, 5)

    if len(trans_a) < 5 or len(trans_b) < 5:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<div class='card flash-error'>"
                    "<strong>生成翻译题失败：</strong>DeepSeek 无法生成完整的翻译练习题。"
                    f"语法A生成了 {len(trans_a)} 题，语法B生成了 {len(trans_b)} 题。</div>"
                    "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
                )
            },
            status_code=500,
        )

    # ---------- Generate multiple-choice questions ----------
    mc_questions = generate_multiple_choice(grammar_a, grammar_b, review_points)
    if len(mc_questions) < 9:
        return templates.TemplateResponse(
            request, "base.html",
            {
                "content": (
                    "<div class='card flash-error'>"
                    "<strong>生成选择题失败：</strong>DeepSeek 无法生成完整的选择题。"
                    f"只生成了 {len(mc_questions)} 题。</div>"
                    "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
                )
            },
            status_code=500,
        )

    # ---------- Create study cycle ----------
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

    # ---------- Persist explanations as payloads for module intros ----------
    # Store explanations in question_payload_json of placeholder rows for module info
    # We'll store A explanation, B explanation, and MC info as metadata on the cycle

    # ---------- Persist 19 question attempts ----------
    question_index = 0

    # Questions 1-5: Grammar A translation
    for i, ex in enumerate(trans_a):
        payload = QuestionPayload(
            type="translation",
            prompt_zh=ex.prompt_zh,
            reference_answer_ja=ex.reference_answer_ja,
            grading_notes=ex.grading_notes,
            grammar_point=ex.grammar_point,
        ).model_dump()
        qa = QuestionAttempt(
            cycle_id=cycle.id,
            module_type="grammar_a_translation",
            question_payload_json=payload,
            user_answer=None,
            correct_answer=ex.reference_answer_ja,
            is_correct=False,
            answered_at=None,
        )
        db.add(qa)

    # Questions 6-10: Grammar B translation
    for i, ex in enumerate(trans_b):
        payload = QuestionPayload(
            type="translation",
            prompt_zh=ex.prompt_zh,
            reference_answer_ja=ex.reference_answer_ja,
            grading_notes=ex.grading_notes,
            grammar_point=ex.grammar_point,
        ).model_dump()
        qa = QuestionAttempt(
            cycle_id=cycle.id,
            module_type="grammar_b_translation",
            question_payload_json=payload,
            user_answer=None,
            correct_answer=ex.reference_answer_ja,
            is_correct=False,
            answered_at=None,
        )
        db.add(qa)

    # Questions 11-19: Multiple choice
    for i, mc in enumerate(mc_questions):
        payload = QuestionPayload(
            type="multiple_choice",
            choices={"A": mc.A, "B": mc.B, "C": mc.C, "D": mc.D},
            prompt=mc.prompt,
            expected=mc.expected,
            grammar_point=mc.grammar_point,
            question_role=mc.question_role,
        ).model_dump()
        qa = QuestionAttempt(
            cycle_id=cycle.id,
            module_type="multiple_choice",
            question_payload_json=payload,
            user_answer=None,
            correct_answer=mc.expected,
            is_correct=False,
            answered_at=None,
        )
        db.add(qa)

    db.commit()

    # ---------- Initialize session state ----------
    state = _get_or_create_session_state(db)
    state.current_cycle_id = cycle.id
    state.current_module = "grammar_a_translation"
    state.current_question_index = 0
    state.updated_at = datetime.datetime.utcnow()
    db.commit()

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
            {"content": "<p>还没有开始学习。请先选择一个素材开始学习。</p><a href='/materials' class='btn btn-primary'>去素材列表</a>"},
        )

    cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
    if not cycle:
        return HTMLResponse("Cycle not found", status_code=404)

    # Count total and answered
    all_qs = (
        db.query(QuestionAttempt)
        .filter(QuestionAttempt.cycle_id == cycle.id)
        .order_by(QuestionAttempt.id)
        .all()
    )
    total = len(all_qs)
    answered = sum(1 for q in all_qs if q.answered_at is not None)
    correct = sum(1 for q in all_qs if q.is_correct)

    # If all answered, show results
    if answered >= total:
        return templates.TemplateResponse(
            request, "study_result.html",
            {
                "cycle": cycle,
                "total": total,
                "answered": answered,
                "correct": correct,
                "accuracy": round(correct / total * 100, 1) if total > 0 else 0,
                "questions": all_qs,
            },
        )

    # Find the current unanswered question
    current_q = all_qs[state.current_question_index] if state.current_question_index < total else None
    if not current_q:
        return HTMLResponse("No current question found", status_code=404)

    # Build the view payload (strip hidden answers)
    payload = current_q.question_payload_json
    view_data = {
        "question_id": current_q.id,
        "module_type": current_q.module_type,
        "index": state.current_question_index + 1,
        "total": total,
        "answered": answered,
        "correct": correct,
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

    return templates.TemplateResponse(
        request, "study.html",
        {
            "question": view_data,
            "explanation_html": explanation_html,
            "module_name": {
                "grammar_a_translation": "语法 A 翻译练习",
                "grammar_b_translation": "语法 B 翻译练习",
                "multiple_choice": "选择题练习",
            }.get(state.current_module, state.current_module or ""),
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

    all_qs = (
        db.query(QuestionAttempt)
        .filter(QuestionAttempt.cycle_id == state.current_cycle_id)
        .order_by(QuestionAttempt.id)
        .all()
    )

    if state.current_question_index >= len(all_qs):
        return HTMLResponse("All questions already answered", status_code=400)

    current_q = all_qs[state.current_question_index]
    if current_q.answered_at is not None:
        return HTMLResponse("This question was already answered", status_code=400)

    payload = current_q.question_payload_json
    module_type = current_q.module_type

    if module_type in ("grammar_a_translation", "grammar_b_translation"):
        # Translation grading via DeepSeek
        ex = TranslationExercise(
            prompt_zh=payload.get("prompt_zh", ""),
            reference_answer_ja=payload.get("reference_answer_ja", ""),
            grammar_point=payload.get("grammar_point", ""),
            grading_notes=payload.get("grading_notes", ""),
        )
        evaluation = evaluate_translation_answer(ex, answer)

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

        current_q.user_answer = answer
        current_q.is_correct = evaluation.is_correct
        current_q.answered_at = datetime.datetime.utcnow()
        current_q.correct_answer = evaluation.corrected_answer_ja
        db.commit()

        feedback = evaluation.feedback_zh

    else:
        # Multiple choice — deterministic Python grading
        normalized = answer.strip().upper()
        # Accept 1/2/3/4 → A/B/C/D
        number_map = {"1": "A", "2": "B", "3": "C", "4": "D"}
        if normalized in number_map:
            normalized = number_map[normalized]

        expected = current_q.correct_answer.upper()
        is_correct = normalized == expected

        current_q.user_answer = answer
        current_q.is_correct = is_correct
        current_q.answered_at = datetime.datetime.utcnow()
        db.commit()

        # Build feedback
        choices = payload.get("choices", {})
        correct_text = choices.get(expected, expected)
        feedback = (
            f"{'✅ 正确！' if is_correct else '❌ 不正确。'}"
            f" 正确答案是 {expected}：{correct_text}"
        )

    # Advance session state to next unanswered question
    next_index = state.current_question_index + 1
    if next_index < len(all_qs):
        next_q = all_qs[next_index]
        state.current_question_index = next_index
        if next_q.module_type != module_type:
            state.current_module = next_q.module_type
        state.updated_at = datetime.datetime.utcnow()
    else:
        # All questions completed
        state.current_question_index = next_index  # past last
        cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
        if cycle:
            cycle.completed_at = datetime.datetime.utcnow()
        state.updated_at = datetime.datetime.utcnow()

    db.commit()

    # Check if we've reached the end
    if next_index >= len(all_qs):
        return RedirectResponse(url="/study/current", status_code=303)

    # Show feedback and auto-redirect
    return templates.TemplateResponse(
        request, "base.html",
        {
            "content": (
                f"<div class='card'><h3>反馈</h3><p>{feedback}</p></div>"
                "<a href='/study/current' class='btn btn-primary'>下一题</a>"
            )
        },
    )


# =============================================================================
# GET /study/progress — show current progress
# =============================================================================

@router.get("/progress", response_class=HTMLResponse)
async def study_progress(
    request: Request,
    db: Session = Depends(get_db),
):
    """Display study progress for the active cycle."""
    state = _get_or_create_session_state(db)
    if not state.current_cycle_id:
        return templates.TemplateResponse(
            request, "base.html",
            {"content": "<p>还没有开始学习。</p><a href='/materials' class='btn btn-primary'>去素材列表</a>"},
        )

    all_qs = (
        db.query(QuestionAttempt)
        .filter(QuestionAttempt.cycle_id == state.current_cycle_id)
        .order_by(QuestionAttempt.id)
        .all()
    )
    total = len(all_qs)
    answered = sum(1 for q in all_qs if q.answered_at is not None)
    correct = sum(1 for q in all_qs if q.is_correct)

    return templates.TemplateResponse(
        request, "study_result.html",
        {
            "cycle": db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first(),
            "total": total,
            "answered": answered,
            "correct": correct,
            "accuracy": round(correct / total * 100, 1) if answered > 0 else 0,
            "questions": all_qs,
            "in_progress": answered < total,
        },
    )


# =============================================================================
# GET /study — placeholder root
# =============================================================================

@router.get("", response_class=HTMLResponse)
async def study_home(request: Request):
    """Redirect to current question or progress."""
    return RedirectResponse(url="/study/current")
