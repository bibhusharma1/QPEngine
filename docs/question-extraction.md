# Question Extraction

## Shared schema

`functions/QuesParser/ParsePipeLIne/schema.py` (SCHEMA_VERSION "1.1.0") is authoritative for
both modes. Key types: `QuestionBank` (schema_version, metadata, questions[]),
`QuestionRecord` (question_id, original_text, cleaned_text, labels/numbers, marks with
`coerce_marks`, hierarchy sub_questions/parent_question_ref, section metadata, source refs,
assets, tables, confidence, validation_issues, review_status, processing_notes),
`UnstructuredQuestionRecord` (status="unstructured", failure_reason ∈ {token_limit, invalid_json,
api_error, response_format, schema_validation, empty_response, structuring_error}).

## Stage 1 — Nemotron 3.5 Parse extraction

`llm/scraper_parser/nemotron_parse.py::NemotronParseClient`:
- POST `NVIDIA_PARSE_ENDPOINT` multipart field `document` = page image (PNG/JPEG only,
  magic-byte validated).
- Keys: `NVIDIA_API` → `NVIDIA_API_KEY` → `NVIDIA_API_KEYS` (comma-separated), round-robined
  per request.
- Retries: `NVIDIA_API_RETRIES` (default 3), exponential backoff, retried on
  408/425/429/5xx; `Retry-After` honored (capped 60 s).
- Response JSON with `content`/`text`. Empty page content raises.
- `combine_pages()` verifies page order, joins pages with
  `<class_Page-header>Page N</class_Page-header>` markers (double newline).

## Normalization + figure cropping

- `NemotronNormalizer.normalize()` strips coordinate/class markers into clean text,
  extracting coordinates, classes, LaTeX tables, math.
- `lite/figure_assets.py::crop_figure_assets()` scans each page's raw Parse content for
  Figure/Image/Picture/Chart/Graph/Plot/Diagram spans with coordinates, maps to
  figure/graph/diagram, computes bbox from first/last coordinate pair, crops page image to PNG
  at `output/Parser/<stem>/assets/figures/page-NNNNN-<class>-NNN.png`. Asset id =
  `sha256("<digest>:<page>:<class>:<bbox>")[:16]`. Classes without ≥2 coords or empty crops
  skipped with `LITE_FIGURE_*` warnings.

## Stage 2 — Nemotron Super VL block classification

`lite/structurer.py::StructureWithLLM.classify_blocks(blocks, figure_assets, batch_id)`:
- Blocks parsed from Parse marker grammar into `block_id = p{page}-b{order}-{sha256[:16]}`,
  layout_type, original_text, cleaned text, per-page figure_asset_ids.
- Batches of `LITE_STAGE2_BLOCK_BATCH_SIZE` (default 24, clamped 8–64) with
  `LITE_STAGE2_BLOCK_OVERLAP` (default 4); batch id = SHA-256 of sorted block IDs.
- Chat Completions to `NVIDIA_STRUCTURE_ENDPOINT`, model `NVIDIA_STRUCTURE_MODEL`,
  temperature 0, reasoning_effort "none", max_tokens clamped 1024–131072 (default 4096),
  response_format json_schema `block_classification`, stream false, timeout 1–600 s (default 180).
- Response: `BlockBatchClassification` (Pydantic, extra="forbid"): questions[], sections[],
  instruction/other/ambiguous block lists. All referenced block/asset IDs must exist in batch.
- Retries up to `NVIDIA_STRUCTURE_RETRIES` (default 2, clamp 1–3), linear backoff, corrective
  prompt on validation errors. Final exhaustion raises `RuntimeError`.
- Auth: single key `NVIDIA_API` → `NVIDIA_API_KEY` → first of `NVIDIA_API_KEYS` (no rotation).
- Cache: `lite_block_classifications.json` (`{version:2, source_sha256, batch_size, overlap, batches}`).

Note: `StructureWithLLM.structure()` (per-question LLM structuring) is implemented + tested
but dormant; active path uses batched classification + deterministic assembly.

## Candidate resolution & boundary recovery

`two_stage._resolve_question_candidates()`:
- Normalizes question numbers (`Q.2`, `Q2`, `2` → `Q2`); numbered subparts become parent + child.
- Unions section refs, instruction blocks, figure assets, ambiguous blocks per question.
- Sorts blocks by reading order; nests sub-questions recursively by label.
- Orphan ambiguous blocks attached to nearest question by reading order.
- `_apply_explicit_question_boundaries()` re-segments questions from raw block stream for
  `Q. N` / `Q N` / `Q.N(a)` headings (excluding page-header/footer), marking recovered/extended
  questions `ambiguous`.

## Deterministic assembly

`_build_question_record()` — pure Python, no LLM:
- Text assembled from resolved blocks (`_question_content_from_blocks`), preserving paragraph breaks.
- Cleanup (`_clean_block_text`/`_clean_question_content`): strips Parse markers, `<br>`→newline,
  `<sub>`→`_(x)`, `<sup>`→`^(x)`, removes markdown bold/heading markers except in code blocks,
  strips printed question-number prefix and subpart label from first non-code block only.
- `_looks_like_code` switches to preserve-code mode.
- Whitespace normalized; Unicode round-trips.
- Sub-question records: `sub_id = sha256(parent_id:label)[:16]`, `original_question_number = parent + label`.
- Marks from classifier (`marks` string or null; `marks_notation` mirrors). Tables list empty in Lite.
- `processing_notes`: "Text resolved from cached Nemotron Parse block references."

## Validation & unstructured fallback

Per candidate, assembled record validated by re-validating whole bank (`QuestionBank.model_validate`).
If assembly/validation raises, candidate stored as `unstructured` record preserving full source text
and pages, increments `metadata.incomplete_records`; job continues.

## Question Bank sync

On completion `question_bank.sync_pdf(subject, filename)`:
- Manifest entry must be `succeeded`/`completed_with_errors` with subject-contained `result_json` path.
- `sync_from_parsed()` flattens bank (leaf rows only), filters to validation-`valid` records, merges
  into `<subject>/question_bank.json` under subject bank lock with idempotent stable IDs.
- Lite jobs rely on dashboard cache 10 s TTL (no explicit `dashboard_metrics.invalidate()`).

## Stable IDs & manual CRUD

- Parsed records keep deterministic SHA-based IDs.
- Manual/synced rows use UUID5 over `subject/source_pdf/identity` (namespace
  `8f3c2a1e-6d4b-4e5a-9c7f-2b1d0a5e7c33`).
- Sync merges by `question_id`; existing rows refreshed preserving operator fields; `protect_edits`
  skips rows whose wording no longer matches; exact-signature duplicates refused.
- Manual CRUD: `POST /api/question-bank` (required subject_id, text, positive int marks; optional
  stable question_id, idempotency_key, section/source/page/asset/table metadata, priority),
  `PATCH` (supplied fields only; ID/subject immutable), `DELETE`. Retry-safe.

## Priorities

Separate store `.masterdeorca/question_priorities.json` (`{version:1, priorities:{question_id:1..3}}`).
`GET/PUT/DELETE /api/question-priorities`. Existence verified against subject banks (404 unknown,
403 when question exists only in unassigned subjects).

## Related

- [complete-workflow.md](complete-workflow.md)
- [ai-models.md](ai-models.md)
- [document-processing.md](document-processing.md)
