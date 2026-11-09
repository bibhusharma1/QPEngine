# Deployment

## Local development

```bash
cp .env.example .env   # configure values
pip install -e ".[dev]"  # or: uv sync
uvicorn src.masterdeorca.app:app --reload --host 0.0.0.0 --port 8000
# Swagger UI: http://localhost:8000/docs
```

Modes:
- `PARSER_MODE=local` — legacy local Scrapper/Parser (GPU where available).
- `PARSER_MODE=llm` — NVIDIA Lite parser (no local OCR/GPU deps):
  `PARSER_MODE=llm uvicorn src.masterdeorca.app:app --host 0.0.0.0 --port 8000`

## Docker

Profiles:
- Full (local scraper/parser + GPU): `docker compose --profile full build && docker compose --profile full up -d`
- Lite (NVIDIA API only): `docker compose --profile lite build && docker compose --profile lite up -d`
- Logs: `docker compose logs -f`; Stop: `docker compose down`; Health: `curl http://localhost:8000/health`

The Lite image includes LibreOffice Writer for DOCX rasterization; PDF pages rasterized by PyMuPDF.

## Environment configuration (no secrets here)

Key settings (see `.env.example` for full list):
- `APP_ENV` = `local` or `render`; `DATABASE_BACKEND=sqlite`; `STORAGE_BACKEND=sqlite` (or `local` alias).
- `DATABASE_PATH` (preferred) or `QUES_DATABASE_PATH`; default `~/.local/share/masterdeorca/masterdeorca.sqlite`.
- `QUES_STORAGE_ROOT` (legacy import/CLI compat); `QUES_DEFAULT_SUBJECT=OperatingSys`; `QUES_MAX_UPLOAD_MB=50`; `QUES_PORT=8000`.
- `QUES_JWT_SECRET` (≥32 chars), `QUES_JWT_TTL_MINUTES`; `QUES_ADMIN_EMAIL`, `QUES_ADMIN_PASSWORD`; `QUES_CORS_ORIGINS`.
- `QUES_TOTP_ENCRYPTION_KEY`, `QUES_TOTP_ISSUER`, `QUES_TOTP_CHALLENGE_TTL_MINUTES`, `QUES_TOTP_FAILED_ATTEMPT_THRESHOLD`, `QUES_TOTP_COOLDOWN_SECONDS`.
- `QUES_RECONCILE_ENABLED` (default true); `QUES_RECONCILE_INTERVAL_HOURS` (12 or 24, default 12).
- NVIDIA Lite: `PARSER_MODE`, `NVIDIA_API`/`NVIDIA_API_KEY`/`NVIDIA_API_KEYS`, `NVIDIA_PARSE_MODEL`, `NVIDIA_STRUCTURE_MODEL`, `NVIDIA_PARSE_ENDPOINT`, `NVIDIA_STRUCTURE_ENDPOINT`, retries/timeouts/backoff, `NVIDIA_RENDER_DPI`, `LITE_STAGE2_BLOCK_BATCH_SIZE`, `LITE_STAGE2_BLOCK_OVERLAP`.

## Migration and recovery

```bash
python -m scripts.migrate_data --storage-root /path/to/legacy-data \
  --database "$DATABASE_PATH" --source-database /path/to/legacy/masterdeorca.sqlite
python -m scripts.validate_data --database "$DATABASE_PATH" --storage-root /path/to/legacy-data
python -m scripts.backup_data
python -m scripts.restore_backup /path/to/backup/database/masterdeorca.sqlite --database "$DATABASE_PATH"
```

Omit `--source-database` when no separate legacy SQLite DB. Source files remain untouched; import is idempotent. `restore_backup` refuses to replace an existing DB unless `--replace`. Stop backend before restoring.

`scripts.sync_sqlite_to_turso` is an unverified future-integration scaffold; not invoked by the app.

## Turso / object storage

Scaffold only, not verified. `DATABASE_BACKEND` must be `sqlite`; selecting Turso fails at startup rather than falling back. `STORAGE_BACKEND=object` reserved but not implemented.

## Render deployment

SQLite-only (SQLite is the only verified runtime backend; Turso is scaffold-only).
Attach a persistent disk at `/data` and set env vars: `APP_ENV=local`, `DATABASE_BACKEND=sqlite`,
`STORAGE_BACKEND=sqlite`, `DATABASE_PATH=/data/masterdeorca.sqlite`, `QUES_STORAGE_ROOT=/data`,
`QUES_JWT_SECRET` (≥32 chars), `QUES_ADMIN_EMAIL`, `QUES_ADMIN_PASSWORD`, `PORT=8000`,
`PARSER_MODE=llm` (recommended on Render, no GPU), `CORS_ORIGINS` (comma-separated frontend
origins), `QUES_RECONCILE_ENABLED=true`. Render uses the root `Dockerfile` (Lite mode by
default); set NVIDIA API credentials via env. `PARSER_MODE=local` is not recommended on Render
(no GPU).

Turso deploy variant exists as an unverified scaffold (`scripts/init_turso_schema.py`,
`scripts/sync_sqlite_to_turso.py`); do not treat as supported.

## Related

- [development-guide.md](development-guide.md)
- [architecture.md](architecture.md)
- [jobs-and-errors.md](jobs-and-errors.md)
