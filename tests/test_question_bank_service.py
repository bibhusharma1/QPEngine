from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from src.masterdeorca import storage, database
from src.masterdeorca.services import question_bank


def _subject(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "store" / "db.sqlite")
    database.initialize()
    return storage.ensure_subject("Subject")


def _manifest(paths, filename="paper.pdf", status="succeeded", parsed_path="output/Parser/paper/questions_parsed.json"):
    manifest = {"subject": "Subject", "pdfs": [{"id": "source-id", "filename": filename,
        "relative_path": f"source/{filename}", "status": status, "result_json": parsed_path}]}
    (paths["manifests"] / "processing_status.json").write_text(json.dumps(manifest), encoding="utf-8")


def _parsed(paths, questions):
    target = paths["parser"] / "paper" / "questions_parsed.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"questions": questions, "metadata": {"provider": "test"}}), encoding="utf-8")
    return target


def test_sync_flattens_nested_records_is_idempotent_and_refreshes_on_reprocess(tmp_path, monkeypatch):
    paths = _subject(tmp_path, monkeypatch)
    _manifest(paths)
    parsed_path = _parsed(paths, [{"question_id": "parent", "original_question_number": "Q1",
        "original_text": "Parent", "section_name": "Section A", "sub_questions": [
            {"sub_id": "a", "label": "(a)", "cleaned_text": "Child A", "marks": 2, "source_page": 3},
            {"sub_id": "b", "label": "(b)", "original_text": "Child B", "marks": 4},
        ]}])
    assert question_bank.sync_pdf("Subject", "paper.pdf") == 2
    bank_path = paths["base"] / "question_bank.json"
    bank = json.loads(bank_path.read_text(encoding="utf-8"))
    assert [row["original_question_number"] for row in bank["questions"]] == ["Q1(a)", "Q1(b)"]
    assert bank["questions"][0]["section_name"] == "Section A"
    assert bank["questions"][0]["page_number"] == 3
    ids = {row["question_id"] for row in bank["questions"]}
    bank["questions"][0]["operator_note"] = "keep me"
    bank_path.write_text(json.dumps(bank), encoding="utf-8")
    assert question_bank.sync_pdf("Subject", "paper.pdf") == 2
    bank = json.loads(bank_path.read_text(encoding="utf-8"))
    assert len(bank["questions"]) == 2
    assert {row["question_id"] for row in bank["questions"]} == ids
    assert bank["questions"][0]["operator_note"] == "keep me"

    _parsed(paths, [{"question_id": "parent", "original_question_number": "Q1",
        "sub_questions": [{"sub_id": "a", "label": "(a)", "original_text": "Updated child A", "marks": 3}]}])
    question_bank.sync_pdf("Subject", "paper.pdf")
    bank = json.loads(bank_path.read_text(encoding="utf-8"))
    child_a = next(row for row in bank["questions"] if row["original_question_number"] == "Q1(a)")
    assert child_a["original_text"] == "Updated child A"
    assert child_a["marks"] == 3
    assert child_a["operator_note"] == "keep me"
    assert len(bank["questions"]) == 2  # prior imported item is preserved, without duplication


def test_failed_or_invalid_result_never_imports_and_invalid_bank_recovers(tmp_path, monkeypatch):
    paths = _subject(tmp_path, monkeypatch)
    _manifest(paths, status="failed")
    _parsed(paths, [{"original_text": "Stale question"}])
    try:
        question_bank.sync_pdf("Subject", "paper.pdf")
    except ValueError:
        pass
    else:
        raise AssertionError("failed manifest entry was imported")
    assert not (paths["base"] / "question_bank.json").exists()

    _manifest(paths)
    (paths["parser"] / "paper" / "questions_parsed.json").write_text("{bad json", encoding="utf-8")
    try:
        question_bank.sync_pdf("Subject", "paper.pdf")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid parsed result was imported")

    bank_path = paths["base"] / "question_bank.json"
    bank_path.write_text("not json", encoding="utf-8")
    _parsed(paths, [{"question_id": "q1", "original_text": "Fresh question"}])
    question_bank.sync_pdf("Subject", "paper.pdf")
    assert json.loads(bank_path.read_text(encoding="utf-8"))["questions"][0]["original_text"] == "Fresh question"
    assert list(paths["base"].glob("question_bank.json.corrupt-*"))


