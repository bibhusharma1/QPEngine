# Vector-drawn figure extraction layer.
# Exam papers often draw diagrams (lines, boxes, curves) as PDF vector
# drawings instead of embedding raster images — those would be missed by
# ParsethePDF/image_extract.py. This module renders the page regions
# covered by such drawings into PNGs so figures are not lost.

from pathlib import Path

import pymupdf

# If a drawing cluster covers more than this fraction of the page area,
# it is treated as a decorative full-page border/frame rather than a
# meaningful figure, and it is skipped.
MAX_PAGE_COVERAGE_RATIO = 0.9

# Drawings closer than this (pt) join the same figure cluster.
CLUSTER_MARGIN = 10.0


def _cluster_rects(rects, margin):
    """Group overlapping/nearby rects into figure clusters (union-find)."""
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    expanded = [rect + (-margin, -margin, margin, margin) for rect in rects]
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if expanded[i].intersects(expanded[j]):
                root_i, root_j = find(i), find(j)
                if root_i != root_j:
                    parent[root_i] = root_j

    clusters = {}
    for i, rect in enumerate(rects):
        clusters.setdefault(find(i), []).append(rect)

    return [rects_list for rects_list in clusters.values()]


def extract_vector_figures(pdf_path, output_dir):
    """Render vector-drawn diagram regions to PNG images.

    Drawings on a page are grouped into separate figure clusters (a page
    with two diagrams yields two PNGs); decorative page borders are
    dropped and do not swallow nearby figures.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Folder in which the figure PNGs are written.

    Returns:
        List of records for every rendered figure:
            {
                "page_number": 1-based page the drawing appears on,
                "image_path": str path of the saved PNG,
                "width": rendered figure width in pixels,
                "height": rendered figure height in pixels,
            }
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    vector_figures = []

    with pymupdf.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf, start=1):
            drawings = page.get_drawings()
            if not drawings:
                continue  # no vector content on this page

            # Clip to the printable page (2pt margin keeps rounding
            # artifacts of the union math from dropping the figure).
            page_rect = page.rect + (-2, -2, 2, 2)

            # Drop decorative full-page frames BEFORE clustering: their
            # huge rect would otherwise join every figure cluster.
            content_rects = [
                drawing["rect"]
                for drawing in drawings
                if drawing["rect"].get_area()
                < MAX_PAGE_COVERAGE_RATIO * page.rect.get_area()
            ]
            if not content_rects:
                continue

            clusters = _cluster_rects(content_rects, CLUSTER_MARGIN)

            figure_number = 0
            for cluster_rects in clusters:
                bounding_rect = pymupdf.Rect()
                for rect in cluster_rects:
                    bounding_rect |= rect

                if bounding_rect.is_empty:
                    continue

                union = bounding_rect & page_rect
                if union.is_empty:
                    continue

                # Full-page rectangles are borders/frames, not figures.
                if union.get_area() >= MAX_PAGE_COVERAGE_RATIO * page.rect.get_area():
                    continue

                figure_number += 1
                pixmap = page.get_pixmap(
                    dpi=250,
                    clip=bounding_rect,
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                )

                image_path = output_dir / (
                    f"page_{page_number:04d}_drawing_{figure_number}.png"
                )
                pixmap.save(image_path)

                vector_figures.append({
                    "page_number": page_number,
                    "image_path": str(image_path),
                    "width": pixmap.width,
                    "height": pixmap.height,
                })

    return vector_figures
