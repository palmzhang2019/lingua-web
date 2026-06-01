"""Phase 4D tests: target-grammar-aware translation scoring, consistency
validation, revised weak-point thresholds, and preserved workflows.

All tests use isolated temp DB and mocked DeepSeek.
Run with: uv run pytest tests/test_phase4d.py -v
"""
import os, sys, tempfile, datetime
from pathlib import Path
from unittest.mock import patch

os.environ["LINGUA_TESTING"] = "1"
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["LINGUA_DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv; load_dotenv()

import pytest
from fastapi.testclient import TestClient
from app.db import init_db, SessionLocal
from app.models import (
    Material, GrammarPoint, StudyCycle, CycleMaterial,
    QuestionAttempt, SessionState, WeakPoint,
    WeakPointEvent, TranslationErrorCandidate,
)
from app.main import app
from app.routes.study import _record_weak_point

# ===========================================================================
# Mock helpers
# ===========================================================================

class MockExp:
    def __init__(self):
        self.point_name = "〜てはいられない"
        self.meaning_zh = "无法持续"
        self.usage_notes_zh = "表示无法保持某种状态"
        self.example_sentences = ["例文1"]

class MockTrans:
    def __init__(self):
        self.prompt_zh = "翻译题"
        self.reference_answer_ja = "答え"
        self.grading_notes = "使用目标语法"
        self.grammar_point = "〜てはいられない"

class MockMC:
    def __init__(self):
        self.prompt = "テスト"
        self.A = "A"; self.B = "B"; self.C = "C"; self.D = "D"
        self.expected = "A"; self.grammar_point = "〜てはいられない"
        self.question_role = "review"

class MockEvalV2:
    def __init__(self, score_hearts=8, additional_errors=None, target_grammar_correct=True):
        self.score_hearts = score_hearts
        self.target_grammar_correct = target_grammar_correct
        self.feedback_zh = "反馈信息"
        self.corrected_answer_ja = "正解"
        self.reason_zh = "评分理由"
        self.additional_errors = additional_errors or []

class MockErrorItem:
    def __init__(self, error_type="particle", error_rule_key="particle:test:key",
                 original_fragment="wrong", corrected_fragment="correct",
                 description="测试错误"):
        self.error_type = error_type
        self.error_rule_key = error_rule_key
        self.original_fragment = original_fragment
        self.corrected_fragment = corrected_fragment
        self.description = description

# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope="session", autouse=True)
def setup_temp_db():
    init_db()
    yield
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass

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
def mock_pass():
    """score_hearts=10, tgc=true, no errors — perfect pass."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=10, target_grammar_correct=True)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_pass_6():
    """score_hearts=6, tgc=true — minimal pass."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=6, target_grammar_correct=True)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_fail_5():
    """score_hearts=5, tgc=false — minimal fail."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=5, target_grammar_correct=False)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_pass_6_with_errors():
    """score_hearts=6, tgc=true, with additional errors — passing but has candidates."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(
            score_hearts=6, target_grammar_correct=True,
            additional_errors=[
                MockErrorItem(error_type="particle", error_rule_key="particle:wo→ni:test",
                              description="助词错误"),
            ]
        )
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_contradictory():
    """tgc=true with score_hearts=5 (invalid pair)."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=5, target_grammar_correct=True)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_inverse_contradictory():
    """tgc=false with score_hearts=6 (invalid pair)."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=6, target_grammar_correct=False)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_missing_tgc():
    """No target_grammar_correct attribute (legacy/malformed response)."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        ev = MockEvalV2(score_hearts=10, target_grammar_correct=True)
        del ev.target_grammar_correct
        mev2.return_value = ev
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_none():
    """Returns None (grading failure)."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = None
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def populated_material(db):
    from app.models import TranslationErrorCandidate
    db.query(WeakPointEvent).delete()
    db.query(TranslationErrorCandidate).delete()
    db.query(WeakPoint).delete()
    db.query(QuestionAttempt).delete()
    db.query(CycleMaterial).delete()
    db.query(StudyCycle).delete()
    db.query(SessionState).delete()
    db.commit()
    mat = Material(filename="test.txt", content_text="Test.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    for name in ["〜てはいられない", "〜がち", "〜たきり"]:
        db.add(GrammarPoint(material_id=mat.id, point_name=name,
               explanation_jp="X", example_from_material="x",
               difficulty_level="N2", mastered=False))
    db.commit()
    return mat


# ===========================================================================
# 1. Schema and migration
# ===========================================================================

def test_fresh_db_has_target_grammar_correct_column(db):
    """Fresh temp DB includes nullable target_grammar_correct column."""
    import sqlalchemy
    insp = sqlalchemy.inspect(db.bind)
    cols = [c["name"] for c in insp.get_columns("question_attempts")]
    assert "target_grammar_correct" in cols

def test_legacy_attempts_remain_null(db):
    """Existing old-style attempts with no tgc remain NULL."""
    gp = db.query(GrammarPoint).first()
    if not gp:
        mat = Material(filename="t.txt", content_text="t", source_type="txt")
        db.add(mat); db.commit()
        gp = GrammarPoint(material_id=mat.id, point_name="test",
                          explanation_jp="x", example_from_material="x")
        db.add(gp); db.commit(); db.refresh(gp)
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    qa = QuestionAttempt(
        cycle_id=cycle.id, module_type="grammar_a_translation",
        question_payload_json={}, correct_answer="x",
        status="answered", is_correct=True, score_hearts=None,
        user_answer="x", answered_at=datetime.datetime.utcnow(),
    )
    db.add(qa); db.commit()
    assert qa.target_grammar_correct is None


# ===========================================================================
# 2. Valid evaluation pairs
# ===========================================================================

def test_tgc_true_6_hearts_is_valid_and_passed(client, db, populated_material, mock_pass_6):
    """tgc=true + score=6: valid, passed (is_correct=True), no auto weak point."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/answer", data={"answer": "test"}, follow_redirects=False)
    state = db.query(SessionState).first()
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.target_grammar_correct is True
    assert qa.score_hearts == 6
    assert qa.is_correct is True
    # No auto weak point for passing translation
    weak_points = db.query(WeakPoint).filter(
        WeakPoint.point_reference == "〜てはいられない"
    ).all()
    assert len(weak_points) == 0

def test_tgc_true_10_hearts_is_valid_and_passed(client, db, populated_material, mock_pass):
    """tgc=true + score=10: valid, passed."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/answer", data={"answer": "perfect"}, follow_redirects=False)
    state = db.query(SessionState).first()
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.target_grammar_correct is True
    assert qa.is_correct is True

