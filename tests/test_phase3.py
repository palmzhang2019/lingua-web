"""Phase 3 tests: lazy question generation with prefetch.

All tests use isolated temp DB and mocked DeepSeek.
Run with: uv run pytest tests/test_phase3.py -v
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
    QuestionAttempt, SessionState,
)
from app.main import app

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

# ===========================================================================
# Fixtures
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
def mock_deepseek():
    with patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

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


# ===========================================================================
# Phase 3 tests
# ===========================================================================

def test_new_cycle_creates_19_planned_slots(client, db, populated_material, mock_deepseek):
    """New cycle creates 19 slots; only question 1 is pending."""
    mat = populated_material
    resp = client.post("/study/start_cycle",
                       data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()

    assert len(all_qs) == 19, f"Expected 19 slots, got {len(all_qs)}"

    # Q1 should be pending (generated)
    assert all_qs[0].status == "pending", f"Q1 should be pending, got {all_qs[0].status}"

    # Q2-Q19 should be planned
    planned = [q for q in all_qs[1:] if q.status == "planned"]
    assert len(planned) == 18, f"Expected 18 planned, got {len(planned)}"


def test_prefetch_next_generates_one_question(client, db, populated_material, mock_deepseek):
    """Prefetch generates only the next planned question."""
    mat = populated_material
    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)

    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()

    # Q2 should be planned
    assert all_qs[1].status == "planned", f"Q2 should be planned before prefetch"

    # Prefetch
    resp = client.post("/study/prefetch_next")
    assert resp.json().get("ok") is True, f"Prefetch failed: {resp.json()}"

    # Q2 should now be pending
    db.refresh(all_qs[1])
    assert all_qs[1].status == "pending", \
        f"Q2 should be pending after prefetch, got {all_qs[1].status}"
    # Q3 should still be planned
    db.refresh(all_qs[2])
    assert all_qs[2].status == "planned", \
        f"Q3 should remain planned, got {all_qs[2].status}"


def test_repeated_prefetch_is_idempotent(client, db, populated_material, mock_deepseek):
    """Repeated prefetch requests do not duplicate generation."""
    mat = populated_material
    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)

    # Prefetch twice consecutively
    r1 = client.post("/study/prefetch_next").json()
    # Second prefetch should either be no-op (no more planned) or generate Q3
    r2 = client.post("/study/prefetch_next").json()

    # First should succeed
    assert r1.get("ok") is True, f"First prefetch failed: {r1}"

    state = db.query(SessionState).first()
    pending = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "pending"
    ).count()
    # After 2 prefetches: Q1 (from start) + Q2 + maybe Q3 = 2 or 3 pending
    assert pending >= 2, f"Expected at least 2 pending, got {pending}"
    assert pending <= 3, f"Expected at most 3 pending, got {pending}"


def test_start_cycle_generates_only_first_question(client, db, populated_material, mock_deepseek):
    """Starting a cycle only generates the first question, not all 19."""
    mat = populated_material
    resp = client.post("/study/start_cycle",
                       data={"material_id": mat.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    pending = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "pending"
    ).count()
    planned = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "planned"
    ).count()

    assert pending == 1, f"Expected 1 pending after start, got {pending}"
    assert planned == 18, f"Expected 18 planned after start, got {planned}"


def test_skip_module_handles_planned_slots(client, db, populated_material, mock_deepseek):
    """Skip module terminalizes planned slots without generation."""
    mat = populated_material
    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)

    # Skip grammar A module (questions 1-5)
    resp = client.post("/study/skip_module", follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()

    skipped = sum(1 for q in all_qs if q.status == "skipped")
    assert skipped == 5, f"Expected 5 skipped, got {skipped}"


def test_mastered_cancels_planned_future_slots(client, db, populated_material, mock_deepseek):
    """Toggling mastered cancels planned slots for that grammar."""
    mat = populated_material
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id
    ).first()

    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)

    # Toggle gp_a to mastered
    client.post(f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
                headers={"X-Requested-With": "XMLHttpRequest"})

    state = db.query(SessionState).first()
    cancelled = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id,
        QuestionAttempt.status == "cancelled_mastered"
    ).all()

    assert len(cancelled) >= 1, "Expected at least 1 cancelled question"


def test_historical_pre_phase3_cycle_renders(client, db):
    """Pre-Phase-3 cycle with status='pending' rows is still renderable."""
    mat = Material(filename="old.txt", content_text="Old.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    gp = GrammarPoint(material_id=mat.id, point_name="〜てはいられない",
                      explanation_jp="X", example_from_material="x",
                      difficulty_level="N2")
    db.add(gp); db.commit()

    cycle = StudyCycle(started_at=datetime.datetime.utcnow(),
                       grammar_a_id=gp.id, grammar_b_id=gp.id,
                       is_valid_completion=False)
    db.add(cycle); db.commit(); db.refresh(cycle)

    q = QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
                        question_payload_json={"type": "translation", "prompt_zh": "Test"},
                        correct_answer="test", status="pending")
    db.add(q); db.commit()

    cm = CycleMaterial(cycle_id=cycle.id, material_id=mat.id)
    db.add(cm); db.commit()

    resp = client.get(f"/study?cycle_id={cycle.id}")
    # Should not crash
    assert resp.status_code in (200, 303, 302)


def test_all_planned_slots_have_correct_types(client, db, populated_material, mock_deepseek):
    """Verify all 19 slots have correct module types."""
    mat = populated_material
    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)

    state = db.query(SessionState).first()
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == state.current_cycle_id
    ).order_by(QuestionAttempt.id).all()

    types = [q.module_type for q in all_qs]
    expected = (["grammar_a_translation"] * 5 +
                ["grammar_b_translation"] * 5 +
                ["multiple_choice"] * 9)
    assert types == expected, f"Slot types mismatch"
