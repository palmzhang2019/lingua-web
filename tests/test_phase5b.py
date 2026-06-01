"""
Phase 5B tests: MC generation failure feedback and retry recovery.

All tests use isolated temporary databases and mocked LLM calls.
"""
import os, sys, tempfile, json
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
    QuestionAttempt, SessionState, TranslationErrorCandidate, WeakPoint,
)
from app.main import app
from app.agents.generator import TranslationExercise


# =============================================================================
# Mock helpers
# =============================================================================

class MockExp:
    def __init__(self):
        self.point_name = "〜てはいられない"
        self.meaning_zh = "无法持续"
        self.usage_notes_zh = "表示无法保持某种状态"
        self.example_sentences = ["例文1"]

class MockTrans:
    _call_count = 0
    def __init__(self, prompt_prefix="翻译题"):
        MockTrans._call_count += 1
        self.prompt_zh = f"{prompt_prefix} {MockTrans._call_count}"
        self.reference_answer_ja = "答え"
        self.grading_notes = "使用目标语法"
        self.grammar_point = "〜てはいられない"

class MockMC:
    def __init__(self):
        self.prompt = "MC题目"
        self.A = "选项A"; self.B = "选项B"; self.C = "选项C"; self.D = "选项D"
        self.expected = "A"; self.grammar_point = "〜てはいられない"
        self.question_role = "review"

class MockEvalV2:
    def __init__(self, score_hearts=10, target_grammar_correct=True, additional_errors=None):
        self.score_hearts = score_hearts
        self.target_grammar_correct = target_grammar_correct
        self.feedback_zh = "反馈"
        self.corrected_answer_ja = "正解"
        self.reason_zh = "理由"
        self.additional_errors = additional_errors or []


# =============================================================================
# Fixtures
# =============================================================================

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
    # Clean all tables before each test
    for t in [TranslationErrorCandidate, WeakPoint, SessionState,
              QuestionAttempt, CycleMaterial, StudyCycle, GrammarPoint, Material]:
        session.query(t).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture(autouse=True)
def reset_counter():
    MockTrans._call_count = 0