def test_concurrent_sync_keeps_bank_valid_and_deduplicated(tmp_path, monkeypatch):
    paths = _subject(tmp_path, monkeypatch)
    _manifest(paths)
    _parsed(paths, [{"question_id": f"q{i}", "original_question_number": f"Q{i}",
        "original_text": f"Question {i}", "marks": i % 5} for i in range(40)])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: question_bank.sync_pdf("Subject", "paper.pdf"), range(16)))
    bank = json.loads((paths["base"] / "question_bank.json").read_text(encoding="utf-8"))
    assert len(bank["questions"]) == 40
    assert len({row["question_id"] for row in bank["questions"]}) == 40


def test_question_bank_api_create_update_read_and_validation(tmp_path, monkeypatch):
    from tests.test_api import _client

    with _client(tmp_path, monkeypatch) as client:
        _subject(tmp_path, monkeypatch)
        asset = tmp_path / "subjects" / "Subject" / "output" / "Scrapper" / "figure.png"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"image")
        payload = {"subject_id": "Subject", "question_id": "manual-q1",
                   "text": "Original question", "marks": 5, "section_name": "Section A",
                   "source_pdf": "paper.pdf", "source_path": "source/paper.pdf",
                   "original_question_number": "Q2(b)", "page_number": 4,
                   "assets": ["output/Scrapper/figure.png"], "priority": 2,
                   "metadata": {"reviewed": True}}
        created = client.post("/api/question-bank", json=payload)
        assert created.status_code == 201, created.text
        result = created.json()
        assert result["question_id"] == "manual-q1"
        assert result["priority"] == 2
        assert result["metadata"] == {"reviewed": True}
        replay = client.post("/api/question-bank", json=payload)
        assert replay.status_code == 201 and replay.json()["question_id"] == result["question_id"]

        updated = client.patch("/api/question-bank/manual-q1?subject_id=Subject",
                               json={"text": "Edited question", "marks": 3,
                                     "source_pdf": "revised.pdf", "source_path": "source/revised.pdf",
                                     "source_pages": [4, 5], "assets": ["output/Scrapper/new-figure.png"]})
        assert updated.status_code == 200, updated.text
        assert updated.json()["question_id"] == "manual-q1"
        assert updated.json()["metadata"] == {"reviewed": True}
        assert updated.json()["source_pages"] == [4, 5]
        assert updated.json()["source_pdf"] == "revised.pdf"
        assert updated.json()["source_path"] == "source/revised.pdf"
        assert updated.json()["original_text"] == "Edited question"
        assert updated.json()["cleaned_text"] == "Edited question"
        assert updated.json()["assets"] == ["output/Scrapper/new-figure.png"]

        listing = client.get("/api/question-bank?subject_id=Subject").json()
        assert listing["pagination"]["total_items"] == 1
        saved = listing["items"][0]
        assert saved["question_id"] == "manual-q1" and saved["marks"] == 3
        assert saved["source_pdf"] == "revised.pdf" and saved["page_number"] == 4
        detail = client.get("/api/question-bank/manual-q1?subject_id=Subject")
        assert detail.status_code == 200
        assert detail.json()["original_text"] == "Edited question"

        assert client.patch("/api/question-bank/missing?subject_id=Subject", json={"marks": 4}).status_code == 404
        assert client.patch("/api/question-bank/manual-q1?subject_id=Other", json={"marks": 4}).status_code == 404
        assert client.post("/api/question-bank", json={"subject_id": "Subject", "text": "No marks"}).status_code == 422
        assert client.patch("/api/question-bank/manual-q1?subject_id=Subject", json={"marks": 0}).status_code == 422
        assert client.patch("/api/question-bank/manual-q1?subject_id=Subject",
                            json={"assets": ["../../outside.png"]}).status_code == 422
        retry_payload = {"subject_id": "Subject", "text": "Retry-safe question", "marks": 2,
                         "idempotency_key": "question-editor-save-1"}
        first = client.post("/api/question-bank", json=retry_payload)
        retry = client.post("/api/question-bank", json=retry_payload)
        assert first.status_code == retry.status_code == 201
        assert first.json()["question_id"] == retry.json()["question_id"]
        assert client.get("/api/question-bank?subject_id=Subject").json()["pagination"]["total_items"] == 2
        client.headers.pop("Authorization")
        assert client.post("/api/question-bank", json=payload).status_code == 401
        assert client.patch("/api/question-bank/manual-q1?subject_id=Subject", json={"marks": 4}).status_code == 401


