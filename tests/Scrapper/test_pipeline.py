import json
from pathlib import Path

import pytest

import functions.QuesScrapper.ScrapePipeLIne.pipeline as pipeline
import functions.QuesScrapper.ScrapePipeLIne.processPDF as process_pdf_module

from tests.Scrapper.helpers import make_pdf


@pytest.fixture()
def batch_env(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    monkeypatch.setattr(pipeline, "INPUT_DIRS", [str(input_dir)])
    monkeypatch.setattr(pipeline, "INPUT_PDFS", [])
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", str(tmp_path / "out"))

    return input_dir, tmp_path / "out"


def test_processes_all_pdfs_without_collisions(batch_env):
    input_dir, output_dir = batch_env
    make_pdf(input_dir / "bookA.pdf", ["alpha one", "alpha two"], with_image_on_page=0)
    make_pdf(input_dir / "bookB.pdf", ["beta one", "beta two"], with_drawing_on_page=1)

    pipeline.run()

    data = [record for path in sorted(output_dir.glob("*/questions_scrapper.json")) for record in json.loads(path.read_text(encoding="utf-8"))]
    assert len(data) == 4

    pdf_names = {record["pdf_name"] for record in data}
    assert pdf_names == {"bookA.pdf", "bookB.pdf"}

    for record in data:
        stem = Path(record["pdf_name"]).stem
        for figure_path in record["figure_paths"]:
            assert figure_path.startswith(str(output_dir / stem))
        assert record["page_image"].startswith(str(output_dir / stem))
    assert (output_dir / "bookA" / "manifest.json").is_file()
    assert (output_dir / "bookB" / "manifest.json").is_file()


def test_rerun_replaces_records_not_duplicates(batch_env):
    input_dir, output_dir = batch_env
    make_pdf(input_dir / "bookA.pdf", ["alpha one", "alpha two"])

    pipeline.run()
    pipeline.run()

    data = json.loads((output_dir / "bookA" / "questions_scrapper.json").read_text(encoding="utf-8"))
    assert len(data) == 2


def test_broken_pdf_does_not_abort_batch(batch_env):
    input_dir, output_dir = batch_env
    make_pdf(input_dir / "bookA.pdf", ["alpha one"])
    (input_dir / "broken.pdf").write_bytes(b"%PDF-1.4 not a real pdf")

    with pytest.raises(SystemExit):
        pipeline.run()

    data = json.loads((output_dir / "bookA" / "questions_scrapper.json").read_text(encoding="utf-8"))
    assert [record["pdf_name"] for record in data] == ["bookA.pdf"]


def test_empty_input_folder_exits(batch_env):
    with pytest.raises(SystemExit) as excinfo:
        pipeline.run()
    assert excinfo.value.code == 1


def test_explicit_pdf_list_reads_only_listed_files(batch_env, monkeypatch):
    input_dir, output_dir = batch_env
    pdf_one = make_pdf(input_dir / "bookOne.pdf", ["one"])
    make_pdf(input_dir / "bookTwo.pdf", ["two"])

    # Folder still configured; the explicit list must take precedence.
    monkeypatch.setattr(pipeline, "INPUT_DIRS", [str(input_dir)])
    monkeypatch.setattr(pipeline, "INPUT_PDFS", [str(pdf_one)])

    pipeline.run()

    data = json.loads((output_dir / "bookOne" / "questions_scrapper.json").read_text(encoding="utf-8"))
    assert [record["pdf_name"] for record in data] == ["bookOne.pdf"]


def test_multiple_input_dirs_are_scanned(batch_env, tmp_path, monkeypatch):
    input_dir, output_dir = batch_env
    second_dir = tmp_path / "more-PDFs"
    second_dir.mkdir()
    make_pdf(input_dir / "bookOne.pdf", ["one"])
    make_pdf(second_dir / "bookTwo.pdf", ["two"])

    monkeypatch.setattr(pipeline, "INPUT_DIRS", [str(input_dir), str(second_dir)])

    pipeline.run()

    data = [record for path in sorted(output_dir.glob("*/questions_scrapper.json")) for record in json.loads(path.read_text(encoding="utf-8"))]
    assert {record["pdf_name"] for record in data} == {"bookOne.pdf", "bookTwo.pdf"}


def test_scanned_pages_use_ocr_text(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"placeholder")
    page_image = tmp_path / "page.png"
    page_image.write_bytes(b"png")
    monkeypatch.setattr(process_pdf_module, "extract_pdf_text", lambda _path: [
        {"page_number": 1, "width_pt": 612, "height_pt": 792, "text": ""},
    ])
    monkeypatch.setattr(process_pdf_module, "render_pdf", lambda *_a, **_k: [
        {"page_number": 1, "image_path": str(page_image), "width_px": 100, "height_px": 100},
    ])
    monkeypatch.setattr(process_pdf_module, "extract_embedded_images", lambda *_a: [])
    monkeypatch.setattr(process_pdf_module, "extract_vector_figures", lambda *_a: [])
    monkeypatch.setattr(process_pdf_module, "run_ocr", lambda paths: [
        {"page_number": 1, "blocks": [
            {"text": "Q1(a) Define an operating system. [5]"},
            {"text": "Q1(b) Explain process scheduling. [5]"},
        ], "mean_confidence": 0.93},
    ])
    monkeypatch.setattr(process_pdf_module, "save_manifest", lambda *_a: None)

    records, _ = process_pdf_module.process_pdf(pdf, tmp_path / "out")

    assert records[0]["raw_text"] == (
        "Q1(a) Define an operating system. [5]\n"
        "Q1(b) Explain process scheduling. [5]"
    )
    assert records[0]["text_character_count"] == len(records[0]["raw_text"])
    assert records[0]["needs_ocr_review"] is True
    assert records[0]["ocr_confidence"] == 0.93
    assert (tmp_path / "out/text/page_0001.txt").read_text() == records[0]["raw_text"]
