# Typed records shared by the OCR/cleanup pipeline (Pydantic).
#
# The LangGraph nodes pass unstructured dicts in the graph state (fast,
# easy to route), but every externally shaped artifact — OCR blocks,
# question candidates, asset associations, persisted bank rows, model
# output — is validated against a schema in THIS file first. Model
# responses in particular are untrusted until they parse cleanly.

from pydantic import BaseModel, Field

# Confidence (0..1) below which an OCR block is flagged for review.
OCR_REVIEW_THRESHOLD = 0.85

# Marks notation a question may carry, e.g. "[3]", "[0.5+1+0.5]".
MARKS_STATUSES = ("explicit", "ambiguous", "missing", "conflicting")

REVIEW_STATUSES = ("clean", "low_ocr_confidence", "ambiguous_marks",
                   "unassigned_assets", "model_output_invalid")


class OcrBlock(BaseModel):
    """One OCR text unit positioned on a rendered page image."""

    page_number: int
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    # Bounding box in rendered-page pixels (x0, y0, x1, y1), (0,0) top-left.
    bbox: tuple[int, int, int, int]
    kind: str = "text"  # "text" | "title" | "table" | "figure" | ...

    @property
    def x0(self):
        return self.bbox[0]

    @property
    def y0(self):
        return self.bbox[1]

    @property
    def x1(self):
        return self.bbox[2]

    @property
    def y1(self):
        return self.bbox[3]


class PageOcr(BaseModel):
    """OCR result of one rendered page image."""

    page_number: int
    image_path: str
    blocks: list[OcrBlock]
    # Page-level aggregated mean confidence over text blocks.
    mean_confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool  # low mean confidence or OCR raised suspicions


class AssetInfo(BaseModel):
    """One visual asset (embedded picture or vector-drawn figure)."""

    asset_id: str                      # stable: sha1(path)[:16]
    path: str
    page_number: int
    kind: str                          # "raster" | "vector"
    width: int = 0
    height: int = 0
    # Where the asset sits on the page in RENDERED pixel coordinates
    # (needs render dpi scaling) — None when unknown.
    bbox: tuple[int, int, int, int] | None = None


class AssetAssignment(BaseModel):
    """Result of linking one asset to a (sub)question."""

    asset_id: str
    question_id: str | None            # None = unassigned
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = []           # why: mentions / proximity / ...
    essential_for_understanding: bool


class QuestionCandidate(BaseModel):
    """A segmented question/subquestion BEFORE cleaning & persistence.

    Produced by the deterministic segmenter; the metadata/normalise
    nodes enrich it; validation may flag it.
    """

    question_id: str                   # deterministic id (pdf+label+text)
    pdf_name: str
    page_numbers: list[int]
    label: str                         # original, e.g. "Q.2(c)"
    parent_id: str | None = None
    text: str                          # assembled question statement
    marks: float | None = None
    marks_status: str = "missing"
    marks_notation: str | None = None  # original, e.g. "[0.5+1+0.5]"
    course_outcome: str | None = None
    bloom_level: str | None = None
    topic: str | None = None
    question_type: str | None = None
    text_characters: int = 0
    ocr_confidence: float | None = None
    review_status: str = "clean"
    asset_ids: list[str] = []
    validation_issues: list[str] = []


class ValidationIssue(BaseModel):
    scope: str          # e.g. "marks_total", "segmentation", "asset"
    severity: str       # "info" | "warning" | "error"
    pdf_name: str | None = None
    question_id: str | None = None
    message: str


class BankRecord(BaseModel):
    """One persisted row of the cleaned question bank.

    Unknown values stay None — never invented. `raw_text_kept` keeps
    the verbatim source wording next to `text` for traceability.
    """

    question_id: str = Field(min_length=4)
    pdf_name: str
    page_numbers: list[int]
    label: str
    parent_id: str | None = None
    text: str
    raw_text_kept: str
    marks: float | None = None
    marks_status: str = "missing"
    marks_notation: str | None = None
    course_outcome: str | None = None
    bloom_level: str | None = None
    topic: str | None = None
    question_type: str | None = None
    asset_ids: list[str] = []
    ocr_confidence: float | None = None
    review_status: str = "clean"
    validation_issues: list[str] = []
    cleaned_by: str = "heuristic"      # "heuristic" | "llm-assisted"
