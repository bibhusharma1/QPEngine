from __future__ import annotations

import re
from typing import Any


COORD_RE = re.compile(r"<x_([-\d\.]+)><y_([-\d\.]+)>")
CLASS_RE = re.compile(r"<class_([^>]+)>")


def normalize_nemotron_content(content: str) -> dict[str, Any]:
    if not isinstance(content, str):
        content = str(content)
    elements = []
    # Split by class markers roughly
    # Track coordinates
    coords = []
    for m in COORD_RE.finditer(content):
        coords.append((float(m.group(1)), float(m.group(2)), m.start()))
    classes = []
    for m in CLASS_RE.finditer(content):
        classes.append((m.group(1), m.start()))
    # Extract tables (LaTeX)
    tables = []
    for m in re.finditer(r"\\begin\{table\}.*?\\end\{table\}", content, re.S):
        tables.append({"type": "latex", "content": m.group(0)})
    for m in re.finditer(r"\\begin\{tabular\}.*?\\end\{tabular\}", content, re.S):
        tables.append({"type": "latex_tabular", "content": m.group(0)})
    # Math expressions
    math = re.findall(r"\$[^\$]+\$|\\\([^\)]+\\\)|\\\[[^\]]+\\\]", content)
    # Strip markers for clean text while preserving structure
    clean = COORD_RE.sub("", content)
    clean = CLASS_RE.sub("", clean)
    return {
        "raw_content": content,
        "text": clean,
        "coordinates": coords,
        "classes": classes,
        "tables": tables,
        "math": math,
    }
