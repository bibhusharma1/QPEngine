"""Parser paths and provider selection from the root configuration."""

from pathlib import Path

from config.settings import PARSER_ROOT, RECONSTRUCT_ROOT, setting, resolve_path

_raw_manifests = setting("QUES_MANIFEST_PATH", "")
MANIFEST_PATHS = [str(resolve_path(item)) for item in _raw_manifests.split(",") if item.strip()]
QUESTIONS_JSON_FILE = setting("QUES_QUESTIONS_JSON", str(PARSER_ROOT.parent / "Scrapper"))
OUTPUT_DIR = str(PARSER_ROOT)
QUESTIONS_OUTPUT_FILE = setting("QUES_QUESTIONS_OUTPUT", "")
QUESTIONS_OUTPUT_DIR = str(resolve_path(QUESTIONS_OUTPUT_FILE).parent) if QUESTIONS_OUTPUT_FILE else OUTPUT_DIR
DOCUMENTS_DIR = str(RECONSTRUCT_ROOT)
PREFERRED_PROVIDER = setting("QUES_RECON_PROVIDER", "auto").lower()
OVERRIDE_MODEL = setting("QUES_RECON_MODEL", "") or None
QUESTIONS_JSON_FILE = str(resolve_path(QUESTIONS_JSON_FILE))
if QUESTIONS_OUTPUT_FILE:
    QUESTIONS_OUTPUT_FILE = str(resolve_path(QUESTIONS_OUTPUT_FILE))
    QUESTIONS_OUTPUT_DIR = str(Path(QUESTIONS_OUTPUT_FILE).parent)

