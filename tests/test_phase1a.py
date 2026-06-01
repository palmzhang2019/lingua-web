"""Phase 1a tests: database isolation and skipped-question semantics.

All tests use a temporary SQLite database — never data/lingua.db.
Run with: uv run pytest tests/test_phase1a.py -v
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Set env vars BEFORE any app import — this is the KEY isolation mechanism.
os.environ["LINGUA_TESTING"] = "1"
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["LINGUA_DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import datetime
import pytest
from fastapi.testclient import TestClient

from app.db import init_db, SessionLocal
from app.config import DATABASE_URL, LINGUA_TESTING
from app.models import (
    Material, GrammarPoint, VocabItem, StudyCycle,
    QuestionAttempt, SessionState, WeakPoint,
)
from app.main import app


# ---------------------------------------------------------------------------
# Mock helpers — avoid real DeepSeek/OpenAI calls during tests
# ---------------------------------------------------------------------------

class MockExplanation:
    def __init__(self, name="〜てはいられない", meaning="无法继续做某事",
                 usage="表示无法保持某种状态", example_sentences=["例文1", "例文2"]):
        self.point_name = name
        self.meaning_zh = meaning
        self.usage_notes_zh = usage
        self.example_sentences = example_sentences


class MockTranslation:
    def __init__(self, prompt_zh="请翻译", reference_answer_ja="答え",
                 grading_notes="确认正确使用", grammar_point="〜てはいられない"):
        self.prompt_zh = prompt_zh
        self.reference_answer_ja = reference_answer_ja
        self.grading_notes = grading_notes
        self.grammar_point = grammar_point


class MockMC:
    def __init__(self, prompt="Choose the correct answer", A="Option A", B="Option B",
                 C="Option C", D="Option D", expected="A",
                 grammar_point="〜てはいられない", question_role="grammar_a"):
        self.prompt = prompt
        self.A = A
        self.B = B
        self.C = C
        self.D = D
        self.expected = expected
        self.grammar_point = grammar_point
        self.question_role = question_role


def make_mock_explanation(gp_name="〜てはいられない"):
    return MockExplanation(name=gp_name)


def make_mock_translations(n=5, gp_name="〜てはいられない"):
    return [MockTranslation(
        prompt_zh=f"翻译题{j+1}",
        reference_answer_ja=f"答え{j+1}",
        grammar_point=gp_name,
    ) for j in range(n)]


def make_mock_mc(n=9):
    return [MockMC(
        prompt=f"选择题{j+1}",
        A=f"选项A-{j+1}",
        B=f"选项B-{j+1}",
        C=f"选项C-{j+1}",
        D=f"选项D-{j+1}",
    ) for j in range(n)]


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def setup_temp_db():
    """Create schema in the temp DB once per session."""
    init_db()
    yield
    os.unlink(_tmp_db.name)


@pytest.fixture
def db():
    """Per-test DB session with rollback isolation."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def populated_material(db):
    """Create a material with 3 grammar points and some vocab for study tests."""
    mat = Material(
        filename="test.txt",
        content_text="Test material content for grammar points.",
        source_type="txt",
    )
    db.add(mat)
    db.commit()
    db.refresh(mat)

    gp_a = GrammarPoint(
        material_id=mat.id,
        point_name="〜てはいられない",
        explanation_jp="〜ていられないと同じく、ある状態を続けることができないことを表す。",
        example_from_material="試験前に遊んではいられない。",
        difficulty_level="N2",
    )
    gp_b = GrammarPoint(
        material_id=mat.id,
        point_name="〜がち",
        explanation_jp="傾向があることを表す。",
        example_from_material="最近、忘れがちだ。",
        difficulty_level="N2",
    )
    gp_c = GrammarPoint(
        material_id=mat.id,
        point_name="〜たきり",
        explanation_jp="最後に〜したまま、ずっと〜ない状態が続いていることを表す。",
        example_from_material="彼は出かけたきり、帰ってこない。",
        difficulty_level="N2",
    )
    db.add_all([gp_a, gp_b, gp_c])
    db.commit()

    v1 = VocabItem(material_id=mat.id, word="協力", reading="きょうりょく",
                   meaning_zh="协力/合作", difficulty_level="N2")
    v2 = VocabItem(material_id=mat.id, word="返事", reading="へんじ",
                   meaning_zh="回信/回答", difficulty_level="N2")
    db.add_all([v1, v2])
    db.commit()

    return mat, gp_a, gp_b, gp_c


