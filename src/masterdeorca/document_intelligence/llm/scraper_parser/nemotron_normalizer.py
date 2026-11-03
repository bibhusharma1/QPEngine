from __future__ import annotations

from bisect import bisect_right
import re
from typing import Any


COORD_RE = re.compile(r"<x_([-\d\.]+)><y_([-\d\.]+)>")
CLASS_RE = re.compile(r"<class_([^>]+)>")
PAGE_RE = re.compile(r"<class_Page-header>\s*Page\s+(\d+)\s*</class_Page-header>", re.I)


class NemotronNormalizer:
    def normalize(self, content: str) -> dict[str, Any]:
        if not isinstance(content, str):
            content = str(content)
        coords = [(float(m.group(1)), float(m.group(2)), m.start()) for m in COORD_RE.finditer(content)]
        classes = [(m.group(1), m.start()) for m in CLASS_RE.finditer(content)]
        page_headers = [(m.start(), int(m.group(1))) for m in PAGE_RE.finditer(content)]
        header_positions = [position for position, _ in page_headers]
        header_pages = [number for _, number in page_headers]

        def page_at(position: int) -> int | None:
            index = bisect_right(header_positions, position) - 1
            return header_pages[index] if index >= 0 else None

        coordinate_details = [
            {
                "page": page_at(position),
                "x": x,
                "y": y,
            }
            for x, y, position in coords
        ]
        class_details = [
            {"page": page_at(position), "class": name}
            for name, position in classes
        ]
        tables = []
        for m in re.finditer(r"\\begin\{table\}.*?\\end\{table\}", content, re.S):
            tables.append({"type": "latex", "content": m.group(0)})
        for m in re.finditer(r"\\begin\{tabular\}.*?\\end\{tabular\}", content, re.S):
            tables.append({"type": "latex_tabular", "content": m.group(0)})
        math = re.findall(r"\$[^\$]+\$|\\\([^\)]+\\\)|\\\[[^\]]+\\\]", content)
        clean = COORD_RE.sub("", content)
        clean = CLASS_RE.sub("", clean)
        return {
            "raw_content": content,
            "text": clean,
            "coordinates": coords,
            "coordinate_details": coordinate_details,
            "classes": classes,
            "class_details": class_details,
            "tables": tables,
            "math": math,
        }