@pytest.fixture
def populated_material(db):
    mat = Material(filename="test.txt", content_text="Test.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    for name in ["〜てはいられない", "〜がち", "〜たきり"]:
        db.add(GrammarPoint(material_id=mat.id, point_name=name,
               explanation_jp="X", example_from_material="x",
               difficulty_level="N2", mastered=False))
    db.commit()
    return mat

@pytest.fixture
def mock_all():
    """Mock all LLM calls: translation, explanation, MC, evaluation."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()
        yield mt, mmc


# =============================================================================
# Helper: complete translation phase through public Phase 5A flow
# =============================================================================

def complete_translations(client, mt_mock, mat_id=1, mmc_mock=None, with_errors=False):
    """Start cycle and answer all 10 translations through public Phase 5A flow.
    mat_id: material id passed to start_cycle.
    """
    # Start the cycle first
    client.post("/study/start_cycle", data={"material_id": mat_id},
                follow_redirects=False)

    errors = [MockErrorItem()] if with_errors else []

    def _error_eval(*a, **kw):
        return MockEvalV2(score_hearts=7, target_grammar_correct=True,
                          additional_errors=errors)

    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        mev2.side_effect = _error_eval if with_errors else None
        if not with_errors:
            mev2.return_value = MockEvalV2()

        # Generate GA
        mt_mock.side_effect = [MockTrans() for _ in range(5)]
        client.post("/study/generate_module")
        for i in range(5):
            client.post("/study/answer", data={"answer": str(i)}, follow_redirects=False)

        # Generate GB
        mt_mock.side_effect = [MockTrans() for _ in range(5)]
        client.post("/study/generate_module")
        for i in range(5):
            client.post("/study/answer", data={"answer": str(i + 5)}, follow_redirects=False)


class MockErrorItem:
    def __init__(self):
        self.error_type = "particle"
        self.error_rule_key = "particle:test:key"
        self.original_fragment = "wrong"
        self.corrected_fragment = "correct"
        self.description = "测试错误"


# =============================================================================
# Tests: MC failure renders retry feedback (not blank page)
# =============================================================================

def test_failed_current_mc_renders_retry_feedback_instead_of_blank_question(
    client, db, populated_material, mock_all,
):
    """MC slot generation_failed → /study/current shows retry feedback, not blank form."""
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = None  # MC generation fails

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    resp = client.get("/study/current")
    html = resp.text

    # Must show failure message
    assert "选择题生成失败" in html, "Missing failure message"
    assert "请重试" in html, "Missing retry prompt"
    # Retry action button must exist
    assert "重新生成" in html or "重试" in html, "Missing retry action"
    # Should NOT show an empty answer form as if MC is valid
    # If we see "MC题目" (from the mock) it means a valid MC was rendered
    assert "MC题目" not in html, "Should not render a valid MC when generation_failed"


def test_retry_failed_current_mc_successfully_displays_regenerated_question(
    client, db, populated_material, mock_all,
):
    """Retry on failed MC slot succeeds → slot goes pending → /study/current shows question."""
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = None  # First attempt fails

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    # Verify failure state
    resp = client.get("/study/current")
    assert "选择题生成失败" in resp.text

    # Retry: now MC succeeds
    mmc_mock.return_value = MockMC()
    retry_resp = client.post("/study/regenerate_mc")
    retry_data = json.loads(retry_resp.body) if hasattr(retry_resp, 'body') else retry_resp.json()
    assert retry_data["ok"] is True, f"Retry failed: {retry_data}"

    # Verify slot is now pending
    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()
    mc_q = all_qs[state.current_question_index]
    assert mc_q.status == "pending", f"Expected pending after retry, got {mc_q.status}"
    assert mc_q.question_payload_json is not None
    assert mc_q.question_payload_json.get("prompt") == "MC题目"

    # /study/current should now render the MC question
    after = client.get("/study/current")
    assert "选择题生成失败" not in after.text, "Failure message still present after retry"
    assert "MC题目" in after.text, "MC question prompt not visible after retry"
    assert "选项A" in after.text, "MC choices not visible after retry"


def test_retry_failed_current_mc_failure_remains_retryable_and_not_generating(
    client, db, populated_material, mock_all,
):
    """Retry on failed MC slot fails → slot stays generation_failed, not generating."""
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = None  # Both first and retry fail

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    # Verify initial failure
    resp = client.get("/study/current")
    assert "选择题生成失败" in resp.text

    # Retry: still fails
    retry_resp = client.post("/study/regenerate_mc")
    retry_data = json.loads(retry_resp.body) if hasattr(retry_resp, 'body') else retry_resp.json()
    assert retry_data["ok"] is False, "Expected retry to fail"

    # Verify slot is NOT in generating (left in retryable state)
    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()
    mc_q = all_qs[state.current_question_index]
    assert mc_q.status == "generation_failed", \
        f"Expected generation_failed after failed retry, got {mc_q.status}"
    assert mc_q.status != "generating", "Must not leave slot in generating"

    # Failure/retry UI should still be visible
    after = client.get("/study/current")
    assert "选择题生成失败" in after.text, "Failure message should persist after failed retry"


def test_retry_does_not_overwrite_pending_or_answered_mc_question(
    client, db, populated_material, mock_all,
):
    """Retry endpoint rejects requests for pending or answered MC slots."""
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = MockMC()  # MC generation succeeds first time

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    # MC should be pending now
    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()
    mc_q = all_qs[state.current_question_index]
    assert mc_q.status == "pending", "MC should be pending"

    # Try to retry pending MC — should be rejected
    retry_resp = client.post("/study/regenerate_mc")
    retry_data = json.loads(retry_resp.body) if hasattr(retry_resp, 'body') else retry_resp.json()
    assert retry_data["ok"] is False, "Should reject retry on pending slot"
    assert "not_retryable" in retry_data.get("error", ""), \
        f"Unexpected error: {retry_data.get('error')}"


def test_normal_mc_pending_rendering_is_unchanged(
    client, db, populated_material, mock_all,
):
    """A normally-generated pending MC question renders as before (no failure UI)."""
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = MockMC()  # MC succeeds normally

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    # Normal rendering — no failure message
    resp = client.get("/study/current")
    html = resp.text
    assert "选择题生成失败" not in html, "Failure message should not appear for valid MC"
    assert "MC题目" in html, "MC prompt should be visible"
    assert "选项A" in html, "MC choices should be visible"


def test_retry_failed_current_mc_claims_generating_before_generation(
    client, db, populated_material, mock_all,
):
    """A generation_failed MC slot is atomically claimed as generating before calling the generator.

    After successful generation the slot transitions to pending.
    """
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = None  # Will be overridden below

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    # Override MC mock to succeed so we can verify the full flow
    mmc_mock.return_value = MockMC()
    mmc_mock.side_effect = None

    # Intercept _generate_slot_content to inspect state when it runs
    import app.routes.study as _rs
    original_gsc = _rs._generate_slot_content
    captured_state = {}

    def _intercept_gsc(db, slot, *args, **kwargs):
        captured_state["status_when_called"] = slot.status
        captured_state["slot_id"] = slot.id
        slot_after = db.query(QuestionAttempt).filter(
            QuestionAttempt.id == slot.id
        ).first()
        captured_state["db_status_when_called"] = slot_after.status if slot_after else "NOT_FOUND"
        return original_gsc(db, slot, *args, **kwargs)

    with patch.object(_rs, "_generate_slot_content", _intercept_gsc):
        retry_resp = client.post("/study/regenerate_mc")
        retry_data = retry_resp.json()

    assert retry_data["ok"] is True, f"Retry should succeed: {retry_data}"
    # At the moment _generate_slot_content is called, the slot must be generating
    assert captured_state.get("status_when_called") == "generating", \
        f"Expected generating when _generate_slot_content called, got {captured_state.get('status_when_called')}"
    assert captured_state.get("db_status_when_called") == "generating", \
        f"Expected generating from DB, got {captured_state.get('db_status_when_called')}"

    # After the full request returns, the slot should be pending
    state = db.query(SessionState).first()
    mc_q = db.query(QuestionAttempt).filter(
        QuestionAttempt.id == captured_state.get("slot_id")
    ).first()
    assert mc_q is not None
    assert mc_q.status == "pending", \
        f"Expected pending after successful retry, got {mc_q.status}"


def test_retry_failed_current_mc_failure_restores_generation_failed(
    client, db, populated_material, mock_all,
):
    """When regeneration fails after claiming, the slot is restored to generation_failed."""
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = None  # MC generation always fails

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    # Intercept to capture state at claim time
    import app.routes.study as _rs
    original_gsc = _rs._generate_slot_content
    captured_state = {}

    def _intercept_gsc(db, slot, *args, **kwargs):
        captured_state["status_when_called"] = slot.status
        return original_gsc(db, slot, *args, **kwargs)

    with patch.object(_rs, "_generate_slot_content", _intercept_gsc):
        retry_resp = client.post("/study/regenerate_mc")
        retry_data = retry_resp.json()

    assert retry_data["ok"] is False, "Expected retry to fail"
    # Slot was claimed as generating before _generate_slot_content
    assert captured_state.get("status_when_called") == "generating", \
        f"Expected generating when called, got {captured_state.get('status_when_called')}"

    # After failure, slot must NOT be generating — restored to generation_failed
    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()
    mc_q = all_qs[state.current_question_index]
    assert mc_q.status == "generation_failed", \
        f"Expected generation_failed after failed retry, got {mc_q.status}"
    assert mc_q.status != "generating", "Must not leave slot in generating"


def test_concurrent_or_repeated_mc_retry_does_not_regenerate_generating_slot(
    client, db, populated_material, mock_all,
):
    """When MC slot is already generating, a second retry is rejected without invoking generator."""
    mt_mock, mmc_mock = mock_all
    mmc_mock.return_value = None  # MC generation fails

    mat = populated_material
    complete_translations(client, mt_mock, mat_id=mat.id)

    # Manually set the slot to generating (simulating a concurrent request's claim)
    state = db.query(SessionState).first()
    db.refresh(state)
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()
    mc_q = all_qs[state.current_question_index]
    mc_q.status = "generating"
    mc_q.generation_started_at = None
    db.commit()

    # Track whether generate_one_multiple_choice is called
    mmc_mock.reset_mock()
    mmc_mock.call_count = 0

    # Attempt retry while slot is already generating
    retry_resp = client.post("/study/regenerate_mc")
    retry_data = retry_resp.json()

    # Must be rejected — the guard catches generating before the claim step
    assert retry_data["ok"] is False, "Should reject retry on generating slot"
    # Either claim_failed or not_retryable is acceptable (both reject correctly)
    error = retry_data.get("error", "")
    assert error in ("claim_failed", "not_retryable"), \
        f"Expected claim_failed or not_retryable, got {retry_data}"

    # The generator must NOT have been called
    assert mmc_mock.call_count == 0, \
        f"Generator was called {mmc_mock.call_count} times despite slot being generating"
