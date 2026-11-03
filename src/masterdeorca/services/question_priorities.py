"""Persistent priorities for parsed questions - stored in SQLite as source of truth."""

from __future__ import annotations

from datetime import datetime, timezone

from src.masterdeorca import database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_priorities(question_ids: set[str] | None = None) -> dict[str, int]:
    with database.connect() as db:
        rows = db.execute(
            "SELECT question_id,priority FROM question_priorities ORDER BY question_id"
        ).fetchall()
    priorities = {row["question_id"]: row["priority"] for row in rows}
    if question_ids is not None:
        return {question_id: priorities.get(question_id, 1) for question_id in sorted(question_ids)}
    return priorities


def set_priority(question_id: str, priority: int) -> None:
    now = _now()
    with database.connect() as db:
        db.execute(
            "INSERT INTO question_priorities(question_id,priority,updated_at) "
            "VALUES(?,?,?) ON CONFLICT(question_id) DO UPDATE SET "
            "priority=excluded.priority,updated_at=excluded.updated_at",
            (question_id, priority, now),
        )


def delete_priority(question_id: str) -> bool:
    with database.connect() as db:
        cur = db.execute(
            "DELETE FROM question_priorities WHERE question_id=?",
            (question_id,),
        )
    return cur.rowcount > 0
