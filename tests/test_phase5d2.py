"""
Phase 5D-2 tests: Legacy broken-cycle recovery preview and confirmed repair.

All tests use isolated temporary databases and mocked LLM calls.
No real data is read or modified.
"""
import os, sys, tempfile, datetime
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
    Material, GrammarPoint, StudyCycle, CycleMaterial,
    QuestionAttempt, SessionState, TranslationErrorCandidate,
    WeakPoint, WeakPointEvent,
)
from app.routes.study import (
    _detect_legacy_broken_cycle,
    _find_eligible_replacement_grammar,
    _execute_manual_mastered_replacement,
    _get_sorted_cycle_questions,
    _first_pending_question_index,
    _check_review_gate,
    _build_legacy_recovery_preview_data,
    _reset_contaminated_mc_slot,
)
from fastapi.testclient import TestClient

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
    _call_count = 0
    def __init__(self, prompt_prefix="翻译题"):
        MockTrans._call_count += 1
        self.prompt_zh = f"{prompt_prefix} {MockTrans._call_count}"
        self.reference_answer_ja = "答え"
        self.grading_notes = "使用目标语法"
        self.grammar_point = "〜てはいられない"

class MockExp:
    def __init__(self):
        self.point_name = "〜てはいられない"
        self.meaning_zh = "无法持续"
        self.usage_notes_zh = "表示无法保持某种状态"
        self.example_sentences = ["例文1"]

class MockEvalV2:
    def __init__(self, score_hearts=10, target_grammar_correct=True, additional_errors=None):
        self.score_hearts = score_hearts
        self.target_grammar_correct = target_grammar_correct
        self.feedback_zh = "反馈"
        self.corrected_answer_ja = "正解"
        self.reason_zh = "理由"
        self.additional_errors = additional_errors or []

class MockErrorItem:
    def __init__(self, error_type="particle", error_rule_key="particle:wo→ni:tt",
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
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    from app.main import app as _app
    return TestClient(_app)


@pytest.fixture
def material_with_four_gp(db):
    """Create a material with 4 grammar points for testing."""
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
    for name in ["〜てはいられない", "〜がち", "〜たきり", "〜ものの"]:
        db.add(GrammarPoint(material_id=mat.id, point_name=name,
               explanation_jp="X", example_from_material="x",
               difficulty_level="N2", mastered=False))
    db.commit()
    return mat


@pytest.fixture
def mock_translation():
    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        me.return_value = MockExp()
        mev2.return_value = MockEvalV2(score_hearts=10)
        yield mt


def _create_legacy_cycle(db, material, position="grammar_a"):
    """Helper: create an active cycle then simulate the legacy broken state
    where a target grammar was marked mastered through the old cancel-only path,
    leaving the cycle target unchanged and MC slots in generation_failed
    with MC_MASTERED_GRAMMAR_CONTAMINATION."""
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == material.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_b = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == material.id,
        GrammarPoint.point_name == "〜がち",
    ).first()
    gp_c = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == material.id,
        GrammarPoint.point_name == "〜たきり",
    ).first()

    cycle = StudyCycle(started_at=datetime.datetime.utcnow(),
                       grammar_a_id=gp_a.id, grammar_b_id=gp_b.id)
    db.add(cycle); db.commit(); db.refresh(cycle)
    db.add(CycleMaterial(cycle_id=cycle.id, material_id=material.id))

    # Create 19 slots like start_cycle
    for i in range(5):
        db.add(QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
            question_payload_json={"type": "translation"}, correct_answer="",
            target_grammar_id=gp_a.id, status="cancelled_mastered"))
    for i in range(5):
        db.add(QuestionAttempt(cycle_id=cycle.id, module_type="grammar_b_translation",
            question_payload_json={"type": "translation"}, correct_answer="",
            target_grammar_id=gp_b.id, status="planned"))

    mc_slots = []
    for i in range(9):
        role = "grammar_a_distinction" if i < 2 else ("grammar_b_distinction" if i < 4 else "review")
        slot = QuestionAttempt(cycle_id=cycle.id, module_type="multiple_choice",
            question_payload_json={"type": "multiple_choice"}, correct_answer="",
            status="planned")
        # GA distinction MC slots (0-1) get contaminated
        if i < 2:
            slot.status = "generation_failed"
            slot.generation_error = "MC_MASTERED_GRAMMAR_CONTAMINATION"
        db.add(slot)
        mc_slots.append(slot)

    db.add(SessionState(current_cycle_id=cycle.id,
                        current_module="multiple_choice",
                        current_question_index=10))
    db.commit()

    # Mark the target grammar as mastered (simulating old toggle behavior)
    if position == "grammar_a":
        gp_a.mastered = True
    else:
        gp_b.mastered = True
    db.commit()

    return cycle, gp_a, gp_b, gp_c


