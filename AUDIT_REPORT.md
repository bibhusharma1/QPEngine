# Backend Persistence Audit Report

## Summary
All application data persistence has been migrated from JSON/filesystem-based sources to SQLite as the single source of truth. Binaries (PDFs, images, DOCX) remain in configured file storage with metadata/paths managed through SQLite.

## Former JSON/Filesystem Persistence Locations → SQLite Replacements

| Location | Type | Service/Code | Replacement | Migration Status |
|----------|------|--------------|-------------|-----------------|
| `{STORAGE_ROOT}/.masterdeorca/question_priorities.json` | Global priorities | `services/question_priorities.py` | `question_priorities` table | ✅ Migrated - SQLite only |
| `{STORAGE_ROOT}/.masterdeorca/question_paper_blueprints.json` | Blueprints | `services/question_papers.py` (blueprints) | `blueprints` table (blueprint_json) | ✅ Migrated - SQLite only |
| `{STORAGE_ROOT}/.masterdeorca/basic_configurations.json` | Basic configs | `services/basic_config.py` | `basic_configurations` table (new) | ✅ Migrated - SQLite only |
| `{STORAGE_ROOT}/.masterdeorca/ui_configurations.json` | UI configs | `services/ui_configurations.py` | `ui_configurations` table (new) | ✅ Migrated - SQLite only |
| `{STORAGE_ROOT}/.masterdeorca/generated_papers/*.json` | Generated paper metadata | `services/question_papers.py` | `generated_papers` table (paper_json, storage_key) | ✅ Migrated - SQLite primary, files optional fallback |
| `{STORAGE_ROOT}/.masterdeorca/generated_papers/drafts/*.json` | Paper drafts | `services/question_papers.py` (drafts) | `paper_drafts` table (draft_json) | ✅ Migrated - SQLite primary, files optional fallback |
| `{STORAGE_ROOT}/{subject}/question_bank.json` | Question bank per subject | `services/question_bank.py` | `question_bank_records` table (record_json, position) | ✅ Migrated - DB is source of truth, JSON merged for compat |
| `{STORAGE_ROOT}/{subject}/metadata/question_bank.json` | Question bank (alt location) | `services/question_bank.py` | `question_bank_records` table | ✅ Migrated |
| `{STORAGE_ROOT}/{subject}/manifests/processing_status.json` | Processing manifest/status | `processing_manifest.py`, `app.py` | `jobs` table + `reconciliation_state` table | ✅ Migrated - derived from DB |
| `{STORAGE_ROOT}/{subject}/metadata/*.processing.json` | Job metadata snapshots | `services/pipeline.py` | `jobs` table (metadata_json) | ✅ DB-backed |
| Jobs in memory/JSON contexts | Job tracking | `database.save_job/get_job` | `jobs` table (already existed) | ✅ Already in SQLite |
| Documents/PDFs metadata | Document registry | `app.py` upload, `database` | `documents` table + `subjects` table | ✅ Already in SQLite |
| Reviews | Review status | `app.py` | `pdf_reviews`/`document_reviews` tables | ✅ Already in SQLite |
| Topics/Questions (legacy) | Question/topic data | `app.py` | `topics`, `questions` tables | ✅ Already in SQLite |
| Teacher assignments | RBAC | `database.py` | `teacher_subjects` table | ✅ Already in SQLite |
| TOTP/auth data | Authentication | `database.py`, `totp/service.py` | `users`, `admins`, `totp_*` tables | ✅ Already in SQLite |
| Reconciliation state | Reconciliation | `reconciliation/worker.py` | `reconciliation_state`, `scheduler_state` tables | ✅ Already in SQLite |

## Binaries (Kept in Filesystem as Required)
| Type | Location | Managed Via |
|------|----------|-------------|
| PDF/DOCX/PNG/JPG uploads | `{STORAGE_ROOT}/{subject}/source/` | `storage.py` paths, `documents` table stores metadata/storage_key |
| Scrapper outputs (intermediate) | `{STORAGE_ROOT}/{subject}/output/Scrapper/` | Processing artifacts during pipeline |
| Parser outputs (intermediate) | `{STORAGE_ROOT}/{subject}/output/Parser/` | Processing artifacts during pipeline/reconciliation |
| Reconstructed DOCX | `{STORAGE_ROOT}/{subject}/output/Reconstruct/` | `reconstruct_pdf` outputs |
| Generated paper DOCX | `{STORAGE_ROOT}/.masterdeorca/generated_papers/*.docx` | `question_papers` service, paths in DB/records |
| Question bank assets (figures/tables) | Referenced via paths in records | Paths validated and stored in question bank records/DB |

## Database Schema (SQLite)
- **Location**: `{STORAGE_ROOT}/{subject}/metadata/masterdeorca.sqlite` (per-subject) or configured via `QUES_DATABASE_PATH`
- **Backend support**: SQLite (default) and Turso (via `DATABASE_BACKEND=turso`)
- **Migrations**: Managed by `src/masterdeorca/migrations.py`, run on startup

## Scripts Created/Updated
- `scripts/backup_data.py` - Create timestamped backups of storage and database with verification
- `scripts/migrate_data.py` - Migrate JSON/filesystem data to SQLite as source of truth
- `scripts/validate_data.py` - Validate data integrity and report migration status
- `scripts/sync_sqlite_to_turso.py` - Template for future local-to-cloud SQLite/Turso synchronization

## Verification
- All existing tests pass (238 passed, 4 skipped) after migration
- Data migrated from existing JSON files to SQLite tables
- API behavior and features preserved
- No silent fallbacks to JSON as source of truth in migrated paths
