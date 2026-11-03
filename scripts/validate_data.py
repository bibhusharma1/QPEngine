"""Validate SQLite data integrity and report migration status."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, default=settings.STORAGE_ROOT)
    parser.add_argument("--database", type=Path, default=settings.DATABASE_PATH)
    args = parser.parse_args()

    storage_root = args.storage_root.expanduser().resolve()
    db_path = args.database.expanduser().resolve()
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    report = {"database": str(db_path), "tables": {}}
    report["integrity_check"] = cur.execute("PRAGMA integrity_check").fetchone()[0]
    report["foreign_key_violations"] = [dict(row) for row in cur.execute("PRAGMA foreign_key_check")]
    
    tables = ['question_bank_records', 'question_priorities', 'blueprints', 'paper_drafts', 
              'generated_papers', 'basic_configurations', 'ui_configurations', 'jobs', 
              'documents', 'subjects', 'reconciliation_state']
    for t in tables:
        try:
            cur.execute(f"SELECT count(*) as c FROM {t}")
            report["tables"][t] = cur.fetchone()["c"]
        except Exception as e:
            report["tables"][t] = f"ERROR: {e}"
    
    try:
        rows = cur.execute(
            "SELECT document_id,filename,checksum_sha256,content_blob FROM documents"
        ).fetchall()
        missing_blobs = []
        checksum_mismatches = []
        for row in rows:
            data = row["content_blob"]
            if data is None:
                missing_blobs.append(f"{row['document_id']}:{row['filename']}")
            elif row["checksum_sha256"] and hashlib.sha256(data).hexdigest() != row["checksum_sha256"]:
                checksum_mismatches.append(f"{row['document_id']}:{row['filename']}")
        report["documents_missing_blobs"] = missing_blobs
        report["document_checksum_mismatches"] = checksum_mismatches
        report["artifact_checksum_mismatches"] = [
            row["artifact_id"]
            for row in cur.execute(
                "SELECT artifact_id,checksum_sha256,data FROM stored_artifacts"
            ).fetchall()
            if hashlib.sha256(row["data"]).hexdigest() != row["checksum_sha256"]
        ]
    except sqlite3.Error as exc:
        report["blob_validation_error"] = str(exc)

    # Legacy JSON is reported but never altered by validation.
    json_files = []
    for p in storage_root.rglob("*.json"):
        if p.is_file() and not p.is_symlink():
            json_files.append(str(p.relative_to(storage_root)))
    report["json_files_remaining"] = len(json_files)
    report["json_files_sample"] = json_files[:30]
    
    print(json.dumps(report, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