# =============================================================================
# Detection and GET no-mutation tests
# =============================================================================

def test_legacy_recovery_preview_shown_for_incomplete_cycle_with_mastered_target_a(
    client, db, material_with_four_gp,
):
    """Preview displayed for anomalous grammar A. GET does not mutate."""
    mat = material_with_four_gp
    _create_legacy_cycle(db, mat, "grammar_a")

    resp = client.get("/study/legacy_recovery", follow_redirects=False)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    html = resp.text

    # Preview content check
    assert "〜てはいられない" in html  # withdrawn grammar
    assert "〜たきり" in html  # replacement grammar (first eligible)

    # Verify no mutation on GET
    gp = db.query(GrammarPoint).filter(GrammarPoint.point_name == "〜てはいられない").first()
    assert gp.mastered is True  # unchanged
    cycle = db.query(StudyCycle).first()
    assert cycle.grammar_a_id == gp.id  # unchanged


def test_legacy_recovery_preview_shown_for_incomplete_cycle_with_mastered_target_b_only(
    client, db, material_with_four_gp,
):
    """Preview displayed for grammar B when A is not anomalous."""
    mat = material_with_four_gp
    _create_legacy_cycle(db, mat, "grammar_b")

    resp = client.get("/study/legacy_recovery", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text
    assert "〜がち" in html  # withdrawn grammar B


def test_both_anomalous_targets_preview_only_grammar_a_first(
    db, material_with_four_gp,
):
    """When both targets are mastered, preview identifies grammar A only."""
    mat = material_with_four_gp
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_b = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜がち",
    ).first()

    cycle, _, _, _ = _create_legacy_cycle(db, mat, "grammar_a")
    # Also mark grammar B as mastered
    gp_b.mastered = True
    db.commit()

    is_legacy, found_cycle, position = _detect_legacy_broken_cycle(db)
    assert is_legacy is True
    assert found_cycle is not None
    assert position == "grammar_a", f"Expected grammar_a first, got {position}"


def test_completed_cycle_with_mastered_target_does_not_show_legacy_recovery(
    db, material_with_four_gp,
):
    """Completed cycle with mastered target does not trigger legacy detection."""
    mat = material_with_four_gp
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_b = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜がち",
    ).first()

    cycle = StudyCycle(started_at=datetime.datetime.utcnow(),
                       completed_at=datetime.datetime.utcnow(),
                       grammar_a_id=gp_a.id, grammar_b_id=gp_b.id)
    db.add(cycle); db.commit()
    gp_a.mastered = True
    db.add(SessionState(current_cycle_id=cycle.id))
    db.commit()

    is_legacy, _, _ = _detect_legacy_broken_cycle(db)
    assert is_legacy is False


def test_non_current_mastered_grammar_does_not_show_legacy_recovery(
    db, material_with_four_gp,
):
    """Non-target grammar mastered does not trigger legacy detection."""
    mat = material_with_four_gp
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_b = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜がち",
    ).first()
    gp_c = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜たきり",
    ).first()

    cycle = StudyCycle(started_at=datetime.datetime.utcnow(),
                       grammar_a_id=gp_a.id, grammar_b_id=gp_b.id)
    db.add(cycle); db.commit()
    db.add(CycleMaterial(cycle_id=cycle.id, material_id=mat.id))
    db.add(SessionState(current_cycle_id=cycle.id))
    # Master non-target grammar
    gp_c.mastered = True
    db.commit()

    is_legacy, _, _ = _detect_legacy_broken_cycle(db)
    assert is_legacy is False


