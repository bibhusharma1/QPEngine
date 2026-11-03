"""Central output layout helpers shared by all functional modules."""

import re
import hashlib
from pathlib import Path

from config.settings import PROJECT_ROOT, RECONSTRUCT_ROOT

OUTPUT_ROOT = RECONSTRUCT_ROOT.parent


def pdf_stem(pdf_name: str | Path) -> str:
    """Return a safe folder name based on a source PDF basename."""
    normalized = str(pdf_name).replace("\\", "/")
    stem = Path(normalized).stem
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" ._") or "unknown"
    if safe != stem or Path(normalized).suffix != ".pdf":
        suffix = hashlib.sha256(Path(normalized).name.encode("utf-8")).hexdigest()[:8]
        return f"{safe}-{suffix}"
    return safe


def stage_output_dir(
    stage: str,
    pdf_name: str | Path | None = None,
    *,
    stage_root: str | Path | None = None,
) -> Path:
    """Return a stage root or the folder for a specific PDF."""
    base = Path(stage_root) if stage_root is not None else OUTPUT_ROOT / stage
    return base / pdf_stem(pdf_name) if pdf_name is not None else base
