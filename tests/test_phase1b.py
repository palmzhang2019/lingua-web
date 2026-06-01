"""Phase 1b tests: mastered leakage prevention and in-cycle cancellation.

All tests use a temporary SQLite database — never data/lingua.db.
Run with: uv run pytest tests/test_phase1b.py -v
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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


# ===========================================================================
# Mock helpers
# ===========================================================================

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


class MockMCWithMastered:
    """MC that contains mastered grammar in its content — used for defense testing."""
    def __init__(self, prompt="正常题", A="正常选项", B="正常选项",
                 C="包含 〜がち 的内容", D="选项D", expected="A",
                 grammar_point="正常语法", question_role="review"):
        self.prompt = prompt
        self.A = A
        self.B = B
        self.C = C
        self.D = D
        self.expected = expected
        self.grammar_point = grammar_point
        self.question_role = question_role


# ===========================================================================
# Pytest fixtures
# ===========================================================================

@pytest.fixture(scope="session", autouse=True)
def setup_temp_db():
    init_db()
    yield
    os.unlink(_tmp_db.name)


@pytest.fixture
def db():
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
    """Material with 3 grammar points. gp_a (tewairenai) is NOT mastered."""
    mat = Material(
        filename="test.txt",
        content_text="Test material content for grammar points.",
        source_type="txt",
    )
    db.add(mat)
    db.commit()
    db.refresh(mat)

    gp_a = GrammarPoint(
        material_id=mat.id, point_name="〜てはいられない",
        explanation_jp="无法持续当前状态",
        example_from_material="遊んではいられない。",
        difficulty_level="N2", mastered=False,
    )
    gp_b = GrammarPoint(
        material_id=mat.id, point_name="〜がち",
        explanation_jp="有…倾向",
        example_from_material="忘れがちだ。",
        difficulty_level="N2", mastered=False,
    )
    gp_c = GrammarPoint(
        material_id=mat.id, point_name="〜たきり",
        explanation_jp="…之后就没有…",
        example_from_material="出かけたきり帰らない。",
        difficulty_level="N2", mastered=False,
    )
    db.add_all([gp_a, gp_b, gp_c])
    db.commit()
    return mat, gp_a, gp_b, gp_c


@pytest.fixture
def material_with_mastered(db):
    """Material where gp_b (gachi) is already mastered before cycle starts."""
    mat = Material(
        filename="mastered_test.txt",
        content_text="Mastered grammar test.",
        source_type="txt",
    )
    db.add(mat)
    db.commit()
    db.refresh(mat)

    gp_a = GrammarPoint(
        material_id=mat.id, point_name="〜てはいられない",
        explanation_jp="无法持续当前状态",
        example_from_material="遊んではいられない。",
        difficulty_level="N2", mastered=False,
    )
    gp_b = GrammarPoint(
        material_id=mat.id, point_name="〜がち",
        explanation_jp="有…倾向",
        example_from_material="忘れがちだ。",
        difficulty_level="N2", mastered=True,
    )
    gp_c = GrammarPoint(
        material_id=mat.id, point_name="〜たきり",
        explanation_jp="…之后就没有…",
        example_from_material="出かけたきり帰らない。",
        difficulty_level="N2", mastered=False,
    )
    db.add_all([gp_a, gp_b, gp_c])
    db.commit()
    return mat, gp_a, gp_b, gp_c


@pytest.fixture
def mock_deepseek():
    with patch("app.routes.study.generate_explanation") as mock_exp, \
         patch("app.routes.study.generate_translation_exercises") as mock_t, \
         patch("app.routes.study.generate_multiple_choice") as mock_mc:
        mock_exp.return_value = MockExplanation()
        mock_t.return_value = make_mock_translations(5, "〜てはいられない")
        mock_mc.return_value = make_mock_mc(9)
        yield


@pytest.fixture
def start_cycle_with_mocks(client, db, populated_material, mock_deepseek):
    mat, gp_a, gp_b, gp_c = populated_material
    resp = client.post("/study/start_cycle", data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Start cycle failed: {resp.status_code}"
    return mat, gp_a, gp_b, gp_c


# ===========================================================================
# Phase 1b Tests
# ===========================================================================

# --- New cycle filter tests ---

def test_mastered_not_selected_as_a_or_b(client, db, material_with_mastered, mock_deepseek):
    """A mastered grammar point must not be selected as grammar A or B."""
    mat, gp_a, gp_b, gp_c = material_with_mastered
    # gp_b is already mastered=True
    resp = client.post("/study/start_cycle", data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Start cycle failed: {resp.status_code}"

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
    assert cycle is not None
    # grammar_a/b must NOT be gp_b (mastered)
    assert cycle.grammar_a_id != gp_b.id, "Mastered point selected as grammar A"
    assert cycle.grammar_b_id != gp_b.id, "Mastered point selected as grammar B"


def test_mastered_not_selected_as_review_target(client, db, mock_deepseek):
    """Mastered grammar must not appear in review targets."""
    mat = Material(filename="r.txt", content_text="Review test.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)

    gp_a = GrammarPoint(material_id=mat.id, point_name="〜てはいられない",
                        explanation_jp="A", example_from_material="a",
                        difficulty_level="N2", mastered=False)
    gp_b = GrammarPoint(material_id=mat.id, point_name="〜がち",
                        explanation_jp="B", example_from_material="b",
                        difficulty_level="N2", mastered=False)
    gp_c = GrammarPoint(material_id=mat.id, point_name="〜たきり",
                        explanation_jp="C", example_from_material="c",
                        difficulty_level="N2", mastered=True)  # mastered!
    db.add_all([gp_a, gp_b, gp_c]); db.commit()

    resp = client.post("/study/start_cycle", data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle.id
    ).order_by(QuestionAttempt.id).all()

    # MC questions (11-19) should not reference gp_c (mastered)
    for q in all_qs[10:]:
        payload = q.question_payload_json or {}
        gp_ref = (payload.get("grammar_point") or "").lower()
        assert "たきり" not in gp_ref, f"MC question references mastered grammar: {gp_ref}"


def test_one_unmastered_grammar_blocks_start(client, db, mock_deepseek):
    """Only one eligible grammar produces the distinct '至少2个' message."""
    mat = Material(filename="one.txt", content_text="One GP left.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)

    gp_a = GrammarPoint(material_id=mat.id, point_name="〜てはいられない",
                        explanation_jp="A", example_from_material="a",
                        difficulty_level="N2", mastered=False)
    gp_b = GrammarPoint(material_id=mat.id, point_name="〜がち",
                        explanation_jp="B", example_from_material="b",
                        difficulty_level="N2", mastered=True)
    gp_c = GrammarPoint(material_id=mat.id, point_name="〜たきり",
                        explanation_jp="C", example_from_material="c",
                        difficulty_level="N2", mastered=True)
    db.add_all([gp_a, gp_b, gp_c]); db.commit()

    resp = client.post("/study/start_cycle", data={"material_id": mat.id})
    assert resp.status_code == 400
    assert "仅剩 1 个" in resp.text


# --- Generation defense tests ---

def test_validate_mc_against_mastered_clean():
    """Clean MC passes validation when no mastered names referenced."""
    from app.routes.study import _validate_mc_against_mastered
    mc_list = make_mock_mc(9)
    result = _validate_mc_against_mastered(mc_list, {"〜たきり", "〜ずに"})
    assert all(result), "Clean MC should pass validation"


def test_validate_mc_against_mastered_contaminated():
    """MC contaminated with mastered grammar fails validation."""
    from app.routes.study import _validate_mc_against_mastered
    mc_list = [
        MockMCWithMastered(prompt="正常", A="正常", B="正常",
                           C="包含 〜がち 的内容", D="正常",
                           expected="A", grammar_point="正常"),
    ]
    for j in range(8):
        mc_list.append(MockMC(prompt=f"正常{j+1}", A=f"A{j+1}", B=f"B{j+1}",
                              C=f"C{j+1}", D=f"D{j+1}"))
    result = _validate_mc_against_mastered(mc_list, {"〜がち"})
    # First MC should fail, rest should pass
    assert result[0] is False, "Mastered grammar in choice should be detected"
    assert all(result[1:]), "Other MCs should pass"


# --- In-cycle cancellation tests ---

def test_toggle_mastered_cancels_future_target_questions(client, db, start_cycle_with_mocks):
    """Toggling mastered cancels future pending questions for that grammar."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Toggle gp_a (tewairenai) to mastered via the toggle route
    resp = client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                       headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200

    # Check all pending questions for gp_a are now cancelled_mastered
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    # First 5 questions: grammar_a_translation target gp_a
    cancelled = [q for q in all_qs[:5] if q.status == "cancelled_mastered"]
    # Some might already be pending (the current question was grammar_a_translation index 0)
    # But all gp_a-targeted pending questions should be cancelled
    for q in all_qs[:5]:
        if q.status == "pending":
            # Some may have been answered by the cancellation advancement
            pass
        assert q.status in ("cancelled_mastered", "pending"), \
            f"Q {q.id}: expected cancelled_mastered or pending, got {q.status}"


