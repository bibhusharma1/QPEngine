from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from config import settings
from src.masterdeorca.document_intelligence.interfaces.base import ExtractedDocument
from src.masterdeorca.document_intelligence.page_images import render_page_images

logger = logging.getLogger(__name__)


class NemotronParseClient:
    def __init__(self, transport=None):
        self.endpoint = getattr(settings, "NVIDIA_PARSE_ENDPOINT", "https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-parse/parse")
        single = settings.setting("NVIDIA_API", "") or settings.setting("NVIDIA_API_KEY", "")
        keys = [single] if single else []
        if not keys:
            keys = list(getattr(settings, "NVIDIA_API_KEYS", []) or [])
        self.keys = keys
        self.timeout = float(getattr(settings, "NVIDIA_API_TIMEOUT", 300))
        self.retries = int(getattr(settings, "NVIDIA_API_RETRIES", 3))
        self.backoff_base = float(getattr(settings, "NVIDIA_API_BACKOFF_BASE", 1.0))
        self.transport = transport
        self._idx = 0

    def _next_key(self) -> str:
        if not self.keys:
            raise RuntimeError(
                "No NVIDIA API configured (NVIDIA_API, NVIDIA_API_KEY, or NVIDIA_API_KEYS)."
            )
        key = self.keys[self._idx % len(self.keys)]
        self._idx += 1
        return key

    def parse_page_image(self, image_bytes: bytes, mime: str, page_number: int | None = None, filename: str | None = None) -> dict[str, Any]:
        mime = mime.lower()
        signatures = {
            "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": (".jpg", b"\xff\xd8\xff"),
        }
        signature = signatures.get(mime)
        if signature is None:
            raise ValueError(f"Nemotron only accepts PNG/JPEG page images, got {mime!r}")
        extension, magic = signature
        if not image_bytes.startswith(magic):
            raise ValueError(f"Page {page_number or 1} is not valid {mime} image data")
        image_filename = filename or f"page{page_number or 1}{extension}"
        allowed_extensions = {extension, ".jpeg"} if extension == ".jpg" else {extension}
        if Path(image_filename).suffix.lower() not in allowed_extensions:
            raise ValueError(f"Image filename {image_filename!r} does not match MIME type {mime}")
        logger.info(
            "NEMOTRON_REQUEST: url=%s page=%s image=%s bytes=%d mime=%s",
            self.endpoint, page_number, image_filename, len(image_bytes), mime,
        )
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            key = self._next_key()
            try:
                retry_after = None
                with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                    resp = client.post(
                        self.endpoint,
                        files={"document": (image_filename, image_bytes, mime)},
                        headers={"Authorization": f"Bearer {key}"},
                    )
                logger.info(
                    "NEMOTRON_RESPONSE: page=%s image=%s mime=%s status=%s",
                    page_number, image_filename, mime, resp.status_code,
                )
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        return {"content": resp.text}
                body = resp.text[:500]
                if resp.status_code in {408, 425, 429} or resp.status_code >= 500:
                    last_exc = RuntimeError(f"Nemotron parse HTTP {resp.status_code}: {body}")
                    logger.error(
                        "NEMOTRON_ERROR: page=%s image=%s mime=%s status=%s body=%s",
                        page_number, image_filename, mime, resp.status_code, body,
                    )
                    retry_after = resp.headers.get("Retry-After")
                else:
                    raise RuntimeError(
                        f"Nemotron parse failed for page {page_number} image {image_filename} "
                        f"({mime}) with HTTP {resp.status_code}: {body}"
                    )
            except httpx.HTTPError as exc:
                last_exc = RuntimeError(
                    f"Nemotron parse network error for page {page_number} image {image_filename}: {exc}"
                )
                logger.error(
                    "NEMOTRON_ERROR: page=%s image=%s mime=%s network=%s",
                    page_number, image_filename, mime, exc,
                )
                retry_after = None
            if attempt < self.retries:
                delay = self.backoff_base * (2 ** (attempt - 1))
                if retry_after:
                    try:
                        delay = max(delay, min(60.0, float(retry_after)))
                    except ValueError:
                        try:
                            retry_at = parsedate_to_datetime(retry_after)
                            if retry_at.tzinfo is None:
                                retry_at = retry_at.replace(tzinfo=timezone.utc)
                            delay = max(
                                delay,
                                min(60.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())),
                            )
                        except (TypeError, ValueError, OverflowError):
                            pass
                time.sleep(delay)
        raise (last_exc or RuntimeError("Nemotron parse failed after retries"))

    def combine_pages(self, pages: list[tuple[int, dict[str, Any]]]) -> str:
        parts: list[str] = []
        for expected_page, (page_number, raw) in enumerate(pages, start=1):
            if page_number != expected_page:
                raise RuntimeError(
                    f"Nemotron page results are out of order: expected page {expected_page}, got {page_number}"
                )
            content = raw.get("content") if isinstance(raw, dict) else None
            if content is None and isinstance(raw, dict):
                content = raw.get("text")
            if content is None:
                content = raw if not isinstance(raw, dict) else ""
            if isinstance(content, (dict, list)):
                import json as _json

                content = _json.dumps(content)
            content = str(content or "")
            parts.append(f"<class_Page-header>Page {page_number}</class_Page-header>\n{content}")
        return "\n\n".join(parts)

    def parse_document_normalized(self, path: Path) -> ExtractedDocument:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        pages_list: list[tuple[int, dict[str, Any]]] = []
        for page in render_page_images(path):
            raw = self.parse_page_image(page.data, page.mime, page.page_number)
            pages_list.append((page.page_number, raw if isinstance(raw, dict) else {"content": str(raw)}))
        combined = self.combine_pages(pages_list)
        raw_payload = {"content": combined, "pages": [{"page": n, "raw": r} for n, r in pages_list]}
        return ExtractedDocument(
            text=combined,
            pages=[{"number": n, "text": str((r.get("content") or r.get("text") or ""))} for n, r in pages_list],
            tables=[],
            figures=[],
            metadata={"raw": raw_payload, "extraction_provider": "nvidia_nemotron", "page_count": len(pages_list)},
            source_path=path,
        )

    def parse_document(self, path: Path) -> ExtractedDocument:
        return self.parse_document_normalized(path)
