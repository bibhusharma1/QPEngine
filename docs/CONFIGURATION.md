# Configuration

The backend uses one local SQLite database for structured data and binary
assets. FastAPI remains the only frontend interface; database credentials and
connections are server-side.

## Application

- `APP_ENV=local` is the only supported runtime environment.
- `DATABASE_BACKEND=sqlite` and `STORAGE_BACKEND=sqlite` are required.
  `STORAGE_BACKEND=local` is accepted as a compatibility alias but still uses
  SQLite BLOB storage. Turso and object-storage connections are disabled.
- `DATABASE_PATH` (preferred) or `QUES_DATABASE_PATH` selects the one database
  file. The default is `~/.local/share/masterdeorca/masterdeorca.sqlite`.
- `QUES_STORAGE_ROOT` is retained for legacy import/CLI compatibility and is
  not used as the runtime document store.
- `QUES_DEFAULT_SUBJECT=OperatingSys`, `QUES_MAX_UPLOAD_MB=50`, and
  `QUES_PORT=8000`.
- `QUES_JWT_SECRET` (at least 32 characters), `QUES_JWT_TTL_MINUTES`,
  `QUES_ADMIN_EMAIL`, `QUES_ADMIN_PASSWORD`, `QUES_CORS_ORIGINS`.
- `QUES_TOTP_ENCRYPTION_KEY`, `QUES_TOTP_ISSUER`,
  `QUES_TOTP_CHALLENGE_TTL_MINUTES`, `QUES_TOTP_FAILED_ATTEMPT_THRESHOLD`,
  and `QUES_TOTP_COOLDOWN_SECONDS`.
- `QUES_RECONCILE_ENABLED=true|false` (default `true`) and
  `QUES_RECONCILE_INTERVAL_HOURS=12|24` (default `12`). The scheduler does not
  execute immediately at startup.

## NVIDIA Lite processing

- `PARSER_MODE=llm` is the default and selects the two-stage NVIDIA pipeline.
- `NVIDIA_API`, `NVIDIA_API_KEY`, and `NVIDIA_API_KEYS` are supported
  authentication variables.
- `NVIDIA_PARSE_MODEL`, `NVIDIA_STRUCTURE_MODEL`,
  `NVIDIA_PARSE_ENDPOINT`, and `NVIDIA_STRUCTURE_ENDPOINT` select the models
  and supported API endpoints.
- `NVIDIA_STRUCTURE_RETRIES`, `NVIDIA_STRUCTURE_TIMEOUT`,
  `NVIDIA_STRUCTURE_BACKOFF_BASE`, and `NVIDIA_STRUCTURE_MAX_TOKENS` bound
  structuring retries and response size.
- `NVIDIA_API_TIMEOUT`, `NVIDIA_API_RETRIES`, `NVIDIA_API_CONCURRENCY`,
  `NVIDIA_API_BACKOFF_BASE`, `NVIDIA_RENDER_DPI`,
  `LITE_STAGE2_BLOCK_BATCH_SIZE`, and `LITE_STAGE2_BLOCK_OVERLAP` configure
  extraction and block processing.

The legacy Local parser implementation remains in the repository but is
disabled by the active processing service. Gemini, Groq, Mistral, Cerebras,
Turso, and object-storage credentials are not required or initialized.

## Migration and recovery

Migration copies preserve the source database and files. Run the orchestrator
against the root containing subject folders and shared configuration:

```bash
python -m scripts.migrate_data --storage-root /path/to/legacy-data \
  --database "$DATABASE_PATH" \
  --source-database /path/to/legacy/masterdeorca.sqlite
python -m scripts.validate_data --database "$DATABASE_PATH" --storage-root /path/to/legacy-data
python -m scripts.backup_data
python -m scripts.restore_backup /path/to/backup/database/masterdeorca.sqlite \
  --database "$DATABASE_PATH"
```

Omit `--source-database` when there is no separate legacy SQLite database.
The source-database option copies into a new destination only; it records an
import marker for safe repeat runs and refuses to merge into an already
populated destination without that marker. JSON, document bytes, parser
outputs, figures and generated DOCX content are imported idempotently into
SQLite, while original files remain untouched.

`restore_backup` refuses to replace an existing database unless `--replace` is
provided. Stop the backend before restoring a database it may be using.

`scripts.sync_sqlite_to_turso` is a future-integration scaffold. It is not
invoked by the application and has not been verified against a live Turso
database; do not treat it as a supported cloud backend.
