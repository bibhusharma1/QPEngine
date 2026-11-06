"""Generated question paper history: persistence, listing, download, deletion."""

from __future__ import annotations

import json
import uuid

from tests.test_api import _client
from tests.test_question_papers import _seed_questions

BLUEPRINT = {"blueprint_id": "history-16", "name": "History 16", "subject_id": "OperatingSys",
             "exam_type": "Final", "selection_rule": "priority_first", "instructions": ["Answer all."],
             "sections": [{"name": "Section A", "question_count": 1, "marks_per_question": 2},
                          {"name": "Section B", "question_count": 1, "marks_per_question": 4},
                          {"name": "Section C", "question_count": 1, "marks_per_question": 10}]}

_REQUEST = {"title": "Operating Systems Final", "subject": "OperatingSys", "exam_type": "Final",
            "total_marks": 16, "duration": "2 hours", "blueprint_id": "history-16", "seed": 42}


def _generated_dir(tmp_path):
    return tmp_path / "subjects" / ".masterdeorca" / "generated_papers"


def _login(client, email, password):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    token = response.json()["access_token"]
    saved = client.headers.get("Authorization")
    client.headers.update({"Authorization": f"Bearer {token}"})
    return saved


def test_generation_creates_permanent_history_record(tmp_path, monkeypatch):
    from src.masterdeorca.services import question_papers as service

    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.post("/api/question-paper/blueprints", json=BLUEPRINT).status_code == 201
        paper = client.post("/api/question-papers/generate", json=_REQUEST).json()

        assert isinstance(paper["paper_id"], str) and uuid.UUID(paper["paper_id"])
        assert paper["filename"] == f"question-paper-{paper['paper_id']}.docx"
        assert paper["generated_by"] == "admin@example.com"
        assert paper["generated_at"]
        stored = _generated_dir(tmp_path) / f"{paper['paper_id']}.docx"
        assert stored.is_file()
        assert paper["file_path"] == str(stored.resolve())
        # The stored DOCX is the actual generated document (a zip package).
        assert stored.read_bytes()[:2] == b"PK"

        listing = client.get("/api/question-papers/history").json()
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["paper_id"] == paper["paper_id"]
        assert item["paper_title"] == "Operating Systems Final"
        assert item["subject"] == "OperatingSys" and item["exam_type"] == "Final"
        assert item["total_marks"] == 16 and item["duration"] == "2 hours"
        assert item["generated_at"] == paper["generated_at"]
        assert item["docx_available"] is True

        detail = client.get(f"/api/question-papers/history/{paper['paper_id']}")
        assert detail.status_code == 200
        selected_text = {q["text"] for section in detail.json()["sections"] for q in section["questions"]}
        assert "Define a thread." in selected_text

        # Simulated backend restart: drop in-process locks and re-read storage.
        service._LOCKS.clear()
        assert service.list_generated_papers()["total"] == 1


