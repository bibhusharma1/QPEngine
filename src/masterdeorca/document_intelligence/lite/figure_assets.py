from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

_COORDINATE = re.compile(r"<x_([-+\d.eE]+)><y_([-+\d.eE]+)>")
_CLASS = re.compile(r"<class_([^>]+)>", re.IGNORECASE)
_FIGURE_CLASSES = {
    "figure": "figure",
    "image": "figure",
    "picture": "figure",
    "chart": "graph",
    "graph": "graph",
    "plot": "graph",
    "diagram": "diagram",
}


def _figure_regions(content: str) -> list[tuple[str, tuple[float, float, float, float]]]:
    regions = []
    classes = list(_CLASS.finditer(content))
    coordinates = [
        (match.start(), float(match.group(1)), float(match.group(2)))
        for match in _COORDINATE.finditer(content)
    ]
    for index, class_match in enumerate(classes):
        classification = _FIGURE_CLASSES.get(class_match.group(1).strip().lower())
        if classification is None:
            continue
        closing = re.search(
            rf"</class_{re.escape(class_match.group(1))}\s*>",
            content[class_match.end():],
            re.IGNORECASE,
        )
        class_end = class_match.end() + closing.end() if closing else (
            classes[index + 1].start() if index + 1 < len(classes) else len(content)
        )
        lower = max(0, class_match.start() - 48)
        upper = min(len(content), class_end + 48)
        nearby = [
            (position, x, y)
            for position, x, y in coordinates
            if lower <= position <= upper
        ]
        if len(nearby) < 2:
            logger.warning(
                "LITE_FIGURE_BBOX_MISSING: class=%s",
                class_match.group(1),
            )
            continue
        x1, y1 = nearby[0][1:]
        x2, y2 = nearby[-1][1:]
        bbox = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            logger.warning("LITE_FIGURE_BBOX_INVALID: class=%s bbox=%s", classification, bbox)
            continue
        regions.append((classification, bbox))
    return regions


def _write_asset(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temp:
            temp_name = temp.name
            temp.write(buffer.getvalue())
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()


def crop_figure_assets(
    pages: dict[int, bytes],
    page_results: list[tuple[int, dict[str, Any]]],
    asset_root: Path,
    source_digest: str,
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    counters: dict[tuple[int, str], int] = {}
    for page_number, response in page_results:
        content = response.get("content") or response.get("text") or ""
        if not isinstance(content, str) or page_number not in pages:
            continue
        try:
            page_image = Image.open(io.BytesIO(pages[page_number])).convert("RGB")
        except (OSError, ValueError) as exc:
            logger.warning("LITE_FIGURE_PAGE_IMAGE_INVALID: page=%d error=%s", page_number, exc)
            continue
        for classification, bbox in _figure_regions(content):
            left = max(0, min(page_image.width, round(bbox[0] * page_image.width)))
            top = max(0, min(page_image.height, round(bbox[1] * page_image.height)))
            right = max(0, min(page_image.width, round(bbox[2] * page_image.width)))
            bottom = max(0, min(page_image.height, round(bbox[3] * page_image.height)))
            if right - left < 2 or bottom - top < 2:
                logger.warning(
                    "LITE_FIGURE_CROP_EMPTY: page=%d class=%s bbox=%s",
                    page_number,
                    classification,
                    bbox,
                )
                continue
            key = (page_number, classification)
            counters[key] = counters.get(key, 0) + 1
            asset_id = hashlib.sha256(
                f"{source_digest}:{page_number}:{classification}:{bbox}".encode("utf-8")
            ).hexdigest()[:16]
            relative_path = Path("assets") / "figures" / (
                f"page-{page_number:05d}-{classification}-{counters[key]:03d}.png"
            )
            _write_asset(asset_root / relative_path, page_image.crop((left, top, right, bottom)))
            assets.append({
                "asset_id": asset_id,
                "asset_type": classification,
                "classification": classification,
                "path": relative_path.as_posix(),
                "source_page": page_number,
                "bbox": [round(value, 6) for value in bbox],
                "description": f"{classification} cropped from page {page_number}",
                "verified": True,
            })
            logger.info(
                "LITE_FIGURE_ASSET_SAVED: asset_id=%s page=%d class=%s path=%s",
                asset_id,
                page_number,
                classification,
                relative_path.as_posix(),
            )
    return assets
