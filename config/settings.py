"""Application settings loaded from the single project-root .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def resolve_path(value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base or PROJECT_ROOT) / path).resolve()


# Render-only deployment configuration
STORAGE_ROOT = resolve_path(setting("QUES_STORAGE_ROOT", "/data"))
DEFAULT_SUBJECT = setting("QUES_DEFAULT_SUBJECT", "OperatingSys")
MAX_UPLOAD_BYTES = int(setting("QUES_MAX_UPLOAD_MB", "50")) * 1024 * 1024
API_PORT = int(setting("QUES_PORT", "8000"))
DATABASE_BACKEND = setting("DATABASE_BACKEND", "turso").casefold()
# Used only by explicit offline legacy-database tools; runtime services never read it.
DATABASE_PATH = resolve_path(setting("DATABASE_PATH", "/data/masterdeorca.sqlite"))
_storage_backend = setting("STORAGE_BACKEND", "sqlite").casefold()
STORAGE_BACKEND = "sqlite" if _storage_backend == "local" else _storage_backend
APP_ENV = setting("APP_ENV", "render").casefold()
TURSO_DATABASE_URL = setting("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = setting("TURSO_AUTH_TOKEN", "")
RECONCILE_ENABLED = setting("QUES_RECONCILE_ENABLED", "true").casefold() in {
    "1", "true", "yes", "on"
}
RECONCILE_INTERVAL_HOURS = int(setting("QUES_RECONCILE_INTERVAL_HOURS", "12"))
JWT_SECRET = setting("QUES_JWT_SECRET", "")
JWT_TTL_MINUTES = int(setting("QUES_JWT_TTL_MINUTES", "480"))
ADMIN_EMAIL = setting("QUES_ADMIN_EMAIL", "admin@example.com").lower()
ADMIN_PASSWORD = setting("QUES_ADMIN_PASSWORD", "admin@123")
DEFAULT_TEACHER_PASSWORD = setting("QUES_DEFAULT_TEACHER_PASSWORD", "admin@123")
CORS_ORIGINS = [origin.strip() for origin in setting("QUES_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
QUES_TOTP_ENCRYPTION_KEY = setting("QUES_TOTP_ENCRYPTION_KEY", "")
TOTP_ISSUER = setting("QUES_TOTP_ISSUER", "Smart QPG")
TOTP_CHALLENGE_TTL_MINUTES = int(setting("QUES_TOTP_CHALLENGE_TTL_MINUTES", "5"))
TOTP_FAILED_ATTEMPT_THRESHOLD = int(setting("QUES_TOTP_FAILED_ATTEMPT_THRESHOLD", "5"))
TOTP_COOLDOWN_SECONDS = int(setting("QUES_TOTP_COOLDOWN_SECONDS", "120"))

SCRAPPER_ROOT = resolve_path(setting("QUES_SCRAPPER_OUTPUT_DIR", str(STORAGE_ROOT / DEFAULT_SUBJECT / "output" / "Scrapper")))
PARSER_ROOT = resolve_path(setting("QUES_PARSER_OUTPUT_DIR", str(STORAGE_ROOT / DEFAULT_SUBJECT / "output" / "Parser")))
RECONSTRUCT_ROOT = resolve_path(setting("QUES_RECON_OUTPUT_DIR", str(STORAGE_ROOT / DEFAULT_SUBJECT / "output" / "Reconstruct")))



# Document intelligence / scraper-parser configuration
# Default to NVIDIA Lite mode for Render; can be overridden for local dev
PARSER_MODE = setting("PARSER_MODE", "llm").lower()
NVIDIA_PARSE_MODEL = setting("NVIDIA_PARSE_MODEL", "nvidia/nemotron-3.5-parse")
NVIDIA_STRUCTURE_MODEL = setting(
    "NVIDIA_STRUCTURE_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b"
)
NVIDIA_PARSE_ENDPOINT = setting("NVIDIA_PARSE_ENDPOINT", "https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-parse/parse")
NVIDIA_STRUCTURE_ENDPOINT = setting(
    "NVIDIA_STRUCTURE_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions"
)
NVIDIA_STRUCTURE_RETRIES = int(setting("NVIDIA_STRUCTURE_RETRIES", "3"))
NVIDIA_STRUCTURE_MAX_TOKENS = int(setting("NVIDIA_STRUCTURE_MAX_TOKENS", "4096"))
NVIDIA_STRUCTURE_TIMEOUT = float(setting("NVIDIA_STRUCTURE_TIMEOUT", "240"))
NVIDIA_STRUCTURE_CONNECT_TIMEOUT = float(setting("NVIDIA_STRUCTURE_CONNECT_TIMEOUT", "20"))
NVIDIA_STRUCTURE_READ_TIMEOUT = float(
    setting("NVIDIA_STRUCTURE_READ_TIMEOUT", str(NVIDIA_STRUCTURE_TIMEOUT))
)
NVIDIA_STRUCTURE_BACKOFF_BASE = float(setting("NVIDIA_STRUCTURE_BACKOFF_BASE", "1.0"))
LITE_STAGE2_BLOCK_BATCH_SIZE = int(setting("LITE_STAGE2_BLOCK_BATCH_SIZE", "2"))
LITE_STAGE2_BLOCK_MIN_BATCH_SIZE = int(setting("LITE_STAGE2_BLOCK_MIN_BATCH_SIZE", "1"))
LITE_STAGE2_BLOCK_OVERLAP = int(setting("LITE_STAGE2_BLOCK_OVERLAP", "0"))
LITE_STAGE2_CONCURRENCY = int(setting("LITE_STAGE2_CONCURRENCY", "3"))
NVIDIA_STRUCTURE_REASONING_EFFORT = setting("NVIDIA_STRUCTURE_REASONING_EFFORT", "low")
NVIDIA_API = setting("NVIDIA_API", "")
NVIDIA_API_KEYS = [k.strip() for k in setting("NVIDIA_API_KEYS", "").split(",") if k.strip()]
NVIDIA_API_TIMEOUT = float(setting("NVIDIA_API_TIMEOUT", "300"))
NVIDIA_API_RETRIES = int(setting("NVIDIA_API_RETRIES", "3"))
NVIDIA_API_CONCURRENCY = int(setting("NVIDIA_API_CONCURRENCY", "2"))
NVIDIA_API_BACKOFF_BASE = float(setting("NVIDIA_API_BACKOFF_BASE", "1.0"))
NVIDIA_RENDER_DPI = int(setting("NVIDIA_RENDER_DPI", "200"))
