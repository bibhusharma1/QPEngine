from __future__ import annotations

import json
import hashlib
import uuid
from contextlib import contextmanager
from fastapi.testclient import TestClient

from src.masterdeorca import app as app_module
from src.masterdeorca import database, storage


@contextmanager
def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "store" / "db.sqlite")
    monkeypatch.setattr(database, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(database, "ADMIN_PASSWORD", "admin123")
    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    monkeypatch.setattr(app_module, "STORAGE_ROOT", tmp_path / "subjects")
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 1024)
    with TestClient(app_module.app) as client:
        response = client.post("/auth/login", json={"email": "admin@example.com", "password": "admin123"})
        assert response.status_code == 200
        client.headers.update({"Authorization": f"Bearer {response.json()['access_token']}"})
        yield client


def test_login_and_protected_subject_creation(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/subjects").status_code == 200
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/subjects", json={"name": "NewSubject"}).status_code == 201
        assert client.post("/subjects", json={"name": "NewSubject"}).status_code == 409
        assert client.post("/subjects", json={"name": "../outside"}).status_code == 422
        routes = {route.path for route in app_module.app.routes}
        assert "/subjects/{subject}/pdfs/{filename}/process" in routes
        assert "/subjects/{subject}/pdfs/{filename}/reconstruct" in routes
        assert "/subjects/{subject}/pdfs/{filename}/reconstruction" in routes
        assert "/parse/upload" not in routes
        assert "/convert" not in routes

        client.headers.pop("Authorization")
        assert client.post("/subjects", json={"name": "NoAuth"}).status_code == 401


def test_login_rejects_wrong_password(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/auth/login", json={"email": "admin@example.com", "password": "wrong"})
        assert response.status_code == 401
        with database.connect() as db:
            stored = db.execute("SELECT password_hash FROM admins WHERE email=?", ("admin@example.com",)).fetchone()[0]
        assert stored.startswith("$argon2")


def test_failed_background_job_is_persisted_as_failed(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch):
        job = {
            "job_id": "failed-local-parser",
            "subject": "OperatingSys",
            "filename": "paper.pdf",
            "status": "queued",
            "stage": "queued",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "error": None,
            "result": None,
        }
        database.save_job(job)

        def fail(_job):
            raise RuntimeError("The parser is disabled.")

        monkeypatch.setattr(app_module, "process_pdf_job", fail)
        app_module._run_job(job, ("OperatingSys", "paper.pdf"))

        saved = database.get_job(job["job_id"])
        assert saved["status"] == "failed"
        assert saved["stage"] == "failed"
        assert saved["error"] == "RuntimeError: The parser is disabled."


def test_subject_and_filename_path_validation():
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        storage.validate_subject_name("../outside")
    with pytest.raises(HTTPException):
        storage.validate_pdf_filename("../outside.pdf")
    with pytest.raises(HTTPException):
        storage.validate_pdf_filename("paper.txt")


def test_upload_accepts_pdf_and_prevents_overwrite(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert "OperatingSys" in client.get("/subjects").json()["subjects"]
        payload = b"%PDF-1.7\nexample"
        response = client.post("/subjects/OperatingSys/pdfs", files=[("files", ("paper.pdf", payload, "application/pdf"))])
        assert response.status_code == 201

        duplicate = client.post("/subjects/OperatingSys/pdfs", files=[("files", ("paper.pdf", payload, "application/pdf"))])
        assert duplicate.status_code == 409
        invalid = client.post("/subjects/OperatingSys/pdfs", files=[("files", ("fake.pdf", b"not pdf", "application/pdf"))])
        assert invalid.status_code == 415
        subject_id = database.get_subject_id("OperatingSys")
        with database.connect() as db:
            row = db.execute(
                "SELECT document_id,subject_id,subject_name,storage_key,checksum_sha256,content_blob "
                "FROM documents WHERE filename=? AND subject_id=?",
                ("paper.pdf", subject_id),
            ).fetchone()
        assert row is not None
        assert row["document_id"] == response.json()["uploaded"][0]["document_id"]
        assert row["storage_key"] == "OperatingSys/source/paper.pdf"
        assert len(row["checksum_sha256"]) == 64
        assert bytes(row["content_blob"]) == payload


def test_subject_pdf_listing_uses_only_registered_subject_documents(
    tmp_path, monkeypatch
):
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/subjects", json={"name": "Physics"}).status_code == 201
        assert client.post("/subjects", json={"name": "EmptySubject"}).status_code == 201
        for subject in ("OperatingSys", "Physics"):
            response = client.post(
                f"/subjects/{subject}/pdfs",
                files=[("files", ("same.pdf", b"%PDF-1.7\n" + subject.encode(), "application/pdf"))],
            )
            assert response.status_code == 201

        with database.connect() as db:
            db.execute(
                """INSERT INTO documents(
                    document_id,subject_name,subject_id,filename,storage_key,content_type,
                    checksum_sha256,size_bytes,metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "orphan-document",
                    "MissingSubject",
                    None,
                    "orphan.pdf",
                    "OperatingSys/source/orphan.pdf",
                    "pdf",
                    "orphan",
                    len(b"%PDF-1.7\norphan"),
                    "{}",
                    "now",
                    "now",
                ),
            )

        operating = client.get("/subjects/OperatingSys/pdfs").json()["pdfs"]
        physics = client.get("/subjects/Physics/pdfs").json()["pdfs"]
        empty = client.get("/subjects/EmptySubject/pdfs").json()["pdfs"]
        assert [item["filename"] for item in operating] == ["same.pdf"]
        assert [item["filename"] for item in physics] == ["same.pdf"]
        assert empty == []
        assert all(item["subject_id"] == "OperatingSys" for item in operating)
        assert all(item["subject_id"] == "Physics" for item in physics)


def test_upload_limit_is_enforced(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert "OperatingSys" in client.get("/subjects").json()["subjects"]
        data = b"%PDF-1.7\n" + b"x" * 1200
        response = client.post("/subjects/OperatingSys/pdfs", files=[("files", ("large.pdf", data, "application/pdf"))])
        assert response.status_code == 413


def test_force_starts_fresh_job_and_duplicate_active_job_is_rejected(tmp_path, monkeypatch):
    calls = []

    def finish(job):
        calls.append(job["job_id"])
        job.update(status="succeeded", stage="complete", result={"question_count": 1})
        database.save_job(job)

    monkeypatch.setattr(app_module, "process_pdf_job", finish)
    with _client(tmp_path, monkeypatch) as client:
        client.post("/subjects/OperatingSys/pdfs", files=[("files", ("paper.pdf", b"%PDF-1.7\n", "application/pdf"))])
        first = client.post("/subjects/OperatingSys/pdfs/paper.pdf/process", json={"force": True})
        assert first.status_code == 202
        assert client.get(f"/jobs/{first.json()['job_id']}").json()["status"] == "succeeded"

        blocked = client.post("/subjects/OperatingSys/pdfs/paper.pdf/process", json={"force": False})
        assert blocked.status_code == 409
        second = client.post("/subjects/OperatingSys/pdfs/paper.pdf/process", json={"force": True})
        assert second.status_code == 202
        assert len(calls) == 2


def test_failed_latest_job_withholds_old_parsed_result(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.post("/subjects/OperatingSys/pdfs", files=[("files", ("paper.pdf", b"%PDF-1.7\n", "application/pdf"))])
        document = database.get_document("OperatingSys", "paper.pdf")
        database.put_artifact(
            subject_id=document["subject_id"],
            document_id=document["document_id"],
            artifact_key="questions_parsed.json",
            artifact_type="parser_json",
            content_type="application/json",
            data=b'{"questions": [{"label": "old"}]}',
        )
        database.save_job({"job_id": "failed-job", "subject": "OperatingSys", "filename": "paper.pdf",
                           "status": "failed", "stage": "scraping", "started_at": "2026-01-01T00:00:00+00:00",
                           "updated_at": "2026-01-01T00:00:00+00:00", "error": "failed", "result": None})
        response = client.get("/subjects/OperatingSys/pdfs/paper.pdf/result")
        assert response.status_code == 409


def test_startup_marks_unfinished_jobs_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "store.sqlite")
    monkeypatch.setattr(database, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(database, "ADMIN_PASSWORD", "admin123")
    database.initialize()
    database.save_job({"job_id": "interrupted", "subject": "OperatingSys", "filename": "paper.pdf",
                       "status": "running", "stage": "parsing", "started_at": "2026-01-01T00:00:00+00:00",
                       "updated_at": "2026-01-01T00:00:00+00:00", "error": None, "result": None})
    database.mark_interrupted_jobs()
    assert database.get_job("interrupted")["status"] == "failed"


def test_question_bank_uses_successful_manifest_results_flattens_and_paginates(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        subject = tmp_path / "subjects" / "OperatingSys"
        manifest_dir = subject / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        results = []
        banks = [
            ("CS239 OPERATING SYSTEM_MID.pdf", [
                {"question_id": "q1", "original_question_number": "Q1", "original_text": "Explain.", "marks": 10,
                 "section_name": "Section I", "source_pages": [2], "sub_questions": [
                     {"sub_id": "a", "label": "(a)", "cleaned_text": "Define process.", "marks": 2, "sub_questions": []},
                     {"sub_id": "b", "label": "(b)", "original_text": "Define thread.", "marks": 4, "sub_questions": []},
                 ]},
                {"question_id": "q2", "original_question_number": "Q2", "cleaned_text": "Explain scheduling.", "marks": 4, "source_page": 3, "sub_questions": []},
            ]),
            ("CS239 OPERATING SYSTEM_END.pdf", [
                {"question_id": "q1", "original_question_number": "Q1", "cleaned_text": "Explain memory.", "marks": 4, "sub_questions": []},
            ]),
        ]
        entries = []
        for index, (filename, questions) in enumerate(banks):
            result = subject / "output" / "Parser" / f"paper-{index}" / "questions_parsed.json"
            result.parent.mkdir(parents=True)
            result.write_text(json.dumps({"questions": questions}), encoding="utf-8")
            entries.append({"id": f"source-{index}", "filename": filename, "relative_path": f"source/{filename}",
                            "status": "succeeded", "result_json": str(result.relative_to(subject))})
            content = b"%PDF-1.7\nlegacy test document"
            document_id = uuid.uuid4().hex
            subject_id = database.get_subject_id("OperatingSys")
            with database.connect() as db:
                db.execute(
                    """INSERT INTO documents(
                        document_id,subject_name,subject_id,filename,storage_key,content_type,
                        checksum_sha256,size_bytes,metadata_json,created_at,updated_at,content_blob
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (document_id, "OperatingSys", subject_id, filename,
                     f"OperatingSys/source/{filename}", "pdf",
                     hashlib.sha256(content).hexdigest(), len(content), "{}", "now", "now", content),
                )
            database.put_artifact(
                subject_id=subject_id, document_id=document_id,
                artifact_key="questions_parsed.json", artifact_type="parser_json",
                content_type="application/json",
                data=json.dumps({"questions": questions}).encode("utf-8"),
            )
            from src.masterdeorca import processing_manifest
            processing_manifest.update_document("OperatingSys", filename, status="succeeded")
        # Failed/still-running entries and missing results must not contribute records.
        entries.extend([
            {"id": "failed", "filename": "failed.pdf", "status": "failed", "result_json": entries[0]["result_json"]},
            {"id": "missing", "filename": "missing.pdf", "status": "completed", "result_json": "output/Parser/missing/questions_parsed.json"},
        ])
        (manifest_dir / "processing_status.json").write_text(json.dumps({"subject": "OperatingSys", "pdfs": entries}), encoding="utf-8")

        from src.masterdeorca.services.question_bank import sync_pdf
        sync_pdf("OperatingSys", banks[0][0])
        sync_pdf("OperatingSys", banks[1][0])

        response = client.get("/api/question-bank?subject_id=OperatingSys&page=1&page_size=2")
        assert response.status_code == 200
        payload = response.json()
        assert payload["pagination"] == {"page": 1, "page_size": 2, "total_items": 4, "total_pages": 2}
        assert len(payload["items"]) == 2
        assert all("question_id" in item and "source_pdf" in item for item in payload["items"])
        all_items = client.get("/api/question-bank?subject_id=OperatingSys&page_size=20").json()["items"]
        assert len(all_items) == 4  # two leaves, one standalone item, one item from the similarly named PDF
        assert {item["source_pdf"] for item in all_items} == {banks[0][0], banks[1][0]}
        assert {item["original_question_number"] for item in all_items} >= {"Q1(a)", "Q1(b)"}
        assert all(item["original_question_number"] != "Q1" for item in all_items if item["source_pdf"] == banks[0][0])
        filtered = client.get("/api/question-bank?subject_id=OperatingSys&marks=4").json()
        assert filtered["pagination"]["total_items"] == 3
        assert all(item["marks"] == 4 for item in filtered["items"])
        assert len({item["question_id"] for item in all_items}) == len(all_items)


def test_question_bank_defaults_to_25_and_filters_before_pagination(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        from src.masterdeorca.services.question_bank import _replace_bank_records

        _replace_bank_records("OperatingSys", [
            {"question_id": f"q{i}", "source_pdf": "paper.pdf", "marks": 2 if i < 27 else 4,
             "original_text": f"Question {i}"} for i in range(30)
        ])
        first = client.get("/api/question-bank?subject_id=OperatingSys").json()
        assert len(first["items"]) == 25
        assert first["pagination"] == {"page": 1, "page_size": 25, "total_items": 30, "total_pages": 2}
        filtered = client.get("/api/question-bank?subject_id=OperatingSys&marks=4&page_size=1").json()
        assert filtered["pagination"]["total_items"] == 3
        assert filtered["pagination"]["total_pages"] == 3


def test_question_priorities_authenticated_validation_and_reset(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        from src.masterdeorca.services.question_bank import _replace_bank_records

        _replace_bank_records("OperatingSys", [
            {"question_id": "stable-q1", "source_pdf": "paper.pdf", "marks": 4, "original_text": "Prompt"}
        ])
        assert client.get("/api/question-priorities?subject_id=OperatingSys").json()["priorities"] == {"stable-q1": 1}
        updated = client.put("/api/question-priorities", json={"question_id": "stable-q1", "priority": 3,
                                     "subject_id": "OperatingSys"})
        assert updated.status_code == 200
        assert updated.json()["priority"] == 3
        # Read from SQLite again, as a subsequent backend process would.
        assert client.get("/api/question-priorities?subject_id=OperatingSys").json()["priorities"] == {"stable-q1": 3}
        assert client.get("/api/question-bank?subject_id=OperatingSys").json()["items"][0]["original_text"] == "Prompt"
        assert client.put("/api/question-priorities", json={"question_id": "stable-q1", "priority": 4,
                                     "subject_id": "OperatingSys"}).status_code == 422
        assert client.put("/api/question-priorities", json={"question_id": "stable-q1", "priority": True,
                                     "subject_id": "OperatingSys"}).status_code == 422
        assert client.put("/api/question-priorities", json={"question_id": "unknown", "priority": 2,
                                     "subject_id": "OperatingSys"}).status_code == 404
        reset = client.delete("/api/question-priorities/stable-q1?subject_id=OperatingSys")
        assert reset.status_code == 200 and reset.json()["priority"] == 1
        assert client.get("/api/question-priorities?subject_id=OperatingSys").json()["priorities"] == {"stable-q1": 1}
        client.headers.pop("Authorization")
        assert client.get("/api/question-priorities").status_code == 401
        assert client.put("/api/question-priorities", json={"question_id": "stable-q1", "priority": 2}).status_code == 401
        assert client.delete("/api/question-priorities/stable-q1").status_code == 401


def test_question_priorities_concurrent_updates_are_preserved(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from src.masterdeorca import database
    from src.masterdeorca.services import question_priorities

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "priorities.sqlite")
    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    database.initialize()
    updates = [(f"q{i}", i % 3 + 1) for i in range(30)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda pair: question_priorities.set_priority(*pair), updates))
    persisted = question_priorities.get_priorities()
    assert persisted == dict(updates)


def test_generated_question_paper_docx_is_stored_and_served_from_sqlite(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        from src.masterdeorca.services.question_bank import _replace_bank_records

        _replace_bank_records("OperatingSys", [
            {"question_id": "paper-q1", "source_pdf": "source.pdf", "marks": 5,
             "original_text": "Explain process scheduling."}
        ])
        response = client.post(
            "/api/question-papers/generate",
            json={"title": "Test Paper", "subject": "OperatingSys", "exam_type": "Final",
                  "total_marks": 5, "duration": "1 hour", "seed": 1},
        )
        assert response.status_code == 200
        paper_id = response.json()["paper_id"]
        with database.connect() as db:
            row = db.execute(
                "SELECT docx_blob FROM generated_papers WHERE paper_id=?",
                (paper_id,),
            ).fetchone()
        assert bytes(row["docx_blob"]).startswith(b"PK")
        download = client.get(f"/api/question-papers/{paper_id}/docx")
        assert download.status_code == 200
        assert download.content.startswith(b"PK")


def test_cors_preflight_allows_localhost_and_127_0_0_1(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
            response = client.options(
                "/auth/login",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
            assert response.status_code in (200, 204)
            assert response.headers.get("access-control-allow-origin") == origin
            assert "POST" in (response.headers.get("access-control-allow-methods") or "")
            assert response.headers.get("access-control-allow-credentials") == "true"
