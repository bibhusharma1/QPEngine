from __future__ import annotations

from pathlib import Path

from config import settings
from src.masterdeorca.document_intelligence.interfaces.base import ExtractedDocument
from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import NemotronParseClient
from src.masterdeorca.services.document_processor import process_document


class DocumentRouter:
    def __init__(self):
        self.mode = getattr(settings, "PARSER_MODE", "local").lower()
        self.llm_client = NemotronParseClient()

    def process(self, path: Path) -> ExtractedDocument:
        if self.mode == "llm":
            return self.llm_client.parse_document(path)
        # local mode uses existing document processor logic adapted
        # Return extracted doc via processor if possible
        try:
            res = process_document(path)
            return ExtractedDocument(
                text="",
                pages=res.get("pages") or [],
                tables=res.get("tables") or [],
                figures=[],
                questions=[],
                metadata=res,
                source_path=path,
            )
        except Exception:
            return ExtractedDocument(text="", source_path=path)


    def process_two_stage(self, path: Path) -> dict:
        if self.mode == "llm":
            from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import NemotronParseClient
            from src.masterdeorca.document_intelligence.llm.scraper_parser.structurer import QuestionStructurer
            client = NemotronParseClient()
            extracted = client.parse_document_normalized(path)
            structurer = QuestionStructurer()
            questions = structurer.structure({
                "text": extracted.text,
                "tables": extracted.tables,
                "figures": [f.__dict__ for f in extracted.figures],
                "pages": extracted.pages,
                "coordinates": extracted.metadata.get("normalized", {}).get("coordinates", []),
                "classes": extracted.metadata.get("normalized", {}).get("classes", []),
            })
            extracted.questions = questions
            return {"extracted": extracted, "questions": questions}
        return {"extracted": self.process(path), "questions": []}


class NemotronOnlyRouter:
    def __init__(self):
        self.mode = "llm"
        from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import NemotronParseClient
        from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_normalizer import NemotronNormalizer
        self.client = NemotronParseClient()
        self.normalizer = NemotronNormalizer()

    def process(self, path: Path):
        if self.mode != "llm":
            raise RuntimeError("NemotronOnlyRouter requires PARSER_MODE=llm")
        extracted = self.client.parse_document_normalized(path)
        norm = self.normalizer.normalize(extracted.metadata.get("raw", {}).get("content") or extracted.text)
        extracted.text = norm.get("text", extracted.text)
        extracted.tables = norm.get("tables") or extracted.tables
        extracted.metadata["nemotron_normalized"] = norm
        return extracted
