from __future__ import annotations

import json
import zipfile

from docx import Document

from tests.test_api import _client
from src.masterdeorca.services.question_bank import _atomic_write


def _seed_questions(tmp_path):
    bank = tmp_path / "subjects" / "OperatingSys" / "question_bank.json"
    _atomic_write(bank, {"subject": "OperatingSys", "updated_at": "now", "questions": [
        {"question_id": "q2-a", "marks": 2, "original_text": "Define a process.", "source_pdf": "mid.pdf"},
        {"question_id": "q2-b", "marks": 2, "cleaned_text": "Define a thread.", "source_pdf": "end.pdf"},
        {"question_id": "q4-a", "marks": 4, "original_text": "Explain scheduling.", "source_pdf": "mid.pdf"},
        {"question_id": "q10-a", "marks": 10, "original_text": "Discuss virtual memory.", "source_pdf": "end.pdf"},
    ]})


def test_blueprint_crud_validation_and_precedence(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        blueprint = {"blueprint_id": "os-midterm", "name": "OS Midterm", "subject_id": "OperatingSys",
                     "exam_type": "Midterm", "sections": [{"name": "Section A", "question_count": 1,
                     "marks_per_question": 4, "question_type": "short"}]}
        created = client.post("/api/question-paper/blueprints", json=blueprint)
        assert created.status_code == 201
        assert client.get("/api/question-paper/blueprints/os-midterm").json()["name"] == "OS Midterm"
        assert client.put("/api/question-paper/blueprints/os-midterm", json={**blueprint, "name": "Updated"}).status_code == 200
        assert client.get("/api/question-paper/blueprints").status_code == 200
        listing = client.get("/api/question-paper/blueprints").json()
        assert listing["quick_mark_presets"] == [15, 25, 50, 60, 70, 80, 100]
        assert sum(item["blueprint_id"] == "default-generic" for item in listing["items"]) == 1
        assert client.post("/api/question-paper/blueprints", json={**blueprint, "blueprint_id": "bad",
                              "sections": [{"name": "A", "question_count": 0, "marks_per_question": 4}]}).status_code == 422
        assert client.delete("/api/question-paper/blueprints/default-generic").status_code == 409
        assert client.delete("/api/question-paper/blueprints/os-midterm").json()["deleted"] is True
        assert client.get("/api/question-paper/blueprints/os-midterm").status_code == 404


def test_assigned_staff_can_save_blueprint_without_repeating_sole_subject(tmp_path, monkeypatch):
    from src.masterdeorca import database

    with _client(tmp_path, monkeypatch) as client:
        staff = client.post("/teachers", json={"name": "HOD", "email": "hod@example.com",
                                                "department": "CSE", "password": "teaching1"})
        assert staff.status_code == 201
        database.set_teacher_subjects(staff.json()["id"], ["OperatingSys"])
        login = client.post("/auth/login", json={"email": "hod@example.com", "password": "teaching1"})
        assert login.status_code == 200
        client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})

        response = client.post("/blueprints", json={"blueprint_id": "hod-os", "name": "OS exam",
                                   "sections": [{"name": "Section A", "question_count": 1,
                                                 "marks_per_question": 5}]})
        assert response.status_code == 201, response.text
        assert response.json().get("subject_id") is None
        assert client.get("/blueprints/hod-os").status_code == 200
        updated = client.put("/blueprints/hod-os", json={"blueprint_id": "hod-os", "name": "Updated",
                                  "sections": [{"name": "Section A", "question_count": 1,
                                                "marks_per_question": 5}]})
        assert updated.status_code == 200, updated.text
        assert updated.json().get("subject_id") is None


