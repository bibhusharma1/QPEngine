from __future__ import annotations

import json
import logging
import re
import time
import base64
import copy
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from config import settings
from functions.QuesParser.ParsePipeLIne.schema import (
    SCHEMA_VERSION,
    QuestionBank,
    QuestionRecord,
)

logger = logging.getLogger(__name__)
_EXPLICIT_PRIORITY = re.compile(
    r"\bpriority\s*(?:(?:level|order)\s*)?[:=]?\s*([123])\b",
    re.IGNORECASE,
)
_EXPLICIT_QUESTION_HEADING = re.compile(
    r"^\s*(?:[*_`~]{1,3}\s*)?"
    r"(?:(?:question\s*(?:no\.?\s*)?|q\s*\.?\s*))?"
    r"(\d+)(?=\s|$|[.():]|[*_`~])",
    re.IGNORECASE,
)


def _safe_http_error_body(response: httpx.Response, api_key: str) -> str:
    """Return a bounded provider error detail with credentials removed."""
    body = (response.text or "").strip()
    if not body:
        return "<empty response body>"
    if api_key:
        body = body.replace(api_key, "[REDACTED]")
    body = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,\"']+",
        r"\1[REDACTED]",
        body,
    )
    body = re.sub(r"(?i)\bnvapi-[A-Za-z0-9_-]+", "[REDACTED]", body)
    body = re.sub(
        r'(?i)("(?:api[_-]?key|access[_-]?token|token|secret)"\s*:\s*)"[^"]*"',
        r'\1"[REDACTED]"',
        body,
    )
    return body[:2000] + ("…[truncated]" if len(body) > 2000 else "")


def _safe_response_excerpt(content: Any, api_key: str, limit: int = 600) -> str:
    text = str(content or "")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,\"']+",
        r"\1[REDACTED]",
        text,
    )
    return text[:limit] + ("…[truncated]" if len(text) > limit else "")


def _classification_response_schema() -> dict[str, Any]:
    schema = BlockBatchClassification.model_json_schema()
    definitions = schema.get("$defs") or {}
    subquestion_definition = definitions.pop("SubQuestionBlockRefs")

    def bounded_subquestion(levels_remaining: int) -> dict[str, Any]:
        item = copy.deepcopy(subquestion_definition)
        properties = item.get("properties") or {}
        nested = properties.get("sub_questions")
        if nested is not None:
            if levels_remaining <= 0:
                properties.pop("sub_questions", None)
            else:
                nested["maxItems"] = 8
                nested["items"] = bounded_subquestion(levels_remaining - 1)
        return item

    question_definition = definitions["QuestionBlockRefs"]
    question_children = question_definition["properties"]["sub_questions"]
    question_children["maxItems"] = 12
    question_children["items"] = bounded_subquestion(1)
    schema["properties"]["questions"]["maxItems"] = 1
    for name in ("sections", "instruction_block_ids", "other_block_ids", "ambiguous_block_ids"):
        schema["properties"][name]["maxItems"] = 64
    return schema


def _stage2_reasoning_effort() -> str:
    configured = str(settings.NVIDIA_STRUCTURE_REASONING_EFFORT or "").strip().casefold()
    return configured if configured in {"low", "medium", "high"} else "low"


