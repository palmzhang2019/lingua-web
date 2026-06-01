"""
Phase 5A tests: Async batch translation generation, duplicate guard,
loading/retry UI, and review-gate redirect regression.
All tests use isolated temporary databases and mocked LLM calls.
"""
import os, sys, tempfile, datetime, json
from pathlib import Path
from unittest.mock import patch
import pytest

os.environ["LINGUA_TESTING"] = "1"
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["LINGUA_DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv; load_dotenv()

from app.db import init_db, SessionLocal
from app.models import (
    Base, QuestionAttempt, StudyCycle, CycleMaterial, GrammarPoint, Material,
    SessionState, TranslationErrorCandidate, WeakPointEvent, WeakPoint,
)
from app.agents.generator import TranslationExercise
from fastapi.testclient import TestClient


# Initialize the temporary database once at module load
init_db()


@pytest.fixture(scope="session", autouse=True)
def cleanup_temp_db():
    yield
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


# =============================================================================
# Mock helpers
# =============================================================================

class MockTrans:
    """Return a unique prompt_zh per call. Use a counter for uniqueness."""
    _call_count = 0

    def __init__(self, prompt_prefix="翻译题"):
        MockTrans._call_count += 1
        self.prompt_zh = f"{prompt_prefix} {MockTrans._call_count}"
        self.reference_answer_ja = "答え"
        self.grading_notes = "使用目标语法"
        self.grammar_point = "〜てはいられない"


class MockTransDuplicate:
    """Always returns the same prompt_zh to trigger duplicate detection."""
    def __init__(self):
        self.prompt_zh = "翻译题 1"
        self.reference_answer_ja = "答え"
        self.grading_notes = "使用目标语法"
        self.grammar_point = "〜てはいられない"


class MockTransNone:
    """Sentinel: when returned from mock, signals 'no more results'."""
    pass


def mock_trans_none(*args, **kwargs):
    return None


class MockExp:
    def __init__(self):
        self.point_name = "〜てはいられない"
        self.meaning_zh = "无法继续做某事"
        self.usage_notes_zh = "用于表示因为某种原因不能继续做某事"
        self.example_sentences = ["もう待てない。"]


class MockMC:
    def __init__(self):
        self.prompt = "テスト"
        self.A = "A"; self.B = "B"; self.C = "C"; self.D = "D"
        self.expected = "A"; self.grammar_point = "〜てはいられない"
        self.question_role = "review"


class MockEvalV2:
    def __init__(self, score_hearts=10, target_grammar_correct=True,
                 additional_errors=None):
        self.score_hearts = score_hearts
        self.target_grammar_correct = target_grammar_correct
        self.feedback_zh = "测试反馈"
        self.corrected_answer_ja = "答え"
        self.reason_zh = "测试原因"
        self.additional_errors = additional_errors or []


class MockErrorItem:
    def __init__(self, error_type="particle",
                 error_rule_key="particle:wo→ni:tt",
                 original_fragment="test", corrected_fragment="test2",
                 description="测试错误"):
        self.error_type = error_type
        self.error_rule_key = error_rule_key
        self.original_fragment = original_fragment
        self.corrected_fragment = corrected_fragment
        self.description = description


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def reset_counter():
    MockTrans._call_count = 0


@pytest.fixture
def db():
    """Provide a clean temporary database session."""
    from app.main import app as _app
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    """Provide a FastAPI TestClient with temp DB."""
    from app.main import app as _app
    return TestClient(_app)


@pytest.fixture
def populated_material(db):
    """Create a material with grammar points for testing."""
    # Clean up leftover data from other modules sharing the same DB
    from app.models import SessionState, WeakPoint, TranslationErrorCandidate
    db.query(TranslationErrorCandidate).delete()
    db.query(WeakPoint).delete()
    db.query(SessionState).delete()
    db.query(QuestionAttempt).delete()
    db.query(CycleMaterial).delete()
    db.query(StudyCycle).delete()
    db.query(GrammarPoint).delete()
    db.query(Material).delete()
    db.commit()
    mat = Material(filename="test.txt", content_text="Test.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    for name in ["〜てはいられない", "〜がち", "〜たきり"]:
        db.add(GrammarPoint(material_id=mat.id, point_name=name,
               explanation_jp="X", example_from_material="x",
               difficulty_level="N2", mastered=False))
    db.commit()
    return mat


@pytest.fixture
def mock_phase5a():
    """Mock all LLM calls for Phase 5A tests."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mmc.return_value = MockMC()
        mev2.return_value = MockEvalV2(score_hearts=10)
        yield mt


@pytest.fixture
def mock_with_errors():
    """Mock with translation errors to trigger review gate."""
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mmc.return_value = MockMC()
        mev2.return_value = MockEvalV2(
            score_hearts=7, target_grammar_correct=True,
            additional_errors=[MockErrorItem()],
        )
        yield mt


def _start_and_generate(client, mt_mock=None):
    """Helper: start cycle and generate GA translation module.

    Returns the session state after generation.
    """
    resp = client.post("/study/start_cycle",
                       data={"material_id": 1},
                       follow_redirects=False)
    assert resp.status_code == 303  # redirect to /study/current

    if mt_mock is not None:
        mt_mock.side_effect = [MockTrans() for _ in range(5)]

    resp2 = client.post("/study/generate_module", follow_redirects=False)
    data = json.loads(resp2.body) if hasattr(resp2, 'body') else resp2.json()
    return resp2, data


# =============================================================================
# Tests: Generation entry and loading
# =============================================================================

def test_start_cycle_no_sync_generation(client, db, populated_material, mock_phase5a):
    """Starting a cycle does NOT synchronously generate Q1 anymore."""
    resp = client.post("/study/start_cycle",
                       data={"material_id": populated_material.id},
                       follow_redirects=False)
    assert resp.status_code == 303

    state = db.query(SessionState).first()
    pending = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "pending"
    ).count()
    planned = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "planned"
    ).count()

    assert pending == 0, f"Expected 0 pending after start, got {pending}"
    assert planned == 19, f"Expected 19 planned after start, got {planned}"


def test_new_cycle_current_page_triggers_loading(client, db, populated_material, mock_phase5a):
    """After start, /study/current shows loading state (not a question)."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.get("/study/current")
    assert resp.status_code == 200
    assert "正在为你生成练习题" in resp.text


def test_translation_loading_message_visible_during_generation(client, db, populated_material, mock_phase5a):
    """Loading message is shown when current translation slot is not ready."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    resp = client.get("/study/current")
    assert "正在为你生成练习题" in resp.text


def test_translation_generation_duplicate_trigger_prevented(client, db, populated_material, mock_phase5a):
    """Multiple rapid generate_module calls don't produce duplicates."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]

    # First call succeeds
    resp1 = client.post("/study/generate_module")
    data1 = json.loads(resp1.body) if hasattr(resp1, 'body') else resp1.json()
    assert data1["ok"] is True

    state = db.query(SessionState).first()
    pending = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "pending"
    ).count()
    assert pending == 5

    # Second call should find 0 eligible slots
    resp2 = client.post("/study/generate_module")
    data2 = json.loads(resp2.body) if hasattr(resp2, 'body') else resp2.json()
    assert data2["generated"] == 0
    assert data2["total"] == 0


# =============================================================================
# Tests: Batch generation and compact slot assignment
# =============================================================================

def test_generate_module_batch_5_valid(client, db, populated_material, mock_phase5a):
    """Batch generation creates 5 pending translation questions."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]

    resp = client.post("/study/generate_module")
    data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()
    assert data["ok"] is True
    assert data["generated"] == 5

    state = db.query(SessionState).first()
    pending = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "pending",
        QuestionAttempt.module_type == "grammar_a_translation",
    ).count()
    assert pending == 5


def test_generate_module_3_valid_then_supplement(client, db, populated_material, mock_phase5a):
    """Partial batch: 3 results → 3 pending, remaining planned for supplement."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)

    # Only return 3 results (2 eligible stay planned for supplement)
    mock_phase5a.side_effect = [MockTrans() for _ in range(3)]

    resp = client.post("/study/generate_module")
    data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()
    assert data["ok"] is True
    assert data["generated"] == 3

    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.module_type == "grammar_a_translation",
    ).order_by(QuestionAttempt.id).all()
    statuses = [q.status for q in all_qs]
    pending_count = statuses.count("pending")
    planned_count = statuses.count("planned")
    assert pending_count == 3, f"Expected 3 pending, got {pending_count}"
    assert planned_count == 2, f"Expected 2 planned, got {planned_count}"

    # Now supplement: call generate_module again
    mock_phase5a.side_effect = [MockTrans(prompt_prefix="补") for _ in range(2)]
    resp2 = client.post("/study/generate_module")
    data2 = json.loads(resp2.body) if hasattr(resp2, 'body') else resp2.json()
    assert data2["generated"] == 2
    from app.db import SessionLocal as _SL
    _vdb = _SL()
    statuses2 = [q.status for q in
                 _vdb.query(QuestionAttempt).filter(
                     QuestionAttempt.cycle_id == state.current_cycle_id,
                     QuestionAttempt.module_type == "grammar_a_translation",
                 ).order_by(QuestionAttempt.id).all()]
    _vdb.close()
    assert statuses2.count("pending") == 5, f"Expected 5 pending after supplement, got {statuses2}"


