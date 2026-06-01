"""Phase 2 tests: multi-material study cycles with provenance.

All tests use a temporary SQLite database — never data/lingua.db.
Run with: uv run pytest tests/test_phase2.py -v
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
    CycleMaterial, QuestionAttempt, SessionState, WeakPoint,
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
    def __init__(self, prompt="Choose", A="A", B="B", C="C", D="D",
                 expected="A", grammar_point="正常", question_role="review"):
        self.prompt = prompt; self.A = A; self.B = B; self.C = C; self.D = D
        self.expected = expected; self.grammar_point = grammar_point
        self.question_role = question_role


def make_mock_translations(n=5, gp_name="〜てはいられない"):
    return [MockTranslation(prompt_zh=f"题{j+1}", reference_answer_ja=f"答{j+1}",
                            grammar_point=gp_name) for j in range(n)]


def make_mock_mc(n=9):
    return [MockMC(prompt=f"MC{j+1}", A=f"A{j+1}", B=f"B{j+1}",
                   C=f"C{j+1}", D=f"D{j+1}") for j in range(n)]


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
         patch("app.routes.study.generate_translation_exercises") as mt, \
         patch("app.routes.study.generate_multiple_choice") as mmc:
        me.return_value = MockExplanation()
        mt.return_value = make_mock_translations(5, "〜てはいられない")
        mmc.return_value = make_mock_mc(9)
        yield


@pytest.fixture
def two_materials(db):
    """Return two materials each with 2 unmastered grammar points."""
    m1 = Material(filename="mat1.txt", content_text="Mat1 content.",
                  source_type="txt")
    m2 = Material(filename="mat2.txt", content_text="Mat2 content.",
                  source_type="txt")
    db.add_all([m1, m2]); db.commit(); db.refresh(m1); db.refresh(m2)

    gp1 = GrammarPoint(material_id=m1.id, point_name="〜てはいられない",
                       explanation_jp="A", example_from_material="a1",
                       difficulty_level="N2", mastered=False)
    gp2 = GrammarPoint(material_id=m1.id, point_name="〜がち",
                       explanation_jp="B", example_from_material="b1",
                       difficulty_level="N2", mastered=False)
    gp3 = GrammarPoint(material_id=m2.id, point_name="〜たきり",
                       explanation_jp="C", example_from_material="c2",
                       difficulty_level="N2", mastered=False)
    gp4 = GrammarPoint(material_id=m2.id, point_name="〜ずに",
                       explanation_jp="D", example_from_material="d2",
                       difficulty_level="N2", mastered=False)
    db.add_all([gp1, gp2, gp3, gp4]); db.commit()
    return m1, m2, gp1, gp2, gp3, gp4


# ===========================================================================
# Schema tests
# ===========================================================================

def test_cycle_materials_table_created(db, two_materials):
    """cycle_materials table exists and is functional."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials
    c = StudyCycle(started_at=datetime.datetime.utcnow(), grammar_a_id=gp1.id,
                   grammar_b_id=gp2.id, is_valid_completion=False)
    db.add(c); db.commit(); db.refresh(c)

    cm1 = CycleMaterial(cycle_id=c.id, material_id=m1.id)
    cm2 = CycleMaterial(cycle_id=c.id, material_id=m2.id)
    db.add_all([cm1, cm2]); db.commit()

    rows = db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == c.id).all()
    assert len(rows) == 2


def test_cycle_materials_unique_constraint(db, two_materials):
    """Duplicate (cycle_id, material_id) is rejected."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials
    c = StudyCycle(started_at=datetime.datetime.utcnow(), grammar_a_id=gp1.id,
                   grammar_b_id=gp2.id, is_valid_completion=False)
    db.add(c); db.commit(); db.refresh(c)

    db.add(CycleMaterial(cycle_id=c.id, material_id=m1.id))
    db.commit()
    with pytest.raises(Exception):
        db.add(CycleMaterial(cycle_id=c.id, material_id=m1.id))
        db.commit()


# ===========================================================================
# Multi-material function tests
# ===========================================================================

def test_start_cycle_from_two_materials(client, db, two_materials, mock_deepseek):
    """Two selected materials create one cycle with two cycle_materials rows."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Failed: {resp.status_code}"

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    assert cycle is not None

    cm = db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == cycle.id).all()
    assert len(cm) == 2, f"Expected 2 cycle_materials, got {len(cm)}"
    mids = sorted([r.material_id for r in cm])
    assert mids == sorted([m1.id, m2.id])


