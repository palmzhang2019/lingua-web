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
    status = Column(String(20), nullable=False, default="pending")  # pending | answered | skipped | studied


class WeakPoint(Base):
    """Tracked weak points for review focus."""

    __tablename__ = "weak_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    point_type = Column(String(50), nullable=False)
    point_reference = Column(String(200), nullable=False)
    error_count = Column(Integer, default=0)
    last_error_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)


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
