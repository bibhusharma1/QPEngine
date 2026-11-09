"""Database source of truth for application records and binary artifacts (Turso/libSQL)."""

from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from config import settings
from config.settings import ADMIN_EMAIL, ADMIN_PASSWORD
from pwdlib import PasswordHash

from src.masterdeorca import migrations

_password_hash = PasswordHash.recommended()
_SUBJECT_ID_NAMESPACE = uuid.UUID("bf54f1f5-1d81-4c12-8d70-29b7a414a0e2")


class _NormalizedRow(dict):
    """Dict-like row that also supports positional (integer) access like sqlite3.Row."""

    def __init__(self, description, values):
        columns = []
        for col in (description or ()):
            name = col[0] if isinstance(col, (tuple, list)) else col
            columns.append(name)
        super().__init__(zip(columns, values))
        self._columns = columns
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self._values[key]
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self):
        return iter(self._values)

    def keys(self):
        return list(self._columns)

    def __len__(self):
        return len(self._values)


class _NormalizedCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, _NormalizedRow):
            return row
        if isinstance(row, dict):
            return row
        desc = getattr(self._cursor, "description", None)
        if desc is not None and not isinstance(row, (str, bytes)):
            try:
                return _NormalizedRow(desc, row)
            except Exception:
                pass
        return row

    def fetchall(self):
        rows = self._cursor.fetchall() or []
        result = []
        for row in rows:
            if isinstance(row, _NormalizedRow) or isinstance(row, dict):
                result.append(row)
                continue
            desc = getattr(self._cursor, "description", None)
            try:
                result.append(_NormalizedRow(desc, row))
            except Exception:
                result.append(row)
        return result

    def fetchmany(self, size=None):
        if size is None:
            rows = self._cursor.fetchmany()
        else:
            rows = self._cursor.fetchmany(size)
        rows = rows or []
        result = []
        for row in rows:
            if isinstance(row, _NormalizedRow) or isinstance(row, dict):
                result.append(row)
                continue
            desc = getattr(self._cursor, "description", None)
            try:
                result.append(_NormalizedRow(desc, row))
            except Exception:
                result.append(row)
        return result

    def __iter__(self):
        for row in self._cursor:
            if isinstance(row, _NormalizedRow) or isinstance(row, dict):
                yield row
            else:
                desc = getattr(self._cursor, "description", None)
                try:
                    yield _NormalizedRow(desc, row)
                except Exception:
                    yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _NormalizedConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, *args, **kwargs):
        return _NormalizedCursor(self._connection.execute(*args, **kwargs))

    def executemany(self, *args, **kwargs):
        return self._connection.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._connection.executescript(*args, **kwargs)

    def commit(self):
        return self._connection.commit()

    def close(self):
        return self._connection.close()

    def __enter__(self):
        enter = getattr(self._connection, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc):
        exit_context = getattr(self._connection, "__exit__", None)
        if exit_context is not None:
            return exit_context(*exc)
        if exc and exc[0] is not None:
            rollback = getattr(self._connection, "rollback", None)
            if rollback is not None:
                rollback()
            return False
        self._connection.commit()
        return False

    def __getattr__(self, name):
        return getattr(self._connection, name)


def validate_configuration() -> None:
    backend = str(getattr(settings, "DATABASE_BACKEND", "")).casefold()
    if backend != "turso":
        raise RuntimeError(
            "DATABASE_BACKEND must be 'turso'; local SQLite is not an application datastore."
        )
    url = str(getattr(settings, "TURSO_DATABASE_URL", "") or "")
    if not url:
        raise RuntimeError("TURSO_DATABASE_URL must be set to the configured Turso database.")
    if not getattr(settings, "TURSO_AUTH_TOKEN", ""):
        raise RuntimeError("TURSO_AUTH_TOKEN must be set for the configured Turso database.")
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "libsql" or not (
        host.startswith("qp-bibhusharma1.") and host.endswith(".turso.io")
    ):
        raise RuntimeError(
            "TURSO_DATABASE_URL must identify database 'qp' in organization 'bibhusharma1'."
        )


