"""SQLite-backed processing manifest for uploaded documents."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.masterdeorca import database

_lock = threading.Lock()
_NAMESPACE = uuid.UUID("f5d7c9ab-7b62-4c04-8a1e-2bd30658ef43")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def entry_id(subject: str, filename: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{subject}/{filename}"))


def _entry(subject: str, filename: str) -> dict:
    stem = Path(filename).stem
    return {
        "id": entry_id(subject, filename),
        "subject": subject,
        "filename": filename,
        "status": "not_processed",
        "last_processed_at": None,
        "result_json": f"output/Parser/{stem}/questions_parsed.json",
        "scraper_json": f"output/Scrapper/{stem}/questions_scrapper.json",
        "docx_path": f"output/Reconstruct/{stem}/questions_reconstructed.docx",
        "error": None,
    }


def _load(subject: str) -> dict:
    with database.connect() as db:
        row = db.execute(
            """SELECT manifest_json,updated_at FROM processing_manifests
               WHERE subject_id=(SELECT subject_id FROM subjects WHERE subject_name=?)""",
            (subject,),
        ).fetchone()
    if row:
        try:
            data = json.loads(row["manifest_json"])
            if data.get("subject") == subject and isinstance(data.get("pdfs"), list):
                return data
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return {"subject": subject, "updated_at": _now(), "pdfs": []}


def _save(subject: str, manifest: dict) -> None:
    subject_id = database.get_subject_id(subject)
    if subject_id is None:
        raise ValueError(f"Unknown subject {subject!r}")
    manifest["updated_at"] = _now()
    with database.connect() as db:
        db.execute(
            """INSERT INTO processing_manifests(subject_id,manifest_json,updated_at)
               VALUES(?,?,?) ON CONFLICT(subject_id) DO UPDATE SET
               manifest_json=excluded.manifest_json,updated_at=excluded.updated_at""",
            (subject_id, json.dumps(manifest, ensure_ascii=False), manifest["updated_at"]),
        )


def update_document(subject: str, filename: str, *, status: str | None = None,
                    job: dict | None = None, result: dict | None = None,
                    error: str | None = None) -> dict:
    return update_pdf(subject, filename, status=status, job=job, result=result, error=error)


add_document = update_document
update_file = update_document


def update_pdf(subject: str, filename: str, *, status: str | None = None,
               job: dict | None = None, result: dict | None = None,
               error: str | None = None) -> dict:
    with _lock:
        manifest = _load(subject)
        by_name = {item.get("filename"): item for item in manifest["pdfs"] if isinstance(item, dict)}
        item = by_name.get(filename) or _entry(subject, filename)
        if job:
            status = job.get("status", status)
            item["last_processed_at"] = job.get("updated_at") or _now()
            item["error"] = error if error is not None else job.get("error")
        elif error is not None:
            item["error"] = error
        if status is not None:
            item["status"] = status
        if result:
            for key, value in (("parsed_json", "result_json"), ("scrapper_json", "scraper_json")):
                if result.get(key):
                    item[value] = Path(result[key]).name
        by_name[filename] = item
        manifest = {
            "subject": subject,
            "updated_at": _now(),
            "pdfs": sorted(by_name.values(), key=lambda row: row["filename"].casefold()),
        }
        _save(subject, manifest)
        return item.copy()


update_pdf_status = update_pdf


def sync_subject(subject: str, jobs: list[dict] | None = None) -> dict:
    job_by_name = {job["filename"]: job for job in (jobs or [])}
    with database.connect() as db:
        documents = db.execute(
            """SELECT d.filename FROM documents d JOIN subjects s ON s.subject_id=d.subject_id
               WHERE s.subject_name=? ORDER BY d.filename COLLATE NOCASE""",
            (subject,),
        ).fetchall()
    for row in documents:
        filename = row["filename"]
        job = job_by_name.get(filename)
        if job:
            update_pdf(subject, filename, job=job, result=job.get("result"))
        else:
            update_pdf(subject, filename)
    return _load(subject)


def remove_pdf(subject: str, filename: str) -> None:
    with _lock:
        manifest = _load(subject)
        manifest["pdfs"] = [item for item in manifest["pdfs"] if item.get("filename") != filename]
        _save(subject, manifest)
