"""
Generator and evaluator for Day 2 study cycle.

Generates explanations, translation exercises, and multiple-choice questions
using DeepSeek. Evaluates translation answers semantically.

Design principle: LLM generates content and evaluates translations;
Python validates all LLM output, persists data, and manages state.
The LLM never directly writes to the database.
"""

import datetime

from app.llm import structured_extraction, is_available
from app.models import GrammarPoint
from app.schemas import (
    GeneratedExplanation,
    TranslationExercise,
    MultipleChoiceQuestion,
    TranslationEvaluation,
    TranslationEvaluationV2,
    QuestionPayload,
)

# =============================================================================
# Generation prompts
# =============================================================================

EXPLANATION_SYSTEM_PROMPT = """You are a Japanese language teacher specializing in JLPT N2 grammar.

Given a grammar point name and its usage example from a real text, generate:
1. meaning_zh: Chinese explanation of the grammar point's meaning/usage
2. usage_notes_zh: Detailed usage notes in Chinese (formation rules, nuances, register)
3. example_sentences: 2-3 example sentences that demonstrate proper usage
   (These may be new sentences, not from the original text.)

Return valid JSON matching the requested schema. No extra text."""

TRANSLATION_SYSTEM_PROMPT = """You are a Japanese language exercise creator specializing in JLPT N2.

Given a grammar point with its explanation and example, create {count} Japanese
translation exercises. Each exercise:
1. prompt_zh: A Chinese sentence the learner should translate into Japanese
2. reference_answer_ja: A model Japanese answer using the target grammar
3. grading_notes: Clear criteria for evaluating the learner's answer

RULES:
- The exercises must naturally require or strongly favor the target grammar.
- The prompt should be natural Chinese, not a word-for-word translation cue.
- The reference answer is a model, not the only correct answer.
- Cover different contexts, not all the same sentence pattern.
- Return valid JSON matching the requested schema. No extra text."""

MULTIPLE_CHOICE_SYSTEM_PROMPT = """You are a Japanese language test designer.

Create multiple-choice questions that test understanding of Japanese grammar
point distinctions.

Each question must include:
- prompt: A Japanese sentence with a blank (_____) where the grammar goes
- A/B/C/D: Four plausible options. Only one is correct.
- expected: The correct option letter (A/B/C/D)
- question_role: One of "grammar_a_distinction", "grammar_b_distinction", or "review"

RULES:
- Distractors must be plausible and grammatically relevant (not obviously broken Japanese).
- Answer distribution should be balanced across A/B/C/D (no pattern like all A).
- Each question must have exactly one best answer.
- Return valid JSON matching the requested schema. No extra text."""

EVALUATION_SYSTEM_PROMPT = """You are a Japanese language grading assistant.

Given a translation exercise and a user's Japanese answer, evaluate:
1. is_correct: Is the answer semantically acceptable? Does it correctly use the target grammar?
2. feedback_zh: Constructive feedback in Chinese
3. corrected_answer_ja: A corrected version of the user's Japanese answer
4. reason_zh: Brief reason for the score in Chinese

RULES:
- Evaluate based on the grading_notes provided.
- Do NOT require exact match with the reference answer.
- Accept any answer that conveys the meaning and uses the target grammar acceptably.
- Minor particle/vocabulary errors that don't affect meaning should still pass.
- Return valid JSON matching the requested schema. No extra text."""