def test_mc_api_or_parse_failure_without_mastered_target_does_not_show_legacy_recovery(
    db, material_with_four_gp,
):
    """MC_API_OR_PARSE_FAILURE without mastered target does not trigger legacy."""
    mat = material_with_four_gp
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_b = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜がち",
    ).first()

    cycle = StudyCycle(started_at=datetime.datetime.utcnow(),
                       grammar_a_id=gp_a.id, grammar_b_id=gp_b.id)
    db.add(cycle); db.commit()
    db.add(CycleMaterial(cycle_id=cycle.id, material_id=mat.id))

    # Create MC slot with API failure (not contamination)
    for i in range(9):
        slot = QuestionAttempt(cycle_id=cycle.id, module_type="multiple_choice",
            question_payload_json={"type": "multiple_choice"}, correct_answer="",
            status="generation_failed" if i == 0 else "planned",
            generation_error="MC_API_OR_PARSE_FAILURE" if i == 0 else None,
            target_grammar_id=gp_a.id if i == 0 else None)
        db.add(slot)
    db.add(SessionState(current_cycle_id=cycle.id, current_module="multiple_choice",
                        current_question_index=10))
    db.commit()

    is_legacy, _, _ = _detect_legacy_broken_cycle(db)
    assert is_legacy is False


def test_valid_phase5d1_replaced_cycle_does_not_show_legacy_recovery(
    client, db, material_with_four_gp, mock_translation,
):
    """Valid Phase 5D-1 replaced cycle does not trigger legacy detection."""
    mat = material_with_four_gp
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()

    # Start a cycle, then do Phase 5D-1 replacement
    client.post("/study/start_cycle", data={"material_id": mat.id},
                follow_redirects=False)
    mock_translation.side_effect = [MockTrans() for _ in range(5)]

    # Replace grammar A (Phase 5D-1 path)
    client.post(
        f"/materials/{mat.id}/grammar/{gp_a.id}/toggle_mastered",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    # After replacement, grammar A is mastered but no longer a cycle target
    # The cycle target now points to the unmastered replacement
    is_legacy, _, _ = _detect_legacy_broken_cycle(db)
    assert is_legacy is False, "Phase 5D-1 replaced cycle should not be detected as legacy"


# =============================================================================
# Preview with replacement tests
# =============================================================================

def test_preview_with_replacement_displays_original_replacement_and_reset_scope_without_mutation(
    client, db, material_with_four_gp,
):
    """Preview with replacement shows original, replacement, and reset scope. No mutation."""
    mat = material_with_four_gp
    _create_legacy_cycle(db, mat, "grammar_a")

    resp = client.get("/study/legacy_recovery", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    # Shows original (withdrawn) grammar
    assert "〜てはいられない" in html
    # Shows replacement grammar (first eligible)
    assert "〜たきり" in html
    # Shows scope summary
    assert "5" in html  # 5 translation slots
    assert "2" in html  # 2 MC slots

    # Verify no mutation on GET
    gp = db.query(GrammarPoint).filter(GrammarPoint.point_name == "〜てはいられない").first()
    assert gp.mastered is True
    cycle = db.query(StudyCycle).first()
    assert cycle.grammar_a_id == gp.id  # still points to original


# =============================================================================
# Confirm replacement tests
# =============================================================================

def test_confirm_recovery_with_replacement_reuses_fixed_slot_transaction(
    client, db, material_with_four_gp,
):
    """Confirm replacement reuses Phase 5D-1 transaction: original stays mastered,
    replacement becomes target, slots reset, navigation to replacement translations."""
    mat = material_with_four_gp
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_c = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜たきり",
    ).first()

    cycle, _, _, _ = _create_legacy_cycle(db, mat, "grammar_a")

    # Confirm replacement via POST
    resp = client.post("/study/legacy_recovery/confirm_replacement",
                       follow_redirects=False)
    assert resp.status_code == 200

    # Verify: original remains mastered
    db.refresh(gp_a)
    assert gp_a.mastered is True

    # Verify: cycle target updated to replacement
    cycle_id = db.query(SessionState).first().current_cycle_id
    cycle = db.query(StudyCycle).filter(StudyCycle.id == cycle_id).first()
    assert cycle.grammar_a_id == gp_c.id, \
        f"Expected grammar_a={gp_c.id}, got {cycle.grammar_a_id}"

    # Verify: GA translation slots reset to planned + rebound
    all_qs = _get_sorted_cycle_questions(db, cycle_id)
    ga_slots = [q for q in all_qs if q.module_type == "grammar_a_translation"]
    assert len(ga_slots) == 5
    for slot in ga_slots:
        assert slot.status == "planned", f"GA slot {slot.id} should be planned"
        assert slot.target_grammar_id == gp_c.id

    # Verify: GA distinction MC slots reset
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]
    assert mc_slots[0].status == "planned"
    assert mc_slots[1].status == "planned"

    # Verify: remaining MC slots (2-8) unchanged
    for i in range(2, 9):
        assert mc_slots[i].status in ("planned",), \
            f"MC slot {i} should remain planned"

    # Verify: navigation points to replacement translations
    state = db.query(SessionState).first()
    assert state.current_module == "grammar_a_translation"


