"""Admin dashboard statistics assembled from the existing backend services.

Question totals scan every registered subject's canonical question-bank file,
so the question summary is short-TTL cached: dashboards can poll this endpoint
without scanning every bank on each request. The cache is invalidated whenever
subjects or question banks change (subject create/delete/rename, question
create/update, PDF bank sync) and is refreshed automatically when the TTL
expires or when the registered subject list changes. Teacher and subject
counts are recomputed on each request; both are cheap indexed operations.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from src.masterdeorca import database
from src.masterdeorca.services import question_bank
from src.masterdeorca.storage import list_subject_names

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 10.0
_guard = threading.Lock()
_cache_entry: tuple[float, list[str], dict] | None = None


def invalidate() -> None:
    """Drop cached metrics so the next request recomputes from current data."""
    global _cache_entry
    with _guard:
        _cache_entry = None


def count_teachers() -> int | None:
    """Registered teacher accounts only; administrator accounts are excluded.

    Mirrors the existing `GET /teachers` business definition (role='teacher');
    emails are unique in the users table, so duplicate accounts cannot be
    counted. Returns None only when the database is unreachable.
    """
    try:
        with database.connect() as db:
            return db.execute("SELECT count(*) FROM users WHERE role='teacher'").fetchone()[0]
    except sqlite3.Error:
        logger.exception("Dashboard teacher count is unavailable")
        return None


def count_subjects() -> int:
    return len(list_subject_names())


def _recompute_questions(subjects: list[str]) -> dict:
    summary = question_bank.count_questions(subjects)
    if summary["unavailable"]:
        status = "partial" if summary["total"] > 0 else "unavailable"
    else:
        status = "ok"
    return {"total_questions": summary["total"] if status != "unavailable" else None,
            "questions_status": status,
            "unavailable_subjects": summary["unavailable"]}


def get_metrics() -> dict:
    """Live dashboard totals with explicit partial-availability reporting."""
    global _cache_entry
    subjects = list_subject_names()
    now = time.monotonic()
    with _guard:
        entry = _cache_entry
    if entry and entry[0] > now and entry[1] == subjects:
        questions = entry[2]
        served_from_cache = True
    else:
        questions = _recompute_questions(subjects)
        served_from_cache = False
        with _guard:
            _cache_entry = (time.monotonic() + CACHE_TTL_SECONDS, subjects, questions)
    teachers = count_teachers()
    return {
        "total_teachers": teachers,
        "total_subjects": len(subjects),
        "total_questions": questions["total_questions"],
        "availability": {
            "total_teachers": "unavailable" if teachers is None else "ok",
            "total_subjects": "ok",
            "total_questions": questions["questions_status"],
        },
        "unavailable_subjects": questions["unavailable_subjects"],
        "cached": served_from_cache,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
    }
