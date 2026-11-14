# UI developer guide

> **Canonical reference:** [docs/BACKEND_ARCHITECTURE.md](docs/BACKEND_ARCHITECTURE.md)
> contains the code-verified endpoint inventory, schemas, and the active NVIDIA
> Lite parser description. This guide remains the UI-focused companion.
> Corrections versus older notes: uploads accept PDF, DOCX, PNG, JPG and JPEG
> (not only PDFs); `GET /health` always returns `{"status": "ok"}`; reconciliation
> validation is deterministic-only (no LLM review pass).

## Start and authenticate

For isolated development, configure an untracked `.env` with `APP_ENV=local`, `DATABASE_BACKEND=sqlite`, local `DATABASE_PATH` and `QUES_STORAGE_ROOT` paths, a private `QUES_JWT_SECRET`, and explicit `QUES_ADMIN_EMAIL` / `QUES_ADMIN_PASSWORD` values. Run `python scripts/create_local_admin.py` to provision the first local administrator, then start the API. There is no default account. The local base URL is `http://localhost:8000`; interactive OpenAPI docs are at `/docs`.

The default `QUES_CORS_ORIGINS` allows `http://localhost:3000`, `http://localhost:5173`, and `http://127.0.0.1:5173`. Keep the frontend's exact origin in this comma-separated setting; `localhost` and `127.0.0.1` are distinct origins.

Login with `POST /auth/login` and JSON containing the configured account email and password. A successful response contains `access_token` and `token_type: "bearer"`. Send `Authorization: Bearer <access_token>` on every protected endpoint below. The token expires after `QUES_JWT_TTL_MINUTES`.

Success response: `{ "access_token": "<JWT>", "token_type": "bearer" }`. Invalid credentials return 401.

```js
const base = "http://localhost:8000";
const login = await fetch(`${base}/auth/login`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email: configuredEmail, password: configuredPassword })
});
const { access_token } = await login.json();
const headers = { Authorization: `Bearer ${access_token}` };
```

## Subjects and PDFs

`GET /health` is public and returns `{ "status": "ok" }`.

`GET /subjects` returns `{ "subjects": ["OperatingSys"] }`.

`POST /subjects` creates a subject and its folders. Body: `{ "name": "NewSubject" }`. Response (201) includes the folder paths. An existing name returns 409. Names may use letters, numbers, spaces, underscores, and hyphens.

Example responses: `GET /subjects` → `{ "subjects": ["OperatingSys"] }`; `POST /subjects` → `{ "name": "NewSubject", "created": true, "directories": { "source": ".../NewSubject/source", "scrapper": "...", "parser": "...", "reconstruct": "...", "manifests": "...", "logs": "...", "metadata": "..." } }`.

```js
const subjects = await fetch(`${base}/subjects`, { headers });
const created = await fetch(`${base}/subjects`, {
  method: "POST", headers: { ...headers, "Content-Type": "application/json" },
  body: JSON.stringify({ email: configuredEmail, password: configuredPassword })
});
```

## Subject-scoped Question Bank

Select a subject before requesting questions. `GET /api/question-bank` requires
`subject_id`; omitting it returns `422` without scanning other subjects. It
supports exact `marks` filtering and `page` / `page_size` pagination (default
25). Each item includes `question_id`, `text`, `marks`, `priority`,
`subject_id`, source PDF/path/page/section references, and lightweight
`resources.figures` / `resources.tables` arrays. Attachment content is not
included in the listing. Paths are subject-relative identifiers; the API does
not return machine-local absolute paths.
Each item also includes `has_figures` and `has_tables`. These booleans are true
only when the corresponding `resources` array contains an attachment the
backend can actually retrieve. Missing, malformed, unsafe, or unavailable
attachment metadata is not treated as confirmed availability. Use the flags
and arrays to decide whether to show the Figures & Tables action.

