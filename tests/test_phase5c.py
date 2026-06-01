"""
Phase 5C tests: sanitized diagnostic error codes for MC regeneration failure.

All tests use isolated temporary databases and mocked LLM calls.
No real learning data is accessed.
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


# =============================================================================
# Helper: complete translation phase through public Phase 5A flow
# =============================================================================

def complete_translations(client, mt_mock, mat_id=1):
    """Start cycle and answer all 10 translations through public Phase 5A flow."""
    client.post("/study/start_cycle", data={"material_id": mat_id},
                follow_redirects=False)

    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
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


# =============================================================================
# Tests: sanitized diagnostic error codes
# =============================================================================

def test_mc_retry_failure_returns_sanitized_error_code(client, db, populated_material):
    """A failed /study/regenerate_mc response contains approved error_code only.
    Response does not contain raw exception text or secret-like values."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()

        # First MC generation fails → tuple return (simulating generator.py change)
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")

        mat = populated_material
        complete_translations(client, mt, mat_id=mat.id)

        retry_resp = client.post("/study/regenerate_mc")
        retry_data = retry_resp.json()

    assert retry_data["ok"] is False
    assert "error_code" in retry_data, "Response must contain error_code"
    code = retry_data["error_code"]
    # Must be one of the approved sanitized codes
    approved_codes = [
        "MC_LLM_UNAVAILABLE",
        "MC_API_OR_PARSE_FAILURE",
        "MC_MASTERED_GRAMMAR_CONTAMINATION",
        "MC_UNKNOWN_GENERATION_FAILURE",
    ]
    assert code in approved_codes, f"Unexpected error_code: {code}"
    # Must NOT contain raw exception text, prompt content, or secret-like values
    assert "sk-" not in code, "Secret prefix leaked into error_code"
    assert "{" not in code, "JSON content leaked into error_code"
    assert "Exception" not in code, "Exception text leaked into error_code"


def test_mc_llm_unavailable_maps_to_safe_code_through_production_code_path(client, db, populated_material):
    """LLM unavailable maps to MC_LLM_UNAVAILABLE via the production code path.

    We simulate is_available=False by having the mock return (None, "MC_LLM_UNAVAILABLE")
    which matches what the real generator returns in that case, and verify
    the error_code propagates through _generate_slot_content and regenerate_mc.
    """
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()
        mmc.return_value = (None, "MC_LLM_UNAVAILABLE")

        mat = populated_material
        complete_translations(client, mt, mat_id=mat.id)

        retry_resp = client.post("/study/regenerate_mc")
        retry_data = retry_resp.json()

    assert retry_data["ok"] is False
    assert retry_data.get("error_code") == "MC_LLM_UNAVAILABLE", \
        f"Expected MC_LLM_UNAVAILABLE, got {retry_data.get('error_code')}"


def test_mc_api_parse_failure_maps_to_safe_code(client, db, populated_material):
    """Mocked API/parse failure produces MC_API_OR_PARSE_FAILURE."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()

        # Simulate the real generator's tuple return for structured_extraction failure
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")

        mat = populated_material
        complete_translations(client, mt, mat_id=mat.id)

        retry_resp = client.post("/study/regenerate_mc")
        retry_data = retry_resp.json()

    assert retry_data["ok"] is False
    assert retry_data.get("error_code") == "MC_API_OR_PARSE_FAILURE", \
        f"Expected MC_API_OR_PARSE_FAILURE, got {retry_data.get('error_code')}"


def test_mastered_grammar_rejection_maps_to_safe_error_code(client, db, populated_material):
    """Existing mastered-grammar rejection is reported as MC_MASTERED_GRAMMAR_CONTAMINATION."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()

        # Mark "〜てはいられない" as globally mastered
        mat = populated_material
        gp = db.query(GrammarPoint).filter(GrammarPoint.point_name == "〜てはいられない").first()
        gp.mastered = True
        db.commit()

        # MockMC that outputs content containing the mastered grammar name
        class ContaminatedMockMC:
            def __init__(self):
                self.prompt = "MC题目（用于练习「てはいられない」）"
                self.A = "选项A"; self.B = "选项B"; self.C = "选项C"; self.D = "选项D"
                self.expected = "A"; self.grammar_point = "〜てはいられない"
                self.question_role = "review"

        mmc.return_value = ContaminatedMockMC()

        complete_translations(client, mt, mat_id=mat.id)

        # MC retry: the slot should be pending (not generation_failed) because
        # the mock returns a valid MC. Actually, the mastered contamination
        # happens inside _generate_slot_content before the slot goes pending.
        # But with the mock returning MockMC() directly, the production code
        # will check content against mastered names.
        # Let me verify by checking the retry response.

        retry_resp = client.post("/study/regenerate_mc")
        retry_data = retry_resp.json()

    # The mock returns MockMC which has grammar_point="〜てはいられない"
    # and that grammar is mastered → should be contamination
    assert retry_data["ok"] is False
    assert retry_data.get("error_code") == "MC_MASTERED_GRAMMAR_CONTAMINATION", \
        f"Expected MC_MASTERED_GRAMMAR_CONTAMINATION, got {retry_data.get('error_code')}"