def test_concurrent_question_bank_updates_are_atomic_and_preserve_fields(tmp_path, monkeypatch):
    from src.masterdeorca.services import question_bank

    paths = _subject(tmp_path, monkeypatch)
    created = question_bank.create_question("Subject", {"text": "Question", "marks": 5,
                                                        "metadata": {"keep": True}})
    qid = created["question_id"]
    changes = [{"source_pdf": f"paper-{index}.pdf"} for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda value: question_bank.update_question("Subject", qid, value), changes))
    bank = json.loads((paths["base"] / "question_bank.json").read_text(encoding="utf-8"))
    assert len(bank["questions"]) == 1
    row = bank["questions"][0]
    assert row["question_id"] == qid and row["metadata"] == {"keep": True}
    assert row["source_pdf"].startswith("paper-")


def test_subject_scoped_question_resources_are_loaded_on_demand(tmp_path, monkeypatch):
    from tests.test_api import _client
    from src.masterdeorca.services.question_bank import _atomic_write

    with _client(tmp_path, monkeypatch) as client:
        paths = _subject(tmp_path, monkeypatch)
        figure = paths["base"] / "output" / "Scrapper" / "figures" / "diagram.png"
        figure.parent.mkdir(parents=True)
        figure.write_bytes(b"png-bytes")
        _atomic_write(paths["base"] / "question_bank.json", {
            "subject": "Subject", "updated_at": "now", "questions": [
                {"question_id": "q1", "original_text": "Question text", "marks": 4,
                 "source_pdf": "paper.pdf", "source_path": "source/paper.pdf", "page_number": 3,
                 "assets": [{"id": "fig-a", "path": "output/Scrapper/figures/diagram.png", "caption": "Architecture"},
                            {"id": "fig-b", "path": "output/Scrapper/figures/diagram.png"}],
                 "tables": [{"id": "tbl-a", "headers": ["State", "Value"], "rows": [["Ready", "1"]],
                             "caption": "States"}]},
            ]})
        other_paths = storage.ensure_subject("Other")
        _atomic_write(other_paths["base"] / "question_bank.json", {
            "subject": "Other", "updated_at": "now", "questions": [{"question_id": "q2", "marks": 4}]})

        scoped = client.get("/api/question-bank?subject_id=Subject&marks=4&page=1&page_size=1")
        assert scoped.status_code == 200
        item = scoped.json()["items"][0]
        assert scoped.json()["available_marks"] == [4]
        assert scoped.json()["filters"] == {"subject_id": "Subject", "marks": 4}
        assert item["question_id"] == "q1" and item["subject_id"] == "Subject"
        assert item["text"] == "Question text"
        assert item["source_path"] == "source/paper.pdf"
        assert item["priority"] == 1
        assert item["has_figures"] is True and item["has_tables"] is True
        assert [row["resource_id"] for row in item["resources"]["figures"]] == ["fig-a", "fig-b"]
        assert item["resources"]["tables"][0]["resource_id"] == "tbl-a"
        assert item["resources"]["figures"][0]["path"] == "output/Scrapper/figures/diagram.png"
        assert client.get("/api/question-bank?subject_id=Other").json()["items"][0]["question_id"] == "q2"
        missing_subject = client.get("/api/question-bank")
        assert missing_subject.status_code == 422  # no cross-subject bank scan
        assert missing_subject.json() == {"detail": [{
            "type": "missing", "loc": ["query", "subject_id"],
            "msg": "Field required", "input": None,
        }]}

        detail = client.get("/api/question-bank/q1?subject_id=Subject")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert detail_payload["source_pdf"] == "paper.pdf"
        assert detail_payload["source_path"] == "source/paper.pdf"
        assert detail_payload["page_number"] == 3
        assert detail_payload["has_figures"] is True and detail_payload["has_tables"] is True
        assert detail_payload["resources"]["tables"][0]["resource_id"] == "tbl-a"
        assert client.get("/api/question-bank/q1?subject_id=Other").status_code == 404
        assert client.get("/api/question-bank/nope?subject_id=Subject").status_code == 404

        image = client.get("/api/question-bank/q1/resources/figure/fig-a?subject_id=Subject")
        assert image.status_code == 200
        assert image.json()["metadata"]["source_pdf"] == "paper.pdf"
        assert image.json()["metadata"]["source_page"] == 3
        assert image.json()["metadata"]["caption"] == "Architecture"
        import base64
        assert base64.b64decode(image.json()["content_base64"]) == b"png-bytes"
        second_figure = client.get("/api/question-bank/q1/resources/figure/fig-b?subject_id=Subject")
        assert second_figure.status_code == 200
        assert base64.b64decode(second_figure.json()["content_base64"]) == b"png-bytes"

        table = client.get("/api/question-bank/q1/resources/table/tbl-a?subject_id=Subject")
        assert table.status_code == 200
        assert table.json()["content"] == {"headers": ["State", "Value"], "rows": [["State", "Value"], ["Ready", "1"]]}
        assert table.json()["metadata"]["caption"] == "States"
        assert client.get("/api/question-bank/q1/resources/figure/missing?subject_id=Subject").status_code == 404
        assert client.get("/api/question-bank/missing/resources/table/0?subject_id=Subject").status_code == 404
        assert client.get("/api/question-bank/q1/resources/bogus/0?subject_id=Subject").status_code == 422
        assert client.get("/api/question-bank/q1/resources/figure/fig-a?subject_id=Other").status_code == 404
        assert client.get("/api/question-bank/q1/resources/table/missing?subject_id=Subject").status_code == 404
        client.headers.pop("Authorization")
        assert client.get("/api/question-bank?subject_id=Subject").status_code == 401
        assert client.get("/api/question-bank/q1/resources/figure/fig-a?subject_id=Subject").status_code == 401


