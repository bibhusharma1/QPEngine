# Database

SQLite is the only runtime datastore. One file at `DATABASE_PATH`
(default `~/.local/share/masterdeorca/masterdeorca.sqlite`), configured for WAL,
foreign keys, 30 s busy timeout, normal synchronous mode. Tables are created
idempotently at startup (`database.initialize()` + `migrations.run_migrations()`).

## Tables

| Table | Purpose |
|---|---|
| `users` | id, email (unique), name, hashed_password (Argon2 via pwdlib), role (`admin`/`teacher`), status (`active`/`approved`/`pending`/`disapproved`), department, created_at, TOTP columns |
| `admins` | email (PK), password_hash, access_mode (`edit`/`readonly`) |
| `teacher_subjects` | teacher_id → subject_name (RBAC assignments) |
| `topics` | id, subject_name, name, unit, description; UNIQUE(subject_name, name) |
| `questions` | legacy staging table: subject_name, topic_id, text, difficulty, type, marks, status, source, timestamps; UNIQUE(source, text) |
| `papers` | legacy table; not written by current generation flow |
| `pdf_reviews` | (subject, filename) PK, review_status |
| `documents` | uploaded document registry incl. content_blob |
| `stored_artifacts` | parser outputs, caches, figures, tables, reconstructed DOCX (BLOB + checksum + metadata) |
| `question_bank_records` | runtime source of truth for Question Bank (JSON record per row, indexed by subject/document/marks, ordered by position) |
| `jobs` | job_id (PK), subject, filename, status, stage, started_at, updated_at, error, result_json |
| `reconciliation_state` | (subject, filename, stage) PK, status, retry_count, last_error, last_attempt_at, next_retry_at, updated_at |
| `totp_login_challenges` | challenge_token (unique), user_email, expires_at, used, created_at |
| `totp_rate_limits` | created by schema but unused by TOTP service |
| `subjects` / `user_subjects` / `reconciliation` / related shared tables | created by versioned migrations |

## JSON stores (filesystem, locked + atomic)

| File | Contents |
|---|---|
| `<subject>/question_bank.json` | compatibility projection of the bank (leaf rows only) |
| `<subject>/manifests/processing_status.json` | per-document status + relative artifact paths |
| `<subject>/metadata/<stem>.processing.json` | last job summary per document |
| `<subject>/metadata/create_idempotency.json` | idempotency key → question_id |
| `.masterdeorca/question_paper_blueprints.json` | blueprints, canonical `default-generic` |
| `.masterdeorca/question_priorities.json` | `{version:1, priorities:{question_id: 1..3}}` |
| `.masterdeorca/basic_configurations.json` | presets, canonical `default-config` |
| `.masterdeorca/ui_configurations.json` | UI presets |
| `.masterdeorca/generated_papers/{paper_id}.json` + `.docx` | generated paper records |
| `.masterdeorca/generated_papers/drafts/{draft_id}.json` | draft state |
| `output/Parser/<stem>/lite_*` | extraction cache, structuring progress, block classifications |

## Operations

- `database.connect()` context manager returns a connection (Row factory, FK on).
- Reads/writes on `jobs`, `documents`, `subjects`, `users`, `admins`, `teacher_subjects`,
  `topics`, `pdf_reviews`, `reconciliation_state`, TOTP state.
- `database.save_job`, `database.get_job`, `database.latest_job`,
  `database.mark_interrupted_jobs`.
- `question_bank_records` is the runtime source of truth; `question_bank.json` is a
  compatibility projection retained for tools/reconciliation.

## In-memory state (not durable)

`_active_jobs` set (duplicate job prevention), dashboard metrics cache (10 s TTL),
reconciliation `_last_run`, Nemotron key rotation index.

## Turso / object storage

Scaffold only, not verified. `DATABASE_BACKEND` must be `sqlite`; selecting Turso
fails at startup rather than falling back. `STORAGE_BACKEND=object` is reserved but
not implemented. See [deployment.md](deployment.md).

## Migration status (from persistence audit)

All application data persistence has been migrated from JSON/filesystem sources to
SQLite as the single source of truth. Binaries (PDFs, images, DOCX) remain in
configured file storage with metadata/paths managed through SQLite. Former JSON
stores (`question_bank.json`, blueprints, priorities, basic/UI configs, generated
papers, drafts, processing manifests) are now SQLite-backed; JSON files may remain
as compatibility projections. Jobs, documents, reviews, topics, questions, teacher
assignments, TOTP/auth, reconciliation state were already SQLite-backed.

## Related

- [backend-structure.md](backend-structure.md)
- [authentication-permissions.md](authentication-permissions.md)