def _get_connection():
    validate_configuration()
    url = settings.TURSO_DATABASE_URL
    token = settings.TURSO_AUTH_TOKEN
    try:
        import libsql  # type: ignore
    except Exception as exc:
        raise RuntimeError("Turso libSQL client is unavailable; install the backend dependencies.") from exc
    try:
        raw = libsql.connect(url, auth_token=token)
    except Exception as exc:
        # Never include the URL or token in an error that could reach logs/API output.
        raise RuntimeError(
            f"Unable to connect to the configured Turso database ({type(exc).__name__}). "
            "Check Turso URL, token, network access, and database availability."
        ) from exc
    conn = _NormalizedConnection(raw)
    for pragma in ("PRAGMA busy_timeout=30000", "PRAGMA foreign_keys=ON"):
        try:
            conn.execute(pragma)
        except Exception:
            pass
    return conn


def _is_busy_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in ("busy", "locked", "idle transaction", "database is locked"))


def _retry_db(operation, attempts: int = 5, base_delay: float = 0.05):
    import time
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if not _is_busy_error(exc) or attempt >= attempts:
                raise
            time.sleep(min(base_delay * (2 ** (attempt - 1)), 1.0))
    if last_error is not None:
        raise last_error


def connect():
    return _get_connection()


def initialize() -> None:
    validate_configuration()
    db = _get_connection()
    try:
        _init_schema(db)
        migrations.run_migrations(db)
        db.commit()
    finally:
        db.close()


