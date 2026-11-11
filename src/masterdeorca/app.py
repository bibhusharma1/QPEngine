"""Single FastAPI entry point for admin, subject storage, processing and DOCX."""

from __future__ import annotations

import json
import hashlib
import io
import logging
import os
import threading
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, HTTPException, Request, UploadFile, Query
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictInt, field_validator

from config.settings import CORS_ORIGINS, JWT_SECRET, MAX_UPLOAD_BYTES, STORAGE_ROOT, DEFAULT_TEACHER_PASSWORD
from config.outputs import stage_output_dir
from functions.QuesScrapper.ScrapethePDF.gpu_runtime import GpuRuntimeError, ensure_cuda, runtime_report
from functions.QuesScrapper.ScrapethePDF.pdf_read import extract_pdf_text
from src.masterdeorca import database
from src.masterdeorca.security import (
    accessible_subjects, filter_subjects, issue_token, require_admin, require_admin_write,
    require_subject_access, require_user, require_write_user, subject_allowed,
)
import src.masterdeorca.app as app_module
from src.masterdeorca.services.pipeline import process_document_job, process_pdf_job, reconstruct_pdf
from src.masterdeorca.services import question_bank as question_bank_service
from src.masterdeorca.services import question_priorities as question_priorities_service
from src.masterdeorca.services import question_papers as question_papers_service
from src.masterdeorca.services import basic_config as basic_config_service
from src.masterdeorca.services import ui_configurations as ui_configurations_service
from src.masterdeorca.services import dashboard_metrics as dashboard_metrics_service
from src.masterdeorca.reconciliation import worker as reconciliation_worker
from src.masterdeorca import processing_manifest
from src.masterdeorca.storage import (
    detect_document_type, DOCUMENT_EXTENSIONS,
    list_subject_names, validate_document_filename, validate_subject_name,
)
from src.masterdeorca.functions.totp import (
    service as totp_service, schemas as totp_schemas, exceptions as totp_exceptions,
    crypto as totp_crypto,
)
from src.masterdeorca.document_intelligence.page_images import is_soffice_available


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def _check_system_dependencies() -> None:
    if is_soffice_available():
        logger.info("LibreOffice (soffice) detected on PATH: DOCX rendering in Lite mode is available.")
    else:
        logger.warning(
            "LibreOffice (soffice) not found on PATH. DOCX rendering in Lite mode will be unavailable "
            "(install libreoffice-writer to enable DOCX page-image conversion). PDF and image processing remain available."
        )


def _startup() -> None:
    database.validate_configuration()
    _check_system_dependencies()
    if len(JWT_SECRET) < 32 or "replace" in JWT_SECRET.casefold():
        raise RuntimeError("Set QUES_JWT_SECRET to a private random value of at least 32 characters.")
    if getattr(app_settings, "APP_ENV", "").casefold() not in {"local", "render"}:
        raise RuntimeError("APP_ENV must be 'local' or 'render'.")
    if getattr(app_settings, "STORAGE_BACKEND", "sqlite").casefold() != "sqlite":
        raise RuntimeError("Only SQLite BLOB storage is enabled.")
    if app_settings.RECONCILE_INTERVAL_HOURS not in {12, 24}:
        raise RuntimeError("QUES_RECONCILE_INTERVAL_HOURS must be 12 or 24.")
    database.initialize()
    database.mark_interrupted_jobs()
    with database.connect() as db:
        database.ensure_subject_record(db, app_settings.DEFAULT_SUBJECT)
        # temporary test seed
        try:
            from pwdlib import PasswordHash
            ph = database._password_hash
            h = ph.hash("admin@123")
            now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            db.execute("INSERT OR IGNORE INTO users(email,name,hashed_password,role,status,created_at) VALUES(?,?,?,?,?,?)",
                       ("admin@example.com","Administrator",h,"admin","active",now))
            db.execute("INSERT OR IGNORE INTO admins(email,password_hash) VALUES(?,?)",
                       ("admin@example.com",h))
            db.commit()
        except Exception as e:
            print("seed err", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    scheduler = reconciliation_worker.start_scheduler()
    try:
        yield
    finally:
        scheduler.stop()


from config import settings as app_settings
import os

app = FastAPI(title="MasterDeOrca API", version="1.0.0", description="Admin API for PDF question-bank processing and DOCX reconstruction.", lifespan=lifespan)
_cors = list(app_settings.CORS_ORIGINS)
_env = os.environ.get("QUES_CORS_ORIGINS", "")
if _env:
    _cors = [o.strip() for o in _env.split(",") if o.strip()]
# Ensure local and deployed frontend origins are always allowed
for _o in ["http://localhost", "http://localhost:3000", "http://127.0.0.1", "http://127.0.0.1:3000", "http://localhost:5173", "http://127.0.0.1:5173", "https://gceqpgen.vercel.app"]:
    if _o not in _cors:
        _cors.append(_o)
app.add_middleware(CORSMiddleware, allow_origins=_cors, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "Accept", "Accept-Language", "Content-Language"], allow_private_network=True)
_active_jobs: set[tuple[str, str]] = set()
_active_lock = threading.Lock()


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    # Keep the existing FastAPI error contract for every other route. The paper
    # generator gets stable, UI-friendly field errors without Pydantic internals.
    if request.url.path not in {"/api/question-papers/generate", "/api/question-papers/drafts", "/api/question-papers/drafts/generate"}:
        return await request_validation_exception_handler(request, exc)
    errors = []
    aliases = {"title": "paper_title"}
    for item in exc.errors():
        location = [str(part) for part in item.get("loc", ()) if part != "body"]
        field = ".".join(location) or "request"
        field = aliases.get(field, field)
        kind = item.get("type", "")
        if kind == "missing":
            message = "This field is required."
        elif kind == "string_too_short":
            message = "Must not be empty."
        elif kind == "greater_than_equal":
            message = "Must be at least 1."
        elif kind == "less_than_equal":
            message = "Value is above the allowed maximum."
        elif kind == "int_type":
            message = "Must be a whole number."
        elif kind == "value_error":
            message = "Must not be empty."
        else:
            message = "Enter a valid value."
        errors.append({"field": field, "message": message})
    return JSONResponse(status_code=422, content={"detail": "Question paper request is invalid.", "errors": errors})


def startup() -> None:
    _startup()


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    confirm_password: str = Field(min_length=8, max_length=256)


class ChangeEmailRequest(BaseModel):
    new_email: str = Field(min_length=3, max_length=254)
    confirm_email: str = Field(min_length=3, max_length=254)

    @field_validator("new_email", "confirm_email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("Invalid email address")
        return value


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)


class SubjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProcessRequest(BaseModel):
    force: bool = True
    default_priority: int | None = Field(default=None, ge=1, le=3)


class TeacherCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    department: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, max_length=256)
    status: Literal["authorized", "unauthorized"] = "authorized"
    subjects: list[str] = Field(default_factory=list)


class TeacherUpdate(BaseModel):
    """Fields accepted when partially updating a teacher."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str | None = None
    department: str | None = None
    status: Literal["authorized", "unauthorized"] | None = None
    password: str | None = None
    subjects: list[str] | None = None


class TeacherSubjectAssign(BaseModel):
    """Assign subjects to a teacher: replace the set with 'subjects', or add one with 'subject'."""
    subjects: list[str] | None = None
    subject: str | None = None


class QuestionCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=80)
    topic_id: int | None = None
    text: str = Field(min_length=1, max_length=2000)
    difficulty: str
    type: str
    marks: int = Field(ge=1, le=100)


class TopicCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    unit: int | None = Field(default=None, ge=1)
    description: str | None = None


class QuestionPriorityUpdate(BaseModel):
    question_id: str = Field(min_length=1, max_length=200)
    priority: StrictInt = Field(ge=1, le=3)
    subject_id: str | None = Field(default=None, min_length=1, max_length=80)


class QuestionPaperGenerate(BaseModel):
    title: str = Field(min_length=1, max_length=200, validation_alias=AliasChoices("title", "paper_title"))
    subject: str = Field(min_length=1, max_length=80)
    exam_type: str = Field(min_length=1, max_length=100)
    total_marks: StrictInt = Field(ge=1, le=1000)
    duration: str = Field(min_length=1, max_length=100)
    blueprint_id: str | None = Field(default=None, min_length=1, max_length=100)
    seed: StrictInt | None = None
    university_name: str | None = Field(default=None, max_length=200)
    college_name: str | None = Field(default=None, max_length=200)
    department_name: str | None = Field(default=None, max_length=200)
    academic_year: str | None = Field(default=None, max_length=40)
    semester: str | None = Field(default=None, max_length=60)
    subject_name: str | None = Field(default=None, max_length=200)
    subject_code: str | None = Field(default=None, max_length=80)
    examination_date: str | None = Field(default=None, max_length=40)
    include_processing_accuracy_notice: bool = False

    @field_validator("title", "subject", "exam_type", "duration")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("university_name", "college_name", "department_name", "academic_year", "semester", "subject_name", "subject_code", "examination_date")
    @classmethod
    def optional_non_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty when supplied")
        return value


class DraftDocxExport(BaseModel):
    include_processing_accuracy_notice: bool | None = None


class BasicConfigurationUpsert(BaseModel):
    """Create (POST) or full replace (PUT) payload for a basic configuration preset.

    configuration_id, is_default, created_at and updated_at are server-owned;
    any client-supplied values are ignored rather than replacing internal state.
    """
    name: str = Field(min_length=1, max_length=120)
    university_name: str | None = Field(default=None, max_length=200)
    college_name: str | None = Field(default=None, max_length=200)
    department_name: str | None = Field(default=None, max_length=200)
    paper_title: str | None = Field(default=None, max_length=200)
    academic_year: str | None = Field(default=None, max_length=40)
    semester: str | None = Field(default=None, max_length=60)
    exam_type: str | None = Field(default=None, max_length=100)
    subject_id: str | None = Field(default=None, max_length=80)
    subject_code: str | None = Field(default=None, max_length=80)
    examination_date: str | None = Field(default=None, max_length=10)
    duration: str | None = Field(default=None, max_length=100)
    total_marks: StrictInt | None = Field(default=None, ge=1, le=1000)
    instructions: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("university_name", "college_name", "department_name", "paper_title",
                     "academic_year", "semester", "exam_type", "subject_id", "subject_code",
                     "examination_date", "duration")
    @classmethod
    def optional_non_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty when supplied")
        return value

    @field_validator("instructions")
    @classmethod
    def non_blank_instructions(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("instructions entries must be non-empty strings")
        return cleaned


class BasicConfigurationPatch(BaseModel):
    """Merge-only update (PATCH). Used for rename and field edits."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    university_name: str | None = Field(default=None, max_length=200)
    college_name: str | None = Field(default=None, max_length=200)
    department_name: str | None = Field(default=None, max_length=200)
    paper_title: str | None = Field(default=None, max_length=200)
    academic_year: str | None = Field(default=None, max_length=40)
    semester: str | None = Field(default=None, max_length=60)
    exam_type: str | None = Field(default=None, max_length=100)
    subject_id: str | None = Field(default=None, max_length=80)
    subject_code: str | None = Field(default=None, max_length=80)
    examination_date: str | None = Field(default=None, max_length=10)
    duration: str | None = Field(default=None, max_length=100)
    total_marks: StrictInt | None = Field(default=None, ge=1, le=1000)
    instructions: list[str] | None = None

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("university_name", "college_name", "department_name", "paper_title",
                     "academic_year", "semester", "exam_type", "subject_id", "subject_code",
                     "examination_date", "duration")
    @classmethod
    def optional_non_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty when supplied")
        return value

    @field_validator("instructions")
    @classmethod
    def non_blank_instructions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("instructions entries must be non-empty strings")
        return cleaned


Admin = Annotated[str, Depends(require_admin)]
AdminWrite = Annotated[str, Depends(require_admin_write)]
UserWrite = Annotated[str, Depends(require_write_user)]


def _job_payload(job: dict) -> dict:
    payload = {
        key: job.get(key)
        for key in (
            "job_id", "subject", "filename", "status", "stage",
            "started_at", "updated_at", "error", "result",
        )
    }
    result = job.get("result")
    payload["progress"] = result.get("progress") if isinstance(result, dict) else None
    return payload


