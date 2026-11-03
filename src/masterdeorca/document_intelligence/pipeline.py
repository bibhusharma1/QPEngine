from __future__ import annotations

from pathlib import Path

from src.masterdeorca.document_intelligence.interfaces.base import ExtractedDocument
from src.masterdeorca.document_intelligence.routing.router import DocumentRouter


class DocumentIntelligencePipeline:
    def __init__(self):
        self.router = DocumentRouter()

    def process(self, path: Path) -> ExtractedDocument:
        return self.router.process(path)


pipeline = DocumentIntelligencePipeline()
