"""Document styling, typography, and layout for question bank reconstruction."""

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor

# Standard page dimensions (Letter, 1-inch margins)
PAGE_WIDTH_INCHES = 8.5
PAGE_HEIGHT_INCHES = 11.0
MARGIN_INCHES = 1.0
PRINTABLE_WIDTH_INCHES = PAGE_WIDTH_INCHES - (2 * MARGIN_INCHES)  # 6.5 in

# Typography colors
COLOR_TEXT = RGBColor(30, 41, 59)          # Slate dark
COLOR_HEADING = RGBColor(51, 65, 85)       # Muted navy/slate
COLOR_MARKS = RGBColor(71, 85, 105)        # Subtle slate gray
COLOR_MUTED = RGBColor(100, 116, 139)      # Caption gray

FONT_NAME = "Calibri"


def apply_document_styles(doc: Document) -> None:
    """Configure default margins, typography, and paragraph spacing."""
    for section in doc.sections:
        section.top_margin = Inches(MARGIN_INCHES)
        section.bottom_margin = Inches(MARGIN_INCHES)
        section.left_margin = Inches(MARGIN_INCHES)
        section.right_margin = Inches(MARGIN_INCHES)
        section.page_width = Inches(PAGE_WIDTH_INCHES)
        section.page_height = Inches(PAGE_HEIGHT_INCHES)

    # Configure Normal style
    style = doc.styles["Normal"]
    font = style.font
    font.name = FONT_NAME
    font.size = Pt(11)
    font.color.rgb = COLOR_TEXT

    # Default paragraph format
    pf = style.paragraph_format
    pf.line_spacing = 1.15
    pf.space_before = Pt(0)
    pf.space_after = Pt(4)


def add_section_heading(doc: Document, title: str) -> None:
    """Add a subtle, polished section separator heading."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.keep_with_next = True

    run = p.add_run(title.strip().upper())
    run.font.name = FONT_NAME
    run.font.size = Pt(12)
    run.font.bold = True
    run.font.color.rgb = COLOR_HEADING

    # Add a thin bottom border to the section heading paragraph
    pPr = p._p.get_or_add_pPr()
    pBdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}>\n'
        f'  <w:bottom w:val="single" w:sz="6" w:space="4" w:color="CBD5E1"/>\n'
        f'</w:pBdr>'
    )
    pPr.append(pBdr)


def add_right_tab_stop(paragraph, position_inches: float = PRINTABLE_WIDTH_INCHES) -> None:
    """Add a right-aligned tab stop at the specified margin position."""
    paragraph.paragraph_format.tab_stops.add_tab_stop(
        Inches(position_inches), WD_TAB_ALIGNMENT.RIGHT
    )


def style_table(table, col_widths: list[float] | None = None) -> None:
    """Apply clean styling and light header shading to a Word table."""
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    # Apply light gray header shading to row 0
    header_tr = table.rows[0]._tr.get_or_add_trPr()
    header_tr.append(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'))

    for i, cell in enumerate(table.rows[0].cells):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="F1F5F9"/>')
        tcPr.append(shd)
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            for r in p.runs:
                r.font.bold = True
                r.font.size = Pt(10)

    # Set cell borders and padding across all cells
    for row in table.rows:
        for idx, cell in enumerate(row.cells):
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if col_widths and idx < len(col_widths):
                cell.width = Inches(col_widths[idx])
            tcPr = cell._tc.get_or_add_tcPr()
            tcMar = parse_xml(
                f'<w:tcMar {nsdecls("w")}>\n'
                f'  <w:top w:w="80" w:type="dxa"/>\n'
                f'  <w:bottom w:w="80" w:type="dxa"/>\n'
                f'  <w:left w:w="120" w:type="dxa"/>\n'
                f'  <w:right w:w="120" w:type="dxa"/>\n'
                f'</w:tcMar>'
            )
            tcPr.append(tcMar)
            borders = parse_xml(
                f'<w:tcBorders {nsdecls("w")}>\n'
                f'  <w:top w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>\n'
                f'  <w:bottom w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>\n'
                f'  <w:left w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>\n'
                f'  <w:right w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>\n'
                f'</w:tcBorders>'
            )
            tcPr.append(borders)

