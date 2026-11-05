from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from config import settings
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
CACHE_FILENAME = "lite_extraction_cache.json"
CACHE_VERSION = 1
STRUCTURING_PROGRESS_FILENAME = "lite_structuring_progress.json"
STRUCTURING_PROGRESS_VERSION = 1


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


def _load_extraction_cache(
    cache_path: Path | None, source_digest: str
) -> dict[str, Any] | None:
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("LITE_EXTRACTION_CACHE_INVALID: path=%s error=%s", cache_path, exc)
        return None
    if (
        not isinstance(cache, dict)
        or cache.get("cache_version") != CACHE_VERSION
        or cache.get("source_sha256") != source_digest
        or cache.get("parse_model") != settings.NVIDIA_PARSE_MODEL
        or not isinstance(cache.get("raw_payload"), dict)
        or not isinstance(cache.get("normalized"), dict)
        or not isinstance(cache["raw_payload"].get("pages"), list)
    ):
        return None
    return cache


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
                    "original_text": line,
                    "text": cleaned_text,
                    "figure_asset_ids": [
                        asset["asset_id"] for asset in page_assets if asset.get("asset_id")
                    ],
                })
    if not blocks:
        raise RuntimeError("Nemotron Parse cache contains no resolvable layout text blocks")
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
    for row_number, raw_row in enumerate(re.split(r"\\\\(?:\s*\[[^\]]+\])?", body), start=1):
        raw_row = raw_row.strip().strip("\\").strip()
        if not raw_row:
            continue
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
        rows.append({
            **block,
            "block_id": f"{block['block_id']}-r{row_number:04d}-{digest}",
            "reading_order": block["reading_order"] + len(rows),
            "layout_type": "Table-row",
            "original_text": raw_row,
            "text": text,
            "table_marks": table_marks,
            "source_block_id": block["block_id"],
            "table_row": row_number,
        })
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
    size = max(8, min(settings.LITE_STAGE2_BLOCK_BATCH_SIZE, 64))
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


