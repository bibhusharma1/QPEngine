"""Question-bank builder pipeline using LangGraph.

Extracts, cleans, categorizes, validates, and saves exam questions into
a marks-indexed JSON question bank.

LangGraph stages:
  1. load_manifest  — Read manifest JSON + referenced text files, images, diagrams
  2. ocr_reconcile  — GPU OCR verification / supplement if needed
  3. segment_questions — Multi-provider AI segmentation of questions and subparts
  4. extract_marks  — Parse marks notation, compute totals, build hierarchy
  5. associate_assets — Link figures, images, diagrams to correct questions
  6. normalize      — Clean text, deduplicate, build marks index
  7. validate       — Schema validation, completeness checks, confidence scoring
  8. persist        — Write validated questions_parsed.json files (NO DOCX)
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from config import parser as config
from config.outputs import pdf_stem, stage_output_dir
from config.settings import PROJECT_ROOT
from functions.QuesParser.ParsePipeLIne.providers import (
    ProviderManager,
    ProviderResult,
    get_gpu_device_name,
    gpu_available,
)
from functions.QuesParser.ParsePipeLIne.schema import (
    SCHEMA_VERSION,
    AssetReference,
    ProcessingMetadata,
    QuestionBank,
    QuestionRecord,
    ReviewStatus,
    SubQuestion,
    TableReference,
    generate_question_id,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Pipeline state
# ═══════════════════════════════════════════════════════════════════════

class PipelineState(TypedDict, total=False):
    # Input
    source_path: str
    manifest_paths: list[str]

    # After load
    pages: list[dict]
    source_manifests: list[str]

    # After OCR
    ocr_texts: dict[int, str]
    gpu_device: str | None

    # After segmentation
    raw_segments: list[dict]

    # After marks extraction
    question_records: list[QuestionRecord]

    # After asset association
    asset_map: dict[str, list[AssetReference]]

    # After normalization
    deduplicated: bool

    # After validation
    validation_issues: list[str]

    # After persist
    output_path: str
    question_bank: QuestionBank

    # Tracking
    providers_used: list[str]
    errors: list[str]


# ═══════════════════════════════════════════════════════════════════════
# AI structured output models
# ═══════════════════════════════════════════════════════════════════════

class ExtractedTable(BaseModel):
    table_id: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    cells: list[list[str]] = Field(default_factory=list)
    source_page: int | None = None
    bbox: list[float] = Field(default_factory=list)
    extraction_method: str | None = None


class ExtractedSubQuestion(BaseModel):
    label: str | None = Field(default=None, description="Original printed label, such as a), (b), or (i)")
    text: str = Field(description="Question text for this sub-part without labels like 'a)' or '(i)'")
    marks: str | None = Field(default=None, description="Marks explicitly stated for this sub-part if any (e.g. '6', '[2]'). If none stated, leave null.")
    sub_questions: list[ExtractedSubQuestion] = Field(default_factory=list, description="Nested subparts if any (e.g. I, II, III)")
    tables: list[ExtractedTable] = Field(default_factory=list, description="Tables belonging to this subpart; preserve exact headers, row order, and cell values")


class ExtractedQuestion(BaseModel):
    label: str | None = Field(default=None, description="Original printed question number/label, such as Q6 or 6.")
    text: str = Field(description="Question stem or prompt text without labels like 'Q1' or 'Question 2'")
    marks: str | None = Field(default=None, description="Marks notation printed for this question (e.g. '2 x 10', '6 x 8', '16', '[5]'). If none, leave null.")
    section_name: str | None = Field(default=None, description="Section or Part, e.g. 'Part-I', 'Part-II', 'Section A'")
    section_instructions: str | None = Field(default=None, description="Section instruction text if any")
    is_compulsory: bool | None = Field(default=None, description="True if marked compulsory")
    source_page: int | None = Field(default=None, description="1-based page number where question appears")
    sub_questions: list[ExtractedSubQuestion] = Field(default_factory=list, description="All subparts belonging to this question. Do NOT create separate top-level questions for subparts.")
    tables: list[ExtractedTable] = Field(default_factory=list, description="Tables belonging to this question; preserve exact headers, row order, and cell values")


class ExtractedPaper(BaseModel):
    institution: str | None = Field(default=None, description="University / Institution name")
    exam_title: str | None = Field(default=None, description="Examination name")
    course_code: str | None = Field(default=None, description="Subject / Course code")
    subject: str | None = Field(default=None, description="Subject title")
    semester: str | None = Field(default=None, description="Semester")
    session: str | None = Field(default=None, description="Academic session / year")
    full_marks: float | None = Field(default=None, description="Total paper marks")
    questions: list[ExtractedQuestion] = Field(default_factory=list, description="All extracted questions in paper order")


# ═══════════════════════════════════════════════════════════════════════
# Stage 1: Load manifest / JSON
# ═══════════════════════════════════════════════════════════════════════

def _resolve_path(rel_or_abs: str, base_dir: Path) -> Path:
    p = Path(rel_or_abs)
    if p.is_file():
        return p
    # Try relative to project root
    root_p = PROJECT_ROOT / rel_or_abs
    if root_p.is_file():
        return root_p
    # Try relative to base_dir
    cand = base_dir / rel_or_abs
    if cand.is_file():
        return cand
    return p


def load_manifest(state: PipelineState) -> PipelineState:
    """Load pages from manifest.json files or legacy JSON."""
    errors: list[str] = []
    pages: list[dict] = []
    loaded_manifests: list[str] = []

    manifest_candidates: list[str] = []
    if state.get("manifest_paths"):
        manifest_candidates.extend(state["manifest_paths"])
    elif config.MANIFEST_PATHS:
        manifest_candidates.extend(config.MANIFEST_PATHS)
    elif state.get("source_path"):
        manifest_candidates.append(state["source_path"])
    elif config.QUESTIONS_JSON_FILE:
        configured_source = Path(config.QUESTIONS_JSON_FILE)
        if configured_source.is_dir() and not (configured_source / "manifest.json").is_file():
            manifest_candidates.extend(str(path) for path in sorted(configured_source.glob("*/manifest.json")))
        else:
            manifest_candidates.append(config.QUESTIONS_JSON_FILE)

    project_root = PROJECT_ROOT

    for item in manifest_candidates:
        item_path = Path(item)
        if not item_path.is_absolute():
            item_path = (project_root / item).resolve()

        if not item_path.is_file():
            # Check if directory with manifest.json
            if item_path.is_dir() and (item_path / "manifest.json").is_file():
                item_path = item_path / "manifest.json"
            else:
                errors.append(f"Source file not found: {item}")
                continue

        loaded_manifests.append(str(item_path))
        try:
            raw_content = item_path.read_text(encoding="utf-8")
            data = json.loads(raw_content)
        except Exception as exc:
            errors.append(f"Failed to read JSON from {item_path}: {exc}")
            continue

        base_dir = item_path.parent

        # Case 1: QuesScrapper manifest.json
        if isinstance(data, dict) and "source_pdf" in data:
            source_pdf = data.get("source_pdf", item_path.stem + ".pdf")
            embedded_imgs = data.get("embedded_images", [])
            vector_figs = data.get("vector_figures", [])

            for p_info in data.get("pages", []):
                p_num = p_info.get("page_number", 1)
                text_path_str = p_info.get("text_path", "")
                page_text = ""
                if text_path_str:
                    resolved_txt = _resolve_path(text_path_str, base_dir)
                    if resolved_txt.is_file():
                        page_text = resolved_txt.read_text(encoding="utf-8", errors="replace")
                    else:
                        errors.append(f"Text file missing: {text_path_str}")

                page_img_str = p_info.get("page_image", "")
                resolved_img = _resolve_path(page_img_str, base_dir) if page_img_str else None

                # Find assets for this page
                p_assets = []
                for img in embedded_imgs:
                    if img.get("page_number") == p_num:
                        p_assets.append(img.get("image_path"))
                for fig in vector_figs:
                    if fig.get("page_number") == p_num:
                        p_assets.append(fig.get("figure_path", fig.get("path")))

                pages.append({
                    "pdf_name": source_pdf,
                    "page_number": p_num,
                    "raw_text": page_text,
                    "page_image_path": str(resolved_img) if resolved_img and resolved_img.is_file() else None,
                    "figure_paths": [p for p in p_assets if p],
                    "layout_blocks": p_info.get("layout_blocks", []),
                    "tables": p_info.get("tables", []),
                    "rejected_table_candidates": p_info.get("rejected_table_candidates", []),
                    "ocr_confidence": p_info.get("ocr_confidence", 1.0),
                    "needs_ocr_review": p_info.get("needs_ocr_review", False),
                })

        # Case 2: Legacy array of page records
        elif isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    pages.append({
                        "pdf_name": row.get("pdf_name", "document.pdf"),
                        "page_number": row.get("page_number", 1),
                        "raw_text": row.get("raw_text", ""),
                        "page_image_path": row.get("page_image_path"),
                        "figure_paths": row.get("figure_paths", []),
                        "layout_blocks": row.get("layout_blocks", []),
                        "tables": row.get("tables", []),
                        "rejected_table_candidates": row.get("rejected_table_candidates", []),
                    })
        else:
            errors.append(f"Unrecognized format in {item_path}")

    if not pages:
        raise FileNotFoundError(f"No pages could be loaded from candidates: {manifest_candidates}")

    logger.info("Loaded %d page(s) from %d manifest(s)", len(pages), len(loaded_manifests))
    return {
        "pages": pages,
        "source_manifests": loaded_manifests,
        "errors": errors,
        "providers_used": [],
    }


# ═══════════════════════════════════════════════════════════════════════
# Stage 2: OCR Reconciliation
# ═══════════════════════════════════════════════════════════════════════

def ocr_reconcile(state: PipelineState) -> PipelineState:
    """Verify GPU OCR status and reconcile with extracted text.

    Designated local operations require NVIDIA CUDA with no silent CPU fallback.
    """
    gpu_name = get_gpu_device_name()
    errors = list(state.get("errors", []))

    if gpu_name:
        logger.info("Local GPU verified for OCR/inference: %s", gpu_name)
    else:
        logger.info("Local GPU not available; proceeding with extracted text & remote API inference")

    return {
        "gpu_device": gpu_name,
        "errors": errors,
    }


# ═══════════════════════════════════════════════════════════════════════
# Stage 3: Question segmentation
# ═══════════════════════════════════════════════════════════════════════

def _extract_original_label(text: str) -> tuple[str | None, str]:
    """Extract original question/subquestion label/number from text without removing from a separate copy.
    Returns (label, remaining_text) where remaining_text has label stripped.
    """
    if not text:
        return None, ""
    s = text.strip()
    label = None
    # Try to capture common forms
    m = re.match(r'^(Q(?:uestion)?\.?\s*\d+(?:[a-zA-Z])?(?:\.[a-zA-Z0-9]+|\([a-zA-Z0-9]+\))?)[:.)\s-]+', s, flags=re.IGNORECASE)
    if m:
        label = m.group(1).strip().rstrip(':. )-')
        s = s[m.end():]
        return label, s.strip()
    m = re.match(r'^(\d+(?:\.[a-zA-Z0-9]+)?)[:.)\s-]+', s)
    if m:
        label = m.group(1).strip()
        s = s[m.end():]
        return label, s.strip()
    m = re.match(r'^(\([ivxlcdmIVXLCDM]+\)|[ivxlcdmIVXLCDM]+\.)[:.)\s-]+', s)
    if m:
        label = m.group(1).strip()
        s = s[m.end():]
        return label, s.strip()
    m = re.match(r'^(\(?[a-zA-Z]\)?)[:.)\s-]+', s)
    if m:
        lab = m.group(1).strip()
        # Avoid matching single letters that might be start of word in some cases - but better to be permissive for exam labels
        if len(lab) <= 4:
            label = lab if lab.endswith(')') or lab.endswith('.') else lab + ')'
            s = s[m.end():]
            return label, s.strip()
    return None, s


def clean_question_text(text: str) -> str:
    """Clean question text by stripping printed question numbers, labels, and outer marks."""
    if not text:
        return ""
    s = text.strip()
    # Learning-outcome annotations are grading metadata, not question text.
    s = re.sub(r"\bCO\s*\d+\b|\bBL\s*\d+\b", "", s, flags=re.IGNORECASE)
    # Preserve printed marks in their metadata field rather than duplicating
    # them inside reconstructed question wording.
    s = re.sub(r"\s*[\[(]\s*\d+(?:\s*[+x×*]\s*\d+)*\s*[\])]", "", s)
    # Strip leading question indicators: e.g. "Q.1", "Q1", "Question 1:", "Q2(a)", "1.", "1)", "(1)"
    s = re.sub(
        r'^(?:Q(?:uestion)?\.?\s*\d+(?:\.[a-zA-Z0-9]+|\([a-zA-Z0-9]+\)|[a-zA-Z])?[:.)-]?|\b\d+[:.)-]|[(]\d+[)])\s*',
        '',
        s,
        flags=re.IGNORECASE,
    )
    # Strip leading subquestion indicators: e.g. "a)", "(a)", "a.", "a -", "i.", "(i)", "i)", "I.", "II.", "A."
    s = re.sub(
        r'^(?:[(]?[a-zA-Z][.)\-:]|[a-zA-Z]\)|[(]?[ivxlcdmIVXLCDM]+[.)\-:])\s*',
        '',
        s,
    )
    # Strip trailing marks notations: e.g. [16], (2 x 10), [6], (6x8)
    s = re.sub(r'\s*\[[0-9.+x×*\sMm]+\]\s*$', '', s)
    s = re.sub(r'\s*\([0-9.+x×*\sMm]+\)\s*$', '', s)
    # Preserve blank-line paragraph boundaries while normalizing line wraps
    # and repeated whitespace within each paragraph.
    s = re.sub(r"(?<=[A-Za-z])-[ \t]*\n[ \t]*(?=[a-z])", "", s)
    paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n+", s) if part.strip()]
    return "\n\n".join(paragraphs)


def remove_structured_table_text(text: str, tables: list[dict]) -> str:
    """Remove an inline text duplicate when the same table is stored structurally."""
    result = text
    for table in tables or []:
        rows = table.get("cells") or ([table.get("headers", [])] if table.get("headers") else []) + table.get("rows", [])
        rows = [[str(cell).strip() for cell in row if str(cell).strip()] for row in rows if row]
        if not rows or not rows[0] or not rows[-1]:
            continue
        first = re.escape(rows[0][0])
        last_values = rows[-1]
        end = r"[\s,;|]+".join(re.escape(value) for value in last_values)
        pattern = re.compile(rf"(?<!\w){first}(?!\w)[\s\S]*?{end}(?!\w)", re.IGNORECASE)
        result, count = pattern.subn("", result, count=1)
        if not count:
            # Some extractors preserve column breaks without spaces. Try
            # matching compact cell content (e.g. ArrivalTime(ms)).
            compact_end = r"[\s,;|]*".join(re.escape(value.replace(" ", "")) for value in last_values)
            pattern = re.compile(rf"(?<!\w){first}(?!\w)[\s\S]*?{compact_end}(?!\w)", re.IGNORECASE)
            result = pattern.sub("", result, count=1)
    return re.sub(r"[ \t]+\n", "\n", result).strip()


SEGMENTATION_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an expert examination question-paper parser.\n"
        "Extract every question and sub-question from the provided exam-paper text.\n\n"
        "COMPLETENESS CHECK: Read from the first line through the final page. Identify every distinct printed main question number before writing the result. "
        "Return every one of those main questions, in order, including the last question. Never omit questions to shorten the response. "
        "When the paper has Q1 through Q5, the result must contain Q1, Q2, Q3, Q4, and Q5.\n\n"
        "CRITICAL RULES:\n"
        "1. PRESERVE ORIGINAL LABELS:\n"
        "   - Put each printed question/subquestion number or label in its `label` field.\n"
        "   - Keep labels out of the `text` field; copy the wording verbatim.\n\n"
        "2. PRESERVE QUESTION HIERARCHY:\n"
        "   - Maintain strict parent-child question hierarchy. A main question containing subparts MUST be structured as ONE main question with its subparts placed inside `sub_questions`.\n"
        "   - NEVER flatten subparts into separate top-level questions! For example:\n"
        "     * Q1 containing short questions (a to j) is ONE top-level question with 10 subquestions in `sub_questions`.\n"
        "     * Q2 containing short questions (a to l) is ONE top-level question with 12 subquestions in `sub_questions`. If part l has subparts (I, II, III), nest them inside part l's sub_questions.\n"
        "     * Q6 with parts (a, b, c) is ONE top-level question with 3 subquestions in `sub_questions`.\n\n"
        "2a. STRUCTURED TABLES AND BOUNDARIES:\n"
        "   - Treat [STRUCTURED TABLE] blocks as cell data, not prose. Attach each table to the question/subquestion it supports and preserve headers and rows exactly in `tables`. Do not repeat its cell values in `text`.\n"
        "   - A newly printed question label begins a new item at that hierarchy level. Do not absorb it into the preceding item.\n\n"
        "3. PRESERVE MARKS ACCURATELY:\n"
        "   - Record exact marks notation (e.g. '2 x 10', '6 x 8', '16', '[2]').\n"
        "   - Do not confuse individual marks with total marks.\n"
        "   - Never invent missing marks. If a question or subquestion has no explicitly printed marks, leave marks null.\n\n"
        "4. SOURCE PAGE:\n"
        "   - Provide the 1-based page number where each question starts in `source_page`.\n\n"
        "5. LABELS AND QUESTION COVERAGE:\n"
        "   - For labels such as Q.1(a), store Q.1 as the parent label and (a) as the child label.\n"
        "   - Preserve all lettered/roman subparts in source order.\n"
        "   - Include questions that continue across page boundaries and assign the page where each starts.\n\n"
        "Never invent questions, answers, or text not present in the source."
    )),
    ("human", "Examination Paper Text:\n\n{paper_text}"),
])


def _reconcile_hierarchy(raw_questions: list[dict]) -> list[dict]:
    """Ensure subquestions are properly nested under their parent question.
    
    If an AI provider or regex produces flattened questions like Q2a, Q2b or parts a, b, c,
    group them into their parent question.
    """
    reconciled: list[dict] = []
    parent_map: dict[str, dict] = {}

    for q in raw_questions:
        q_text = (q.get("text") or "").strip()
        q_label = (q.get("label") or "").strip()

        sub_match = re.match(
            r'^(?:Q\.?\s*)?(\d+)\s*(?:[._/]\s*([a-zA-Z]|[ivxlcdmIVXLCDM]+|\([a-zA-Z0-9]+\))\b|([a-zA-Z])(?:\.|\)|\s*[-:])|\(([a-zA-Z0-9]+)\))',
            q_label,
        )
        if not sub_match:
            sub_match = re.match(
                r'^(?:Q\.?\s*)?(\d+)\s*(?:[._/]\s*([a-zA-Z]|[ivxlcdmIVXLCDM]+|\([a-zA-Z0-9]+\))\b|([a-zA-Z])(?:\.|\)|\s*[-:])|\(([a-zA-Z0-9]+)\))',
                q_text,
            )

        if sub_match:
            parent_num = sub_match.group(1)
            clean_sub_text = clean_question_text(q_text)
            sub_entry = {
                "text": clean_sub_text,
                "label": q.get("label"),
                "marks": q.get("marks"),
                "sub_questions": q.get("sub_questions", []),
            }

            if parent_num in parent_map:
                parent_map[parent_num]["sub_questions"].append(sub_entry)
            else:
                parent_q = {
                    # Keep an empty parent stem rather than inventing wording.
                    "text": "",
                    "label": f"Q{parent_num}",
                    "original_question_number": f"Q{parent_num}",
                    "marks": q.get("marks"),
                    "section_name": q.get("section_name"),
                    "section_instructions": q.get("section_instructions"),
                    "is_compulsory": q.get("is_compulsory"),
                    "source_page": q.get("source_page"),
                    "sub_questions": [sub_entry],
                }
                parent_map[parent_num] = parent_q
                reconciled.append(parent_q)
        else:
            clean_text = clean_question_text(q_text)
            q["text"] = clean_text
            # Clean subquestions recursively
            def preserve_child(child: dict) -> dict:
                copy = dict(child)
                copy["text"] = clean_question_text(child.get("text", ""))
                copy["sub_questions"] = [preserve_child(nested) for nested in child.get("sub_questions", [])]
                return copy
            q["sub_questions"] = [preserve_child(sq) for sq in q.get("sub_questions", [])]

            # Track parent number if identifiable
            num_match = re.search(r'\bQ\.?\s*(\d+)\b', q_label) or re.search(r'\bQ\.?\s*(\d+)\b', q_text)
            if num_match:
                parent_map[num_match.group(1)] = q

            reconciled.append(q)

    return reconciled


def segment_questions(state: PipelineState) -> PipelineState:
    """Use AI providers with fallback chain to segment questions."""
    pages = state["pages"]
    errors = list(state.get("errors", []))
    providers_used = list(state.get("providers_used", []))
    raw_segments: list[dict] = []

    # Group pages by PDF
    pdf_groups: dict[str, list[dict]] = {}
    for p in pages:
        pdf_groups.setdefault(p["pdf_name"], []).append(p)

    manager = ProviderManager(preferred_provider=config.PREFERRED_PROVIDER)
    available_providers = manager.validate_providers()
    logger.info("Available AI providers: %s", available_providers)

    for pdf_name, p_list in pdf_groups.items():
        p_list.sort(key=lambda x: int(x["page_number"]))
        page_nums = [int(p["page_number"]) for p in p_list]

        page_chunks = []
        for p in p_list:
            chunk = f"=== Page {p['page_number']} ===\n{p.get('raw_text', '')}"
            for table in p.get("tables", []):
                chunk += "\n[STRUCTURED TABLE " + str(table.get("table_id", "")) + "]\n"
                chunk += json.dumps({"headers": table.get("headers", []), "rows": table.get("rows", []), "cells": table.get("cells", [])}, ensure_ascii=False)
            page_chunks.append(chunk)
        combined_text = "\n\n".join(page_chunks)

        if not combined_text.strip():
            errors.append(f"Empty text for PDF {pdf_name}")
            continue

        def _call_model(model):
            chain = SEGMENTATION_PROMPT | model
            return chain.invoke({"paper_text": combined_text})

        result = manager.invoke_with_fallback(
            _call_model,
            structured_output=ExtractedPaper,
        )

        if result.success and result.result is not None:
            providers_used.append(result.provider_name)
            extracted: ExtractedPaper = result.result
            extracted_dict = extracted.model_dump()
            extracted_dict["questions"] = _reconcile_hierarchy(extracted_dict.get("questions", []))
            expected_numbers = {
                match.group(1)
                for page in p_list
                for match in re.finditer(r"(?im)^\s*Q\.?\s*(\d+)\b", page.get("raw_text", ""))
            }
            returned_numbers = set()
            for question in extracted_dict.get("questions", []):
                label = str(question.get("label") or "")
                match = re.search(r"(?:Q\.?\s*)?(\d+)", label)
                if match:
                    returned_numbers.add(match.group(1))
            missing_numbers = expected_numbers - returned_numbers
            if missing_numbers:
                logger.warning(
                    "AI segmentation for %s omitted question numbers %s; using local label-based fallback",
                    pdf_name, sorted(missing_numbers, key=int),
                )
                fallback = _regex_segment(pdf_name, p_list)
                raw_segments.append(fallback)
                providers_used.append("regex_fallback")
            else:
                raw_segments.append({
                    "pdf_name": pdf_name,
                    "pages": page_nums,
                    "extraction": extracted_dict,
                    "page_data": p_list,
                    "method": f"ai_{result.provider_name}",
                })
                logger.info("Successfully segmented %s using %s (%d questions)", pdf_name, result.provider_name, len(extracted.questions))
        else:
            errors.append(f"AI segmentation failed for {pdf_name}: {result.error}")
            logger.warning("Falling back to regex segmentation for %s", pdf_name)
            fallback = _regex_segment(pdf_name, p_list)
            raw_segments.append(fallback)
            providers_used.append("regex_fallback")

    return {
        "raw_segments": raw_segments,
        "providers_used": list(set(providers_used)),
        "errors": errors,
    }


def _regex_segment(pdf_name: str, pages: list[dict], *, lite_mode: bool = False) -> dict:
    """Fallback segmenter that retains sections, labels, and page-spanning text."""
    questions_by_number: dict[str, dict] = {}
    if lite_mode:
        main_pattern = re.compile(
            r"^\s*(?:"
            r"Q\.?\s*(?P<qnum>\d+)\s*(?:\(\s*(?P<qletter>[a-z])\s*\)|(?P<qletter_dot>[a-z])\s*[.)])?"
            r"|Question\s+(?P<question_num>\d+)"
            r")\s*(?P<text>.*)$",
            re.I,
        )
    else:
        main_pattern = re.compile(
            r"^\s*(?:Q\.?\s*(\d+)\s*(?:\(\s*([a-z])\s*\)|([a-z])\s*[.)])?|Question\s+(\d+))\s*(.*)$",
            re.I,
        )
    child_pattern = re.compile(r"^\s*(?:\(\s*([a-z])\s*\)|([a-z])\s*[.)])\s*(.*)$", re.I)
    section_pattern = re.compile(
        r"^\s*(Part\s*[- ]?\s*[IVX0-9il]+|Section\s+[A-Z0-9]+|Instructions?)\s*:?\s*$",
        re.I,
    )
    instruction_pattern = re.compile(r"^\s*(Only\b|Answer\b|Attempt\b|Choose\b)", re.I)
    nested_pattern = re.compile(r"^\s*(?:\(\s*([ivxlcdm]+)\s*\)|([ivxlcdm]+)[.)])\s*(.*)$", re.I)
    active_number = None
    active_label = None
    active_block: list[str] = []
    active_pages: list[int] = []
    active_page = 1
    active_section = None
    active_marks = None
    current_section = None
    current_instruction = None

    def flush_block():
        nonlocal active_number, active_label, active_block, active_marks, active_pages
        if active_number is None:
            active_block = []
            return
        number = str(active_number)
        parent = questions_by_number.setdefault(number, {
            "label": f"Q{number}", "original_question_number": f"Q{number}",
            "text": "", "marks": None,
            "section_name": (
                None
                if lite_mode and str(active_section or "").casefold().startswith("instruction")
                else active_section
            ),
            "section_instructions": current_instruction, "source_page": active_page,
            "sub_questions": [], "tables": [],
        })
        if lite_mode:
            pages = parent.setdefault("source_pages", [])
            pages.extend(page for page in active_pages if page not in pages)
        raw = "\n".join(active_block).strip()
        marks_match = re.search(r"[\[(]\s*([0-9]+(?:\s*[+x×*]\s*[0-9]+)*)\s*[\])]", raw)
        marks = marks_match.group(1).strip() if marks_match else active_marks
        def nested_parts(value: str) -> tuple[str, list[dict]]:
            lines = value.splitlines()
            starts = [(idx, match.group(1) or match.group(2), match.group(3) or "") for idx, line in enumerate(lines) if (match := nested_pattern.match(line))]
            if not starts:
                return value, []
            stem = "\n".join(lines[:starts[0][0]]).strip()
            nested = []
            for pos, (line_idx, roman_label, first_text) in enumerate(starts):
                end_idx = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
                body = "\n".join([first_text, *lines[line_idx + 1:end_idx]]).strip()
                nested.append({"label": f"({roman_label.lower()})", "text": body, "marks": None, "sub_questions": [], "tables": []})
            return stem, nested
        if active_label:
            stem, nested = nested_parts(raw)
            parent["sub_questions"].append({"label": active_label, "text": stem, "marks": marks, "sub_questions": nested, "tables": []})
        elif raw:
            raw_lines = raw.splitlines()
            child_starts = [
                (idx, m.group(1) or m.group(2), m.group(3) or "")
                for idx, line in enumerate(raw_lines)
                if (m := child_pattern.match(line))
                and (
                    not lite_mode
                    or not re.match(r"^\s*\(\s*[ivxlcdm]+\s*\)", line, re.I)
                )
                and not re.match(r"^\s*[ivxlcdm]+\.", line, re.I)
            ]
            if child_starts:
                stem_lines = raw_lines[:child_starts[0][0]]
                stem = " ".join(part.strip() for part in stem_lines if part.strip())
                if stem and not parent["text"]:
                    parent["text"] = stem
                for pos, (line_idx, child_label, first_text) in enumerate(child_starts):
                    end_idx = child_starts[pos + 1][0] if pos + 1 < len(child_starts) else len(raw_lines)
                    child_text = "\n".join([first_text, *raw_lines[line_idx + 1:end_idx]]).strip()
                    child_text, nested = nested_parts(child_text)
                    child_mark = re.search(r"[\[(]\s*([0-9]+(?:\s*[+x×*]\s*[0-9]+)*)\s*[\])]", child_text)
                    parent["sub_questions"].append({"label": f"{child_label.lower()})", "text": child_text, "marks": child_mark.group(1).strip() if child_mark else None, "sub_questions": nested, "tables": []})
            elif not parent["text"]:
                parent["text"] = raw
            if marks and parent["marks"] is None:
                parent["marks"] = marks
        active_number = active_label = None
        active_block = []
        active_marks = None
        active_pages = []

    for page in sorted(pages, key=lambda row: int(row.get("page_number", 1))):
        page_num = int(page.get("page_number", 1))
        for line in (page.get("raw_text") or "").splitlines():
            sec = section_pattern.match(line)
            if sec:
                # Flush before switching sections; never attach a heading to
                # the previous question.
                flush_block()
                current_section = " ".join(sec.group(1).split())
                current_instruction = None
                continue
            numbered_instruction_line = bool(
                current_section
                and current_section.casefold().startswith("instruction")
                and re.match(r"^\s*\d+[.)]\s+", line)
            )
            if (
                current_section
                and active_number is None
                and (
                    current_section.casefold().startswith("instruction")
                    or instruction_pattern.match(line)
                )
                and (not main_pattern.match(line) or numbered_instruction_line)
            ):
                instruction = line.strip()
                if instruction:
                    current_instruction = (
                        f"{current_instruction}\n{instruction}"
                        if current_instruction
                        else instruction
                    )
                continue
            header = main_pattern.match(line)
            if header:
                flush_block()
                if lite_mode:
                    active_number = (
                        header.group("qnum")
                        or header.group("question_num")
                    )
                    letter = header.group("qletter") or header.group("qletter_dot")
                    remainder = header.group("text") or ""
                else:
                    active_number = header.group(1) or header.group(4)
                    letter = header.group(2) or header.group(3)
                    remainder = header.group(5) or ""
                active_label = f"({letter.lower()})" if letter else None
                active_page = page_num
                active_section = current_section
                active_pages = [page_num]
                if remainder.strip():
                    active_block.append(remainder.strip())
                continue
            if active_number is not None:
                active_block.append(line)
                if page_num not in active_pages:
                    active_pages.append(page_num)
    flush_block()

    output_questions = list(questions_by_number.values())
    return {
        "pdf_name": pdf_name,
        "pages": [int(p["page_number"]) for p in pages],
        "extraction": {"questions": output_questions},
        "page_data": pages,
        "method": "regex_fallback",
    }


# ═══════════════════════════════════════════════════════════════════════
# Stage 4: Marks extraction and hierarchy
# ═══════════════════════════════════════════════════════════════════════

def _parse_marks_notation(raw: str | None) -> tuple[float | None, float | None, list[str]]:
    """Parse marks from notations like [2], (2 x 10), (6 x 8), 0.5+1+0.5, 6, [16].

    Returns:
        (total_marks, individual_mark, components)
    """
    if not raw:
        return None, None, []
    s = str(raw).strip().strip("[]()").strip()
    s = re.sub(r'[Mm](?:arks?)?$', '', s).strip()

    # Multiplication pattern e.g. "2 x 10" or "6 x 8"
    mult = re.match(r'^(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)$', s)
    if mult:
        each_mark = float(mult.group(1))
        count = float(mult.group(2))
        total_mark = each_mark * count
        return total_mark, each_mark, [f"{mult.group(1)} each", f"total {total_mark}"]

    # Addition pattern e.g. "0.5+1+0.5+1+1+1"
    if "+" in s:
        parts = [p.strip() for p in s.split("+") if p.strip()]
        try:
            total = sum(float(p) for p in parts)
            return total, None, parts
        except ValueError:
            return None, None, parts

    try:
        val = float(s)
        return val, val, [s]
    except ValueError:
        return None, None, [s] if s else []


def _match_source_pages(question_text: str, pages: list[dict]) -> list[int]:
    """Find which page(s) a question appears on by searching raw page text."""
    if not pages:
        return [1]
    clean = " ".join(question_text.split())
    if not clean:
        return [int(pages[0].get("page_number", 1))]

    words = clean.split()
    sample = " ".join(words[:min(6, len(words))]).lower()

    matched = []
    for p in pages:
        p_raw = p.get("raw_text", "").lower()
        if sample and sample in p_raw:
            matched.append(int(p["page_number"]))

    if matched:
        return matched

    for i in range(len(words) - 3):
        chunk = " ".join(words[i:i + 4]).lower()
        for p in pages:
            if chunk in p.get("raw_text", "").lower():
                p_num = int(p["page_number"])
                if p_num not in matched:
                    matched.append(p_num)
        if matched:
            return matched

    return [int(pages[0].get("page_number", 1))]


def _find_matching_pages(question_text: str, pages: list[dict]) -> list[int]:
    """Return only pages where a question's distinctive opening text occurs."""
    words = " ".join((question_text or "").split()).casefold().split()
    if len(words) < 3:
        return []
    sample = " ".join(words[:min(8, len(words))])
    return [
        int(page.get("page_number", 1))
        for page in pages
        if sample in " ".join(str(page.get("raw_text", "")).split()).casefold()
    ]


