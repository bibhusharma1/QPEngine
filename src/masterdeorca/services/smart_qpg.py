"""Syllabus and AI generated draft question persistence for Smart QPG."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import random
import re
import threading
import tempfile
import time
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import settings
from src.masterdeorca import database

logger = logging.getLogger(__name__)
_job_locks: dict[str, threading.Lock] = {}
_job_locks_guard = threading.Lock()
_syllabus_locks: dict[str, threading.Lock] = {}
_syllabus_locks_guard = threading.Lock()
_SYLLABUS_PROCESSING_MAX_ATTEMPTS = 3


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyllabusConflictError(ValueError):
    pass


def _safe_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_syllabus_pages(filename: str, content: bytes):
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("Syllabus files must be PDF or DOCX.")
    with tempfile.TemporaryDirectory(prefix="smart-qpg-syllabus-") as temporary:
        source = Path(temporary) / ("syllabus" + suffix)
        source.write_bytes(content)
        from src.masterdeorca.document_intelligence.page_images import render_page_images
        pages = list(render_page_images(source))
    if not pages:
        raise ValueError("The syllabus has no pages to process.")
    return pages


_MODULE_RE = re.compile(
    r"(?i)(?<![a-z0-9])((?:module|unit|chapter)\s*[-–—:]?\s*(?:[ivxlcdm]+|\d+|[a-z]))"
    r"\s*:?[ \t]*(?:\(\s*(\d+)\s*hours?\s*\))?"
)
_HEADING_RE = re.compile(r"(?m)^\s*([A-Z][A-Z0-9 &,()/.-]{2,90})\s*$")


def _approx_tokens(text: str) -> int:
    """Conservative character-based estimate; this is not a model tokenizer."""
    return max(1, (len(text) + 2) // 3)


def _canonical_source_phrase(source: str, candidate: str) -> str | None:
    """Return the source's exact spelling when candidate tokens occur contiguously."""
    source_tokens = list(re.finditer(r"[A-Za-z0-9]+", source))
    wanted = [token.casefold() for token in re.findall(r"[A-Za-z0-9]+", candidate)]
    if not wanted:
        return None
    values = [token.group().casefold() for token in source_tokens]
    for start in range(len(values) - len(wanted) + 1):
        if values[start:start + len(wanted)] == wanted:
            return source[source_tokens[start].start():source_tokens[start + len(wanted) - 1].end()]
    return None


def _source_units(text: str) -> tuple[str, list[dict], str]:
    """Split source into a preamble, ordered module spans, and trailing material."""
    matches = list(_MODULE_RE.finditer(text))
    if not matches:
        return "", [{"module_name": "", "hours": None, "text": text}], ""
    preamble = text[:matches[0].start()].strip()
    modules: list[dict] = []
    tail_start = len(text)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start():end].strip()
        following = text[match.end():match.end() + 120]
        hours_match = re.search(r"\(\s*(\d+)\s*hours?\s*\)", following, re.I)
        if index + 1 == len(matches):
            # Course-level headings after the final module are not module content.
            tail_match = re.search(
                r"(?i)\b(?:course\s+outcomes?|outcomes?|books?|references?|digital learning resources?)\s*:?",
                body,
            )
            if tail_match:
                tail_start = match.start() + tail_match.start()
                body = text[match.start():tail_start].strip()
        modules.append({
            "module_name": match.group(1).rstrip(": "),
            "hours": int(match.group(2) or hours_match.group(1)) if match.group(2) or hours_match else None,
            "text": body,
        })
    trailing = text[tail_start:].strip() if tail_start < len(text) else ""
    return preamble, modules, trailing


def _split_module_chunks(module: dict, token_budget: int) -> list[dict]:
    text = str(module.get("text") or "").strip()
    if _approx_tokens(text) <= token_budget:
        return [{**module, "heading_context": "", "text": text}]
    # Split oversized modules at visible uppercase heading markers when possible.
    # Each resulting piece carries its heading label into every model request.
    inline_heading_re = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9 &,()/.-]{2,80}?)\s*[–—-]\s*(?=[A-Z])")
    heading_matches = list(inline_heading_re.finditer(text))
    blocks: list[tuple[str, str]] = []
    if heading_matches:
        if heading_matches[0].start() > 0:
            blocks.append((text[:heading_matches[0].start()].strip(), ""))
        for index, match in enumerate(heading_matches):
            end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(text)
            blocks.append((text[match.start():end].strip(), match.group(1).strip()))
    else:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        if len(paragraphs) == 1:
            paragraphs = [part.strip() for part in re.split(r"(?<=[.;:])\s+(?=(?:[-–—]\s*)?[A-Z])", text) if part.strip()]
        blocks = [(part, "") for part in paragraphs]
    chunks: list[dict] = []
    current: list[str] = []
    active_heading = ""
    chunk_heading = ""

    def emit() -> None:
        nonlocal current, chunk_heading
        if current:
            chunks.append({**module, "heading_context": chunk_heading or module.get("module_name") or "Unlabeled module preface",
                           "text": "\n".join(current).strip()})
            current = []
            chunk_heading = active_heading

    for block, heading_context in blocks:
        if heading_context:
            active_heading = heading_context
        heading = _HEADING_RE.match(block)
        if heading:
            active_heading = heading.group(1).strip()
        context = f"Module: {module.get('module_name') or 'unspecified'}\nHeading context: {active_heading}\n"
        prospective = "\n".join(current + [block])
        if current and _approx_tokens(context + prospective) > token_budget:
            emit()
            context = f"Module: {module.get('module_name') or 'unspecified'}\nHeading context: {active_heading}\n"
        if _approx_tokens(context + block) > token_budget:
            words = block.split()
            piece: list[str] = []
            for word in words:
                candidate = " ".join(piece + [word])
                if piece and _approx_tokens(context + candidate) > token_budget:
                    emit()
                    chunks.append({**module, "heading_context": active_heading or module.get("module_name") or "Unlabeled module preface",
                                   "text": " ".join(piece)})
                    piece = [word]
                else:
                    piece.append(word)
            if piece:
                current = [" ".join(piece)]
                chunk_heading = active_heading
        else:
            current.append(block)
            if len(current) == 1:
                chunk_heading = active_heading
    emit()
    return chunks or [{**module, "heading_context": module.get("module_name") or "Unlabeled module preface", "text": text}]


class SyllabusSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject: str
    objectives: list[str] = Field(default_factory=list)
    learning_outcomes: list[str] = Field(default_factory=list)
    modules: list["SyllabusModule"] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    digital_learning_resources: list["SyllabusResource"] = Field(default_factory=list)
    assessment_information: list[str] = Field(default_factory=list)
    question_generation_guidance: "QuestionGenerationGuidance" = Field(default_factory=lambda: QuestionGenerationGuidance())


class SyllabusResource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    course_name: str = ""
    course_link: str = ""
    instructor: str = ""
    details: list[str] = Field(default_factory=list)


class SyllabusSection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    heading: str
    topics: list[str] = Field(default_factory=list)
    topic_details: list[dict] = Field(default_factory=list)


class SyllabusModule(BaseModel):
    model_config = ConfigDict(extra="ignore")
    module_name: str
    hours: int | None = None
    sections: list[SyllabusSection] = Field(default_factory=list)


class TopicQuestionGuidance(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topic: str
    importance: str = "AI-inferred candidate"
    topic_categories: list[Literal[
        "definition-based", "conceptual", "descriptive", "comparison-based",
        "algorithmic", "numerical", "problem-solving", "application-based",
    ]] = Field(default_factory=list)
    question_formats: list[str] = Field(default_factory=list)
    suggested_marks: list[int] = Field(default_factory=list)
    difficulty_levels: list[str] = Field(default_factory=list)
    importance_rationale: str = ""


class SectionQuestionGuidance(BaseModel):
    model_config = ConfigDict(extra="ignore")
    module_name: str
    heading: str
    topic_guidance: list[TopicQuestionGuidance] = Field(default_factory=list)
    coverage_suggestions: list[str] = Field(default_factory=list)


class QuestionGenerationGuidance(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sections: list[SectionQuestionGuidance] = Field(default_factory=list)
    marks_are_ai_suggestions: bool = True


SyllabusSummary.model_rebuild()


def _nvidia_client():
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("The OpenAI SDK is required for Smart QPG model calls.") from exc
    key = settings.setting("NVIDIA_API_KEY", "") or settings.setting("NVIDIA_API", "")
    if not key:
        raise RuntimeError("NVIDIA_API_KEY is not configured.")
    # Retries are handled below so attempt counts, backoff, and errors remain visible.
    return AsyncOpenAI(api_key=key, base_url=settings.QUESTION_GENERATION_BASE_URL,
                       timeout=settings.QUESTION_GENERATION_TIMEOUT, max_retries=0)


def _generation_event(event: str, **fields) -> None:
    """Emit structured diagnostics without prompt or credential contents."""
    logger.info("smart_qpg %s", _safe_json({"event": event, "timestamp": now(), **fields}))


def _model_event(event: str, *, job_id: str | None, item_id: str | None, **fields) -> None:
    if job_id is not None or item_id is not None:
        _generation_event(event, job_id=job_id, item_id=item_id, **fields)


class _InvalidModelOutput(ValueError):
    pass


class _GenerationRequestError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool, status_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


def _generation_error_detail(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                          for error in exc.errors(include_input=False, include_context=False))[:500]
    if isinstance(exc, _InvalidModelOutput):
        return str(exc)[:300]
    if isinstance(exc, _GenerationRequestError):
        return str(exc)[:400]
    return f"{type(exc).__name__} (status={getattr(exc, 'status_code', None)})"


def _request_failure(exc: Exception) -> _GenerationRequestError:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    try:
        retry_after = min(8.0, max(0.0, float(headers.get("retry-after"))))
    except (TypeError, ValueError):
        retry_after = None
    name = type(exc).__name__
    if status == 429:
        category, retryable = "rate_limit", True
    elif status in {408, 504} or name in {"APITimeoutError", "TimeoutError", "ReadTimeout", "WriteTimeout", "PoolTimeout"}:
        category, retryable = "timeout", True
    elif name in {"APIConnectionError", "ConnectError", "ConnectionError", "ConnectionResetError"}:
        category, retryable = "network_error", True
    elif isinstance(status, int) and status >= 500:
        category, retryable = "server_error", True
    elif status is not None and 400 <= status < 500:
        category, retryable = "permanent_request_error", False
    else:
        category, retryable = "request_error", False
    return _GenerationRequestError(category, f"{name}: {str(exc)[:350]}", retryable=retryable,
                                   status_code=status, retry_after=retry_after)


def _extract_json_object(content: str) -> dict:
    content = str(content or "").strip()
    if not content:
        raise ValueError("NVIDIA returned empty content")
    content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content, flags=re.I)
    decoder = json.JSONDecoder()
    for index, char in enumerate(content):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(content[index:])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    raise ValueError("NVIDIA response did not contain a valid JSON object")


async def _chat(client, system: str, payload: dict, *, max_tokens: int | None = None,
                model: str | None = None, temperature: float | None = None,
                enable_thinking: bool | None = None, job_id: str | None = None,
                item_id: str | None = None, diagnostics: dict | None = None) -> dict:
    attempt = 1
    while True:
        request_attempt = int((diagnostics or {}).get("generation_attempt", 1))
        started_at, started = now(), time.perf_counter()
        parse_started = None
        response_duration = None
        if diagnostics is not None:
            diagnostics["http_attempts"] = diagnostics.get("http_attempts", 0) + 1
        _model_event("model_request_started", job_id=job_id, item_id=item_id,
                     attempt=request_attempt, timestamp=started_at, model=model or settings.QUESTION_GENERATION_MODEL)
        try:
            response = await client.chat.completions.create(
                model=model or settings.QUESTION_GENERATION_MODEL,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": _safe_json(payload)}],
                temperature=(settings.QUESTION_GENERATION_TEMPERATURE if temperature is None else temperature),
                top_p=settings.QUESTION_GENERATION_TOP_P,
                max_tokens=max_tokens or settings.QUESTION_GENERATION_MAX_TOKENS,
                response_format={"type": "json_object"},
                extra_body={"chat_template_kwargs": {"enable_thinking": (
                    settings.QUESTION_GENERATION_ENABLE_THINKING if enable_thinking is None else enable_thinking)}},
            )
            response_duration = time.perf_counter() - started
            parse_started = time.perf_counter()
            usage = getattr(response, "usage", None)
            if diagnostics is not None and usage is not None:
                diagnostics["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                diagnostics["completion_tokens"] = getattr(usage, "completion_tokens", None)
                diagnostics["total_tokens"] = getattr(usage, "total_tokens", None)
            choices = getattr(response, "choices", None) or []
            if not choices:
                raise _InvalidModelOutput("NVIDIA response contained no choices")
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None) if message else None
            if isinstance(content, list):
                content = "\n".join(str(getattr(part, "text", "") or "") for part in content)
            if finish_reason == "length":
                raise _InvalidModelOutput("NVIDIA response was truncated (finish_reason=length)")
            try:
                parsed = _extract_json_object(content)
            except ValueError as exc:
                raise _InvalidModelOutput(str(exc)) from exc
            parse_duration = time.perf_counter() - parse_started
            if diagnostics is not None:
                diagnostics["response_duration_seconds"] = response_duration
                diagnostics["response_parse_duration_seconds"] = parse_duration
            _model_event("model_response_parsed", job_id=job_id, item_id=item_id, attempt=request_attempt,
                         timestamp=now(), http_attempts=diagnostics.get("http_attempts") if diagnostics else attempt,
                         response_duration_seconds=round(response_duration, 3),
                         parse_validation_duration_seconds=round(parse_duration, 3))
            return parsed
        except _InvalidModelOutput as exc:
            duration = time.perf_counter() - started
            if diagnostics is not None:
                diagnostics["response_duration_seconds"] = response_duration if response_duration is not None else duration
                if parse_started is not None:
                    diagnostics["response_parse_duration_seconds"] = time.perf_counter() - parse_started
            _model_event("model_output_invalid", job_id=job_id, item_id=item_id, attempt=request_attempt,
                         http_attempts=diagnostics.get("http_attempts") if diagnostics else attempt,
                         response_duration_seconds=round(response_duration if response_duration is not None else duration, 3),
                         parse_duration_seconds=round(time.perf_counter() - parse_started, 3) if parse_started else None,
                         reason=str(exc)[:300])
            raise
        except Exception as exc:
            failure = _request_failure(exc)
            duration = time.perf_counter() - started
            _model_event("model_request_failed", job_id=job_id, item_id=item_id, attempt=request_attempt,
                             http_attempts=diagnostics.get("http_attempts") if diagnostics else attempt,
                             duration_seconds=round(duration, 3), status_code=failure.status_code,
                             exception=type(exc).__name__, category=failure.category, retryable=failure.retryable)
            raise failure from exc


def _clean_extracted_text(text: str) -> str:
    """Repair line-wrap artifacts without merging likely headings or list items."""
    text = re.sub(r"<x_[-\d.]+><y_[-\d.]+>", "", str(text or ""))
    text = re.sub(r"<class_[^>]+>", "", text)
    text = text.replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=[a-z])", "", text)
    # Repair only known parse artifacts observed in this syllabus family.
    text = re.sub(r"\b(learning)(from)\b", r"\1 \2", text, flags=re.I)
    text = re.sub(r"\b(Representing)(and)\b", r"\1 \2", text, flags=re.I)
    text = re.sub(r"\b(Formal)(Learning)\b", r"\1 \2", text, flags=re.I)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    output: list[str] = []
    heading_re = re.compile(r"^(?:module|unit|chapter|section)\s+[\w.-]+\b", re.I)
    list_re = re.compile(r"^(?:[-*•]|\d+[.)]|[a-z][.)])\s+")
    def heading_like(line: str) -> bool:
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", line)
        uppercase = line.upper() == line and any(ch.isalpha() for ch in line)
        title = bool(words) and len(words) <= 9 and len(line) < 105 and all(word[0].isupper() for word in words)
        return bool(heading_re.match(line) or uppercase or list_re.match(line)
                    or title)
    for line in lines:
        if not line:
            if output and output[-1] != "":
                output.append("")
            continue
        if not output or output[-1] == "" or heading_like(line) or heading_like(output[-1]):
            output.append(line)
        elif output[-1].endswith((".", ";", ":", "?", "!")):
            output.append(line)
        else:
            output[-1] = f"{output[-1]} {line}"
    return "\n".join(output).strip()