EVALUATION_V2_SYSTEM_PROMPT = """You are a Japanese language grading assistant.

Given a translation exercise and a user's Japanese answer, evaluate:

1. target_grammar_correct: Whether the specifically tested target grammar was used
correctly in the user's sentence. Judge this independently of other errors.
- true: The target grammar form, meaning, and application are correct in context.
- false: The target grammar is missing, used with wrong form, or used in an
 incorrect context.

2. score_hearts: Overall quality of the answer on a 0-10 scale.
- If target_grammar_correct is true, score_hearts must be between 6 and 10.
 - 10: Perfect. Natural, correct, uses target grammar flawlessly.
 - 8-9: Acceptable. Conveys meaning, target grammar used correctly.
 - 6-7: Target grammar correct but has notable additional errors.
- If target_grammar_correct is false, score_hearts must be between 0 and 5.
 - 5: Target grammar wrong but otherwise natural; reserved for when only
      the target grammar is incorrect.
 - 1-4: Significant errors, target grammar wrong.
 - 0: Completely wrong or unrelated.
IMPORTANT: You MUST respect the band constraint. If target grammar is correct,
the minimum score is 6. If target grammar is wrong, the maximum score is 5.

3. feedback_zh: Constructive feedback in Chinese
4. corrected_answer_ja: A corrected version of the user's Japanese answer
5. reason_zh: Brief reason for the score in Chinese
6. additional_errors: A list of specific errors found in the answer, EXCLUDING any
failure related to the target grammar itself (target grammar issues are handled
separately). Include vocabulary, particle, conjugation, expression, and other
non-target-grammar errors.
Each error item has:
- error_type: one of "particle", "vocabulary", "conjugation", "grammar", "expression", "other"
- error_rule_key: A stable key for grouping identical errors, e.g. "particle:wo→ni:noru"
- original_fragment: The user's incorrect fragment in Japanese
- corrected_fragment: The correct version in Japanese
- description: Chinese description of the error

RULES:
- DO NOT report target grammar failure as an additional_error. Target grammar is handled separately.
- Minor particle/vocabulary errors that don't affect meaning should still be reflected in the score.
- Evaluate based on the grading_notes provided.
- Return valid JSON matching the requested schema. No extra text."""


# =============================================================================
# Generator functions
# =============================================================================

def generate_explanation(grammar_point: GrammarPoint) -> GeneratedExplanation | None:
    """
    Generate a Chinese explanation for a grammar point.
    """
    if not is_available():
        print("[generator] LLM not available — skipping explanation")
        return None

    user_prompt = (
        f"Grammar point: {grammar_point.point_name}\n"
        f"Difficulty: {grammar_point.difficulty_level}\n"
        f"Example from material: {grammar_point.example_from_material}\n"
        f"Original explanation: {grammar_point.explanation_jp}\n\n"
        "Generate a detailed Chinese explanation for this grammar point."
    )

    result = structured_extraction(
        EXPLANATION_SYSTEM_PROMPT,
        user_prompt,
        GeneratedExplanation,
    )
    if result is None:
        print("[generator] Explanation generation failed")
        return None
    # Ensure point_name matches
    result.point_name = grammar_point.point_name
    return result


def generate_translation_exercises(
    grammar_point: GrammarPoint,
    count: int = 5,
) -> list[TranslationExercise]:
    """
    Generate *count* translation exercises targeting a grammar point.
    """
    if not is_available():
        print("[generator] LLM not available — skipping translation exercises")
        return []

    system_prompt = TRANSLATION_SYSTEM_PROMPT.format(count=count)
    schema_hint = (
        '{"exercises": [{"prompt_zh": "...", "reference_answer_ja": "...", '
        '"grammar_point": "' + grammar_point.point_name + '", "grading_notes": "..."}]}'
    )

    user_prompt = (
        f"Grammar point: {grammar_point.point_name} ({grammar_point.difficulty_level})\n"
        f"Explanation: {grammar_point.explanation_jp}\n"
        f"Material example: {grammar_point.example_from_material}\n\n"
        f"Create exactly {count} translation exercises that require using this grammar point.\n"
        f"Return JSON: {schema_hint}"
    )

    # Wrap in a container model for parsing
    from pydantic import BaseModel, Field
    class ExercisesContainer(BaseModel):
        exercises: list[TranslationExercise] = Field(default_factory=list)

    result = structured_extraction(system_prompt, user_prompt, ExercisesContainer)
    if result is None or not result.exercises:
        print("[generator] Translation exercise generation failed")
        return []

    # Fix grammar_point attribution
    for ex in result.exercises:
        ex.grammar_point = grammar_point.point_name

    print(f"[generator] Generated {len(result.exercises)} translation exercises")
    return result.exercises[:count]


