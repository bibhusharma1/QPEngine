# Architecture

High-level overview. The authoritative, code-verified backend reference —
including the active NVIDIA Lite parser, all API endpoints, persistence
inventory, and the planned Turso transition — is
[BACKEND_ARCHITECTURE.md](BACKEND_ARCHITECTURE.md).

## Overview

```mermaid
graph TD
  A[Client/UI] --> B[FastAPI App src/masterdeorca/app.py]
  B --> C[Auth JWT + TOTP + RBAC]
  B --> D[Services: jobs, question bank, papers, configs]
  D --> E{Document Intelligence}
  E -->|PARSER_MODE=llm| F[Lite two-stage: Nemotron Parse + Super VL classification + deterministic assembly]
  E -->|PARSER_MODE=local| G[Legacy local Scrapper/Parser - PyMuPDF + GPU PaddleOCR + LangGraph providers]
  F --> H[Shared Question Bank Pydantic schema 1.1.0]
  G --> H
  H --> I[Validation / Reconciliation / Dedup - deterministic only]
  I --> J[Storage: per-subject JSON banks, manifests, figure assets]
  B --> K[SQLite: users, RBAC, jobs, reviews, reconciliation state]
```

## Processing modes

```mermaid
graph LR
  A[Input File PDF/DOCX/PNG/JPG] --> B{PARSER_MODE}
  B -->|llm| C[Lite: render page images]
  C --> D[Nemotron 3.5 Parse per page]
  D --> E[Normalize + crop figure assets]
  E --> F[Nemotron Super VL block classification]
  F --> G[Deterministic Python question assembly]
  G --> H[Pydantic validation -> questions_parsed.json]
  B -->|local| I[Legacy Scrapper + LangGraph Parser]
  I --> H
  H --> J[Question Bank sync -> reconstruction/generation]
```

- **Lite mode (`PARSER_MODE=llm`)** is the active NVIDIA parser: no local GPU,
  no OCR, no Scrapper stage. See
  [BACKEND_ARCHITECTURE.md §5](BACKEND_ARCHITECTURE.md#5-active-parsing-pipeline--nvidia-lite-two-stage-parser_modelllm).
- **Local mode (`PARSER_MODE=local`, the settings default)** keeps the legacy
  PyMuPDF/GPU-OCR/LangGraph pipeline operational; it is excluded from the
  active parser description but its code is unchanged.

## Key facts

- Single FastAPI process hosts routes, background jobs, and the reconciliation
  worker thread.
- Persistent Question Banks are per-subject JSON files; administration, jobs,
  RBAC and TOTP state live in SQLite.
- PDFs/figures remain files; only references are stored.
- Localhost is the current development configuration (port 8000, CORS for
  localhost:3000/5173); Turso migration is planned, not implemented.
