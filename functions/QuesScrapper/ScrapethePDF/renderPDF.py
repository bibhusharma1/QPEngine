# Page rendering layer: paints each PDF page to a high-resolution PNG.
# These rasters are the later OCR fallback (ParsethePDF/ocr.py) and a
# visual reference for pages with sparse native text.

from pathlib import Path

import pymupdf


def render_pdf(pdf_path, output_dir, dpi=250):
    """Render every page of a PDF to a PNG image.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Folder in which the page PNGs are written.
        dpi: Rendering resolution. 250 DPI keeps text OCR-legible while
            keeping file sizes reasonable.

    Returns:
        List of dicts, one per page (1-indexed):
            {
                "page_number": 1-based page index,
                "image_path": str path of the rendered page PNG,
                "width_px": rendered image width in pixels,
                "height_px": rendered image height in pixels,
            }
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rendered_pages = []

    with pymupdf.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf, start=1):
            image_path = output_dir / f"page_{page_number:04d}.png"

            # alpha=False drops transparency for plain RGB PNGs,
            # annots=True includes form fields/stamp annotations in the render.
            pixmap = page.get_pixmap(
                dpi=dpi,
                colorspace=pymupdf.csRGB,
                alpha=False,
                annots=True,
            )
            pixmap.save(image_path)

            rendered_pages.append({
                "page_number": page_number,
                "image_path": str(image_path),
                "width_px": pixmap.width,
                "height_px": pixmap.height,
            })

    return rendered_pages
