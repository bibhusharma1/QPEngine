# Jobs and Errors

## Execution model

FastAPI `BackgroundTasks` runs one job per document in the API process; `_active_jobs`
(in-memory, lock-guarded) prevents duplicates. `jobs` table upserted at every stage
transition (`database.save_job`); manifest and `metadata/<stem>.processing.json` updated in the
same `_update()` call; subject log appended.

## Statuses

- Job `status`: `queued`, `running`, `succeeded`, `completed_with_errors`, `failed`.
- Job `stage`: `queued`, `lite_extraction`, `lite_structuring`, `question_bank_sync`,
  `complete` (lite); `queued`, `scraping`, `parsing`, `publishing`, `question_bank_sync`,
  `complete` (local); `interrupted` (restart-killed).

## Restart safety

Startup marks stranded `queued`/`running` jobs failed (`mark_interrupted_jobs()`). Lite
checkpoints/caches let a fresh `force=true` run avoid repeating paid extraction. Reconciliation
recovers interrupted documents autonomously.

## Polling

- `GET /jobs/{job_id}` (admin) or `GET /subjects/{s}/pdfs/{f}/status` (subject-access user)
  returns `{job_id, subject, filename, status, stage, started_at, updated_at, error, result, progress}`.
- `result.progress`: `{total_questions, processed_questions, current_question, current_page,
  failed_questions[], completion_percent, status}`.

## Reconciliation

`src/masterdeorca/reconciliation/` — autonomous recovery hosted inside the FastAPI process
(daemon thread; first pass runs immediately, then every `QUES_RECONCILE_INTERVAL` seconds,
clamped 180–300, default 240).

Per pass, per registered subject, per source document:
1. Skip documents with active `queued`/`running` job.
2. Parse recovery — parsed JSON missing: local mode replays scraped manifest through legacy
   parser (staged); lite mode locates source document by stem and runs `LiteTwoStageProcessor`
   directly. Partial leftovers (`.tmp/.partial/.part`) cleaned.
3. Deterministic validation — `validation.assess_question()` classifies every record as
   `valid`, `needs_review`, `invalid`, `duplicate` (empty text, numeric/marks fragments,
   separator lines, OCR noise, question signals, finite positive marks). No LLM calls.
   Verdicts persisted as per-record `validation` annotations.
4. Duplicate marking — exact normalized-text signature + `difflib` similarity ≥ 0.92, per
   subject and within batch.
5. Bank sync — only `valid` records import via `sync_from_parsed(protect_edits=True)`;
   manifest entry becomes `succeeded`.
6. Retry bookkeeping — per `(subject, filename, stage)` rows in `reconciliation_state`;
   bounded retries (parse ≤3, sync ≤5) with exponential backoff 120 s → 900 s.

Concurrency: in-process `threading.Lock` + non-blocking `flock` on
`<STORAGE_ROOT>/reconciliation.lock`. Admin endpoints: `POST /api/reconciliation/run`
(edit-mode admin, synchronous forced pass), `GET /api/reconciliation/status`.

## Error handling conventions

- `HTTPException` with `detail` (string or object); business conflicts 409; validation 422
  (including custom paper-route shape); not-found 404; auth 401; RBAC 403; upload 400/413/415;
  OCR-unavailable 503 (local mode only).
- Job failures store sanitized message (env values containing `API_KEY`/`SECRET`/`PASSWORD`
  replaced with `[REDACTED]`); details go to subject log.
- JSON stores: unreadable/corrupt files archived as `*.corrupt-<ts>` and rebuilt; per-file errors
  never fail an entire listing.
- Empty results are successful 200 with empty arrays/zero totals.

## Related

- [document-processing.md](document-processing.md)
- [database.md](database.md)
- [complete-workflow.md](complete-workflow.md)
