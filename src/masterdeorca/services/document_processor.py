"""Multi-format document processing adapter for the Scrapper pipeline.

Extends PDF processing to support DOCX, PNG, and JPG/JPEG while reusing the
existing scrapper record/manifest schema so downstream Parser, validation,
deduplication, and Question Bank synchronization remain unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from PIL import Image

from config.outputs import pdf_stem
from functions.QuesScrapper.ScrapethePDF.gpu_runtime import GpuRuntimeError
from functions.QuesScrapper.ScrapethePDF.manifest import save_manifest
from functions.QuesScrapper.ScrapethePDF.ocr import run_ocr
from functions.QuesScrapper.ScrapePipeLIne.quesandFig import build_page_records, append_records

logger = logging.getLogger(__name__)


def _docx_to_pages(docx_path: Path, output_dir: Path) -> tuple[list[dict], list[dict]]:
    """Extract text, tables, and images from DOCX into page-like structures."""
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(docx_path)
    output_dir = Path(output_dir)
    pages_dir = output_dir / "pages"
    images_dir = output_dir / "images"
    pages_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    pages = []
    rendered_pages = []
    image_counter = 0

    # Render DOCX pages conceptually - create text-based representations
    # For each paragraph and table, build page content
    page_text_parts = []
    layout_blocks = []
    tables = []
    
    block_id = 0
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        block_id += 1
        layout_blocks.append({
            "block_id": block_id,
            "bbox": [0, block_id * 50, 612, (block_id + 1) * 50],
            "coordinate_system": "page_points",
            "text": text,
            "source": "native",
        })
        page_text_parts.append(text)

    # Extract tables
    for table_idx, table in enumerate(doc.tables):
        table_data = []
        for row in table.rows:
            row_data = [cell.text.strip() for cell in row.cells]
            table_data.append(row_data)
        tables.append({
            "table_id": table_idx + 1,
            "page_number": 1,
            "bbox": [50, 400, 562, 600],
            "rows": table_data,
            "header": table_data[0] if table_data else [],
            "source": "native",
        })
        if table_data:
            page_text_parts.append("\n" + "\n".join([" | ".join(row) for row in table_data]))

    # Extract embedded images
    embedded_images = []
    for rel in doc.part.rels.values():
        if "image" in rel.reltype:
            try:
                image_data = rel.target_part.blob
                ext = os.path.splitext(rel.target_ref)[1].lower() or ".png"
                image_counter += 1
                img_path = images_dir / f"image_{image_counter}{ext}"
                img_path.write_bytes(image_data)
                embedded_images.append({
                    "image_path": str(img_path),
                    "page_number": 1,
                    "bbox": [50, 100, 200, 200],
                    "source": "embedded",
                })
            except Exception as e:
                logger.warning(f"Failed to extract DOCX image: {e}")

    full_text = "\n\n".join(page_text_parts)
    pages.append({
        "page_number": 1,
        "text": full_text,
        "layout_blocks": layout_blocks,
        "tables": tables,
        "rejected_table_candidates": [],
    })

    # Create a rendered page image (convert first page conceptually)
    # For DOCX, create a simple rendered representation
    rendered_path = pages_dir / "page_0001.png"
    try:
        # Create a blank image as placeholder; OCR can process if needed
        img = Image.new('RGB', (612, 792), color='white')
        img.save(rendered_path)
    except Exception:
        rendered_path.touch()

    rendered_pages.append({
        "page_number": 1,
        "image_path": str(rendered_path),
        "width_px": 612,
        "height_px": 792,
    })

    return pages, rendered_pages, embedded_images


def _image_to_pages(image_path: Path, output_dir: Path) -> tuple[list[dict], list[dict]]:
    """Convert single image (PNG/JPG/JPEG) to page structures with OCR."""
    output_dir = Path(output_dir)
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Copy/render the image as page
    rendered_path = pages_dir / "page_0001.png"
    try:
        img = Image.open(image_path)
        img = img.convert('RGB')
        img.save(rendered_path)
    except Exception as e:
        raise RuntimeError(f"Failed to process image: {e}")

    rendered_pages = [{
        "page_number": 1,
        "image_path": str(rendered_path),
        "width_px": img.width if 'img' in locals() else 612,
        "height_px": img.height if 'img' in locals() else 792,
    }]

    # OCR the image (images require OCR)
    try:
        ocr_pages = run_ocr([str(rendered_path)])
        if ocr_pages:
            ocr_page = ocr_pages[0]
            text = "\n".join(block["text"] for block in ocr_page["blocks"])
            layout_blocks = [
                {
                    "bbox": list(block.get("bbox", ())),
                    "coordinate_system": "rendered_pixels",
                    "text": block.get("text", ""),
                    "confidence": block.get("confidence"),
                    "source": "ocr",
                }
                for block in ocr_page["blocks"]
            ]
            pages = [{
                "page_number": 1,
                "text": text,
                "layout_blocks": layout_blocks,
                "tables": [],
                "rejected_table_candidates": [],
                "ocr_confidence": ocr_page.get("mean_confidence"),
                "needs_ocr_review": True,
            }]
        else:
            pages = [{
                "page_number": 1,
                "text": "",
                "layout_blocks": [],
                "tables": [],
                "rejected_table_candidates": [],
                "needs_ocr_review": True,
            }]
    except GpuRuntimeError:
        raise
    except Exception as e:
        logger.warning(f"OCR failed for image: {e}")
        pages = [{
            "page_number": 1,
            "text": "",
            "layout_blocks": [],
            "tables": [],
            "rejected_table_candidates": [],
            "needs_ocr_review": True,
        }]

    return pages, rendered_pages, []


def process_document(doc_path, output_dir, dpi=250):
    """Process PDF, DOCX, PNG, JPG/JPEG into scrapper records+manifest."""
    from config import settings as _s
    if getattr(_s, "PARSER_MODE", "local").lower() == "llm":
        from src.masterdeorca.document_intelligence.lite.processor import LiteProcessor
        proc = LiteProcessor()
        extracted = proc.process(Path(doc_path))
        records = []
        if extracted.pages:
            for i, p in enumerate(extracted.pages):
                records.append({"page": i + 1, "text": extracted.text, "figures": [f.__dict__ for f in extracted.figures]})
        if not records and extracted.text:
            records = [{"page": 1, "text": extracted.text, "figures": [f.__dict__ for f in extracted.figures]}]
        output_dir_p = Path(output_dir)
        output_dir_p.mkdir(parents=True, exist_ok=True)
        manifest = save_manifest(output_dir_p, [Path(doc_path)], metadata={"lite_mode": True, "nemotron": True})
        append_records(records, output_dir_p / "questions_scrapper.json")
        return records, manifest
    doc_path = Path(doc_path)
    output_dir = Path(output_dir)
    suffix = doc_path.suffix.lower()

    if not doc_path.is_file():
        raise FileNotFoundError(doc_path)

    if suffix == ".pdf":
        from functions.QuesScrapper.ScrapePipeLIne.processPDF import process_pdf
        return process_pdf(doc_path, output_dir, dpi=dpi)
    elif suffix == ".docx":
        pages, rendered_pages, embedded_images = _docx_to_pages(doc_path, output_dir)
        vector_figures = []
        records, manifest = build_page_records(doc_path, pages, rendered_pages, embedded_images, vector_figures, output_dir, dpi=dpi)
        save_manifest(manifest, output_dir / "manifest.json")
        return records, manifest
    elif suffix in {".png", ".jpg", ".jpeg"}:
        pages, rendered_pages, embedded_images = _image_to_pages(doc_path, output_dir)
        vector_figures = []
        records, manifest = build_page_records(doc_path, pages, rendered_pages, embedded_images, vector_figures, output_dir, dpi=dpi)
        save_manifest(manifest, output_dir / "manifest.json")
        return records, manifest
    else:
        raise ValueError(f"Unsupported document format: {suffix}")


# Lite mode guard
try:
    from config import settings as _settings
    if getattr(_settings, "PARSER_MODE", "local").lower() == "llm":
        import logging as _logging
        _logging.getLogger(__name__).warning("LITE_MODE_ACTIVE: using Nemotron-only processing")
except Exception:
    pass
