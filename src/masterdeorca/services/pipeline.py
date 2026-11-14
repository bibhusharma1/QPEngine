"""Fresh Scrapper -> Parser orchestration for one uploaded PDF."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import time
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import parser as parser_config
from config import settings as _settings
from config.outputs import pdf_stem, stage_output_dir
from functions.QuesParser.ParsePipeLIne.question_bank import run as parse_questions
from functions.QuesScrapper.ScrapePipeLIne.processPDF import process_pdf
from functions.QuesScrapper.ScrapePipeLIne.quesandFig import append_records
from functions.ReconstructQues.generator import DocumentGenerator
from src.masterdeorca import database
from src.masterdeorca import processing_manifest
from src.masterdeorca.services.question_bank import sync_pdf as sync_question_bank
from src.masterdeorca.services.document_processor import process_document
from src.masterdeorca.storage import ensure_subject, pdf_path


logger = logging.getLogger(__name__)
_pipeline_lock = threading.Lock()
# Reconciliation worker reuses the same lock so stray parse stages cannot race
# an active processing job.
pipeline_lock = _pipeline_lock


def _update(job: dict, *, status: str | None = None, stage: str | None = None,
            error: str | None = None, result: dict | None = None) -> None:
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    if status is not None:
        job["status"] = status
    if stage is not None:
        job["stage"] = stage
    job["error"] = error
    if result is not None:
        job["result"] = result
    database.save_job(job)
    processing_manifest.update_pdf(job["subject"], job["filename"], status=job.get("status"), job=job,
                                   result=job.get("result"), error=job.get("error"))


def _log(job: dict, message: str) -> None:
    def _insert(db) -> None:
        db.execute(
            "INSERT INTO processing_logs(job_id,level,message,created_at) VALUES(?,?,?,?)",
            (job["job_id"], "INFO", message, datetime.now(timezone.utc).isoformat()),
        )
    database.run_transaction(_insert, operation_name=f"processing_log:{job['job_id']}")


def _rewrite_paths(root: Path, before: str, after: str) -> None:
    for item in root.rglob("*"):
        if item.is_file() and item.suffix.lower() in {".json", ".txt"}:
            try:
                text = item.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            changed = text.replace(before, after)
            if changed != text:
                item.write_text(changed, encoding="utf-8")


def _replace_stage(staged: Path, final: Path, backup: Path) -> None:
    if final.exists():
        final.parent.mkdir(parents=True, exist_ok=True)
        final.replace(backup)
    try:
        staged.replace(final)
    except Exception:
        if backup.exists() and not final.exists():
            backup.replace(final)
        raise


def process_pdf_job(job: dict) -> None:
    """Run fresh extraction and parsing in isolated work folders, then publish."""
    mode = getattr(_settings, "PARSER_MODE", "local").lower()
    if mode == "llm":
        _run_lite_job(job)
        return
    raise RuntimeError("The legacy Local parser is retained but disabled; set PARSER_MODE=llm.")


def reconstruct_pdf(subject: str, filename: str) -> dict:
    document = database.get_document(subject, filename)
    if not document:
        raise FileNotFoundError("Document not found in this subject.")
    parsed_artifact = database.get_artifact(document["document_id"], "questions_parsed.json")
    if parsed_artifact is None:
        raise FileNotFoundError("No parsed question bank exists. Process the PDF first.")
    with tempfile.TemporaryDirectory(prefix="masterdeorca-reconstruct-") as temporary:
        root = Path(temporary)
        bank_path = root / "questions_parsed.json"
        bank_path.write_bytes(parsed_artifact["data"])
        with database.connect() as db:
            figure_rows = db.execute(
                "SELECT artifact_key,data FROM stored_artifacts WHERE document_id=? AND artifact_type='figure'",
                (document["document_id"],),
            ).fetchall()
        figure_root = root / "figures"
        for row in figure_rows:
            asset_path = figure_root / Path(row["artifact_key"]).name
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(bytes(row["data"]))
        bank = json.loads(bank_path.read_text(encoding="utf-8"))

        def rebase_assets(items: list) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                assets = item.get("assets") or []
                for asset in assets:
                    if not isinstance(asset, dict):
                        continue
                    old_path = asset.get("path") or asset.get("figure_path") or asset.get("image_path")
                    if old_path:
                        candidate = figure_root / Path(str(old_path)).name
                        if candidate.is_file():
                            asset["path"] = str(candidate)
                children = item.get("sub_questions") or item.get("subquestions") or []
                if isinstance(children, list):
                    rebase_assets(children)

        rebase_assets(bank.get("questions", []))
        bank_path.write_text(json.dumps(bank, ensure_ascii=False), encoding="utf-8")
        generated = DocumentGenerator(json_path=bank_path, output_dir=root / "reconstruct").generate()
        data = generated.docx_path.read_bytes()
    database.put_artifact(
        subject_id=document["subject_id"],
        document_id=document["document_id"],
        artifact_key="questions_reconstructed.docx",
        artifact_type="generated_docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data=data,
        metadata={"question_count": generated.total_questions},
    )
    return {"docx_path": "questions_reconstructed.docx", "question_count": generated.total_questions,
            "subquestion_count": generated.total_subquestions, "figures_inserted": generated.total_assets_inserted,
            "warnings": generated.warnings, "missing_assets": generated.missing_assets}


def process_document_job(job: dict) -> None:
    return process_pdf_job(job)


def _run_lite_job(job: dict) -> None:
    mode = "llm"
    logger.info(
        "mode=%s -> extraction=nvidia_nemotron_parse -> "
        "structuring=nvidia_nemotron_super_vl -> question_bank_saved",
        mode,
    )
    stem = pdf_stem(job["filename"])
    try:
        document = database.get_document(job["subject"], job["filename"])
        if not document or document.get("content_blob") is None:
            raise RuntimeError("The uploaded document is not stored in SQLite; run the binary import migration.")
        with tempfile.TemporaryDirectory(prefix=f"masterdeorca-{job['job_id']}-") as temporary:
            workspace = Path(temporary)
            source = workspace / job["filename"]
            source.write_bytes(bytes(document["content_blob"]))
            parser_root = workspace / "parser" / stem
            parser_root.mkdir(parents=True, exist_ok=True)
            with database.connect() as db:
                cached = db.execute(
                    "SELECT artifact_key,data FROM stored_artifacts WHERE document_id=?",
                    (document["document_id"],),
                ).fetchall()
            cached_pages: dict[int, dict] = {}
            for artifact in cached:
                target = parser_root / artifact["artifact_key"]
                if target.resolve().is_relative_to(parser_root.resolve()):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bytes(artifact["data"]))
                if artifact["artifact_key"].startswith("lite_pages/page-") and artifact["artifact_key"].endswith(".json"):
                    try:
                        page_number = int(Path(artifact["artifact_key"]).stem.split("-")[-1])
                        page_payload = json.loads(bytes(artifact["data"]).decode("utf-8"))
                        if isinstance(page_payload, dict) and str(
                            page_payload.get("content") or page_payload.get("text") or ""
                        ).strip():
                            cached_pages[page_number] = page_payload
                    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                        logger.warning("Ignoring invalid cached Lite extraction artifact: %s", artifact["artifact_key"])
            try:
                _update(job, status="running", stage="lite_extraction")
                _log(job, "Lite mode: Nemotron extraction started")
                from src.masterdeorca.document_intelligence.lite.two_stage import LiteTwoStageProcessor
                proc = LiteTwoStageProcessor()

                def mark_structuring_started(page_count: int) -> None:
                    _update(job, stage="lite_structuring")
                    _log(job, f"Lite mode: {page_count} image pages extracted; NVIDIA structuring started")

                def update_question_progress(progress: dict) -> None:
                    _update(job, stage="lite_structuring", result={"progress": progress})

                def persist_extracted_page(page_number: int, raw: dict, extraction_seconds: float) -> None:
                    key = f"lite_pages/page-{page_number:05d}.json"
                    payload = json.dumps(raw, ensure_ascii=False).encode("utf-8")
                    local_page = parser_root / key
                    local_page.parent.mkdir(parents=True, exist_ok=True)
                    local_page.write_bytes(payload)
                    started = time.monotonic()
                    try:
                        database.put_artifact(
                            subject_id=document["subject_id"],
                            document_id=document["document_id"],
                            artifact_key=key,
                            artifact_type="lite_page_extraction",
                            content_type="application/json",
                            data=payload,
                            metadata={"page": page_number, "state": "persisted",
                                      "extraction_seconds": extraction_seconds},
                        )
                    except Exception:
                        _log(job, f"Lite page {page_number}: persistence failed after successful extraction; result remains in process memory")
                        raise
                    persistence_seconds = time.monotonic() - started
                    _log(job, f"Lite page {page_number}: state=persisted extraction_seconds={extraction_seconds:.3f} database_persistence_seconds={persistence_seconds:.3f}")

                extracted, bank_data = proc.process(
                    source,
                    output_dir=parser_root,
                    on_structuring=mark_structuring_started,
                    on_event=lambda message: _log(job, message),
                    on_page_extracted=persist_extracted_page,
                    cached_page_results=cached_pages,
                    on_question_progress=update_question_progress,
                    default_priority=int(job.get("default_priority") or 1),
                )
            finally:
                for output in parser_root.rglob("*"):
                    if not output.is_file():
                        continue
                    relative = output.relative_to(parser_root).as_posix()
                    if relative == "questions_parsed.json":
                        continue
                    mime = mimetypes.guess_type(output.name)[0] or "application/octet-stream"
                    if output.suffix.lower() == ".json":
                        artifact_type = "parser_json"
                    elif output.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                        artifact_type = "figure"
                    else:
                        artifact_type = "parser_artifact"
                    database.put_artifact(
                        subject_id=document["subject_id"],
                        document_id=document["document_id"],
                        artifact_key=relative,
                        artifact_type=artifact_type,
                        content_type=mime,
                        data=output.read_bytes(),
                        metadata={"original_key": relative},
                    )
            _log(job, "Lite mode: NVIDIA structuring completed; validating")
            from functions.QuesParser.ParsePipeLIne.schema import QuestionBank
            try:
                bank = QuestionBank.model_validate(bank_data)
            except Exception as exc:
                raise RuntimeError(f"Lite structured JSON failed schema validation: {exc}") from exc
            if not (parser_root / "questions_parsed.json").is_file():
                raise RuntimeError("Lite pipeline did not produce questions_parsed.json")
            database.put_artifact(
                subject_id=document["subject_id"],
                document_id=document["document_id"],
                artifact_key="questions_parsed.json",
                artifact_type="parser_json",
                content_type="application/json",
                data=(parser_root / "questions_parsed.json").read_bytes(),
                metadata={"validated": True, "schema": "QuestionBank"},
            )
            result = {
                "source_pdf": job["filename"],
                "parsed_json": "questions_parsed.json",
                "mode": "llm",
                "question_count": len(bank_data.get("questions", [])),
                "structured_question_count": sum(
                    question.status == "structured" for question in bank.questions
                ),
                "unstructured_question_count": sum(
                    question.status == "unstructured" for question in bank.questions
                ),
                "extraction_provider": "nvidia_nemotron",
                "structuring_provider": "nvidia_nemotron_super_vl",
                "progress": (job.get("result") or {}).get("progress"),
            }
            completion_status = (
                "completed_with_errors"
                if result["unstructured_question_count"]
                else "succeeded"
            )
            _update(job, status=completion_status, stage="question_bank_sync", result=result)
            _log(job, "Lite mode: publishing to question bank")
            imported = sync_question_bank(job["subject"], job["filename"],
                                          default_priority=int(job.get("default_priority") or 1))
            result["question_bank_items"] = imported
            _update(job, status=completion_status, stage="complete", result=result)
            _log(
                job,
                f"Lite mode: processing complete: {result['structured_question_count']} "
                f"structured, {result['unstructured_question_count']} unstructured questions",
            )
    except Exception as exc:
        safe_message = str(exc)
        for key, value in os.environ.items():
            if ("API_KEY" in key or "SECRET" in key or "PASSWORD" in key) and value:
                safe_message = safe_message.replace(value, "[REDACTED]")
        logger.error("Lite PDF job %s failed: %s: %s", job["job_id"], type(exc).__name__, safe_message)
        _update(job, status="failed", stage=job.get("stage", "failed"),
                error=f"{type(exc).__name__}: {safe_message}"[:500])
        _log(job, f"Lite processing failed: {type(exc).__name__}: {safe_message}")
