#!/usr/bin/env python3
"""Main execution pipeline runner for QuesParser Question-Bank Builder.

Reads the manifests/files configured in .env and builds the marks-indexed question bank.
Does not generate DOCX files. Does not run test scripts.
"""

import sys
from pathlib import Path

# Add project root to path for direct script execution as well as `-m`.
ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from functions.QuesParser.ParsePipeLIne.question_bank import run


def main():
    """Execute the question bank pipeline using .env configuration."""
    result = run()
    output_path = result.get("output_path")
    if output_path and Path(output_path).is_file():
        print(f"Success! Question bank built at: {output_path}")
    else:
        print("Pipeline finished.")


if __name__ == "__main__":
    main()