The same response includes `available_marks`, derived from that subject's
persisted bank before filtering and pagination. It is a numerically ascending
array of distinct positive JSON numbers with at least one valid question.
Question `marks` are JSON numbers or `null`; numeric strings in storage are
normalized in the API response. Send `marks` as a positive finite number for an
exact match. For example, `marks=2` matches persisted `2`, `2.0`, or `"2.0"`.
Invalid/non-positive filter values return `422`. A valid filter with no matches
returns `200` with an empty `items` array and zero pagination totals. The
response shape is `{ items, pagination: { page, page_size, total_items,
total_pages }, filters: { subject_id, marks }, available_marks }`.

```js
const bank = await fetch(
  `${base}/api/question-bank?subject_id=OperatingSys&marks=4&page=1&page_size=25`,
  { headers }
);
```

`subject_id` is the exact subject directory name and is required. The list
query is `subject_id` (required), `marks` (optional positive finite number),
`page` (positive integer, default `1`), and `page_size` (integer `1`–`200`,
default `25`). Omitting `subject_id` returns `422` with
`{"detail":[{"type":"missing","loc":["query","subject_id"],"msg":"Field required","input":null}]}`;
an unknown subject returns `404`. Invalid query values return `422`. A valid
filter with no records returns `200` and an empty `items` array with zero
pagination totals. `available_marks` contains only distinct valid positive
marks present in the selected subject's bank, regardless of the active marks
filter or page.

Fetch one full record with
`GET /api/question-bank/{question_id}?subject_id={subject}`. The detail response
contains the persisted question fields, normalized `text`, priority, source
references, lightweight attachment references, and the same
`has_figures`/`has_tables` availability booleans. It returns `404` when the
question is not part of the selected subject.

Fetch an individual actual figure or table only when the user requests it:
`GET /api/question-bank/{question_id}/resources/{figure|table}/{resource_id}?subject_id={subject}`.
Figure responses include base64-encoded image bytes and metadata; table
responses include structured headers and rows. The resource ID comes from the
listing/detail attachment metadata. Missing attachments return `404`.

Existing edits remain persistent through
`PATCH /api/question-bank/{question_id}?subject_id={subject}`. Send only fields
to change; for example `{ "original_text": "Updated wording" }`. The stable
question ID and unrelated source/attachment metadata are preserved.

`POST /subjects/{subject}/pdfs` accepts multipart form data with one or more repeated `files` fields. Only valid PDF, DOCX, PNG, JPG and JPEG files up to `QUES_MAX_UPLOAD_MB` (50 MB by default) are accepted. Existing filenames are never overwritten. Response (201) lists the stored filename, byte size, and path.

```js
const form = new FormData();
for (const file of fileInput.files) form.append("files", file);
const upload = await fetch(`${base}/subjects/OperatingSys/pdfs`, {
  method: "POST", headers, body: form
});
```

`GET /subjects/{subject}/pdfs` lists every PDF actually present in the subject's
`source` directory together with its processing state. It never returns a
dummy or stub entry: if the subject directory or manifest is missing it
returns `404`; corrupted manifests are skipped per-file rather than failing the
whole listing. Each item now carries:

- `subject_id` — the requested subject (so every record is attributable).
- `filename` — original PDF basename.
- `size_bytes` — file size.
- `uploaded_at` — ISO-8601 UTC timestamp of the source file (upload time).
- `status` — authoritative processing status (`queued` / `running` / `succeeded` / `failed` / `interrupted` / `not_processed`), taken from the latest job row when one exists and falling back to the manifest otherwise.
- `job_id`, `started_at`, `processed_at`, `last_error` — from the latest job
  (null when there has been no processing attempt).
- `question_count` — number of extracted questions from that PDF's
  `questions_parsed.json` when the parsed result exists and the latest job
  succeeded; otherwise `null`.
- `result_available` — `true` only when the parsed JSON and a successful job
  are both present, so the UI can gate the question bank import / result view.
