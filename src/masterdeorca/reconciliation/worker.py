"""Deterministic reconciliation of cached parser results into Question Bank.

The worker reads and updates database-backed parser artifacts, uses Python-only
validation/synchronization, skips active processing jobs, and prevents
overlapping runs with a database lease.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings as _settings
from config.outputs import pdf_stem
from src.masterdeorca import database, processing_manifest, storage
from src.masterdeorca.reconciliation import validation
from src.masterdeorca.services import question_bank

logger = logging.getLogger("masterdeorca.reconciliation")

VALIDATOR_ID = "reconciliation-validator/2"
RETRY_BOUNDS = {"parse": 3, "sync": 5}
BACKOFF_START_SECONDS = 120
BACKOFF_MAX_SECONDS = 900
DEFAULT_INTERVAL_HOURS = 12
QUESTION_BANK_CLEANUP_INTERVAL_SECONDS = 5 * 60
LEASE_SECONDS = 600
LEASE_HEARTBEAT_SECONDS = 120
SCHEDULER_STATE_KEY = "reconciliation"

run_lock = threading.Lock()
_run_lock = run_lock
_cleanup_lock = threading.Lock()
_scheduler: "_Scheduler | None" = None
_last_run: dict = {}


def _interval_seconds() -> int:
    raw = getattr(_settings, "RECONCILE_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_HOURS
    if value not in {12, 24}:
        raise RuntimeError("QUES_RECONCILE_INTERVAL_HOURS must be 12 or 24.")
    return value * 60 * 60


def _enabled() -> bool:
    return bool(getattr(_settings, "RECONCILE_ENABLED", True))


def _read_scheduler_state() -> dict:
    now = _now()
    with database.connect() as db:
        row = db.execute(
            "SELECT value_json FROM scheduler_state WHERE state_key=?",
            (SCHEDULER_STATE_KEY,),
        ).fetchone()
        if row:
            try:
                state = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Persisted reconciliation scheduler state is invalid.") from exc
            if not isinstance(state, dict):
                raise RuntimeError("Persisted reconciliation scheduler state must be an object.")
            if state.get("interval_hours") not in {12, 24}:
                raise RuntimeError("Persisted reconciliation interval must be 12 or 24 hours.")
            return state
        state = {
            "enabled": _enabled(),
            "interval_hours": _interval_seconds() // 3600,
            "last_started_at": None,
            "last_finished_at": None,
            "duration_seconds": None,
            "counts": {"imported": 0, "changed": 0, "skipped": 0, "failed": 0},
            "last_error": None,
            "next_execution_at": (
                datetime.now(timezone.utc)
                + timedelta(seconds=_interval_seconds())
            ).isoformat(),
            "last_run": {},
        }
        db.execute(
            "INSERT INTO scheduler_state(state_key,value_json,updated_at) VALUES(?,?,?)",
            (SCHEDULER_STATE_KEY, json.dumps(state), now),
        )
        return state


def _write_scheduler_state(state: dict) -> None:
    with database.connect() as db:
        db.execute(
            "UPDATE scheduler_state SET value_json=?,updated_at=? WHERE state_key=?",
            (json.dumps(state, ensure_ascii=False), _now(), SCHEDULER_STATE_KEY),
        )


def _acquire_database_lease(owner: str) -> bool:
    now = datetime.now(timezone.utc)
    lease_until = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
    def _acquire(db):
        result = db.execute(
            "UPDATE scheduler_state SET lease_owner=?,lease_until=?,updated_at=? "
            "WHERE state_key=? AND (lease_owner IS NULL OR lease_owner=? OR lease_until IS NULL OR lease_until<=?)",
            (owner, lease_until, now.isoformat(), SCHEDULER_STATE_KEY, owner, now.isoformat()),
        )
        return bool(result.rowcount)
    return database.run_transaction(_acquire, operation_name="reconciliation_acquire_lease")


def _renew_database_lease(owner: str) -> None:
    now = datetime.now(timezone.utc)
    def _renew(db):
        db.execute(
            "UPDATE scheduler_state SET lease_until=?,updated_at=? "
            "WHERE state_key=? AND lease_owner=?",
            (
                (now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                now.isoformat(),
                SCHEDULER_STATE_KEY,
                owner,
            ),
        )
    database.run_transaction(_renew, operation_name="reconciliation_renew_lease")


def _release_database_lease(owner: str) -> None:
    def _release(db):
        db.execute(
            "UPDATE scheduler_state SET lease_owner=NULL,lease_until=NULL,updated_at=? "
            "WHERE state_key=? AND lease_owner=?",
            (_now(), SCHEDULER_STATE_KEY, owner),
        )
    database.run_transaction(_release, operation_name="reconciliation_release_lease")


def _lite_mode() -> bool:
    return getattr(_settings, "PARSER_MODE", "local").strip().casefold() == "llm"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _zero_totals() -> dict:
    return {"valid": 0, "parsed_recovered": 0, "imported": 0, "needs_review": 0, "invalid": 0,
            "duplicates": 0, "parse_failures": 0, "parse_skipped": 0, "sync_failures": 0}


# ---------------------------------------------------------------------------
# Retry state (SQLite-backed)
# ---------------------------------------------------------------------------


def _state_row(subject: str, filename: str, stage: str) -> dict | None:
    try:
        with database.connect() as db:
            row = db.execute(
                "SELECT subject,filename,stage,status,retry_count,last_error,last_attempt_at,"
                "next_retry_at,updated_at FROM reconciliation_state WHERE subject=? AND filename=? AND stage=?",
                (subject, filename, stage)).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def _state_upsert(subject: str, filename: str, stage: str, **fields) -> None:
    status = fields.get("status", "pending")
    now = _now()
    assignments = ["status=excluded.status", "updated_at=excluded.updated_at"]
    for column in ("retry_count", "last_error", "last_attempt_at", "next_retry_at"):
        if column in fields and fields[column] is not None:
            assignments.append(f"{column}=excluded.{column}")
    retry_count = fields.get("retry_count")
    with database.connect() as db:
        db.execute(
            "INSERT INTO reconciliation_state(subject,filename,stage,status,retry_count,"
            "last_error,last_attempt_at,next_retry_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(subject,filename,stage) DO UPDATE SET " + ", ".join(assignments),
            (subject, filename, stage, status, retry_count if retry_count is not None else 0,
             fields.get("last_error"), fields.get("last_attempt_at", now),
             fields.get("next_retry_at"), now))


def _backoff_seconds(retry_count: int) -> float:
    return min(BACKOFF_START_SECONDS * (2 ** max(0, retry_count - 1)), BACKOFF_MAX_SECONDS)


def _retry_allowed(row: dict | None, force: bool) -> bool:
    if not row:
        return True
    status = row.get("status")
    if status == "failed" and not force:
        return False
    if status != "retrying" or force:
        return True
    next_at = row.get("next_retry_at")
    if not next_at:
        return True
    try:
        due = datetime.fromisoformat(str(next_at))
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= due


def _record_failure(subject: str, filename: str, stage: str, exc: Exception,
                    totals: dict, counter: str) -> None:
    row = _state_row(subject, filename, stage) or {}
    retry_count = (row.get("retry_count") or 0) + 1
    bound = RETRY_BOUNDS.get(stage, 3)
    if retry_count > bound:
        status, next_retry_at = "failed", None
        logger.error("Reconciliation %s stage exhausted for %s/%s after %d attempts: %s",
                     stage, subject, filename, retry_count, exc)
    else:
        status = "retrying"
        next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(retry_count))).isoformat()
        logger.warning("Reconciliation %s stage failed for %s/%s (attempt %d/%d): %s",
                       stage, subject, filename, retry_count, bound, exc)
    _state_upsert(subject, filename, stage, status=status, retry_count=retry_count,
                  last_error=f"{type(exc).__name__}: {exc}", next_retry_at=next_retry_at,
                  last_attempt_at=_now())
    totals[counter] += 1


# ---------------------------------------------------------------------------
# Parser recovery
# ---------------------------------------------------------------------------


def _run_parser(manifest_path, parser_root) -> None:
    """Run the existing Parser over a scraped manifest into a staged root."""
    manifest_path = Path(manifest_path)
    parser_root = Path(parser_root)
    if _lite_mode():
        _run_lite_parser(manifest_path, parser_root)
        return
    from config import parser as parser_config
    from functions.QuesParser.ParsePipeLIne.question_bank import run as parse_questions

    previous = (parser_config.OUTPUT_DIR, parser_config.QUESTIONS_OUTPUT_DIR, parser_config.QUESTIONS_OUTPUT_FILE)
    try:
        parser_config.OUTPUT_DIR = str(parser_root)
        parser_config.QUESTIONS_OUTPUT_DIR = str(parser_root)
        parser_config.QUESTIONS_OUTPUT_FILE = ""
        parse_questions(manifest_paths=[str(manifest_path)])
    finally:
        (parser_config.OUTPUT_DIR, parser_config.QUESTIONS_OUTPUT_DIR,
         parser_config.QUESTIONS_OUTPUT_FILE) = previous


def _run_lite_parser(manifest_path: Path, parser_root: Path) -> None:
    """Lite mode recovers from the source document directly (no Scrapper stage)."""
    from src.masterdeorca.document_intelligence.lite.two_stage import LiteTwoStageProcessor

    stem = manifest_path.parent.name
    source = None
    try:
        source_dir = manifest_path.parents[3] / "source"
    except IndexError:
        source_dir = None
    if source_dir is not None and source_dir.is_dir():
        for candidate in sorted(source_dir.iterdir(), key=lambda item: item.name.casefold()):
            if candidate.is_file() and pdf_stem(candidate.name) == stem:
                source = candidate
                break
    if source is None:
        raise FileNotFoundError(f"No source document found for recovered stem {stem}")
    processor = LiteTwoStageProcessor()
    processor.process(source, output_dir=parser_root / stem)


def _read_parsed(path: Path) -> dict | list | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return data if isinstance(data.get("questions"), list) else None
    return data if isinstance(data, list) else None


def _read_parsed_bytes(content: bytes) -> dict | list | None:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return data if isinstance(data.get("questions"), list) else None
    return data if isinstance(data, list) else None


def _cleanup_partials(parser_dir: Path) -> None:
    if not parser_dir.is_dir():
        return
    for item in parser_dir.iterdir():
        if item.is_file() and item.suffix.lower() in {".tmp", ".partial", ".part"}:
            try:
                item.unlink()
            except OSError:
                pass


def _write_parsed(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


# ---------------------------------------------------------------------------
# Validation and duplicate marking
# ---------------------------------------------------------------------------


def _same_record(item: dict, bank_row: dict, filename: str) -> bool:
    """Idempotence: the signature match is the record's own imported row."""
    item_id = item.get("question_id")
    if item_id is not None and str(item_id) == str(bank_row.get("question_id")):
        return True
    return (bank_row.get("source_pdf") == filename
            and validation.record_text(bank_row) == validation.record_text(item))


