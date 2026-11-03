"""Create a verified, non-destructive copy of the configured backend data."""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import settings


def main() -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    default_destination = Path.home() / ".local" / "share" / "masterdeorca-backups" / timestamp
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=default_destination)
    parser.add_argument("--database", type=Path, default=settings.DATABASE_PATH)
    parser.add_argument("--include-legacy-files", action="store_true",
                        help="Also copy legacy filesystem data without changing it.")
    parser.add_argument("--source", type=Path, default=settings.STORAGE_ROOT)
    args = parser.parse_args()

    dest = args.destination.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    copied = 0
    if args.include_legacy_files:
        source = args.source.expanduser().resolve()
        if source.exists():
            # Copy legacy files without following symbolic links.
            for root, dirs, files in os.walk(source):
                dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
                for name in files:
                    src_file = Path(root) / name
                    if src_file.is_symlink():
                        continue
                    rel = src_file.relative_to(source)
                    dst_file = dest / "legacy-storage" / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    copied += 1

    # SQLite's backup API includes committed WAL contents consistently.
    db_src = args.database.expanduser().resolve()
    if not db_src.is_file():
        raise FileNotFoundError(f"Configured SQLite database does not exist: {db_src}")
    db_dst = dest / "database" / db_src.name
    if db_dst.exists():
        raise FileExistsError(f"Backup database already exists: {db_dst}")
    db_dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{db_dst.name}.", suffix=".tmp", dir=db_dst.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(str(db_src)) as source_db, sqlite3.connect(str(temporary)) as dest_db:
            source_db.backup(dest_db)
            result = dest_db.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"Backup integrity check failed: {result}")
        os.replace(temporary, db_dst)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Database backed up and verified: {db_dst}")
    
    print(f"Backup completed: {copied} files copied to {dest}")
    print(f"Timestamp: {timestamp}")


if __name__ == "__main__":
    main()
