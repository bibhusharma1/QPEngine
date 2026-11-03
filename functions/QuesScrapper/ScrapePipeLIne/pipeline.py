# Batch entry point: parse the PDFs configured via .env/config.py
# (folders to scan + optional explicit file list), write each PDF's
# extraction and page records into the configured external Scrapper root.
#
# Run from the project root:
#   python -m functions.QuesScrapper.ScrapePipeLIne.pipeline

import sys
import json
from pathlib import Path

from config.scrapper import INPUT_DIRS, INPUT_PDFS, OUTPUT_DIR
from config.outputs import pdf_stem, stage_output_dir

from functions.QuesScrapper.ScrapePipeLIne.processPDF import process_pdf
from functions.QuesScrapper.ScrapePipeLIne.quesandFig import append_records


def _already_scraped(json_file: Path, pdf_path: Path) -> bool:
    """Return true when this per-PDF JSON already contains its source records."""
    if not json_file.is_file():
        return False
    try:
        rows = json.loads(json_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(rows, list):
        return False
    expected_path = str(pdf_path.resolve())
    expected_name = pdf_path.name.casefold()
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("pdf_name") or row.get("source_pdf") or "").strip()
        if source and (
            source.replace("\\", "/").casefold() == expected_path.replace("\\", "/").casefold()
            or Path(source.replace("\\", "/")).name.casefold() == expected_name
        ):
            return True
    return False


def collect_pdf_files(input_dirs, input_pdfs):
    """Resolve the PDF batch from configured explicit files +/or folders.

    Args:
        input_dirs: Folder list — each is searched recursively for *.pdf.
        input_pdfs: Explicit PDF paths. When set, ONLY these are read and
            the folders are ignored, so "which pdfs to read" stays exact.

    Returns:
        Sorted list of resolved PDF paths (de-duplicated).
    """
    if input_pdfs:
        # Explicit selection takes precedence over folder scanning.
        pdf_files = set()
        for pdf_name in input_pdfs:
            pdf = Path(pdf_name)
            if not pdf.is_file():
                print(f"Warning: listed PDF not found, skipping: {pdf}", file=sys.stderr)
                continue
            pdf_files.add(pdf.resolve())
        return sorted(pdf_files)

    pdf_files = set()
    for folder_name in input_dirs:
        folder = Path(folder_name)
        if not folder.is_dir():
            print(f"Warning: input folder not found, skipping: {folder}", file=sys.stderr)
            continue
        # rglob so PDFs in subfolders count too; set() dedupes overlaps.
        pdf_files |= {pdf.resolve() for pdf in folder.rglob("*") if pdf.is_file() and pdf.suffix.lower() == ".pdf"}

    return sorted(pdf_files)


def run():
    """Run the full multi-PDF scrape and append the dataset.

    Behaviour:
        - Collects PDFs from every configured input folder, or — when
          QUES_INPUT_PDFS is set — exclusively the listed files.
        - Each PDF is isolated: one failing PDF prints an error and the
          batch continues with the rest.
        - Dataset file is upserted per (pdf_name, page_number), so repeated
          runs never duplicate records.

    Exits non-zero if no PDF was found or any PDF failed.
    """
    pdf_files = collect_pdf_files(INPUT_DIRS, INPUT_PDFS)

    if not pdf_files:
        print("No PDF files found. Check QUES_INPUT_DIRS / QUES_INPUT_PDFS.", file=sys.stderr)
        raise SystemExit(1)

    print(f"Found {len(pdf_files)} PDF file(s)")

    output_dir = Path(OUTPUT_DIR)
    succeeded = 0
    failed = 0
    skipped = 0
    total_records = 0

    for pdf_path in pdf_files:
        json_file = stage_output_dir("Scrapper", pdf_path.name, stage_root=output_dir) / "questions_scrapper.json"
        if _already_scraped(json_file, pdf_path):
            skipped += 1
            print(f"Skipping (already in questions_scrapper.json): {pdf_path.name}")
            continue
        print(f"\nProcessing: {pdf_path.name}")
        try:
            records, manifest = process_pdf(
                pdf_path,
                # Per-PDF subfolder avoids page_NNNN.* name collisions.
                stage_output_dir("Scrapper", pdf_path.name, stage_root=output_dir),
            )
        except Exception as exc:
            failed += 1
            print(f"FAILED: {pdf_path.name} -> {exc}", file=sys.stderr)
            continue

        total_records += append_records(records, json_file)
        succeeded += 1
        print(
            f"Done: {manifest['page_count']} page(s) -> "
            f"{output_dir / pdf_stem(pdf_path.name)}"
        )

    print("\nSummary:")
    print(f"  PDFs processed: {succeeded}")
    print(f"  PDFs skipped:   {skipped}")
    print(f"  PDFs failed:    {failed}")
    print(f"  Records in per-PDF questions_scrapper.json files: {total_records}")

    if failed:
        raise SystemExit(1)
    return {"processed": succeeded, "skipped": skipped, "failed": failed}


if __name__ == "__main__":
    run()
