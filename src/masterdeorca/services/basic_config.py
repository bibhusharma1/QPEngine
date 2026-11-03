"""Persistent Basic Configuration presets for the Generate Question Paper page.

Stored in SQLite as source of truth.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from src.masterdeorca import storage, database

DEFAULT_CONFIGURATION_ID = "default-config"
_MAX_TEXT_LENGTHS = {
    "university_name": 200,
    "college_name": 200,
    "department_name": 200,
    "paper_title": 200,
    "academic_year": 40,
    "semester": 60,
    "exam_type": 100,
    "subject_code": 80,
    "examination_date": 10,
    "duration": 100,
}
_OPTIONAL_TEXT_FIELDS = tuple(_MAX_TEXT_LENGTHS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return {
        "configuration_id": row["configuration_id"],
        "name": row["name"],
        "is_default": bool(row["is_default"]),
        "university_name": row["university_name"],
        "college_name": row["college_name"],
        "department_name": row["department_name"],
        "paper_title": row["paper_title"],
        "academic_year": row["academic_year"],
        "semester": row["semester"],
        "exam_type": row["exam_type"],
        "subject_id": row["subject_id"],
        "subject_code": row["subject_code"],
        "examination_date": row["examination_date"],
        "duration": row["duration"],
        "total_marks": row["total_marks"],
        "instructions": json.loads(row["instructions_json"]) if row["instructions_json"] else [],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _default_configuration() -> dict:
    """Neutral preset with no fabricated institutional information."""
    return {
        "configuration_id": DEFAULT_CONFIGURATION_ID,
        "name": "Default configuration",
        "university_name": None,
        "college_name": None,
        "department_name": None,
        "paper_title": None,
        "academic_year": None,
        "semester": None,
        "exam_type": None,
        "subject_id": None,
        "subject_code": None,
        "examination_date": None,
        "duration": None,
        "total_marks": None,
        "instructions": [],
        "is_default": True,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _clean_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_TEXT_LENGTHS[field]:
        raise ValueError(f"{field} must be no longer than {_MAX_TEXT_LENGTHS[field]} characters")
    return value


def _validate_subject_id(value: object, *, require_exists: bool = False,
                         grandfathered: object = None) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("subject_id must be a string or null")
    try:
        storage.validate_subject_name(value)
    except Exception as exc:
        raise ValueError(str(exc.detail)) from exc
    subject = value.strip()
    if require_exists and subject != grandfathered and subject not in storage.list_subject_names():
        raise ValueError(
            f"Subject does not exist: {subject}. Register it (or use an existing subject) first.")
    return subject


def _validate_instructions(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("instructions must be an array of strings")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("instructions entries must be non-empty strings")
        cleaned.append(item.strip())
    return cleaned


def validate_configuration(value: object, *, require_subject_exists: bool = False,
                           grandfathered_subject: object = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object")
    required = {"configuration_id", "name"}
    if not required.issubset(value):
        raise ValueError("Configuration requires configuration_id and name")
    if not isinstance(value["configuration_id"], str) or not value["configuration_id"].strip():
        raise ValueError("configuration_id must be a non-empty string")
    if not isinstance(value["name"], str) or not value["name"].strip():
        raise ValueError("name must be a non-empty string")
    if len(value["name"].strip()) > 120:
        raise ValueError("name must be no longer than 120 characters")
    if type(value.get("is_default", False)) is not bool:
        raise ValueError("is_default must be boolean")
    total_marks = value.get("total_marks")
    if total_marks is not None and (type(total_marks) is not int or not 1 <= total_marks <= 1000):
        raise ValueError("total_marks must be a positive integer no greater than 1000 or null")
    row = {"configuration_id": value["configuration_id"].strip(),
           "name": value["name"].strip(),
           "is_default": bool(value.get("is_default", False)),
           "subject_id": _validate_subject_id(value.get("subject_id"),
                                              require_exists=require_subject_exists,
                                              grandfathered=grandfathered_subject),
           "total_marks": total_marks,
           "instructions": _validate_instructions(value.get("instructions", []))}
    row.update({field: _clean_optional_text(value.get(field), field) for field in _OPTIONAL_TEXT_FIELDS})
    # Timestamps are server-owned but must survive the read/validation round-trip.
    for field in ("created_at", "updated_at"):
        if value.get(field) is not None and (not isinstance(value[field], str) or not value[field].strip()):
            raise ValueError(f"{field} must be a timestamp string")
        row[field] = value.get(field)
    if row["examination_date"] is not None:
        try:
            date.fromisoformat(row["examination_date"])
        except ValueError as exc:
            raise ValueError("examination_date must be an ISO date (YYYY-MM-DD) or null") from exc
    return row


def _ensure_default_exists() -> None:
    with database.connect() as db:
        row = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (DEFAULT_CONFIGURATION_ID,)).fetchone()
        if not row:
            now = _now()
            d = _default_configuration()
            db.execute("""INSERT INTO basic_configurations(
                configuration_id,name,is_default,university_name,college_name,department_name,
                paper_title,academic_year,semester,exam_type,subject_id,subject_code,
                examination_date,duration,total_marks,instructions_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d["configuration_id"], d["name"], 1, d["university_name"], d["college_name"], d["department_name"],
             d["paper_title"], d["academic_year"], d["semester"], d["exam_type"], d["subject_id"], d["subject_code"],
             d["examination_date"], d["duration"], d["total_marks"], json.dumps(d["instructions"]), d["created_at"], d["updated_at"]))


