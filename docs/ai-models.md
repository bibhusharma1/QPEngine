# AI Models

## Stage 1 — Nemotron 3.5 Parse (extraction)

- Model: `nvidia/nemotron-3.5-parse` (`NVIDIA_PARSE_MODEL`).
- Endpoint: `https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-parse/parse`
  (`NVIDIA_PARSE_ENDPOINT`).
- Client: `llm/scraper_parser/nemotron_parse.py::NemotronParseClient`.
- Input: one multipart image POST per page (`document` field, PNG/JPEG only, magic-byte validated).
- Auth: key list `NVIDIA_API` → `NVIDIA_API_KEY` → `NVIDIA_API_KEYS` (comma-separated);
  parse client round-robins across all configured keys per request.
- Retries: `NVIDIA_API_RETRIES` (default 3), exponential backoff `NVIDIA_API_BACKOFF_BASE * 2^(attempt-1)`,
  retried on transport errors and HTTP 408/425/429/5xx; `Retry-After` honored (capped 60 s).
- Output: JSON with `content`/`text`. Empty page content raises. Combined in page order with
  `<class_Page-header>Page N</class_Page-header>` markers.

## Stage 2 — Nemotron Super VL 120B A12B (block classification)

- Model: `nvidia/nemotron-3.5-super-vl-120b-a12b` (`NVIDIA_STRUCTURE_MODEL`).
- Endpoint: `https://integrate.api.nvidia.com/v1/chat/completions` (`NVIDIA_STRUCTURE_ENDPOINT`),
  Chat Completions API.
- Client: `lite/structurer.py::StructureWithLLM.classify_blocks`.
- Request: model, temperature 0, reasoning_effort "none", max_tokens clamped 1024–131072
  (default 4096), response_format json_schema `block_classification`, stream false, timeout 1–600 s
  (default 180). Prompt requests only compact block references (IDs, asset IDs, question numbers,
  subpart labels, marks, sections, ambiguity flags) — never reproduced text.
- Auth: single key `NVIDIA_API` → `NVIDIA_API_KEY` → first of `NVIDIA_API_KEYS` (no rotation).
- Retries: `NVIDIA_STRUCTURE_RETRIES` (default 2, clamp 1–3), linear backoff
  `NVIDIA_STRUCTURE_BACKOFF_BASE * attempt` (base clamped to 30 s). Transports, HTTP errors, invalid
  JSON, schema failures, dangling references are retryable; retry prompt includes correction note.
- Cache: `lite_block_classifications.json` keyed by batch ID + digest.

## Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `NVIDIA_RENDER_DPI` | 200 | page rasterization DPI |
| `NVIDIA_API_TIMEOUT` | 300 | transport timeout (s) |
| `NVIDIA_API_RETRIES` | 3 | Stage 1 retries |
| `NVIDIA_API_BACKOFF_BASE` | 1.0 | Stage 1 backoff base |
| `NVIDIA_STRUCTURE_RETRIES` | 2 | Stage 2 retries |
| `NVIDIA_STRUCTURE_TIMEOUT` | 180 | Stage 2 timeout (s) |
| `NVIDIA_STRUCTURE_MAX_TOKENS` | 4096 | Stage 2 max tokens |
| `NVIDIA_STRUCTURE_BACKOFF_BASE` | 1.0 | Stage 2 backoff base |
| `LITE_STAGE2_BLOCK_BATCH_SIZE` | 24 | Stage 2 batch size (clamped 8–64) |
| `LITE_STAGE2_BLOCK_OVERLAP` | 4 | Stage 2 batch overlap |
| `NVIDIA_API_CONCURRENCY` | 2 | defined but unused (Lite is sequential) |

## Other dependencies

- LibreOffice (`soffice`) — DOCX rasterization (Lite Docker image installs it).
- PyMuPDF (`fitz`) — page rendering (both modes).
- PaddleOCR/PaddlePaddle GPU — local-mode OCR only (optional `ocr-gpu` extra).
- LangChain providers (Gemini/Groq/Mistral/NVIDIA/Cerebras) — legacy local parser only.
- Turso/libSQL — scaffold only, not verified.

## Model references

- [Nemotron 3.5 Parse](https://build.nvidia.com/nvidia/nemotron-3.5-parse)
- [Nemotron Super VL 120B A12B](https://build.nvidia.com/nvidia/nemotron-3-5-super-vl-120b-a12b)

## Related

- [question-extraction.md](question-extraction.md)
- [document-processing.md](document-processing.md)
- [complete-workflow.md](complete-workflow.md)