@pytest.fixture
def mock_deepseek():
    """Mock DeepSeek generation calls so start_cycle doesn't hit real API."""
    with patch("app.routes.study.generate_explanation") as mock_exp, \
         patch("app.routes.study.generate_translation_exercises") as mock_t, \
         patch("app.routes.study.generate_multiple_choice") as mock_mc:
        mock_exp.return_value = MockExplanation()
        mock_t.return_value = make_mock_translations(5, "〜てはいられない")
        mock_mc.return_value = make_mock_mc(9)
        yield


@pytest.fixture
def start_cycle_with_mocks(client, db, populated_material, mock_deepseek):
    """Start a study cycle with mocked LLM calls. Returns (mat, gp_a, gp_b)."""
    mat, gp_a, gp_b, gp_c = populated_material
    resp = client.post("/study/start_cycle", data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Start cycle failed: {resp.status_code}"
    return mat, gp_a, gp_b, gp_c


# ===========================================================================
# Database isolation tests
# ===========================================================================

def test_testing_guard_rejects_real_database():
    """The test guard must prevent connecting to data/lingua.db when LINGUA_TESTING=1."""
    assert LINGUA_TESTING is True, "LINGUA_TESTING must be True during tests"
    assert "data/lingua.db" not in DATABASE_URL, \
        f"Database URL must not point at real DB: {DATABASE_URL}"
    assert _tmp_db.name in DATABASE_URL, \
        f"Database URL must use temp file: {DATABASE_URL}"


def test_tests_use_temporary_database(client):
    """Test-created materials exist only in temp DB."""
    resp = client.post("/materials/upload", files={
        "file": ("test.txt", b"Japanese test content for N2 grammar.", "text/plain")
    })
    assert resp.status_code in (200, 303), f"Upload failed: {resp.status_code}"
    resp = client.get("/materials")
    assert "test.txt" in resp.text


def test_tests_do_not_use_real_database():
    """Hard assertion: The temp DB path must NOT match the real DB."""
    real_db = str(Path(__file__).resolve().parent.parent / "data" / "lingua.db")
    assert real_db not in os.environ.get("LINGUA_DATABASE_URL", ""), \
        "Test DB URL must not point at real user database"


# ===========================================================================
# Skipped-question semantics tests
# ===========================================================================

def test_skipped_question_not_wrong(client, db, start_cycle_with_mocks):
    """Skipped question must NOT have graded is_correct=False semantics."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    resp = client.post("/study/skip_module", follow_redirects=False)
    assert resp.status_code in (303, 302), f"Skip module failed: {resp.status_code}"

    skipped = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id,
        QuestionAttempt.status == "skipped",
    ).all()
    assert len(skipped) > 0, "Expected skipped questions"

    for q in skipped:
        assert q.user_answer is None, \
            f"Skipped question {q.id} must not have user_answer"


def test_skipped_question_excluded_from_accuracy(client, db, start_cycle_with_mocks):
    """Skipped questions must not be counted in the accuracy denominator."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Skip all three modules
    for _ in range(3):
        resp = client.post("/study/skip_module", follow_redirects=False)
        assert resp.status_code in (303, 302)

    resp = client.get("/study/current")
    assert resp.status_code == 200

    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    assert cycle.completed_at is not None

    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).all()
    answered = sum(1 for q in all_qs if q.status == "answered")
    skipped = sum(1 for q in all_qs if q.status == "skipped")

    assert answered == 0, f"Expected 0 answered, got {answered}"
    assert skipped == 19, f"Expected 19 skipped, got {skipped}"
    assert cycle.is_valid_completion is False, \
        "Cycle with all modules skipped must not be valid completion"

    # Result page must show "无实际作答记录" for zero-answered
    assert "无实际作答记录" in resp.text


def test_skipped_question_does_not_create_weak_point(client, db, start_cycle_with_mocks):
    """Skipping questions must not create or increment weak points."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    wp_before = db.query(WeakPoint).count()

    # Skip grammar A module
    resp = client.post("/study/skip_module", follow_redirects=False)
    assert resp.status_code in (303, 302)

    wp_after = db.query(WeakPoint).count()
    assert wp_after == wp_before, \
        f"Skipping module created {wp_after - wp_before} weak point(s)"

    # Skip remaining modules
    for _ in range(2):
        client.post("/study/skip_module", follow_redirects=False)

    wp_final = db.query(WeakPoint).count()
    assert wp_final == wp_before, \
        f"Complete skip cycle created {wp_final - wp_before} weak point(s)"


def test_skip_module_invalid_completion_without_wrong_scores(
    client, db, start_cycle_with_mocks
):
    """A cycle with skipped modules reaches results but is_valid_completion=False."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Skip all three modules
    for _ in range(3):
        client.post("/study/skip_module", follow_redirects=False)

    resp = client.get("/study/current")
    assert resp.status_code == 200

    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    assert cycle.completed_at is not None
    assert cycle.is_valid_completion is False

    # Skipped questions must display as skipped, not incorrect
    assert "⏭️" in resp.text or "跳过" in resp.text, \
        "Result page should show skipped indicator"


