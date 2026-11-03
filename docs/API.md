# API

The complete, code-verified endpoint inventory — methods, auth, query/body
parameters, response shapes and status codes — is in
[BACKEND_ARCHITECTURE.md §15](BACKEND_ARCHITECTURE.md#15-complete-api-endpoint-reference).

Interactive OpenAPI docs: `GET /docs` on the running server.

## Endpoint groups (quick index)

- **System**: `GET /health`
- **Authentication**: `/auth/login`, `/auth/me`, `/auth/access-mode`, `/auth/totp/*`
- **Subjects**: `/subjects`, `/subjects/{subject}` (CRUD), `/subjects/{subject}/processing-status`
- **Documents**: `/subjects/{s}/pdfs` (alias `/documents`), upload/process/status/result/review/delete
- **Jobs**: `GET /jobs/{job_id}`
- **Reconstruction**: `POST .../reconstruct`, `GET .../reconstruction`
- **Question Bank**: `/api/question-bank`, `/api/question-bank/{id}`, `/api/question-bank/{id}/resources/{type}/{rid}`
- **Priorities**: `/api/question-priorities`
- **Blueprints**: `/blueprints` (alias `/api/question-paper/blueprints`)
- **Papers & drafts**: `/api/question-papers/generate`, `/api/question-papers/history`, `/api/question-papers/drafts`
- **Presets**: `/api/basic-configurations`, `/api/ui-configurations`
- **Dashboard & reconciliation**: `/api/dashboard/metrics`, `/api/reconciliation/run`, `/api/reconciliation/status`
- **Teachers/RBAC**: `/teachers`, `/teachers/{id}/subjects`
- **Legacy DB**: `/questions`, `/subjects/{s}/topics`, `/papers`, `/subjects/{s}/weightage`, `.../questions/import`

Document intelligence (Lite/Nemotron) is reached through the common upload +
process + result endpoints; `PARSER_MODE` selects the pipeline server-side.
