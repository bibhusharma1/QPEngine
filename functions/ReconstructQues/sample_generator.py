"""Build one reproducible sample DOCX from source-matched PDF question items."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import random
from typing import Any

from docx import Document
from docx.shared import Pt

from config.outputs import OUTPUT_ROOT
from .asset_handler import AssetHandler
from .json_loader import PROJECT_ROOT
from .question_renderer import QuestionRenderer
from .styles import apply_document_styles


@dataclass
class PdfSampleSummary:
    pdf_name: str
    matched_items: int
    selected_items: int


@dataclass
class CombinedSampleResult:
    docx_path: Path
    seed: int | None
    pdf_summaries: list[PdfSampleSummary]
    json_paths: list[Path]


def _text(item: dict[str, Any]) -> str:
    return str(item.get("cleaned_text") or item.get("original_text") or item.get("text") or "").strip()


def _first(item: dict[str, Any], *keys: str) -> Any:
    return next((item.get(k) for k in keys if item.get(k) not in (None, "")), None)


def _label(item: dict[str, Any], fallback: str) -> str:
    return str(_first(item, "original_question_number", "original_label", "label", "question_number", "number") or fallback).strip()


def _flatten_items(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten every independently selectable main question and subquestion."""
    found: list[dict[str, Any]] = []

    def visit(item: dict[str, Any], parent_ref: str | None = None, inherited: dict[str, Any] | None = None) -> None:
        inherited = inherited or {}
        source = _first(item, "source_pdf", "source_file", "pdf_path", "pdf_name") or inherited.get("source_pdf")
        pages = _first(item, "source_pages", "pages", "page_numbers", "source_page")
        if pages is None:
            pages = inherited.get("source_pages")
        section = item.get("section_name") or inherited.get("section_name")
        own_label = _label(item, "")
        ref = f"{parent_ref}({own_label.strip('()')})" if parent_ref and own_label else (own_label or parent_ref or "Question")
        if _text(item):
            record = {
                "label": ref,
                "text": _text(item),
                "marks": item.get("marks"),
                "marks_notation": item.get("marks_notation"),
                "assets": item.get("assets") or [],
                "section_name": section,
                "source_pdf": source,
                "source_pages": pages,
                "source_ref": ref,
                "json_context": inherited.get("json_context"),
            }
            found.append(record)
        next_inherited = {"source_pdf": source, "source_pages": pages, "section_name": section, "json_context": inherited.get("json_context")}
        for child in item.get("sub_questions") or []:
            if isinstance(child, dict):
                visit(child, ref, next_inherited)

    for question in questions:
        if isinstance(question, dict):
            visit(question)
    return found


def _source_matches(source: Any, pdf_path: Path) -> bool:
    if not source:
        return False
    source_string = str(source).strip().replace("\\", "/")
    candidate = Path(source_string).name
    return candidate.casefold() == pdf_path.name.casefold()


def generate_combined_sample(
    *,
    pdf_dir: str | Path,
    json_paths: list[str | Path],
    max_items_per_pdf: int = 5,
    seed: int | None = None,
    output_dir: str | Path | None = None,
    filename: str = "OperatingSys_sample.docx",
    strict_assets: bool = False,
) -> CombinedSampleResult:
    """Select individual items per discovered PDF and render one combined DOCX."""
    folder = Path(pdf_dir).expanduser().resolve()
    if not folder.exists():
        raise FileNotFoundError(f"PDF input does not exist: {folder}")
    if not (folder.is_dir() or (folder.is_file() and folder.suffix.casefold() == ".pdf")):
        raise NotADirectoryError(f"PDF input must be a folder or a PDF file: {folder}")
    if max_items_per_pdf < 1:
        raise ValueError("max_items_per_pdf must be positive")
    if folder.is_file() and folder.suffix.casefold() == ".pdf":
        pdf_paths = [folder]
    else:
        pdf_paths = sorted(folder.rglob("*.pdf"), key=lambda p: p.as_posix().casefold())
    if not pdf_paths:
        raise FileNotFoundError(f"No PDF files found in {folder}")

    resolved_json_paths = [Path(p).expanduser().resolve() for p in json_paths]
    all_items: list[dict[str, Any]] = []
    for json_path in resolved_json_paths:
        if not json_path.is_file():
            continue
        data = json.loads(json_path.read_text(encoding="utf-8"))
        questions = data.get("questions", []) if isinstance(data, dict) else data
        if not isinstance(questions, list):
            continue
        bank_items = _flatten_items(questions)
        for item in bank_items:
            item["json_context"] = json_path.parent
        all_items.extend(bank_items)

    rng = random.Random(seed)
    selected_by_pdf: list[tuple[Path, list[dict[str, Any]], int]] = []
    summaries: list[PdfSampleSummary] = []
    for pdf_path in pdf_paths:
        matching = [item for item in all_items if _source_matches(item.get("source_pdf"), pdf_path)]
        selected = rng.sample(matching, min(max_items_per_pdf, len(matching)))
        selected_by_pdf.append((pdf_path, selected, len(matching)))
        summaries.append(PdfSampleSummary(pdf_path.name, len(matching), len(selected)))

    doc = Document()
    apply_document_styles(doc)
    references: list[str] = []
    missing_assets: list[str] = []
    warnings: list[str] = []
    for pdf_path, selected, _count in selected_by_pdf:
        doc.add_heading(pdf_path.name, level=1)
        if not selected:
            doc.add_paragraph("No matching parsed question records were found for this PDF.")
            continue
        context_dir = selected[0].get("json_context")
        handler = AssetHandler(base_dir=PROJECT_ROOT, context_dir=context_dir, strict=strict_assets)
        renderer = QuestionRenderer(handler)
        for item in selected:
            renderer.render_question(doc, {
                "original_question_number": item["label"],
                "original_label": item["label"],
                "cleaned_text": item["text"],
                "marks": item["marks"],
                "marks_notation": item["marks_notation"],
                "assets": item["assets"],
                "section_name": item["section_name"],
                "source_pdf": str(item["source_pdf"]) if item["source_pdf"] else str(pdf_path),
                "source_pages": item["source_pages"],
                "sub_questions": [],
            })
        references.extend(renderer.source_references)
        missing_assets.extend(handler.missing_assets)
        warnings.extend(handler.warnings)

    doc.add_page_break()
    doc.add_heading("Processing accuracy notice", level=1)
    doc.add_paragraph(
        "OCR and parsing have a working accuracy target of approximately 85–90%; this is a target, not a guaranteed or independently verified result. "
        "OCR errors, incorrect question segmentation, missing metadata, and formatting issues may require manual review."
    )
    doc.add_heading("Source references", level=1)
    for reference in references:
        paragraph = doc.add_paragraph(reference, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(1)
        for run in paragraph.runs:
            run.font.size = Pt(8)

    target_dir = Path(output_dir).expanduser().resolve() if output_dir else OUTPUT_ROOT / "Reconstruct" / "_combined"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / (filename if filename.endswith(".docx") else f"{filename}.docx")
    doc.save(target_path)
    reopened = Document(target_path)
    if not reopened.paragraphs:
        raise RuntimeError(f"Generated sample document is empty: {target_path}")
    if missing_assets:
        warnings.extend(f"Missing asset: {asset}" for asset in missing_assets)
    return CombinedSampleResult(target_path, seed, summaries, resolved_json_paths)
