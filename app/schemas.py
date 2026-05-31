"""Pydantic schemas for Lingua Web — Day 1 extraction + Day 2 study cycle."""

from pydantic import BaseModel, Field


# =============================================================================
# Day 1 — Extraction schemas
# =============================================================================

class GrammarPointExtract(BaseModel):
    """A single extracted grammar point from Japanese material."""

    point_name: str = Field(description="Grammar point name, e.g. 〜てはいられない")
    explanation_jp: str = Field(description="Japanese explanation of the grammar point")
    example_from_material: str = Field(
        description="Exact excerpt from the original material demonstrating the point"
    )
    difficulty_level: str = Field(
        description="JLPT level: N5, N4, N3, N2, or N1"
    )


class VocabItemExtract(BaseModel):
    """A single extracted vocabulary item from Japanese material."""

    word: str = Field(description="The vocabulary word in Japanese")
    reading: str | None = Field(default=None, description="Reading/pronunciation (hiragana)")
    meaning_zh: str | None = Field(default=None, description="Chinese meaning of the word")
    example_from_material: str | None = Field(
        default=None, description="Exact excerpt from the original material containing this word"
    )
    difficulty_level: str | None = Field(
        default=None, description="JLPT level: N5, N4, N3, N2, or N1"
    )


class ExtractionResult(BaseModel):
    """Full extraction result from the LLM."""

    grammar_points: list[GrammarPointExtract] = Field(
        default_factory=list,
        description="Extracted grammar points from the material",
    )
    vocab_items: list[VocabItemExtract] = Field(
        default_factory=list,
        description="Extracted vocabulary items from the material",
    )


# =============================================================================
# Day 2 — Study cycle schemas
# =============================================================================

class GeneratedExplanation(BaseModel):
    """Generated explanation for a grammar point."""

    point_name: str = Field(description="Grammar point name")
    meaning_zh: str = Field(description="Chinese meaning / usage meaning")
    usage_notes_zh: str = Field(description="Usage notes in Chinese")
    example_sentences: list[str] = Field(
        description="Example sentences showing the grammar point in context"
    )


class TranslationExercise(BaseModel):
    """A single translation exercise (Chinese prompt → Japanese answer)."""

    prompt_zh: str = Field(description="Chinese prompt the user should translate from")
    reference_answer_ja: str = Field(description="Reference Japanese answer (hidden until after answer)")
    grammar_point: str = Field(description="Which grammar point this exercise targets")
    grading_notes: str = Field(
        description="Notes for the evaluator on what to check (acceptability criteria)"
    )


class MultipleChoiceQuestion(BaseModel):
    """A single multiple-choice question."""

    prompt: str = Field(description="Question text / sentence with blank")
    A: str = Field(description="Option A")
    B: str = Field(description="Option B")
    C: str = Field(description="Option C")
    D: str = Field(description="Option D")
    expected: str = Field(description="Correct answer (A/B/C/D) — server-side only")
    grammar_point: str = Field(description="Which grammar point this question tests")
    question_role: str = Field(
        description="One of: grammar_a_distinction, grammar_b_distinction, review"
    )


class TranslationEvaluation(BaseModel):
    """Structured evaluation of a user's translation answer."""

    is_correct: bool = Field(description="Whether the answer is semantically acceptable")
    feedback_zh: str = Field(description="Feedback in Chinese")
    corrected_answer_ja: str = Field(description="Corrected Japanese answer")
    reason_zh: str = Field(description="Reason for the evaluation in Chinese")


class QuestionPayload(BaseModel):
    """
    Unified payload stored in question_attempts.question_payload_json.

    For translation:
      type="translation", prompt_zh, reference_answer_ja, grammar_point, grading_notes

    For multiple_choice:
      type="multiple_choice", prompt, choices (A/B/C/D), expected, grammar_point, question_role

    The correct_answer field on the DB row carries:
      - translation: reference_answer_ja
      - multiple_choice: expected letter (A/B/C/D)
    """

    type: str = Field(description="question type: translation or multiple_choice")
    prompt_zh: str | None = Field(default=None, description="Chinese prompt (translation)")
    prompt: str | None = Field(default=None, description="Question text (multiple_choice)")
    reference_answer_ja: str | None = Field(default=None, description="Hidden reference answer (translation)")
    grading_notes: str | None = Field(default=None, description="Grading notes (translation)")
    choices: dict[str, str] | None = Field(default=None, description="Options dict (multiple_choice)")
    expected: str | None = Field(default=None, description="Correct option letter (multiple_choice)")
    grammar_point: str = Field(description="Target grammar point name")
    question_role: str | None = Field(default=None, description="For MC: grammar_a/b_distinction or review")
