# Backend services

> **Current storage note:** active persistence is SQLite-only. The service
> descriptions below include historical filesystem paths and JSON projections;
> those persistence details have been superseded by
> [docs/BACKEND_ARCHITECTURE.md](docs/BACKEND_ARCHITECTURE.md). FastAPI remains
> the frontend contract; the architecture document describes the current
> database-backed implementation.

## Question Bank API

`GET /api/question-bank` is served by the existing FastAPI application in
`src/masterdeorca/app.py` and uses the existing bearer-authenticated user
dependency. Each subject stores its persistent bank at
`{subject_root}/question_bank.json`, with top-level `subject`, `updated_at`, and
`questions` fields. The listing reads this persistent bank directly — it does
not read parser output or manifests per request.

Query parameters:

- `subject_id` (required): subject directory name. The endpoint never scans
  every subject when the caller has not selected one; an omitted value returns
  `422` with FastAPI's query-validation shape, for example
  `{"detail":[{"type":"missing","loc":["query","subject_id"],"msg":"Field required","input":null}]}`.
- `marks` (optional): exact numeric marks filter.
- `page` (default `1`) and `page_size` (default `25`, maximum `200`): pagination.

The API reads the subject's persistent `question_bank.json`; it does not scan
parser outputs on every request. After a successful Scrapper + Parser job
publishes its fresh result, the backend checks `manifests/processing_status.json`
for the PDF's successful status and subject-relative `result_json` path, then
imports the parsed file into the bank. The manifest supplies status and file
location only; question records come from the parsed result. Failed or
incomplete jobs, missing or invalid result files, and paths outside the subject
are not imported. Nested subquestions are stored as leaf items, with a parent
having children omitted as a separate item. Parsed wording, marks, question
numbering, section and page metadata are retained. Stable IDs include subject
and source identity to prevent collisions across PDFs.

Imports are idempotent: reprocessing updates matching records rather than
adding duplicates, while unrelated questions and additional bank fields are
preserved. Writes use a subject bank lock and atomic replacement. A missing
bank initializes automatically. An invalid bank is preserved as
`question_bank.json.corrupt-*` and rebuilt before new records are imported.
Pagination totals are calculated after filters and before page slicing.

The response shape is:

```json
{
  "items": [],
  "pagination": {"page": 1, "page_size": 25, "total_items": 0, "total_pages": 0},
  "filters": {"subject_id": "OperatingSys", "marks": null},
  "available_marks": [2, 3, 5, 16]
}
```

The subject-scoped list request is
`GET /api/question-bank?subject_id={subject_id}[&marks={number}&page={int}&page_size={int}]`.
`subject_id` is the exact subject directory name (for example `OperatingSys`)
and is mandatory. `marks` is an optional positive finite numeric exact match;
`page` is a positive integer defaulting to `1`; `page_size` is an integer from
`1` through `200`, defaulting to `25`. A syntactically valid but unknown
subject returns `404`; malformed or missing query values return `422`. A valid
subject and marks filter with no matching questions returns `200` using the
empty response shown above. Authentication is required.

`available_marks` is calculated from valid, positive numeric marks in the same
subject bank read for this response, before applying the optional filter or
pagination. It contains distinct values sorted numerically ascending; null,
non-numeric, non-finite, and zero/negative marks are excluded. Marks are exposed
as JSON numbers (integral values as integers); records with missing/invalid
marks retain `marks: null`. The `marks` query value is a positive finite number
and is an exact match, so `marks=2` matches numeric `2`, `2.0`, or a persisted
numeric string such as `"2.0"`. `marks=0`, non-numeric values, and non-finite
values return `422` validation errors.

An empty match is a successful `200` response with `items: []`,
`pagination.total_items: 0`, and `pagination.total_pages: 0`; it is not an API
error. Pagination totals are calculated after exact marks filtering and before
the requested page slice. `subject_id` is the subject directory name and is
required; the service does not load other subject banks to construct the mark
options.

Each listed item includes its `question_id`, normalized `text`, original
wording fields, marks, `priority` (defaults to `1`), `subject_id`, source PDF,
subject-relative source path, original question number, section, page
references, and lightweight `resources` references (`figures` and `tables`).
The list and detail responses also include boolean `has_figures` and
`has_tables`. These flags are true only when the corresponding `resources`
array contains a retrievable attachment: figures must have an existing file
inside the subject directory; tables must contain structured headers, rows, or
cells. Missing, malformed, unsafe, or unavailable attachments are omitted and
do not set the flags. Attachment references include their stable resource ID
and available filename, caption, page, and subject-relative path. Machine-local
absolute paths are not returned. Listing verifies figure file existence but
does not read image bytes or return table cell contents.
`GET /api/question-bank/{question_id}?subject_id={subject}` returns one full
question record with the same source and attachment metadata, including figure
and table references. It returns `404` if the ID is not in that subject.
A selected resource
can be loaded on demand using the authenticated route
`GET /api/question-bank/{question_id}/resources/{content_type}/{resource_id}?subject_id={subject}`.
`content_type` is `figure` or `table`; `resource_id` is the ID in the question's
asset/table metadata, or its zero-based array index when no ID was supplied.
Figure responses contain `metadata` and `content_base64` with the actual bytes;
table responses contain `metadata` and structured `content.headers` and
`content.rows`. Metadata includes available filename, caption/description,
source PDF, and source page. The service resolves the resource only from the
selected question's references and rejects paths outside that subject. Missing
questions or resources return `404`; unsupported content types return `422`.
This route requires bearer authentication and never accepts a filesystem path.