def _similar(text_a: str, text_b: str) -> bool:
    if not text_a or not text_b:
        return False
    return difflib.SequenceMatcher(None, text_a.casefold(), text_b.casefold()).ratio() >= 0.92


def _duplicate_owner(signature: str, text: str, item: dict, bank_by_signature: dict,
                     pass_seen: dict, filename: str) -> object:
    bank_row = bank_by_signature.get(signature) if signature else None
    if bank_row is not None:
        if _same_record(item, bank_row, filename):
            return None  # Own already-imported record: idempotence, not duplication.
        return bank_row.get("question_id") or bank_row.get("original_question_number") or True
    owner = pass_seen.get(signature) if signature else None
    if owner is not None:
        return owner
    for row_signature, row in bank_by_signature.items():
        if row_signature != signature and _similar(text, validation.record_text(row)):
            return row.get("question_id") or row.get("original_question_number") or True
    for seen_text, seen_owner in pass_seen.values():
        if _similar(text, seen_text):
            return seen_owner
    return None


def _validate_and_annotate(subject: str, filename: str, parsed_data, totals: dict,
                           bank_by_signature: dict) -> bool:
    """Assess deterministically, mark duplicates and persist annotations."""
    questions = parsed_data.get("questions") if isinstance(parsed_data, dict) else parsed_data
    if not isinstance(questions, list):
        return False
    now = _now()
    pass_seen: dict[str, tuple[str, object]] = {}
    resolved: list[tuple[dict, dict]] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        text = validation.record_text(item)
        signature = validation.text_signature(text)
        existing = item.get("validation")
        if (isinstance(existing, dict) and existing.get("validator") == VALIDATOR_ID
                and existing.get("signature") == signature
                and existing.get("outcome") in validation.OUTCOMES):
            verdict = dict(existing)
        else:
            verdict = dict(validation.assess_question(item))
        resolved.append((item, verdict))
    for item, verdict in resolved:
        outcome = verdict.get("outcome")
        if outcome == "valid":
            text = validation.record_text(item)
            signature = validation.text_signature(text)
            owner = _duplicate_owner(signature, text, item, bank_by_signature, pass_seen, filename)
            if owner is not None:
                verdict = {"outcome": "duplicate", "reason": "duplicate_of_existing_record",
                           "duplicate_of": owner}
            else:
                pass_seen[signature] = (text, item.get("original_question_number")
                                        or item.get("label") or item.get("question_id") or True)
        annotation = {
            "validator": VALIDATOR_ID,
            "signature": validation.text_signature(validation.record_text(item)),
            "outcome": verdict.get("outcome"),
            "reason": verdict.get("reason") or "",
            "checked_at": now,
        }
        if verdict.get("duplicate_of") is not None:
            annotation["duplicate_of"] = verdict.get("duplicate_of")
        if item.get("validation") != annotation:
            item["validation"] = annotation
        counter = "duplicates" if annotation["outcome"] == "duplicate" else annotation["outcome"]
        totals[counter] = totals.get(counter, 0) + 1
    if isinstance(parsed_data, dict):
        metadata = parsed_data.get("metadata") if isinstance(parsed_data.get("metadata"), dict) else {}
        metadata["validation"] = {"validator": VALIDATOR_ID, "checked_at": now,
                                  "outcomes": {key: value for key, value in totals.items()
                                               if key in validation.OUTCOMES}}
        parsed_data["metadata"] = metadata
    return True


