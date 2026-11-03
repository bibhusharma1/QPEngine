"""Idempotently import legacy JSON metadata into the local SQLite database."""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings
from config.outputs import pdf_stem
from src.masterdeorca import database
from src.masterdeorca import migrations
from scripts.data_tools import sha256

_ID_NAMESPACE = uuid.UUID("bf54f1f5-1d81-4c12-8d70-29b7a414a0e2")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to import JSON metadata at {path}: {exc}") from exc


def _stable_id(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, "/".join((kind, *parts))))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _insert_setting(db: sqlite3.Connection, key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    existing = db.execute(
        "SELECT value_json FROM app_settings WHERE setting_key=?", (key,)
    ).fetchone()
    if existing is not None:
        if existing["value_json"] != payload:
            raise RuntimeError(
                f"Conflicting existing app setting during import: {key}"
            )
        return
    db.execute(
        "INSERT INTO app_settings(setting_key,value_json,updated_at) VALUES(?,?,?)",
        (key, payload, _now()),
    )


def import_tree(root: Path) -> dict[str, int]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Legacy data root does not exist: {root}")
    with database.connect() as db:
        migrations.run_migrations(db)
    counts = {
        "subjects": 0,
        "documents": 0,
        "questions": 0,
        "reviews": 0,
        "priorities": 0,
        "blueprints": 0,
        "drafts": 0,
        "generated_papers": 0,
        "settings": 0,
        "manifests": 0,
        "artifacts": 0,
    }
    now = _now()
    with database.connect() as db:
        for subject_dir in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if not subject_dir.is_dir() or subject_dir.name.startswith("."):
                continue
            subject = subject_dir.name
            if not (subject_dir / "source").is_dir():
                continue
            source_dir = subject_dir / "source"
            source_files = sorted(
                (path for path in source_dir.iterdir() if path.is_file()),
                key=lambda item: item.name.casefold(),
            )
            bank_path = subject_dir / "question_bank.json"
            if not source_files and not bank_path.is_file():
                continue
            db.execute(
                "INSERT OR IGNORE INTO subjects(subject_id,subject_name,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (_stable_id("subject", subject), subject, now, now),
            )
            subject_row = db.execute(
                "SELECT subject_id FROM subjects WHERE subject_name=?",
                (subject,),
            ).fetchone()
            if subject_row is None:
                raise RuntimeError(f"Unable to register document subject {subject!r}")
            subject_id = subject_row["subject_id"]
            counts["subjects"] += 1

            for path in source_files:
                digest = sha256(path)
                stat = path.stat()
                content = path.read_bytes()
                document_id = _stable_id("document", subject, path.name)
                metadata = {
                    "migrated_from": str(path.relative_to(root)),
                    "original_mtime_ns": stat.st_mtime_ns,
                }
                existing = db.execute(
                    "SELECT document_id,checksum_sha256,subject_id FROM documents "
                    "WHERE subject_name=? AND filename=?",
                    (subject, path.name),
                ).fetchone()
                if existing:
                    if existing["checksum_sha256"] not in (None, digest):
                        raise RuntimeError(
                            f"Conflicting document content for {subject}/{path.name}"
                        )
                    if existing["subject_id"] not in (None, subject_id):
                        raise RuntimeError(
                            f"Conflicting subject association for {subject}/{path.name}"
                        )
                    if existing["subject_id"] is None:
                        db.execute(
                            "UPDATE documents SET subject_id=?,updated_at=? "
                            "WHERE document_id=? AND subject_id IS NULL",
                            (subject_id, now, existing["document_id"]),
                        )
                    document_id = existing["document_id"]
                    blob = db.execute(
                        "SELECT content_blob FROM documents WHERE document_id=?",
                        (document_id,),
                    ).fetchone()
                    if blob["content_blob"] is None:
                        db.execute(
                            "UPDATE documents SET content_blob=?,updated_at=? WHERE document_id=?",
                            (content, now, document_id),
                        )
                else:
                    db.execute(
                        """INSERT INTO documents(
                            document_id,subject_name,subject_id,filename,storage_key,content_type,
                            checksum_sha256,size_bytes,metadata_json,created_at,updated_at,content_blob
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            document_id,
                            subject,
                            subject_id,
                            path.name,
                            path.relative_to(root).as_posix(),
                            path.suffix.lower().lstrip("."),
                            digest,
                            stat.st_size,
                            json.dumps(metadata, ensure_ascii=False),
                            now,
                            now,
                            content,
                        ),
                    )
                counts["documents"] += 1

            documents_by_stem = {
                pdf_stem(row["filename"]).casefold(): row["document_id"]
                for row in db.execute(
                    "SELECT document_id,filename FROM documents WHERE subject_id=?",
                    (subject_id,),
                ).fetchall()
            }
            for stage in ("Parser", "Scrapper", "Reconstruct"):
                stage_root = subject_dir / "output" / stage
                if not stage_root.is_dir():
                    continue
                for output_dir in sorted(stage_root.iterdir(), key=lambda item: item.name.casefold()):
                    if not output_dir.is_dir():
                        continue
                    document_id = documents_by_stem.get(output_dir.name.casefold())
                    if not document_id:
                        continue
                    for asset_path in sorted(output_dir.rglob("*")):
                        if not asset_path.is_file():
                            continue
                        relative = asset_path.relative_to(output_dir).as_posix()
                        if stage == "Reconstruct":
                            artifact_key = asset_path.name
                        elif stage == "Parser":
                            artifact_key = relative
                        else:
                            artifact_key = f"Scrapper/{relative}"
                        data = asset_path.read_bytes()
                        checksum = hashlib.sha256(data).hexdigest()
                        suffix = asset_path.suffix.lower()
                        artifact_type = (
                            "figure" if suffix in {".png", ".jpg", ".jpeg", ".webp"}
                            else "generated_docx" if suffix == ".docx"
                            else "parser_json" if stage == "Parser" and suffix == ".json"
                            else "scraper_artifact" if stage == "Scrapper"
                            else "parser_artifact"
                        )
                        db.execute(
                            """INSERT INTO stored_artifacts(
                                artifact_id,subject_id,document_id,artifact_key,artifact_type,
                                content_type,checksum_sha256,size_bytes,data,metadata_json,
                                created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(document_id,artifact_key) DO UPDATE SET
                                artifact_type=excluded.artifact_type,
                                content_type=excluded.content_type,
                                checksum_sha256=excluded.checksum_sha256,
                                size_bytes=excluded.size_bytes,data=excluded.data,
                                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
                            WHERE stored_artifacts.checksum_sha256<>excluded.checksum_sha256""",
                            (
                                _stable_id("artifact", document_id, artifact_key),
                                subject_id,
                                document_id,
                                artifact_key,
                                artifact_type,
                                mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream",
                                checksum,
                                len(data),
                                data,
                                json.dumps({"migrated_from": str(asset_path.relative_to(root))}),
                                now,
                                now,
                            ),
                        )
                        counts["artifacts"] += 1

            if bank_path.is_file():
                bank = _load_json(bank_path)
                records = bank.get("questions") if isinstance(bank, dict) else None
                if not isinstance(records, list):
                    raise ValueError(f"Invalid question bank schema: {bank_path}")
                for position, record in enumerate(records):
                    if not isinstance(record, dict):
                        raise ValueError(f"Invalid question record in {bank_path}")
                    question_id = str(
                        record.get("question_id")
                        or record.get("id")
                        or _stable_id(
                            "question",
                            subject,
                            str(record.get("source_pdf") or ""),
                            str(position),
                            str(record.get("text") or record.get("original_text") or ""),
                        )
                    )
                    record = dict(record)
                    record["question_id"] = question_id
                    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
                    existing = db.execute(
                        "SELECT record_json,marks_numeric,position FROM question_bank_records "
                        "WHERE question_id=?",
                        (question_id,),
                    ).fetchone()
                    if existing:
                        existing_record = (
                            json.loads(existing["record_json"])
                            if existing["record_json"] is not None
                            else None
                        )
                        if (
                            isinstance(existing_record, dict)
                            and existing_record != record
                            and existing_record.get("question_id") is None
                            and existing_record.get("id") is None
                        ):
                            existing_record["question_id"] = question_id
                        if existing_record not in (None, record):
                            raise RuntimeError(
                                f"Conflicting question ID found during import: {question_id}"
                            )
                        if (
                            existing_record == record
                            and existing["marks_numeric"] == _number(record.get("marks"))
                            and existing["position"] == position
                        ):
                            continue
                        db.execute(
                            "UPDATE question_bank_records SET record_json=?,marks_numeric=?,"
                            "position=?,updated_at=? WHERE question_id=?",
                            (serialized, _number(record.get("marks")), position, now, question_id),
                        )
                        continue
                    created_at = record.get("created_at") or now
                    updated_at = record.get("updated_at") or created_at
                    source_pdf = str(record.get("source_pdf") or "")
                    document = db.execute(
                        "SELECT document_id FROM documents WHERE subject_name=? AND filename=?",
                        (subject, Path(source_pdf).name),
                    ).fetchone() if source_pdf else None
                    db.execute(
                        """INSERT INTO question_bank_records(
                            question_id,subject_name,document_id,source_pdf,parent_question_id,
                            marks_numeric,record_json,created_at,updated_at,position
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            question_id,
                            subject,
                            document["document_id"] if document else None,
                            source_pdf or None,
                            record.get("parent_question_ref") or record.get("parent_id"),
                            _number(record.get("marks")),
                            serialized,
                            str(created_at),
                            str(updated_at),
                            position,
                        ),
                    )
                    counts["questions"] += 1

            metadata_dir = subject_dir / "metadata"
            question_bank_meta = metadata_dir / "question_bank.json"
            if question_bank_meta.is_file():
                _insert_setting(
                    db,
                    f"legacy_question_bank_metadata:{subject}",
                    _load_json(question_bank_meta),
                )
                counts["settings"] += 1
            for review in db.execute(
                "SELECT filename,review_status FROM pdf_reviews WHERE subject=?",
                (subject,),
            ).fetchall():
                document = db.execute(
                    "SELECT document_id FROM documents WHERE subject_name=? AND filename=?",
                    (subject, review["filename"]),
                ).fetchone()
                db.execute(
                    """INSERT INTO document_reviews(
                        review_id,document_id,subject_name,filename,review_status,
                        review_json,updated_at
                    ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(subject_name,filename) DO UPDATE SET
                    document_id=excluded.document_id,review_status=excluded.review_status,
                    updated_at=excluded.updated_at""",
                    (
                        _stable_id("review", subject, review["filename"]),
                        document["document_id"] if document else None,
                        subject,
                        review["filename"],
                        review["review_status"],
                        json.dumps(
                            {
                                "subject": subject,
                                "filename": review["filename"],
                                "review_status": review["review_status"],
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                counts["reviews"] += 1

            manifest_path = subject_dir / "manifests" / "processing_status.json"
            if manifest_path.is_file():
                manifest = _load_json(manifest_path)
                db.execute(
                    """INSERT INTO processing_manifests(subject_id,manifest_json,updated_at)
                       VALUES(?,?,?) ON CONFLICT(subject_id) DO NOTHING""",
                    (subject_id, json.dumps(manifest, ensure_ascii=False), now),
                )
                counts["manifests"] += 1
                if isinstance(manifest, dict):
                    for item in manifest.get("pdfs") or []:
                        if not isinstance(item, dict) or not item.get("filename"):
                            continue
                        db.execute(
                            "INSERT OR IGNORE INTO document_reviews("
                            "review_id,document_id,subject_name,filename,review_status,"
                            "review_json,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                _stable_id("review", subject, str(item["filename"])),
                                None,
                                subject,
                                str(item["filename"]),
                                str(item.get("review_status") or "unreviewed"),
                                json.dumps(item, ensure_ascii=False),
                                now,
                            ),
                        )

        priorities_path = root / ".masterdeorca" / "question_priorities.json"
        if priorities_path.is_file():
            value = _load_json(priorities_path)
            priorities = value.get("priorities") if isinstance(value, dict) else None
            if not isinstance(priorities, dict):
                raise ValueError(f"Invalid priorities schema: {priorities_path}")
            for question_id, priority in priorities.items():
                if type(priority) is not int or priority not in {1, 2, 3}:
                    raise ValueError(f"Invalid priority value for question {question_id}")
                db.execute(
                    "INSERT INTO question_priorities(question_id,priority,updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(question_id) DO NOTHING",
                    (str(question_id), priority, now),
                )
                counts["priorities"] += 1

        shared_root = root / ".masterdeorca"
        blueprints_path = shared_root / "question_paper_blueprints.json"
        if blueprints_path.is_file():
            data = _load_json(blueprints_path)
            rows = data.get("blueprints") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError(f"Invalid blueprints schema: {blueprints_path}")
            for blueprint in rows:
                blueprint_id = str(blueprint.get("blueprint_id") or "")
                if not blueprint_id:
                    raise ValueError(f"Blueprint without ID in {blueprints_path}")
                payload = json.dumps(blueprint, ensure_ascii=False, sort_keys=True)
                db.execute(
                    "INSERT INTO blueprints(blueprint_id,subject_name,blueprint_json,updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(blueprint_id) DO NOTHING",
                    (blueprint_id, blueprint.get("subject_id"), payload, now),
                )
                counts["blueprints"] += 1

        for path in (shared_root / "generated_papers" / "drafts").glob("*.json"):
            draft = _load_json(path)
            draft_id = str(draft.get("draft_id") or path.stem)
            docx_path = path.with_suffix(".docx")
            db.execute(
                "INSERT INTO paper_drafts(draft_id,subject_name,status,draft_json,created_at,updated_at,docx_blob) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(draft_id) DO NOTHING",
                (
                    draft_id,
                    draft.get("subject") or draft.get("subject_name"),
                    "draft",
                    json.dumps(draft, ensure_ascii=False, sort_keys=True),
                    str(draft.get("created_at") or now),
                    str(draft.get("updated_at") or now),
                    docx_path.read_bytes() if docx_path.is_file() else None,
                ),
            )
            counts["drafts"] += 1

        generated_root = shared_root / "generated_papers"
        for path in generated_root.glob("*.json"):
            paper = _load_json(path)
            paper_id = str(paper.get("paper_id") or path.stem)
            docx_path = generated_root / f"{paper_id}.docx"
            docx_blob = docx_path.read_bytes() if docx_path.is_file() else None
            db.execute(
                "INSERT INTO generated_papers("
                "paper_id,subject_name,storage_key,paper_json,created_at,updated_at,docx_blob"
                ") VALUES(?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO NOTHING",
                (
                    paper_id,
                    paper.get("subject") or paper.get("subject_name"),
                    f"{paper_id}.docx" if docx_blob else None,
                    json.dumps(paper, ensure_ascii=False, sort_keys=True),
                    str(paper.get("generated_at") or now),
                    str(paper.get("generated_at") or now),
                    docx_blob,
                ),
            )
            counts["generated_papers"] += 1

        for filename, key in (
            ("basic_configurations.json", "basic_configurations"),
            ("ui_configurations.json", "ui_configurations"),
        ):
            path = shared_root / filename
            if path.is_file():
                _insert_setting(db, key, _load_json(path))
                counts["settings"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=settings.STORAGE_ROOT)
    args = parser.parse_args()
    counts = import_tree(args.root)
    print("Legacy data imported idempotently:")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    print(f"Database: {settings.DATABASE_PATH}")


if __name__ == "__main__":
    main()
