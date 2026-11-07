# NVIDIA Lite mode Dockerfile (default for Render/local Docker)
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PARSER_MODE=llm

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY config ./config
COPY functions ./functions
COPY src ./src
COPY pipeline_runner.py ./pipeline_runner.py
COPY scripts ./scripts

EXPOSE 8000
CMD ["/bin/sh", "-c", "exec /app/.venv/bin/python -m uvicorn src.masterdeorca.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