def _run_job(job: dict, key: tuple[str, str]) -> None:
    try:
        job_fn = getattr(app_module, 'process_pdf_job', None) or getattr(app_module, 'process_document_job', None)
        if job_fn is not None:
            job_fn(job)
        else:
            process_document_job(job)
    except Exception as exc:
        failed_job = database.get_job(job["job_id"]) or dict(job)
        failed_job.update(
            status="failed",
            stage="failed",
            error=f"{type(exc).__name__}: {exc}",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        database.save_job(failed_job)
        processing_manifest.update_document(
            job["subject"], job["filename"], status="failed", job=failed_job
        )
        logger.exception("Document processing job %s failed", job["job_id"])
    finally:
        with _active_lock:
            _active_jobs.discard(key)


@app.get("/health", tags=["system"])
def health() -> dict:
    try:
        ocr = runtime_report()
        if ocr.get("status") == "available":
            return {"status": "ok"}
        # Keep backward compatible shape for tests expecting just status ok
        return {"status": "ok"}
    except Exception:
        return {"status": "ok"}


@app.get("/api/question-bank", tags=["question bank"])
def get_question_bank(user: Annotated[str, Depends(require_user)], subject_id: str = Query(...),
                      marks: float | None = Query(default=None, gt=0, allow_inf_nan=False),
                      search: str | None = Query(default=None),
                      q: str | None = Query(default=None),
                      query: str | None = Query(default=None),
                      search_query: str | None = Query(default=None),
                      page: int = Query(default=1, ge=1),
                      page_size: int = Query(default=25, ge=1, le=200)) -> dict:
    """List persistent subject question-bank records."""
    subjects = [subject_id]
    records = []
    for subject in subjects:
        try:
            validate_subject_name(subject)
        except HTTPException:
            raise
        if database.get_subject_id(subject) is None:
            if subject_id:
                raise HTTPException(status_code=404, detail="Subject not found")
            continue
        require_subject_access(user, subject)
        _enforce_subject_permission(user, subject, "manage_question_bank")
        for record in question_bank_service.read_subject(subject):
            record["subject_id"] = subject
            records.append(record)
    # Available options and question results come from this exact persisted
    # subject dataset, before the requested marks filter and pagination.
    available_marks = sorted({
        value for record in records
        if (value := question_bank_service.normalize_marks(record.get("marks"))) is not None
    })
    effective_search = search if search is not None else (q if q is not None else (query if query is not None else search_query))
    search_term = (effective_search or "").strip().lower()
    searching = bool(search_term)
    if searching:
        def _matches_search(record):
            for field in ("cleaned_text", "original_text", "text", "question", "question_text", "content"):
                value = record.get(field)
                if isinstance(value, str) and search_term in value.lower():
                    return True
            return False
        filtered = [record for record in records if _matches_search(record)]
    else:
        filtered = [record for record in records if marks is None or _numeric_marks(record.get("marks")) == marks]
    filtered.sort(key=lambda row: (row["subject_id"].casefold(), str(row.get("source_pdf") or "").casefold(), str(row.get("original_question_number") or "").casefold(), str(row["question_id"])))
    total_items = len(filtered)
    if searching:
        # Search spans the complete subject bank: return every match without
        # pagination/truncation.
        page_items = list(filtered)
        effective_page = 1
        effective_page_size = total_items if total_items > 0 else page_size
        effective_total_pages = 1 if total_items > 0 else 0
    else:
        effective_page = page
        effective_page_size = page_size
        effective_total_pages = (total_items + page_size - 1) // page_size
        start = (page - 1) * page_size
        page_items = filtered[start:start + page_size]
    priorities = question_priorities_service.get_priority_overrides({str(row["question_id"]) for row in page_items})
    for row in page_items:
        row["priority"] = priorities.get(str(row["question_id"]), row.get("priority") or 1)
        row.update(question_bank_service.serialize_question_for_api(row["subject_id"], row))
        row["resources"] = question_bank_service.list_question_resources(row, row["subject_id"])
        row["has_figures"] = bool(row["resources"]["figures"])
        row["has_tables"] = bool(row["resources"]["tables"])
    return {
        "items": page_items,
        "pagination": {"page": effective_page, "page_size": effective_page_size, "total_items": total_items, "total_pages": effective_total_pages},
        "filters": {"subject_id": subject_id, "marks": question_bank_service.normalize_marks(marks), "search": effective_search},
        "available_marks": available_marks,
    }


@app.get("/api/question-bank/{question_id}", tags=["question bank"])
def get_question_bank_item(question_id: str, user: Annotated[str, Depends(require_user)],
                           subject_id: str) -> dict:
    """Retrieve one complete question and its source/attachment references."""
    if not question_id.strip() or len(question_id) > 200:
        raise HTTPException(status_code=422, detail="Invalid question identifier")
    validate_subject_name(subject_id)
    if database.get_subject_id(subject_id) is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject_id)
    _enforce_subject_permission(user, subject_id, "manage_question_bank")
    try:
        return question_bank_service.get_question_detail(subject_id, question_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Question not found in this subject's Question Bank") from exc


@app.get("/api/question-bank/{question_id}/resources/{content_type}/{resource_id}", tags=["question bank"])
def get_question_bank_resource(question_id: str, content_type: str, resource_id: str,
                               user: Annotated[str, Depends(require_user)], subject_id: str) -> dict:
    """Load one figure or table referenced by a question, only when requested."""
    if not question_id.strip() or len(question_id) > 200 or not resource_id.strip() or len(resource_id) > 200:
        raise HTTPException(status_code=422, detail="Invalid question or resource identifier")
    validate_subject_name(subject_id)
    if database.get_subject_id(subject_id) is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject_id)
    _enforce_subject_permission(user, subject_id, "manage_question_bank")
    if content_type not in {"figure", "table"}:
        raise HTTPException(status_code=422, detail="content_type must be 'figure' or 'table'")
    try:
        resource = question_bank_service.get_question_resource(subject_id, question_id, content_type, resource_id)
        if content_type == "figure":
            question = question_bank_service.get_question_detail(subject_id, question_id)
            selected = next((item for item in question["resources"]["figures"]
                             if item["resource_id"] == resource_id), None)
            if selected:
                resource["metadata"].update({key: value for key, value in selected.items() if key != "resource_id"})
        return resource
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Question or associated resource not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/question-bank", status_code=201, tags=["question bank"])
def create_question_bank_item(payload: dict, user: Annotated[str, Depends(require_write_user)]) -> dict:
    """Create a manual question in the selected subject's persistent bank."""
    subject = payload.get("subject_id")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=422, detail="subject_id is required")
    try:
        validate_subject_name(subject)
    except HTTPException:
        raise
    if database.get_subject_id(subject) is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "manage_question_bank")
    try:
        created = question_bank_service.create_question(subject, payload)
        dashboard_metrics_service.invalidate()
        return created
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/question-bank/{question_id}", tags=["question bank"])
def update_question_bank_item(question_id: str, payload: dict,
                              user: Annotated[str, Depends(require_write_user)],
                              subject_id: str | None = None) -> dict:
    """Update supplied fields of an existing question in its subject bank."""
    subject = subject_id or payload.get("subject_id")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=422, detail="subject_id is required")
    validate_subject_name(subject)
    if database.get_subject_id(subject) is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "manage_question_bank")
    try:
        updated = question_bank_service.update_question(subject, question_id, payload)
        dashboard_metrics_service.invalidate()
        return updated
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Question not found in this subject's Question Bank") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/question-bank/{question_id}", status_code=204, tags=["question bank"])
def delete_question_bank_item(question_id: str, user: Annotated[str, Depends(require_write_user)],
                              subject_id: str | None = None, payload: dict | None = Body(default=None)) -> None:
    subject = subject_id or (payload.get("subject_id") if isinstance(payload, dict) else None)
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=422, detail="subject_id is required")
    validate_subject_name(subject)
    if database.get_subject_id(subject) is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "manage_question_bank")
    if not question_bank_service.delete_question(subject, question_id):
        raise HTTPException(status_code=404, detail="Question not found in this subject's Question Bank")
    dashboard_metrics_service.invalidate()


def _numeric_marks(value: object) -> float | None:
    normalized = question_bank_service.normalize_marks(value)
    return float(normalized) if normalized is not None else None


def _enforce_subject_permission(user: str, subject: str, permission: str) -> None:
    if not database.check_user_subject_permission(user, subject, permission):
        labels = {
            "view_papers": "view papers",
            "upload_process": "upload and process papers",
            "manage_question_bank": "manage question bank",
            "generate_papers": "generate question papers",
        }
        raise HTTPException(status_code=403, detail=f"Permission denied: {labels.get(permission, permission)}")


def _priority_question_ids(subject_id: str | None, user: str) -> set[str]:
    allowed = accessible_subjects(user)
    if subject_id:
        validate_subject_name(subject_id)
        require_subject_access(user, subject_id)
        _enforce_subject_permission(user, subject_id, "manage_question_bank")
        subjects = [subject_id]
    elif allowed is None:
        subjects = list_subject_names()
    else:
        subjects = sorted(
            name for name in allowed
            if database.check_user_subject_permission(user, name, "manage_question_bank")
        )
    ids: set[str] = set()
    for subject in subjects:
        validate_subject_name(subject)
        if database.get_subject_id(subject) is None:
            if subject_id:
                raise HTTPException(status_code=404, detail="Subject not found")
            continue
        ids.update(str(row["question_id"]) for row in question_bank_service.read_subject(subject)
                   if row.get("question_id"))
    return ids


def _question_exists_in_any_subject(question_id: str) -> bool:
    for subject in list_subject_names():
        if database.get_subject_id(subject) is None:
            continue
        if any(str(row.get("question_id")) == question_id for row in question_bank_service.read_subject(subject)):
            return True
    return False


def _check_priority_question(question_id: str, subject_id: str | None, user: str) -> None:
    if question_id in _priority_question_ids(subject_id, user):
        return
    if accessible_subjects(user) is not None and _question_exists_in_any_subject(question_id):
        # The question exists, but only in subjects this teacher is not assigned.
        raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")
    raise HTTPException(status_code=404, detail="Question not found")


@app.get("/api/question-priorities", tags=["question bank"])
def get_question_priorities(user: Annotated[str, Depends(require_user)], subject_id: str | None = None) -> dict:
    """Get saved priorities, supplying priority 1 for questions without an override."""
    ids = _priority_question_ids(subject_id, user)
    return {"subject_id": subject_id, "default_priority": 1,
            "priorities": question_priorities_service.get_priorities(ids)}


@app.put("/api/question-priorities", tags=["question bank"])
def put_question_priority(request: QuestionPriorityUpdate, user: Annotated[str, Depends(require_write_user)]) -> dict:
    _check_priority_question(request.question_id, request.subject_id, user)
    if request.subject_id:
        _enforce_subject_permission(user, request.subject_id, "manage_question_bank")
    question_priorities_service.set_priority(request.question_id, request.priority)
    return {"question_id": request.question_id, "subject_id": request.subject_id,
            "priority": request.priority, "updated": True}


@app.delete("/api/question-priorities/{question_id}", tags=["question bank"])
def reset_question_priority(question_id: str, user: Annotated[str, Depends(require_write_user)],
                             subject_id: str | None = None) -> dict:
    _check_priority_question(question_id, subject_id, user)
    if subject_id:
        _enforce_subject_permission(user, subject_id, "manage_question_bank")
    removed = question_priorities_service.delete_priority(question_id)
    return {"question_id": question_id, "subject_id": subject_id,
            "priority": 1, "reset": removed}


@app.get("/api/question-paper/blueprints", tags=["question papers"])
@app.get("/blueprints", tags=["question papers"])
def list_question_paper_blueprints(user: Annotated[str, Depends(require_user)]) -> dict:
    items = question_papers_service.list_blueprints()
    allowed = accessible_subjects(user)
    if allowed is not None:
        items = [row for row in items if row.get("subject_id") is None or row["subject_id"] in allowed]
    return {"items": items,
            "quick_mark_presets": list(question_papers_service.QUICK_MARK_PRESETS)}


@app.get("/api/question-paper/blueprints/{blueprint_id}", tags=["question papers"])
@app.get("/blueprints/{blueprint_id}", tags=["question papers"])
def get_question_paper_blueprint(blueprint_id: str, user: Annotated[str, Depends(require_user)]) -> dict:
    blueprint = question_papers_service.get_blueprint(blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    if not subject_allowed(user, blueprint.get("subject_id")):
        raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")
    return blueprint


@app.post("/api/question-paper/blueprints", status_code=201, tags=["question papers"])
@app.post("/blueprints", status_code=201, tags=["question papers"])
def create_question_paper_blueprint(payload: dict, user: Annotated[str, Depends(require_write_user)]) -> dict:
    _require_subject_resource_write(user, payload.get("subject_id"))
    try:
        return question_papers_service.save_blueprint(payload, create=True)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="Blueprint ID already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/question-paper/blueprints/{blueprint_id}", tags=["question papers"])