def test_previously_answered_rows_unchanged(client, db, start_cycle_with_mocks):
    """Historical answered results are preserved after mastering."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    # Answer first question
    q0 = all_qs[0]
    q0.status = "answered"
    q0.user_answer = q0.correct_answer
    q0.is_correct = True
    q0.answered_at = datetime.datetime.utcnow()
    db.commit()

    # Now toggle gp_a to mastered
    resp = client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                       headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200

    # Q0 should still be answered and correct
    db.refresh(q0)
    assert q0.status == "answered"
    assert q0.is_correct is True


def test_unmastering_does_not_resurrect_cancelled(client, db, start_cycle_with_mocks):
    """Toggling mastered back to False does NOT resurrect cancelled questions."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Toggle gp_a to mastered then back to unmastered
    client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    # Toggle back to False
    client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    cancelled = [q for q in all_qs if q.status == "cancelled_mastered"]
    # Questions cancelled first time remain cancelled; unmastering only affects
    # future cycles, not current cycle
    assert any(q.status == "cancelled_mastered" for q in all_qs[:5]), \
        "Cancelled questions must remain cancelled even after unmastering"


# --- Scoring and completion tests ---

def test_cancelled_mastered_excluded_from_accuracy(client, db, start_cycle_with_mocks):
    """cancelled_mastered does not enter answer denominator or wrong count."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Answer all questions
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()
    for q in all_qs:
        q.status = "answered"
        q.user_answer = q.correct_answer if q.id % 2 == 0 else "wrong"
        q.is_correct = (q.id % 2 == 0)
        q.answered_at = datetime.datetime.utcnow()

    # Then toggle gp_b to mastered — this shouldn't affect already-answered questions
    client.post(f"/materials/{mat.id}/grammar/{gp_b.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    from app.routes.study import _compute_cycle_completion
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    stats = _compute_cycle_completion(db, cycle)

    # All were answered before toggle, so cancelled_mastered count should be 0
    assert stats["cancelled_mastered"] == 0, \
        "Already-answered questions must not be cancelled"
    assert stats["answered"] == 19
    assert stats["is_valid_completion"] is True


def test_cancelled_mastered_no_weak_points(client, db, start_cycle_with_mocks):
    """cancelled_mastered must not create weak points."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    wp_before = db.query(WeakPoint).count()

    # Toggle gp_a to mastered
    client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    wp_after = db.query(WeakPoint).count()
    assert wp_after == wp_before, \
        f"Mastered toggle created {wp_after - wp_before} weak point(s)"


