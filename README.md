# MasterDeOrca

FastAPI backend that turns examination-paper documents (PDF, DOCX, PNG, JPG/JPEG) into a
validated, editable Question Bank and generates question-paper DOCX files from that bank.

## Technologies

Python 3.11+, FastAPI, uvicorn, Turso/libSQL, PyMuPDF, python-docx, Pydantic v2, pwdlib (Argon2),
PyJWT, pyotp, cryptography, httpx. Optional: LibreOffice (DOCX rasterization), PaddleOCR GPU
(local mode).

## Architecture (short)

Single FastAPI process hosts routes, background jobs, and a reconciliation worker thread.
Document intelligence runs in two modes selected by `PARSER_MODE`: `llm` (active NVIDIA Lite
two-stage parser: Nemotron 3.5 Parse + Nemotron Super VL + deterministic assembly) and `local`
(legacy PyMuPDF/GPU-OCR/LangGraph pipeline). Turso Cloud is the single source of truth for
Question Banks, administration, jobs, RBAC, TOTP state, documents and stored artifacts.

See [docs/architecture.md](docs/architecture.md).

## Installation and running

Dependencies are declared in `pyproject.toml` (single source of truth; no
`requirements.txt`). Python 3.11+ required.

### 1. Configure environment

```bash
cp .env.example .env
# edit .env: set QUES_JWT_SECRET (>=32 chars), QUES_ADMIN_EMAIL/PASSWORD, NVIDIA_API (if PARSER_MODE=llm), etc.
```

### 2a. With uv (recommended)

Uses the existing `uv.lock` for reproducible installs.

```bash
uv sync                      # installs project + dev deps into .venv
uv run uvicorn src.masterdeorca.app:app --reload --host 0.0.0.0 --port 8000
```

Optional extras:

```bash
uv sync --extra ocr-gpu     # local GPU OCR (Linux, Python <3.12)
```

### 2b. With pip

Install directly from `pyproject.toml` (no `requirements.txt` needed):

```bash
python -m venv .venv
source .venv/bin/activate

# Runtime dependencies only:
python -m pip install .

# Runtime + development dependencies:
python -m pip install -e ".[dev]"

# Optional extras:
python -m pip install -e ".[ocr-gpu]"   # local GPU OCR (Linux, Python <3.12)

# Run backend
uvicorn src.masterdeorca.app:app --reload --host 0.0.0.0 --port 8000
```

How it works: pip reads `pyproject.toml` (`[project]` for metadata/runtime deps,
`[project.optional-dependencies]` for `dev`/`ocr-gpu`/`turso`, and `[build-system]`
for the build backend), then builds/installs the project and its declared
dependencies. Keep `pyproject.toml` as the single source of truth; use `uv.lock`
for reproducible uv installs and `pip install -e ".[dev]"` for pip installs.

### 2c. With Docker

Build and run the existing image (Lite/NVIDIA mode by default):

```bash
docker build -t masterdeorca .
docker run -p 8000:8000 --env-file .env masterdeorca
```

Or with compose profiles:

```bash
docker compose --profile full build && docker compose --profile full up -d   # full (local parser + GPU)
docker compose --profile lite build && docker compose --profile lite up -d   # lite (NVIDIA API only)
```

Swagger UI: `http://localhost:8000/docs`.

Processing modes (set `PARSER_MODE` in `.env`):
- `local` — legacy local Scrapper/Parser (GPU optional).
- `llm` — NVIDIA Lite parser (no local OCR/GPU deps).

## Environment configuration

All settings come from `.env` / environment variables. See `.env.example` and
[docs/deployment.md](docs/deployment.md). Never commit secrets.

## Documentation

- [Architecture](docs/architecture.md)
- [Backend structure](docs/backend-structure.md)
- [API reference](docs/api-reference.md)
- [Database](docs/database.md)
- [Authentication & permissions](docs/authentication-permissions.md)
- [Document processing](docs/document-processing.md)
- [Question extraction](docs/question-extraction.md)
- [AI models](docs/ai-models.md)
- [Jobs & errors](docs/jobs-and-errors.md)
- [Deployment](docs/deployment.md)
- [Development guide](docs/development-guide.md)
- [Complete workflow](docs/complete-workflow.md)
- Deep reference: [BACKEND_ARCHITECTURE.md](docs/BACKEND_ARCHITECTURE.md)
- UI companion: [for_ui_developers.md](for_ui_developers.md)