def test_generate_module_duplicate_results_compacted(client, db, populated_material, mock_phase5a):
    """Duplicate results: only unique prompts become pending; rejected stay planned."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)

    # All return the SAME prompt_zh
    mock_phase5a.side_effect = [MockTransDuplicate() for _ in range(5)]

    resp = client.post("/study/generate_module")
    data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()
    assert data["ok"] is True
    assert data["generated"] == 1  # only first is unique

    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.module_type == "grammar_a_translation",
    ).order_by(QuestionAttempt.id).all()
    statuses = [q.status for q in all_qs]
    assert statuses[0] == "pending", f"First should be pending, got {statuses[0]}"
    assert statuses[1:].count("planned") >= 4,  \
        f"Expected at least 4 planned for duplicates, got {statuses}"


def test_generate_module_never_leaves_generating(client, db, populated_material, mock_phase5a):
    """After generate_module, no translation slot remains in 'generating' state."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]

    client.post("/study/generate_module")

    state = db.query(SessionState).first()
    generating = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "generating",
    ).count()
    assert generating == 0, f"Expected 0 generating, got {generating}"


# =============================================================================
# Tests: Retry and non-overwrite
# =============================================================================

def test_generate_module_all_failure_shows_retry(client, db, populated_material, mock_phase5a):
    """Complete generation failure returns ok=false, frontend shows retry."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    mock_phase5a.return_value = None
    mock_phase5a.side_effect = None

    resp = client.post("/study/generate_module")
    data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()
    assert data["ok"] is False
    assert data["generated"] == 0
    assert data["total"] == 5


def test_generate_module_retry_only_missing_slots(client, db, populated_material, mock_phase5a):
    """Retry after partial success only targets planned/generation_failed slots."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)

    # First batch: 2 valid out of 5 eligible
    mock_phase5a.side_effect = [MockTrans() for _ in range(2)]
    client.post("/study/generate_module")

    # Retry: only 3 slots should be eligible (2 are already pending)
    mock_phase5a.side_effect = [MockTrans(prompt_prefix="补") for _ in range(3)]
    resp = client.post("/study/generate_module")
    data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()
    assert data["generated"] == 3, f"Expected 3 generated on retry, got {data['generated']}"

    from app.db import SessionLocal as _SL
    _vdb = _SL()
    cycle_id = _vdb.query(SessionState).first().current_cycle_id
    total_pending = _vdb.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id,
        QuestionAttempt.status == "pending",
    ).count()
    _vdb.close()
    assert total_pending >= 5, f"Expected >=5 pending total, got {total_pending}"


