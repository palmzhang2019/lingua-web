"""
Phase 5D-2.1 acceptance audit tests.

Audit 2 — role-aware MC contamination guard correctness:
  The guard must reject an MC only when the ACTUAL learning target is mastered.
  It checks both the MC's declared grammar_point and the server-side intended
  target derived from the slot role (slot_idx), so a missing/empty/mislabeled
  declaration cannot bypass mastered-target protection, while incidental textual
  presence of an unrelated mastered grammar does NOT block a valid question.

Audit 3 — stale `generating` MC slot recovery:
  A slot abandoned in `generating` is recovered to a retryable state by
  _ensure_next_question_generated once it is older than the stale threshold,
  while a recently-claimed in-flight slot is not reset, and pending/answered
  slots are left untouched.

All tests use an isolated temporary database and mocked LLM calls.
No real learning data is accessed.
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
from app.db import init_db, SessionLocal
from app.models import (
    Material, GrammarPoint, StudyCycle, CycleMaterial,
    QuestionAttempt, SessionState, TranslationErrorCandidate, WeakPoint,
    WeakPointEvent,
)
from app.routes.study import (
    _generate_slot_content,
    _ensure_next_question_generated,
    _STALE_GENERATING_SECONDS,
)


# =============================================================================
# Fixtures and helpers
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
    for t in [WeakPointEvent, TranslationErrorCandidate, WeakPoint, SessionState,
              QuestionAttempt, CycleMaterial, StudyCycle, GrammarPoint, Material]:
        session.query(t).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class FakeMC:
    def __init__(self, grammar_point, role="grammar_a_distinction", prompt="_____。"):
        self.prompt = prompt
        self.A = "選択肢A"; self.B = "選択肢B"; self.C = "選択肢C"; self.D = "選択肢D"
        self.expected = "A"
        self.grammar_point = grammar_point
        self.question_role = role


def _make_grammar(db, mat, name, mastered=False):
    gp = GrammarPoint(material_id=mat.id, point_name=name, explanation_jp="X",
                      example_from_material="x", difficulty_level="N2",
                      mastered=mastered)
    db.add(gp); db.commit(); db.refresh(gp)
    return gp


def _make_cycle_with_mc_slots(db, grammar_a, grammar_b, n_mc=9):
    """Create a cycle, its material association, and n_mc planned MC slots."""
    cycle = StudyCycle(started_at=datetime.datetime.utcnow(), completed_at=None,
                       grammar_a_id=grammar_a.id, grammar_b_id=grammar_b.id,
                       is_valid_completion=False)
    db.add(cycle); db.commit(); db.refresh(cycle)
    db.add(CycleMaterial(cycle_id=cycle.id, material_id=grammar_a.material_id))
    db.commit()
    slots = []
    for _ in range(n_mc):
        s = QuestionAttempt(cycle_id=cycle.id, module_type="multiple_choice",
                            question_payload_json={"type": "multiple_choice"},
                            correct_answer="", is_correct=False, status="planned")
        db.add(s); slots.append(s)
    db.commit()
    for s in slots:
        db.refresh(s)
    return cycle, slots


def _gen(db, slot, grammar_a, grammar_b, review_points, mastered_names, fake_mc):
    with patch("app.routes.study.generate_one_multiple_choice") as mmc:
        mmc.return_value = (fake_mc, None)
        return _generate_slot_content(db, slot, grammar_a, grammar_b,
                                      review_points, set(mastered_names))


# =============================================================================
# Audit 2 — role-aware guard
# =============================================================================

def test_non_target_mastered_text_does_not_reject_valid_mc_target(db):
    """Unrelated mastered grammar appearing in the text must NOT block a valid MC
    whose declared (and intended) target is unmastered."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜てからでないと")  # unmastered target A (contains から)
    gb = _make_grammar(db, mat, "〜ものなら")
    _make_grammar(db, mat, "から", mastered=True)    # unrelated short mastered

    cycle, slots = _make_cycle_with_mc_slots(db, ga, gb)
    fake = FakeMC(grammar_point=ga.point_name, role="grammar_a_distinction",
                  prompt="日本に来てからでないと_____。")  # incidental "から"
    ok = _gen(db, slots[0], ga, gb, [], {"から"}, fake)

    assert ok is True
    assert slots[0].status == "pending"