def _build_subquestions(
    sub_dicts: list[dict],
    pdf_name: str,
    parent_q_id: str,
    page_num: int,
    inherited_mark: float | None,
    inherited_notation: str | None,
    is_fallback: bool,
    parent_question_ref: str | None = None,
    inherited_source_pages: list[int] | None = None,
    page_data: list[dict] | None = None,
) -> list[SubQuestion]:
    """Recursively construct SubQuestion models without labels."""
    sub_qs: list[SubQuestion] = []
    inherited_pages = inherited_source_pages if inherited_source_pages is not None else [page_num]
    for idx, sq in enumerate(sub_dicts):
        sq_text_raw = sq.get("text", "") or sq.get("original_text", "")
        orig_label, cleaned_from_label = _extract_original_label(sq_text_raw)
        sq_text = clean_question_text(cleaned_from_label if cleaned_from_label else sq_text_raw)
        sq_text = remove_structured_table_text(sq_text, sq.get("tables", []))
        source_pages = _find_matching_pages(sq_text_raw, page_data or []) or inherited_pages
        sq_marks_raw = sq.get("marks") or sq.get("marks_notation")
        sq_total_marks, sq_each_marks, _ = _parse_marks_notation(sq_marks_raw)

        # Use explicitly stated marks if present, else inherit from multiplication notation
        if sq_total_marks is not None:
            final_sq_marks = sq_total_marks
            final_sq_notation = sq_marks_raw
        elif inherited_mark is not None:
            final_sq_marks = inherited_mark
            final_sq_notation = inherited_notation
        else:
            final_sq_marks = None
            final_sq_notation = None

        sub_id = generate_question_id(pdf_name, f"{parent_q_id}_{idx}_{sq_text[:30]}", page_num)

        sq_orig_ref = sq.get("original_question_number") or sq.get("label") or orig_label
        sq_parent_ref = parent_question_ref
        if parent_question_ref and sq_orig_ref:
            p_ref_str = str(parent_question_ref).strip()
            s_ref_str = str(sq_orig_ref).strip()
            if p_ref_str and s_ref_str and not s_ref_str.lower().startswith(p_ref_str.lower()):
                m_p = re.search(r'([Qq]?\d+)', p_ref_str)
                if m_p:
                    # normalize like Q6(b)
                    s_clean = s_ref_str.rstrip(').:')
                    if s_clean.startswith('('):
                        s_clean = s_clean.strip('()')
                    combined = f"{m_p.group(1)}({s_clean})"
                else:
                    s_clean = s_ref_str.rstrip(').:')
                    combined = f"{p_ref_str}({s_clean})"
                sq_parent_ref_combined = combined
            else:
                sq_parent_ref_combined = sq_orig_ref if sq_orig_ref else parent_question_ref
        else:
            sq_parent_ref_combined = sq_orig_ref or parent_question_ref

        # Nested subquestions (e.g. I, II, III under part l)
        nested_sub_qs = _build_subquestions(
            sq.get("sub_questions", []),
            pdf_name=pdf_name,
            parent_q_id=sub_id,
            page_num=page_num,
            inherited_mark=None,  # Do not cascade marks into deeper levels unless explicit
            inherited_notation=None,
            is_fallback=is_fallback,
            parent_question_ref=sq_parent_ref_combined or sq_orig_ref or parent_question_ref,
            inherited_source_pages=source_pages,
            page_data=page_data,
        )

        sub_qs.append(SubQuestion(
            sub_id=sub_id,
            original_text=sq_text_raw,
            cleaned_text=sq_text,
            original_label=sq.get("label") or orig_label,
            original_question_number=sq_orig_ref,
            parent_question_ref=sq_parent_ref_combined or parent_question_ref,
            marks=final_sq_marks,
            marks_notation=final_sq_notation,
            sub_questions=nested_sub_qs,
            tables=[
                TableReference(
                    table_id=str(table.get("table_id") or f"p{table.get('source_page', page_num)}-t{table_idx + 1}"),
                    headers=[str(cell) for cell in table.get("headers", [])],
                    rows=[[str(cell) for cell in row] for row in table.get("rows", [])],
                    cells=[[str(cell) for cell in row] for row in table.get("cells", [])],
                    source_page=table.get("source_page", page_num),
                    bbox=table.get("bbox", []),
                    extraction_method=table.get("extraction_method"),
                )
                for table_idx, table in enumerate(sq.get("tables", []))
                if isinstance(table, dict)
            ],
            source_pdf=pdf_name,
            source_pages=list(source_pages) if source_pages else [],
            confidence=0.75 if is_fallback else 0.95,
        ))
    return sub_qs


