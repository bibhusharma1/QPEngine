import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.masterdeorca.document_intelligence.lite.two_stage import LiteTwoStageProcessor
from src.masterdeorca.document_intelligence.lite.two_stage import (
    _build_question_record,
    _apply_explicit_question_boundaries,
    _clean_block_text,
    _clean_question_content,
    _extract_layout_blocks,
    _extract_priority_from_text,
    _question_id,
    _resolve_question_candidates,
    _block_batches,
    _logical_block_batches,
    _classify_layout_blocks,
    _classify_blocks_adaptive,
    _supplement_explicit_table_questions,
)
from functions.QuesParser.ParsePipeLIne.schema import QuestionBank


def test_default_plan_splits_24_blocks_into_three_batches_of_eight(monkeypatch):
    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_BATCH_SIZE", 8)
    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_OVERLAP", 0)
    blocks = [{"block_id": f"b{i}"} for i in range(24)]

    batches = _block_batches(blocks)

    assert [len(batch) for batch in batches] == [8, 8, 8]
    assert [block["block_id"] for batch in batches for block in batch] == [
        f"b{i}" for i in range(24)
    ]


def test_logical_batches_keep_sibling_question_parts_together():
    blocks = [
        {"block_id": "intro", "text": "INSTRUCTIONS: attempt all questions"},
        {"block_id": "q1a", "text": "Q.1(a) Explain agents.", "page_number": 1},
        {"block_id": "q1b", "text": "Q.1(b) Discuss search.", "page_number": 1},
        {"block_id": "q1ctx", "text": "Additional context for Q1.", "page_number": 1},
        {"block_id": "q2", "text": "Q.2 Describe planning.", "page_number": 1},
        {"block_id": "footer", "text": "End of paper", "page_number": 1},
    ]

    batches = _logical_block_batches(blocks)

    assert [[block["block_id"] for block in batch] for batch in batches] == [
        ["intro"], ["intro", "q1a", "q1b", "q1ctx"],
        ["intro", "q2", "footer"],
    ]


def test_logical_classifier_sends_page_images_only_with_question_units(monkeypatch):
    calls = []

    class CapturingStructurer:
        def classify_blocks(self, blocks, assets, *, batch_id, page_images=None):
            calls.append((batch_id, [b["block_id"] for b in blocks], page_images))
            return {"other_block_ids": [b["block_id"] for b in blocks]}

    blocks = [
        {"block_id": "i1", "text": "Instructions", "page_number": 1},
        {"block_id": "q1a", "text": "Q.1(a) Explain", "page_number": 1},
        {"block_id": "q1b", "text": "Q.1(b) Discuss", "page_number": 1},
        {"block_id": "q2", "text": "Q.2 Define", "page_number": 1},
    ]
    classifications, _ = _classify_layout_blocks(
        CapturingStructurer(), blocks, [], "logical-test", None,
        page_images={1: b"full-page-image"},
    )

    assert len(classifications) == 3
    assert calls[0][2] is None
    assert calls[1][1] == ["i1", "q1a", "q1b"] and calls[1][2] == {1: b"full-page-image"}
    assert calls[2][1] == ["i1", "q2"] and calls[2][2] == {1: b"full-page-image"}


@pytest.mark.parametrize(
    ("text", "marks", "priority"),
    [
        ("Q 22. Differentiate between CNN and RNN. Marks 3, priority 2", 3, 2),
        ("Q10 Explain a CNN. Marks: 2 | Priority order 3", 2, 3),
        ("Q7 Explain topic. Priority = 1", None, 1),
    ],
)
def test_priority_text_examples_keep_marks_and_priority_separate(text, marks, priority):
    marks_match = re.search(r"\bmarks?\s*[:=]?\s*(\d+)\b", text, re.I)
    assert (int(marks_match.group(1)) if marks_match else None) == marks
    assert _extract_priority_from_text(text) == priority


