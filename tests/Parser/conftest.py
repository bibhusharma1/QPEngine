# Puts the project root on sys.path so tests can import project packages
# without installation.

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Every provider API key the recon code may consult. Tests NEVER touch
# real APIs: an autouse fixture strips them all before each test, so
# nothing in this suite can accidentally auto-select a provider from a
# real .env. Tests that need a key present opt in via monkeypatch.setenv.
PROVIDER_ENV_KEYS = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "NVIDIA_API_KEY",
    "CEREBRAS_API_KEY",
)


@pytest.fixture(autouse=True)
def isolate_provider_keys(monkeypatch):
    for key in PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