def extract_marks(state: PipelineState) -> PipelineState:
    """Organize extracted questions into generic QuestionRecords with accurate marks."""
    raw_segments = state.get("raw_segments", [])
    errors = list(state.get("errors", []))
    question_records: list[QuestionRecord] = []

    for seg in raw_segments:
        pdf_name = seg["pdf_name"]
        pages = seg.get("pages", [1])
        page_data = seg.get("page_data", [])
        ext = seg.get("extraction", {})
        is_fallback = "fallback" in seg.get("method", "")

        extracted_questions = ext.get("questions", [])
        implicit_tables: dict[int, list[dict]] = {}
        implicit_sub_tables: dict[tuple[int, int], list[dict]] = {}
        for page in page_data:
            tables = page.get("tables", [])
            if not tables:
                continue
            page_no = int(page.get("page_number", 1))
            candidates = []
            for idx, item in enumerate(extracted_questions):
                item_pages = _find_matching_pages(str(item.get("text", "")), page_data)
                on_page = int(item.get("source_page") or 0) == page_no or page_no in item_pages
                if on_page:
                    candidates.append((idx, None, item))
                for sub_idx, sub in enumerate(item.get("sub_questions", [])):
                    sub_pages = _find_matching_pages(str(sub.get("text", "")), page_data)
                    if page_no in sub_pages:
                        candidates.append((idx, sub_idx, sub))
            for table in tables:
                if not candidates:
                    continue
                def table_relevance(item: dict) -> int:
                    text = str(item.get("text", "")).casefold()
                    return sum(weight for term, weight in (
                        ("table", 4), ("arrival", 4), ("burst", 4),
                        ("process", 2), ("scheduling", 2), ("given below", 1),
                    ) if term in text)
                ranked = sorted(
                    ((table_relevance(item), idx, sub_idx) for idx, sub_idx, item in candidates),
                    key=lambda candidate: (
                        candidate[0],
                        candidate[1],
                        candidate[2] is not None,
                        candidate[2] if candidate[2] is not None else -1,
                    ),
                    reverse=True,
                )
                # Never duplicate page tables across unrelated questions.
                if ranked and ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
                    _, q_idx, sub_idx = ranked[0]
                    table_data = {**table, "source_page": page_no}
                    if sub_idx is None:
                        implicit_tables.setdefault(q_idx, []).append(table_data)
                    else:
                        implicit_sub_tables.setdefault((q_idx, sub_idx), []).append(table_data)

        for q_index, q_dict in enumerate(extracted_questions):
            for sub_index, sub_dict in enumerate(q_dict.get("sub_questions", [])):
                if implicit_sub_tables.get((q_index, sub_index)) and not sub_dict.get("tables"):
                    sub_dict["tables"] = implicit_sub_tables[(q_index, sub_index)]
            text_raw = q_dict.get("text", "")
            clean_text = clean_question_text(text_raw)
            clean_text = remove_structured_table_text(clean_text, q_dict.get("tables", []) or implicit_tables.get(q_index, []))
            raw_marks = q_dict.get("marks")
            total_marks, ind_mark, marks_comps = _parse_marks_notation(raw_marks)
            priority_match = re.search(r"\bpriority(?:\s*(?:level|order))?\s*[:=]?\s*([123])\b", str(text_raw or ""), re.IGNORECASE)
            q_priority = int(priority_match.group(1)) if priority_match and priority_match.group(1) in {"1", "2", "3"} else None

            # Match question to its actual page(s)
            q_pages = (
                [int(q_dict["source_page"])]
                if q_dict.get("source_page")
                else (_match_source_pages(text_raw, page_data) if page_data else pages)
            )
            first_page = q_pages[0] if q_pages else 1

            q_orig_label, q_cleaned_from_label = _extract_original_label(text_raw)
            q_orig_label = q_dict.get("label") or q_orig_label
            q_orig_ref = q_dict.get("original_question_number") or q_dict.get("label") or q_orig_label
            # Include the printed label so distinct blank-stem parents do not
            # collapse to the same deterministic ID during normalization.
            q_id = generate_question_id(pdf_name, q_orig_ref or clean_text[:40], first_page)

            # Inherit individual marks only for multiplication patterns like (2 x 10)
            inherited_sub_mark = ind_mark if (raw_marks and any(x in str(raw_marks).lower() for x in ("x", "×", "*"))) else None
            inherited_sub_notation = str(int(ind_mark)) if (inherited_sub_mark is not None and ind_mark.is_integer()) else (str(inherited_sub_mark) if inherited_sub_mark is not None else None)

            sub_qs = _build_subquestions(
                q_dict.get("sub_questions", []),
                pdf_name=pdf_name,
                parent_q_id=q_id,
                page_num=first_page,
                inherited_mark=inherited_sub_mark,
                inherited_notation=inherited_sub_notation,
                is_fallback=is_fallback,
                page_data=page_data,
            )

            # If parent question had no marks but subquestions have marks, calculate total if appropriate
            if total_marks is None and sub_qs:
                sub_sum = sum(sq.marks for sq in sub_qs if sq.marks is not None)
                if sub_sum > 0:
                    total_marks = sub_sum

            q_clean_text_final = clean_text if clean_text and clean_text != text_raw.strip() else (q_cleaned_from_label or clean_text)

            record = QuestionRecord(
                question_id=q_id,
                original_text=text_raw,
                cleaned_text=q_clean_text_final,
                original_label=q_dict.get("label") or q_orig_label,
                original_question_number=q_orig_ref,
                parent_question_ref=q_dict.get("parent_question_ref"),
                marks=total_marks,
                marks_notation=raw_marks,
                marks_components=marks_comps,
                marks_notation_preserved=True,
                priority=q_priority,
                sub_questions=sub_qs,
                section_name=q_dict.get("section_name"),
                section_instructions=q_dict.get("section_instructions"),
                is_compulsory=q_dict.get("is_compulsory"),
                source_pdf=pdf_name,
                source_pages=q_pages,
                tables=[
                    TableReference(
                        table_id=str(table.get("table_id") or f"p{table.get('source_page', first_page)}-t{idx + 1}"),
                        headers=[str(cell) for cell in table.get("headers", [])],
                        rows=[[str(cell) for cell in row] for row in table.get("rows", [])],
                        cells=[[str(cell) for cell in row] for row in table.get("cells", [])],
                        source_page=table.get("source_page", first_page),
                        bbox=table.get("bbox", []),
                        extraction_method=table.get("extraction_method"),
                    )
                    for idx, table in enumerate(q_dict.get("tables") or implicit_tables.get(q_index, []))
                    if isinstance(table, dict)
                ],
                confidence=0.65 if is_fallback else 0.92,
                review_status=ReviewStatus.PENDING,
            )

            if is_fallback:
                record.processing_notes.append("Segmented via fallback parser")
                record.review_status = ReviewStatus.INCOMPLETE

            question_records.append(record)

    return {
        "question_records": question_records,
        "errors": errors,
    }