# ---------------------------------------------------------------------------
# Per-document reconciliation
# ---------------------------------------------------------------------------


def _active_job(subject: str, filename: str) -> bool:
    job = database.latest_job(subject, filename)
    return bool(job) and job.get("status") in {"queued", "running"}


def _reconcile_document(subject: str, filename: str, paths: dict, force: bool,
                        lite: bool, totals: dict, bank_by_signature: dict) -> None:
    if _active_job(subject, filename):
        logger.info("Reconciliation skips %s/%s: a processing job owns it", subject, filename)
        return
    document = database.get_document(subject, filename)
    if not document:
        totals["parse_skipped"] += 1
        return
    artifact = database.get_artifact(document["document_id"], "questions_parsed.json")
    parsed_data = _read_parsed_bytes(artifact["data"]) if artifact else None
    if parsed_data is None:
        totals["parse_skipped"] += 1
        return
    else:
        state = _state_row(subject, filename, "parse")
        if state is None or state.get("status") != "complete":
            _state_upsert(subject, filename, "parse", status="complete", retry_count=0,
                          last_error=None, next_retry_at=None, last_attempt_at=_now())
    changed = _validate_and_annotate(subject, filename, parsed_data, totals, bank_by_signature)
    if changed:
        database.put_artifact(
            subject_id=document["subject_id"],
            document_id=document["document_id"],
            artifact_key="questions_parsed.json",
            artifact_type="parser_json",
            content_type="application/json",
            data=json.dumps(parsed_data, ensure_ascii=False).encode("utf-8"),
            metadata=artifact.get("metadata") or {},
        )
    sync_state = _state_row(subject, filename, "sync")
    if not _retry_allowed(sync_state, force):
        return
    try:
        imported = question_bank.sync_from_parsed(subject, json.dumps(parsed_data, ensure_ascii=False).encode("utf-8"), source_pdf=filename,
                                                  protect_edits=True)
        totals["imported"] += imported
        _state_upsert(subject, filename, "sync", status="complete", retry_count=0,
                      last_error=None, next_retry_at=None, last_attempt_at=_now())
        processing_manifest.update_pdf(subject, filename, status="succeeded")
        logger.info("Reconciliation synced %s/%s (imported=%d)", subject, filename, imported)
    except Exception as exc:
        _record_failure(subject, filename, "sync", exc, totals, "sync_failures")


