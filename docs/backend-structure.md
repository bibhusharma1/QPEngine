# Backend Structure

## Repository layout

```
MasterDeOrca/
├── config/                # settings, outputs, parser/scrapper/reconstruct config
├── functions/             # legacy local-mode pipelines (Scrapper/Parser/Reconstruct)
├── src/masterdeorca/
│   ├── app.py             # FastAPI app + all routes + Pydantic models
│   ├── database.py        # authenticated Turso connection + schema helpers
│   ├── migrations.py      # idempotent startup migrations
│   ├── security.py        # JWT, RBAC, subject access
│   ├── storage.py         # subject/document name + path validation
│   ├── processing_manifest.py
│   ├── services/          # pipeline, question_bank, papers, priorities, configs, metrics
│   ├── reconciliation/    # worker + validation
│   ├── functions/totp/    # TOTP service, crypto, QR, schemas
│   └── document_intelligence/
│       ├── lite/          # ACTIVE NVIDIA two-stage parser
│       ├── llm/           # Nemotron clients + dormant adapters
│       ├── page_images.py # ACTIVE page rendering
│       └── interfaces/    # dataclasses
├── tests/
├── docs/
├── Dockerfile* / compose*.yaml
├── pyproject.toml
└── .env / .env.example
```

## Active vs dormant modules

| Module | Status |
|---|---|
| `document_intelligence/lite/two_stage.py` (`LiteTwoStageProcessor`) | ACTIVE |
| `document_intelligence/lite/structurer.py` (`classify_blocks`) | ACTIVE |
| `document_intelligence/lite/figure_assets.py` | ACTIVE |
| `document_intelligence/page_images.py` | ACTIVE |
| `services/pipeline.py` | ACTIVE (job orchestration) |
| `services/question_bank.py` | ACTIVE |
| `reconciliation/worker.py`, `validation.py` | ACTIVE |
| `document_intelligence/lite/structurer.py` (`structure()`) | dormant |
| `document_intelligence/llm/scraper_parser/{nemotron_adapter,structurer,normalizer}.py` | dormant |
| `document_intelligence/routing/`, `pipeline.py` (wrapper) | dormant |
| `document_intelligence/local/`, `extraction/`, `figure_extraction/`, `normalization/` | empty placeholders |

## Entry points

- API server: `uvicorn src.masterdeorca.app:app --host 0.0.0.0 --port 8000`
  (or `python -m src.masterdeorca`, `QUES_PORT`).
- Application object: `src/masterdeorca/app.py` creates `FastAPI(..., lifespan=lifespan)`.
- Startup: `_startup()` validates secrets, `database.initialize()`,
  `database.mark_interrupted_jobs()`, ensures default subject, starts reconciliation
  scheduler.

## Configuration

Single source of truth: `config/settings.py` (loads `.env` via python-dotenv,
`override=False`). All paths resolve relative to `PROJECT_ROOT`. See
[deployment.md](deployment.md) and `.env.example`.

## Storage layout (subject-scoped)

```
<STORAGE_ROOT>/<Subject>/
├── source/                 # uploaded originals (never modified)
├── output/Scrapper/<stem>/ # local mode only
├── output/Parser/<stem>/   # questions_parsed.json + lite caches + assets/figures
├── output/Reconstruct/<stem>/ # questions_reconstructed.docx
├── manifests/processing_status.json
├── logs/<stem>.log
├── metadata/               # compatibility summaries/projections only
└── question_bank.json      # compatibility projection; Turso is canonical
<STORAGE_ROOT>/.masterdeorca/ # legacy/local projections; Turso is canonical
```

## Database

Turso Cloud at `TURSO_DATABASE_URL` (the shared `qp` database in organization
`bibhusharma1`). SQLite is not a runtime fallback. Tables and operations:
[database.md](database.md).

## Related

- [architecture.md](architecture.md)
- [api-reference.md](api-reference.md)
- [complete-workflow.md](complete-workflow.md)