def generate_multiple_choice(
    grammar_a: GrammarPoint,
    grammar_b: GrammarPoint,
    review_points: list[GrammarPoint],
) -> list[MultipleChoiceQuestion]:
    """
    Generate 9 multiple-choice questions:
      2 for grammar A, 2 for grammar B, 5 review from remaining points.
    """
    if not is_available():
        print("[generator] LLM not available — skipping MC generation")
        return []

    # Build context for LLM
    a_desc = f"[Grammar A] {grammar_a.point_name} ({grammar_a.difficulty_level}): {grammar_a.explanation_jp}"
    b_desc = f"[Grammar B] {grammar_b.point_name} ({grammar_b.difficulty_level}): {grammar_b.explanation_jp}"
    review_descs = "\n".join(
        f"[Review] {gp.point_name} ({gp.difficulty_level}): {gp.explanation_jp}"
        for gp in review_points
    ) if review_points else "[Review] No additional review points available — reuse grammar A/B."

    from pydantic import BaseModel, Field
    class MCContainer(BaseModel):
        questions: list[MultipleChoiceQuestion] = Field(default_factory=list)

    user_prompt = (
        f"Create exactly 9 multiple-choice questions for Japanese grammar learning.\n\n"
        f"{a_desc}\n"
        f"{b_desc}\n\n"
        f"Review grammar points:\n{review_descs}\n\n"
        "Structure:\n"
        "- Questions 1-2: grammar_a_distinction\n"
        "- Questions 3-4: grammar_b_distinction\n"
        "- Questions 5-9: review (from review grammar points)\n\n"
        "If insufficient review points exist, create review questions that test "
        "grammar A/B or general N2 knowledge.\n\n"
        "Return JSON: {\"questions\": [...]}\n"
        "Each question: prompt, A, B, C, D, expected (letter), grammar_point, question_role"
    )

    result = structured_extraction(
        MULTIPLE_CHOICE_SYSTEM_PROMPT, user_prompt, MCContainer
    )
    if result is None or not result.questions:
        print("[generator] MC generation failed")
        return []

    # Fix question_role attribution and ensure grammar_point is set
    for i, q in enumerate(result.questions):
        if i < 2:
            q.question_role = "grammar_a_distinction"
            q.grammar_point = grammar_a.point_name
        elif i < 4:
            q.question_role = "grammar_b_distinction"
            q.grammar_point = grammar_b.point_name
        else:
            q.question_role = "review"
            if not q.grammar_point:
                # LLM may omit grammar_point for review questions —
                # fall back to the first review point name
                q.grammar_point = review_points[0].point_name if review_points else "review"

    print(f"[generator] Generated {len(result.questions)} MC questions")
    return result.questions[:9]


def generate_one_translation(
    grammar_point: GrammarPoint,
) -> TranslationExercise | None:
    """Generate exactly 1 translation exercise for a grammar point."""
    result = generate_translation_exercises(grammar_point, count=1)
    return result[0] if result else None