def test_successful_mc_retry_contains_no_error_code(client, db, populated_material):
    """Successful retry response has ok:True and no error_code."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()

        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")

        mat = populated_material
        complete_translations(client, mt, mat_id=mat.id)

        # First attempt fails
        retry_resp = client.post("/study/regenerate_mc")
        assert retry_resp.json()["ok"] is False

        # Now MC succeeds
        mmc.return_value = MockMC()
        retry_resp2 = client.post("/study/regenerate_mc")
        retry_data = retry_resp2.json()

    assert retry_data["ok"] is True
    assert "error_code" not in retry_data, \
        "Successful retry must not contain error_code"


def test_failed_mc_page_shows_friendly_message_and_safe_diagnostic_code(client, db, populated_material):
    """Failed MC page renders friendly message and sanitized diagnostic code."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")

        mat = populated_material
        complete_translations(client, mt, mat_id=mat.id)

        resp = client.get("/study/current")
        html = resp.text

    # Friendly message preserved
    assert "选择题生成失败" in html, "Friendly failure message missing"
    assert "请重试" in html, "Retry prompt missing"
    # Sanitized diagnostic code displayed (not raw content)
    assert "MC_API_OR_PARSE_FAILURE" in html, \
        "Sanitized diagnostic code not rendered in page"
    # Raw exception/output/content NOT rendered
    assert "Exception" not in html, "Raw exception leaked"
    assert "sk-" not in html, "Secret leaked"


def test_successful_mc_retry_clears_prior_diagnostic_and_shows_question(client, db, populated_material):
    """After successful retry, diagnostic code is gone and question is shown."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")

        mat = populated_material
        complete_translations(client, mt, mat_id=mat.id)

        # Verify failure state
        resp = client.get("/study/current")
        assert "MC_API_OR_PARSE_FAILURE" in resp.text

        # Now succeed on retry
        mmc.return_value = MockMC()
        retry_resp = client.post("/study/regenerate_mc")
        assert retry_resp.json()["ok"] is True

        # After redirect, no diagnostic code
        after = client.get("/study/current")
        html = after.text
        assert "选择题生成失败" not in html, "Failure message still present after retry"
        assert "MC题目" in html, "MC prompt not visible after retry"
        assert "选项A" in html, "MC choices not visible after retry"
        # Diagnostic code should not be present
        assert "诊断代码" not in html or "MC_API_OR_PARSE_FAILURE" not in html, \
            "Diagnostic code persisted after successful retry"


def test_phase5b_claim_and_concurrency_guard_remain_intact(client, db, populated_material):
    """Phase 5B concurrency guard and claim semantics are preserved after diagnostic changes."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2()
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")

        mat = populated_material
        complete_translations(client, mt, mat_id=mat.id)

        # Verify slot is generation_failed
        state = db.query(SessionState).first()
        all_qs = db.query(QuestionAttempt).filter(
            QuestionAttempt.cycle_id == state.current_cycle_id
        ).order_by(QuestionAttempt.id).all()
        mc_q = all_qs[state.current_question_index]
        assert mc_q.status == "generation_failed", \
            f"Expected generation_failed, got {mc_q.status}"

        # Manually set to generating to simulate concurrent claim
        mc_q.status = "generating"
        mc_q.generation_started_at = None
        db.commit()

        # Concurrent retry should be rejected without invoking generator
        mmc.reset_mock()
        mmc.call_count = 0

        retry_resp = client.post("/study/regenerate_mc")
        retry_data = retry_resp.json()

    assert retry_data["ok"] is False, "Concurrent retry should be rejected"
    error = retry_data.get("error", "")
    assert error in ("claim_failed", "not_retryable"), \
        f"Expected claim_failed or not_retryable, got {retry_data}"
    assert mmc.call_count == 0, \
        f"Generator called {mmc.call_count} times despite slot being generating"