def _reconcile_pass(force: bool) -> dict:
    totals = _zero_totals()
    per_subject: dict[str, dict] = {}
    subjects = storage.list_subject_names()
    for subject in subjects:
        subject_totals = _zero_totals()
        rows = question_bank.read_subject(subject)
        bank_by_signature = {validation.text_signature(validation.record_text(row)): row
                             for row in rows if validation.text_signature(validation.record_text(row))}
        with database.connect() as db:
            sources = db.execute(
                """SELECT d.filename FROM documents d JOIN subjects s ON s.subject_id=d.subject_id
                   WHERE s.subject_name=? ORDER BY d.filename COLLATE NOCASE""",
                (subject,),
            ).fetchall()
        for source in sources:
            if Path(source["filename"]).suffix.lower() not in storage.DOCUMENT_EXTENSIONS:
                continue
            _reconcile_document(subject, source["filename"], {}, force, False, subject_totals,
                                bank_by_signature)
            rows = question_bank.read_subject(subject)
            bank_by_signature = {validation.text_signature(validation.record_text(row)): row
                                 for row in rows if validation.text_signature(validation.record_text(row))}
        per_subject[subject] = subject_totals
        for key, value in subject_totals.items():
            totals[key] += value
    global _last_run
    _last_run = {"subjects": subjects, "per_subject": per_subject, "totals": totals,
                 "finished_at": _now()}
    return {"subjects": subjects, "per_subject": per_subject, "totals": totals}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def reconcile_all(force: bool = False) -> dict:
    """One synchronous recovery pass over every registered subject."""
    if not run_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "another reconciliation run is active in this process"}
    try:
        state = _read_scheduler_state()
    except Exception:
        run_lock.release()
        raise
    if not force and not state.get("enabled", True):
        run_lock.release()
        return {"skipped": True, "reason": "automatic reconciliation is disabled"}
    owner = uuid.uuid4().hex
    acquired_database_lease = False
    lease_stop = threading.Event()
    lease_thread: threading.Thread | None = None
    try:
        acquired_database_lease = _acquire_database_lease(owner)
        if not acquired_database_lease:
            return {"skipped": True, "reason": "another reconciliation run is active in another instance"}
        interval = int(state.get("interval_hours", DEFAULT_INTERVAL_HOURS))
        started = datetime.now(timezone.utc)
        started_at = started.isoformat()
        state.update(last_started_at=started_at, last_error=None)
        _write_scheduler_state(state)

        def renew_lease() -> None:
            while not lease_stop.wait(LEASE_HEARTBEAT_SECONDS):
                try:
                    _renew_database_lease(owner)
                except Exception:
                    logger.exception("Reconciliation database lease renewal failed")

        lease_thread = threading.Thread(
            target=renew_lease,
            name="reconciliation-lease-renewal",
            daemon=True,
        )
        lease_thread.start()
        try:
            summary = _reconcile_pass(force)
            finished = datetime.now(timezone.utc)
            totals = summary.get("totals") or {}
            state.update(
                last_finished_at=finished.isoformat(),
                duration_seconds=round((finished - started).total_seconds(), 3),
                counts={
                    "imported": int(totals.get("imported", 0)),
                    "changed": int(totals.get("parsed_recovered", 0)),
                    "skipped": int(totals.get("parse_skipped", 0)),
                    "failed": int(totals.get("parse_failures", 0))
                    + int(totals.get("sync_failures", 0)),
                },
                last_error=None,
                last_run=summary,
                next_execution_at=(
                    finished + timedelta(hours=interval)
                ).isoformat() if state.get("enabled", True) else None,
            )
            _write_scheduler_state(state)
            return summary
        except Exception as exc:
            finished = datetime.now(timezone.utc)
            state.update(
                last_finished_at=finished.isoformat(),
                duration_seconds=round((finished - started).total_seconds(), 3),
                last_error=f"{type(exc).__name__}: {exc}",
                next_execution_at=(
                    finished + timedelta(hours=interval)
                ).isoformat() if state.get("enabled", True) else None,
            )
            _write_scheduler_state(state)
            raise
        finally:
            lease_stop.set()
            if lease_thread is not None:
                lease_thread.join(timeout=2)
    finally:
        if acquired_database_lease:
            try:
                _release_database_lease(owner)
            except Exception:
                # The lease has a finite expiry; never mask a reconciliation error
                # with a failed best-effort release on a broken stream.
                logger.exception("Could not release reconciliation database lease owner=%s", owner)
        run_lock.release()


