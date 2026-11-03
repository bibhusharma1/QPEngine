from __future__ import annotations

import json
import hashlib
import sqlite3
import builtins
import sys

import pytest

from scripts import backup_data, migrate_data, restore_backup
from scripts.data_tools import copy_tree, validate_tree
from scripts.import_legacy_data import import_tree
from src.masterdeorca import database
from src.masterdeorca import migrations
from src.masterdeorca import storage
from src.masterdeorca.services import question_bank, question_priorities


def test_copy_and_validate_preserves_source_files_and_sqlite_rows(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"%PDF-1.7 data")
    (source / "bank.json").write_text('{"questions":[{"id":"stable"}]}', encoding="utf-8")
    with sqlite3.connect(source / "app.sqlite") as db:
        db.execute("CREATE TABLE sample(id TEXT PRIMARY KEY, payload TEXT)")
        db.execute("INSERT INTO sample VALUES(?,?)", ("stable-id", "original"))

    copied, identical = copy_tree(source, destination)
    assert copied == 3 and identical == 0
    report = validate_tree(source, destination)
    assert report["files"] == 3
    assert (source / "paper.pdf").read_bytes() == b"%PDF-1.7 data"
    with sqlite3.connect(destination / "app.sqlite") as db:
        assert db.execute("SELECT id,payload FROM sample").fetchone() == (
            "stable-id",
            "original",
        )
        db.execute("CREATE TABLE migration_extra(value TEXT)")
        db.execute("INSERT INTO migration_extra VALUES('keep')")

    assert copy_tree(source, destination) == (0, 3)
    with sqlite3.connect(destination / "app.sqlite") as db:
        assert db.execute("SELECT value FROM migration_extra").fetchone() == ("keep",)


def test_copy_refuses_conflicting_destination_without_overwriting(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "state.json").write_text('{"source":true}', encoding="utf-8")
    (destination / "state.json").write_text('{"source":false}', encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        copy_tree(source, destination)
    assert json.loads((destination / "state.json").read_text()) == {"source": False}


def test_backup_and_restore_use_verified_atomic_database_copies(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE sample(id TEXT PRIMARY KEY, payload BLOB)")
        db.execute("INSERT INTO sample VALUES(?, ?)", ("stable", b"original"))

    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(
        sys,
        "argv",
        ["backup_data", "--database", str(source), "--destination", str(backup_dir)],
    )
    backup_data.main()
    backup = backup_dir / "database" / source.name

    destination = tmp_path / "restored.sqlite"
    with sqlite3.connect(destination) as db:
        db.execute("CREATE TABLE existing(value TEXT)")
        db.execute("INSERT INTO existing VALUES('keep until replace')")
    monkeypatch.setattr(
        sys,
        "argv",
        ["restore_backup", str(backup), "--database", str(destination), "--replace"],
    )
    restore_backup.main()

    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT id,payload FROM sample").fetchone() == (
            "stable",
            b"original",
        )
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT id,payload FROM sample").fetchone() == (
            "stable",
            b"original",
        )


def test_existing_sqlite_database_migration_is_idempotent(tmp_path, monkeypatch):
    source = tmp_path / "legacy.sqlite"
    destination = tmp_path / "new" / "masterdeorca.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE legacy_record(id TEXT PRIMARY KEY, payload TEXT)")
        db.execute("INSERT INTO legacy_record VALUES('stable-id', 'preserve me')")

    monkeypatch.setattr(database, "DATABASE_PATH", destination)
    migrate_data._prepare_database(source, destination)
    migrate_data._prepare_database(source, destination)

    with sqlite3.connect(destination) as db:
        assert db.execute(
            "SELECT id,payload FROM legacy_record"
        ).fetchone() == ("stable-id", "preserve me")
        markers = db.execute(
            "SELECT COUNT(*) FROM app_settings WHERE setting_key LIKE 'legacy_sqlite_import:%'"
        ).fetchone()[0]
        assert markers == 1
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT COUNT(*) FROM legacy_record").fetchone()[0] == 1


