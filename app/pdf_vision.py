"""
OpenAI PDF Vision analysis for Lingua Web.

Uses gpt-5.4-mini via the Responses API to understand PDF pages
and extract structured Japanese grammar/vocabulary learning content.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = os.getenv("OPENAI_API_KEY", "")
PDF_MODEL = os.getenv("OPENAI_PDF_MODEL", "gpt-5.4-mini")

# Pricing per million tokens (verified from official OpenAI pricing)
INPUT_COST_PER_M = 0.75      # $0.75/1M input tokens
CACHED_INPUT_COST_PER_M = 0.075  # $0.075/1M cached input tokens
OUTPUT_COST_PER_M = 4.50     # $4.50/1M output tokens

# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------
_usage_records: list[dict] = []


def get_and_clear_usage() -> list[dict]:
    records = list(_usage_records)
    _usage_records.clear()
    return records


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PdfGrammarItem:
    point_name: str
    explanation_zh: str
    example_from_page: str
    source_page: int
    difficulty_level: str = "N2"


@dataclass
class PdfVocabItem:
    word: str
    reading: str | None = None
    meaning_zh: str | None = None
    example_from_page: str | None = None
    source_page: int = 1
    difficulty_level: str = "N2"


@dataclass
class PdfExtractionResult:
    grammar_items: list[PdfGrammarItem] = field(default_factory=list)
    vocab_items: list[PdfVocabItem] = field(default_factory=list)
    page_summary: list[dict] = field(default_factory=list)
    raw_response: str = ""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """Check if OpenAI API key is configured."""
    return bool(API_KEY)


def extract_from_pdf_pages(
    pdf_path: str,
    start_page: int,
    end_page: int,
) -> PdfExtractionResult | None:
    """
    Extract learning content from selected PDF pages using gpt-5.4-mini.

    Uses the Responses API with PDF file input for vision-based understanding.
    Returns structured grammar/vocabulary items or None on failure.
    """
    if not API_KEY:
        return None

    client = OpenAI(api_key=API_KEY)

    # Step 1: Upload the PDF file
    file_id = _upload_pdf(client, pdf_path)
    if not file_id:
        return None

    # Step 2: Build the prompt
    prompt_text = _build_extraction_prompt(start_page, end_page)

    # Step 3: Call the Responses API
    try:
        response = client.responses.create(
            model=PDF_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_id": file_id,
                        },
                        {
                            "type": "input_text",
                            "text": prompt_text,
                        },
                    ],
                }
            ],
            temperature=0.1,
        )
    except Exception as exc:
        print(f"[openai_pdf] Responses API call failed: {exc}")
        _record_usage("error", 0, 0, 0)
        return None
    finally:
        # Step 4: Clean up uploaded file
        try:
            client.files.delete(file_id)
        except Exception:
            pass

    # Step 5: Parse response
    raw_text = ""
    for item in response.output:
        if hasattr(item, "content") and item.content:
            for part in item.content:
                if hasattr(part, "text") and part.text:
                    raw_text += part.text

    if not raw_text:
        print("[openai_pdf] Empty response from model")
        return None

    # Record usage
    usage = getattr(response, "usage", None)
    if usage:
        total_tokens = (getattr(usage, "input_tokens", 0) or 0) + \
                       (getattr(usage, "output_tokens", 0) or 0)
        _record_usage(
            "pdf_vision_extraction",
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            total_tokens,
        )

    # Step 6: Parse structured JSON from the response
    result = _parse_extraction_response(raw_text, start_page)
    result.raw_response = raw_text
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _upload_pdf(client: OpenAI, pdf_path: str) -> str | None:
    """Upload a PDF to OpenAI Files API and return the file ID."""
    try:
        with open(pdf_path, "rb") as f:
            file_obj = client.files.create(
                file=f,
                purpose="user_data",
            )
        return file_obj.id
    except Exception as exc:
        print(f"[openai_pdf] File upload failed: {exc}")
        return None


def _build_extraction_prompt(start_page: int, end_page: int) -> str:
    """Build the structured extraction prompt for the model."""
    prompt = f"""You are a Japanese language analysis assistant. Analyze pages {start_page} to {end_page} of this PDF document, which is a Japanese language learning material.

