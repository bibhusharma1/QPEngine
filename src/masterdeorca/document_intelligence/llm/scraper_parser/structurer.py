from __future__ import annotations

import json
from typing import Any

from config import settings
from src.masterdeorca.document_intelligence.llm.clients.nvidia_keys import KeyManager


class QuestionStructurer:
    def __init__(self, key_manager: KeyManager | None = None):
        self.key_manager = key_manager or KeyManager()
        self.structure_model = getattr(settings, "NVIDIA_STRUCTURE_MODEL", "nvidia/llama-3.1-nemotron-70b-instruct")

    def structure(self, extracted: dict[str, Any]) -> list[dict[str, Any]]:
        # Send only extracted content + figure refs, no OCR/reparse
        prompt = {
            "model": self.structure_model,
            "messages": [
                {"role": "system", "content": "Convert extracted document content to MasterDeOrca question structure with sections, instructions, questions, subquestions, marks, numbering, tables, figures, pages."},
                {"role": "user", "content": json.dumps({
                    "text": extracted.get("text"),
                    "tables": extracted.get("tables"),
                    "figures": extracted.get("figures"),
                    "pages": extracted.get("pages"),
                    "coordinates": extracted.get("coordinates"),
                    "classes": extracted.get("classes"),
                })},
            ],
            "temperature": 0.1,
        }
        # Try to call if keys available; otherwise return minimal structure
        key = self.key_manager.get_key()
        if not key:
            return []
        try:
            import httpx
            resp = httpx.post("https://integrate.api.nvidia.com/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=prompt, timeout=300)
            if resp.status_code >= 400:
                return []
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "[]")
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        except Exception:
            pass
        return []