def test_attachment_availability_flags_only_report_retrievable_resources(tmp_path, monkeypatch):
    from tests.test_api import _client
    from src.masterdeorca.services import question_bank
    from src.masterdeorca.services.question_bank import _atomic_write

    paths = _subject(tmp_path, monkeypatch)
    figure = paths["base"] / "output" / "figures" / "real.png"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"image")
    records = {
        "none": {"assets": [], "tables": []},
        "figure": {"assets": [{"id": "f1", "path": "output/figures/real.png"}], "tables": []},
        "table": {"assets": [], "tables": [{"table_id": "t1", "cells": [["h"], ["v"]]}]},
        "both": {"assets": [
            {"id": "f1", "path": "output/figures/real.png"},
            {"id": "f2", "path": "output/figures/real.png"},
        ], "tables": [
            {"table_id": "t1", "rows": [["v"]]},
            {"table_id": "t2", "headers": ["h"], "rows": [["v"]]},
        ]},
        "unavailable": {"assets": [{"id": "missing", "path": "output/figures/missing.png"}],
                        "tables": [{"table_id": "empty", "rows": []}]},
    }
    for question_id, record in records.items():
        resources = question_bank.list_question_resources(record, "Subject")
        assert bool(resources["figures"]) == (question_id in {"figure", "both"})
        assert bool(resources["tables"]) == (question_id in {"table", "both"})
        if question_id == "both":
            assert [row["resource_id"] for row in resources["figures"]] == ["f1", "f2"]
            assert [row["resource_id"] for row in resources["tables"]] == ["t1", "t2"]

    with _client(tmp_path, monkeypatch) as client:
        _atomic_write(paths["base"] / "question_bank.json", {
            "subject": "Subject", "updated_at": "now", "questions": [
                {"question_id": question_id, "original_text": question_id, "marks": 2, **record}
                for question_id, record in records.items()
            ],
        })
        response = client.get("/api/question-bank?subject_id=Subject&page_size=25")
        assert response.status_code == 200
        by_id = {item["question_id"]: item for item in response.json()["items"]}
        assert (by_id["none"]["has_figures"], by_id["none"]["has_tables"]) == (False, False)
        assert (by_id["figure"]["has_figures"], by_id["figure"]["has_tables"]) == (True, False)
        assert (by_id["table"]["has_figures"], by_id["table"]["has_tables"]) == (False, True)
        assert (by_id["both"]["has_figures"], by_id["both"]["has_tables"]) == (True, True)
        assert (by_id["unavailable"]["has_figures"], by_id["unavailable"]["has_tables"]) == (False, False)
        assert len(by_id["both"]["resources"]["figures"]) == 2
        assert len(by_id["both"]["resources"]["tables"]) == 2
        detail = client.get("/api/question-bank/none?subject_id=Subject")
        assert detail.json()["has_figures"] is False and detail.json()["has_tables"] is False