def test_duplicate_material_ids_deduplicated(client, db, two_materials, mock_deepseek):
    """Repeated selected IDs do not create duplicate association rows."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id, m1.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    cm = db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == cycle.id).all()
    assert len(cm) == 2


def test_no_material_selected_blocked(client, db, mock_deepseek):
    """No cycle created without selected materials."""
    resp = client.post("/study/start_cycle", data={}, follow_redirects=False)
    assert resp.status_code == 400
    assert "至少选择一份素材" in resp.text


def test_combined_pool_zero_eligible_blocked(client, db, mock_deepseek):
    """No cycle when all grammar is mastered."""
    mat = Material(filename="zero.txt", content_text="Z", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    gp = GrammarPoint(material_id=mat.id, point_name="〜てはいられない",
                      explanation_jp="X", example_from_material="x",
                      difficulty_level="N2", mastered=True)
    db.add(gp); db.commit()

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [mat.id]})
    assert resp.status_code == 400
    assert "没有未掌握" in resp.text


def test_combined_pool_one_eligible_blocked(client, db, mock_deepseek):
    """Only one eligible grammar produces distinct message."""
    mat = Material(filename="one.txt", content_text="O", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    gp1 = GrammarPoint(material_id=mat.id, point_name="〜てはいられない",
                       explanation_jp="A", example_from_material="a",
                       difficulty_level="N2", mastered=False)
    gp2 = GrammarPoint(material_id=mat.id, point_name="〜がち",
                       explanation_jp="B", example_from_material="b",
                       difficulty_level="N2", mastered=True)
    db.add_all([gp1, gp2]); db.commit()

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [mat.id]})
    assert resp.status_code == 400
    assert "仅剩 1 个" in resp.text


def test_two_materials_each_one_unique_can_start(client, db, mock_deepseek):
    """Two materials each with 1 unique GP can combine for A/B."""
    m1 = Material(filename="wa.txt", content_text="W", source_type="txt")
    m2 = Material(filename="wb.txt", content_text="X", source_type="txt")
    db.add_all([m1, m2]); db.commit(); db.refresh(m1); db.refresh(m2)

    gp1 = GrammarPoint(material_id=m1.id, point_name="〜てはいられない",
                       explanation_jp="A", example_from_material="a",
                       difficulty_level="N2", mastered=False)
    gp2 = GrammarPoint(material_id=m2.id, point_name="〜がち",
                       explanation_jp="B", example_from_material="b",
                       difficulty_level="N2", mastered=False)
    db.add_all([gp1, gp2]); db.commit()

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Failed: {resp.status_code}"
    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    assert cycle is not None
    cm = db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == cycle.id).all()
    assert len(cm) == 2


def test_legacy_single_material_start_still_works(client, db, two_materials, mock_deepseek):
    """Legacy single-material start still works and creates 1 cycle_material."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials

    resp = client.post("/study/start_cycle",
                       data={"material_id": m1.id},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    assert cycle is not None

    cm = db.query(CycleMaterial).filter(
        CycleMaterial.cycle_id == cycle.id).all()
    assert len(cm) == 1
    assert cm[0].material_id == m1.id


# ===========================================================================
# Duplicate grammar and cross-material mastered tests
# ===========================================================================

def test_duplicate_grammar_across_materials_deduped(client, db, mock_deepseek):
    """Same grammar in 2 materials counts as 1 candidate for A/B."""
    m1 = Material(filename="da.txt", content_text="D", source_type="txt")
    m2 = Material(filename="db.txt", content_text="E", source_type="txt")
    db.add_all([m1, m2]); db.commit(); db.refresh(m1); db.refresh(m2)

    # Both materials have "〜てはいられない" (duplicate)
    gp1 = GrammarPoint(material_id=m1.id, point_name="〜てはいられない",
                       explanation_jp="A", example_from_material="a",
                       difficulty_level="N2", mastered=False)
    gp2 = GrammarPoint(material_id=m1.id, point_name="〜がち",
                       explanation_jp="B", example_from_material="b",
                       difficulty_level="N2", mastered=False)
    gp3 = GrammarPoint(material_id=m2.id, point_name="〜てはいられない",
                       explanation_jp="A2", example_from_material="a2",
                       difficulty_level="N2", mastered=False)  # duplicate!
    gp4 = GrammarPoint(material_id=m2.id, point_name="〜たきり",
                       explanation_jp="C", example_from_material="c",
                       difficulty_level="N2", mastered=False)
    db.add_all([gp1, gp2, gp3, gp4]); db.commit()

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Failed: {resp.status_code}"

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    # A/B should be: gp1 (tewairenai) and gp2 (gachi)
    # "てはいられない" appears twice but counts as 1 → 3 unique: tewairenai, gachi, takiri
    assert cycle is not None
    assert cycle.grammar_a_id in (gp1.id, gp2.id, gp4.id)
    assert cycle.grammar_b_id in (gp1.id, gp2.id, gp4.id)
    assert cycle.grammar_a_id != cycle.grammar_b_id


def test_mastered_in_one_excludes_duplicate_in_other(client, db, mock_deepseek):
    """Mastered in matA → duplicates in matB excluded from new cycle."""
    m1 = Material(filename="ma.txt", content_text="M", source_type="txt")
    m2 = Material(filename="mb.txt", content_text="N", source_type="txt")
    db.add_all([m1, m2]); db.commit(); db.refresh(m1); db.refresh(m2)

    gp1 = GrammarPoint(material_id=m1.id, point_name="〜てはいられない",
                       explanation_jp="A", example_from_material="a",
                       difficulty_level="N2", mastered=True)  # mastered!
    gp2 = GrammarPoint(material_id=m1.id, point_name="〜がち",
                       explanation_jp="B", example_from_material="b",
                       difficulty_level="N2", mastered=False)
    gp3 = GrammarPoint(material_id=m2.id, point_name="〜てはいられない",
                       explanation_jp="A2", example_from_material="a2",
                       difficulty_level="N2", mastered=False)
    gp4 = GrammarPoint(material_id=m2.id, point_name="〜たきり",
                       explanation_jp="C", example_from_material="c",
                       difficulty_level="N2", mastered=False)
    db.add_all([gp1, gp2, gp3, gp4]); db.commit()

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Failed: {resp.status_code}"

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    # Only gachi and takiri are eligible (tewairenai excluded globally)
    assert cycle.grammar_a_id in (gp2.id, gp4.id)
    assert cycle.grammar_b_id in (gp2.id, gp4.id)
    assert cycle.grammar_a_id != cycle.grammar_b_id


def test_mastered_weak_point_does_not_override_exclusion(client, db, mock_deepseek):
    """Active weak point for mastered grammar does not override exclusion."""
    mat = Material(filename="wp.txt", content_text="W", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    gp1 = GrammarPoint(material_id=mat.id, point_name="〜てはいられない",
                       explanation_jp="A", example_from_material="a",
                       difficulty_level="N2", mastered=True)
    gp2 = GrammarPoint(material_id=mat.id, point_name="〜がち",
                       explanation_jp="B", example_from_material="b",
                       difficulty_level="N2", mastered=False)
    gp3 = GrammarPoint(material_id=mat.id, point_name="〜たきり",
                       explanation_jp="C", example_from_material="c",
                       difficulty_level="N2", mastered=False)
    db.add_all([gp1, gp2, gp3]); db.commit()

    wp = WeakPoint(point_type="grammar", point_reference=gp1.point_name,
                   error_count=3, is_active=True)
    db.add(wp); db.commit()

    # Only gp2 and gp3 eligible → must start
    resp = client.post("/study/start_cycle",
                       data={"material_ids": [mat.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"Failed: {resp.status_code}"

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    assert cycle.grammar_a_id != gp1.id
    assert cycle.grammar_b_id != gp1.id


# ===========================================================================
# Provenance tests
# ===========================================================================

def test_selected_grammar_preserves_material_source(client, db, two_materials,
                                                     mock_deepseek):
    """A/B grammar retains its original material_id."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    cycle = db.query(StudyCycle).filter(
        StudyCycle.id == state.current_cycle_id).first()
    ga = db.query(GrammarPoint).filter(
        GrammarPoint.id == cycle.grammar_a_id).first()
    gb = db.query(GrammarPoint).filter(
        GrammarPoint.id == cycle.grammar_b_id).first()
    # Each selected grammar's material_id must be one of the selected materials
    assert ga.material_id in (m1.id, m2.id)
    assert gb.material_id in (m1.id, m2.id)


# ===========================================================================
# Non-regression: skip, cancelled_mastered
# ===========================================================================

def test_skipped_still_correct_in_multi_material_cycle(client, db, two_materials,
                                                       mock_deepseek):
    """Skipped behavior from Phase 1a/1b remains correct."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Skip all
    for _ in range(3):
        client.post("/study/skip_module", follow_redirects=False)

    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    assert cycle.is_valid_completion is False


def test_cancelled_mastered_still_correct_in_multi_material_cycle(
    client, db, two_materials, mock_deepseek
):
    """cancelled_mastered from Phase 1b remains functional."""
    m1, m2, gp1, gp2, gp3, gp4 = two_materials

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [m1.id, m2.id]},
                       follow_redirects=False)
    assert resp.status_code in (303, 302)

    # Toggle gp1 to mastered
    resp = client.post(f"/materials/{m1.id}/grammar/{gp1.id}/toggle_mastered",
                       headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200

    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).all()

    cancelled = [q for q in all_qs if q.status == "cancelled_mastered"]
    assert len(cancelled) >= 1, "Expected at least 1 cancelled question"
