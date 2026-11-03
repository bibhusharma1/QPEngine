# Parser

Parser consumes the manifest created by Scrapper and produces one validated `questions_parsed.json` per PDF. It keeps the existing question hierarchy, section, number, marks, page, figure, and table metadata.

The API runs Parser immediately after a successful fresh scrape. A configured provider key enables the corresponding model provider; if no provider is available, the existing fallback parser is used. Invalid manifests, unreadable page text, and provider failures are recorded as failed jobs.

The authoritative question bank is saved in `<subject>/output/Parser/<PDF stem>/questions_parsed.json`. See [known accuracy limits](docs/LIMITATIONS.md).

## Lite mode (`PARSER_MODE=llm`)

In Lite mode there is **no Scrapper stage and no legacy Parser run**. The job
drives `LiteTwoStageProcessor` directly (Nemotron 3.5 Parse extraction, then
Nemotron Super VL block classification, then deterministic Python question
assembly), writing `questions_parsed.json` — plus the extraction cache, block
classification cache, structuring checkpoint and cropped figure assets — under
`<subject>/output/Parser/<stem>/`. See
[docs/BACKEND_ARCHITECTURE.md §5](docs/BACKEND_ARCHITECTURE.md#5-active-parsing-pipeline--nvidia-lite-two-stage-parser_modelllm).