No frontend or persisted question schema changes are required.

Manual Question Bank create/update operations now use the same subject
`question_bank.json` and locked atomic-write implementation:

- `POST /api/question-bank` creates one record. The JSON body requires
  `subject_id`, question text (`text`, `original_text`, or `cleaned_text`), and
  positive integer `marks`. Optional fields include `question_id` (stable
  client supplied ID; otherwise generated), section/question labels, source PDF
  and subject-local source path, page metadata, assets/figure paths, tables,
  question type, `priority` (`1`–`3`), and optional `idempotency_key`. A
  supplied duplicate ID returns the existing record for an exact retry and
  `409` if its content differs; an idempotency key provides the same retry
  behavior when the server generates the question ID. Source and asset paths
  must remain within the selected subject.
- `PATCH /api/question-bank/{question_id}?subject_id=...` patches only supplied
  fields on an existing record. `subject_id` may instead be supplied in the
  body. The ID and subject ownership are immutable. Priority changes use the
  existing separate priorities file, not the bank JSON.

Both routes require bearer authentication and return the saved question record.
Missing subjects/questions return `404`, malformed data or ownership/path
mismatches return `422`, and unauthenticated requests return `401`. Reads,
automatic parsed-PDF synchronization, priority lookup, and paper generation
continue to use their existing stores and contracts. Draft-only question edits
reuse the Question Bank field validator but continue to persist only in the
draft, leaving the source bank untouched.

## Parsed Question Priorities API

Priorities are stored separately from all subject `question_bank.json` files in
`{QUES_STORAGE_ROOT}/.masterdeorca/question_priorities.json`. The file uses
schema version `1` and maps the existing stable `question_id` values to integer
priorities. Questions without an override have priority `1` (Regular).

All endpoints require an active user's bearer token:

- `GET /api/question-priorities?subject_id=OperatingSys` returns a
  `priorities` map for that subject's current bank questions; omit `subject_id`
  to include questions from every subject. Unset priorities are returned as
  `1`.
- `PUT /api/question-priorities` accepts
  `{"question_id":"...","priority":1,"subject_id":"OperatingSys"}`.
  `subject_id` is optional when the ID is globally unambiguous. Values must be
  integer `1`, `2`, or `3` (Regular, Higher priority, Must come).
- `DELETE /api/question-priorities/{question_id}?subject_id=OperatingSys`
  resets a saved value to the default. `subject_id` is optional.

Successful writes return the question ID, priority, and update/reset status.
Unknown subjects or question IDs return `404`; unauthenticated requests return
`401`; invalid priority values return `422`. The backend verifies IDs against
the persistent question bank before changing a priority. Updates use a process
and file lock plus an atomic replacement, preserving other records across
concurrent updates and backend restarts. If the priority file is malformed, it
is preserved with a `.corrupt-*` suffix before a fresh versioned file is
written. These endpoints do not alter question-bank records or processing.

## Question Paper Blueprints and Generation API

The authenticated endpoints below are implemented by the existing FastAPI app
in `src/masterdeorca/app.py`. The frontend-facing routes use `/blueprints`; the
older `/api/question-paper/blueprints` routes remain as backward-compatible
aliases. They read questions from the subject's existing
`question_bank.json` through `src/masterdeorca/services/question_bank.py`; they
do not modify the bank or priority records. Blueprint JSON is stored at
`{QUES_STORAGE_ROOT}/.masterdeorca/question_paper_blueprints.json` and generated
DOCX files are stored under
`{QUES_STORAGE_ROOT}/.masterdeorca/generated_papers/`. This is the single
canonical blueprint JSON file. The authenticated list request initializes it
on first use with exactly one protected `default-generic` record; subsequent
create, update, and delete operations persist to the same file using an atomic
replacement under a cross-process file lock. The list response includes
`quick_mark_presets` (`15`, `25`, `50`, `60`, `70`, `80`, `100`) for editable UI
section setup. Presets are shared constants, not separately stored blueprints.

- `GET /blueprints` returns `{items, quick_mark_presets}`;
  `items` lists the canonical generic blueprint and saved blueprints.