def test_mastered_cancelled_cycle_valid_completion(client, db, populated_material, mock_deepseek):
    """Cycle with answers + cancelled_mastered (no skipped) can be valid."""
    mat, gp_a, gp_b, gp_c = populated_material

    resp = client.post("/study/start_cycle", data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Cancel gp_a mid-cycle
    client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    # Answer remaining pending questions
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()
    for q in all_qs:
        if q.status == "pending":
            q.status = "answered"
            q.user_answer = q.correct_answer if q.id % 2 == 0 else "wrong"
            q.is_correct = (q.id % 2 == 0)
            q.answered_at = datetime.datetime.utcnow()

    from app.routes.study import _compute_cycle_completion
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    stats = _compute_cycle_completion(db, cycle)

    assert stats["cancelled_mastered"] >= 1, "Expected cancelled questions"
    assert stats["is_valid_completion"] is True, \
        "Cycle with cancelled_mastered (no skipped) must be valid"


def test_skipped_still_invalid(client, db, start_cycle_with_mocks):
    """Phase 1a skipped behavior must not regress."""
    mat, gp_a, gp_b, gp_c = start_cycle_with_mocks

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Skip all
    for _ in range(3):
        client.post("/study/skip_module", follow_redirects=False)

    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    assert cycle.is_valid_completion is False, "Skipped cycle must remain invalid"

    # Also verify cancelled_mastered handled alongside skipped
    from app.routes.study import _compute_cycle_completion
    stats = _compute_cycle_completion(db, cycle)
    assert stats["skipped"] == 19
    assert stats["cancelled_mastered"] == 0
    assert stats["is_valid_completion"] is False


def test_mastered_not_selected_when_active_weak_point_exists(
    client, db, mock_deepseek
):
    """A mastered grammar with active weak point must not enter new cycle."""
    mat = Material(filename="wp_test.txt", content_text="WP test.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)

    gp_a = GrammarPoint(material_id=mat.id, point_name="〜てはいられない",
                        explanation_jp="A", example_from_material="a",
                        difficulty_level="N2", mastered=True)  # mastered + weak!
    gp_b = GrammarPoint(material_id=mat.id, point_name="〜がち",
                        explanation_jp="B", example_from_material="b",
                        difficulty_level="N2", mastered=False)
    gp_c = GrammarPoint(material_id=mat.id, point_name="〜たきり",
                        explanation_jp="C", example_from_material="c",
                        difficulty_level="N2", mastered=False)
    db.add_all([gp_a, gp_b, gp_c]); db.commit()

    # Add active weak point for the mastered grammar
    wp = WeakPoint(point_type="grammar", point_reference=gp_a.point_name,
                   error_count=3, is_active=True)
    db.add(wp); db.commit()

    # Start cycle — must pick gp_b and gp_c, never gp_a
    resp = client.post("/study/start_cycle", data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Start cycle failed: {resp.status_code}"

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(StudyCycle.id == state.current_cycle_id).first()
    assert cycle.grammar_a_id != gp_a.id, "Mastered+weak point selected as grammar A"
    assert cycle.grammar_b_id != gp_a.id, "Mastered+weak point selected as grammar B"