Extract the following and return ONLY valid JSON with this exact structure — no markdown fences, no extra text:

{{
  "grammar_points": [
    {{
      "point_name": "〜てはいられない",
      "explanation_zh": "中文解释",
      "example_from_page": "原文中的例句（含该语法）",
      "source_page": {start_page},
      "difficulty_level": "N2"
    }}
  ],
  "vocab_items": [
    {{
      "word": "単語",
      "reading": "よみかた",
      "meaning_zh": "中文意思",
      "example_from_page": "原文中的例句（含该词汇）",
      "source_page": {start_page},
      "difficulty_level": "N2"
    }}
  ],
  "page_summary": [
    {{
      "source_page": {start_page},
      "short_topic_zh": "该页主题",
      "quality_note": "内容质量简要评价"
    }}
  ]
}}

RULES:
- Only extract items that are actually visible in the selected pages.
- Ignore isolated furigana/ruby annotations — focus on base text.
- Every example_from_page must be an actual sentence from the document.
- difficulty_level must be one of: N5, N4, N3, N2, N1.
- source_page must match the actual page number in the PDF (1-based).
- Return empty arrays if no items are found.
- If you cannot read any Japanese content on the pages, set quality_note to "No visible Japanese learning content detected".
"""
    return prompt


def _parse_extraction_response(
    raw_text: str,
    default_page: int,
) -> PdfExtractionResult:
    """Parse the model's JSON response into structured data."""
    # Strip markdown fences if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        match = re.match(r"```(?:json)?\s*\n?(.*?)```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        print(f"[openai_pdf] JSON parse failed: {exc}")
        print(f"[openai_pdf] Raw (first 500 chars): {raw_text[:500]}")
        return PdfExtractionResult()

    grammar_items = []
    for g in data.get("grammar_points", []):
        try:
            grammar_items.append(PdfGrammarItem(
                point_name=str(g.get("point_name", "")),
                explanation_zh=str(g.get("explanation_zh", "")),
                example_from_page=str(g.get("example_from_page", "")),
                source_page=int(g.get("source_page", default_page)),
                difficulty_level=str(g.get("difficulty_level", "N2")),
            ))
        except (ValueError, TypeError) as e:
            print(f"[openai_pdf] Skipping grammar item: {e}")

    vocab_items = []
    for v in data.get("vocab_items", []):
        try:
            vocab_items.append(PdfVocabItem(
                word=str(v.get("word", "")),
                reading=str(v.get("reading", "")) if v.get("reading") else None,
                meaning_zh=str(v.get("meaning_zh", "")) if v.get("meaning_zh") else None,
                example_from_page=str(v.get("example_from_page", "")) if v.get("example_from_page") else None,
                source_page=int(v.get("source_page", default_page)),
                difficulty_level=str(v.get("difficulty_level", "N2")),
            ))
        except (ValueError, TypeError) as e:
            print(f"[openai_pdf] Skipping vocab item: {e}")

    page_summary = data.get("page_summary", [])

    print(f"[openai_pdf] Extracted {len(grammar_items)} grammar, {len(vocab_items)} vocab from pages")
    return PdfExtractionResult(
        grammar_items=grammar_items,
        vocab_items=vocab_items,
        page_summary=page_summary,
    )


def _record_usage(
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
):
    """Record API usage for cost tracking."""
    _usage_records.append({
        "purpose": purpose,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_estimate": _estimate_cost(input_tokens, output_tokens),
    })


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD based on gpt-5.4-mini rates."""
    return (input_tokens / 1_000_000) * INPUT_COST_PER_M + \
           (output_tokens / 1_000_000) * OUTPUT_COST_PER_M
