import json
from pathlib import Path

from functions.QuesScrapper.ScrapePipeLIne.quesandFig import append_records, build_page_records


def make_inputs():
    pages = [
        {"page_number": 1, "width_pt": 612, "height_pt": 792, "text": "what is the derivative of x squared with respect to x"},
        {"page_number": 2, "width_pt": 612, "height_pt": 792, "text": ""},
    ]
    rendered_pages = [
        {"page_number": 1, "image_path": "out/book/pages/page_0001.png", "width_px": 1674, "height_px": 2339},
        {"page_number": 2, "image_path": "out/book/pages/page_0002.png", "width_px": 1674, "height_px": 2339},
    ]
    embedded_images = [
        {"page_number": 1, "image_path": "out/book/images/page_0001_image_1.png", "width": 8, "height": 8},
    ]
    vector_figures = [
        {"page_number": 2, "image_path": "out/book/images_vector/page_0002_drawing_1.png", "width": 700, "height": 350},
    ]
    return pages, rendered_pages, embedded_images, vector_figures


def test_build_page_records_merges_figures(tmp_path):
    pages, rendered, embedded, vector = make_inputs()

    records, manifest = build_page_records(
        tmp_path / "book.pdf",
        pages,
        rendered,
        embedded,
        vector,
        tmp_path / "out" / "book",
    )

    assert len(records) == 2

    page1 = records[0]
    assert page1["pdf_name"] == "book.pdf"
    assert page1["raw_text"] == "what is the derivative of x squared with respect to x"
    assert page1["text_character_count"] == len("what is the derivative of x squared with respect to x")
    assert page1["figure_paths"] == ["out/book/images/page_0001_image_1.png"]
    assert page1["needs_ocr_review"] is False

    page2 = records[1]
    assert page2["raw_text"] == ""
    assert page2["text_character_count"] == 0
    assert page2["figure_paths"] == ["out/book/images_vector/page_0002_drawing_1.png"]
    assert page2["needs_ocr_review"] is True

    assert manifest["source_pdf"] == "book.pdf"
    assert manifest["page_count"] == len(pages)
    assert len(manifest["pages"]) == len(pages)

    assert Path(manifest["pages"][0]["text_path"]).read_text(encoding="utf-8") == "what is the derivative of x squared with respect to x"


def test_append_records_creates_file(tmp_path):
    pages, rendered, embedded, vector = make_inputs()
    records, _ = build_page_records(tmp_path / "book.pdf", pages, rendered, embedded, vector, tmp_path / "out")
    json_file = tmp_path / "questions_data.json"

    count = append_records(records, json_file)

    assert count == 2
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data == records


def test_append_records_replaces_existing_pdf(tmp_path):
    json_file = tmp_path / "questions_data.json"
    append_records(
        [{"pdf_name": "book.pdf", "page_number": 1, "raw_text": "old", "text_character_count": 3, "figure_paths": [], "page_image": "x", "needs_ocr_review": False}],
        json_file,
    )

    count = append_records(
        [
            {"pdf_name": "book.pdf", "page_number": 1, "raw_text": "new", "text_character_count": 3, "figure_paths": [], "page_image": "x", "needs_ocr_review": False},
            {"pdf_name": "book.pdf", "page_number": 2, "raw_text": "added", "text_character_count": 5, "figure_paths": [], "page_image": "x", "needs_ocr_review": False},
        ],
        json_file,
    )

    assert count == 2
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0]["raw_text"] == "new"
    assert data[1]["page_number"] == 2


def test_append_records_sorts_and_extends_across_pdfs(tmp_path):
    json_file = tmp_path / "questions_data.json"

    record = lambda pdf_name, page_number: {
        "pdf_name": pdf_name, "page_number": page_number,
        "raw_text": "", "text_character_count": 0,
        "figure_paths": [], "page_image": "", "needs_ocr_review": True,
    }

    append_records([record("zeta.pdf", 2), record("zeta.pdf", 1)], json_file)
    append_records([record("alpha.pdf", 1)], json_file)

    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert [(d["pdf_name"], d["page_number"]) for d in data] == [
        ("alpha.pdf", 1), ("zeta.pdf", 1), ("zeta.pdf", 2),
    ]


def test_append_records_backs_up_corrupt_file(tmp_path):
    json_file = tmp_path / "questions_data.json"
    json_file.write_text("{not valid json", encoding="utf-8")

    append_records([], json_file)

    assert Path(str(json_file) + ".bak").is_file()
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data == []
