#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from src.masterdeorca import database

def main():
    if getattr(settings, "DATABASE_BACKEND", "turso").casefold() != "sqlite" or getattr(settings, "APP_ENV", "render").casefold() != "local":
        print("Only for local SQLite development mode", file=sys.stderr)
        return 1
    email = "admin@example.com"
    password = "admin@123"
    now = datetime.now(timezone.utc).isoformat()
    db = database.connect()
    try:
        try:
            db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, hashed_password TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', department TEXT, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS admins (email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, access_mode TEXT NOT NULL DEFAULT 'edit')")
        except Exception:
            pass
        try:
            db.execute("DELETE FROM users WHERE email=?", (email,))
            db.execute("DELETE FROM admins WHERE email=?", (email,))
        except Exception:
            pass
        db.execute("INSERT INTO users(email,name,hashed_password,role,status,created_at) VALUES(?,?,?,?,?,?)", (email, "Administrator", password, "admin", "active", now))
        db.execute("INSERT INTO admins(email,password_hash,access_mode) VALUES(?,?,?)", (email, password, "edit"))
        print("Local admin created")
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass

if __name__ == '__main__':
    raise SystemExit(main())
