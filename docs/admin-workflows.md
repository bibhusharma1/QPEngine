# Admin workflow API

All routes below require the existing `Authorization: Bearer <token>` header.
`/auth/me`, question/topic/paper reads, and weightage reads allow any active
account. Teacher administration and all writes require the admin role. Errors
use FastAPI's `{"detail": ...}` response shape.

## Identity and teachers

- `GET /auth/me` resolves `id`, `email`, `name`, `role`, and `status` from the
  persisted account. The JWT remains role-free; role changes take effect from
  the database on the next request.
- `GET /teachers?status=&search=&page=&page_size=` lists teachers.
- `POST /teachers` creates a teacher (`name`, `email`, `department`, optional
  `password`, optional `status`; status defaults to `pending`).
- `PATCH /teachers/{teacher_id}` updates fields or changes approval status.
- `DELETE /teachers/{teacher_id}` removes the teacher account.

## Questions and syllabus topics

- `GET /api/question-bank?subject_id=&marks=&page=&page_size=` returns the
  subject's persistent Question Bank records from
  `{subject_root}/question_bank.json` (synced from parsed PDFs whose processing
  succeeded or completed with errors; manual records live there too). Nested
  subquestions are returned as individual leaf items (with their parent number
  prefixed to the label), so parents with children are not counted again as
  separate records. Missing or invalid bank files are handled per the bank
  service contract. `subject_id` is the existing subject directory name.
- `GET /questions?subject=&status=&difficulty=&search=&page=&page_size=` is
  available to authenticated users; CRUD writes at `/questions` and
  `/questions/{question_id}` are admin-only.
- `GET|POST /subjects/{subject}/topics` lists or creates topics. Admin-only
  `PATCH|DELETE /topics/{topic_id}` edits or removes them.
- `POST /subjects/{subject}/pdfs/{filename}/questions/import` imports parsed
  question text as pending `pyq:<filename>` records. `{"overwrite": true}`
  replaces earlier records from that PDF; duplicate `(source, text)` entries
  are skipped.

## PDF and subject curation

- `GET /subjects/{subject}/pdfs` includes `review_status` (defaults to
  `unreviewed`). Admin-only `PATCH /subjects/{subject}/pdfs/{filename}` accepts
  `review_status: unreviewed|approved|disapproved`.
- `GET /subjects/{subject}/processing-status` returns the persistent per-PDF
  manifest at `{subject_root}/manifests/processing_status.json`. Entries are
  created when PDFs are uploaded or discovered and updated as jobs move through
  queued, running, succeeded, and failed states. Artifact paths are relative to
  the subject root; PDFs without processing history are `not_processed`.
- Admin-only `DELETE /subjects/{subject}/pdfs/{filename}` removes the source PDF,
  stage output directories, processing jobs, review metadata, and imported
  question records. A running job blocks deletion.
- Admin-only `PATCH /subjects/{subject}` renames the directory and updates
  question, topic, job, PDF review, and paper subject references.
- Admin-only `DELETE /subjects/{subject}` removes its directory and associated
  questions, topics, papers, jobs, and review metadata. A running job blocks
  deletion.

## Papers and analytics

- `GET /papers?subject=&page=&page_size=&created_by=me` lists saved paper
  metadata. Paper generation and creation are not part of this API yet.
- `GET /subjects/{subject}/weightage` derives topic counts and marks from
  question-bank rows, including imported PYQ questions. It returns an empty
  topic list when no syllabus topics exist.

The service uses SQLite and creates/migrates its workflow tables at startup.
Teacher accounts without a supplied password receive a generated credential;
the response intentionally does not disclose that credential, so password
reset/invitation delivery remains a separate workflow.
