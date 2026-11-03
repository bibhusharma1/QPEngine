# Docker (Lite mode only)

This project supports **NVIDIA Lite mode** via Docker. The **Local parser mode** must be run directly from the terminal using your local Python environment (do not use Docker for Local mode).

## Lite mode (Docker)
```bash
docker build -t masterdeorca-lite .
docker run -p 8000:8000 \
  -e TURSO_DATABASE_URL=... \
  -e TURSO_AUTH_TOKEN=... \
  -e QUES_JWT_SECRET=... \
  -e NVIDIA_API_KEYS=... \
  -e PARSER_MODE=llm \
  masterdeorca-lite
```

## Local development (terminal)
- **Lite mode:** `PARSER_MODE=llm uvicorn src.masterdeorca.app:app --reload --host 0.0.0.0 --port 8000`
- **Local mode:** `PARSER_MODE=local uvicorn src.masterdeorca.app:app --reload --host 0.0.0.0 --port 8000` (uses local environment/deps as configured)

## Render
Render uses the root `Dockerfile` with Lite mode by default. Set NVIDIA API credentials and Turso credentials via environment variables.
