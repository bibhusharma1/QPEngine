from __future__ import annotations

import pytest

from src.masterdeorca import database
from src.masterdeorca.services import dashboard_metrics
from src.masterdeorca.services.question_bank import _atomic_write
from tests.test_api import _client


def _seed_bank(subject: str, tmp_path, questions: list[dict]) -> None:
    for required in ("source", "metadata"):
        (tmp_path / "subjects" / subject / required).mkdir(parents=True, exist_ok=True)
    bank = tmp_path / "subjects" / subject / "question_bank.json"
    _atomic_write(bank, {"subject": subject, "updated_at": "now", "questions": questions})


def _question(question_id: str, text: str = "What is a process?") -> dict:
    return {"question_id": question_id, "marks": 4, "original_text": text}


@pytest.fixture
def api(tmp_path, monkeypatch):
    dashboard_metrics.invalidate()
    with _client(tmp_path, monkeypatch) as client:
        yield client, tmp_path
    dashboard_metrics.invalidate()


def test_teachers_subjects_and_questions_are_accurate(api):
    client, tmp_path = api
    assert client.post("/teachers", json={"name": "Teacher One", "email": "t1@example.com",
                                          "department": "CSE", "password": "teaching1"}).status_code == 201
    assert client.post("/teachers", json={"name": "Teacher Two", "email": "t2@example.com",
                                          "department": "IT", "status": "pending"}).status_code == 201
    assert client.post("/subjects", json={"name": "DataStructures"}).status_code == 201
    _seed_bank("OperatingSys", tmp_path, [_question("a"), _question("b2")])
    _seed_bank("DataStructures", tmp_path, [_question("c")])

    metrics = client.get("/api/dashboard/metrics").json()

    with database.connect() as db:
        expected_teachers = db.execute("SELECT count(*) FROM users WHERE role='teacher'").fetchone()[0]
    assert metrics["total_teachers"] == expected_teachers == 2
    assert metrics["total_subjects"] == len(client.get("/subjects").json()["subjects"])
    assert metrics["total_questions"] == 3
    assert metrics["availability"] == {"total_teachers": "ok", "total_subjects": "ok",
                                       "total_questions": "ok"}
    assert metrics["unavailable_subjects"] == []


def test_administrators_are_not_counted_as_teachers(api):
    client, _ = api
    with database.connect() as db:
        admins = db.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0]
    assert admins >= 1  # The seeded administrator is an admin, not a teacher.
    metrics = client.get("/api/dashboard/metrics").json()
    assert metrics["total_teachers"] == 0


def test_duplicate_question_ids_are_counted_once(api):
    client, tmp_path = api
    _seed_bank("OperatingSys", tmp_path, [
        _question("dup"), _question("unique"), _question("dup", "Different artifact reference")])
    metrics = client.get("/api/dashboard/metrics").json()
    assert metrics["total_questions"] == 2


def test_pagination_of_the_question_bank_api_does_not_change_the_total(api):
    client, tmp_path = api
    _seed_bank("OperatingSys", tmp_path, [_question(f"q-{index}") for index in range(60)])
    first_page = client.get("/api/question-bank", params={"subject_id": "OperatingSys", "page": 1,
                                                          "page_size": 50}).json()
    assert first_page["pagination"]["total_pages"] >= 2  # The bank API paginates.
    metrics = client.get("/api/dashboard/metrics").json()
    assert metrics["total_questions"] == 60  # Metrics read the banks directly.


def test_corrupt_and_missing_banks_are_reported_safely(api):
    client, tmp_path = api
    _seed_bank("OperatingSys", tmp_path, [_question("a")])
    for required in ("source", "metadata"):
        (tmp_path / "subjects" / "BrokenSubject" / required).mkdir(parents=True, exist_ok=True)
    (tmp_path / "subjects" / "BrokenSubject" / "question_bank.json").write_text("{corrupted")
    client.post("/subjects", json={"name": "EmptySubject"})

    metrics = client.get("/api/dashboard/metrics").json()
    assert metrics["total_questions"] == 1  # Available banks are still verified.
    assert metrics["availability"]["total_questions"] == "partial"
    assert metrics["unavailable_subjects"] == ["BrokenSubject"]

    # A subject with no bank file is empty, not unavailable.
    (tmp_path / "subjects" / "BrokenSubject" / "question_bank.json").rename(
        tmp_path / "subjects" / "BrokenSubject" / "question_bank.corrupt.json")
    dashboard_metrics.invalidate()
    refreshed = client.get("/api/dashboard/metrics").json()
    assert refreshed["total_questions"] == 1
    assert refreshed["availability"]["total_questions"] == "ok"
    assert refreshed["unavailable_subjects"] == []


def test_authentication_and_admin_authorization(api, tmp_path, monkeypatch):
    client, _ = api
    assert client.get("/api/dashboard/metrics").status_code == 200
    # A teacher account passes require_user but fails require_admin.
    client.post("/teachers", json={"name": "Teacher", "email": "teacher@example.com",
                                   "department": "CSE", "password": "teaching1",
                                   "status": "approved"})
    login = client.post("/auth/login", json={"email": "teacher@example.com", "password": "teaching1"})
    client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
    assert client.get("/api/dashboard/metrics").status_code == 403
    client.headers.update({"Authorization": ""})
    assert client.get("/api/dashboard/metrics").status_code == 401


def test_metrics_refresh_after_data_changes(api):
    client, tmp_path = api
    first = client.get("/api/dashboard/metrics").json()  # caches the question summary
    assert first["total_subjects"] == 1
    client.post("/subjects", json={"name": "DataStructures"})  # invalidates the cache
    _seed_bank("DataStructures", tmp_path, [_question("x"), _question("y")])
    refreshed = client.get("/api/dashboard/metrics").json()
    assert refreshed["total_subjects"] == 2
    assert refreshed["total_questions"] == 2

    cached = client.get("/api/dashboard/metrics").json()
    assert cached["cached"] is True and cached["total_questions"] == 2

    client.post("/api/question-bank", json={"subject_id": "DataStructures",
                                            "text": "New manual question", "marks": 5,
                                            "difficulty": "easy", "type": "short"})
    updated = client.get("/api/dashboard/metrics").json()
    assert updated["cached"] is False
    assert updated["total_questions"] == 3  # Manual records are bank records too.

    client.delete("/subjects/DataStructures")
    after_delete = client.get("/api/dashboard/metrics").json()
    assert after_delete["total_subjects"] == 1 and after_delete["total_questions"] == 0


def test_dont_show_paths_or_secrets(api):
    client, _ = api
    body = client.get("/api/dashboard/metrics").json()
    flattened = repr(body).lower()
    assert "subjects/" not in flattened and "sqlite" not in flattened and "password" not in flattened
