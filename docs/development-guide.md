# Development Guide

## Prerequisites

- Python 3.11+
- `uv` or `pip`
- Docker (optional) for containerized runs
- NVIDIA API key(s) only if using `PARSER_MODE=llm`

## Setup

```bash
cp .env.example .env
pip install -e ".[dev]"  # or: uv sync
uvicorn src.masterdeorca.app:app --reload --host 0.0.0.0 --port 8000
```

Swagger UI: `http://localhost:8000/docs`.

## Local authentication

There is no default account and startup does not seed an administrator. For isolated local development, configure an untracked `.env` with `APP_ENV=local`, `DATABASE_BACKEND=sqlite`, local `DATABASE_PATH` and `QUES_STORAGE_ROOT` paths, a private `QUES_JWT_SECRET`, and explicit `QUES_ADMIN_EMAIL` / `QUES_ADMIN_PASSWORD` values. Then run `python scripts/create_local_admin.py` once. The command is restricted to local SQLite, uses the application password hasher, and refuses to overwrite an existing account. Render deployments must use `APP_ENV=render` with Turso; local SQLite configuration is rejected there.

## Running tests

- CI: `uv run pytest tests/Parser tests/Reconstruct tests/Scrapper` (80 passed).
- Full local run: 231 passed, 2 failed, 1 skipped.
  - The 2 failures (`tests/document_intelligence/test_lite_process_execution.py`) are stale
    integration tests written for the earlier per-question structuring flow; current pipeline uses
    batched classification + deterministic assembly (covered by passing `test_lite_two_stage.py`).
  - 1 skip is a DOCX rasterization test requiring LibreOffice.
  - `tests/Scrapper/test_hardware_gpu.py` is hardware-marked.
- `tests/document_intelligence/` and `tests/test_*.py` cover auth, subjects, uploads, jobs, banks,
  papers, reconciliation, dashboard via `TestClient`.

## Coding notes

- Do not modify application logic for documentation work.
- All JSON writes in services layer are atomic (temp file + fsync + os.replace) with thread
  RLock + fcntl.flock lock files for cross-process safety.
- Corrupt JSON stores are preserved as `*.corrupt-<timestamp>` and rebuilt.
- Do not expose secrets; `.env` is git-ignored.

## Known frontend/backend mismatches (from integration test report)

- `/teachers/{teacher_id}/subjects` supports GET/POST/DELETE (no PUT/PATCH).
  Frontend calling PUT `/teachers/{id}/subjects` will not match backend routes;
  use POST (replace set) or DELETE per subject instead.

## Related

- [deployment.md](deployment.md)
- [architecture.md](architecture.md)
- [jobs-and-errors.md](jobs-and-errors.md)