def test_both_anomalous_targets_recover_a_then_detect_b_on_next_entry(
    client, db, material_with_four_gp,
):
    """First confirmation acts only on A. After A recovery, B detection still works."""
    mat = material_with_four_gp
    cycle, gp_a, gp_b, gp_c = _create_legacy_cycle(db, mat, "grammar_a")

    # Also mark grammar B as mastered (both anomalous)
    gp_b.mastered = True
    db.commit()

    # Detect: should find grammar A first
    is_legacy, _, position = _detect_legacy_broken_cycle(db)
    assert is_legacy is True
    assert position == "grammar_a"

    # Confirm recovery for A
    resp = client.post("/study/legacy_recovery/confirm_replacement",
                       follow_redirects=False)
    assert resp.status_code == 200

    # Start fresh transaction to see committed changes
    db.commit()

    # After A recovery, grammar A is no longer a cycle target
    # But grammar B is STILL mastered and still a cycle target
    is_legacy2, _, position2 = _detect_legacy_broken_cycle(db)
    assert is_legacy2 is True, "Grammar B should still be detected as anomalous"
    assert position2 == "grammar_b", f"Expected grammar_b, got {position2}"


# =============================================================================
# Preview without replacement tests
# =============================================================================

def test_preview_without_replacement_offers_restore_and_continue_without_mutation(
    client, db, material_with_four_gp,
):
    """No-replacement preview shows restore option. GET does not mutate."""
    mat = material_with_four_gp
    # Mark all other grammars as mastered so no replacement exists
    gp_a = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜てはいられない",
    ).first()
    gp_b = db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.point_name == "〜がち",
    ).first()
    for gp in db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.id.notin_([gp_a.id, gp_b.id]),
    ).all():
        gp.mastered = True
    db.commit()

    _create_legacy_cycle(db, mat, "grammar_a")

    resp = client.get("/study/legacy_recovery", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    # Shows restore option
    assert "取消掌握" in html

    # Verify no mutation
    db.refresh(gp_a)
    assert gp_a.mastered is True
    cycle = db.query(StudyCycle).first()
    assert cycle.grammar_a_id == gp_a.id


# =============================================================================
# Confirm restore tests
# =============================================================================

def test_confirm_restore_sets_mastered_false_and_preserves_existing_learning_records(
    client, db, material_with_four_gp,
):
    """Restore sets grammar mastered=False. Existing translations/scores/candidates/events unchanged."""
    mat = material_with_four_gp
    cycle, gp_a, gp_b, gp_c = _create_legacy_cycle(db, mat, "grammar_a")

    # Mark all replacements as mastered so 'Restore' is the only path
    for gp in db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.id.notin_([gp_a.id, gp_b.id]),
    ).all():
        gp.mastered = True
    db.commit()

    # Create candidates and weak-point events tied to existing GA slots
    ga_slots = [q for q in _get_sorted_cycle_questions(db, cycle.id)
                if q.module_type == "grammar_a_translation"]
    if ga_slots:
        for s in ga_slots:
            db.add(TranslationErrorCandidate(cycle_id=cycle.id,
                source_attempt_id=s.id, error_type="particle",
                error_rule_key=f"k{s.id}", original_fragment="x",
                corrected_fragment="y", description="d"))
    wp = WeakPoint(point_type="grammar", point_reference="test_wp",
                   error_count=1, is_active=True)
    db.add(wp); db.commit(); db.refresh(wp)
    if ga_slots:
        db.add(WeakPointEvent(cycle_id=cycle.id, weak_point_id=wp.id,
            source_type="translation_low_score_target_grammar",
            event_type="created", source_attempt_id=ga_slots[0].id))
    db.commit()

    # Capture pre-restore counts
    candidate_count_before = db.query(TranslationErrorCandidate).filter(
        TranslationErrorCandidate.cycle_id == cycle.id
    ).count()
    wp_count_before = db.query(WeakPoint).count()
    wp_event_count_before = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle.id
    ).count()

    # Confirm restore
    resp = client.post("/study/legacy_recovery/confirm_restore",
                       follow_redirects=False)
    assert resp.status_code == 303, f"Expected 303 redirect, got {resp.status_code}"

    # Verify: grammar mastered=False
    db.refresh(gp_a)
    assert gp_a.mastered is False

    # Verify: existing candidates unchanged
    candidate_count_after = db.query(TranslationErrorCandidate).filter(
        TranslationErrorCandidate.cycle_id == cycle.id
    ).count()
    assert candidate_count_after == candidate_count_before, \
        "Candidates should be preserved"

    # Verify: weak points unchanged
    wp_count_after = db.query(WeakPoint).count()
    assert wp_count_after == wp_count_before, "WeakPoints should be preserved"

    # Verify: weak point events unchanged
    wp_event_count_after = db.query(WeakPointEvent).filter(
        WeakPointEvent.cycle_id == cycle.id
    ).count()
    assert wp_event_count_after == wp_event_count_before, \
        "WeakPointEvents should be preserved"