- `review_status` — the administrator's review status (`unreviewed` /
  `approved` / `disapproved`) for this PDF, if set.

```js
const pdfs = await fetch(`${base}/subjects/OperatingSys/pdfs`, { headers });
```

## Process and retrieve results

`POST /subjects/{subject}/pdfs/{filename}/process` starts a fresh Scrapper → Parser run and returns HTTP 202 with `{ "job_id", "status", "subject", "filename" }`. Body `{ "force": true }` requests a fresh run even if a successful result exists. If omitted, `force` defaults to true. A PDF already running returns 409.

```js
const started = await fetch(`${base}/subjects/OperatingSys/pdfs/paper.pdf/process`, {
  method: "POST", headers: { ...headers, "Content-Type": "application/json" },
  body: JSON.stringify({ email: configuredEmail, password: configuredPassword })
});
const { job_id } = await started.json();
```

`GET /jobs/{job_id}` returns `queued`, `running`, `succeeded`, or `failed`, the active stage, timestamps, error summary, and output metadata on success. The PDF-specific status is also available at `GET /subjects/{subject}/pdfs/{filename}/status`.

The job response includes `job_id`, `subject`, `filename`, `status`, `stage`, `started_at`, `updated_at`, `error`, and `result`. On success, `result` includes paths for the scraper JSON, manifest, parsed JSON, page count, question count, and providers used.

```js
const status = await fetch(`${base}/jobs/${job_id}`, { headers });
```

`GET /subjects/{subject}/pdfs/{filename}/result` returns parsed JSON path, question count, and processing metadata. Add `?include_questions=true` to include all structured questions.

```js
const result = await fetch(
  `${base}/subjects/OperatingSys/pdfs/paper.pdf/result?include_questions=true`, { headers }
);
```

## Reconstruct

`POST /subjects/{subject}/pdfs/{filename}/reconstruct` generates the full DOCX and returns its path, counts, and any missing assets or warnings.

Its JSON response contains `docx_path`, `question_count`, `subquestion_count`, `figures_inserted`, `warnings`, and `missing_assets`. Download returns the DOCX bytes with the standard Word MIME type.

```js
const docx = await fetch(`${base}/subjects/OperatingSys/pdfs/paper.pdf/reconstruct`, {
  method: "POST", headers
});
```

Download the completed document from `GET /subjects/{subject}/pdfs/{filename}/reconstruction` with the same bearer header. It returns the DOCX file (404 if reconstruction has not run).

## Question-paper blueprints

All blueprint routes require the same bearer token. The frontend routes are
`/blueprints` and `/blueprints/{blueprint_id}`. The list response is
`{"items":[...],"quick_mark_presets":[15,25,50,60,70,80,100]}`. Each blueprint
uses the persisted schema fields `blueprint_id`, `name`, `sections`, and the
optional `description`, `subject_id`, `exam_type`, `auto_select`,
`selection_rule`, `instructions`, `total_marks`, and `is_default` fields. A
fixed section contains `name`, positive integer `question_count`, positive
integer `marks_per_question`, and optional `question_type`/`instructions`.
When present, blueprint `total_marks` must equal the sum of section counts
multiplied by marks per question. Section names must be unique.

- `GET /blueprints` lists saved blueprints and quick-mark presets.
- `GET /blueprints/{id}` retrieves a blueprint.
- `POST /blueprints` creates a blueprint (201); IDs must be unique.
- `PATCH /blueprints/{id}` updates only supplied fields, including rename-only
  `{"name":"Updated title"}` requests. `blueprint_id` remains stable.
- `PUT /blueprints/{id}` replaces a blueprint.
- `POST /blueprints/{id}/duplicate` creates a copy. Optional body:
  `{"blueprint_id":"copy-id","name":"Copy name"}`; either field may be
  omitted and the service supplies a value.
- `DELETE /blueprints/{id}` deletes a saved blueprint.

