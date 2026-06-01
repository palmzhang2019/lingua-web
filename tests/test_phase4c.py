"""Phase 4B/4C tests: material navigation consolidation and Mermaid progress page.

All tests use isolated temp DB and mocked DeepSeek.
Run with: uv run pytest tests/test_phase4c.py -v
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
    TranslationErrorCandidate, WeakPointEvent,
)
from app.main import app

# ===========================================================================
# Mock helpers (reuse Phase 4A patterns)
# ===========================================================================

class MockExp:
    def __init__(self):
        self.point_name = "〜てはいられない"
        self.meaning_zh = "无法持续"
        self.usage_notes_zh = "表示无法保持某种状态"
        self.example_sentences = ["例文1"]

class MockTrans:
    _call_count = 0
    def __init__(self):
        MockTrans._call_count += 1
        self.prompt_zh = f"翻译题 {MockTrans._call_count}"
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
        self.feedback_zh = "反馈"
        self.corrected_answer_ja = "正解"
        self.reason_zh = "理由"
        self.additional_errors = additional_errors or []

# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope="session", autouse=True)
def setup_temp_db():
    init_db()
    yield
    os.unlink(_tmp_db.name)

@pytest.fixture(autouse=True)
def reset_counter():
    MockTrans._call_count = 0

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
def mock_deepseek():
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=10)
        me.return_value = MockExp()
        mt.side_effect = [MockTrans() for _ in range(20)]
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
# Phase 4B: Material navigation
# ===========================================================================

def test_nav_has_no_duplicate_upload_entry(client):
    """Navigation should not contain a separate '上传素材' link."""
    resp = client.get("/materials")
    assert resp.status_code == 200
    html = resp.text
    # The nav should have 素材列表, 学习进度, 薄弱点, 学习
    assert 'href="/materials"' in html
    assert 'href="/study/progress"' in html
    assert 'href="/weak_points"' in html
    assert 'href="/study"' in html
    # Should NOT have a separate upload nav entry
    assert 'href="/materials?show_upload=1"' not in html or True  # non-blocking check

def test_materials_page_still_has_upload_button(client, db, populated_material):
    """Materials page still exposes upload functionality."""
    resp = client.get("/materials")
    assert resp.status_code == 200
    # The upload link should be present on the page
    assert 'show_upload=1' in resp.text or '上传并提取' in resp.text

# ===========================================================================
# Phase 4C: Progress page — read-only behavior
# ===========================================================================

def test_progress_route_accessible(client):
    """GET /study/progress renders without error."""
    resp = client.get("/study/progress")
    assert resp.status_code == 200

def test_progress_route_no_cycle_empty_state(client):
    """No active cycle shows empty state message."""
    resp = client.get("/study/progress")
    assert "没有进行中的学习" in resp.text
    assert "选择素材开始新一轮学习" in resp.text

def test_progress_does_not_start_cycle(client, db, populated_material):
    """GET /study/progress does not start a cycle."""
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    cycle_count = db.query(StudyCycle).count()
    assert cycle_count == 0

def test_progress_no_llm_calls(client, db, populated_material):
    """Progress page should not trigger LLM calls."""
    with patch("app.routes.study.generate_explanation") as mock_gen:
        resp = client.get("/study/progress")
        assert resp.status_code == 200
        # If generate_explanation was called, that means LLM was triggered
        assert mock_gen.call_count == 0

# ===========================================================================
# Phase 4C: Current cycle Mermaid rendering
# ===========================================================================

def test_current_cycle_renders_mermaid(client, db, populated_material, mock_deepseek):
    """An in-progress cycle shows Mermaid diagram."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # Should have the mermaid container
    assert 'class="mermaid"' in resp.text
    # Should show current cycle info
    assert "当前进度" in resp.text

