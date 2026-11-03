# GPU-backed text OCR adapter (PaddleOCR on the NVIDIA GPU).
#
# This module is the ONLY place OCR runs. It refuses to start unless
# ParsethePDF.gpu_runtime.ensure_cuda() confirms real CUDA execution —
# a CPU paddle build or a hidden GPU aborts with GpuRuntimeError.
#
# The actual PaddleOCR parser object is injectable so unit tests never
# need a GPU, a model download, or the network.

import re
from difflib import SequenceMatcher
from pathlib import Path

from functions.QuesScrapper.ScrapethePDF import schemas
from functions.QuesScrapper.ScrapethePDF.gpu_runtime import ensure_cuda

_parser_cache = {}


def _expected_parser_result(prediction):
    """Extract PaddleOCR result fields from a prediction object.

    PaddleOCR and PPStructure prediction objects expose recognized text
    and polygon fields through their `.json` attribute. The nested
    overall_ocr_res shape remains accepted for older result forms.
    Everything is fall-back-tolerant so tests can pass simple mocks.
    """
    raw = getattr(prediction, "json", None) or getattr(prediction, "res", None) or {}
    if isinstance(raw, list) and raw:
        raw = raw[0]
    res = raw.get("res", raw) if isinstance(raw, dict) else {}
    return res


def _rec_values(res, key):
    overall = res.get("overall_ocr_res", {}) or {}
    values = overall.get(key) or res.get(key) or []
    return values


def _build_parser():
    """Create a text-only PaddleOCR parser pinned to the GPU.

    The GPU-only check runs FIRST — an unusable runtime raises before
    any model weight downloads. Options keep the CPU paths off and cap
    memory for 6 GB laptop cards.
    """
    facts = ensure_cuda()
    from paddleocr import PaddleOCR

    # The scraper saves figures independently; PPStructureV3's layout,
    # formula, and table models add several GB without helping text OCR.
    # Use the OCR pipeline with a bounded detector input size instead.
    parser = PaddleOCR(
        lang="en",
        device="gpu:0",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_limit_side_len=1600,
    )
    return parser, facts


def get_parser():
    """Return (parser, gpu_facts), building it once per process."""
    if "parser" not in _parser_cache:
        _parser_cache["parser"], _parser_cache["facts"] = _build_parser()
    return _parser_cache["parser"], _parser_cache["facts"]


def _bbox_pixels(poly_points, width, height):
    """Paddle polygon/box -> (x0, y0, x1, y1) pixel bbox, clipped."""
    xs = [point[0] for point in poly_points]
    ys = [point[1] for point in poly_points]
    x0 = max(0, int(min(xs)))
    y0 = max(0, int(min(ys)))
    x1 = int(max(xs) + 0.5)
    y1 = int(max(ys) + 0.5)
    if width:
        x1 = min(width, x1)
    if height:
        y1 = min(height, y1)
    if x1 < x0:
        x1 = x0
    if y1 < y0:
        y0 = y1
    return (x0, y0, x1, y1)


def _block_bbox(res, index, width, height):
    polys = (
        _rec_values(res, "rec_polys")
        or _rec_values(res, "dt_polys")
    )
    if index < len(polys):
        return _bbox_pixels(
            [tuple(point) for point in polys[index]], width, height
        )
    layout = res.get("layout_det_res", {}) or {}
    boxes = layout.get("boxes", [])
    if index < len(boxes):
        coordinate = boxes[index].get("coordinate")
        if coordinate is not None:
            return _bbox_pixels(
                [tuple(point) for point in coordinate], width, height
            )
    return (0, 0, 0, 0)


