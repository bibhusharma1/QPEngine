# Deploying MasterDeOrca on Render with Turso

## Required Environment Variables
Set these in Render → Environment:

| Variable | Value | Notes |
| --- | --- | --- |
| `APP_ENV` | `render` | Required |
| `DATABASE_BACKEND` | `turso` | Required |
| `TURSO_DATABASE_URL` | `libsql://...` or `https://...` | Your Turso database URL |
| `TURSO_AUTH_TOKEN` | `<token>` | Your Turso auth token |
| `STORAGE_BACKEND` | `sqlite` | BLOBs stored in DB |
| `QUES_STORAGE_ROOT` | `/data` | Optional |
| `QUES_JWT_SECRET` | `<long-random-secret>` | >= 32 chars |
| `QUES_ADMIN_EMAIL` | `admin@example.com` | Initial admin |
| `QUES_ADMIN_PASSWORD` | `<strong-password>` | Initial admin password |
| `PORT` | `8000` | Render sets this |
| `PARSER_MODE` | `llm` or `local` | `llm` recommended |
| `CORS_ORIGINS` | `https://your-frontend.onrender.com` | Comma-separated |
| `NVIDIA_API` / `NVIDIA_API_KEYS` | ... | If using llm mode |

## Schema Initialization
On first start, the app calls `database.initialize()` which creates all tables, indexes, constraints, and runs migrations for Turso. The script `scripts/init_turso_schema.py` can also be used to initialize schema independently.

## Notes
- All BLOBs and JSON remain stored as before (TEXT/BLOB columns). No data migration performed by these changes.
- Schema is identical to existing 31-table schema defined in `database.py` and `migrations.py`.
- Initialization is idempotent (uses CREATE TABLE IF NOT EXISTS, ADD COLUMN guarded, migrations tracked).
