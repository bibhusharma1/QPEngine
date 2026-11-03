from pathlib import Path

import pymupdf

from functions.QuesScrapper.ScrapethePDF.vector_figures import extract_vector_figures

from tests.Scrapper.helpers import make_pdf


def test_extract_vector_figures_picks_up_drawing(tmp_path):
    pdf = make_pdf(
        tmp_path / "sample.pdf",
        ["text page one", "text page two"],
        with_drawing_on_page=0,
    )

    figures = extract_vector_figures(pdf, tmp_path / "images_vector")

    assert len(figures) == 1
    assert figures[0]["page_number"] == 1
    assert figures[0]["width"] > 0
    assert figures[0]["height"] > 0

    figure_path = Path(figures[0]["image_path"])
    assert figure_path.is_file()
    assert figure_path.suffix == ".png"


def test_extract_vector_figures_skips_text_only_pdf(tmp_path):
    pdf = make_pdf(tmp_path / "sample.pdf", ["plain text"])

    figures = extract_vector_figures(pdf, tmp_path / "images_vector")

    assert figures == []


def test_separate_diagrams_on_one_page_make_separate_pictures(tmp_path):
    """Two diagrams far apart are two figures, not one merged blob."""
    doc = pymupdf.open()
    page = doc.new_page()
    # Figure A: box at the top-left area.
    page.draw_rect(pymupdf.Rect(72, 72, 180, 130))
    page.draw_line(pymupdf.Point(72, 130), pymupdf.Point(180, 72))
    # Figure B: circle far away at the bottom-right area.
    page.draw_circle(pymupdf.Point(480, 680), 40)
    doc.save(tmp_path / "two_diagrams.pdf")
    doc.close()

    figures = extract_vector_figures(tmp_path / "two_diagrams.pdf", tmp_path / "images_vector")

    assert len(figures) == 2
    assert {Path(fig["image_path"]).name for fig in figures} == {
        "page_0001_drawing_1.png",
        "page_0001_drawing_2.png",
    }
    for figure in figures:
        assert Path(figure["image_path"]).is_file()


def test_page_border_does_not_kill_nearby_figure(tmp_path):
    """A decorative full-page frame must not swallow a real figure."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    # Decorative border right at the page edge (covers ~everything).
    page.draw_rect(page.rect + (2, 2, -2, -2))
    page.draw_rect(page.rect + (6, 6, -6, -6))
    # A real diagram in the middle, well away from the border.
    page.draw_rect(pymupdf.Rect(200, 350, 350, 430))
    doc.save(tmp_path / "bordered.pdf")
    doc.close()

    figures = extract_vector_figures(tmp_path / "bordered.pdf", tmp_path / "images_vector")

    assert len(figures) == 1
    assert Path(figures[0]["image_path"]).is_file()
    # The rendered figure is the middle diagram, not the whole page.
    assert figures[0]["width"] < figures[0]["height"] * 6  # roughly square, not a full page
