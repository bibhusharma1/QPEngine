"""Turn QuesScrapper's page-record JSON into a marks-indexed question bank.

This module replaces the DOCX-generating pipeline. It extracts, cleans,
categorizes, validates, and writes a legacy DOCX from Scrapper page-record JSON.

The previous DOCX pipeline is preserved in _legacy_write_documents() but
is no longer the default workflow.
"""

import json
import re
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from config import parser as config
from config.settings import SCRAPPER_ROOT
from config.outputs import stage_output_dir


# ═══════════════════════════════════════════════════════════════════════
# Legacy DOCX types (preserved for backward compatibility)
# ═══════════════════════════════════════════════════════════════════════

class LegacyState(TypedDict, total=False):
    source_path: str
    pages: list[dict]
    groups: dict[str, list[dict]]
    documents: list[str]


class ArrangedQuestion(BaseModel):
    label: str = Field(description="Question/subquestion label copied from source, if present")
    text: str = Field(description="Question wording copied verbatim from source text")


class ArrangedPage(BaseModel):
    heading: str | None = Field(description="A short heading copied or inferred from explicit page text")
    questions: list[ArrangedQuestion] = Field(description="Questions in source order; do not invent content")


def _arrange_with_model(raw_text: str):
    """Use LangChain structured output when an LLM is configured.

    The model may group and label existing text, but must preserve the
    source wording. If no model is configured, the original page text is
    emitted unchanged in the document.
    """
    import os

    model = os.environ.get("QUES_LLM_MODEL", "").strip()
    api_key = os.environ.get("QUES_LLM_API_KEY", "").strip()
    if not model or not api_key or not raw_text.strip():
        return None
    from langchain_openai import ChatOpenAI

    client = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=os.environ.get("QUES_LLM_BASE_URL") or None,
        temperature=float(os.environ.get("QUES_LLM_TEMPERATURE", "0")),
    ).with_structured_output(ArrangedPage)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "Organize exam-page text into questions in source order. "
         "Copy wording exactly. Do not solve, paraphrase, infer marks, or invent text. "
         "Keep instructions in the heading or question text."),
        ("human", "Page text:\n{raw_text}"),
    ])
    return (prompt | client).invoke({"raw_text": raw_text})


# ═══════════════════════════════════════════════════════════════════════
# Legacy helpers (preserved)
# ═══════════════════════════════════════════════════════════════════════

def load_json(state: LegacyState) -> LegacyState:
    explicit_source = state.get("source_path")
    configured = Path(explicit_source or config.QUESTIONS_JSON_FILE)
    if explicit_source and not configured.is_file() and not configured.is_dir():
        raise FileNotFoundError(f"Scraper JSON not found: {configured}")
    paths = [configured] if configured.is_file() else sorted(configured.glob("*/questions_scrapper.json"))
    if not paths:
        paths = sorted(Path(SCRAPPER_ROOT).glob("*/questions_scrapper.json"))
    if not paths:
        raise FileNotFoundError(f"Scraper JSON not found: {configured}")
    data = []
    for path in paths:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Expected a JSON array of page records in {path}")
        data.extend(rows)
    for index, row in enumerate(data):
        if not row.get("pdf_name") or "page_number" not in row:
            raise ValueError(f"Page record {index} needs pdf_name and page_number")
        if not isinstance(row.get("raw_text", ""), str):
            raise ValueError(f"Page record {index} raw_text must be a string")
    return {"pages": data}


def arrange_pages(state: LegacyState) -> LegacyState:
    groups: dict[str, list[dict]] = {}
    for row in state["pages"]:
        groups.setdefault(row["pdf_name"], []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: int(row["page_number"]))
    return {"groups": groups}


def _safe_stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name.replace("\\", "/")).stem).strip(" ._") or "questions"


def _arrangement_is_source_faithful(raw_text: str, arranged: ArrangedPage) -> bool:
    source = " ".join(raw_text.split())
    return all(
        " ".join(question.text.split()) in source
        for question in arranged.questions
        if question.text.strip()
    )


