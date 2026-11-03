"""Deterministic validation for reconciliation records.

The deterministic pass rejects only obvious junk (empty text, numeric or
marks fragments, repetitive separators, OCR garbage) and downgrades records
with invalid marks metadata or without a clear question signal to
``needs_review`` for human review.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

OUTCOMES = ("valid", "needs_review", "invalid", "duplicate")

VALIDATOR_ID = "reconciliation-validator/2"

_SEPARATOR_CHARS = set("-_=~*")

_QUESTION_WORDS = {
    "what", "why", "how", "when", "where", "which", "who", "whom", "whose",
    "whether", "name", "state", "define", "explain", "describe", "solve",
    "find", "calculate", "compute", "prove", "derive", "list", "identify",
    "determine", "evaluate", "show", "write", "complete", "fill", "match",
    "select", "choose", "label", "draw", "compare", "discuss", "illustrate",
    "answer", "trace", "outline", "summarize", "summarise", "analyse",
    "analyze", "classify", "differentiate", "justify", "contrast", "relate",
    "interpret", "translate", "convert", "measure", "estimate", "predict",
    "verify", "check", "true", "false",
}

_FINITE_VERBS = {
    "am", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "do", "does", "did",
    "can", "could", "will", "would", "shall", "should", "may", "might", "must",
    "need", "needs", "show", "shows", "give", "gives", "use", "uses", "used",
    "want", "wants", "require", "requires", "required", "follow", "follows",
}


def _is_complete_statement(text: str) -> bool:
    """Declarative sentences with a finite verb are genuine statement questions."""
    tokens = re.findall(r"[A-Za-z]+", text)
    if len(tokens) < 4:
        return False
    letters = sum(len(token) for token in tokens)
    if letters < len(re.sub(r"\s", "", text)) * 0.5:
        return False
    return any(token in _FINITE_VERBS for token in tokens)


def text_signature(text: object) -> str:
    """Normalized signature used for duplicate detection and audit reuse."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def record_text(record: dict) -> str:
    for key in ("text", "cleaned_text", "original_text", "question", "content"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _finite_positive(value: object) -> bool:
    if value is None or type(value) is bool:
        return True
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _is_numeric_or_marks_fragment(text: str) -> bool:
    core = re.sub(r"\bmarks?\b", " ", text, flags=re.IGNORECASE)
    if re.findall(r"[A-Za-z]{2,}", core):
        return False
    return bool(re.search(r"\d", text)) or bool(re.search(r"\bmarks?\b", text, flags=re.IGNORECASE))


def _is_separator_line(text: str) -> bool:
    if len(text) < 8:
        return False
    return any(ch in _SEPARATOR_CHARS for ch in text) and \
        all(ch in _SEPARATOR_CHARS or ch.isspace() for ch in text)


def _is_ocr_noise(text: str) -> bool:
    symbols = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    if symbols < 3:
        return False
    return symbols / max(1, len(text)) >= 0.3


def _has_question_signal(text: str) -> bool:
    if "?" in text:
        return True
    tokens = re.findall(r"[A-Za-z]+", text.casefold())
    if not tokens:
        return False
    if tokens[0] in _QUESTION_WORDS:
        return True
    if any(token in _QUESTION_WORDS for token in tokens):
        return True
    return _is_complete_statement(text)


def assess_question(record: dict) -> dict:
    """Deterministic verdict for one parsed question record."""
    text = record_text(record).strip()
    marks = record.get("marks")
    if not text:
        return {"outcome": "invalid", "reason": "empty_text"}
    if marks is not None and not _finite_positive(marks):
        return {"outcome": "needs_review", "reason": "invalid_marks_metadata"}
    if _is_numeric_or_marks_fragment(text):
        return {"outcome": "invalid", "reason": "numeric_or_marks_fragment"}
    if _is_separator_line(text):
        if marks is not None:
            return {"outcome": "needs_review", "reason": "separator_with_marks"}
        return {"outcome": "invalid", "reason": "separator_line"}
    if _is_ocr_noise(text):
        return {"outcome": "invalid", "reason": "ocr_noise"}
    if _has_question_signal(text):
        return {"outcome": "valid", "reason": "clear_question_signal"}
    return {"outcome": "needs_review", "reason": "no_clear_question_signal"}


def validate_records(records: list[dict]) -> list[dict]:
    """Deterministic verdicts for a full parsed record list."""
    return [assess_question(record) for record in records if isinstance(record, dict)]


def validate_parsed_questions(parsed_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not parsed_path.is_file():
        raise FileNotFoundError(f"Missing parsed JSON: {parsed_path}")
    try:
        data = json.loads(parsed_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid parsed JSON: {exc}")
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list):
        raise ValueError("Parsed JSON missing questions list")
    valid = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        if not q.get("question") and not q.get("text") and not q.get("content"):
            continue
        valid.append(q)
    return valid, data.get("metadata") or {}