def list_configurations() -> list[dict]:
    _ensure_default_exists()
    with database.connect() as db:
        rows = db.execute("SELECT * FROM basic_configurations ORDER BY is_default DESC, name").fetchall()
    return [_row_to_dict(row) for row in rows]


def get_configuration(configuration_id: str) -> dict | None:
    _ensure_default_exists()
    with database.connect() as db:
        row = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (configuration_id,)).fetchone()
    return _row_to_dict(row) if row else None


def default_configuration() -> dict:
    """The effective default: the flagged row, or the canonical neutral preset."""
    _ensure_default_exists()
    with database.connect() as db:
        row = db.execute("SELECT * FROM basic_configurations WHERE is_default=1 ORDER BY configuration_id").fetchone()
        if not row:
            row = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (DEFAULT_CONFIGURATION_ID,)).fetchone()
    if not row:
        d = _default_configuration()
        return d
    return _row_to_dict(row)


def create_configuration(data: dict) -> dict:
    """Create a preset; internal IDs, timestamps and the default flag are server-owned."""
    row = validate_configuration({**data, "configuration_id": str(uuid.uuid4()), "is_default": False},
                                 require_subject_exists=True)
    now = _now()
    row["created_at"] = now
    row["updated_at"] = now
    with database.connect() as db:
        db.execute("""INSERT INTO basic_configurations(
            configuration_id,name,is_default,university_name,college_name,department_name,
            paper_title,academic_year,semester,exam_type,subject_id,subject_code,
            examination_date,duration,total_marks,instructions_json,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["configuration_id"], row["name"], 0, row["university_name"], row["college_name"], row["department_name"],
         row["paper_title"], row["academic_year"], row["semester"], row["exam_type"], row["subject_id"], row["subject_code"],
         row["examination_date"], row["duration"], row["total_marks"], json.dumps(row["instructions"]), row["created_at"], row["updated_at"]))
    return row


def update_configuration(configuration_id: str, changes: dict, *, replace: bool) -> dict:
    """Replace (PUT) or merge (PATCH) preset fields."""
    with database.connect() as db:
        current_row = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (configuration_id,)).fetchone()
        if not current_row:
            raise KeyError(configuration_id)
        current = _row_to_dict(current_row)
        if replace:
            proposed = {**current, **changes}
        else:
            allowed = {"name", *_OPTIONAL_TEXT_FIELDS, "subject_id", "total_marks", "instructions"}
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError("Only preset fields may be updated")
            proposed = {**current, **changes}
        grandfathered = proposed.get("subject_id") if proposed.get("subject_id") == current.get("subject_id") else None
        final = validate_configuration(proposed, require_subject_exists=True,
                                       grandfathered_subject=grandfathered)
        final["configuration_id"] = current["configuration_id"]
        final["created_at"] = current["created_at"]
        final["is_default"] = current["is_default"]
        final["updated_at"] = _now()
        db.execute("""UPDATE basic_configurations SET name=?,is_default=?,university_name=?,college_name=?,department_name=?,
            paper_title=?,academic_year=?,semester=?,exam_type=?,subject_id=?,subject_code=?,examination_date=?,duration=?,
            total_marks=?,instructions_json=?,updated_at=? WHERE configuration_id=?""",
        (final["name"], 1 if final["is_default"] else 0, final["university_name"], final["college_name"], final["department_name"],
         final["paper_title"], final["academic_year"], final["semester"], final["exam_type"], final["subject_id"], final["subject_code"],
         final["examination_date"], final["duration"], final["total_marks"], json.dumps(final["instructions"]), final["updated_at"], configuration_id))
    return final


def duplicate_configuration(configuration_id: str, name: str | None = None) -> dict:
    source = get_configuration(configuration_id)
    if source is None:
        raise KeyError(configuration_id)
    row = validate_configuration({**source, "configuration_id": str(uuid.uuid4()),
                                  "name": name or f"{source['name']} (copy)", "is_default": False},
                                 require_subject_exists=True)
    now = _now()
    row["created_at"] = now
    row["updated_at"] = now
    with database.connect() as db:
        db.execute("""INSERT INTO basic_configurations(
            configuration_id,name,is_default,university_name,college_name,department_name,
            paper_title,academic_year,semester,exam_type,subject_id,subject_code,
            examination_date,duration,total_marks,instructions_json,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["configuration_id"], row["name"], 0, row["university_name"], row["college_name"], row["department_name"],
         row["paper_title"], row["academic_year"], row["semester"], row["exam_type"], row["subject_id"], row["subject_code"],
         row["examination_date"], row["duration"], row["total_marks"], json.dumps(row["instructions"]), row["created_at"], row["updated_at"]))
    return row