def _stage2_generation_parameters(model: str, max_tokens: int, reasoning_effort: str) -> dict[str, Any]:
    if model == "nvidia/nemotron-3.5-super-vl-120b-a12b":
        # NVIDIA documents this model's reasoning switch in its chat template.
        return {
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    return {"max_tokens": max_tokens, "reasoning_effort": reasoning_effort}


class StructuringFailure(RuntimeError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        finish_reason: Any = None,
        response_shape: str = "unknown",
        content_present: bool = False,
        content_length: int = 0,
        content_type: str = "missing",
        token_usage: Any = None,
        validation_errors: list[str] | None = None,
        previous_response: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.finish_reason = finish_reason
        self.response_shape = response_shape
        self.content_present = content_present
        self.content_length = content_length
        self.content_type = content_type
        self.token_usage = token_usage
        self.validation_errors = validation_errors or []
        self.previous_response = previous_response


class QuestionStructuringError(RuntimeError):
    def __init__(self, message: str, *, failure_reason: str):
        super().__init__(message)
        self.failure_reason = failure_reason


class BlockReferenceValidationError(ValueError):
    """A response referenced IDs outside this request or omitted supplied blocks."""

    def __init__(
        self,
        message: str,
        *,
        invalid_blocks: list[str] | None = None,
        omitted_blocks: list[str] | None = None,
        invalid_assets: list[str] | None = None,
        partial_result: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.invalid_blocks = invalid_blocks or []
        self.omitted_blocks = omitted_blocks or []
        self.invalid_assets = invalid_assets or []
        self.partial_result = partial_result


_MARKS_ONLY_BLOCK = re.compile(
    r"^\s*\[\s*\d+(?:\.\d+)?(?:\s*[+x×*]\s*\d+(?:\.\d+)?)*\s*\]"
    r"(?:\s+(?:CO|BL)\s*\d+)*\s*$",
    re.IGNORECASE,
)


def _recover_omitted_marks_blocks(
    data: Any,
    blocks: list[dict[str, Any]],
    *,
    batch_id: str,
) -> list[tuple[str, str]]:
    """Attach extracted marks-only rows to already referenced subquestions."""
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return []
    known_ids = {str(block.get("block_id")) for block in blocks}
    referenced: set[str] = set()
    recipients: list[tuple[dict[str, Any], str]] = []

    def walk(record: Any, label: str) -> None:
        if not isinstance(record, dict):
            return
        for field in ("block_ids", "instruction_block_ids", "ambiguous_block_ids"):
            referenced.update(str(item) for item in record.get(field) or [] if item is not None)
        children = record.get("sub_questions") or []
        if children:
            for child in children:
                child_label = str(
                    child.get("question_number") or child.get("label") or label
                ) if isinstance(child, dict) else label
                walk(child, child_label)
        elif record.get("block_ids"):
            recipients.append((record, label))

    for question in data["questions"]:
        walk(question, str(question.get("question_number") or "question"))
    omitted = known_ids - referenced
    blocks_by_id = {str(block.get("block_id")): block for block in blocks}
    marks_blocks = [
        blocks_by_id[block_id]
        for block_id in omitted
        if _MARKS_ONLY_BLOCK.fullmatch(
            str(blocks_by_id[block_id].get("text") or blocks_by_id[block_id].get("original_text") or "")
        )
    ]
    if not omitted or len(marks_blocks) != len(omitted) or len(marks_blocks) != len(recipients):
        return []
    marks_blocks.sort(key=lambda block: int(block.get("reading_order") or 0))
    recipients.sort(key=lambda pair: min(
        [
            int(blocks_by_id[str(block_id)].get("reading_order") or 0)
            for block_id in pair[0].get("block_ids") or []
            if str(block_id) in blocks_by_id
        ],
        default=10**9,
    ))
    recovered: list[tuple[str, str]] = []
    for block, (record, owner) in zip(marks_blocks, recipients):
        block_id = str(block["block_id"])
        record.setdefault("block_ids", []).append(block_id)
        if record.get("marks") is None:
            mark = re.search(r"\[\s*([\d.+x×*]+)\s*\]", str(block.get("text") or ""), re.IGNORECASE)
            if mark:
                record["marks"] = f"[{mark.group(1)}]"
        recovered.append((block_id, owner))
        logger.info(
            "STAGE2_MARKS_BLOCK_REFERENCE_RECOVERED: batch=%s block_id=%s owner=%s",
            batch_id,
            block_id,
            owner,
        )
    return recovered


class SubQuestionBlockRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    question_number: str | None = None
    parent_label: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    marks: str | None = None
    priority: StrictInt | None = Field(default=None, ge=1, le=3)
    priority_evidence: str | None = None
    figure_asset_ids: list[str] = Field(default_factory=list)
    ambiguous_block_ids: list[str] = Field(default_factory=list)
    instruction_block_ids: list[str] = Field(default_factory=list)
    sub_questions: list[SubQuestionBlockRefs] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_priority_evidence(self):
        if self.priority is not None and not (self.priority_evidence or "").strip():
            raise ValueError("priority requires explicit priority_evidence")
        return self


class QuestionBlockRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_number: str
    block_ids: list[str] = Field(default_factory=list)
    sub_questions: list[SubQuestionBlockRefs] = Field(default_factory=list)
    marks: str | None = None
    priority: StrictInt | None = Field(default=None, ge=1, le=3)
    priority_evidence: str | None = None
    section_name: str | None = None
    instruction_block_ids: list[str] = Field(default_factory=list)
    figure_asset_ids: list[str] = Field(default_factory=list)
    ambiguous_block_ids: list[str] = Field(default_factory=list)
    ambiguity_note: str | None = None

    @model_validator(mode="after")
    def require_priority_evidence(self):
        if self.priority is not None and not (self.priority_evidence or "").strip():
            raise ValueError("priority requires explicit priority_evidence")
        return self


class SectionBlockRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_name: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    instruction_block_ids: list[str] = Field(default_factory=list)


class BlockBatchClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[QuestionBlockRefs] = Field(default_factory=list)
    sections: list[SectionBlockRefs] = Field(default_factory=list)
    instruction_block_ids: list[str] = Field(default_factory=list)
    other_block_ids: list[str] = Field(default_factory=list)
    ambiguous_block_ids: list[str] = Field(default_factory=list)


def _validation_error_details(exc: ValidationError) -> list[str]:
    details = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        details.append(
            f"{location}: {error['msg']} (type={error['type']})"
        )
    return details


def _token_usage_fields(usage: Any) -> tuple[Any, Any, Any]:
    if not isinstance(usage, dict):
        return None, None, None
    details = usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return reasoning, usage.get("completion_tokens"), usage.get("total_tokens")


def _apply_text_priority_evidence(data: dict[str, Any], blocks: list[dict[str, Any]]) -> None:
    block_map = {str(block.get("block_id")): block for block in blocks}

    def apply(record: dict[str, Any]) -> None:
        references = [
            *(record.get("block_ids") or []),
            *(record.get("instruction_block_ids") or []),
        ]
        text = "\n".join(
            str(block_map.get(str(block_id), {}).get("original_text")
                or block_map.get(str(block_id), {}).get("text") or "")
            for block_id in references
        )
        match = _EXPLICIT_PRIORITY.search(text)
        if match:
            record["priority"] = int(match.group(1))
            record["priority_evidence"] = match.group(0)
        for child in record.get("sub_questions") or []:
            if isinstance(child, dict):
                apply(child)

    for question in data.get("questions") or []:
        if isinstance(question, dict):
            apply(question)


def _recover_single_question_block_reference(
    data: Any,
    blocks: list[dict[str, Any]],
    *,
    batch_id: str,
) -> bool:
    """Bind a lone, explicitly numbered source block to its sole returned question.

    Some model responses omit ``block_ids`` for a one-block request even though
    they return a question. The association is unambiguous only when the exact
    request contained one explicit question block and the response has one
    top-level question with no other reference accounting for that block.
    """
    if not isinstance(data, dict) or len(blocks) != 1:
        return False
    questions = data.get("questions")
    if not isinstance(questions, list) or len(questions) != 1:
        return False
    block = blocks[0]
    block_id = str(block.get("block_id") or "")
    text = str(block.get("text") or block.get("original_text") or "")
    if not block_id or not _EXPLICIT_QUESTION_HEADING.match(text):
        return False
    question = questions[0]
    if not isinstance(question, dict) or question.get("block_ids"):
        return False
    referenced = {
        str(reference)
        for field in (
            "instruction_block_ids", "other_block_ids", "ambiguous_block_ids",
        )
        for reference in data.get(field) or []
    }
    if block_id in referenced:
        return False
    question["block_ids"] = [block_id]
    logger.warning(
        "LITE_STAGE2_SINGLE_QUESTION_REFERENCE_RECOVERED: batch=%s "
        "question_number=%s block_id=%s reason=sole_explicit_question_block",
        batch_id, question.get("question_number"), block_id,
    )
    return True


def _structured_response_schema() -> dict[str, Any]:
    question_schema = QuestionRecord.model_json_schema()
    definitions = question_schema.pop("$defs", {})
    return {
        "$defs": definitions,
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": question_schema,
                "minItems": 1,
                "maxItems": 1,
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


def _reference_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(item) for item in values if item is not None and str(item).strip()]


def _normalize_block_subquestions(
    items: Any,
    *,
    parent_label: str | None,
    parent_number: str | None,
    parent_instructions: list[str],
    parent_section: list[str | None],
    parent_ambiguity: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        allowed_input_fields = {
            "label", "subquestion_label", "part", "question_number", "number",
            "question_no", "q_number", "question", "id", "parent_label",
            "parent_question_number", "block_ids", "blocks", "refs", "references",
            "block_references", "marks", "figure_asset_ids", "figure_assets",
            "asset_ids", "assets", "ambiguous_block_ids", "ambiguous_blocks",
            "ambiguous_refs", "instruction_block_ids", "instruction_blocks",
            "instruction_refs", "section_name", "ambiguity_note", "sub_questions",
            "subquestions", "sub_parts", "parts", "priority", "priority_evidence",
        }
        unknown = sorted(set(item) - allowed_input_fields)
        if unknown:
            raise ValueError(
                "block classification schema validation failed: unsupported "
                f"sub-question fields: {', '.join(unknown)}"
            )
        number = (
            item.get("question_number") or item.get("number")
            or item.get("question_no") or item.get("q_number")
        )
        label = (
            item.get("label") or item.get("subquestion_label")
            or item.get("part") or number or item.get("question") or item.get("id")
        )
        if number is not None and parent_number:
            numbered_part = re.fullmatch(
                r"\s*(?:Q\s*\.?\s*)?(\d+)\s*[.(]\s*([a-zivxlcdm]+)\s*[).]?\s*",
                str(number), flags=re.IGNORECASE,
            )
            parent_number_match = re.search(r"\d+", parent_number)
            if numbered_part and parent_number_match and numbered_part.group(1) == parent_number_match.group(0):
                label = f"({numbered_part.group(2)})"
        explicit_parent = item.get("parent_label") or item.get("parent_question_number")
        own_instructions = (
            item.get("instruction_block_ids") or item.get("instruction_blocks")
            or item.get("instruction_refs") or []
        )
        if item.get("section_name") and not parent_section[0]:
            parent_section[0] = str(item["section_name"])
        block_ids = (
            item.get("block_ids") or item.get("blocks") or item.get("refs")
            or item.get("references") or item.get("block_references") or []
        )
        figures = (
            item.get("figure_asset_ids") or item.get("figure_assets")
            or item.get("asset_ids") or item.get("assets") or []
        )
        ambiguous = (
            item.get("ambiguous_block_ids") or item.get("ambiguous_blocks")
            or item.get("ambiguous_refs") or []
        )
        normalized_items.append({
            "label": str(label or "").strip(),
            "question_number": str(number).strip() if number is not None else None,
            "parent_label": str(explicit_parent or parent_label or "").strip() or None,
            "block_ids": _reference_list(block_ids),
            "marks": str(item["marks"]) if item.get("marks") is not None else None,
            "priority": item.get("priority"),
            "priority_evidence": item.get("priority_evidence"),
            "figure_asset_ids": _reference_list(figures),
            "ambiguous_block_ids": _reference_list(ambiguous),
            "instruction_block_ids": _reference_list(own_instructions),
        })
        if item.get("ambiguity_note"):
            parent_ambiguity.append(str(item["ambiguity_note"]))
        child_parent = str(label or "").strip() or parent_label
        normalized_items.extend(_normalize_block_subquestions(
            item.get("sub_questions") or item.get("subquestions")
            or item.get("sub_parts") or item.get("parts"),
            parent_label=child_parent,
            parent_number=child_parent,
            parent_instructions=parent_instructions,
            parent_section=parent_section,
            parent_ambiguity=parent_ambiguity,
        ))
    return normalized_items


def _normalize_block_question(question: Any) -> dict[str, Any]:
    if not isinstance(question, dict):
        return {}
    allowed_input_fields = {
        "question_number", "number", "question", "label", "id", "question_no",
        "q_number", "block_ids", "blocks", "refs", "references",
        "block_references", "block_ids_list", "sub_questions", "subquestions",
        "sub_parts", "parts", "instruction_block_ids", "instruction_blocks",
        "instruction_refs", "figure_asset_ids", "figure_assets", "asset_ids",
        "assets", "ambiguous_block_ids", "ambiguous_blocks", "ambiguous_refs",
        "marks", "section_name", "ambiguity_note",
        "priority", "priority_evidence",
    }
    unknown = sorted(set(question) - allowed_input_fields)
    if unknown:
        raise ValueError(
            "block classification schema validation failed: unsupported question "
            f"fields: {', '.join(unknown)}"
        )
    number = (
        question.get("question_number")
        or question.get("number")
        or question.get("question")
        or question.get("label")
        or question.get("id")
        or question.get("question_no")
        or question.get("q_number")
    )
    block_ids = (
        question.get("block_ids")
        or question.get("blocks")
        or question.get("refs")
        or question.get("references")
        or question.get("block_references")
        or question.get("block_ids_list")
        or []
    )
    sub_questions = (
        question.get("sub_questions")
        or question.get("subquestions")
        or question.get("sub_parts")
        or question.get("parts")
        or []
    )
    instruction_block_ids = (
        question.get("instruction_block_ids")
        or question.get("instruction_blocks")
        or question.get("instruction_refs")
        or []
    )
    figure_asset_ids = (
        question.get("figure_asset_ids")
        or question.get("figure_assets")
        or question.get("asset_ids")
        or question.get("assets")
        or []
    )
    ambiguous_block_ids = (
        question.get("ambiguous_block_ids")
        or question.get("ambiguous_blocks")
        or question.get("ambiguous_refs")
        or []
    )
    normalized_instructions = _reference_list(instruction_block_ids)
    normalized_section = [
        str(question.get("section_name")) if question.get("section_name") else None
    ]
    normalized_ambiguity = (
        [str(question["ambiguity_note"])] if question.get("ambiguity_note") else []
    )
    normalized = {
        "question_number": str(number).strip() if number is not None else "",
        "block_ids": _reference_list(block_ids),
        "sub_questions": _normalize_block_subquestions(
            sub_questions,
            parent_label=None,
            parent_number=str(number) if number is not None else None,
            parent_instructions=normalized_instructions,
            parent_section=normalized_section,
            parent_ambiguity=normalized_ambiguity,
        ),
        "marks": str(question["marks"]) if question.get("marks") is not None else None,
        "priority": question.get("priority"),
        "priority_evidence": question.get("priority_evidence"),
        "section_name": normalized_section[0],
        "instruction_block_ids": list(dict.fromkeys(normalized_instructions)),
        "figure_asset_ids": _reference_list(figure_asset_ids),
        "ambiguous_block_ids": _reference_list(ambiguous_block_ids),
        "ambiguity_note": "; ".join(dict.fromkeys(normalized_ambiguity)) or None,
    }
    return normalized


def _normalize_block_classification_payload(data: Any) -> Any:
    if isinstance(data, list):
        data = {"questions": data}
    if not isinstance(data, dict):
        return data
    questions = None
    for key in ("questions", "items", "results", "classified_questions",
                "question_references", "question_blocks", "references"):
        if isinstance(data.get(key), list):
            questions = data.get(key)
            break
    if questions is None:
        # Maybe nested under "data"
        nested = data.get("data")
        if isinstance(nested, dict):
            return _normalize_block_classification_payload(nested)
        if isinstance(nested, list):
            return {"questions": [_normalize_block_question(q) for q in nested]}
        return data
    sections = data.get("sections")
    if not isinstance(sections, list):
        sections = []
    normalized = {
        "questions": [_normalize_block_question(q) for q in questions],
        "sections": sections,
        "instruction_block_ids": data.get("instruction_block_ids") or data.get("instructions") or [],
        "other_block_ids": data.get("other_block_ids") or data.get("other_blocks") or [],
        "ambiguous_block_ids": data.get("ambiguous_block_ids") or data.get("ambiguous_blocks") or [],
    }
    # Preserve extra known fields on sections.
    normalized_sections = []
    for section in sections:
        if isinstance(section, dict):
            normalized_sections.append(section)
    normalized["sections"] = normalized_sections
    return normalized


def _strip_fences(text: Any) -> str:
    if not isinstance(text, str):
        raise ValueError(
            f"expected textual JSON content, got {type(text).__name__}"
        )
    text = text.strip()
    match = re.fullmatch(
        r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        text = match.group(1).strip()
    return text


def _response_shape(value: Any) -> str:
    if not isinstance(value, dict):
        return type(value).__name__
    fields = ",".join(sorted(str(key) for key in value))
    return f"object(fields={fields or 'none'})"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    raise ValueError(
        f"message content must be text; got {type(content).__name__}"
    )


def _extract_completion(payload: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise StructuringFailure(
            "response_format",
            f"expected JSON object from Chat Completions, got {type(payload).__name__}",
            response_shape=type(payload).__name__,
        )
    diagnostics: dict[str, Any] = {
        "response_shape": _response_shape(payload),
        "finish_reason": None,
        "content_present": False,
        "content_length": 0,
        "content_type": "missing",
        "token_usage": payload.get("usage"),
    }

    error = payload.get("error")
    if payload.get("object") == "error" or error is not None:
        if isinstance(error, dict):
            error_type = str(error.get("type") or "")
            error_message = str(error.get("message") or "NVIDIA returned an API error")
        else:
            error_type = str(payload.get("type") or "")
            error_message = str(
                payload.get("message") or "NVIDIA returned an API error"
            )
        retryable = any(
            marker in f"{error_type} {error_message}".casefold()
            for marker in ("rate limit", "rate_limit", "overloaded", "temporarily unavailable")
        )
        raise StructuringFailure(
            "api_error",
            f"NVIDIA returned an API error (type={error_type or 'unknown'})",
            retryable=retryable,
            response_shape=_response_shape(payload),
            token_usage=payload.get("usage"),
        )

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise StructuringFailure(
            "response_format",
            "Chat Completions response has no non-empty choices array",
            response_shape=_response_shape(payload),
            token_usage=payload.get("usage"),
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise StructuringFailure(
            "response_format",
            "Chat Completions first choice is not an object",
            response_shape=_response_shape(choice),
            token_usage=payload.get("usage"),
        )
    diagnostics["finish_reason"] = choice.get("finish_reason")
    message = choice.get("message")
    if not isinstance(message, dict):
        # Support the legacy text-completion field without treating arbitrary
        # response fields as generated content.
        if isinstance(choice.get("text"), str):
            content = choice["text"]
            diagnostics.update(content_present=bool(content.strip()),
                               content_length=len(content),
                               content_type="choice.text")
            return content, diagnostics
        raise StructuringFailure(
            "response_format",
            "Chat Completions choice has no message object",
            finish_reason=diagnostics["finish_reason"],
            response_shape=_response_shape(choice),
            token_usage=payload.get("usage"),
        )

    if message.get("refusal"):
        refusal_content = message.get("content")
        raise StructuringFailure(
            "refusal",
            "NVIDIA response contains a refusal and no usable Question Bank",
            finish_reason=diagnostics["finish_reason"],
            response_shape=_response_shape(message),
            content_present=bool(refusal_content),
            content_length=len(refusal_content) if isinstance(refusal_content, str) else 0,
            content_type=type(refusal_content).__name__,
            token_usage=payload.get("usage"),
        )
    if message.get("tool_calls"):
        tool_content = message.get("content")
        raise StructuringFailure(
            "tool_call",
            "NVIDIA returned tool calls instead of a Question Bank JSON response",
            finish_reason=diagnostics["finish_reason"],
            response_shape=_response_shape(message),
            content_present=bool(tool_content),
            content_length=len(tool_content) if isinstance(tool_content, str) else 0,
            content_type=type(tool_content).__name__,
            token_usage=payload.get("usage"),
        )

    parsed = message.get("parsed")
    if isinstance(parsed, dict):
        diagnostics.update(
            content_present=True,
            content_length=len(json.dumps(parsed, ensure_ascii=False)),
            content_type="message.parsed",
            token_usage=payload.get("usage"),
        )
        return parsed, diagnostics

    content = message.get("content")
    diagnostics["content_type"] = type(content).__name__
    if content is None:
        truncated = diagnostics["finish_reason"] == "length"
        raise StructuringFailure(
            "truncated_response" if truncated else "empty_response",
            "Chat Completions message.content is null",
            retryable=truncated,
            finish_reason=diagnostics["finish_reason"],
            response_shape=_response_shape(message),
            content_present=False,
            content_length=0,
            content_type="NoneType",
            token_usage=payload.get("usage"),
        )
    try:
        text = _text_from_content(content)
    except ValueError as exc:
        raise StructuringFailure(
            "response_format",
            str(exc),
            finish_reason=diagnostics["finish_reason"],
            response_shape=_response_shape(message),
            content_present=True,
            content_length=len(content) if isinstance(content, str) else 0,
            content_type=type(content).__name__,
            token_usage=payload.get("usage"),
        ) from exc
    diagnostics.update(
        content_present=bool(text.strip()),
        content_length=len(text),
        content_type=type(content).__name__,
    )
    if not text.strip():
        truncated = diagnostics["finish_reason"] == "length"
        raise StructuringFailure(
            "truncated_response" if truncated else "empty_response",
            "Chat Completions message.content is empty",
            retryable=truncated,
            finish_reason=diagnostics["finish_reason"],
            response_shape=_response_shape(message),
            content_present=False,
            content_length=0,
            content_type=type(content).__name__,
            token_usage=payload.get("usage"),
        )
    return text, diagnostics


def _api_key() -> str:
    return (
        settings.setting("NVIDIA_API", "")
        or settings.setting("NVIDIA_API_KEY", "")
        or (settings.NVIDIA_API_KEYS[0] if settings.NVIDIA_API_KEYS else "")
    )


def _stage2_model() -> str:
    return settings.NVIDIA_STRUCTURE_MODEL


def _stage2_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=max(1.0, min(settings.NVIDIA_STRUCTURE_CONNECT_TIMEOUT, 600.0)),
        read=max(1.0, min(settings.NVIDIA_STRUCTURE_READ_TIMEOUT, 600.0)),
        write=max(1.0, min(settings.NVIDIA_STRUCTURE_READ_TIMEOUT, 600.0)),
        pool=max(1.0, min(settings.NVIDIA_STRUCTURE_CONNECT_TIMEOUT, 600.0)),
    )


def _stage2_backoff(attempt: int) -> float:
    base = max(0.0, min(settings.NVIDIA_STRUCTURE_BACKOFF_BASE, 30.0))
    return min(base * (2 ** max(0, attempt - 1)), 30.0)


class StructureWithLLM:
    def _build_prompt(
        self,
        raw_response: dict[str, Any],
        normalized: dict[str, Any],
        source_pdf: str,
    ) -> str:
        schema = QuestionRecord.model_json_schema()
        return f"""Structure one identified exam question as one Question Bank question record.
Return only JSON: {{"questions":[<one record>]}}, matching this record schema:
{json.dumps(schema, ensure_ascii=False, separators=(",", ":"))}

Use only the supplied extraction; do not invent or omit source content. Preserve question and
sub-question wording/order, marks, section instructions, tables, equations and page references.
Include only supplied asset IDs associated with this question. Set source_pdf to
{json.dumps(source_pdf)} for the question and its subquestions. Return exactly one top-level
question, with no prose or reasoning.

Question extraction:
{json.dumps(raw_response, ensure_ascii=False, separators=(",", ":"))}

Associated figure assets:
{json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))}
"""

    def _bind_extracted_assets(
        self, data: dict[str, Any], figure_assets: list[dict[str, Any]]
    ) -> int:
        known_assets = {asset["asset_id"]: asset for asset in figure_assets}

        def bind_record(record: dict[str, Any]) -> int:
            linked = 0
            supplied = record.get("assets")
            if isinstance(supplied, list):
                references = []
                for reference in supplied:
                    if not isinstance(reference, dict):
                        continue
                    asset = known_assets.get(reference.get("asset_id"))
                    if asset is None:
                        continue
                    box = ", ".join(str(value) for value in asset["bbox"])
                    references.append({
                        **reference,
                        "asset_id": asset["asset_id"],
                        "asset_type": asset["asset_type"],
                        "path": asset["path"],
                        "source_page": asset["source_page"],
                        "description": f"{asset['description']}; normalized bbox=[{box}]",
                        "verified": True,
                    })
                record["assets"] = references
                linked += len(references)
            for child in record.get("sub_questions") or []:
                if isinstance(child, dict):
                    linked += bind_record(child)
            return linked

        return sum(
            bind_record(question)
            for question in data.get("questions", [])
            if isinstance(question, dict)
        )

    def classify_blocks(
        self,
        blocks: list[dict[str, Any]],
        figure_assets: list[dict[str, Any]],
        *,
        batch_id: str,
        page_images: dict[int, bytes] | None = None,
    ) -> dict[str, Any]:
        api_key = _api_key()
        if not api_key:
            raise RuntimeError(
                "NVIDIA structuring unavailable: configure NVIDIA_API "
                "(NVIDIA_API_KEY is supported for backward compatibility)."
            )
        endpoint = settings.NVIDIA_STRUCTURE_ENDPOINT
        model = _stage2_model()
        retries = max(1, min(settings.NVIDIA_STRUCTURE_RETRIES, 3))
        timeout = _stage2_timeout()
        max_tokens = max(1024, min(settings.NVIDIA_STRUCTURE_MAX_TOKENS, 131072))
        reasoning_effort = _stage2_reasoning_effort()
        model_blocks = [
            {
                key: block[key]
                for key in (
                    "block_id", "page_number", "reading_order", "layout_type",
                    "bbox", "text", "table_marks", "figure_asset_ids",
                    "source_block_id", "table_row",
                )
                if key in block
            }
            for block in blocks
        ]
        request_block_ids = [str(block["block_id"]) for block in blocks]
        if len(request_block_ids) != len(set(request_block_ids)):
            raise ValueError(f"Stage 2 request {batch_id} contains duplicate block IDs")
        if [str(block["block_id"]) for block in model_blocks] != request_block_ids:
            raise ValueError(f"Stage 2 request {batch_id} changed block IDs during serialization")
        allowed_assets = [
            str(asset["asset_id"]) for asset in figure_assets if asset.get("asset_id")
        ]
        prompt = (
            "These blocks contain at most one main question. Return exactly one top-level "
            "question when a question is present; never return a second main question. "
            "Classify these exam-paper blocks. Group all parts of that main question; "
            "keep question/subquestion numbers, marks, section, instruction and figure "
            "references, and explicit priority evidence. Numbered formats include 1., "
            "Q1, Q.1, and 1(a), including Markdown emphasis around the number. Recognize "
            "academic question wording such as Explain, Discuss, Differentiate, Compare, "
            "Describe, Define, Illustrate, Evaluate, Analyse, and How; these are question "
            "verbs even when the sentence has no question mark. Preserve marks such as "
            "[5 marks] and Marks 3. Assign each block to its most "
            "specific question, instruction, other, or ambiguous role. Never classify an "
            "Extract marks and priority independently from question wording into their "
            "existing marks, priority, and priority_evidence fields. Question block text "
            "must contain only the printed question wording; never include marks or "
            "priority metadata as question wording. Recognize marks forms such as "
            "[5 marks], (5 Marks), Marks: 5, and Marks 3; recognize priority 2 and "
            "priority order 3. Never infer either value when it is not explicit. "
            "Preserve question numbering, subquestions, original wording, punctuation, "
            "and compound questions. Do not remove ambiguous text or split questions. "
            "Never classify an explicitly numbered exam question as other or ambiguous; an empty questions "
            "array is valid only if these blocks contain no exam question. References must "
            "use supplied block_id or asset_id values; always populate block_ids for a "
            "returned question, including when this request contains only one block. "
            "Return only the JSON schema, without "
            "question text. Use page images and block bounding boxes for explicit visual "
            "priority marks only; do not infer priority from difficulty. Block references "
            "must be copied character-for-character from this exact allowlist; never invent, "
            "complete, or alter an ID. Every supplied block must be accounted for exactly "
            "once across question, instruction, other, ambiguous, or section references.\n\n"
            "Allowed block IDs (exact): " + json.dumps(request_block_ids, separators=(",", ":"))
            + "\nAllowed figure asset IDs (exact): "
            + json.dumps(allowed_assets, separators=(",", ":")) + "\n\n"
            "Blocks:\n"
            + json.dumps(model_blocks, ensure_ascii=False, separators=(",", ":"))
            + "\n\nFigure assets:\n"
            + json.dumps(figure_assets, ensure_ascii=False, separators=(",", ":"))
        )
        correction: str | None = None
        schema_correction_used = False
        reference_correction_attempts = 0
        failures: list[str] = []
        last_reference_error: BlockReferenceValidationError | None = None
        schema = _classification_response_schema()
        explicit_numbers = {
            match.group(1)
            for block in blocks
            if (match := _EXPLICIT_QUESTION_HEADING.match(
                str(block.get("text") or block.get("original_text") or "")
            ))
        }
        if len(explicit_numbers) > 1:
            raise ValueError(
                "Stage 2 request must contain one main question; found headings "
                + ", ".join(sorted(explicit_numbers))
            )
        for attempt in range(1, retries + 1):
            request_started = time.monotonic()
            try:
                # On reference errors, resend the same original question unit with a
                # compact text-only payload. This preserves every source block while
                # removing image bytes and optional bounding-box metadata that cannot
                # help the model repair an exact-ID typo.
                repair_request = reference_correction_attempts > 0
                attempt_blocks = model_blocks
                if repair_request:
                    compact_keys = {
                        "block_id", "page_number", "reading_order", "layout_type",
                        "text", "table_marks", "figure_asset_ids", "source_block_id",
                        "table_row", "table_row_count",
                    }
                    attempt_blocks = [
                        {key: value for key, value in block.items() if key in compact_keys}
                        for block in model_blocks
                    ]
                    attempt_assets = [
                        {key: asset.get(key) for key in ("asset_id", "asset_type", "source_page", "bbox")}
                        for asset in figure_assets
                    ]
                else:
                    attempt_assets = figure_assets
                attempt_prompt = (
                    prompt.replace(
                        json.dumps(model_blocks, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(attempt_blocks, ensure_ascii=False, separators=(",", ":")),
                        1,
                    )
                    if repair_request else prompt
                )
                if repair_request:
                    attempt_prompt = attempt_prompt.replace(
                        json.dumps(figure_assets, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(attempt_assets, ensure_ascii=False, separators=(",", ":")),
                        1,
                    )
                attempt_prompt_content: str | list[dict[str, Any]] = attempt_prompt
                if not repair_request and page_images:
                    attempt_parts: list[dict[str, Any]] = [{"type": "text", "text": attempt_prompt}]
                    included_pages: set[int] = set()
                    for block in blocks:
                        try:
                            page_number = int(block.get("page_number"))
                        except (TypeError, ValueError):
                            continue
                        image_bytes = page_images.get(page_number)
                        if image_bytes is None or page_number in included_pages:
                            continue
                        included_pages.add(page_number)
                        attempt_parts.append({
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")},
                        })
                    attempt_prompt_content = attempt_parts
                logger.info(
                    "LITE_STAGE2_BLOCK_BATCH_ATTEMPT: batch=%s model=%s "
                    "attempt=%d/%d blocks=%d max_tokens=%d reasoning_setting=%s "
                    "reference_repair=%s",
                    batch_id,
                    model,
                    attempt,
                    retries,
                    len(blocks),
                    max_tokens,
                    "thinking=disabled" if model == "nvidia/nemotron-3.5-super-vl-120b-a12b" else f"effort={reasoning_effort}",
                    repair_request,
                )
                logger.info("STAGE2_MODEL_REQUEST: model=%s", model)
                response = httpx.post(
                    endpoint,
                    headers={
                        "Authorization": "Bearer " + api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Return compact JSON references only. Do not provide "
                                    "reasoning or explanations. Never reproduce block text."
                                ),
                            },
                            {
                                "role": "user",
                                "content": (
                                    attempt_prompt_content + (f"\n\nCorrection:\n{correction}" if correction else "")
                                    if isinstance(attempt_prompt_content, str)
                                    else [
                                        *attempt_prompt_content,
                                        *([{"type": "text", "text": "Correction:\n" + correction}] if correction else []),
                                    ]
                                ),
                            },
                        ],
                        "temperature": 0,
                        **_stage2_generation_parameters(model, max_tokens, reasoning_effort),
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "block_classification",
                                "schema": schema,
                            },
                        },
                        "stream": False,
                    },
                    timeout=timeout,
                )
                request_duration = time.monotonic() - request_started
                logger.info(
                    "STAGE2_REQUEST_COMPLETE: batch=%s attempt=%d/%d status=%d "
                    "duration_seconds=%.3f",
                    batch_id, attempt, retries, response.status_code, request_duration,
                )
                if response.status_code != 200:
                    error_body = _safe_http_error_body(response, api_key)
                    logger.error(
                        "LITE_STAGE2_BLOCK_BATCH_HTTP_ERROR: batch=%s status=%d "
                        "provider_body=%s",
                        batch_id,
                        response.status_code,
                        error_body,
                    )
                    raise StructuringFailure(
                        "api_error",
                        f"NVIDIA block classification HTTP {response.status_code}: "
                        f"{error_body}",
                        retryable=response.status_code in (408, 425, 429)
                        or response.status_code >= 500,
                    )
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"NVIDIA block classification returned invalid response JSON "
                        f"(line={exc.lineno}, column={exc.colno})"
                    ) from exc
                content, diagnostics = _extract_completion(payload)
                if diagnostics["finish_reason"] == "length":
                    raise StructuringFailure(
                        "truncated_response",
                        "block classification response truncated at token limit",
                        retryable=True,
                        finish_reason=diagnostics["finish_reason"],
                        response_shape=diagnostics["response_shape"],
                        content_present=diagnostics["content_present"],
                        content_length=diagnostics["content_length"],
                        content_type=diagnostics["content_type"],
                        token_usage=diagnostics.get("token_usage"),
                        previous_response=(
                            content if isinstance(content, str)
                            else json.dumps(content, ensure_ascii=False)
                        ),
                    )
                data = (
                    json.loads(_strip_fences(content))
                    if isinstance(content, str)
                    else content
                )
                data = _normalize_block_classification_payload(data)
                if isinstance(data, dict):
                    _recover_omitted_marks_blocks(data, blocks, batch_id=batch_id)
                    _recover_single_question_block_reference(
                        data, blocks, batch_id=batch_id
                    )
                    _apply_text_priority_evidence(data, blocks)
                try:
                    validated = BlockBatchClassification.model_validate(data)
                except ValidationError as exc:
                    details = _validation_error_details(exc)
                    raise ValueError(
                        "block classification schema validation failed: "
                        + "; ".join(details)
                    ) from exc
                if len(validated.questions) > 1:
                    raise ValueError(
                        "Stage 2 response contains multiple main questions for a "
                        "single-question request"
                    )
                if explicit_numbers and validated.questions:
                    returned_number = re.search(
                        r"\d+", validated.questions[0].question_number
                    )
                    if returned_number and returned_number.group(0) not in explicit_numbers:
                        raise ValueError(
                            "Stage 2 response question number does not match this request"
                        )
                known_blocks = set(request_block_ids)
                known_assets = set(allowed_assets)
                referenced_blocks = set(validated.instruction_block_ids)
                referenced_blocks.update(validated.other_block_ids)
                referenced_blocks.update(validated.ambiguous_block_ids)
                for section in validated.sections:
                    referenced_blocks.update(section.block_ids)
                    referenced_blocks.update(section.instruction_block_ids)
                referenced_assets = set()
                for question in validated.questions:
                    referenced_blocks.update(question.block_ids)
                    referenced_blocks.update(question.instruction_block_ids)
                    referenced_blocks.update(question.ambiguous_block_ids)
                    referenced_assets.update(question.figure_asset_ids)
                    for subquestion in question.sub_questions:
                        referenced_blocks.update(subquestion.block_ids)
                        referenced_blocks.update(subquestion.instruction_block_ids)
                        referenced_blocks.update(subquestion.ambiguous_block_ids)
                        referenced_assets.update(subquestion.figure_asset_ids)
                invalid_blocks = sorted(referenced_blocks - known_blocks)
                invalid_assets = sorted(referenced_assets - known_assets)
                accounted_blocks = referenced_blocks.intersection(known_blocks)
                omitted_blocks = sorted(known_blocks - accounted_blocks)
                if invalid_blocks or invalid_assets or omitted_blocks:
                    logger.warning(
                        "LITE_STAGE2_BLOCK_BATCH_REFERENCE_VALIDATION_FAILED: batch=%s "
                        "allowed_block_ids=%s invalid_block_ids=%s omitted_block_ids=%s "
                        "allowed_asset_ids=%s invalid_asset_ids=%s",
                        batch_id,
                        request_block_ids,
                        invalid_blocks,
                        omitted_blocks,
                        allowed_assets,
                        invalid_assets,
                    )
                    details = []
                    if invalid_blocks or invalid_assets:
                        details.append(
                            "unknown references: " + ", ".join(invalid_blocks + invalid_assets)
                        )
                    if omitted_blocks:
                        details.append("omitted input block IDs: " + ", ".join(omitted_blocks))
                    raise BlockReferenceValidationError(
                        "block classification reference validation failed: " + "; ".join(details),
                        invalid_blocks=invalid_blocks,
                        omitted_blocks=omitted_blocks,
                        invalid_assets=invalid_assets,
                        partial_result=(
                            validated.model_dump(mode="json")
                            if omitted_blocks and not invalid_blocks and not invalid_assets
                            else None
                        ),
                    )
                if not validated.questions:
                    heading_blocks = [
                        str(block.get("text") or block.get("original_text") or "")
                        for block in blocks
                        if _EXPLICIT_QUESTION_HEADING.match(
                            str(block.get("text") or block.get("original_text") or "")
                        )
                    ]
                    if heading_blocks:
                        raise ValueError(
                            "block classification schema validation failed: returned "
                            f"questions=0 despite {len(heading_blocks)} explicit Q headings"
                        )
                    logger.info(
                        "LITE_STAGE2_BLOCK_BATCH_NO_QUESTIONS: batch=%s "
                        "instructions=%d other=%d ambiguous=%d",
                        batch_id,
                        len(validated.instruction_block_ids),
                        len(validated.other_block_ids),
                        len(validated.ambiguous_block_ids),
                    )
                logger.info(
                    "LITE_STAGE2_BLOCK_BATCH_SUCCESS: batch=%s attempt=%d/%d "
                    "questions=%d finish_reason=%s reasoning_tokens=%s "
                    "completion_tokens=%s total_tokens=%s token_usage=%s",
                    batch_id,
                    attempt,
                    retries,
                    len(validated.questions),
                    diagnostics["finish_reason"],
                    *_token_usage_fields(diagnostics.get("token_usage")),
                    diagnostics.get("token_usage"),
                )
                return validated.model_dump(mode="json")
            except (httpx.TimeoutException, httpx.TransportError, RuntimeError, ValueError) as exc:
                request_duration = time.monotonic() - request_started
                failures.append(
                    f"attempt {attempt}: {type(exc).__name__}: {exc} "
                    f"(finish_reason={getattr(exc, 'finish_reason', None)})"
                )
                is_truncated = "truncat" in str(exc).casefold() or (
                    isinstance(exc, StructuringFailure)
                    and exc.finish_reason == "length"
                ) or "finish_reason=length" in str(exc).casefold()
                schema_error = "validation failed" in str(exc).casefold()
                reference_error = isinstance(exc, BlockReferenceValidationError)
                if reference_error:
                    last_reference_error = exc
                retryable_status = (
                    exc.retryable
                    if isinstance(exc, StructuringFailure) and exc.kind == "api_error"
                    else any(
                        f"HTTP {status}" in str(exc)
                        for status in (408, 425, 429, 500, 502, 503, 504)
                    )
                )
                retry = attempt < retries and (
                    isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
                    or retryable_status
                    or is_truncated
                    or (
                        reference_error
                        and reference_correction_attempts < min(2, retries - 1)
                    )
                    or (
                        schema_error
                        and not reference_error
                        and not schema_correction_used
                        and not is_truncated
                    )
                )
                if is_truncated:
                    logger.warning(
                        "LITE_STAGE2_TRUNCATED_RESPONSE_EXCERPT: batch=%s attempt=%d "
                        "max_tokens=%d preview=%s",
                        batch_id,
                        attempt,
                        max_tokens,
                        _safe_response_excerpt(
                            getattr(exc, "previous_response", None), api_key
                        ),
                    )
                    if retry:
                        max_tokens = min(max_tokens * 2, 131072)
                        correction = (
                            "The previous response was truncated. Return only one compact "
                            "classification object for this same single main question. Use "
                            "only supplied block and asset IDs; do not repeat source text "
                            "or create nested subquestions beyond the supplied schema."
                        )
                if retry and reference_error:
                    reference_correction_attempts += 1
                    correction = (
                        "Reference repair required for this same complete logical question. "
                        "Use only IDs from the exact allowlists in the original request. "
                        "Returned block IDs that are invalid: "
                        + json.dumps(exc.invalid_blocks, ensure_ascii=False)
                        + ". Returned asset IDs that are invalid: "
                        + json.dumps(exc.invalid_assets, ensure_ascii=False)
                        + ". Supplied block IDs omitted from the response: "
                        + json.dumps(exc.omitted_blocks, ensure_ascii=False)
                        + ". Correct every typo by selecting the matching original ID, and "
                        "account for every supplied block exactly once. The next request "
                        "contains the original blocks for this question in compact text form; "
                        "do not omit source content or invent references."
                    )
                    logger.warning(
                        "LITE_STAGE2_BLOCK_BATCH_REFERENCE_REPAIR: batch=%s "
                        "next_attempt=%d invalid_block_ids=%s omitted_block_ids=%s "
                        "invalid_asset_ids=%s original_block_ids=%s compact_text_only=true",
                        batch_id,
                        attempt + 1,
                        exc.invalid_blocks,
                        exc.omitted_blocks,
                        exc.invalid_assets,
                        request_block_ids,
                    )
                elif retry and schema_error:
                    schema_correction_used = True
                    correction = (
                        "Correct the previous classification response for this specific "
                        "schema or coverage failure. Preserve question numbers, block and "
                        "figure references, marks, sections, instructions, and parent-child "
                        "relationships. Use the supplied recursive sub_questions schema; "
                        "retain question_number, instruction_block_ids, and priority on the "
                        "specific question or subquestion they belong to. Do not repeat question "
                        "text. Failure: "
                        f"{exc}"
                    )
                logger.warning(
                    "LITE_STAGE2_BLOCK_BATCH_RETRY: batch=%s attempt=%d/%d "
                    "error_type=%s finish_reason=%s response_shape=%s content_type=%s "
                    "reasoning_tokens=%s completion_tokens=%s total_tokens=%s "
                    "token_usage=%s duration_seconds=%.3f error=%s retry=%s",
                    batch_id,
                    attempt,
                    retries,
                    type(exc).__name__,
                    getattr(exc, "finish_reason", None),
                    getattr(exc, "response_shape", "unknown"),
                    getattr(exc, "content_type", "unknown"),
                    *_token_usage_fields(getattr(exc, "token_usage", None)),
                    getattr(exc, "token_usage", None),
                    request_duration,
                    exc,
                    retry,
                )
                if retry:
                    time.sleep(_stage2_backoff(attempt))
                else:
                    break
        if last_reference_error is not None:
            logger.warning(
                "LITE_STAGE2_BLOCK_REFERENCE_RECOVERY_REQUIRED: batch=%s omitted_block_ids=%s "
                "invalid_block_ids=%s invalid_asset_ids=%s",
                batch_id, last_reference_error.omitted_blocks,
                last_reference_error.invalid_blocks, last_reference_error.invalid_assets,
            )
            raise last_reference_error
        raise RuntimeError(
            f"NVIDIA block classification failed for batch {batch_id}: "
            + "; ".join(failures)
        )

    def structure(
        self,
        raw_response: dict[str, Any],
        normalized: dict[str, Any],
        source_pdf: str,
        *,
        question_number: str | None = None,
        total_questions: int | None = None,
    ) -> dict[str, Any]:
        api_key = _api_key()
        if not api_key:
            raise RuntimeError(
                "NVIDIA structuring unavailable: configure NVIDIA_API "
                "(NVIDIA_API_KEY is supported for backward compatibility)."
            )
        endpoint = settings.NVIDIA_STRUCTURE_ENDPOINT
        model = _stage2_model()
        retries = max(1, min(settings.NVIDIA_STRUCTURE_RETRIES, 3))
        timeout = _stage2_timeout()
        max_tokens = max(1024, min(settings.NVIDIA_STRUCTURE_MAX_TOKENS, 131072))
        reasoning_effort = _stage2_reasoning_effort()
        prompt = self._build_prompt(raw_response, normalized, source_pdf)
        failures: list[str] = []
        final_failure_reason = "structuring_error"
        correction_note: str | None = None
        question_number = question_number or str(
            (raw_response.get("question") or {}).get("original_question_number")
            or (raw_response.get("question") or {}).get("label")
            or "unknown"
        )
        question_pages = (raw_response.get("question") or {}).get("source_pages") or []
        question_page = ",".join(str(page) for page in question_pages) or "unknown"

        for attempt in range(1, retries + 1):
            logger.info(
                "LITE_STAGE2_NVIDIA_ATTEMPT: model=%s question=%s page=%s attempt=%d/%d "
                "max_tokens=%d reasoning_setting=%s structured_output=json_schema",
                model,
                question_number,
                question_page,
                attempt,
                retries,
                max_tokens,
                "thinking=disabled" if model == "nvidia/nemotron-3.5-super-vl-120b-a12b" else f"effort={reasoning_effort}",
            )
            diagnostics: dict[str, Any] = {
                "finish_reason": None,
                "response_shape": "unavailable",
                "content_present": False,
                "content_length": 0,
                "content_type": "unavailable",
            }
            request_started = time.monotonic()
            try:
                response = httpx.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Return one complete JSON object only. Do not use markdown. "
                                    "Do not omit extracted content."
                                ),
                            },
                            {
                                "role": "user",
                                "content": prompt
                                + (f"\n\n{correction_note}" if correction_note else ""),
                            },
                        ],
                        "temperature": 0,
                        **_stage2_generation_parameters(model, max_tokens, reasoning_effort),
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "question_bank_record",
                                "schema": _structured_response_schema(),
                            },
                        },
                        "stream": False,
                    },
                    timeout=timeout,
                )
                request_duration = time.monotonic() - request_started
                logger.info(
                    "LITE_STAGE2_NVIDIA_REQUEST_COMPLETE: model=%s question=%s "
                    "page=%s attempt=%d/%d status=%d response_type=%s duration_seconds=%.3f",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    retries,
                    response.status_code,
                    response.headers.get("content-type", "unknown"),
                    request_duration,
                )
                if response.status_code != 200:
                    error_body = _safe_http_error_body(response, api_key)
                    logger.error(
                        "LITE_STAGE2_NVIDIA_HTTP_ERROR: model=%s question=%s "
                        "status=%d provider_body=%s",
                        model,
                        question_number,
                        response.status_code,
                        error_body,
                    )
                    raise StructuringFailure(
                        "api_error",
                        f"NVIDIA structuring HTTP {response.status_code}: "
                        f"{error_body}",
                        retryable=response.status_code in (408, 425, 429)
                        or response.status_code >= 500,
                    )

                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise StructuringFailure(
                        "response_format",
                        f"NVIDIA returned a non-JSON HTTP 200 response "
                        f"(line={exc.lineno}, column={exc.colno})",
                    ) from exc

                content, diagnostics = _extract_completion(payload)
                logger.info(
                    "LITE_STAGE2_NVIDIA_RESPONSE: model=%s question=%s page=%s attempt=%d/%d "
                    "finish_reason=%s content_present=%s content_length=%d "
                    "content_type=%s response_shape=%s token_usage=%s",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    retries,
                    diagnostics["finish_reason"],
                    diagnostics["content_present"],
                    diagnostics["content_length"],
                    diagnostics["content_type"],
                    diagnostics["response_shape"],
                    diagnostics.get("token_usage"),
                )
                previous_response = (
                    content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False)
                )
                if diagnostics["finish_reason"] == "length":
                    raise StructuringFailure(
                        "truncated_response",
                        "NVIDIA stopped at the output limit; the response is incomplete",
                        retryable=True,
                        finish_reason=diagnostics["finish_reason"],
                        response_shape=diagnostics["response_shape"],
                        content_present=diagnostics["content_present"],
                        content_length=diagnostics["content_length"],
                        content_type=diagnostics["content_type"],
                        token_usage=diagnostics.get("token_usage"),
                        previous_response=previous_response,
                    )
                if isinstance(content, str):
                    try:
                        data = json.loads(_strip_fences(content))
                    except json.JSONDecodeError as exc:
                        logger.warning(
                            "LITE_STAGE2_NVIDIA_JSON_PARSE_FAILURE: model=%s "
                            "question=%s page=%s attempt=%d/%d finish_reason=%s "
                            "token_usage=%s line=%d column=%d",
                            model,
                            question_number,
                            question_page,
                            attempt,
                            retries,
                            diagnostics["finish_reason"],
                            diagnostics.get("token_usage"),
                            exc.lineno,
                            exc.colno,
                        )
                        raise StructuringFailure(
                            "truncated_response"
                            if diagnostics["finish_reason"] == "length"
                            else "json_decode",
                            f"generated content is not complete valid JSON "
                            f"(line={exc.lineno}, column={exc.colno})",
                            retryable=diagnostics["finish_reason"] == "length",
                            finish_reason=diagnostics["finish_reason"],
                            response_shape=diagnostics["response_shape"],
                            content_present=diagnostics["content_present"],
                            content_length=diagnostics["content_length"],
                            content_type=diagnostics["content_type"],
                            token_usage=diagnostics.get("token_usage"),
                            previous_response=content,
                        ) from exc
                    logger.info(
                        "LITE_STAGE2_NVIDIA_JSON_PARSE_SUCCESS: model=%s question=%s "
                        "page=%s attempt=%d/%d token_usage=%s",
                        model,
                        question_number,
                        question_page,
                        attempt,
                        retries,
                        diagnostics.get("token_usage"),
                    )
                else:
                    data = content
                if not isinstance(data, dict):
                    raise StructuringFailure(
                        "truncated_response"
                        if diagnostics["finish_reason"] == "length"
                        else "response_format",
                        "NVIDIA structuring response must be a JSON object",
                        retryable=diagnostics["finish_reason"] == "length",
                        finish_reason=diagnostics["finish_reason"],
                        response_shape=diagnostics["response_shape"],
                        token_usage=diagnostics.get("token_usage"),
                    )
                if not isinstance(data.get("questions"), list) or not data["questions"]:
                    raise StructuringFailure(
                        "schema_validation",
                        "NVIDIA response must contain a non-empty questions array",
                        retryable=True,
                        finish_reason=diagnostics["finish_reason"],
                        response_shape=diagnostics["response_shape"],
                        token_usage=diagnostics.get("token_usage"),
                        validation_errors=[
                            "questions: expected a non-empty array with exactly one record"
                        ],
                        previous_response=previous_response,
                    )
                if len(data["questions"]) != 1:
                    raise StructuringFailure(
                        "schema_validation",
                        "NVIDIA must return exactly one identified question per request",
                        retryable=True,
                        finish_reason=diagnostics["finish_reason"],
                        response_shape=diagnostics["response_shape"],
                        token_usage=diagnostics.get("token_usage"),
                        validation_errors=[
                            f"questions: expected exactly one record, got {len(data['questions'])}"
                        ],
                        previous_response=previous_response,
                    )

                metadata = data.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["providers_used"] = ["nvidia_nemotron_parse", "nvidia_nemotron_super_vl"]
                metadata["total_questions_found"] = len(data["questions"])
                metadata["total_pages_processed"] = len(raw_response.get("pages") or [])
                metadata["total_assets_linked"] = self._bind_extracted_assets(
                    data, normalized.get("figure_assets") or []
                )
                data["metadata"] = metadata
                data.setdefault("schema_version", SCHEMA_VERSION)
                for question in data["questions"]:
                    if isinstance(question, dict):
                        question.setdefault("source_pdf", source_pdf)
                        for subquestion in question.get("sub_questions") or []:
                            if isinstance(subquestion, dict):
                                subquestion.setdefault("source_pdf", source_pdf)
                try:
                    validated = QuestionBank.model_validate(data)
                except ValidationError as exc:
                    validation_errors = _validation_error_details(exc)
                    logger.error(
                        "LITE_STAGE2_NVIDIA_SCHEMA_VALIDATION_FAILURE: model=%s "
                        "question=%s page=%s attempt=%d/%d errors=%s",
                        model,
                        question_number,
                        question_page,
                        attempt,
                        retries,
                        validation_errors,
                    )
                    raise StructuringFailure(
                        "schema_validation",
                        "Question Bank response failed schema validation",
                        retryable=True,
                        finish_reason=diagnostics["finish_reason"],
                        response_shape=diagnostics["response_shape"],
                        content_present=diagnostics["content_present"],
                        content_length=diagnostics["content_length"],
                        content_type=diagnostics["content_type"],
                        token_usage=diagnostics.get("token_usage"),
                        validation_errors=validation_errors,
                        previous_response=previous_response,
                    ) from exc
                result = validated.model_dump(mode="json")
                logger.info(
                    "LITE_STAGE2_NVIDIA_SUCCESS: model=%s question=%s page=%s "
                    "attempt=%d/%d questions=%d total_questions=%s token_usage=%s",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    retries,
                    len(result["questions"]),
                    total_questions,
                    diagnostics.get("token_usage"),
                )
                return result
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                request_duration = time.monotonic() - request_started
                final_failure_reason = "api_error"
                failures.append(
                    f"attempt {attempt}: transient_api_error: {type(exc).__name__}"
                )
                logger.warning(
                    "LITE_STAGE2_NVIDIA_RETRY_DECISION: model=%s question=%s page=%s "
                    "attempt=%d/%d failure_kind=transport finish_reason=unavailable "
                    "token_usage=unavailable duration_seconds=%.3f error_type=%s retry=%s",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    retries,
                    request_duration,
                    type(exc).__name__,
                    attempt < retries,
                )
                if attempt < retries:
                    time.sleep(_stage2_backoff(attempt))
            except StructuringFailure as exc:
                request_duration = time.monotonic() - request_started
                final_failure_reason = {
                    "truncated_response": "token_limit",
                    "invalid_json": "invalid_json",
                    "json_decode": "invalid_json",
                    "api_error": "api_error",
                    "response_format": "response_format",
                    "empty_response": "empty_response",
                    "schema_validation": "schema_validation",
                    "refusal": "response_format",
                    "tool_call": "response_format",
                }.get(exc.kind, "structuring_error")
                failures.append(
                    f"attempt {attempt}: {exc.kind}: {exc} "
                    f"(finish_reason={exc.finish_reason}, response_shape={exc.response_shape}, "
                    f"validation_errors={exc.validation_errors})"
                )
                retry = (
                    exc.retryable
                    or exc.kind == "schema_validation"
                ) and attempt < retries
                logger.warning(
                    "LITE_STAGE2_NVIDIA_RETRY_DECISION: model=%s question=%s page=%s "
                    "attempt=%d/%d "
                    "failure_kind=%s finish_reason=%s content_present=%s "
                    "content_length=%s content_type=%s response_shape=%s "
                    "token_usage=%s duration_seconds=%.3f validation_errors=%s retry=%s",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    retries,
                    exc.kind,
                    exc.finish_reason,
                    exc.content_present,
                    exc.content_length,
                    exc.content_type,
                    exc.response_shape,
                    exc.token_usage,
                    request_duration,
                    exc.validation_errors,
                    retry,
                )
                if retry:
                    if exc.kind == "schema_validation":
                        correction_note = (
                            "Correct the previous JSON response so it validates against the "
                            "required Question Bank question schema. Keep every extracted "
                            "detail; do not omit fields to hide an error.\nValidation errors:\n"
                            + "\n".join(f"- {error}" for error in exc.validation_errors)
                            + "\nPrevious response:\n"
                            + (exc.previous_response or "")
                        )
                    elif exc.kind == "truncated_response" or exc.finish_reason == "length":
                        correction_note = (
                            "The previous response was truncated at the output limit. "
                            "Generate the complete JSON object, compactly, preserving all "
                            "question and sub-question details. Previous response:\n"
                            + (exc.previous_response or "")
                        )
                    time.sleep(_stage2_backoff(attempt))
                else:
                    logger.error(
                        "LITE_STAGE2_NVIDIA_FINAL_FAILURE: model=%s question=%s page=%s attempt=%d "
                        "failure_kind=%s finish_reason=%s content_present=%s "
                        "content_length=%s content_type=%s response_shape=%s token_usage=%s",
                        model,
                        question_number,
                        question_page,
                        attempt,
                        exc.kind,
                        exc.finish_reason,
                        exc.content_present,
                        exc.content_length,
                        exc.content_type,
                        exc.response_shape,
                        exc.token_usage,
                    )
                    break
            except Exception as exc:
                final_failure_reason = (
                    "api_error"
                    if isinstance(exc, httpx.RequestError)
                    else "schema_validation"
                )
                failures.append(
                    f"attempt {attempt}: {final_failure_reason}: {type(exc).__name__}"
                )
                logger.error(
                    "LITE_STAGE2_NVIDIA_FINAL_FAILURE: model=%s question=%s page=%s attempt=%d "
                    "failure_kind=%s finish_reason=%s "
                    "response_shape=%s token_usage=%s error_type=%s",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    final_failure_reason,
                    diagnostics["finish_reason"],
                    diagnostics["response_shape"],
                    diagnostics.get("token_usage"),
                    type(exc).__name__,
                )
                break

        raise QuestionStructuringError(
            f"NVIDIA Nemotron structuring failed after {len(failures)} attempts "
            f"(model={model}, timeout={timeout}s): {'; '.join(failures)}",
            failure_reason=final_failure_reason,
        )
