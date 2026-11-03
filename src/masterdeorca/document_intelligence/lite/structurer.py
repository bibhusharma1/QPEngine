from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import settings
from functions.QuesParser.ParsePipeLIne.schema import (
    SCHEMA_VERSION,
    QuestionBank,
    QuestionRecord,
)

logger = logging.getLogger(__name__)


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


class SubQuestionBlockRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    parent_label: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    marks: str | None = None
    figure_asset_ids: list[str] = Field(default_factory=list)
    ambiguous_block_ids: list[str] = Field(default_factory=list)


class QuestionBlockRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_number: str
    block_ids: list[str] = Field(default_factory=list)
    sub_questions: list[SubQuestionBlockRefs] = Field(default_factory=list)
    marks: str | None = None
    section_name: str | None = None
    instruction_block_ids: list[str] = Field(default_factory=list)
    figure_asset_ids: list[str] = Field(default_factory=list)
    ambiguous_block_ids: list[str] = Field(default_factory=list)
    ambiguity_note: str | None = None


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
    ) -> dict[str, Any]:
        api_key = _api_key()
        if not api_key:
            raise RuntimeError(
                "NVIDIA structuring unavailable: configure NVIDIA_API "
                "(NVIDIA_API_KEY is supported for backward compatibility)."
            )
        endpoint = settings.NVIDIA_STRUCTURE_ENDPOINT
        model = settings.NVIDIA_STRUCTURE_MODEL
        retries = max(1, min(settings.NVIDIA_STRUCTURE_RETRIES, 3))
        timeout = max(1.0, min(settings.NVIDIA_STRUCTURE_TIMEOUT, 600.0))
        max_tokens = max(1024, min(settings.NVIDIA_STRUCTURE_MAX_TOKENS, 131072))
        prompt = (
            "Classify the supplied extracted blocks into exam-paper structure. "
            "Return only the compact JSON object matching the provided schema. "
            "Do not repeat or rewrite any text. Every reference must be an input block_id "
            "or supplied asset_id. Group each main question with its subquestions; assign "
            "block IDs to the smallest applicable question/subquestion. Identify question "
            "number, section, instructions, marks, and associated figures. Keep reading "
            "order. Mark uncertain or incomplete assignments using ambiguous_block_ids "
            "and ambiguity_note. Do not invent details. Put non-question blocks in "
            "instruction_block_ids or other_block_ids. Nearby blocks are context; return "
            "only questions and block roles supported by this batch.\n\n"
            "Blocks:\n"
            + json.dumps(blocks, ensure_ascii=False, separators=(",", ":"))
            + "\n\nFigure assets:\n"
            + json.dumps(figure_assets, ensure_ascii=False, separators=(",", ":"))
        )
        correction: str | None = None
        failures: list[str] = []
        schema = BlockBatchClassification.model_json_schema()
        for attempt in range(1, retries + 1):
            try:
                logger.info(
                    "LITE_STAGE2_BLOCK_BATCH_ATTEMPT: batch=%s model=%s "
                    "attempt=%d/%d blocks=%d max_tokens=%d reasoning_effort=none",
                    batch_id,
                    model,
                    attempt,
                    retries,
                    len(blocks),
                    max_tokens,
                )
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
                                    "Return compact JSON references only. Never explain "
                                    "or reproduce block text."
                                ),
                            },
                            {
                                "role": "user",
                                "content": prompt
                                + (f"\n\nCorrection:\n{correction}" if correction else ""),
                            },
                        ],
                        "temperature": 0,
                        "max_tokens": max_tokens,
                        "reasoning_effort": "none",
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
                if response.status_code != 200:
                    raise RuntimeError(
                        f"NVIDIA block classification HTTP {response.status_code}"
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
                    raise ValueError(
                        "block classification response truncated at token limit"
                    )
                data = (
                    json.loads(_strip_fences(content))
                    if isinstance(content, str)
                    else content
                )
                try:
                    validated = BlockBatchClassification.model_validate(data)
                except ValidationError as exc:
                    details = _validation_error_details(exc)
                    raise ValueError(
                        "block classification schema validation failed: "
                        + "; ".join(details)
                    ) from exc
                known_blocks = {block["block_id"] for block in blocks}
                known_assets = {
                    str(asset["asset_id"])
                    for asset in figure_assets
                    if asset.get("asset_id")
                }
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
                        referenced_blocks.update(subquestion.ambiguous_block_ids)
                        referenced_assets.update(subquestion.figure_asset_ids)
                invalid_blocks = sorted(referenced_blocks - known_blocks)
                invalid_assets = sorted(referenced_assets - known_assets)
                if invalid_blocks or invalid_assets:
                    raise ValueError(
                        "classification referenced IDs outside this batch: "
                        f"blocks={invalid_blocks}, assets={invalid_assets}"
                    )
                logger.info(
                    "LITE_STAGE2_BLOCK_BATCH_SUCCESS: batch=%s attempt=%d/%d "
                    "questions=%d finish_reason=%s token_usage=%s",
                    batch_id,
                    attempt,
                    retries,
                    len(validated.questions),
                    diagnostics["finish_reason"],
                    diagnostics.get("token_usage"),
                )
                return validated.model_dump(mode="json")
            except (httpx.TimeoutException, httpx.TransportError, RuntimeError, ValueError) as exc:
                failures.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                correction = (
                    "Previous batch response failed validation. Return only corrected "
                    "block references; do not generate or repeat question text. Failure: "
                    f"{exc}"
                )
                logger.warning(
                    "LITE_STAGE2_BLOCK_BATCH_RETRY: batch=%s attempt=%d/%d "
                    "error_type=%s error=%s retry=%s",
                    batch_id,
                    attempt,
                    retries,
                    type(exc).__name__,
                    exc,
                    attempt < retries,
                )
                if attempt < retries:
                    time.sleep(
                        max(0.0, min(settings.NVIDIA_STRUCTURE_BACKOFF_BASE, 30.0))
                        * attempt
                    )
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
        model = settings.NVIDIA_STRUCTURE_MODEL
        retries = max(1, min(settings.NVIDIA_STRUCTURE_RETRIES, 3))
        timeout = max(1.0, min(settings.NVIDIA_STRUCTURE_TIMEOUT, 600.0))
        max_tokens = max(1024, min(settings.NVIDIA_STRUCTURE_MAX_TOKENS, 131072))
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
                "max_tokens=%d reasoning_effort=none structured_output=json_schema",
                model,
                question_number,
                question_page,
                attempt,
                retries,
                max_tokens,
            )
            diagnostics: dict[str, Any] = {
                "finish_reason": None,
                "response_shape": "unavailable",
                "content_present": False,
                "content_length": 0,
                "content_type": "unavailable",
            }
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
                        "max_tokens": max_tokens,
                        "reasoning_effort": "none",
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
                logger.info(
                    "LITE_STAGE2_NVIDIA_REQUEST_COMPLETE: model=%s question=%s "
                    "page=%s attempt=%d/%d status=%d response_type=%s",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    retries,
                    response.status_code,
                    response.headers.get("content-type", "unknown"),
                )
                if response.status_code != 200:
                    raise StructuringFailure(
                        "api_error",
                        f"NVIDIA structuring HTTP {response.status_code}",
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
                final_failure_reason = "api_error"
                failures.append(
                    f"attempt {attempt}: transient_api_error: {type(exc).__name__}"
                )
                logger.warning(
                    "LITE_STAGE2_NVIDIA_RETRY_DECISION: model=%s question=%s page=%s "
                    "attempt=%d/%d failure_kind=transport finish_reason=unavailable "
                    "token_usage=unavailable error_type=%s retry=%s",
                    model,
                    question_number,
                    question_page,
                    attempt,
                    retries,
                    type(exc).__name__,
                    attempt < retries,
                )
                if attempt < retries:
                    delay = max(0.0, min(settings.NVIDIA_STRUCTURE_BACKOFF_BASE, 30.0))
                    time.sleep(delay * attempt)
            except StructuringFailure as exc:
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
                    "token_usage=%s validation_errors=%s retry=%s",
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
                    delay = max(0.0, min(settings.NVIDIA_STRUCTURE_BACKOFF_BASE, 30.0))
                    time.sleep(delay * attempt)
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
