"""Shared, non-destructive local data copy and verification helpers."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_summary(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path}: {integrity}")
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"SQLite foreign-key check failed for {path}: {len(violations)} violation(s)"
            )
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }


def validate_sqlite_migration(source: Path, destination: Path) -> None:
    source_counts = sqlite_summary(source)
    target_counts = sqlite_summary(destination)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        target_tables = {
            row[0]
            for row in target_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in source_counts:
            if table not in target_tables:
                raise RuntimeError(f"Migrated database is missing source table {table!r}")
            source_columns = [
                row[1] for row in source_db.execute(f'PRAGMA table_info("{table}")')
            ]
            target_columns = {
                row[1] for row in target_db.execute(f'PRAGMA table_info("{table}")')
            }
            common = [column for column in source_columns if column in target_columns]
            selected = ",".join(f'"{column}"' for column in common)
            source_rows = Counter(
                json.dumps(tuple(row), ensure_ascii=False, default=str)
                for row in source_db.execute(f'SELECT {selected} FROM "{table}"')
            )
            target_rows = Counter(
                json.dumps(tuple(row), ensure_ascii=False, default=str)
                for row in target_db.execute(f'SELECT {selected} FROM "{table}"')
            )
            if source_rows - target_rows:
                raise RuntimeError(
                    f"Migrated database lost or changed source rows in table {table!r}"
                )
    if any(target_counts.get(table, 0) < count for table, count in source_counts.items()):
        raise RuntimeError("Migrated database has fewer rows than the source database")


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def _copy_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            validate_sqlite_migration(source, destination)
        except RuntimeError as exc:
            raise FileExistsError(
                f"Destination database does not preserve source data; "
                f"refusing to overwrite: {destination}"
            ) from exc
        return
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
    shutil.copystat(source, destination)


def copy_tree(source: Path, destination: Path) -> tuple[int, int]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Source data directory does not exist: {source}")
    if source == destination or source in destination.parents:
        raise ValueError("Destination must not be the source directory or inside it")
    if destination in source.parents:
        raise ValueError("Destination must not contain the source directory")

    copied = 0
    identical = 0
    destination.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_symlink():
            raise RuntimeError(f"Refusing to follow symbolic link during migration: {source_path}")
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
            shutil.copystat(source_path, destination_path)
            continue
        if not source_path.is_file():
            continue
        if destination_path.exists():
            if _is_sqlite(source_path):
                _copy_sqlite(source_path, destination_path)
                identical += 1
            elif sha256(source_path) == sha256(destination_path):
                identical += 1
            else:
                raise FileExistsError(
                    f"Destination file differs from source; refusing to overwrite: {destination_path}"
                )
            continue
        if _is_sqlite(source_path):
            _copy_sqlite(source_path, destination_path)
        else:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
        copied += 1
    return copied, identical


def validate_tree(source: Path, destination: Path) -> dict[str, int]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    source_files = {
        path.relative_to(source): path
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    destination_files = {
        path.relative_to(destination): path
        for path in destination.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    missing = sorted(str(path) for path in source_files.keys() - destination_files.keys())
    extra = sorted(str(path) for path in destination_files.keys() - source_files.keys())
    mismatched: list[str] = []
    checked = 0
    for relative, source_path in source_files.items():
        target_path = destination_files.get(relative)
        if target_path is None:
            continue
        if _is_sqlite(source_path):
            try:
                validate_sqlite_migration(source_path, target_path)
            except RuntimeError:
                mismatched.append(str(relative))
        elif sha256(source_path) != sha256(target_path):
            mismatched.append(str(relative))
        elif source_path.suffix.casefold() == ".json":
            try:
                json.loads(target_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                mismatched.append(str(relative))
        checked += 1
    if missing or extra or mismatched:
        raise RuntimeError(
            "Data-tree validation failed: "
            f"missing={missing[:10]}, extra={extra[:10]}, mismatched={mismatched[:10]}"
        )
    return {"files": checked, "bytes": sum(path.stat().st_size for path in source_files.values())}
