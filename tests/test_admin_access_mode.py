"""Administrator access-mode (readonly / edit) enforcement tests.

The mode lives in the admins table, is selected by the authenticated
administrator via PUT /auth/access-mode, and is re-read from the database on
every request, so read-only users cannot bypass write restrictions with their
existing bearer token. Teacher accounts are unaffected.
"""

from __future__ import annotations

from tests.test_api import _client

READONLY_DETAIL = "Administrator account is in read-only mode; editing is disabled"


def _set_mode(client, mode: str):
    response = client.put("/auth/access-mode", json={"access_mode": mode})
    assert response.status_code == 200
    assert response.json() == {"role": "admin", "access_mode": mode}
    return response


def _readonly_admin(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        yield client


def test_default_mode_is_edit_and_reported_by_auth_endpoints(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/auth/access-mode").json() == {"role": "admin", "access_mode": "edit"}
        me = client.get("/auth/me").json()
        assert me["role"] == "admin" and me["access_mode"] == "edit"
        _set_mode(client, "readonly")
        assert client.get("/auth/access-mode").json()["access_mode"] == "readonly"
        # A fresh token after re-login keeps the persisted mode; mode is account state.
        login = client.post("/auth/login", json={"email": "admin@example.com", "password": "admin@123"})
        client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
        assert client.get("/auth/access-mode").json()["access_mode"] == "readonly"


def test_mode_value_is_validated(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.put("/auth/access-mode", json={"access_mode": "superuser"}).status_code == 422
        assert client.put("/auth/access-mode", json={} ).status_code == 422
        assert client.get("/auth/access-mode").json()["access_mode"] == "edit"


def test_readonly_admin_can_read_everything_primary_views(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.post("/subjects", json={"name": "DataStructures"})
        client.post("/api/question-paper/blueprints", json={"name": "BP"})
        _set_mode(client, "readonly")
        for request in (
            client.get("/subjects"),
            client.get("/api/dashboard/metrics"),
            client.get("/teachers"),
            client.get("/api/question-bank", params={"subject_id": "OperatingSys"}),
            client.get("/api/question-paper/blueprints"),
            client.get("/api/basic-configurations"),
            client.get("/papers"),
            client.get("/auth/totp/status"),
        ):
            assert request.status_code == 200


def test_readonly_blocks_administrative_writes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.post("/teachers", json={"name": "T", "email": "t@example.com", "password": "teaching1"})
        client.post("/subjects", json={"name": "ExtraSubject"})
        _set_mode(client, "readonly")
        blocked = [
            client.post("/teachers", json={"name": "T2", "email": "t2@example.com", "password": "teaching1"}),
            client.patch("/teachers/1", json={"name": "Renamed"}),
            client.delete("/teachers/1"),
            client.post("/subjects", json={"name": "MoreSubjects"}),
            client.patch("/subjects/ExtraSubject", json={"name": "ExtraSubjectRenamed"}),
            client.delete("/subjects/ExtraSubject"),
            client.post("/questions", json={"subject": "OperatingSys", "text": "Q", "marks": 2,
                                            "difficulty": "easy", "type": "short"}),
            client.patch("/questions/1", json={"marks": 3}),
            client.delete("/questions/1"),
            client.post("/subjects/OperatingSys/topics", json={"name": "Topic One"}),
            client.patch("/topics/1", json={"name": "Renamed Topic"}),
            client.delete("/topics/1"),
            client.put("/api/question-priorities", json={"question_id": "q-1", "priority": 2}),
            client.delete("/api/question-priorities/q-1"),
            client.post("/api/question-paper/blueprints", json={"name": "BP"}),
            client.put("/api/question-paper/blueprints/bp-1", json={"name": "BP"}),
            client.patch("/api/question-paper/blueprints/bp-1", json={"name": "BP"}),
            client.post("/api/question-paper/blueprints/bp-1/duplicate"),
            client.delete("/api/question-paper/blueprints/bp-1"),
            client.post("/api/basic-configurations", json={"name": "Preset"}),
            client.put("/api/basic-configurations/preset-1", json={"name": "Preset"}),
            client.patch("/api/basic-configurations/preset-1", json={"name": "Preset"}),
            client.post("/api/basic-configurations/preset-1/duplicate"),
            client.post("/api/basic-configurations/preset-1/set-default"),
            client.delete("/api/basic-configurations/preset-1"),
            client.patch("/subjects/OperatingSys/pdfs/paper.pdf", json={"review_status": "approved"}),
            client.delete("/subjects/OperatingSys/pdfs/paper.pdf"),
        ]
        for response in blocked:
            assert response.status_code == 403, response.request.url
            assert response.json()["detail"] == READONLY_DETAIL


def test_readonly_blocks_user_scoped_writes_but_reads_survive(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _set_mode(client, "readonly")
        blocked = [
            client.post("/api/question-bank", json={"subject_id": "OperatingSys", "text": "Q", "marks": 2}),
            client.patch("/api/question-bank/any", json={"marks": 3}),
            client.post("/api/question-papers/generate", json={}),
            client.post("/api/question-papers/drafts", json={}),
            client.post("/api/question-papers/drafts/draft-1/questions", json={}),
            client.patch("/api/question-papers/drafts/draft-1", json={}),
            client.put("/api/question-papers/drafts/draft-1/question-order", json={}),
            client.delete("/api/question-papers/drafts/draft-1/questions/q-1"),
        ]
        for response in blocked:
            assert response.status_code == 403, response.request.url

        # Reads and paper export/download stay available in readonly mode.
        assert client.get("/api/question-bank", params={"subject_id": "OperatingSys"}).status_code == 200
        assert client.post("/api/question-papers/drafts/missing/docx").status_code == 404  # reachable handler


def test_switching_back_to_edit_restores_writes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _set_mode(client, "readonly")
        assert client.post("/subjects", json={"name": "BlockedSubject"}).status_code == 403
        _set_mode(client, "edit")
        assert client.post("/subjects", json={"name": "DataStructures"}).status_code == 201
        assert sorted(client.get("/subjects").json()["subjects"]) == ["DataStructures", "OperatingSys"]


def test_teacher_accounts_keep_full_question_bank_access(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        created = client.post("/teachers", json={"name": "Teacher", "email": "teacher@example.com",
                                                 "department": "CSE", "password": "teaching1",
                                                 "status": "approved"})
        assert created.status_code == 201
        _set_mode(client, "readonly")  # The admin went read-only; teachers are unaffected.
        login = client.post("/auth/login", json={"email": "teacher@example.com", "password": "teaching1"})
        client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
        assert client.post("/api/question-bank",
                           json={"subject_id": "OperatingSys", "text": "Teacher question",
                                 "marks": 2}).status_code == 201
        # Access-mode management is administrator-only.
        assert client.get("/auth/access-mode").status_code == 403
        assert client.put("/auth/access-mode", json={"access_mode": "edit"}).status_code == 403
        me = client.get("/auth/me").json()
        assert me["role"] == "teacher" and "access_mode" not in me


def test_mode_persists_for_a_new_session(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _set_mode(client, "readonly")
    with _client(tmp_path, monkeypatch) as second_client:
        assert second_client.get("/auth/access-mode").json()["access_mode"] == "readonly"
        assert second_client.post("/subjects", json={"name": "Nope"}).status_code == 403
        second_client.put("/auth/access-mode", json={"access_mode": "edit"})
        assert second_client.post("/subjects", json={"name": "Yes"}).status_code == 201
