from pathlib import Path

import pymupdf
import pytest

from functions.QuesScrapper.ScrapethePDF.image_extract import extract_embedded_images

from tests.Scrapper.helpers import make_pdf


def test_extract_embedded_images_finds_inserted_image(tmp_path):
    pdf = make_pdf(
        tmp_path / "sample.pdf",
        ["page one", "page two"],
        with_image_on_page=0,
    )
    output_dir = tmp_path / "images"

    images = extract_embedded_images(pdf, output_dir)

    assert len(images) == 1
    assert images[0]["page_number"] == 1
    assert images[0]["width"] == 8
    assert images[0]["height"] == 8
    assert Path(images[0]["image_path"]).is_file()


def test_extract_embedded_images_empty_pdf(tmp_path):
    pdf = make_pdf(tmp_path / "sample.pdf", ["just text"])

    images = extract_embedded_images(pdf, tmp_path / "images")

    assert images == []


def test_fallback_renders_image_when_stream_extraction_fails(tmp_path):
    """A broken stream must not lose the clear image — pass 2 clips it."""
    pdf = make_pdf(
        tmp_path / "sample.pdf",
        ["page one"],
        with_image_on_page=0,
    )

    original_extract = pymupdf.Document.extract_image
    calls = {"n": 0}

    def failing_extract_image(self, xref):
        calls["n"] += 1
        raise RuntimeError("stream corrupt")

    pymupdf.Document.extract_image = failing_extract_image
    try:
        images = extract_embedded_images(pdf, tmp_path / "images")
    finally:
        pymupdf.Document.extract_image = original_extract

    assert calls["n"] == 1
    assert len(images) == 1, "fallback render must save the image anyway"
    assert Path(images[0]["image_path"]).is_file()
    assert images[0]["width"] > 0


def test_image_nested_in_form_xobject_is_found(tmp_path):
    """Image hidden inside a page-in-page Form XObject must be extracted."""
    src = pymupdf.open()
    src_page = src.new_page()
    src_page.insert_image(
        pymupdf.Rect(10, 10, 110, 110),
        pixmap=pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), 0),
    )
    src.save(tmp_path / "src.pdf")
    src.close()

    doc = pymupdf.open()
    page = doc.new_page()
    page.show_pdf_page(page.rect, pymupdf.open(tmp_path / "src.pdf"), 0)
    doc.save(tmp_path / "shown.pdf")
    doc.close()

    images = extract_embedded_images(tmp_path / "shown.pdf", tmp_path / "images")

    assert len(images) == 1
    assert Path(images[0]["image_path"]).is_file()


def test_same_image_placed_twice_on_one_page(tmp_path):
    """One image placed twice is saved twice (each placement), not zero."""
    doc = pymupdf.open()
    page = doc.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), 0)
    page.insert_image(pymupdf.Rect(10, 10, 90, 90), pixmap=pixmap)
    page.insert_image(pymupdf.Rect(100, 10, 180, 90), pixmap=pixmap)
    doc.save(tmp_path / "twice.pdf")
    doc.close()

    images = extract_embedded_images(tmp_path / "twice.pdf", tmp_path / "images")

    assert len(images) == 2
    assert all(Path(image["image_path"]).is_file() for image in images)


@pytest.mark.parametrize("fmt", ["png", "jpeg"])
def test_formats(tmp_path, fmt):
    """Both png and jpeg encodings come out as readable image files."""
    doc = pymupdf.open()
    page = doc.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), 0)
    page.insert_image(pymupdf.Rect(5, 5, 105, 105), pixmap=pixmap)
    doc.save(tmp_path / f"sample_{fmt}.pdf")
    doc.close()

    images = extract_embedded_images(tmp_path / f"sample_{fmt}.pdf", tmp_path / f"out_{fmt}")

    assert len(images) == 1
    assert Path(images[0]["image_path"]).is_file()
