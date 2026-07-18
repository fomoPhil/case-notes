"""Thin client for the local LM Studio Gemma 4 endpoint.

One entry point, chat(), used for all three model roles:
  - text generation (intent + note extraction)
  - glossary correction
  - vision verification (pass image file paths)

Hard rules baked in here:
  - Every request sends "reasoning_effort": "none" (disables thinking).
  - schema given -> structured output via json_schema, returns a parsed dict.
  - images are file paths, base64-encoded as data URIs onto the LAST user message.
  - Non-200 or JSON parse failure raises RuntimeError carrying the body text.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

import requests

from .config import LMSTUDIO_URL, MODEL

# Generous: local vision calls after an idle model can be slow (cold start).
_TIMEOUT_SECONDS = 300


def _encode_image(path: str) -> str:
    """Read an image file and return an OpenAI-style data URI."""
    p = Path(path)
    data = p.read_bytes()
    mime, _ = mimetypes.guess_type(p.name)
    if mime is None:
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _attach_images(messages: list, images: list[str]) -> list:
    """Return a copy of messages with images attached to the last user message.

    Gemma / OpenAI multimodal format: the message content becomes a list of
    parts, one {"type":"text",...} plus one {"type":"image_url",...} per image.
    """
    messages = [dict(m) for m in messages]

    # Find the last user message; fall back to appending one.
    idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            idx = i
            break
    if idx is None:
        messages.append({"role": "user", "content": ""})
        idx = len(messages) - 1

    original = messages[idx].get("content", "")
    parts: list = []
    if isinstance(original, str):
        if original:
            parts.append({"type": "text", "text": original})
    elif isinstance(original, list):
        parts.extend(original)

    for img in images:
        parts.append({"type": "image_url", "image_url": {"url": _encode_image(img)}})

    messages[idx] = {**messages[idx], "content": parts}
    return messages


def chat(
    messages: list,
    schema: dict | None = None,
    images: list[str] | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.2,
) -> str | dict:
    """Call the local Gemma model once.

    Args:
        messages: OpenAI-style chat messages.
        schema: JSON Schema for structured output. If given, the return value
            is the parsed dict matching that schema.
        images: file paths to attach to the last user message (for vision).
        max_tokens: generation cap.
        temperature: sampling temperature (low default for determinism).

    Returns:
        Parsed dict when schema is given, otherwise the raw string content.

    Raises:
        RuntimeError: on non-200 response or when the content cannot be parsed
            as the requested JSON.
    """
    if images:
        messages = _attach_images(messages, images)

    payload: dict = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # The load-bearing flag: turn thinking off on every single request.
        "reasoning_effort": "none",
    }

    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "debrief_schema",
                "strict": True,
                "schema": schema,
            },
        }

    try:
        resp = requests.post(LMSTUDIO_URL, json=payload, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(f"LM Studio request failed: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"LM Studio returned {resp.status_code}: {resp.text}"
        )

    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected LM Studio response body: {resp.text}") from exc

    if schema is None:
        return content

    try:
        return json.loads(content)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Failed to parse structured output as JSON: {exc}\nContent was:\n{content}"
        ) from exc
