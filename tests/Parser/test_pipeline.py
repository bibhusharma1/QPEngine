"""The public pipeline entry point delegates to the JSON-only workflow."""

from functions.QuesParser.ParsePipeLIne import pipeline
from functions.QuesParser.ParsePipeLIne.question_bank import _regex_segment, clean_question_text, extract_marks, normalize, remove_structured_table_text
from functions.QuesParser.ParsePipeLIne.schema import TableReference


def test_pipeline_does_not_discover_or_read_pdfs():
    assert not hasattr(pipeline, "collect_pdf_files")
    assert callable(pipeline.run)


def test_regex_segment_keeps_labeled_subquestion_and_flushes_section():
    pages = [
        {"page_number": 1, "raw_text": "Part-II\nQ1\nAnswer the following:\na) First short item\nl) Use SRTF.\nTable below:\nI. Completion Time\nII. Turnaround Time"},
        {"page_number": 2, "raw_text": "Process P1 0 7\nPart-III\nOnly Long Answer Type Questions\nQ3 Describe deadlock.\n(16)"},
    ]
    result = _regex_segment("paper.pdf", pages)
    questions = result["extraction"]["questions"]
    q1, q3 = questions
    assert q1["section_name"] == "Part-II"
    assert [sq["label"] for sq in q1["sub_questions"]] == ["a)", "l)"]
    assert [sq["label"] for sq in q1["sub_questions"][-1]["sub_questions"]] == ["(i)", "(ii)"]
    assert "Part-III" not in q1["sub_questions"][-1]["text"]
    assert q3["section_name"] == "Part-III"
    assert q3["source_page"] == 2


def test_clean_question_text_keeps_paragraph_breaks_and_symbols():
    cleaned = clean_question_text("Compute the result.\n\nUse P1 → P4 and 2 × 10 marks.")
    assert cleaned == "Compute the result.\n\nUse P1 → P4 and 2 × 10 marks."


def test_structured_table_is_not_repeated_as_flat_question_prose():
    text = "Given the table below: Process Arrival Time (ms) Burst Time (ms) P1 0 7 P2 2 4 P3 4 1 P4 5 4 Compute using SRTF."
    table = {"cells": [["Process", "Arrival Time (ms)", "Burst Time (ms)"], ["P1", "0", "7"], ["P2", "2", "4"], ["P3", "4", "1"], ["P4", "5", "4"]]}
    cleaned = remove_structured_table_text(text, [table])
    assert cleaned == "Given the table below:  Compute using SRTF."


def test_srtf_table_and_labeled_part_survive_parser_hierarchy():
    table = {
        "table_id": "p2-t1", "source_page": 2,
        "headers": ["Process", "Arrival Time (ms)", "Burst Time (ms)"],
        "rows": [["P1", "0", "7"], ["P2", "2", "4"], ["P3", "4", "1"], ["P4", "5", "4"]],
        "cells": [["Process", "Arrival Time (ms)", "Burst Time (ms)"], ["P1", "0", "7"], ["P2", "2", "4"], ["P3", "4", "1"], ["P4", "5", "4"]],
    }
    pages = [
        {"page_number": 1, "raw_text": "Part-II\nQ2\nAnswer any parts:\na) First item\nk) Last prior item"},
        {"page_number": 2, "raw_text": "l)\nFour processes arrive in the ready queue at different times. Their arrival times and CPU burst times are given in the table below:\nProcess\nArrival Time (ms)\nBurst Time (ms)\nP1\n0\n7\nP2\n2\n4\nP3\n4\n1\nP4\n5\n4\nUsing Preemptive Shortest Remaining Time First (SRTF) scheduling, compute the following for each process:\nI. Completion Time (CT)\nII. Turnaround Time (TAT)\nIII. Waiting Time (WT)\nPart-III\nQ3\nDescribe deadlock.\n(16)", "tables": [table]},
    ]
    segment = _regex_segment("paper.pdf", pages)
    questions = extract_marks({"raw_segments": [segment], "pages": pages})["question_records"]
    q2 = next(q for q in questions if q.original_label == "Q2")
    item_l = next(sub for sub in q2.sub_questions if sub.original_label == "l)")
    assert item_l.source_pages == [2]
    assert [sub.original_label for sub in item_l.sub_questions] == ["(i)", "(ii)", "(iii)"]
    assert item_l.tables[0].headers == table["headers"]
    assert "P1 0 7" not in item_l.cleaned_text
    assert "P1" in item_l.original_text

    # Simulate a model repeating the page table on nested part (i).
    item_l.sub_questions[0].tables.append(TableReference(
        table_id="p2-t1", headers=table["headers"], rows=table["rows"], cells=[], source_page=2,
    ))
    normalized = normalize({"question_records": questions})["question_records"]
    q2 = next(q for q in normalized if q.original_label == "Q2")
    item_l = next(sub for sub in q2.sub_questions if sub.original_label == "l)")
    assert len(item_l.tables) == 1
    assert item_l.sub_questions[0].tables == []
