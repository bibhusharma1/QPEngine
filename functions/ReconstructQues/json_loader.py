"""Question bank JSON loader and validator."""

import json
import os
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT
from config.reconstruct import INPUT_JSON
from config.parser import OUTPUT_DIR as PARSER_OUTPUT_DIR


def discover_json_paths() -> list[Path]:
    """Return parsed question banks, ordered by their source PDF folder."""
    return sorted((Path(PARSER_OUTPUT_DIR)).glob("*/questions_parsed.json"))


def resolve_json_path(specified_path: str | Path | None = None) -> Path:
    """Resolve the questions_parsed.json path from explicit arg, env, or defaults."""
    # If explicitly specified by caller, only check that path
    if specified_path is not None:
        p = Path(specified_path)
        resolved = p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"Specified question bank JSON file does not exist: {p} (resolved: {resolved})"
            )
        return resolved

    candidates: list[Path] = []

    # Environment variables
    if INPUT_JSON:
        candidates.append(Path(INPUT_JSON))

    env_data_path = os.environ.get("QUES_QUESTIONS_DATA_PATH")
    if env_data_path:
        candidates.append(Path(env_data_path.strip()))

    env_output_file = os.environ.get("QUES_QUESTIONS_OUTPUT")
    if env_output_file:
        candidates.append(Path(env_output_file.strip()))

    for cand in candidates:
        cand_resolved = cand if cand.is_absolute() else (PROJECT_ROOT / cand).resolve()
        if cand_resolved.is_file():
            return cand_resolved

    matches = discover_json_paths()
    if len(matches) == 1:
        return matches[0]

    searched_str = "\n  - ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"Could not find questions_parsed.json. Checked candidates:\n  - {searched_str}\n"
        f"Provide --input with a specific bank, or run the reconstruction CLI to process all per-PDF banks."
    )


def load_question_bank(
    json_path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load and validate the questions data from JSON.
    
    Returns:
        (questions_list, raw_json_data)
    
    Raises:
        FileNotFoundError: If the JSON file does not exist.
        ValueError: If JSON syntax is invalid or structure does not contain valid questions.
    """
    path = resolve_json_path(json_path)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Corrupted or invalid JSON in '{path}': {e}") from e
    except OSError as e:
        raise FileNotFoundError(f"Cannot read file '{path}': {e}") from e

    # Extract questions list
    if isinstance(data, dict):
        raw_dict = data
        questions = data.get("questions")
        if questions is None:
            raise ValueError(
                f"Invalid question bank schema in '{path}': missing 'questions' key."
            )
    elif isinstance(data, list):
        raw_dict = {"questions": data}
        questions = data
    else:
        raise ValueError(
            f"Invalid question bank format in '{path}': expected object or array, got {type(data).__name__}."
        )

    if not isinstance(questions, list):
        raise ValueError(
            f"Expected 'questions' to be a list, got {type(questions).__name__}."
        )

    if not questions:
        raise ValueError(
            f"Question bank in '{path}' contains zero questions."
        )

    # Validate individual question structure
    for idx, q in enumerate(questions):
        if not isinstance(q, dict):
            raise ValueError(
                f"Question at index {idx} is not an object (got {type(q).__name__})."
            )
        # Ensure at least some question identifier or text is present
        has_text = any(
            bool(q.get(k))
            for k in (
                "cleaned_text", "original_text", "text", "label",
                "original_label", "original_question_number",
            )
        )
        if not has_text and not q.get("sub_questions"):
            raise ValueError(
                f"Question at index {idx} has no text, label, or content."
            )

    return questions, raw_dict
