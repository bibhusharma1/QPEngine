# Native text extraction layer of the PDF library.

import pymupdf


def extract_pdf_text(pdf_path):
    """Extract native text from every page of a PDF.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        List of dicts, one per page (1-indexed):
            {
                "page_number": 1-based page index,
                "width_pt": page width in PDF points,
                "height_pt": page height in PDF points,
                "text": full page text, reading order sorted and stripped,
            }

    Raises:
        ValueError: If the PDF is encrypted.
    """
    pages = []

    with pymupdf.open(pdf_path) as pdf:
        # Encrypted PDFs cannot be read without a password — fail fast.
        if pdf.is_encrypted:
            raise ValueError("PDF is encrypted.")

        for page_number, page in enumerate(pdf, start=1):
            # Keep positioned text blocks so later stages can recover reading
            # order and relate question text to nearby tables and figures.
            layout = []
            for block in page.get_text("dict", sort=True).get("blocks", []):
                if block.get("type") != 0:
                    continue
                text = "\n".join(
                    "".join(span.get("text", "") for span in line.get("spans", []))
                    for line in block.get("lines", [])
                ).strip()
                if text:
                    layout.append({"bbox": [round(v, 2) for v in block.get("bbox", [])], "coordinate_system": "pdf_points", "text": text})

            tables = []
            rejected_tables = []
            try:
                finder = page.find_tables()
                for index, table in enumerate(finder.tables, start=1):
                    cells = table.extract()
                    if not cells:
                        continue
                    words = page.get_text("words", sort=True)
                    row_count = len(cells)
                    normalized = []
                    for row_idx, row in enumerate(cells):
                        normalized_row = []
                        for col_idx, fallback in enumerate(row):
                            cell_index = col_idx * row_count + row_idx
                            rect = table.cells[cell_index] if cell_index < len(table.cells) else None
                            selected = []
                            if rect:
                                x0, y0, x1, y1 = rect
                                selected = [w for w in words if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1]
                            if selected:
                                selected.sort(key=lambda word: (round(word[1] / 2) * 2, word[0]))
                                lines = []
                                for word in selected:
                                    line_y = round(word[1] / 2) * 2
                                    if not lines or abs(lines[-1][0] - line_y) > 2:
                                        lines.append((line_y, []))
                                    lines[-1][1].append(word[4])
                                value = "\n".join(" ".join(line) for _, line in lines)
                            else:
                                value = fallback or ""
                            normalized_row.append(value.strip())
                        normalized.append(normalized_row)
                    # Discard empty detections; retain original row/column relations.
                    normalized = [row for row in normalized if any(row)]
                    if normalized:
                        header_cells = [cell for cell in normalized[0] if cell]
                        if len(header_cells) < 2 or sum(len(cell) <= 48 for cell in header_cells) < 2:
                            rejected_tables.append({
                                "table_id": f"p{page_number}-t{index}",
                                "bbox": [round(v, 2) for v in table.bbox],
                                "reason": "weak_header_row_may_be_multicolumn_page_text",
                                "candidate_cells": normalized,
                            })
                            continue
                        tables.append({
                            "table_id": f"p{page_number}-t{index}",
                            "bbox": [round(v, 2) for v in table.bbox],
                            "headers": normalized[0],
                            "rows": normalized[1:],
                            "cells": normalized,
                            "extraction_method": "pymupdf_find_tables",
                        })
            except Exception:
                # Table detection can fail for damaged or unusual pages; text
                # and visual page assets remain available as fallbacks.
                tables = []
            pages.append({
                "page_number": page_number,
                "width_pt": page.rect.width,
                "height_pt": page.rect.height,
                # sort=True orders text blocks as they visually appear,
                # avoiding jumbled output from table-style layouts.
                "text": page.get_text("text", sort=True).strip(),
                "layout_blocks": layout,
                "tables": tables,
                "rejected_table_candidates": rejected_tables,
            })

    return pages
