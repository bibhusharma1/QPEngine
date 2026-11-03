"""Unit tests for functions.ReconstructQues.json_loader."""

import json
from pathlib import Path
import pytest

from functions.ReconstructQues.json_loader import load_question_bank, resolve_json_path


def test_load_valid_dict_format(tmp_path: Path):
    """Test loading valid JSON with 'questions' key."""
    sample = {
        "schema_version": "1.0.0",
        "metadata": {"total_questions": 1},
        "questions": [
            {
                "question_id": "q1",
                "label": "Q1",
                "cleaned_text": "What is an OS?",
                "marks": 5.0,
            }
        ],
    }
    file_path = tmp_path / "valid_questions.json"
    file_path.write_text(json.dumps(sample), encoding="utf-8")

    questions, raw_dict = load_question_bank(file_path)
    assert len(questions) == 1
    assert questions[0]["label"] == "Q1"
    assert questions[0]["cleaned_text"] == "What is an OS?"
    assert raw_dict["schema_version"] == "1.0.0"


def test_load_valid_list_format(tmp_path: Path):
    """Test loading valid JSON when file contains a root array of questions."""
    sample = [
        {
            "question_id": "q1",
            "label": "1",
            "original_text": "Define process.",
            "marks": 2.0,
        }
    ]
    file_path = tmp_path / "list_questions.json"
    file_path.write_text(json.dumps(sample), encoding="utf-8")

    questions, raw_dict = load_question_bank(file_path)
    assert len(questions) == 1
    assert questions[0]["original_text"] == "Define process."
    assert "questions" in raw_dict


def test_missing_file_raises_not_found(tmp_path: Path):
    """Test that a non-existent file path raises FileNotFoundError."""
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        load_question_bank(missing)


def test_corrupted_json_raises_value_error(tmp_path: Path):
    """Test that invalid JSON syntax raises ValueError."""
    bad_file = tmp_path / "corrupt.json"
    bad_file.write_text("{this is not valid json:", encoding="utf-8")

    with pytest.raises(ValueError, match="Corrupted or invalid JSON"):
        load_question_bank(bad_file)


def test_missing_questions_key_raises_value_error(tmp_path: Path):
    """Test that dict without 'questions' key raises ValueError."""
    bad_schema = tmp_path / "bad_schema.json"
    bad_schema.write_text(json.dumps({"metadata": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing 'questions' key"):
        load_question_bank(bad_schema)


def test_empty_questions_raises_value_error(tmp_path: Path):
    """Test that empty questions array raises ValueError."""
    empty_file = tmp_path / "empty.json"
    empty_file.write_text(json.dumps({"questions": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="contains zero questions"):
        load_question_bank(empty_file)


def test_question_missing_text_raises_value_error(tmp_path: Path):
    """Test that a question object with no label or text raises ValueError."""
    invalid_q = tmp_path / "invalid_q.json"
    invalid_q.write_text(
        json.dumps({"questions": [{"question_id": "123", "marks": 5.0}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="has no text, label, or content"):
        load_question_bank(invalid_q)