def test_import_legacy_question_bank_is_idempotent_and_preserves_ids(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "masterdeorca.sqlite"
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    database.initialize()
    root = tmp_path / "data"
    subject = root / "Physics"
    (subject / "source").mkdir(parents=True)
    (subject / "source" / "paper.pdf").write_bytes(b"%PDF-1.7")
    bank = {
        "subject": "Physics",
        "questions": [
            {
                "question_id": "existing-stable-id",
                "source_pdf": "paper.pdf",
                "original_question_number": "Q4",
                "original_text": "Calculate velocity.",
                "cleaned_text": "Calculate velocity.",
                "marks": 5,
                "sub_questions": [{"sub_id": "child-id", "cleaned_text": "Part"}],
            }
        ],
    }
    (subject / "question_bank.json").write_text(
        json.dumps(bank), encoding="utf-8"
    )

    first = import_tree(root)
    second = import_tree(root)
    assert first["questions"] == 1
    assert second["questions"] == 0
    with database.connect() as db:
        row = db.execute(
            "SELECT question_id,subject_name,source_pdf,record_json "
            "FROM question_bank_records WHERE question_id=?",
            ("existing-stable-id",),
        ).fetchone()
        assert row["subject_name"] == "Physics"
        assert row["source_pdf"] == "paper.pdf"
        assert json.loads(row["record_json"])["sub_questions"][0]["sub_id"] == "child-id"
        assert db.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        document = db.execute(
            "SELECT document_id,subject_id,storage_key,content_blob,checksum_sha256 FROM documents WHERE filename=?",
            ("paper.pdf",),
        ).fetchone()
        assert document["subject_id"] == database.get_subject_id("Physics")
        assert document["storage_key"] == "Physics/source/paper.pdf"
        assert bytes(document["content_blob"]) == b"%PDF-1.7"
        assert document["checksum_sha256"] == hashlib.sha256(b"%PDF-1.7").hexdigest()


def test_document_migration_backfills_only_registered_subjects_and_preserves_ids():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE subjects(subject_id TEXT PRIMARY KEY,subject_name TEXT UNIQUE)"
    )
    db.execute(
        """CREATE TABLE documents(
            document_id TEXT PRIMARY KEY,subject_name TEXT,filename TEXT,storage_key TEXT
        )"""
    )
    db.executemany(
        "INSERT INTO subjects(subject_id,subject_name) VALUES(?,?)",
        [("subject-id-1", "Physics")],
    )
    db.executemany(
        "INSERT INTO documents(document_id,subject_name,filename,storage_key) VALUES(?,?,?,?)",
        [
            ("doc-id-1", "Physics", "paper.pdf", "Physics/source/paper.pdf"),
            ("doc-id-orphan", "Unregistered", "orphan.pdf", "orphan.pdf"),
        ],
    )

    migrations._migrate_document_subject_ids(db)

    rows = {
        row["document_id"]: row
        for row in db.execute(
            "SELECT document_id,subject_id,storage_key FROM documents"
        )
    }
    assert rows["doc-id-1"]["subject_id"] == "subject-id-1"
    assert rows["doc-id-1"]["storage_key"] == "Physics/source/paper.pdf"
    assert rows["doc-id-orphan"]["subject_id"] is None
    assert db.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 1


def test_turso_backend_requires_config_and_never_falls_back(monkeypatch):
    monkeypatch.setattr(database.settings, "DATABASE_BACKEND", "turso")
    with pytest.raises(RuntimeError, match="Only the local SQLite storage backend"):
        database.connect()


def test_turso_backend_never_initializes_external_client(monkeypatch):
    imported = []
    real_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    monkeypatch.setattr(database.settings, "DATABASE_BACKEND", "turso")
    with pytest.raises(RuntimeError, match="Only the local SQLite storage backend"):
        database.connect()
    assert not any(name.split(".", 1)[0] in {"libsql", "turso"} for name in imported)


def test_question_bank_reads_sqlite_as_source_of_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "store.sqlite")
    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    database.initialize()

    created = question_bank.create_question(
        "Physics",
        {"text": "Determine acceleration.", "marks": 3, "question_id": "stable-id"},
    )
    bank_file = question_bank.bank_path("Physics")
    assert not bank_file.exists()

    assert question_bank.read_subject("Physics") == [created]
    with database.connect() as db:
        row = db.execute(
            "SELECT question_id,position,marks_numeric FROM question_bank_records "
            "WHERE subject_name=?",
            ("Physics",),
        ).fetchone()
    assert (row["question_id"], row["position"], row["marks_numeric"]) == (
        "stable-id",
        0,
        3,
    )


def test_priorities_read_from_sqlite_when_json_projection_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "store.sqlite")
    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    database.initialize()

    question_priorities.set_priority("stable-id", 2)
    assert question_priorities.get_priorities() == {"stable-id": 2}
    with database.connect() as db:
        row = db.execute(
            "SELECT priority FROM question_priorities WHERE question_id=?",
            ("stable-id",),
        ).fetchone()
    assert row["priority"] == 2