def cleanup_question_bank_safety() -> dict:
    """Clean unsafe Question Bank rows under the shared cross-instance lease."""
    if not _cleanup_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "another safety cleanup is active in this process"}
    owner = uuid.uuid4().hex
    acquired_database_lease = False
    lease_stop = threading.Event()
    lease_thread: threading.Thread | None = None
    try:
        _read_scheduler_state()
        acquired_database_lease = _acquire_database_lease(owner)
        if not acquired_database_lease:
            return {"skipped": True, "reason": "another maintenance run is active in another instance"}

        def renew_lease() -> None:
            while not lease_stop.wait(LEASE_HEARTBEAT_SECONDS):
                try:
                    _renew_database_lease(owner)
                except Exception:
                    logger.exception("Question Bank cleanup lease renewal failed")

        lease_thread = threading.Thread(
            target=renew_lease,
            name="question-bank-cleanup-lease-renewal",
            daemon=True,
        )
        lease_thread.start()
        counts = question_bank.cleanup_safety_records()
        logger.info(
            "Question Bank safety cleanup removed %d duplicate rows and %d rows without valid marks (counts may overlap)",
            counts["duplicates_removed"], counts["incomplete_removed"],
        )
        return counts
    finally:
        lease_stop.set()
        if lease_thread is not None:
            lease_thread.join(timeout=2)
        if acquired_database_lease:
            try:
                _release_database_lease(owner)
            except Exception:
                logger.exception("Could not release Question Bank cleanup database lease owner=%s", owner)
        _cleanup_lock.release()