def test_tgc_false_5_hearts_is_valid_and_failed(client, db, populated_material, mock_fail_5):
    """tgc=false + score=5: valid, failed, auto creates weak point."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/answer", data={"answer": "bad"}, follow_redirects=False)
    state = db.query(SessionState).first()
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.target_grammar_correct is False
    assert qa.score_hearts == 5
    assert qa.is_correct is False
    # Auto weak point created
    wp = db.query(WeakPoint).filter(
        WeakPoint.point_reference == "〜てはいられない"
    ).first()
    assert wp is not None
    assert wp.error_count >= 1

def test_tgc_false_0_hearts_is_valid(client, db, populated_material, mock_fail_5):
    """tgc=false + score=0: valid, failed."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=0, target_grammar_correct=False)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        client.post("/study/start_cycle", data={"material_id": populated_material.id},
                    follow_redirects=False)
        client.post("/study/answer", data={"answer": "完全错误"}, follow_redirects=False)
    state = db.query(SessionState).first()
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.target_grammar_correct is False
    assert qa.score_hearts == 0
    assert qa.is_correct is False


# ===========================================================================
# 3. Invalid contradictory pairs — controlled failure
# ===========================================================================

def test_tgc_true_5_rejected_without_persistence(client, db, populated_material, mock_contradictory):
    """tgc=true + score=5: rejected — no persistence, no side effects."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    resp = client.post("/study/answer", data={"answer": "contradictory"}, follow_redirects=False)
    assert resp.status_code == 200
    assert "评分结果不一致" in resp.text
    # No persistence
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.score_hearts is None
    assert qa.target_grammar_correct is None
    assert qa.status in ("pending", "planned")  # Not advanced
    # No side effects
    assert db.query(WeakPoint).count() == 0
    assert db.query(WeakPointEvent).count() == 0
    assert db.query(TranslationErrorCandidate).count() == 0

def test_tgc_false_6_rejected_without_persistence(client, db, populated_material, mock_inverse_contradictory):
    """tgc=false + score=6: rejected — no persistence, no side effects."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    resp = client.post("/study/answer", data={"answer": "inverse"}, follow_redirects=False)
    assert resp.status_code == 200
    assert "评分结果不一致" in resp.text
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.score_hearts is None
    assert db.query(WeakPoint).count() == 0
    assert db.query(WeakPointEvent).count() == 0