# ═══════════════════════════════════════════════════════════════════════
# Stage 5: Asset association & filtering
# ═══════════════════════════════════════════════════════════════════════

FIGURE_KEYWORDS = (
    "figure", "diagram", "graph", "table", "chart", "plot",
    "markov", "circuit", "shown below", "given below", "burst time", "arrival time",
    "refer", "illustration", "curve", "schematic"
)


def is_genuine_figure_asset(
    asset_path: str,
    project_root: Path,
    manifest_page_info: dict | None = None,
) -> bool:
    """Filter out whole-page background scans, tiny stamps, and divider lines.

    Returns True ONLY for genuine question diagrams, charts, plots, and figures.
    """
    resolved = Path(asset_path)
    if not resolved.is_file():
        resolved = project_root / asset_path
    if not resolved.is_file():
        return False

    width, height = None, None
    try:
        from PIL import Image
        with Image.open(resolved) as img:
            width, height = img.size
    except Exception:
        return False

    # 1. Reject tiny stamps, barcodes, paper code labels, icons
    if height <= 45 or width <= 50 or (width * height < 6000):
        return False

    # 2. Reject horizontal / vertical divider lines
    if height <= 15 or width <= 15:
        return False
    aspect_ratio = width / height
    if aspect_ratio > 12.0 or aspect_ratio < 0.08:
        return False

    # 3. Reject full-page scans and backgrounds
    # In scanned A4 PDFs (height >= 1000 and width >= 750 with aspect ratio ~ 0.707)
    if height >= 1000 and width >= 750 and (0.60 <= aspect_ratio <= 0.85):
        return False

    if manifest_page_info:
        render_w = manifest_page_info.get("render_width_px")
        render_h = manifest_page_info.get("render_height_px")
        if render_w and render_h:
            if width >= 0.75 * render_w and height >= 0.75 * render_h:
                return False
        if manifest_page_info.get("native_text_characters", 1) == 0 and height >= 900:
            return False

    return True