Create, retrieve, update, and duplicate return the blueprint object. Delete
returns `{"blueprint_id":"...","deleted":true}`. The canonical
`default-generic` blueprint is initialized once in
`{QUES_STORAGE_ROOT}/.masterdeorca/question_paper_blueprints.json` and cannot
be renamed, duplicated as a default, or deleted. Saved changes persist across
refreshes and restarts. Existing `/api/question-paper/blueprints` aliases
remain supported for compatibility.

## Responses and errors

| Status | Meaning |
| --- | --- |
| 200 / 201 / 202 | Read or operation accepted (201 creates/uploads; 202 starts background processing) |
| 401 | Missing/invalid token or incorrect login |
| 404 | Subject, PDF, job, or parsed result not found |
| 409 | Existing subject, duplicate filename, active job, or `force: false` for a completed PDF |
| 413 / 415 / 422 | Upload too large, not a PDF, or invalid name/request |
| 500 | Unexpected server error; check the subject `logs/` folder |

The UI flow is: login → create or list a subject → upload PDF → start processing → poll job status → read result metadata/questions → optionally request DOCX reconstruction. Do not include passwords in logs or client-side source code.

## Editable question-paper drafts

Create a draft from real questions with the same body as the generation request:

```js
const response = await fetch(`${base}/api/question-papers/drafts`, {
  method: "POST",
  headers: { ...headers, "Content-Type": "application/json" },
  body: JSON.stringify({ email: configuredEmail, password: configuredPassword })
    paper_title: "Operating Systems Final",
    subject: "OperatingSys",
    exam_type: "Final",
    total_marks: 16,
    duration: "2 Hours",
    seed: 42,
    university_name: "Example University",
    college_name: "Engineering College",
    department_name: "Computer Science",
    academic_year: "2026-27",
    semester: "V",
    subject_name: "Operating Systems",
    subject_code: "CS239",
    examination_date: "2026-11-02",
    include_processing_accuracy_notice: false
  })
});
const draft = await response.json();
```

The returned draft has `draft_id`, paper metadata, instructions, and ordered
`sections`. Each section has its name, `question_count`, `marks_per_question`
(null for the automatic generic section), and `questions`. Each question
includes `question_id` and `bank_question_id`, editable `text`, `marks`,
`priority`, original number, `source_pdf`, `source_path`, page and section
metadata, plus parser-provided `assets` and `tables`. Editing draft text does
not change the source Question Bank.

Draft endpoints (all require bearer authentication):

- `GET /api/question-papers/drafts/{draft_id}` reloads the saved draft.
- `PATCH /api/question-papers/drafts/{draft_id}` changes paper metadata,
  requested total, instructions, and section configuration/name/order.
- `POST /api/question-papers/drafts/{draft_id}/questions` adds a question with
  `{"question_id":"bank-id","section":"Section A"}`. The ID must belong
  to this draft's subject bank.
- `PATCH /api/question-papers/drafts/{draft_id}/questions/{question_id}` edits
  `text`, `marks`, or `section` in the draft only.
- `DELETE /api/question-papers/drafts/{draft_id}/questions/{question_id}`
  removes a question from the draft.
- `PUT /api/question-papers/drafts/{draft_id}/question-order` reorders with
  `{"question_ids":["id-1","id-2"]}`; include every current question once
  and keep each section's questions contiguous. The first occurrence of each
  section sets section order.