- `GET /blueprints/{blueprint_id}` retrieves one blueprint.
- `POST /blueprints` creates a blueprint. Required fields:
  `blueprint_id`, `name`, and `sections`; optional fields are `description`,
  `subject_id`, `exam_type`, `auto_select`, `selection_rule`, and `instructions`.
- `PUT /blueprints/{blueprint_id}` replaces an existing editable blueprint.
  The ID in the body, if supplied, must match the path.
- `PATCH /blueprints/{blueprint_id}` merges supplied fields into an existing
  blueprint, allowing rename-only bodies such as `{"name":"New name"}`. The
  ID is stable and cannot be changed.
- `POST /blueprints/{blueprint_id}/duplicate` duplicates a blueprint. An
  optional body may set `blueprint_id` and/or `name`; otherwise the API assigns
  a unique ID and appends `(copy)` to the name. The duplicate is never default.
- `DELETE /blueprints/{blueprint_id}` deletes an editable
  blueprint. The built-in `default-generic` blueprint cannot be changed.
  These routes return the blueprint object for create/read/update/duplicate;
  delete returns `{"blueprint_id":"...","deleted":true}`. Duplicate IDs
  return `409`, invalid schemas return `422`, missing IDs return `404`, and
  unauthenticated calls return `401`.
- `POST /api/question-papers/generate` accepts `title`, `subject`, `exam_type`,
  `total_marks`, `duration`, optional `blueprint_id`, and optional integer
  `seed`. It applies an explicitly selected blueprint, otherwise the most
  specific blueprint whose subject and exam type match, otherwise
  `default-generic`. Blueprint selection is optional; the generic configuration
  is applied automatically when no saved template matches.
- `GET /api/question-papers/{paper_id}/docx` downloads the generated DOCX.

A fixed section has `name`, positive integer `question_count`, positive integer
`marks_per_question`, and optional `question_type` and `instructions`. Section
names must be unique within a blueprint. If `total_marks` is stored on a
blueprint, it must equal the sum of section counts multiplied by their marks;
generation validates that same total against the requested paper marks.
Question selection always prefers priority 3, then 2, then 1, and randomizes
among eligible questions within the same priority level. The legacy
`selection_rule` field remains accepted in saved templates for compatibility,
but cannot disable priority ordering. Question IDs cannot be selected twice.
Questions are eligible only when their marks match the section and, when
specified, their parsed `question_type`/`type`/`kind` matches. The generic
blueprint uses `auto_select: true` and selects a set of parsed questions whose
marks reach the requested total when possible. No questions are fabricated.

The JSON generation response contains paper metadata, `sections`, numbered
question records with text, marks, original question number, source PDF/path,
page number(s), section name, and saved priority; it also contains requested
and achieved marks, `shortages`, `seed`, `docx_url`, and the exact
`processing_accuracy_notice` text. Each full response is retained as
`{QUES_STORAGE_ROOT}/.masterdeorca/generated_papers/{paper_id}.json` alongside
the DOCX for traceability. Source metadata is kept out of the printed exam
paper. When the bank cannot satisfy the
requested blueprint, available questions are included and the shortage is
reported in the response; the DOCX contains only those selected questions.
Invalid blueprint structures and total mismatches return `422`, unknown
subjects or blueprint IDs return `404`, and all endpoints require bearer
authentication (`401` when unauthenticated).

### Generated paper history

Every successful `POST /api/question-papers/generate` response is also a
permanent history record. The DOCX is stored on the backend at
`{QUES_STORAGE_ROOT}/.masterdeorca/generated_papers/{paper_id}.docx`
(atomically written, verified to reopen, and preserved exactly as generated),
and the paper record persists `paper_id`, `filename`
(`question-paper-{paper_id}.docx`), `paper_title`/`title`, `subject`,
`exam_type`, `total_marks`, `duration`, `generated_at` (ISO-8601 UTC),
`generated_by` (the e-mail of the authenticated user who generated it),
`file_path` (absolute storage path of the stored DOCX), and `docx_url`.
Records are only written after the DOCX itself has been stored successfully,
survive backend restarts, and are validated on read; no data is fabricated.

Authenticated history routes (unauthenticated calls return `401`):

- `GET /api/question-papers/history?page=1&page_size=25` lists summaries
  newest-first, with `generated_at` descending. `page_size` is 1-100
  (default 25), `page` starts at 1, and the response is
  `{"items":[...],"page":n,"page_size":n,"total":n}`. Each item carries
  `paper_id`, `filename`, `subject`, `paper_title`, `exam_type`,
  `total_marks`, `duration`, `generated_at`, `generated_by`, `file_path`,
  `docx_url`, and `docx_available` (`false` when the stored DOCX file went
  missing). An optional `subject` query filters by exact subject ID.
  Administrators see the full history; other users only the papers they
  generated themselves. Corrupted record files are skipped instead of failing
  the listing, and the response never contains stub or dummy entries.
