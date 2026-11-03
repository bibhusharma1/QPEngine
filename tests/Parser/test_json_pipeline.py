import json
from pathlib import Path

from docx import Document

from functions.QuesParser.ParsePipeLIne import json_pipeline


def test_legacy_docx_pipeline_creates_one_document_per_pdf(tmp_path, monkeypatch):
    dataset = tmp_path / "questions_data.json"
    image = tmp_path / "figure.png"
    image.write_bytes(b"not-an-image")
    dataset.write_text(json.dumps([
        {"pdf_name": "paper-one.pdf", "page_number": 2,
         "raw_text": "Q2. Second page text", "figure_paths": [str(image)]},
        {"pdf_name": "paper-one.pdf", "page_number": 1,
         "raw_text": "Q1. First page text", "figure_paths": []},
        {"pdf_name": "paper-two.pdf", "page_number": 1,
         "raw_text": "Another paper", "figure_paths": []},
    ]), encoding="utf-8")
    output = tmp_path / "docs"
    monkeypatch.setattr(json_pipeline.config, "DOCUMENTS_DIR", str(output))

    monkeypatch.setattr(json_pipeline, "_arrange_with_model", lambda text: None)
    result = json_pipeline.run_legacy_docx(dataset)

    assert len(result["documents"]) == 2
    one = Document(output / "paper-one" / "paper-one.docx")
    text = "\n".join(paragraph.text for paragraph in one.paragraphs)
    assert text.index("Page 1") < text.index("Page 2")
    assert "Q1. First page text" in text
    assert "Q2. Second page text" in text
    assert "not-an-image" not in text


def test_missing_scraper_json_fails_clearly(tmp_path):
    try:
        json_pipeline.run_legacy_docx(tmp_path / "missing.json")
    except FileNotFoundError as exc:
        assert "Scraper JSON not found" in str(exc)
    else:
        raise AssertionError("missing JSON should fail")

