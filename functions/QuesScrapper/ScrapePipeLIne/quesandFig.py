# Records + dataset layer: merges the per-PDF extraction outputs into
# flat JSON records and appends them to the consolidated dataset file.
# This replaces the earlier "arrange into json or csv" TODO — JSON was
# chosen because figure paths and page metadata nest naturally per record.

import json
from pathlib import Path


def build_page_records(
    pdf_path,
    pages,
    rendered_pages,
    embedded_images,
    vector_figures,
    output_dir,
    dpi=250,
):
    """Write per-page text files and build flat records + manifest for one PDF.

    Args:
        pdf_path: Path to the PDF being processed.
        pages: From ParsethePDF.pdf_read.extract_pdf_text.
        rendered_pages: From ParsethePDF.renderPDF.render_pdf.
        embedded_images: From ParsethePDF.image_extract.extract_embedded_images.
        vector_figures: From ParsethePDF.vector_figures.extract_vector_figures.
        output_dir: Per-PDF output folder (pages/ text/ images/ images_vector).
        dpi: Render DPI, recorded in the manifest.

    Returns:
        Tuple (records, manifest):
        - records: one flat dict per page:
            {
                "pdf_name": source PDF file name,
                "page_number": 1-based page index,
                "raw_text": full extracted page text (plain text),
                "text_character_count": len(raw_text),
                "figure_paths": figure image paths found on this page
                    (raster + vector, in that order),
                "page_image": rendered full-page PNG path,
                "needs_ocr_review": True when text is < 20 characters —
                    heuristic flag that the page is likely scanned and
                    should be OCR'd in a later phase,
            }
        - manifest: same shape as the earlier process_pdf manifest, plus a
            "vector_figures" section, used by ParsethePDF.manifest.save_manifest.
    """
    output_dir = Path(output_dir)
    pdf_path = Path(pdf_path)

    manifest = {
        "source_pdf": pdf_path.name,
        "page_count": len(pages),
        "render_dpi": dpi,
        "pages": [],
        "embedded_images": embedded_images,
        "vector_figures": vector_figures,
    }

    # Group figure paths by page so records can reference their page's figures.
    raster_by_page = {}
    for image in embedded_images:
        raster_by_page.setdefault(image["page_number"], []).append(image["image_path"])

    vector_by_page = {}
    for figure in vector_figures:
        vector_by_page.setdefault(figure["page_number"], []).append(figure["image_path"])

    records = []

    for page, rendered in zip(pages, rendered_pages):
        page_number = page["page_number"]

        # Keep the per-page raw text file alongside the JSON record.
        text_path = output_dir / "text" / (f"page_{page_number:04d}.txt")
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(page["text"], encoding="utf-8")

        manifest["pages"].append({
            **{
                key: value
                for key, value in page.items()
                if key != "text"
            },
            "text_path": str(text_path),
            "native_text_characters": page.get(
                "native_text_characters", len(page["text"])
            ),
            "needs_ocr_review": page.get(
                "needs_ocr_review", len(page["text"]) < 20
            ),
            "page_image": rendered["image_path"],
            "render_width_px": rendered["width_px"],
            "render_height_px": rendered["height_px"],
            "layout_blocks": page.get("layout_blocks", []),
            "tables": page.get("tables", []),
            "rejected_table_candidates": page.get("rejected_table_candidates", []),
        })

        figure_paths = raster_by_page.get(page_number, []) + vector_by_page.get(page_number, [])

        records.append({
            "pdf_name": pdf_path.name,
            "page_number": page_number,
            "raw_text": page["text"],
            "layout_blocks": page.get("layout_blocks", []),
            "tables": page.get("tables", []),
            "rejected_table_candidates": page.get("rejected_table_candidates", []),
            "text_character_count": len(page["text"]),
            "figure_paths": figure_paths,
            "page_image": rendered["image_path"],
            "needs_ocr_review": page.get(
                "needs_ocr_review", len(page["text"]) < 20
            ),
            **(
                {"ocr_confidence": page["ocr_confidence"]}
                if "ocr_confidence" in page else {}
            ),
        })

    return records, manifest


def append_records(records, json_path):
    """Append (or upsert) page records into the consolidated JSON dataset.

    Semantics:
        - The dataset file is a JSON array; it is created on first append.
        - Records are keyed by (pdf_name, page_number), so re-running the
          same PDF REPLACES its pages instead of duplicating them, while
          new PDFs simply extend the array.
        - Final array is sorted by (pdf_name, page_number) for stable diffs.
        - Write is atomic (temp file + rename) so a crash mid-write cannot
          leave a half-corrupted dataset.

    Args:
        records: Record list as produced by build_page_records.
        json_path: Path of the per-PDF questions_scrapper.json file.

    Returns:
        Total number of records now stored in the dataset file.
    """
    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
            else:
                print(f"Warning: {json_path} is not a JSON list, starting fresh.")
        except json.JSONDecodeError as exc:
            # Keep the unreadable file around for inspection instead of
            # silently destroying it.
            backup = json_path.with_suffix(".json.bak")
            json_path.rename(backup)
            print(f"Warning: could not parse {json_path} ({exc}); backed up to {backup}.")

    # Index existing records by (pdf, page); writes below upsert into it.
    merged = {
        (record["pdf_name"], record["page_number"]): record
        for record in existing
    }
    for record in records:
        merged[(record["pdf_name"], record["page_number"])] = record

    records_out = sorted(
        merged.values(),
        key=lambda record: (record["pdf_name"], record["page_number"]),
    )

    temp_path = json_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(records_out, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_path.replace(json_path)

    return len(records_out)
