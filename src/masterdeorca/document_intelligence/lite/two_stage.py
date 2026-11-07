from __future__ import annotations

import hashlib
import io
import inspect
import json
import logging
import os
import re
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from config import settings
from pydantic import ValidationError
from functions.QuesParser.ParsePipeLIne.schema import QuestionBank, generate_question_id
from src.masterdeorca.document_intelligence.interfaces.base import (
    DocumentResource,
    ExtractedDocument,
)
from src.masterdeorca.document_intelligence.lite.figure_assets import crop_figure_assets
from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_normalizer import (
    NemotronNormalizer,
)
from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import (
    NemotronParseClient,
)
from src.masterdeorca.document_intelligence.page_images import render_page_images

logger = logging.getLogger(__name__)


def _concurrency() -> int:
    try:
        value = int(getattr(settings, "LITE_STAGE2_CONCURRENCY", 3))
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 8))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unstructured_source_text(question: dict[str, Any]) -> str:
    parts = [str(question.get("text") or question.get("original_text") or "").strip()]

    def append_children(children: Any, depth: int = 0) -> None:
        if not isinstance(children, list):
            return
        for child in children:
            if not isinstance(child, dict):
                continue
            label = child.get("original_question_number") or child.get("label")
            text = str(child.get("text") or child.get("original_text") or "").strip()
            prefix = f"{label}: " if label else ""
            parts.append(f"{'  ' * depth}{prefix}{text}".rstrip())
            append_children(child.get("sub_questions") or child.get("subquestions"), depth + 1)

    append_children(question.get("sub_questions") or question.get("subquestions"))
    return "\n".join(part for part in parts if part)


def _atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp_name = temp.name
            json.dump(data, temp, indent=2, ensure_ascii=False)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()


_BLOCK_MARKER = re.compile(
    r"<x_([-+\d.eE]+)><y_([-+\d.eE]+)>(.*?)"
    r"<x_([-+\d.eE]+)><y_([-+\d.eE]+)><class_([^>]+)>",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_QUESTION_ROW = re.compile(
    r"^\s*Q\s*\.?\s*(\d+)\s*(?:\(([a-z0-9ivxlcdm]+)\))?"
    r"(?:\s*(?:\||&)\s*|\s+)",
    re.IGNORECASE | re.MULTILINE,
)
_LATEX_TABULAR = re.compile(
    r"\\begin\{tabular\}\{[^}]*\}(.*?)\\end\{tabular\}",
    re.IGNORECASE | re.DOTALL,
)

_SOURCE_QUESTION_PREFIX = re.compile(
    r"^\s*(?:(?:question)\s*(?:no\.?\s*)?|q\s*\.?\s*(?:no\.?\s*)?)"
    r"\d+\s*(?:\([a-z0-9ivxlcdm]+\))?\s*[:.)\-–—]?\s*",
    re.IGNORECASE,
)
_FENCED_CODE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)
_INLINE_CODE = re.compile(r"(`+[^`\n]*?`+)")
_CODE_SYNTAX = re.compile(
    r"(?m)^\s*(?:#include\b|(?:import|from)\s+\w|"
    r"(?:def|class|return|if|for|while|switch)\b.*[({:;]|"
    r"[\w\]\)]\s*(?:=|\+=|-=|:=)\s*[^=])"
)


def _looks_like_code(text: str) -> bool:
    return bool(_FENCED_CODE.search(text) or _CODE_SYNTAX.search(text))


def _normalize_text_whitespace(text: str, *, preserve_code: bool = False) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if preserve_code:
        return text.strip()

    def normalize_prose(prose: str) -> str:
        lines = [
            re.sub(r"[^\S\n]+", " ", line).rstrip()
            for line in prose.split("\n")
        ]
        normalized = "\n".join(lines)
        normalized = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", normalized)
        return normalized

    # Keep code spans untouched while cleaning OCR whitespace around them.
    code_tokens: list[str] = []

    def protect_code(match: re.Match[str]) -> str:
        code_tokens.append(match.group(0))
        return f"\x00CODE{len(code_tokens) - 1}\x00"

    text = _FENCED_CODE.sub(protect_code, text)
    text = _INLINE_CODE.sub(protect_code, text)
    text = normalize_prose(text).strip()
    for index, code in enumerate(code_tokens):
        text = text.replace(f"\x00CODE{index}\x00", code)
    return text