def test_mastered_declared_mc_target_is_still_rejected(db):
    """A genuinely mastered declared target remains rejected."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜がち")
    gb = _make_grammar(db, mat, "〜たきり")
    rev = _make_grammar(db, mat, "〜ものの")
    mastered = _make_grammar(db, mat, "〜てはいられない", mastered=True)

    cycle, slots = _make_cycle_with_mc_slots(db, ga, gb)
    fake = FakeMC(grammar_point=mastered.point_name, role="review")
    ok = _gen(db, slots[4], ga, gb, [rev], {mastered.point_name}, fake)

    assert ok is False
    assert slots[4].status == "generation_failed"
    assert slots[4].generation_error == "MC_MASTERED_GRAMMAR_CONTAMINATION"


def test_grammar_a_distinction_mc_must_declare_current_grammar_a_target(db):
    """A grammar-A distinction slot whose intended target (grammar A) is mastered
    is rejected even if the MC mislabels itself with a different unmastered name."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜てはいられない", mastered=True)  # intended A mastered
    gb = _make_grammar(db, mat, "〜たきり")

    cycle, slots = _make_cycle_with_mc_slots(db, ga, gb)
    # Mislabel as an unmastered grammar — must NOT bypass the server-side check.
    fake = FakeMC(grammar_point="〜がち", role="grammar_a_distinction")
    ok = _gen(db, slots[0], ga, gb, [], {ga.point_name}, fake)

    assert ok is False
    assert slots[0].status == "generation_failed"
    assert slots[0].generation_error == "MC_MASTERED_GRAMMAR_CONTAMINATION"


def test_grammar_b_distinction_mc_must_declare_current_grammar_b_target(db):
    """A grammar-B distinction slot whose intended target (grammar B) is mastered
    is rejected even if the MC mislabels itself."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜がち")
    gb = _make_grammar(db, mat, "〜てはいられない", mastered=True)  # intended B mastered

    cycle, slots = _make_cycle_with_mc_slots(db, ga, gb)
    fake = FakeMC(grammar_point="〜たきり", role="grammar_b_distinction")
    ok = _gen(db, slots[2], ga, gb, [], {gb.point_name}, fake)  # slot_idx 2 → grammar B

    assert ok is False
    assert slots[2].status == "generation_failed"
    assert slots[2].generation_error == "MC_MASTERED_GRAMMAR_CONTAMINATION"


def test_replacement_distinction_mc_must_declare_replacement_target(db):
    """After Phase 5D-1 replacement, grammar A is the unmastered replacement; an MC
    declaring that replacement target is accepted (the withdrawn mastered grammar
    is no longer the intended target)."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    replacement = _make_grammar(db, mat, "〜たきり")     # unmastered replacement A
    gb = _make_grammar(db, mat, "〜がち")
    _make_grammar(db, mat, "〜てはいられない", mastered=True)  # withdrawn, now mastered

    cycle, slots = _make_cycle_with_mc_slots(db, replacement, gb)
    fake = FakeMC(grammar_point=replacement.point_name, role="grammar_a_distinction")
    ok = _gen(db, slots[0], replacement, gb, [], {"〜てはいられない"}, fake)

    assert ok is True
    assert slots[0].status == "pending"


def test_review_mc_target_mastered_protection_remains_enforced(db):
    """A review MC declaring a mastered actual target is rejected."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜がち")
    gb = _make_grammar(db, mat, "〜たきり")
    rev = _make_grammar(db, mat, "〜ものの")
    mastered = _make_grammar(db, mat, "〜わけにはいかない", mastered=True)

    cycle, slots = _make_cycle_with_mc_slots(db, ga, gb)
    fake = FakeMC(grammar_point=mastered.point_name, role="review")
    ok = _gen(db, slots[5], ga, gb, [rev], {mastered.point_name}, fake)

    assert ok is False
    assert slots[5].status == "generation_failed"
    assert slots[5].generation_error == "MC_MASTERED_GRAMMAR_CONTAMINATION"


def test_missing_or_invalid_mc_grammar_point_does_not_bypass_guard(db):
    """An empty/missing declared grammar_point must not let a mastered intended
    target through — the server-side slot target back-stops the guard."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜てはいられない", mastered=True)  # intended A mastered
    gb = _make_grammar(db, mat, "〜たきり")

    cycle, slots = _make_cycle_with_mc_slots(db, ga, gb)
    fake = FakeMC(grammar_point="", role="grammar_a_distinction")  # empty declaration
    ok = _gen(db, slots[0], ga, gb, [], {ga.point_name}, fake)

    assert ok is False
    assert slots[0].status == "generation_failed"
    assert slots[0].generation_error == "MC_MASTERED_GRAMMAR_CONTAMINATION"