def test_confirm_restore_resets_only_contaminated_distinction_mc_slots_for_affected_target(
    client, db, material_with_four_gp,
):
    """Restore resets only MC_MASTERED_GRAMMAR_CONTAMINATION failed slots for the affected target."""
    mat = material_with_four_gp
    cycle, gp_a, gp_b, gp_c = _create_legacy_cycle(db, mat, "grammar_a")

    # Mark all replacements as mastered
    for gp in db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.id.notin_([gp_a.id, gp_b.id]),
    ).all():
        gp.mastered = True
    db.commit()

    # Simulate an additional MC_API_OR_PARSE_FAILURE on a non-affected slot (MC slot 4)
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]
    if len(mc_slots) > 4:
        mc_slots[4].status = "generation_failed"
        mc_slots[4].generation_error = "MC_API_OR_PARSE_FAILURE"
        db.commit()

    # Confirm restore
    client.post("/study/legacy_recovery/confirm_restore", follow_redirects=False)

    db.refresh(cycle)
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]

    # GA distinction MC (0-1): should be reset to planned
    assert mc_slots[0].status == "planned", \
        f"GA MC slot 0 should be planned, got {mc_slots[0].status}"
    assert mc_slots[0].generation_error is None
    assert mc_slots[1].status == "planned", \
        f"GA MC slot 1 should be planned, got {mc_slots[1].status}"
    assert mc_slots[1].generation_error is None

    # GB distinction MC (2-3): should remain planned (unchanged)
    assert mc_slots[2].status == "planned"
    assert mc_slots[3].status == "planned"

    # MC_API_OR_PARSE_FAILURE slot (4): should NOT be reset
    assert mc_slots[4].status == "generation_failed", \
        f"API failure MC slot 4 should remain generation_failed, got {mc_slots[4].status}"
    assert mc_slots[4].generation_error == "MC_API_OR_PARSE_FAILURE", \
        "API failure MC slot should retain its original error code"

    # Remaining MC slots (5-8): unchanged
    for i in range(5, 9):
        assert mc_slots[i].status == "planned"


def test_confirm_restore_returns_cycle_to_continuable_study_flow(
    client, db, material_with_four_gp, mock_translation,
):
    """After restore, session routes to first pending question, MC can regenerate."""
    mat = material_with_four_gp
    cycle, gp_a, gp_b, gp_c = _create_legacy_cycle(db, mat, "grammar_a")

    # Mark all replacements as mastered
    for gp in db.query(GrammarPoint).filter(
        GrammarPoint.material_id == mat.id,
        GrammarPoint.id.notin_([gp_a.id, gp_b.id]),
    ).all():
        gp.mastered = True
    db.commit()

    # Confirm restore
    client.post("/study/legacy_recovery/confirm_restore", follow_redirects=False)

    # After restore, session should point to the first pending question
    state = db.query(SessionState).first()
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    first_pending = _first_pending_question_index(all_qs)
    assert first_pending is not None, "Should have pending questions"
    assert state.current_question_index == first_pending

    # The first pending should be the MC module (GA distinction MC was reset to planned)
    # Visit /study/current — should redirect to MC retry or show MC
    # Since the grammar is no longer mastered, MC generation should succeed
    mock_translation.side_effect = lambda *a: MockTrans()

    # Visit study page (should not redirect to legacy recovery now)
    resp = client.get("/study/current", follow_redirects=False)
    # Should either show current question or redirect to review/loading
    assert resp.status_code in (200, 303)


