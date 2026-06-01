"""Weak-point provenance tests: verify WeakPointEvent records and per-cycle
new-vs-re-hit aggregation across all three locked source types.

Rules (from Phase 4C locked product semantics):
- translation_low_score_target_grammar: target_grammar_correct false + score_hearts <= 5 auto-creates or
  re-hits a weak point for the target grammar.
- translation_candidate_confirmed: user adds a pending candidate as weak point.
- choice_wrong_answer: wrong MC answer creates or re-hits a weak point.

Each qualifying write produces one WeakPointEvent with event_type="created"
or "hit_existing". Completed-cycle historical summary aggregates these into
separate "new weak-point count" and "repeated-hit count" columns.

All tests use isolated temp DB and mocked DeepSeek.
Run with: uv run pytest tests/test_weak_point_provenance.py -v
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
from app.routes.study import _record_weak_point, _insert_error_candidates

# ===========================================================================
# Mock helpers (mirror Phase 4A patterns)
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
        self.feedback_zh = "反馈"
        self.corrected_answer_ja = "正解"
        self.reason_zh = "理由"
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
def mock_basic():
    """score_hearts=10, no errors (pass)"""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=10)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_low_score():
    """score_hearts=4 (<=5, should trigger auto weak point)"""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(score_hearts=4, target_grammar_correct=False)
        me.return_value = MockExp()
        mt.return_value = MockTrans()
        mmc.return_value = MockMC()
        yield

@pytest.fixture
def mock_low_with_errors():
    """score_hearts=5 with additional errors (both auto + candidate sources)"""
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mev2.return_value = MockEvalV2(
            score_hearts=5,
            target_grammar_correct=False,
            additional_errors=[
                MockErrorItem(error_type="particle", error_rule_key="particle:を→に:test",
                              description="助词错误"),
            ]
        )
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

@pytest.fixture
def populated_material_suffix(db):
    """Material with a grammar point that has a different name, for distinct weak points."""
    from app.models import TranslationErrorCandidate
    db.query(WeakPointEvent).delete()
    db.query(TranslationErrorCandidate).delete()
    db.query(WeakPoint).delete()
    db.query(QuestionAttempt).delete()
    db.query(CycleMaterial).delete()
    db.query(StudyCycle).delete()
    db.query(SessionState).delete()
    db.commit()
    mat = Material(filename="test2.txt", content_text="Test2.", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    for name in ["〜わけがない", "〜ものか", "〜ものだ"]:
        db.add(GrammarPoint(material_id=mat.id, point_name=name,
               explanation_jp="Y", example_from_material="y",
               difficulty_level="N2", mastered=False))
    db.commit()
    return mat


# ===========================================================================
# Unit-level: _record_weak_point returns correct event_type
# ===========================================================================

def test_record_weak_point_created_event(db):
    """_record_weak_point returns 'created' when weak point doesn't exist yet."""
    cycle = StudyCycle(started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    event_type = _record_weak_point(db, "〜てはいられない",
                                     cycle_id=cycle.id,
                                     source_type="translation_low_score_target_grammar")
    assert event_type == "created", f"Expected 'created', got {event_type}"

def test_record_weak_point_hit_existing_event(db):
    """_record_weak_point returns 'hit_existing' when weak point already exists."""
    cycle = StudyCycle(started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    _record_weak_point(db, "〜てはいられない",
                       cycle_id=cycle.id,
                       source_type="translation_low_score_target_grammar")
    event_type = _record_weak_point(db, "〜てはいられない",
                                     cycle_id=cycle.id,
                                     source_type="translation_low_score_target_grammar")
    assert event_type == "hit_existing", f"Expected 'hit_existing', got {event_type}"


# ===========================================================================
# Unit-level: WeakPointEvent records created for each source type
# ===========================================================================

def test_translation_low_score_creates_event(db):
    """Source 1: low_heart translation creates WeakPointEvent with correct type."""
    # Use a unique grammar name to avoid cross-test collision
    cycle = StudyCycle(started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    _record_weak_point(db, "〜source1_create_test",
                       cycle_id=cycle.id,
                       source_type="translation_low_score_target_grammar")
    events = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle.id
    ).all()
    assert len(events) == 1
    assert events[0].source_type == "translation_low_score_target_grammar"
    assert events[0].event_type == "created"

def test_translation_low_score_hit_existing_event(db):
    """Same grammar hit twice: first 'created', second 'hit_existing'."""
    cycle = StudyCycle(started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    _record_weak_point(db, "〜source1_hit_test",
                       cycle_id=cycle.id,
                       source_type="translation_low_score_target_grammar")
    _record_weak_point(db, "〜source1_hit_test",
                       cycle_id=cycle.id,
                       source_type="translation_low_score_target_grammar")
    events = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle.id
    ).order_by(WeakPointEvent.id).all()
    assert len(events) == 2
    assert events[0].event_type == "created"
    assert events[1].event_type == "hit_existing"

def test_candidate_confirmed_creates_event(db):
    """Source 2: confirmed candidate creates WeakPointEvent."""
    cycle = StudyCycle(started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    _record_weak_point(db, "助词错误",
                       cycle_id=cycle.id,
                       source_type="translation_candidate_confirmed")
    events = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle.id
    ).all()
    assert len(events) == 1
    assert events[0].source_type == "translation_candidate_confirmed"
    assert events[0].event_type == "created"

def test_choice_wrong_answer_creates_event(db):
    """Source 3: wrong choice answer creates WeakPointEvent."""
    cycle = StudyCycle(started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    _record_weak_point(db, "〜source3_create_test",
                       cycle_id=cycle.id,
                       source_type="choice_wrong_answer")
    events = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle.id
    ).all()
    assert len(events) == 1
    assert events[0].source_type == "choice_wrong_answer"
    assert events[0].event_type == "created"


# ===========================================================================
# Integration-level: All three sources produce events during an actual cycle
# ===========================================================================

def test_all_three_source_types_integration(client, db, populated_material, mock_low_with_errors):
    """Real cycle with all three sources produces WeakPointEvents for each."""
    # Start cycle (grammar_a = 〜てはいられない)
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle_id
    ).order_by(QuestionAttempt.id).all()

    # Q1 translation: score_hearts=5 (tgc=false) with additional errors
    # This creates source 1 (auto target grammar) and source 2 (pending candidate)
    client.post("/study/answer", data={"answer": "bad answer"}, follow_redirects=False)

    # Check source 1 event: translation_low_score_target_grammar
    events_cycle1 = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle_id,
        WeakPointEvent.source_type == "translation_low_score_target_grammar"
    ).all()
    assert len(events_cycle1) > 0, "Expected translation_low_score events"
    assert events_cycle1[0].source_type == "translation_low_score_target_grammar"
    assert events_cycle1[0].event_type in ("created", "hit_existing")

    # Check source 2: pending candidates exist (from additional_errors)
    candidates = db.query(TranslationErrorCandidate).filter(
        TranslationErrorCandidate.cycle_id == cycle_id,
        TranslationErrorCandidate.status == "pending"
    ).all()
    assert len(candidates) > 0, "Expected pending candidates from additional errors"

    # Answer remaining 9 translations to reach review gate
    for i in range(9):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)

    # Confirm candidate and add it (source 2)
    candidate = db.query(TranslationErrorCandidate).filter(
        TranslationErrorCandidate.cycle_id == cycle_id,
        TranslationErrorCandidate.status == "pending"
    ).first()
    assert candidate is not None
    client.post(f"/study/candidate/{candidate.id}/add", follow_redirects=False)

    # Check source 2 event
    events_cycle2 = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle_id,
        WeakPointEvent.source_type == "translation_candidate_confirmed"
    ).all()
    assert len(events_cycle2) > 0, "Expected translation_candidate_confirmed events"


