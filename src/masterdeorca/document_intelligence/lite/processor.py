from __future__ import annotations

import logging
from pathlib import Path

from config import settings
from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import NemotronParseClient
from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_normalizer import NemotronNormalizer

logger = logging.getLogger(__name__)


class LiteProcessor:
    def __init__(self):
        if getattr(settings, "PARSER_MODE", "local").lower() != "llm":
            raise RuntimeError("LiteProcessor only available in PARSER_MODE=llm")
        self.client = NemotronParseClient()
        self.normalizer = NemotronNormalizer()

    def process(self, path: Path):
        from src.masterdeorca.document_intelligence.lite.two_stage import LiteTwoStageProcessor
        proc = LiteTwoStageProcessor()
        extracted, bank_data = proc.process(path)
        return extracted

    def process_to_parsed_json(self, path: Path, output_dir: Path):
        from src.masterdeorca.document_intelligence.lite.two_stage import LiteTwoStageProcessor
        proc = LiteTwoStageProcessor()
        extracted, _ = proc.process(path, output_dir=output_dir)
        return Path(extracted.metadata["parsed_json"])