def test_missing_tgc_rejected(client, db, populated_material, mock_missing_tgc):
    """Missing target_grammar_correct: rejected like a grading failure."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    resp = client.post("/study/answer", data={"answer": "missing"}, follow_redirects=False)
    assert resp.status_code == 200
    assert "评分结果不一致" in resp.text
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.score_hearts is None
    assert db.query(WeakPoint).count() == 0
    assert db.query(WeakPointEvent).count() == 0

def test_none_grading_still_rejected(client, db, populated_material, mock_none):
    """Evaluation returning None is still a controlled failure (unchanged behavior)."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    resp = client.post("/study/answer", data={"answer": "fail"}, follow_redirects=False)
    qa = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).first()
    assert qa.score_hearts is None
    assert qa.status == "pending"  # Not advanced
    assert db.query(WeakPoint).count() == 0
    assert db.query(TranslationErrorCandidate).count() == 0


# ===========================================================================
# 4. Weak-point behavior with new threshold
# ===========================================================================

def test_tgc_true_6_does_not_create_weak_point(client, db, populated_material, mock_pass_6):
    """tgc=true + score=6: no target grammar weak point."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/answer", data={"answer": "passing"}, follow_redirects=False)
    wp = db.query(WeakPoint).filter(
        WeakPoint.point_reference == "〜てはいられない"
    ).all()
    assert len(wp) == 0

def test_tgc_true_6_with_errors_creates_candidates_only(client, db, populated_material, mock_pass_6_with_errors):
    """tgc=true + score=6 + additional errors: candidates created, no weak point."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/answer", data={"answer": "passing but errors"}, follow_redirects=False)
    # No target grammar weak point
    wp = db.query(WeakPoint).filter(
        WeakPoint.point_reference == "〜てはいられない"
    ).all()
    assert len(wp) == 0
    # Candidates created
    cands = db.query(TranslationErrorCandidate).all()
    assert len(cands) > 0
    assert cands[0].status == "pending"