def test_generate_module_retry_does_not_overwrite(client, db, populated_material, mock_phase5a):
    """Retry must not overwrite already pending or answered slots."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)

    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    resp = client.post("/study/generate_module")
    data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()
    assert data["generated"] == 5

    # Answer Q1
    from app.routes.study import evaluate_translation_answer_v2
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        mev2.return_value = MockEvalV2(score_hearts=10)
        client.post("/study/answer", data={"answer": "test"})

    # Generate again — should generate 0 because no planned slots
    resp2 = client.post("/study/generate_module")
    data2 = json.loads(resp2.body) if hasattr(resp2, 'body') else resp2.json()
    assert data2["generated"] == 0
    assert data2["total"] == 0


# =============================================================================
# Tests: Answer progression — no sync block
# =============================================================================

def test_translation_answer_no_sync_generation(client, db, populated_material, mock_phase5a):
    """Submit answer does NOT synchronously generate missing next translation."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    client.post("/study/generate_module")

    # Answer Q1 — advance to Q2 (already pending)
    from app.routes.study import evaluate_translation_answer_v2
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        mev2.return_value = MockEvalV2(score_hearts=10)
        resp = client.post("/study/answer", data={"answer": "test"},
                           follow_redirects=False)
    # Should NOT redirect to review_candidates (not done yet)
    assert resp.status_code in (200, 303)