def test_question_and_subquestion_keep_explicit_priority_and_pdf_fallback():
    source = {
        "label": "Q22",
        "original_question_number": "Q22",
        "original_text": "Differentiate between CNN and RNN. Marks 3, priority 2",
        "text": "Differentiate between CNN and RNN. Marks 3, priority 2",
        "marks": "3",
        "priority": 2,
        "source_pages": [2],
        "source_block_ids": ["b1"],
        "sub_questions": [{
            "label": "(a)",
            "text": "Explain a CNN.",
            "original_text": "Explain a CNN.",
            "source_pages": [2],
            "source_block_ids": ["b2"],
            "sub_questions": [{
                "label": "(i)",
                "text": "Give an example.",
                "original_text": "Give an example.",
                "source_pages": [2],
                "source_block_ids": ["b3"],
                "sub_questions": [],
            }],
        }],
    }
    record = _build_question_record("paper.pdf", source, "stable", default_priority=3)
    validated = QuestionBank.model_validate({"questions": [record]}).questions[0]
    assert validated.priority == 2
    assert validated.sub_questions[0].priority == 3
    assert validated.sub_questions[0].parent_question_priority == 2
    assert validated.sub_questions[0].source_block_ids == ["b2"]
    assert validated.sub_questions[0].original_question_number == "Q22(a)"
    assert validated.sub_questions[0].parent_question_id == "stable"
    assert validated.sub_questions[0].sub_questions[0].original_question_number == "Q22(a)(i)"
    assert validated.sub_questions[0].sub_questions[0].parent_question_ref == "Q22(a)"
    assert validated.sub_questions[0].sub_questions[0].parent_question_id == validated.sub_questions[0].sub_id
    assert validated.sub_questions[0].sub_questions[0].parent_question_priority == 3


def test_24_block_plan_executes_three_batches_with_bounded_concurrency(monkeypatch):
    import threading
    import time

    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_BATCH_SIZE", 8)
    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_OVERLAP", 0)
    monkeypatch.setattr("config.settings.LITE_STAGE2_CONCURRENCY", 2)
    lock = threading.Lock()
    state = {"active": 0, "maximum": 0, "sizes": []}

    class FakeStructurer:
        def classify_blocks(self, blocks, figure_assets, *, batch_id):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["sizes"].append(len(blocks))
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            return {"other_block_ids": [block["block_id"] for block in blocks]}

    blocks = [
        {"block_id": f"b{i}", "page_number": 1, "reading_order": i}
        for i in range(24)
    ]
    classifications, _ = _classify_layout_blocks(
        FakeStructurer(), blocks, [], "digest", None
    )

    assert len(classifications) == 3
    assert sorted(state["sizes"]) == [8, 8, 8]
    assert state["maximum"] == 2


def test_adaptive_split_reuses_successful_child_when_sibling_fails():
    calls = {"parent": 0, "a": 0, "b": 0}
    cache = {}

    class FlakyStructurer:
        def classify_blocks(self, blocks, figure_assets, *, batch_id):
            if len(blocks) == 2:
                calls["parent"] += 1
                raise RuntimeError("truncated response finish_reason=length")
            key = blocks[0]["block_id"]
            calls[key] += 1
            if key == "b" and calls[key] == 1:
                raise RuntimeError("temporary API failure")
            return {"other_block_ids": [key]}

    blocks = [{"block_id": "a"}, {"block_id": "b"}]
    run = lambda: _classify_blocks_adaptive(
        FlakyStructurer(), blocks, [], batch_id="parent", min_batch_size=1,
        cache_lookup=lambda child: cache.get(tuple(b["block_id"] for b in child)),
        cache_store=lambda child, value: cache.__setitem__(
            tuple(b["block_id"] for b in child), value
        ),
    )
    with pytest.raises(RuntimeError, match="temporary API failure"):
        run()

    result = run()
    assert calls == {"parent": 2, "a": 1, "b": 2}
    assert result == [
        {"other_block_ids": ["a"]},
        {"other_block_ids": ["b"]},
    ]


@dataclass
class FakePage:
    page_number: int
    data: bytes
    mime: str