def write_documents(state: LegacyState) -> LegacyState:
    """Legacy DOCX writer — preserved for backward compatibility."""
    from docx import Document
    from docx.shared import Inches

    output_dir = Path(config.DOCUMENTS_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = []
    for pdf_name, pages in state["groups"].items():
        doc = Document()
        doc.add_heading(Path(pdf_name).stem, level=0)
        for page_index, page in enumerate(pages):
            doc.add_heading(f"Page {page['page_number']}", level=1)
            raw_text = page.get("raw_text", "").strip()
            arranged = None
            if raw_text:
                try:
                    arranged = _arrange_with_model(raw_text)
                    if arranged is not None and not _arrangement_is_source_faithful(raw_text, arranged):
                        arranged = None
                        doc.add_paragraph(
                            "AI output changed source wording; original text retained.",
                            style="Intense Quote",
                        )
                except Exception as exc:
                    doc.add_paragraph(f"AI arrangement unavailable; original text retained ({exc}).", style="Intense Quote")
            if arranged is not None:
                if arranged.heading:
                    doc.add_heading(arranged.heading, level=2)
                if arranged.questions:
                    for question in arranged.questions:
                        label = f"{question.label} " if question.label else ""
                        doc.add_paragraph(f"{label}{question.text}")
                else:
                    doc.add_paragraph(raw_text)
            elif raw_text:
                for paragraph in re.split(r"\n\s*\n", raw_text):
                    if paragraph.strip():
                        doc.add_paragraph(paragraph.strip())
            else:
                doc.add_paragraph("[No text was captured for this page.]", style="Intense Quote")
            for image_path in page.get("figure_paths", []):
                path = Path(image_path)
                if path.is_file():
                    try:
                        doc.add_picture(str(path), width=Inches(5.5))
                    except Exception:
                        doc.add_paragraph(f"[Could not embed figure: {path.name}]")
                else:
                    doc.add_paragraph(f"[Figure file missing: {path}]")
            if page_index < len(pages) - 1:
                doc.add_page_break()
        pdf_output_dir = stage_output_dir("Reconstruct", pdf_name, stage_root=output_dir)
        pdf_output_dir.mkdir(parents=True, exist_ok=True)
        output_path = pdf_output_dir / f"{_safe_stem(pdf_name)}.docx"
        doc.save(output_path)
        documents.append(str(output_path))
    return {"documents": documents}


def build_legacy_graph():
    """Build the legacy DOCX pipeline graph (preserved for backward compat)."""
    graph = StateGraph(LegacyState)
    graph.add_node("read_json", load_json)
    graph.add_node("arrange_pages", arrange_pages)
    graph.add_node("write_documents", write_documents)
    graph.set_entry_point("read_json")
    graph.add_edge("read_json", "arrange_pages")
    graph.add_edge("arrange_pages", "write_documents")
    graph.add_edge("write_documents", END)
    return graph.compile()


def run_legacy_docx(source_path=None):
    """Legacy: Read the scraper JSON and create one .docx per source PDF."""
    result = build_legacy_graph().invoke({"source_path": str(source_path)} if source_path else {})
    print(f"Read {len(result['pages'])} page record(s) from {source_path or config.QUESTIONS_JSON_FILE}")
    print(f"Created {len(result['documents'])} document(s):")
    for path in result["documents"]:
        print(f"  {path}")
    return result


# ═══════════════════════════════════════════════════════════════════════
# Default entry point — question bank (not DOCX)
# ═══════════════════════════════════════════════════════════════════════

def build_graph():
    """Build the question-bank pipeline graph (default)."""
    from functions.QuesParser.ParsePipeLIne.question_bank import build_graph as _build_qb_graph
    return _build_qb_graph()


def run(source_path=None):
    """Run the question-bank extraction pipeline (default).

    To use the legacy DOCX pipeline, call run_legacy_docx() instead.
    """
    from functions.QuesParser.ParsePipeLIne.question_bank import run as _run_qb
    return _run_qb(source_path=source_path)


if __name__ == "__main__":
    run()