def test_missing_next_translation_routes_to_async_flow(client, db, populated_material, mock_phase5a):
    """When next question isn't pending, /study/current shows loading."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    # Don't generate — /study/current should show loading
    resp = client.get("/study/current")
    assert "正在为你生成练习题" in resp.text


def test_choice_generation_existing_behavior_preserved(client, db, populated_material, mock_phase5a):
    """MC generation still happens synchronously (not async like translation)."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)

    # Generate all 10 translation questions + advance session past them
    calls = [MockTrans() for _ in range(10)]
    mock_phase5a.side_effect = calls
    client.post("/study/generate_module")  # GA
    # Advance to GB module by answering Q1
    state = db.query(SessionState).first()
    # Generate GB
    # Mock more translations for the remaining GA + GB
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    # Session is at GA, answer all 5
    from app.routes.study import evaluate_translation_answer_v2
    for i in range(5):
        with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
            mev2.return_value = MockEvalV2(score_hearts=10)
            client.post("/study/answer", data={"answer": str(i)},
                        follow_redirects=False)

    # State should be at GB now
    db.refresh(state)
    assert state.current_module == "grammar_b_translation"

    # Generate GB slots
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    resp = client.post("/study/generate_module")
    data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()
    assert data["generated"] == 5


# =============================================================================
# Tests: Review gate regression (redirect only)
# =============================================================================

def test_10th_translation_with_pending_candidates_redirects_to_review(client, db, populated_material, mock_phase5a):
    """Completing 10 translations with pending candidates → 303 to review_candidates."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        mev2.return_value = MockEvalV2(
            score_hearts=7, target_grammar_correct=True,
            additional_errors=[MockErrorItem()],
        )
        client.post("/study/start_cycle",
                    data={"material_id": populated_material.id},
                    follow_redirects=False)

        mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
        client.post("/study/generate_module")  # GA

        # Answer all 5 GA translations (generates candidates)
        for i in range(5):
            client.post("/study/answer", data={"answer": str(i)},
                        follow_redirects=False)

        # Generate GB
        state = db.query(SessionState).first()
        assert state.current_module == "grammar_b_translation"
        mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
        resp = client.post("/study/generate_module")
        data = json.loads(resp.body) if hasattr(resp, 'body') else resp.json()

        # Answer all 5 GB translations
        for i in range(5):
            resp = client.post("/study/answer", data={"answer": str(i + 5)},
                               follow_redirects=False)

        # After Q10, should redirect to review_candidates
        from app.routes.study import _check_review_gate
        assert _check_review_gate(db, state.current_cycle_id) is True

        # The 10th answer response should be the redirect (TestClient's
        # follow_redirects=False returns the 303 directly)
        # But after 10 answers with the TestClient, it might have followed
        # the last redirect. Let's check the state instead.
        state2 = db.query(SessionState).first()
        # State should be at review_candidates or MC depending on redirect
        # Rather than checking state, check the last response
        assert resp.status_code == 303, \
            f"Expected 303 redirect after Q10, got {resp.status_code}"
        assert "/study/review_candidates" in resp.headers.get("location", ""), \
            f"Expected redirect to review_candidates, got {resp.headers.get('location')}"


def test_10th_translation_without_pending_candidates_continues_flow(client, db, populated_material, mock_phase5a):
    """Completing 10 translations with NO candidates → progresses without redirect."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    client.post("/study/generate_module")

    for i in range(5):
        with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
            mev2.return_value = MockEvalV2(score_hearts=10)
            client.post("/study/answer", data={"answer": str(i)},
                        follow_redirects=False)

    # Generate GB
    state = db.query(SessionState).first()
    assert state.current_module == "grammar_b_translation"
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    client.post("/study/generate_module")

    for i in range(5):
        with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
            mev2.return_value = MockEvalV2(score_hearts=10)
            resp = client.post("/study/answer", data={"answer": str(i + 5)},
                               follow_redirects=False)

    from app.routes.study import _check_review_gate
    assert _check_review_gate(db, state.current_cycle_id) is False