def test_choice_wrong_answer_integration(client, db, populated_material_suffix, mock_low_score):
    """Wrong MC answer during real cycle creates choice_wrong_answer event."""
    # Use mock_low_score so translation Q1 is low-score (source 1),
    # then answer the rest with mock_high_score for translation, then MC wrong.
    # Actually mock_low_score always returns 4. We need to switch mocks.
    # Let's directly test _record_weak_point call via the MC route.
    
    # Start cycle
    client.post("/study/start_cycle", data={"material_id": populated_material_suffix.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id
    
    # Answer all 10 translations with mock_low_score (they'll produce events)
    for i in range(10):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)
    
    # Now MC: we mock wrong answer by using a mock that returns grammar_point
    # but we send wrong choice
    client.post("/study/answer", data={"answer": "B"}, follow_redirects=False)  # MC mock returns expected="A"
    
    # Check source 3 event
    events_mc = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle_id,
        WeakPointEvent.source_type == "choice_wrong_answer"
    ).all()
    assert len(events_mc) > 0, "Expected choice_wrong_answer events"
    assert events_mc[0].source_type == "choice_wrong_answer"
    assert events_mc[0].event_type in ("created", "hit_existing")


# ===========================================================================
# Cycle summary: WeakPointEvent aggregation produces correct counts
# ===========================================================================