- `GET /api/question-papers/history/{paper_id}` retrieves the full stored
  record (sections, questions, shortages, metadata) plus `docx_available`.
  Unknown IDs return `404`.
- `GET /api/question-papers/{paper_id}/docx` downloads the original stored
  DOCX (`FileResponse`, correct DOCX media type). A missing stored file
  returns `404` rather than an error page.
- `DELETE /api/question-papers/history/{paper_id}` deletes the history record
  and its stored DOCX atomically under the store file lock, returning
  `{"paper_id":"...","deleted":true}`. It requires an authenticated
  non-read-only user, and only the administrator or the user recorded in
  `generated_by` may delete a paper (others get `403`; unknown IDs `404`).
  Deleting a record whose DOCX file is already missing still succeeds.

The processing accuracy notice is mandatory for every generated DOCX. It is
rendered server-side, once, near the top of the paper right after the
examination details and instructions and before the questions, regardless of
the request or generation configuration (no frontend flag can disable it). The
notice text is:

> **Processing Accuracy Notice**
>
> Review extracted questions before relying on them. OCR and question
> segmentation can produce errors, especially for scanned or complex PDFs.
>
> **Important Information:** Some questions are extracted from PDF files using
> automated text recognition (OCR). This process may occasionally misread
> words, numbers, mathematical expressions, diagrams, or tables. Questions may
> also be split incorrectly or have formatting issues.
>
> Please check the questions carefully before using this paper for an
> examination. Make any necessary corrections to ensure that the questions,
> marks, and figures are accurate.

The notice is rendered in black text only; structural distinction is by
heading/bold styling, never colour. The `include_processing_accuracy_notice`
field is still accepted by generation/draft payloads for API compatibility but
is ignored — the notice is always present. The same DOCX builder is used by
generated papers, drafts, and draft exports, so all of them carry the notice.

### Editable question-paper drafts

Drafts reuse the subject's persistent `question_bank.json`, saved priorities,
blueprint selection, and existing DOCX builder. They do not copy or edit bank
records. Creation uses the same priority order (3, 2, 1), randomized within a
priority tier, and exact section marks. If the selected subject cannot meet the
requested marks and section counts, draft creation returns `422` with an
insufficient-question detail rather than creating a partial paper.

Authenticated routes:

- `POST /api/question-papers/drafts` (also `/drafts/generate`) accepts the same
  request as `/api/question-papers/generate` and creates a persisted draft. In
  addition to `paper_title`/`title`, `subject`, `exam_type`, `total_marks`, and
  `duration`, it accepts optional `university_name`, `college_name`,
  `department_name`, `academic_year`, `semester`, `subject_name`,
  `subject_code`, and `examination_date`. These values are saved on the draft
  and can be edited with its PATCH route. `include_processing_accuracy_notice`
  is accepted as a boolean (default `false`) for payload compatibility, but it
  is ignored: the mandatory processing accuracy notice is always rendered into
  every exported DOCX.
- `GET /api/question-papers/drafts/{draft_id}` retrieves its metadata, ordered
  sections and questions, bank IDs, source metadata, and available asset refs.
- `PATCH /api/question-papers/drafts/{draft_id}` edits title, exam type,
  duration, total marks, instructions, and section names/order/instructions/
  marks-per-question. The subject remains fixed to the original request.
- `POST /api/question-papers/drafts/{draft_id}/questions` adds a same-subject
  bank item with `{"question_id":"...","section":"Section A"}`. Marks
  must match the target section when that section has fixed marks.
- `PATCH /api/question-papers/drafts/{draft_id}/questions/{question_id}` edits
  draft-only `text`, `marks`, or `section` fields.
- `DELETE /api/question-papers/drafts/{draft_id}/questions/{question_id}` removes
  that item from the draft only.
- `PUT /api/question-papers/drafts/{draft_id}/question-order` accepts
  `{"question_ids":[...]}` with every current ID exactly once; this changes
  reading order and section order while preserving section membership. A
  section's questions must stay contiguous in the requested order.
- `POST /api/question-papers/drafts/{draft_id}/docx` validates mark totals and
  returns the final `.docx` download. An optional JSON body
  `{"include_processing_accuracy_notice":true|false}` controls the notice for
  that export; omission uses the saved draft option. The document includes
  available institutional/exam metadata, the edited draft wording and figures,
  and a final `References / Sources` section with available source PDF, source
  path, original question label, pages, and source section. No PDF export is
  provided.

Draft JSON is stored under
`{QUES_STORAGE_ROOT}/.masterdeorca/generated_papers/drafts/{draft_id}.json`;
the exported DOCX uses the matching `.docx` path. Draft edits use atomic writes
and per-draft interprocess locks. Source PDF, source path, original question
label, page numbers, and parser asset/table references remain in the draft
response, not in the printed source-free question text. DOCX figure paths are
only opened when they resolve inside that question's subject directory.
Incomplete drafts can be edited, but export returns `422` until the unique
questions' marks equal the requested paper total and section mark constraints
are satisfied. All draft routes require bearer authentication.