def _readable_parse_text(value) -> str:
    """Resolve common Nemotron Parse response containers into readable text."""
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(decoded, (dict, list)):
                    return _readable_parse_text(decoded)
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_readable_parse_text(item) for item in value) if part)
    if isinstance(value, dict):
        for key in ("text", "content", "markdown", "page_content"):
            candidate = value.get(key)
            if isinstance(candidate, (str, list, dict)) and candidate:
                return _readable_parse_text(candidate)
        for key in ("blocks", "elements", "lines", "paragraphs", "tables"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                extracted = _readable_parse_text(candidate)
                if extracted:
                    return extracted
    return ""


def _normalize_structure_payload(value: dict, subject_name: str) -> dict:
    """Map small naming/type variations onto the one canonical syllabus schema."""
    if not isinstance(value, dict):
        raise ValueError("Nemotron structure response must be a JSON object")
    subject = subject_name or value.get("subject") or value.get("subject_name") or ""
    raw_modules = value.get("modules") or value.get("units") or []
    modules = []
    for raw_module in raw_modules if isinstance(raw_modules, list) else []:
        if isinstance(raw_module, str):
            raw_module = {"module_name": raw_module}
        if not isinstance(raw_module, dict):
            continue
        name = raw_module.get("module_name") or raw_module.get("module") or raw_module.get("name") or raw_module.get("unit")
        if not name:
            continue
        hours = raw_module.get("hours", raw_module.get("module_hours"))
        if isinstance(hours, str):
            match = re.search(r"\d+", hours)
            hours = int(match.group()) if match else None
        raw_sections = raw_module.get("sections") or raw_module.get("headings") or raw_module.get("main_sections") or []
        if not raw_sections and isinstance(raw_module.get("topics"), list):
            raw_sections = [{"heading": name, "topics": raw_module["topics"]}]
        sections = []
        for raw_section in raw_sections if isinstance(raw_sections, list) else []:
            if isinstance(raw_section, str):
                raw_section = {"heading": raw_section}
            if not isinstance(raw_section, dict):
                continue
            heading = raw_section.get("heading") or raw_section.get("section") or raw_section.get("title") or raw_section.get("name")
            if not heading:
                continue
            raw_topics = raw_section.get("topics") or raw_section.get("subtopics") or []
            topics = []
            topic_details = []
            raw_details = raw_section.get("topic_details") or []
            details_by_name = {
                str(detail.get("topic") or detail.get("name") or "").strip().casefold(): detail
                for detail in raw_details if isinstance(detail, dict)
            } if isinstance(raw_details, list) else {}
            for topic in raw_topics if isinstance(raw_topics, list) else []:
                if isinstance(topic, str) and topic.strip():
                    label = topic.strip()
                    topics.append(label)
                    detail = details_by_name.get(label.casefold(), {})
                    topic_details.append({**detail, "topic": label})
                elif isinstance(topic, dict):
                    label = topic.get("topic") or topic.get("name") or topic.get("title")
                    if isinstance(label, str) and label.strip():
                        label = label.strip()
                        topics.append(label)
                        topic_details.append({**topic, "topic": label})
            sections.append({"heading": str(heading).strip(), "topics": topics,
                             "topic_details": topic_details})
        modules.append({"module_name": str(name).strip(), "hours": hours, "sections": sections})
    def text_list(*keys: str) -> list[str]:
        candidate = next((value.get(key) for key in keys if value.get(key) is not None), [])
        if isinstance(candidate, str):
            candidate = [candidate]
        return [item.strip() for item in candidate if isinstance(item, str) and item.strip()] if isinstance(candidate, list) else []

    raw_resources = value.get("digital_learning_resources") or value.get("resources") or []
    resources = []
    for resource in raw_resources if isinstance(raw_resources, list) else []:
        if isinstance(resource, str):
            resources.append({"course_name": resource})
        elif isinstance(resource, dict):
            resources.append({
                "course_name": str(resource.get("course_name") or resource.get("name") or "").strip(),
                "course_link": str(resource.get("course_link") or resource.get("link") or "").strip(),
                "instructor": str(resource.get("instructor") or "").strip(),
                "details": text_list_from(resource.get("details") or resource.get("description") or []),
            })
    guidance_raw = value.get("question_generation_guidance") or {}
    return {
        "subject": str(subject).strip(),
        "objectives": text_list("objectives", "course_objectives"),
        "learning_outcomes": text_list("learning_outcomes", "outcomes", "course_outcomes"),
        "modules": modules,
        "references": text_list("references", "books"),
        "digital_learning_resources": resources,
        "assessment_information": text_list("assessment_information", "marks_distribution", "assessment"),
        "question_generation_guidance": guidance_raw if isinstance(guidance_raw, dict) else {},
    }


def text_list_from(candidate) -> list[str]:
    if isinstance(candidate, str):
        candidate = [candidate]
    return [item.strip() for item in candidate if isinstance(item, str) and item.strip()] if isinstance(candidate, list) else []


def _extract_metadata_from_source(text: str) -> dict:
    """Conservatively recover labeled course metadata if model extraction omits it."""
    def section(start: str, stops: str) -> str:
        match = re.search(rf"(?is)\b(?:{start})\b\s*:?\s*(?P<content>.*?)(?=\b(?:{stops})\b\s*:?\s*|\Z)", text)
        return match.group("content").strip() if match else ""

    objectives_text = section(r"objectives?", r"module\s*[-–—:]?\s*[ivxlcdm\d]+|outcomes?|books?|references?|digital learning resources?")
    outcomes_text = section(r"(?:course\s+)?outcomes?", r"books?|references?|digital learning resources?")
    references_text = section(r"books?|references?", r"digital learning resources?")
    resources_text = section(r"digital learning resources?", r"\Z")

    def split_items(value: str, marker: str) -> list[str]:
        value = re.sub(r"\[[0-9]+\]", " ", value)
        value = re.sub(r"\*{1,2}", "", value)
        parts = re.split(rf"(?i)(?={marker})", value)
        return [re.sub(r"\s+", " ", item).strip(" \t\r\n-;,*")
                for item in parts if item.strip(" \t\r\n-;,")]

    objectives = split_items(objectives_text, r"to\s+(?:learn|introduce)\b") if objectives_text else []
    outcomes = split_items(outcomes_text, r"ability\s+to\b") if outcomes_text else []
    references = []
    if references_text:
        matches = list(re.finditer(r"\[\s*\d+\s*\]\s*(.*?)(?=\[\s*\d+\s*\]|\Z)", references_text, re.S))
        references = []
        for match in matches:
            value = re.sub(r"(?i)(?:\d+(?:st|nd|rd|th)\s+Semester\s+)?B\.\s*Tech\s*\([^)]*\)\s*Syllabus from Admission Batch\s+\d{4}-\d{2}(?:\s+\d+(?:st|nd|rd|th)\s+Semester)?", " ", match.group(1))
            value = re.sub(r"\[Page\s+\d+\]", " ", value, flags=re.I)
            value = re.sub(r"(?i)\bB\.\s*Tech\s*\([^)]*\)\s*Syllabus from Admission Batch\s+\d{4}-\d{2}", " ", value)
            value = re.sub(r"\*{1,2}", "", value)
            value = re.sub(r"\s+", " ", value).strip(" \t\r\n;,*")
            if value:
                references.append(value)
    resources = []
    if resources_text:
        entries = re.split(r"(?i)(?=course\s+name\s*:)", resources_text)
        for entry in entries:
            if not re.search(r"(?i)course\s+name\s*:", entry):
                continue
            def field(label: str, following: str) -> str:
                found = re.search(rf"(?is){label}\s*:\s*(.*?)(?={following}|\Z)", entry)
                value = re.sub(r"\[Page\s+\d+\]", " ", found.group(1), flags=re.I) if found else ""
                value = re.sub(r"\*{1,2}", "", value)
                return re.sub(r"\s+", " ", value).strip(" \t\r\n:,*")
            resources.append({
                "course_name": field(r"course\s+name", r"course\s+link\s*:|course\s+instructor\s*:"),
                "course_link": field(r"course\s+link", r"course\s+instructor\s*:"),
                "instructor": field(r"course\s+instructor", r"course\s+name\s*:"),
                "details": [],
            })
    assessment_match = re.search(
        r"(?is)\b(?:assessment information|assessment pattern|scheme of (?:evaluation|examination)|marks distribution)\b\s*:?\s*(.*?)(?=\b(?:objectives?|course outcomes?|learning outcomes?|module\s*[-–—:]?\s*[ivxlcdm\d]+|books?|references?|digital learning resources?)\b\s*:|\Z)",
        text,
    )
    assessment = [re.sub(r"\s+", " ", line).strip(" -;,*")
                  for line in assessment_match.group(1).splitlines()
                  if line.strip(" -;,*")] if assessment_match else []
    return {"objectives": objectives, "learning_outcomes": outcomes,
            "references": references, "digital_learning_resources": resources,
            "assessment_information": assessment}


def _native_syllabus_text(filename: str, content: bytes) -> str:
    """Read embedded text for metadata fallback; page parsing remains Nemotron based."""
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".pdf":
            import pymupdf
            document = pymupdf.open(stream=content, filetype="pdf")
            try:
                return "\n\n".join(page.get_text() for page in document)
            finally:
                document.close()
        if suffix == ".docx":
            from docx import Document
            document = Document(io.BytesIO(content))
            parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
            for table in document.tables:
                parts.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
            return "\n".join(parts)
    except Exception as exc:
        logger.info("Native syllabus metadata fallback unavailable: %s", type(exc).__name__)
    return ""


def _render_summary_text(value: str) -> str:
    try:
        record = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
        return value
    if not isinstance(record, dict):
        return value
    candidate = record.get("summary") if isinstance(record.get("summary"), dict) else record
    if not (candidate.get("modules") or candidate.get("units")):
        return value
    structured = SyllabusSummary.model_validate(
        _normalize_structure_payload(candidate, str(record.get("subject") or candidate.get("subject") or "")))
    if not structured.modules:
        return value
    return _render_summary_data(structured.model_dump())


def _render_summary_data(summary: dict) -> str:
    lines = [str(summary.get("subject") or "")]
    for module in summary.get("modules") or []:
        if isinstance(module, dict):
            module_label = str(module.get("module_name") or "")
            hours = module.get("hours")
            sections = module.get("sections") or []
        else:
            hours = getattr(module, "hours", None)
            sections = getattr(module, "sections", [])
        if hours is not None:
            module_label += f" ({hours} hours)"
        lines.append(f"\n{module_label}")
        for section in sections:
            heading = section.get("heading", "") if isinstance(section, dict) else section.heading
            topics = section.get("topics", []) if isinstance(section, dict) else section.topics
            lines.append(f"  {heading}")
            lines.extend(f"    - {topic}" for topic in topics)
    return "\n".join(lines).strip()


def validate_submitted_summary(submitted: dict | str, syllabus: dict) -> None:
    """Reject a frontend summary that does not match the selected stored syllabus."""
    stored = syllabus.get("summary_data")
    if not isinstance(stored, dict) or not isinstance(stored.get("modules"), list):
        raise ValueError("The selected syllabus has no validated structured summary.")
    subject_name = str(syllabus.get("subject_name") or stored.get("subject") or "")
    if isinstance(submitted, str):
        supplied_text = " ".join(submitted.split())
        stored_text = " ".join(_render_summary_data(stored).split())
        if supplied_text != stored_text:
            raise ValueError("Submitted syllabus summary does not match the selected syllabus.")
        return
    if not isinstance(submitted, dict):
        raise ValueError("Syllabus summary must be structured data or summary text.")
    for key in ("summary_data", "syllabus_summary", "summary"):
        nested = submitted.get(key)
        if isinstance(nested, dict):
            submitted = nested
            break
    try:
        normalized = SyllabusSummary.model_validate(
            _normalize_structure_payload(submitted, subject_name)
        ).model_dump()
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("Submitted syllabus summary is not a valid structured syllabus.") from exc
    stored_normalized = SyllabusSummary.model_validate(
        _normalize_structure_payload(stored, subject_name)
    ).model_dump()
    if (normalized.get("subject", "").casefold() != stored_normalized.get("subject", "").casefold()
            or normalized.get("modules") != stored_normalized.get("modules")):
        raise ValueError("Submitted syllabus summary does not match the selected syllabus.")


def _scope_key(item: dict) -> str:
    return "\x1f".join(str(item.get(key) or "").casefold() for key in
                        ("module_name", "heading", "topic"))


def _scope_hierarchy(scope: list[dict]) -> list[dict]:
    modules: list[dict] = []
    module_index: dict[str, dict] = {}
    section_index: dict[tuple[str, str], dict] = {}
    for entry in scope:
        module_name = str(entry.get("module_name") or "").strip()
        heading = str(entry.get("heading") or "").strip()
        module_key = module_name.casefold()
        module = module_index.get(module_key)
        if module is None:
            module = {"module_name": module_name, "sections": []}
            module_index[module_key] = module
            modules.append(module)
        section_key = (module_key, heading.casefold())
        section = section_index.get(section_key)
        if section is None:
            section = {"heading": heading, "topics": []}
            section_index[section_key] = section
            module["sections"].append(section)
        topic = {"topic": entry.get("topic")}
        if entry.get("description"):
            topic["description"] = entry["description"]
        section["topics"].append(topic)
    return modules


def _generation_request_identity(request: dict) -> str:
    transient = {"syllabus_summary", "selected_modules", "selected_topics", "resolved_scope",
                 "resolved_hierarchy", "resolved_guidance", "generation_context_id",
                 "calculated_total_questions", "calculated_total_marks"}
    stable = {key: value for key, value in request.items() if key not in transient}
    return _safe_json({key: stable[key] for key in sorted(stable)})


def _topic_generation_guidance(syllabus: dict, item: dict) -> dict:
    """Read validated, stored guidance for the selected syllabus topic."""
    summary = syllabus.get("summary_data")
    if not isinstance(summary, dict):
        return {}
    guidance = summary.get("question_generation_guidance") or {}
    topic = str(item.get("topic") or "").casefold()
    matches = []
    for section in guidance.get("sections", []):
        for entry in section.get("topic_guidance", []):
            if str(entry.get("topic") or "").casefold() == topic:
                matches.append({
                    "module_name": section.get("module_name"),
                    "heading": section.get("heading"),
                    "topic_guidance": entry,
                    "coverage_suggestions": section.get("coverage_suggestions", []),
                    "marks_are_ai_suggestions": bool(guidance.get("marks_are_ai_suggestions", True)),
                })
    requested_heading = str(item.get("source_heading") or "").casefold()
    requested_module = str(item.get("source_module") or "").casefold()
    return next((match for match in matches
                 if str(match["heading"]).casefold() == requested_heading
                 and str(match["module_name"]).casefold() == requested_module), {})


def _merge_module_sections(module_name: str, hours: int | None, module_parts: list[dict]) -> dict:
    sections: list[dict] = []
    for part in module_parts:
        for section in part.get("sections", []):
            if sections and sections[-1]["heading"].casefold() == section["heading"].casefold():
                sections[-1]["topics"].extend(section.get("topics", []))
            else:
                sections.append({"heading": section["heading"], "topics": list(section.get("topics", []))})
        for section in sections:
            seen: set[str] = set()
            section["topics"] = [topic for topic in section["topics"]
                                 if not (topic.casefold() in seen or seen.add(topic.casefold()))]
            combined = re.match(r"^([A-Z][A-Z0-9 &,()/.-]{2,}?)\s+[–—-]\s*([A-Z][a-z].+)$", section["heading"])
            if combined:
                section["heading"] = combined.group(1).strip()
                section["topics"].insert(0, combined.group(2).strip())
    return {"module_name": module_name, "hours": hours, "sections": sections}


def _fallback_module_from_source(module: dict) -> dict:
    """Recover a wholly omitted module from explicit heading/topic markers in source text."""
    text = _clean_extracted_text(module.get("text", ""))
    text = re.sub(r"(?im)^\s*module\s*[-–—:]?\s*[ivxlcdm\d]+\s*:?[ \t]*", "", text)
    text = re.sub(r"(?im)^\s*\(\s*\d+\s*hours?\s*\)\s*", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    marker = re.compile(
        r"(?P<heading>(?:[A-Z][A-Z0-9 &,()/.-]{2,80}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4}))\s*(?P<separator>[-–—:])\s*"
    )
    matches = []
    for found in marker.finditer(text):
        heading = found.group("heading")
        if heading != heading.upper():
            prefix = text[:found.start()].rstrip()
            if found.group("separator") != ":" or (prefix and prefix[-1] not in ".!?\n"):
                continue
        matches.append(found)
    sections = []
    for index, found in enumerate(matches):
        heading = found.group("heading").strip(" -–—:")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[found.end():end]
        topics = [re.sub(r"\s+", " ", part).strip(" \t\r\n-–—;,*")
                  for part in re.split(r"[,;]", body) if part.strip(" \t\r\n-–—;,*")]
        topics = [topic for topic in topics if topic and topic.casefold() != heading.casefold()]
        if topics:
            sections.append({"heading": heading, "topics": topics})
    return {"module_name": module["module_name"], "hours": module.get("hours"), "sections": sections}


async def _structure_module_chunk(client, chunk: dict, semaphore: asyncio.Semaphore) -> dict:
    system = (
        "Structure only the supplied source passage. Preserve every meaningful heading and topic in source order, "
        "retain original wording, capitalization and punctuation, and do not invent, explain, or omit content. "
        "Return JSON {\"modules\":[{\"module_name\":string,\"hours\":integer|null,"
        "\"sections\":[{\"heading\":string,\"topics\":[string]}]}]}. "
        "The source module and heading context are authoritative; do not move content to another module. "
        "Do not include course objectives, outcomes, references, or resources in topics."
    )
    payload = {
        "module_context": chunk.get("module_name") or "Module not explicitly labeled",
        "hours_context": chunk.get("hours"),
        "heading_context": chunk.get("heading_context", ""),
        "source_text": chunk["text"],
    }
    last_error = None
    for attempt in range(2):
        try:
            async with semaphore:
                response = await _chat(
                    client, system, payload,
                    max_tokens=int(settings.SMART_QPG_SYLLABUS_STRUCTURE_OUTPUT_TOKENS),
                    model="nvidia/nemotron-3-ultra-550b-a55b", temperature=0.1, enable_thinking=False,
                )
            normalized = _normalize_structure_payload(response, "")
            modules = [SyllabusModule.model_validate(item).model_dump() for item in normalized["modules"]]
            if not modules:
                raise ValueError("Nemotron returned no structured module for a non-empty source chunk")
            unsupported = False
            for parsed_module in modules:
                for parsed_section in parsed_module["sections"]:
                    source_heading = _canonical_source_phrase(chunk["text"], parsed_section["heading"])
                    if source_heading:
                        parsed_section["heading"] = source_heading
                    else:
                        unsupported = True
                    canonical_topics = []
                    for topic in parsed_section["topics"]:
                        source_topic = _canonical_source_phrase(chunk["text"], topic)
                        if source_topic:
                            canonical_topics.append(source_topic)
                        else:
                            unsupported = True
                    parsed_section["topics"] = canonical_topics
            return {"modules": modules, "unsupported_topics": unsupported}
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("Retrying failed syllabus module chunk (%s)", type(exc).__name__)
    raise last_error


async def _extract_syllabus_metadata(client, metadata_text: str, subject_name: str,
                                     metadata_source_text: str = "") -> dict:
    if not metadata_text.strip() and not metadata_source_text.strip():
        return {"objectives": [], "learning_outcomes": [], "references": [],
                "digital_learning_resources": [], "assessment_information": []}
    system = (
        "Extract only course-level syllabus metadata explicitly present in the supplied text. Do not infer or invent. "
        "Preserve source wording, capitalization, and order. Return JSON with objectives (string array), "
        "learning_outcomes (string array), references (string array), digital_learning_resources "
        "(array of {course_name,course_link,instructor,details}), and assessment_information (string array) "
        "for official marks distribution or assessment rules. Include assessment entries only when explicitly stated. "
        "Use empty arrays when absent. "
        "Do not return modules or topics."
    )
    result = {"objectives": [], "learning_outcomes": [], "references": [],
              "digital_learning_resources": [], "assessment_information": []}
    if metadata_text.strip():
        token_budget = max(500, int(settings.SMART_QPG_SYLLABUS_INPUT_TOKEN_BUDGET))
        metadata_chunks = _split_module_chunks(
            {"module_name": "course metadata", "hours": None, "text": metadata_text}, token_budget)
        for chunk in metadata_chunks:
            response = await _chat(
                client, system, {"subject": subject_name, "metadata_source": chunk["text"]},
                max_tokens=int(settings.SMART_QPG_SYLLABUS_STRUCTURE_OUTPUT_TOKENS),
                model="nvidia/nemotron-3-ultra-550b-a55b", temperature=0.1, enable_thinking=False,
            )
            normalized = _normalize_structure_payload(response, subject_name)
            for key in result:
                result[key].extend(normalized[key])
    fallback = _extract_metadata_from_source(metadata_source_text) if metadata_source_text else {}
    parsed_fallback = _extract_metadata_from_source(metadata_text)
    for key, items in parsed_fallback.items():
        if not fallback.get(key):
            fallback[key] = items
    for key, items in fallback.items():
        if items:
            result[key] = items
    # Assessment entries must be grounded in wording present in a supplied
    # source, because official allocations must never be AI-inferred.
    source_text = "\n".join(part for part in (metadata_text, metadata_source_text) if part)
    result["assessment_information"] = [
        source_phrase for entry in result["assessment_information"]
        if (source_phrase := _canonical_source_phrase(source_text, entry))
    ]
    return result


async def _guidance_for_section(client, module: dict, section: dict,
                                semaphore: asyncio.Semaphore) -> dict:
    topics = section.get("topics", [])
    if not topics:
        return {"module_name": module["module_name"], "heading": section["heading"],
                "topic_guidance": [], "coverage_suggestions": []}
    system = (
        "Suggest compact question-generation guidance for the supplied syllabus section. Do not write questions or answers. "
        "Use only topic strings exactly as supplied; do not add topics. Suggested marks and importance are AI-generated "
        "recommendations, never official allocations. Return JSON {\"topic_guidance\":[{\"topic\":string,"
        "\"topic_categories\":[string],\"question_formats\":[string],\"suggested_marks\":[integer],"
        "\"difficulty_levels\":[\"easy\",\"medium\",\"hard\"],\"importance_rationale\":string}],"
        "\"coverage_suggestions\":[string]}. Categories may be definition-based, conceptual, descriptive, "
        "comparison-based, algorithmic, numerical, problem-solving, or application-based; include only applicable categories. "
        "Question formats may include Define / What is?, State / List, Explain, Differentiate / Compare, "
        "Describe with a diagram, Explain with an example, Write or trace an algorithm, Solve a problem, or Analyse / Evaluate. "
        "Choose only plausible formats for each topic. Use only suggested marks 1, 2, 5, or 10, selected by topic scope. "
        "Explain why a topic may be important without claiming official weightage. Keep coverage suggestions short."
    )
    payload = {"module_name": module["module_name"], "heading": section["heading"], "topics": topics}
    last_error = None
    for attempt in range(2):
        try:
            async with semaphore:
                response = await _chat(
                    client, system, payload,
                    max_tokens=int(settings.SMART_QPG_SYLLABUS_GUIDANCE_OUTPUT_TOKENS),
                    model="nvidia/nemotron-3-ultra-550b-a55b", temperature=0.2, enable_thinking=False,
                )
            candidate = {
                "module_name": module["module_name"], "heading": section["heading"],
                "topic_guidance": response.get("topic_guidance", []),
                "coverage_suggestions": response.get("coverage_suggestions", []),
            }
            validated = SectionQuestionGuidance.model_validate(candidate).model_dump()
            # Model output is advisory. Enforce exact membership in the source list.
            valid_topics = set(topics)
            validated["topic_guidance"] = [item for item in validated["topic_guidance"]
                                           if item["topic"] in valid_topics]
            validated["coverage_suggestions"] = validated["coverage_suggestions"][:3]
            missing = [topic for topic in topics if topic not in {item["topic"] for item in validated["topic_guidance"]}]
            retry_error = None
            if missing:
                try:
                    async with semaphore:
                        retry_response = await _chat(
                            client, system, {**payload, "topics": missing},
                            max_tokens=int(settings.SMART_QPG_SYLLABUS_GUIDANCE_OUTPUT_TOKENS),
                            model="nvidia/nemotron-3-ultra-550b-a55b", temperature=0.2, enable_thinking=False,
                        )
                    retry_candidate = SectionQuestionGuidance.model_validate({
                        "module_name": module["module_name"], "heading": section["heading"],
                        "topic_guidance": retry_response.get("topic_guidance", []),
                        "coverage_suggestions": [],
                    }).model_dump()
                    existing = {item["topic"] for item in validated["topic_guidance"]}
                    for item in retry_candidate["topic_guidance"]:
                        if item["topic"] in missing and item["topic"] not in existing:
                            validated["topic_guidance"].append(item)
                            existing.add(item["topic"])
                except Exception as exc:
                    retry_error = type(exc).__name__
                    logger.warning("Retry for missing guidance topics failed (%s)", retry_error)
            if retry_error:
                validated["_errors"] = [f"missing topic guidance retry: {retry_error}"]
            for item in validated["topic_guidance"]:
                item["question_formats"] = item["question_formats"][:5]
                item["suggested_marks"] = sorted({mark for mark in item["suggested_marks"] if mark in {1, 2, 5, 10}})
                item["difficulty_levels"] = [level for level in item["difficulty_levels"]
                                              if level.casefold() in {"easy", "medium", "hard"}][:3]
            return validated
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("Retrying failed syllabus guidance call (%s)", type(exc).__name__)
    raise last_error


async def structure_syllabus(text: str, *, subject_name: str = "", metadata_source_text: str = "") -> dict:
    client = _nvidia_client()
    try:
        token_budget = max(500, int(settings.SMART_QPG_SYLLABUS_INPUT_TOKEN_BUDGET))
        preamble, modules, trailing = _source_units(text)
        metadata_text = "\n\n".join(part for part in (preamble, trailing) if part)
        chunks = [chunk for module in modules for chunk in _split_module_chunks(module, token_budget)]
        concurrency = max(1, min(8, int(settings.SMART_QPG_SYLLABUS_CALL_CONCURRENCY)))
        semaphore = asyncio.Semaphore(concurrency)
        module_results = await asyncio.gather(
            *(_structure_module_chunk(client, chunk, semaphore) for chunk in chunks),
            return_exceptions=True,
        )
        errors: list[str] = []
        by_module: dict[int, list[dict]] = {}
        module_names: dict[int, tuple[str, int | None]] = {}
        for index, (chunk, result) in enumerate(zip(chunks, module_results)):
            if isinstance(result, BaseException):
                errors.append(f"{chunk.get('module_name') or 'unlabeled module'} chunk {index + 1}: {type(result).__name__}")
                logger.warning("Syllabus module chunk %d failed: %s", index + 1, type(result).__name__)
                continue
            module_index = next((i for i, module in enumerate(modules)
                                 if module["module_name"] == chunk["module_name"]), 0)
            module_names[module_index] = (modules[module_index]["module_name"], modules[module_index].get("hours"))
            by_module.setdefault(module_index, []).extend(result["modules"])
            if result.get("unsupported_topics"):
                errors.append(f"{chunk.get('module_name') or 'unlabeled module'}: unsupported model topics were discarded")

        ordered_modules = []
        native_modules = _source_units(metadata_source_text)[1] if metadata_source_text else []
        for module_index, source_module in enumerate(modules):
            name, hours = source_module["module_name"], source_module.get("hours")
            parts = by_module.get(module_index, [])
            structured = _merge_module_sections(name, hours, parts) if parts else None
            if not structured or not any(section["topics"] for section in structured["sections"]):
                native_module = next((candidate for candidate in native_modules
                                      if candidate["module_name"].casefold() == name.casefold()), None)
                fallback = _fallback_module_from_source(native_module) if native_module else None
                if fallback and fallback["sections"]:
                    structured = fallback
                else:
                    errors.append(f"{name}: no topics could be validated from the source")
                    logger.warning("No structured topics retained for %s", name)
                    structured = structured or {"module_name": name, "hours": hours, "sections": []}
            ordered_modules.append(structured)

        metadata = {"objectives": [], "learning_outcomes": [], "references": [],
                    "digital_learning_resources": [], "assessment_information": []}
        if metadata_text or metadata_source_text:
            try:
                metadata = await _extract_syllabus_metadata(
                    client, metadata_text, subject_name, metadata_source_text=metadata_source_text)
            except Exception as exc:
                errors.append(f"course metadata: {type(exc).__name__}")
                logger.warning("Course metadata extraction failed: %s", type(exc).__name__)

        guidance_jobs = [(module, section) for module in ordered_modules for section in module["sections"]]
        guidance_results = await asyncio.gather(
            *(_guidance_for_section(client, module, section, semaphore)
              for module, section in guidance_jobs),
            return_exceptions=True,
        )
        guidance_sections = []
        for index, (job, result) in enumerate(zip(guidance_jobs, guidance_results)):
            module, section = job
            if isinstance(result, BaseException):
                errors.append(f"guidance {module['module_name']} / {section['heading']}: {type(result).__name__}")
                logger.warning("Syllabus guidance failed for %s / %s: %s",
                               module["module_name"], section["heading"], type(result).__name__)
            else:
                errors.extend(f"guidance {module['module_name']} / {section['heading']}: {message}"
                              for message in result.pop("_errors", []))
                guidance_sections.append(result)

        candidate = {
            "subject": subject_name or "",
            **metadata,
            "modules": ordered_modules,
            "question_generation_guidance": {
                "sections": guidance_sections,
                "marks_are_ai_suggestions": True,
            },
        }
        validated = SyllabusSummary.model_validate(candidate).model_dump()
        if not validated["subject"] or not validated["modules"]:
            raise ValueError("The generated syllabus summary is empty.")
        # Internal status hints let persistence retain successful module work if
        # another independent extraction/guidance call failed.
        if errors:
            validated["_processing_errors"] = errors
        return validated
    finally:
        await client.close()


def create_syllabus_upload(*, subject_id: str, filename: str, content: bytes,
                           syllabus_name: str | None, academic_year: str | None,
                           created_by: str, replace: bool = False) -> dict:
    subject = database.get_subject_by_id(subject_id)
    if not subject:
        raise KeyError("Subject not found")
    effective_name = (syllabus_name or Path(filename).stem).strip()
    if not effective_name or len(effective_name) > 200:
        raise ValueError("Syllabus name must contain 1 to 200 characters.")
    if academic_year is not None:
        academic_year = academic_year.strip()
        if not academic_year or len(academic_year) > 40:
            raise ValueError("Academic year must contain 1 to 40 characters when supplied.")
    digest, stamp = hashlib.sha256(content).hexdigest(), now()
    syllabus_id = uuid.uuid4().hex
    with database.connect() as db:
        existing = db.execute("""SELECT id FROM syllabuses
            WHERE subject_id=? AND is_active=1 AND syllabus_name=?
              AND COALESCE(academic_year,'')=COALESCE(?, '') LIMIT 1""",
            (subject_id,effective_name,academic_year)).fetchone()
        if existing and not replace:
            raise SyllabusConflictError("A syllabus with this name and academic year already exists; set replace=true to upload a new version.")
        duplicate = db.execute("SELECT 1 FROM syllabuses WHERE subject_id=? AND checksum_sha256=? LIMIT 1", (subject_id,digest)).fetchone()
        if duplicate and not replace:
            raise SyllabusConflictError("This file has already been uploaded for the selected subject.")
        old = db.execute("SELECT COALESCE(MAX(version),0) AS version FROM syllabuses WHERE subject_id=?", (subject_id,)).fetchone()
        version = int(old["version"] or 0) + 1
        db.execute("""INSERT INTO syllabuses(id,subject_id,filename,content_blob,summary_text,processing_status,
            syllabus_name,academic_year,version,is_active,uploaded_by,checksum_sha256,created_at,updated_at)
            VALUES(?,?,?,?,'','pending',?,?,?,0,?,?,?,?)""",
            (syllabus_id,subject_id,filename,content,effective_name,academic_year,version,created_by,digest,stamp,stamp))
    return {"syllabus_id": syllabus_id, "id": syllabus_id, "subject_id": subject_id,
            "subject_name": subject["subject_name"],
            "filename": filename, "syllabus_name": effective_name, "academic_year": academic_year,
            "version": version, "is_active": False, "status": "pending", "processing_status": "pending", "processing_error": None,
            "summary_text": "", "content_type": Path(filename).suffix.lower().lstrip("."), "size_bytes": len(content),
            "checksum_sha256": digest, "created_by": created_by, "created_at": stamp, "updated_at": stamp}


async def process_syllabus_upload(syllabus_id: str) -> None:
    with _syllabus_locks_guard:
        lock = _syllabus_locks.setdefault(syllabus_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        with database.connect() as db:
            row = db.execute("""SELECT sy.subject_id,sy.content_blob,sy.filename,sy.processing_status,
                sy.processing_attempts,sy.summary_text,s.subject_name FROM syllabuses sy JOIN subjects s ON s.subject_id=sy.subject_id
                WHERE sy.id=?""", (syllabus_id,)).fetchone()
            if not row:
                return
            # Retry requests atomically mark the record processing and increment
            # attempts before scheduling this worker. New uploads arrive pending.
            if row["processing_status"] != "processing":
                db.execute("""UPDATE syllabuses SET processing_status='processing',processing_error=NULL,
                    processing_attempts=processing_attempts+1,updated_at=? WHERE id=?""", (now(), syllabus_id))
        try:
            content = bytes(row["content_blob"] or b"")
            if not content:
                raise ValueError("Stored syllabus file is empty.")
            native_metadata_text = await asyncio.to_thread(
                _native_syllabus_text, str(row["filename"]), content)
            pages = await asyncio.to_thread(_render_syllabus_pages, str(row["filename"]), content)
            from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import NemotronParseClient
            from src.masterdeorca.document_intelligence.llm.scraper_parser.normalizer import normalize_nemotron_content
            parser = NemotronParseClient()
            semaphore = asyncio.Semaphore(max(1, min(4, int(settings.NVIDIA_API_CONCURRENCY))))
            async def parse_page(page):
                async with semaphore:
                    for attempt in range(2):
                        try:
                            raw = await asyncio.to_thread(parser.parse_page_image, page.data, page.mime, page.page_number,
                                                          f"page{page.page_number}.png")
                            raw_content = raw.get("content", raw.get("text", "")) if isinstance(raw, dict) else raw
                            extracted = normalize_nemotron_content(_readable_parse_text(raw_content))["text"].strip()
                            extracted = _clean_extracted_text(extracted)
                            if not extracted:
                                raise ValueError("Nemotron returned no readable text")
                            return page.page_number, extracted, None
                        except Exception as exc:
                            logger.warning("Syllabus page %d parse attempt %d failed: %s", page.page_number,
                                           attempt + 1, type(exc).__name__)
                            if attempt == 1:
                                return page.page_number, "", f"{type(exc).__name__}: page parsing failed"
                            await asyncio.sleep(1)
            results = sorted(await asyncio.gather(*(parse_page(page) for page in pages)), key=lambda item: item[0])
            extracted_pages = [(n, text) for n, text, error in results if text]
            failed_pages = [{"page": n, "error": error} for n, text, error in results if error]
            if not extracted_pages:
                raise ValueError("No syllabus pages could be extracted.")
            extracted_text = "\n\n".join(f"[Page {n}]\n{text}" for n, text in extracted_pages)
            summary = await structure_syllabus(
                extracted_text, subject_name=str(row["subject_name"]), metadata_source_text=native_metadata_text)
            processing_errors = summary.pop("_processing_errors", [])
            stored_summary = _safe_json(summary)
            stamp = now()
            db_status = "failed" if failed_pages or processing_errors else "completed"
            failed_numbers = ", ".join(str(item["page"]) for item in failed_pages)
            error_parts = []
            if failed_pages:
                error_parts.append(f"Partial page extraction: pages {failed_numbers} of {len(pages)} failed.")
            if processing_errors:
                error_parts.append("Partial syllabus processing: " + "; ".join(processing_errors)[:800])
            error_text = " ".join(error_parts) or None
            with database.connect() as db:
                current = db.execute("SELECT subject_id FROM syllabuses WHERE id=?", (syllabus_id,)).fetchone()
                if not current:
                    return
                if db_status == "completed":
                    db.execute("UPDATE syllabuses SET is_active=0,updated_at=? WHERE subject_id=?", (stamp, current["subject_id"]))
                    db.execute("""UPDATE syllabuses SET summary_text=?,processing_status='completed',processing_error=NULL,
                        is_active=1,updated_at=? WHERE id=?""", (stored_summary, stamp, syllabus_id))
                else:
                    # An incomplete extraction never replaces the last validated summary.
                    db.execute("""UPDATE syllabuses SET processing_status='failed',processing_error=?,
                        updated_at=? WHERE id=?""", (error_text, stamp, syllabus_id))
        except Exception as exc:
            logger.exception("Syllabus page extraction/summarisation failed for %s", syllabus_id)
            safe_error = str(exc)[:1000] if isinstance(exc, ValueError) else f"{type(exc).__name__}: syllabus processing failed"
            with database.connect() as db:
                db.execute("UPDATE syllabuses SET processing_status='failed',processing_error=?,updated_at=? WHERE id=?",
                           (safe_error, now(), syllabus_id))
    finally:
        lock.release()


class SyllabusAlreadyProcessingError(ValueError):
    pass


def queue_syllabus_reprocessing(syllabus_id: str) -> dict | None:
    """Atomically mark an existing record for reprocessing without touching its file or summary."""
    stamp = now()
    with database.connect() as db:
        row = db.execute("SELECT id,processing_status FROM syllabuses WHERE id=?", (syllabus_id,)).fetchone()
        if not row:
            return None
        if row["processing_status"] == "processing":
            raise SyllabusAlreadyProcessingError("This syllabus is already being processed.")
        updated = db.execute("""UPDATE syllabuses SET processing_status='processing',processing_error=NULL,
            processing_attempts=processing_attempts+1,updated_at=? WHERE id=? AND processing_status<>'processing'""",
                             (stamp, syllabus_id))
        if updated.rowcount != 1:
            raise SyllabusAlreadyProcessingError("This syllabus is already being processed.")
    return get_syllabus(syllabus_id)


def resume_interrupted_syllabus_uploads() -> None:
    """Resume persisted pending/running uploads after an API process restart."""
    with database.connect() as db:
        rows = db.execute("SELECT id FROM syllabuses WHERE processing_status IN ('pending','processing')").fetchall()
        db.execute("UPDATE syllabuses SET processing_status='pending',updated_at=? WHERE processing_status='processing'", (now(),))
    for row in rows:
        threading.Thread(target=_run_syllabus_worker, args=(row[0],), daemon=True,
                         name=f"syllabus-{row[0][:8]}").start()


def _run_syllabus_worker(syllabus_id: str) -> None:
    """Run an async worker in its own recovery thread after process startup."""
    asyncio.run(process_syllabus_upload(syllabus_id))


def get_syllabus(syllabus_id: str) -> dict | None:
    with database.connect() as db:
        row = db.execute("""SELECT sy.id,sy.subject_id,sy.filename,sy.summary_text,sy.processing_status,
                sy.processing_error,sy.syllabus_name,sy.academic_year,sy.version,sy.is_active,sy.uploaded_by,
                sy.checksum_sha256,sy.created_at,sy.updated_at,LENGTH(sy.content_blob) AS size_bytes,
                s.subject_name AS subject_name
            FROM syllabuses sy JOIN subjects s ON s.subject_id=sy.subject_id
            WHERE sy.id=?""", (syllabus_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["is_active"] = bool(result["is_active"])
    result["syllabus_id"] = result["id"]
    result["original_filename"] = result["filename"]
    result["status"] = result["processing_status"]
    result["created_by"] = result["uploaded_by"]
    raw_summary = str(result.get("summary_text") or "")
    try:
        summary_record = json.loads(raw_summary)
        if isinstance(summary_record, dict):
            summary_record = summary_record.get("summary") if isinstance(summary_record.get("summary"), dict) else summary_record
            result["summary_data"] = _normalize_structure_payload(summary_record, str(result.get("subject_name") or ""))
            result["summary_data"] = SyllabusSummary.model_validate(result["summary_data"]).model_dump()
    except (TypeError, ValueError, ValidationError):
        # Legacy plain-text summaries remain available through summary_text.
        pass
    result["summary_text"] = (_render_summary_data(result["summary_data"])
                              if isinstance(result.get("summary_data"), dict)
                              else _render_summary_text(raw_summary))
    return result


def list_syllabi(subject_id: str | None = None, status: str | None = None) -> list[dict]:
    if status is not None and status not in {"pending", "processing", "completed", "failed"}:
        raise ValueError("Invalid syllabus status.")
    clauses, params = [], []
    if subject_id is not None:
        clauses.append("sy.subject_id=?"); params.append(subject_id)
    if status is not None:
        clauses.append("sy.processing_status=?"); params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with database.connect() as db:
        ids = db.execute("SELECT sy.id FROM syllabuses sy JOIN subjects s ON s.subject_id=sy.subject_id" +
                         where + " ORDER BY sy.created_at DESC", params).fetchall()
    return [get_syllabus(row[0]) for row in ids]


def syllabus_upload_content(syllabus_id: str) -> bytes | None:
    with database.connect() as db:
        row = db.execute("SELECT content_blob FROM syllabuses WHERE id=?", (syllabus_id,)).fetchone()
    return bytes(row["content_blob"]) if row and row["content_blob"] is not None else None


def set_syllabus_active(syllabus_id: str, active: bool) -> dict | None:
    stamp = now()
    with database.connect() as db:
        row = db.execute("SELECT subject_id,processing_status FROM syllabuses WHERE id=?", (syllabus_id,)).fetchone()
        if not row:
            return None
        if active and row["processing_status"] != "completed":
            raise ValueError("Only completed syllabi can be made active.")
        if active:
            db.execute("UPDATE syllabuses SET is_active=0,updated_at=? WHERE subject_id=?", (stamp, row["subject_id"]))
        db.execute("UPDATE syllabuses SET is_active=?,updated_at=? WHERE id=?", (1 if active else 0, stamp, syllabus_id))
    return get_syllabus(syllabus_id)


def set_active_syllabus(syllabus_id: str) -> dict | None:
    return set_syllabus_active(syllabus_id, True)


class GeneratedQuestion(BaseModel):
    section: str
    subject: str
    topic: str
    source_module: str = ""
    source_heading: str = ""
    marks: int = Field(gt=0)
    type: str
    difficulty: str
    question_text: str = Field(min_length=8)
    sub_questions: list[dict] = Field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _is_duplicate(text: str, known: list[str]) -> bool:
    normalized = _norm(text)
    if not normalized:
        return True
    return any(normalized == _norm(item) or SequenceMatcher(None, normalized, _norm(item)).ratio() >= 0.86 for item in known)


def _normalize_generated_question(value: object, *, item: dict, subject_name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Model question must be a JSON object")
    normalized = dict(value)
    aliases = {"question_text": ("text", "question"), "section": ("section_name",),
               "topic": ("source_topic",), "marks": ("mark",),
               "type": ("question_type",), "sub_questions": ("subquestions",)}
    for field, candidates in aliases.items():
        if field not in normalized:
            candidate = next((normalized.get(alias) for alias in candidates if alias in normalized), None)
            if candidate is not None:
                normalized[field] = candidate
    normalized.setdefault("subject", subject_name)
    normalized.setdefault("section", item["section"])
    normalized.setdefault("topic", item["topic"])
    normalized.setdefault("marks", item["marks"])
    normalized.setdefault("type", item["type"])
    normalized.setdefault("difficulty", item["difficulty"])
    normalized["source_module"] = str(item.get("source_module") or "")
    normalized["source_heading"] = str(item.get("source_heading") or "")
    return GeneratedQuestion.model_validate(normalized).model_dump()


def _existing_examples(subject_name: str, topics: list[str], marks: list[int], context_limit: int = 10000) -> tuple[list[dict], list[str]]:
    with database.connect() as db:
        rows = db.execute("""SELECT q.record_json,q.marks_numeric,COALESCE(r.review_status,'unreviewed') AS document_review
            FROM question_bank_records q
            LEFT JOIN documents d ON d.document_id=q.document_id
            LEFT JOIN pdf_reviews r ON r.subject=q.subject_name AND r.filename=d.filename
            WHERE q.subject_name=? ORDER BY q.updated_at DESC LIMIT 1000""", (subject_name,)).fetchall()
    candidates, duplicate_texts = [], []
    for row in rows:
        try: record = json.loads(row["record_json"])
        except Exception: continue
        text = str(record.get("text") or record.get("question_text") or record.get("question") or "").strip()
        if not text: continue
        topic = str(record.get("topic") or record.get("topic_name") or "")
        if topics and topic and not any(t.casefold() in topic.casefold() or topic.casefold() in t.casefold() for t in topics):
            continue
        duplicate_texts.append(text)
        if row["document_review"] == "approved" or str(record.get("status", "")).casefold() == "approved":
            candidates.append({"text": text, "marks": row["marks_numeric"], "topic": topic})
    by_marks: dict[int, list[dict]] = {}
    for item in candidates:
        try: by_marks.setdefault(int(item["marks"]), []).append(item)
        except (TypeError, ValueError): pass
    examples = []
    for mark in dict.fromkeys(marks):
        pool = by_marks.get(mark, [])
        examples.extend(random.sample(pool, min(2, len(pool))))
    text_size = 0; bounded = []
    for item in examples:
        size = len(item["text"])
        if text_size + size > context_limit: continue
        bounded.append(item); text_size += size
    return bounded, duplicate_texts


def _topic_names(topics: list) -> set[str]:
    found: set[str] = set()
    def walk(value):
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
        elif isinstance(value, dict):
            label = value.get("name") or value.get("topic") or value.get("title")
            if isinstance(label, str) and label.strip():
                found.add(label.strip())
            for key in ("topics", "subtopics", "concepts"):
                children = value.get(key)
                if isinstance(children, list):
                    for child in children:
                        walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(topics)
    return found


def _resolve_generation_scope(syllabus: dict, request: dict) -> list[dict]:
    """Validate the hierarchy carried in `instructions` against stored data."""
    summary = syllabus.get("summary_data")
    if not isinstance(summary, dict) or not isinstance(summary.get("modules"), list):
        raise ValueError("This syllabus has no validated structured summary. Reprocess it before generating questions.")
    modules = [item for item in summary["modules"] if isinstance(item, dict)]
    by_name = {str(item.get("module_name") or "").strip().casefold(): item for item in modules}
    by_identifier = {str(item.get(key)).strip().casefold(): item for item in modules
                     for key in ("module_id", "id") if item.get(key) not in (None, "")}

    instructions = request.get("instructions")
    instruction_data = instructions if isinstance(instructions, dict) else {}
    hierarchy = next((instruction_data.get(key) for key in
                      ("selected_hierarchy", "syllabus_selection", "selection")
                      if isinstance(instruction_data.get(key), (dict, list))), None)
    if hierarchy is None:
        hierarchy = instruction_data
    if isinstance(hierarchy, list):
        hierarchy = {"modules": hierarchy}
    elif isinstance(instructions, list) and any(isinstance(item, dict) for item in instructions):
        hierarchy = {"modules": instructions}
    elif not isinstance(hierarchy, dict):
        hierarchy = {}
    raw_modules = request.get("selected_modules") or next((hierarchy.get(key) for key in
                        ("selected_modules", "modules", "module_selections")
                        if isinstance(hierarchy.get(key), list)), [])
    requested_scope: list[dict] = []
    selected_names: list[str] = []
    for raw_module in raw_modules:
        if isinstance(raw_module, str):
            module_name = raw_module.strip()
            selected_names.append(module_name)
            continue
        if not isinstance(raw_module, dict):
            raise ValueError("Each selected module in instructions must be a module name or hierarchy object.")
        module_name = str(raw_module.get("module_name") or raw_module.get("name") or raw_module.get("module") or raw_module.get("module_id") or raw_module.get("id") or "").strip()
        if module_name:
            selected_names.append(module_name)
        raw_sections = raw_module.get("sections", raw_module.get("selected_sections", []))
        for raw_section in raw_sections if isinstance(raw_sections, list) else []:
            if isinstance(raw_section, str):
                requested_scope.append({"module_name": module_name, "heading": raw_section.strip(), "topic": ""})
                continue
            if not isinstance(raw_section, dict):
                raise ValueError("Each selected section must be a section name or hierarchy object.")
            heading = str(raw_section.get("heading") or raw_section.get("section") or raw_section.get("name") or "").strip()
            topics = raw_section.get("topics", raw_section.get("selected_topics", []))
            if not isinstance(topics, list):
                raise ValueError("Selected section topics must be an array.")
            if not topics:
                requested_scope.append({"module_name": module_name, "heading": heading, "topic": ""})
            for topic in topics:
                if isinstance(topic, dict):
                    topic = topic.get("topic") or topic.get("name") or topic.get("title")
                if not isinstance(topic, str) or not topic.strip():
                    raise ValueError("Selected topics in instructions must be non-empty names.")
                requested_scope.append({"module_name": module_name, "heading": heading, "topic": topic.strip()})
    selected_topics = {str(topic).strip().casefold() for topic in request.get("selected_topics", [])
                       if str(topic).strip()}
    if not selected_names and not selected_topics:
        # A syllabus selection is the generation scope by default. Explicit
        # legacy selections remain understood by direct callers, but the API
        # generation workflow passes no selection fields.
        selected_names = [str(module.get("module_name") or "").strip() for module in modules
                          if str(module.get("module_name") or "").strip()]
    if not selected_names:
        # Topic-only selection is safe only when each topic name identifies a
        # single stored location. Ambiguous names must include hierarchy.
        for topic in selected_topics:
            locations = [(module, section, value)
                         for module in modules
                         for section in module.get("sections", []) if isinstance(section, dict)
                         for value in section.get("topics", [])
                         if str(value).strip().casefold() == topic]
            if not locations:
                raise ValueError(f"Selected topic does not exist in this syllabus: {topic}")
            if len(locations) > 1:
                raise ValueError(f"Selected topic {topic!r} is ambiguous; include its module and section in instructions.")
            module, section, value = locations[0]
            selected_names.append(str(module.get("module_name") or ""))
            requested_scope.append({"module_name": str(module.get("module_name") or ""),
                                    "heading": str(section.get("heading") or ""), "topic": str(value).strip()})
    if not selected_names:
        raise ValueError("Select at least one syllabus module and topic before generating questions.")
    selected_names = list(dict.fromkeys(selected_names))
    resolved_names = []
    unknown = []
    for name in selected_names:
        module = by_name.get(name.casefold()) or by_identifier.get(name.casefold())
        if module:
            resolved_names.append(str(module.get("module_name") or name).strip())
        else:
            unknown.append(name)
    if unknown:
        raise ValueError(f"Selected syllabus module does not exist (name or ID): {unknown[0]}")
    selected_names = list(dict.fromkeys(resolved_names))
    # An explicitly selected module means all of its topics unless section or
    # topic leaves narrow it. selected_topics always narrows by name.
    has_leaf_selection = bool(requested_scope)
    result = []
    for module_name in selected_names:
        module = by_name[module_name.casefold()]
        for section in module.get("sections", []):
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            detail_by_topic = {
                str(detail.get("topic") or detail.get("name") or "").strip().casefold(): detail
                for detail in section.get("topic_details", []) if isinstance(detail, dict)
            }
            for topic in section.get("topics", []):
                topic = str(topic).strip()
                if not heading or not topic:
                    continue
                identity = (module_name.casefold(), heading.casefold(), topic.casefold())
                if selected_topics and topic.casefold() not in selected_topics:
                    continue
                if has_leaf_selection and not any(
                    selected_module == identity[0]
                    and (not selected_heading or selected_heading == identity[1])
                    and (not selected_topic or selected_topic == identity[2])
                    for selected_module, selected_heading, selected_topic in
                    ((str(item.get("module_name") or "").strip().casefold(),
                      str(item.get("heading") or "").strip().casefold(),
                      str(item.get("topic") or "").strip().casefold())
                     for item in requested_scope if isinstance(item, dict))):
                    continue
                resolved = {"module_name": str(module.get("module_name") or module_name),
                            "heading": heading, "topic": topic}
                detail = detail_by_topic.get(topic.casefold(), {})
                description = (detail.get("description") or detail.get("topic_description")
                               or detail.get("details") or detail.get("content"))
                if description:
                    resolved["description"] = description
                result.append(resolved)
    for item in requested_scope:
        module_key = str(item.get("module_name") or "").strip().casefold()
        section_key = str(item.get("heading") or "").strip().casefold()
        topic_key = str(item.get("topic") or "").strip().casefold()
        module = by_name.get(module_key)
        section = next((section for section in module.get("sections", []) if isinstance(section, dict)
                        and str(section.get("heading") or "").strip().casefold() == section_key), None) if module else None
        if not module or not section:
            raise ValueError("A selected section does not belong to its stated module in this syllabus.")
        if topic_key and not any(str(topic).strip().casefold() == topic_key for topic in section.get("topics", [])):
            raise ValueError("A selected topic does not belong to its stated module and section in this syllabus.")
    if selected_topics:
        found_topics = {item["topic"].casefold() for item in result}
        unknown_topics = selected_topics - found_topics
        if unknown_topics:
            raise ValueError(f"Selected topic does not belong to the selected syllabus scope: {sorted(unknown_topics)[0]}")
    if selected_topics and not result:
        raise ValueError("Selected topics were not found under the selected syllabus modules.")
    if not result:
        raise ValueError("The selected syllabus modules do not contain usable topics.")
    return result


def create_job(*, user: str, subject_id: str, syllabus_id: str, request: dict,
               syllabus_record: dict | None = None) -> str:
    syllabus = syllabus_record if syllabus_record is not None else get_syllabus(syllabus_id)
    if not syllabus or syllabus["subject_id"] != subject_id:
        raise ValueError("Syllabus does not belong to the selected subject.")
    if syllabus.get("status") != "completed":
        raise ValueError("Syllabus processing must complete before question generation can start.")
    subject = database.get_subject_by_id(subject_id)
    if not subject: raise KeyError("Subject not found")
    sections = request.get("sections") or []
    if not sections: raise ValueError("At least one section is required.")
    section_names = [str(section.get("name", "")).strip().casefold() for section in sections]
    if any(not name for name in section_names) or len(set(section_names)) != len(section_names):
        raise ValueError("Section names must be non-empty and unique.")
    if any(not str(section.get(key, "")).strip() for section in sections for key in ("question_type", "difficulty")):
        raise ValueError("Each section needs a question type and difficulty.")
    # Full-syllabus generation is the default; legacy frontend selection fields
    # must not silently narrow coverage.
    scope = _resolve_generation_scope(syllabus, {})
    request["resolved_scope"] = scope
    request["resolved_hierarchy"] = _scope_hierarchy(scope)
    request["resolved_guidance"] = {
        _scope_key(item): _topic_generation_guidance(syllabus, item) for item in scope
    }
    raw_instructions = request.get("instructions")
    general = []
    if isinstance(raw_instructions, str):
        general = [line.strip() for line in raw_instructions.splitlines() if line.strip()]
    elif isinstance(raw_instructions, list):
        general = [str(line).strip() for line in raw_instructions if isinstance(line, str) and line.strip()]
    elif isinstance(raw_instructions, dict):
        for key in ("general_instructions", "general", "examination_instructions", "text"):
            value = raw_instructions.get(key)
            if isinstance(value, str) and value.strip():
                general.append(value.strip())
            elif isinstance(value, list):
                general.extend(str(line).strip() for line in value if isinstance(line, str) and line.strip())
    request["instructions"] = general
    total_count = sum(int(section["question_count"]) for section in sections)
    total_marks = sum(int(section["question_count"]) * int(section["marks_per_question"]) for section in sections)
    requested_marks = request.get("total_marks")
    if requested_marks is not None and int(requested_marks) != total_marks:
        raise ValueError("Total marks must match the section distribution.")
    if total_count < 1 or total_count > 300: raise ValueError("Total requested questions must be between 1 and 300.")
    request["calculated_total_questions"] = total_count
    request["calculated_total_marks"] = total_marks
    request_identity = _generation_request_identity(request)
    request["generation_context_id"] = uuid.uuid4().hex
    job_id, stamp = uuid.uuid4().hex, now()
    position = 0
    with database.connect() as db:
        active_result = db.execute("""SELECT job_id,request_json FROM question_generation_jobs
            WHERE created_by=? AND subject_id=? AND syllabus_id=? AND status IN ('queued','running')
            ORDER BY created_at DESC""", (user, subject_id, syllabus_id))
        active_jobs = active_result.fetchall() if hasattr(active_result, "fetchall") else []
        for active in active_jobs:
            try:
                if _generation_request_identity(json.loads(active["request_json"])) == request_identity:
                    return str(active["job_id"])
            except (TypeError, ValueError, KeyError):
                continue
        db.execute("""INSERT INTO question_generation_jobs(job_id,created_by,subject_id,syllabus_id,status,
            request_json,progress_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id,user,subject_id,syllabus_id,"queued",_safe_json(request),"{}",stamp,stamp))
        for section in sections:
            topics = scope
            if not topics:
                raise ValueError(f"Section {section['name']!r} has no selected syllabus topics.")
            question_count = int(section["question_count"])
            for _index in range(question_count):
                # Distribute questions across the whole paper so separate
                # exam sections do not all restart at the same syllabus topic.
                source_index = min(len(topics) - 1, ((2 * position + 1) * len(topics)) // (2 * total_count))
                source = topics[source_index]
                db.execute("""INSERT INTO question_generation_items(item_id,job_id,section,topic,marks,type,difficulty,
                    source_module,source_heading,position,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                    (uuid.uuid4().hex,job_id,str(section["name"]),source["topic"],int(section["marks_per_question"]),
                     str(section["question_type"]),str(section["difficulty"]),source["module_name"],source["heading"],position,stamp,stamp))
                position += 1
    return job_id


def _update_progress(job_id: str, *, status: str | None = None, error: str | None = None) -> None:
    stamp = now()
    with database.connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM question_generation_items WHERE job_id=? GROUP BY status", (job_id,)).fetchall()}
        total = sum(counts.values())
        completed = sum(counts.get(s, 0) for s in ("draft", "approved", "rejected"))
        progress = {"total": total, "queued": counts.get("queued", 0), "running": counts.get("running", 0),
                    "completed": completed, "failed": counts.get("failed", 0),
                    "percentage_completed": int(completed * 100 / total) if total else 0, "counts": counts}
        updates = "progress_json=?,updated_at=?"; params: list = [_safe_json(progress), stamp]
        if status is not None: updates += ",status=?"; params.append(status)
        updates += ",error=?"; params.append(error)
        params.append(job_id)
        db.execute(f"UPDATE question_generation_jobs SET {updates} WHERE job_id=?", params)


def _set_item_state(job_id: str, item_id: str, status: str, *, question: dict | None = None,
                    error: str | None = None, retries: int | None = None) -> dict:
    """Persist an item transition and matching progress snapshot atomically."""
    stamp = now()
    with database.connect() as db:
        allowed_statuses = "status IN ('queued','failed','running')" if status == "running" else (
            "status='running'" if status in {"draft", "failed"} else "1=1")
        db.execute(f"""UPDATE question_generation_items SET status=?,question_json=?,error=?,
            retries=COALESCE(?,retries),updated_at=? WHERE job_id=? AND item_id=? AND {allowed_statuses}""",
                   (status, _safe_json(question) if question is not None else None, error, retries,
                    stamp, job_id, item_id))
        counts = {row["status"]: row["n"] for row in db.execute(
            "SELECT status,COUNT(*) n FROM question_generation_items WHERE job_id=? GROUP BY status", (job_id,)).fetchall()}
        total = sum(counts.values())
        completed = sum(counts.get(s, 0) for s in ("draft", "approved", "rejected"))
        progress = {"total": total, "queued": counts.get("queued", 0), "running": counts.get("running", 0),
                    "completed": completed, "failed": counts.get("failed", 0),
                    "percentage_completed": int(completed * 100 / total) if total else 0, "counts": counts}
        db.execute("UPDATE question_generation_jobs SET progress_json=?,updated_at=? WHERE job_id=?",
                   (_safe_json(progress), stamp, job_id))
    return progress


async def _generate_batch(client, job: dict, syllabus: dict | None, items: list[dict], examples: list[dict],
                         avoid: list[str], lock: asyncio.Lock, diagnostics: dict | None = None) -> list[dict]:
    if not items:
        return []
    request = job.get("request")
    if not isinstance(request, dict):
        request = json.loads(job["request_json"]) if isinstance(job.get("request_json"), str) else {}
    all_scope = request.get("resolved_scope") or []
    selected_scope = all_scope
    bounded_examples = []
    example_chars = 0
    for example in examples:
        text = str(example.get("text", ""))[:1000]
        if example_chars + len(text) > 9000:
            break
        bounded_examples.append({**example, "text": text})
        example_chars += len(text)
    assignments = []
    for index, item in enumerate(items, start=1):
        guidance = request.get("resolved_guidance", {}).get(
            _scope_key({"module_name": item.get("source_module"), "heading": item.get("source_heading"),
                        "topic": item.get("topic")}))
        if guidance is None:
            guidance = _topic_generation_guidance(syllabus or {}, item)
        section = next((value for value in request.get("sections", [])
                        if str(value.get("name")) == str(item.get("section"))), {})
        assignments.append({"question_number": index, "module": item.get("source_module"),
            "section_heading": item.get("source_heading"), "topic": item["topic"],
            "section": item["section"], "section_instructions": section.get("instructions", ""),
            "marks": item["marks"], "question_type": item["type"], "difficulty": item["difficulty"],
            "syllabus_topic_guidance": guidance})
    task = {"generation_context_id": request.get("generation_context_id"),
            "subject": job["subject_name"],
            "selected_syllabus_scope": request.get("resolved_hierarchy") or selected_scope,
            "assignments": assignments,
            "examination": {key: request.get(key) for key in (
                "university_name", "college_name", "department_name", "paper_title", "examination_name",
                "academic_year", "semester", "exam_type", "subject_code", "examination_date", "duration",
                "total_marks", "instructions", "blueprint_id", "blueprint_name", "blueprint_description")},
            "previous_question_examples": bounded_examples, "existing_questions_to_avoid":[str(text)[:400] for text in avoid[-30:]]}
    if len(items) == 1:
        item = items[0]
        task.update({"selected_topic": {"module": item.get("source_module"),
            "section_heading": item.get("source_heading"), "topic": item["topic"]},
            "syllabus_topic_guidance": assignments[0]["syllabus_topic_guidance"],
            "section": item["section"], "section_instructions": assignments[0]["section_instructions"],
            "marks": item["marks"], "question_type": item["type"], "difficulty": item["difficulty"],
            "number_required": 1})
    system = (f"Generate exactly {len(items)} original exam questions as compact JSON. Use the complete supplied syllabus hierarchy "
              "for course context and follow each numbered assignment's module, section, topic, marks, type, difficulty, "
              'and instructions. Return only a JSON object with a "questions" array; array order must match question_number. '
              "Each entry must contain question_text and may contain sub_questions. Do not include answers, explanations, "
              "markdown, or text outside JSON.")
    preparation_duration = time.perf_counter() - (diagnostics.get("started_at", time.perf_counter()) if diagnostics else time.perf_counter())
    if diagnostics is not None:
        diagnostics["prompt_preparation_duration_seconds"] = preparation_duration
    # Retry ownership lives in _generate_one so transport and output failures
    # share one bounded per-question attempt budget.
    retry_limit = 0
    outcomes: list[dict | Exception | None] = [None] * len(items)
    pending = list(range(len(items)))
    feedback = None
    calls = 0
    for attempt in range(retry_limit + 1):
        if not pending:
            break
        attempt_diag = {"http_attempts": 0,
                        "generation_attempt": int((diagnostics or {}).get("generation_attempts", 0)) + 1}
        pending_items = [items[index] for index in pending]
        attempt_task = {**task, "assignments": [assignments[index] for index in pending]}
        if feedback:
            attempt_task["previous_attempt_feedback"] = feedback
        metrics_recorded = False
        try:
            token_budget = min(int(settings.QUESTION_GENERATION_MAX_TOKENS),
                               max(768, 512 + sum(min(1600, max(256, int(item["marks"]) * 80))
                                                   for item in pending_items)))
            calls += 1
            result = await _chat(client, system, attempt_task, max_tokens=token_budget,
                                 job_id=job.get("job_id"), item_id=pending_items[0].get("item_id"),
                                 diagnostics=attempt_diag, enable_thinking=False)
            if diagnostics is not None:
                diagnostics["http_attempts"] = diagnostics.get("http_attempts", 0) + attempt_diag["http_attempts"]
                diagnostics["generation_attempts"] = diagnostics.get("generation_attempts", 0) + calls
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if attempt_diag.get(key) is not None:
                        diagnostics[key] = (diagnostics.get(key) or 0) + attempt_diag[key]
            metrics_recorded = True
            raw_questions = result.get("questions")
            if not isinstance(raw_questions, list) or len(raw_questions) != len(pending):
                raise _InvalidModelOutput(f"Model returned the wrong question count: got "
                    f"{len(raw_questions) if isinstance(raw_questions, list) else 'no'}, expected {len(pending)}")
            validation_started = time.perf_counter()
            accepted: list[tuple[int, dict]] = []
            rejected: dict[int, Exception] = {}
            for index, raw in zip(pending, raw_questions):
                item = items[index]
                try:
                    question = _normalize_generated_question(raw, item=item, subject_name=job["subject_name"])
                    if (question["marks"] != item["marks"] or question["section"] != item["section"]
                            or question["subject"].casefold() != job["subject_name"].casefold()
                            or question["type"].casefold() != item["type"].casefold()
                            or question["difficulty"].casefold() != item["difficulty"].casefold()
                            or question["topic"].casefold() != item["topic"].casefold()):
                        raise _InvalidModelOutput("Question does not match its assigned section, topic, marks, type, or difficulty")
                    accepted.append((index, question))
                except (ValueError, ValidationError) as exc:
                    rejected[index] = exc
            async with lock:
                accepted_texts: list[str] = []
                for index, question in accepted:
                    if _is_duplicate(question["question_text"], [*avoid, *accepted_texts]):
                        rejected[index] = _InvalidModelOutput("Generated question is a duplicate")
                    else:
                        outcomes[index] = question
                        accepted_texts.append(question["question_text"])
                avoid.extend(accepted_texts)
            _generation_event("question_validated", job_id=job.get("job_id"), item_id=pending_items[0].get("item_id"),
                item_count=len(pending), accepted_count=len(accepted_texts), rejected_count=len(rejected),
                attempt=attempt + 1, duration_seconds=round(time.perf_counter() - validation_started, 3))
            pending = list(rejected)
            if pending and attempt < retry_limit:
                feedback = "; ".join(f"{items[index]['topic']}: {_generation_error_detail(error)}"
                                      for index, error in rejected.items())[:700]
                _generation_event("output_validation_retry", job_id=job.get("job_id"),
                    item_ids=[items[index].get("item_id") for index in pending], item_count=len(pending),
                    attempt=attempt + 1, retry_reason=feedback, retrying=True)
                await asyncio.sleep(min(settings.QUESTION_GENERATION_BACKOFF_BASE * (2 ** attempt), 4.0))
            elif pending:
                for index, error in rejected.items():
                    outcomes[index] = error
                pending = []
        except Exception as exc:
            if not metrics_recorded and diagnostics is not None:
                diagnostics["http_attempts"] = diagnostics.get("http_attempts", 0) + attempt_diag["http_attempts"]
                diagnostics["generation_attempts"] = diagnostics.get("generation_attempts", 0) + calls
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if attempt_diag.get(key) is not None:
                        diagnostics[key] = (diagnostics.get(key) or 0) + attempt_diag[key]
            if attempt < retry_limit and isinstance(exc, _InvalidModelOutput):
                feedback = _generation_error_detail(exc)
                _generation_event("output_validation_retry", job_id=job.get("job_id"),
                    item_ids=[items[index].get("item_id") for index in pending], item_count=len(pending),
                    attempt=attempt + 1, retry_reason=feedback, retrying=True)
                await asyncio.sleep(min(settings.QUESTION_GENERATION_BACKOFF_BASE * (2 ** attempt), 4.0))
            else:
                failure = exc if isinstance(exc, (_InvalidModelOutput, _GenerationRequestError, ValidationError)) else RuntimeError(
                    f"Generation request failed: {type(exc).__name__}: {_generation_error_detail(exc)}")
                for index in pending:
                    outcomes[index] = failure
                pending = []
    for index in pending:
        outcomes[index] = RuntimeError("Generation failed after bounded retries.")
    return [outcome if outcome is not None else RuntimeError("Generation produced no question.") for outcome in outcomes]


async def _generate_one(client, job: dict, syllabus: dict | None, item: dict, examples: list[dict],
                        avoid: list[str], lock: asyncio.Lock, diagnostics: dict | None = None,
                        on_retry=None) -> dict:
    """Generate one question with a strict initial-plus-two-retries budget."""
    max_retries = min(2, max(0, int(settings.QUESTION_GENERATION_MAX_RETRIES)))
    last_error: Exception | None = None
    for retry_index in range(max_retries + 1):
        try:
            result = (await _generate_batch(client, job, syllabus, [item], examples, avoid, lock, diagnostics))[0]
            if isinstance(result, Exception):
                raise result
            return result
        except Exception as exc:
            last_error = exc
            if isinstance(exc, _GenerationRequestError):
                retryable = exc.retryable
                category = exc.category
                retry_after = exc.retry_after
            elif isinstance(exc, (_InvalidModelOutput, ValidationError)):
                retryable, category, retry_after = True, "invalid_output" if isinstance(exc, _InvalidModelOutput) else "validation_error", None
            else:
                retryable, category, retry_after = False, "permanent_request_error", None
            if not retryable or retry_index >= max_retries:
                if diagnostics is not None:
                    diagnostics["failure_category"] = category
                    diagnostics["failure_reason"] = _generation_error_detail(exc)
                raise
            next_retry = retry_index + 1
            backoff = min(settings.QUESTION_GENERATION_BACKOFF_BASE * (2 ** retry_index), 8.0)
            delay = retry_after if category == "rate_limit" and retry_after is not None else (
                backoff if category in {"rate_limit", "network_error", "timeout", "server_error"} else 0.0)
            _generation_event("question_retry_scheduled", job_id=job.get("job_id"), item_id=item.get("item_id"),
                retry=next_retry, max_retries=max_retries, category=category,
                retry_reason=_generation_error_detail(exc), backoff_seconds=round(delay, 3))
            if diagnostics is not None:
                diagnostics["retries_used"] = next_retry
                diagnostics["failure_category"] = category
                diagnostics["failure_reason"] = _generation_error_detail(exc)
            if on_retry is not None:
                await on_retry(next_retry, category, exc, delay)
            if delay:
                await asyncio.sleep(delay)
    raise last_error or RuntimeError("Question generation failed without an error detail.")


def _load_generation_rows(job_id: str):
    with database.connect() as db:
        job_row = db.execute("""SELECT j.*,s.subject_name FROM question_generation_jobs j
            JOIN subjects s ON s.subject_id=j.subject_id WHERE job_id=?""", (job_id,)).fetchone()
        items = [dict(r) for r in db.execute(
            "SELECT * FROM question_generation_items WHERE job_id=? AND status IN ('queued','failed') ORDER BY position,item_id",
            (job_id,)).fetchall()]
        saved_questions = [json.loads(r["question_json"]).get("question_text", "") for r in db.execute(
            "SELECT question_json FROM question_generation_items WHERE job_id=? AND status IN ('draft','approved') AND question_json IS NOT NULL",
            (job_id,)).fetchall()]
    return dict(job_row) if job_row else None, items, saved_questions


def _job_state(job_id: str) -> str | None:
    with database.connect() as db:
        row = db.execute("SELECT status FROM question_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
    return row["status"] if row else None


def _claim_job(job_id: str) -> bool:
    """Allow only one worker to execute a queued job, including across processes."""
    with database.connect() as db:
        result = db.execute("UPDATE question_generation_jobs SET status='running',updated_at=? "
                            "WHERE job_id=? AND status='queued'", (now(), job_id))
        return bool(result.rowcount)


def _cancel_queued_items(job_id: str) -> None:
    with database.connect() as db:
        db.execute("UPDATE question_generation_items SET status='cancelled',updated_at=? WHERE job_id=? AND status IN ('queued','failed')",
                   (now(), job_id))


def _job_item_counts(job_id: str) -> tuple[dict[str, int], str | None]:
    with database.connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute(
            "SELECT status,COUNT(*) n FROM question_generation_items WHERE job_id=? GROUP BY status", (job_id,)).fetchall()}
        row = db.execute("SELECT status FROM question_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
    return counts, row["status"] if row else None


def _set_docx_error(job_id: str, error: str | None) -> None:
    with database.connect() as db:
        db.execute("UPDATE question_generation_jobs SET docx_error=?,updated_at=? WHERE job_id=?",
                   (error, now(), job_id))


async def run_job(job_id: str) -> None:
    client = None
    duplicate_lock = asyncio.Lock()
    progress_lock = asyncio.Lock()
    job_started = time.perf_counter()
    _generation_event("job_worker_started", job_id=job_id)
    try:
        if not await asyncio.to_thread(_claim_job, job_id):
            _generation_event("job_worker_skipped", job_id=job_id, reason="job is not queued or was claimed by another worker")
            return
        client = _nvidia_client()
        context_load_started = time.perf_counter()
        job, items, saved_questions = await asyncio.to_thread(_load_generation_rows, job_id)
        context_load_duration = time.perf_counter() - context_load_started
        if not job:
            raise KeyError("Generation job not found")
        request = json.loads(job["request_json"])
        job["request"] = request
        items.sort(key=lambda item: (item["section"], item["topic"], item["marks"], item["type"], item["difficulty"], item["position"]))
        sections = {str(section["name"]): section for section in request["sections"]}
        for item in items:
            item["instructions"] = sections.get(item["section"], {}).get("instructions", "")

        examples_started = time.perf_counter()
        examples, existing = await asyncio.to_thread(
            _existing_examples, job["subject_name"], [item["topic"] for item in items], [item["marks"] for item in items])
        _generation_event("worker_context_loaded", job_id=job_id, item_count=len(items),
                          database_read_duration_seconds=round(context_load_duration, 3),
                          duplicate_lookup_duration_seconds=round(time.perf_counter() - examples_started, 3))
        avoid = [*existing, *saved_questions]
        current = await asyncio.to_thread(_job_state, job_id)
        if current == "cancelling":
            await asyncio.to_thread(_cancel_queued_items, job_id)
            await asyncio.to_thread(_update_progress, job_id, status="cancelled")
            _generation_event("job_worker_finished", job_id=job_id, status="cancelled",
                              duration_seconds=round(time.perf_counter() - job_started, 3))
            return
        await asyncio.to_thread(_update_progress, job_id)
        semaphore = asyncio.Semaphore(settings.QUESTION_GENERATION_CONCURRENCY)

        async def change_item(item: dict, status: str, *, question: dict | None = None,
                              error: str | None = None, retries: int | None = None) -> dict:
            async with progress_lock:
                return await asyncio.to_thread(_set_item_state, job_id, item["item_id"], status,
                                               question=question, error=error, retries=retries)

        async def process_item(item):
            async with semaphore:
                state = await asyncio.to_thread(_job_state, job_id)
                if not state or state == "cancelling":
                    async with progress_lock:
                        await asyncio.to_thread(_cancel_queued_items, job_id)
                        await asyncio.to_thread(_update_progress, job_id)
                    return
                item_started = time.perf_counter()
                await change_item(item, "running", retries=item.get("retries", 0))
                _generation_event("item_started", job_id=job_id, item_id=item["item_id"], timestamp=now(),
                                  concurrency=settings.QUESTION_GENERATION_CONCURRENCY)
                diagnostics = {"started_at": item_started, "http_attempts": 0, "generation_attempts": 0}
                previous_retries = int(item.get("retries", 0))

                async def record_retry(retry_number, category, exc, delay):
                    reason = f"{category}: {_generation_error_detail(exc)}; retry {retry_number}/{settings.QUESTION_GENERATION_MAX_RETRIES}"
                    progress = await change_item(item, "running", error=reason,
                                                 retries=previous_retries + retry_number)
                    _generation_event("question_retry_progress", job_id=job_id, item_id=item["item_id"],
                        retry=retry_number, category=category, backoff_seconds=round(delay, 3),
                        retry_reason=_generation_error_detail(exc), progress=progress)
                try:
                    question = await _generate_one(client, job, None, item, examples, avoid,
                                                   duplicate_lock, diagnostics, on_retry=record_retry)
                except Exception as exc:
                    logger.warning("Question generation exhausted job_id=%s item_id=%s category=%s reason=%s",
                                   job_id, item["item_id"], diagnostics.get("failure_category", "unknown"),
                                   _generation_error_detail(exc))
                    failure_category = diagnostics.get("failure_category", "unknown")
                    failure_reason = diagnostics.get("failure_reason", _generation_error_detail(exc))
                    progress = await change_item(item, "failed", error=f"{failure_category}: {failure_reason}",
                        retries=previous_retries + diagnostics.get("retries_used", 0))
                    _generation_event("item_finished", job_id=job_id, item_id=item["item_id"],
                        status="failed", failure_category=failure_category,
                        retry_reason=failure_reason, progress=progress,
                        total_duration_seconds=round(time.perf_counter() - item_started, 3))
                    return
                _generation_event("item_model_finished", job_id=job_id, item_id=item["item_id"],
                    http_attempts=diagnostics.get("http_attempts", 0),
                    generation_attempts=diagnostics.get("generation_attempts", 0),
                    prompt_preparation_duration_seconds=round(diagnostics.get("prompt_preparation_duration_seconds", 0), 3),
                    prompt_tokens=diagnostics.get("prompt_tokens"),
                    completion_tokens=diagnostics.get("completion_tokens"),
                    total_tokens=diagnostics.get("total_tokens"),
                    duration_seconds=round(time.perf_counter() - item_started, 3))
                persist_started = time.perf_counter()
                try:
                    progress = await change_item(item, "draft", question=question,
                        retries=previous_retries + diagnostics.get("retries_used", 0), error=None)
                    _generation_event("item_finished", job_id=job_id, item_id=item["item_id"],
                        status="draft", database_persistence_duration_seconds=round(time.perf_counter() - persist_started, 3),
                        total_duration_seconds=round(time.perf_counter() - item_started, 3), progress=progress)
                except Exception as exc:
                    logger.exception("Could not persist generated question job_id=%s item_id=%s", job_id, item["item_id"])
                    progress = await change_item(item, "failed", error=f"Could not persist question: {exc}")
                    _generation_event("item_finished", job_id=job_id, item_id=item["item_id"],
                        status="failed", retry_reason=str(exc)[:400], progress=progress)

        await asyncio.gather(*(process_item(item) for item in items))
        counts, final_state = await asyncio.to_thread(_job_item_counts, job_id)
        remaining = counts.get("queued", 0) + counts.get("running", 0)
        failed = counts.get("failed", 0)
        if final_state == "cancelling":
            await asyncio.to_thread(_update_progress, job_id, status="cancelled")
        elif remaining:
            await asyncio.to_thread(_update_progress, job_id, status="partial",
                                    error="Generation stopped before every requested question completed; retry the pending questions.")
        elif failed:
            total_items = sum(counts.values())
            status = "failed" if failed == total_items else "partial"
            await asyncio.to_thread(_update_progress, job_id, status=status,
                                    error=f"{failed} of {total_items} requested questions failed. Retry the failed items.")
        else:
            # Pollers commonly stop when they see "completed". Keep the job
            # running until its download is actually persisted.
            await asyncio.to_thread(_update_progress, job_id, status="running", error=None)
            await asyncio.to_thread(_set_docx_error, job_id, None)
            docx_started = time.perf_counter()
            try:
                await asyncio.to_thread(render_job_docx, job_id)
                await asyncio.to_thread(_update_progress, job_id, status="completed", error=None)
                _generation_event("job_docx_ready", job_id=job_id,
                                  duration_seconds=round(time.perf_counter() - docx_started, 3))
            except Exception as exc:
                logger.exception("Could not automatically create completed question paper DOCX job_id=%s", job_id)
                message = f"DOCX generation failed: {type(exc).__name__}: {exc}"
                await asyncio.to_thread(_set_docx_error, job_id, message)
                await asyncio.to_thread(_update_progress, job_id, status="failed", error=message)
                _generation_event("job_docx_failed", job_id=job_id,
                                  duration_seconds=round(time.perf_counter() - docx_started, 3),
                                  error=str(exc)[:400])
        final = await asyncio.to_thread(_job_state, job_id)
        _generation_event("job_worker_finished", job_id=job_id, status=final, item_counts=counts,
                          duration_seconds=round(time.perf_counter() - job_started, 3))
    except Exception as exc:
        logger.exception("Question generation job %s failed", job_id)
        await asyncio.to_thread(_update_progress, job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        _generation_event("job_worker_failed", job_id=job_id, status="failed", exception=type(exc).__name__,
                          error=str(exc)[:400], duration_seconds=round(time.perf_counter() - job_started, 3))
    finally:
        if client is not None:
            await client.close()


def start_job(job_id: str) -> None:
    with _job_locks_guard:
        lock = _job_locks.setdefault(job_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        asyncio.run(run_job(job_id))
    finally:
        lock.release()


def retry_job(job_id: str) -> bool:
    stamp = now()
    with database.connect() as db:
        claimed = db.execute("UPDATE question_generation_jobs SET status='queued',error=NULL,updated_at=? "
                             "WHERE job_id=? AND status IN ('failed','partial','cancelled')", (stamp, job_id))
        if not claimed.rowcount:
            return False
        db.execute("UPDATE question_generation_items SET status='queued',error=NULL,updated_at=? WHERE job_id=? AND status IN ('failed','queued','running')", (stamp,job_id))
    _update_progress(job_id, status="queued", error=None)
    return True


def cancel_job(job_id: str) -> bool:
    stamp = now()
    with database.connect() as db:
        cur = db.execute("UPDATE question_generation_jobs SET status='cancelling',updated_at=? WHERE job_id=? AND status IN ('queued','running','partial')", (stamp,job_id))
        db.execute("UPDATE question_generation_items SET status='cancelled',updated_at=? WHERE job_id=? AND status IN ('queued','failed')", (stamp,job_id))
    if cur.rowcount:
        _update_progress(job_id, status="cancelling")
    return cur.rowcount > 0


def recover_interrupted_jobs() -> None:
    """Expose interrupted in-process work as retryable without touching saved drafts."""
    stamp = now()
    with database.connect() as db:
        jobs = [row["job_id"] for row in db.execute("SELECT job_id FROM question_generation_jobs WHERE status IN ('queued','running','cancelling')").fetchall()]
        db.execute("UPDATE question_generation_items SET status='queued',updated_at=? WHERE status='running' AND job_id IN (SELECT job_id FROM question_generation_jobs WHERE status IN ('queued','running','cancelling'))", (stamp,))
        db.execute("UPDATE question_generation_jobs SET status='partial',error='Worker stopped before generation completed; retry pending questions.',updated_at=? WHERE status IN ('queued','running','cancelling')", (stamp,))
    for job_id in jobs:
        _update_progress(job_id, status="partial", error="Worker stopped before generation completed; retry pending questions.")


def get_job(job_id: str) -> dict | None:
    with database.connect() as db:
        row = db.execute("SELECT * FROM question_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row: return None
        job = dict(row)
        docx_available = job.get("docx_blob") is not None
        rows = db.execute("SELECT * FROM question_generation_items WHERE job_id=? ORDER BY position,item_id", (job_id,)).fetchall()
    job.pop("docx_blob", None)
    job["request"] = json.loads(job.pop("request_json"))
    stored_progress = json.loads(job.pop("progress_json"))
    job["items"] = []
    counts: dict[str, int] = {}
    for row in rows:
        item = dict(row)
        item["question"] = json.loads(item.pop("question_json")) if item.get("question_json") else None
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        job["items"].append(item)
    total = len(job["items"])
    completed = sum(counts.get(state, 0) for state in ("draft", "approved", "rejected"))
    job["progress"] = {**stored_progress, "total": total, "queued": counts.get("queued", 0),
                        "running": counts.get("running", 0), "completed": completed,
                        "failed": counts.get("failed", 0),
                        "percentage_completed": int(completed * 100 / total) if total else 0,
                        "counts": counts}
    job.update({"total_questions": total, "queued_questions": counts.get("queued", 0),
                "running_questions": counts.get("running", 0), "completed_questions": completed,
                "failed_questions": counts.get("failed", 0),
                "percentage_completed": int(completed * 100 / total) if total else 0,
                "docx_ready": docx_available,
                "docx_error": job.get("docx_error"),
                "docx_url": f"/api/smart-qpg/generation-jobs/{job_id}/docx" if docx_available else None})
    return job


def edit_item(job_id: str, item_id: str, question: dict | None) -> bool:
    stamp = now()
    if question is not None:
        with database.connect() as db:
            item = db.execute("""SELECT i.section,i.topic,i.marks,i.type,i.difficulty,i.source_module,i.source_heading,
                j.subject_id,s.subject_name
                FROM question_generation_items i JOIN question_generation_jobs j ON j.job_id=i.job_id
                JOIN subjects s ON s.subject_id=j.subject_id WHERE i.job_id=? AND i.item_id=?""", (job_id,item_id)).fetchone()
            siblings = db.execute("SELECT question_json FROM question_generation_items WHERE job_id=? AND item_id<>? AND status IN ('draft','approved')", (job_id,item_id)).fetchall()
        if not item:
            return False
        if (question.get("section") != item["section"] or question.get("topic", "").casefold() != item["topic"].casefold()
                or question.get("marks") != item["marks"] or question.get("type", "").casefold() != item["type"].casefold()
                or question.get("difficulty", "").casefold() != item["difficulty"].casefold()
                or question.get("subject", "").casefold() != item["subject_name"].casefold()):
            raise ValueError("Editing may change wording and subquestions, while section, topic, marks, type and difficulty must stay as requested.")
        question["source_module"] = item["source_module"]
        question["source_heading"] = item["source_heading"]
        _, bank_existing = _existing_examples(item["subject_name"], [item["topic"]], [item["marks"]])
        sibling_texts = [json.loads(row["question_json"]).get("question_text", "") for row in siblings if row["question_json"]]
        if _is_duplicate(question["question_text"], bank_existing + sibling_texts):
            raise ValueError("Edited question duplicates an existing or generated question.")
    with database.connect() as db:
        cur = db.execute("UPDATE question_generation_items SET question_json=?,status=?,updated_at=? WHERE job_id=? AND item_id=? AND status IN ('draft','approved')",
                         (_safe_json(question) if question else None, "draft" if question else "rejected",stamp,job_id,item_id))
        if cur.rowcount:
            db.execute("UPDATE question_generation_jobs SET docx_blob=NULL,updated_at=? WHERE job_id=?",(stamp,job_id))
            db.execute("DELETE FROM generated_papers WHERE paper_id=?", (job_id,))
        changed = cur.rowcount > 0
    if changed:
        _update_progress(job_id)
    return changed


def approve_items(job_id: str, item_ids: list[str]) -> int:
    if not item_ids: return 0
    stamp = now(); item_ids = list(dict.fromkeys(item_ids))
    with database.connect() as db:
        placeholders = ",".join("?" for _ in item_ids)
        rows = db.execute(f"SELECT item_id FROM question_generation_items WHERE job_id=? AND status='draft' AND item_id IN ({placeholders})", (job_id,*item_ids)).fetchall()
        if len(rows) != len(item_ids):
            return -1
        for item_id in item_ids:
            db.execute("UPDATE question_generation_items SET status='approved',updated_at=? WHERE job_id=? AND item_id=? AND status='draft'", (stamp,job_id,item_id))
        db.execute("UPDATE question_generation_jobs SET docx_blob=NULL,updated_at=? WHERE job_id=?",(stamp,job_id))
        db.execute("DELETE FROM generated_papers WHERE paper_id=?", (job_id,))
    _update_progress(job_id)
    return len(item_ids)


def _load_regeneration_data(job_id: str, item_id: str):
    with database.connect() as db:
        row = db.execute("""SELECT i.*,j.request_json,j.syllabus_id,j.subject_id,s.subject_name
            FROM question_generation_items i JOIN question_generation_jobs j ON j.job_id=i.job_id
            JOIN subjects s ON s.subject_id=j.subject_id WHERE i.job_id=? AND i.item_id=?""", (job_id,item_id)).fetchone()
        accepted = db.execute("SELECT question_json FROM question_generation_items WHERE job_id=? AND status IN ('draft','approved') AND item_id<>?",(job_id,item_id)).fetchall()
    return (dict(row) if row else None), [dict(entry) for entry in accepted]


def _persist_regenerated_question(job_id: str, item_id: str, question: dict) -> None:
    stamp = now()
    with database.connect() as db:
        db.execute("UPDATE question_generation_items SET question_json=?,status='draft',error=NULL,retries=retries+1,updated_at=? WHERE job_id=? AND item_id=?",
                   (_safe_json(question), stamp, job_id, item_id))
        db.execute("UPDATE question_generation_jobs SET docx_blob=NULL,updated_at=? WHERE job_id=?", (stamp, job_id))
        db.execute("DELETE FROM generated_papers WHERE paper_id=?", (job_id,))


async def regenerate_item(job_id: str, item_id: str) -> dict:
    started = time.perf_counter()
    _generation_event("item_started", job_id=job_id, item_id=item_id, timestamp=now(), operation="regenerate")
    item, accepted = await asyncio.to_thread(_load_regeneration_data, job_id, item_id)
    if not item:
        raise KeyError("Generation item not found")
    request = json.loads(item["request_json"])
    section = next((s for s in request.get("sections", []) if s.get("name") == item["section"]), {})
    item["instructions"] = section.get("instructions", "")
    existing = [json.loads(r["question_json"])["question_text"] for r in accepted if r["question_json"]]
    if item.get("question_json"):
        try:
            existing.append(json.loads(item["question_json"]).get("question_text", ""))
        except (TypeError, ValueError):
            pass
    examples, old_questions = await asyncio.to_thread(
        _existing_examples, item["subject_name"], [item["topic"]], [item["marks"]])
    avoid = old_questions + existing
    client = _nvidia_client()
    diagnostics = {"started_at": time.perf_counter(), "http_attempts": 0, "generation_attempts": 0}
    try:
        job = {"job_id": job_id, "subject_name": item["subject_name"], "request_json": item["request_json"]}
        question = await _generate_one(client, job, None, item, examples, avoid, asyncio.Lock(), diagnostics)
        persist_started = time.perf_counter()
        await asyncio.to_thread(_persist_regenerated_question, job_id, item_id, question)
        await asyncio.to_thread(_update_progress, job_id)
        _generation_event("item_finished", job_id=job_id, item_id=item_id, status="draft",
                          http_attempts=diagnostics.get("http_attempts", 0),
                          generation_attempts=diagnostics.get("generation_attempts", 0),
                          database_persistence_duration_seconds=round(time.perf_counter() - persist_started, 3),
                          total_duration_seconds=round(time.perf_counter() - started, 3))
        return question
    except Exception as exc:
        _generation_event("item_finished", job_id=job_id, item_id=item_id, status="failed",
                          http_attempts=diagnostics.get("http_attempts", 0),
                          generation_attempts=diagnostics.get("generation_attempts", 0),
                          retry_reason=_generation_error_detail(exc),
                          total_duration_seconds=round(time.perf_counter() - started, 3))
        raise
    finally:
        await client.close()


def render_job_docx(job_id: str) -> bytes:
    job = get_job(job_id)
    if not job: raise KeyError("Generation job not found")
    request = job["request"]
    expected_count = int(request.get("calculated_total_questions") or 0)
    generated_items = [item for item in job["items"] if item["status"] in {"draft", "approved"} and item.get("question")]
    if len(generated_items) != expected_count or len(job["items"]) != expected_count:
        raise ValueError("Every requested question must be generated successfully before the complete paper is available.")
    validated = []
    for item in generated_items:
        question = GeneratedQuestion.model_validate(item["question"]).model_dump()
        expected = {"section": item["section"], "topic": item["topic"], "marks": item["marks"],
                    "type": item["type"], "difficulty": item["difficulty"],
                    "source_module": item.get("source_module", ""), "source_heading": item.get("source_heading", "")}
        for key, value in expected.items():
            if question.get(key) != value:
                raise ValueError(f"Approved question no longer matches its requested {key}.")
        validated.append((item, question))
    if sum(question["marks"] for _, question in validated) != int(request["calculated_total_marks"]):
        raise ValueError("Approved question marks do not match the configured total.")

    subject = database.get_subject_by_id(job["subject_id"])
    if not subject:
        raise ValueError("The generation job's subject is no longer available.")
    grouped: dict[str, list[dict]] = {}
    for item, question in validated:
        grouped.setdefault(item["section"], []).append({
            "text": question["question_text"], "marks": question["marks"],
            "sub_questions": question.get("sub_questions", []),
            "source_module": question.get("source_module"), "source_heading": question.get("source_heading"),
            "source_topic": question.get("topic"),
        })
    section_config = {str(section["name"]): section for section in request.get("sections", [])}
    sections = []
    for section in request.get("sections", []):
        name = str(section["name"])
        questions = grouped.get(name, [])
        expected_section_count = int(section["question_count"])
        expected_section_marks = expected_section_count * int(section["marks_per_question"])
        if len(questions) != expected_section_count or sum(q["marks"] for q in questions) != expected_section_marks:
            raise ValueError(f"Section {name!r} does not match its configured question count and marks.")
        sections.append({"name": name, "instructions": section.get("instructions", ""),
                         "questions": questions, "question_count": expected_section_count,
                         "marks": expected_section_marks})
    instructions = request.get("instructions") or []
    if isinstance(instructions, str):
        instructions = [line.strip() for line in instructions.splitlines() if line.strip()]
    else:
        instructions = [str(line).strip() for line in instructions if str(line).strip()]
    paper_id = job_id
    paper = {
        "paper_id": paper_id, "generation_job_id": job_id, "syllabus_id": job["syllabus_id"],
        "configuration_id": request.get("configuration_id"),
        "title": request.get("paper_title") or request.get("examination_name") or "Generated Question Paper",
        "paper_title": request.get("paper_title") or request.get("examination_name") or "Generated Question Paper",
        "subject": subject["subject_name"], "subject_id": job["subject_id"],
        "subject_name": subject["subject_name"], "subject_code": request.get("subject_code"),
        "exam_type": request.get("exam_type") or "Examination",
        "examination_name": request.get("examination_name"),
        "university_name": request.get("university_name"), "college_name": request.get("college_name"),
        "department_name": request.get("department_name"), "academic_year": request.get("academic_year"),
        "semester": request.get("semester"), "examination_date": request.get("examination_date"),
        "duration": request.get("duration") or "", "requested_total_marks": int(request["calculated_total_marks"]),
        "total_marks": int(request["calculated_total_marks"]), "sections": sections,
        "instructions": instructions, "blueprint_id": request.get("blueprint_id"),
        "blueprint_name": request.get("blueprint_name"), "generation_status": "generated",
        "blueprint_description": request.get("blueprint_description"),
        "generation_request": request,
        "generated_by": job["created_by"], "generated_at": now(),
        "docx_url": f"/api/smart-qpg/generation-jobs/{job_id}/docx",
        "show_section_totals": True, "show_total_marks": True,
        "include_processing_accuracy_notice": False,
    }
    request = job["request"]
    from src.masterdeorca.services.question_papers import _docx
    from docx import Document
    with tempfile.TemporaryDirectory(prefix="smart-qpg-docx-") as temporary:
        target = Path(temporary) / "generated.docx"
        _docx(paper,target)
        content = target.read_bytes()
    document = Document(io.BytesIO(content))
    paragraph_texts = [paragraph.text for paragraph in document.paragraphs]
    document_text = "\n".join(paragraph_texts)
    numbered = [int(match.group(1)) for text in paragraph_texts
                if (match := re.match(r"^(\d+)\. ", text))]
    if numbered != list(range(1, expected_count + 1)):
        raise ValueError("The rendered DOCX question count does not match the generated paper.")
    for required in (paper["title"], subject["subject_name"], f"Total marks: {paper['total_marks']}"):
        if required not in document_text:
            raise ValueError(f"The rendered DOCX is missing required paper content: {required}")
    expected_headers = [section["name"].upper() if section["name"].upper().startswith("SECTION ")
                        else f"SECTION {section['name']}".upper() for section in sections]
    header_positions = []
    for header in expected_headers:
        try:
            header_positions.append(next(index for index, text in enumerate(paragraph_texts)
                                         if text.strip().casefold() == header.casefold()))
        except StopIteration:
            header_positions.append(-1)
    if -1 in header_positions or header_positions != sorted(header_positions):
        raise ValueError("The rendered DOCX does not contain every configured section.")
    for _, question in validated:
        if question["question_text"] not in document_text or f"[{question['marks']} Marks]" not in document_text:
            raise ValueError("The rendered DOCX is missing a validated question or its marks.")
    for section in sections:
        expected_total = f"Section total: {section['marks']} marks"
        if expected_total not in document_text:
            raise ValueError(f"The rendered DOCX is missing the total for section {section['name']!r}.")
    for key in ("university_name", "college_name", "department_name", "subject_code", "academic_year",
                "semester", "examination_date", "duration", "examination_name"):
        value = paper.get(key)
        if value and str(value) not in document_text:
            raise ValueError(f"The rendered DOCX is missing configured examination metadata: {key}.")
    if paper["exam_type"] and paper["examination_name"] and paper["exam_type"].casefold() != paper["examination_name"].casefold():
        if f"Examination type: {paper['exam_type']}" not in document_text:
            raise ValueError("The rendered DOCX is missing the configured examination type.")
    for instruction in instructions:
        if instruction not in document_text:
            raise ValueError("The rendered DOCX is missing a general examination instruction.")
    with database.connect() as db:
        stamp = now()
        db.execute("""INSERT OR REPLACE INTO generated_papers(
            paper_id,subject_name,storage_key,paper_json,docx_blob,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (paper_id, subject["subject_name"], f"{paper_id}.docx", _safe_json(paper), content, stamp, stamp))
        db.execute("UPDATE question_generation_jobs SET docx_blob=?,docx_error=NULL,updated_at=? WHERE job_id=?", (content, stamp, job_id))
        db.commit()
    return content

