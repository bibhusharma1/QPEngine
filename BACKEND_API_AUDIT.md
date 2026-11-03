# MasterDeOrca — Complete Backend API Audit

**Audit date:** 2026-11-03  
**Scope:** Backend API endpoints, implementation, database interactions, dependencies, and deployment/config  
**Constraints:** Read-only; no code/schema changes, no data modifications, no triggers of processing/generation

---

## 1) Endpoint inventory (from OpenAPI + source)
### api
- DELETE /api/basic-configurations/{configuration_id}
- DELETE /api/question-bank/{question_id}
- DELETE /api/question-paper/blueprints/{blueprint_id}
- DELETE /api/question-papers/drafts/{draft_id}/questions/{question_id}
- DELETE /api/question-papers/history/{paper_id}
- DELETE /api/question-priorities/{question_id}
- DELETE /api/ui-configurations/{configuration_id}
- GET /api/basic-configurations
- GET /api/basic-configurations/default
- GET /api/basic-configurations/{configuration_id}
- GET /api/dashboard/metrics
- GET /api/question-bank
- GET /api/question-bank/{question_id}
- GET /api/question-bank/{question_id}/resources/{content_type}/{resource_id}
- GET /api/question-paper/blueprints
- GET /api/question-paper/blueprints/{blueprint_id}
- GET /api/question-papers/drafts/{draft_id}
- GET /api/question-papers/history
- GET /api/question-papers/history/{paper_id}
- GET /api/question-papers/{paper_id}/docx
- GET /api/question-priorities
- GET /api/reconciliation/status
- GET /api/ui-configurations
- GET /api/ui-configurations/{configuration_id}
- PATCH /api/basic-configurations/{configuration_id}
- PATCH /api/question-bank/{question_id}
- PATCH /api/question-paper/blueprints/{blueprint_id}
- PATCH /api/question-papers/drafts/{draft_id}
- PATCH /api/question-papers/drafts/{draft_id}/questions/{question_id}
- PATCH /api/ui-configurations/{configuration_id}
- POST /api/basic-configurations
- POST /api/basic-configurations/{configuration_id}/duplicate
- POST /api/basic-configurations/{configuration_id}/set-default
- POST /api/question-bank
- POST /api/question-paper/blueprints
- POST /api/question-paper/blueprints/{blueprint_id}/duplicate
- POST /api/question-papers/drafts
- POST /api/question-papers/drafts/generate
- POST /api/question-papers/drafts/{draft_id}/docx
- POST /api/question-papers/drafts/{draft_id}/questions
- POST /api/question-papers/generate
- POST /api/reconciliation/run
- POST /api/ui-configurations
- PUT /api/basic-configurations/{configuration_id}
- PUT /api/question-paper/blueprints/{blueprint_id}
- PUT /api/question-papers/drafts/{draft_id}/question-order
- PUT /api/question-priorities

### auth
- DELETE /auth/totp
- GET /auth/access-mode
- GET /auth/me
- GET /auth/totp/enroll/qr
- GET /auth/totp/status
- POST /auth/login
- POST /auth/totp/confirm
- POST /auth/totp/enroll
- POST /auth/totp/verify
- PUT /auth/access-mode

### blueprints
- DELETE /blueprints/{blueprint_id}
- GET /blueprints
- GET /blueprints/{blueprint_id}
- PATCH /blueprints/{blueprint_id}
- POST /blueprints
- POST /blueprints/{blueprint_id}/duplicate
- PUT /blueprints/{blueprint_id}

### health
- GET /health

### jobs
- GET /jobs/{job_id}

### papers
- GET /papers

### questions
- DELETE /questions/{question_id}
- GET /questions
- PATCH /questions/{question_id}
- POST /questions

