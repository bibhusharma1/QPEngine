from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Page:
    number: int
    text: str = ""


@dataclass
class Figure:
    id: str
    page: int = 1
    bbox: list[float] = field(default_factory=list)
    image_path: str | None = None


@dataclass
class ExtractedDocument:
    pages: list[Page] = field(default_factory=list)
    text: str = ""
    tables: list[dict[str, Any]] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
