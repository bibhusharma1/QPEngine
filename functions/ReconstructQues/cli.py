"""Command-line interface for ReconstructQues."""

import argparse
import sys
from pathlib import Path

from .generator import DocumentGenerator
from .json_loader import discover_json_paths
from .sample_generator import generate_combined_sample
from config.reconstruct import (
    INPUT_JSON, OUTPUT_DIR as RECON_OUTPUT_DIR, SAMPLE_COUNT, SAMPLE_PDF,
    SAMPLE_SEED, SOURCE_PDF_DIR, project_path,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for CLI execution."""
    parser = argparse.ArgumentParser(
        prog="ReconstructQues",
        description="Reconstruct an editable Word (.docx) exam paper from questions_parsed.json.",
    )
    parser.add_argument(
        "-i", "--input",
        dest="input_path",
        default=None,
        help="Path to questions_parsed.json. If omitted, searches .env and standard locations.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        dest="output_dir",
        default=None,
        help="Directory to save the generated DOCX. Defaults to QUES_RECON_OUTPUT_DIR from .env.",
    )
    parser.add_argument(
        "-f", "--filename",
        dest="filename",
        default="questions_reconstructed.docx",
        help="Filename for the output DOCX (default: questions_reconstructed.docx).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Fail immediately if any referenced asset file is missing.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--sample",
        nargs="?",
        const=SAMPLE_COUNT,
        type=int,
        metavar="COUNT",
        help="Generate up to COUNT individual items from the selected PDF only (default: configured sample count).",
    )
    mode.add_argument("--all", action="store_true", help="Generate full reconstructed DOCX files for every parsed JSON bank (also the default mode).")
    parser.add_argument("--seed", type=int, default=int(SAMPLE_SEED) if SAMPLE_SEED else None, help="Optional random seed for reproducible sample selection.")
    parser.add_argument("--pdf-file", default=SAMPLE_PDF or None, help="One source PDF to sample. Defaults to QUES_RECON_SAMPLE_PDF.")
    parser.add_argument(
        "--pdf-dir",
        default=SOURCE_PDF_DIR,
        help="Folder containing source PDFs; used when --pdf-file is not set.",
    )
    return parser


def main(args: list[str] | None = None) -> int:
    """CLI execution entrypoint."""
    parser = build_parser()
    parsed = parser.parse_args(args)

    print("=" * 60)
    print("ReconstructQues: DOCX Question Bank Reconstructor")
    print("=" * 60)

    try:
        if parsed.sample is not None:
            if parsed.sample < 1:
                parser.error("--sample COUNT must be a positive integer")
            input_paths = [Path(parsed.input_path)] if parsed.input_path else ([project_path(INPUT_JSON)] if INPUT_JSON else discover_json_paths())
            pdf_target = Path(parsed.pdf_file).expanduser() if parsed.pdf_file else Path(parsed.pdf_dir).expanduser()
            output_dir = Path(parsed.output_dir) if parsed.output_dir else project_path(RECON_OUTPUT_DIR) / "_combined"
            result = generate_combined_sample(
                pdf_dir=pdf_target,
                json_paths=input_paths,
                max_items_per_pdf=parsed.sample,
                seed=parsed.seed,
                output_dir=output_dir,
                filename=parsed.filename if parsed.filename != "questions_reconstructed.docx" else f"{pdf_target.stem}_sample.docx",
                strict_assets=parsed.strict,
            )
            print(f"\nSample DOCX: {result.docx_path}")
            print(f"Seed: {result.seed if result.seed is not None else '<not set>'}")
            print("Per-PDF summary:")
            for row in result.pdf_summaries:
                status = " (UNMATCHED)" if not row.matched_items else ""
                print(f"  {row.pdf_name}: {row.matched_items} matching items, {row.selected_items} selected{status}")
            print("JSON inputs:")
            for path in input_paths:
                print(f"  {path}")
            return 0

        filename = parsed.filename
        # Full generation always processes every parsed bank unless a specific
        # JSON file is explicitly supplied. The local sample JSON setting only
        # configures sample mode.
        input_paths = [Path(parsed.input_path)] if parsed.input_path else discover_json_paths()
        if not input_paths:
            input_paths = [None]
        for input_path in input_paths:
            output_dir = Path(parsed.output_dir) / (input_path.parent.name if input_path else "questions") if parsed.output_dir else None
            generator = DocumentGenerator(
                json_path=input_path,
                output_dir=output_dir,
                strict_assets=parsed.strict,
            )
            result = generator.generate(filename=filename)

            print("\nGeneration completed successfully:")
            print(f"  • Input file:       {generator.json_path or '<auto-detect>'}")
            print(f"  • Output file:      {result.docx_path}")
            print(f"  • Main questions:   {result.total_questions}")
            print(f"  • Subquestions:     {result.total_subquestions}")
            print(f"  • Figures inserted: {result.total_assets_inserted}")

            if result.missing_assets:
                print(f"\n⚠️ Missing assets ({len(result.missing_assets)}):")
                for m in result.missing_assets:
                    print(f"  - {m}")

            if result.warnings and not result.missing_assets:
                print(f"\n⚠️ Warnings ({len(result.warnings)}):")
                for w in result.warnings:
                    print(f"  - {w}")

        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

