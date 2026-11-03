# Embedded raster image (figure/diagram) extraction layer.
# Only images physically embedded in the PDF are collected here;
# vector-drawn diagrams are handled by ParsethePDF/vector_figures.py.

from pathlib import Path

import pymupdf

# Formats extractable as-is; anything else (jp2, jbig2, cmyk/special
# colorspaces, corrupt streams) is re-generated PNG via pixmap rendering.
DIRECT_EXTENSIONS = {"png", "jpeg", "jpg"}


def _save_image(pdf, xref, image, image_path):
    """Save an extracted image, normalizing odd encodings to PNG."""
    if image["ext"] in DIRECT_EXTENSIONS:
        image_path.write_bytes(image["image"])
        return image["width"], image["height"]

    # Unusual format (JPX/JBIG2, CMYK...): decode to a normal PNG so the
    # figure stays viewable even though the raw stream is not.
    pixmap = pymupdf.Pixmap(pdf, xref)
    if pixmap.colorspace is None or pixmap.colorspace.name != pymupdf.csRGB.name:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
    png_path = image_path.with_suffix(".png")
    pixmap.save(png_path)
    return pixmap.width, pixmap.height


def extract_embedded_images(pdf_path, output_dir):
    """Extract all embedded raster images from a PDF, per page.

    Two passes per page:
      1. direct extraction from the image streams (original resolution);
      2. any image placed on the page but not extracted in pass 1 is
         re-rendered from its on-page rectangle (clipped page photo) —
         this is the fallback that catches corrupt streams and any
         image placement the stream extraction misses, so clear images
         are never silently dropped.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Folder in which extracted images are written.

    Returns:
        List of records for every successfully extracted image:
            {
                "page_number": 1-based page the image appears on,
                "image_path": str path of the saved image file,
                "width": image width in pixels,
                "height": image height in pixels,
            }
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    extracted_images = []

    with pymupdf.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf, start=1):
            image_number = 0

            # Pass 1: direct stream extraction (full=True also descends
            # into Form XObjects, i.e. images nested inside groups).
            handled_xrefs = set()
            for info in page.get_images(full=True):
                xref = info[0]
                image_number += 1
                try:
                    image = pdf.extract_image(xref)
                    if not image:
                        raise ValueError("empty image stream")
                    image_path = output_dir / (
                        f"page_{page_number:04d}_"
                        f"image_{image_number}.{image['ext']}"
                    )
                    width, height = _save_image(pdf, xref, image, image_path)
                    handled_xrefs.add(xref)
                    extracted_images.append({
                        "page_number": page_number,
                        "image_path": str(image_path),
                        "width": width,
                        "height": height,
                    })
                except Exception as exc:
                    # One broken image should not abort the whole PDF run.
                    print(f"Could not extract image {xref}: {exc}")

            # Pass 2 (fallback): render placements direct extraction did
            # not manage — image is guaranteed to be captured as it
            # appears on the page.
            for image_number, placement in enumerate(
                page.get_image_info(xrefs=True), start=image_number + 1
            ):
                xref = placement.get("xref")
                if not xref or xref in handled_xrefs or xref <= 0:
                    continue

                try:
                    clip = pymupdf.Rect(placement["bbox"])
                    pixmap = page.get_pixmap(
                        dpi=250,
                        clip=clip,
                        colorspace=pymupdf.csRGB,
                        alpha=False,
                    )
                    image_path = output_dir / (
                        f"page_{page_number:04d}_image_{image_number}.png"
                    )
                    pixmap.save(image_path)
                    extracted_images.append({
                        "page_number": page_number,
                        "image_path": str(image_path),
                        "width": pixmap.width,
                        "height": pixmap.height,
                    })
                except Exception as exc:
                    print(f"Could not render image {xref}: {exc}")

    return extracted_images