def status() -> dict:
    """Persistent worker settings, last-run summary and pending retry states."""
    pending: list[dict] = []
    try:
        with database.connect() as db:
            rows = db.execute(
                "SELECT subject,filename,stage,status,retry_count,last_error,last_attempt_at,"
                "next_retry_at,updated_at FROM reconciliation_state WHERE status<>? "
                "ORDER BY updated_at", ("complete",)).fetchall()
        pending = [dict(row) for row in rows]
    except Exception:
        logger.exception("Unable to read reconciliation retry state")
        raise
    state = _read_scheduler_state()
    interval_hours = int(state["interval_hours"])
    return {
        "enabled": bool(state.get("enabled", True)),
        "interval_hours": interval_hours,
        "interval_seconds": interval_hours * 3600,
        "last_started_at": state.get("last_started_at"),
        "last_execution": state.get("last_finished_at"),
        "last_finished_at": state.get("last_finished_at"),
        "next_execution_at": state.get("next_execution_at"),
        "duration_seconds": state.get("duration_seconds"),
        "counts": state.get("counts") or {},
        "last_error": state.get("last_error"),
        "last_run": state.get("last_run") or dict(_last_run),
        "pending": pending,
    }


class _Scheduler:
    """Single-flight scheduler for reconciliation and Question Bank safety."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="reconciliation-scheduler", daemon=True)

    def _loop(self) -> None:
        next_cleanup = time.monotonic() + QUESTION_BANK_CLEANUP_INTERVAL_SECONDS
        next_reconciliation = time.monotonic() + _interval_seconds()
        while not self._stop.is_set():
            now = time.monotonic()
            wait_for = max(0.0, min(next_cleanup, next_reconciliation) - now)
            if self._stop.wait(wait_for):
                break
            now = time.monotonic()
            if now >= next_cleanup:
                try:
                    cleanup_question_bank_safety()
                except Exception:
                    logger.exception("Scheduled Question Bank safety cleanup failed")
                next_cleanup = time.monotonic() + QUESTION_BANK_CLEANUP_INTERVAL_SECONDS
            if now >= next_reconciliation:
                try:
                    if _read_scheduler_state().get("enabled", True):
                        reconcile_all()
                except Exception:
                    logger.exception("Scheduled reconciliation pass failed")
                next_reconciliation = time.monotonic() + _interval_seconds()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)


def start_scheduler():
    """Host the worker in the FastAPI process; returns a handle with stop()."""
    global _scheduler
    if _scheduler is not None and _scheduler._thread.is_alive():
        return _scheduler
    _scheduler = _Scheduler()
    _scheduler._thread.start()
    return _scheduler
