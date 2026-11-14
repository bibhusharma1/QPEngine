#!/usr/bin/env python3
"""Create the first admin in an explicitly local SQLite development database."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from src.masterdeorca import database


def main() -> int:
    if settings.APP_ENV != "local" or settings.DATABASE_BACKEND != "sqlite":
        print("Local admin provisioning requires APP_ENV=local and DATABASE_BACKEND=sqlite.", file=sys.stderr)
        return 1
    if not os.environ.get("QUES_ADMIN_EMAIL") or not os.environ.get("QUES_ADMIN_PASSWORD"):
        print("Set QUES_ADMIN_EMAIL and QUES_ADMIN_PASSWORD in the local environment.", file=sys.stderr)
        return 1

    email = settings.ADMIN_EMAIL.strip().lower()
    password = settings.ADMIN_PASSWORD
    if not email or len(password) < 8 or len(password) > 256:
        print("Local admin configuration is invalid.", file=sys.stderr)
        return 1

    try:
        database.initialize()
        now = datetime.now(timezone.utc).isoformat()
        password_hash = database._password_hash.hash(password)
        if not database._password_hash.verify(password, password_hash):
            raise RuntimeError("Password hash verification failed.")
        with database.connect() as db:
            existing_user = db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
            existing_admin = db.execute("SELECT 1 FROM admins WHERE email=?", (email,)).fetchone()
            if existing_user or existing_admin:
                print("An account with the configured email already exists; no changes were made.", file=sys.stderr)
                return 1
            db.execute(
                "INSERT INTO users(email,name,hashed_password,role,status,created_at) VALUES(?,?,?,?,?,?)",
                (email, "Local Administrator", password_hash, "admin", "active", now),
            )
            db.execute(
                "INSERT INTO admins(email,password_hash,access_mode) VALUES(?,?,?)",
                (email, password_hash, "edit"),
            )
        print("Local administrator provisioned with the application password hasher.")
        return 0
    except Exception:
        print("Local admin provisioning failed; check local SQLite configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
