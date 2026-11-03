"""Versioned Pydantic schema for the marks-indexed question bank.

Every question record has a stable ID, source references, marks, hierarchy,
associated assets, confidence, and review flags. The top-level QuestionBank
wraps the list with schema version and processing metadata.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


SCHEMA_VERSION = "1.1.0"


class ReviewStatus(str, Enum):
    """Human review disposition of a question record."""
    PENDING = "pending"
    APPROVED = "approved"
    FLAGGED = "flagged"
    INCOMPLETE = "incomplete"


class AssetReference(BaseModel):
    """A figure, table, graph, equation image, or diagram linked to a question."""
    asset_id: str = Field(description="Unique asset identifier")
    asset_type: str = Field(description="One of: image, table, graph, equation, diagram, figure")
    path: str | None = Field(default=None, description="Filesystem path relative to extraction root")
    source_page: int | None = Field(default=None, description="Page the asset was extracted from")
    description: str | None = Field(default=None, description="AI-generated or OCR-derived caption")
    verified: bool = Field(default=False, description="Whether the asset file exists and is readable")


class TableReference(BaseModel):
    """Structured table data extracted from a source page."""
    table_id: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    cells: list[list[str]] = Field(default_factory=list)
    source_page: int | None = None
    bbox: list[float] = Field(default_factory=list)
    extraction_method: str | None = None


class SubQuestion(BaseModel):
    """A subpart of a main question (e.g., nested short questions or sub-parts)."""
    sub_id: str = Field(description="Stable subquestion ID")
    original_text: str = Field(description="Verbatim text from the source")
    cleaned_text: str = Field(default="", description="Cleaned/normalized question text without printed labels")
    # Original numbering/labels preserved as metadata (not mixed into text)
    original_label: str | None = Field(default=None, description="Original subquestion label from source (e.g., 'a)', '(i)')")
    original_question_number: str | None = Field(default=None, description="Original question/subquestion number/reference from source")
    parent_question_ref: str | None = Field(default=None, description="Parent question reference")
    marks: float | None = Field(default=None, description="Marks for this subpart, if separately stated")
    marks_notation: str | None = Field(default=None, description="Original marks string (e.g., '[2]', '2M')")
    assets: list[AssetReference] = Field(default_factory=list)
    tables: list[TableReference] = Field(default_factory=list)
    sub_questions: list[SubQuestion] = Field(default_factory=list, description="Nested sub-parts")
    source_pdf: str | None = Field(default=None, description="Source PDF filename/path")
    source_pages: list[int] = Field(default_factory=list, description="Page numbers where this subquestion appears")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class QuestionRecord(BaseModel):
    """A single identifiable question from an examination paper."""
    question_id: str = Field(description="Stable unique ID (deterministic from source)")
    original_text: str = Field(description="Verbatim question text from extraction")
    cleaned_text: str = Field(default="", description="Cleaned/normalized version without printed labels")
    # Original numbering/labels preserved as metadata (not mixed into text)
    original_label: str | None = Field(default=None, description="Original question label from source (e.g., 'Q1', '1.')")
    original_question_number: str | None = Field(default=None, description="Original question number/reference from source")
    parent_question_ref: str | None = Field(default=None, description="Parent question reference if any")

    # Marks — primary organization axis
    marks: float | None = Field(default=None, description="Total marks for this question")
    marks_notation: str | None = Field(default=None, description="Original marks string (e.g., '[5]', '5M', '2 x 10')")
    marks_components: list[str] = Field(default_factory=list, description="Broken-down marks parts if composite")
    marks_notation_preserved: bool = Field(default=True, description="Whether marks notation like '2 × 10' is preserved separately")

    # Hierarchy
    parent_question_id: str | None = Field(default=None, description="ID of parent question if this is a subpart")
    sub_questions: list[SubQuestion] = Field(default_factory=list)

    # Section and structure metadata (preserved for paper organization)
    section_name: str | None = Field(default=None, description="Section/Part name (e.g., 'Part I', 'Section A')")
    section_instructions: str | None = Field(default=None, description="Instructions for the section")
    is_compulsory: bool | None = Field(default=None, description="Whether the question is compulsory")

    # Source metadata
    source_pdf: str = Field(description="Source PDF filename/path")
    source_pages: list[int] = Field(default_factory=list, description="Page numbers where question appears")

    # Assets
    assets: list[AssetReference] = Field(default_factory=list)
    tables: list[TableReference] = Field(default_factory=list)

    # Quality / review
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="AI confidence in extraction accuracy")
    validation_issues: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = Field(default=ReviewStatus.PENDING)
    processing_notes: list[str] = Field(default_factory=list, description="Any warnings from extraction")

    @field_validator("marks", mode="before")
    @classmethod
    def coerce_marks(cls, v: Any) -> float | None:
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip().strip("[]()").strip()
            mult = re.match(r"^(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)$", s)
            if mult:
                return float(mult.group(1)) * float(mult.group(2))
            try:
                return float(s)
            except ValueError:
                try:
                    return sum(float(x.strip()) for x in s.split("+"))
                except ValueError:
                    return None
        return float(v)


class StructuredQuestionRecord(QuestionRecord):
    """A question successfully structured and validated by the LLM."""

    status: Literal["structured"] = "structured"


class UnstructuredQuestionRecord(BaseModel):
    """Source-preserving question record when LLM structuring failed."""

    question_id: str
    status: Literal["unstructured"] = "unstructured"
    original_text: str
    cleaned_text: str | None = None
    original_label: str | None = None
    original_question_number: str | None = None
    parent_question_ref: str | None = None
    marks: float | None = None
    marks_notation: str | None = None
    marks_components: list[str] = Field(default_factory=list)
    marks_notation_preserved: bool = True
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    section_name: str | None = None
    section_instructions: str | None = None
    is_compulsory: bool | None = None
    source_pdf: str
    source_pages: list[int] = Field(default_factory=list)
    assets: list[AssetReference] = Field(default_factory=list)
    tables: list[TableReference] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    validation_issues: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.INCOMPLETE
    processing_notes: list[str] = Field(default_factory=list)
    failure_reason: Literal[
        "token_limit",
        "invalid_json",
        "api_error",
        "response_format",
        "schema_validation",
        "empty_response",
        "structuring_error",
    ]
    structuring_error: str

    @field_validator("marks", mode="before")
    @classmethod
    def coerce_marks(cls, value: Any) -> float | None:
        return QuestionRecord.coerce_marks(value)


QuestionEntry = Annotated[
    Union[StructuredQuestionRecord, UnstructuredQuestionRecord],
    Field(discriminator="status"),
]


def generate_question_id(source_pdf: str, text_or_key: str, page: int | None = None) -> str:
    """Create a deterministic, stable question ID from source info."""
    clean_key = " ".join(str(text_or_key).split()[:15])
    raw = f"{source_pdf}::{clean_key}::page{page or 0}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ProcessingMetadata(BaseModel):
    """Metadata about the extraction/processing run."""
    processed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    processor_version: str = Field(default=SCHEMA_VERSION)
    providers_used: list[str] = Field(default_factory=list)
    gpu_device: str | None = Field(default=None)
    total_pages_processed: int = Field(default=0)
    total_questions_found: int = Field(default=0)
    total_assets_linked: int = Field(default=0)
    total_tables_linked: int = Field(default=0)
    incomplete_records: int = Field(default=0)
    errors: list[str] = Field(default_factory=list)
    source_manifests: list[str] = Field(default_factory=list)


class QuestionBank(BaseModel):
    """Top-level output: a validated, versioned question bank."""
    schema_version: str = Field(default=SCHEMA_VERSION)
    metadata: ProcessingMetadata = Field(default_factory=ProcessingMetadata)
    questions: list[QuestionEntry] = Field(default_factory=list)

    @field_validator("questions", mode="before")
    @classmethod
    def type_question_states(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        typed = []
        for item in value:
            if isinstance(item, StructuredQuestionRecord | UnstructuredQuestionRecord):
                typed.append(item)
                continue
            if isinstance(item, QuestionRecord):
                item = item.model_dump(mode="python")
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("status", "structured")
            typed.append(item)
        return typed

    def by_marks(self) -> dict[float | None, list[QuestionEntry]]:
        """Group questions by their marks value — primary organization axis."""
        groups: dict[float | None, list[QuestionEntry]] = {}
        for q in self.questions:
            groups.setdefault(q.marks, []).append(q)
        return dict(sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))

    def by_section(self) -> dict[str | None, list[QuestionEntry]]:
        """Group questions by section name."""
        groups: dict[str | None, list[QuestionEntry]] = {}
        for q in self.questions:
            groups.setdefault(q.section_name, []).append(q)
        return groups

    def by_source(self) -> dict[str, list[QuestionEntry]]:
        """Group questions by source PDF."""
        groups: dict[str, list[QuestionEntry]] = {}
        for q in self.questions:
            groups.setdefault(q.source_pdf, []).append(q)
        return groups

    def incomplete(self) -> list[QuestionEntry]:
        """Return questions needing review or with issues."""
        return [q for q in self.questions if q.review_status in (ReviewStatus.FLAGGED, ReviewStatus.INCOMPLETE) or q.validation_issues]

    def to_output_dict(self) -> dict:
        """Serialize for JSON output with marks-wise grouping included."""
        base = self.model_dump(mode="json")
        # Add marks_index as a convenience view (not duplicating records, just IDs)
        marks_index: dict[str, list[str]] = {}
        for marks_val, qs in self.by_marks().items():
            key = str(marks_val) if marks_val is not None else "unspecified"
            marks_index[key] = [q.question_id for q in qs]
        base["marks_index"] = marks_index
        return base
