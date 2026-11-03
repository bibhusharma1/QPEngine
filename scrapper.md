# Scrapper

Scrapper reads an original PDF and writes page text, rendered pages, extracted figures, a manifest, and `questions_scrapper.json`. The FastAPI process calls the existing `functions/QuesScrapper` implementation directly.

Each PDF is processed in a temporary work folder. The new artifacts are published only after extraction and parsing both succeed. A failed scrape never sends an old manifest to Parser.

Artifacts are stored under `<subject>/output/Scrapper/<PDF stem>/`. The source PDF stays in `<subject>/source/`. Native text is extracted on CPU; when GPU OCR is unavailable, short nonempty native text is retained for review. Empty scanned pages use the optional `ocr-gpu` extra and need a compatible NVIDIA setup. Install locally with `uv sync --extra ocr-gpu`; for Docker set `INSTALL_OCR_GPU=true` and start with `docker compose -f compose.yaml -f compose.gpu.yaml up --build`.

> **Lite mode note:** with `PARSER_MODE=llm` the Scrapper stage is bypassed
> completely — Nemotron Parse consumes rendered page images directly and no
> Scrapper artifacts are produced. See
> [docs/BACKEND_ARCHITECTURE.md §5](docs/BACKEND_ARCHITECTURE.md#5-active-parsing-pipeline--nvidia-lite-two-stage-parser_modelllm).