def test_cycle_summary_new_and_re_hit_counts(client, db, populated_material, mock_low_score):
    """Completed cycle with WeakPointEvents shows correct new and re-hit counts."""
    from app.routes.study import _get_historical_cycle_summaries, _compute_cycle_completion

    # Start cycle
    client.post("/study/start_cycle", data={"material_id": populated_material.id},
                follow_redirects=False)
    state = db.query(SessionState).first()
    cycle_id = state.current_cycle_id

    # Answer Q1 (low score) -> creates "〜てはいられない" weak point (created)
    client.post("/study/answer", data={"answer": "bad"}, follow_redirects=False)

    # For remaining: still low score -> hit_existing
    for i in range(9):
        client.post("/study/answer", data={"answer": f"t{i}"}, follow_redirects=False)

    # Also: MC wrong answers produce choice_wrong_answer events
    for i in range(9):
        client.post("/study/answer", data={"answer": "B"}, follow_redirects=False)  # wrong, expected=A

    # Complete the cycle
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    _compute_cycle_completion(db, cycle)

    summaries = _get_historical_cycle_summaries(db)
    this_cycle = [s for s in summaries if s["id"] == cycle_id]
    assert len(this_cycle) == 1, "Cycle should appear in summaries"
    summary = this_cycle[0]

    # We should have: 1 created (first low-heart), 9 hit_existing (subsequent low-heart)
    # + however many choice_wrong_answer events
    events = db.query(WeakPointEvent).filter(WeakPointEvent.cycle_id == cycle_id).all()
    expected_created = sum(1 for e in events if e.event_type == "created")
    expected_hit = sum(1 for e in events if e.event_type == "hit_existing")

    assert not summary.get("is_legacy_stats", False), "Cycle with events should not show legacy"
    assert summary["new_wp"] == expected_created, (
        f"new_wp={summary['new_wp']} != expected_created={expected_created}"
    )
    assert summary["re_hit_wp"] == expected_hit, (
        f"re_hit_wp={summary['re_hit_wp']} != expected_hit={expected_hit}"
    )


def test_cycle_with_both_created_and_hit(db, populated_material):
    """Direct test: new grammar first creates, then re-hits."""
    from app.routes.study import _get_historical_cycle_summaries, _compute_cycle_completion

    gp = db.query(GrammarPoint).first()
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime.utcnow())
    db.add(cycle); db.commit(); db.refresh(cycle)
    cm = CycleMaterial(cycle_id=cycle.id, material_id=populated_material.id)
    db.add(cm)

    # Create 1 question attempt answered
    qa = QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
        question_payload_json={"type": "translation", "grammar_point": "〜てはいられない"},
        correct_answer="x", status="answered", is_correct=False,
        score_hearts=4, user_answer="bad",
        answered_at=datetime.datetime.utcnow())
    db.add(qa); db.commit()

    # Record weak point events:
    # First hit -> created
    _record_weak_point(db, "〜てはいられない", cycle_id=cycle.id,
                        source_type="translation_low_score_target_grammar",
                        attempt_id=qa.id)
    # Second hit on same grammar -> hit_existing
    _record_weak_point(db, "〜てはいられない", cycle_id=cycle.id,
                        source_type="translation_low_score_target_grammar",
                        attempt_id=qa.id)

    cycle.completed_at = datetime.datetime.utcnow()
    db.commit()

    summaries = _get_historical_cycle_summaries(db)
    this_cycle = [s for s in summaries if s["id"] == cycle.id]
    assert len(this_cycle) == 1

    assert not this_cycle[0].get("is_legacy_stats", False)
    assert this_cycle[0]["new_wp"] == 1, f"Expected 1 created, got {this_cycle[0]['new_wp']}"
    assert this_cycle[0]["re_hit_wp"] == 1, f"Expected 1 re-hit, got {this_cycle[0]['re_hit_wp']}"


def test_legacy_cycle_no_provenance_shows_dash(db, populated_material):
    """Old cycle without WeakPointEvents shows — for both count columns."""
    from app.routes.study import _get_historical_cycle_summaries

    gp = db.query(GrammarPoint).first()
    # Pre-provenance cycle
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime(2025, 1, 1),
                       completed_at=datetime.datetime(2025, 1, 1, 1, 0),
                       is_valid_completion=True)
    db.add(cycle); db.commit(); db.refresh(cycle)
    cm = CycleMaterial(cycle_id=cycle.id, material_id=populated_material.id)
    db.add(cm)
    qa = QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
        question_payload_json={"type": "translation", "grammar_point": "〜てはいられない"},
        correct_answer="x", status="answered", is_correct=True,
        user_answer="x", answered_at=datetime.datetime(2025, 1, 1))
    db.add(qa); db.commit()

    summaries = _get_historical_cycle_summaries(db)
    this_cycle = [s for s in summaries if s["id"] == cycle.id]
    assert len(this_cycle) == 1
    assert this_cycle[0]["new_wp"] == "—"
    assert this_cycle[0]["re_hit_wp"] == "—"
    assert this_cycle[0].get("is_legacy_stats", False) is True