def associate_assets(state: PipelineState) -> PipelineState:
    """Link genuine images, diagrams, and figures to questions using page & keyword matching."""
    pages = state["pages"]
    question_records = list(state.get("question_records", []))
    errors = list(state.get("errors", []))
    asset_map: dict[str, list[AssetReference]] = {}

    project_root = PROJECT_ROOT

    # Group questions by (pdf_name, page_number)
    page_to_qs: dict[tuple[str, int], list[QuestionRecord]] = {}
    for q in question_records:
        for p_num in q.source_pages:
            page_to_qs.setdefault((q.source_pdf, p_num), []).append(q)

    for page in pages:
        pdf_name = page["pdf_name"]
        p_num = int(page["page_number"])
        figure_paths = page.get("figure_paths", [])

        if not figure_paths:
            continue

        qs_on_page = page_to_qs.get((pdf_name, p_num), [])

        for f_path in figure_paths:
            if not is_genuine_figure_asset(f_path, project_root, manifest_page_info=page):
                logger.info("Filtered out non-figure image: %s", f_path)
                continue

            resolved = Path(f_path)
            if not resolved.is_file():
                resolved = project_root / f_path
            verified = resolved.is_file()

            asset_ref = AssetReference(
                asset_id=generate_question_id(pdf_name, f_path, p_num),
                asset_type="figure" if "fig" in f_path.lower() or "draw" in f_path.lower() else "image",
                path=f_path,
                source_page=p_num,
                verified=verified,
            )

            # Prefer specific references to tables/figures over generic
            # mentions such as "diagram" so nearby questions do not steal an asset.
            keyword_weights = {
                "figure": 6, "table": 5, "circuit": 5, "burst time": 4,
                "arrival time": 4, "graph": 3, "chart": 3, "plot": 3,
                "diagram": 2, "markov": 2, "shown below": 1,
                "given below": 1, "refer": 1, "illustration": 1,
            }

            def relevance(text: str) -> int:
                lowered = text.lower()
                return sum(weight for term, weight in keyword_weights.items() if term in lowered)

            candidates: list[tuple[int, QuestionRecord, SubQuestion | None]] = []
            for q in qs_on_page:
                q_score = relevance(q.cleaned_text)
                if q_score:
                    candidates.append((q_score, q, None))
                for sq in q.sub_questions:
                    sq_score = relevance(sq.cleaned_text)
                    if sq_score:
                        candidates.append((sq_score, q, sq))

            matched_q: QuestionRecord | None = None
            matched_sq: SubQuestion | None = None
            if candidates:
                _, matched_q, matched_sq = max(candidates, key=lambda candidate: candidate[0])

            # If single question on page, link to it
            if not matched_q and len(qs_on_page) == 1:
                matched_q = qs_on_page[0]

            if matched_q:
                if matched_sq is not None:
                    matched_sq.assets.append(asset_ref)
                matched_q.assets.append(asset_ref)
                asset_map.setdefault(matched_q.question_id, []).append(asset_ref)
            else:
                logger.info("Figure %s on page %d has no matching question on that page", f_path, p_num)

    return {
        "asset_map": asset_map,
        "question_records": question_records,
        "errors": errors,
    }


