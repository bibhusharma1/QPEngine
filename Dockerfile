# NVIDIA Lite mode Dockerfile (default for Render/local Docker)
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Run the service without root privileges. Keep application data and temporary
# conversion files writable by the runtime account, including named Docker volumes.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /home/app --create-home app \
    && mkdir -p /data /tmp \
    && chown -R app:app /app /data /home/app \
    && chmod 1777 /tmp

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PARSER_MODE=llm \
    HOME=/home/app \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/tmp/app-cache \
    XDG_CONFIG_HOME=/tmp/app-config \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY config ./config
COPY functions ./functions
COPY src ./src
COPY pipeline_runner.py ./pipeline_runner.py
COPY scripts ./scripts

USER app

EXPOSE 8000
CMD ["/bin/sh", "-c", "exec /app/.venv/bin/python -m uvicorn src.masterdeorca.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
