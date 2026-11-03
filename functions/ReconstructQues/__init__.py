"""ReconstructQues: JSON-to-DOCX Question Bank Reconstructor.

Self-contained module to rebuild formatted Word (.docx) examination
question banks from structured questions_parsed.json files without
running OCR, AI providers, or question extraction pipelines.
"""

from .generator import DocumentGenerator, GenerationResult
from .json_loader import load_question_bank

__all__ = ["DocumentGenerator", "GenerationResult", "load_question_bank"]
__version__ = "1.0.0"