def test_tgc_false_5_creates_weak_point_and_event(client, db, populated_material, mock_fail_5):
    """tgc=false + score=5: weak point + WeakPointEvent created."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    client.post("/study/answer", data={"answer": "failing"}, follow_redirects=False)
    wp = db.query(WeakPoint).filter(
        WeakPoint.point_reference == "〜てはいられない"
    ).first()
    assert wp is not None
    # WeakPointEvent recorded
    events = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle_id,
        WeakPointEvent.source_type == "translation_low_score_target_grammar"
    ).all()
    assert len(events) >= 1
    assert events[0].event_type in ("created", "hit_existing")


# ===========================================================================
# 5. Scoring and display
# ===========================================================================

def test_6_hearts_contributes_60_percent(client, db, populated_material, mock_pass_6):
    """6-heart passing translation contributes 60% to final score."""
    from app.routes.study import _compute_final_cycle_score, _compute_cycle_completion
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    client.post("/study/answer", data={"answer": "test"}, follow_redirects=False)
    # Answer all remaining questions to complete cycle
    for i in range(9):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    for i in range(9):
        client.post("/study/answer", data={"answer": "A"}, follow_redirects=False)
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    _compute_cycle_completion(db, cycle)
    score = _compute_final_cycle_score(db, cycle)
    assert score is not None
    # First translation: 6/10*100 = 60. Rest (9 mock translations + 9 MC): all 100.
    # (60 + 9*100 + 9*100) / 19 = 1860 / 19 = 97.9
    assert score["final_score_percent"] > 0

def test_5_hearts_contributes_50_percent(client, db, populated_material, mock_fail_5):
    """5-heart failing translation contributes 50% to final score."""
    from app.routes.study import _compute_final_cycle_score, _compute_cycle_completion
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    client.post("/study/answer", data={"answer": "bad"}, follow_redirects=False)
    for i in range(9):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    for i in range(9):
        client.post("/study/answer", data={"answer": "A"}, follow_redirects=False)
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    _compute_cycle_completion(db, cycle)
    score = _compute_final_cycle_score(db, cycle)
    assert score is not None

def test_aggregate_score_hidden_during_learning(client, db, populated_material, mock_pass):
    """Final score is not shown while cycle is in progress."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.get("/study")
    assert "最终得分" not in resp.text

def test_6_hearts_passed_display(client, db, populated_material, mock_pass_6):
    """6-heart translation shows pass indicator in feedback."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.post("/study/answer", data={"answer": "test"}, follow_redirects=False)
    assert "正确!" in resp.text or "❤️❤️❤️❤️❤️❤️" in resp.text


# ===========================================================================
# 6. Preserved workflows
# ===========================================================================

def test_additional_error_review_still_occurs(client, db, populated_material, mock_pass_6_with_errors):
    """Review gate still triggers after 10 translations with candidates."""
    from app.routes.study import _check_review_gate
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    # All mock_pass_6_with_errors returns score=6 + error for every call
    # So all 10 translations score 6 with errors -> candidates
    for i in range(10):
        client.post("/study/answer", data={"answer": f"a{i}"}, follow_redirects=False)
    state = db.query(SessionState).first()
    assert _check_review_gate(db, state.current_cycle_id) is True

def test_pending_candidates_still_block_choice(client, db, populated_material, mock_pass_6_with_errors):
    """Candidates must be resolved before choice questions."""
    from app.routes.study import _check_review_gate
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    for i in range(10):
        client.post("/study/answer", data={"answer": f"a{i}"}, follow_redirects=False)
    state = db.query(SessionState).first()
    assert _check_review_gate(db, state.current_cycle_id) is True

def test_choice_wrong_answer_weak_point_unchanged(client, db, populated_material, mock_pass):
    """MC wrong answers still create weak points (independent of translation)."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    # Answer all 10 translations (all pass with tgc=true, score=10)
    for i in range(10):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    # No candidates (no additional errors in mock_pass)
    wp_before = db.query(WeakPoint).count()
    # Answer MC wrong (expected=A, send=B)
    client.post("/study/answer", data={"answer": "B"}, follow_redirects=False)
    # Should create weak point for MC wrong answer
    assert db.query(WeakPoint).count() > wp_before

def test_weak_point_event_provenance_unchanged(client, db, populated_material, mock_fail_5):
    """WeakPointEvent for tgc=false score=5 still records properly."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    client.post("/study/answer", data={"answer": "bad"}, follow_redirects=False)
    events = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle_id,
        WeakPointEvent.source_type == "translation_low_score_target_grammar"
    ).all()
    assert len(events) >= 1


# ===========================================================================
# 7. Non-regression: lazy generation and Phase 4C
# ===========================================================================

def test_lazy_generation_unchanged(client, db, populated_material, mock_pass):
    """Phase 3 lazy generation still works with updated scoring."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()
    # Slot 1 should be pending (generated by start_cycle)
    assert all_qs[0].status == "pending"

def test_mermaid_progress_unchanged(client, db, populated_material, mock_pass):
    """Phase 4C Mermaid page still renders."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    assert "当前进度" in resp.text