# ═══════════════════════════════════════════════════════════════════════
# Stage 6: Normalize and deduplicate
# ═══════════════════════════════════════════════════════════════════════

def normalize(state: PipelineState) -> PipelineState:
    """Deduplicate records by stable ID, keeping the highest confidence."""
    question_records = list(state.get("question_records", []))
    deduped: dict[str, QuestionRecord] = {}

    for q in question_records:
        if q.question_id in deduped:
            if q.confidence > deduped[q.question_id].confidence:
                deduped[q.question_id] = q
        else:
            deduped[q.question_id] = q

    # Models sometimes repeat a page-level table on a nested child after it
    # has already been associated with the containing subquestion. Keep one
    # best-quality copy so DOCX reconstruction cannot duplicate the table.
    for question in deduped.values():
        seen_tables: dict[str, tuple[list[TableReference], int, int]] = {}

        def table_quality(table: TableReference) -> int:
            cell_count = sum(len(row) for row in table.cells)
            content = table.cells or ([table.headers] if table.headers else []) + table.rows
            text_size = sum(len(str(cell)) for row in content for cell in row)
            return cell_count * 100 + text_size + (10 if table.bbox else 0) + (5 if table.extraction_method else 0)

        def dedupe(items: list[TableReference]) -> None:
            index = 0
            while index < len(items):
                table = items[index]
                key = table.table_id or json.dumps(table.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
                previous = seen_tables.get(key)
                if previous is None:
                    seen_tables[key] = (items, index, table_quality(table))
                    index += 1
                    continue
                old_items, old_index, old_quality = previous
                quality = table_quality(table)
                if quality > old_quality:
                    old_items[old_index] = table
                    seen_tables[key] = (old_items, old_index, quality)
                items.pop(index)

        def dedupe_children(items: list[SubQuestion]) -> None:
            for item in items:
                dedupe(item.tables)
                dedupe_children(item.sub_questions)

        dedupe(question.tables)
        dedupe_children(question.sub_questions)

    return {
        "question_records": list(deduped.values()),
        "deduplicated": len(deduped) < len(question_records),
    }


# ═══════════════════════════════════════════════════════════════════════
# Stage 7: Validate
# ═══════════════════════════════════════════════════════════════════════

def validate(state: PipelineState) -> PipelineState:
    """Validate schema, marks totals, check for empty fields."""
    question_records = list(state.get("question_records", []))
    validation_issues: list[str] = []
    tables_by_pdf_page = {
        (page.get("pdf_name"), int(page.get("page_number", 1))): page.get("tables", [])
        for page in state.get("pages", [])
    }

    for q in question_records:
        if not q.original_text.strip() and not q.sub_questions:
            q.validation_issues.append("Question has no text or subquestions")
            q.review_status = ReviewStatus.FLAGGED

        if q.marks is None:
            q.validation_issues.append("Marks unspecified")

        for table in q.tables:
            cells = table.cells or ([table.headers] if table.headers else []) + table.rows
            if not cells or not any(any(str(cell).strip() for cell in row) for row in cells):
                q.validation_issues.append(f"Table {table.table_id} is empty or has no extracted cells")

        def has_child_table(items: list[SubQuestion]) -> bool:
            return any(item.tables or has_child_table(item.sub_questions) for item in items)

        q_has_table = bool(q.tables) or has_child_table(q.sub_questions)
        q_mentions_table = any(term in q.original_text.casefold() for term in ("table", "arrival time", "burst time", "processes are given"))
        if q_mentions_table and not q_has_table and any(tables_by_pdf_page.get((q.source_pdf, page_no)) for page_no in q.source_pages):
            q.validation_issues.append("Question refers to a table on its source page, but no table is linked; review association")

        def check_child_tables(items: list[SubQuestion], parent_ref: str) -> bool:
            found = False
            for item in items:
                reference = item.original_label or item.original_question_number or parent_ref
                mentions = any(term in item.original_text.casefold() for term in ("table", "arrival time", "burst time", "processes are given"))
                if mentions and not item.tables and any(tables_by_pdf_page.get((item.source_pdf or q.source_pdf, page_no)) for page_no in item.source_pages):
                    q.validation_issues.append(f"Subquestion {reference} refers to a table on its source page, but no table is linked; review association")
                    found = True
                found = check_child_tables(item.sub_questions, str(reference)) or found
            return found

        check_child_tables(q.sub_questions, q.original_question_number or q.original_label or "question")

        if re.search(r"\bQ\.?\s*\d+\b", q.cleaned_text, re.I):
            q.validation_issues.append("Question text contains another main-question label; possible segmentation boundary error")

        labels = [str(sq.original_label or sq.original_question_number or "").casefold() for sq in q.sub_questions]
        labels = [label for label in labels if label]
        if len(labels) != len(set(labels)):
            q.validation_issues.append("Duplicate subquestion labels")

        unverified = [a for a in q.assets if not a.verified]
        if unverified:
            q.validation_issues.append(f"{len(unverified)} unverified assets")

        if any("table" in issue.casefold() or "segmentation boundary" in issue.casefold() or "duplicate subquestion" in issue.casefold() for issue in q.validation_issues):
            q.review_status = ReviewStatus.FLAGGED
        validation_issues.extend(f"{q.question_id}: {i}" for i in q.validation_issues)

    return {
        "question_records": question_records,
        "validation_issues": validation_issues,
    }


# ═══════════════════════════════════════════════════════════════════════
# Stage 8: Persist
# ═══════════════════════════════════════════════════════════════════════

def persist(state: PipelineState) -> PipelineState:
    """Save one marks-indexed question bank per source PDF."""
    question_records = state.get("question_records", [])
    errors = state.get("errors", [])
    providers_used = state.get("providers_used", [])

    def count_tables(question: QuestionRecord) -> int:
        def count_children(items: list[SubQuestion]) -> int:
            return sum(len(item.tables) + count_children(item.sub_questions) for item in items)
        return len(question.tables) + count_children(question.sub_questions)

    bank = QuestionBank(
        schema_version=SCHEMA_VERSION,
        metadata=ProcessingMetadata(
            providers_used=providers_used,
            gpu_device=state.get("gpu_device"),
            total_pages_processed=len(state.get("pages", [])),
            total_questions_found=len(question_records),
            total_assets_linked=sum(len(q.assets) for q in question_records),
            total_tables_linked=sum(count_tables(q) for q in question_records),
            incomplete_records=sum(1 for q in question_records if q.review_status != ReviewStatus.PENDING),
            errors=errors,
            source_manifests=state.get("source_manifests", []),
        ),
        questions=question_records,
    )

    data = bank.to_output_dict()
    output_paths: list[Path] = []
    if config.QUESTIONS_OUTPUT_FILE:
        output_path = Path(config.QUESTIONS_OUTPUT_FILE)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        output_paths.append(output_path)
    else:
        output_dir = Path(config.QUESTIONS_OUTPUT_DIR)
        by_pdf: dict[str, list[QuestionRecord]] = {}
        for question in question_records:
            by_pdf.setdefault(question.source_pdf or "unknown.pdf", []).append(question)
        page_counts: dict[str, set[int]] = {}
        for page in state.get("pages", []):
            page_counts.setdefault(page.get("pdf_name") or "unknown.pdf", set()).add(int(page.get("page_number", 1)))
        for source_pdf, questions in by_pdf.items():
            safe_stem = pdf_stem(source_pdf)
            question_ids = {question.question_id for question in questions}
            pdf_data = dict(data)
            pdf_data["questions"] = [q.model_dump(mode="json") for q in questions]
            pdf_data["marks_index"] = {
                marks: [qid for qid in qids if qid in question_ids]
                for marks, qids in data.get("marks_index", {}).items()
                if any(qid in question_ids for qid in qids)
            }
            pdf_metadata = dict(data.get("metadata", {}))
            pdf_metadata["total_pages_processed"] = len(page_counts.get(source_pdf, set()))
            pdf_metadata["total_questions_found"] = len(questions)
            pdf_metadata["total_assets_linked"] = sum(len(q.assets) for q in questions)
            pdf_metadata["total_tables_linked"] = sum(count_tables(q) for q in questions)
            pdf_metadata["incomplete_records"] = sum(
                1 for q in questions if q.review_status != ReviewStatus.PENDING
            )
            pdf_metadata["source_manifests"] = [
                manifest for manifest in state.get("source_manifests", [])
                if Path(manifest).parent.name == safe_stem
            ]
            pdf_data["metadata"] = pdf_metadata
            output_path = stage_output_dir(
                "Parser", source_pdf, stage_root=output_dir
            ) / "questions_parsed.json"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(pdf_data, indent=2, ensure_ascii=False), encoding="utf-8")
            output_paths.append(output_path)

    for output_path in output_paths:
        logger.info("Saved question bank to %s", output_path)
    output_path = output_paths[0] if len(output_paths) == 1 else Path(config.QUESTIONS_OUTPUT_DIR)
    return {
        "output_path": str(output_path),
        "question_bank": bank,
    }


