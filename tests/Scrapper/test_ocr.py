import numpy as np

from functions.QuesScrapper.ScrapethePDF.ocr import (
    _deduplicate_overlapping_blocks,
    _ocr_regions,
    _repair_expanded_acronyms,
)


def test_tall_scans_use_overlapping_bands():
    image = np.zeros((2924, 2067, 3), dtype=np.uint8)

    regions = _ocr_regions(image)

    assert len(regions) == 2
    (top, top_offset), (bottom, bottom_offset) = regions
    assert top_offset == 0
    assert bottom_offset < top.shape[0]
    assert bottom_offset + bottom.shape[0] == image.shape[0]


def test_deduplication_removes_overlap_copy_but_keeps_repeated_lines():
    def block(text, y):
        return {"text": text, "bbox": (10, y, 200, y + 20)}

    result = _deduplicate_overlapping_blocks([
        block("SSTF scheduling algorithm", 100),
        block("SSTF scheduling algorithm (16)", 112),
        block("SSTF scheduling algorithm", 300),
    ])

    assert [item["bbox"][1] for item in result] == [112, 300]


def test_deduplication_keeps_a_margin_mark_only_once():
    result = _deduplicate_overlapping_blocks([
        {"text": "(16)", "bbox": (1800, 100, 1840, 130)},
        {"text": "Describe the critical section problem. (16)",
         "bbox": (100, 108, 1800, 138)},
    ])

    assert [item["text"] for item in result] == [
        "Describe the critical section problem. (16)"
    ]


def test_expansion_repairs_dropped_final_acronym_letter():
    assert _repair_expanded_acronyms(
        "using the SST (Shortest Seek Time First) algorithm"
    ) == "using the SSTF (Shortest Seek Time First) algorithm"
