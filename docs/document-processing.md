# Document Processing

## Upload

`POST /subjects/{subject}/pdfs` (alias `/documents`), multipart `files[]`. Per file:
plain-filename validation (no separators, ≤180 chars, extension ∈ `.pdf .docx .png .jpg .jpeg`),
magic-byte check, size limit `QUES_MAX_UPLOAD_MB` (413), no overwrite (409), DOCX must contain
`word/` zip entries (415). Bytes stored in `documents.content_blob`. Manifest entry created as
`not_processed`.

## Page rendering (Lite mode, `PARSER_MODE=llm`)

`document_intelligence/page_images.py::render_page_images(path, dpi)` yields
`PageImage(page_number, data, mime, width, height, source_format)` in strict 1-based order:

- **PDF** — PyMuPDF (`fitz`) `get_pixmap` at `NVIDIA_RENDER_DPI` (default 200), RGB, no alpha.
- **DOCX** — headless LibreOffice (`soffice`, 300 s timeout) → PDF → PyMuPDF rasterization.
  Missing LibreOffice raises `RuntimeError`.
- **PNG/JPG/JPEG** — original bytes passed through as page 1.

Page images are held in memory (`dict[int, bytes]`); not persisted.

## Processing trigger

`POST /subjects/{subject}/pdfs/{filename}/process` (or `/documents/.../process`):

- Validates document, subject access, permission `upload_process`.
- Local mode only: OCR pre-flight (pages with <20 native-text chars or image inputs require
  `ensure_cuda()`; else 503).
- Guards: latest job `queued`/`running` → 409; already succeeded and `force != true` → 409;
  `(subject, filename)` in in-memory `_active_jobs` → 409.
- Creates `jobs` row (`queued`), updates manifest, schedules `_run_job` background task.

## Job lifecycle (Lite)

Stages: `queued` → `lite_extraction` → `lite_structuring` → `question_bank_sync` → `complete`.
Final status: `succeeded`, `completed_with_errors` (some questions `unstructured`), or `failed`.

## Status / result endpoints

- `GET /subjects/{subject}/pdfs/{filename}/status` — job payload or `not_processed`.
- `GET /subjects/{subject}/pdfs/{filename}/result?include_questions=true` — parsed bank
  metadata + optional questions; withheld (409) unless latest job succeeded/completed_with_errors.
- `GET /subjects/{subject}/processing-status` — raw manifest.

## Reconstruction

`POST /subjects/{subject}/pdfs/{filename}/reconstruct` runs `DocumentGenerator`
(`functions/ReconstructQues/generator.py`) over the parsed bank into
`output/Reconstruct/<stem>/questions_reconstructed.docx`. Preserves order, labels, marks,
sections, source metadata, embeds available figures/tables, always appends accuracy notice +
source references. Missing assets reported, never guessed. Withheld (409) unless latest job
succeeded. `GET .../reconstruction` downloads the DOCX.

## Deletion

`DELETE .../pdfs/{filename}` — refuses while job runs (409); removes stage output dirs,
source file, `jobs`, `pdf_reviews`, imported `pyq:` questions, manifest entry.

## Related

- [complete-workflow.md](complete-workflow.md)
- [question-extraction.md](question-extraction.md)
- [ai-models.md](ai-models.md)
- [jobs-and-errors.md](jobs-and-errors.md)