def _setup(monkeypatch, page_raws, structurer_error=None):
    monkeypatch.setattr("config.settings.PARSER_MODE", "llm")

    def fake_render(path):
        for n in range(1, len(page_raws) + 1):
            yield FakePage(n, f"img{n}".encode(), "image/png")

    monkeypatch.setattr(
        "src.masterdeorca.document_intelligence.lite.two_stage.render_page_images", fake_render
    )

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def parse_page_image(self, data, mime, page_number, filename=None):
            captured.setdefault("requests", []).append((filename, mime, data))
            raw = page_raws[page_number - 1]
            if isinstance(raw, Exception):
                raise raw
            return {"content": raw}

        def combine_pages(self, pages):
            return "\n\n".join(
                f"<class_Page-header>Page {n}</class_Page-header>\n{r.get('content', '')}"
                for n, r in pages
            )

    monkeypatch.setattr(
        "src.masterdeorca.document_intelligence.lite.two_stage.NemotronParseClient", FakeClient
    )

    captured = {}

    class FakeStructurer:
        errors = list(structurer_error) if isinstance(structurer_error, list) else None

        def classify_blocks(self, blocks, figure_assets, *, batch_id):
            captured.setdefault("structure_calls", []).append(blocks)
            if self.errors is not None:
                error = self.errors.pop(0) if self.errors else None
            else:
                error = structurer_error
            if error is not None:
                raise error
            questions = {}
            active_number = None
            active_subpart = None
            active_section = None
            active_instruction_ids = []
            sections = []
            for block in blocks:
                text = block["text"]
                section_match = re.search(r"\bSection\s+([A-Z])\b", text, re.IGNORECASE)
                if section_match:
                    active_section = f"Section {section_match.group(1).upper()}"
                    sections.append({
                        "section_name": active_section,
                        "block_ids": [],
                        "instruction_block_ids": [],
                    })
                    continue
                if "answer all questions" in text.casefold():
                    active_instruction_ids.append(block["block_id"])
                    if sections:
                        sections[-1]["instruction_block_ids"].append(block["block_id"])
                    continue
                question_match = re.search(
                    r"\bQ\.?\s*(\d+)(?:\s*\(([a-z])\))?",
                    text,
                    re.IGNORECASE,
                )
                if question_match:
                    active_number = f"Q{question_match.group(1)}"
                    entry = questions.setdefault(active_number, {
                        "question_number": active_number,
                        "block_ids": [],
                        "sub_questions": [],
                        "marks": None,
                        "section_name": active_section,
                        "instruction_block_ids": list(active_instruction_ids),
                        "figure_asset_ids": [],
                        "ambiguous_block_ids": [],
                        "ambiguity_note": None,
                    })
                    if question_match.group(2):
                        child_label = f"({question_match.group(2).lower()})"
                        child = next(
                            (item for item in entry["sub_questions"] if item["label"] == child_label),
                            None,
                        )
                        if child is None:
                            child = {
                                "label": child_label,
                                "block_ids": [],
                                "marks": None,
                                "figure_asset_ids": [],
                                "ambiguous_block_ids": [],
                            }
                            entry["sub_questions"].append(child)
                        child["block_ids"].append(block["block_id"])
                        active_subpart = child
                    else:
                        entry["block_ids"].append(block["block_id"])
                        active_subpart = None
                    if sections and block["block_id"] not in sections[-1]["block_ids"]:
                        sections[-1]["block_ids"].append(block["block_id"])
                    continue
                subpart_match = re.match(
                    r"\s*(?:\(([a-z])\)|([a-z])[\.)])\s*", text, re.IGNORECASE
                )
                if subpart_match and active_number:
                    label = f"({(subpart_match.group(1) or subpart_match.group(2)).lower()})"
                    entry = questions[active_number]
                    parent_label = (
                        active_subpart["label"]
                        if active_subpart is not None
                        and label not in {"(a)", "(b)", "(c)"}
                        else None
                    )
                    active_subpart = next(
                        (
                            item for item in entry["sub_questions"]
                            if item["label"] == label
                            and item.get("parent_label") == parent_label
                        ),
                        None,
                    )
                    if active_subpart is None:
                        active_subpart = {
                            "label": label,
                            "parent_label": parent_label,
                            "block_ids": [],
                            "marks": None,
                            "figure_asset_ids": [],
                            "ambiguous_block_ids": [],
                        }
                        entry["sub_questions"].append(active_subpart)
                    active_subpart["block_ids"].append(block["block_id"])
                    continue
                if active_number:
                    entry = questions[active_number]
                    if active_subpart is not None:
                        active_subpart["block_ids"].append(block["block_id"])
                    else:
                        entry["block_ids"].append(block["block_id"])
                    if sections:
                        sections[-1]["block_ids"].append(block["block_id"])
            captured.setdefault("classified_questions", []).extend(questions.values())
            return {
                "schema_version": "1.0",
                "questions": list(questions.values()),
                "sections": sections,
                "instruction_block_ids": active_instruction_ids,
                "other_block_ids": [],
                "ambiguous_block_ids": [],
            }

    monkeypatch.setattr(
        "src.masterdeorca.document_intelligence.lite.structurer.StructureWithLLM", FakeStructurer
    )
    return captured


