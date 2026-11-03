# Test fixture factories: build tiny PDFs with pymupdf itself so the
# test suite never depends on the binary files in sampleData/.

from pathlib import Path

import pymupdf


def make_pdf(
    path,
    page_texts,
    with_image_on_page=None,
    with_drawing_on_page=None,
):
    """Create a small PDF on disk for testing.

    Args:
        path: File path to write the PDF to (parent dirs created).
        page_texts: List of text strings — one per resulting page.
            Empty strings create textless pages.
        with_image_on_page: 0-based page index to stamp an embedded
            raster image onto (None to skip).
        with_drawing_on_page: 0-based page index to add vector drawings
            (a rectangle + line) onto (None to skip).

    Returns:
        The written path as a Path.
    """
    path = Path(path)
    doc = pymupdf.open()
    pdf_page_numbers = []

    for index, text in enumerate(page_texts):
        page = doc.new_page()

        if text:
            page.insert_text((72, 72), text)

        if with_image_on_page is not None and index == with_image_on_page:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8))
            pixmap.set_rect(pixmap.irect, (255, 0, 0))
            page.insert_image(pymupdf.Rect(300, 300, 340, 340), pixmap=pixmap)

        if with_drawing_on_page is not None and index == with_drawing_on_page:
            page.draw_rect(pymupdf.Rect(100, 400, 200, 450), color=(0, 0, 1))
            page.draw_line(pymupdf.Point(100, 400), pymupdf.Point(200, 450))

        pdf_page_numbers.append(index)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()

    return path
