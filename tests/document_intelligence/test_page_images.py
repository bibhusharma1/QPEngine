import io
from pathlib import Path

import fitz
import pytest
from PIL import Image

from src.masterdeorca.document_intelligence.page_images import (
    PageImage,
    is_blank_image,
    render_page_images,
)


def _make_pdf(path: Path, pages: int = 2) -> Path:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1} content")
    doc.save(str(path))
    doc.close()
    return path


def test_render_pdf_pages_ordered(tmp_path):
    pdf = _make_pdf(tmp_path / "t.pdf", pages=3)
    pages = list(render_page_images(pdf))
    assert len(pages) == 3
    assert [p.page_number for p in pages] == [1, 2, 3]
    for p in pages:
        assert isinstance(p, PageImage)
        assert p.mime == "image/png"
        assert p.data[:8] == b"\x89PNG\r\n\x1a\n"
        assert p.width > 0 and p.height > 0


def test_render_image_passthrough(tmp_path):
    img_path = tmp_path / "a.png"
    img = Image.new("RGB", (10, 10), color=(255, 0, 0))
    img.save(img_path)
    raw = img_path.read_bytes()
    pages = list(render_page_images(img_path))
    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert pages[0].mime == "image/png"
    assert pages[0].data == raw


def test_render_jpeg_passthrough(tmp_path):
    img_path = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8), color=(0, 128, 0)).save(img_path, format="JPEG")
    raw = img_path.read_bytes()
    pages = list(render_page_images(img_path))
    assert len(pages) == 1
    assert pages[0].mime == "image/jpeg"
    assert pages[0].data == raw


def test_render_tiff_frames(tmp_path):
    img_path = tmp_path / "a.tiff"
    frames = [Image.new("RGB", (6, 6), color=(i * 40, 0, 0)) for i in range(3)]
    frames[0].save(img_path, save_all=True, append_images=frames[1:])
    pages = list(render_page_images(img_path))
    assert len(pages) == 3
    assert [p.page_number for p in pages] == [1, 2, 3]
    assert all(p.mime == "image/png" for p in pages)


def test_is_blank_image():
    blank = Image.new("L", (20, 20), color=255)
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    assert is_blank_image(buf.getvalue()) is True

    noisy = Image.new("RGB", (20, 20))
    for x in range(20):
        for y in range(20):
            noisy.putpixel((x, y), (x * 10 % 256, y * 10 % 256, 0))
    buf2 = io.BytesIO()
    noisy.save(buf2, format="PNG")
    assert is_blank_image(buf2.getvalue()) is False


def test_render_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(render_page_images(tmp_path / "nope.pdf"))


@pytest.mark.skipif(
    __import__("shutil").which("soffice") is None
    and __import__("shutil").which("libreoffice") is None,
    reason="LibreOffice not installed",
)
def test_render_docx_pages(tmp_path):
    import docx

    docx_path = tmp_path / "d.docx"
    d = docx.Document()
    d.add_paragraph("Hello DOCX")
    d.save(str(docx_path))
    pages = list(render_page_images(docx_path))
    assert len(pages) >= 1
    assert all(p.mime == "image/png" for p in pages)
    assert all(p.source_format == "docx" for p in pages)