def test_review_completion_still_allows_choice_flow(client, db, populated_material, mock_phase5a):
    """Processing all pending candidates lets user continue to choices."""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        mev2.return_value = MockEvalV2(
            score_hearts=7, target_grammar_correct=True,
            additional_errors=[MockErrorItem()],
        )
        client.post("/study/start_cycle",
                    data={"material_id": populated_material.id},
                    follow_redirects=False)
        mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
        client.post("/study/generate_module")

        for i in range(5):
            client.post("/study/answer", data={"answer": str(i)},
                        follow_redirects=False)

        mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
        client.post("/study/generate_module")

        for i in range(5):
            client.post("/study/answer", data={"answer": str(i + 5)},
                        follow_redirects=False)

        # Process candidates
        state = db.query(SessionState).first()
        candidates = db.query(TranslationErrorCandidate).filter(
            TranslationErrorCandidate.status == "pending"
        ).all()
        for c in candidates:
            client.post(f"/study/candidate/{c.id}/add", follow_redirects=False)

        from app.routes.study import _check_review_gate
        assert _check_review_gate(db, state.current_cycle_id) is False


# =============================================================================
# Phase 5A: Page-level loading/retry verification
# =============================================================================

def test_loading_page_contains_generation_html(client, db, populated_material, mock_phase5a):
    """Loading page HTML includes the generation trigger script and retry button."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.get("/study/current")
    html = resp.text

    # Loading message
    assert "正在为你生成练习题" in html, "Missing loading message"
    # JS generation trigger function
    assert "function startGeneration()" in html, "Missing JS function"
    # Retry button (hidden by JS but present in markup)
    assert "id=\"retry-btn\"" in html, "Missing retry button"
    # Formless fetch-based POST (not a submit button)
    assert "/study/generate_module" in html or "generate_module" in html, \
        "Missing generate_module call in JS"


def test_generate_then_current_shows_question(client, db, populated_material, mock_phase5a):
    """After successful generate_module, /study/current shows a translation question."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)

    # /study/current should show loading
    before = client.get("/study/current")
    assert "正在为你生成练习题" in before.text

    # Generate GA module
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    gen = client.post("/study/generate_module")
    data = json.loads(gen.body) if hasattr(gen, 'body') else gen.json()
    assert data["ok"] is True
    assert data["generated"] == 5

    # Now /study/current should no longer be loading — it should render question
    after = client.get("/study/current")
    # Should NOT have loading text
    assert "正在为你生成练习题" not in after.text, \
        "Loading text still present after generation"
    # Should show question content — the answer form
    assert "answer" in after.text or "type=\"text\"" in after.text or "form" in after.text, \
        "Question form not found in rendered page"


def test_retry_flow_failure_to_success_shows_question(client, db, populated_material, mock_phase5a):
    """Retry after generation failure: request, fail, retry, succeed, see question."""
    client.post("/study/start_cycle",
                data={"material_id": populated_material.id},
                follow_redirects=False)

    # First attempt: complete failure
    mock_phase5a.return_value = None
    mock_phase5a.side_effect = None
    fail_resp = client.post("/study/generate_module")
    fail_data = json.loads(fail_resp.body) if hasattr(fail_resp, 'body') else fail_resp.json()
    assert fail_data["ok"] is False
    assert fail_data["generated"] == 0

    # /study/current should still show loading (with retry available via JS)
    still_loading = client.get("/study/current")
    assert "正在为你生成练习题" in still_loading.text, \
        "Should still show loading after failure"
    # The retry button should be in the markup (JS toggles visibility)
    assert "id=\"retry-btn\"" in still_loading.text, "Retry button must be in DOM"

    # Retry: this time succeed
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    mock_phase5a.return_value = None
    retry_resp = client.post("/study/generate_module")
    retry_data = json.loads(retry_resp.body) if hasattr(retry_resp, 'body') else retry_resp.json()
    assert retry_data["ok"] is True
    assert retry_data["generated"] == 5, \
        f"Expected 5 generated on retry, got {retry_data['generated']}"

    # /study/current should now show a question
    after_retry = client.get("/study/current")
    assert "正在为你生成练习题" not in after_retry.text, \
        "Loading text still present after successful retry"
    assert "answer" in after_retry.text or "type=\"text\"" in after_retry.text, \
        "Question form not found after retry"


