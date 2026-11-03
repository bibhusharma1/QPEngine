"""Incremental end-to-end runner for Scrapper -> Parser -> Reconstruction.

Run from the repository root with ``.venv/bin/python pipeline_runner.py``.
Existing scraper records, parsed banks, and reconstructed DOCX files are
preserved and skipped by default.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import parser as parser_config
from config import scrapper as scrapper_config
from config.outputs import stage_output_dir
from functions.QuesScrapper.ScrapePipeLIne.pipeline import run as run_scrapper
from functions.QuesParser.ParsePipeLIne.question_bank import run as run_parser
from functions.ReconstructQues.generator import DocumentGenerator


def _manifest_source(manifest: Path) -> str:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return str(data.get("source_pdf") or manifest.parent.name)
    except (OSError, json.JSONDecodeError, AttributeError):
        return manifest.parent.name


def _manifests() -> list[Path]:
    return sorted(Path(scrapper_config.OUTPUT_DIR).glob("*/manifest.json"))


def _parsed_path(source_pdf: str) -> Path:
    return stage_output_dir("Parser", source_pdf, stage_root=parser_config.OUTPUT_DIR) / "questions_parsed.json"


def _reconstruct(json_path: Path) -> Path:
    result = DocumentGenerator(json_path=json_path).generate()
    print(f"Reconstructed: {result.docx_path}")
    return result.docx_path


def main() -> int:
    argp = argparse.ArgumentParser(description="Run the MasterDeOrca PDF question pipeline incrementally.")
    stages = argp.add_mutually_exclusive_group()
    stages.add_argument("--scrape-only", action="store_true", help="Run the scraper stage only.")
    stages.add_argument("--parse-only", action="store_true", help="Parse scraper manifests that do not have a parsed bank yet.")
    stages.add_argument("--reconstruct-only", action="store_true", help="Reconstruct existing parsed banks that do not have a DOCX yet.")
    args = argp.parse_args()

    do_scrape = not (args.parse_only or args.reconstruct_only)
    do_parse = not (args.scrape_only or args.reconstruct_only)
    do_reconstruct = not (args.scrape_only or args.parse_only)

    if do_scrape:
        print("\n=== Scrapper ===")
        run_scrapper()

    fresh_parsed: list[Path] = []
    if do_parse:
        print("\n=== Parser ===")
        parse_manifests = []
        for manifest in _manifests():
            output = _parsed_path(_manifest_source(manifest))
            if output.is_file():
                print(f"Skipping (already parsed): {manifest.parent.name}")
            else:
                parse_manifests.append(str(manifest))
        if parse_manifests:
            run_parser(manifest_paths=parse_manifests)
            fresh_parsed = [_parsed_path(_manifest_source(Path(path))) for path in parse_manifests]
        else:
            print("No new scraper manifests need parsing.")

    if do_reconstruct:
        print("\n=== DOCX Reconstruction ===")
        candidates = fresh_parsed if not args.reconstruct_only else sorted(Path(parser_config.OUTPUT_DIR).glob("*/questions_parsed.json"))
        generated = 0
        for json_path in candidates:
            source_name = json_path.parent.name
            docx_path = stage_output_dir("Reconstruct", source_name) / "questions_reconstructed.docx"
            if docx_path.is_file():
                print(f"Skipping (already reconstructed): {source_name}")
                continue
            _reconstruct(json_path)
            generated += 1
        if generated == 0:
            print("No parsed banks need reconstruction.")

    print("\nPipeline runner finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
