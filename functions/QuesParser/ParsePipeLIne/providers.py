"""Multi-provider LLM system with fallback chain and bounded retries.

Supports: Gemini, Mistral, Cerebras, Groq, NVIDIA.
Reads credentials exclusively from environment variables.
Validates provider availability before routing requests.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider."""
    name: str
    env_key: str
    default_model: str
    supports_vision: bool = True
    is_local: bool = False
    max_retries: int = 2
    retry_delay: float = 1.0


# Provider registry — order defines default fallback priority
PROVIDER_REGISTRY: list[ProviderConfig] = [
    ProviderConfig(
        name="gemini",
        env_key="GEMINI_API_KEY",
        default_model="gemini-2.5-flash",
        supports_vision=True,
    ),
    ProviderConfig(
        name="groq",
        env_key="GROQ_API_KEY",
        default_model="qwen/qwen3.8-27b",
        supports_vision=False,
    ),
    ProviderConfig(
        name="mistral",
        env_key="MISTRAL_API_KEY",
        default_model="open-mistral-7b",
        supports_vision=True,
    ),
    ProviderConfig(
        name="nvidia",
        env_key="NVIDIA_API_KEY",
        default_model="meta/llama3-8b-instruct",
        supports_vision=False,
    ),
    ProviderConfig(
        name="cerebras",
        env_key="CEREBRAS_API_KEY",
        default_model="llama-3.3-70b",
        supports_vision=False,
    ),
]


def _provider_by_name(name: str) -> ProviderConfig | None:
    for p in PROVIDER_REGISTRY:
        if p.name == name:
            return p
    return None


# ---------------------------------------------------------------------------
# GPU verification
# ---------------------------------------------------------------------------

def verify_gpu_available() -> str:
    """Verify NVIDIA CUDA is available. Returns device name or raises."""
    try:
        import subprocess
        res = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().split("\n")[0]
    except Exception:
        pass
    raise RuntimeError("NVIDIA GPU required for designated local operations.")


def gpu_available() -> bool:
    """Check GPU without raising."""
    try:
        verify_gpu_available()
        return True
    except RuntimeError:
        return False


def get_gpu_device_name() -> str | None:
    try:
        return verify_gpu_available()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Provider instantiation
# ---------------------------------------------------------------------------

def _build_chat_model(provider: ProviderConfig, model: str | None = None,
                      max_output_tokens: int | None = None,
                      timeout: float | None = None,
                      max_retries: int | None = None) -> BaseChatModel:
    """Instantiate a LangChain chat model for the given provider."""
    api_key = os.environ.get(provider.env_key, "").strip()
    if not api_key:
        raise ValueError(f"No API key for {provider.name} (env: {provider.env_key})")

    use_model = model or provider.default_model

    if provider.name == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        kwargs = dict(model=use_model, google_api_key=api_key, temperature=0)
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return ChatGoogleGenerativeAI(**kwargs)
    elif provider.name == "mistral":
        from langchain_mistralai import ChatMistralAI
        kwargs = dict(model=use_model, api_key=api_key, temperature=0)
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return ChatMistralAI(**kwargs)
    elif provider.name == "groq":
        from langchain_groq import ChatGroq
        kwargs = dict(model=use_model, api_key=api_key, temperature=0)
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return ChatGroq(**kwargs)
    elif provider.name == "nvidia":
        from langchain_nvidia_ai_endpoints import ChatNVIDIA
        kwargs = dict(model=use_model, api_key=api_key, temperature=0)
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return ChatNVIDIA(**kwargs)
    elif provider.name == "cerebras":
        from langchain_cerebras import ChatCerebras
        kwargs = dict(model=use_model, api_key=api_key, temperature=0)
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return ChatCerebras(**kwargs)
    else:
        raise ValueError(f"Unknown provider: {provider.name}")


# ---------------------------------------------------------------------------
# Provider manager — fallback chain with retries
# ---------------------------------------------------------------------------

@dataclass
class ProviderResult:
    """Result from a provider call attempt."""
    provider_name: str
    success: bool
    result: Any = None
    error: str | None = None
    attempts: int = 0
    duration_ms: float = 0.0


@dataclass
class ProviderManager:
    """Manages multi-provider LLM access with fallback and retries."""
    preferred_provider: str | None = None
    _available: list[ProviderConfig] = field(default_factory=list)
    _validated: bool = False

    def validate_providers(self) -> list[str]:
        """Check which providers have API keys configured. Returns available names."""
        self._available = []
        for provider in PROVIDER_REGISTRY:
            key = os.environ.get(provider.env_key, "").strip()
            if key:
                self._available.append(provider)
                logger.info("Provider %s: configured", provider.name)
            else:
                logger.debug("Provider %s: no API key", provider.name)

        self._validated = True
        return [p.name for p in self._available]

    def get_provider_chain(self, require_vision: bool = False) -> list[ProviderConfig]:
        """Get ordered provider list, optionally filtered for vision support."""
        if not self._validated:
            self.validate_providers()

        chain = self._available[:]

        # Put preferred provider first if specified
        if self.preferred_provider and self.preferred_provider != "auto":
            pref = _provider_by_name(self.preferred_provider)
            if pref and pref in chain:
                chain.remove(pref)
                chain.insert(0, pref)

        if require_vision:
            chain = [p for p in chain if p.supports_vision]

        return chain

    def invoke_with_fallback(
        self,
        prompt_fn,
        *,
        require_vision: bool = False,
        structured_output: type[BaseModel] | None = None,
        max_output_tokens: int | None = None,
    ) -> ProviderResult:
        """Try providers in order with bounded retries until one succeeds."""
        chain = self.get_provider_chain(require_vision=require_vision)
        if not chain:
            return ProviderResult(
                provider_name="none",
                success=False,
                error="No compatible providers available",
            )

        errors: list[str] = []

        for provider in chain:
            for attempt in range(1, provider.max_retries + 1):
                start = time.monotonic()
                try:
                    model = _build_chat_model(provider, max_output_tokens=max_output_tokens)
                    if structured_output:
                        model = model.with_structured_output(structured_output)
                    result = prompt_fn(model)
                    duration = (time.monotonic() - start) * 1000
                    logger.info(
                        "Provider %s succeeded (attempt %d, %.0fms)",
                        provider.name, attempt, duration,
                    )
                    return ProviderResult(
                        provider_name=provider.name,
                        success=True,
                        result=result,
                        attempts=attempt,
                        duration_ms=duration,
                    )
                except Exception as exc:
                    duration = (time.monotonic() - start) * 1000
                    err_msg = f"{provider.name} attempt {attempt}: {type(exc).__name__}: {exc}"
                    # Never expose secrets in logs or errors
                    key_val = os.environ.get(provider.env_key, "")
                    if key_val and key_val in err_msg:
                        err_msg = err_msg.replace(key_val, "***")
                    errors.append(err_msg)
                    logger.warning(err_msg)

                    if attempt < provider.max_retries:
                        time.sleep(provider.retry_delay * attempt)

        return ProviderResult(
            provider_name="all_failed",
            success=False,
            error=f"All providers failed: {'; '.join(errors)}",
        )