def test_marks_options_are_dynamic_numeric_and_filter_before_pagination(tmp_path, monkeypatch):
    from tests.test_api import _client
    from src.masterdeorca.services.question_bank import _atomic_write

    with _client(tmp_path, monkeypatch) as client:
        paths = _subject(tmp_path, monkeypatch)
        _atomic_write(paths["base"] / "question_bank.json", {
            "subject": "Subject", "updated_at": "now", "questions": [
                {"question_id": "q2", "original_text": "Two marks", "marks": "2.0", "source_pdf": "paper.pdf", "page_number": 1},
                {"question_id": "q3a", "original_text": "Three marks A", "marks": 3.0},
                {"question_id": "q3b", "original_text": "Three marks B", "marks": 3},
                {"question_id": "invalid", "original_text": "No usable marks", "marks": None},
                {"question_id": "zero", "original_text": "Zero is not a filter option", "marks": 0},
            ]})
        storage.ensure_subject("Other")
        _atomic_write(storage.subject_layout("Other")["base"] / "question_bank.json", {
            "subject": "Other", "updated_at": "now", "questions": [
                {"question_id": "other-q", "original_text": "Other subject", "marks": 8},
            ]})

        all_rows = client.get("/api/question-bank?subject_id=Subject&page=1&page_size=2").json()
        assert all_rows["available_marks"] == [2, 3]
        assert all_rows["pagination"] == {"page": 1, "page_size": 2, "total_items": 5, "total_pages": 3}
        assert all(item["marks"] is None or isinstance(item["marks"], (int, float)) for item in all_rows["items"])

        two = client.get("/api/question-bank?subject_id=Subject&marks=2&page=1&page_size=25").json()
        assert [item["question_id"] for item in two["items"]] == ["q2"]
        assert two["pagination"]["total_items"] == 1 and two["pagination"]["total_pages"] == 1
        assert two["items"][0]["marks"] == 2
        assert two["items"][0]["source_pdf"] == "paper.pdf" and two["items"][0]["page_number"] == 1

        three = client.get("/api/question-bank?subject_id=Subject&marks=3&page=1&page_size=1").json()
        assert three["pagination"]["total_items"] == 2 and three["pagination"]["total_pages"] == 2
        assert three["items"][0]["marks"] == 3
        empty = client.get("/api/question-bank?subject_id=Subject&marks=7").json()
        assert empty["items"] == []
        assert empty["pagination"]["total_items"] == 0 and empty["pagination"]["total_pages"] == 0
        assert empty["available_marks"] == [2, 3]
        assert client.get("/api/question-bank?subject_id=Other&marks=2").json()["items"] == []
        assert client.get("/api/question-bank?subject_id=Subject&marks=0").status_code == 422
        assert client.get("/api/question-bank?subject_id=Subject&marks=not-a-number").status_code == 422
