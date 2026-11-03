"""Reconciliation worker, deterministic question validation and bank safety tests."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from config.outputs import pdf_stem
from src.masterdeorca import app as app_module
from src.masterdeorca import database, storage
from src.masterdeorca.reconciliation import worker
from src.masterdeorca.reconciliation.validation import (
    OUTCOMES,
    assess_question,
)
from src.masterdeorca.services import question_bank

# ---------------------------------------------------------------------------
# sandboxes without the API layer
# ---------------------------------------------------------------------------


@contextmanager
def _sandbox(tmp_path):
    old_db, old_root = database.DATABASE_PATH, storage.STORAGE_ROOT
    database.DATABASE_PATH = tmp_path / "store" / "db.sqlite"
    storage.STORAGE_ROOT = tmp_path / "subjects"
    database.initialize()
    try:
        yield tmp_path
    finally:
        database.DATABASE_PATH, storage.STORAGE_ROOT = old_db, old_root


def _subject(tmp_path: Path, name: str, pdf: str = "paper-1.pdf") -> dict:
    paths = storage.ensure_subject(name)
    paths["source"].mkdir(parents=True, exist_ok=True)
    (paths["source"] / pdf).write_bytes(b"%PDF-1.7 test")
    stem = pdf_stem(pdf)
    scrape = paths["scrapper"] / stem
    scrape.mkdir(parents=True, exist_ok=True)
    (scrape / "questions_scrapper.json").write_text("[]")
    (scrape / "manifest.json").write_text(json.dumps({"files": []}))
    return {"paths": paths, "stem": stem, "pdf": pdf}


def _fake_parser(parsed: dict, stem: str):
    def run(manifest_path, parser_root):
        out = parser_root / stem
        out.mkdir(parents=True, exist_ok=True)
        (out / "questions_parsed.json").write_text(json.dumps(parsed))
    return run


def _parse_only(*specs) -> list[dict]:
    return [{"original_question_number": label, "marks": marks, "text": text}
            for label, marks, text in specs]


BASE_QUESTIONS = _parse_only(
    ("q1", 2, "What is 2+2?"),
    ("q2", 2, "2, 500"),
    ("q3", 2, "3, 400 e. 4, 112"),
    ("q4", 2, "--------------------------------------------------------------------------------"),
    ("q5", 2, ""),
    ("q6", 2, "42"),
    ("q7", 2, "###&&&%%%@@@"),
    ("q8", 2, "State whether the following statement is true or false."),
)


# ---------------------------------------------------------------------------
# deterministic validation rules
# ---------------------------------------------------------------------------


def test_numeric_fragments_and_marks_only_are_invalid(tmp_path):
    for text in ("2, 500", "3, 400 e. 4, 112", "42", "500 marks", "marks", "17.", "7 - 4"):
        verdict = assess_question({"text": text, "marks": None})
        assert verdict["outcome"] == "invalid", text
        assert verdict["reason"] == "numeric_or_marks_fragment", text


def test_ocr_noise_and_empty_text_are_invalid(tmp_path):
    for text in ("", "   ", "--------------------------------------------------", "###&&&%%%@@@", "^^^^^^^^^@#!!"):
        verdict = assess_question({"text": text, "marks": None})
        assert verdict["outcome"] == "invalid", text
    assert assess_question({"text": "RT%^§∓ffff@!!!", "marks": None})["reason"] == "ocr_noise"


def test_valid_short_marked_questions_survive_deterministic_checks(tmp_path):
    for text in ("What is 2+2?", "Define inertia.", "State Ohm's law.",
                 "Solve the following system of equations.",
                 "In the given figure, find the value of x."):
        verdict = assess_question({"text": text, "marks": 2})
        assert verdict["outcome"] == "valid", text


def test_ambiguous_text_without_llm_stays_uncertain(tmp_path, monkeypatch):
    verdict = assess_question({"text": "Given the solution window below.", "marks": 2})
    assert verdict["outcome"] == "needs_review"
    assert verdict["reason"] == "no_clear_question_signal"


def test_invalid_marks_metadata_downgrades_to_needs_review(tmp_path):
    verdict = assess_question({"text": "What is the capital of India?", "marks": "NaN"})
    assert verdict["outcome"] == "needs_review" and verdict["reason"] == "invalid_marks_metadata"


# ---------------------------------------------------------------------------
# end-to-end reconciliation flows
# ---------------------------------------------------------------------------


def test_scraped_but_not_parsed_is_recovered_and_only_valid_records_imported(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "Physics")
        parsed = {"metadata": {}, "questions": BASE_QUESTIONS}
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed, setup["stem"]))
        summary = worker.reconcile_all(force=True)
        totals = summary["totals"]
        assert totals["parsed_recovered"] == 1
        assert totals["imported"] == 2  # q1 and q8
        assert totals["invalid"] == 5

        rows = question_bank.read_subject("Physics")
        assert sorted(row["original_question_number"] for row in rows) == ["q1", "q8"]

        paths = setup["paths"]
        final = json.loads((paths["parser"] / setup["stem"] / "questions_parsed.json").read_text())
        outcomes = {item["original_question_number"]: item["validation"]["outcome"]
                    for item in final["questions"]}
        assert outcomes["q1"] == "valid"
        assert outcomes["q2"] == "invalid" and outcomes["q3"] == "invalid"
        assert outcomes["q5"] == "invalid" and outcomes["q6"] == "invalid"
        assert outcomes["q7"] == "invalid"
        # Uncertain/invalid records survive in the parsed output — never deleted.
        assert {item["original_question_number"] for item in final["questions"]} >= \
            {"q2", "q3", "q5", "q6", "q7"}
        for item in final["questions"]:
            annotation = item["validation"]
            assert annotation["validator"] == "reconciliation-validator/2"
            assert annotation["reason"] and annotation["checked_at"]

        manifest = json.loads((paths["manifests"] / "processing_status.json").read_text())
        entry = next(p for p in manifest["pdfs"] if p["filename"] == setup["pdf"])
        assert entry["status"] == "succeeded"

        states = {(row["stage"], row["status"]) for row in database.connect().execute(
            "SELECT stage, status FROM reconciliation_state WHERE subject='Physics'").fetchall()}
        assert states == {("parse", "complete"), ("sync", "complete")}


def test_parsed_but_not_imported_records_sync_without_a_parser_rerun(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "Mathematics")
        paths = setup["paths"]
        parsed = {"metadata": {}, "questions": BASE_QUESTIONS}
        target = paths["parser"] / setup["stem"]
        target.mkdir(parents=True, exist_ok=True)
        (target / "questions_parsed.json").write_text(json.dumps(parsed))

        def failing_parser(*args, **kwargs):  # must never be called
            raise AssertionError("parser must not run when parsed output exists")

        monkeypatch.setattr(worker, "_run_parser", failing_parser)
        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["imported"] == 2
        assert summary["totals"]["parse_failures"] == 0
        assert summary["totals"]["sync_failures"] == 0


def test_repeated_reconciliation_never_creates_duplicates(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "DuplicateSubject")
        parsed = {"metadata": {}, "questions": BASE_QUESTIONS}
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed, setup["stem"]))
        worker.reconcile_all(force=True)
        for _ in range(3):
            summary = worker.reconcile_all(force=True)
            assert summary["totals"]["imported"] == 0
        assert len(question_bank.read_subject("DuplicateSubject")) == 2


def test_batch_and_cross_document_duplicates_are_marked_not_imported(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "DuplicateDoc")
        parsed = {"metadata": {}, "questions": _parse_only(
            ("q1", 2, "What is 2+2?"),
            ("q2", 2, "What  is 2+2 ?"),   # normalized duplicate inside the batch
            ("q3", 2, "The capital of India is New Delhi."),
        )}
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed, setup["stem"]))
        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["imported"] == 2
        assert summary["totals"]["duplicates"] == 1

        # cross-document duplicate: second paper repeats q1
        parsed2 = {"metadata": {}, "questions": _parse_only(("q1", 2, "WHAT IS 2 + 2?"))}
        setup2 = _subject(tmp_path, "DuplicateDoc", pdf="paper-2.pdf")
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed2, setup2["stem"]))
        summary2 = worker.reconcile_all(force=True)
        # totals cover both docs of the subject: paper-1's known duplicate plus
        # paper-2's cross-document copy; already-imported rows never re-duplicate.
        assert summary2["totals"]["duplicates"] == 2
        assert summary2["totals"]["imported"] == 0

        paths = setup2["paths"]
        final = json.loads((paths["parser"] / setup2["stem"] / "questions_parsed.json").read_text())
        item = final["questions"][0]
        assert item["validation"]["outcome"] == "duplicate"
        assert item["validation"]["duplicate_of"]
        assert len(question_bank.read_subject("DuplicateDoc")) == 2


def test_manually_edited_bank_records_are_never_overwritten(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "EditingSubject")
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(
            {"metadata": {}, "questions": BASE_QUESTIONS}, setup["stem"]))
        worker.reconcile_all(force=True)
        bank_rows = question_bank.read_subject("EditingSubject")
        original = next(row for row in bank_rows if row["original_question_number"] == "q1")
        question_bank.update_question("EditingSubject", original["question_id"],
                                      {"text": "Manually rewritten by the teacher."})

        # a fresh re-parse produces *older* wording for the same stable ID
        parsed = {"metadata": {}, "questions": BASE_QUESTIONS}
        (setup["paths"]["scrapper"] / setup["stem"] / "manifest.json").touch()
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed, setup["stem"]))
        (setup["paths"]["parser"] / setup["stem"] / "questions_parsed.json").unlink()
        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["imported"] == 0

        rows = question_bank.read_subject("EditingSubject")
        kept = next(row for row in rows if row["original_question_number"] == "q1")
        assert kept["text"] == "Manually rewritten by the teacher."


def test_ambiguous_records_remain_uncertain_and_preserved(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "ReviewSubject")
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(
            {"metadata": {}, "questions": _parse_only(
                ("q1", 2, "What is 2+2?"),
                ("q9", 2, "Given the solution window below."),
            )}, setup["stem"]))
        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["imported"] == 1
        assert summary["totals"]["needs_review"] == 1
        paths = setup["paths"]
        final = json.loads((paths["parser"] / setup["stem"] / "questions_parsed.json").read_text())
        review = final["questions"][1]
        assert review["validation"]["outcome"] == "needs_review"
        assert review["validation"]["reason"] == "no_clear_question_signal"
        rows = question_bank.read_subject("ReviewSubject")
        assert len(rows) == 1 and rows[0]["original_question_number"] == "q1"


def test_reconciliation_leaves_ambiguous_records_for_review_without_ai(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "ReviewSubject")
        ambiguous = "Given the solution window below."
        parsed = {"metadata": {}, "questions": _parse_only(
            ("q1", 2, "What is 2+2?"),
            ("q9", 2, ambiguous),
        )}

        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed, setup["stem"]))
        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["needs_review"] == 1
        assert summary["totals"]["imported"] == 1

        paths = setup["paths"]
        final = json.loads((paths["parser"] / setup["stem"] / "questions_parsed.json").read_text())
        item = final["questions"][1]
        assert item["validation"]["outcome"] == "needs_review"
        assert item["validation"]["reason"] == "no_clear_question_signal"
        assert "llm_audited" not in item["validation"]

        # Deterministic annotations are reused on subsequent passes.
        from functions.QuesParser.ParsePipeLIne.providers import ProviderManager

        def unexpected_provider_call(*args, **kwargs):
            raise AssertionError("Reconciliation must not call an LLM provider")

        monkeypatch.setattr(ProviderManager, "invoke_with_fallback", unexpected_provider_call)
        worker.reconcile_all(force=True)


# ---------------------------------------------------------------------------
# interruption, retries, concurrency, scheduling
# ---------------------------------------------------------------------------


def test_retry_backoff_and_bounded_failures(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "RetrySubject")

        def boom(manifest_path, parser_root):
            raise RuntimeError("parser unavailable")

        monkeypatch.setattr(worker, "_run_parser", boom)
        worker._state_upsert("RetrySubject", setup["pdf"], "parse", status="none", retry_count=None,
                             error=None, next_retry_at=None)
        # Speed up the backoff clock by wetting the state helper: keep bounds small.
        monkeypatch.setattr(worker, "RETRY_BOUNDS", {"parse": 2, "sync": 5}, raising=False)

        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["parse_failures"] == 1
        state = worker.status()["pending"][0]
        assert state["status"] == "retrying" and state["retry_count"] == 1
        assert state["next_retry_at"]

        # Non-forced cycle honours the backoff and does not attempt parsing.
        summary2 = worker.reconcile_all()
        assert summary2["totals"]["parse_failures"] == 0
        assert summary2["totals"]["parsed_recovered"] == 0

        # After the backoff expires (db row backdated) the retry runs and fails again.
        with database.connect() as db:
            db.execute("UPDATE reconciliation_state SET next_retry_at=null WHERE stage='parse' "
                       "AND subject='RetrySubject'")
        worker.reconcile_all(force=True)  # forced retry #2 also fails
        worker.reconcile_all(force=True)  # forced retry exceeds the parse bound (2)
        with database.connect() as db:
            row = db.execute("SELECT * FROM reconciliation_state WHERE stage='parse' AND subject='RetrySubject'"
                             ).fetchone()
        assert row["status"] == "failed" and row["retry_count"] > 2   # bound reached, no further retries

        # Recovery clears the failure state on a later successful parse.
        with database.connect() as db:
            db.execute("DELETE FROM reconciliation_state WHERE stage='parse' AND subject='RetrySubject'")
        parsed = {"metadata": {}, "questions": BASE_QUESTIONS}
        (setup["paths"]["scrapper"] / setup["stem"] / "manifest.json").touch()
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed, setup["stem"]))
        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["parsed_recovered"] == 1
        with database.connect() as db:
            row = db.execute("SELECT status FROM reconciliation_state WHERE stage='parse' AND subject='RetrySubject'"
                             ).fetchone()
        assert row["status"] == "complete"


def test_interrupted_processing_resumes_from_last_completed_stage(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        setup = _subject(tmp_path, "InterruptedSubject")
        parsed = {"metadata": {}, "questions": BASE_QUESTIONS}
        monkeypatch.setattr(worker, "_run_parser", _fake_parser(parsed, setup["stem"]))
        # Simulate a crash that left a partial Parser stage behind.
        (setup["paths"]["parser"] / setup["stem"]).mkdir(parents=True, exist_ok=True)
        (setup["paths"]["parser"] / setup["stem"] / "partial.tmp").write_text("x")
        assert not (setup["paths"]["parser"] / setup["stem"] / "questions_parsed.json").is_file()

        summary = worker.reconcile_all(force=True)
        assert summary["totals"]["parsed_recovered"] == 1
        assert summary["totals"]["parse_failures"] == 0
        assert summary["totals"]["imported"] == 2
        final = json.loads((setup["paths"]["parser"] / setup["stem"] / "questions_parsed.json").read_text())
        assert final["questions"][0]["validation"]["outcome"] == "valid"
        assert not (setup["paths"]["parser"] / setup["stem"] / "partial.tmp").exists()
        assert worker.status()["pending"] == []


def test_concurrent_reconciliation_runs_are_skipped(tmp_path, monkeypatch):
    with _sandbox(tmp_path) as root:
        with worker._run_lock:   # in-process overlap
            summary = worker.reconcile_all()
            assert summary == {"skipped": True, "reason": "another reconciliation run is active in this process"}
        (storage.STORAGE_ROOT).mkdir(parents=True, exist_ok=True)
        with (Path(storage.STORAGE_ROOT) / "reconciliation.lock").open("a+") as stream:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)   # cross-process overlap
            summary = worker.reconcile_all()
            assert summary == {"skipped": True, "reason": "another reconciliation run is active in another process"}
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        assert "skipped" not in worker.reconcile_all()


# ---------------------------------------------------------------------------
# API layer: manual trigger, auth, readonly enforcement
# ---------------------------------------------------------------------------


@contextmanager
def _client(tmp_path, monkeypatch):
    class _NoopHandle:
        def stop(self):
            pass

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "store" / "db.sqlite")
    monkeypatch.setattr(database, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(database, "ADMIN_PASSWORD", "admin123")
    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    monkeypatch.setattr(app_module, "STORAGE_ROOT", tmp_path / "subjects")
    monkeypatch.setattr(worker, "start_scheduler", lambda: _NoopHandle())
    with TestClient(app_module.app) as client:
        response = client.post("/auth/login", json={"email": "admin@example.com", "password": "admin123"})
        assert response.status_code == 200
        client.headers.update({"Authorization": f"Bearer {response.json()['access_token']}"})
        yield client


def test_manual_trigger_endpoint_reports_summary_and_requires_admin(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/reconciliation/run")
        assert response.status_code == 200
        body = response.json()
        assert "skipped" not in body
        assert "OperatingSys" in body["subjects"]
        assert isinstance(body["totals"]["imported"], int)

        status = client.get("/api/reconciliation/status").json()
        assert status["interval_seconds"] in {12 * 60 * 60, 24 * 60 * 60}
        assert status["last_run"]["subjects"]
        assert isinstance(status["pending"], list)


def test_manual_trigger_is_blocked_for_teachers_and_readonly_admins(tmp_path, monkeypatch):
    from tests.test_api import _client as _api_client

    with _api_client(tmp_path, monkeypatch) as client:
        client.post("/teachers", json={
            "name": "Teacher", "email": "teacher@example.com", "department": "CSE",
            "password": "teaching1", "status": "approved"})
        client.put("/auth/access-mode", json={"access_mode": "readonly"})
        login = client.post("/auth/login", json={"email": "teacher@example.com", "password": "teaching1"})
        assert login.status_code == 200
    auth_teacher = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # Fresh client: readonly state persists per administrator account.
    with _api_client(tmp_path, monkeypatch) as fresh:
        assert fresh.post("/api/reconciliation/run",
                          headers={**auth_teacher}).status_code == 403
        assert fresh.post("/api/reconciliation/run").status_code == 403  # readonly admin
        fresh.put("/auth/access-mode", json={"access_mode": "edit"})
        assert fresh.post("/api/reconciliation/run").status_code == 200


def test_all_outcomes_are_stable_values():
    assert set(OUTCOMES) == {"valid", "needs_review", "invalid", "duplicate"}
