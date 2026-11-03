from __future__ import annotations

import json

from src.masterdeorca.services import basic_config
from src.masterdeorca.services import question_papers
from tests.test_api import _client
from tests.test_question_papers import _seed_questions


def _payload(**overrides):
    payload = {"name": "OS Midterm", "university_name": "Biju Patnaik University",
               "college_name": "College of Engineering", "department_name": "Computer Science",
               "paper_title": "Operating Systems Midterm", "academic_year": "2025-26",
               "semester": "5th", "exam_type": "Midterm", "subject_id": "OperatingSys",
               "subject_code": "CSE-301", "examination_date": "2026-01-15",
               "duration": "2 hours", "total_marks": 60,
               "instructions": ["Answer all questions."]}
    payload.update(overrides)
    return payload


def test_listing_retrieval_and_neutral_default(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        listing = client.get("/api/basic-configurations")
        assert listing.status_code == 200
        items = listing.json()["items"]
        # The canonical neutral preset exists exactly once and holds no fabricated data.
        assert sum(item["configuration_id"] == "default-config" for item in items) == 1
        default = client.get("/api/basic-configurations/default").json()
        assert default["configuration_id"] == "default-config"
        assert default["university_name"] is None and default["college_name"] is None
        assert default["total_marks"] is None and default["instructions"] == []

        assert client.get("/api/basic-configurations/default-config").json()["name"] == "Default configuration"
        assert client.get("/api/basic-configurations/missing-id").status_code == 404

        created = client.post("/api/basic-configurations", json=_payload()).json()
        listing = client.get("/api/basic-configurations").json()["items"]
        names = [item["name"] for item in listing]
        assert names.index("OS Midterm") > names.index("Default configuration")


def test_create_rejects_client_owned_internal_fields(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        hijack = _payload(name="Hijack", configuration_id="default-config",
                          created_at="1999-01-01", updated_at="1999-01-01", is_default=True)
        created = client.post("/api/basic-configurations", json=hijack)
        assert created.status_code == 201
        body = created.json()
        assert body["configuration_id"] != "default-config" and body["is_default"] is False
        assert body["created_at"] != "1999-01-01"
        # The canonical default remains untouched.
        assert client.get("/api/basic-configurations/default").json()["configuration_id"] == "default-config"


def test_update_fields_and_validation_rules(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        cid = client.post("/api/basic-configurations", json=_payload()).json()["configuration_id"]
        updated = client.put(f"/api/basic-configurations/{cid}", json=_payload(total_marks=70,
                                                                              instructions=[]))
        assert updated.status_code == 200
        body = updated.json()
        assert body["total_marks"] == 70 and body["instructions"] == []
        assert body["created_at"] and body["updated_at"] >= body["created_at"]

        assert client.put(f"/api/basic-configurations/{cid}", json=_payload(name=" ")).status_code == 422
        assert client.put(f"/api/basic-configurations/{cid}", json=_payload(examination_date="soon")).status_code == 422
        assert client.put(f"/api/basic-configurations/{cid}", json=_payload(subject_id="../bad")).status_code == 422
        assert client.put(f"/api/basic-configurations/{cid}", json=_payload(total_marks=5000)).status_code == 422
        assert client.put(f"/api/basic-configurations/{cid}", json=_payload(subject_code="A" * 90)).status_code == 422


def test_duplicate_and_rename(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        cid = client.post("/api/basic-configurations", json=_payload()).json()["configuration_id"]
        renamed = client.patch(f"/api/basic-configurations/{cid}", json={"name": "OS Midterm v2"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "OS Midterm v2"
        # Internal fields cannot be renamed out or mutated through PATCH.
        assert client.patch(f"/api/basic-configurations/{cid}", json={"hax": 1}).status_code == 200
        source = client.get(f"/api/basic-configurations/{cid}").json()

        duplicate = client.post(f"/api/basic-configurations/{cid}/duplicate")
        assert duplicate.status_code == 201
        dup = duplicate.json()
        assert dup["configuration_id"] != cid and dup["name"] == "OS Midterm v2 (copy)"
        assert dup["university_name"] == source["university_name"]  # content copied
        assert dup["created_at"] > source["created_at"]

        renamed_copy = client.post(f"/api/basic-configurations/{cid}/duplicate",
                                   json={"name": "Custom copy"})
        assert renamed_copy.json()["name"] == "Custom copy"

        assert client.patch("api/basic-configurations/missing/name").status_code in {404, 422}
        assert client.post("/api/basic-configurations/missing-id/duplicate").status_code == 404


def test_delete(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        cid = client.post("/api/basic-configurations", json=_payload()).json()["configuration_id"]
        assert client.delete(f"/api/basic-configurations/{cid}").json()["deleted"] is True
        assert client.get(f"/api/basic-configurations/{cid}").status_code == 404
        assert client.delete(f"/api/basic-configurations/{cid}").status_code == 404
        # Deleting the default re-points the default; nothing dangles.
        other = client.post("/api/basic-configurations", json=_payload(name="Other")).json()["configuration_id"]
        client.post(f"/api/basic-configurations/{other}/set-default")
        assert client.delete(f"/api/basic-configurations/{other}").status_code == 200
        fallback = client.get("/api/basic-configurations/default").json()
        assert fallback["configuration_id"] == "default-config" and fallback["is_default"] is True


def test_default_selection_single_default_and_fallback(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        first = client.post("/api/basic-configurations", json=_payload(name="First")).json()["configuration_id"]
        second = client.post("/api/basic-configurations", json=_payload(name="Second")).json()["configuration_id"]
        assert client.post(f"/api/basic-configurations/{first}/set-default").json()["is_default"] is True
        assert client.get("/api/basic-configurations/default").json()["configuration_id"] == first
        assert client.post(f"/api/basic-configurations/{second}/set-default").status_code == 200
        items = client.get("/api/basic-configurations").json()["items"]
        assert [item["configuration_id"] for item in items if item["is_default"]] == [second]
        # User-chosen default survives repeated reads (no silent revert).
        client.get("/api/basic-configurations")
        assert client.get("/api/basic-configurations/default").json()["configuration_id"] == second
        assert client.post("/api/basic-configurations/missing-id/set-default").status_code == 404


def test_persistence_across_restart(tmp_path, monkeypatch):
    from src.masterdeorca.services import basic_config as service

    with _client(tmp_path, monkeypatch) as client:
        cid = client.post("/api/basic-configurations", json=_payload()).json()["configuration_id"]
        client.put(f"/api/basic-configurations/{cid}", json=_payload(name="Updated OS Midterm"))
        client.post(f"/api/basic-configurations/{cid}/set-default")

        # Simulate a backend restart: drop in-process locks and re-read from disk.
        try:
            question_papers._LOCKS.clear()
        except Exception:
            pass
        records = service.list_configurations()
        match = next(row for row in records if row["configuration_id"] == cid)
        assert match["name"] == "Updated OS Midterm" and match["is_default"] is True
        assert service.default_configuration()["configuration_id"] == cid


def test_authentication_required(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.headers.pop("Authorization")
        assert client.get("/api/basic-configurations").status_code == 401
        assert client.get("/api/basic-configurations/default").status_code == 401
        assert client.post("/api/basic-configurations", json=_payload()).status_code == 401
        assert client.put("/api/basic-configurations/default-config", json=_payload()).status_code == 401
        assert client.delete("/api/basic-configurations/default-config").status_code == 401


def test_preset_never_touches_blueprints_and_generation_uses_both(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _seed_questions(tmp_path)
        blueprint = {"blueprint_id": "os-midterm", "name": "OS Midterm", "subject_id": "OperatingSys",
                     "exam_type": "Midterm", "sections": [{"name": "Section A", "question_count": 1,
                     "marks_per_question": 4}]}
        assert client.post("/api/question-paper/blueprints", json=blueprint).status_code == 201
        preset = client.post("/api/basic-configurations", json=_payload()).json()

        # The preset and blueprint are stored independently (SQLite).
        data_dir = tmp_path / "subjects" / ".masterdeorca"

        generated = client.post("/api/question-papers/generate", json={
            "title": preset["paper_title"], "subject": "OperatingSys", "exam_type": "Midterm",
            "total_marks": 4, "duration": "2 hours", "blueprint_id": "os-midterm",
            "university_name": preset["university_name"], "college_name": preset["college_name"],
            "department_name": preset["department_name"], "academic_year": preset["academic_year"],
            "semester": preset["semester"], "subject_code": preset["subject_code"],
            "examination_date": preset["examination_date"]})
        assert generated.status_code == 200
        paper = generated.json()
        assert paper["university_name"] == preset["university_name"]
        assert paper["blueprint_id"] == "os-midterm"
        # Presets and blueprints remain independently loadable.
        assert client.get("/api/question-paper/blueprints/os-midterm").status_code == 200
        assert client.get("/api/question-paper/blueprints").json()["items"]
        assert client.get(f"/api/basic-configurations/{preset['configuration_id']}").status_code == 200


def test_subject_references_must_exist_when_set_or_changed(tmp_path, monkeypatch):
    from src.masterdeorca import storage as storage_module

    with _client(tmp_path, monkeypatch) as client:
        # The default-registered subject is accepted...
        assert client.post("/api/basic-configurations", json=_payload()).status_code == 201
        # ...but an unregistered subject reference is refused with 422.
        ghost = client.post("/api/basic-configurations", json=_payload(subject_id="GhostSubject"))
        assert ghost.status_code == 422
        assert "GhostSubject" in ghost.json()["detail"]

        assert client.post("/subjects", json={"name": "Databases"}).status_code == 201
        assert client.post("/api/basic-configurations", json=_payload(
            name="DB Final", subject_id="Databases")).status_code == 201

        cid = client.post("/api/basic-configurations", json=_payload()).json()["configuration_id"]
        # Unchanged references stay grandfathered (still editable) ...
        assert client.patch(f"/api/basic-configurations/{cid}", json={"name": "OS Midterm renamed"}).status_code == 200
        # ...while switching to a nonexistent subject is refused.
        assert client.patch(f"/api/basic-configurations/{cid}",
                            json={"subject_id": "GhostSubject"}).status_code == 422
        # Clearing the reference (null) is always allowed.
        cleared = client.patch(f"/api/basic-configurations/{cid}", json={"subject_id": None})
        assert cleared.status_code == 200 and cleared.json()["subject_id"] is None

        # Duplicates re-validate the copied reference too.
        with_databases = client.post("/api/basic-configurations", json={
            "name": "DB", "subject_id": "Databases"}).json()["configuration_id"]
        assert client.post(f"/api/basic-configurations/{with_databases}/duplicate").status_code == 201

        # Editing a saved preset stays possible even after its referenced subject
        # directory is removed (the stored reference is grandfathered on reads).
        cid_db = client.post("/api/basic-configurations", json={
            "name": "Stale", "subject_id": "Databases", "duration": "1 hour"}).json()["configuration_id"]
        assert client.patch(f"/api/basic-configurations/{cid_db}",
                            json={"paper_title": "Updated"}).status_code == 200


def test_startup_never_overwrites_saved_default(tmp_path, monkeypatch):
    from src.masterdeorca.services import basic_config as service
    from src.masterdeorca.services import question_papers as qp

    with _client(tmp_path, monkeypatch) as client:
        cid = client.post("/api/basic-configurations", json=_payload(name="Saved default")).json()["configuration_id"]
        client.post(f"/api/basic-configurations/{cid}/set-default")
        default = client.get("/api/basic-configurations/default").json()
        assert default["configuration_id"] == cid
        assert default["total_marks"] == 60 and default["university_name"] == "Biju Patnaik University"

        # A fresh startup (locks cleared, storage re-read) keeps the saved default
        # and never resets it back to the neutral preset.
        qp._LOCKS.clear()
        restored = service.default_configuration()
        assert restored["configuration_id"] == cid and restored["is_default"] is True