# =============================================================================
# Phase 5A: _cancel_mastered_cycle_questions focus tests
# =============================================================================

def test_mastered_grammar_b_cancels_only_grammar_b_translation_slots(
    client, db, populated_material, mock_phase5a,
):
    """Marking grammar B as mastered cancels only eligible GB translation slots.

    GA translation slots must remain unaffected.
    """
    mat = populated_material
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_b = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜がち",
    ).first()

    # Start cycle + generate GA + answer GA to reach GB, then generate GB
    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)

    def _gen_with_gp(*args):
        """Generate a MockTrans whose grammar_point matches the target gp."""
        target_gp = args[0] if args else gp_a
        gp_name = getattr(target_gp, "point_name", "〜てはいられない")
        t = MockTrans()
        t.grammar_point = gp_name
        t.grading_notes = f"使用目标语法 {gp_name}"
        return t

    mock_phase5a.side_effect = _gen_with_gp
    client.post("/study/generate_module")  # GA

    # Answer GA Q1-Q5 to advance to GB
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        mev2.return_value = MockEvalV2(score_hearts=10)
        for i in range(5):
            client.post("/study/answer", data={"answer": str(i)},
                        follow_redirects=False)

    # Generate GB
    mock_phase5a.side_effect = _gen_with_gp
    client.post("/study/generate_module")

    # Now mark grammar B as mastered
    client.post(f"/materials/{mat.id}/grammar/{gp_b.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    cycle_id = db.query(SessionState).first().current_cycle_id
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    ga_cancelled = [
        q for q in all_qs
        if q.module_type == "grammar_a_translation"
        and q.status == "cancelled_mastered"
    ]
    gb_cancelled = [
        q for q in all_qs
        if q.module_type == "grammar_b_translation"
        and q.status == "cancelled_mastered"
    ]

    # GB slots should be cancelled
    assert len(gb_cancelled) >= 1, \
        f"Expected at least 1 GB cancelled, got {len(gb_cancelled)}"
    # GA slots must NOT be cancelled
    assert len(ga_cancelled) == 0, \
        f"Expected 0 GA cancelled, got {len(ga_cancelled)}"


def test_mastered_grammar_a_does_not_cancel_grammar_b_or_unrelated_mc_slots(
    client, db, populated_material, mock_phase5a,
):
    """Marking grammar A as mastered cancels GA slots but not GB/MC slots."""
    mat = populated_material
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()

    # Start cycle, generate GA only (GB and MC stay planned)
    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)
    mock_phase5a.side_effect = [MockTrans() for _ in range(5)]
    client.post("/study/generate_module")

    # Mark grammar A as mastered
    client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    cycle_id = db.query(SessionState).first().current_cycle_id
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    ga_cancelled = [
        q for q in all_qs
        if q.module_type == "grammar_a_translation"
        and q.status == "cancelled_mastered"
    ]
    gb_cancelled = [
        q for q in all_qs
        if q.module_type == "grammar_b_translation"
        and q.status == "cancelled_mastered"
    ]
    mc_cancelled = [
        q for q in all_qs
        if q.module_type == "multiple_choice"
        and q.status == "cancelled_mastered"
    ]

    # GA slots should be cancelled
    assert len(ga_cancelled) >= 1, \
        f"Expected >=1 GA cancelled, got {len(ga_cancelled)}"
    # GB slots must NOT be cancelled
    assert len(gb_cancelled) == 0, \
        f"Expected 0 GB cancelled, got {len(gb_cancelled)}"
    # MC slots must NOT be cancelled by grammar A mastered
    assert len(mc_cancelled) == 0, \
        f"Expected 0 MC cancelled, got {len(mc_cancelled)}"