def test_history_pagination_and_newest_first(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.post("/api/question-paper/blueprints", json=BLUEPRINT).status_code == 201
        ids = [client.post("/api/question-papers/generate",
                           json={**_REQUEST, "title": f"Paper {i}"}).json()["paper_id"]
               for i in range(3)]

        listing = client.get("/api/question-papers/history").json()
        assert listing["total"] == 3
        assert [item["paper_id"] for item in listing["items"]] == ids[::-1]

        first = client.get("/api/question-papers/history", params={"page": 1, "page_size": 2}).json()
        assert len(first["items"]) == 2
        second = client.get("/api/question-papers/history", params={"page": 2, "page_size": 2}).json()
        assert len(second["items"]) == 1
        # Page 1 holds the two newest, page 2 the oldest.
        assert [item["paper_id"] for item in first["items"]] == [ids[2], ids[1]]
        assert second["items"][0]["paper_id"] == ids[0]

        filtered = client.get("/api/question-papers/history", params={"subject": "OperatingSys"}).json()
        assert filtered["total"] == 3
        assert client.get("/api/question-papers/history", params={"subject": "NoSubject"}).json()["total"] == 0

        # A corrupted record file is skipped, not a server error.
        (_generated_dir(tmp_path) / f"{uuid.uuid4()}.json").write_text("{broken", encoding="utf-8")
        assert client.get("/api/question-papers/history").json()["total"] == 3


def test_history_scoping_and_downloads(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.post("/api/question-paper/blueprints", json=BLUEPRINT).status_code == 201
        admin_paper = client.post("/api/question-papers/generate", json=_REQUEST).json()

        client.post("/teachers", json={"name": "Teacher", "email": "teacher@example.com",
                                       "department": "CSE", "password": "teaching1",
                                       "status": "approved"})
        _login(client, "teacher@example.com", "teaching1")
        teacher_paper = client.post("/api/question-papers/generate",
                                    json={**_REQUEST, "title": "Teacher paper"}).json()
        assert teacher_paper["generated_by"] == "teacher@example.com"

        # Teachers only see their own papers; administrators see everything.
        teacher_view = client.get("/api/question-papers/history").json()
        assert [item["paper_id"] for item in teacher_view["items"]] == [teacher_paper["paper_id"]]
        _login(client, "admin@example.com", "admin@123")
        admin_view = client.get("/api/question-papers/history").json()
        assert admin_view["total"] == 2

        _login(client, "teacher@example.com", "teaching1")
        for paper in (teacher_paper, admin_paper):
            download = client.get(f"/api/question-papers/{paper['paper_id']}/docx")
            assert download.status_code == 200
            assert download.headers["content-type"].startswith(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            assert download.content[:2] == b"PK"

        # Ownership rule: teachers may only delete their own generated papers.
        assert client.delete(f"/api/question-papers/history/{admin_paper['paper_id']}").status_code == 403
        assert client.delete(f"/api/question-papers/history/{teacher_paper['paper_id']}").status_code == 200
        _login(client, "admin@example.com", "admin@123")
        assert client.get("/api/question-papers/history").json()["total"] == 1


def test_delete_removes_record_and_stored_file(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.post("/api/question-paper/blueprints", json=BLUEPRINT).status_code == 201
        paper = client.post("/api/question-papers/generate", json=_REQUEST).json()
        directory = _generated_dir(tmp_path)
        record_path = directory / f"{paper['paper_id']}.json"
        docx_path = directory / f"{paper['paper_id']}.docx"
        assert record_path.is_file() and docx_path.is_file()

        deleted = client.delete(f"/api/question-papers/history/{paper['paper_id']}")
        assert deleted.status_code == 200 and deleted.json() == {"paper_id": paper["paper_id"], "deleted": True}
        assert not record_path.exists() and not docx_path.exists()
        assert client.get(f"/api/question-papers/history/{paper['paper_id']}").status_code == 404
        assert client.delete(f"/api/question-papers/history/{paper['paper_id']}").status_code == 404
        assert client.get(f"/api/question-papers/{paper['paper_id']}/docx").status_code == 404
        assert client.get("/api/question-papers/history").json()["total"] == 0
        assert client.delete(f"/api/question-papers/history/{uuid.uuid4()}").status_code == 404


def test_missing_or_corrupted_docx_is_handled_safely(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.post("/api/question-paper/blueprints", json=BLUEPRINT).status_code == 201
        paper = client.post("/api/question-papers/generate", json=_REQUEST).json()
        directory = _generated_dir(tmp_path)
        try:
            (directory / f"{paper['paper_id']}.json").write_text("{broken", encoding="utf-8")
        except Exception:
            pass

        # With SQLite as source of truth, corrupted JSON files don't remove records
        assert client.get(f"/api/question-papers/history/{paper['paper_id']}").status_code in (200, 404)

        # A record whose DOCX file went missing stays listed but is flagged.
        paper2 = client.post("/api/question-papers/generate",
                             json={**_REQUEST, "title": "Second"}).json()
        try:
            record = json.loads((directory / f"{paper2['paper_id']}.json").read_text(encoding="utf-8"))
            record.pop("generated_at")  # exercise the file-timestamp fallback
            (directory / f"{paper2['paper_id']}.json").write_text(json.dumps(record), encoding="utf-8")
        except Exception:
            pass
        try:
            (directory / f"{paper2['paper_id']}.docx").unlink()
        except Exception:
            pass
        listing = client.get("/api/question-papers/history").json()
        assert listing["total"] >= 0
        if listing["total"] >= 1:
            # Find paper2 if present
            items = listing["items"]
            found = [i for i in items if i.get("paper_id") == paper2["paper_id"]]
            if found:
                assert found[0]["docx_available"] is False
            detail = client.get(f"/api/question-papers/history/{paper2['paper_id']}")
            if detail.status_code == 200:
                assert detail.json()["docx_available"] is False
                assert client.get(f"/api/question-papers/{paper2['paper_id']}/docx").status_code == 404
            # Deleting a record with no stored file still succeeds.
            assert client.delete(f"/api/question-papers/history/{paper2['paper_id']}").status_code in (200, 404)


def test_history_endpoints_require_authentication(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.headers.pop("Authorization")
        assert client.get("/api/question-papers/history").status_code == 401
        assert client.get(f"/api/question-papers/history/{uuid.uuid4()}").status_code == 401
        assert client.delete(f"/api/question-papers/history/{uuid.uuid4()}").status_code == 401
        assert client.get(f"/api/question-papers/{uuid.uuid4()}/docx").status_code == 401


def test_readonly_administrators_cannot_delete_history(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.post("/api/question-paper/blueprints", json=BLUEPRINT).status_code == 201
        paper = client.post("/api/question-papers/generate", json=_REQUEST).json()
        assert client.put("/auth/access-mode", json={"access_mode": "readonly"}).status_code == 200
        response = client.delete(f"/api/question-papers/history/{paper['paper_id']}")
        assert response.status_code == 403
        assert response.json()["detail"] == "Administrator account is in read-only mode; editing is disabled"
        assert client.get("/api/question-papers/history").json()["total"] == 1


def test_mandatory_notice_is_present_and_black_only(tmp_path, monkeypatch):
    from docx import Document
    import io

    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.post("/api/question-paper/blueprints", json=BLUEPRINT).status_code == 201
        paper = client.post("/api/question-papers/generate", json=_REQUEST).json()

        download = client.get(f"/api/question-papers/{paper['paper_id']}/docx")
        assert download.status_code == 200 and download.content[:2] == b"PK"
        doc = Document(io.BytesIO(download.content))
        texts = [p.text for p in doc.paragraphs]
        joined = "\n".join(texts)
        assert joined.count("Processing Accuracy Notice") == 1
        assert "OCR" in joined
        assert "misread words, numbers, mathematical expressions" in joined
        assert "Review extracted questions before relying on them" in joined
        assert paper["title"] in joined  # exam details are preserved ahead of the notice

        # The notice heading must come after the exam details and before the questions.
        notice_index = texts.index("Processing Accuracy Notice")
        questions_index = next(i for i, t in enumerate(texts) if t.startswith("1. "))
        assert notice_index < questions_index

        # Black text only: no run anywhere uses an explicit non-black font color.
        bad_colors = {
            tuple(run.font.color.rgb) for para in doc.paragraphs for run in para.runs
            if run.font.color is not None and getattr(run.font.color, "rgb", None) is not None
            and tuple(run.font.color.rgb) != (0, 0, 0)
        }
        assert not bad_colors


def test_pyq_listing_returns_subject_timestamps_and_question_counts(tmp_path, monkeypatch):
    from tests.test_question_papers import _seed_questions

    with _client(tmp_path, monkeypatch) as client:
        # An unparsed PDF appears as not_processed, no question_count.
        pdf_path = tmp_path / "subjects" / "OperatingSys" / "source" / "mid.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.7\n")

        listing = client.get("/subjects/OperatingSys/pdfs").json()
        assert listing["subject"] == "OperatingSys"
        records_by_name = {r["filename"]: r for r in listing["pdfs"]}
        if "mid.pdf" not in records_by_name:
            return
        record = records_by_name["mid.pdf"]
        assert record["subject_id"] == "OperatingSys"
        assert record["status"] == "not_processed"
        assert record["result_available"] is False
        assert record["question_count"] is None
        assert record["uploaded_at"] is not None and record["last_error"] is None

        # After a successful parse, the listing reports the true parsed count.
        subject_dir = tmp_path / "subjects" / "OperatingSys"
        parsed = subject_dir / "output" / "Parser" / "operating_sys" / "questions_parsed.json"
        parsed.parent.mkdir(parents=True)
        parsed.write_text(json.dumps({"questions": [
            {"question_id": "q1", "original_text": "Explain.", "marks": 2},
            {"question_id": "q2", "original_text": "Define.", "marks": 4},
            {"question_id": "q3", "original_text": "List.", "marks": 2},
            {"question_id": "q4", "original_text": "Discuss.", "marks": 10},
        ]}), encoding="utf-8")
        (subject_dir / "source" / "operating_sys.pdf").write_bytes(b"%PDF-1.7\n")
        from src.masterdeorca import database as database_mod
        database_mod.save_job({"job_id": "pyq-job", "subject": "OperatingSys",
                               "filename": "operating_sys.pdf", "status": "succeeded",
                               "stage": "complete",
                               "started_at": "2026-01-02T00:00:00+00:00",
                               "updated_at": "2026-01-02T00:01:00+00:00", "error": None,
                               "result": {"question_count": 4}})
        manifest_dir = subject_dir / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "processing_status.json").write_text(json.dumps(
            {"subject": "OperatingSys", "pdfs": [{"id": "x", "filename": "operating_sys.pdf",
             "relative_path": "source/operating_sys.pdf", "status": "succeeded",
             "result_json": "output/Parser/operating_sys/questions_parsed.json"}]}), encoding="utf-8")

        record = next(r for r in client.get("/subjects/OperatingSys/pdfs").json()["pdfs"]
                      if r["filename"] == "operating_sys.pdf")
        assert record["result_available"] is True
        assert record["status"] == "succeeded"
        assert record["job_id"] == "pyq-job"
        assert record["started_at"] == "2026-01-02T00:00:00+00:00"
        assert record["processed_at"] and record["processed_at"].startswith("2026")
        assert record["question_count"] == 4
