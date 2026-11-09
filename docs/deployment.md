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
- `APP_ENV` = `local` or `render`; `DATABASE_BACKEND=turso` (required); `STORAGE_BACKEND=sqlite` selects the existing Turso BLOB storage mode and does not select a local database.
- `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are required. All instances must use the same Turso database (`qp`, organization `bibhusharma1`).
- `QUES_STORAGE_ROOT` (legacy import/CLI compat); `QUES_DEFAULT_SUBJECT=OperatingSys`; `QUES_MAX_UPLOAD_MB=50`; `QUES_PORT=8000`.
- `QUES_JWT_SECRET` (≥32 chars), `QUES_JWT_TTL_MINUTES`; `QUES_ADMIN_EMAIL`, `QUES_ADMIN_PASSWORD`; `QUES_CORS_ORIGINS`.
- `QUES_TOTP_ENCRYPTION_KEY`, `QUES_TOTP_ISSUER`, `QUES_TOTP_CHALLENGE_TTL_MINUTES`, `QUES_TOTP_FAILED_ATTEMPT_THRESHOLD`, `QUES_TOTP_COOLDOWN_SECONDS`.
- `QUES_RECONCILE_ENABLED` (default true); `QUES_RECONCILE_INTERVAL_HOURS` (12 or 24, default 12).
- NVIDIA Lite: `PARSER_MODE`, `NVIDIA_API`/`NVIDIA_API_KEY`/`NVIDIA_API_KEYS`, `NVIDIA_PARSE_MODEL`, `NVIDIA_STRUCTURE_MODEL`, `NVIDIA_PARSE_ENDPOINT`, `NVIDIA_STRUCTURE_ENDPOINT`, retries/timeouts/backoff, `NVIDIA_RENDER_DPI`, `LITE_STAGE2_BLOCK_BATCH_SIZE`, `LITE_STAGE2_BLOCK_OVERLAP`.

## Migration and recovery

```bash
python -m scripts.import_legacy_data --root /path/to/legacy-data
```

This explicit import utility reads legacy files and writes records/artifacts to
the configured Turso connection. The old SQLite file backup/restore tools are
for offline legacy archives only; they do not back up or configure the runtime
database. Do not point application instances at local SQLite.

## Render or Docker deployment

Set `DATABASE_BACKEND=turso`, `TURSO_DATABASE_URL`, and `TURSO_AUTH_TOKEN` to
the shared `qp` database in organization `bibhusharma1`. No persistent local
database disk or `DATABASE_PATH` is required. Set `STORAGE_BACKEND=sqlite`, `QUES_STORAGE_ROOT=/data`,
`QUES_JWT_SECRET` (≥32 chars), `QUES_ADMIN_EMAIL`, `QUES_ADMIN_PASSWORD`, `PORT=8000`,
`PARSER_MODE=llm` (recommended on Render, no GPU), `CORS_ORIGINS` (comma-separated frontend
origins), and `QUES_RECONCILE_ENABLED=true`. The root `Dockerfile` runs the Lite
backend; set NVIDIA API credentials via environment. All runtime database reads,
writes, and startup migrations use the configured authenticated Turso connection.

## Related

- [development-guide.md](development-guide.md)
- [architecture.md](architecture.md)
- [jobs-and-errors.md](jobs-and-errors.md)