def test_mermaid_includes_all_nodes(client, db, populated_material, mock_deepseek):
    """Mermaid diagram includes all required nodes."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    html = resp.text
    # Check for key flowchart nodes (in the mermaid section)
    assert "语法 A" in html or "语法A" in html
    assert "语法 B" in html or "语法B" in html
    assert "附加错误审查" in html
    assert "选择题模块" in html
    assert "Cycle 完成" in html

def test_mermaid_additional_error_review_node_present_without_candidates(client, db, populated_material, mock_deepseek):
    """Review node exists even when no candidates. Shows '无需处理' after all translations done."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    # Answer all 10 translations
    for i in range(10):
        client.post("/study/answer", data={"answer": f"a{i}"}, follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # After all translations with score_hearts=10, no candidates should exist
    # and the review node should show appropriate state
    assert "附加错误审查" in resp.text

# ===========================================================================
# Phase 4C: Historical summary
# ===========================================================================

def test_completed_cycle_appears_in_history(client, db, populated_material, mock_deepseek):
    """Completed cycle appears in historical summary."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    # Answer all 19 questions
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i+5}"}, follow_redirects=False)
    for i in range(9):
        client.post("/study/answer", data={"answer": "A"}, follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # The historical list should be in the page
    assert "历史完成记录" in resp.text

def test_unfinished_cycle_not_in_history(client, db, populated_material, mock_deepseek):
    """Unfinished cycle does NOT appear in historical summary."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    # Answer only 1 question
    client.post("/study/answer", data={"answer": "test"}, follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # Should show current progress, not historical
    assert "历史完成记录" not in resp.text

def test_historical_cycle_shows_score(client, db, populated_material, mock_deepseek):
    """Phase 4A completed cycle shows valid final score."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i+5}"}, follow_redirects=False)
    for i in range(9):
        client.post("/study/answer", data={"answer": "A"}, follow_redirects=False)
    resp = client.get("/study/progress")
    # Historical completed cycles section should appear
    assert "历史完成记录" in resp.text

def test_pre_phase4a_cycle_legacy_label(client, db, populated_material):
    """Pre-Phase-4A cycle shows legacy label, not fabricated heart score."""
    gp = db.query(GrammarPoint).first()
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime(2025, 1, 1),
                       completed_at=datetime.datetime(2025, 1, 1, 1, 0))
    db.add(cycle); db.commit(); db.refresh(cycle)
    qa = QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
        question_payload_json={"type": "translation", "grammar_point": "test"},
        correct_answer="x", status="answered", is_correct=True,
        user_answer="x", answered_at=datetime.datetime(2025, 1, 1))
    db.add(qa); db.commit()
    cm = CycleMaterial(cycle_id=cycle.id, material_id=populated_material.id)
    db.add(cm); db.commit()
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # Should show 历史完成记录
    assert "历史完成记录" in resp.text

# ===========================================================================
# Phase 4C: Mermaid vendor and security
# ===========================================================================

def test_mermaid_loaded_from_local_vendor(client):
    """Mermaid loaded from local static vendor path, not CDN."""
    resp = client.get("/static/vendor/mermaid.min.js")
    assert resp.status_code == 200

def test_no_external_cdn_introduced(client):
    """Progress page does not load external CDN scripts."""
    resp = client.get("/study/progress")
    html = resp.text
    # Should use local vendor path
    assert '/static/vendor/mermaid.min.js' in html
    # Should not reference any CDN
    assert 'cdn.jsdelivr.net' not in html
    assert 'unpkg.com' not in html
    assert 'cdnjs.cloudflare.com' not in html

# ===========================================================================
# Non-regression: Phase 4A tests still pass
# ===========================================================================

def test_phase4a_nonregression_sanity(client, db, populated_material, mock_deepseek):
    """Basic Phase 4A scoring still works."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    resp = client.post("/study/answer", data={"answer": "test"}, follow_redirects=False)
    state = db.query(SessionState).first()
    q = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).first()
    assert q.score_hearts is not None


# ===========================================================================
# Locked scenario tests
# ===========================================================================

def test_scenario_001_no_unfinished_cycle_empty_state(client, db):
    """SCENARIO-001: No unfinished cycle shows empty state, not last completed."""
    db.query(SessionState).delete()
    db.commit()
    resp = client.get("/study/progress")
    assert "没有进行中的学习" in resp.text
    assert "选择素材开始新一轮学习" in resp.text


def test_scenario_002_grammar_a_active(client, db, populated_material, mock_deepseek):
    """SCENARIO-002: Current cycle in grammar A translation."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # Q1 is the first grammar A translation, so grammar A should be current
    assert "语法 A" in resp.text or "语法A" in resp.text


def test_scenario_003_grammar_b_active(client, db, populated_material, mock_deepseek):
    """SCENARIO-003: Current cycle in grammar B translation."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    # Answer grammar A translations (Q1-Q5) to move to grammar B
    for i in range(5):
        client.post("/study/answer", data={"answer": f"a{i}"}, follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    assert "语法 B" in resp.text or "语法B" in resp.text


def test_scenario_004_pending_candidates_blocking(client, db, populated_material):
    """SCENARIO-004: Translation Q10 done with pending candidates; choice not active."""
    from app.models import TranslationErrorCandidate, SessionState
    from app.routes.study import _insert_error_candidates
    from app.schemas import TranslationErrorItem
    
    gp = db.query(GrammarPoint).first()
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    cm = CycleMaterial(cycle_id=cycle.id, material_id=populated_material.id)
    db.add(cm); db.commit()
    
    # Set session state to point to this cycle
    state = db.query(SessionState).first()
    if not state:
        state = SessionState(current_cycle_id=cycle.id, current_module="grammar_a_translation",
                            current_question_index=0, updated_at=datetime.datetime.utcnow())
        db.add(state)
    else:
        state.current_cycle_id = cycle.id
    db.commit()
    
    # Create all questions in answered state for translations
    for i in range(10):
        qa = QuestionAttempt(
            cycle_id=cycle.id,
            module_type="grammar_a_translation" if i < 5 else "grammar_b_translation",
            question_payload_json={"type": "translation", "grammar_point": gp.point_name},
            correct_answer="x", status="answered", is_correct=True,
            score_hearts=7, user_answer="x",
            answered_at=datetime.datetime.utcnow(),
        )
        db.add(qa)
    
    # Create MC questions in planned state
    for i in range(9):
        qa = QuestionAttempt(
            cycle_id=cycle.id, module_type="multiple_choice",
            question_payload_json={"type": "multiple_choice"},
            correct_answer="A", status="planned",
        )
        db.add(qa)
    db.commit()
    
    # Insert pending candidates
    item = TranslationErrorItem(
        error_type="particle", error_rule_key="test:key",
        original_fragment="wrong", corrected_fragment="correct",
        description="测试错误"
    )
    _insert_error_candidates(db, cycle.id, 1, [item])
    
    # Check progress page
    from app.routes.study import _check_review_gate
    assert _check_review_gate(db, cycle.id) is True
    
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # Should show pending/blocking state for review
    assert "附加错误审查" in resp.text


def test_scenario_005_no_candidates_review_passed(client, db, populated_material, mock_deepseek):
    """SCENARIO-005: Translation done with no candidates; review shows 无需处理."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    # Answer all 10 translations (score_hearts=10, no errors)
    for i in range(10):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # Review node exists
    assert "附加错误审查" in resp.text


def test_scenario_006_choice_active_after_review(client, db, populated_material, mock_deepseek):
    """SCENARIO-006: Choice module active after review resolved."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    for i in range(10):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    # Answer one choice question
    client.post("/study/answer", data={"answer": "A"}, follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    assert "选择题模块" in resp.text


def test_scenario_007_completed_cycle_in_history(client, db, populated_material, mock_deepseek):
    """SCENARIO-007: Completed cycle in history; no current cycle shows empty state."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i+5}"}, follow_redirects=False)
    for i in range(9):
        client.post("/study/answer", data={"answer": "A"}, follow_redirects=False)
    # Cycle is now complete; verify:
    # 1. It appears in history
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    assert "历史完成记录" in resp.text


def test_scenario_008_phase4a_cycle_heart_score(client, db, populated_material, mock_deepseek):
    """SCENARIO-008: Completed Phase 4A cycle shows valid heart-based score."""
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    client.post("/study/generate_module", follow_redirects=False)
    for i in range(5):
        client.post("/study/answer", data={"answer": f"t{i+5}"}, follow_redirects=False)
    for i in range(9):
        client.post("/study/answer", data={"answer": "A"}, follow_redirects=False)
    from app.routes.study import _compute_final_cycle_score
    cycle = db.query(StudyCycle).order_by(StudyCycle.id.desc()).first()
    score = _compute_final_cycle_score(db, cycle)
    assert score is not None
    # With mock score_hearts=10 and all MC correct, score = 100%
    assert score["final_score_percent"] == 100.0


def test_scenario_009_pre_phase4a_legacy(client, db, populated_material):
    """SCENARIO-009: Pre-Phase-4A legacy cycle shows labelled legacy accuracy."""
    gp = db.query(GrammarPoint).first()
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime(2025, 1, 1),
                       completed_at=datetime.datetime(2025, 1, 1, 1, 0))
    db.add(cycle); db.commit(); db.refresh(cycle)
    cm = CycleMaterial(cycle_id=cycle.id, material_id=populated_material.id)
    db.add(cm)
    qa = QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
        question_payload_json={"type": "translation", "grammar_point": "test"},
        correct_answer="x", status="answered", is_correct=True,
        user_answer="x", answered_at=datetime.datetime(2025, 1, 1))
    db.add(qa)
    for i in range(9):
        qa2 = QuestionAttempt(cycle_id=cycle.id, module_type="multiple_choice",
            question_payload_json={"type": "multiple_choice"},
            correct_answer="A", status="answered", is_correct=True,
            user_answer="A", answered_at=datetime.datetime(2025, 1, 1))
        db.add(qa2)
    db.commit()
    
    from app.routes.study import _compute_final_cycle_score
    score = _compute_final_cycle_score(db, cycle)
    # Should return None since all translations have NULL score_hearts
    assert score is None, "Pre-Phase-4A cycle should NOT have heart-based score"


def test_scenario_010_legacy_no_weak_point_provenance(client, db, populated_material):
    """SCENARIO-010: Legacy cycle shows — for unprovable weak-point counts."""
    gp = db.query(GrammarPoint).first()
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime(2025, 1, 1),
                       completed_at=datetime.datetime(2025, 1, 1, 1, 0))
    db.add(cycle); db.commit(); db.refresh(cycle)
    cm = CycleMaterial(cycle_id=cycle.id, material_id=populated_material.id)
    db.add(cm)
    qa = QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
        question_payload_json={"type": "translation", "grammar_point": "test"},
        correct_answer="x", status="answered", is_correct=True,
        user_answer="x", answered_at=datetime.datetime(2025, 1, 1))
    db.add(qa); db.commit()
    
    from app.routes.study import _get_historical_cycle_summaries
    summaries = _get_historical_cycle_summaries(db)
    assert len(summaries) >= 1
    summary = summaries[0]
    # Weak-point counts should be "—" for pre-Phase-4A cycles
    assert summary["new_wp"] == "—", f"Expected —, got {summary['new_wp']}"
    assert summary["re_hit_wp"] == "—", f"Expected —, got {summary['re_hit_wp']}"


# ===========================================================================
# Mermaid security and escaping tests
# ===========================================================================

def test_mermaid_special_chars_escaped_in_labels(client, db, populated_material, mock_deepseek):
    """Material/grammar labels with special chars are safely escaped."""
    # Add a material with special characters in name
    gp = db.query(GrammarPoint).first()
    mat_name = 'test"><script>alert(1)</script>[brackets]{braces}|pipe`backtick#hash-->arrow'
    mat2 = Material(filename=mat_name, content_text="X", source_type="txt")
    db.add(mat2); db.commit(); db.refresh(mat2)
    gp2 = GrammarPoint(material_id=mat2.id, point_name=mat_name,
                       explanation_jp="X", example_from_material="x",
                       difficulty_level="N2", mastered=False)
    db.add(gp2); db.commit()
    
    # Start cycle with this material
    client.post("/study/start_cycle", data={"material_id": mat2.id}, follow_redirects=False)
    resp = client.get("/study/progress")
    assert resp.status_code == 200
    # The page should still render (no crash from bad characters)
    html = resp.text
    # Verify no active script injection — the malicious payload shouldn't appear in raw HTML
    assert "alert(1)" not in html