def test_zero_answered_accuracy_display(client, db, start_cycle_with_mocks):
    """A fully-skipped cycle must not report misleading 0/N accuracy."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    # Skip all
    for _ in range(3):
        client.post("/study/skip_module", follow_redirects=False)

    resp = client.get("/study/current")
    assert resp.status_code == 200
    assert "无实际作答记录" in resp.text
    assert "已答：0" in resp.text


def test_mark_studied_no_weak_point(client, db, start_cycle_with_mocks):
    """Marking a module as studied must not create weak points."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    client.post("/study/mark_studied", follow_redirects=False)

    weak = db.query(WeakPoint).filter(
        WeakPoint.point_reference == gp_a.point_name,
    ).first()
    assert weak is None, f"Weak point created for studied grammar {gp_a.point_name}"


def test_answered_accuracy_mixed_with_skipped(client, db, start_cycle_with_mocks):
    """Mixed answered+skipped cycle: accuracy based only on answered questions."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    # Answer 4 questions directly (bypassing API route — set DB directly)
    for i in [0, 1, 5, 10]:
        q = all_qs[i]
        q.status = "answered"
        q.user_answer = q.correct_answer if i in (0, 5) else "wrong"
        q.is_correct = (i in (0, 5))
        q.answered_at = datetime.datetime.utcnow()

    # Skip 3 questions in grammar A section (indices 2-4)
    for q in all_qs[2:5]:
        q.status = "skipped"
        q.answered_at = datetime.datetime.utcnow()

    # Answer remaining 12 pending questions
    for q in all_qs:
        if q.status == "pending":
            q.status = "answered"
            q.user_answer = q.correct_answer if q.id % 2 == 0 else "wrong"
            q.is_correct = (q.id % 2 == 0)
            q.answered_at = datetime.datetime.utcnow()

    db.commit()

    from app.routes.study import _compute_cycle_completion
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    stats = _compute_cycle_completion(db, cycle)

    assert stats["answered"] == 16, f"Expected 16 answered, got {stats['answered']}"
    assert stats["skipped"] == 3, f"Expected 3 skipped, got {stats['skipped']}"
    assert stats["is_valid_completion"] is False, \
        "Cycle with skipped questions must not be valid completion"

    total_correct = sum(1 for q in all_qs if q.status == "answered" and q.is_correct)
    assert stats["correct"] == total_correct
    expected_acc = round(total_correct / 16 * 100, 1)
    assert stats["accuracy"] == expected_acc, \
        f"Expected accuracy {expected_acc}, got {stats['accuracy']}"


def test_normal_all_answered_cycle_valid(client, db, start_cycle_with_mocks):
    """A cycle where all questions are genuinely answered is valid completion."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    # Answer all 19 questions (no skips)
    for q in all_qs:
        q.status = "answered"
        q.user_answer = q.correct_answer if q.id % 2 == 0 else "wrong"
        q.is_correct = (q.id % 2 == 0)
        q.answered_at = datetime.datetime.utcnow()

    from app.routes.study import _compute_cycle_completion
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    stats = _compute_cycle_completion(db, cycle)

    assert stats["answered"] == 19, f"Expected 19 answered, got {stats['answered']}"
    assert stats["skipped"] == 0, f"Expected 0 skipped, got {stats['skipped']}"
    assert stats["is_valid_completion"] is True, \
        "All-answered cycle must be valid completion"


def test_skip_module_no_weak_point_leakage(client, db, start_cycle_with_mocks):
    """Skipping a module must not create weak points for any question in that module."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    # Skip grammar A translation only
    client.post("/study/skip_module", follow_redirects=False)

    weak = db.query(WeakPoint).filter(
        WeakPoint.point_reference == gp_a.point_name,
    ).first()
    assert weak is None, f"Weak point created for skipped grammar {gp_a.point_name}"