### subjects
- DELETE /subjects/{subject}
- DELETE /subjects/{subject}/pdfs/{filename}
- GET /subjects
- GET /subjects/{subject}/pdfs
- GET /subjects/{subject}/pdfs/{filename}/reconstruction
- GET /subjects/{subject}/pdfs/{filename}/result
- GET /subjects/{subject}/pdfs/{filename}/status
- GET /subjects/{subject}/processing-status
- GET /subjects/{subject}/topics
- GET /subjects/{subject}/weightage
- PATCH /subjects/{subject}
- PATCH /subjects/{subject}/pdfs/{filename}
- POST /subjects
- POST /subjects/{subject}/documents
- POST /subjects/{subject}/documents/{filename}/process
- POST /subjects/{subject}/pdfs
- POST /subjects/{subject}/pdfs/{filename}/process
- POST /subjects/{subject}/pdfs/{filename}/questions/import
- POST /subjects/{subject}/pdfs/{filename}/reconstruct
- POST /subjects/{subject}/topics

### teachers
- DELETE /teachers/{teacher_id}
- DELETE /teachers/{teacher_id}/subjects/{subject}
- GET /teachers
- GET /teachers/{teacher_id}/subjects
- PATCH /teachers/{teacher_id}
- POST /teachers
- POST /teachers/{teacher_id}/subjects

### topics
- DELETE /topics/{topic_id}
- PATCH /topics/{topic_id}

### users
- DELETE /users/{user_id}/subjects/{subject}
- GET /users
- GET /users/{user_id}/subjects
- POST /users/{user_id}/subjects
- PUT /users/{user_id}/subjects/{subject}/permissions


---

## 2) Implementation details (source-based)
### Core files
- `src/masterdeorca/app.py` - FastAPI app, all routes, auth checks, validation
- `src/masterdeorca/database.py` - DB access, schema init, subject/assignments/permissions
- `src/masterdeorca/security.py` - JWT, RBAC, subject access
- `src/masterdeorca/migrations.py` - Schema migrations
- `src/masterdeorca/storage.py` - Path validation
- `src/masterdeorca/services/*` - Business logic
- `src/masterdeorca/reconciliation/worker.py` - Background reconciliation

### Authentication
- JWT (HS256) via `PyJWT`, tokens issued by `issue_token()`, validated by `require_user()`. 
- Admin checks via `require_admin()`, write checks via `require_admin_write()`/`require_write_user()` (admin readonly enforced).
- TOTP endpoints present.

### Subjects
- Subject names are user-facing identifiers (validated by regex). `subjects` table has `subject_id` (deterministic UUID) + `subject_name` UNIQUE. Internal IDs used for FKs where needed.
- `ensure_subject_record()` creates/resolves subject by name.

### Assignments/Permissions
- `teacher_subjects` (legacy), `user_subject_assignments`, `user_subject_permissions` added. APIs for users/teachers assignment management exist.

### Processing/Storage
- Documents stored as BLOBs in `documents.content_blob`; artifacts in `stored_artifacts.data` (BLOB). JSON in TEXT fields. Jobs tracked in `jobs`. Lite mode uses NVIDIA API; local mode uses local pipeline.

---

## 3) Database (Turso/libSQL)
- Backend: Turso via `libsql` client. Connection in `database._get_connection()`/`connect()`. Row handling made compatible with tuple/dict/Row.
- Schema created by `database.initialize()` + `migrations.run_migrations()`. 31 tables present (verified). Migrations idempotent.
- Foreign keys enabled where possible.

---

## 4) Deployment & Configuration
- Dockerfile: uses uv, venv at `/app/.venv`, CMD runs `python -m uvicorn` with `--host 0.0.0.0 --port ${PORT:-8000}`. Lite mode default.
- Dependencies: `pyproject.toml` includes `uvicorn[standard]`, `libsql>=0.1.11`, etc.
- Env vars: Turso (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`), JWT, NVIDIA keys, parser mode.
- CORS configured.

---

## 5) Issues & Priorities

### Critical
- (Turso) Ensure all rows accessed with compatible access pattern (fixed for key paths)
- Container startup can hang in some environments waiting on connections

### High
- Some legacy code paths may assume sqlite3.Row; need to verify all usages

### Medium
- Docker/local verification blocked by daemon in some environments

### Low
- Minor cleanup opportunities

**Note:** This audit is code/OpenAPI-based. Live behavior verified for core endpoints (health/docs/openapi/auth/CORS). Full functional testing of protected write operations not performed per constraints.