def test_missing_grammar_point_with_unmastered_target_is_accepted(db):
    """Empty declared grammar_point with an unmastered intended target is still a
    valid question (the back-stop only rejects mastered targets)."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜がち")
    gb = _make_grammar(db, mat, "〜たきり")

    cycle, slots = _make_cycle_with_mc_slots(db, ga, gb)
    fake = FakeMC(grammar_point="", role="grammar_a_distinction")
    ok = _gen(db, slots[0], ga, gb, [], set(), fake)

    assert ok is True
    assert slots[0].status == "pending"


# =============================================================================
# Audit 3 — stale generating MC slot recovery
# =============================================================================

def _answered_translation(cycle_id):
    return QuestionAttempt(
        cycle_id=cycle_id, module_type="grammar_a_translation",
        question_payload_json={"type": "translation"}, correct_answer="x",
        is_correct=True, status="answered", score_hearts=10,
        answered_at=datetime.datetime.utcnow(),
    )


def _setup_cycle_with_first_unresolved_mc(db, mc_status, started_offset_seconds,
                                          extra_before=None):
    """Build a cycle whose first unresolved slot is a multiple_choice slot with the
    given status/age. Translations are answered so the MC slot is first unresolved."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    ga = _make_grammar(db, mat, "〜がち")
    gb = _make_grammar(db, mat, "〜たきり")
    cycle = StudyCycle(started_at=datetime.datetime.utcnow(), completed_at=None,
                       grammar_a_id=ga.id, grammar_b_id=gb.id, is_valid_completion=False)
    db.add(cycle); db.commit(); db.refresh(cycle)
    db.add(CycleMaterial(cycle_id=cycle.id, material_id=mat.id)); db.commit()
    # one answered translation so MC becomes the first unresolved slot
    db.add(_answered_translation(cycle.id)); db.commit()
    for spec in (extra_before or []):
        s = QuestionAttempt(cycle_id=cycle.id, module_type="multiple_choice",
                            question_payload_json={"type": "multiple_choice"},
                            correct_answer="A", is_correct=spec.get("is_correct", False),
                            status=spec["status"],
                            answered_at=datetime.datetime.utcnow() if spec["status"] == "answered" else None)
        db.add(s)
    db.commit()
    started = None
    if started_offset_seconds is not None:
        started = datetime.datetime.utcnow() - datetime.timedelta(seconds=started_offset_seconds)
    target = QuestionAttempt(cycle_id=cycle.id, module_type="multiple_choice",
                             question_payload_json={"type": "multiple_choice"},
                             correct_answer="", is_correct=False, status=mc_status,
                             generation_started_at=started)
    db.add(target); db.commit(); db.refresh(target)
    state = SessionState(current_cycle_id=cycle.id, current_module="multiple_choice",
                         current_question_index=0, updated_at=datetime.datetime.utcnow())
    db.add(state); db.commit()
    return cycle, state, target


def test_stale_generating_mc_slot_becomes_retryable(db):
    """A generating slot older than the stale threshold is recovered to
    generation_failed (retryable)."""
    cycle, state, target = _setup_cycle_with_first_unresolved_mc(
        db, "generating", started_offset_seconds=_STALE_GENERATING_SECONDS + 30)
    _ensure_next_question_generated(db, state, cycle)
    db.refresh(target)
    assert target.status == "generation_failed"
    assert target.generation_error == "Generation timed out"


def test_stale_generating_with_no_started_at_is_recovered(db):
    """A generating slot with no generation_started_at is treated as stale."""
    cycle, state, target = _setup_cycle_with_first_unresolved_mc(
        db, "generating", started_offset_seconds=None)
    _ensure_next_question_generated(db, state, cycle)
    db.refresh(target)
    assert target.status == "generation_failed"


def test_recent_generating_mc_slot_not_reset(db):
    """A generating slot claimed moments ago (in-flight) is NOT force-failed."""
    cycle, state, target = _setup_cycle_with_first_unresolved_mc(
        db, "generating", started_offset_seconds=1)
    _ensure_next_question_generated(db, state, cycle)
    db.refresh(target)
    assert target.status == "generating", \
        "Recently claimed in-flight slot must not be reset"


def test_pending_and_answered_mc_slots_untouched_by_recovery(db):
    """Recovery of a stale generating slot leaves pending/answered MC slots intact."""
    cycle, state, target = _setup_cycle_with_first_unresolved_mc(
        db, "generating", started_offset_seconds=_STALE_GENERATING_SECONDS + 30,
        extra_before=[{"status": "pending"}, {"status": "answered", "is_correct": True}])
    all_before = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle.id,
        QuestionAttempt.module_type == "multiple_choice").order_by(QuestionAttempt.id).all()
    pending_slot, answered_slot = all_before[0], all_before[1]
    assert pending_slot.status == "pending" and answered_slot.status == "answered"

    _ensure_next_question_generated(db, state, cycle)

    db.refresh(pending_slot); db.refresh(answered_slot); db.refresh(target)
    assert pending_slot.status == "pending", "pending MC slot must be untouched"
    assert answered_slot.status == "answered", "answered MC slot must be untouched"
    assert target.status == "generation_failed", "stale generating slot recovered"


def test_phase5b_regenerate_claim_only_accepts_generation_failed(db):
    """Phase 5B concurrency guard preserved: regenerate_mc rejects a generating slot."""
    from fastapi.testclient import TestClient
    from app.main import app

    cycle, state, target = _setup_cycle_with_first_unresolved_mc(
        db, "generating", started_offset_seconds=1)
    # point session at the generating MC slot
    all_qs = db.query(QuestionAttempt).filter(
        QuestionAttempt.cycle_id == cycle.id).order_by(QuestionAttempt.id).all()
    idx = next(i for i, q in enumerate(all_qs) if q.id == target.id)
    state.current_question_index = idx
    state.current_module = "multiple_choice"
    db.commit()

    client = TestClient(app)
    resp = client.post("/study/regenerate_mc").json()
    assert resp["ok"] is False
    assert resp.get("error") in ("claim_failed", "not_retryable"), resp
    db.refresh(target)
    assert target.status == "generating", "claim must not mutate a generating slot"
