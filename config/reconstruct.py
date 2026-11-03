"""Reconstruction configuration backed by the project-root .env."""

from config.settings import DEFAULT_SUBJECT, PROJECT_ROOT, RECONSTRUCT_ROOT, STORAGE_ROOT, setting, resolve_path

INPUT_JSON = setting("QUES_RECON_INPUT_JSON", "")
SOURCE_PDF_DIR = setting("QUES_RECON_SOURCE_PDF_DIR", str(STORAGE_ROOT / DEFAULT_SUBJECT / "source"))
SAMPLE_PDF = setting("QUES_RECON_SAMPLE_PDF", "")
SAMPLE_COUNT = int(setting("QUES_RECON_SAMPLE_COUNT", "5"))
SAMPLE_SEED = setting("QUES_RECON_SAMPLE_SEED", "")
OUTPUT_DIR = str(RECONSTRUCT_ROOT)


def project_path(value):
    return resolve_path(value)
