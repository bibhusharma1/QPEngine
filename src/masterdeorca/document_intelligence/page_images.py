"""Prepare document pages as images for image-based Nemotron extraction.

No OCR or GPU inference is performed here; this module only rasterizes or
normalizes input documents into ordered page images.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

import fitz

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class PageImage:
    page_number: int
    data: bytes
    mime: str
    width: int
    height: int
    source_format: str


def _dpi() -> int:
    try:
        return int(getattr(settings, "NVIDIA_RENDER_DPI", 200))
    except (TypeError, ValueError):
        return 200


def _image_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _render_pdf_pages(pdf_path: Path, dpi: int) -> Iterator[PageImage]:
    scale = dpi / 72.0
    doc = fitz.open(str(pdf_path))
    try:
        for i, page in enumerate(doc):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            yield PageImage(
                page_number=i + 1,
                data=pixmap.tobytes("png"),
                mime="image/png",
                width=pixmap.width,
                height=pixmap.height,
                source_format="pdf",
            )
    finally:
        doc.close()


def _docx_to_pdf(docx_path: Path, out_dir: Path) -> Path:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError(
            "DOCX rendering in Lite mode requires LibreOffice (soffice) on PATH. "
            "Install libreoffice-writer to enable DOCX page-image conversion."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out_dir.iterdir()} if out_dir.is_dir() else set()
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "LibreOffice DOCX->PDF conversion failed: "
            + (result.stderr or result.stdout or "").strip()
        )
    candidates = [
        p for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf" and p.name not in before
    ]
    if not candidates:
        expected = out_dir / (docx_path.stem + ".pdf")
        if expected.is_file():
            candidates = [expected]
    if not candidates:
        raise RuntimeError("LibreOffice did not produce a PDF for the DOCX input.")
    return candidates[0]


def _render_docx_pages(docx_path: Path, dpi: int) -> Iterator[PageImage]:
    with tempfile.TemporaryDirectory(prefix="masterdeorca-lite-") as tmpdir:
        pdf_path = _docx_to_pdf(docx_path, Path(tmpdir))
        scale = dpi / 72.0
        doc = fitz.open(str(pdf_path))
        try:
            for i, page in enumerate(doc):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                yield PageImage(
                    page_number=i + 1,
                    data=pixmap.tobytes("png"),
                    mime="image/png",
                    width=pixmap.width,
                    height=pixmap.height,
                    source_format="docx",
                )
        finally:
            doc.close()


def _render_image_pages(image_path: Path) -> Iterator[PageImage]:
    suffix = image_path.suffix.lower()
    raw = image_path.read_bytes()
    if suffix in {".png", ".jpg", ".jpeg"}:
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        try:
            with Image.open(io.BytesIO(raw)) as img:
                width, height = img.size
        except Exception:
            width, height = 0, 0
        yield PageImage(
            page_number=1,
            data=raw,
            mime=mime,
            width=width,
            height=height,
            source_format=suffix.lstrip("."),
        )
        return
    with Image.open(image_path) as img:
        n_frames = getattr(img, "n_frames", 1)
        for i in range(n_frames):
            img.seek(i)
            frame = img.convert("RGB")
            png = _image_to_png_bytes(frame)
            yield PageImage(
                page_number=i + 1,
                data=png,
                mime="image/png",
                width=frame.width,
                height=frame.height,
                source_format=suffix.lstrip(".") or "image",
            )


def render_page_images(path, dpi: int | None = None) -> Iterator[PageImage]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if dpi is None:
        dpi = _dpi()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        yield from _render_pdf_pages(path, dpi)
    elif suffix == ".docx":
        yield from _render_docx_pages(path, dpi)
    else:
        yield from _render_image_pages(path)


def is_blank_image(data: bytes, *, threshold: float = 4.0) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            grayscale = img.convert("L")
            lo, hi = grayscale.getextrema()
            return (hi - lo) < threshold
    except Exception:
        return False