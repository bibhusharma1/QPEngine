from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DocumentResource:
    path: Path | str
    kind: str  # image/figure/table
    page: int | None = None
    bbox: dict[str, Any] | None = None
    ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedDocument:
    text: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    figures: list[DocumentResource] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: Path | str | None = None