def generate_one_multiple_choice(
    grammar_a: GrammarPoint,
    grammar_b: GrammarPoint,
    review_points: list[GrammarPoint],
    slot_index: int,  # 0-based within MC module (0-8)
) -> MultipleChoiceQuestion | None:
    """Generate exactly 1 multiple-choice question for a specific slot.

    slot_index mapping (must match start_cycle):
      0-1 → grammar_a_distinction
      2-3 → grammar_b_distinction
      4-8 → review
    """
    if not is_available():
        print("[generator] LLM not available — skipping single MC generation")
        return None

    if slot_index < 2:
        role = "grammar_a_distinction"
        target = grammar_a
        from app.schemas import MultipleChoiceQuestion as MCQSchema
    elif slot_index < 4:
        role = "grammar_b_distinction"
        target = grammar_b
    else:
        role = "review"
        # Pick review point round-robin
        if review_points:
            idx = (slot_index - 4) % len(review_points)
            target = review_points[idx]
        else:
            target = grammar_a  # fallback

    from pydantic import BaseModel, Field

    class SingleMC(BaseModel):
        prompt: str = Field(description="Japanese sentence with blank")
        A: str = Field(description="Option A")
        B: str = Field(description="Option B")
        C: str = Field(description="Option C")
        D: str = Field(description="Option D")
        expected: str = Field(description="Correct answer letter (A/B/C/D)")
        grammar_point: str = Field(default="", description="Target grammar")

    context = (
        f"[Grammar A] {grammar_a.point_name} ({grammar_a.difficulty_level}): {grammar_a.explanation_jp}\n"
        f"[Grammar B] {grammar_b.point_name} ({grammar_b.difficulty_level}): {grammar_b.explanation_jp}\n"
    )
    if target and target.id not in (grammar_a.id, grammar_b.id):
        context += (
            f"[Target Review] {target.point_name} ({target.difficulty_level}): "
            f"{target.explanation_jp}\n"
        )

    user_prompt = (
        f"Create 1 multiple-choice question for Japanese grammar learning.\n\n"
        f"{context}\n"
        f"Role: {role}\n"
        f"Target grammar: {target.point_name if target else 'general'}\n\n"
        "Return JSON: {\"prompt\": \"...\", \"A\": \"...\", \"B\": \"...\", "
        "\"C\": \"...\", \"D\": \"...\", \"expected\": \"A\", \"grammar_point\": \"...\"}\n"
        "Expected is A/B/C/D. The grammar_point field should name the target grammar."
    )

    result = structured_extraction(
        _single_mc_system_prompt(), user_prompt, SingleMC
    )
    if result is None:
        print(f"[generator] Single MC generation failed (slot {slot_index})")
        return None

    from app.schemas import MultipleChoiceQuestion as MCQ

    return MCQ(
        prompt=result.prompt,
        A=result.A, B=result.B, C=result.C, D=result.D,
        expected=result.expected,
        grammar_point=result.grammar_point or (target.point_name if target else ""),
        question_role=role,
    )


def _single_mc_system_prompt() -> str:
    """System prompt for single MC generation."""
    return (
        "You are a Japanese language test designer. "
        "Create exactly ONE multiple-choice question."
        "Each question must have a prompt (Japanese with a blank), "
        "four options A/B/C/D, one expected answer letter, "
        "and the target grammar_point name. "
        "Distractors must be plausible. Return valid JSON only."
    )


# =============================================================================
# Translation evaluation
# =============================================================================

def evaluate_translation_answer(
    exercise: TranslationExercise,
    user_answer: str,
) -> TranslationEvaluation | None:
    """
    Evaluate a user's Japanese translation answer using DeepSeek.

    Returns a validated TranslationEvaluation, or None if evaluation fails.
    """
    if not is_available():
        print("[generator] LLM not available — cannot evaluate translation")
        return None

    user_prompt = (
        f"Exercise prompt (Chinese): {exercise.prompt_zh}\n"
        f"Reference answer (model): {exercise.reference_answer_ja}\n"
        f"Target grammar: {exercise.grammar_point}\n"
        f"Grading notes: {exercise.grading_notes}\n\n"
        f"User's answer: {user_answer}\n\n"
        "Evaluate this answer. Is it semantically acceptable?"
    )

    result = structured_extraction(
        EVALUATION_SYSTEM_PROMPT,
        user_prompt,
        TranslationEvaluation,
    )
    if result is None:
        print("[generator] Translation evaluation failed — returning None")
        return None

    return result


def evaluate_translation_answer_v2(
    exercise: TranslationExercise,
    user_answer: str,
) -> TranslationEvaluationV2 | None:
    """
    Evaluate a user's Japanese translation answer using DeepSeek,
    returning heart scores and additional error details.

    Returns a validated TranslationEvaluationV2, or None if evaluation fails.
    """
    if not is_available():
        print("[generator] LLM not available — cannot evaluate translation")
        return None

    user_prompt = (
        f"Exercise prompt (Chinese): {exercise.prompt_zh}\n"
        f"Reference answer (model): {exercise.reference_answer_ja}\n"
        f"Target grammar: {exercise.grammar_point}\n"
        f"Grading notes: {exercise.grading_notes}\n\n"
        f"User's answer: {user_answer}\n\n"
        "Evaluate this answer. Provide score_hearts (0-10) and list any additional errors."
    )

    result = structured_extraction(
        EVALUATION_V2_SYSTEM_PROMPT,
        user_prompt,
        TranslationEvaluationV2,
    )
    if result is None:
        print("[generator] Translation evaluation v2 failed — returning None")
        return None

    return result