# =============================================================================
# Unit tests for helper functions
# =============================================================================

def test_reset_contaminated_mc_slot_clears_failure_state(db):
    """_reset_contaminated_mc_slot clears failure state correctly."""
    slot = QuestionAttempt(
        cycle_id=1, module_type="multiple_choice",
        question_payload_json={"type": "multiple_choice", "prompt": "old"},
        correct_answer="A", user_answer="B",
        is_correct=False, answered_at=datetime.datetime.utcnow(),
        score_hearts=None, target_grammar_correct=None,
        generation_error="MC_MASTERED_GRAMMAR_CONTAMINATION",
        generation_started_at=datetime.datetime.utcnow(),
        status="generation_failed",
    )
    _reset_contaminated_mc_slot(slot)

    assert slot.status == "planned"
    assert slot.generation_error is None
    assert slot.correct_answer == ""
    assert slot.user_answer is None
    assert slot.is_correct is False
    assert slot.answered_at is None
    assert slot.question_payload_json == {"type": "multiple_choice"}


# =============================================================================
# Audit 2: Preview HTML security tests
# =============================================================================

def test_legacy_recovery_preview_escapes_grammar_labels(client, db):
    """Grammar label containing HTML-like characters is safely escaped."""
    from app.models import Material, GrammarPoint, StudyCycle, CycleMaterial, \
        QuestionAttempt, SessionState, TranslationErrorCandidate, WeakPoint

    # Clean and create minimal data
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

    # Grammar name with HTML-like content to verify escaping
    gp_a = GrammarPoint(material_id=mat.id, point_name='<script>alert("xss")</script>',
                        explanation_jp="X", example_from_material="x",
                        difficulty_level="N2", mastered=True)
    gp_b = GrammarPoint(material_id=mat.id, point_name="〜がち",
                        explanation_jp="X", example_from_material="x",
                        difficulty_level="N2", mastered=False)
    db.add(gp_a); db.add(gp_b); db.commit(); db.refresh(gp_a); db.refresh(gp_b)

    cycle = StudyCycle(started_at=datetime.datetime.utcnow(),
                       grammar_a_id=gp_a.id, grammar_b_id=gp_b.id)
    db.add(cycle); db.commit(); db.refresh(cycle)
    db.add(CycleMaterial(cycle_id=cycle.id, material_id=mat.id))
    for i in range(5):
        db.add(QuestionAttempt(cycle_id=cycle.id, module_type="grammar_a_translation",
            question_payload_json={"type": "translation"}, correct_answer="",
            target_grammar_id=gp_a.id, status="cancelled_mastered"))
    for i in range(5):
        db.add(QuestionAttempt(cycle_id=cycle.id, module_type="grammar_b_translation",
            question_payload_json={"type": "translation"}, correct_answer="",
            target_grammar_id=gp_b.id, status="planned"))
    for i in range(9):
        slot = QuestionAttempt(cycle_id=cycle.id, module_type="multiple_choice",
            question_payload_json={"type": "multiple_choice"}, correct_answer="",
            status="generation_failed" if i < 2 else "planned",
            generation_error="MC_MASTERED_GRAMMAR_CONTAMINATION" if i < 2 else None)
        db.add(slot)
    db.add(SessionState(current_cycle_id=cycle.id, current_module="multiple_choice",
                        current_question_index=10))
    db.commit()

    resp = client.get("/study/legacy_recovery", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    # The HTML-escaped version should be present (not raw)
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in html, \
        "HTML-like grammar label should be escaped"
    # Raw script tag should NOT be present
    assert "<script>alert" not in html, \
        "Raw script tag must not appear in HTML"


def test_legacy_recovery_preview_does_not_expose_question_or_candidate_content(
    client, db, material_with_four_gp,
):
    """Only summary/count/safe labels shown. No payload/body/candidate content."""
    _create_legacy_cycle(db, material_with_four_gp, "grammar_a")

    resp = client.get("/study/legacy_recovery", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    # Should show grammar names (safe labels)
    assert "〜てはいられない" in html
    assert "〜たきり" in html

    # Should NOT show question payload content
    assert "prompt_zh" not in html
    assert "reference_answer" not in html

    # Should NOT show candidate body content
    assert "original_fragment" not in html
    assert "corrected_fragment" not in html


def test_get_legacy_recovery_preview_has_no_mutation(
    client, db, material_with_four_gp,
):
    """All state unchanged after GET preview."""
    cycle, gp_a, gp_b, gp_c = _create_legacy_cycle(db, material_with_four_gp, "grammar_a")

    # Capture state before GET
    ga_mastered_before = gp_a.mastered
    cycle_a_id_before = cycle.grammar_a_id

    resp = client.get("/study/legacy_recovery", follow_redirects=False)
    assert resp.status_code == 200

    # Verify no mutation
    db.commit()  # refresh test session
    db.refresh(gp_a)
    assert gp_a.mastered == ga_mastered_before, "Mastered flag changed on GET"
    db.refresh(cycle)
    assert cycle.grammar_a_id == cycle_a_id_before, "Cycle target changed on GET"

    # Slot states unchanged
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]
    assert mc_slots[0].generation_error == "MC_MASTERED_GRAMMAR_CONTAMINATION"
    assert mc_slots[0].status == "generation_failed"


# =============================================================================
# Audit 3: Legacy error compatibility tests
# =============================================================================

def test_restore_resets_current_sanitized_mastered_contamination_failure_only(
    client, db, material_with_four_gp,
):
    """Current MC_MASTERED_GRAMMAR_CONTAMINATION is reset for affected target.
    MC_API_OR_PARSE_FAILURE is preserved."""
    cycle, gp_a, gp_b, gp_c = _create_legacy_cycle(db, material_with_four_gp, "grammar_a")

    # Mark all replacements as mastered
    for gp in db.query(GrammarPoint).filter(
        GrammarPoint.material_id == material_with_four_gp.id,
        GrammarPoint.id.notin_([gp_a.id, gp_b.id]),
    ).all():
        gp.mastered = True
    db.commit()

    # Add API failure on non-affected slot
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]
    if len(mc_slots) > 4:
        mc_slots[4].status = "generation_failed"
        mc_slots[4].generation_error = "MC_API_OR_PARSE_FAILURE"
        db.commit()

    client.post("/study/legacy_recovery/confirm_restore", follow_redirects=False)
    db.commit()

    db.refresh(cycle)
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]

    # GA distinction MC (0-1): reset to planned
    assert mc_slots[0].status == "planned"
    assert mc_slots[0].generation_error is None
    assert mc_slots[1].status == "planned"
    assert mc_slots[1].generation_error is None

    # API failure slot (4): NOT reset
    assert mc_slots[4].status == "generation_failed"
    assert mc_slots[4].generation_error == "MC_API_OR_PARSE_FAILURE"


