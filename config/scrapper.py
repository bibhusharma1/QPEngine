"""Shared scraper paths backed by the root configuration."""

from config.settings import PROJECT_ROOT, SCRAPPER_ROOT, setting, resolve_path

_raw_dirs = setting("QUES_INPUT_DIRS", "")
INPUT_DIRS = [str(resolve_path(part)) for part in _raw_dirs.split(",") if part.strip()]
if not INPUT_DIRS:
    INPUT_DIRS = [str(resolve_path(setting("QUES_INPUT_PDF_FOLDER", "data/input")))]
INPUT_PDFS = [str(resolve_path(part)) for part in setting("QUES_INPUT_PDFS", "").split(",") if part.strip()]
OUTPUT_DIR = str(SCRAPPER_ROOT)
QUESTIONS_JSON_FILE = setting("QUES_QUESTIONS_JSON", OUTPUT_DIR)