def test_combines_pages_in_order(monkeypatch, tmp_path):
    captured = _setup(monkeypatch, ["Q.1 CONTENT_PAGE_1", "continued CONTENT_PAGE_2"])
    proc = LiteTwoStageProcessor()
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    extracted, bank = proc.process(src, output_dir=tmp_path / "out")
    assert bank["questions"]
    raw_content = extracted.metadata["nemotron_normalized"]["raw_content"]
    assert raw_content.index("CONTENT_PAGE_1") < raw_content.index("CONTENT_PAGE_2")
    assert "Page 1" in raw_content and "Page 2" in raw_content
    assert [page["page"] for page in extracted.metadata["nemotron_normalized"]["pages"]] == [1, 2]
    assert [item[0] for item in captured["requests"]] == ["page-00001.png", "page-00002.png"]
    assert all(item[1] == "image/png" for item in captured["requests"])
    assert extracted.metadata["page_count"] == 2
    assert len(captured["structure_calls"]) == 1
    assert bank["questions"][0]["source_pages"] == [1, 2]
    out = tmp_path / "out" / "questions_parsed.json"
    assert out.is_file()
    assert '"marks_index"' in out.read_text(encoding="utf-8")


def test_empty_extraction_raises(monkeypatch, tmp_path):
    _setup(monkeypatch, ["CONTENT_PAGE_1", "   "])
    proc = LiteTwoStageProcessor()
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    with pytest.raises(RuntimeError):
        proc.process(src)


def test_page_failure_propagates(monkeypatch, tmp_path):
    _setup(monkeypatch, ["CONTENT_PAGE_1", RuntimeError("boom")])
    proc = LiteTwoStageProcessor()
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    with pytest.raises(RuntimeError):
        proc.process(src)


def test_no_pages_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.PARSER_MODE", "llm")

    def fake_render(path):
        return iter([])

    monkeypatch.setattr(
        "src.masterdeorca.document_intelligence.lite.two_stage.render_page_images", fake_render
    )

    class FakeClient:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(
        "src.masterdeorca.document_intelligence.lite.two_stage.NemotronParseClient", FakeClient
    )
    proc = LiteTwoStageProcessor()
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    with pytest.raises(RuntimeError):
        proc.process(src)


def test_block_classification_failure_does_not_save_unresolved_question(monkeypatch, tmp_path):
    _setup(monkeypatch, ["Q.1 CONTENT_PAGE_1"], RuntimeError("providers exhausted"))
    proc = LiteTwoStageProcessor()
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "out"

    with pytest.raises(RuntimeError, match="providers exhausted"):
        proc.process(src, output_dir=output_dir)

    assert (output_dir / "lite_extraction_cache.json").is_file()
    assert not (output_dir / "questions_parsed.json").exists()


def test_numbered_questions_in_parse_latex_table_are_recovered_from_cached_blocks():
    table = (
        r"\begin{tabular}{ccc}" "\n"
        r" & Question & Marks \\" "\n"
        r"Q.1(a) & Explain agent software. & [5] \\" "\n"
        r"Q.1(b) & Discuss search space. & [4] \\" "\n"
        r"Q.2(a) & Explain A* search. & [5] \\" "\n"
        r"\end{tabular}"
    )
    raw_payload = {
        "pages": [{
            "page": 1,
            "raw": {
                "content": (
                    "<x_0.1><y_0.1>" + table
                    + "<x_0.9><y_0.9><class_Table>"
                )
            },
        }]
    }
    blocks = _extract_layout_blocks(raw_payload, "source-digest", [])
    classifications = [{
        "questions": [],
        "sections": [],
        "instruction_block_ids": [],
        "other_block_ids": [],
        "ambiguous_block_ids": [],
    }]

    recovered = _supplement_explicit_table_questions(classifications, blocks)
    candidates = _resolve_question_candidates(
        recovered,
        {block["block_id"]: block for block in blocks},
        [],
    )

    assert [item["label"] for item in candidates] == ["Q1", "Q2"]
    assert all(not item["ambiguous"] for item in candidates)
    assert [
        (child["label"], child["text"], child["marks"], child["source_pages"])
        for child in candidates[0]["sub_questions"]
    ] == [
        ("(a)", "Explain agent software.", "5", [1]),
        ("(b)", "Discuss search space.", "4", [1]),
    ]
    assert candidates[1]["sub_questions"][0]["text"] == "Explain A* search."
    assert all(
        child["text"] is not None
        and "Marks" not in child["text"]
        and "[5]" not in child["text"]
        for candidate in candidates
        for child in candidate["sub_questions"]
    )


