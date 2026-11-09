# API Reference

Base URL (dev): `http://localhost:8000`. All routes except `/health`, `POST /auth/login`,
`POST /auth/totp/verify` require `Authorization: Bearer <token>`. Interactive docs: `GET /docs`.

Auth legend: **A** = any authenticated user; **W** = write-gated user (teachers allowed,
readonly admins refused); **Adm** = admin role; **AdmW** = admin + write gate.

## System

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | public | `{"status":"ok"}` |

## Authentication

`/auth/login`, `/auth/me`, `/auth/access-mode`, `/auth/totp/enroll`, `/auth/totp/enroll/qr`,
`/auth/totp/confirm`, `/auth/totp/verify`, `/auth/totp/status`, `/auth/totp` (disable).
See [authentication-permissions.md](authentication-permissions.md).

## Subjects

`GET /subjects`, `POST /subjects` (AdmW), `PATCH /subjects/{subject}` (AdmW),
`DELETE /subjects/{subject}` (AdmW). Admin rename updates DB references.

## Documents (PDFs alias `/pdfs`, documents alias `/documents`)

`GET /subjects/{s}/pdfs`, `POST /subjects/{s}/pdfs` (W, multipart `files[]`),
`PATCH /subjects/{s}/pdfs/{f}` (AdmW review status), `DELETE /subjects/{s}/pdfs/{f}` (AdmW),
`POST .../process` (W), `GET .../status`, `GET .../result`, `GET /subjects/{s}/processing-status`.

## Jobs

`GET /jobs/{job_id}` (Adm).

## Reconstruction

`POST /subjects/{s}/pdfs/{f}/reconstruct` (W), `GET /subjects/{s}/pdfs/{f}/reconstruction` (A).

## Question Bank (`/api/question-bank`)

`GET` (A, query subject_id required, marks, page, page_size), `GET /{qid}` (A, subject_id),
`GET /{qid}/resources/{figure|table}/{rid}` (A), `POST` (W), `PATCH /{qid}` (W),
`DELETE /{qid}` (W).

## Priorities (`/api/question-priorities`)

`GET` (A), `PUT` (W), `DELETE /{qid}` (W).

## Blueprints (`/blueprints` and `/api/question-paper/blueprints`)

`GET`, `GET /{id}`, `POST` (W), `PUT/PATCH /{id}` (W), `POST /{id}/duplicate` (W),
`DELETE /{id}` (W). Canonical `default-generic` protected.

## Question papers & drafts

`POST /api/question-papers/generate` (W), `GET /api/question-papers/{paper_id}/docx` (A),
`GET /api/question-papers/history` (A), `GET /api/question-papers/history/{paper_id}` (A),
`DELETE /api/question-papers/history/{paper_id}` (W).
Drafts: `POST /api/question-papers/drafts` (W), `GET/PATCH /drafts/{id}`, question
add/edit/remove/reorder, `POST /drafts/{id}/docx`.

## Presets

`/api/basic-configurations/*` (A read, W write); `/api/ui-configurations/*` (A read, W write).

## Dashboard & reconciliation (admin)

`GET /api/dashboard/metrics` (Adm), `POST /api/reconciliation/run` (AdmW),
`GET /api/reconciliation/status` (Adm).

## Teachers (`/teachers`)

`GET` (Adm), `POST` (AdmW), `PATCH /{id}` (AdmW), `DELETE /{id}` (AdmW),
`GET /{id}/subjects` (Adm), `POST /{id}/subjects` (AdmW), `DELETE /{id}/subjects/{subject}` (AdmW).

## Legacy DB (`/questions`, `/subjects/{s}/topics`, `/papers`, `/subjects/{s}/weightage`, import)

Admin-gated CRUD on legacy `questions`/`topics`; `POST /subjects/{s}/pdfs/{f}/questions/import`
(AdmW) flattens parsed bank into SQLite `questions`.

## Full shapes

Complete method/auth/body/query/response tables and status codes:
`BACKEND_ARCHITECTURE.md` §15. Custom error contract: `BACKEND_ARCHITECTURE.md` §15.17.

## Related

- [authentication-permissions.md](authentication-permissions.md)
- [database.md](database.md)
- [complete-workflow.md](complete-workflow.md)