def set_default_configuration(configuration_id: str) -> dict:
    _ensure_default_exists()
    with database.connect() as db:
        row = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (configuration_id,)).fetchone()
        if not row:
            raise KeyError(configuration_id)
        db.execute("UPDATE basic_configurations SET is_default=0")
        now = _now()
        db.execute("UPDATE basic_configurations SET is_default=1,updated_at=? WHERE configuration_id=?", (now, configuration_id))
        target = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (configuration_id,)).fetchone()
    return _row_to_dict(target)


def delete_configuration(configuration_id: str) -> bool:
    _ensure_default_exists()
    with database.connect() as db:
        row = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (configuration_id,)).fetchone()
        if not row:
            return False
        was_default = bool(row["is_default"])
        cur = db.execute("DELETE FROM basic_configurations WHERE configuration_id=?", (configuration_id,))
        if cur.rowcount == 0:
            return False
        if was_default:
            canonical = db.execute("SELECT * FROM basic_configurations WHERE configuration_id=?", (DEFAULT_CONFIGURATION_ID,)).fetchone()
            if canonical is None:
                d = _default_configuration()
                now = _now()
                db.execute("""INSERT INTO basic_configurations(
                    configuration_id,name,is_default,university_name,college_name,department_name,
                    paper_title,academic_year,semester,exam_type,subject_id,subject_code,
                    examination_date,duration,total_marks,instructions_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["configuration_id"], d["name"], 1, d["university_name"], d["college_name"], d["department_name"],
                 d["paper_title"], d["academic_year"], d["semester"], d["exam_type"], d["subject_id"], d["subject_code"],
                 d["examination_date"], d["duration"], d["total_marks"], json.dumps(d["instructions"]), d["created_at"], d["updated_at"]))
            else:
                db.execute("UPDATE basic_configurations SET is_default=0")
                db.execute("UPDATE basic_configurations SET is_default=1 WHERE configuration_id=?", (DEFAULT_CONFIGURATION_ID,))
    return True
