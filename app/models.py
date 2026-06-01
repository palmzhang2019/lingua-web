"""SQLAlchemy ORM models for Lingua Web."""

import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    JSON,
    UniqueConstraint,
)

from app.db import Base


class Material(Base):
    """Uploaded language learning material (TXT/MD/PDF)."""

    __tablename__ = "materials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)
    content_text = Column(Text, nullable=False)
    source_type = Column(String(20), nullable=False, default="txt")  # txt | md | pdf
    language_code = Column(String(10), nullable=False, default="ja")
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)  # null=active, set=removed from active library
    # PDF page grounding (nullable for TXT/MD)
    source_page_start = Column(Integer, nullable=True)
    source_page_end = Column(Integer, nullable=True)
    extraction_method = Column(String(30), nullable=True)  # None for TXT/MD, "openai_pdf_vision" for PDF


class GrammarPoint(Base):
    """Extracted Japanese grammar points from a material."""

    __tablename__ = "grammar_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    point_name = Column(String(100), nullable=False)
    explanation_jp = Column(Text, nullable=False)
    example_from_material = Column(Text, nullable=False)
    difficulty_level = Column(String(10), nullable=False, default="N2")
    extracted_at = Column(DateTime, default=datetime.datetime.utcnow)
    source_page = Column(Integer, nullable=True)  # PDF page where this was found
    mastered = Column(Boolean, default=False)  # user-marked as already known


class VocabItem(Base):
    """Extracted N2-level vocabulary items from a material."""

    __tablename__ = "vocab_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    word = Column(String(200), nullable=False)
    reading = Column(String(200), nullable=True)
    meaning_zh = Column(Text, nullable=True)
    example_from_material = Column(Text, nullable=True)
    difficulty_level = Column(String(10), nullable=False, default="N2")
    extracted_at = Column(DateTime, default=datetime.datetime.utcnow)
    source_page = Column(Integer, nullable=True)  # PDF page where this was found
    mastered = Column(Boolean, default=False)  # user-marked as already known


class StudyCycle(Base):
    """A complete study cycle linking two grammar points."""

    __tablename__ = "study_cycles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    grammar_a_id = Column(Integer, ForeignKey("grammar_points.id"), nullable=True)
    grammar_b_id = Column(Integer, ForeignKey("grammar_points.id"), nullable=True)
    is_valid_completion = Column(Boolean, default=False)


class QuestionAttempt(Base):
    """Record of a single question attempt within a study cycle."""

    __tablename__ = "question_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(Integer, ForeignKey("study_cycles.id"), nullable=False)
    module_type = Column(String(50), nullable=False)
    question_payload_json = Column(JSON, nullable=False)
    user_answer = Column(Text, nullable=True)
    correct_answer = Column(Text, nullable=False)
    is_correct = Column(Boolean, default=False)
    answered_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # planned | generating | pending | answered | skipped | studied | cancelled_mastered | generation_failed
    # Phase 3: lazy generation support
    target_grammar_id = Column(Integer, ForeignKey("grammar_points.id"), nullable=True)
    generation_error = Column(Text, nullable=True)
    generation_started_at = Column(DateTime, nullable=True)
    # Phase 4A: heart scoring
    score_hearts = Column(Integer, nullable=True)  # NULL=unscored/historical, 0-10 for answered translations


class TranslationErrorCandidate(Base):
    """Additional detected errors from translation answers, pending user review."""

    __tablename__ = "translation_error_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(Integer, ForeignKey("study_cycles.id"), nullable=False)
    source_attempt_id = Column(Integer, ForeignKey("question_attempts.id"), nullable=False)
    error_type = Column(String(50), nullable=False)  # particle, vocabulary, conjugation, grammar, expression, other
    error_rule_key = Column(String(200), nullable=False)
    original_fragment = Column(Text, nullable=False)
    corrected_fragment = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    suggested_grammar_point_id = Column(Integer, ForeignKey("grammar_points.id"), nullable=True)
    target_grammar_id = Column(Integer, nullable=True)  # FK not enforced; for traceability
    status = Column(String(20), nullable=False, default="pending")  # pending, added, ignored
    occurrence_count = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)


class WeakPoint(Base):
    """Tracked weak points for review focus."""

    __tablename__ = "weak_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    point_type = Column(String(50), nullable=False)
    point_reference = Column(String(200), nullable=False)
    error_count = Column(Integer, default=0)
    last_error_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)


class WeakPointEvent(Base):
    """Per-cycle provenance for each weak-point trigger event.

    Each qualifying weak-point write (low-heart translation auto-insert,
    user-confirmed candidate, wrong-choice answer) records one event row.
    Enables accurate new-vs-re-hit counts per cycle without inferring from
    aggregate WeakPoint.error_count.
    """

    __tablename__ = "weak_point_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(Integer, ForeignKey("study_cycles.id"), nullable=False)
    weak_point_id = Column(Integer, ForeignKey("weak_points.id"), nullable=True)
    source_type = Column(String(50), nullable=False)  # translation_low_score_target_grammar | translation_candidate_confirmed | choice_wrong_answer
    event_type = Column(String(20), nullable=False)  # created | hit_existing
    source_attempt_id = Column(Integer, ForeignKey("question_attempts.id"), nullable=True)
    source_candidate_id = Column(Integer, ForeignKey("translation_error_candidates.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class SessionState(Base):
    """Current study session state for resume support."""

    __tablename__ = "session_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    current_cycle_id = Column(Integer, ForeignKey("study_cycles.id"), nullable=True)
    current_module = Column(String(50), nullable=True)
    current_question_index = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class UsageLog(Base):
    """Token usage log for LLM API calls."""

    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_purpose = Column(String(100), nullable=False)
    cycle_id = Column(Integer, ForeignKey("study_cycles.id"), nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    called_at = Column(DateTime, default=datetime.datetime.utcnow)


class CycleMaterial(Base):
    """Many-to-many association between study cycles and materials."""

    __tablename__ = "cycle_materials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(Integer, ForeignKey("study_cycles.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)

    # Each material can appear only once per cycle
    __table_args__ = (
        UniqueConstraint("cycle_id", "material_id", name="uq_cycle_material"),
    )