- `POST /api/question-papers/drafts/{draft_id}/docx` validates totals and
  returns an `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
  download. `{"include_processing_accuracy_notice":true|false}` is accepted but
   ignored: the mandatory Processing Accuracy Notice is always present in every
   exported DOCX (it cannot be turned off from the frontend). The DOCX prints
   supplied institution/examination metadata, draft edits, and figures, and
   ends with a concise `References / Sources` section (source PDF/path,
   original question label, page(s), source section); this workflow has no PDF
   export.

Creation follows saved priority 3 → 2 → 1 and randomizes ties, selecting only
matching marks from that subject. Insufficient eligible questions return `422`
with a readable shortfall. A draft may be temporarily incomplete while edited;
DOCX export requires question marks to total the requested marks. Source details
stay in the JSON draft for review. Figure references are retained and only
assets within the subject directory are embedded in the DOCX.

## Persistent Question Bank editing

All calls require the bearer token. Create and update questions in the same
subject bank used by Question Bank reads and paper generation:

```js
const created = await fetch(`${base}/api/question-bank`, {
  method: "POST",
  headers: { ...headers, "Content-Type": "application/json" },
  body: JSON.stringify({ email: configuredEmail, password: configuredPassword })
    subject_id: "OperatingSys",
    text: "Explain process scheduling.",
    marks: 5,
    section_name: "Section B",
    source_pdf: "midterm.pdf",
    original_question_number: "Q4(b)",
    page_number: 3,
    assets: ["output/Scrapper/midterm/figures/fig-1.png"],
    priority: 2
  })
});
const question = await created.json();