## Basic Configuration Presets API

The authenticated endpoints below are implemented by the existing FastAPI app in
`src/masterdeorca/app.py` (`basic configuration` tag). They let the Generate
Question Paper page save and reuse examination metadata without re-entering it.
Logic lives in `src/masterdeorca/services/basic_config.py`. Presets are separate
JSON entities stored at
`{QUES_STORAGE_ROOT}/.masterdeorca/basic_configurations.json`; blueprints are
still stored in `question_paper_blueprints.json`, and the two stores are never
merged. Presets persist across restarts through atomic file replacement under
the same cross-process file lock used by blueprints. The file is initialized on
first use with exactly one canonical neutral record named `default-config`
whose institutional fields are null (no fabricated university, college or
examination data). It is editable, savable and reusable, and a fresh neutral
preset is recreated server-side if the flagged default is deleted.

Schema (all responses snake_case):

- `configuration_id` (string, server-owned UUID; client-supplied values are ignored)
- `name` (string, required, 1-120 chars, at least one non-space character)
- `university_name`, `college_name`, `department_name`, `paper_title`
  (string or null, max 200 chars)
- `academic_year` (string or null, max 40), `semester` (string or null, max 60),
  `exam_type` (string or null, max 100)
- `subject_id` (string or null, max 80; must satisfy the existing subject-name
  validation when supplied, and must reference a registered subject when set
  or changed)
- `subject_code` (string or null, max 80)
- `examination_date` (string or null; ISO date `YYYY-MM-DD` when supplied)
- `duration` (string or null, max 100, e.g. `"2 hours"`)
- `total_marks` (integer 1-1000 or null)
- `instructions` (array of non-empty strings, general examination instructions)
- `is_default` (boolean, server-owned; at most one preset in the store is default)
- `created_at`, `updated_at` (ISO-8601 UTC timestamps, server-owned)

Empty strings are rejected for optional text fields; send `null` (or omit the
field) to leave a value unset, matching the question-paper generate contract.

Routes (all require bearer authentication; unauthenticated calls return `401`):

- `GET /api/basic-configurations` returns `{"items": [...]}` sorted default-first
  then by name.
- `GET /api/basic-configurations/default` returns the flagged default preset,
  or the canonical `default-config` preset when no preset is flagged. It never
  recreates the default on repeated requests. There is always exactly one
  default.
- `GET /api/basic-configurations/{configuration_id}` retrieves one preset; a
  missing ID returns `404`.
- `POST /api/basic-configurations` creates a preset (201). The body requires
  `name`; every other field is optional. Server-owned fields
  (`configuration_id`, `is_default`, `created_at`, `updated_at`) are ignored if
  supplied. New presets are never created as the default.
- `PUT /api/basic-configurations/{configuration_id}` replaces the preset fields
  (200). `configuration_id`, `created_at` and `is_default` are preserved; a
  missing ID returns `404`.
- `PATCH /api/basic-configurations/{configuration_id}` merges supplied fields
  (200) and is the rename endpoint (e.g. `{"name":"New name"}`). Unknown or
  internal fields are rejected with `422`.
- `POST /api/basic-configurations/{configuration_id}/duplicate` duplicates a
  preset (201). An optional body may set `{"name":"..."}`; otherwise the name
  gets `(copy)` appended. Duplicates always receive a new ID, fresh timestamps,
  and `is_default:false`.
- `POST /api/basic-configurations/{configuration_id}/set-default` flags exactly
  one preset as the default (200) and clears the flag from every other preset.
  A missing ID returns `404`.
- `DELETE /api/basic-configurations/{configuration_id}` deletes a preset and
  returns `{"configuration_id":"...","deleted":true}`. Deleting raises no
  dangling default: when the flagged default is deleted, the canonical neutral
  `default-config` preset becomes the default again. Blueprint and
  question-bank data are never affected.

Validation failures (blank name, empty optional text, bad `subject_id`,
non-ISO `examination_date`, out-of-range `total_marks`, blank instruction
entries, oversized strings) return `422` with a human-readable detail. A
`subject_id` that names an unregistered subject is also `422`
(`"Subject does not exist: ..."`), as is duplicating a preset whose copied
reference no longer matches a real subject. A stored reference is tolerated
on update when it is left unchanged (or cleared to `null`), so presets keep
loading and stay editable even if a subject directory is removed later;
frontend lists (`GET /subjects`) remain the source for the dropdown picker.

## Admin Dashboard Metrics API

