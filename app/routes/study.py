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
    QuestionAttempt,
    SessionState,
    WeakPoint,
    UsageLog,
)
from app.schemas import TranslationExercise, MultipleChoiceQuestion, QuestionPayload
from app.agents.generator import (
    generate_explanation,
    generate_translation_exercises,
    generate_multiple_choice,
    evaluate_translation_answer,
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
        pending = [q for q in module_qs if q.status == "pending"]
        if pending:
            return mod
    return None


def _first_pending_question_index(all_qs: list[QuestionAttempt]) -> int | None:
    """Return the index (in all_qs) of the first pending question."""
    for i, q in enumerate(all_qs):
        if q.status == "pending":
            return i
    return None


def _compute_cycle_completion(db: Session, cycle: StudyCycle) -> dict:
    """Compute completion stats for a cycle. Also sets completed_at + is_valid_completion if fully done."""
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    total = len(all_qs)
    answered = sum(1 for q in all_qs if q.status == "answered")
    skipped_count = sum(1 for q in all_qs if q.status == "skipped")
    studied_count = sum(1 for q in all_qs if q.status == "studied")
    pending = sum(1 for q in all_qs if q.status == "pending")
    correct = sum(1 for q in all_qs if q.is_correct)

    # Module-level analysis
    module_statuses = {}
    had_skipped = False
    for mod in MODULE_ORDER:
        mod_qs = _get_module_questions(all_qs, mod)
        mod_pending = sum(1 for q in mod_qs if q.status == "pending")
        mod_skipped = sum(1 for q in mod_qs if q.status == "skipped")
        if mod_skipped > 0:
            had_skipped = True
        module_statuses[mod] = {
            "total": len(mod_qs),
            "pending": mod_pending,
            "skipped": mod_skipped,
            "studied": sum(1 for q in mod_qs if q.status == "studied"),
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
        "pending": pending,
        "correct": correct,
        "accuracy": round(correct / answered * 100, 1) if answered > 0 else 0,
        "is_done": is_done,
        "had_skipped_module": had_skipped,
        "is_valid_completion": cycle.is_valid_completion if is_done else False,
        "module_statuses": module_statuses,
    }


def _record_weak_point(
    db: Session, grammar_point_name: str
) -> None:
    """Record or increment a grammar weak point from a wrong answer."""
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

    # Filter out user-marked-as-mastered grammar points
    unmastered = [gp for gp in grammar_points if not gp.mastered]

    if len(unmastered) < 2:
        total = len(grammar_points)
        mastered_count = total - len(unmastered)
        if len(unmastered) == 1:
            msg = (
                "<div class='card flash-error'>"
                "<strong>无法开始学习：</strong>该素材需要至少 2 个未掌握的语法点才能开始学习。"
                f"共 {total} 个语法点，已掌握 {mastered_count} 个，"
                f"仅剩 1 个，仍需至少 2 个。</div>"
                "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
            )
        else:
            msg = (
                "<div class='card flash-error'>"
                "<strong>无法开始学习：</strong>该素材需要至少 2 个未掌握的语法点。"
                f"共 {total} 个语法点，已掌握 {mastered_count} 个。"
                "请先上传更多素材。</div>"
                "<a href='/materials' class='btn btn-primary'>返回素材列表</a>"
            )
        return templates.TemplateResponse(
            request, "base.html",
            {"content": msg},
            status_code=400,
        )

    # Select grammar A and B from unmastered points deterministically
    n2_points = [gp for gp in unmastered if gp.difficulty_level == "N2"]
    if len(n2_points) >= 2:
        grammar_a = n2_points[0]
        grammar_b = n2_points[1]
    else:
        grammar_a = grammar_points[0]
        grammar_b = grammar_points[1]

    # Remaining points for review questions
    review_points = [
        gp for gp in unmastered
        if gp.id not in (grammar_a.id, grammar_b.id)
    ]

    # --- Day 3: Prioritize active weak points for review questions ---
    active_weak_points = (
        db.query(WeakPoint)
        .filter(WeakPoint.point_type == "grammar", WeakPoint.is_active == True)
        .all()
    )
    weak_point_names = {wp.point_reference for wp in active_weak_points}
    # Move active weak-point grammar points to the front of review_points
    weak_review = [gp for gp in review_points if gp.point_name in weak_point_names]
    other_review = [gp for gp in review_points if gp.point_name not in weak_point_names]
    prioritized_review = weak_review + other_review
    # If we have more than enough for 5 review slots, prefer weak ones
    # The LLM will use what it needs from the list

    # ---------- Parallel generation: 5 independent DeepSeek calls ----------
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=5) as executor:
        expl_a_future = loop.run_in_executor(
            executor, generate_explanation, grammar_a
        )
        expl_b_future = loop.run_in_executor(
            executor, generate_explanation, grammar_b
        )
        trans_a_future = loop.run_in_executor(
            executor, generate_translation_exercises, grammar_a, 5
        )
        trans_b_future = loop.run_in_executor(
            executor, generate_translation_exercises, grammar_b, 5
        )
        mc_future = loop.run_in_executor(
            executor, generate_multiple_choice,
            grammar_a, grammar_b, prioritized_review,
        )

        (explanation_a, explanation_b,
         trans_a, trans_b, mc_questions) = await asyncio.gather(
            expl_a_future, expl_b_future,
            trans_a_future, trans_b_future,
            mc_future,
        )

    # ---------- Validate results ----------
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

    # ---------- Persist 19 question attempts with status="pending" ----------
    # Questions 1-5: Grammar A translation
    for ex in trans_a:
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
            status="pending",
        )
        db.add(qa)

    # Questions 6-10: Grammar B translation
    for ex in trans_b:
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
            status="pending",
        )
        db.add(qa)

    # Questions 11-19: Multiple choice
    for mc in mc_questions:
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
            status="pending",
        )
        db.add(qa)

    db.commit()

    # ---------- Persist usage logs for generation phase ----------
    _persist_usage_logs(db, cycle.id)

    # ---------- Initialize session state ----------
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
                "accuracy": stats["accuracy"],
                "questions": all_qs,
                "in_progress": False,
                "had_skipped_module": stats["had_skipped_module"],
                "is_valid_completion": stats["is_valid_completion"],
                "module_statuses": stats["module_statuses"],
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
            },
        )

    # Fix session state to point to the actual first pending question
    state.current_question_index = first_pending_idx
    current_q = all_qs[first_pending_idx]
    state.current_module = current_q.module_type
    state.updated_at = datetime.datetime.utcnow()
    db.commit()

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
        is_correct = evaluation.is_correct
        current_q.is_correct = is_correct
        current_q.answered_at = datetime.datetime.utcnow()
        current_q.status = "answered"
        current_q.correct_answer = evaluation.corrected_answer_ja
        db.commit()

        feedback = evaluation.feedback_zh
        feedback_html = _build_answer_feedback_html(
            is_correct, evaluation.corrected_answer_ja, answer, feedback
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

    # --- Day 3: Record weak point for wrong answers ---
    grammar_point_name = payload.get("grammar_point", "")
    if grammar_point_name and not is_correct:
        _record_weak_point(db, grammar_point_name)

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
    pending_in_mod = [q for q in mod_qs if q.status == "pending"]

    if not pending_in_mod:
        return RedirectResponse(url="/study/current", status_code=303)

    # Mark all pending questions in this module as skipped
    for q in pending_in_mod:
        q.status = "skipped"
        q.answered_at = datetime.datetime.utcnow()
        # Do NOT set user_answer or is_correct — unanswered skipped questions
        # must not create weak points
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
    pending_in_mod = [q for q in mod_qs if q.status == "pending"]

    if not pending_in_mod:
        return RedirectResponse(url="/study/current", status_code=303)

    # Mark all pending questions in this module as studied
    for q in pending_in_mod:
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

    stats = _compute_cycle_completion(db, db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first())
    all_qs = _get_sorted_cycle_questions(db, state.current_cycle_id)

    return templates.TemplateResponse(
        request, "study_result.html",
        {
            "cycle": db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first(),
            "total": stats["total"],
            "answered": stats["answered"],
            "correct": stats["correct"],
            "skipped": stats["skipped"],
            "studied": stats["studied"],
            "accuracy": stats["accuracy"],
            "questions": all_qs,
            "in_progress": not stats["is_done"],
            "had_skipped_module": stats["had_skipped_module"],
            "is_valid_completion": stats["is_valid_completion"],
            "module_statuses": stats["module_statuses"],
        },
    )
