# MasterDeOrca

MasterDeOrca is a FastAPI-based system for processing question papers (PDF, DOCX, PNG, JPG/JPEG), extracting questions/figures/tables, storing them in a Question Bank, and generating papers/reconstructions.

## Prerequisites
- Python 3.11+
- `uv` or `pip`
- Docker (optional) for containerized runs
- NVIDIA API key(s) only if using `PARSER_MODE=llm`

## Quick Start (Local Development)
1. Copy `.env.example` to `.env` and configure values
2. Install dependencies: `pip install -e ".[dev]"` or `uv sync`
3. Start the API: `uvicorn src.masterdeorca.app:app --reload --host 0.0.0.0 --port 8000`
4. Open Swagger UI: `http://localhost:8000/docs`

## Processing Modes
- `PARSER_MODE=local` - uses existing local Scrapper/Parser (GPU supported where available)
- `PARSER_MODE=llm` - NVIDIA Lite parser: Nemotron 3.5 Parse per-page extraction, Nemotron Super VL batched block classification, then deterministic Python question assembly with Pydantic validation, atomic progress saving and resume support

See `docs/` for full documentation. The authoritative backend reference is
[docs/BACKEND_ARCHITECTURE.md](docs/BACKEND_ARCHITECTURE.md).


## Two Independent Startup Options
- **Local server (GPU/local deps):** `PARSER_MODE=local` - uses existing local Scrapper/Parser. Start: `uvicorn src.masterdeorca.app:app --host 0.0.0.0 --port 8000`
- **Lite server (NVIDIA API):** `PARSER_MODE=llm` - uses Nemotron Parse extraction + Nemotron Super VL block classification with deterministic question assembly; no local OCR/GPU deps. Start: `PARSER_MODE=llm uvicorn src.masterdeorca.app:app --host 0.0.0.0 --port 8000`

Same app/routes/schemas/auth/DB/storage/port 8000. Switch by running the other deployment.
