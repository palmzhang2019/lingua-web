"""
Phase 5D-2.1 tests: role-aware MC contamination guard.

Root cause fixed here: the previous guard raw-substring-scanned the full MC
prompt + all choices against every globally-mastered grammar name. Japanese
grammar expressions are short and routinely appear as substrings of unrelated
(even of the legitimately-targeted, unmastered) grammar — so the scan permanently
false-failed and blocked all MC generation with MC_MASTERED_GRAMMAR_CONTAMINATION.

The corrected guard rejects an MC only when its declared TARGET grammar
(grammar_point) is itself mastered. Incidental textual presence of an unrelated
mastered grammar must not block a valid current MC.

All tests use isolated temporary databases and mocked LLM calls.
No real learning data is accessed.
"""
import os, sys, tempfile
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


# =============================================================================
# Mock helpers
# =============================================================================

class MockExp:
    def __init__(self):
        self.point_name = "X"
        self.meaning_zh = "m"
        self.usage_notes_zh = "u"
        self.example_sentences = ["e"]

class MockTrans:
    _call_count = 0
    def __init__(self):
        MockTrans._call_count += 1
        self.prompt_zh = f"翻译题 {MockTrans._call_count}"
        self.reference_answer_ja = "答え"
        self.grading_notes = "使用目标语法"
        self.grammar_point = "X"

class MockEvalV2:
    def __init__(self):
        self.score_hearts = 10
        self.target_grammar_correct = True
        self.feedback_zh = "反馈"
        self.corrected_answer_ja = "正解"
        self.reason_zh = "理由"
        self.additional_errors = []

class FakeMC:
    """A generated MC. grammar_point is the structured target grammar."""
    def __init__(self, prompt, grammar_point, role="grammar_a_distinction"):
        self.prompt = prompt
        self.A = "選択肢A"; self.B = "選択肢B"; self.C = "選択肢C"; self.D = "選択肢D"
        self.expected = "A"
        self.grammar_point = grammar_point
        self.question_role = role


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


def _make_material_with(db, names_mastered):
    """Create one material with grammar points. names_mastered: dict name->mastered."""
    mat = Material(filename="t.txt", content_text="t", source_type="txt")
    db.add(mat); db.commit(); db.refresh(mat)
    for name, mastered in names_mastered.items():
        db.add(GrammarPoint(material_id=mat.id, point_name=name,
               explanation_jp="X", example_from_material="x",
               difficulty_level="N2", mastered=mastered))
    db.commit()
    return mat


def _complete_translations(client, mt_mock, mat_id):
    client.post("/study/start_cycle", data={"material_id": mat_id},
                follow_redirects=False)
    with patch("app.routes.study.evaluate_translation_answer_v2") as mev2:
        mev2.return_value = MockEvalV2()
        mt_mock.side_effect = [MockTrans() for _ in range(5)]
        client.post("/study/generate_module")
        for i in range(5):
            client.post("/study/answer", data={"answer": str(i)}, follow_redirects=False)
        mt_mock.side_effect = [MockTrans() for _ in range(5)]
        client.post("/study/generate_module")
        for i in range(5):
            client.post("/study/answer", data={"answer": str(i + 5)}, follow_redirects=False)


# =============================================================================
# Test 1: incidental unrelated mastered grammar substring no longer blocks MC
# =============================================================================

def test_incidental_unrelated_mastered_substring_does_not_block_valid_mc(client, db):
    """Reproduces the real bug: a short, unrelated mastered grammar whose name is
    a substring of the generated MC text (and even of an unmastered target's name)
    must NOT cause MC_MASTERED_GRAMMAR_CONTAMINATION when the MC targets an
    unmastered grammar."""
    # "から" is mastered and is a 2-char substring that appears inside the
    # unmastered target "〜てからでないと" and inside the generated prompt.
    mat = _make_material_with(db, {
        "〜てからでないと": False,   # unmastered target A (contains "から")
        "〜ものなら": False,         # unmastered target B
        "から": True,                # unrelated short mastered grammar
    })

    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        me.return_value = MockExp()
        # First MC generation fails so the slot enters generation_failed.
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")
        _complete_translations(client, mt, mat.id)

        # Now the MC legitimately targets the unmastered grammar A; its prompt
        # text incidentally contains the mastered "から" substring.
        mmc.return_value = (
            FakeMC(prompt="日本に来てからでないと_____。",
                   grammar_point="〜てからでないと",
                   role="grammar_a_distinction"),
            None,
        )
        retry = client.post("/study/regenerate_mc").json()

    assert retry["ok"] is True, f"Valid MC must not be blocked, got {retry}"


# =============================================================================
# Test 2: an MC that actually TARGETS a mastered grammar is still rejected
# =============================================================================

def test_mc_targeting_mastered_grammar_is_still_rejected(client, db):
    """Structured exclusion preserved: if the MC's grammar_point is itself a
    mastered grammar, generation must fail with the contamination code."""
    mat = _make_material_with(db, {
        "〜がち": False,           # unmastered target A
        "〜たきり": False,          # unmastered target B
        "〜てはいられない": True,   # mastered grammar (used as MC target below)
    })

    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        me.return_value = MockExp()
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")
        _complete_translations(client, mt, mat.id)

        mmc.return_value = (
            FakeMC(prompt="_____ を選びなさい。",
                   grammar_point="〜てはいられない",  # mastered → must be rejected
                   role="review"),
            None,
        )
        retry = client.post("/study/regenerate_mc").json()

    assert retry["ok"] is False
    assert retry.get("error_code") == "MC_MASTERED_GRAMMAR_CONTAMINATION", \
        f"Expected contamination rejection, got {retry}"


# =============================================================================
# Test 3: normalization — leading 〜 difference must still match as target
# =============================================================================

def test_target_match_is_normalized_for_tilde(client, db):
    """grammar_point '〜てはいられない' must match mastered 'てはいられない'
    (and vice versa) under normalization, independent of the leading 〜."""
    mat = _make_material_with(db, {
        "〜がち": False,
        "〜たきり": False,
        "てはいられない": True,   # mastered WITHOUT leading 〜
    })

    with patch("app.routes.study.generate_one_translation") as mt, \
         patch("app.routes.study.generate_explanation") as me, \
         patch("app.routes.study.generate_one_multiple_choice") as mmc:
        me.return_value = MockExp()
        mmc.return_value = (None, "MC_API_OR_PARSE_FAILURE")
        _complete_translations(client, mt, mat.id)

        mmc.return_value = (
            FakeMC(prompt="_____。",
                   grammar_point="〜てはいられない",  # WITH leading 〜
                   role="review"),
            None,
        )
        retry = client.post("/study/regenerate_mc").json()

    assert retry["ok"] is False
    assert retry.get("error_code") == "MC_MASTERED_GRAMMAR_CONTAMINATION", \
        f"Expected normalized target match to reject, got {retry}"
