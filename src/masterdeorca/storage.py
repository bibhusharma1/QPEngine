"""Subject and document storage helpers with strict path validation."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException

from config.settings import DEFAULT_SUBJECT, STORAGE_ROOT

_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,79}$")

DOCUMENT_EXTENSIONS = (".pdf", ".docx", ".png", ".jpg", ".jpeg")
_DOCUMENT_MAGIC = {
    ".pdf": b"%PDF-",
    ".docx": b"PK\x03\x04",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
}


def validate_subject_name(name: str) -> str:
    if not _SUBJECT_RE.fullmatch(name) or name in {".", ".."}:
        raise HTTPException(status_code=422, detail="Invalid subject name.")
    return name


def validate_document_filename(filename: str) -> str:
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=422, detail="Filename must be a plain document filename.")
    if filename in {".", ".."} or len(filename) > 180:
        raise HTTPException(status_code=422, detail="Filename is too long.")
    if Path(filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Only PDF, DOCX, PNG, JPG and JPEG files are accepted.")
    return filename


def detect_document_type(filename: str, header: bytes) -> str:
    """Validate uploaded bytes against the filename extension; returns the format kind."""
    suffix = Path(filename).suffix.lower()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Only PDF, DOCX, PNG, JPG and JPEG files are accepted.")
    if not header.startswith(_DOCUMENT_MAGIC[suffix]):
        raise HTTPException(status_code=415, detail=f"{filename!r} does not match its file extension.")
    return "jpeg" if suffix in {".jpg", ".jpeg"} else suffix.lstrip(".")


def subject_path(name: str) -> Path:
    safe_name = validate_subject_name(name)
    root = STORAGE_ROOT.resolve()
    candidate = (root / safe_name).resolve()
    if candidate.parent != root:
        raise HTTPException(status_code=422, detail="Invalid subject path.")
    return candidate


def subject_layout(name: str) -> dict[str, Path]:
    base = subject_path(name)
    return {
        "base": base,
        "source": base / "source",
        "scrapper": base / "output" / "Scrapper",
        "parser": base / "output" / "Parser",
        "reconstruct": base / "output" / "Reconstruct",
        "manifests": base / "manifests",
        "logs": base / "logs",
        "metadata": base / "metadata",
    }


def ensure_subject(name: str) -> dict[str, Path]:
    paths = subject_layout(name)
    from src.masterdeorca.database import connect, ensure_subject_record

    with connect() as db:
        ensure_subject_record(db, name)
    return paths


def list_subject_names() -> list[str]:
    from src.masterdeorca.database import connect

    with connect() as db:
        rows = db.execute("SELECT subject_name FROM subjects ORDER BY subject_name COLLATE NOCASE").fetchall()
    return [str(row["subject_name"]) for row in rows]


def document_path(subject: str, filename: str) -> Path:
    safe_filename = validate_document_filename(filename)
    paths = subject_layout(subject)
    result = (paths["source"] / safe_filename).resolve()
    base = paths["base"].resolve()
    source_dir = paths["source"].resolve()
    if source_dir.parent != base or result.parent != source_dir:
        raise HTTPException(status_code=422, detail="Invalid document path.")
    return result


# Backward compatibility
pdf_path = document_path
validate_pdf_filename = validate_document_filename
validate_document_name = validate_document_filename


def default_subject() -> None:
    ensure_subject(DEFAULT_SUBJECT)
