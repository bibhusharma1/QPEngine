# Per-PDF processing orchestrator.
# One call = full extraction of a single PDF:
#   native text -> rendered pages -> embedded images -> vector figures
# and returns flat page records + a manifest.

from pathlib import Path
import logging

from functions.QuesScrapper.ScrapethePDF.pdf_read import extract_pdf_text
from functions.QuesScrapper.ScrapethePDF.renderPDF import render_pdf
from functions.QuesScrapper.ScrapethePDF.image_extract import extract_embedded_images
from functions.QuesScrapper.ScrapethePDF.vector_figures import extract_vector_figures
from functions.QuesScrapper.ScrapethePDF.manifest import save_manifest
from functions.QuesScrapper.ScrapethePDF.ocr import run_ocr
from functions.QuesScrapper.ScrapethePDF.gpu_runtime import GpuRuntimeError

from functions.QuesScrapper.ScrapePipeLIne.quesandFig import build_page_records

logger = logging.getLogger(__name__)


def process_pdf(pdf_path, output_dir, dpi=250):
    """Extract everything from one PDF into its own output folder.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Folder for this PDF's pages/, images/, images_vector/,
            text/ and manifest.json. Use a distinct folder per PDF to
            avoid filename collisions (pipeline.py passes <stem>/).
        dpi: Rendering resolution for page images and figure clips.

    Returns:
        Tuple (records, manifest) — see ParsePipeLIne.quesandFig
        .build_page_records for the exact shapes.

    Raises:
        FileNotFoundError: If the PDF does not exist.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    # 1. Native text layer.
    pages = extract_pdf_text(pdf_path)

    # 2. Full-page rasters (later OCR fallback + visual reference).
    rendered_pages = render_pdf(
        pdf_path,
        output_dir / "pages",
        dpi=dpi,
    )

    # Scanned PDFs have no usable text layer. OCR those pages on the
    # configured NVIDIA GPU; run_ocr fails closed if CUDA is unavailable.
    pages_needing_ocr = [
        index for index, page in enumerate(pages)
        if len(page["text"].strip()) < 20
    ]
    ocr_pages = []
    if pages_needing_ocr:
        try:
            ocr_pages = run_ocr([
                rendered_pages[index]["image_path"] for index in pages_needing_ocr
            ])
        except GpuRuntimeError:
            # Short native text remains usable without GPU OCR. Empty pages
            # are scanned/unreadable, so never report those as processed.
            if any(not pages[index]["text"].strip() for index in pages_needing_ocr):
                raise
            logger.warning("GPU OCR unavailable; retaining short native PDF text for review.")
            ocr_pages = []
        if not ocr_pages:
            pages_needing_ocr = []
    for index, ocr_page in zip(pages_needing_ocr, ocr_pages):
        pages[index]["native_text_characters"] = len(pages[index]["text"])
        pages[index]["text"] = "\n".join(
            block["text"] for block in ocr_page["blocks"]
        )
        pages[index]["layout_blocks"] = [
            {
                "bbox": list(block.get("bbox", ())),
                "coordinate_system": "rendered_pixels",
                "text": block.get("text", ""),
                "confidence": block.get("confidence"),
                "source": "ocr",
            }
            for block in ocr_page["blocks"]
        ]
        pages[index]["ocr_confidence"] = ocr_page["mean_confidence"]
        # Scanned-page OCR is persisted for use, but stays reviewable
        # because recognition may miss small labels or marks.
        pages[index]["needs_ocr_review"] = True

    # 3. Embedded raster figures (diagrams stored as images).
    images = extract_embedded_images(
        pdf_path,
        output_dir / "images",
    )

    # 4. Vector-drawn figures (diagrams stored as PDF drawings).
    vector_figures = extract_vector_figures(
        pdf_path,
        output_dir / "images_vector",
    )

    # 5. Merge everything into flat records + manifest; save the manifest.
    records, manifest = build_page_records(
        pdf_path,
        pages,
        rendered_pages,
        images,
        vector_figures,
        output_dir,
        dpi=dpi,
    )

    save_manifest(manifest, output_dir / "manifest.json")

    return records, manifest