# ═══════════════════════════════════════════════════════════════════════
# LangGraph Assembly
# ═══════════════════════════════════════════════════════════════════════

def build_graph():
    """Build the LangGraph question-bank pipeline."""
    graph = StateGraph(PipelineState)
    graph.add_node("load_manifest", load_manifest)
    graph.add_node("ocr_reconcile", ocr_reconcile)
    graph.add_node("segment_questions", segment_questions)
    graph.add_node("extract_marks", extract_marks)
    graph.add_node("associate_assets", associate_assets)
    graph.add_node("normalize", normalize)
    graph.add_node("validate", validate)
    graph.add_node("persist", persist)

    graph.set_entry_point("load_manifest")
    graph.add_edge("load_manifest", "ocr_reconcile")
    graph.add_edge("ocr_reconcile", "segment_questions")
    graph.add_edge("segment_questions", "extract_marks")
    graph.add_edge("extract_marks", "associate_assets")
    graph.add_edge("associate_assets", "normalize")
    graph.add_edge("normalize", "validate")
    graph.add_edge("validate", "persist")
    graph.add_edge("persist", END)

    return graph.compile()


def run(source_path=None, manifest_paths=None):
    """Execute the pipeline on input files."""
    initial: dict[str, Any] = {}
    if source_path:
        initial["source_path"] = str(source_path)
    if manifest_paths:
        initial["manifest_paths"] = manifest_paths

    compiled = build_graph()
    result = compiled.invoke(initial)
    bank: QuestionBank = result.get("question_bank")

    if bank:
        print("\n" + "=" * 60)
        print(" QuesScrapper Question-Bank Builder ")
        print("=" * 60)
        print(f" Source manifests : {', '.join(bank.metadata.source_manifests)}")
        print(f" Pages processed  : {bank.metadata.total_pages_processed}")
        print(f" Questions found  : {bank.metadata.total_questions_found}")
        print(f" Assets linked    : {bank.metadata.total_assets_linked}")
        print(f" AI Provider used : {', '.join(bank.metadata.providers_used)}")
        if bank.metadata.gpu_device:
            print(f" GPU Device       : {bank.metadata.gpu_device}")
        print("-" * 60)
        print(" Questions by Marks (Primary Organization):")
        marks_dict = bank.by_marks()
        for marks_val, qs in marks_dict.items():
            label = f"{marks_val} marks" if marks_val is not None else "Unspecified marks"
            print(f"   • {label}: {len(qs)} question(s)")
            for q in qs[:3]:
                sample_text = (q.cleaned_text[:60] + "...") if len(q.cleaned_text) > 60 else q.cleaned_text
                print(f"       - {sample_text}")
            if len(qs) > 3:
                print(f"       ... and {len(qs) - 3} more")
        print("-" * 60)
        print(f" Output saved to  : {result.get('output_path')}")
        print("=" * 60 + "\n")
    return result


if __name__ == "__main__":
    run()