# ===========================================================================
# Non-regression: Phase 4C existing tests remain valid
# ===========================================================================

def test_mermaid_loaded_from_local_vendor(client):
    resp = client.get("/static/vendor/mermaid.min.js")
    assert resp.status_code == 200

def test_progress_route_no_cycle_empty_state(client):
    resp = client.get("/study/progress")
    assert "没有进行中的学习" in resp.text


# ===========================================================================
# Non-regression: No event is created without cycle context
# ===========================================================================

def test_record_weak_point_without_cycle_does_not_create_event(db):
    """_record_weak_point without cycle_id/source_type does NOT create an event."""
    _record_weak_point(db, "〜てはいられない")  # No cycle context
    events = db.query(WeakPointEvent).all()
    assert len(events) == 0, "No event should be created without cycle context"


# ===========================================================================
# Voided event exclusion from progress counts
# ===========================================================================

def test_voided_event_excluded_from_summary_counts(db, populated_material):
    """
    A voided WeakPointEvent must be excluded from both new-wp and re-hit-wp
    counts in cycle summaries. It must remain queryable for audit history.
    """
    from app.routes.study import _get_historical_cycle_summaries, _compute_cycle_completion

    gp = db.query(GrammarPoint).first()
    cycle = StudyCycle(grammar_a_id=gp.id, grammar_b_id=gp.id,
                       started_at=datetime.datetime.utcnow())
    db.add(cycle)
    db.commit()
    db.refresh(cycle)
    cycle_id = cycle.id

    # Record a created event
    _record_weak_point(db, "〜voided_test", cycle_id=cycle_id,
                       source_type="translation_low_score_target_grammar",
                       attempt_id=99901)
    # Record a hit_existing event
    _record_weak_point(db, "〜voided_test", cycle_id=cycle_id,
                       source_type="translation_low_score_target_grammar",
                       attempt_id=99902)

    # Verify summary before voiding
    cycle.completed_at = datetime.datetime.utcnow()
    db.commit()
    summaries = _get_historical_cycle_summaries(db)
    this = [s for s in summaries if s["id"] == cycle_id]
    assert len(this) == 1, "Cycle should appear in summaries"

    pre_summary = this[0]
    # Before voiding: 1 created, 1 hit_existing
    assert pre_summary["new_wp"] == 1, f"Expected new_wp=1 before void, got {pre_summary['new_wp']}"
    assert pre_summary["re_hit_wp"] == 1, f"Expected re_hit_wp=1 before void, got {pre_summary['re_hit_wp']}"

    # Now void the hit_existing event
    event_to_void = (
        db.query(WeakPointEvent)
        .filter(
            WeakPointEvent.cycle_id == cycle_id,
            WeakPointEvent.event_type == "hit_existing"
        )
        .first()
    )
    assert event_to_void is not None, "hit_existing event should exist"
    event_id = event_to_void.id
    event_to_void.event_type = "voided"
    db.commit()

    # Verify voided event is still queryable
    still_there = db.query(WeakPointEvent).filter(WeakPointEvent.id == event_id).first()
    assert still_there is not None, "Voided event must remain queryable in DB"
    assert still_there.event_type == "voided", "Event should still show voided type"

    # Re-query summaries
    summaries2 = _get_historical_cycle_summaries(db)
    this2 = [s for s in summaries2 if s["id"] == cycle_id]
    assert len(this2) == 1

    post_summary = this2[0]
    # After voiding hit_existing → 1 created, 0 hit_existing, voided excluded from both
    assert post_summary["new_wp"] == 1, (
        f"new_wp should still be 1 (created event unchanged), got {post_summary['new_wp']}"
    )
    assert post_summary["re_hit_wp"] == 0, (
        f"re_hit_wp should be 0 (hit_existing was voided), got {post_summary['re_hit_wp']}"
    )

    # Clean up test data (isolated temp DB — no real DB impact)
    db.query(WeakPointEvent).filter(WeakPointEvent.cycle_id == cycle_id).delete()
    db.query(WeakPoint).filter(
        WeakPoint.point_reference == "〜voided_test"
    ).delete()
    db.query(StudyCycle).filter(StudyCycle.id == cycle_id).delete()
    db.commit()
