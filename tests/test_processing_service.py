from __future__ import annotations

import json
from pathlib import Path

from src.masterdeorca import database, storage
from src.masterdeorca.services import pipeline


def _job(tmp_path, monkeypatch, filename="paper.pdf"):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "store.sqlite")
    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    paths = storage.ensure_subject("Subject")
    source = paths["source"] / filename
    source.write_bytes(b"%PDF-1.7\nmock")
    database.initialize()
    return {"job_id": "job-one", "subject": "Subject", "filename": filename,
            "status": "queued", "stage": "queued", "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00", "error": None, "result": None}, paths


def test_scrape_failure_does_not_run_parser_or_replace_previous_bank(tmp_path, monkeypatch):
    job, paths = _job(tmp_path, monkeypatch)
    target = paths["parser"] / "paper" / "questions_parsed.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"questions": [{"label": "old"}]}', encoding="utf-8")
    called = []

    def fail_scrape(*args, **kwargs):
        raise RuntimeError("synthetic scrape failure")

    monkeypatch.setattr(pipeline, "process_pdf", fail_scrape)
    monkeypatch.setattr(pipeline, "parse_questions", lambda **kwargs: called.append(kwargs))
    pipeline.process_pdf_job(job)

    assert database.get_job("job-one")["status"] == "failed"
    assert not called
    assert json.loads(target.read_text(encoding="utf-8"))["questions"][0]["label"] == "old"
    assert not (paths["base"] / "question_bank.json").exists()


def test_successful_job_parses_fresh_manifest_and_publishes_outputs(tmp_path, monkeypatch):
    job, paths = _job(tmp_path, monkeypatch)
    parsed_calls = []

    def fake_scrape(source: Path, output: Path):
        output.mkdir(parents=True)
        manifest = {"source_pdf": str(source), "page_count": 1, "pages": []}
        (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return ([{"pdf_name": source.name, "page_number": 1, "raw_text": "Question text"}], manifest)

    def fake_parser(*, manifest_paths):
        manifest = Path(manifest_paths[0])
        assert manifest.is_file()
        parsed_calls.append(manifest)
        destination = Path(pipeline.parser_config.OUTPUT_DIR) / "paper" / "questions_parsed.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({"questions": [{"label": "Q1", "cleaned_text": "Question text"}],
                                           "metadata": {"total_pages_processed": 1, "providers_used": []}}), encoding="utf-8")

    monkeypatch.setattr(pipeline, "process_pdf", fake_scrape)
    monkeypatch.setattr(pipeline, "parse_questions", fake_parser)
    pipeline.process_pdf_job(job)

    assert database.get_job("job-one")["status"] == "succeeded"
    assert len(parsed_calls) == 1
    assert (paths["scrapper"] / "paper" / "questions_scrapper.json").is_file()
    final_json = paths["parser"] / "paper" / "questions_parsed.json"
    assert json.loads(final_json.read_text(encoding="utf-8"))["questions"][0]["label"] == "Q1"
    bank = json.loads((paths["base"] / "question_bank.json").read_text(encoding="utf-8"))
    assert len(bank["questions"]) == 1
    assert bank["questions"][0]["source_pdf"] == "paper.pdf"
    assert not list(paths["metadata"].glob("job-one-*"))