def _classify_layout_blocks(
    structurer: Any,
    blocks: list[dict[str, Any]],
    figure_assets: list[dict[str, Any]],
    source_digest: str,
    cache_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path_cache: dict[str, Any] = {}
    if cache_path is not None and cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if loaded.get("source_sha256") == source_digest and loaded.get("version") == 2:
                path_cache = loaded.get("batches", {})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            logger.warning("LITE_BLOCK_CLASSIFICATION_CACHE_INVALID: error=%s", type(exc).__name__)

    block_map = {block["block_id"]: block for block in blocks}
    classifications = []
    batches = _block_batches(blocks)
    for index, batch in enumerate(batches):
        batch_id = hashlib.sha256(
            "\n".join(block["block_id"] for block in batch).encode("utf-8")
        ).hexdigest()[:24]
        result = path_cache.get(batch_id)
        if not isinstance(result, dict):
            batch_pages = {block["page_number"] for block in batch}
            batch_assets = [
                asset for asset in figure_assets
                if asset.get("source_page") in batch_pages
            ]
            result = structurer.classify_blocks(
                batch,
                batch_assets,
                batch_id=batch_id,
            )
            path_cache[batch_id] = result
            if cache_path is not None:
                _atomic_json_write(cache_path, {
                    "version": 2,
                    "source_sha256": source_digest,
                    "batch_size": settings.LITE_STAGE2_BLOCK_BATCH_SIZE,
                    "overlap": settings.LITE_STAGE2_BLOCK_OVERLAP,
                    "batches": path_cache,
                })
        else:
            logger.info(
                "LITE_STAGE2_BLOCK_BATCH_CACHE_REUSED: batch=%s index=%d/%d",
                batch_id,
                index + 1,
                len(batches),
            )
        classifications.append(result)
    return classifications, block_map


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
                normalized_sub.append(s)
            q["sub_questions"] = normalized_sub
            normalized_questions.append(q)
        normalized["questions"] = normalized_questions
        normalized_classifications.append(normalized)

    classifications = normalized_classifications
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
            if not merged.get("section_name"):
                merged["section_name"] = question.get("section_name")
            if question.get("ambiguity_note"):
                merged["ambiguity_notes"].append(question["ambiguity_note"])
            question_sub_questions = list(question.get("sub_questions") or [])
            if parent_label is not None:
                question_sub_questions.append({
                    "label": f"({numbered_subpart.group(2)})",
                    "parent_label": None,
                    "block_ids": question.get("block_ids") or [],
                    "marks": question.get("marks"),
                    "figure_asset_ids": question.get("figure_asset_ids") or [],
                    "ambiguous_block_ids": question.get("ambiguous_block_ids") or [],
                })
            for subquestion in question_sub_questions:
                label = str(subquestion.get("label") or "").strip()
                sub_parent_label = str(
                    subquestion.get("parent_label")
                    or (f"({numbered_subpart.group(2)})" if parent_label else "")
                ).strip()
                subkey = (
                    re.sub(r"[^a-z0-9]", "", sub_parent_label.casefold()),
                    re.sub(r"[^a-z0-9]", "", label.casefold()),
                )
                child = merged["sub_questions"].setdefault(subkey, {
                    "label": label,
                    "parent_label": sub_parent_label or None,
                    "block_ids": [],
                    "marks": subquestion.get("marks"),
                    "figure_asset_ids": [],
                    "ambiguous_block_ids": [],
                })
                for field in ("block_ids", "figure_asset_ids", "ambiguous_block_ids"):
                    child[field] = list(dict.fromkeys(
                        [*child[field], *(subquestion.get(field) or [])]
                    ))
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
        instruction_ids = list(question["instruction_block_ids"])
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
            child_assets, child_asset_issues = resolve_assets(
                child["figure_asset_ids"], f"{question_label}{child['label']}"
            )
            issues.extend(child_issues + child_asset_issues)
            all_blocks.extend(child_blocks)
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
                child["sub_questions"] = nest_children(child["label"])
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
        candidates.append({
            "label": question_label,
            "original_question_number": question_label,
            "text": _question_content_from_blocks(question_blocks),
            "original_text": _question_content_from_blocks(question_blocks),
            "marks": question["marks"],
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


def _build_question_record(
    source_pdf: str,
    source_question: dict[str, Any],
    stable_id: str,
) -> dict[str, Any]:
    question_label = source_question.get("original_question_number") or source_question.get("label")
    pages = source_question.get("source_pages") or []

    def build_subquestion(
        child: dict[str, Any], parent_id: str = stable_id
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
        return {
            "sub_id": child_id,
            "original_text": child.get("original_text") or "",
            "cleaned_text": child.get("text") or "",
            "original_label": child_label or None,
            "original_question_number": f"{question_label}{child_label}",
            "parent_question_ref": str(question_label),
            "marks": child.get("marks"),
            "assets": child_assets,
            "source_pdf": source_pdf,
            "source_pages": child.get("source_pages") or pages,
            "sub_questions": [
                build_subquestion(nested, child_id)
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
    return {
        "question_id": stable_id,
        "status": "structured",
        "original_text": source_question.get("original_text") or "",
        "cleaned_text": source_question.get("text") or "",
        "original_label": str(question_label),
        "original_question_number": str(question_label),
        "marks": source_question.get("marks"),
        "marks_notation": (
            str(source_question["marks"])
            if source_question.get("marks") is not None
            else None
        ),
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
            "Text resolved from cached Nemotron Parse block references."
        ],
    }


def _candidate_id(question: dict[str, Any]) -> str:
    payload = json.dumps(question, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _question_id(source_pdf: str, question: dict[str, Any]) -> str:
    label = question.get("original_question_number") or question.get("label") or ""
    text = question.get("text") or ""
    pages = question.get("source_pages") or [question.get("source_page")]
    first_page = next((int(page) for page in pages if page is not None), None)
    return generate_question_id(source_pdf, f"{label}::{text}", first_page)


def _load_structuring_progress(
    path: Path | None,
    source_digest: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("LITE_STRUCTURING_PROGRESS_INVALID: error=%s", type(exc).__name__)
        return None
    candidate_ids = [_candidate_id(question) for question in candidates]
    if (
        not isinstance(progress, dict)
        or progress.get("version") != STRUCTURING_PROGRESS_VERSION
        or progress.get("source_sha256") != source_digest
        or progress.get("candidate_ids") != candidate_ids
        or not isinstance(progress.get("completed"), dict)
    ):
        return None
    return progress


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
    ):
        path = Path(path)
        output = Path(output_dir) if output_dir is not None else None
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)
        cache_path = output / CACHE_FILENAME if output is not None else None
        source_digest = _file_sha256(path)
        input_format = path.suffix.lower().lstrip(".") or "unknown"
        logger.info("LITE_INPUT_PREPARATION: file=%s format=%s", path.name, input_format)
        logger.info(
            "LITE_STAGE1_EXTRACTION: provider=nvidia_nemotron_parse endpoint=%s",
            getattr(settings, "NVIDIA_PARSE_ENDPOINT", ""),
        )

        cache = _load_extraction_cache(cache_path, source_digest)
        if cache is not None:
            raw_payload = cache["raw_payload"]
            normalized = cache["normalized"]
            figure_assets = cache.get("figure_assets") or []
            logger.info(
                "LITE_EXTRACTION_CACHE_REUSED: file=%s pages=%d figures=%d",
                path.name,
                len(raw_payload["pages"]),
                len(figure_assets),
            )
            if on_event is not None:
                on_event(
                    f"Reused cached Nemotron extraction for {path.name}: "
                    f"{len(raw_payload['pages'])} pages"
                )
            missing_asset = output is not None and any(
                not (output / asset["path"]).is_file() for asset in figure_assets
            )
            if missing_asset:
                images = self._render_page_bytes(path)
                page_results = [
                    (entry["page"], entry["raw"]) for entry in raw_payload["pages"]
                ]
                figure_assets = crop_figure_assets(
                    images, page_results, output, source_digest
                )
                normalized["figure_assets"] = figure_assets
        else:
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

            page_results: list[tuple[int, dict[str, Any]]] = []
            for page_number, image_data in images.items():
                mime = "image/jpeg" if image_data.startswith(b"\xff\xd8\xff") else "image/png"
                extension = ".jpg" if mime == "image/jpeg" else ".png"
                image_name = f"page-{page_number:05d}{extension}"
                logger.info(
                    "LITE_PAGE_EXTRACTION: page=%d/%d image=%s mime=%s",
                    page_number,
                    len(images),
                    image_name,
                    mime,
                )
                if on_event is not None:
                    on_event(
                        f"Nemotron request page {page_number}/{len(images)} "
                        f"image={image_name} mime={mime}"
                    )
                try:
                    raw = self.nemotron.parse_page_image(
                        image_data, mime, page_number, filename=image_name
                    )
                except Exception as exc:
                    if on_event is not None:
                        on_event(
                            f"Nemotron failed page {page_number}/{len(images)} "
                            f"image={image_name} mime={mime} error={type(exc).__name__}"
                        )
                    raise RuntimeError(
                        f"Nemotron extraction failed for page {page_number}/{len(images)} "
                        f"image {image_name} ({mime}) of {path.name}: {exc}"
                    ) from exc
                if on_event is not None:
                    on_event(
                        f"Nemotron response page {page_number}/{len(images)} "
                        f"image={image_name} mime={mime} HTTP 200"
                    )
                if not isinstance(raw, dict):
                    raw = {"content": str(raw)}
                content = raw.get("content")
                if content is None:
                    content = raw.get("text")
                if not str(content or "").strip():
                    raise RuntimeError(
                        f"Nemotron extraction returned empty content for page {page_number} "
                        f"image {image_name} of {path.name}"
                    )
                page_results.append((page_number, raw))

            combined = self.nemotron.combine_pages(page_results)
            raw_payload = {
                "content": combined,
                "pages": [{"page": number, "raw": raw} for number, raw in page_results],
            }
            normalized = self.normalizer.normalize(combined)
            normalized["pages"] = [
                {
                    "page": number,
                    "content": str(raw.get("content") or raw.get("text") or ""),
                }
                for number, raw in page_results
            ]
            figure_assets = (
                crop_figure_assets(images, page_results, output, source_digest)
                if output is not None
                else []
            )
            normalized["figure_assets"] = figure_assets
            if cache_path is not None:
                _atomic_json_write(cache_path, {
                    "cache_version": CACHE_VERSION,
                    "source_sha256": source_digest,
                    "parse_model": settings.NVIDIA_PARSE_MODEL,
                    "raw_payload": raw_payload,
                    "normalized": normalized,
                    "figure_assets": figure_assets,
                })

        if cache_path is not None and cache is not None:
            _atomic_json_write(cache_path, {
                "cache_version": CACHE_VERSION,
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
        classification_cache_path = (
            output / "lite_block_classifications.json" if output is not None else None
        )
        classifications, block_map = _classify_layout_blocks(
            structurer,
            blocks,
            all_figure_assets,
            source_digest,
            classification_cache_path,
        )
        classifications = _supplement_explicit_table_questions(
            classifications,
            list(block_map.values()),
        )
        candidates = _resolve_question_candidates(
            classifications,
            block_map,
            all_figure_assets,
        )
        if not candidates:
            raise RuntimeError(
                "Nemotron Super VL did not identify any question references in extracted blocks"
            )
        candidate_ids = [_candidate_id(question) for question in candidates]
        progress_path = output / STRUCTURING_PROGRESS_FILENAME if output is not None else None
        parsed_path = output / "questions_parsed.json" if output is not None else None
        checkpoint = _load_structuring_progress(progress_path, source_digest, candidates)
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
        if checkpoint is not None and parsed_path is not None and parsed_path.is_file():
            try:
                bank = QuestionBank.model_validate(
                    json.loads(parsed_path.read_text(encoding="utf-8"))
                )
                logger.info(
                    "LITE_STRUCTURING_RESUME: file=%s completed=%d total=%d",
                    path.name,
                    len(bank.questions),
                    len(candidates),
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "LITE_STRUCTURING_RESUME_BANK_INVALID: file=%s error=%s",
                    path.name,
                    type(exc).__name__,
                )
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

        existing_ids = {question.question_id for question in bank.questions}
        completed = {
            candidate_id: _question_id(path.name, question)
            for candidate_id, question in zip(candidate_ids, candidates)
            if candidate_id in (checkpoint or {}).get("completed", {})
            and _question_id(path.name, question) in existing_ids
        }
        # A crash can occur after writing the bank but before its checkpoint.
        for candidate_id, question in zip(candidate_ids, candidates):
            stable_id = _question_id(path.name, question)
            if stable_id in existing_ids:
                completed[candidate_id] = stable_id

        failed_questions: list[str] = [
            question.original_question_number or question.question_id
            for question in bank.questions
            if question.status == "unstructured"
        ]

        def emit_progress(current: dict[str, Any] | None, status: str) -> None:
            if on_question_progress is None:
                return
            done = len(completed)
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
        for index, (candidate_id, source_question) in enumerate(
            zip(candidate_ids, candidates), start=1
        ):
            stable_id = _question_id(path.name, source_question)
            if candidate_id in completed or stable_id in existing_ids:
                completed[candidate_id] = stable_id
                continue

            pages = source_question.get("source_pages") or [
                source_question.get("source_page")
            ]
            pages = [int(page) for page in pages if page is not None]
            emit_progress(source_question, "processing")
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
            failure_reason: str | None = None
            failure_details: str | None = None
            record: dict[str, Any] = {}
            try:
                record = _build_question_record(path.name, source_question, stable_id)
                QuestionBank.model_validate({
                    "schema_version": bank.schema_version,
                    "metadata": bank.metadata.model_dump(mode="json"),
                    "questions": [
                        question.model_dump(mode="json") for question in bank.questions
                    ] + [record],
                })
            except Exception as exc:
                failure_reason = getattr(exc, "failure_reason", None)
                if failure_reason not in {
                    "token_limit",
                    "invalid_json",
                    "api_error",
                    "response_format",
                    "schema_validation",
                    "empty_response",
                }:
                    if isinstance(exc, (ValueError, TypeError)):
                        failure_reason = "schema_validation"
                    elif isinstance(exc, RuntimeError) and "unavailable" not in str(exc).casefold():
                        failure_reason = "schema_validation"
                    else:
                        failure_reason = "api_error"
                failure_details = (
                    str(exc)
                    if isinstance(exc, (ValueError, TypeError))
                    else f"{type(exc).__name__}: {exc}"
                )
                logger.warning(
                    "LITE_STAGE2_QUESTION_STRUCTURING_FAILED: question=%s index=%d/%d "
                    "page=%s failure_reason=%s error_type=%s",
                    question_label,
                    index,
                    len(candidates),
                    pages,
                    failure_reason,
                    type(exc).__name__,
                )
                failed_questions.append(str(question_label))
                source_text = _unstructured_source_text(source_question)
                source_marks = source_question.get("marks")
                record = {
                    "question_id": stable_id,
                    "status": "unstructured",
                    "original_text": source_text,
                    "cleaned_text": None,
                    "original_label": (
                        str(source_question["label"])
                        if source_question.get("label") is not None
                        else None
                    ),
                    "original_question_number": str(
                        source_question.get("original_question_number") or question_label
                    ),
                    "section_name": source_question.get("section_name"),
                    "section_instructions": source_question.get("section_instructions"),
                    "marks": source_marks,
                    "marks_notation": (
                        str(source_marks) if source_marks is not None else None
                    ),
                    "source_pdf": path.name,
                    "source_pages": pages,
                    "failure_reason": failure_reason,
                    "structuring_error": failure_details[:2000],
                    "validation_issues": [
                        f"Nemotron Super structuring failed: {failure_reason}"
                    ],
                    "processing_notes": ["Original extracted text preserved without structuring."],
                }

            validated = QuestionBank.model_validate({
                "schema_version": bank.schema_version,
                "metadata": bank.metadata.model_dump(mode="json"),
                "questions": [
                    question.model_dump(mode="json") for question in bank.questions
                ] + [record],
            })
            bank = validated
            if failure_reason is not None:
                bank.metadata.incomplete_records += 1
            bank.metadata.total_questions_found = len(candidates)
            bank.metadata.total_pages_processed = len(raw_payload.get("pages") or [])
            bank.metadata.total_assets_linked = sum(
                len(question.assets)
                + sum(len(child.assets) for child in question.sub_questions)
                for question in bank.questions
            )
            existing_ids.add(stable_id)
            completed[candidate_id] = stable_id
            bank_data = bank.to_output_dict()
            if parsed_path is not None:
                _atomic_json_write(parsed_path, bank_data)
            if progress_path is not None:
                _atomic_json_write(progress_path, {
                    "version": STRUCTURING_PROGRESS_VERSION,
                    "source_sha256": source_digest,
                    "candidate_ids": candidate_ids,
                    "completed": completed,
                })
            emit_progress(None, "processing")
            if failure_reason is None:
                logger.info(
                    "LITE_STAGE2_QUESTION_SUCCESS: question=%s index=%d/%d "
                    "processed=%d total=%d",
                    question_label,
                    index,
                    len(candidates),
                    len(completed),
                    len(candidates),
                )
            else:
                logger.warning(
                    "LITE_STAGE2_QUESTION_SAVED_UNSTRUCTURED: question=%s index=%d/%d "
                    "failure_reason=%s processed=%d total=%d",
                    question_label,
                    index,
                    len(candidates),
                    failure_reason,
                    len(completed),
                    len(candidates),
                )

        bank.metadata.total_questions_found = len(candidates)
        bank.metadata.total_pages_processed = len(raw_payload.get("pages") or [])
        bank_data = bank.to_output_dict()
        if parsed_path is not None:
            _atomic_json_write(parsed_path, bank_data)
            extracted.metadata["parsed_json"] = str(parsed_path)
        if progress_path is not None:
            _atomic_json_write(progress_path, {
                "version": STRUCTURING_PROGRESS_VERSION,
                "source_sha256": source_digest,
                "candidate_ids": candidate_ids,
                "completed": completed,
            })
        emit_progress(
            None,
            "completed_with_errors" if failed_questions else "complete",
        )
        return extracted, bank_data