`GET /api/dashboard/metrics` (`dashboard` tag) returns the administrator
dashboard statistics. It requires bearer authentication and the existing
administrator policy (`require_admin`): other authenticated users receive
`403`, unauthenticated requests receive `401`. The endpoint is implemented by
`src/masterdeorca/services/dashboard_metrics.py` and reuses the existing
services only; it exposes no teacher or question records, no file paths, and no
database details.

Response schema (snake_case):

```json
{
  "total_teachers": 5,
  "total_subjects": 7,
  "total_questions": 412,
  "availability": {
    "total_teachers": "ok",
    "total_subjects": "ok",
    "total_questions": "ok"
  },
  "unavailable_subjects": [],
  "cached": false,
  "cache_ttl_seconds": 10
}
```

Counting rules:

- `total_teachers` counts registered teacher accounts only, using the same
  business definition as `GET /teachers` (`users.role = 'teacher'`). The
  seeded administrator account is not counted. Emails are unique in the users
  table, so duplicate accounts are impossible.
- `total_subjects` equals the length of the subject-management system's
  canonical subject list (`GET /subjects`); it is never a hardcoded number.
- `total_questions` counts valid records in every registered subject's
  canonical `question_bank.json` (both parsed and manually added records are
  bank records by the existing design; the separate SQLite `questions` staging
  table is not counted). Records are deduplicated by `question_id`, so a
  question referenced by more than one processing artifact is counted once.
  The count reads the bank files directly and is unaffected by the
  question-bank API's pagination.

Availability and reliability behaviour:

- A subject with no bank file is empty, not unavailable.
- A subject whose bank file is unreadable or malformed is listed in
  `unavailable_subjects`. When that list is non-empty,
  `availability.total_questions` becomes `"partial"` (some banks were
  readable) or `"unavailable"` (none were, and `total_questions` is `null`);
  the other metrics are unaffected.
- `total_teachers` becomes `null` with
  `availability.total_teachers = "unavailable"` only if the database is
  unreachable.
- Caching: the question summary is cached in-process for
  `cache_ttl_seconds` (10 s); `cached` reports whether the response was served
  from that cache. The cache is invalidated whenever data changes through the
  API: subject creation, rename or deletion, question-bank record
  creation/update, and successful PDF question-bank sync. Subject and teacher
  counts are recomputed on every request.

### All Questions vs specific marks modes

`GET /api/question-bank` supports two query modes, decided solely by whether
the optional `marks` query parameter is present:

- **All Questions (Mode A):**
  `GET /api/question-bank?subject_id=OperatingSys&page=1&page_size=25` — no
  `marks` parameter. Returns *every* question in the selected subject, covering
  all available marks values, `page` by `page_size`; `filters.marks` is `null`
  and pagination totals cover the entire subject bank.
- **Specific marks (Mode B):**
  `GET /api/question-bank?subject_id=OperatingSys&marks=2&page=1&page_size=25`.
  Returns only that subject's questions whose normalized marks numerically
  equal the requested value; pagination and totals are calculated after the
  filter, and `filters.marks` echoes the requested value. A value with zero
  matching questions returns `200` with an empty page, not an error.

An omitted `marks` parameter is always full-mode; no arbitrary invalid value is
interpreted as All Questions — `marks=0`, negatives, non-numbers and
non-finite numbers are rejected with `422` by the existing validation.
`available_marks` lists the distinct marks values actually present in the
selected subject's persisted bank, numerically ascending, with no hardcoded
values and no values with zero matching questions; it is independent of the
active marks filter and page.

## Administrator role and access modes (Admin)

The administrator principal is the `users` row with `role='admin'` (the seeded
Administrator account). `GET /auth/me` identifies this principal as
`role: "admin"` — the Admin identity, not Teacher — and for administrators it
additionally returns the account's `access_mode`. Teacher accounts
(`GET /teachers`, `/teachers/*` teachers resource) are unchanged: they keep
their accounts, status values, and existing question-bank/paper permissions.

### Administrator access modes

Administrators have exactly two access modes:

- `edit` (default): full existing permissions — create, update and delete
  records, manage subjects, upload/process PDFs, edit questions, manage
  teacher accounts, and administrative operations.
- `readonly`: view-only dashboard data, subject lists, question banks, PYQ
  records and generated papers. Every mutating request is refused by the
  backend, not only hidden in the UI.

### Mode-selection contract

- `GET /auth/access-mode` (admin role required; teachers receive `403` with
  `{"detail":"Admin role required"}`) → `{"role": "admin", "access_mode": "<readonly|edit>"}`.
- `PUT /auth/access-mode` with `{"access_mode": "readonly" | "edit"}` (bearer
  token of the administrator) → `{"role": "admin", "access_mode": "<mode>"}`.
- `422` for any other or missing `access_mode` value. `401` without a valid
  token; `403` for non-admin users.
