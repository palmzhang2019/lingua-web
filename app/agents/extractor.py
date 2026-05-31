"""
Structured extractor for Japanese language learning material.

Extracts grammar points and N2-level vocabulary from uploaded Japanese text
using the DeepSeek LLM. All extraction results are validated with Pydantic
before being returned to the caller for persistence.

Design principle: LLM only performs structured content extraction;
it must not directly operate the database.
"""

from app.llm import structured_extraction, is_available
from app.schemas import GrammarPointExtract, VocabItemExtract, ExtractionResult

# ---------------------------------------------------------------------------
# System prompt — instructs the model on the extraction task.
# The prompt is written to enforce strict JSON structure and require
# example_from_material to reduce hallucination. Difficulty level is explicit
# and constrained to JLPT levels N5-N1.
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM_PROMPT = """You are a Japanese language analysis assistant specialized in JLPT N2 material.

Your task is to analyse the provided Japanese text and extract:

1. **Grammar points** (文法) — identify distinct Japanese grammar patterns \
present in the text. Target N2-level grammar points primarily, but include \
relevant N3/N1 points if clearly evidenced. Each grammar point MUST include \
an EXACT excerpt from the input text as the example.

2. **Vocabulary items** (語彙) — extract useful N2-level vocabulary words \
that are actually used in the text. Include reading (hiragana), Chinese \
meaning, and an EXACT excerpt from the input text.

CRITICAL RULES:
- example_from_material MUST be a verbatim excerpt from the input text. \
Do not create fabricated examples.
- Only extract items that are actually evidenced in the provided material.
- difficulty_level must be exactly one of: N5, N4, N3, N2, N1.
- If no grammar points or vocabulary are found, return empty arrays.
- Return ONLY valid JSON matching the requested schema. No extra text.
"""


def extract_grammar_points(material_text: str) -> list[GrammarPointExtract]:
    """
    Extract Japanese grammar points from *material_text*.

    Returns a list of validated GrammarPointExtract objects. Returns an
    empty list if extraction fails or the LLM is unavailable.
    """
    if not is_available():
        print("[extractor] LLM not available — skipping grammar extraction")
        return []

    user_prompt = (
        "Extract Japanese grammar points from the following material.\n\n"
        "--- MATERIAL START ---\n"
        f"{material_text}\n"
        "--- MATERIAL END ---\n\n"
        "Return JSON with this exact structure:\n"
        '{"grammar_points": [{"point_name": "...", "explanation_jp": "...", '
        '"example_from_material": "...", "difficulty_level": "N2"}], '
        '"vocab_items": []}\n'
        "Each example_from_material must be an exact excerpt from the material."
    )

    system_msg = EXTRACT_SYSTEM_PROMPT + (
        "\nFocus on grammar points only. Return vocab_items as an empty array."
    )

    result = structured_extraction(system_msg, user_prompt, ExtractionResult)
    if result is None:
        print("[extractor] Grammar extraction returned None")
        return []

    validated = _validate_examples(result.grammar_points, material_text)
    print(f"[extractor] Grammar extraction: {len(result.grammar_points)} raw → {len(validated)} validated")
    return validated


def extract_vocab(material_text: str) -> list[VocabItemExtract]:
    """
    Extract Japanese N2-level vocabulary from *material_text*.

    Returns a list of validated VocabItemExtract objects. Returns an
    empty list if extraction fails or the LLM is unavailable.
    """
    if not is_available():
        print("[extractor] LLM not available — skipping vocab extraction")
        return []

    user_prompt = (
        "Extract useful N2-level Japanese vocabulary from the following material.\n\n"
        "--- MATERIAL START ---\n"
        f"{material_text}\n"
        "--- MATERIAL END ---\n\n"
        "Return JSON with this exact structure:\n"
        '{"vocab_items": [{"word": "...", "reading": "...", "meaning_zh": "...", '
        '"example_from_material": "...", "difficulty_level": "N2"}], '
        '"grammar_points": []}\n'
        "Each example_from_material must be an exact excerpt from the material."
    )

    system_msg = EXTRACT_SYSTEM_PROMPT + (
        "\nFocus on vocabulary items only. Return grammar_points as an empty array."
    )

    result = structured_extraction(system_msg, user_prompt, ExtractionResult)
    if result is None:
        print("[extractor] Vocab extraction returned None")
        return []

    validated = _validate_vocab_examples(result.vocab_items, material_text)
    print(f"[extractor] Vocab extraction: {len(result.vocab_items)} raw → {len(validated)} validated")
    return validated


def _validate_examples(
    items: list[GrammarPointExtract],
    material_text: str,
) -> list[GrammarPointExtract]:
    """Filter out items whose example_from_material cannot be found in the source text."""
    valid: list[GrammarPointExtract] = []
    for item in items:
        # The example must appear verbatim in the uploaded text (exact substring match)
        if item.example_from_material and item.example_from_material in material_text:
            valid.append(item)
        else:
            print(
                f"[extractor] Rejecting grammar point '{item.point_name}' — "
                f"example not found in material text"
            )
    return valid


def _validate_vocab_examples(
    items: list[VocabItemExtract],
    material_text: str,
) -> list[VocabItemExtract]:
    """Filter out vocab items whose example_from_material cannot be found."""
    valid: list[VocabItemExtract] = []
    for item in items:
        if item.example_from_material and item.example_from_material in material_text:
            valid.append(item)
        else:
            print(
                f"[extractor] Rejecting vocab '{item.word}' — "
                f"example not found in material text"
            )
    return valid
