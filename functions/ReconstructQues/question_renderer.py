"""Renders questions, subquestions, marks, tables, and figures into DOCX."""

import re
from typing import Any

from docx import Document
from docx.shared import Inches, Pt, RGBColor

from .asset_handler import AssetHandler
from .styles import (
    COLOR_MARKS,
    COLOR_TEXT,
    FONT_NAME,
    PRINTABLE_WIDTH_INCHES,
    add_right_tab_stop,
    add_section_heading,
    style_table,
)


class QuestionRenderer:
    """Renders structured exam questions into formatted Word documents."""

    def __init__(self, asset_handler: AssetHandler):
        self.asset_handler = asset_handler
        self.current_section: str | None = None
        self.rendered_questions_count: int = 0
        self.rendered_subquestions_count: int = 0
        self._main_question_counter: int = 0
        self._subquestion_counters: list[int] = []
        self._current_parent_source: dict[str, Any] = {}
        self.source_references: list[str] = []

    @staticmethod
    def format_marks(marks: Any, notation: str | None = None) -> str | None:
        """Format marks into standard [X marks] or [1 mark] notation."""
        # 1. Prefer notation if descriptive (e.g. "2 x 10")
        if notation and str(notation).strip():
            raw_not = str(notation).strip()
            # Clean outer brackets
            cleaned = re.sub(r"^\[\s*|\s*\]$", "", raw_not).strip()
            if cleaned:
                lower = cleaned.lower()
                if "mark" in lower:
                    return f"[{cleaned}]"
                if cleaned == "1":
                    return "[1 mark]"
                return f"[{cleaned} marks]"

        # 2. Use numeric marks
        if marks is not None:
            try:
                val = float(marks)
                if val <= 0:
                    return None
                if val == 1.0:
                    return "[1 mark]"
                if val.is_integer():
                    return f"[{int(val)} marks]"
                return f"[{val:g} marks]"
            except (ValueError, TypeError):
                pass

        return None

    @staticmethod
    def _format_subquestion_label(counter: int) -> str:
        """Generate simple letter labels for subquestions (a), b), c), ..."""
        # Use lowercase letters starting from 'a'
        return f"{chr(ord('a') + counter - 1)})"

    @staticmethod
    def _get_basename(path_val: Any) -> str:
        if path_val is None:
            return ""
        try:
            s = str(path_val).strip()
            if not s:
                return ""
            # Handle both posix and windows paths
            return s.replace("\\", "/").split("/")[-1] or s
        except Exception:
            return str(path_val).strip()

    @staticmethod
    def _format_pages(pages: Any) -> str | None:
        if pages is None:
            return None
        if isinstance(pages, (list, tuple, set)):
            pages_list = []
            for p in pages:
                if p is None:
                    continue
                # Skip NaN
                try:
                    if isinstance(p, float) and (p != p):
                        continue
                except Exception:
                    pass
                pages_list.append(str(p).strip())
            if not pages_list:
                return None
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for p in pages_list:
                if p in seen:
                    continue
                seen.add(p)
                unique.append(p)
            if not unique:
                return None
            if len(unique) == 1:
                return unique[0]
            return ", ".join(unique)
        # Single value
        try:
            if isinstance(pages, float) and (pages != pages):
                return None
        except Exception:
            pass
        s = str(pages).strip()
        if not s or s.lower() == "none":
            return None
        return s

    def _format_source_reference(
        self,
        source_pdf: Any,
        source_pages: Any,
        section_name: Any = None,
        parent_ref: Any = None,
        item_ref: Any = None,
    ) -> str:
        """Format source reference as 'Source: original.pdf | Page 2 | Part-III | Q6(b)' or 'Source: unavailable'."""
        # Try to get filename/path
        pdf_path = source_pdf
        # Also check common alternative keys if passed differently
        pdf_str_full = ""
        if pdf_path is not None:
            pdf_str_full = str(pdf_path).strip()
        if not pdf_str_full:
            return "Source: unavailable"
        parts = [f"Source: {pdf_str_full}"]
        page_str = self._format_pages(source_pages)
        if page_str:
            parts.append(f"Page {page_str}")
        else:
            # Don't fabricate page numbers
            pass

        sec = section_name
        if sec is not None and str(sec).strip():
            sec_s = str(sec).strip()
            # Avoid duplicating if it looks like just "Part" etc
            parts.append(sec_s)

        # Prefer item_ref (this question/subquestion's original ref) if available
        ref_to_show = item_ref if item_ref is not None and str(item_ref).strip() else parent_ref
        if ref_to_show is not None and str(ref_to_show).strip():
            ref_s = str(ref_to_show).strip()
            # Don't add if it's redundant
            parts.append(ref_s)

        return " | ".join(parts)

    def render_question(self, doc: Document, q: dict[str, Any]) -> None:
        """Render a single question record with its hierarchy and assets."""
        # Check and render section heading if changed
        sec_name = q.get("section_name")
        if sec_name and str(sec_name).strip():
            clean_sec = str(sec_name).strip()
            if clean_sec != self.current_section:
                self.current_section = clean_sec
                add_section_heading(doc, clean_sec)

        self._main_question_counter += 1
        original_label = q.get("original_question_number") or q.get("original_label")
        label = str(original_label).strip() if original_label else f"Q{self._main_question_counter}."
        text = (q.get("cleaned_text") or q.get("original_text") or q.get("text") or "").strip()
        marks_str = self.format_marks(q.get("marks"), q.get("marks_notation"))
        assets = q.get("assets") or []
        sub_questions = q.get("sub_questions") or []

        # Store parent source for inheritance
        self._current_parent_source = {
            "source_pdf": q.get("source_pdf"),
            "source_pages": q.get("source_pages"),
            "section_name": sec_name,
        }

        # Determine original reference for this item
        item_ref = (
            q.get("original_question_number")
            or q.get("question_number")
            or q.get("original_label")
            or q.get("label")
            or q.get("q_number")
            or q.get("number")
            or q.get("parent_question_ref")
        )
        parent_ref = item_ref

        # Render question text (including any embedded tables)
        self._render_text_block(
            doc=doc,
            label=label,
            text=text,
            marks_str=marks_str,
            left_indent_inches=0.0,
            is_main_question=True,
            has_children=bool(sub_questions or assets),
        )
        self._render_structured_tables(doc, q.get("tables") or [], 0.0)
        self.rendered_questions_count += 1

        # Render question-level assets directly below question text
        for asset in assets:
            self.asset_handler.insert_asset(doc, asset, question_label=label)

        # Collect source references for a concise final appendix.
        src_ref = self._format_source_reference(
            q.get("source_pdf") or q.get("source_file") or q.get("pdf_name") or q.get("pdf_path"),
            q.get("source_pages") or q.get("pages") or q.get("page_numbers") or q.get("source_page"),
            section_name=sec_name,
            parent_ref=parent_ref,
            item_ref=item_ref,
        )
        main_marks = self.format_marks(q.get("marks"), q.get("marks_notation"))
        if main_marks:
            src_ref += f" | {main_marks.strip('[]')}"
        self.source_references.append(src_ref)

        # Render subquestions recursively
        self._subquestion_counters = []
        for idx, sq in enumerate(sub_questions, start=1):
            self._render_subquestion(
                doc,
                sq,
                level=1,
                counter=idx,
                parent_source=self._current_parent_source,
                parent_question_ref=parent_ref,
            )

    def _render_subquestion(
        self,
        doc: Document,
        sq: dict[str, Any],
        level: int = 1,
        counter: int = 1,
        parent_source: dict[str, Any] | None = None,
        parent_question_ref: Any = None,
    ) -> None:
        """Render a subquestion with indentation, marks, and nested assets."""
        # Generate sequential label based on hierarchy level
        original_sub_label = sq.get("original_label") or sq.get("original_question_number") or sq.get("label")
        sq_label = str(original_sub_label).strip() if original_sub_label else self._format_subquestion_label(counter)
        sq_text = (sq.get("cleaned_text") or sq.get("original_text") or sq.get("text") or "").strip()
        sq_marks_str = self.format_marks(sq.get("marks"), sq.get("marks_notation"))
        sq_assets = sq.get("assets") or []
        nested_sub_questions = sq.get("sub_questions") or []

        indent_inches = 0.35 * level

        self._render_text_block(
            doc=doc,
            label=sq_label,
            text=sq_text,
            marks_str=sq_marks_str,
            left_indent_inches=indent_inches,
            is_main_question=False,
            has_children=bool(nested_sub_questions or sq_assets),
        )
        self._render_structured_tables(doc, sq.get("tables") or [], indent_inches)
        self.rendered_subquestions_count += 1

        # Subquestion assets
        for asset in sq_assets:
            self.asset_handler.insert_asset(doc, asset, question_label=sq_label)

        # Determine original references
        sq_item_ref = (
            sq.get("original_question_number")
            or sq.get("question_number")
            or sq.get("original_label")
            or sq.get("label")
            or sq.get("sub_label")
            or sq.get("number")
        )
        # Build combined ref like Q6(b) if parent ref exists
        combined_ref = sq_item_ref
        if parent_question_ref and sq_item_ref:
            p_ref_str = str(parent_question_ref).strip()
            s_ref_str = str(sq_item_ref).strip()
            # If parent is Q6 and sub is b), combine as Q6(b)
            if p_ref_str and s_ref_str and not s_ref_str.startswith(p_ref_str):
                sub_ref = s_ref_str.strip().rstrip(").:").strip("()").strip()
                combined_ref = f"{p_ref_str}({sub_ref})"

        # Add source reference below subquestion
        # Use own source if available, else inherit from parent
        sq_source_pdf = sq.get("source_pdf") or sq.get("source_file") or sq.get("pdf_name") or sq.get("pdf_path")
        sq_source_pages = sq.get("source_pages") or sq.get("pages") or sq.get("page_numbers") or sq.get("source_page")
        sq_section = sq.get("section_name")
        if (sq_source_pdf is None or (isinstance(sq_source_pdf, str) and not sq_source_pdf.strip())) and parent_source:
            sq_source_pdf = parent_source.get("source_pdf")
            sq_source_pages = parent_source.get("source_pages")
        if (sq_section is None or (isinstance(sq_section, str) and not sq_section.strip())) and parent_source:
            sq_section = parent_source.get("section_name")
        src_ref = self._format_source_reference(sq_source_pdf, sq_source_pages, section_name=sq_section, parent_ref=parent_question_ref, item_ref=combined_ref or sq_item_ref)
        if sq_marks_str:
            src_ref += f" | {sq_marks_str.strip('[]')}"
        self.source_references.append(src_ref)

        # Recursive nested subquestions - inherit current subquestion's source
        current_sq_source = {"source_pdf": sq_source_pdf, "source_pages": sq_source_pages, "section_name": sq_section}
        for idx, nested in enumerate(nested_sub_questions, start=1):
            self._render_subquestion(doc, nested, level=level + 1, counter=idx, parent_source=current_sq_source, parent_question_ref=combined_ref or sq_item_ref or parent_question_ref)

    def _render_text_block(
        self,
        doc: Document,
        label: str,
        text: str,
        marks_str: str | None,
        left_indent_inches: float,
        is_main_question: bool,
        has_children: bool,
    ) -> None:
        """Render question or subquestion text, handling markdown tables and right tab stops."""
        lines = [line.rstrip() for line in text.split("\n")] if text else [""]

        # Parse any markdown tables inside text
        blocks = self._split_into_paragraphs_and_tables(lines)

        first_block = True
        marks_placed = False

        for block_type, content in blocks:
            if block_type == "table":
                self._render_markdown_table(doc, content, left_indent_inches)
            else:
                # Text paragraphs block
                for line in content:
                    p = doc.add_paragraph()
                    p.paragraph_format.left_indent = Inches(left_indent_inches)
                    add_right_tab_stop(p, PRINTABLE_WIDTH_INCHES)

                    if first_block:
                        # Top spacing
                        p.paragraph_format.space_before = Pt(8 if is_main_question else 4)
                        p.paragraph_format.space_after = Pt(3)
                        if has_children:
                            p.paragraph_format.keep_with_next = True

                        # Add label
                        if label:
                            clean_label = label if label.endswith((".", ")", ":")) else f"{label}."
                            r_lbl = p.add_run(f"{clean_label} ")
                            r_lbl.font.name = FONT_NAME
                            r_lbl.font.size = Pt(11)
                            r_lbl.font.bold = True
                            r_lbl.font.color.rgb = COLOR_TEXT

                        # Add text
                        r_txt = p.add_run(line)
                        r_txt.font.name = FONT_NAME
                        r_txt.font.size = Pt(11)
                        r_txt.font.color.rgb = COLOR_TEXT

                        # If this is the only text block or we place marks on first line
                        if marks_str and not marks_placed:
                            r_tab = p.add_run(f"\t{marks_str}")
                            r_tab.font.name = FONT_NAME
                            r_tab.font.size = Pt(10.5)
                            r_tab.font.bold = True
                            r_tab.font.color.rgb = COLOR_MARKS
                            marks_placed = True

                        first_block = False
                    else:
                        p.paragraph_format.space_before = Pt(2)
                        p.paragraph_format.space_after = Pt(3)
                        r_txt = p.add_run(line)
                        r_txt.font.name = FONT_NAME
                        r_txt.font.size = Pt(11)
                        r_txt.font.color.rgb = COLOR_TEXT

        # If text was empty but label/marks existed
        if first_block:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(left_indent_inches)
            p.paragraph_format.space_before = Pt(8 if is_main_question else 4)
            p.paragraph_format.space_after = Pt(3)
            add_right_tab_stop(p, PRINTABLE_WIDTH_INCHES)
            if label:
                clean_label = label if label.endswith((".", ")", ":")) else f"{label}."
                r_lbl = p.add_run(f"{clean_label} ")
                r_lbl.font.name = FONT_NAME
                r_lbl.font.size = Pt(11)
                r_lbl.font.bold = True
            if marks_str and not marks_placed:
                r_tab = p.add_run(f"\t{marks_str}")
                r_tab.font.name = FONT_NAME
                r_tab.font.size = Pt(10.5)
                r_tab.font.bold = True
                r_tab.font.color.rgb = COLOR_MARKS

    @staticmethod
    def _render_structured_tables(doc: Document, tables: list[dict], left_indent_inches: float) -> None:
        """Render extracted cell relationships as native, editable Word tables."""
        for data in tables:
            cells = data.get("cells") or []
            if not cells:
                cells = ([data.get("headers", [])] if data.get("headers") else []) + data.get("rows", [])
            rows = [[str(cell or "") for cell in row] for row in cells if row]
            if not rows:
                continue
            columns = max(map(len, rows))
            table = doc.add_table(rows=len(rows), cols=columns)
            table.autofit = True
            for row_index, row in enumerate(rows):
                for col_index in range(columns):
                    cell = table.cell(row_index, col_index)
                    value = row[col_index] if col_index < len(row) else ""
                    cell.text = value
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.space_before = Pt(2)
                        paragraph.paragraph_format.space_after = Pt(2)
                        for run in paragraph.runs:
                            run.font.name = FONT_NAME
                            run.font.size = Pt(9.5)
                            if row_index == 0:
                                run.font.bold = True
            width = max(1.0, PRINTABLE_WIDTH_INCHES - left_indent_inches) / columns
            style_table(table, [width] * columns)

    @staticmethod
    def _split_into_paragraphs_and_tables(
        lines: list[str],
    ) -> list[tuple[str, list[str]]]:
        """Split text lines into paragraphs and markdown table segments."""
        blocks: list[tuple[str, list[str]]] = []
        current_text: list[str] = []
        current_table: list[str] = []
        in_table = False

        for line in lines:
            stripped = line.strip()
            # Markdown table line check: starts and ends with '|' or contains multiple '|'
            is_table_row = stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2

            if is_table_row:
                if not in_table:
                    if current_text:
                        blocks.append(("text", current_text))
                        current_text = []
                    in_table = True
                current_table.append(stripped)
            else:
                if in_table:
                    if current_table:
                        blocks.append(("table", current_table))
                        current_table = []
                    in_table = False
                if stripped or current_text:
                    current_text.append(line)

        if in_table and current_table:
            blocks.append(("table", current_table))
        elif current_text:
            blocks.append(("text", current_text))

        return blocks if blocks else [("text", [""])]

    @staticmethod
    def _render_markdown_table(
        doc: Document,
        table_lines: list[str],
        left_indent_inches: float,
    ) -> None:
        """Parse markdown table rows and render as a native Word table."""
        parsed_rows = []
        for line in table_lines:
            # Skip divider rows like |---|---|
            if re.match(r"^\|(?:\s*[:-]+[-| :]*)\|?$", line):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells:
                parsed_rows.append(cells)

        if not parsed_rows:
            return

        cols_count = max(len(r) for r in parsed_rows)
        table = doc.add_table(rows=len(parsed_rows), cols=cols_count)

        # Calculate width
        available_width = PRINTABLE_WIDTH_INCHES - left_indent_inches
        col_w = available_width / max(cols_count, 1)

        for row_idx, row_data in enumerate(parsed_rows):
            for col_idx in range(cols_count):
                cell_text = row_data[col_idx] if col_idx < len(row_data) else ""
                cell = table.cell(row_idx, col_idx)
                p = cell.paragraphs[0]
                p.paragraph_format.space_before = Pt(2)
                p.paragraph_format.space_after = Pt(2)
                r = p.add_run(cell_text)
                r.font.name = FONT_NAME
                r.font.size = Pt(10)
                if row_idx == 0:
                    r.font.bold = True

        style_table(table, [col_w] * cols_count)
