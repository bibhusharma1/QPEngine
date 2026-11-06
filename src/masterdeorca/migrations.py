"""Idempotent schema migrations, executed at startup by database.initialize().

Each step is safe to re-run: tables use IF NOT EXISTS, columns are added only
when missing, and indexes use IF NOT EXISTS. New migrations append to the
MIGRATIONS tuple in execution order.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def add_column_if_missing(db, table: str, column: str, definition: str) -> None:
    try:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        columns = set()
        for row in rows:
            try:
                if isinstance(row, dict):
                    columns.add(row.get("name"))
                elif hasattr(row, "keys") and "name" in row.keys():
                    columns.add(row["name"])
                else:
                    # tuple format
                    columns.add(row[1])
            except Exception:
                continue
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except Exception:
        pass


def _migrate_teacher_subjects(db: sqlite3.Connection) -> None:
    """Persistent teacher -> subject assignments (many teachers, many subjects)."""
    db.execute("""CREATE TABLE IF NOT EXISTS teacher_subjects (
        teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        subject_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(teacher_id, subject_name)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_teacher_subjects_subject ON teacher_subjects(subject_name)")


def _migrate_totp_pending_secret(db: sqlite3.Connection) -> None:
    """Re-enrollment stores the new secret as pending until confirmed."""
    add_column_if_missing(db, "users", "totp_pending_secret_encrypted", "TEXT")


def _migrate_totp_challenge_index(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS idx_totp_login_challenges_email ON totp_login_challenges(user_email)")


def _migrate_papers_created_by(db: sqlite3.Connection) -> None:
    """Ensure papers.created_by exists for ownership checks (older schemas)."""
    add_column_if_missing(db, "papers", "created_by", "INTEGER REFERENCES users(id)")


def _migrate_unified_backend_entities(db: sqlite3.Connection) -> None:
    """Add indexed, JSON-capable records for local-first unified persistence."""
    db.execute("""CREATE TABLE IF NOT EXISTS subjects (
        subject_id TEXT PRIMARY KEY,
        subject_name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS documents (
        document_id TEXT PRIMARY KEY,
        subject_name TEXT NOT NULL,
        filename TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        content_type TEXT,
        checksum_sha256 TEXT,
        size_bytes INTEGER,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(subject_name, filename)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_documents_subject ON documents(subject_name)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_documents_checksum ON documents(checksum_sha256)")

    add_column_if_missing(db, "questions", "question_id", "TEXT")
    add_column_if_missing(db, "questions", "document_id", "TEXT")
    add_column_if_missing(db, "questions", "parent_question_id", "TEXT")
    add_column_if_missing(db, "questions", "marks_numeric", "REAL")
    add_column_if_missing(db, "questions", "record_json", "TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_question_id "
        "ON questions(question_id) WHERE question_id IS NOT NULL"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(subject_name)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_questions_document ON questions(document_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_questions_marks ON questions(marks_numeric)")
    add_column_if_missing(db, "jobs", "metadata_json", "TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_subject_status ON jobs(subject,status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_document ON jobs(subject,filename)")
    db.execute("""CREATE TABLE IF NOT EXISTS document_reviews (
        review_id TEXT PRIMARY KEY,
        document_id TEXT,
        subject_name TEXT NOT NULL,
        filename TEXT NOT NULL,
        review_status TEXT NOT NULL DEFAULT 'unreviewed',
        review_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        UNIQUE(subject_name, filename)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_document_reviews_status ON document_reviews(review_status)")

    db.execute("""CREATE TABLE IF NOT EXISTS question_priorities (
        question_id TEXT PRIMARY KEY,
        priority INTEGER NOT NULL CHECK(priority IN (1,2,3)),
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS blueprints (
        blueprint_id TEXT PRIMARY KEY,
        subject_name TEXT,
        blueprint_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS paper_drafts (
        draft_id TEXT PRIMARY KEY,
        subject_name TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        draft_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS generated_papers (
        paper_id TEXT PRIMARY KEY,
        subject_name TEXT,
        storage_key TEXT,
        paper_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        setting_key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS scheduler_state (
        state_key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL DEFAULT '{}',
        lease_owner TEXT,
        lease_until TEXT,
        updated_at TEXT NOT NULL
    )""")


def _migrate_question_bank_records(db: sqlite3.Connection) -> None:
    """Keep JSON-bank records separate from the legacy relational questions API."""
    db.execute("""CREATE TABLE IF NOT EXISTS question_bank_records (
        question_id TEXT PRIMARY KEY,
        subject_name TEXT NOT NULL,
        document_id TEXT,
        source_pdf TEXT,
        parent_question_id TEXT,
        marks_numeric REAL,
        record_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_bank_records_subject "
        "ON question_bank_records(subject_name)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_bank_records_document "
        "ON question_bank_records(document_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_bank_records_marks "
        "ON question_bank_records(marks_numeric)"
    )


def _migrate_question_bank_record_order(db: sqlite3.Connection) -> None:
    """Persist stable bank ordering independently of JSON file projections."""
    add_column_if_missing(db, "question_bank_records", "position", "INTEGER NOT NULL DEFAULT 0")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_bank_records_subject_position "
        "ON question_bank_records(subject_name,position)"
    )


def _migrate_document_subject_ids(db: sqlite3.Connection) -> None:
    """Attach existing document rows only to already-registered subjects."""
    add_column_if_missing(
        db, "documents", "subject_id",
        "TEXT REFERENCES subjects(subject_id) ON DELETE CASCADE",
    )
    db.execute(
        """UPDATE documents
           SET subject_id=(
               SELECT subjects.subject_id FROM subjects
               WHERE subjects.subject_name=documents.subject_name
           )
           WHERE subject_id IS NULL
             AND EXISTS (
               SELECT 1 FROM subjects
               WHERE subjects.subject_name=documents.subject_name
           )"""
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_subject_id_filename "
        "ON documents(subject_id,filename) WHERE subject_id IS NOT NULL"
    )


def _migrate_basic_configurations(db: sqlite3.Connection) -> None:
    """Store basic configuration presets in SQLite."""
    db.execute("""CREATE TABLE IF NOT EXISTS basic_configurations (
        configuration_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        university_name TEXT,
        college_name TEXT,
        department_name TEXT,
        paper_title TEXT,
        academic_year TEXT,
        semester TEXT,
        exam_type TEXT,
        subject_id TEXT,
        subject_code TEXT,
        examination_date TEXT,
        duration TEXT,
        total_marks INTEGER,
        instructions_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")


def _migrate_ui_configurations(db: sqlite3.Connection) -> None:
    """Store UI configuration presets in SQLite."""
    db.execute("""CREATE TABLE IF NOT EXISTS ui_configurations (
        config_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        values_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")


def _migrate_sqlite_blob_storage(db: sqlite3.Connection) -> None:
    """Keep uploaded documents and every runtime artifact inside SQLite."""
    add_column_if_missing(db, "documents", "content_blob", "BLOB")
    add_column_if_missing(db, "generated_papers", "docx_blob", "BLOB")
    add_column_if_missing(db, "paper_drafts", "docx_blob", "BLOB")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_subject_filename "
        "ON documents(subject_id,filename)"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS stored_artifacts (
        artifact_id TEXT PRIMARY KEY,
        subject_id TEXT REFERENCES subjects(subject_id) ON DELETE CASCADE,
        document_id TEXT REFERENCES documents(document_id) ON DELETE CASCADE,
        artifact_key TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        content_type TEXT NOT NULL,
        checksum_sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        data BLOB NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(document_id,artifact_key)
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifacts_document_type "
        "ON stored_artifacts(document_id,artifact_type)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifacts_subject_type "
        "ON stored_artifacts(subject_id,artifact_type)"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS processing_manifests (
        subject_id TEXT PRIMARY KEY REFERENCES subjects(subject_id) ON DELETE CASCADE,
        manifest_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS processing_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        level TEXT NOT NULL DEFAULT 'INFO',
        message TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_processing_logs_job ON processing_logs(job_id,log_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS idempotency_keys (
        subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(subject_id,key)
    )""")


def _migrate_user_subject_permissions(db: sqlite3.Connection) -> None:
    """Per-user subject assignments and feature permissions."""
    db.execute("""CREATE TABLE IF NOT EXISTS user_subject_assignments (
        assignment_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        subject_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, subject_name)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_user_subject_assignments_user ON user_subject_assignments(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_user_subject_assignments_subject ON user_subject_assignments(subject_name)")
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
    db.execute("CREATE INDEX IF NOT EXISTS idx_user_subject_permissions_user ON user_subject_permissions(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_user_subject_permissions_subject ON user_subject_permissions(subject_name)")


def _migrate_teacher_subjects_to_user_assignments(db: sqlite3.Connection) -> None:
    """Backfill legacy teacher_subjects into user_subject_assignments/permissions."""
    # Ensure canonical tables exist.
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
    now = datetime.now(timezone.utc).isoformat()
    try:
        rows = db.execute("SELECT teacher_id, subject_name FROM teacher_subjects").fetchall()
    except Exception:
        rows = []
    for row in rows:
        try:
            teacher_id = row["teacher_id"] if hasattr(row, "keys") else row[0]
            subject_name = row["subject_name"] if hasattr(row, "keys") else row[1]
        except Exception:
            continue
        if teacher_id is None or not subject_name:
            continue
        user = db.execute("SELECT id FROM users WHERE id=?", (teacher_id,)).fetchone()
        if not user:
            continue
        existing = db.execute(
            "SELECT assignment_id FROM user_subject_assignments WHERE user_id=? AND subject_name=?",
            (teacher_id, subject_name),
        ).fetchone()
        if existing:
            assignment_id = existing["assignment_id"] if hasattr(existing, "keys") else existing[0]
        else:
            assignment_id = uuid.uuid4().hex
            try:
                db.execute(
                    "INSERT INTO user_subject_assignments(assignment_id,user_id,subject_name,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (assignment_id, teacher_id, subject_name, now, now),
                )
            except Exception:
                row2 = db.execute(
                    "SELECT assignment_id FROM user_subject_assignments WHERE user_id=? AND subject_name=?",
                    (teacher_id, subject_name),
                ).fetchone()
                if row2:
                    assignment_id = row2["assignment_id"] if hasattr(row2, "keys") else row2[0]
        # Do not auto-grant permissions; leave permission records absent so the
        # existing policy (no record => no access) applies until an admin sets them.


MIGRATIONS: tuple[tuple[str, object], ...] = (
    ("teacher_subjects", _migrate_teacher_subjects),
    ("totp_pending_secret", _migrate_totp_pending_secret),
    ("totp_challenge_email_index", _migrate_totp_challenge_index),
    ("papers_created_by", _migrate_papers_created_by),
    ("unified_backend_entities_v1", _migrate_unified_backend_entities),
    ("question_bank_records_v1", _migrate_question_bank_records),
    ("question_bank_record_order_v1", _migrate_question_bank_record_order),
    ("document_subject_ids_v1", _migrate_document_subject_ids),
    ("basic_configurations_v1", _migrate_basic_configurations),
    ("ui_configurations_v1", _migrate_ui_configurations),
    ("sqlite_blob_storage_v1", _migrate_sqlite_blob_storage),
    ("user_subject_permissions_v1", _migrate_user_subject_permissions),
    ("teacher_subjects_to_user_assignments_v1", _migrate_teacher_subjects_to_user_assignments),
)


def run_migrations(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )""")
    for version, step in MIGRATIONS:
        applied = db.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
        ).fetchone()
        if applied:
            continue
        step(db)
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?,datetime('now'))",
            (version,),
        )