def _clean_block_text(text: str, *, preserve_code: bool = False) -> str:
    preserve_code = preserve_code or _looks_like_code(text)
    text = re.sub(r"</?class_[^>]+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<x_[-+\d.eE]+><y_[-+\d.eE]+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    if not preserve_code:
        text = re.sub(
            r"<sub>(.*?)</sub>",
            lambda match: f"_({match.group(1)})",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            r"<sup>(.*?)</sup>",
            lambda match: f"^({match.group(1)})",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        code_tokens: list[str] = []

        def protect_code(match: re.Match[str]) -> str:
            code_tokens.append(match.group(0))
            return f"\x00CODE{len(code_tokens) - 1}\x00"

        text = _FENCED_CODE.sub(protect_code, text)
        text = _INLINE_CODE.sub(protect_code, text)
        text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
        text = re.sub(r"__([^_\n]+)__", r"\1", text)
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
        for index, code in enumerate(code_tokens):
            text = text.replace(f"\x00CODE{index}\x00", code)
    return _normalize_text_whitespace(text, preserve_code=preserve_code)


def _clean_question_content(
    text: str,
    *,
    subquestion_label: str | None = None,
    preserve_code: bool = False,
) -> str:
    cleaned = _normalize_text_whitespace(text, preserve_code=preserve_code)
    cleaned = _SOURCE_QUESTION_PREFIX.sub("", cleaned, count=1)
    if subquestion_label:
        label = subquestion_label.strip().strip("()")
        label_pattern = re.escape(label)
        cleaned = re.sub(
            rf"^\s*(?:\({label_pattern}\)|{label_pattern}[.)])\s*[:\-–—]?\s*",
            "",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )
    return _normalize_text_whitespace(cleaned, preserve_code=preserve_code)


def _block_content(block: dict[str, Any]) -> str:
    return _clean_block_text(
        str(block.get("text") or ""),
        preserve_code="code" in str(block.get("layout_type") or "").casefold(),
    )


def _question_content_from_blocks(
    blocks: list[dict[str, Any]], *, subquestion_label: str | None = None
) -> str:
    content_blocks = []
    label_pending = True
    for block in blocks:
        block_text = str(block.get("text") or "")
        is_code = (
            "code" in str(block.get("layout_type") or "").casefold()
            or _looks_like_code(block_text)
        )
        content = _block_content(block)
        if not content:
            continue
        if label_pending and not is_code:
            content = _clean_question_content(
                content,
                subquestion_label=subquestion_label,
            )
            label_pending = False
        content_blocks.append(content)
    return "\n\n".join(content_blocks).strip()


def _extract_layout_blocks(
    raw_payload: dict[str, Any],
    source_digest: str,
    figure_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    reading_order = 0
    for entry in raw_payload.get("pages", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("raw"), dict):
            continue
        page = entry.get("page")
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            continue
        content = entry["raw"].get("content") or entry["raw"].get("text")
        if not isinstance(content, str):
            continue
        matches = list(_BLOCK_MARKER.finditer(content))
        page_assets = [
            {
                key: asset.get(key)
                for key in ("asset_id", "asset_type", "source_page", "bbox", "description")
            }
            for asset in figure_assets
            if asset.get("source_page") == page_number
        ]
        for match in matches:
            original_text = match.group(3).strip()
            layout_type = match.group(6)
            cleaned_text = _clean_block_text(
                original_text,
                preserve_code="code" in layout_type.casefold(),
            )
            if not cleaned_text:
                continue
            reading_order += 1
            identity = hashlib.sha256(
                f"{source_digest}:{page_number}:{reading_order}:{original_text}".encode("utf-8")
            ).hexdigest()[:16]
            block = {
                "block_id": f"p{page_number:04d}-b{reading_order:05d}-{identity}",
                "page_number": page_number,
                "reading_order": reading_order,
                "layout_type": layout_type,
                "bbox": [
                    float(match.group(1)), float(match.group(2)),
                    float(match.group(4)), float(match.group(5)),
                ],
                "original_text": original_text,
                "text": cleaned_text,
                "figure_asset_ids": [
                    asset["asset_id"] for asset in page_assets if asset.get("asset_id")
                ],
            }
            rows = _split_explicit_question_table(block)
            if rows:
                blocks.extend(rows)
                reading_order += len(rows) - 1
            else:
                blocks.append(block)
        if not matches and content.strip():
            for line in content.splitlines():
                cleaned_text = _clean_block_text(line)
                if not cleaned_text:
                    continue
                reading_order += 1
                identity = hashlib.sha256(
                    f"{source_digest}:{page_number}:{reading_order}:{line}".encode("utf-8")
                ).hexdigest()[:16]
                blocks.append({
                    "block_id": f"p{page_number:04d}-b{reading_order:05d}-{identity}",
                    "page_number": page_number,
                    "reading_order": reading_order,
                    "layout_type": "Unclassified",
                    "bbox": None,
                    "original_text": line,
                    "text": cleaned_text,
                    "figure_asset_ids": [
                        asset["asset_id"] for asset in page_assets if asset.get("asset_id")
                    ],
                })
    if not blocks:
        raise RuntimeError("Fresh Nemotron Parse output contains no resolvable layout text blocks")
    return blocks


def _split_explicit_question_table(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose Parse table rows as stable references when they contain numbered questions."""
    if "table" not in str(block.get("layout_type") or "").casefold():
        return []
    match = _LATEX_TABULAR.search(str(block.get("text") or ""))
    if match is None:
        return []
    body = match.group(1)
    if not _TABLE_QUESTION_ROW.search(body):
        return []
    rows = []
    table_rows = [
        row.strip().strip("\\").strip()
        for row in re.split(r"\\\\(?:\s*\[[^\]]+\])?", body)
        if row.strip().strip("\\").strip()
    ]
    table_bbox = block.get("bbox")
    for row_number, raw_row in enumerate(table_rows, start=1):
        cells = [re.sub(r"\s+", " ", cell).strip() for cell in raw_row.split("&")]
        cells = [cell for cell in cells if cell]
        if not cells:
            continue
        question_cells = []
        table_marks = None
        for cell in cells[1:]:
            marks_match = re.fullmatch(r"\[\s*(\d+(?:\.\d+)?)\s*\]", cell)
            if marks_match:
                table_marks = marks_match.group(1)
                break
            question_cells.append(cell)
        text = " ".join([cells[0], *question_cells]).strip()
        digest = hashlib.sha256(
            f"{block['block_id']}:{row_number}:{text}".encode("utf-8")
        ).hexdigest()[:12]
        row_block = {
            **block,
            "block_id": f"{block['block_id']}-r{row_number:04d}-{digest}",
            "reading_order": block["reading_order"] + len(rows),
            "layout_type": "Table-row",
            "original_text": raw_row,
            "text": text,
            "table_marks": table_marks,
            "source_block_id": block["block_id"],
            "table_row": row_number,
            "table_row_count": len(table_rows),
        }
        if isinstance(table_bbox, (list, tuple)) and len(table_bbox) == 4:
            x1, y1, x2, y2 = (float(value) for value in table_bbox)
            row_height = (y2 - y1) / len(table_rows)
            row_block["bbox"] = [
                x1,
                y1 + row_height * (row_number - 1),
                x2,
                y1 + row_height * row_number,
            ]
        rows.append(row_block)
    return rows


def _supplement_explicit_table_questions(
    classifications: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recover explicit Q-numbered table rows omitted by a batch classification."""
    assigned = set()
    for classification in classifications:
        for question in classification.get("questions") or []:
            assigned.update(question.get("block_ids") or [])
            for subquestion in question.get("sub_questions") or []:
                assigned.update(subquestion.get("block_ids") or [])
    groups: dict[str, dict[str, Any]] = {}
    recovered = 0
    for block in blocks:
        if block.get("layout_type") != "Table-row" or block["block_id"] in assigned:
            continue
        match = _TABLE_QUESTION_ROW.match(str(block.get("text") or ""))
        if not match:
            continue
        number = f"Q{match.group(1)}"
        label = f"({match.group(2).lower()})" if match.group(2) else None
        marks = block.get("table_marks")
        group = groups.setdefault(number, {
            "question_number": number,
            "block_ids": [],
            "sub_questions": [],
            "marks": None,
            "section_name": None,
            "instruction_block_ids": [],
            "figure_asset_ids": list(block.get("figure_asset_ids") or []),
            "ambiguous_block_ids": [],
        })
        if label:
            group["sub_questions"].append({
                "label": label,
                "parent_label": None,
                "block_ids": [block["block_id"]],
                "marks": marks,
                "figure_asset_ids": list(block.get("figure_asset_ids") or []),
                "ambiguous_block_ids": [],
            })
        else:
            group["block_ids"].append(block["block_id"])
            group["marks"] = marks
        recovered += 1
    if not recovered:
        return classifications
    logger.warning(
        "LITE_STAGE2_EXPLICIT_TABLE_QUESTIONS_RECOVERED: rows=%d questions=%d",
        recovered,
        len(groups),
    )
    return [*classifications, {"questions": list(groups.values())}]


def _block_batches(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    size = max(1, min(settings.LITE_STAGE2_BLOCK_BATCH_SIZE, 64))
    overlap = max(0, min(settings.LITE_STAGE2_BLOCK_OVERLAP, size - 1))
    batches = []
    start = 0
    while start < len(blocks):
        end = min(start + size, len(blocks))
        batches.append(blocks[start:end])
        if end == len(blocks):
            break
        start = end - overlap
    return batches


_TOP_LEVEL_QUESTION_START = re.compile(
    r"^\s*(?:[*_`~]{1,3}\s*)?"
    r"(?:(?:question\s*(?:no\.?\s*)?|q\s*\.?\s*))?"
    r"(\d+)(?=\s|[.():]|[*_`~]|$)",
    re.IGNORECASE,
)
_NUMBERED_SUBQUESTION_START = re.compile(
    r"^\s*(?:[*_`~]{1,3}\s*)?"
    r"(?:(?:question\s*(?:no\.?\s*)?|q\s*\.?\s*))?"
    r"(\d+)((?:\s*\([^)]+\))+)",
    re.IGNORECASE,
)
_ACADEMIC_QUESTION_VERB = re.compile(
    r"\b(?:explain|discuss|differentiate|compare|describe|define|illustrate|"
    r"evaluate|analyse|analyze|how)\b",
    re.IGNORECASE,
)
_QUESTION_MARKS_METADATA = re.compile(
    r"(?:\[\s*\d+(?:\.\d+)?\s*marks?\s*\]|\bmarks?\s*[:=]?\s*\d+(?:\.\d+)?\b)",
    re.IGNORECASE,
)
_SECTION_CONTEXT = re.compile(r"^\s*(?:section|part)\s+[A-Z0-9IVX]+\b", re.IGNORECASE)
_DOCUMENT_FOOTER = re.compile(
    r"^\W*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\W*[A-Z]?\s*$",
    re.IGNORECASE,
)


def _logical_block_batches(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Return one complete main-question unit per group, plus preamble groups."""
    groups: list[list[dict[str, Any]]] = []
    plain_run: list[dict[str, Any]] = []
    question_run: list[dict[str, Any]] = []
    question_number: str | None = None
    section_context: list[dict[str, Any]] = []

    def flush_plain() -> None:
        nonlocal plain_run
        if plain_run:
            # Preamble is classified deterministically and is never sent to Stage 2.
            groups.append(plain_run)
        plain_run = []

    def flush_question() -> None:
        nonlocal question_run, question_number
        if question_run:
            groups.append(question_run)
        question_run = []
        question_number = None

    for block in blocks:
        text = str(block.get("text") or block.get("original_text") or "")
        match = _TOP_LEVEL_QUESTION_START.match(text)
        if match:
            number = match.group(1)
            if question_run and number != question_number:
                flush_question()
            flush_plain()
            question_number = number
            if not question_run:
                context = section_context
                seen_context = set()
                for item in context:
                    if item.get("block_id") not in seen_context:
                        question_run.append(item)
                        seen_context.add(item.get("block_id"))
            question_run.append(block)
        elif question_run and _DOCUMENT_FOOTER.match(text):
            flush_question()
            plain_run.append(block)
        elif question_run:
            # Keep attached tables, continuation paragraphs and marks with their
            # nearest preceding question until the next explicit main question.
            question_run.append(block)
        else:
            plain_run.append(block)
            if _SECTION_CONTEXT.match(text):
                section_context = [block]
    flush_question()
    flush_plain()
    if not any(
        _TOP_LEVEL_QUESTION_START.match(
            str(block.get("text") or block.get("original_text") or "")
        )
        for block in blocks
    ):
        return _block_batches(blocks)
    return groups


def _question_candidate_reason(block: dict[str, Any]) -> str | None:
    """Conservatively recognize question-like text before deterministic preclassification."""
    text = str(block.get("text") or block.get("original_text") or "").strip()
    match = _TOP_LEVEL_QUESTION_START.match(text)
    if match:
        return f"numbered_question:{match.group(1)}"
    if _QUESTION_MARKS_METADATA.search(text) and not re.search(
        r"\bmaximum\s+marks?\b", text, re.IGNORECASE
    ):
        return "question_marks_metadata"
    if _ACADEMIC_QUESTION_VERB.search(text) or "?" in text:
        return "academic_question_wording"
    return None


def _crop_page_image(image_bytes: bytes, blocks: list[dict[str, Any]]) -> bytes:
    boxes = [
        block.get("bbox") for block in blocks
        if isinstance(block.get("bbox"), (list, tuple)) and len(block["bbox"]) == 4
    ]
    if not boxes:
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            width, height = source.size
            coords = [float(value) for box in boxes for value in box]
            if max(abs(value) for value in coords) > 1.5:
                return image_bytes
            x1 = max(0, int(min(float(box[0]) for box in boxes) * width))
            y1 = max(0, int(min(float(box[1]) for box in boxes) * height))
            x2 = min(width, int(max(float(box[2]) for box in boxes) * width))
            y2 = min(height, int(max(float(box[3]) for box in boxes) * height))
            pad_x, pad_y = max(8, int(width * 0.01)), max(3, int(height * 0.003))
            crop = source.convert("RGB").crop((
                max(0, x1 - pad_x), max(0, y1 - pad_y),
                min(width, x2 + pad_x), min(height, y2 + pad_y),
            ))
            output = io.BytesIO()
            crop.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (OSError, ValueError, TypeError):
        return image_bytes


def _classification_batch_id(blocks: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        "\n".join(block["block_id"] for block in blocks).encode("utf-8")
    ).hexdigest()[:24]


def _classify_nonquestion_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify a contiguous document preamble from its explicit layout labels."""
    instructions: list[str] = []
    other: list[str] = []
    sections: list[dict[str, Any]] = []
    in_instructions = False
    for block in blocks:
        block_id = str(block["block_id"])
        text = str(block.get("text") or block.get("original_text") or "").strip()
        if "table" in str(block.get("layout_type") or "").casefold():
            in_instructions = False
        if re.search(r"\binstructions?\s*[:：]?", text, re.IGNORECASE):
            in_instructions = True
        if in_instructions:
            instructions.append(block_id)
        else:
            other.append(block_id)
        section = re.match(r"^\s*(section|part)\s+([A-Z0-9IVX]+)\b", text, re.IGNORECASE)
        if section:
            sections.append({
                "section_name": f"{section.group(1).title()} {section.group(2)}",
                "block_ids": [block_id],
                "instruction_block_ids": [],
            })
    return {
        "questions": [],
        "sections": sections,
        "instruction_block_ids": instructions,
        "other_block_ids": other,
        "ambiguous_block_ids": [],
    }


def _classify_layout_blocks(
    structurer: Any,
    blocks: list[dict[str, Any]],
    figure_assets: list[dict[str, Any]],
    page_images: dict[int, bytes] | None = None,
    *,
    fallback_pass: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    block_map = {block["block_id"]: block for block in blocks}
    batches = _logical_block_batches(blocks)
    batch_ids = [_classification_batch_id(batch) for batch in batches]
    results: list[Any | None] = [None] * len(batches)
    per_page_limit = 4
    global_limit = 8
    request_pages: dict[int, int] = {}
    window_batches: dict[int, list[int]] = {}
    logger.info(
        "STAGE2_PAGE_WINDOW_PLAN: blocks=%d logical_units=%d "
        "per_page_concurrency=%d global_concurrency=%d",
        len(blocks), len(batches), per_page_limit, global_limit,
    )
    logger.info("STAGE2_PROVIDER_SELECTED: provider=nvidia_nemotron_super_vl")
    logger.info("STAGE2_MODEL_REQUEST: model=%s", settings.NVIDIA_STRUCTURE_MODEL)
    request_payloads: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, bytes] | None]] = {}
    for index, batch in enumerate(batches):
        batch_id = batch_ids[index]
        question_positions = [
            position for position, block in enumerate(batch)
            if _question_candidate_reason(block)
        ]
        if not question_positions:
            preclassification = _classify_nonquestion_blocks(batch)
            results[index] = preclassification
            ids = [str(block.get("block_id") or "") for block in batch]
            logger.info(
                "LITE_STAGE2_UNIT_CLASSIFIED: batch=%s result=deterministic_nonquestion "
                "reason=title_section_or_exam_instruction instructions=%s other=%s "
                "sections=%d blocks=%d block_ids=%s",
                batch_id, preclassification["instruction_block_ids"],
                preclassification["other_block_ids"], len(preclassification["sections"]),
                len(batch), ids,
            )
            continue

        reasons = [
            _question_candidate_reason(batch[position])
            for position in question_positions
        ]
        logger.info(
            "LITE_STAGE2_UNIT_CLASSIFIED: batch=%s result=question_candidate "
            "reason=%s blocks=%d block_ids=%s fallback=%s",
            batch_id, list(dict.fromkeys(reason for reason in reasons if reason)),
            len(batch), [str(block.get("block_id") or "") for block in batch],
            fallback_pass,
        )

        # A logical question may continue onto later pages. Schedule it by the
        # page where its main question starts, while retaining all continuation
        # blocks and images in the same request.
        primary_page = int(batch[question_positions[0]].get("page_number") or 1)
        request_pages[index] = primary_page
        window_batches.setdefault((primary_page - 1) // 2, []).append(index)
        batch_pages = {int(block["page_number"]) for block in batch if block.get("page_number") is not None}
        batch_assets = [
            asset for asset in figure_assets
            if asset.get("source_page") in batch_pages
        ]
        visual_blocks = batch[question_positions[0]:]
        batch_images = None
        if page_images:
            batch_images = {
                page: _crop_page_image(
                    page_images[page],
                    [block for block in visual_blocks if int(block.get("page_number") or -1) == page],
                )
                for page in batch_pages if page in page_images
            }
        request_payloads[index] = (batch, batch_assets, batch_images)

    max_page = max(
        [int(page) for page in (page_images or {})]
        + [int(block.get("page_number") or 0) for block in blocks]
        + [1]
    )
    failures: list[tuple[int, BaseException]] = []
    for window_index in sorted(window_batches):
        pending = list(window_batches[window_index])
        window_start = window_index * 2 + 1
        window_end = min(window_start + 1, max_page)
        per_page_requests: dict[int, int] = {}
        for batch_index in pending:
            page = request_pages[batch_index]
            per_page_requests[page] = per_page_requests.get(page, 0) + 1
        page_request_summary = ",".join(
            f"{page}:{count}" for page, count in sorted(per_page_requests.items())
        )
        logger.info(
            "STAGE2_PAGE_WINDOW_START: pages=%d-%d requests=%d page_requests=%s "
            "model=%s per_page_limit=%d global_limit=%d",
            window_start, window_end, len(pending), page_request_summary,
            settings.NVIDIA_STRUCTURE_MODEL, per_page_limit, global_limit,
        )
        active: dict[Any, int] = {}
        active_by_page: dict[int, int] = {}
        max_active_global = 0
        max_active_page = 0
        with ThreadPoolExecutor(max_workers=global_limit) as executor:
            while pending or active:
                while len(active) < global_limit:
                    eligible_position = next((
                        position for position, batch_index in enumerate(pending)
                        if active_by_page.get(request_pages[batch_index], 0) < per_page_limit
                    ), None)
                    if eligible_position is None:
                        break
                    index = pending.pop(eligible_position)
                    page = request_pages[index]
                    batch, assets, images = request_payloads[index]
                    future = executor.submit(
                        _classify_one_batch, structurer, batch, assets,
                        batch_id=batch_ids[index], page_images=images,
                    )
                    active[future] = index
                    active_by_page[page] = active_by_page.get(page, 0) + 1
                    max_active_global = max(max_active_global, len(active))
                    max_active_page = max(max_active_page, active_by_page[page])
                    logger.info(
                        "STAGE2_PAGE_REQUEST_START: page=%d window=%d-%d "
                        "batch=%d/%d active_global=%d active_page=%d batch_id=%s",
                        page, window_start, window_end, index + 1, len(batches),
                        len(active), active_by_page[page], batch_ids[index],
                    )
                if not active:
                    break
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = active.pop(future)
                    page = request_pages[index]
                    active_by_page[page] -= 1
                    try:
                        results[index] = future.result()
                    except BaseException as exc:
                        failures.append((index, exc))
                        logger.warning(
                            "LITE_STAGE2_BLOCK_BATCH_FAILED: batch=%s index=%d/%d "
                            "page=%d error_type=%s",
                            batch_ids[index], index + 1, len(batches), page,
                            type(exc).__name__,
                        )
                        continue
                    logger.info(
                        "LITE_STAGE2_BLOCK_BATCH_SUCCESS: batch=%s index=%d/%d page=%d",
                        batch_ids[index], index + 1, len(batches), page,
                    )
        logger.info(
            "STAGE2_PAGE_WINDOW_COMPLETE: pages=%d-%d requests=%d failed=%d "
            "max_active_global=%d max_active_page=%d",
            window_start, window_end, len(window_batches[window_index]),
            sum(1 for index, _ in failures if window_index == (request_pages[index] - 1) // 2),
            max_active_global, max_active_page,
        )
    if failures:
        index, failure = failures[0]
        raise RuntimeError(
            f"Stage 2 failed for {len(failures)} question unit(s); "
            f"first failed batch={batch_ids[index]}: {failure}"
        ) from failure
    def flatten(items):
        for item in items:
            if isinstance(item, list):
                yield from flatten(item)
            elif isinstance(item, dict):
                yield item
    classifications = list(flatten(results))
    logger.info("STAGE2_BATCH_MERGE_COMPLETE: logical_units=%d", len(batches))
    return classifications, block_map


def _classify_one_batch(
    structurer: Any,
    blocks: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    *,
    batch_id: str,
    page_images: dict[int, bytes] | None,
) -> dict[str, Any] | list[dict[str, Any]]:
    parameters = inspect.signature(structurer.classify_blocks).parameters
    accepts_images = "page_images" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if page_images and accepts_images:
        return structurer.classify_blocks(
            blocks, assets, batch_id=batch_id, page_images=page_images
        )
    return structurer.classify_blocks(blocks, assets, batch_id=batch_id)


def _resolve_question_candidates(
    classifications: list[dict[str, Any]],
    block_map: dict[str, dict[str, Any]],
    figure_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Normalize alternative reference field names produced by valid model
    # responses so question data is not dropped for formatting differences.
    normalized_classifications: list[dict[str, Any]] = []
    for classification in classifications or []:
        if not isinstance(classification, dict):
            continue
        normalized = dict(classification)
        normalized_questions = []
        for question in classification.get("questions") or []:
            if not isinstance(question, dict):
                continue
            q = dict(question)
            q["question_number"] = (
                q.get("question_number")
                or q.get("number")
                or q.get("question")
                or q.get("label")
                or q.get("id")
                or q.get("question_no")
                or q.get("q_number")
            )
            q["block_ids"] = (
                q.get("block_ids")
                or q.get("blocks")
                or q.get("refs")
                or q.get("references")
                or q.get("block_references")
                or []
            )
            q["instruction_block_ids"] = (
                q.get("instruction_block_ids")
                or q.get("instruction_blocks")
                or q.get("instruction_refs")
                or []
            )
            q["figure_asset_ids"] = (
                q.get("figure_asset_ids")
                or q.get("figure_assets")
                or q.get("asset_ids")
                or q.get("assets")
                or []
            )
            q["ambiguous_block_ids"] = (
                q.get("ambiguous_block_ids")
                or q.get("ambiguous_blocks")
                or q.get("ambiguous_refs")
                or []
            )
            if "sub_questions" not in q:
                q["sub_questions"] = (
                    q.get("subquestions")
                    or q.get("sub_parts")
                    or q.get("parts")
                    or []
                )
            normalized_sub = []
            for sub in q.get("sub_questions") or []:
                if not isinstance(sub, dict):
                    continue
                s = dict(sub)
                s["block_ids"] = (
                    s.get("block_ids")
                    or s.get("blocks")
                    or s.get("refs")
                    or s.get("references")
                    or []
                )
                s["figure_asset_ids"] = (
                    s.get("figure_asset_ids")
                    or s.get("figure_assets")
                    or s.get("asset_ids")
                    or s.get("assets")
                    or []
                )
                s["ambiguous_block_ids"] = (
                    s.get("ambiguous_block_ids")
                    or s.get("ambiguous_blocks")
                    or s.get("ambiguous_refs")
                    or []
                )
                if not str(s.get("label") or "").strip():
                    question_match = _TOP_LEVEL_QUESTION_START.match(
                        str(q.get("question_number") or "")
                    )
                    subquestion_sources = [str(s.get("question_number") or "")]
                    subquestion_sources.extend(
                        str(block_map.get(str(block_id), {}).get("text") or "")
                        for block_id in s.get("block_ids") or []
                    )
                    for source_text in subquestion_sources:
                        numbered = _NUMBERED_SUBQUESTION_START.match(source_text)
                        if not numbered or (
                            question_match
                            and numbered.group(1) != question_match.group(1)
                        ):
                            continue
                        suffixes = re.findall(r"\(([^)]+)\)", numbered.group(2))
                        if not suffixes:
                            continue
                        declared_parent = str(s.get("parent_label") or "").strip()
                        parent_suffixes = re.findall(r"\(([^)]+)\)", declared_parent)
                        if parent_suffixes and suffixes[:len(parent_suffixes)] == parent_suffixes:
                            suffixes = suffixes[len(parent_suffixes):]
                        if not parent_suffixes and len(suffixes) > 1:
                            s["parent_label"] = f"({suffixes[-2]})"
                            suffixes = suffixes[-1:]
                        if suffixes:
                            s["label"] = f"({suffixes[-1]})"
                            break
                normalized_sub.append(s)
            q["sub_questions"] = normalized_sub
            normalized_questions.append(q)
        normalized["questions"] = normalized_questions
        normalized_classifications.append(normalized)

    classifications = normalized_classifications
    global_instruction_ids = list(dict.fromkeys(
        block_id
        for classification in classifications
        for block_id in classification.get("instruction_block_ids") or []
    ))
    merged_questions: dict[str, dict[str, Any]] = {}
    section_refs: list[dict[str, Any]] = []
    orphan_ambiguities: list[str] = []
    for classification in classifications:
        section_refs.extend(classification.get("sections") or [])
        orphan_ambiguities.extend(classification.get("ambiguous_block_ids") or [])
        for question in classification.get("questions") or []:
            number = str(
                question.get("question_number")
                or question.get("number")
                or question.get("question")
                or question.get("label")
                or question.get("id")
                or question.get("question_no")
                or question.get("q_number")
                or ""
            ).strip()
            if not number:
                orphan_ambiguities.extend(
                    question.get("block_ids")
                    or question.get("blocks")
                    or question.get("refs")
                    or []
                )
                continue
            numbered_subpart = re.fullmatch(
                r"(?:Q\.?\s*)?(\d+)\s*\(([^)]+)\)",
                number,
                flags=re.IGNORECASE,
            )
            parent_label = None
            if numbered_subpart:
                parent_label = f"Q{numbered_subpart.group(1)}"
                number = parent_label
            key = re.sub(r"[^a-z0-9]", "", number.casefold())
            merged = merged_questions.setdefault(key, {
                "question_number": number,
                "block_ids": [],
                "sub_questions": {},
                "marks": question.get("marks"),
                "priority": question.get("priority"),
                "priority_evidence": question.get("priority_evidence"),
                "section_name": question.get("section_name"),
                "instruction_block_ids": [],
                "figure_asset_ids": [],
                "ambiguous_block_ids": [],
                "ambiguity_notes": [],
            })
            for field in ("instruction_block_ids", "figure_asset_ids", "ambiguous_block_ids"):
                merged[field] = list(dict.fromkeys([
                    *merged[field],
                    *(question.get(field) or []),
                ]))
            if parent_label is None:
                merged["block_ids"] = list(dict.fromkeys([
                    *merged["block_ids"],
                    *(question.get("block_ids") or []),
                ]))
            if not merged.get("marks"):
                merged["marks"] = question.get("marks") if parent_label is None else None
            if merged.get("priority") is None and question.get("priority") is not None:
                merged["priority"] = question.get("priority")
                merged["priority_evidence"] = question.get("priority_evidence")
            if not merged.get("section_name"):
                merged["section_name"] = question.get("section_name")
            if question.get("ambiguity_note"):
                merged["ambiguity_notes"].append(question["ambiguity_note"])
            question_sub_questions = list(question.get("sub_questions") or [])
            if parent_label is not None:
                question_sub_questions.append({
                    "label": f"({numbered_subpart.group(2)})",
                    "question_number": number,
                    "parent_label": None,
                    "block_ids": question.get("block_ids") or [],
                    "marks": question.get("marks"),
                    "priority": question.get("priority"),
                    "priority_evidence": question.get("priority_evidence"),
                    "figure_asset_ids": question.get("figure_asset_ids") or [],
                    "ambiguous_block_ids": question.get("ambiguous_block_ids") or [],
                    "instruction_block_ids": question.get("instruction_block_ids") or [],
                })
            for subquestion in question_sub_questions:
                subquestion = dict(subquestion)
                label = str(subquestion.get("label") or "").strip()
                subquestion_block_ids = subquestion.get("block_ids") or []
                if not label and not any((
                    subquestion_block_ids,
                    subquestion.get("figure_asset_ids"),
                    subquestion.get("ambiguous_block_ids"),
                    subquestion.get("instruction_block_ids"),
                    subquestion.get("marks"),
                    subquestion.get("priority"),
                    subquestion.get("sub_questions"),
                    subquestion.get("question_number"),
                )):
                    # Nemotron may emit empty placeholder children. They carry
                    # no references or question data and must not become graph nodes.
                    continue
                sub_parent_label = str(
                    subquestion.get("parent_label")
                    or (f"({numbered_subpart.group(2)})" if parent_label else "")
                ).strip()
                subkey = (
                    re.sub(r"[^a-z0-9]", "", sub_parent_label.casefold()),
                    re.sub(r"[^a-z0-9]", "", label.casefold())
                    or "block-" + "-".join(map(str, subquestion_block_ids)),
                )
                child = merged["sub_questions"].setdefault(subkey, {
                    "label": label,
                    "parent_label": sub_parent_label or None,
                    "block_ids": [],
                    "marks": subquestion.get("marks"),
                    "priority": subquestion.get("priority"),
                    "priority_evidence": subquestion.get("priority_evidence"),
                    "figure_asset_ids": [],
                    "ambiguous_block_ids": [],
                    "instruction_block_ids": [],
                })
                for field in ("block_ids", "figure_asset_ids", "ambiguous_block_ids"):
                    child[field] = list(dict.fromkeys(
                        [*child[field], *(subquestion.get(field) or [])]
                    ))
                child["instruction_block_ids"] = list(dict.fromkeys([
                    *child.get("instruction_block_ids", []),
                    *(subquestion.get("instruction_block_ids") or []),
                ]))
                if child.get("priority") is None and subquestion.get("priority") is not None:
                    child["priority"] = subquestion.get("priority")
                    child["priority_evidence"] = subquestion.get("priority_evidence")
                if not child.get("marks"):
                    child["marks"] = subquestion.get("marks")

    asset_map = {str(asset.get("asset_id")): asset for asset in figure_assets}

    def resolve_ids(ids: list[str], owner: str) -> tuple[list[dict[str, Any]], list[str]]:
        resolved = []
        missing = []
        for block_id in ids:
            block = block_map.get(block_id)
            if block is None:
                missing.append(f"{owner}: unresolved block reference {block_id}")
            else:
                resolved.append(block)
        resolved.sort(key=lambda block: block["reading_order"])
        return resolved, missing

    def resolve_assets(ids: list[str], owner: str) -> tuple[list[dict[str, Any]], list[str]]:
        resolved = []
        missing = []
        for asset_id in ids:
            asset = asset_map.get(str(asset_id))
            if asset is None:
                missing.append(f"{owner}: unresolved figure asset reference {asset_id}")
            else:
                resolved.append(asset)
        return resolved, missing

    candidates = []
    for question in merged_questions.values():
        question_label = question["question_number"]
        question_blocks, issues = resolve_ids(question["block_ids"], question_label)
        instruction_ids = list(dict.fromkeys([
            *question["instruction_block_ids"],
            *global_instruction_ids,
        ]))
        for section in section_refs:
            section_block_ids = set(section.get("block_ids") or [])
            if section_block_ids.intersection(question["block_ids"]):
                if not question.get("section_name"):
                    question["section_name"] = section.get("section_name")
                instruction_ids.extend(section.get("instruction_block_ids") or [])
        instruction_ids = list(dict.fromkeys(instruction_ids))
        instruction_blocks, instruction_issues = resolve_ids(instruction_ids, question_label)
        issues.extend(instruction_issues)
        figure_records, figure_issues = resolve_assets(
            question["figure_asset_ids"], question_label
        )
        issues.extend(figure_issues)
        resolved_children: dict[tuple[str, str], dict[str, Any]] = {}
        all_blocks = [*question_blocks, *instruction_blocks]
        for key, child in question["sub_questions"].items():
            child_blocks, child_issues = resolve_ids(child["block_ids"], f"{question_label}{child['label']}")
            child_instruction_blocks, child_instruction_issues = resolve_ids(
                child.get("instruction_block_ids") or [],
                f"{question_label}{child['label']} instructions",
            )
            child_assets, child_asset_issues = resolve_assets(
                child["figure_asset_ids"], f"{question_label}{child['label']}"
            )
            issues.extend(child_issues + child_instruction_issues + child_asset_issues)
            all_blocks.extend([*child_blocks, *child_instruction_blocks])
            resolved_children[key] = {
                "label": child["label"],
                "parent_label": child.get("parent_label"),
                "text": _question_content_from_blocks(
                    child_blocks,
                    subquestion_label=child["label"],
                ),
                "original_text": _question_content_from_blocks(
                    child_blocks,
                    subquestion_label=child["label"],
                ),
                "marks": child["marks"],
                "priority": child.get("priority"),
                "priority_evidence": child.get("priority_evidence"),
                "instruction_block_ids": list(child.get("instruction_block_ids") or []),
                "source_block_ids": [
                    block["block_id"] for block in [*child_blocks, *child_instruction_blocks]
                ],
                "section_instructions": "\n\n".join(
                    block["text"] for block in child_instruction_blocks
                ) or None,
                "source_pages": sorted({block["page_number"] for block in child_blocks}),
                "source_block_ids": [block["block_id"] for block in child_blocks],
                "figure_assets": child_assets,
                "ambiguous_block_ids": child["ambiguous_block_ids"],
                "sub_questions": [],
            }
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for child in resolved_children.values():
            children_by_parent.setdefault(
                re.sub(r"[^a-z0-9]", "", str(child.get("parent_label") or "").casefold()),
                [],
            ).append(child)

        def nest_children(parent_label: str = "") -> list[dict[str, Any]]:
            parent_key = re.sub(r"[^a-z0-9]", "", parent_label.casefold())
            children = children_by_parent.get(parent_key, [])
            for child in children:
                child_key = re.sub(r"[^a-z0-9]", "", str(child.get("label") or "").casefold())
                # A blank label resolves to the root key. Recursing on that key
                # would attach the root children to themselves indefinitely.
                child["sub_questions"] = nest_children(child["label"]) if child_key else []
            return children

        sub_questions = nest_children()
        unknown_question_ambiguities = [
            block_id for block_id in question["ambiguous_block_ids"]
            if block_id not in block_map
        ]
        issues.extend(
            f"{question_label}: unresolved ambiguous block reference {block_id}"
            for block_id in unknown_question_ambiguities
        )
        valid_ambiguous_ids = [
            block_id for block_id in question["ambiguous_block_ids"]
            if block_id in block_map
        ]
        ambiguous_ids = set(question["ambiguous_block_ids"] + valid_ambiguous_ids)
        ambiguous_ids.update(
            child_block
            for child in question["sub_questions"].values()
            for child_block in child["ambiguous_block_ids"]
        )
        ambiguity_notes = list(question["ambiguity_notes"])
        if valid_ambiguous_ids:
            ambiguity_notes.append(
                "Ambiguous extracted blocks: " + ", ".join(valid_ambiguous_ids)
            )
        if issues:
            ambiguity_notes.extend(issues)
        all_blocks.extend(
            block_map[block_id]
            for block_id in ambiguous_ids
            if block_id in block_map
        )
        all_blocks = sorted(
            {block["block_id"]: block for block in all_blocks}.values(),
            key=lambda block: block["reading_order"],
        )
        source_pages = sorted({block["page_number"] for block in all_blocks})
        question_text = _question_content_from_blocks(question_blocks)
        question_priority_text = _extract_priority_from_text(
            "\n".join([
                question_text,
                *(block["text"] for block in instruction_blocks),
            ])
        )
        question_priority = (
            question_priority_text
            if question_priority_text is not None
            else question.get("priority")
        )
        question_priority_source = (
            "extracted_text" if question_priority_text is not None
            else "visual_evidence" if question_priority is not None
            else "missing"
        )
        logger.info(
            "STAGE2_QUESTION_PRIORITY: question=%s priority=%s source=%s evidence=%s",
            question_label,
            question_priority,
            question_priority_source,
            question.get("priority_evidence") if question_priority_source == "visual_evidence" else None,
        )

        def finalize_child_priority(child: dict[str, Any]) -> None:
            child_text_priority = _extract_priority_from_text(
                "\n".join([
                    str(child.get("original_text") or child.get("text") or ""),
                    str(child.get("section_instructions") or ""),
                ])
            )
            if child_text_priority is not None:
                child["priority"] = child_text_priority
                source = "extracted_text"
            elif child.get("priority") is not None:
                source = "visual_evidence"
            else:
                source = "missing"
            child["priority_source"] = source
            logger.info(
                "STAGE2_SUBQUESTION_PRIORITY: question=%s%s priority=%s source=%s evidence=%s",
                question_label,
                child.get("label") or "",
                child.get("priority"),
                source,
                child.get("priority_evidence") if source == "visual_evidence" else None,
            )
            for nested in child.get("sub_questions") or []:
                finalize_child_priority(nested)

        for child in sub_questions:
            finalize_child_priority(child)
        candidates.append({
            "label": question_label,
            "original_question_number": question_label,
            "text": question_text,
            "original_text": question_text,
            "marks": question["marks"],
            "priority": question_priority,
            "priority_source": question_priority_source,
            "priority_evidence": question.get("priority_evidence"),
            "instruction_block_ids": instruction_ids,
            "section_name": question["section_name"],
            "section_instructions": "\n\n".join(
                block["text"] for block in instruction_blocks
            ) or None,
            "source_pages": source_pages,
            "source_page": source_pages[0] if source_pages else None,
            "source_block_ids": [block["block_id"] for block in all_blocks],
            "sub_questions": sub_questions,
            "figure_assets": figure_records,
            "ambiguous": bool(ambiguity_notes),
            "ambiguity_notes": list(dict.fromkeys(ambiguity_notes)),
        })
    candidates.sort(
        key=lambda question: min(
            (
                block_map[block_id]["reading_order"]
                for block_id in question["source_block_ids"]
                if block_id in block_map
            ),
            default=10**9,
        )
    )
    for block_id in dict.fromkeys(orphan_ambiguities):
        ambiguous_block = block_map.get(block_id)
        if ambiguous_block is None:
            logger.warning(
                "LITE_STAGE2_UNRESOLVED_AMBIGUOUS_BLOCK: block_id=%s reason=unknown_reference",
                block_id,
            )
            continue
        nearest = min(
            candidates,
            key=lambda question: min(
                (
                    abs(block_map[ref]["reading_order"] - ambiguous_block["reading_order"])
                    for ref in question["source_block_ids"]
                    if ref in block_map
                ),
                default=10**9,
            ),
            default=None,
        )
        if nearest is not None:
            note = f"Unassigned ambiguous block near question: {block_id}"
            nearest["ambiguous"] = True
            nearest["ambiguity_notes"] = list(dict.fromkeys([
                *nearest["ambiguity_notes"],
                note,
            ]))
    result = _apply_explicit_question_boundaries(
        candidates,
        sorted(block_map.values(), key=lambda block: block["reading_order"]),
        figure_assets,
    )
    if not result:
        fallback = _fallback_candidates_from_blocks(
            sorted(block_map.values(), key=lambda block: block["reading_order"]),
            block_map,
            figure_assets,
        )
        if fallback:
            result = _apply_explicit_question_boundaries(
                fallback,
                sorted(block_map.values(), key=lambda block: block["reading_order"]),
                figure_assets,
            )
    return result


def _fallback_candidates_from_blocks(
    blocks: list[dict[str, Any]],
    block_map: dict[str, dict[str, Any]],
    figure_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Controlled fallback: build candidates from explicit question headings in
    extracted blocks when reference mapping produced no candidates."""
    headings: list[tuple[int, str, str | None]] = []
    for index, block in enumerate(blocks):
        match = _EXPLICIT_QUESTION_HEADING.match(str(block.get("text") or ""))
        if match:
            headings.append((
                index,
                f"Q{match.group(1)}",
                f"({match.group(2).lower()})" if match.group(2) else None,
            ))
    if not headings:
        return []
    asset_map = {
        str(asset.get("asset_id")): asset
        for asset in figure_assets
        if asset.get("asset_id")
    }
    candidates: list[dict[str, Any]] = []
    for position, (start, question_number, sub_label) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(blocks)
        segment_blocks = [
            block for block in blocks[start:end]
            if str(block.get("layout_type") or "").casefold()
            not in {"page-header", "page-footer"}
        ]
        if not segment_blocks:
            continue
        block_ids = [block["block_id"] for block in segment_blocks]
        source_pages = sorted({block["page_number"] for block in segment_blocks})
        text = _question_content_from_blocks(segment_blocks)
        if not text:
            continue
        asset_ids = list(dict.fromkeys(
            asset_id
            for block in segment_blocks
            for asset_id in block.get("figure_asset_ids", [])
        ))
        candidate = {
            "label": question_number,
            "original_question_number": question_number,
            "text": text,
            "original_text": text,
            "marks": None,
            "section_name": None,
            "section_instructions": None,
            "source_pages": source_pages,
            "source_page": source_pages[0] if source_pages else None,
            "source_block_ids": block_ids,
            "sub_questions": [],
            "figure_assets": [asset_map[aid] for aid in asset_ids if aid in asset_map],
            "ambiguous": True,
            "ambiguity_notes": [
                "Fallback: question references recovered from extracted block text."
            ],
        }
        candidates.append(candidate)
    logger.warning(
        "LITE_STAGE2_FALLBACK_CANDIDATES: recovered=%d", len(candidates)
    )
    return candidates


_EXPLICIT_QUESTION_HEADING = re.compile(
    r"^\s*Q\s*\.?\s*(\d+)\s*(?:\(([a-z])\))?(?=\s|$|[.:)])",
    re.IGNORECASE,
)


def _apply_explicit_question_boundaries(
    candidates: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    figure_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    headings: list[tuple[int, str, str | None]] = []
    for index, block in enumerate(blocks):
        match = _EXPLICIT_QUESTION_HEADING.match(str(block.get("text") or ""))
        if match:
            headings.append((
                index,
                f"Q{match.group(1)}",
                f"({match.group(2).lower()})" if match.group(2) else None,
            ))
    if not headings:
        return [
            candidate for candidate in candidates
            if str(candidate.get("text") or "").strip()
            or (candidate.get("sub_questions") or [])
        ]
    explicit_question_keys = {
        re.sub(r"[^a-z0-9]", "", question_number.casefold())
        for _, question_number, sub_label in headings
        if sub_label is not None
    }
    if not explicit_question_keys:
        return [
            candidate for candidate in candidates
            if str(candidate.get("text") or "").strip()
            or (candidate.get("sub_questions") or [])
        ]
    candidate_by_number = {
        re.sub(
            r"[^a-z0-9]",
            "",
            str(candidate.get("original_question_number") or candidate.get("label") or "").casefold(),
        ): candidate
        for candidate in candidates
    }
    original_reference_ids: dict[str, set[str]] = {}
    model_children: dict[str, dict[str, dict[str, Any]]] = {}
    for key in explicit_question_keys:
        candidate = candidate_by_number.get(key)
        if candidate is None:
            continue
        original_reference_ids[key] = set(candidate.get("source_block_ids") or [])
        model_children[key] = {
            str(child.get("label")): child
            for child in candidate.get("sub_questions") or []
        }
        for child in model_children[key].values():
            original_reference_ids[key].update(child.get("source_block_ids") or [])
        candidate.update(
            text="",
            original_text="",
            source_pages=[],
            source_page=None,
            source_block_ids=[],
            sub_questions=[],
            figure_assets=[],
        )
    asset_map = {
        str(asset.get("asset_id")): asset
        for asset in figure_assets
        if asset.get("asset_id")
    }
    recovered: dict[str, dict[str, Any]] = {}
    for position, (start, question_number, sub_label) in enumerate(headings):
        if str(blocks[start].get("layout_type") or "").casefold() == "table-row":
            end = start + 1
        else:
            end = headings[position + 1][0] if position + 1 < len(headings) else len(blocks)
        question_key = re.sub(r"[^a-z0-9]", "", question_number.casefold())
        if question_key not in explicit_question_keys:
            continue
        candidate = candidate_by_number.get(question_key)
        if candidate is None:
            candidate = recovered.get(question_key)
        if candidate is None:
            candidate = {
                "label": question_number,
                "original_question_number": question_number,
                "text": "",
                "original_text": "",
                "marks": None,
                "section_name": None,
                "section_instructions": None,
                "source_pages": [],
                "source_page": None,
                "source_block_ids": [],
                "sub_questions": [],
                "figure_assets": [],
                "ambiguous": True,
                "ambiguity_notes": [
                    "Question boundary recovered from explicit Parse block headings."
                ],
            }
            recovered[question_key] = candidate

        segment_blocks = [
            block
            for block in blocks[start:end]
            if str(block.get("layout_type") or "").casefold()
            not in {"page-header", "page-footer"}
        ]
        block_ids = [block["block_id"] for block in segment_blocks]
        candidate["source_block_ids"] = list(dict.fromkeys([
            *candidate.get("source_block_ids", []),
            *block_ids,
        ]))
        candidate["source_pages"] = sorted({
            *candidate.get("source_pages", []),
            *(block["page_number"] for block in segment_blocks),
        })
        if candidate["source_pages"]:
            candidate["source_page"] = candidate["source_pages"][0]

        segment_text = _question_content_from_blocks(
            segment_blocks,
            subquestion_label=sub_label,
        )
        segment_original = segment_text
        if sub_label is None:
            candidate["text"] = segment_text
            candidate["original_text"] = segment_original
            parent_asset_ids = list(dict.fromkeys(
                asset_id
                for block in segment_blocks
                for asset_id in block.get("figure_asset_ids", [])
            ))
            candidate["figure_assets"] = [
                asset_map[asset_id]
                for asset_id in parent_asset_ids
                if asset_id in asset_map
            ]
        else:
            children = candidate.setdefault("sub_questions", [])
            child = next(
                (item for item in children if item.get("label") == sub_label),
                None,
            )
            if child is None:
                child = model_children.get(question_key, {}).get(sub_label) or {
                    "label": sub_label,
                    "parent_label": None,
                    "text": "",
                    "original_text": "",
                    "marks": None,
                    "source_pages": [],
                    "source_block_ids": [],
                    "figure_assets": [],
                    "ambiguous_block_ids": [],
                    "sub_questions": [],
                }
                children.append(child)
            child["text"] = segment_text
            child["original_text"] = segment_original
            child["source_block_ids"] = block_ids
            child["source_pages"] = sorted({
                block["page_number"] for block in segment_blocks
            })
            child["ambiguous_block_ids"] = []
            child_asset_ids = list(dict.fromkeys(
                asset_id
                for block in segment_blocks
                for asset_id in block.get("figure_asset_ids", [])
            ))
            child["figure_assets"] = list({
                asset_id: asset_map[asset_id]
                for asset_id in child_asset_ids
                if asset_id in asset_map
            }.values())

    candidates.extend(recovered.values())
    # Drop placeholder candidates that carry no question text and no subquestions;
    # they only produce empty Question Bank rows.
    candidates = [
        candidate for candidate in candidates
        if str(candidate.get("text") or "").strip()
        or (candidate.get("sub_questions") or [])
    ]
    for key in explicit_question_keys:
        candidate = candidate_by_number.get(key) or recovered.get(key)
        if candidate is None:
            continue
        explicit_ids = set(candidate.get("source_block_ids") or [])
        missing_ids = explicit_ids - original_reference_ids.get(key, set())
        if missing_ids:
            candidate["ambiguous"] = True
            candidate["ambiguity_notes"] = list(dict.fromkeys([
                *(candidate.get("ambiguity_notes") or []),
                "Explicit question headings recovered block boundaries omitted by the classifier.",
            ]))
    return [
        candidate for candidate in candidates
        if str(candidate.get("text") or "").strip()
        or (candidate.get("sub_questions") or [])
    ]


def _extract_priority_from_text(text: object) -> int | None:
    if not isinstance(text, str) or not text.strip():
        return None
    match = re.search(
        r"\bpriority\s*(?:(?:level|order)\s*)?[:=]?\s*([123])\b",
        text,
        re.IGNORECASE,
    )
    if match:
        try:
            value = int(match.group(1))
        except ValueError:
            return None
        if value in {1, 2, 3}:
            return value
    return None


def _normalize_marks(value: Any) -> tuple[float | None, str | None, list[str]]:
    """Keep printed marks notation while deriving the numeric schema value."""
    if value is None or not str(value).strip():
        return None, None, []
    notation = str(value).strip()
    expression = re.sub(r"(?i)\s*(?:marks?|m)\s*$", "", notation)
    expression = expression.strip().strip("[](){}").strip()
    components = [part.strip() for part in re.split(r"[+＋]", expression) if part.strip()]
    try:
        if len(components) > 1:
            return sum(float(part) for part in components), notation, components
        multiplied = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)", expression)
        if multiplied:
            first, count = (float(part) for part in multiplied.groups())
            return first * count, notation, [f"{first:g} each", f"total {first * count:g}"]
        return float(expression), notation, [expression]
    except (TypeError, ValueError):
        # Preserve unfamiliar notation for review without passing a string as marks.
        return None, notation, []


def _build_question_record(
    source_pdf: str,
    source_question: dict[str, Any],
    stable_id: str,
    *,
    default_priority: int = 1,
) -> dict[str, Any]:
    question_label = source_question.get("original_question_number") or source_question.get("label")
    pages = source_question.get("source_pages") or []
    question_priority = source_question.get("priority")
    if question_priority is None:
        question_priority = _extract_priority_from_text(
            source_question.get("original_text") or source_question.get("text") or ""
        )
    if question_priority is None:
        question_priority = default_priority
        question_priority_source = "pdf_default"
    else:
        question_priority_source = source_question.get("priority_source") or "individual"
    logger.info(
        "STAGE2_QUESTION_PRIORITY_FINAL: question=%s priority=%s source=%s",
        question_label,
        question_priority,
        question_priority_source,
    )

    def build_subquestion(
        child: dict[str, Any],
        parent_id: str = stable_id,
        parent_number: str = str(question_label),
        parent_priority: int = question_priority,
    ) -> dict[str, Any]:
        child_label = child.get("label") or ""
        child_id = hashlib.sha256(
            f"{parent_id}:{child_label}".encode("utf-8")
        ).hexdigest()[:16]
        child_assets = [
            {
                "asset_id": asset.get("asset_id"),
                "asset_type": asset.get("asset_type") or "figure",
                "path": asset.get("path"),
                "source_page": asset.get("source_page"),
                "description": asset.get("description"),
                "verified": bool(asset.get("path")),
            }
            for asset in child.get("figure_assets") or []
        ]
        child_priority = child.get("priority")
        if child_priority is None:
            child_priority = _extract_priority_from_text(child.get("original_text") or child.get("text") or "")
        if child_priority is None:
            child_priority = default_priority
            child_priority_source = "pdf_default"
        else:
            child_priority_source = child.get("priority_source") or "individual"
        logger.info(
            "STAGE2_SUBQUESTION_PRIORITY_FINAL: question=%s%s priority=%s source=%s",
            question_label,
            child_label,
            child_priority,
            child_priority_source,
        )
        child_number = child.get("question_number") or f"{parent_number}{child_label}"
        child_marks, child_marks_notation, _ = _normalize_marks(child.get("marks"))
        return {
            "sub_id": child_id,
            "original_text": child.get("original_text") or "",
            "cleaned_text": child.get("text") or "",
            "original_label": child_label or None,
            "original_question_number": str(child_number),
            "parent_question_ref": str(parent_number),
            "parent_question_id": parent_id,
            "marks": child_marks,
            "marks_notation": child_marks_notation,
            "priority": child_priority,
            "parent_question_priority": parent_priority,
            "source_block_ids": child.get("source_block_ids") or [],
            "instruction_block_ids": child.get("instruction_block_ids") or [],
            "section_instructions": child.get("section_instructions"),
            "assets": child_assets,
            "source_pdf": source_pdf,
            "source_pages": child.get("source_pages") or pages,
            "sub_questions": [
                build_subquestion(nested, child_id, str(child_number), child_priority)
                for nested in child.get("sub_questions") or []
            ],
        }

    question_assets = [
        {
            "asset_id": asset.get("asset_id"),
            "asset_type": asset.get("asset_type") or "figure",
            "path": asset.get("path"),
            "source_page": asset.get("source_page"),
            "description": asset.get("description"),
            "verified": bool(asset.get("path")),
        }
        for asset in source_question.get("figure_assets") or []
    ]
    ambiguous = bool(source_question.get("ambiguous"))
    question_marks, question_marks_notation, marks_components = _normalize_marks(
        source_question.get("marks")
    )
    return {
        "question_id": stable_id,
        "status": "structured",
        "original_text": source_question.get("original_text") or "",
        "cleaned_text": source_question.get("text") or "",
        "original_label": str(question_label),
        "original_question_number": str(question_label),
        "marks": question_marks,
        "marks_notation": question_marks_notation,
        "marks_components": marks_components,
        "priority": question_priority,
        "source_block_ids": source_question.get("source_block_ids") or [],
        "instruction_block_ids": source_question.get("instruction_block_ids") or [],
        "sub_questions": [
            build_subquestion(child)
            for child in source_question.get("sub_questions") or []
        ],
        "section_name": source_question.get("section_name"),
        "section_instructions": source_question.get("section_instructions"),
        "source_pdf": source_pdf,
        "source_pages": pages,
        "assets": question_assets,
        "tables": [],
        "confidence": 0.0 if ambiguous else 1.0,
        "validation_issues": source_question.get("ambiguity_notes") or [],
        "review_status": "incomplete" if ambiguous else "pending",
        "processing_notes": [
            "Text resolved from Nemotron Parse block references."
        ],
    }

def _question_id(source_pdf: str, question: dict[str, Any]) -> str:
    label = question.get("original_question_number") or question.get("label") or ""
    text = question.get("text") or ""
    pages = question.get("source_pages") or [question.get("source_page")]
    first_page = next((int(page) for page in pages if page is not None), None)
    return generate_question_id(source_pdf, f"{label}::{text}", first_page)


class LiteTwoStageProcessor:
    def __init__(self):
        if getattr(settings, "PARSER_MODE", "local").lower() != "llm":
            raise RuntimeError("LiteTwoStageProcessor only in PARSER_MODE=llm")
        self.nemotron = NemotronParseClient()
        self.normalizer = NemotronNormalizer()

    def _render_page_bytes(self, path: Path) -> dict[int, bytes]:
        images: dict[int, bytes] = {}
        for expected_page, page in enumerate(render_page_images(path), start=1):
            if page.page_number != expected_page:
                raise RuntimeError(
                    f"Page renderer returned page {page.page_number}; expected page {expected_page}"
                )
            if page.mime not in {"image/png", "image/jpeg"}:
                raise RuntimeError(
                    f"Page {page.page_number} rendered unsupported MIME type {page.mime!r}"
                )
            images[page.page_number] = page.data
        return images

    def _extracted_document(
        self,
        path: Path,
        raw_payload: dict[str, Any],
        normalized: dict[str, Any],
        figure_assets: list[dict[str, Any]],
    ) -> ExtractedDocument:
        pages = raw_payload["pages"]
        return ExtractedDocument(
            text=normalized.get("text") or raw_payload["content"],
            pages=[
                {
                    "number": entry["page"],
                    "text": str(
                        entry["raw"].get("content") or entry["raw"].get("text") or ""
                    ),
                    "raw": entry["raw"],
                }
                for entry in pages
            ],
            tables=normalized.get("tables") or [],
            figures=[
                DocumentResource(
                    path=asset["path"],
                    kind=asset["classification"],
                    page=asset["source_page"],
                    bbox={
                        "x1": asset["bbox"][0],
                        "y1": asset["bbox"][1],
                        "x2": asset["bbox"][2],
                        "y2": asset["bbox"][3],
                    },
                    ref=asset["asset_id"],
                    metadata=asset,
                )
                for asset in figure_assets
            ],
            metadata={
                "raw": raw_payload,
                "nemotron_normalized": normalized,
                "extraction_provider": "nvidia_nemotron_parse",
                "page_count": len(pages),
                "figure_assets": figure_assets,
            },
            source_path=path,
        )

    def process(
        self,
        path: Path,
        output_dir: Path | None = None,
        on_structuring: Callable[[int], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        on_question_progress: Callable[[dict[str, Any]], None] | None = None,
        default_priority: int = 1,
    ):
        if default_priority not in {1, 2, 3}:
            raise ValueError("default_priority must be 1, 2, or 3")
        path = Path(path)
        output = Path(output_dir) if output_dir is not None else None
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)
            # Artifacts from a prior or failed run must not be mistaken for this run.
            for artifact_name in (
                "lite_extraction_cache.json",
                "lite_stage1_extraction.json",
                "lite_block_classifications.json",
                "lite_structuring_progress.json",
                "questions_parsed.json",
            ):
                (output / artifact_name).unlink(missing_ok=True)
            figure_dir = output / "assets" / "figures"
            if figure_dir.exists():
                for stale_asset in figure_dir.glob("*"):
                    if stale_asset.is_file():
                        stale_asset.unlink()
        source_digest = _file_sha256(path)
        input_format = path.suffix.lower().lstrip(".") or "unknown"
        logger.info("LITE_INPUT_PREPARATION: file=%s format=%s", path.name, input_format)
        logger.info(
            "LITE_STAGE1_EXTRACTION: provider=nvidia_nemotron_parse endpoint=%s",
            getattr(settings, "NVIDIA_PARSE_ENDPOINT", ""),
        )

        images = self._render_page_bytes(path)
        if not images:
            raise RuntimeError(
                f"No pages could be rendered from {path.name} (input format: {input_format})"
            )
        logger.info(
            "LITE_PAGE_PREPARATION_COMPLETE: file=%s format=%s rendered_pages=%d",
            path.name,
            input_format,
            len(images),
        )
        if on_event is not None:
            on_event(
                f"Lite rendered {len(images)} page images from {input_format} input {path.name}"
            )

        page_results_map: dict[int, dict[str, Any]] = {}

        def _parse_page(page_number: int, image_data: bytes) -> tuple[int, dict[str, Any]]:
            mime = "image/jpeg" if image_data.startswith(b"\\xff\\xd8\\xff") else "image/png"
            extension = ".jpg" if mime == "image/jpeg" else ".png"
            image_name = f"page-{page_number:05d}{extension}"
            logger.info(
                "LITE_PAGE_EXTRACTION: page=%d/%d image=%s mime=%s",
                page_number, len(images), image_name, mime,
            )
            if on_event is not None:
                on_event(f"Nemotron request page {page_number}/{len(images)} image={image_name} mime={mime}")
            try:
                raw = self.nemotron.parse_page_image(
                    image_data, mime, page_number, filename=image_name
                )
            except Exception as exc:
                if on_event is not None:
                    on_event(f"Nemotron failed page {page_number}/{len(images)} image={image_name} mime={mime} error={type(exc).__name__}")
                raise RuntimeError(
                    f"Nemotron extraction failed for page {page_number}/{len(images)} "
                    f"image {image_name} ({mime}) of {path.name}: {exc}"
                ) from exc
            if on_event is not None:
                on_event(f"Nemotron response page {page_number}/{len(images)} image={image_name} mime={mime} HTTP 200")
            if not isinstance(raw, dict):
                raw = {"content": str(raw)}
            content = raw.get("content") or raw.get("text")
            if not str(content or "").strip():
                raise RuntimeError(
                    f"Nemotron extraction returned empty content for page {page_number} "
                    f"image {image_name} of {path.name}"
                )
            return page_number, raw

        with ThreadPoolExecutor(max_workers=_concurrency()) as executor:
            futures = {
                executor.submit(_parse_page, page_number, image_data): page_number
                for page_number, image_data in images.items()
            }
            for future in as_completed(futures):
                page_number, raw = future.result()
                page_results_map[page_number] = raw

        page_results = [
            (page_number, page_results_map[page_number])
            for page_number in sorted(images)
        ]
        combined = self.nemotron.combine_pages(page_results)
        raw_payload = {
            "content": combined,
            "pages": [{"page": number, "raw": raw} for number, raw in page_results],
        }
        normalized = self.normalizer.normalize(combined)
        normalized["pages"] = [
            {"page": number, "content": str(raw.get("content") or raw.get("text") or "")}
            for number, raw in page_results
        ]
        figure_assets = (
            crop_figure_assets(images, page_results, output, source_digest)
            if output is not None else []
        )
        normalized["figure_assets"] = figure_assets
        if output is not None:
            _atomic_json_write(output / "lite_stage1_extraction.json", {
                "source_sha256": source_digest,
                "parse_model": settings.NVIDIA_PARSE_MODEL,
                "raw_payload": raw_payload,
                "normalized": normalized,
                "figure_assets": figure_assets,
            })

        extracted = self._extracted_document(path, raw_payload, normalized, figure_assets)
        logger.info(
            "LITE_EXTRACTION_COMPLETE: file=%s pages=%d figures=%d",
            path.name,
            len(raw_payload["pages"]),
            len(figure_assets),
        )
        if on_structuring is not None:
            on_structuring(len(raw_payload["pages"]))
        if on_event is not None:
            on_event(
                f"Lite mode: {len(raw_payload['pages'])} pages extracted; "
                "NVIDIA structuring started"
            )
        logger.info("LITE_STAGE2_STRUCTURING: provider=nvidia_nemotron_super_vl")

        from src.masterdeorca.document_intelligence.lite.structurer import StructureWithLLM

        structurer = StructureWithLLM()
        all_figure_assets = list(figure_assets)
        blocks = _extract_layout_blocks(raw_payload, source_digest, all_figure_assets)
        classifications, block_map = _classify_layout_blocks(
            structurer,
            blocks,
            all_figure_assets,
            page_images=images,
        )
        if output is not None:
            _atomic_json_write(output / "lite_block_classifications.json", {
                "source_sha256": source_digest,
                "classifications": classifications,
            })
        classifications = _supplement_explicit_table_questions(
            classifications,
            list(block_map.values()),
        )
        candidates = _resolve_question_candidates(
            classifications,
            block_map,
            all_figure_assets,
        )
        def count_subquestions(items: list[dict[str, Any]]) -> int:
            return sum(
                len(children := item.get("sub_questions") or [])
                + count_subquestions(children)
                for item in items
            )

        logger.info(
            "STAGE2_QUESTION_COUNTS: questions=%d sub_questions=%d blocks=%d",
            len(candidates),
            count_subquestions(candidates),
            len(blocks),
        )
        if not candidates:
            question_references = sum(
                len(item.get("questions") or [])
                for item in classifications if isinstance(item, dict)
            )
            explicit_headings = sum(
                bool(_TOP_LEVEL_QUESTION_START.match(
                    str(block.get("text") or block.get("original_text") or "")
                ))
                for block in block_map.values()
            )
            logger.error(
                "STAGE2_NO_QUESTION_CANDIDATES: question_references=%d "
                "explicit_headings=%d blocks=%d",
                question_references, explicit_headings, len(blocks),
            )
            logger.warning(
                "STAGE2_FALLBACK_CLASSIFICATION_START: reason=no_question_candidates "
                "original_blocks=%d; rechecking original question-like units",
                len(blocks),
            )
            try:
                fallback_classifications, _ = _classify_layout_blocks(
                    structurer,
                    blocks,
                    all_figure_assets,
                    page_images=images,
                    fallback_pass=True,
                )
                fallback_classifications = _supplement_explicit_table_questions(
                    fallback_classifications,
                    list(block_map.values()),
                )
                fallback_candidates = _resolve_question_candidates(
                    fallback_classifications,
                    block_map,
                    all_figure_assets,
                )
            except Exception:
                logger.exception("STAGE2_FALLBACK_CLASSIFICATION_FAILED")
                fallback_classifications = []
                fallback_candidates = []
            if fallback_candidates:
                classifications.extend(fallback_classifications)
                candidates = fallback_candidates
                logger.warning(
                    "STAGE2_FALLBACK_CLASSIFICATION_RECOVERED: questions=%d",
                    len(candidates),
                )
                if output is not None:
                    _atomic_json_write(output / "lite_block_classifications.json", {
                        "source_sha256": source_digest,
                        "classifications": classifications,
                    })
            else:
                logger.error(
                    "STAGE2_FALLBACK_CLASSIFICATION_EMPTY: original_blocks=%d",
                    len(blocks),
                )
        if not candidates:
            if explicit_headings:
                raise RuntimeError(
                    "Nemotron Super VL returned no usable question candidates despite "
                    f"{explicit_headings} explicit question headings in extracted blocks"
                )
            raise RuntimeError(
                "Stage 2 identified no question candidates; the extracted blocks were "
                "classified as instructions, other content, or ambiguous"
            )
        parsed_path = output / "questions_parsed.json" if output is not None else None
        bank = QuestionBank(
            metadata={
                "providers_used": [
                    "nvidia_nemotron_parse",
                    "nvidia_nemotron_super_vl",
                ],
                "total_pages_processed": len(raw_payload.get("pages") or []),
                "total_questions_found": len(candidates),
            }
        )
        processed_questions = 0
        failed_questions: list[str] = []

        def emit_progress(current: dict[str, Any] | None, status: str) -> None:
            if on_question_progress is None:
                return
            done = processed_questions
            current_label = None
            current_page = None
            if current is not None:
                current_label = (
                    current.get("original_question_number")
                    or current.get("label")
                    or f"question {candidates.index(current) + 1}"
                )
                current_page = current.get("source_page")
            on_question_progress({
                "total_questions": len(candidates),
                "processed_questions": done,
                "current_question": current_label if status == "processing" else None,
                "current_page": current_page if status == "processing" else None,
                "failed_questions": list(failed_questions),
                "completion_percent": round(100 * done / len(candidates), 1),
                "status": status,
            })

        emit_progress(None, "processing")
        for index, source_question in enumerate(candidates, start=1):
            stable_id = _question_id(path.name, source_question)

            pages = source_question.get("source_pages") or [
                source_question.get("source_page")
            ]
            pages = [int(page) for page in pages if page is not None]
            question_label = (
                source_question.get("original_question_number")
                or source_question.get("label")
                or f"question {index}"
            )
            if on_event is not None:
                on_event(
                    f"Structuring question {index}/{len(candidates)} "
                    f"({question_label}) page={pages}"
                )
            logger.info(
                "LITE_STAGE2_QUESTION_START: question=%s index=%d/%d pages=%s",
                question_label,
                index,
                len(candidates),
                pages,
            )
            record: dict[str, Any] = {}
            try:
                record = _build_question_record(
                    path.name,
                    source_question,
                    stable_id,
                    default_priority=default_priority,
                )
                validated_record = QuestionBank.model_validate({
                    "schema_version": bank.schema_version,
                    "metadata": bank.metadata.model_dump(mode="json"),
                    "questions": [record],
                })
            except Exception as exc:
                if isinstance(exc, ValidationError):
                    field_errors = [
                        ".".join(str(part) for part in error["loc"])
                        + ": " + error["msg"]
                        for error in exc.errors(include_url=False)
                    ]
                else:
                    field_errors = [f"{type(exc).__name__}: {exc}"]
                logger.error(
                    "LITE_STAGE2_QUESTION_VALIDATION_FAILED: question=%s index=%d/%d fields=%s",
                    question_label,
                    index,
                    len(candidates),
                    field_errors,
                )
                raise RuntimeError(
                    f"Question Bank validation failed for {question_label}: "
                    + "; ".join(field_errors)
                ) from exc

            bank.questions.append(validated_record.questions[0])
            bank.metadata.total_questions_found = len(candidates)
            bank.metadata.total_pages_processed = len(raw_payload.get("pages") or [])
            bank.metadata.total_assets_linked = sum(
                len(question.assets)
                + sum(len(child.assets) for child in question.sub_questions)
                for question in bank.questions
            )
            processed_questions += 1
            emit_progress(None, "processing")
            logger.info(
                "LITE_STAGE2_QUESTION_SUCCESS: question=%s index=%d/%d "
                "processed=%d total=%d",
                question_label,
                index,
                len(candidates),
                processed_questions,
                len(candidates),
            )

        bank.metadata.total_questions_found = len(candidates)
        bank.metadata.total_pages_processed = len(raw_payload.get("pages") or [])
        bank = QuestionBank.model_validate(bank.model_dump(mode="python"))
        logger.info(
            "STAGE2_MERGED_RESULT_VALIDATED: questions=%d sub_questions=%d default_priority=%d",
            len(bank.questions),
            count_subquestions([
                {"sub_questions": [
                    child.model_dump(mode="python") for child in question.sub_questions
                ]}
                for question in bank.questions
            ]),
            default_priority,
        )
        bank_data = bank.to_output_dict()
        if parsed_path is not None:
            _atomic_json_write(parsed_path, bank_data)
            extracted.metadata["parsed_json"] = str(parsed_path)
        emit_progress(
            None,
            "completed_with_errors" if failed_questions else "complete",
        )
        return extracted, bank_data
