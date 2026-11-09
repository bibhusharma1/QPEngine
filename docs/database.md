# Database

Turso Cloud (libSQL) is the sole runtime database and source of truth for
application records. Every Uvicorn or Docker instance must be configured with the
same `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`; for this deployment, that URL
identifies database `qp` in organization `bibhusharma1`.

`DATABASE_BACKEND` must be `turso`. Startup and every call to `database.connect()`
reject any other value, missing credentials, missing libSQL support, or a failed
Turso connection. There is no local SQLite fallback. The application creates and
migrates its schema through the same authenticated connection used by requests;
migration and commit failures abort startup.

## Persistent records

Users, teachers, permissions, subjects, Question Bank rows, jobs, documents,
processing metadata, priorities, blueprints, drafts, generated papers, and stored
artifacts use tables and BLOB columns in Turso. API services and migrations use
`src.masterdeorca.database.connect()`; no service opens its own application
database.

## Local files

Local temporary directories are used while libraries process files. A few
compatibility projections and exports (for example `question_bank.json`) may be
written under `QUES_STORAGE_ROOT`, but runtime reads and writes use Turso and the
projections are not an alternate source of truth. Uploaded originals and runtime
artifacts are persisted in Turso before being materialized for processing or
download. RAM-only job locks, short-lived metrics, and scheduler coordination
state are process-local.

`STORAGE_BACKEND=sqlite` is a legacy setting name for the existing BLOB-storage
mode; it does not select or create a local SQLite database.