def test_structuring_retry_reuses_cached_extraction_without_parse_calls(monkeypatch, tmp_path):
    captured = _setup(
        monkeypatch,
        ["Q.1 CONTENT_PAGE_1", "continued CONTENT_PAGE_2"],
        [RuntimeError("temporary structuring failure"), None],
    )
    proc = LiteTwoStageProcessor()
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "out"

    with pytest.raises(RuntimeError, match="temporary structuring failure"):
        proc.process(src, output_dir=output_dir)
    _, resumed = proc.process(src, output_dir=output_dir)

    assert resumed["questions"][0]["status"] == "structured"
    assert len(captured["requests"]) == 2
    assert len(captured["structure_calls"]) == 2
    assert (output_dir / "questions_parsed.json").is_file()


def test_multiple_questions_on_one_page_are_structured_sequentially(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_BATCH_SIZE", 8)
    captured = _setup(
        monkeypatch,
        ["Section A\nAnswer all questions\nQ.1 First question [2]\nQ.2 Second question [3]"],
    )
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4")
    _, bank = LiteTwoStageProcessor().process(source, output_dir=tmp_path / "out")

    assert [item["original_question_number"] for item in bank["questions"]] == ["Q1", "Q2"]
    assert [item["source_pages"] for item in bank["questions"]] == [[1], [1]]
    assert [item["section_name"] for item in bank["questions"]] == ["Section A", "Section A"]
    assert [item["section_instructions"] for item in bank["questions"]] == [
        "Answer all questions", "Answer all questions",
    ]
    assert len(bank["questions"]) == 2
    assert bank["questions"][0]["question_id"] != bank["questions"][1]["question_id"]


def test_question_subparts_are_kept_together_in_one_request(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_BATCH_SIZE", 8)
    captured = _setup(
        monkeypatch,
        ["Q.1 Explain the process.\na) First part [2]\nb) Second part [3]\n(i) Nested part"],
    )
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4")
    _, bank = LiteTwoStageProcessor().process(source, output_dir=tmp_path / "out")

    assert len(captured["structure_calls"]) == 1
    parts = bank["questions"][0]["sub_questions"]
    assert [part["original_label"] for part in parts] == ["(a)", "(b)"]
    assert parts[1]["sub_questions"][0]["original_label"] == "(i)"


def test_failed_classification_batch_is_retried_from_cached_extraction(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_BATCH_SIZE", 8)
    monkeypatch.setattr("config.settings.LITE_STAGE2_BLOCK_OVERLAP", 2)
    page_text = "\n".join(f"Q.{number} Question {number}" for number in range(1, 31))
    captured = _setup(
        monkeypatch,
        [page_text],
        [None, RuntimeError("batch two failed"), None],
    )
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4")
    output = tmp_path / "out"
    progress_events = []

    with pytest.raises(RuntimeError, match="batch two failed"):
        LiteTwoStageProcessor().process(
            source,
            output_dir=output,
            on_question_progress=progress_events.append,
        )

    partial_path = output / "questions_parsed.json"
    assert not partial_path.exists()

    _, completed = LiteTwoStageProcessor().process(
        source,
        output_dir=output,
        on_question_progress=progress_events.append,
    )
    # Thirty self-contained question units are run independently. The 29
    # successful unit results are cached; only the failed one is called again.
    assert len(captured["structure_calls"]) == 31
    assert len(completed["questions"]) == 30
    assert [question["original_question_number"] for question in completed["questions"]] == [
        f"Q{number}" for number in range(1, 31)
    ]
    assert all(question["status"] == "structured" for question in completed["questions"])
    assert progress_events[-1]["completion_percent"] == 100
    assert progress_events[-1]["processed_questions"] == 30


def test_completed_questions_are_not_duplicated_or_reprocessed(monkeypatch, tmp_path):
    captured = _setup(monkeypatch, ["Q.1 First"])
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4")
    output = tmp_path / "out"
    processor = LiteTwoStageProcessor()
    _, first = processor.process(source, output_dir=output)
    _, second = processor.process(source, output_dir=output)

    assert len(captured["structure_calls"]) == 1
    assert len(first["questions"]) == len(second["questions"]) == 1


def test_explicit_parse_subquestion_headings_recover_missing_classifier_refs():
    blocks = [
        {
            "block_id": "p1-b1",
            "page_number": 1,
            "reading_order": 1,
            "layout_type": "Text",
            "text": "Q.3(a) First part",
            "original_text": "Q.3(a) First part",
            "figure_asset_ids": [],
        },
        {
            "block_id": "p2-b2",
            "page_number": 2,
            "reading_order": 2,
            "layout_type": "Table",
            "text": "Continuation table",
            "original_text": "Continuation table",
            "figure_asset_ids": [],
        },
        {
            "block_id": "p2-b3",
            "page_number": 2,
            "reading_order": 3,
            "layout_type": "Text",
            "text": "Q.3(b) Second part",
            "original_text": "Q.3(b) Second part",
            "figure_asset_ids": [],
        },
    ]
    candidate = {
        "label": "Q3",
        "original_question_number": "Q3",
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
        "ambiguous": False,
        "ambiguity_notes": [],
    }

    recovered = _apply_explicit_question_boundaries([candidate], blocks, [])

    assert len(recovered) == 1
    question = recovered[0]
    assert question["source_pages"] == [1, 2]
    assert [part["label"] for part in question["sub_questions"]] == ["(a)", "(b)"]
    assert question["sub_questions"][0]["source_pages"] == [1, 2]
    assert question["sub_questions"][0]["text"] == (
        "First part\n\nContinuation table"
    )
    assert question["sub_questions"][1]["text"] == "Second part"
    assert question["sub_questions"][0]["original_text"] == (
        "First part\n\nContinuation table"
    )
    assert question["sub_questions"][1]["original_text"] == "Second part"
    record = _build_question_record(
        "paper.pdf", question, _question_id("paper.pdf", question)
    )
    QuestionBank.model_validate({"questions": [record]})
    assert record["original_question_number"] == "Q3"
    assert [part["original_label"] for part in record["sub_questions"]] == ["(a)", "(b)"]
    assert all(
        "Q.3" not in part["original_text"]
        and "Q.3" not in part["cleaned_text"]
        for part in record["sub_questions"]
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Q.1(b)  Calculate   x + y.", "Calculate x + y."),
        ("Q3(a): Preserve 100 MB and Ω.", "Preserve 100 MB and Ω."),
        ("Question 3: Keep the complete wording.", "Keep the complete wording."),
        ("Question No. 3(b)  Use [a, b] and x².", "Use [a, b] and x²."),
        ("The phrase Question 3 occurs here.", "The phrase Question 3 occurs here."),
    ],
)
def test_question_content_removes_source_question_prefixes_only(source, expected):
    assert _clean_question_content(source) == expected


def test_subquestion_labels_are_metadata_and_not_question_content():
    assert _clean_question_content(
        "a)  Find the value of x.", subquestion_label="(a)"
    ) == "Find the value of x."
    assert _clean_question_content(
        "(i)  Explain the result.", subquestion_label="(i)"
    ) == "Explain the result."


def test_block_cleanup_normalizes_ocr_whitespace_and_math_markup():
    assert _clean_block_text(
        "  **Find**   x<sup>2</sup>.\n\n\nThen solve   y.  "
    ) == "Find x^(2).\n\nThen solve y."


def test_block_cleanup_preserves_code_and_json_round_trips_special_characters():
    code = 'if (value ** 2):\n    print("Q.1", "λ")'
    assert _clean_block_text(code, preserve_code=True) == code
    assert _clean_block_text(code) == code
    content = _clean_question_content(
        'Q.1 Print "λ" and C:\\\\tmp\\tend.',
    )
    assert json.loads(json.dumps({"content": content}, ensure_ascii=False))["content"] == content