await fetch(`${base}/api/question-bank/${encodeURIComponent(question.question_id)}?subject_id=OperatingSys`, {
  method: "PATCH",
  headers: { ...headers, "Content-Type": "application/json" },
  body: JSON.stringify({ email: configuredEmail, password: configuredPassword })
});
```

`POST /api/question-bank` requires `subject_id`, one of `text`,
`original_text`, or `cleaned_text`, and positive integer `marks`. An optional
stable `question_id` may be supplied; otherwise the server generates one.
For retry-safe creates when the server generates the ID, send a stable
`idempotency_key`. An exact retry returns the original record; reusing an ID or
key with different content returns `409`.
`PATCH /api/question-bank/{question_id}` accepts only supplied fields and
requires `subject_id` either in the query or body. Editable fields include
wording, marks, question/section labels, source PDF/path, page references,
figures/assets, tables, and question type. `priority` is saved through the
existing separate priority store. Source and asset paths must remain inside
that subject's directory.

Both operations return the saved question record. Duplicate IDs return `409`,
invalid fields return `422`, unknown subjects/questions return `404`, and
unauthenticated requests return `401`. After either operation,
`GET /api/question-bank?subject_id=OperatingSys` returns the updated record in
the existing `{items, pagination, filters}` response. Updates preserve the ID
and every unspecified metadata field. Draft question edits remain draft-local;
use these routes when changing the persistent bank record.

### Subject questions and lazy figure/table loading

Select a subject by calling `GET /api/question-bank?subject_id=OperatingSys`.
The existing `marks`, `page`, and `page_size` query parameters remain
available. A selected subject's response contains only that subject's records.
Each item includes `question_id`, available text/marks/source metadata,
`subject_id`, `priority` (default `1`), and lightweight `resources.figures` /
`resources.tables` arrays plus accurate `has_figures` and `has_tables`
booleans. The flags are true only for retrievable stored attachments; do not
infer availability from question wording. Resource list entries contain a `resource_id` and
available filename/caption/page metadata; listing does not download image
bytes or send table cells.

When the user selects a resource, request it with the same bearer token:

```http
GET /api/question-bank/{question_id}/resources/{content_type}/{resource_id}?subject_id=OperatingSys
```

Use `content_type=figure` or `table` and the `resource_id` returned by the
question listing. When parser metadata has no resource ID, the listing uses
the zero-based item index as a string. Figure responses return
`metadata` plus `content_base64` containing the actual image bytes; table
responses return `metadata` plus structured `content.headers` and
`content.rows`. Available metadata includes caption/description, filename,
source PDF, and source page. The server resolves paths only from the selected
question record and does not expose absolute paths. Missing questions or
resources return `404`, unsupported resource types return `422`, and calls
without authentication return `401`.


## Basic Configuration presets (Generate Question Paper page)

`/api/basic-configurations/*` endpoints (bearer token required, same as
blueprints) save reusable examination metadata so users stop re-typing it. They
are a separate store from blueprints; presets and blueprints can be loaded
independently and combined during paper generation.

Schema per preset (snake_case): `configuration_id` (string UUID,
server-generated), `name` (required, 1-120 chars), and optional-or-null fields
`university_name`, `college_name`, `department_name` (max 200 chars each),
`paper_title` (200), `academic_year` (40), `semester` (60), `exam_type` (100),
`subject_id` (80, must pass subject-name validation **and** reference a
registered subject when set or changed — show the value from the subject
dropdown list and send `null` to clear), `subject_code` (80),
`examination_date` (`YYYY-MM-DD`), `duration` (free text, e.g. `"2 hours"`),
`total_marks` (integer 1-1000), plus `instructions` (array of non-empty
strings) and server-owned `is_default`, `created_at`, `updated_at`. Never send
empty strings for optional fields; send `null` or omit them. The API ignores
client-supplied `configuration_id`, `is_default`, `created_at`/`updated_at`;
do not fabricate university or college data for a fresh install - the backend
starts everyone on a neutral preset.

Endpoints:

- `GET /api/basic-configurations` → `{"items":[...]}`, default preset first.
- `GET /api/basic-configurations/default` → the current default preset. A
  neutral `default-config` preset appears on first use and is always there
  exactly once; it is editable like any other preset.
- `GET /api/basic-configurations/{id}` → one preset (404 when missing).
- `POST /api/basic-configurations` → 201 with the stored preset.
- `PUT /api/basic-configurations/{id}` → full replace of user fields (200).
- `PATCH /api/basic-configurations/{id}` → merge update, e.g. rename
  `{"name":"Renamed"}` (200).
- `POST /api/basic-configurations/{id}/duplicate` → 201; optional
  `{"name":"Copy name"}` body. New ID, `is_default:false`.
- `POST /api/basic-configurations/{id}/set-default` → flags it as the single
  default across the whole store (200).
- `DELETE /api/basic-configurations/{id}` → `{"deleted":true}`. Blueprints,
  question banks, and drafts are untouched. If the deleted preset was the
  default, the canonical neutral preset becomes the default again.

Status codes: 401 unauthenticated, 404 unknown ID, 422 validation (blank name,
empty optional strings, invalid or unregistered `subject_id` — the 422 detail
reads `"Subject does not exist: ..."`, non-ISO `examination_date`,
out-of-range `total_marks`, blank instruction strings). Exactly one preset is
marked default at any time; changes persist across refreshes and restarts.

UI flow on the Generate Question Paper page: load the list
(`GET /api/basic-configurations`) or the current default
(`GET /api/basic-configurations/default`) to prefill the metadata form, keep a
"Save as preset" button that POSTs the form fields with `name`, plus
"duplicate", "rename", "set default", and "delete" actions for presets the user
manages. To generate a paper, take the loaded preset's `paper_title`,
`exam_type`, `total_marks`, `duration`, and institutional fields into the
`POST /api/question-papers/generate` body (`title`, `university_name`,
`college_name`, `department_name`, `academic_year`, `semester`, `subject_code`,
`examination_date`), pick a blueprint separately (`GET /blueprints`), and
submit both - preset and blueprint are combined only at generation time.

## Generated paper history (Paper History page)

Every `POST /api/question-papers/generate` that succeeds is saved permanently
by the backend: the DOCX is stored at
`.masterdeorca/generated_papers/{paper_id}.docx` inside the storage root
(atomically written, byte-for-byte the document that was generated), and a
history record persists `paper_id`, `filename`
(`question-paper-{paper_id}.docx`), `subject`, `paper_title`, `exam_type`,
`total_marks`, `duration`, `generated_at` (ISO-8601 UTC), `generated_by`
(e-mail of the account that generated it), `file_path`, and `docx_url`.
Records survive backend restarts. There is no seed data: only papers actually
generated appear, newest first.

Endpoints (bearer token required; 401 otherwise):

- `GET /api/question-papers/history?page=1&page_size=25` →
  `{"items":[...],"page":..,"page_size":..,"total":..}`, sorted by
  `generated_at` descending. Optional `subject` filter (exact subject ID).
  Administrators see every paper; teachers/admins-in-readonly see only papers
  they generated themselves. Each item has a boolean `docx_available`; render
  a "file missing" notice instead of a download button when it is `false`.
  A corrupted record file is skipped by the backend rather than breaking the
  page. Empty history is `total: 0, items: []`.
- `GET /api/question-papers/history/{paper_id}` → the full record (sections,
  selected questions with sources, shortages, institutional metadata) plus
  `docx_available`. 404 when unknown.
- `GET /api/question-papers/{paper_id}/docx` → the original stored DOCX file
  (preserve the binary; do not re-render it in the frontend). 404 when the
  stored file is missing.
- `DELETE /api/question-papers/history/{paper_id}` →
  `{"paper_id":"...","deleted":true}`; removes the record and the stored file
  together. Allowed for administrators in edit mode or the generating user;
  everyone else gets `403` with
  `"Only the owner or an administrator can delete this paper"`. Ask for
  confirmation in the UI — deletion is permanent and removes the DOCX from
  the backend.

UI flow on the paper history page: fetch page 1 (page size 25) on load, show
`paper_title`, `subject`, `exam_type`, `total_marks`, `duration`,
`generated_at`, and `generated_by` per row, paginate with
`page`/`page_size`/`total`, offer download (link to `docx_url`) and delete
(only when permitted), and surface `docx_available=false` rows as damaged
records instead of hiding them. Generation responses already contain the new
history fields, so a "View in history" link can use `paper_id`/`docx_url`
immediately after generation.

## Admin dashboard metrics

`GET /api/dashboard/metrics` (bearer token of an administrator; other users get
`403`) powers the Admin Dashboard statistics. It returns exactly these
counters plus availability metadata:

| Field | Meaning |
| --- | --- |
| `total_teachers` | Registered teacher accounts (`users.role='teacher'`, the same definition as `GET /teachers`); `null` when the database is unreachable |
| `total_subjects` | Registered subjects, identical to the `GET /subjects` list length |
| `total_questions` | Valid records in every registered subject's canonical question bank, deduplicated by `question_id`; `null` only when no bank could be read |
| `availability` | `"ok"` / `"partial"` / `"unavailable"` per metric |
| `unavailable_subjects` | Subjects whose question-bank files could not be read (their questions are not included in the total) |
| `cached`, `cache_ttl_seconds` | Whether the question total came from the short-lived (10 s) server cache |

The dashboard UI should render the numeric fields directly, treat `null` as
"unavailable" for that tile, and when `availability.total_questions` is
`"partial"` show a subtle notice that the question total may be incomplete
listing `unavailable_subjects`. No extra permissions, endpoints, or manual
polling bookkeeping are needed: values refresh automatically after subject or
question changes, or within ~10 seconds after out-of-band file edits.

### All Questions vs specific marks query modes

The subject question-bank listing supports both dashboard views without any
new endpoint, based only on whether `marks` is present in the query:

- **All Questions:** `GET /api/question-bank?subject_id=OperatingSys&page=1&page_size=25`
  (omit `marks`). Returns every question of the subject across all marks
  values, paginated with accurate totals (`filters.marks` is `null`).
- **Specific marks:** `GET /api/question-bank?subject_id=OperatingSys&marks=2&page=1&page_size=25`.
  Filtered totals/pagination; an empty match is `200` with zero totals.

Never substitute an invalid `marks` value for All Questions: `0`, negative,
non-numeric or non-finite values return `422`. Build the marks selector from
`available_marks` (distinct values actually present in the subject's bank,
ascending — never hardcoded, and independent of the active filter or page).

```js
// All
await fetch(`${base}/api/question-bank?subject_id=OperatingSys&page=1&page_size=25`, { headers });
// Specific marks
await fetch(`${base}/api/question-bank?subject_id=OperatingSys&marks=2&page=1&page_size=25`, { headers });
```

## Admin navigation and access modes

The administrator experience is identified as **Admin** (never Teacher): after
login, call `GET /auth/me` — an administrator's response contains
`role: "admin"` plus `access_mode` (`"edit"` or `"readonly"`), which should
drive the Admin navigation and header identity.

### Selecting and switching the mode

- Current mode: `GET /auth/access-mode` (admin token) →
  `{"role": "admin", "access_mode": "edit"}`.
- Switch: `PUT /auth/access-mode` with `{"access_mode": "readonly" | "edit"}`
  → same shape. The server persists the mode per administrator account; a
  re-login or page reload keeps the selected mode. Invalid values return
  `422`; teacher tokens get `403` on both calls.
- Show the mode as a visible toggle/badge (Read-only / Edit) and always allow
  re-enabling Edit — switching back from read-only is never blocked.

### Read-only (view-only) behaviour

When `access_mode` is `readonly`, the backend enforces the restriction itself:
**every** write request returns
`403 {"detail": "Administrator account is in read-only mode; editing is
disabled"}`. Do not rely on hiding buttons alone — handle the 403 globally by
showing a "view-only" state and re-fetching the mode when a write fails with
this detail. Blocked operations include managing subjects and their PDFs
(upload, process, review, delete, reconstruct), creating/updating/deleting
question records (question bank, priorities, drafts, DB questions, topics),
blueprints, basic configurations, paper generation, PDF question imports, and
teacher account management.

Everything read-only stays available: dashboard metrics, subject lists, the
question bank browser, PYQ views, generated papers (list and DOCX export), and
the administrator's own account endpoints (`/auth/me`, TOTP status). Editing
permissions return as soon as the mode is switched back to `edit` — no new
login is required.

## Automatic recovery and validated Question Bank import

The backend now keeps the processing pipeline complete on its own. A
background reconciliation worker (hosted inside the API, one pass every
3–5 minutes, recoverable after every restart) re-parses scraped-but-unparsed
PDFs, validates parsed questions and appends only verified questions to each
subject's Question Bank. The UI does not need to drive any of this; two admin
endpoints exist for visibility and manual control:

- `POST /api/reconciliation/run` (edit-mode admin token) — triggers one
  reconciliation pass immediately and returns its summary; useful for testing
  and "run recovery now" buttons. Read-only administrators (and teachers)
  receive `403`. Overlap is safe: a concurrent run answers with
  `{"skipped": true, "reason": ...}` rather than corrupting state.
- `GET /api/reconciliation/status` (any admin) — schedule interval, last-run
  summary (`totals` with `parsed_recovered`, `imported`, `needs_review`,
  `invalid`, `duplicates`, failure counters) and pending/failed retry states
  with retry counts and next-retry times.

### What the UI can rely on

- **Only fully validated questions auto-appear in the Question Bank.** Parsed
  records that are uncertain (`needs_review`), broken (`invalid`) or
  duplicated are preserved in the parsed PDF output with audit annotations
  (outcome, reason, timestamp, `duplicate_of` when relevant); nothing is
  silently deleted. The bank itself contains exactly the records whose
  validation verdict is `valid`, so question-bank browsing stays clean.
- **Manual edits are safe.** The import is append-only by stable question ID:
  editing or deleting a question in the bank can never be reverted or
  overwritten by old parsed data on later import passes.
- **Duplicates are impossible** even if the same paper is reprocessed or a
  document repeats wording: normalized-text + similarity checks keep the bank
  one-record-per-question per subject.
- **Processing status surfaces failures transparently**: when a PDF cannot be
  recovered (e.g. parser keeps failing after its bounded retries), the status
  endpoint lists it as `failed` with the error summary; the processing
  lifecycle endpoints (job status, manifest) remain unchanged.