def test_restore_resets_known_pre_phase5c_mastered_contamination_value(
    client, db, material_with_four_gp,
):
    """Pre-Phase 5C static 'Generated content contains mastered grammar' is also reset."""
    cycle, gp_a, gp_b, gp_c = _create_legacy_cycle(db, material_with_four_gp, "grammar_a")

    # Override the error value to the pre-Phase 5C string
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]
    for i in range(2):
        mc_slots[i].generation_error = "Generated content contains mastered grammar"
    db.commit()

    # Mark all replacements as mastered
    for gp in db.query(GrammarPoint).filter(
        GrammarPoint.material_id == material_with_four_gp.id,
        GrammarPoint.id.notin_([gp_a.id, gp_b.id]),
    ).all():
        gp.mastered = True
    db.commit()

    client.post("/study/legacy_recovery/confirm_restore", follow_redirects=False)
    db.commit()

    db.refresh(cycle)
    all_qs = _get_sorted_cycle_questions(db, cycle.id)
    mc_slots = [q for q in all_qs if q.module_type == "multiple_choice"]

    # Both pre-Phase 5C contaminated slots should be reset
    assert mc_slots[0].status == "planned"
    assert mc_slots[0].generation_error is None
    assert mc_slots[1].status == "planned"
    assert mc_slots[1].generation_error is None
