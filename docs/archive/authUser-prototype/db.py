"""
Database layer — tries Turso (remote libsql) first, falls back to local SQLite.
"""

import os
import sqlite3
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# Try to import libsql for Turso; if the package isn't installed we'll
# silently fall back to plain sqlite3.
# ---------------------------------------------------------------------------
try:
    import libsql_experimental as libsql  # type: ignore

    HAS_LIBSQL = True
except ImportError:
    HAS_LIBSQL = False

TURSO_DB_URL = os.getenv("TURSO_DB_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

LOCAL_DB_PATH = os.path.join(os.path.dirname(__file__), "local_auth.db")

# ── table DDL ──────────────────────────────────────────────────────────────
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL
);
"""

_SEED = """
INSERT OR IGNORE INTO users (username, password) VALUES ('admin', 'admin123');
INSERT OR IGNORE INTO users (username, password) VALUES ('testuser', 'test123');
"""


# ── helpers ────────────────────────────────────────────────────────────────
def _init_local_db() -> None:
    """Ensure the local SQLite file has the users table + seed data."""
    con = sqlite3.connect(LOCAL_DB_PATH)
    cur = con.cursor()
    cur.executescript(_CREATE_TABLE + _SEED)
    con.commit()
    con.close()


def _init_turso_db(con) -> None:
    """Ensure the remote Turso DB has the users table + seed data."""
    con.executescript(_CREATE_TABLE + _SEED)
    con.commit()


# ── public API ─────────────────────────────────────────────────────────────
def _query_turso(username: str, password: str) -> bool | None:
    """
    Query Turso. Returns True/False on success, None if Turso is
    unreachable or not configured.
    """
    if not (HAS_LIBSQL and TURSO_DB_URL and TURSO_AUTH_TOKEN):
        return None  # not configured → skip

    try:
        con = libsql.connect(
            database=TURSO_DB_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )
        _init_turso_db(con)
        cur = con.execute(
            "SELECT 1 FROM users WHERE username = ? AND password = ?",
            (username, password),
        )
        return cur.fetchone() is not None
    except Exception as exc:
        print(f"[WARN] Turso query failed ({exc}), falling back to local DB")
        return None


def _query_local(username: str, password: str) -> bool:
    """Query the local SQLite mirror."""
    _init_local_db()
    con = sqlite3.connect(LOCAL_DB_PATH)
    cur = con.execute(
        "SELECT 1 FROM users WHERE username = ? AND password = ?",
        (username, password),
    )
    found = cur.fetchone() is not None
    con.close()
    return found


def check_credentials(username: str, password: str) -> tuple[bool, str]:
    """
    Check username + password.

    Returns (is_valid, source) where source is 'turso' or 'local'.
    Strategy: try Turso first → if unavailable fall back to local SQLite.
    """
    result = _query_turso(username, password)
    if result is not None:
        return result, "turso"

    return _query_local(username, password), "local"