def test_blueprints_are_shared_across_authenticated_users(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        for email in ("user-a@example.com", "user-b@example.com"):
            assert client.post("/teachers", json={"name": email, "email": email,
                                                   "department": "CSE", "password": "teaching1"}).status_code == 201
        login_a = client.post("/auth/login", json={"email": "user-a@example.com", "password": "teaching1"})
        assert login_a.status_code == 200
        client.headers.update({"Authorization": f"Bearer {login_a.json()['access_token']}"})
        blueprint = {"blueprint_id": "shared-os", "name": "OS exam", "subject_id": "OperatingSys",
                     "sections": [{"name": "Section A", "question_count": 1, "marks_per_question": 5}]}
        saved = client.post("/blueprints", json=blueprint)
        assert saved.status_code == 201, saved.text
        client.headers.update({"Authorization": f"Bearer {login_a.json()['access_token']}"})
        assert client.get("/blueprints/shared-os").json()["name"] == "OS exam"

        login_b = client.post("/auth/login", json={"email": "user-b@example.com", "password": "teaching1"})
        assert login_b.status_code == 200
        client.headers.update({"Authorization": f"Bearer {login_b.json()['access_token']}"})
        assert client.get("/blueprints/shared-os").status_code == 200
        assert any(row["blueprint_id"] == "shared-os" for row in client.get("/blueprints").json()["items"])
        patched = client.patch("/blueprints/shared-os", json={"name": "Renamed by B"})
        assert patched.status_code == 200, patched.text
        assert client.get("/blueprints/shared-os").json()["name"] == "Renamed by B"
        copied = client.post("/blueprints/shared-os/duplicate", json={"blueprint_id": "shared-copy"})
        assert copied.status_code == 201, copied.text
        assert client.get("/blueprints/shared-copy").status_code == 200
        assert client.delete("/blueprints/shared-os").json()["deleted"] is True
        assert client.get("/blueprints/shared-os").status_code == 404

        client.headers.update({"Authorization": f"Bearer {login_a.json()['access_token']}"})
        assert client.get("/blueprints/shared-os").status_code == 404
        assert client.get("/blueprints/shared-copy").json()["name"] == "Renamed by B (copy)"
        assert client.delete("/blueprints/shared-copy").status_code == 200
        client.headers.pop("Authorization")
        assert client.get("/blueprints").status_code == 401
        assert client.patch("/blueprints/shared-os", json={"name": "No auth"}).status_code == 401

        # Administrative endpoints retain their separate role check.
        assert client.post("/subjects", json={"name": "NotAllowed"}).status_code == 401
        client.headers.update({"Authorization": f"Bearer {login_b.json()['access_token']}"})
        assert client.post("/subjects", json={"name": "NotAllowed"}).status_code == 403


def test_blueprint_persists_to_one_canonical_file_and_default_is_initialized(tmp_path, monkeypatch):
    from src.masterdeorca.services import question_papers

    with _client(tmp_path, monkeypatch) as client:
        path = tmp_path / "subjects" / ".masterdeorca" / "question_paper_blueprints.json"
        listing = client.get("/api/question-paper/blueprints")
        assert listing.status_code == 200

        blueprint = {"blueprint_id": "persisted", "name": "Persistent", "sections": [
            {"name": "Section A", "question_count": 2, "marks_per_question": 5}], "total_marks": 10}
        assert client.post("/api/question-paper/blueprints", json=blueprint).status_code == 201
        assert client.put("/api/question-paper/blueprints/persisted",
                         json={**blueprint, "name": "Updated"}).status_code == 200

        # Re-read through the service as a fresh process would after backend restart.
        question_papers._LOCKS.clear()
        records = question_papers.list_blueprints()
        assert [row["blueprint_id"] for row in records].count("persisted") == 1
        assert question_papers.get_blueprint("persisted")["name"] == "Updated"
        assert sum(row["blueprint_id"] == "default-generic" for row in records) == 1

        duplicate_default = {"blueprint_id": "other-default", "name": "Duplicate default",
                             "is_default": True, "sections": [{"name": "A", "question_count": 1,
                             "marks_per_question": 15}], "total_marks": 15}
        assert client.post("/api/question-paper/blueprints", json=duplicate_default).status_code == 422
        bad_total = {**blueprint, "blueprint_id": "bad-total", "total_marks": 9}
        assert client.post("/api/question-paper/blueprints", json=bad_total).status_code == 422
        duplicate_sections = {**blueprint, "blueprint_id": "duplicate-sections", "sections": [
            {"name": "A", "question_count": 1, "marks_per_question": 5},
            {"name": "a", "question_count": 1, "marks_per_question": 5}], "total_marks": 10}
        assert client.post("/api/question-paper/blueprints", json=duplicate_sections).status_code == 422


def test_frontend_blueprint_routes_patch_duplicate_and_persist(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        base = {"blueprint_id": "ui-template", "name": "UI template", "sections": [
            {"name": "Section A", "question_count": 2, "marks_per_question": 5}], "total_marks": 10}
        created = client.post("/blueprints", json=base)
        assert created.status_code == 201, created.text
        assert client.get("/blueprints").status_code == 200
        assert client.get("/blueprints/ui-template").json()["name"] == "UI template"

        patched = client.patch("/blueprints/ui-template", json={"name": "Renamed"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["blueprint_id"] == "ui-template"
        assert patched.json()["name"] == "Renamed"
        assert client.patch("/blueprints/ui-template", json={"total_marks": 11}).status_code == 422

        copied = client.post("/blueprints/ui-template/duplicate", json={"blueprint_id": "ui-copy"})
        assert copied.status_code == 201, copied.text
        assert copied.json()["blueprint_id"] == "ui-copy"
        assert copied.json()["name"] == "Renamed (copy)"
        assert client.post("/blueprints/ui-template/duplicate", json={"blueprint_id": "ui-copy"}).status_code == 409

        # A service reload reads the same persisted record after the simulated restart.
        from src.masterdeorca.services import question_papers
        question_papers._LOCKS.clear()
        assert client.get("/blueprints/ui-template").json()["name"] == "Renamed"
        assert client.delete("/blueprints/ui-copy").json() == {"blueprint_id": "ui-copy", "deleted": True}
        assert client.delete("/blueprints/ui-template").json() == {"blueprint_id": "ui-template", "deleted": True}
        assert client.get("/blueprints/ui-template").status_code == 404

        client.headers.pop("Authorization")
        assert client.get("/blueprints").status_code == 401


def test_fixed_blueprint_generation_totals_priority_and_docx(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        assert client.put("/api/question-priorities", json={"question_id": "q2-a", "priority": 3,
                              "subject_id": "OperatingSys"}).status_code == 200
        blueprint = {"blueprint_id": "paper-20", "name": "20 marks", "subject_id": "OperatingSys",
                     "exam_type": "Final", "selection_rule": "priority_first", "instructions": ["Answer all."],
                     "sections": [{"name": "Section A", "question_count": 1, "marks_per_question": 2},
                                  {"name": "Section B", "question_count": 1, "marks_per_question": 4},
                                  {"name": "Section C", "question_count": 1, "marks_per_question": 10}]}
        assert client.post("/api/question-paper/blueprints", json=blueprint).status_code == 201
        request = {"title": "Operating Systems Final", "subject": "OperatingSys", "exam_type": "Final",
                   "total_marks": 16, "duration": "2 hours", "seed": 42}
        response = client.post("/api/question-papers/generate", json=request)
        assert response.status_code == 200, response.text
        paper = response.json()
        assert paper["total_marks"] == paper["requested_total_marks"] == 16
        assert paper["blueprint_id"] == "paper-20"
        selected = [q for section in paper["sections"] for q in section["questions"]]
        assert len(selected) == 3 and len({q["question_id"] for q in selected}) == 3
        assert selected[0]["priority"] == 3
        assert sum(q["marks"] for q in selected) == 16
        download = client.get(paper["docx_url"])
        assert download.status_code == 200
        path = tmp_path / "paper.docx"
        path.write_bytes(download.content)
        text = "\n".join(p.text for p in Document(path).paragraphs)
        assert "Operating Systems Final" in text and "Discuss virtual memory." in text
        bad_total = client.post("/api/question-papers/generate", json={**request, "total_marks": 18})
        assert bad_total.status_code == 422
        assert bad_total.json()["errors"][0]["field"] == "total_marks"
        assert json.loads((tmp_path / "subjects" / "OperatingSys" / "question_bank.json").read_text())["questions"][0]["question_id"] == "q2-a"


def test_generic_generation_and_insufficient_questions_authentication(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        response = client.post("/api/question-papers/generate", json={"title": "Short test", "subject": "OperatingSys",
                                 "exam_type": "Quiz", "total_marks": 6, "duration": "30 minutes", "seed": 7})
        assert response.status_code == 200
        paper = response.json()
        assert paper["blueprint_id"] == "default-generic"
        assert paper["total_marks"] == 6
        assert sum(q["marks"] for section in paper["sections"] for q in section["questions"]) == 6
        insufficient = client.post("/api/question-papers/generate", json={"title": "Long test", "subject": "OperatingSys",
                                      "exam_type": "Quiz", "total_marks": 100, "duration": "1 hour"})
        assert insufficient.status_code == 200
        assert insufficient.json()["total_marks"] == 18
        assert insufficient.json()["shortages"]
        client.headers.pop("Authorization")
        assert client.get("/api/question-paper/blueprints").status_code == 401
        assert client.post("/api/question-papers/generate", json={"title": "x", "subject": "OperatingSys",
                       "exam_type": "x", "total_marks": 2, "duration": "x"}).status_code == 401


def test_generation_validation_is_field_specific_and_accepts_duration_formats(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        base = {"paper_title": "Validation check", "subject": "OperatingSys", "exam_type": "Exam",
                "total_marks": 2, "duration": "3 Hours"}
        # Each advertised duration form passes request validation and reaches generation.
        for duration in ("3 Hours", "2 Hours", "90 Minutes"):
            response = client.post("/api/question-papers/generate", json={**base, "duration": duration})
            assert response.status_code == 200, response.text
            assert response.json()["duration"] == duration

        missing_duration = client.post("/api/question-papers/generate", json={k: v for k, v in base.items() if k != "duration"})
        assert missing_duration.status_code == 422
        assert missing_duration.json()["errors"] == [{"field": "duration", "message": "This field is required."}]
        assert "input" not in missing_duration.text and "ctx" not in missing_duration.text

        for field in ("paper_title", "subject", "exam_type", "total_marks"):
            response = client.post("/api/question-papers/generate", json={k: v for k, v in base.items() if k != field})
            assert response.status_code == 422
            assert response.json()["errors"][0]["field"] == field
        for field, value in (("paper_title", "  "), ("subject", "  "), ("exam_type", "  "), ("duration", "  ")):
            response = client.post("/api/question-papers/generate", json={**base, field: value})
            assert response.status_code == 422
            assert response.json()["errors"][0]["field"] == field
        for value in (0, -1, "many"):
            response = client.post("/api/question-papers/generate", json={**base, "total_marks": value})
            assert response.status_code == 422
            assert response.json()["errors"][0]["field"] == "total_marks"


def test_priority_tiers_random_ties_exact_marks_and_source_record(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        bank_path = tmp_path / "subjects" / "OperatingSys" / "question_bank.json"
        bank = json.loads(bank_path.read_text())
        bank["questions"].extend([
            {"question_id": "q2-c", "marks": 2, "original_text": "Define a scheduler.",
             "original_question_number": "Q3(b)", "source_pdf": "extra.pdf", "source_path": "source/extra.pdf",
             "source_pages": [4, 5], "section_name": "Section B"},
            {"question_id": "q2-d", "marks": 2, "original_text": "Define a deadlock.",
             "original_question_number": "Q4", "source_pdf": "extra.pdf", "source_path": "source/extra.pdf",
             "page_number": 8, "section_name": "Section C"},
        ])
        _atomic_write(bank_path, bank)
        for question_id, priority in (("q2-a", 3), ("q2-b", 2), ("q2-c", 2)):
            assert client.put("/api/question-priorities", json={"question_id": question_id,
                "priority": priority, "subject_id": "OperatingSys"}).status_code == 200

        blueprint = {"blueprint_id": "exact-two", "name": "Exact two marks", "selection_rule": "random",
                     "sections": [{"name": "Section A", "question_count": 1, "marks_per_question": 2}]}
        assert client.post("/api/question-paper/blueprints", json=blueprint).status_code == 201
        request = {"title": "Priority paper", "subject": "OperatingSys", "exam_type": "Test",
                   "total_marks": 2, "duration": "20 minutes", "blueprint_id": "exact-two"}
        choices = [client.post("/api/question-papers/generate", json={**request, "seed": seed}).json()
                   for seed in range(12)]
        assert all(result["total_marks"] == 2 for result in choices)
        # A priority 3 record wins even though a template requests random;
        # when only the priority 2 tier is eligible, seeded runs vary among ties.
        assert all(result["sections"][0]["questions"][0]["question_id"] == "q2-a" for result in choices)

        # Remove the priority-3 question from eligibility by using a specific
        # marks/type set, then check random selection within the equal tier.
        for question_id in ("q2-a",):
            client.put("/api/question-priorities", json={"question_id": question_id,
                "priority": 1, "subject_id": "OperatingSys"})
        tied = [client.post("/api/question-papers/generate", json={**request, "seed": seed}).json()
                for seed in range(12)]
        selected = {result["sections"][0]["questions"][0]["question_id"] for result in tied}
        assert selected >= {"q2-b", "q2-c"}
        item = next(row for row in tied if row["sections"][0]["questions"][0]["question_id"] == "q2-c")
        question = item["sections"][0]["questions"][0]
        assert question["original_question_number"] == "Q3(b)"
        assert question["source_pdf"] == "extra.pdf"
        assert question["source_path"] == "source/extra.pdf"
        assert question["page_number"] == 4 and question["source_pages"] == [4, 5]
        assert question["section_name"] == "Section B"
        record = tmp_path / "subjects" / ".masterdeorca" / "generated_papers" / f"{item['paper_id']}.json"
        assert json.loads(record.read_text())["sections"][0]["questions"][0] == question
        assert item["processing_accuracy_notice"] == (
            "Processing Accuracy Notice\n\nReview extracted questions before relying on them. OCR and question "
            "segmentation can produce errors, especially for scanned or complex PDFs.\n\n"
            "Important Information: Some questions are extracted from PDF files using automated text recognition (OCR). "
            "This process may occasionally misread words, numbers, mathematical expressions, diagrams, or tables. "
            "Questions may also be split incorrectly or have formatting issues.\n\n"
            "Please check the questions carefully before using this paper for an examination. Make any necessary "
            "corrections to ensure that the questions, marks, and figures are accurate."
        )
        shortage_blueprint = {"blueprint_id": "shortage-two", "name": "Five two-mark questions",
                              "sections": [{"name": "Section A", "question_count": 5, "marks_per_question": 2}]}
        assert client.post("/api/question-paper/blueprints", json=shortage_blueprint).status_code == 201
        short = client.post("/api/question-papers/generate", json={**request, "blueprint_id": "shortage-two",
            "total_marks": 10, "seed": 3}).json()
        assert short["total_marks"] == 8
        assert short["shortages"][0]["requested_questions"] == 5
        assert short["shortages"][0]["available_questions"] == 4


def test_editable_drafts_use_bank_data_preserve_assets_and_export_docx(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        from PIL import Image

        bank_path = tmp_path / "subjects" / "OperatingSys" / "question_bank.json"
        bank = json.loads(bank_path.read_text())
        figure = tmp_path / "subjects" / "OperatingSys" / "output" / "Parser" / "figures" / "sample.png"
        figure.parent.mkdir(parents=True)
        Image.new("RGB", (32, 32), "white").save(figure)
        bank["questions"][0].update({"original_question_number": "Q1(a)", "source_path": "source/mid.pdf",
            "source_pages": [2], "section_name": "Section I", "assets": [
                {"asset_id": "fig-1", "asset_type": "figure", "path": str(figure)},
                {"asset_id": "unsafe", "asset_type": "figure", "path": "/etc/passwd"}]})
        _atomic_write(bank_path, bank)
        assert client.put("/api/question-priorities", json={"question_id": "q2-a", "priority": 3,
            "subject_id": "OperatingSys"}).status_code == 200

        request = {"paper_title": "Editable OS Draft", "subject": "OperatingSys", "exam_type": "Final",
                   "total_marks": 16, "duration": "2 Hours", "seed": 12,
                   "university_name": "Example University", "college_name": "Engineering College",
                   "department_name": "Computer Science", "academic_year": "2026-27", "semester": "V",
                   "subject_name": "Operating Systems", "subject_code": "CS239", "examination_date": "2026-11-02"}
        response = client.post("/api/question-papers/drafts", json=request)
        assert response.status_code == 201, response.text
        draft = response.json()
        assert draft["status"] == "draft"
        assert draft["subject"] == "OperatingSys"
        questions = [q for section in draft["sections"] for q in section["questions"]]
        assert len(questions) == 3 and sum(q["marks"] for q in questions) == 16
        assert questions[0]["question_id"] == "q2-a" and questions[0]["priority"] == 3
        assert questions[0]["source_pdf"] == "mid.pdf"
        assert questions[0]["source_path"] == "source/mid.pdf"
        assert questions[0]["original_question_number"] == "Q1(a)"
        assert questions[0]["source_pages"] == [2]
        assert questions[0]["assets"][0]["asset_id"] == "fig-1"
        assert draft["university_name"] == "Example University"
        assert draft["academic_year"] == "2026-27" and draft["subject_code"] == "CS239"
        draft_id = draft["draft_id"]

        loaded = client.get(f"/api/question-papers/drafts/{draft_id}")
        assert loaded.status_code == 200
        first_id = questions[0]["question_id"]
        edited = client.patch(f"/api/question-papers/drafts/{draft_id}/questions/{first_id}",
                              json={"text": "Draft-only wording"})
        assert edited.status_code == 200
        assert json.loads(bank_path.read_text())["questions"][0]["original_text"] == "Define a process."

        removed_id = questions[-1]["question_id"]
        assert client.delete(f"/api/question-papers/drafts/{draft_id}/questions/{removed_id}").status_code == 200
        assert client.post(f"/api/question-papers/drafts/{draft_id}/questions", json={
            "question_id": removed_id, "section": "Questions"}).status_code == 200
        after_add = client.get(f"/api/question-papers/drafts/{draft_id}").json()
        ordered_ids = [q["question_id"] for section in after_add["sections"] for q in section["questions"]]
        reordered = client.put(f"/api/question-papers/drafts/{draft_id}/question-order",
                               json={"question_ids": list(reversed(ordered_ids))})
        assert reordered.status_code == 200
        assert [q["question_id"] for q in reordered.json()["sections"][0]["questions"]] == list(reversed(ordered_ids))
        assert client.put(f"/api/question-papers/drafts/{draft_id}/question-order",
                          json={"question_ids": ordered_ids}).status_code == 200
        renamed = client.patch(f"/api/question-papers/drafts/{draft_id}", json={
            "title": "Reviewed OS Draft", "university_name": "Updated University",
            "include_processing_accuracy_notice": True,
            "sections": [{"name": "Main Section", "marks_per_question": None}]})
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Reviewed OS Draft"
        assert renamed.json()["university_name"] == "Updated University"
        assert renamed.json()["sections"][0]["name"] == "Main Section"

        exported = client.post(f"/api/question-papers/drafts/{draft_id}/docx",
                               json={"include_processing_accuracy_notice": True})
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        docx = tmp_path / "editable-draft.docx"
        docx.write_bytes(exported.content)
        with zipfile.ZipFile(docx) as package:
            assert "word/document.xml" in package.namelist()
            assert not any(name.casefold().endswith(".pdf") for name in package.namelist())
        document = Document(docx)
        assert "Reviewed OS Draft" in "\n".join(p.text for p in document.paragraphs)
        assert "Draft-only wording" in "\n".join(p.text for p in document.paragraphs)
        doc_text = "\n".join(p.text for p in document.paragraphs)
        assert "Updated University" in doc_text and "CS239" in doc_text
        assert "Processing Accuracy Notice" in doc_text
        assert "References / Sources" in doc_text
        assert "Source PDF: mid.pdf" in doc_text and "Page(s): 2" in doc_text
        assert len(document.inline_shapes) == 1

        # The notice is mandatory for every DOCX and appears exactly once, even
        # when an export explicitly requests it off (the option is ignored).
        without_notice = client.post(f"/api/question-papers/drafts/{draft_id}/docx",
                                     json={"include_processing_accuracy_notice": False})
        assert without_notice.status_code == 200
        no_notice_doc = tmp_path / "without-notice.docx"
        no_notice_doc.write_bytes(without_notice.content)
        doc_text = "\n".join(p.text for p in Document(no_notice_doc).paragraphs)
        assert doc_text.count("Processing Accuracy Notice") == 1

        _atomic_write(tmp_path / "subjects" / "OtherSubject" / "question_bank.json", {
            "subject": "OtherSubject", "questions": [{"question_id": "other-q", "marks": 2, "original_text": "Wrong subject."}]})
        assert client.post(f"/api/question-papers/drafts/{draft_id}/questions", json={
            "question_id": "other-q", "section": "Main Section"}).status_code == 404


def test_editable_draft_shortage_and_export_total_validation(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        shortage = client.post("/api/question-papers/drafts", json={"title": "Too much", "subject": "OperatingSys",
            "exam_type": "Final", "total_marks": 100, "duration": "3 Hours"})
        assert shortage.status_code == 422
        assert "Insufficient eligible questions" in shortage.json()["detail"]

        draft = client.post("/api/question-papers/drafts", json={"title": "Incomplete draft", "subject": "OperatingSys",
            "exam_type": "Quiz", "total_marks": 2, "duration": "30 Minutes"}).json()
        qid = draft["sections"][0]["questions"][0]["question_id"]
        assert client.delete(f"/api/question-papers/drafts/{draft['draft_id']}/questions/{qid}").status_code == 200
        assert client.post(f"/api/question-papers/drafts/{draft['draft_id']}/docx").status_code == 422
        client.headers.pop("Authorization")
        assert client.get(f"/api/question-papers/drafts/{draft['draft_id']}").status_code == 401