- The mode is persisted per administrator account (new `access_mode` column in
  the `admins` table, default `edit`) and re-read from the database on every
  request. Tokens carry no role or permission claims, and no
  client-supplied role or permission value is trusted: the only way to change
  the mode is this authenticated endpoint, and it always applies to the
  administrator's own account. Switching from `readonly` back to `edit` is
  always allowed for the authenticated administrator, so a read-only admin
  never gets locked out. Existing authentication (including TOTP enrollment,
  verification, and status endpoints) is unchanged.

### Permission enforcement

Write routes are split into two guarded dependency groups; both return
`403 {"detail": "Administrator account is in read-only mode; editing is
disabled"}` when the authenticated administrator is in `readonly` mode:

- Administrative mutations (admins only): `POST/PATCH/DELETE /teachers`,
  `POST /subjects`, `PATCH /subjects/{subject}`, `DELETE /subjects/{subject}`,
  `POST /subjects/{subject}/pdfs` (upload), `POST .../pdfs/{filename}/process`,
  `POST .../pdfs/{filename}/reconstruct`, `POST/PATCH/DELETE /questions`,
  `POST /subjects/{subject}/topics`, `PATCH/DELETE /topics/{topic_id}`,
  `POST .../pdfs/{filename}/questions/import`,
  `PATCH/DELETE /subjects/{subject}/pdfs/{filename}` (review status, delete).
- Shared record mutations (admin principals gated; teacher principals keep
  their existing permissions because teachers have no access mode):
  `POST`/`PATCH /api/question-bank*`, `PUT`/`DELETE /api/question-priorities*`,
  `POST/PUT/PATCH/duplicate/DELETE /api/question-paper/blueprints*`,
  `POST/PUT/PATCH/duplicate/set-default/DELETE /api/basic-configurations*`,
  `POST /api/question-papers/generate`, and question-paper draft
  creation/modification endpoints.

Read-only administrators keep full read access: `GET /subjects`, question-bank
listing and detail, priority reads, blueprint/configuration listing, `GET
/papers`, `GET /teachers`, `GET /api/dashboard/metrics`, processing status
endpoints, DOCX draft export, and `GET /auth/me` / `/auth/totp/*`.

## Automatic processing recovery, AI question validation and Question Bank safety

The processing lifecycle is logically: **Scrapped → Parsed → Validated →
Imported into the Question Bank**. Each stage is tracked independently and a
failed stage stays retryable without damaging successful previous stages. All
of this lives in one backend module (`src/masterdeorca/reconciliation/`),
reusing the existing Scrapper, Parser, Question-Bank services and pipeline —
there is no parallel processing architecture and no external service; the
FastAPI application hosts the worker.

### Background reconciliation worker

- Runs inside the FastAPI app (`lifespan`) as a single-flight asyncio task,
  one pass every 3–5 minutes (`QUES_RECONCILE_INTERVAL`, clamped to 180–300 s,
  default 240 s). The first pass runs immediately on startup, so interrupted
  work recovers after every application restart.
- Every pass inspects the on-disk stage artifacts of **every registered
  subject**: `output/Scrapper/<stem>/` (scrape records + manifest) and
  `output/Parser/<stem>/questions_parsed.json`, cross-referenced with the
  manifest, the jobs table and the subject's `question_bank.json`.
- Cases handled, resuming from the last successfully completed stage:
  1. *Scrapped but not parsed* — the scraped manifest is sent to the existing
     Parser (staged run + atomic stage replacement, so partial previous output
     cannot survive). The PDF manifest entry is marked `succeeded` afterwards.
  2. *Parsed but not imported* — parsed questions are validated (below) and
     only `valid` records are appended to the subject's `question_bank.json`.
  3. *Processing interrupted* — only the incomplete stage runs; the Scrapper
     stage is never repeated from the worker.
  4. *Missing Question Bank records* — the parsed doc is diffed against the
     bank every pass; missing records are re-imported idempotently.
- Files with a `queued`/`running` job (still owned by a live processing run)
  are never touched by the worker.
- Logs: `masterdeorca.reconciliation` logger; per-operation log lines at INFO/
  WARNING/ERROR, incl. stage, subject, filename, and result.

### PYQ (PDF question) listing and results endpoint

`GET /subjects/{subject}/pdfs` (authenticated user with subject access; teachers see assigned subjects) returns the real records for
every PDF in the subject's actual `source` directory. It is the single verified
data view the frontend uses for the Previous-Year Questions picker, combining
three related but separate data sources:

- the subject's `source` directory (which PDFs truly exist),
- the `jobs` table (the authoritative status, timestamps, job id, error),
- `manifests/processing_status.json` (fallback status + paths to the parsed
  result),
- and the parsed `output/Parser/<stem>/questions_parsed.json` file (the
  `question_count`), which is read only when the manifest points to a real,
  readable file and the latest job succeeded.

