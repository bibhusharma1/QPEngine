from __future__ import annotations

import os
import time
from typing import Iterator

from config import settings


class KeyManager:
    def __init__(self):
        api_key = settings.setting("NVIDIA_API", "") or settings.setting("NVIDIA_API_KEY", "")
        self.keys = (
            [api_key]
            if api_key
            else list(settings.NVIDIA_API_KEYS)
            or [k.strip() for k in os.environ.get("NVIDIA_API_KEYS", "").split(",") if k.strip()]
        )
        self.failed = {}
        self.index = 0

    def get_key(self) -> str | None:
        if not self.keys:
            return None
        now = time.time()
        for _ in range(len(self.keys)):
            key = self.keys[self.index]
            self.index = (self.index + 1) % len(self.keys)
            info = self.failed.get(key)
            if info and info.get("permanent"):
                continue
            if info and now - info.get("ts", 0) < info.get("cooldown", 60):
                continue
            return key
        return None

    def mark_failed(self, key: str, *, permanent: bool = False, cooldown: int = 60):
        if not key:
            return
        self.failed[key] = {"ts": time.time(), "permanent": permanent, "cooldown": cooldown}
