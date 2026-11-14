"""Idempotent schema migrations, executed at startup by database.initialize().

Each step is safe to re-run: tables use IF NOT EXISTS, columns are added only
when missing, and indexes use IF NOT EXISTS. New migrations append to the
MIGRATIONS tuple in execution order.
"""

from __future__ import annotations

import sqlite3
import uuid
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def add_column_if_missing(db, table: str, column: str, definition: str) -> None:
    rows = db.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
    columns = {row["name"] if hasattr(row, "keys") else row[0] for row in rows}
    if column in columns:
        logger.info("DB_MIGRATION_COLUMN_SKIPPED table=%s column=%s reason=already_exists", table, column)
        return
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except Exception:
        # A second startup may have added this column after our inspection.
        refreshed = db.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
        refreshed_columns = {
            row["name"] if hasattr(row, "keys") else row[0] for row in refreshed
        }
        if column not in refreshed_columns:
            raise
        logger.info("DB_MIGRATION_COLUMN_SKIPPED table=%s column=%s reason=concurrent_add", table, column)


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


def _migrate_users_totp_columns(db: sqlite3.Connection) -> None:
    for column, definition in (
        ("totp_secret_encrypted", "TEXT"),
        ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("totp_enrolled", "INTEGER NOT NULL DEFAULT 0"),
        ("totp_failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("totp_cooldown_until", "TEXT"),
        ("totp_last_used_step", "TEXT"),
    ):
        add_column_if_missing(db, "users", column, definition)


def _migrate_admin_access_mode(db: sqlite3.Connection) -> None:
    add_column_if_missing(db, "admins", "access_mode", "TEXT NOT NULL DEFAULT 'edit'")


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
        # Auto-grant all four permissions for backfilled assignments.
        try:
            perm = db.execute(
                "SELECT 1 FROM user_subject_permissions WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if not perm:
                db.execute(
                    """INSERT INTO user_subject_permissions(permission_id,assignment_id,user_id,subject_name,
                       can_view_papers,can_upload_process,can_manage_question_bank,can_generate_papers,created_at,updated_at)
                       VALUES(?,?,?,?,1,1,1,1,?,?)""",
                    (__import__("uuid").uuid4().hex, assignment_id, teacher_id, subject_name, now, now),
                )
        except Exception:
            pass


def _migrate_subject_permission_backfill(db: sqlite3.Connection) -> None:
    """Ensure every user_subject_assignment has a permission row with all four flags."""
    try:
        _ensure = None
        try:
            from src.masterdeorca.database import _ensure_user_assignment_tables
            _ensure_user_assignment_tables(db)
        except Exception:
            pass
        rows = db.execute("SELECT assignment_id,user_id,subject_name FROM user_subject_assignments").fetchall()
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            assignment_id = row["assignment_id"] if hasattr(row, "keys") else row[0]
            user_id = row["user_id"] if hasattr(row, "keys") else row[1]
            subject_name = row["subject_name"] if hasattr(row, "keys") else row[2]
            perm = db.execute("SELECT 1 FROM user_subject_permissions WHERE assignment_id=?", (assignment_id,)).fetchone()
            if not perm:
                try:
                    db.execute(
                        """INSERT INTO user_subject_permissions(permission_id,assignment_id,user_id,subject_name,
                           can_view_papers,can_upload_process,can_manage_question_bank,can_generate_papers,created_at,updated_at)
                           VALUES(?,?,?,?,1,1,1,1,?,?)""",
                        (__import__("uuid").uuid4().hex, assignment_id, user_id, subject_name, now, now),
                    )
                except Exception:
                    pass
            else:
                pass
    except Exception:
        pass


def _migrate_teacher_status_values(db: sqlite3.Connection) -> None:
    """Map legacy teacher statuses to authorized/unauthorized."""
    try:
        db.execute("UPDATE users SET status='authorized' WHERE role='teacher' AND status IN ('approved','active')")
    except Exception:
        pass


def _migrate_smart_qpg(db) -> None:
    """Syllabus versions and restart-safe AI question generation state."""
    db.execute("""CREATE TABLE IF NOT EXISTS syllabi (
        syllabus_id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
        document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
        original_filename TEXT NOT NULL,
        syllabus_name TEXT,
        academic_year TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        processing_error TEXT,
        version INTEGER NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        summary_json TEXT NOT NULL,
        summary_text TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(subject_id,version)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_syllabi_subject_active ON syllabi(subject_id,is_active)")
    db.execute("""CREATE TABLE IF NOT EXISTS question_generation_jobs (
        job_id TEXT PRIMARY KEY,
        created_by TEXT NOT NULL,
        subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
        syllabus_id TEXT NOT NULL REFERENCES syllabi(syllabus_id),
        status TEXT NOT NULL,
        request_json TEXT NOT NULL,
        progress_json TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        docx_blob BLOB,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_generation_jobs_owner ON question_generation_jobs(created_by,created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS question_generation_items (
        item_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES question_generation_jobs(job_id) ON DELETE CASCADE,
        section TEXT NOT NULL,
        topic TEXT NOT NULL,
        marks INTEGER NOT NULL,
        type TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        source_module TEXT NOT NULL DEFAULT '',
        source_heading TEXT NOT NULL DEFAULT '',
        position INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        question_json TEXT,
        error TEXT,
        retries INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    add_column_if_missing(db, "question_generation_items", "position", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(db, "question_generation_items", "source_module", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(db, "question_generation_items", "source_heading", "TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS idx_generation_items_job_status ON question_generation_items(job_id,status)")
    try:
        db.execute("UPDATE users SET status='unauthorized' WHERE role='teacher' AND status IN ('pending','disapproved')")
    except Exception:
        pass


def _migrate_smart_qpg_docx_status(db) -> None:
    """Persist DOCX export failures separately from question-generation errors."""
    add_column_if_missing(db, "question_generation_jobs", "docx_error", "TEXT")


def _migrate_syllabus_upload_status(db) -> None:
    """Track syllabus labels and background PDF processing state."""
    add_column_if_missing(db, "syllabi", "syllabus_name", "TEXT")
    add_column_if_missing(db, "syllabi", "academic_year", "TEXT")
    add_column_if_missing(db, "syllabi", "status", "TEXT NOT NULL DEFAULT 'pending'")
    add_column_if_missing(db, "syllabi", "processing_error", "TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_syllabi_subject_status ON syllabi(subject_id,status,created_at)")
    db.execute("UPDATE syllabi SET status='completed' WHERE status='pending' AND summary_json NOT IN ('{}','')")
    db.execute("UPDATE syllabi SET syllabus_name=original_filename WHERE syllabus_name IS NULL")


def _summary_to_text(value: str | None) -> str:
    """Render a legacy structured summary as readable syllabus text."""
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return ""
    parts = []
    if data.get("overview"):
        parts.append(str(data["overview"]).strip())
    def render(label: str, entries) -> None:
        if not isinstance(entries, list) or not entries:
            return
        lines = [label]
        for entry in entries:
            if isinstance(entry, str):
                lines.append(f"- {entry.strip()}")
            elif isinstance(entry, dict):
                title = entry.get("unit") or entry.get("chapter") or entry.get("name") or entry.get("topic") or entry.get("title")
                if title:
                    lines.append(f"- {title}")
                for key in ("topics", "subtopics", "concepts", "learning_objectives"):
                    children = entry.get(key)
                    if isinstance(children, list):
                        lines.extend(f"  - {item if isinstance(item, str) else item.get('name', item.get('topic', str(item)))}" for item in children)
        if len(lines) > 1:
            parts.extend(lines)
    render("Units and chapters", data.get("units"))
    render("Topics", data.get("topics"))
    render("Learning objectives", data.get("learning_objectives"))
    render("Marks distribution", data.get("marks_distribution"))
    return "\n".join(parts).strip()


def _migrate_syllabuses_separate_storage(db) -> None:
    """Copy only explicit Syllabus Library entries into their own BLOB table."""
    db.execute("""CREATE TABLE IF NOT EXISTS syllabuses (
        id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        content_blob BLOB NOT NULL,
        summary_text TEXT NOT NULL DEFAULT '',
        processing_status TEXT NOT NULL DEFAULT 'pending',
        processing_error TEXT,
        syllabus_name TEXT,
        academic_year TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 0,
        uploaded_by TEXT NOT NULL,
        checksum_sha256 TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(subject_id,version)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_syllabuses_subject_active ON syllabuses(subject_id,is_active)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_syllabuses_subject_status ON syllabuses(subject_id,processing_status,created_at)")
    try:
        legacy = db.execute("""SELECT s.syllabus_id,s.subject_id,s.original_filename,s.syllabus_name,s.academic_year,
            s.status,s.processing_error,s.version,s.is_active,s.summary_json,s.created_by,s.created_at,s.updated_at,
            d.content_blob,d.checksum_sha256
            FROM syllabi s JOIN documents d ON d.document_id=s.document_id
            WHERE lower(s.original_filename) LIKE '%.pdf' AND d.content_blob IS NOT NULL""").fetchall()
    except Exception:
        legacy = []
    for row in legacy:
        blob = bytes(row["content_blob"] or b"")
        if not blob.startswith(b"%PDF-"):
            continue
        db.execute("""INSERT OR IGNORE INTO syllabuses(id,subject_id,filename,content_blob,summary_text,
            processing_status,processing_error,syllabus_name,academic_year,version,is_active,uploaded_by,
            checksum_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["syllabus_id"],row["subject_id"],row["original_filename"],blob,
             _summary_to_text(row["summary_json"]),row["status"],row["processing_error"],
             row["syllabus_name"],row["academic_year"],row["version"],row["is_active"],
             row["created_by"],row["checksum_sha256"],row["created_at"],row["updated_at"]))
    # Generation jobs retain historical rows while dropping the old FK to `syllabi`.
    # New jobs now reference IDs in `syllabuses`.
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS question_generation_jobs_v2 (
            job_id TEXT PRIMARY KEY, created_by TEXT NOT NULL,
            subject_id TEXT NOT NULL REFERENCES subjects(subject_id), syllabus_id TEXT NOT NULL,
            status TEXT NOT NULL, request_json TEXT NOT NULL, progress_json TEXT NOT NULL DEFAULT '{}',
            error TEXT, docx_blob BLOB, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("""INSERT OR IGNORE INTO question_generation_jobs_v2
            SELECT job_id,created_by,subject_id,syllabus_id,status,request_json,progress_json,error,docx_blob,created_at,updated_at
            FROM question_generation_jobs""")
        db.execute("""CREATE TABLE IF NOT EXISTS question_generation_items_v2 (
            item_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES question_generation_jobs_v2(job_id) ON DELETE CASCADE,
            section TEXT NOT NULL, topic TEXT NOT NULL, marks INTEGER NOT NULL, type TEXT NOT NULL,
            difficulty TEXT NOT NULL, source_module TEXT NOT NULL DEFAULT '',
            source_heading TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
            question_json TEXT, error TEXT, retries INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("""INSERT OR IGNORE INTO question_generation_items_v2
            SELECT item_id,job_id,section,topic,marks,type,difficulty,source_module,source_heading,position,status,question_json,error,retries,created_at,updated_at
            FROM question_generation_items""")
        db.execute("DROP TABLE question_generation_items")
        db.execute("DROP TABLE question_generation_jobs")
        db.execute("ALTER TABLE question_generation_jobs_v2 RENAME TO question_generation_jobs")
        db.execute("ALTER TABLE question_generation_items_v2 RENAME TO question_generation_items")
        db.execute("CREATE INDEX IF NOT EXISTS idx_generation_jobs_owner ON question_generation_jobs(created_by,created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_generation_items_job_status ON question_generation_items(job_id,status)")
    except Exception:
        # Older installations may not have enabled generation tables yet.
        pass


def _migrate_syllabus_processing_retries(db) -> None:
    """Persist automatic retry counts while keeping the original PDF in place."""
    add_column_if_missing(db, "syllabuses", "processing_attempts", "INTEGER NOT NULL DEFAULT 0")


def _migrate_generation_item_source_refs(db) -> None:
    """Store validated syllabus provenance alongside each generated item."""
    add_column_if_missing(db, "question_generation_items", "source_module", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(db, "question_generation_items", "source_heading", "TEXT NOT NULL DEFAULT ''")


def _migrate_canonical_user_roles(db) -> None:
    """Normalize legacy roles without touching user identity or assignments."""
    db.execute("""UPDATE users
        SET role=CASE WHEN LOWER(TRIM(COALESCE(role,'')))='admin' THEN 'admin' ELSE 'teacher' END
        WHERE role IS NULL
           OR role <> CASE WHEN LOWER(TRIM(COALESCE(role,'')))='admin' THEN 'admin' ELSE 'teacher' END""")


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
    ("teacher_status_values_v1", _migrate_teacher_status_values),
    ("smart_qpg_syllabus_generation_v1", _migrate_smart_qpg),
    ("smart_qpg_syllabus_upload_status_v1", _migrate_syllabus_upload_status),
    ("smart_qpg_syllabuses_separate_storage_v1", _migrate_syllabuses_separate_storage),
    ("smart_qpg_syllabus_processing_retries_v1", _migrate_syllabus_processing_retries),
    ("smart_qpg_generation_item_source_refs_v1", _migrate_generation_item_source_refs),
    ("canonical_user_roles_v1", _migrate_canonical_user_roles),
    ("smart_qpg_generation_docx_status_v1", _migrate_smart_qpg_docx_status),
    ("users_totp_columns_v1", _migrate_users_totp_columns),
    ("admin_access_mode_v1", _migrate_admin_access_mode),
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
            logger.info("DB_MIGRATION_SKIPPED version=%s reason=already_applied", version)
            continue
        logger.info("DB_MIGRATION_START version=%s", version)
        try:
            step(db)
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?,datetime('now'))",
                (version,),
            )
        except Exception:
            logger.exception("DB_MIGRATION_FAILED version=%s", version)
            raise
        logger.info("DB_MIGRATION_APPLIED version=%s", version)
