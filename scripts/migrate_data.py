"""Safely migrate an existing SQLite database and legacy filesystem data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from config import settings
from scripts.import_legacy_data import import_tree
from src.masterdeorca import database


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.migration-", suffix=".tmp",
        dir=destination.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(str(source)) as source_db, sqlite3.connect(
            str(temporary)
        ) as target_db:
            source_db.backup(target_db)
            integrity = target_db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Source database copy failed integrity check: {integrity}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_database(source: Path | None, destination: Path) -> None:
    database.DATABASE_PATH = destination
    if source is not None:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Source SQLite database does not exist: {source}")
        if source != destination.resolve():
            source_digest = _file_sha256(source)
            marker_key = f"legacy_sqlite_import:{source}"
            copied_source = not destination.exists()
            if copied_source:
                _copy_database(source, destination)
            database.initialize()
            with database.connect() as db:
                marker = db.execute(
                    "SELECT value_json FROM app_settings WHERE setting_key=?",
                    (marker_key,),
                ).fetchone()
                if marker is not None:
                    prior = json.loads(marker["value_json"])
                    if prior.get("sha256") != source_digest:
                        raise RuntimeError(
                            f"Source database changed since its import: {source}. "
                            "Resolve the conflict before retrying."
                        )
                elif not copied_source:
                    raise RuntimeError(
                        "The destination already contains application data and has no "
                        f"migration marker for {source}; refusing to merge conflicting "
                        "SQLite records."
                    )
                else:
                    db.execute(
                        "INSERT INTO app_settings(setting_key,value_json,updated_at) "
                        "VALUES(?,?,datetime('now'))",
                        (
                            marker_key,
                            json.dumps({"sha256": source_digest}, sort_keys=True),
                        ),
                    )
        else:
            database.initialize()
    else:
        database.initialize()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, default=settings.STORAGE_ROOT)
    parser.add_argument("--database", type=Path, default=settings.DATABASE_PATH)
    parser.add_argument(
        "--source-database",
        type=Path,
        help="Existing SQLite database to copy into a new destination before migration.",
    )
    args = parser.parse_args()

    storage_root = args.storage_root.expanduser().resolve()
    database_path = args.database.expanduser().resolve()
    _prepare_database(args.source_database, database_path)
    counts = import_tree(storage_root)
    print(json.dumps({
        "database": str(database_path),
        "legacy_root": str(storage_root),
        "imported": counts,
    }, indent=2))


if __name__ == "__main__":
    main()
