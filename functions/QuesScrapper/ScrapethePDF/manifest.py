# Manifest serialization layer: writes the per-PDF extraction report.

import json
from pathlib import Path


def save_manifest(data, output_path):
    """Write a manifest dict to disk as pretty-printed JSON.

    Args:
        data: Manifest dict assembled by the pipeline (source PDF,
            page records, embedded/vector figure records).
        output_path: Destination file path (created if missing).

    Returns:
        The output_path as a Path after writing.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ensure_ascii=False keeps non-ASCII question text readable.
    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return output_path
