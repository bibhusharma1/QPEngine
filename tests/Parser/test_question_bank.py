"""Unit and integration tests for the updated question parser and question bank."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from functions.QuesParser.ParsePipeLIne.question_bank import (
    _build_subquestions,
    _match_source_pages,
    _parse_marks_notation,
    _reconcile_hierarchy,
    clean_question_text,
    is_genuine_figure_asset,
)
from functions.QuesParser.ParsePipeLIne.schema import (
    QuestionBank,
    QuestionRecord,
    SubQuestion,
    generate_question_id,
)
from functions.ReconstructQues.generator import DocumentGenerator
from functions.ReconstructQues.json_loader import load_question_bank


def test_no_labels_in_schema():
    """Verify QuestionRecord and SubQuestion have no label attribute."""
    assert "label" not in QuestionRecord.model_fields
    assert "label" not in SubQuestion.model_fields

    sq = SubQuestion(
        sub_id="sub1",
        original_text="Define swapping with an example.",
        cleaned_text="Define swapping with an example.",
        marks=2.0,
        marks_notation="2",
    )
    assert not hasattr(sq, "label")

    q = QuestionRecord(
        question_id="q1",
        original_text="Answer the following questions:",
        cleaned_text="Answer the following questions:",
        marks=20.0,
        marks_notation="2 x 10",
        sub_questions=[sq],
        source_pdf="test.pdf",
        source_pages=[1],
    )
    assert not hasattr(q, "label")

    # Serialized JSON should not have "label"
    dump = q.model_dump(mode="json")
    assert "label" not in dump
    assert "label" not in dump["sub_questions"][0]


def test_clean_question_text():
    """Verify printed question numbers, labels, and outer marks are cleanly stripped."""
    assert clean_question_text("Q1 Answer the following questions:") == "Answer the following questions:"
    assert clean_question_text("Q.1(a) Assume a situation where 5 friends") == "Assume a situation where 5 friends"
    assert clean_question_text("a) Define a time-sharing system with an example.") == "Define a time-sharing system with an example."
    assert clean_question_text("(b) What is context switching?") == "What is context switching?"
    assert clean_question_text("Q3 Describe in detail the critical section problem. (16)") == "Describe in detail the critical section problem."
    assert clean_question_text("1. Identify set and relations.") == "Identify set and relations."
    assert clean_question_text("I. Completion Time (CT).") == "Completion Time (CT)."


def test_parse_marks_notation():
    """Verify combined notation 2 x 10, single marks 16, and missing marks."""
    # Multiplication: 2 x 10 -> total 20, individual 2
    total, ind, comps = _parse_marks_notation("2 x 10")
    assert total == 20.0
    assert ind == 2.0
    assert "2 each" in comps
    assert "total 20.0" in comps

    # Multiplication: 6 x 8 -> total 48, individual 6
    total, ind, comps = _parse_marks_notation("(6 x 8)")
    assert total == 48.0
    assert ind == 6.0

    # Single mark: 16 -> total 16, individual 16
    total, ind, comps = _parse_marks_notation("(16)")
    assert total == 16.0
    assert ind == 16.0

    # Composite addition: 0.5+1+0.5 -> total 2.0
    total, ind, comps = _parse_marks_notation("0.5+1+0.5")
    assert total == 2.0
    assert ind is None

    # Missing / None marks
    total, ind, comps = _parse_marks_notation(None)
    assert total is None
    assert ind is None
    assert comps == []


def test_hierarchy_reconciliation_and_nesting():
    """Verify subquestions are properly nested under their parent question."""
    raw = [
        {"text": "Q1 Answer the following:", "marks": "2 x 10", "sub_questions": [
            {"text": "a) Define time sharing", "marks": None, "sub_questions": []},
            {"text": "b) Define context switching", "marks": None, "sub_questions": []},
        ]},
        {"text": "Q2.a Discuss multithreading models", "marks": "6", "sub_questions": []},
        {"text": "Q2.b Explain process operations", "marks": "6", "sub_questions": []},
        {"text": "Q3 Describe critical section problem", "marks": "16", "sub_questions": []},
    ]

    reconciled = _reconcile_hierarchy(raw)
    # Q2.a and Q2.b should be merged into a single parent question Q2
    assert len(reconciled) == 3

    # Check Q1 has 2 subquestions and clean text
    assert reconciled[0]["text"] == "Answer the following:"
    assert len(reconciled[0]["sub_questions"]) == 2
    assert reconciled[0]["sub_questions"][0]["text"] == "Define time sharing"

    # Check Q2 is parent of subquestions
    assert len(reconciled[1]["sub_questions"]) == 2
    assert reconciled[1]["sub_questions"][0]["text"] == "Discuss multithreading models"
    assert reconciled[1]["sub_questions"][1]["text"] == "Explain process operations"

    # Check Q3 has no subquestions
    assert reconciled[2]["text"] == "Describe critical section problem"
    assert len(reconciled[2]["sub_questions"]) == 0


def test_missing_marks_not_invented_for_subquestions():
    """Verify subquestions of a single-mark question (like Q6 [16]) remain None."""
    sub_dicts = [
        {"text": "Compute FCFS head movement", "marks": None},
        {"text": "Compute SSTF head movement", "marks": None},
    ]
    sub_qs = _build_subquestions(
        sub_dicts=sub_dicts,
        pdf_name="paper.pdf",
        parent_q_id="q6",
        page_num=2,
        inherited_mark=None,  # Single-mark parent does not pass inherited mark
        inherited_notation=None,
        is_fallback=False,
    )
    assert len(sub_qs) == 2
    assert sub_qs[0].marks is None
    assert sub_qs[0].marks_notation is None
    assert sub_qs[1].marks is None


def test_asset_filtering():
    """Verify full-page scans and stamps are rejected while genuine figures pass."""
    project_root = Path(__file__).resolve().parents[2]
    from config.settings import SCRAPPER_ROOT

    # Test full-page scan detection (e.g. 1029x1400)
    manifest_scanned_page = {
        "native_text_characters": 0,
        "render_width_px": 2067,
        "render_height_px": 2924,
    }

    # An image file matching full-page scan dimensions should be rejected
    sample_img = SCRAPPER_ROOT / "btech-ce-cse-ece-it-5-sem-operating-systems-cspc3002-2026/images/page_0001_image_2.png"
    if sample_img.is_file():
        assert not is_genuine_figure_asset(str(sample_img), project_root, manifest_scanned_page)

    # A tiny stamp (119x30) should be rejected
    sample_stamp = SCRAPPER_ROOT / "btech-ce-cse-ece-it-5-sem-operating-systems-cspc3002-2026/images/page_0001_image_1.png"
    if sample_stamp.is_file():
        assert not is_genuine_figure_asset(str(sample_stamp), project_root, manifest_scanned_page)

    # A genuine figure (e.g. AI24201 1241x661 plot) should be accepted
    genuine_fig = SCRAPPER_ROOT / "AI24201 MATHEMATICS FOR AIML_MID/images/page_0001_image_1.jpeg"
    if genuine_fig.is_file():
        assert is_genuine_figure_asset(str(genuine_fig), project_root, None)


def test_reconstruct_ques_compatibility_with_new_schema(tmp_path):
    """Verify ReconstructQues loads and generates DOCX from the new schema without labels."""
    bank_data = {
        "schema_version": "1.0.0",
        "metadata": {
            "processed_at": "2026-10-28T12:00:00Z",
            "processor_version": "1.0.0",
            "providers_used": ["test"],
            "total_pages_processed": 1,
            "total_questions_found": 2,
            "total_assets_linked": 0,
            "source_manifests": ["test/manifest.json"],
        },
        "questions": [
            {
                "question_id": "q1",
                "original_text": "Answer the following questions:",
                "cleaned_text": "Answer the following questions:",
                "marks": 20.0,
                "marks_notation": "2 x 10",
                "sub_questions": [
                    {
                        "sub_id": "q1_a",
                        "original_text": "Define a time-sharing system.",
                        "cleaned_text": "Define a time-sharing system.",
                        "marks": 2.0,
                        "marks_notation": "2",
                        "assets": [],
                        "sub_questions": [],
                    }
                ],
                "source_pdf": "test.pdf",
                "source_pages": [1],
                "assets": [],
            },
            {
                "question_id": "q2",
                "original_text": "Describe critical section problem.",
                "cleaned_text": "Describe critical section problem.",
                "marks": 16.0,
                "marks_notation": "16",
                "sub_questions": [],
                "source_pdf": "test.pdf",
                "source_pages": [1],
                "assets": [],
            }
        ],
    }

    json_file = tmp_path / "questions_data.json"
    json_file.write_text(json.dumps(bank_data), encoding="utf-8")

    # Load with ReconstructQues json_loader
    questions, raw_dict = load_question_bank(json_file)
    assert len(questions) == 2
    assert "label" not in questions[0]

    # Generate document with DocumentGenerator
    out_dir = tmp_path / "output_docx"
    generator = DocumentGenerator(json_path=json_file, output_dir=out_dir)
    res = generator.generate(filename="test_output.docx")
    assert Path(res.docx_path).is_file()
    assert res.total_questions == 2
    assert res.total_subquestions == 1
