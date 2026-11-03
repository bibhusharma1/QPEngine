from functions.QuesScrapper.ScrapethePDF.pdf_read import extract_pdf_text

from tests.Scrapper.helpers import make_pdf
import pymupdf


def test_extract_pdf_text_roundtrip(tmp_path):
    pdf = make_pdf(
        tmp_path / "sample.pdf",
        ["first page text", "second page text"],
    )

    pages = extract_pdf_text(pdf)

    assert len(pages) == 2
    assert pages[0]["page_number"] == 1
    assert pages[1]["page_number"] == 2
    assert "first page text" in pages[0]["text"]
    assert "second page text" in pages[1]["text"]
    assert pages[0]["width_pt"] > 0
    assert pages[0]["height_pt"] > 0


def test_extract_pdf_text_missing_file(tmp_path):
    try:
        extract_pdf_text(tmp_path / "missing.pdf")
    except Exception as exc:
        assert type(exc).__name__ in ("FileNotFoundError", "ValueError", "RuntimeError")
    else:
        raise AssertionError("expected extraction to fail for missing file")


def test_extract_pdf_text_keeps_structured_table_and_layout(tmp_path):
    pdf_path = tmp_path / "table.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=260)
    xs = [40, 150, 260, 360]
    ys = [40, 80, 120, 160, 200, 240]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), color=(0, 0, 0), width=1)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), color=(0, 0, 0), width=1)
    values = [
        ["Process", "Arrival", "Burst"],
        ["P1", "0", "7"], ["P2", "2", "4"],
        ["P3", "4", "1"], ["P4", "5", "4"],
    ]
    for row, y in zip(values, ys[:-1]):
        for col, x in zip(row, xs[:-1]):
            page.insert_text((x + 8, y + 25), col, fontsize=11)
    doc.save(pdf_path)
    doc.close()

    extracted = extract_pdf_text(pdf_path)[0]
    assert extracted["tables"][0]["headers"] == ["Process", "Arrival", "Burst"]
    assert extracted["tables"][0]["rows"] == [["P1", "0", "7"], ["P2", "2", "4"], ["P3", "4", "1"], ["P4", "5", "4"]]
    assert extracted["layout_blocks"]