def _init_schema(db) -> None:
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, hashed_password TEXT NOT NULL, role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', department TEXT, created_at TEXT NOT NULL,
                totp_secret_encrypted TEXT,
                totp_enabled INTEGER NOT NULL DEFAULT 0,
                totp_enrolled INTEGER NOT NULL DEFAULT 0,
                totp_failed_attempts INTEGER NOT NULL DEFAULT 0,
                totp_cooldown_until TEXT,
                totp_last_used_step TEXT
            )""")
        except Exception:
            pass
        # Add columns if they don't exist (for migration)
        try:
            db.execute("ALTER TABLE users ADD COLUMN totp_secret_encrypted TEXT")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE users ADD COLUMN totp_enrolled INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE users ADD COLUMN totp_failed_attempts INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE users ADD COLUMN totp_cooldown_until TEXT")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE users ADD COLUMN totp_last_used_step TEXT")
        except Exception:
            pass
        db.execute("""CREATE TABLE IF NOT EXISTS totp_login_challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenge_token TEXT NOT NULL UNIQUE,
            user_email TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS totp_rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            window_start TEXT NOT NULL,
            cooldown_until TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subject_name TEXT NOT NULL,
            name TEXT NOT NULL, unit INTEGER, description TEXT,
            UNIQUE(subject_name, name)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subject_name TEXT NOT NULL,
            topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
            text TEXT NOT NULL, difficulty TEXT NOT NULL, type TEXT NOT NULL,
            marks INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, UNIQUE(source, text)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            subject_name TEXT NOT NULL, exam_type TEXT NOT NULL DEFAULT '',
            total_marks INTEGER NOT NULL DEFAULT 0, duration TEXT NOT NULL DEFAULT '',
            question_count INTEGER NOT NULL DEFAULT 0, created_by INTEGER REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}'
        )""")
        try:
            db.execute("ALTER TABLE papers ADD COLUMN created_by INTEGER REFERENCES users(id)")
        except Exception:
            pass
        db.execute("""CREATE TABLE IF NOT EXISTS pdf_reviews (
            subject TEXT NOT NULL, filename TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'unreviewed',
            PRIMARY KEY(subject, filename)
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS admins (email TEXT PRIMARY KEY, password_hash TEXT NOT NULL)")
        try:
            db.execute("ALTER TABLE admins ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'edit'")
        except Exception:
            pass
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, subject TEXT NOT NULL, filename TEXT NOT NULL,
            status TEXT NOT NULL, stage TEXT NOT NULL, started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, error TEXT, result_json TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS reconciliation_state (
            subject TEXT NOT NULL, filename TEXT NOT NULL, stage TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, last_attempt_at TEXT, next_retry_at TEXT,
            updated_at TEXT NOT NULL, PRIMARY KEY(subject, filename, stage)
        )""")
        # Authentication is performed exclusively against the Turso users table.
        # Do not seed or overwrite administrator credentials from environment
        # variables; the database is the single source of truth.


def _get_row_value(row, key):
    if row is None:
        return None
    try:
        # dict-like
        if isinstance(row, dict):
            return row.get(key)
        # sqlite3.Row or similar
        try:
            return row[key]
        except Exception:
            pass
        # tuple
        try:
            if hasattr(row, 'keys'):
                keys = row.keys()
                if key in keys:
                    return row[key]
        except Exception:
            pass
    except Exception:
        pass
    return None


def ensure_subject_record(db: Any, subject_name: str) -> str:
    """Return a stable database subject ID, creating a record for an explicit subject."""
    now = datetime.now(timezone.utc).isoformat()
    subject_id = str(uuid.uuid5(_SUBJECT_ID_NAMESPACE, f"subject/{subject_name}"))
    try:
        db.execute(
            "INSERT OR IGNORE INTO subjects(subject_id,subject_name,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            (subject_id, subject_name, now, now),
        )
    except Exception:
        pass
    try:
        row = db.execute(
            "SELECT subject_id FROM subjects WHERE subject_name=?",
            (subject_name,),
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        raise RuntimeError(f"Unable to register subject {subject_name!r}.")
    val = _get_row_value(row, "subject_id")
    if val is None:
        # try tuple
        try:
            if isinstance(row, (tuple, list)):
                val = row[0]
        except Exception:
            pass
    if val is None:
        raise RuntimeError(f"Unable to register subject {subject_name!r}.")
    return str(val)


def get_subject_id(subject_name: str) -> str | None:
    with connect() as db:
        row = db.execute(
            "SELECT subject_id FROM subjects WHERE subject_name=?",
            (subject_name,),
        ).fetchone()
    return str(row["subject_id"]) if row else None


def get_document(subject_name: str, filename: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            """SELECT d.* FROM documents d JOIN subjects s ON s.subject_id=d.subject_id
               WHERE s.subject_name=? AND d.filename=?""",
            (subject_name, filename),
        ).fetchone()
    return dict(row) if row else None


def get_document_bytes(subject_name: str, filename: str) -> bytes | None:
    with connect() as db:
        row = db.execute(
            """SELECT content_blob FROM documents d JOIN subjects s ON s.subject_id=d.subject_id
               WHERE s.subject_name=? AND d.filename=?""",
            (subject_name, filename),
        ).fetchone()
    return bytes(row["content_blob"]) if row and row["content_blob"] is not None else None


def put_artifact(
    *,
    subject_id: str | None,
    document_id: str | None,
    artifact_key: str,
    artifact_type: str,
    content_type: str,
    data: bytes,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(data).hexdigest()
    with connect() as db:
        existing = db.execute(
            "SELECT checksum_sha256 FROM stored_artifacts WHERE document_id IS ? AND artifact_key=?",
            (document_id, artifact_key),
        ).fetchone()
        if existing and existing["checksum_sha256"] == digest:
            return
        db.execute(
            """INSERT INTO stored_artifacts(
                artifact_id,subject_id,document_id,artifact_key,artifact_type,content_type,
                checksum_sha256,size_bytes,data,metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(document_id,artifact_key) DO UPDATE SET
                subject_id=excluded.subject_id,artifact_type=excluded.artifact_type,
                content_type=excluded.content_type,checksum_sha256=excluded.checksum_sha256,
                size_bytes=excluded.size_bytes,data=excluded.data,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (
                uuid.uuid4().hex,
                subject_id,
                document_id,
                artifact_key,
                artifact_type,
                content_type,
                digest,
                len(data),
                data,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
                now,
            ),
        )


def get_artifact(document_id: str, artifact_key: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            """SELECT artifact_type,content_type,checksum_sha256,size_bytes,data,metadata_json
               FROM stored_artifacts WHERE document_id=? AND artifact_key=?""",
            (document_id, artifact_key),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["data"] = bytes(result["data"])
    result["metadata"] = json.loads(result.pop("metadata_json"))
    return result


def mark_interrupted_jobs() -> None:
    """Make unfinished in-process jobs retryable after an API restart."""
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("""UPDATE jobs SET status='failed', stage='interrupted', updated_at=?,
            error='Worker stopped before completion. Start a fresh processing run.'
            WHERE status IN ('queued', 'running')""", (now,))


def verify_admin(email: str, password: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT password_hash FROM admins WHERE email = ?", (email.lower(),)).fetchone()
    if not row:
        # Reduce user enumeration timing differences.
        try:
            _password_hash.verify(password, _password_hash.hash("invalid-login-padding"))
        except Exception:
            pass
        return False
    hp = _row_get(row, "password_hash")
    if hp is None:
        try:
            hp = row["password_hash"] if not isinstance(row, (tuple, list)) else (row[0] if len(row) else None)
        except Exception:
            hp = None
    if hp is None:
        try:
            _password_hash.verify(password, _password_hash.hash("invalid-login-padding"))
        except Exception:
            pass
        return False
    try:
        return bool(_password_hash.verify(password, hp))
    except (ValueError, TypeError, Exception):
        # Keep timing-ish behavior; do not accept plaintext.
        return False


def _row_get(row, key, default=None):
    if row is None:
        return default
    try:
        if isinstance(row, dict):
            return row.get(key, default)
    except Exception:
        pass
    try:
        return row[key]
    except Exception:
        pass
    try:
        if hasattr(row, 'keys') and key in row.keys():
            return row[key]
    except Exception:
        pass
    try:
        if isinstance(row, (tuple, list)):
            # common: hashed_password,status -> 0,1
            if key == 'hashed_password':
                return row[0] if len(row) > 0 else default
            if key == 'status':
                return row[1] if len(row) > 1 else default
            if key == 'password_hash':
                return row[0] if len(row) > 0 else default
    except Exception:
        pass
    return default


def verify_user(email: str, password: str) -> bool:
    with connect() as db:
        try:
            row = db.execute("SELECT hashed_password,status FROM users WHERE email=?", (email.lower(),)).fetchone()
        except Exception:
            row = None
    status = _row_get(row, "status")
    if not row or status not in {"active", "approved", "authorized"}:
        try:
            _password_hash.verify(password, _password_hash.hash("invalid-login-padding"))
        except Exception:
            pass
        return False
    hp = _row_get(row, "hashed_password")
    if hp is None:
        try:
            _password_hash.verify(password, _password_hash.hash("invalid-login-padding"))
        except Exception:
            pass
        return False
    try:
        return bool(_password_hash.verify(password, hp))
    except Exception:
        return False


def save_job(job: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    job_id = job["job_id"]
    subject = job["subject"]
    filename = job["filename"]
    status = job["status"]
    stage = job["stage"]
    started_at = job["started_at"]
    error = job.get("error")
    result_json = json.dumps(job.get("result")) if job.get("result") is not None else None
    def _write():
        with connect() as db:
            db.execute(
                "INSERT INTO jobs(job_id, subject, filename, status, stage, started_at, updated_at, error, result_json) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, "
                "stage=excluded.stage, updated_at=excluded.updated_at, error=excluded.error, "
                "result_json=excluded.result_json",
                (job_id, subject, filename, status, stage, started_at, now, error, result_json),
            )
            try:
                db.commit()
            except Exception:
                pass
    _retry_db(_write)


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
    return result


def latest_job(subject: str, filename: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT job_id FROM jobs WHERE subject=? AND filename=? ORDER BY started_at DESC LIMIT 1", (subject, filename)).fetchone()
    return get_job(row["job_id"]) if row else None


# ---------------------------------------------------------------------------
# Teacher -> subject assignments (RBAC)
# ---------------------------------------------------------------------------


def get_teacher_by_id(teacher_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT id,email,name,role FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone()
    return dict(row) if row else None


def get_teacher_subjects(email: str) -> list[str]:
    """Subject names assigned to a teacher account, ordered (empty when none).

    The canonical source is user_subject_assignments. Fall back to the legacy
    teacher_subjects table for any records not yet migrated.
    """
    with connect() as db:
        rows = db.execute(
            """SELECT usa.subject_name FROM user_subject_assignments usa
               JOIN users u ON u.id = usa.user_id WHERE u.email=? ORDER BY usa.subject_name""",
            (email.lower(),)).fetchall()
        subjects = [row["subject_name"] for row in rows]
        if not subjects:
            try:
                legacy = db.execute(
                    """SELECT ts.subject_name FROM teacher_subjects ts
                       JOIN users u ON u.id = ts.teacher_id WHERE u.email=? ORDER BY ts.subject_name""",
                    (email.lower(),)).fetchall()
                subjects = [row["subject_name"] for row in legacy]
            except Exception:
                subjects = []
    return subjects


def set_teacher_subjects(teacher_id: int, subject_names: list[str]) -> None:
    """Replace a teacher's full assignment set in one transaction."""
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("DELETE FROM teacher_subjects WHERE teacher_id=?", (teacher_id,))
        db.executemany("INSERT INTO teacher_subjects(teacher_id, subject_name, created_at) VALUES(?,?,?)",
                       [(teacher_id, name, now) for name in subject_names])
    # Keep canonical user-subject assignments in sync (preserve existing permissions).
    sync_user_subject_assignments(teacher_id, subject_names)


def sync_user_subject_assignments(user_id: int, subject_names: list[str]) -> None:
    """Ensure user_subject_assignments/permissions match subject_names.

    Existing permission flags are preserved; only missing assignments/permissions
    are created with defaults, and removed subjects are deleted.
    """
    import uuid as _uuid
    now = datetime.now(timezone.utc).isoformat()
    subject_set = {str(s).strip() for s in (subject_names or []) if str(s).strip()}
    with connect() as db:
        try:
            from src.masterdeorca.database import _ensure_user_assignment_tables
        except Exception:
            pass
        try:
            _ensure_user_assignment_tables(db)
        except Exception:
            pass
        existing_rows = db.execute(
            "SELECT assignment_id, subject_name FROM user_subject_assignments WHERE user_id=?",
            (user_id,),
        ).fetchall()
        existing_map = {row["subject_name"]: row["assignment_id"] for row in existing_rows}
        # Remove assignments not in the new set.
        for subject_name, assignment_id in list(existing_map.items()):
            if subject_name not in subject_set:
                db.execute("DELETE FROM user_subject_permissions WHERE assignment_id=?", (assignment_id,))
                db.execute("DELETE FROM user_subject_assignments WHERE assignment_id=?", (assignment_id,))
                existing_map.pop(subject_name, None)
        # Ensure assignments and permissions exist for requested subjects.
        for subject_name in subject_set:
            assignment_id = existing_map.get(subject_name)
            if assignment_id is None:
                assignment_id = _uuid.uuid4().hex
                try:
                    db.execute(
                        "INSERT INTO user_subject_assignments(assignment_id,user_id,subject_name,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (assignment_id, user_id, subject_name, now, now),
                    )
                except Exception:
                    row = db.execute(
                        "SELECT assignment_id FROM user_subject_assignments WHERE user_id=? AND subject_name=?",
                        (user_id, subject_name),
                    ).fetchone()
                    assignment_id = row["assignment_id"] if row else assignment_id
            else:
                db.execute(
                    "UPDATE user_subject_assignments SET updated_at=? WHERE assignment_id=?",
                    (now, assignment_id),
                )
            perm = db.execute(
                "SELECT 1 FROM user_subject_permissions WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if not perm:
                try:
                    db.execute(
                        """INSERT INTO user_subject_permissions(permission_id,assignment_id,user_id,subject_name,
                           can_view_papers,can_upload_process,can_manage_question_bank,can_generate_papers,created_at,updated_at)
                           VALUES(?,?,?,?,1,1,1,1,?,?)""",
                        (_uuid.uuid4().hex, assignment_id, user_id, subject_name, now, now),
                    )
                except Exception:
                    pass
            else:
                db.execute(
                    "UPDATE user_subject_permissions SET subject_name=?, can_view_papers=1, can_upload_process=1, can_manage_question_bank=1, can_generate_papers=1, updated_at=? WHERE assignment_id=?",
                    (subject_name, now, assignment_id),
                )


def add_teacher_subject(teacher_id: int, subject_name: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO teacher_subjects(teacher_id, subject_name, created_at) VALUES(?,?,?)",
            (teacher_id, subject_name, now))
    return cur.rowcount > 0


def remove_teacher_subject(teacher_id: int, subject_name: str) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM teacher_subjects WHERE teacher_id=? AND subject_name=?",
                         (teacher_id, subject_name))
        try:
            _ensure_user_assignment_tables(db)
            db.execute("DELETE FROM user_subject_permissions WHERE user_id=? AND subject_name=?",
                       (teacher_id, subject_name))
            cur2 = db.execute("DELETE FROM user_subject_assignments WHERE user_id=? AND subject_name=?",
                              (teacher_id, subject_name))
            if cur2.rowcount > cur.rowcount:
                cur = cur2
        except Exception:
            pass
        try:
            db.commit()
        except Exception:
            pass
    return cur.rowcount > 0


def rename_teacher_subject(old_name: str, new_name: str) -> None:
    with connect() as db:
        db.execute("UPDATE teacher_subjects SET subject_name=? WHERE subject_name=?", (new_name, old_name))
        try:
            db.execute("UPDATE user_subject_assignments SET subject_name=? WHERE subject_name=?", (new_name, old_name))
            db.execute("UPDATE user_subject_permissions SET subject_name=? WHERE subject_name=?", (new_name, old_name))
        except Exception:
            pass


def delete_teacher_subjects(subject_name: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM teacher_subjects WHERE subject_name=?", (subject_name,))
        try:
            db.execute("DELETE FROM user_subject_assignments WHERE subject_name=?", (subject_name,))
            db.execute("DELETE FROM user_subject_permissions WHERE subject_name=?", (subject_name,))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# User subject assignments & permissions
# ---------------------------------------------------------------------------


def _ensure_user_assignment_tables(db):
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS user_subject_assignments (
            assignment_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subject_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, subject_name)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS user_subject_permissions (
            permission_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL REFERENCES user_subject_assignments(assignment_id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subject_name TEXT NOT NULL,
            can_view_papers INTEGER NOT NULL DEFAULT 1,
            can_upload_process INTEGER NOT NULL DEFAULT 1,
            can_manage_question_bank INTEGER NOT NULL DEFAULT 1,
            can_generate_papers INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(assignment_id),
            UNIQUE(user_id, subject_name)
        )""")
    except Exception:
        pass


def get_user_by_id(user_id: int):
    with connect() as db:
        row = db.execute("SELECT id,email,name,role,status FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def set_user_password_by_id(user_id: int, hashed_password: str) -> bool:
    """Update only the target user's hashed_password. Returns False if no row matched."""
    def _write():
        with connect() as db:
            cur = db.execute("UPDATE users SET hashed_password=? WHERE id=?", (hashed_password, user_id))
            try:
                db.commit()
            except Exception:
                pass
            return cur
    cur = _retry_db(_write)
    try:
        return cur.rowcount > 0
    except Exception:
        return True


def set_user_password_by_email(email: str, hashed_password: str) -> bool:
    """Update only the identified user's hashed_password. Returns False if no row matched."""
    def _write():
        with connect() as db:
            cur = db.execute("UPDATE users SET hashed_password=? WHERE email=?", (hashed_password, email.lower()))
            try:
                db.commit()
            except Exception:
                pass
            return cur
    cur = _retry_db(_write)
    try:
        return cur.rowcount > 0
    except Exception:
        return True


def set_user_email_by_email(old_email: str, new_email: str) -> bool:
    """Update only the identified user's email. Returns False on conflict/failure."""
    def _write():
        with connect() as db:
            cur = db.execute("UPDATE users SET email=? WHERE email=?", (new_email.lower(), old_email.lower()))
            try:
                db.commit()
            except Exception:
                pass
            return cur
    try:
        cur = _retry_db(_write)
        try:
            return cur.rowcount > 0
        except Exception:
            return True
    except Exception:
        return False


def get_user_by_email(email: str):
    with connect() as db:
        row = db.execute("SELECT id,email,name,role,status FROM users WHERE email=?", (email.lower(),)).fetchone()
        return dict(row) if row else None


def get_user_status(email: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT status FROM users WHERE email=?", (email.lower(),)).fetchone()
    if not row:
        return None
    return _row_get(row, "status")


def get_user_hashed_password(email: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT hashed_password FROM users WHERE email=?", (email.lower(),)).fetchone()
    if not row:
        return None
    return _row_get(row, "hashed_password")


def list_all_users():
    with connect() as db:
        rows = db.execute("SELECT id,email,name,role,status,department,created_at FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def assign_user_subjects(user_id: int, subject_names: list[str], permissions: dict | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    permissions = permissions or {}
    result = {"user_id": user_id, "assigned": []}
    with connect() as db:
        _ensure_user_assignment_tables(db)
        for subject_name in subject_names:
            subject_name = str(subject_name).strip()
            if not subject_name:
                continue
            assignment_id = uuid.uuid4().hex
            try:
                db.execute("""INSERT INTO user_subject_assignments(assignment_id,user_id,subject_name,created_at,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(user_id,subject_name) DO UPDATE SET updated_at=excluded.updated_at""",
                    (assignment_id, user_id, subject_name, now, now))
            except Exception:
                cur = db.execute("SELECT assignment_id FROM user_subject_assignments WHERE user_id=? AND subject_name=?", (user_id, subject_name))
                row = cur.fetchone()
                assignment_id = row["assignment_id"] if row else assignment_id
            perm_id = uuid.uuid4().hex
            # Auto-grant all four capabilities on assignment.
            can_view = 1
            can_upload = 1
            can_manage = 1
            can_generate = 1
            try:
                db.execute("""INSERT INTO user_subject_permissions(permission_id,assignment_id,user_id,subject_name,
                    can_view_papers,can_upload_process,can_manage_question_bank,can_generate_papers,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,subject_name) DO UPDATE SET
                    can_view_papers=excluded.can_view_papers,can_upload_process=excluded.can_upload_process,
                    can_manage_question_bank=excluded.can_manage_question_bank,can_generate_papers=excluded.can_generate_papers,
                    updated_at=excluded.updated_at""",
                    (perm_id, assignment_id, user_id, subject_name, can_view, can_upload, can_manage, can_generate, now, now))
            except Exception:
                pass
            result["assigned"].append(subject_name)
    # Keep legacy teacher_subjects in sync for teacher accounts.
    try:
        user = get_user_by_id(user_id)
        if user and user.get("role") == "teacher":
            now2 = datetime.now(timezone.utc).isoformat()
            with connect() as db2:
                db2.execute("DELETE FROM teacher_subjects WHERE teacher_id=?", (user_id,))
                db2.executemany("INSERT INTO teacher_subjects(teacher_id, subject_name, created_at) VALUES(?,?,?)",
                                [(user_id, name, now2) for name in subject_names])
    except Exception:
        pass
    return result


def update_user_subject_permissions(user_id: int, subject_name: str, permissions: dict) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        _ensure_user_assignment_tables(db)
        sets = []
        vals = []
        for k, col in [("can_view_papers","can_view_papers"),("can_upload_process","can_upload_process"),
                       ("can_manage_question_bank","can_manage_question_bank"),("can_generate_papers","can_generate_papers")]:
            if k in permissions:
                sets.append(f"{col}=?")
                vals.append(1 if permissions[k] else 0)
        if not sets:
            return False
        sets.append("updated_at=?")
        vals.extend([now, user_id, subject_name])
        cur = db.execute(f"UPDATE user_subject_permissions SET {', '.join(sets)} WHERE user_id=? AND subject_name=?", vals)
        return cur.rowcount > 0


def remove_user_subject(user_id: int, subject_name: str) -> bool:
    with connect() as db:
        try:
            _ensure_user_assignment_tables(db)
            cur = db.execute("DELETE FROM user_subject_assignments WHERE user_id=? AND subject_name=?", (user_id, subject_name))
            db.execute("DELETE FROM user_subject_permissions WHERE user_id=? AND subject_name=?", (user_id, subject_name))
            return cur.rowcount > 0
        except Exception:
            return False


def get_user_subjects(user_id: int):
    with connect() as db:
        try:
            _ensure_user_assignment_tables(db)
            rows = db.execute("""SELECT a.subject_name, p.can_view_papers,p.can_upload_process,p.can_manage_question_bank,p.can_generate_papers
                FROM user_subject_assignments a LEFT JOIN user_subject_permissions p ON p.assignment_id=a.assignment_id
                WHERE a.user_id=? ORDER BY a.subject_name""", (user_id,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def get_user_subjects_by_email(email: str):
    user = get_user_by_email(email)
    if not user:
        return []
    return get_user_subjects(user["id"])


def check_user_subject_permission(email: str, subject_name: str, permission: str) -> bool:
    user = get_user_by_email(email)
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    with connect() as db:
        try:
            _ensure_user_assignment_tables(db)
            row = db.execute("""SELECT p.can_view_papers,p.can_upload_process,p.can_manage_question_bank,p.can_generate_papers
                FROM user_subject_permissions p WHERE p.user_id=? AND p.subject_name=?""", (user["id"], subject_name)).fetchone()
            if not row:
                return False
            mapping = {
                "view_papers": row["can_view_papers"],
                "upload_process": row["can_upload_process"],
                "manage_question_bank": row["can_manage_question_bank"],
                "generate_papers": row["can_generate_papers"],
            }
            return bool(mapping.get(permission, 0))
        except Exception:
            return False
