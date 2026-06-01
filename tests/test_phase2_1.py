"""Phase 2.1 tests: safe selected-material deletion with history protection.

All tests use a temporary SQLite database — never data/lingua.db.
Run with: uv run pytest tests/test_phase2_1.py -v
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
    Material, GrammarPoint, VocabItem, StudyCycle,
    CycleMaterial, QuestionAttempt, SessionState,
)
from app.main import app

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
def unused_material(db):
    m = Material(filename="unused.txt", content_text="Unused.", source_type="txt")
    db.add(m); db.commit(); db.refresh(m)
    gp = GrammarPoint(material_id=m.id, point_name="〜てはいられない",
                      explanation_jp="X", example_from_material="x",
                      difficulty_level="N2")
    v = VocabItem(material_id=m.id, word="テスト", reading="てすと",
                  meaning_zh="测试")
    db.add_all([gp, v]); db.commit()
    return m

@pytest.fixture
def used_material(db):
    m = Material(filename="used.txt", content_text="Used.", source_type="txt")
    db.add(m); db.commit(); db.refresh(m)
    gp_a = GrammarPoint(material_id=m.id, point_name="〜がち",
                      explanation_jp="Y", example_from_material="y1",
                      difficulty_level="N2")
    gp_b = GrammarPoint(material_id=m.id, point_name="〜たきり",
                      explanation_jp="Z", example_from_material="y2",
                      difficulty_level="N2")
    db.add_all([gp_a, gp_b]); db.commit()
    # Create a cycle referencing this material
    c = StudyCycle(started_at=datetime.datetime.utcnow(), grammar_a_id=gp_a.id,
                   grammar_b_id=gp_b.id, is_valid_completion=False)
    db.add(c); db.commit(); db.refresh(c)
    cm = CycleMaterial(cycle_id=c.id, material_id=m.id)
    db.add(cm); db.commit()
    return m, c, gp_a, gp_b

# ===========================================================================
# Schema tests
# ===========================================================================

def test_archived_at_migration(db):
    """archived_at column exists and defaults to None."""
    m = Material(filename="test.txt", content_text="T", source_type="txt")
    db.add(m); db.commit(); db.refresh(m)
    assert m.archived_at is None, "New material must be active by default"

def test_existing_material_default_active(db):
    """Existing materials without archive stay active."""
    m = Material(filename="old.txt", content_text="O", source_type="txt")
    db.add(m); db.commit(); db.refresh(m)
    assert m.archived_at is None

# ===========================================================================
# Delete route tests
# ===========================================================================

def test_delete_requires_at_least_one(client):
    """Empty delete action shows error."""
    resp = client.post("/materials/delete_selected", data={})
    assert resp.status_code == 400
    assert "至少选择" in resp.text

def test_duplicate_ids_processed_once(client, db, unused_material):
    """Duplicate submitted IDs processed once."""
    resp = client.post("/materials/delete_selected",
                       data={"material_ids": [unused_material.id, unused_material.id]})
    assert resp.status_code == 200
    assert "已删除 1" in resp.text

def test_delete_unused_hard_deletes(client, db, unused_material):
    """Unused material: hard delete removes material + grammar + vocab."""
    mid = unused_material.id
    resp = client.post("/materials/delete_selected",
                       data={"material_ids": [mid]})
    assert resp.status_code == 200

    m = db.query(Material).filter(Material.id == mid).first()
    assert m is None, "Unused material must be hard-deleted"
    gp = db.query(GrammarPoint).filter(GrammarPoint.material_id == mid).first()
    assert gp is None, "Grammar points must be deleted with unused material"
    v = db.query(VocabItem).filter(VocabItem.material_id == mid).first()
    assert v is None, "Vocab items must be deleted with unused material"

def test_delete_used_material_archives(client, db, used_material):
    """Used material: archive preserves history."""
    mat, cycle, gp_a, gp_b = used_material
    resp = client.post("/materials/delete_selected",
                       data={"material_ids": [mat.id]})
    assert resp.status_code == 200

    db.refresh(mat)
    assert mat.archived_at is not None, "Used material must be archived"

    # Cycle/association preserved
    cm = db.query(CycleMaterial).filter(
        CycleMaterial.material_id == mat.id).first()
    assert cm is not None, "cycle_materials must survive archive"

    # Grammar preserved
    gp = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id).first()
    assert gp is not None, "Grammar points must survive archive"

def test_archived_material_hidden_from_list(client, db, used_material):
    """Archived material not shown in active materials list."""
    mat, cycle, gp_a, gp_b = used_material
    client.post("/materials/delete_selected",
                data={"material_ids": [mat.id]})

    resp = client.get("/materials")
    assert mat.filename not in resp.text, "Archived material must not appear in list"

def test_archived_material_rejected_from_single_start(client, db, used_material):
    """Single-material start rejects archived material."""
    mat, cycle, gp_a, gp_b = used_material
    client.post("/materials/delete_selected",
                data={"material_ids": [mat.id]})

    resp = client.post("/study/start_cycle",
                       data={"material_id": mat.id})
    assert resp.status_code == 400
    assert "已被删除或隐藏" in resp.text

def test_archived_material_rejected_from_multi_start(client, db, used_material, unused_material):
    """Multi-material start rejects mix containing archived material."""
    mat, cycle, gp_a, gp_b = used_material
    client.post("/materials/delete_selected",
                data={"material_ids": [mat.id]})

    resp = client.post("/study/start_cycle",
                       data={"material_ids": [mat.id, unused_material.id]})
    assert resp.status_code == 400
    assert "已被删除或隐藏" in resp.text

def test_active_materials_still_work(client, db, used_material):
    """Active materials still work for multi-material start."""
    mat, cycle, gp_a, gp_b = used_material
    # Still active — should work
    resp = client.post("/study/start_cycle",
                       data={"material_id": mat.id})
    # Will 500 because no DeepSeek mock, but NOT 400 archived error
    assert resp.status_code != 400, "Active material must not be rejected as archived"

def test_mastery_from_archived_still_excludes(client, db, used_material):
    """Mastery from archived material still excludes duplicate (via _build_combined_grammar_pool)."""
    mat, cycle, gp_a, gp_b = used_material

    # Mark grammar mastered
    gp_a.mastered = True
    db.commit()

    # Archive the material
    client.post("/materials/delete_selected",
                data={"material_ids": [mat.id]})

    # Create another material with same grammar (gachi) + distinct (takiri)
    m2 = Material(filename="m2.txt", content_text="M2", source_type="txt")
    db.add(m2); db.commit(); db.refresh(m2)
    gp2 = GrammarPoint(material_id=m2.id, point_name="〜がち",
                       explanation_jp="Z", example_from_material="z",
                       difficulty_level="N2", mastered=False)
    gp3 = GrammarPoint(material_id=m2.id, point_name="〜たきり",
                       explanation_jp="W", example_from_material="w",
                       difficulty_level="N2", mastered=False)
    db.add_all([gp2, gp3]); db.commit()

    # Test _build_combined_grammar_pool directly
    from app.routes.study import _build_combined_grammar_pool
    candidates, mastered = _build_combined_grammar_pool(db, [m2.id])

    # "〜がち" should be excluded by mastered from archived material
    candidate_names = {gp.point_name for gp in candidates}
    assert "〜がち" not in candidate_names, \
        "Mastered grammar from archived material must still be excluded"
    assert "〜たきり" in candidate_names, \
        "Unmastered grammar must remain eligible"
    assert "〜がち" in mastered or any("がち" in n for n in mastered), \
        "Mastered name must be in global mastered set"


def test_legacy_gp_cycle_reference_forces_archive(client, db):
    """Grammar point used as cycle A/B forces archive even without cycle_materials."""
    m = Material(filename="legacy.txt", content_text="Legacy ref.",
                 source_type="txt")
    db.add(m); db.commit(); db.refresh(m)
    gp = GrammarPoint(material_id=m.id, point_name="〜てはいられない",
                      explanation_jp="X", example_from_material="x",
                      difficulty_level="N2", mastered=False)
    db.add(gp); db.commit()

    # Create a cycle referencing this gp, WITHOUT a cycle_materials row
    c = StudyCycle(started_at=datetime.datetime.utcnow(),
                   grammar_a_id=gp.id, grammar_b_id=gp.id,
                   is_valid_completion=False)
    db.add(c); db.commit()

    # Delete — must archive because gp is referenced by cycle
    resp = client.post("/materials/delete_selected",
                       data={"material_ids": [m.id]})
    assert resp.status_code == 200

    db.refresh(m)
    assert m.archived_at is not None, \
        "Material must be archived when its GP is referenced by a cycle"
    # GP must still exist
    gp_check = db.query(GrammarPoint).filter(GrammarPoint.id == gp.id).first()
    assert gp_check is not None, "Grammar point referenced by cycle must survive"


def test_deleted_unused_mistaken_material_does_not_leave_mastery_side_effect(
    client, db
):
    """Hard-deleted mistaken source mastery does not persist in global set."""
    m = Material(filename="mistake.txt", content_text="Wrong upload.",
                 source_type="txt")
    db.add(m); db.commit(); db.refresh(m)
    gp = GrammarPoint(material_id=m.id, point_name="〜てはいられない",
                      explanation_jp="X", example_from_material="x",
                      difficulty_level="N2", mastered=True)
    db.add(gp); db.commit()

    client.post("/materials/delete_selected",
                data={"material_ids": [m.id]})
    assert db.query(Material).filter(Material.id == m.id).first() is None

    m2 = Material(filename="fresh.txt", content_text="Fresh.", source_type="txt")
    db.add(m2); db.commit(); db.refresh(m2)
    gp2 = GrammarPoint(material_id=m2.id, point_name="〜てはいられない",
                       explanation_jp="Y", example_from_material="y",
                       difficulty_level="N2", mastered=False)
    gp3 = GrammarPoint(material_id=m2.id, point_name="〜たきり",
                       explanation_jp="Z", example_from_material="z",
                       difficulty_level="N2", mastered=False)
    db.add_all([gp2, gp3]); db.commit()

    from app.routes.study import _build_combined_grammar_pool
    candidates, mastered = _build_combined_grammar_pool(db, [m2.id])
    candidate_names = {g.point_name for g in candidates}
    assert "〜てはいられない" in candidate_names, \
        "Mastery from hard-deleted mistaken material must not persist"
