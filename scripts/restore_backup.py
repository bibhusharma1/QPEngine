"""Restore a verified SQLite backup without modifying the backup source."""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path

from config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="SQLite backup file")
    parser.add_argument("--database", type=Path, default=settings.DATABASE_PATH,
                        help="Destination SQLite database path")
    parser.add_argument("--replace", action="store_true",
                        help="Replace an existing destination database")
    args = parser.parse_args()

    source = args.backup.expanduser().resolve()
    destination = args.database.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Backup database does not exist: {source}")
    if destination.exists() and not args.replace:
        raise FileExistsError(
            f"Destination database already exists: {destination}; pass --replace to replace it."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore-", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(str(source)) as source_db, sqlite3.connect(str(temporary)) as target_db:
            source_db.backup(target_db)
            result = target_db.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"Restored database integrity check failed: {result}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Restored and verified SQLite database at {destination}")


if __name__ == "__main__":
    main()
