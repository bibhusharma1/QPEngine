"""Persistent per-subject Question Bank records stored in SQLite.

Parsed documents are flattened into leaf rows (a parent with subquestions is
never stored as a separate item), and stable uuid5 IDs prevent cross-document
collisions. Flexible question data remains JSON-encoded in the database.
"""

from __future__ import annotations

import base64
import json
import math
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from src.masterdeorca import database
from src.masterdeorca.storage import ensure_subject, subject_layout

BANK_FILENAME = "question_bank.json"
_IDEMPOTENCY_FILENAME = "create_idempotency.json"
_NAMESPACE = uuid.UUID("8f3c2a1e-6d4b-4e5a-9c7f-2b1d0a5e7c33")
_VALIDATOR_ID = "reconciliation-validator/2"

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}

_SYNC_FIELDS = ("text", "original_text", "cleaned_text", "marks", "section_name",
                "page_number", "source_pages", "source_pdf", "source_path",
                "assets", "tables", "original_question_number", "status",
                "failure_reason", "structuring_error", "validation_issues",
                "section_instructions", "marks_notation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bank_path(subject: str) -> Path:
    return subject_layout(subject)["base"] / BANK_FILENAME


def _signature(text: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def _record_text(record: dict) -> str:
    for key in ("text", "cleaned_text", "original_text", "question", "content"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def normalize_marks(value: object) -> int | float | None:
    """Numeric marks for filtering/exposure; null for missing or unusable values."""
    if value is None or type(value) is bool:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric <= 0:
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _norm_marks(value: object) -> int | float | None:
    if value is None:
        return None
    return normalize_marks(value)


# ---------------------------------------------------------------------------
# Locked, atomic bank file I/O
# ---------------------------------------------------------------------------


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    key = str(path)
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        yield


def _read_bank(subject: str, path: Path) -> dict:
    with database.connect() as db:
        rows = db.execute(
            "SELECT record_json,updated_at FROM question_bank_records "
            "WHERE subject_name=? ORDER BY position,question_id",
            (subject,),
        ).fetchall()
    questions = [json.loads(row["record_json"]) for row in rows]
    return {
        "subject": subject,
        "updated_at": max((row["updated_at"] for row in rows), default=_now()),
        "questions": questions,
    }


def _replace_bank_records(subject: str, rows: list[dict]) -> None:
    now = _now()
    with database.connect() as db:
        existing = {
            row["question_id"]: (row["record_json"], row["position"])
            for row in db.execute(
                "SELECT question_id,record_json,position FROM question_bank_records "
                "WHERE subject_name=?",
                (subject,),
            ).fetchall()
        }
        documents = {
            row["filename"]: row["document_id"]
            for row in db.execute(
                "SELECT filename,document_id FROM documents WHERE subject_name=?",
                (subject,),
            ).fetchall()
        }
        retained: set[str] = set()
        for position, source in enumerate(rows):
            record = dict(source)
            question_id = str(
                record.get("question_id")
                or record.get("id")
                or _stable_id(subject, str(record.get("source_pdf") or ""), f"row-{position}")
            )
            record["question_id"] = question_id
            retained.add(question_id)
            source_pdf = record.get("source_pdf")
            document_id = (
                documents.get(Path(source_pdf).name)
                if isinstance(source_pdf, str) and source_pdf
                else None
            )
            serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if existing.get(question_id) == (serialized, position):
                continue
            db.execute(
                """INSERT INTO question_bank_records(
                    question_id,subject_name,document_id,source_pdf,parent_question_id,
                    marks_numeric,record_json,created_at,updated_at,position
                ) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET
                    subject_name=excluded.subject_name,
                    document_id=excluded.document_id,
                    source_pdf=excluded.source_pdf,
                    parent_question_id=excluded.parent_question_id,
                    marks_numeric=excluded.marks_numeric,
                    record_json=excluded.record_json,
                    updated_at=excluded.updated_at,
                    position=excluded.position""",
                (
                    question_id,
                    subject,
                    document_id,
                    source_pdf if isinstance(source_pdf, str) else None,
                    record.get("parent_question_id") or record.get("parent_id"),
                    normalize_marks(record.get("marks")),
                    serialized,
                    str(record.get("created_at") or now),
                    str(record.get("updated_at") or now),
                    position,
                ),
            )
        for question_id in existing.keys() - retained:
            db.execute(
                "DELETE FROM question_bank_records WHERE subject_name=? AND question_id=?",
                (subject, question_id),
            )


def _write_bank(subject: str, path: Path, bank: dict) -> None:
    _replace_bank_records(subject, bank["questions"])


def _load_bank_rows(subject: str) -> list[dict]:
    path = bank_path(subject)
    data = _read_bank(subject, path)
    return [dict(row) for row in data["questions"] if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# Flattening parsed records into bank rows
# ---------------------------------------------------------------------------


def _stable_id(subject: str, source_pdf: str, identity: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{subject}/{source_pdf}/{identity}"))


def _clean_label(label: object) -> str:
    text = str(label or "").strip()
    if len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return text.strip("() \t")


def _record_ref(record: dict, index: int) -> str:
    for key in ("original_question_number", "label", "original_label", "question_id", "sub_id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    signature = _signature(_record_text(record))
    return signature[:48] or f"item-{index}"


def _flatten_records(subject: str, source_pdf: str, records: list, out: list[dict],
                     *, defaults: dict | None = None, parent_id: str | None = None,
                     parent_number: str | None = None, chain: str = "",
                     nested: bool = False) -> None:
    inherited = defaults or {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        subs = [sub for sub in (record.get("sub_questions") or record.get("subquestions") or [])
                if isinstance(sub, dict)]
        own_ref = _record_ref(record, index)
        full_identity = f"{chain}|{own_ref}" if chain else own_ref
        record_id = record.get("question_id") or record.get("id")
        if not record_id:
            record_id = _stable_id(subject, source_pdf, full_identity)
        if nested:
            number = f"{parent_number or chain or 'Q'}({_clean_label(own_ref)})"
        else:
            number = record.get("original_question_number") or record.get("label") \
                or record.get("original_label") or parent_number
        if subs:
            child_defaults = dict(inherited)
            for key in ("section_name", "source_page", "page_number", "source_pages",
                        "assets", "figure_paths", "tables", "text"):
                if record.get(key) not in (None, "", []):
                    child_defaults[key] = record.get(key)
            _flatten_records(subject, source_pdf, subs, out, defaults=child_defaults,
                             parent_id=record_id, parent_number=number, chain=full_identity,
                             nested=True)
            continue
        _leaf_row(subject, source_pdf, record, inherited, out,
                  record_id=record_id, number=number)
    return out


def _leaf_row(subject: str, source_pdf: str, record: dict, inherited: dict, out: list[dict],
              *, record_id: str, number: str | None) -> None:
    row = dict(record)
    row.pop("sub_questions", None)
    row.pop("subquestions", None)
    row["question_id"] = record_id
    row["original_question_number"] = number
    text = _record_text(row) or str(inherited.get("text") or "")
    original_text = row.get("original_text") or row.get("text") or inherited.get("text") or text
    row["text"] = text
    row["original_text"] = original_text
    row["cleaned_text"] = row.get("cleaned_text") or text
    row["marks"] = _norm_marks(row.get("marks") if row.get("marks") is not None else inherited.get("marks"))
    row["section_name"] = row.get("section_name") or inherited.get("section_name")
    page = row.get("page_number") or row.get("source_page") or inherited.get("page_number") \
        or inherited.get("source_page")
    row["page_number"] = page
    pages = row.get("source_pages") or ([page] if page is not None else inherited.get("source_pages") or [])
    row["source_pages"] = pages
    row["source_pdf"] = source_pdf
    row["source_path"] = row.get("source_path") or f"source/{source_pdf}"
    row["assets"] = row.get("assets") or inherited.get("assets") or []
    row["figure_paths"] = row.get("figure_paths") or inherited.get("figure_paths") or []
    row["tables"] = row.get("tables") or inherited.get("tables") or []
    row.setdefault("priority", 1)
    row.setdefault("metadata", {})
    out.append(row)


def _importable(row: dict) -> bool:
    verdict = row.get("validation")
    if isinstance(verdict, dict) and verdict.get("validator") == _VALIDATOR_ID:
        return verdict.get("outcome") == "valid"
    return True


# ---------------------------------------------------------------------------
# Parsed-result synchronization
# ---------------------------------------------------------------------------


def sync_from_parsed(subject: str, parsed_json: Path | str | bytes, source_pdf: str | None = None,
                     *, protect_edits: bool = False) -> int:
    """Append valid parsed records to the subject bank; returns the import count.

    Records carrying a reconciliation annotation are imported only when their
    outcome is ``valid``. Existing rows are refreshed in place (preserving
    operator fields); with ``protect_edits`` a row whose wording no longer
    matches the parsed record is treated as manually maintained and skipped.
    """
    try:
        if isinstance(parsed_json, bytes):
            data = json.loads(parsed_json.decode("utf-8"))
        else:
            parsed_json = Path(parsed_json)
            data = json.loads(parsed_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid parsed JSON: {exc}") from exc
    questions = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(questions, list):
        raise ValueError("Parsed JSON missing questions list")
    if source_pdf is None:
        source_pdf = (data.get("metadata") or {}).get("source_pdf") if isinstance(data, dict) else None
    if not source_pdf:
        source_pdf = parsed_json.parent.name if isinstance(parsed_json, Path) else "unknown"
    rows = _flatten_records(subject, str(source_pdf), questions, [])
    rows = [row for row in rows if _importable(row)]
    path = bank_path(subject)
    with _locked(path):
        bank = _read_bank(subject, path)
        existing_rows = bank["questions"]
        by_id: dict[str, int] = {}
        by_signature: dict[str, int] = {}
        for position, row in enumerate(existing_rows):
            if isinstance(row, dict) and row.get("question_id") is not None:
                by_id[str(row["question_id"])] = position
            signature = _signature(_record_text(row)) if isinstance(row, dict) else ""
            if signature:
                by_signature[signature] = position
        imported = 0
        for row in rows:
            question_id = str(row.get("question_id") or "")
            signature = _signature(_record_text(row))
            prior_position = by_id.get(question_id) if question_id else None
            if prior_position is not None:
                prior = existing_rows[prior_position]
                if protect_edits and _record_text(prior) != _record_text(row):
                    continue  # Manually maintained wording is never overwritten.
                if signature and signature in by_signature and by_signature[signature] != prior_position:
                    continue  # Exact wording already stored under another ID.
                prior_signature = _signature(_record_text(prior))
                if prior_signature and by_signature.get(prior_signature) == prior_position:
                    by_signature.pop(prior_signature, None)
                merged = dict(prior)
                for field in _SYNC_FIELDS:
                    if row.get(field) is not None:
                        merged[field] = row[field]
                merged["validation"] = row.get("validation") or merged.get("validation")
                merged["updated_at"] = _now()
                existing_rows[prior_position] = merged
                if not protect_edits:
                    imported += 1  # A refreshed record is a synced record.
                if signature:
                    by_signature[signature] = prior_position
                continue
            if signature and signature in by_signature:
                continue
            new_row = dict(row)
            new_row.setdefault("created_at", _now())
            new_row["updated_at"] = _now()
            existing_rows.append(new_row)
            by_id[str(new_row["question_id"])] = len(existing_rows) - 1
            if signature:
                by_signature[signature] = len(existing_rows) - 1
            imported += 1
        bank["questions"] = existing_rows
        bank["updated_at"] = _now()
        _write_bank(subject, path, bank)
    return imported


def sync_pdf(subject: str, filename: str) -> int:
    """Import a successfully processed document's parsed result into the bank."""
    if database.get_subject_id(subject) is None:
        raise ValueError(f"Unknown subject {subject!r}")
    document = database.get_document(subject, filename)
    if not document:
        raise ValueError(f"Document {filename} not found in subject {subject}")
    artifact = database.get_artifact(document["document_id"], "questions_parsed.json")
    if artifact is None:
        raise ValueError(f"Parsed result is not available for {filename}")
    return sync_from_parsed(subject, artifact["data"], source_pdf=filename)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_subject(subject: str) -> list[dict]:
    return _load_bank_rows(subject)


def _find_row(subject: str, question_id: str) -> dict:
    for row in _load_bank_rows(subject):
        if str(row.get("question_id")) == str(question_id):
            return row
    raise KeyError(f"Question {question_id} not found in subject {subject}")


def get_question_detail(subject: str, question_id: str) -> dict:
    row = dict(_find_row(subject, question_id))
    resources = list_question_resources(row, subject)
    row["subject_id"] = subject
    row["resources"] = resources
    row["has_figures"] = bool(resources["figures"])
    row["has_tables"] = bool(resources["tables"])
    return row


# ---------------------------------------------------------------------------
# Resources (figures / tables)
# ---------------------------------------------------------------------------


def _resolve_within(path_value: object, base: Path) -> Path | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    base_resolved = base.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        return None
    return resolved


def _asset_reference(asset: object, index: int) -> tuple[str, object] | None:
    if isinstance(asset, str):
        return str(index), asset
    if isinstance(asset, dict):
        reference = asset.get("id") or asset.get("asset_id") or asset.get("resource_id")
        path = asset.get("path") or asset.get("figure_path") or asset.get("image_path")
        if reference is None or path is None:
            return None
        return str(reference), asset
    return None


def _asset_path(asset: object) -> object:
    if isinstance(asset, str):
        return asset
    if isinstance(asset, dict):
        return asset.get("path") or asset.get("figure_path") or asset.get("image_path")
    return None


def _asset_caption(asset: object) -> str | None:
    if isinstance(asset, dict):
        for key in ("caption", "description", "alt"):
            value = asset.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _display_path(path_value: object, base: Path) -> str | None:
    resolved = _resolve_within(path_value, base)
    if resolved is None:
        return None
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path_value)


def _stored_figure(subject: str, source_pdf: object, asset: object) -> dict | None:
    if not isinstance(source_pdf, str) or not source_pdf:
        return None
    document = database.get_document(subject, Path(source_pdf).name)
    if not document:
        return None
    path = _asset_path(asset)
    filename = Path(str(path or "")).name
    reference = _asset_reference(asset, 0)
    asset_id = reference[0] if reference else ""
    with database.connect() as db:
        rows = db.execute(
            """SELECT artifact_key,content_type,data,metadata_json FROM stored_artifacts
               WHERE document_id=? AND artifact_type='figure'""",
            (document["document_id"],),
        ).fetchall()
    for row in rows:
        key = str(row["artifact_key"])
        if (filename and Path(key).name == filename) or (asset_id and asset_id in key):
            return {
                "artifact_key": key,
                "content_type": row["content_type"],
                "data": bytes(row["data"]),
            }
    return None


def list_question_resources(row: dict, subject: str) -> dict:
    """Retrievable figure/table references for one bank row."""
    figures = []
    for index, asset in enumerate(row.get("assets") or []):
        reference = _asset_reference(asset, index)
        if reference is None:
            continue
        resource_id, asset_value = reference
        stored = _stored_figure(subject, row.get("source_pdf"), asset_value)
        if stored is None:
            continue
        figures.append({
            "resource_id": resource_id,
            "path": stored["artifact_key"],
            "filename": Path(stored["artifact_key"]).name,
            "caption": _asset_caption(asset_value),
            "source_page": row.get("page_number"),
        })
    tables = []
    for index, table in enumerate(row.get("tables") or []):
        if not isinstance(table, dict):
            continue
        reference = table.get("table_id") or table.get("id") or table.get("resource_id") or str(index)
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        cells = table.get("cells") or []
        if not headers and not rows and not cells:
            continue
        tables.append({
            "resource_id": str(reference),
            "caption": table.get("caption") or table.get("description"),
            "headers": headers,
            "rows": rows,
        })
    return {"figures": figures, "tables": tables}


def get_question_resource(subject: str, question_id: str, content_type: str,
                          resource_id: str) -> dict:
    if content_type not in {"figure", "table"}:
        raise ValueError("content_type must be 'figure' or 'table'")
    row = _find_row(subject, question_id)
    if content_type == "figure":
        for index, asset in enumerate(row.get("assets") or []):
            reference = _asset_reference(asset, index)
            if reference is None or reference[0] != str(resource_id):
                continue
            stored = _stored_figure(subject, row.get("source_pdf"), reference[1])
            if stored is None:
                raise KeyError(f"Figure {resource_id} is not retrievable")
            return {
                "resource_id": str(resource_id),
                "type": "figure",
                "path": stored["artifact_key"],
                "content_base64": base64.b64encode(stored["data"]).decode("ascii"),
                "metadata": {
                    "filename": Path(stored["artifact_key"]).name,
                    "caption": _asset_caption(reference[1]),
                    "source_pdf": row.get("source_pdf"),
                    "source_page": row.get("page_number"),
                },
            }
        raise KeyError(f"Figure {resource_id} not found")
    for index, table in enumerate(row.get("tables") or []):
        if not isinstance(table, dict):
            continue
        reference = table.get("table_id") or table.get("id") or table.get("resource_id") or str(index)
        if str(reference) != str(resource_id):
            continue
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        cells = table.get("cells") or []
        if not headers and cells:
            headers = [str(cell) for cell in cells[0]]
            rows = cells[1:]
        if not headers and not rows:
            raise KeyError(f"Table {resource_id} has no structured content")
        return {
            "resource_id": str(resource_id),
            "type": "table",
            "content": {"headers": headers, "rows": [list(headers)] + [list(item) for item in rows]},
            "metadata": {
                "caption": table.get("caption") or table.get("description"),
                "source_pdf": row.get("source_pdf"),
                "source_page": row.get("page_number"),
            },
        }
    raise KeyError(f"Table {resource_id} not found")


# ---------------------------------------------------------------------------
# Manual create / update
# ---------------------------------------------------------------------------


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Value must be a string")
    return value.strip() or None


def _validated_marks(value: object) -> int:
    if type(value) is bool or not isinstance(value, (int, float, str)):
        raise ValueError("marks must be a positive integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError("marks must be a positive integer") from None
    if not math.isfinite(numeric) or numeric < 1 or numeric != int(numeric):
        raise ValueError("marks must be a positive integer")
    return int(numeric)


def _validated_priority(value: object) -> int:
    if type(value) is bool or not isinstance(value, int) or value not in {1, 2, 3}:
        raise ValueError("priority must be 1, 2 or 3")
    return value


def _validated_assets(value: object, base: Path) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("assets must be an array")
    for asset in value:
        if isinstance(asset, str):
            path_value: object = asset
        elif isinstance(asset, dict):
            path_value = asset.get("path") or asset.get("figure_path") or asset.get("image_path")
        else:
            raise ValueError("assets entries must be strings or objects")
        if _resolve_within(path_value, base) is None:
            raise ValueError(f"Asset path escapes the subject directory: {path_value!r}")
    return value


def _validated_tables(value: object) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("tables must be an array of objects")
    return value


def _validated_pages(value: object) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or any(type(item) is not int or item < 1 for item in value):
        raise ValueError("source_pages must be an array of positive integers")
    return value


def _validated_page(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError("page_number must be a positive integer")
    return value


def _idempotency_path(paths: dict) -> Path:
    return paths["metadata"] / _IDEMPOTENCY_FILENAME


def _read_idempotency(paths: dict) -> dict:
    with database.connect() as db:
        rows = db.execute(
            "SELECT key,value_json FROM idempotency_keys WHERE subject_id=?",
            (database.get_subject_id(paths["base"].name),),
        ).fetchall()
    return {row["key"]: json.loads(row["value_json"]) for row in rows}


def _write_idempotency(paths: dict, key: str, question_id: str) -> None:
    subject_id = database.get_subject_id(paths["base"].name)
    if subject_id is None:
        raise ValueError(f"Unknown subject {paths['base'].name!r}")
    with database.connect() as db:
        db.execute(
            """INSERT INTO idempotency_keys(subject_id,key,value_json,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(subject_id,key) DO UPDATE SET
               value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (subject_id, str(key), json.dumps(str(question_id)), _now()),
        )


def _matches_payload(prior: dict, payload: dict, text: str, marks: int) -> bool:
    if prior.get("text") != text or prior.get("marks") != marks:
        return False
    for key in ("section_name", "source_pdf", "source_path", "original_question_number",
                "page_number", "source_pages", "assets", "tables", "metadata",
                "question_type", "priority"):
        if key in payload and prior.get(key) != payload.get(key):
            return False
    return True


def create_question(subject: str, payload: dict) -> dict:
    """Create one manual bank record; retries with the same ID or key are safe."""
    payload = dict(payload or {})
    paths = ensure_subject(subject)
    base = paths["base"]
    text = payload.get("text") or payload.get("original_text") or payload.get("cleaned_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Question text is required")
    text = text.strip()
    marks = _validated_marks(payload.get("marks"))
    question_id = payload.get("question_id")
    if question_id is not None:
        question_id = str(question_id).strip()
        if not question_id:
            raise ValueError("question_id must be a non-empty string")
    idempotency_key = payload.get("idempotency_key")
    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip()
        if not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
    section_name = _optional_text(payload.get("section_name"))
    original_number = _optional_text(payload.get("original_question_number"))
    source_pdf = _optional_text(payload.get("source_pdf"))
    source_path = _optional_text(payload.get("source_path"))
    if source_path is not None and _resolve_within(source_path, base) is None:
        raise ValueError(f"Source path escapes the subject directory: {source_path!r}")
    question_type = _optional_text(payload.get("question_type") or payload.get("type"))
    metadata = payload.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    row = {
        "question_id": question_id or uuid.uuid4().hex,
        "text": text, "original_text": text, "cleaned_text": text,
        "marks": marks,
        "section_name": section_name,
        "source_pdf": source_pdf,
        "source_path": source_path,
        "original_question_number": original_number,
        "page_number": _validated_page(payload.get("page_number")),
        "source_pages": _validated_pages(payload.get("source_pages")),
        "assets": _validated_assets(payload.get("assets"), base),
        "tables": _validated_tables(payload.get("tables")),
        "metadata": metadata,
        "question_type": question_type,
        "priority": _validated_priority(payload.get("priority", 1)),
        "created_at": _now(), "updated_at": _now(),
    }
    path = bank_path(subject)
    with _locked(path):
        bank = _read_bank(subject, path)
        rows = bank["questions"]
        if question_id:
            prior = next((item for item in rows if str(item.get("question_id")) == question_id), None)
            if prior is not None:
                if _matches_payload(prior, payload, text, marks):
                    return dict(prior)
                raise FileExistsError(f"Question already exists with id: {question_id}")
        if idempotency_key:
            keys = _read_idempotency(paths)
            prior_id = keys.get(idempotency_key)
            if prior_id:
                prior = next((item for item in rows if str(item.get("question_id")) == prior_id), None)
                if prior is not None:
                    return dict(prior)
        rows.append(row)
        bank["questions"] = rows
        bank["updated_at"] = row["updated_at"]
        _write_bank(subject, path, bank)
        if idempotency_key:
            _write_idempotency(paths, idempotency_key, row["question_id"])
    return dict(row)


def update_question(subject: str, question_id: str, changes: dict) -> dict:
    """Patch supplied fields of an existing bank record in place."""
    changes = dict(changes or {})
    paths = ensure_subject(subject)
    base = paths["base"]
    path = bank_path(subject)
    with _locked(path):
        bank = _read_bank(subject, path)
        rows = bank["questions"]
        row = next((item for item in rows if str(item.get("question_id")) == str(question_id)), None)
        if row is None:
            raise KeyError(f"Question {question_id} not found in subject {subject}")
        if "text" in changes or "original_text" in changes or "cleaned_text" in changes:
            new_text = changes.get("text") or changes.get("original_text") or changes.get("cleaned_text")
            if not isinstance(new_text, str) or not new_text.strip():
                raise ValueError("Question text must not be empty")
            row["text"] = row["original_text"] = row["cleaned_text"] = new_text.strip()
        if "marks" in changes:
            row["marks"] = _validated_marks(changes["marks"])
        if "source_path" in changes:
            value = _optional_text(changes["source_path"])
            if value is not None and _resolve_within(value, base) is None:
                raise ValueError(f"Source path escapes the subject directory: {value!r}")
            row["source_path"] = value
        if "assets" in changes:
            row["assets"] = _validated_assets(changes["assets"], base)
        if "tables" in changes:
            row["tables"] = _validated_tables(changes["tables"])
        if "source_pages" in changes:
            row["source_pages"] = _validated_pages(changes["source_pages"])
        if "page_number" in changes:
            row["page_number"] = _validated_page(changes["page_number"])
        if "metadata" in changes:
            value = changes["metadata"]
            if value is not None and not isinstance(value, dict):
                raise ValueError("metadata must be an object")
            row["metadata"] = value if isinstance(value, dict) else {}
        for key in ("section_name", "source_pdf", "original_question_number", "question_type", "status"):
            if key in changes:
                row[key] = _optional_text(changes[key])
        if "priority" in changes:
            from src.masterdeorca.services import question_priorities
            question_priorities.set_priority(str(row["question_id"]), _validated_priority(changes["priority"]))
        row["updated_at"] = _now()
        bank["updated_at"] = row["updated_at"]
        _write_bank(subject, path, bank)
        return dict(row)


def delete_question(subject: str, question_id: str) -> bool:
    """Remove one bank record from the subject's persistent bank."""
    paths = ensure_subject(subject)
    path = bank_path(subject)
    with _locked(path):
        bank = _read_bank(subject, path)
        rows = bank["questions"]
        before = len(rows)
        bank["questions"] = [row for row in rows
                            if str(row.get("question_id")) != str(question_id)]
        if len(bank["questions"]) == before:
            return False
        bank["updated_at"] = _now()
        _write_bank(subject, path, bank)
    try:
        from src.masterdeorca.services import question_priorities
        question_priorities.delete_priority(question_id)
    except Exception:
        pass
    try:
        from src.masterdeorca.services import dashboard_metrics as dm
        dm.invalidate()
    except Exception:
        pass
    return True


def validate_question_changes(changes: dict) -> None:
    """Shared validation for draft question edits (text and marks)."""
    for key in ("text", "original_text", "cleaned_text"):
        if key in changes:
            value = changes[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must not be empty")
    if "marks" in changes:
        _validated_marks(changes["marks"])


# ---------------------------------------------------------------------------
# API serialization and dashboard counts
# ---------------------------------------------------------------------------


def serialize_question_for_api(subject: str, row: dict) -> dict:
    text = _record_text(row)
    return {
        "question_id": row.get("question_id"),
        "subject_id": subject,
        "text": text,
        "original_text": row.get("original_text") or text,
        "cleaned_text": row.get("cleaned_text") or text,
        "marks": normalize_marks(row.get("marks")),
        "section_name": row.get("section_name"),
        "original_question_number": row.get("original_question_number"),
        "source_pdf": row.get("source_pdf"),
        "source_path": row.get("source_path"),
        "page_number": row.get("page_number"),
        "source_pages": row.get("source_pages") or [],
        "assets": row.get("assets") or [],
        "tables": row.get("tables") or [],
        "metadata": row.get("metadata") or {},
        "question_type": row.get("question_type") or row.get("type"),
        "priority": row.get("priority", 1),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def count_questions(subjects: list[str]) -> dict:
    """Totals across subject banks; unreadable banks are reported unavailable."""
    total = 0
    unavailable: list[str] = []
    for subject in subjects:
        path = bank_path(subject)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unavailable.append(subject)
            continue
        rows = data.get("questions") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            unavailable.append(subject)
            continue
        ids = {str(row.get("question_id")) for row in rows if isinstance(row, dict) and row.get("question_id")}
        total += len(ids)
    return {"total": total, "unavailable": unavailable}