A missing subject directory returns `404` (never an empty dummy list); a
corrupted/unreadable manifest is regenerated by `sync_subject` before listing,
and a corrupted parsed JSON yields `question_count: null` for that PDF alone
without failing the request. The response never fabricates entries. Each item
carries `subject_id`, `filename`, `size_bytes`, `uploaded_at` (source file
mtime), `status`, `job_id`, `started_at`, `processed_at`, `last_error`,
`question_count`, `result_available`, and `review_status`. Status is taken from
the latest job row when present and falls back to the manifest only when no job
exists (so a manifest clobbered to `not_processed` by a sync pass with no job
does not mis-label a processed PDF). This is distinct from
`GET /subjects/{subject}/pdfs/{filename}/result`, which returns the parsed
question payload, and from `GET /subjects/{subject}/processing-status`, which
returns the raw manifest.

### Deterministic question validation

Validation is **deterministic Python/JSON only** — the current worker makes no
LLM calls (there is no LangGraph AI-review pass and `QUES_VALIDATION_ENABLE_LLM`
is not implemented anywhere in the code). Every parsed record is assessed by
`validation.assess_question()`:

- **Junk rejection** — empty text; numeric/marks fragments (`2, 500`,
  `3, 400 e. 4, 112`, isolated values, `500 marks`, page numbers, bare
  numbers); repetitive separator noise; OCR garbage (symbol-heavy or vowel-less
  text). Valid metadata is checked (`marks`, when supplied, must be a finite
  positive number). Text is never rejected merely for being short,
  mathematical, diagram-based or statement/instruction-style: declarative
  statement questions with a finite verb are recognized as genuine.
- **Ambiguity handling** — plausible records without a clear question signal,
  or with invalid `marks` metadata, are marked `needs_review` for a human and
  are never imported.
- **No unnecessary re-evaluation**: verdicts are persisted into the parsed
  output as per-record audit annotations keyed by a text signature (validator
  version + text hash). Unchanged records are re-verified from stored verdicts
  without any provider contact.

### Validation outcomes

Every extracted record receives exactly one outcome, stored in its parsed
record's `validation` field (with `reason`, `validator`, `checked_at`,
`duplicate_of` where relevant) — records are never deleted from the parsed
output, only annotated:

- `valid` — automatically imported into the Question Bank.
- `needs_review` — plausible but uncertain (no clear question signal, or
  invalid `marks` metadata). Never imported.
- `invalid` — clearly not a question (never deleted, preserved for audit).
- `duplicate` — matches an existing bank record (or an earlier record in the
  same batch) via normalized text plus similarity checks (`≥ 0.92`); the
  annotation records the `duplicate_of` question ID. A match against a
  question's *own* already-imported ID is idempotence, not duplication.

A per-document summary is written into the parsed file's
`metadata.validation` (schema, timestamp, outcome counts).

### Duplicate prevention and bank safety

- Stable question IDs: deterministic `uuid5` over
  `subject/manifest-id/label-identity` (shared with `question_bank.py`'s
  flattened IDs), so re-parsing the same document yields the same IDs.
- Duplicate detection: exact normalized-text match plus `difflib` similarity
  checks, isolated per subject (a subject's bank is only compared with
  itself) and within the same batch.
- Bank imports are append-only (`question_bank.import_questions`): a record
  whose stable ID already exists is skipped **without modification**, so
  manually edited bank records are never overwritten with older parsed data;
  exact-duplicate wording is refused at write time as a final guard anyway.
- Atomic bank writes under the per-subject file lock (thread lock + `flock`),
  atomic `fsync`+rename writes for parsed-output annotation updates.
- Idempotent imports (repeated worker passes import nothing new), safe retry
  handling, and protection against concurrent workers: in-process lock +
  non-blocking `flock` (`<STORAGE_ROOT>/reconciliation.lock`); overlapping
  runs return `{"skipped": true, "reason": ...}`.

### Processing status, retries and manual trigger

Per-subject/filename/stage retry bookkeeping persists in the
`reconciliation_state` SQLite table (created by `database.initialize()`):
`status` (`complete`/`retrying`/`failed`), `retry_count`, `last_error`,
`last_attempt_at`, `next_retry_at`, `updated_at`. Bounded retries with
exponential backoff: parse max 3 attempts, sync max 5; backoff starts at
120 s and is capped at 15 min. A stage that exhausts its bound stops retrying
until a successful later run clears it.

New admin endpoints:

- `POST /api/reconciliation/run` (edit-mode admin; `403` for teachers,
  unauthenticated requests, and read-only administrators) — manual trigger
  for testing: one synchronous pass over every registered subject, returning
  the run summary (`subjects`, `per_subject`, `totals`: `parsed_recovered`,
  `imported`, `needs_review`, `invalid`, `duplicates`, failure counters).
- `GET /api/reconciliation/status` (any admin) — worker interval, last-run
  summary and all pending/failed retry states (no file paths, no secrets).