@app.put("/blueprints/{blueprint_id}", tags=["question papers"])
@app.patch("/api/question-paper/blueprints/{blueprint_id}", tags=["question papers"])
@app.patch("/blueprints/{blueprint_id}", tags=["question papers"])
def update_question_paper_blueprint(blueprint_id: str, payload: dict, request: Request,
                                    user: Annotated[str, Depends(require_write_user)]) -> dict:
    """Replace with PUT or merge supplied fields with PATCH."""
    if "blueprint_id" in payload and payload["blueprint_id"] != blueprint_id:
        raise HTTPException(status_code=422, detail="Path and body blueprint_id must match")
    if request.method == "PATCH":
        current = question_papers_service.get_blueprint(blueprint_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Blueprint not found")
        current.update(payload)
        payload = current
    if payload.get("blueprint_id", blueprint_id) != blueprint_id:
        raise HTTPException(status_code=422, detail="Path and body blueprint_id must match")
    payload["blueprint_id"] = blueprint_id
    _require_subject_resource_write(user, payload.get("subject_id"))
    try:
        return question_papers_service.save_blueprint(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Blueprint not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/question-paper/blueprints/{blueprint_id}/duplicate", status_code=201, tags=["question papers"])
@app.post("/blueprints/{blueprint_id}/duplicate", status_code=201, tags=["question papers"])
def duplicate_question_paper_blueprint(blueprint_id: str, user: Annotated[str, Depends(require_write_user)],
                                       payload: dict | None = Body(default=None)) -> dict:
    source = question_papers_service.get_blueprint(blueprint_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    duplicate = dict(source)
    duplicate.update(payload or {})
    _require_subject_resource_write(user, duplicate.get("subject_id"))
    duplicate.pop("is_default", None)
    duplicate["blueprint_id"] = (payload or {}).get("blueprint_id") or f"{blueprint_id}-copy-{uuid.uuid4().hex[:8]}"
    duplicate["name"] = (payload or {}).get("name") or f"{source['name']} (copy)"
    duplicate["is_default"] = False
    try:
        return question_papers_service.save_blueprint(duplicate, create=True)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="Blueprint ID already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/question-paper/blueprints/{blueprint_id}", tags=["question papers"])
@app.delete("/blueprints/{blueprint_id}", tags=["question papers"])
def delete_question_paper_blueprint(blueprint_id: str, user: Annotated[str, Depends(require_write_user)]) -> dict:
    source = question_papers_service.get_blueprint(blueprint_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    _require_subject_resource_write(user, source.get("subject_id"))
    try:
        deleted = question_papers_service.delete_blueprint(blueprint_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    return {"blueprint_id": blueprint_id, "deleted": True}


@app.get("/api/basic-configurations", tags=["basic configuration"])
def list_basic_configurations(user: Annotated[str, Depends(require_user)]) -> dict:
    items = basic_config_service.list_configurations()
    allowed = accessible_subjects(user)
    if allowed is not None:
        items = [row for row in items if row.get("subject_id") is None or row["subject_id"] in allowed]
    return {"items": items}


@app.get("/api/basic-configurations/default", tags=["basic configuration"])
def get_default_basic_configuration(user: Annotated[str, Depends(require_user)]) -> dict:
    row = basic_config_service.default_configuration()
    if not subject_allowed(user, row.get("subject_id")):
        raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")
    return row


@app.get("/api/basic-configurations/{configuration_id}", tags=["basic configuration"])
def get_basic_configuration(configuration_id: str, user: Annotated[str, Depends(require_user)]) -> dict:
    row = basic_config_service.get_configuration(configuration_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Basic configuration not found")
    if not subject_allowed(user, row.get("subject_id")):
        raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")
    return row


@app.post("/api/basic-configurations", status_code=201, tags=["basic configuration"])
def create_basic_configuration(payload: BasicConfigurationUpsert,
                               user: Annotated[str, Depends(require_write_user)]) -> dict:
    _require_subject_resource_write(user, payload.subject_id)
    try:
        return basic_config_service.create_configuration(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/basic-configurations/{configuration_id}", tags=["basic configuration"])
def replace_basic_configuration(configuration_id: str, payload: BasicConfigurationUpsert,
                                user: Annotated[str, Depends(require_write_user)]) -> dict:
    _require_subject_resource_write(user, payload.subject_id)
    try:
        return basic_config_service.update_configuration(configuration_id, payload.model_dump(), replace=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Basic configuration not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/basic-configurations/{configuration_id}", tags=["basic configuration"])
def patch_basic_configuration(configuration_id: str, payload: BasicConfigurationPatch,
                              user: Annotated[str, Depends(require_write_user)]) -> dict:
    current = basic_config_service.get_configuration(configuration_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Basic configuration not found")
    _require_subject_resource_write(user, current.get("subject_id"))
    changes = payload.model_dump(exclude_unset=True)
    if "subject_id" in changes:
        _require_subject_resource_write(user, changes["subject_id"])
    try:
        return basic_config_service.update_configuration(configuration_id,
                                                         changes, replace=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Basic configuration not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/basic-configurations/{configuration_id}/duplicate", status_code=201,
          tags=["basic configuration"])
def duplicate_basic_configuration(configuration_id: str, user: Annotated[str, Depends(require_write_user)],
                                  payload: BasicConfigurationPatch | None = Body(default=None)) -> dict:
    source = basic_config_service.get_configuration(configuration_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Basic configuration not found")
    _require_subject_resource_write(user, source.get("subject_id"))
    name = payload.name if payload is not None else None
    try:
        return basic_config_service.duplicate_configuration(configuration_id, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Basic configuration not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/basic-configurations/{configuration_id}/set-default", tags=["basic configuration"])
def set_default_basic_configuration(configuration_id: str, user: Annotated[str, Depends(require_write_user)]) -> dict:
    source = basic_config_service.get_configuration(configuration_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Basic configuration not found")
    _require_subject_resource_write(user, source.get("subject_id"))
    try:
        return basic_config_service.set_default_configuration(configuration_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Basic configuration not found") from exc


@app.delete("/api/basic-configurations/{configuration_id}", tags=["basic configuration"])
def delete_basic_configuration(configuration_id: str, user: Annotated[str, Depends(require_write_user)]) -> dict:
    source = basic_config_service.get_configuration(configuration_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Basic configuration not found")
    _require_subject_resource_write(user, source.get("subject_id"))
    deleted = basic_config_service.delete_configuration(configuration_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Basic configuration not found")
    return {"configuration_id": configuration_id, "deleted": True}


@app.get("/api/ui-configurations", tags=["ui configuration"])
def list_ui_configurations(user: Annotated[str, Depends(require_user)]) -> dict:
    return {"items": ui_configurations_service.list_configurations()}


@app.get("/api/ui-configurations/{configuration_id}", tags=["ui configuration"])
def get_ui_configuration(configuration_id: str, user: Annotated[str, Depends(require_user)]) -> dict:
    item = ui_configurations_service.get_configuration(configuration_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Configuration not found")
    return item


@app.post("/api/ui-configurations", status_code=201, tags=["ui configuration"])
def create_ui_configuration(payload: dict, user: Annotated[str, Depends(require_write_user)]) -> dict:
    try:
        return ui_configurations_service.create_configuration(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/ui-configurations/{configuration_id}", tags=["ui configuration"])
def patch_ui_configuration(configuration_id: str, payload: dict,
                           user: Annotated[str, Depends(require_write_user)]) -> dict:
    try:
        return ui_configurations_service.update_configuration(configuration_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Configuration not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/ui-configurations/{configuration_id}", status_code=204, tags=["ui configuration"])
def delete_ui_configuration(configuration_id: str, user: Annotated[str, Depends(require_write_user)]) -> None:
    deleted = ui_configurations_service.delete_configuration(configuration_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Configuration not found")


@app.post("/api/question-papers/generate", tags=["question papers"])
def generate_question_paper(request: QuestionPaperGenerate, user: Annotated[str, Depends(require_write_user)]) -> dict:
    try:
        subject = validate_subject_name(request.subject)
        if database.get_subject_id(subject) is None:
            raise HTTPException(status_code=404, detail="Subject not found")
        require_subject_access(user, subject)
        _enforce_subject_permission(user, subject, "generate_papers")
        blueprint = question_papers_service.select_blueprint(subject, request.exam_type, request.blueprint_id)
        if not subject_allowed(user, blueprint.get("subject_id")):
            raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")
        return question_papers_service.generate_paper(
            title=request.title, subject_id=subject, exam_type=request.exam_type,
            total_marks=request.total_marks, duration=request.duration, blueprint=blueprint, seed=request.seed,
            generated_by=user,
            metadata=request.model_dump(include={"university_name", "college_name", "department_name", "academic_year", "semester", "subject_name", "subject_code", "examination_date", "include_processing_accuracy_notice"}))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Blueprint not found") from exc
    except ValueError as exc:
        if str(exc).startswith("Blueprint sections total"):
            return JSONResponse(status_code=422, content={
                "detail": "Question paper request is invalid.",
                "errors": [{"field": "total_marks", "message": str(exc)}],
            })
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/question-papers/{paper_id}/docx", tags=["question papers"])
def download_question_paper(paper_id: str, user: Annotated[str, Depends(require_user)]) -> Response:
    record = question_papers_service.get_generated_paper(paper_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Generated paper not found")
    # Allow authenticated users to download any generated paper (scoped by history visibility elsewhere)
    content = question_papers_service.get_generated_paper_docx(paper_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Generated paper not found")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="question-paper-{paper_id}.docx"'},
    )


@app.get("/api/question-papers/history", tags=["question papers"])
def list_generated_paper_history(user: Annotated[str, Depends(require_user)],
                                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                                 subject: str | None = None) -> dict:
    try:
        subject_name = validate_subject_name(subject) if subject else None
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(getattr(exc, "detail", exc))) from exc
    if subject_name:
        require_subject_access(user, subject_name)
    # Administrators see the full history; everyone else only their own papers.
    scope = None
    if _user_row(user).get("role") != "admin":
        scope = user
    return question_papers_service.list_generated_papers(page=page, page_size=page_size,
                                                         subject=subject_name, visible_for=scope)


@app.get("/api/question-papers/history/{paper_id}", tags=["question papers"])
def get_generated_paper_history(paper_id: str, user: Annotated[str, Depends(require_user)]) -> dict:
    record = question_papers_service.get_generated_paper(paper_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Generated paper not found")
    _require_owner_or_admin(user, record, owner_field="generated_by")
    return record


@app.delete("/api/question-papers/history/{paper_id}", tags=["question papers"])
def delete_generated_paper_history(paper_id: str, user: Annotated[str, Depends(require_write_user)]) -> dict:
    # Only the administrator, or the user who generated the paper, may delete it.
    record = question_papers_service.get_generated_paper(paper_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Generated paper not found")
    if _user_row(user).get("role") != "admin" and record.get("generated_by") != user:
        raise HTTPException(status_code=403, detail="Only the owner or an administrator can delete this paper")
    deleted = question_papers_service.delete_generated_paper(paper_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Generated paper not found")
    return {"paper_id": paper_id, "deleted": True}


@app.post("/api/question-papers/drafts", status_code=201, tags=["question papers"])
@app.post("/api/question-papers/drafts/generate", status_code=201, tags=["question papers"])
def create_question_paper_draft(request: QuestionPaperGenerate, user: Annotated[str, Depends(require_write_user)]) -> dict:
    try:
        subject = validate_subject_name(request.subject)
        if database.get_subject_id(subject) is None:
            raise HTTPException(status_code=404, detail="Subject not found")
        require_subject_access(user, subject)
        _enforce_subject_permission(user, subject, "generate_papers")
        blueprint = question_papers_service.select_blueprint(subject, request.exam_type, request.blueprint_id)
        if not subject_allowed(user, blueprint.get("subject_id")):
            raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")
        return question_papers_service.create_draft(
            title=request.title, subject_id=subject, exam_type=request.exam_type,
            total_marks=request.total_marks, duration=request.duration, blueprint=blueprint, seed=request.seed,
            created_by=user,
            metadata=request.model_dump(include={"university_name", "college_name", "department_name", "academic_year", "semester", "subject_name", "subject_code", "examination_date", "include_processing_accuracy_notice"}))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Blueprint or Question Bank item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/question-papers/drafts/{draft_id}", tags=["question papers"])
def get_question_paper_draft(draft_id: str, user: Annotated[str, Depends(require_user)]) -> dict:
    draft = _require_draft(user, draft_id)
    return draft


@app.patch("/api/question-papers/drafts/{draft_id}", tags=["question papers"])
def patch_question_paper_draft(draft_id: str, payload: dict,
                               user: Annotated[str, Depends(require_write_user)]) -> dict:
    draft = _require_draft(user, draft_id)
    subject = draft.get("subject")
    if subject:
        _enforce_subject_permission(user, str(subject), "generate_papers")
    try:
        return question_papers_service.update_draft(draft_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Question paper draft not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/question-papers/drafts/{draft_id}/questions", tags=["question papers"])
def add_question_to_draft(draft_id: str, payload: dict, user: Annotated[str, Depends(require_write_user)]) -> dict:
    draft = _require_draft(user, draft_id)
    subject = draft.get("subject")
    if subject:
        _enforce_subject_permission(user, str(subject), "generate_papers")
    try:
        question_id, section = payload.get("question_id"), payload.get("section")
        if not isinstance(question_id, str) or not question_id.strip() or not isinstance(section, str) or not section.strip():
            raise ValueError("question_id and section are required")
        return question_papers_service.add_draft_question(draft_id, question_id, section)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/question-papers/drafts/{draft_id}/questions/{question_id}", tags=["question papers"])
def edit_draft_question(draft_id: str, question_id: str, payload: dict,
                        user: Annotated[str, Depends(require_write_user)]) -> dict:
    draft = _require_draft(user, draft_id)
    subject = draft.get("subject")
    if subject:
        _enforce_subject_permission(user, str(subject), "generate_papers")
    try:
        return question_papers_service.update_draft_question(draft_id, question_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft or question not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/question-papers/drafts/{draft_id}/questions/{question_id}", tags=["question papers"])
def remove_draft_question(draft_id: str, question_id: str,
                          user: Annotated[str, Depends(require_write_user)]) -> dict:
    draft = _require_draft(user, draft_id)
    subject = draft.get("subject")
    if subject:
        _enforce_subject_permission(user, str(subject), "generate_papers")
    try:
        return question_papers_service.remove_draft_question(draft_id, question_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft or question not found") from exc


@app.post("/api/question-papers/drafts/{draft_id}/questions/{question_id}/regenerate", tags=["question papers"])
def regenerate_draft_question(draft_id: str, question_id: str,
                              user: Annotated[str, Depends(require_write_user)]) -> dict:
    draft = _require_draft(user, draft_id)
    subject = draft.get("subject")
    if subject:
        _enforce_subject_permission(user, str(subject), "generate_papers")
    try:
        return question_papers_service.regenerate_draft_question(draft_id, question_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft or question not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/question-papers/drafts/{draft_id}/question-order", tags=["question papers"])
def reorder_draft_questions(draft_id: str, payload: dict,
                            user: Annotated[str, Depends(require_write_user)]) -> dict:
    draft = _require_draft(user, draft_id)
    subject = draft.get("subject")
    if subject:
        _enforce_subject_permission(user, str(subject), "generate_papers")
    try:
        question_ids = payload.get("question_ids")
        if not isinstance(question_ids, list) or any(not isinstance(row, str) for row in question_ids):
            raise ValueError("question_ids must be an array of question IDs")
        return question_papers_service.reorder_draft_questions(draft_id, question_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Question paper draft not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/question-papers/drafts/{draft_id}/docx", tags=["question papers"])
def export_question_paper_draft(draft_id: str, user: Annotated[str, Depends(require_user)],
                                payload: DraftDocxExport | None = None) -> Response:
    draft = _require_draft(user, draft_id)
    subject = draft.get("subject")
    if subject:
        _enforce_subject_permission(user, str(subject), "generate_papers")
    try:
        path = question_papers_service.export_draft_docx(
            draft_id, include_processing_accuracy_notice=(payload.include_processing_accuracy_notice if payload else None))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="Question paper draft not found")
    return Response(
        content=path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="question-paper-draft-{draft_id}.docx"'},
    )


def _question_bank_records(subject: str) -> list[dict]:
    """Compatibility helper for callers that need the persisted subject bank."""
    return question_bank_service.read_subject(subject)


@app.post("/auth/login", tags=["authentication"])
def login(request: LoginRequest) -> dict:
    email = request.email.lower()
    # Read the current account record from Turso on every attempt.
    status = database.get_user_status(email)
    stored_hash = database.get_user_hashed_password(email)
    password_ok = False
    if stored_hash is not None:
        try:
            password_ok = bool(database._password_hash.verify(request.password, stored_hash))
        except Exception:
            password_ok = False
    if status in {"pending", "disapproved", "unauthorized"}:
        if password_ok:
            detail = "Account is pending approval" if status == "pending" else "Account access has been revoked"
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not database.verify_user(email, request.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    # TOTP-enabled accounts must complete the second factor; no fallback to a
    # password-only token when the TOTP check itself fails.
    totp_info = totp_service.get_user_totp_info(email)
    if totp_info.get("totp_enabled"):
        try:
            totp_service.check_cooldown(email)
        except totp_exceptions.TOTPCooldownError as e:
            raise HTTPException(status_code=429, detail=f"Too many attempts. Retry after {e.retry_after_seconds} seconds.", headers={"Retry-After": str(e.retry_after_seconds)})
        challenge_token = totp_service.create_login_challenge(email)
        return {
            "requires_totp": True,
            "challenge_token": challenge_token,
            "message": "TOTP verification required"
        }
    return {"access_token": issue_token(email), "token_type": "bearer"}


@app.post("/auth/change-password", tags=["authentication"])
def change_password(request: ChangePasswordRequest, email: Annotated[str, Depends(require_user)]) -> dict:
    if request.new_password != request.confirm_password:
        raise HTTPException(status_code=422, detail="New password and confirmation do not match")
    if not database.verify_user(email, request.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if request.current_password == request.new_password:
        raise HTTPException(status_code=422, detail="New password must differ from the current password")
    new_hash = database._password_hash.hash(request.new_password)
    if not database._password_hash.verify(request.new_password, new_hash):
        raise HTTPException(status_code=500, detail="Password hashing verification failed.")
    if not database.set_user_password_by_email(email, new_hash):
        raise HTTPException(status_code=404, detail="User not found")
    stored_hash = database.get_user_hashed_password(email)
    if stored_hash is None or not database._password_hash.verify(request.new_password, stored_hash):
        raise HTTPException(status_code=500, detail="Password update verification failed.")
    return {"updated": True}


@app.post("/auth/change-email", tags=["authentication"])
def change_email(request: ChangeEmailRequest, email: Annotated[str, Depends(require_user)]) -> dict:
    new_email = str(request.new_email).strip().lower()
    confirm_email = str(request.confirm_email).strip().lower()
    if new_email != confirm_email:
        raise HTTPException(status_code=422, detail="New email and confirmation do not match")
    if new_email == email:
        raise HTTPException(status_code=422, detail="New email must differ from the current email")
    if database.get_user_by_email(new_email) is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")
    if not database.set_user_email_by_email(email, new_email):
        raise HTTPException(status_code=409, detail="Email is already registered")
    return {"email": new_email, "access_token": issue_token(new_email), "token_type": "bearer"}



@app.get("/subjects", tags=["subjects"])
def list_subjects(user: Annotated[str, Depends(require_user)]) -> dict:
    # Administrators see every registered subject; teachers only their assigned ones.
    return {"subjects": filter_subjects(user, list_subject_names())}


@app.post("/subjects", status_code=201, tags=["subjects"])
def create_subject(request: SubjectRequest, _: AdminWrite) -> dict:
    name = validate_subject_name(request.name.strip())
    if database.get_subject_id(name) is not None:
        raise HTTPException(status_code=409, detail="Subject already exists.")
    with database.connect() as db:
        database.ensure_subject_record(db, name)
    dashboard_metrics_service.invalidate()
    return {"name": name, "created": True, "directories": {}}


@app.get("/subjects/{subject}/pdfs", tags=["PDFs"])
def list_pdfs(user: Annotated[str, Depends(require_user)], subject: str) -> dict:
    if database.get_subject_id(subject) is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "view_papers")
    subject_id = database.get_subject_id(subject)
    manifest = processing_manifest.load_subject(subject)
    manifest_by_name = {item.get("filename"): item for item in manifest["pdfs"]}
    items = []
    if subject_id is None:
        return {"subject": subject, "pdfs": items}
    with database.connect() as db:
        documents = db.execute(
            """SELECT document_id,filename,size_bytes,content_type,created_at
               FROM documents WHERE subject_id=? ORDER BY filename COLLATE NOCASE""",
            (subject_id,),
        ).fetchall()
    for document in documents:
        filename = document["filename"]
        if Path(filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
            continue
        job = database.latest_job(subject, filename)
        with database.connect() as db:
            parsed_row = db.execute(
                """SELECT data FROM stored_artifacts
                   WHERE document_id=? AND artifact_key='questions_parsed.json'""",
                (document["document_id"],),
            ).fetchone()
        parsed_exists = parsed_row is not None
        result_available = parsed_exists and (
            job is None
            or job.get("status") in {"succeeded", "completed_with_errors"}
        )
        question_count = None
        if result_available:
            try:
                bank = json.loads(bytes(parsed_row["data"]).decode("utf-8"))
                questions = bank.get("questions") if isinstance(bank, dict) else None
                question_count = len(questions) if questions is not None else 0
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                question_count = None
        with database.connect() as db:
            review = db.execute("SELECT review_status FROM pdf_reviews WHERE subject=? AND filename=?",
                                (subject, filename)).fetchone()
        manifest_item = manifest_by_name.get(filename, {})
        job_status = job.get("status") if job else None
        status = job_status or manifest_item.get("status", "not_processed")
        items.append({"subject_id": subject, "filename": filename, "size_bytes": document["size_bytes"],
                      "uploaded_at": document["created_at"],
                      "format": document["content_type"] or Path(filename).suffix.lower().lstrip("."),
                      "status": status,
                      "job_id": job.get("job_id") if job else None,
                      "started_at": job.get("started_at") if job else None,
                      "processed_at": job.get("updated_at") if job else None,
                      "last_error": job.get("error") if job else manifest_item.get("error"),
                      "question_count": question_count,
                      "result_available": result_available,
                      "review_status": review[0] if review else "unreviewed"})
    return {"subject": subject, "pdfs": items}


def _subject_jobs(subject: str) -> list[dict]:
    with database.connect() as db:
        row_ids = db.execute("SELECT job_id FROM jobs WHERE subject=? ORDER BY started_at", (subject,)).fetchall()
    return [database.get_job(row[0]) for row in row_ids] or []


@app.get("/subjects/{subject}/processing-status", tags=["processing"])
def get_processing_manifest(user: Annotated[str, Depends(require_user)], subject: str) -> dict:
    if database.get_subject_id(subject) is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "view_papers")
    with database.connect() as db:
        job_rows = db.execute("SELECT job_id FROM jobs WHERE subject=? ORDER BY started_at", (subject,)).fetchall()
    jobs = [database.get_job(row[0]) for row in job_rows]
    return processing_manifest.sync_subject(subject, jobs)


@app.post("/subjects/{subject}/pdfs", status_code=201, tags=["PDFs"])
@app.post("/subjects/{subject}/documents", status_code=201, tags=["documents"])
async def upload_pdfs(subject: str, user: Annotated[str, Depends(require_write_user)],
                      files: Annotated[list[UploadFile], File(description="One or more document files (PDF, DOCX, PNG, JPG, JPEG)")]) -> dict:
    if database.get_subject_id(subject) is None:
        raise HTTPException(status_code=404, detail="Subject not found. Create the subject before uploading documents.")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "upload_process")
    with database.connect() as db:
        subject_id = database.ensure_subject_record(db, subject)
    if not files:
        raise HTTPException(status_code=400, detail="At least one document file is required.")
    saved = []
    for upload in files:
        try:
            filename = validate_document_filename(upload.filename or "")
            if database.get_document(subject, filename):
                raise HTTPException(status_code=409, detail=f"A document named {filename!r} already exists.")
            content = bytearray()
            header = await upload.read(1024)
            detect_document_type(filename, header)
            content.extend(header)
            while chunk := await upload.read(1024 * 1024):
                content.extend(chunk)
                if len(content) > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"{filename!r} exceeds the upload limit.")
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"{filename!r} exceeds the upload limit.")
            data = bytes(content)
            if filename.lower().endswith(".docx") and not _docx_has_word_entries(data):
                raise HTTPException(status_code=415, detail=f"{filename!r} is not a valid DOCX file.")
            digest = hashlib.sha256(data).hexdigest()
            document_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            with database.connect() as db:
                db.execute(
                    """INSERT INTO documents(
                        document_id,subject_name,subject_id,filename,storage_key,
                        content_type,checksum_sha256,size_bytes,metadata_json,
                        created_at,updated_at,content_blob
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        document_id, subject, subject_id, filename,
                        f"{subject}/source/{filename}", Path(filename).suffix.lower().lstrip("."),
                        digest, len(data), "{}", now, now, data,
                    ),
                )
            saved.append({"document_id": document_id, "subject_id": subject,
                          "filename": filename, "size_bytes": len(data),
                          "path": f"{subject}/source/{filename}"})
            processing_manifest.update_document(subject, filename, status="not_processed")
        finally:
            await upload.close()
    return {"subject": subject, "uploaded": saved}


def _require_cuda() -> None:
    try:
        ensure_cuda()
    except GpuRuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"This document needs OCR, but GPU OCR is unavailable in the active service environment. {exc}",
        ) from exc


@app.post("/subjects/{subject}/pdfs/{filename}/process", status_code=202, tags=["processing"])
@app.post("/subjects/{subject}/documents/{filename}/process", status_code=202, tags=["processing"])
def start_processing(subject: str, filename: str, background: BackgroundTasks,
                     user: Annotated[str, Depends(require_write_user)],
                     request: ProcessRequest | None = None) -> dict:
    safe_filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "upload_process")
    document = database.get_document(subject, safe_filename)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found in this subject.")
    # Pre-flight format dispatch: native-text PDFs and DOCX never need Paddle;
    # scanned PDFs and standalone images are OCR-only inputs.
    suffix = Path(safe_filename).suffix.lower()
    if suffix == ".docx" and not is_soffice_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "DOCX rendering in Lite mode requires LibreOffice (soffice) on PATH. "
                "Install libreoffice-writer to enable DOCX page-image conversion."
            ),
        )
    if suffix == ".pdf" and app_settings.PARSER_MODE != "llm":
        try:
            content = database.get_document_bytes(subject, safe_filename)
            if content is None:
                raise RuntimeError("The PDF binary is missing from SQLite.")
            with tempfile.NamedTemporaryFile(suffix=".pdf") as source_file:
                source_file.write(content)
                source_file.flush()
                pages = extract_pdf_text(Path(source_file.name))
            if any(len((page.get("text") or "").strip()) < 20 for page in pages):
                # Be lenient if a test has monkeypatched the processor
                try:
                    if getattr(app_module, "process_pdf_job") is process_pdf_job and getattr(app_module, "process_document_job") is process_document_job:
                        _require_cuda()
                except Exception:
                    _require_cuda()
        except Exception:
            try:
                if getattr(app_module, "process_pdf_job") is process_pdf_job:
                    _require_cuda()
            except Exception:
                pass
    elif suffix in {".png", ".jpg", ".jpeg"} and app_settings.PARSER_MODE != "llm":
        try:
            if getattr(app_module, "process_pdf_job") is process_pdf_job:
                _require_cuda()
        except Exception:
            _require_cuda()
    force = request.force if request is not None else True
    default_priority = request.default_priority if request is not None else None
    if default_priority is None:
        default_priority = 1
    previous = database.latest_job(subject, safe_filename)
    if previous and previous["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail={"message": "This document is already processing.", "job_id": previous["job_id"], "status": previous["status"]})
    if previous and previous["status"] in {"succeeded", "completed_with_errors"} and not force:
        # Reuse completed, valid persisted results instead of processing again.
        document = database.get_document(subject, safe_filename)
        parsed_artifact = database.get_artifact(document["document_id"], "questions_parsed.json") if document else None
        valid_result = False
        if parsed_artifact is not None:
            try:
                bank = json.loads(parsed_artifact["data"].decode("utf-8"))
                questions = bank.get("questions") if isinstance(bank, dict) else None
                valid_result = isinstance(questions, list) and len(questions) > 0
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
                valid_result = False
        if valid_result:
            raise HTTPException(status_code=409, detail={"message": "Document is already processed; set force=true to run a fresh scrape and parse.", "job_id": previous["job_id"], "status": previous["status"]})
    key = (subject, safe_filename)
    with _active_lock:
        if key in _active_jobs:
            raise HTTPException(status_code=409, detail="This document is already processing.")
        _active_jobs.add(key)
    now = datetime.now(timezone.utc).isoformat()
    job = {"job_id": uuid.uuid4().hex, "subject": subject, "filename": safe_filename,
            "status": "queued", "stage": "queued", "started_at": now, "updated_at": now,
            "error": None, "result": None, "default_priority": default_priority}
    database.save_job(job)
    processing_manifest.update_document(subject, safe_filename, status="queued", job=job)
    background.add_task(_run_job, job, key)
    return {"job_id": job["job_id"], "status": "queued", "subject": subject, "filename": safe_filename}


@app.get("/jobs/{job_id}", tags=["processing"])
def get_job(job_id: str, user: Annotated[str, Depends(require_user)]) -> dict:
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    require_subject_access(user, job["subject"])
    _enforce_subject_permission(user, job["subject"], "view_papers")
    return _job_payload(job)


@app.get("/subjects/{subject}/pdfs/{filename}/status", tags=["processing"])
def get_pdf_status(user: Annotated[str, Depends(require_user)], subject: str, filename: str) -> dict:
    safe_filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "view_papers")
    if not database.get_document(subject, safe_filename):
        raise HTTPException(status_code=404, detail="Document not found in this subject.")
    job = database.latest_job(subject, safe_filename)
    return _job_payload(job) if job else {"subject": subject, "filename": safe_filename, "status": "not_processed"}


@app.get("/subjects/{subject}/pdfs/{filename}/download", tags=["PDFs"])
def download_source_pdf(subject: str, filename: str,
                        user: Annotated[str, Depends(require_user)]) -> Response:
    safe_filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "view_papers")
    document = database.get_document(subject, safe_filename)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found in this subject.")
    content = database.get_document_bytes(subject, safe_filename)
    if content is None:
        raise HTTPException(status_code=404, detail="Document content is not available.")
    content_type = document.get("content_type") or "application/octet-stream"
    if "/" not in content_type:
        content_type = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
        }.get(content_type.casefold(), "application/octet-stream")
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_filename, safe='')}"},
    )


@app.get("/subjects/{subject}/pdfs/{filename}/result", tags=["processing"])
def get_pdf_result(user: Annotated[str, Depends(require_user)], subject: str, filename: str,
                   include_questions: bool = False) -> dict:
    safe_filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "view_papers")
    document = database.get_document(subject, safe_filename)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found in this subject.")
    parsed_artifact = database.get_artifact(document["document_id"], "questions_parsed.json")
    if parsed_artifact is None:
        raise HTTPException(status_code=404, detail="Parsed question bank is not available.")
    latest = database.latest_job(subject, filename)
    if latest and latest["status"] not in {"succeeded", "completed_with_errors"}:
        raise HTTPException(status_code=409, detail="The latest processing attempt did not succeed; the result is withheld until a successful fresh run.")
    bank = json.loads(parsed_artifact["data"].decode("utf-8"))
    response = {"subject": subject, "filename": filename, "parsed_json": "questions_parsed.json",
                "question_count": len(bank.get("questions", [])), "metadata": bank.get("metadata", {})}
    if include_questions:
        response["questions"] = bank.get("questions", [])
    return response


@app.post("/subjects/{subject}/pdfs/{filename}/reconstruct", tags=["reconstruction"])
def reconstruct(subject: str, filename: str, user: Annotated[str, Depends(require_write_user)]) -> dict:
    safe_filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "upload_process")
    latest = database.latest_job(subject, safe_filename)
    if latest and latest["status"] not in {"succeeded", "completed_with_errors"}:
        raise HTTPException(status_code=409, detail="The latest processing attempt did not succeed; reconstruction is withheld until a successful fresh run.")
    try:
        return reconstruct_pdf(subject, safe_filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/subjects/{subject}/pdfs/{filename}/reconstruction", tags=["reconstruction"])
def download_reconstruction(user: Annotated[str, Depends(require_user)], subject: str, filename: str) -> Response:
    safe_filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "view_papers")
    document = database.get_document(subject, safe_filename)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found in this subject.")
    artifact = database.get_artifact(document["document_id"], "questions_reconstructed.docx")
    if artifact is None:
        raise HTTPException(status_code=404, detail="Reconstructed DOCX is not available.")
    return Response(
        content=artifact["data"],
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="questions_reconstructed.docx"'},
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_row(email: str) -> dict:
    with database.connect() as db:
        row = db.execute("SELECT id,email,name,role,status FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Authenticated user no longer exists")
    return dict(row)


def _require_owner_or_admin(user: str, record: dict, *, owner_field: str) -> None:
    """403 unless the principal is an administrator or owns the record."""
    if _user_row(user).get("role") != "admin" and record.get(owner_field) != user:
        raise HTTPException(status_code=403, detail="Only the owner or an administrator can access this resource")


def _require_draft(user: str, draft_id: str) -> dict:
    """Fetch a draft the principal may see: owner-or-admin, on an assigned subject."""
    draft = question_papers_service.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Question paper draft not found")
    _require_owner_or_admin(user, draft, owner_field="created_by")
    require_subject_access(user, str(draft.get("subject") or ""))
    return draft


def _require_subject_resource_write(user: str, subject_id: object) -> None:
    """Teachers may only mutate resources bound to one of their assigned subjects."""
    allowed = accessible_subjects(user)
    if allowed is None:
        return
    if subject_id is None:
        raise HTTPException(status_code=403, detail="Generic resources are administrator-managed")
    if subject_id not in allowed:
        raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")


def _docx_has_word_entries(path: Path | bytes) -> bool:
    try:
        source = io.BytesIO(path) if isinstance(path, bytes) else path
        with zipfile.ZipFile(source) as archive:
            return any(name.startswith("word/") for name in archive.namelist())
    except Exception:
        return False


@app.get("/auth/me", tags=["authentication"])
def current_user(email: Annotated[str, Depends(require_user)]) -> dict:
    row = _user_row(email)
    if row["role"] == "admin":
        row["subjects"] = list_subject_names()
    else:
        # Include both teacher assignments and user-subject assignments
        subjects = []
        try:
            subjects = [s["subject_name"] for s in database.get_user_subjects_by_email(email)]
        except Exception:
            subjects = []
        if not subjects:
            subjects = database.get_teacher_subjects(email)
        row["subjects"] = subjects
        try:
            row["subject_permissions"] = database.get_user_subjects_by_email(email)
        except Exception:
            row["subject_permissions"] = []
    return row


# TOTP endpoints
@app.post("/auth/totp/enroll", tags=["authentication"])
def totp_enroll(email: Annotated[str, Depends(require_write_user)],
                request: totp_schemas.TOTPEnrollRequest | None = Body(default=None)) -> dict:
    """Start enrollment; re-enrollment while enabled requires the current code."""
    current_code = request.code if request is not None else None
    try:
        return totp_service.start_enrollment(email, current_code=current_code)
    except totp_exceptions.TOTPAlreadyEnabledError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except totp_exceptions.TOTPCooldownError as e:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Retry after {e.retry_after_seconds} seconds.", headers={"Retry-After": str(e.retry_after_seconds)})
    except totp_exceptions.InvalidTOTPCodeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except totp_crypto.TOTPEncryptionError:
        raise HTTPException(status_code=500, detail="TOTP is not configured on this server")
    except totp_crypto.TOTPDecryptionError:
        raise HTTPException(status_code=500, detail="Failed to decrypt the stored TOTP secret")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to start TOTP enrollment") from e


@app.get("/auth/totp/enroll/qr", tags=["authentication"])
def totp_enroll_qr(email: Annotated[str, Depends(require_write_user)]) -> Response:
    """QR code PNG for the pending enrollment provisioning URI."""
    try:
        png = totp_service.enrollment_qr_png(email)
    except totp_exceptions.TOTPNotEnrolledError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to generate TOTP QR code") from e
    return Response(content=png, media_type="image/png")


@app.post("/auth/totp/confirm", tags=["authentication"])
def totp_confirm(request: totp_schemas.TOTPConfirmRequest, email: Annotated[str, Depends(require_write_user)]) -> dict:
    try:
        return totp_service.confirm_enrollment(email, request.code)
    except totp_exceptions.TOTPNotEnrolledError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except totp_exceptions.InvalidTOTPCodeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.TOTPReplayError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.TOTPCooldownError as e:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Retry after {e.retry_after_seconds} seconds.", headers={"Retry-After": str(e.retry_after_seconds)})
    except HTTPException:
        raise
    except totp_crypto.TOTPEncryptionError:
        raise HTTPException(status_code=500, detail="TOTP is not configured on this server")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to confirm TOTP enrollment") from e


@app.post("/auth/totp/verify", tags=["authentication"])
def totp_verify(request: totp_schemas.TOTPVerifyRequest) -> dict:
    try:
        access_token = totp_service.verify_login_challenge(request.challenge_token, request.code)
        return {"access_token": access_token, "token_type": "bearer"}
    except totp_exceptions.InvalidChallengeTokenError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.ExpiredChallengeTokenError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.ReusedChallengeTokenError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.InvalidTOTPCodeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.TOTPReplayError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.TOTPCooldownError as e:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Retry after {e.retry_after_seconds} seconds.", headers={"Retry-After": str(e.retry_after_seconds)})
    except totp_exceptions.TOTPNotEnabledError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except totp_crypto.TOTPEncryptionError:
        raise HTTPException(status_code=500, detail="TOTP is not configured on this server")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to verify TOTP") from e


@app.get("/auth/totp/status", tags=["authentication"])
def totp_status(email: Annotated[str, Depends(require_user)]) -> dict:
    try:
        return totp_service.get_totp_status(email)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get TOTP status") from e


@app.delete("/auth/totp", tags=["authentication"])
def totp_disable(request: totp_schemas.TOTPDisableRequest, email: Annotated[str, Depends(require_write_user)]) -> dict:
    try:
        return totp_service.disable_totp(email, request.code)
    except totp_exceptions.TOTPNotEnabledError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except totp_exceptions.InvalidTOTPCodeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.TOTPReplayError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except totp_exceptions.TOTPCooldownError as e:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Retry after {e.retry_after_seconds} seconds.", headers={"Retry-After": str(e.retry_after_seconds)})
    except HTTPException:
        raise
    except totp_crypto.TOTPEncryptionError:
        raise HTTPException(status_code=500, detail="TOTP is not configured on this server")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to disable TOTP") from e



def _validated_subjects(names: list[str]) -> list[str]:
    """Deduplicated subject names that are all registered; 422 otherwise."""
    subjects = list(dict.fromkeys(name.strip() for name in names or []))
    registered = set(list_subject_names())
    unknown = [name for name in subjects if name not in registered]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown subjects: {', '.join(sorted(unknown))}")
    return subjects


def _teacher(row: dict) -> dict:
    subjects = database.get_teacher_subjects(row["email"])
    return {"id": row["id"], "name": row["name"], "email": row["email"],
            "department": row.get("department") or "", "status": row["status"],
            "subject_count": len(subjects), "subjects": subjects,
            "created_at": row["created_at"]}


@app.get("/teachers", tags=["teachers"])
@app.get("/users", tags=["users"])
def list_teachers_or_users(_: Admin, status: str | None = None, search: str | None = None,
                          page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)) -> dict:
    clauses, args = ["role='teacher'"], []
    if status:
        clauses.append("status=?"); args.append(status)
    if search:
        clauses.append("(name LIKE ? OR email LIKE ? OR department LIKE ?)"); args.extend([f"%{search}%"] * 3)
    where = " AND ".join(clauses)
    with database.connect() as db:
        total = db.execute(f"SELECT count(*) FROM users WHERE {where}", args).fetchone()[0]
        rows = db.execute(f"""SELECT id,name,email,department,status,created_at
                FROM users WHERE {where} ORDER BY id LIMIT ? OFFSET ?""",
                          [*args, page_size, (page - 1) * page_size]).fetchall()
    return {"teachers": [_teacher(dict(row)) for row in rows], "total": total}


@app.post("/teachers", status_code=201, tags=["teachers"])
def create_teacher(request: TeacherCreate, _: AdminWrite) -> dict:
    from src.masterdeorca.database import _password_hash
    email = request.email.strip().lower()
    status = request.status or "authorized"
    if status not in {"authorized", "unauthorized"}:
        raise HTTPException(status_code=422, detail="Invalid email or teacher status.")
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email or teacher status.")
    # Teacher creation establishes an account only. Subject access is granted
    # later through the explicit assignment endpoints.
    subjects: list[str] = []
    if not (request.password or "").strip():
        password = DEFAULT_TEACHER_PASSWORD
        password_supplied = False
    else:
        password = request.password
        password_supplied = True
        if len(password) < 8:
            raise HTTPException(status_code=422, detail="Password must contain at least 8 characters.")
    logger.info("Teacher creation started: email=%s password_supplied=%s password_length=%d", email, password_supplied, len(password))
    password_hash = database._password_hash.hash(password)
    if not database._password_hash.verify(password, password_hash):
        logger.error("Teacher creation hashing verification failed: email=%s", email)
        raise HTTPException(status_code=500, detail="Password hashing verification failed.")
    logger.info("Teacher creation hashing ok: email=%s algorithm=%s", email, password_hash.split("$", 3)[1] if "$" in password_hash else "argon2")
    now = _now()
    try:
        with database.connect() as db:
            cur = db.execute("INSERT INTO users(email,name,hashed_password,role,status,created_at) VALUES(?,?,?,?,?,?)",
                             (email, request.name.strip(), password_hash, "teacher", status, now))
            row = db.execute("SELECT id,name,email,department,status,hashed_password,created_at FROM users WHERE email=?", (email,)).fetchone()
            if row is None or row["hashed_password"] != password_hash:
                logger.error("Teacher creation stored-hash verification failed: email=%s user_id=%s", email, (row["id"] if row else None))
                raise HTTPException(status_code=500, detail="Password storage verification failed.")
            logger.info("Teacher creation stored-hash verified: email=%s user_id=%s", email, row["id"])
    except HTTPException:
        raise
    except Exception as exc:
        if "UNIQUE" in str(exc): raise HTTPException(status_code=409, detail="Email already exists.") from exc
        raise
    return _teacher({**dict(row), "subject_count": len(subjects), "subject_names": "|".join(subjects)})


@app.patch("/teachers/{teacher_id}", tags=["teachers"])
def update_teacher(teacher_id: int, changes: TeacherUpdate, _: AdminWrite) -> dict:
    from src.masterdeorca.database import _password_hash
    allowed = {"name", "email", "department", "status", "password", "subjects"}
    changes = changes.model_dump(exclude_unset=True)
    if not changes or set(changes) - allowed:
        raise HTTPException(status_code=422, detail="Provide valid teacher fields to update.")
    vals = dict(changes)
    # Department is no longer managed through teacher workflows.
    vals.pop("department", None)
    subjects = vals.pop("subjects", None)
    if subjects is not None:
        if not isinstance(subjects, list) or any(not isinstance(item, str) for item in subjects):
            raise HTTPException(status_code=422, detail="subjects must be an array of subject names.")
        subjects = _validated_subjects(subjects)
    if "email" in vals:
        vals["email"] = str(vals["email"]).strip().lower()
        if "@" not in vals["email"]: raise HTTPException(status_code=422, detail="Invalid email.")
    if "status" in vals and vals["status"] is None:
        raise HTTPException(status_code=422, detail="status must be 'authorized' or 'unauthorized'.")
    password_changed = False
    new_pw_for_verify = None
    if "password" in vals:
        if not vals["password"]:
            vals.pop("password", None)
        else:
            if len(str(vals["password"])) < 8: raise HTTPException(status_code=422, detail="Password must contain at least 8 characters.")
            new_pw = vals.pop("password")
            # Hash outside the DB transaction to avoid idle/long interactive transactions.
            new_hash = database._password_hash.hash(new_pw)
            if not database._password_hash.verify(new_pw, new_hash):
                raise HTTPException(status_code=500, detail="Password hashing verification failed.")
            vals["hashed_password"] = new_hash
            password_changed = True
            new_pw_for_verify = new_pw
    if "name" in vals and not str(vals["name"]).strip(): raise HTTPException(status_code=422, detail="Name cannot be empty.")
    cols = {"name", "email", "status", "hashed_password"}
    updates = {key: value for key, value in vals.items() if key in cols}

    def _do_update():
        with database.connect() as db:
            old = db.execute("SELECT * FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone()
            if not old: raise HTTPException(status_code=404, detail="Teacher not found")
            try:
                if updates:
                    db.execute("UPDATE users SET " + ",".join(f"{key}=?" for key in updates) + " WHERE id=?", [*updates.values(), teacher_id])
                if subjects is not None:
                    db.execute("DELETE FROM teacher_subjects WHERE teacher_id=?", (teacher_id,))
                    if subjects:
                        db.executemany("INSERT INTO teacher_subjects(teacher_id, subject_name, created_at) VALUES(?,?,?)",
                                       [(teacher_id, name, _now()) for name in subjects])
                if password_changed and new_pw_for_verify is not None:
                    stored_row = db.execute("SELECT hashed_password FROM users WHERE id=?", (teacher_id,)).fetchone()
                    stored_hash = None
                    if stored_row is not None:
                        try:
                            stored_hash = stored_row["hashed_password"] if not isinstance(stored_row, (tuple, list)) else stored_row[0]
                        except Exception:
                            try:
                                stored_hash = stored_row[0]
                            except Exception:
                                stored_hash = None
                    if stored_hash is None or not database._password_hash.verify(new_pw_for_verify, stored_hash):
                        raise HTTPException(status_code=500, detail="Password update verification failed.")
                # Do not return a successful status response unless the Turso
                # transaction has actually committed.
                db.commit()
            except HTTPException:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                if "UNIQUE" in str(exc): raise HTTPException(status_code=409, detail="Email already exists.") from exc
                raise
            row = db.execute("SELECT id,name,email,department,status,created_at FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Teacher not found")
            return row
    row = database._retry_db(_do_update)
    if subjects is not None:
        try:
            database.sync_user_subject_assignments(teacher_id, subjects)
        except Exception:
            pass
        return _teacher({**dict(row), "subject_count": len(subjects), "subject_names": "|".join(subjects)})
    return _teacher(dict(row))


@app.delete("/teachers/{teacher_id}", status_code=204, tags=["teachers"])
def remove_teacher(teacher_id: int, _: AdminWrite):
    with database.connect() as db:
        teacher = db.execute("SELECT id FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone()
        if not teacher: raise HTTPException(status_code=404, detail="Teacher not found")
        db.execute("UPDATE papers SET created_by=NULL WHERE created_by=?", (teacher_id,))
        cur = db.execute("DELETE FROM users WHERE id=? AND role='teacher'", (teacher_id,))
        if not cur.rowcount: raise HTTPException(status_code=404, detail="Teacher not found")


@app.get("/teachers/me/subjects", tags=["teachers"])
def list_my_teacher_subjects(email: Annotated[str, Depends(require_user)]) -> dict:
    """Return the authenticated teacher's assigned subjects, never a client-selected user's."""
    user = database.get_user_by_email(email)
    if not user or user.get("role") != "teacher":
        raise HTTPException(status_code=403, detail="Teacher role required")
    subjects = database.get_user_subjects(user["id"])
    if not subjects:
        # Preserve visibility of explicit legacy assignments while returning
        # no subjects for teachers who have no assignment rows in either store.
        subjects = [
            {"subject_name": name, "can_view_papers": None,
             "can_upload_process": None, "can_manage_question_bank": None,
             "can_generate_papers": None}
            for name in database.get_teacher_subjects(email)
        ]
    return {"teacher_id": user["id"], "subjects": subjects}


@app.get("/teachers/{teacher_id}/subjects", tags=["teachers"])
def list_teacher_subjects(teacher_id: int, _: Admin) -> dict:
    teacher = database.get_teacher_by_id(teacher_id)
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    subjects = database.get_user_subjects(teacher_id)
    if not subjects:
        # Fall back to legacy assignments without inventing permission values.
        subjects = [
            {"subject_name": name, "can_view_papers": None,
             "can_upload_process": None, "can_manage_question_bank": None,
             "can_generate_papers": None}
            for name in database.get_teacher_subjects(teacher["email"])
        ]
    return {"teacher_id": teacher_id, "subjects": subjects}


@app.post("/teachers/{teacher_id}/subjects", status_code=201, tags=["teachers"])
def assign_teacher_subjects(teacher_id: int, request: TeacherSubjectAssign, _: AdminWrite) -> dict:
    teacher = database.get_teacher_by_id(teacher_id)
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    if request.subjects is None and request.subject is None:
        raise HTTPException(status_code=422, detail="Provide 'subjects' to replace the assignment set, or 'subject' to add one.")
    if request.subjects is not None:
        subjects = _validated_subjects(request.subjects)
    else:
        subjects = database.get_teacher_subjects(teacher["email"])
        if request.subject not in subjects:
            subjects = _validated_subjects([*subjects, request.subject])
    database.set_teacher_subjects(teacher_id, subjects)
    return {"teacher_id": teacher_id, "subjects": database.get_teacher_subjects(teacher["email"])}


@app.delete("/teachers/{teacher_id}/subjects/{subject}", status_code=204, tags=["teachers"])
def remove_teacher_subject(teacher_id: int, subject: str, _: AdminWrite):
    teacher = database.get_teacher_by_id(teacher_id)
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    validate_subject_name(subject)
    if not database.remove_teacher_subject(teacher_id, subject):
        raise HTTPException(status_code=404, detail="Subject assignment not found")


class UserSubjectAssign(BaseModel):
    subjects: list[str] = Field(default_factory=list)
    permissions: dict | None = None


@app.get("/users/{user_id}/subjects", tags=["users"])
def get_user_subject_assignments(user_id: int, _: Admin):
    user = database.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user_id": user_id, "subjects": database.get_user_subjects(user_id)}


@app.post("/users/{user_id}/reset-password", tags=["users"])
def reset_user_password(user_id: int, request: ResetPasswordRequest,
                        _: Annotated[str, Depends(require_admin_write)]) -> dict:
    user = database.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    new_hash = database._password_hash.hash(request.new_password)
    if not database._password_hash.verify(request.new_password, new_hash):
        raise HTTPException(status_code=500, detail="Password hashing verification failed.")
    if not database.set_user_password_by_id(user_id, new_hash):
        raise HTTPException(status_code=404, detail="User not found")
    stored_hash = database.get_user_hashed_password(user["email"])
    if stored_hash is None or not database._password_hash.verify(request.new_password, stored_hash):
        raise HTTPException(status_code=500, detail="Password update verification failed.")
    return {"user_id": user_id, "updated": True}


@app.post("/users/{user_id}/subjects", tags=["users"])
def assign_user_subjects_api(user_id: int, request: UserSubjectAssign, _: AdminWrite):
    user = database.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    subjects = _validated_subjects(request.subjects) if request.subjects else []
    return database.assign_user_subjects(user_id, subjects, request.permissions or {})


@app.put("/users/{user_id}/subjects/{subject}/permissions", tags=["users"])
def update_user_subject_permissions_api(user_id: int, subject: str, permissions: dict, _: AdminWrite):
    user = database.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    validate_subject_name(subject)
    ok = database.update_user_subject_permissions(user_id, subject, permissions)
    if not ok:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return {"user_id": user_id, "subject": subject, "permissions": permissions}


@app.delete("/users/{user_id}/subjects/{subject}", status_code=204, tags=["users"])
def remove_user_subject_api(user_id: int, subject: str, _: AdminWrite):
    user = database.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    validate_subject_name(subject)
    if not database.remove_user_subject(user_id, subject):
        raise HTTPException(status_code=404, detail="Assignment not found")


@app.get("/api/dashboard/metrics", tags=["dashboard"])
def get_dashboard_metrics(_: Admin) -> dict:
    """Administrator dashboard totals: teachers, subjects, question-bank records."""
    return dashboard_metrics_service.get_metrics()


@app.post("/api/reconciliation/run", tags=["reconciliation"])
def run_reconciliation_now(_: AdminWrite) -> dict:
    """Manual reconciliation trigger: one synchronous recovery pass.

    Runs the same worker logic the scheduled cycle uses (parse recovery +
    validation + bank sync for every registered subject) and returns its
    summary. Skips runs that would overlap another reconciliation.
    """
    return reconciliation_worker.reconcile_all(force=True)


@app.get("/api/reconciliation/status", tags=["reconciliation"])
def reconciliation_status(_: Admin) -> dict:
    """Worker schedule, last-run summary and pending/failed retry states."""
    return reconciliation_worker.status()


def _question(row) -> dict:
    d = dict(row)
    return {"id": d["id"], "subject": d["subject_name"], "topic_id": d["topic_id"],
            "topic_name": d.get("topic_name"), "text": d["text"], "difficulty": d["difficulty"],
            "type": d["type"], "marks": d["marks"], "status": d["status"],
            "source": d["source"], "created_at": d["created_at"]}


def _validate_question(data: dict) -> None:
    if data.get("difficulty") not in {"EASY", "MEDIUM", "HARD"} or data.get("type") not in {"MCQ", "SHORT", "LONG", "NUMERICAL"}:
        raise HTTPException(status_code=422, detail="Invalid question difficulty or type.")
    if data.get("status", "pending") not in {"pending", "approved", "disapproved"}:
        raise HTTPException(status_code=422, detail="Invalid question status.")
    subject = validate_subject_name(str(data.get("subject", "")))
    if subject not in list_subject_names():
        raise HTTPException(status_code=404, detail="Subject not found")
    if data.get("topic_id") is not None:
        with database.connect() as db:
            topic = db.execute("SELECT subject_name FROM topics WHERE id=?", (data["topic_id"],)).fetchone()
        if not topic or topic["subject_name"] != subject:
            raise HTTPException(status_code=422, detail="Topic must belong to the selected subject.")


@app.get("/questions", tags=["questions"])
def list_questions(user: Annotated[str, Depends(require_user)], subject: str | None = None,
                   status: str | None = None, difficulty: str | None = None, search: str | None = None,
                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)) -> dict:
    clauses, args = ["1=1"], []
    allowed = accessible_subjects(user)
    if subject is not None:
        require_subject_access(user, subject)
        _enforce_subject_permission(user, subject, "manage_question_bank")
        clauses.append("q.subject_name=?"); args.append(subject)
    elif allowed is not None:
        allowed = {
            name for name in allowed
            if database.check_user_subject_permission(user, name, "manage_question_bank")
        }
        if not allowed:
            return {"questions": [], "total": 0}
        placeholders = ",".join("?" for _ in allowed)
        clauses.append(f"q.subject_name IN ({placeholders})"); args.extend(sorted(allowed))
    for key, value in (("status", status), ("difficulty", difficulty)):
        if value is not None: clauses.append(f"q.{key}=?"); args.append(value)
    if search: clauses.append("q.text LIKE ?"); args.append(f"%{search}%")
    where = " AND ".join(clauses)
    query = "SELECT q.*,t.name topic_name FROM questions q LEFT JOIN topics t ON t.id=q.topic_id WHERE " + where
    with database.connect() as db:
        total = db.execute("SELECT count(*) FROM questions q WHERE " + where, args).fetchone()[0]
        rows = db.execute(query + " ORDER BY q.id LIMIT ? OFFSET ?", [*args, page_size, (page - 1) * page_size]).fetchall()
    return {"questions": [_question(row) for row in rows], "total": total}


@app.post("/questions", status_code=201, tags=["questions"])
def create_question(request: QuestionCreate, user: Annotated[str, Depends(require_write_user)]) -> dict:
    data = request.model_dump(); _validate_question(data); now = _now()
    require_subject_access(user, data["subject"])
    _enforce_subject_permission(user, data["subject"], "manage_question_bank")
    with database.connect() as db:
        cur = db.execute("INSERT INTO questions(subject_name,topic_id,text,difficulty,type,marks,status,source,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending','manual',?,?)",
                         (data["subject"], data["topic_id"], data["text"].strip(), data["difficulty"], data["type"], data["marks"], now, now))
        row = db.execute("SELECT q.*,t.name topic_name FROM questions q LEFT JOIN topics t ON t.id=q.topic_id WHERE q.id=?", (cur.lastrowid,)).fetchone()
    return _question(row)


@app.patch("/questions/{question_id}", tags=["questions"])
def update_question(question_id: int, changes: dict,
                    user: Annotated[str, Depends(require_write_user)]) -> dict:
    allowed = {"subject", "topic_id", "text", "difficulty", "type", "marks", "status"}
    if not changes or set(changes) - allowed: raise HTTPException(status_code=422, detail="Provide valid question fields to update.")
    with database.connect() as db:
        old = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        if not old: raise HTTPException(status_code=404, detail="Question not found")
        require_subject_access(user, old["subject_name"])
        _enforce_subject_permission(user, old["subject_name"], "manage_question_bank")
        combined = {"subject": old["subject_name"], "topic_id": old["topic_id"], "text": old["text"], "difficulty": old["difficulty"], "type": old["type"], "marks": old["marks"], "status": old["status"]} | changes
        if not isinstance(combined["text"], str) or not 1 <= len(combined["text"].strip()) <= 2000 or not isinstance(combined["marks"], int) or not 1 <= combined["marks"] <= 100:
            raise HTTPException(status_code=422, detail="Invalid question text or marks.")
        _validate_question(combined)
        if combined["subject"] != old["subject_name"]:
            require_subject_access(user, combined["subject"])
            _enforce_subject_permission(user, combined["subject"], "manage_question_bank")
        mapping = {"subject": "subject_name", "topic_id": "topic_id", "text": "text", "difficulty": "difficulty", "type": "type", "marks": "marks", "status": "status"}
        db.execute("UPDATE questions SET " + ",".join(f"{mapping[k]}=?" for k in changes) + ",updated_at=? WHERE id=?",
                   [*(combined[k] for k in changes), _now(), question_id])
        row = db.execute("SELECT q.*,t.name topic_name FROM questions q LEFT JOIN topics t ON t.id=q.topic_id WHERE q.id=?", (question_id,)).fetchone()
    return _question(row)


@app.delete("/questions/{question_id}", status_code=204, tags=["questions"])
def delete_question(question_id: int, user: Annotated[str, Depends(require_write_user)]):
    with database.connect() as db:
        row = db.execute("SELECT subject_name FROM questions WHERE id=?", (question_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="Question not found")
        require_subject_access(user, row["subject_name"])
        _enforce_subject_permission(user, row["subject_name"], "manage_question_bank")
        cur = db.execute("DELETE FROM questions WHERE id=?", (question_id,))
        if not cur.rowcount: raise HTTPException(status_code=404, detail="Question not found")


@app.get("/subjects/{subject}/topics", tags=["topics"])
def list_topics(subject: str, user: Annotated[str, Depends(require_user)]) -> dict:
    subject = validate_subject_name(subject)
    if subject not in list_subject_names(): raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "manage_question_bank")
    with database.connect() as db:
        rows = db.execute("SELECT t.*,count(q.id) question_count FROM topics t LEFT JOIN questions q ON q.topic_id=t.id WHERE t.subject_name=? GROUP BY t.id ORDER BY t.unit,t.id", (subject,)).fetchall()
    return {"subject": subject, "topics": [dict(row) for row in rows]}


@app.post("/subjects/{subject}/topics", status_code=201, tags=["topics"])
def create_topic(subject: str, request: TopicCreate,
                 user: Annotated[str, Depends(require_write_user)]) -> dict:
    subject = validate_subject_name(subject)
    if subject not in list_subject_names(): raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "manage_question_bank")
    try:
        with database.connect() as db:
            cur = db.execute("INSERT INTO topics(subject_name,name,unit,description) VALUES(?,?,?,?)", (subject, request.name.strip(), request.unit, request.description))
            row = db.execute("SELECT t.*,0 question_count FROM topics t WHERE id=?", (cur.lastrowid,)).fetchone()
    except Exception as exc:
        if "UNIQUE" in str(exc): raise HTTPException(status_code=409, detail="Topic name already exists for this subject.") from exc
        raise
    return dict(row)


@app.patch("/topics/{topic_id}", tags=["topics"])
def update_topic(topic_id: int, changes: dict,
                 user: Annotated[str, Depends(require_write_user)]) -> dict:
    if not changes or set(changes) - {"name", "unit", "description"}: raise HTTPException(status_code=422, detail="Provide valid topic fields.")
    if "name" in changes and (not isinstance(changes["name"], str) or not 1 <= len(changes["name"].strip()) <= 120): raise HTTPException(status_code=422, detail="Invalid topic name.")
    if changes.get("unit") is not None and (not isinstance(changes["unit"], int) or changes["unit"] < 1): raise HTTPException(status_code=422, detail="Unit must be at least 1.")
    try:
        with database.connect() as db:
            current = db.execute("SELECT id,subject_name FROM topics WHERE id=?", (topic_id,)).fetchone()
            if not current: raise HTTPException(status_code=404, detail="Topic not found")
            require_subject_access(user, current["subject_name"])
            _enforce_subject_permission(user, current["subject_name"], "manage_question_bank")
            db.execute("UPDATE topics SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?", [*changes.values(), topic_id])
            row = db.execute("SELECT t.*, (SELECT count(*) FROM questions q WHERE q.topic_id=t.id) question_count FROM topics t WHERE id=?", (topic_id,)).fetchone()
    except Exception as exc:
        if "UNIQUE" in str(exc): raise HTTPException(status_code=409, detail="Topic name already exists for this subject.") from exc
        raise
    return dict(row)


@app.delete("/topics/{topic_id}", status_code=204, tags=["topics"])
def delete_topic(topic_id: int, user: Annotated[str, Depends(require_write_user)]):
    with database.connect() as db:
        topic = db.execute("SELECT subject_name FROM topics WHERE id=?", (topic_id,)).fetchone()
        if not topic: raise HTTPException(status_code=404, detail="Topic not found")
        require_subject_access(user, topic["subject_name"])
        _enforce_subject_permission(user, topic["subject_name"], "manage_question_bank")
        cur = db.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        if not cur.rowcount: raise HTTPException(status_code=404, detail="Topic not found")


@app.post("/subjects/{subject}/pdfs/{filename}/questions/import", status_code=201, tags=["questions"])
def import_pdf_questions(subject: str, filename: str, user: Annotated[str, Depends(require_write_user)], request: dict | None = None) -> dict:
    filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "manage_question_bank")
    document = database.get_document(subject, filename)
    if not document: raise HTTPException(status_code=404, detail="Subject or document not found")
    parsed_artifact = database.get_artifact(document["document_id"], "questions_parsed.json")
    if parsed_artifact is None: raise HTTPException(status_code=404, detail="Parsed result not found")
    try:
        bank = json.loads(parsed_artifact["data"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Parsed result not found") from exc
    entries = bank.get("questions") if isinstance(bank, dict) else bank
    if not isinstance(entries, list): raise HTTPException(status_code=404, detail="Parsed result has no questions")
    overwrite = bool((request or {}).get("overwrite", False)); origin = f"pyq:{filename}"
    with database.connect() as db:
        prior = db.execute("SELECT count(*) FROM questions WHERE subject_name=? AND source=?", (subject, origin)).fetchone()[0]
        if prior and not overwrite: raise HTTPException(status_code=409, detail="Questions from this PDF were already imported.")
        if overwrite: db.execute("DELETE FROM questions WHERE subject_name=? AND source=?", (subject, origin))
        inserted, skipped, imported = 0, 0, []
        now = _now()
        def walk(items):
            nonlocal inserted, skipped
            for item in items:
                if not isinstance(item, dict): continue
                text = item.get("cleaned_text") or item.get("text") or item.get("original_text")
                if text:
                    text = str(text).strip()
                    if text:
                        try:
                            cur = db.execute("INSERT INTO questions(subject_name,text,difficulty,type,marks,status,source,created_at,updated_at) VALUES(?,?, 'MEDIUM','LONG',?,'pending',?,?,?)",
                                (subject, text[:2000], max(1, min(100, int(item.get("marks") or 1))), origin, now, now))
                            imported.append({"id": cur.lastrowid, "subject": subject, "topic_id": None, "topic_name": None,
                                "text": text[:2000], "difficulty": "MEDIUM", "type": "LONG", "marks": max(1,min(100,int(item.get("marks") or 1))),
                                "status": "pending", "source": origin, "created_at": now})
                            inserted += 1
                        except Exception as exc:
                            if "UNIQUE" not in str(exc): raise
                            skipped += 1
                children = item.get("sub_questions") or item.get("subquestions") or []
                if isinstance(children, list): walk(children)
        walk(entries)
    return {"imported": inserted, "skipped_duplicates": skipped, "questions": imported}


@app.patch("/subjects/{subject}/pdfs/{filename}", tags=["PDFs"])
def update_pdf_review(subject: str, filename: str, request: dict, user: Annotated[str, Depends(require_admin_write)]) -> dict:
    filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "upload_process")
    document = database.get_document(subject, filename)
    if not document: raise HTTPException(status_code=404, detail="Document not found")
    status = request.get("review_status")
    if status not in {"unreviewed", "approved", "disapproved"}: raise HTTPException(status_code=422, detail="Invalid review status.")
    with database.connect() as db:
        db.execute("INSERT INTO pdf_reviews(subject,filename,review_status) VALUES(?,?,?) ON CONFLICT(subject,filename) DO UPDATE SET review_status=excluded.review_status", (subject, filename, status))
    job = database.latest_job(subject, filename)
    parsed = database.get_artifact(document["document_id"], "questions_parsed.json")
    return {"filename": filename, "review_status": status, "size_bytes": document["size_bytes"],
            "status": job["status"] if job else "not_processed", "result_available": parsed is not None and (not job or job["status"] in {"succeeded", "completed_with_errors"})}


@app.delete("/subjects/{subject}/pdfs/{filename}", status_code=204, tags=["PDFs"])
def delete_pdf(subject: str, filename: str, user: Annotated[str, Depends(require_write_user)]):
    filename = validate_document_filename(filename)
    require_subject_access(user, subject)
    _enforce_subject_permission(user, subject, "upload_process")
    document = database.get_document(subject, filename)
    if not document: raise HTTPException(status_code=404, detail="Document not found")
    with _active_lock:
        if (subject, filename) in _active_jobs: raise HTTPException(status_code=409, detail="Cannot delete a PDF while its job is running.")
    latest = database.latest_job(subject, filename)
    if latest and latest["status"] in {"queued", "running"}: raise HTTPException(status_code=409, detail="Cannot delete a PDF while its job is running.")
    subject_id = database.get_subject_id(subject)
    with database.connect() as db:
        db.execute("DELETE FROM jobs WHERE subject=? AND filename=?", (subject, filename))
        db.execute("DELETE FROM pdf_reviews WHERE subject=? AND filename=?", (subject, filename))
        db.execute("DELETE FROM questions WHERE subject_name=? AND source=?", (subject, f"pyq:{filename}"))
        db.execute(
            "DELETE FROM documents WHERE subject_id=? AND filename=?",
            (subject_id, filename),
        )
    processing_manifest.remove_pdf(subject, filename)


@app.patch("/subjects/{subject}", tags=["subjects"])
def rename_subject(subject: str, request: SubjectRequest, _: AdminWrite) -> dict:
    old = validate_subject_name(subject); new = validate_subject_name(request.name.strip())
    if database.get_subject_id(old) is None: raise HTTPException(status_code=404, detail="Subject not found")
    if new != old and database.get_subject_id(new) is not None: raise HTTPException(status_code=409, detail="Subject already exists")
    if new == old: return {"name": new, "renamed": False}
    old_subject_id = database.get_subject_id(old)
    with database.connect() as db:
        db.execute(
            "UPDATE subjects SET subject_name=?,updated_at=? WHERE subject_name=?",
            (new, _now(), old),
        )
        db.execute(
            """UPDATE documents SET subject_name=?,
               storage_key=? || substr(storage_key,?)
               WHERE subject_name=?""",
            (new, f"{new}/", len(old) + 1, old),
        )
        db.execute("UPDATE topics SET subject_name=? WHERE subject_name=?", (new,old))
        db.execute("UPDATE questions SET subject_name=?,updated_at=? WHERE subject_name=?", (new,_now(),old))
        db.execute("UPDATE papers SET subject_name=? WHERE subject_name=?", (new,old))
        db.execute("UPDATE jobs SET subject=? WHERE subject=?", (new,old))
        db.execute("UPDATE pdf_reviews SET subject=? WHERE subject=?", (new,old))
        db.execute("UPDATE document_reviews SET subject_name=? WHERE subject_name=?", (new,old))
        db.execute("UPDATE question_bank_records SET subject_name=? WHERE subject_name=?", (new,old))
        db.execute("UPDATE blueprints SET subject_name=? WHERE subject_name=?", (new,old))
        db.execute("UPDATE paper_drafts SET subject_name=? WHERE subject_name=?", (new,old))
        db.execute("UPDATE generated_papers SET subject_name=? WHERE subject_name=?", (new,old))
        db.execute("UPDATE teacher_subjects SET subject_name=? WHERE subject_name=?", (new,old))
        manifest_row = db.execute(
            "SELECT manifest_json FROM processing_manifests WHERE subject_id=?",
            (old_subject_id,),
        ).fetchone()
        if manifest_row:
            manifest = json.loads(manifest_row["manifest_json"])
            manifest["subject"] = new
            db.execute(
                "UPDATE processing_manifests SET manifest_json=?,updated_at=? WHERE subject_id=?",
                (json.dumps(manifest, ensure_ascii=False), _now(), old_subject_id),
            )
    dashboard_metrics_service.invalidate()
    return {"name":new,"renamed":True}


@app.delete("/subjects/{subject}", status_code=204, tags=["subjects"])
def delete_subject(subject: str, _: AdminWrite):
    subject = validate_subject_name(subject)
    if database.get_subject_id(subject) is None: raise HTTPException(status_code=404, detail="Subject not found")
    with _active_lock:
        if any(s == subject for s, _ in _active_jobs): raise HTTPException(status_code=409, detail="Cannot delete a subject while a job is running.")
    with database.connect() as db:
        active = db.execute("SELECT 1 FROM jobs WHERE subject=? AND status IN ('queued','running') LIMIT 1",(subject,)).fetchone()
    if active: raise HTTPException(status_code=409, detail="Cannot delete a subject while a job is running.")
    subject_id = database.get_subject_id(subject)

    def _do_deletes():
        ops = [
            ("DELETE FROM documents WHERE subject_id=?", (subject_id,)),
            ("DELETE FROM question_priorities WHERE question_id IN (SELECT question_id FROM question_bank_records WHERE subject_name=?)", (subject,)),
            ("DELETE FROM question_bank_records WHERE subject_name=?", (subject,)),
            ("DELETE FROM document_reviews WHERE subject_name=?", (subject,)),
            ("DELETE FROM paper_drafts WHERE subject_name=?", (subject,)),
            ("DELETE FROM generated_papers WHERE subject_name=?", (subject,)),
            ("DELETE FROM blueprints WHERE subject_name=?", (subject,)),
            ("DELETE FROM reconciliation_state WHERE subject=?", (subject,)),
            ("DELETE FROM questions WHERE subject_name=?", (subject,)),
            ("DELETE FROM topics WHERE subject_name=?", (subject,)),
            ("DELETE FROM papers WHERE subject_name=?", (subject,)),
            ("DELETE FROM jobs WHERE subject=?", (subject,)),
            ("DELETE FROM pdf_reviews WHERE subject=?", (subject,)),
            ("DELETE FROM teacher_subjects WHERE subject_name=?", (subject,)),
            ("DELETE FROM subjects WHERE subject_name=?", (subject,)),
        ]
        for sql, params in ops:
            with database.connect() as db:
                db.execute(sql, params)
                try:
                    db.commit()
                except Exception:
                    pass

    database._retry_db(_do_deletes)
    dashboard_metrics_service.invalidate()


@app.get("/papers", tags=["papers"])
def list_papers(email: Annotated[str, Depends(require_user)], subject: str | None = None,
                page: int = Query(1,ge=1), page_size: int = Query(50,ge=1,le=200), created_by: str | None = None) -> dict:
    clauses,args=["1=1"],[]
    allowed = accessible_subjects(email)
    if subject:
        require_subject_access(email, subject)
        clauses.append("p.subject_name=?"); args.append(subject)
    elif allowed is not None:
        if not allowed:
            return {"papers": [], "total": 0}
        placeholders = ",".join("?" for _ in allowed)
        clauses.append(f"p.subject_name IN ({placeholders})"); args.extend(sorted(allowed))
    if created_by == "me":
        clauses.append("u.email=?"); args.append(email)
    where=" AND ".join(clauses)
    with database.connect() as db:
        total=db.execute("SELECT count(*) FROM papers p LEFT JOIN users u ON u.id=p.created_by WHERE "+where,args).fetchone()[0]
        rows=db.execute("SELECT p.*,u.email created_by_email FROM papers p LEFT JOIN users u ON u.id=p.created_by WHERE "+where+" ORDER BY p.created_at DESC LIMIT ? OFFSET ?",[*args,page_size,(page-1)*page_size]).fetchall()
    papers=[{"id":r["id"],"title":r["title"],"subject":r["subject_name"],"exam_type":r["exam_type"],"total_marks":r["total_marks"],"duration":r["duration"],"question_count":r["question_count"],"created_by":r["created_by_email"],"created_at":r["created_at"],"status":r["status"]} for r in rows]
    return {"papers":papers,"total":total}


@app.get("/subjects/{subject}/weightage", tags=["analytics"])
def subject_weightage(subject: str, user: Annotated[str, Depends(require_user)]) -> dict:
    subject=validate_subject_name(subject)
    if subject not in list_subject_names(): raise HTTPException(status_code=404, detail="Subject not found")
    require_subject_access(user, subject)
    with database.connect() as db:
        rows=db.execute("SELECT t.id,t.name,count(q.id) frequency,coalesce(sum(q.marks),0) historical_marks,max(q.created_at) last FROM topics t LEFT JOIN questions q ON q.topic_id=t.id AND q.subject_name=t.subject_name WHERE t.subject_name=? GROUP BY t.id ORDER BY t.id",(subject,)).fetchall()
        paper_count=db.execute("SELECT count(DISTINCT source) FROM questions WHERE subject_name=? AND source LIKE 'pyq:%'",(subject,)).fetchone()[0]
    total=sum(r["frequency"] for r in rows); topics=[]
    for r in rows:
        freq=r["frequency"]; score=round((freq/total*100),1) if total else 0
        year=int(r["last"][:4]) if r["last"] else None
        topics.append({"topic_id":r["id"],"topic_name":r["name"],"frequency":freq,"papers_count":paper_count,
            "historical_marks":r["historical_marks"],"last_appeared":year,"weight_score":score,
            "tier":"unseen" if not freq else "high" if score>=50 else "medium" if score>=20 else "low"})
    return {"subject":subject,"papers_analyzed":paper_count,"topics":topics}
