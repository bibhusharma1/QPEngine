"""Main orchestrator for JSON-to-DOCX question bank reconstruction."""

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Pt

from .asset_handler import AssetHandler
from .json_loader import PROJECT_ROOT, load_question_bank
from .question_renderer import QuestionRenderer
from .styles import apply_document_styles
from config.reconstruct import OUTPUT_DIR as RECON_OUTPUT_DIR, project_path

logger = logging.getLogger(__name__)


@dataclass
class GenerationResult:
    """Summary result of question bank DOCX reconstruction."""
    docx_path: Path
    total_questions: int
    total_subquestions: int
    total_assets_inserted: int
    missing_assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_missing_assets(self) -> bool:
        return len(self.missing_assets) > 0


class DocumentGenerator:
    """Reconstructs clean, editable DOCX documents from questions_parsed.json."""

    def __init__(
        self,
        json_path: str | Path | None = None,
        output_dir: str | Path | None = None,
        base_dir: str | Path | None = None,
        strict_assets: bool = False,
        question_limit: int | None = None,
    ):
        self.json_path = json_path
        self.base_dir = Path(base_dir).resolve() if base_dir else PROJECT_ROOT
        self.strict_assets = strict_assets
        self.question_limit = question_limit

        # Resolve output directory
        if output_dir:
            self.output_dir = Path(output_dir).resolve()
        else:
            parsed_path = Path(json_path).resolve() if json_path else None
            pdf_name = parsed_path.parent.name if parsed_path else "questions"
            self.output_dir = (project_path(RECON_OUTPUT_DIR) / pdf_name).resolve()

    def generate(self, filename: str | None = None) -> GenerationResult:
        """Run the document generation pipeline.
        
        Args:
            filename: Name of the output docx file. If None, defaults to
                      'questions_reconstructed.docx'.
        
        Returns:
            GenerationResult containing metrics and output path.
        """
        # 1. Load questions JSON
        questions, raw_dict = load_question_bank(self.json_path)
        if self.question_limit is not None:
            if self.question_limit < 1:
                raise ValueError("question_limit must be a positive integer")
            questions = questions[: self.question_limit]

        # Context dir is the parent folder of the resolved JSON file
        json_file_path = getattr(raw_dict, "_path", None) or self.json_path
        context_dir = Path(json_file_path).parent if json_file_path else None

        # 2. Initialize DOCX and styling
        doc = Document()
        apply_document_styles(doc)

        # 3. Initialize AssetHandler and QuestionRenderer
        asset_handler = AssetHandler(
            base_dir=self.base_dir,
            context_dir=context_dir,
            strict=self.strict_assets,
        )
        renderer = QuestionRenderer(asset_handler=asset_handler)

        # 4. Render questions in original order
        for q in questions:
            renderer.render_question(doc, q)

        # Keep document-wide accuracy context and source references after
        # the reconstructed paper so the question text remains uncluttered.
        doc.add_page_break()
        doc.add_heading("Processing accuracy notice", level=1)
        doc.add_paragraph(
            "OCR and parsing have a working accuracy target of approximately 85–90%; "
            "this is a target, not a guaranteed or independently verified result. "
            "OCR errors, incorrect question segmentation, missing metadata, and "
            "formatting issues may require manual review."
        )
        if renderer.source_references:
            doc.add_heading("Source references", level=1)
            for reference in renderer.source_references:
                paragraph = doc.add_paragraph(reference, style="List Bullet")
                paragraph.paragraph_format.space_after = Pt(1)
                for run in paragraph.runs:
                    run.font.size = Pt(8)

        # 5. Determine destination path and save
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_name = filename or "questions_reconstructed.docx"
        if not out_name.endswith(".docx"):
            out_name += ".docx"
        output_file_path = self.output_dir / out_name

        doc.save(str(output_file_path))

        # 6. Verify generated file can be reopened
        self._verify_file_integrity(output_file_path)

        # 7. Collect and return results
        all_warnings = list(asset_handler.warnings)
        return GenerationResult(
            docx_path=output_file_path,
            total_questions=renderer.rendered_questions_count,
            total_subquestions=renderer.rendered_subquestions_count,
            total_assets_inserted=asset_handler.inserted_count,
            missing_assets=asset_handler.missing_assets,
            warnings=all_warnings,
        )

    @staticmethod
    def _verify_file_integrity(file_path: Path) -> None:
        """Verify the generated DOCX file can be reopened and read."""
        if not file_path.is_file():
            raise FileNotFoundError(f"Generated DOCX does not exist on disk: {file_path}")
        try:
            reopened = Document(str(file_path))
            # Basic sanity check
            if len(reopened.paragraphs) == 0:
                raise ValueError("Generated document contains no paragraphs.")
        except Exception as e:
            raise RuntimeError(f"Generated DOCX file failed validation on reopen: {e}") from e

