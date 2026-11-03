
import json
from pathlib import Path

import pymupdf

INPUT_PDF = Path("sampleData/ques_bank/AI24201 MATHEMATICS FOR AIML_MID.pdf")
OUTPUT_DIR = Path("sampleData/ques_bank/output")
PAGES_DIR = OUTPUT_DIR / "pages"
TEXT_DIR = OUTPUT_DIR / "text"

RENDER_DPI = 250



def process_pdf(pdf_path: Path) -> None:
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    page_records = []

    with pymupdf.open(pdf_path) as pdf:
        if pdf.is_encrypted:
            raise ValueError(
                "PDF is encrypted; provide an accessible PDF."
            )

        page_count = len(pdf)
        print(f"File: {pdf_path.name}")
        print(f"Pages: {page_count}")

        for index, page in enumerate(pdf, start=1):
            page_name = f"page_{index:04d}"

            # 1. Extract native PDF text.
            text = page.get_text("text", sort=True).strip()

            text_path = TEXT_DIR / f"{page_name}.txt"
            text_path.write_text(text, encoding="utf-8")

            # 2. Render the page to a high-resolution image.
            pixmap = page.get_pixmap(
                dpi=RENDER_DPI,
                colorspace=pymupdf.csRGB,
                alpha=False,
                annots=True,
            )

            image_path = PAGES_DIR / f"{page_name}.png"
            pixmap.save(image_path)

            # 3. Inspect embedded images and vector drawings.
            embedded_images = page.get_images(full=True)
            vector_drawings = page.get_drawings()

            text_characters = len(text)

            # Heuristic only: sparse text may indicate a scanned page.
            needs_ocr = text_characters < 20

            # Image and vector presence indicates possible visual content.
            has_visual_content = (
                bool(embedded_images) or bool(vector_drawings)
            )

            # Flag visual pages for later inspection.
            needs_visual_review = has_visual_content

            # 4. Record metadata for downstream processing.
            record = {
                "page_number": index,
                "width_pt": round(page.rect.width, 2),
                "height_pt": round(page.rect.height, 2),
                "render_width_px": pixmap.width,
                "render_height_px": pixmap.height,
                "image_path": str(image_path),
                "text_path": str(text_path),
                "native_text_characters": text_characters,
                "needs_ocr": needs_ocr,
                "embedded_image_count": len(embedded_images),
                "vector_drawing_count": len(vector_drawings),
                "has_visual_content": has_visual_content,
                "needs_visual_review": needs_visual_review,
            }

            page_records.append(record)

            # 5. Print a useful processing summary.
            status = []

            status.append(
                "OCR needed" if needs_ocr else "Native text found"
            )

            if has_visual_content:
                status.append("Visual content detected")

            print(f"Page {index}: {' | '.join(status)}")

        # 6. Save the manifest after processing all pages.
        manifest = {
            "source_pdf": str(pdf_path),
            "page_count": page_count,
            "render_dpi": RENDER_DPI,
            "pages": page_records,
        }

    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\nPDF processing complete.")
    print(f"Rendered pages: {PAGES_DIR}")
    print(f"Extracted text: {TEXT_DIR}")
    print(f"Manifest: {manifest_path}")



if __name__ == "__main__":
    process_pdf(INPUT_PDF)
