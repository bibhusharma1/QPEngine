"""Safe asset resolution, validation, sizing, and insertion for DOCX."""

import logging
from pathlib import Path
from PIL import Image

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from .styles import COLOR_MUTED, FONT_NAME

logger = logging.getLogger(__name__)


class AssetHandler:
    """Manages asset resolution, validation, duplicate prevention, and DOCX insertion."""

    def __init__(
        self,
        base_dir: Path | None = None,
        context_dir: Path | None = None,
        strict: bool = False,
        max_width_inches: float = 5.5,
        max_height_inches: float = 4.8,
    ):
        self.base_dir = (base_dir or Path.cwd()).resolve()
        self.context_dir = context_dir.resolve() if context_dir else None
        self.strict = strict
        self.max_width_inches = max_width_inches
        self.max_height_inches = max_height_inches

        # Tracking state
        self.inserted_asset_keys: set[str] = set()
        self.inserted_count: int = 0
        self.missing_assets: list[str] = []
        self.warnings: list[str] = []

    def resolve_path(self, raw_path: str) -> Path:
        """Safely resolve an asset path within allowed directories."""
        if not raw_path or not raw_path.strip():
            raise ValueError("Empty asset path provided.")

        candidate = Path(raw_path.strip())

        # If absolute, verify it's within base_dir or context_dir
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            # First try relative to base_dir
            resolved = (self.base_dir / candidate).resolve()
            if not resolved.exists() and self.context_dir:
                # Fallback to context_dir
                alt = (self.context_dir / candidate).resolve()
                if alt.exists():
                    resolved = alt

        # Security check: verify path is contained in permitted directory
        allowed_roots = [self.base_dir]
        if self.context_dir:
            allowed_roots.append(self.context_dir)

        is_permitted = any(
            self._is_subpath(resolved, root) for root in allowed_roots
        )
        if not is_permitted:
            raise ValueError(
                f"Security violation: asset path '{raw_path}' resolves outside permitted root: {resolved}"
            )

        return resolved

    @staticmethod
    def _is_subpath(child: Path, parent: Path) -> bool:
        """Check if child path is within parent directory."""
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    def insert_asset(
        self,
        doc: Document,
        asset: dict,
        question_label: str = "",
    ) -> bool:
        """Verify, size, and insert an image asset into the document.
        
        Returns True if inserted, False if skipped (e.g. duplicate or missing non-strict).
        """
        raw_path = asset.get("path")
        asset_id = asset.get("asset_id") or ""

        if not raw_path:
            msg = f"Asset in {question_label or 'question'} missing 'path' property."
            if self.strict:
                raise ValueError(msg)
            self.missing_assets.append(f"{question_label}: <unspecified path>")
            self.warnings.append(msg)
            return False

        # Attempt to resolve
        try:
            resolved_path = self.resolve_path(raw_path)
        except ValueError as e:
            if self.strict:
                raise
            self.missing_assets.append(f"{raw_path} ({e})")
            self.warnings.append(str(e))
            return False

        # Check existence
        if not resolved_path.is_file():
            msg = f"Referenced asset file does not exist: {raw_path} (resolved: {resolved_path})"
            if self.strict:
                raise FileNotFoundError(msg)
            self.missing_assets.append(raw_path)
            self.warnings.append(msg)
            # Insert a clear placeholder notice in non-strict mode
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(f"[Figure missing: {Path(raw_path).name}]")
            run.font.name = FONT_NAME
            run.font.size = Pt(9.5)
            run.font.italic = True
            run.font.color.rgb = COLOR_MUTED
            return False

        # Duplicate prevention check
        dedup_key = f"{asset_id}:{resolved_path.as_posix()}" if asset_id else resolved_path.as_posix()
        if dedup_key in self.inserted_asset_keys:
            logger.info("Skipping duplicate asset insertion: %s", dedup_key)
            return False

        # Calculate dimensions preserving aspect ratio
        try:
            with Image.open(resolved_path) as img:
                orig_w, orig_h = img.size
                if orig_w <= 0 or orig_h <= 0:
                    raise ValueError("Invalid image dimensions")
                aspect = orig_h / orig_w
        except Exception as e:
            msg = f"Could not open image '{resolved_path}': {e}"
            if self.strict:
                raise ValueError(msg) from e
            self.missing_assets.append(raw_path)
            self.warnings.append(msg)
            return False

        # Target dimensions
        target_w = min(self.max_width_inches, orig_w / 96.0) if orig_w > 0 else self.max_width_inches
        target_w = max(target_w, 2.0)  # Reasonable minimum width
        target_w = min(target_w, self.max_width_inches)
        target_h = target_w * aspect

        if target_h > self.max_height_inches:
            target_h = self.max_height_inches
            target_w = target_h / aspect

        # Insert picture into document
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.keep_with_next = True

        run = p.add_run()
        run.add_picture(str(resolved_path), width=Inches(target_w), height=Inches(target_h))

        # Optional caption/description
        description = asset.get("description")
        if description and description.strip():
            cap_p = doc.add_paragraph()
            cap_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap_p.paragraph_format.space_before = Pt(2)
            cap_p.paragraph_format.space_after = Pt(8)
            cap_run = cap_p.add_run(description.strip())
            cap_run.font.name = FONT_NAME
            cap_run.font.size = Pt(9.5)
            cap_run.font.italic = True
            cap_run.font.color.rgb = COLOR_MUTED

        # Record inserted
        self.inserted_asset_keys.add(dedup_key)
        self.inserted_count += 1
        return True

