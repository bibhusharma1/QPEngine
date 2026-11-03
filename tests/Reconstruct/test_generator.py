"""Integration tests for functions.ReconstructQues.generator."""

import json
from pathlib import Path
from PIL import Image
import pytest

from docx import Document
from functions.ReconstructQues.generator import DocumentGenerator


@pytest.fixture
def sample_question_bank(tmp_path: Path) -> Path:
    """Create a sample question bank with metadata and embedded image."""
    img_path = tmp_path / "diagram.png"
    Image.new("RGB", (120, 80), color="green").save(img_path)

    data = {
        "schema_version": "1.0.0",
        "metadata": {
            "institution": "Biju Patnaik University of Technology",
            "exam_title": "Regular Examination",
            "course_code": "CSPC3002",
            "confidence": 0.96,
            "processing_notes": ["Pipeline run at 12:00"],
        },
        "questions": [
            {
                "question_id": "q1",
                "label": "Q1",
                "cleaned_text": "Answer all questions briefly:",
                "marks": 2.0,
                "marks_notation": "2 x 2",
                "section_name": "Part-A",
                "institution": "Biju Patnaik University of Technology",
                "course_code": "CSPC3002",
                "confidence": 0.99,
                "sub_questions": [
                    {
                        "sub_id": "sq1",
                        "label": "a)",
                        "cleaned_text": "What is paging?",
                        "marks": 2.0,
                    },
                    {
                        "sub_id": "sq2",
                        "label": "b)",
                        "cleaned_text": "What is segmentation?",
                        "marks": 2.0,
                    },
                ],
            },
            {
                "question_id": "q2",
                "label": "Q2",
                "cleaned_text": "Analyze the disk head scheduling for the given track request:",
                "marks": 16.0,
                "section_name": "Part-B",
                "assets": [
                    {
                        "asset_id": "fig1",
                        "path": "diagram.png",
                        "description": "Figure 1: Disk Request Graph",
                    }
                ],
            },
        ],
    }

    json_file = tmp_path / "questions_data.json"
    json_file.write_text(json.dumps(data), encoding="utf-8")
    return json_file


def test_end_to_end_generation_and_reopen(tmp_path: Path, sample_question_bank: Path):
    """Test full document generation, file creation, and reopening."""
    output_dir = tmp_path / "output_docs"
    generator = DocumentGenerator(
        json_path=sample_question_bank,
        output_dir=output_dir,
        base_dir=tmp_path,
    )

    result = generator.generate(filename="test_exam.docx")

    assert result.docx_path.is_file()
    assert result.total_questions == 2
    assert result.total_subquestions == 2
    assert result.total_assets_inserted == 1
    assert len(result.missing_assets) == 0

    # Reopen docx to verify structure
    reopened = Document(str(result.docx_path))
    assert len(reopened.paragraphs) > 0
    assert len(reopened.inline_shapes) == 1


def test_metadata_exclusion(tmp_path: Path, sample_question_bank: Path):
    """Verify that excluded metadata (institution, exam title, course code, confidence) is NOT in document."""
    output_dir = tmp_path / "output_docs"
    generator = DocumentGenerator(
        json_path=sample_question_bank,
        output_dir=output_dir,
        base_dir=tmp_path,
    )
    result = generator.generate()

    doc = Document(str(result.docx_path))
    full_doc_text = "\n".join(p.text for p in doc.paragraphs)

    # Excluded metadata strings
    excluded = [
        "Biju Patnaik University of Technology",
        "Regular Examination",
        "CSPC3002",
        "0.99",
        "0.96",
        "Pipeline run at 12:00",
        "confidence",
        "schema_version",
    ]
    for term in excluded:
        assert term not in full_doc_text, f"Found excluded metadata '{term}' in generated document!"

    # Included content strings
    assert "PART-A" in full_doc_text
    assert "PART-B" in full_doc_text
    assert "Answer all questions briefly:" in full_doc_text
    assert "[2 x 2 marks]" in full_doc_text
    assert "What is paging?" in full_doc_text
    assert "[2 marks]" in full_doc_text
    assert "Analyze the disk head scheduling" in full_doc_text
    assert "[16 marks]" in full_doc_text
    assert "Figure 1: Disk Request Graph" in full_doc_text


def test_strict_mode_failure_on_missing_asset(tmp_path: Path):
    """Test that strict mode raises FileNotFoundError if an asset is missing."""
    data = {
        "questions": [
            {
                "label": "Q1",
                "cleaned_text": "Look at figure:",
                "assets": [{"path": "ghost_file.png"}],
            }
        ]
    }
    json_file = tmp_path / "missing_fig.json"
    json_file.write_text(json.dumps(data), encoding="utf-8")

    generator = DocumentGenerator(
        json_path=json_file,
        output_dir=tmp_path,
        base_dir=tmp_path,
        strict_assets=True,
    )

    with pytest.raises(FileNotFoundError):
        generator.generate()

