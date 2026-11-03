"""Unit tests for functions.ReconstructQues.question_renderer."""

from pathlib import Path
from docx import Document
from functions.ReconstructQues.asset_handler import AssetHandler
from functions.ReconstructQues.question_renderer import QuestionRenderer


def test_format_marks_integers():
    """Test integer marks formatting."""
    assert QuestionRenderer.format_marks(1.0) == "[1 mark]"
    assert QuestionRenderer.format_marks(6.0) == "[6 marks]"
    assert QuestionRenderer.format_marks(16) == "[16 marks]"


def test_format_marks_floats():
    """Test fractional marks formatting."""
    assert QuestionRenderer.format_marks(2.5) == "[2.5 marks]"
    assert QuestionRenderer.format_marks(0.5) == "[0.5 marks]"


def test_format_marks_notations():
    """Test marks notation preservation."""
    assert QuestionRenderer.format_marks(2.0, "2 x 10") == "[2 x 10 marks]"
    assert QuestionRenderer.format_marks(16.0, "[16]") == "[16 marks]"
    assert QuestionRenderer.format_marks(None, "10 marks") == "[10 marks]"
    assert QuestionRenderer.format_marks(None, "1 mark") == "[1 mark]"


def test_format_marks_none_or_invalid():
    """Test that missing or invalid marks return None without error."""
    assert QuestionRenderer.format_marks(None) is None
    assert QuestionRenderer.format_marks(0) is None
    assert QuestionRenderer.format_marks(-5) is None
    assert QuestionRenderer.format_marks("invalid") is None


def test_render_nested_subquestions(tmp_path: Path):
    """Test rendering of questions with nested subquestions."""
    handler = AssetHandler(base_dir=tmp_path)
    renderer = QuestionRenderer(asset_handler=handler)
    doc = Document()

    q = {
        "label": "Q1",
        "cleaned_text": "Answer both parts:",
        "marks": 10.0,
        "sub_questions": [
            {
                "label": "a)",
                "cleaned_text": "Define kernel.",
                "marks": 5.0,
                "sub_questions": [
                    {
                        "label": "(i)",
                        "cleaned_text": "Monolithic kernel.",
                        "marks": 2.5,
                    }
                ],
            },
            {
                "label": "b)",
                "cleaned_text": "Define microkernel.",
                "marks": 5.0,
            },
        ],
    }

    renderer.render_question(doc, q)
    assert renderer.rendered_questions_count == 1
    assert renderer.rendered_subquestions_count == 3

    # Source references are collected for the document appendix, not inserted
    # after each question or subquestion.
    paragraphs = doc.paragraphs
    assert len(paragraphs) == 4
    # Main question: indent 0
    assert paragraphs[0].paragraph_format.left_indent is None or paragraphs[0].paragraph_format.left_indent.inches == 0.0
    # Subquestion a): indent approx 0.35 in
    assert round(paragraphs[1].paragraph_format.left_indent.inches, 2) == 0.35
    # Sub-subquestion (i): indent approx 0.70 in
    assert round(paragraphs[2].paragraph_format.left_indent.inches, 2) == 0.70
    # Subquestion b): indent approx 0.35 in
    assert round(paragraphs[3].paragraph_format.left_indent.inches, 2) == 0.35


def test_render_markdown_table(tmp_path: Path):
    """Test rendering of questions containing markdown tables."""
    handler = AssetHandler(base_dir=tmp_path)
    renderer = QuestionRenderer(asset_handler=handler)
    doc = Document()

    q = {
        "label": "Q2",
        "cleaned_text": (
            "Refer to the scheduling table below:\n"
            "| Process | Arrival Time | Burst Time |\n"
            "| --- | --- | --- |\n"
            "| P1 | 0 | 5 |\n"
            "| P2 | 2 | 3 |\n"
            "Compute the average waiting time."
        ),
        "marks": 6.0,
    }

    renderer.render_question(doc, q)
    assert len(doc.tables) == 1
    tbl = doc.tables[0]
    assert len(tbl.rows) == 3
    assert len(tbl.columns) == 3
    assert tbl.cell(0, 0).text == "Process"
    assert tbl.cell(1, 0).text == "P1"
    assert tbl.cell(1, 2).text == "5"


def test_render_structured_extracted_table(tmp_path: Path):
    handler = AssetHandler(base_dir=tmp_path)
    renderer = QuestionRenderer(asset_handler=handler)
    doc = Document()
    renderer.render_question(doc, {
        "label": "Q2(l)",
        "cleaned_text": "Use SRTF scheduling to calculate the completion times.",
        "tables": [{
            "table_id": "p2-t1",
            "headers": ["Process", "Arrival Time (ms)", "Burst Time (ms)"],
            "rows": [["P1", "0", "7"], ["P2", "2", "4"], ["P3", "4", "1"], ["P4", "5", "4"]],
        }],
    })
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert len(table.rows) == 5
    assert [cell.text for cell in table.rows[0].cells] == ["Process", "Arrival Time (ms)", "Burst Time (ms)"]
    assert [cell.text for cell in table.rows[4].cells] == ["P4", "5", "4"]


def test_section_heading_transitions(tmp_path: Path):
    """Test that section headings are rendered on transition and not repeated."""
    handler = AssetHandler(base_dir=tmp_path)
    renderer = QuestionRenderer(asset_handler=handler)
    doc = Document()

    questions = [
        {"label": "Q1", "cleaned_text": "First Q", "section_name": "Part-I"},
        {"label": "Q2", "cleaned_text": "Second Q", "section_name": "Part-I"},
        {"label": "Q3", "cleaned_text": "Third Q", "section_name": "Part-II"},
    ]

    for q in questions:
        renderer.render_question(doc, q)

    headings = [p.text for p in doc.paragraphs if p.text in ("PART-I", "PART-II")]
    assert headings == ["PART-I", "PART-II"]

