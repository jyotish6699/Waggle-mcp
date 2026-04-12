"""
Structured LLM-based extraction for Waggle.

Uses Pydantic models for schema validation + type coercion.
Primary path: local Ollama LLM → validated Pydantic model.
Fallback: regex pipeline in intelligence.py (zero-config guarantee).

Environment variables:
    WAGGLE_EXTRACT_BACKEND  = auto | llm | regex  (default: auto)
    WAGGLE_EXTRACT_MODEL    = ollama model name    (default: mistral)
    WAGGLE_EXTRACT_MIN_CONFIDENCE = float 0-1      (default: 0.5)
    WAGGLE_OLLAMA_URL       = ollama base URL       (default: http://localhost:11434)
    WAGGLE_OLLAMA_TIMEOUT_SECONDS = request timeout in seconds (default: 15)
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from waggle.models import NodeType

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EXTRACT_BACKEND: Literal["auto", "llm", "regex"] = os.getenv("WAGGLE_EXTRACT_BACKEND", "auto")  # type: ignore[assignment]
EXTRACT_MODEL = os.getenv("WAGGLE_EXTRACT_MODEL", "mistral")
MIN_CONFIDENCE = float(os.getenv("WAGGLE_EXTRACT_MIN_CONFIDENCE", "0.5"))
OLLAMA_URL = os.getenv("WAGGLE_OLLAMA_URL", "http://localhost:11434")

try:
    OLLAMA_TIMEOUT_SECONDS = float(os.getenv("WAGGLE_OLLAMA_TIMEOUT_SECONDS", "15"))
except (ValueError, TypeError):
    OLLAMA_TIMEOUT_SECONDS = 15.0

# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class ExtractedNodeType(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    ENTITY = "entity"
    CONCEPT = "concept"
    QUESTION = "question"
    NOTE = "note"


class ExtractedFact(BaseModel):
    """A single extracted memory node returned by the LLM."""

    label: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="Short title (≤ 10 words) summarising this fact.",
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Full natural-language description of the fact.",
    )
    node_type: ExtractedNodeType = Field(
        ...,
        description="Category: fact | decision | preference | entity | concept | question | note",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Extraction confidence: 1.0 = explicit, 0.7 = implied, 0.4 = weak signal.",
    )
    tags: list[str] = Field(default_factory=list)

    @field_validator("label", "content", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()

    @field_validator("tags", mode="before")
    @classmethod
    def coerce_tags(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(t).strip() for t in v if t]

    @model_validator(mode="after")
    def label_not_too_long(self) -> "ExtractedFact":
        words = self.label.split()
        if len(words) > 10:
            self.label = " ".join(words[:10])
        return self

    def to_candidate(self) -> dict:
        """Convert to the dict format expected by graph.observe_conversation."""
        tags = list(self.tags)
        tags.append("llm-extracted")
        tags.append(f"confidence:{self.confidence:.2f}")
        return {
            "label": self.label,
            "content": self.content,
            "node_type": NodeType(self.node_type.value),
            "tags": tags,
        }


class ExtractionResult(BaseModel):
    """The complete structured response from the LLM."""

    facts: list[ExtractedFact] = Field(default_factory=list)

    @field_validator("facts", mode="before")
    @classmethod
    def coerce_facts(cls, v: object) -> list[dict]:
        """Silently drop malformed individual facts instead of failing the whole batch."""
        if not isinstance(v, list):
            return []
        valid = []
        for item in v:
            if not isinstance(item, dict):
                continue
            # Must have at least a non-empty label and content
            if not str(item.get("label", "")).strip():
                log.debug("Skipping fact with empty label: %s", item)
                continue
            if not str(item.get("content", "")).strip():
                log.debug("Skipping fact with empty content: %s", item)
                continue
            valid.append(item)
        return valid


# ---------------------------------------------------------------------------
# Ollama transport
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a knowledge extraction engine for a developer memory graph.

Extract every technically meaningful fact, decision, preference, or architectural \
choice from the conversation turn provided.

Rules:
1. Prefer precision over recall — only extract things a developer would need to remember later.
2. Skip pleasantries, filler ("ok", "sure"), and generic acknowledgements.
3. confidence scale:
   - 1.0 = explicit statement  ("we decided to use Postgres")
   - 0.7 = strong implication  ("Postgres makes more sense here")
   - 0.4 = weak signal         ("maybe Postgres")
4. node_type must be exactly one of: fact, decision, preference, entity, concept, question, note
5. Return ONLY valid JSON — no markdown, no explanation."""

_USER_TEMPLATE = """\
User message:
{user_message}

Assistant response:
{assistant_response}

Return JSON:
{{"facts": [{{"label": "...", "content": "...", "node_type": "...", "confidence": 0.0, "tags": []}}]}}"""


def _call_ollama(prompt: str, model: str, url: str, timeout_seconds: float) -> str | None:
    """POST to local Ollama and return the raw response string, or None on error."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }).encode()

    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = json.loads(resp.read())
            return data.get("response", "")
    except Exception as exc:
        log.debug("Ollama unavailable (%s, timeout=%ss): %s", url, timeout_seconds, exc)
        return None


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_with_llm(
    user_message: str,
    assistant_response: str,
    *,
    model: str = EXTRACT_MODEL,
    min_confidence: float = MIN_CONFIDENCE,
    ollama_url: str = OLLAMA_URL,
    timeout_seconds: float = OLLAMA_TIMEOUT_SECONDS,
) -> list[dict] | None:
    """
    Extract structured facts using a local Ollama LLM with Pydantic validation.

    Returns:
        list[dict]  — validated candidate dicts ready for graph.observe_conversation()
        None        — if LLM is unavailable (caller should use regex fallback)

    Each dict contains: label, content, node_type (NodeType), tags (list[str])
    """
    prompt = (
        _SYSTEM_PROMPT
        + "\n\n"
        + _USER_TEMPLATE.format(
            user_message=user_message.strip() or "(none)",
            assistant_response=assistant_response.strip() or "(none)",
        )
    )

    raw = _call_ollama(prompt, model, ollama_url, timeout_seconds)
    if raw is None:
        return None  # signal caller: use regex fallback

    raw = _strip_fences(raw)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("LLM returned invalid JSON (%s) — falling back to regex", exc)
        return None

    try:
        result = ExtractionResult.model_validate(payload)
    except Exception as exc:
        log.warning("Pydantic validation failed (%s) — falling back to regex", exc)
        return None

    candidates = []
    for fact in result.facts:
        if fact.confidence < min_confidence:
            log.debug("Skipping low-confidence fact (%.2f): %s", fact.confidence, fact.label)
            continue
        candidates.append(fact.to_candidate())

    log.info(
        "LLM extraction: %d/%d facts passed confidence filter (≥ %.2f)",
        len(candidates),
        len(result.facts),
        min_confidence,
    )
    return candidates