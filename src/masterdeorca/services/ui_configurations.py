"""Persistent UI configuration presets for the Generate Question Paper page - stored in SQLite."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from src.masterdeorca import database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    item = {
        "config_id": row["config_id"],
        "name": row["name"],
        "is_default": bool(row["is_default"]),
        "values": json.loads(row["values_json"]) if row["values_json"] else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return item


def list_configurations() -> list[dict]:
    with database.connect() as db:
        rows = db.execute("SELECT * FROM ui_configurations ORDER BY name").fetchall()
    return [_row_to_dict(row) for row in rows]


def get_configuration(configuration_id: str) -> dict | None:
    with database.connect() as db:
        row = db.execute("SELECT * FROM ui_configurations WHERE config_id=?", (configuration_id,)).fetchone()
    return _row_to_dict(row) if row else None


def create_configuration(payload: dict) -> dict:
    payload = dict(payload or {})
    name = str(payload.get("name") or "Configuration").strip() or "Configuration"
    values = payload.get("values")
    if not isinstance(values, dict):
        values = {}
    new_id = str(payload.get("config_id") or payload.get("configuration_id") or uuid.uuid4().hex)
    is_default = bool(payload.get("is_default", False))
    now = _now()
    with database.connect() as db:
        if is_default:
            db.execute("UPDATE ui_configurations SET is_default=0")
        db.execute(
            "INSERT INTO ui_configurations(config_id,name,is_default,values_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (new_id, name, 1 if is_default else 0, json.dumps(values), now, now),
        )
    return {"config_id": new_id, "name": name, "is_default": is_default, "values": values, "created_at": now, "updated_at": now}


def update_configuration(configuration_id: str, changes: dict) -> dict:
    changes = dict(changes or {})
    with database.connect() as db:
        row = db.execute("SELECT * FROM ui_configurations WHERE config_id=?", (configuration_id,)).fetchone()
        if not row:
            raise KeyError("Configuration not found")
        item = _row_to_dict(row)
        if "name" in changes and changes["name"] is not None:
            item["name"] = str(changes["name"]).strip() or item["name"]
        if "values" in changes and isinstance(changes["values"], dict):
            merged = dict(item.get("values") or {})
            merged.update(changes["values"])
            item["values"] = merged
        if "is_default" in changes:
            item["is_default"] = bool(changes["is_default"])
            if item["is_default"]:
                db.execute("UPDATE ui_configurations SET is_default=0")
        now = _now()
        item["updated_at"] = now
        db.execute(
            "UPDATE ui_configurations SET name=?,is_default=?,values_json=?,updated_at=? WHERE config_id=?",
            (item["name"], 1 if item["is_default"] else 0, json.dumps(item["values"]), now, configuration_id),
        )
    return item


def delete_configuration(configuration_id: str) -> bool:
    with database.connect() as db:
        cur = db.execute("DELETE FROM ui_configurations WHERE config_id=?", (configuration_id,))
    return cur.rowcount > 0