def _ocr_regions(image):
    """Split tall page scans into overlapping bands to preserve small text."""
    height, width = image.shape[:2]
    if height <= 1800:
        return [(image, 0)]

    split = height // 2
    overlap = max(128, height // 12)
    return [
        (image[:split + overlap], 0),
        (image[split - overlap:], split - overlap),
    ]


def _deduplicate_overlapping_blocks(blocks):
    """Remove identical line detections repeated in overlapping OCR bands."""
    unique = []
    for block in blocks:
        normalized = re.sub(r"\W+", "", block["text"].casefold())
        y0 = block["bbox"][1]
        mark = re.fullmatch(r"\(?\s*(\d+(?:\.\d+)?)\s*\)?", block["text"].strip())
        if mark and any(
            abs(y0 - other["bbox"][1]) <= 48
            and re.search(rf"\({re.escape(mark.group(1))}\)", other["text"])
            for other in unique
        ):
            continue
        if not mark:
            unique = [
                other for other in unique
                if not (
                    abs(y0 - other["bbox"][1]) <= 48
                    and (other_mark := re.fullmatch(
                        r"\(?\s*(\d+(?:\.\d+)?)\s*\)?",
                        other["text"].strip(),
                    ))
                    and re.search(
                        rf"\({re.escape(other_mark.group(1))}\)",
                        block["text"],
                    )
                )
            ]
        duplicate_index = next(
            (
                index for index, other in enumerate(unique)
                if normalized
                and abs(y0 - other["bbox"][1]) <= 48
                and SequenceMatcher(
                    None,
                    normalized,
                    re.sub(r"\W+", "", other["text"].casefold()),
                ).ratio() >= 0.9
            ),
            None,
        )
        if duplicate_index is None:
            unique.append(block)
        elif len(block["text"]) > len(unique[duplicate_index]["text"]):
            # Keep the fuller detection when one crop includes adjacent
            # marks or punctuation that the other crop clipped.
            unique[duplicate_index] = block
    return unique


def _repair_expanded_acronyms(text):
    """Restore a dropped final letter when the OCR also reads the expansion."""
    return re.sub(
        r"\bSST(?=\s*\(\s*Shortest\s+Seek\s+Time\s+First\b)",
        "SSTF",
        text,
        flags=re.IGNORECASE,
    )


def run_ocr(image_paths, parser=None, gpu_facts=None):
    """Run GPU document parsing over rendered page images.

    Args:
        image_paths: Iterable of rendered page PNG paths.
        parser: Optional pre-built PaddleOCR parser (tests inject a
            fake here; production uses get_parser()).
        gpu_facts: Optional pre-fetched ensure_cuda() report.

    Returns:
        List of ParsethePDF.schemas.PageOcr dicts, one per input image,
        with blocks carrying text + confidence + pixel bbox; blocks are
        sorted in reading order (top-to-bottom, left-to-right).

    Raises:
        GpuRuntimeError: when no functional NVIDIA CUDA runtime exists.
            Callers must abort — never fall back to CPU OCR.
    """
    if parser is None:
        parser, gpu_facts = get_parser()

    files = [str(Path(path)) for path in image_paths]

    page_ocrs = []
    for page_number, image_file in enumerate(files, start=1):
        import cv2

        image = cv2.imread(image_file)
        if image is None:
            raise ValueError(f"Could not read rendered page image: {image_file}")
        blocks = []
        for region, y_offset in _ocr_regions(image):
            for prediction in parser.predict(region):
                res = _expected_parser_result(prediction)
                texts = list(_rec_values(res, "rec_texts"))
                scores = list(_rec_values(res, "rec_scores"))

                for index, text in enumerate(texts):
                    confidence = float(scores[index]) if index < len(scores) else 0.0
                    x0, y0, x1, y1 = _block_bbox(
                        res, index, region.shape[1], region.shape[0]
                    )
                    blocks.append({
                        "page_number": page_number,
                        "text": str(text),
                        "confidence": round(confidence, 4),
                        "bbox": (x0, y0 + y_offset, x1, y1 + y_offset),
                        "kind": "text",
                    })
                break  # one prediction result per input region

        blocks = _deduplicate_overlapping_blocks(blocks)

        # Reading order: OCR yields text line by line; sort defensively.
        blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))

        text_scores = [block["confidence"] for block in blocks]
        mean = sum(text_scores) / len(text_scores) if text_scores else 0.0

        page_ocrs.append({
            "page_number": page_number,
            "image_path": image_file,
            "blocks": [
                {**block, "text": _repair_expanded_acronyms(block["text"])}
                for block in blocks
            ],
            "mean_confidence": round(mean, 6),
            "needs_review": mean < schemas.OCR_REVIEW_THRESHOLD
            and bool(blocks),
        })

    return [
        schemas.PageOcr.model_validate(page).model_dump()
        for page in page_ocrs
    ]
