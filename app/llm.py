"""
DeepSeek LLM adapter for Lingua Web.

Centralises all provider-specific LLM calls in one file.
Uses OpenAI-compatible client to talk to DeepSeek API.
Reads configuration from environment variables only:
  - DEEPSEEK_API_KEY (required for live extraction)
  - DEEPSEEK_BASE_URL (default: https://api.deepseek.com/v1)
  - DEEPSEEK_MODEL   (default: deepseek-chat)

Note: DeepSeek does NOT support OpenAI's response_format structured parse.
We use regular chat completions + manual JSON parsing instead.
"""

import json
import os
import re
from typing import Any

from openai import OpenAI

# ---------------------------------------------------------------------------
# Usage tracking (module-level accumulator — thread-local not needed for single-thread dev)
# ---------------------------------------------------------------------------
_usage_records: list[dict] = []  # each: {"purpose": str, "prompt_tokens": int, "completion_tokens": int, "total_tokens": int}


def record_usage(purpose: str, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
    _usage_records.append({
        "purpose": purpose,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    })


def get_and_clear_usage() -> list[dict]:
    """Return all pending usage records and reset the accumulator."""
    records = list(_usage_records)
    _usage_records.clear()
    return records


# ---------------------------------------------------------------------------
# Configuration (read from environment)
# ---------------------------------------------------------------------------
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def _get_client() -> OpenAI | None:
    """Return an OpenAI client if credentials are available, else None."""
    if not API_KEY:
        return None
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def structured_extraction(
    system_prompt: str,
    user_prompt: str,
    response_format: type[Any],
) -> Any | None:
    """
    Make a structured LLM call returning data conforming to *response_format*.

    Since DeepSeek does not support OpenAI's structured output API, we:
    1. Request JSON in the prompt
    2. Parse the raw response with the Pydantic model

    Parameters
    ----------
    system_prompt : str
        The system message instructing the model.
    user_prompt : str
        The user message with the input data.
    response_format : Pydantic BaseModel subclass
        The expected structured output schema.

    Returns
    -------
    An instance of *response_format* if successful, or None if the call fails
    or credentials are unavailable.
    """
    client = _get_client()
    if client is None:
        return None

    # Build a schema hint for the prompt since DeepSeek can't do structured output
    schema_hint = _build_schema_description(response_format)
    system_with_schema = (
        f"{system_prompt}\n\n"
        f"You MUST respond with valid JSON matching this schema:\n"
        f"{schema_hint}\n\n"
        f"Return ONLY the JSON object, no markdown, no extra text."
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_with_schema},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=8192,
        )
        raw = response.choices[0].message.content
        if not raw:
            print("[llm] Empty response from DeepSeek")
            return None

        # Strip markdown code fences if present
        raw = _strip_fences(raw)

        # Record usage if available
        if response.usage:
            record_usage(
                purpose=f"structured_extraction:{response_format.__name__}",
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        # Parse JSON
        data = json.loads(raw)
        return response_format.model_validate(data)

    except json.JSONDecodeError as exc:
        print(f"[llm] JSON parse failed: {exc}")
        print(f"[llm] Raw response (first 500 chars): {raw[:500] if raw else 'N/A'}")
        return None
    except Exception as exc:
        print(f"[llm] DeepSeek call failed: {exc}")
        return None


def _strip_fences(text: str) -> str:
    """Remove markdown JSON code fences from the response."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        # Find the closing ```
        match = re.match(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    return text


def _build_schema_description(model_class: type[Any]) -> str:
    """Build a human-readable JSON schema description for the prompt."""
    schema = model_class.model_json_schema()
    lines = ["{"]
    required = set(schema.get("required", []))
    props = schema.get("properties", {})
    prop_lines = []
    for name, prop in props.items():
        req = " (REQUIRED)" if name in required else ""
        desc = prop.get("description", "")
        ptype = prop.get("type", "any")
        items_info = ""
        if "items" in prop:
            ref = prop["items"].get("$ref", "")
            if ref:
                items_info = f" — array of objects"
            else:
                items_info = f" — array of {prop['items'].get('type', 'any')}"
        prop_lines.append(f'    "{name}": <{ptype}>{items_info} — {desc}{req}')
    lines.append(",\n".join(prop_lines))
    lines.append("}")
    return "\n".join(lines)


def is_available() -> bool:
    """Check if LLM credentials are configured."""
    return bool(API_KEY)
