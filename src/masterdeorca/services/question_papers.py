"""Blueprint persistence and parsed-question based paper generation - stored in SQLite."""

from __future__ import annotations

import json
import os
import random
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.shared import Inches, Pt, RGBColor

from src.masterdeorca import database
from src.masterdeorca.services import question_bank, question_priorities

DEFAULT_BLUEPRINT_ID = "default-generic"
QUICK_MARK_PRESETS = (15, 25, 50, 60, 70, 80, 100)
PROCESSING_ACCURACY_NOTICE = (
    "Processing Accuracy Notice\n\n"
    "Review extracted questions before relying on them. OCR and question "
    "segmentation can produce errors, especially for scanned or complex PDFs.\n\n"
    "Important Information: Some questions are extracted from PDF files using "
    "automated text recognition (OCR). This process may occasionally misread "
    "words, numbers, mathematical expressions, diagrams, or tables. Questions "
    "may also be split incorrectly or have formatting issues.\n\n"
    "Please check the questions carefully before using this paper for an "
    "examination. Make any necessary corrections to ensure that the questions, "
    "marks, and figures are accurate."
)


def _add_processing_accuracy_notice(doc: Document) -> None:
    """Render the mandatory processing accuracy notice once, in black text."""
    doc.add_page_break()
    doc.add_heading("Processing Accuracy Notice", level=1)
    # The constant's first paragraph is the title (already a heading above),
    # so render the remaining body paragraphs only.
    for paragraph in PROCESSING_ACCURACY_NOTICE.split("\n\n")[1:]:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        run = p.add_run(paragraph.strip())
        run.font.color.rgb = RGBColor(0, 0, 0)


def _default_blueprint() -> dict:
    return {
        "blueprint_id": DEFAULT_BLUEPRINT_ID,
        "name": "Generic question paper",
        "description": "Automatically select suitable parsed questions to match the requested total marks.",
        "subject_id": None,
        "exam_type": None,
        "auto_select": True,
        "selection_rule": "priority_first",
        "instructions": ["Answer all questions."],
        "sections": [],
        "is_default": True,
    }


def _ensure_default_blueprint() -> None:
    with database.connect() as db:
        row = db.execute("SELECT * FROM blueprints WHERE blueprint_id=?", (DEFAULT_BLUEPRINT_ID,)).fetchone()
        if not row:
            bp = _default_blueprint()
            db.execute("INSERT OR REPLACE INTO blueprints(blueprint_id,subject_name,blueprint_json,updated_at) VALUES(?,?,?,?)",
                       (bp["blueprint_id"], bp.get("subject_id"), json.dumps(bp), datetime.now(timezone.utc).isoformat()))


def _read_all_locked() -> dict[str, dict]:
    _ensure_default_blueprint()
    with database.connect() as db:
        rows = db.execute("SELECT * FROM blueprints").fetchall()
    result = {}
    for r in rows:
        try:
            bp = json.loads(r["blueprint_json"])
            validate_blueprint(bp)
            result[bp["blueprint_id"]] = bp
        except Exception:
            continue
    if DEFAULT_BLUEPRINT_ID not in result:
        bp = _default_blueprint()
        with database.connect() as db:
            db.execute("INSERT OR REPLACE INTO blueprints(blueprint_id,subject_name,blueprint_json,updated_at) VALUES(?,?,?,?)",
                       (bp["blueprint_id"], bp.get("subject_id"), json.dumps(bp), datetime.now(timezone.utc).isoformat()))
        result[DEFAULT_BLUEPRINT_ID] = bp
    return result


def validate_blueprint(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Blueprint must be a JSON object")
    required = {"blueprint_id", "name", "sections"}
    if not required.issubset(value):
        raise ValueError("Blueprint requires blueprint_id, name, and sections")
    if not isinstance(value["blueprint_id"], str) or not value["blueprint_id"].strip():
        raise ValueError("blueprint_id must be a non-empty string")
    if not isinstance(value["name"], str) or not value["name"].strip():
        raise ValueError("name must be a non-empty string")
    auto = value.get("auto_select", False)
    sections = value["sections"]
    if type(auto) is not bool or not isinstance(sections, list):
        raise ValueError("auto_select must be boolean and sections must be an array")
    if auto:
        if sections:
            raise ValueError("auto_select blueprints must not define fixed sections")
    else:
        if not sections:
            raise ValueError("A section-based blueprint needs at least one section")
        names = set()
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("name"), str) or not section["name"].strip():
                raise ValueError("Every section needs a name")
            normalized_name = section["name"].strip().casefold()
            if normalized_name in names:
                raise ValueError("Section names must be unique within a blueprint")
            names.add(normalized_name)
            count, marks = section.get("question_count"), section.get("marks_per_question")
            if type(count) is not int or count < 1 or type(marks) is not int or marks < 1:
                raise ValueError("Section question_count and marks_per_question must be positive integers")
            if section.get("question_type") is not None and not isinstance(section["question_type"], str):
                raise ValueError("question_type must be a string when provided")
        section_total = sum(section["question_count"] * section["marks_per_question"] for section in sections)
        if "total_marks" in value and (type(value["total_marks"]) is not int or value["total_marks"] != section_total):
            raise ValueError("total_marks must equal the sum of all section marks")
    if value.get("selection_rule", "priority_first") not in {"priority_first", "random"}:
        raise ValueError("selection_rule must be priority_first or random")
    for field in ("subject_id", "exam_type"):
        if value.get(field) is not None and (not isinstance(value[field], str) or not value[field].strip()):
            raise ValueError(f"{field} must be a non-empty string or null")
    instructions = value.get("instructions", [])
    if not isinstance(instructions, list) or any(not isinstance(item, str) for item in instructions):
        raise ValueError("instructions must be an array of strings")
    return dict(value)


def list_blueprints() -> list[dict]:
    rows = _read_all_locked()
    return sorted(rows.values(), key=lambda row: (row["blueprint_id"] != DEFAULT_BLUEPRINT_ID, row["name"].casefold()))


def get_blueprint(blueprint_id: str) -> dict | None:
    return _read_all_locked().get(blueprint_id)


def save_blueprint(value: dict, *, create: bool = False) -> dict:
    value = dict(value or {})
    if not value.get("blueprint_id"):
        value["blueprint_id"] = uuid.uuid4().hex
    # Custom blueprints are never the canonical default, regardless of client input.
    value["is_default"] = False
    row = validate_blueprint(value)
    if row["blueprint_id"] == DEFAULT_BLUEPRINT_ID:
        raise ValueError("The built-in default blueprint cannot be replaced or deleted")
    if row.get("is_default") is True:
        raise ValueError("Only the canonical default-generic blueprint can be marked as default")
    rows = _read_all_locked()
    exists = row["blueprint_id"] in rows
    if create and exists:
        raise FileExistsError(row["blueprint_id"])
    if not create and not exists:
        raise KeyError(row["blueprint_id"])
    row["is_default"] = False
    now = datetime.now(timezone.utc).isoformat()
    with database.connect() as db:
        db.execute("INSERT OR REPLACE INTO blueprints(blueprint_id,subject_name,blueprint_json,updated_at) VALUES(?,?,?,?)",
                   (row["blueprint_id"], row.get("subject_id"), json.dumps(row), now))
    return row


def delete_blueprint(blueprint_id: str) -> bool:
    if blueprint_id == DEFAULT_BLUEPRINT_ID:
        raise ValueError("The built-in default blueprint cannot be replaced or deleted")
    rows = _read_all_locked()
    if blueprint_id not in rows:
        return False
    with database.connect() as db:
        db.execute("DELETE FROM blueprints WHERE blueprint_id=?", (blueprint_id,))
    return True


def select_blueprint(subject_id: str, exam_type: str, requested_id: str | None = None) -> dict:
    if requested_id:
        result = get_blueprint(requested_id)
        if result is None:
            raise KeyError(requested_id)
        return result
    candidates = [row for row in list_blueprints() if row["blueprint_id"] != DEFAULT_BLUEPRINT_ID
                  and row.get("subject_id") in (None, subject_id)
                  and row.get("exam_type") in (None, exam_type)]
    if not candidates:
        return _default_blueprint()
    candidates.sort(key=lambda row: (int(row.get("subject_id") == subject_id)
                                     + int(row.get("exam_type") == exam_type),
                                     row.get("exam_type") == exam_type), reverse=True)
    return candidates[0]


def _marks(question: dict) -> int | None:
    value = question.get("marks")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return int(numeric) if numeric > 0 and numeric.is_integer() else None


def _question_text(question: dict) -> str:
    return str(question.get("original_text") or question.get("cleaned_text") or question.get("text") or "").strip()


def _is_type(question: dict, requested: str | None) -> bool:
    if not requested:
        return True
    actual = question.get("question_type") or question.get("type") or question.get("kind")
    return isinstance(actual, str) and actual.casefold() == requested.casefold()


from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def _add_horizontal_rule(paragraph) -> None:
    p = paragraph._p
    pPr = p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "000000")
    pbdr.append(bottom)
    pPr.append(pbdr)


def _docx(paper: dict, target: Path) -> None:
    doc = Document()
    section = doc.sections[0]
    section.top_margin = section.bottom_margin = Inches(1)
    section.left_margin = section.right_margin = Inches(1)
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    for style_name, size in (("Heading 1", 14), ("Heading 2", 12)):
        try:
            style = doc.styles[style_name]
            style.font.name = "Times New Roman"
            style.font.size = Pt(size)
            style.font.color.rgb = RGBColor(0, 0, 0)
            style.font.bold = True
        except KeyError:
            pass
    for value in (paper.get("university_name"), paper.get("college_name"), paper.get("department_name")):
        if value:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(value)
            r.bold = True
            r.font.size = Pt(16)
    heading = doc.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = heading.add_run(paper["title"])
    run.bold = True
    run.font.size = Pt(14)
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subject_label = paper.get("subject_name") or paper["subject"]
    if paper.get("subject_code"):
        subject_label += f" ({paper['subject_code']})"
    meta.add_run(f"{subject_label}  |  {paper['exam_type']}\n")
    if paper.get("academic_year"):
        meta.add_run(f"Academic Year: {paper['academic_year']}  ")
    if paper.get("semester"):
        meta.add_run(f"Semester: {paper['semester']}  ")
    if paper.get("examination_date"):
        meta.add_run(f"Date: {paper['examination_date']}  ")
    if paper.get("academic_year") or paper.get("semester") or paper.get("examination_date"):
        meta.add_run("\n")
    meta.add_run(f"Maximum marks: {paper['requested_total_marks']}  |  Duration: {paper['duration']}")

    # Horizontal separator.
    separator = doc.add_paragraph()
    separator.paragraph_format.space_after = Pt(6)
    _add_horizontal_rule(separator)

    if paper.get("instructions"):
        doc.add_heading("Instructions", level=2)
        for instruction in paper["instructions"]:
            doc.add_paragraph(instruction, style="List Number")

    print_number = 1
    reference_rows = []
    for section_data in paper["sections"]:
        section_name = str(section_data.get("name") or "").strip()
        if not section_name.upper().startswith("SECTION "):
            section_heading = f"SECTION {section_name}".strip()
        else:
            section_heading = section_name.upper()
        doc.add_heading(section_heading, level=2)
        if section_data.get("instructions"):
            doc.add_paragraph(section_data["instructions"])
        for item in section_data["questions"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(8)
            p.paragraph_format.tab_stops.add_tab_stop(Inches(6.5), WD_TAB_ALIGNMENT.RIGHT)
            label = p.add_run(f"{print_number}. ")
            print_number += 1
            label.bold = True
            p.add_run(item["text"])
            p.add_run("\t")
            marks = p.add_run(f"[{item['marks']} Marks]")
            marks.bold = True
            provenance = []
            if item.get("source_pdf"):
                provenance.append(f"Source PDF: {Path(str(item['source_pdf'])).name}")
            if item.get("source_path"):
                provenance.append(f"Source path: {item['source_path']}")
            if item.get("original_question_number"):
                provenance.append(f"Original question: {item['original_question_number']}")
            pages = item.get("source_pages")
            if isinstance(pages, list) and pages:
                provenance.append("Page(s): " + ", ".join(str(page) for page in pages))
            elif item.get("page_number") is not None:
                provenance.append(f"Page: {item['page_number']}")
            if item.get("section_name"):
                provenance.append(f"Source section: {item['section_name']}")
            if provenance:
                reference_rows.append((print_number - 1, "; ".join(provenance)))
            for asset in item.get("assets", []):
                asset_path = _safe_asset_path(
                    paper["subject"], item.get("source_pdf"), asset, target.parent
                )
                if asset_path:
                    try:
                        doc.add_picture(str(asset_path), width=Inches(4.8))
                    except Exception:
                        pass
            for table in item.get("tables", []):
                if not isinstance(table, dict):
                    continue
                rows = table.get("cells") or (([table.get("headers", [])] if table.get("headers") else []) + table.get("rows", []))
                if not rows:
                    continue
                width = max((len(row) for row in rows if isinstance(row, list)), default=0)
                if not width:
                    continue
                word_table = doc.add_table(rows=0, cols=width)
                word_table.style = "Table Grid"
                for row in rows:
                    if isinstance(row, list):
                        cells = word_table.add_row().cells
                        for cell, value in zip(cells, row):
                            cell.text = str(value)

    # Processing Accuracy Notice, after questions.
    _add_processing_accuracy_notice(doc)

    # References / Sources, after the notice.
    if reference_rows:
        doc.add_page_break()
        doc.add_heading("References / Sources", level=1)
        for number, reference in reference_rows:
            doc.add_paragraph(f"Q{number} | {reference}")

    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publication: fully render and verify the package on a temporary
    # file in the same directory, then swap it into place under its final ID.
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        doc.save(temp_name)
        Document(temp_name)  # verify the saved package reopens
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def generate_paper(*, title: str, subject_id: str, exam_type: str, total_marks: int,
                   duration: str, blueprint: dict, seed: int | None = None,
                   metadata: dict | None = None, generated_by: str | None = None) -> dict:
    blueprint = validate_blueprint(blueprint)
    if not title.strip() or not exam_type.strip() or not duration.strip():
        raise ValueError("title, exam_type, and duration must not be empty")
    questions = question_bank.read_subject(subject_id)
    _ids = {str(q.get("question_id")) for q in questions if q.get("question_id")}
    _overrides = question_priorities.get_priority_overrides(_ids)
    priorities = {str(q.get("question_id")): _overrides.get(str(q.get("question_id")), q.get("priority") or 1)
                  for q in questions if q.get("question_id")}
    rng = random.Random(seed)
    used: set[str] = set()
    sections_out: list[dict] = []
    shortages: list[dict] = []
    next_number = 1

    def candidates(marks: int | None, question_type: str | None) -> list[dict]:
        return [q for q in questions if q.get("question_id") and str(q["question_id"]) not in used
                and _question_text(q) and (marks is None or _marks(q) == marks) and _is_type(q, question_type)]

    def order(pool: list[dict]) -> list[dict]:
        # Preserve the Question Bank's stored order; do not reorder by priority,
        # marks, page, ID or any other derived key.
        return list(pool)

    if blueprint.get("auto_select"):
        pool = candidates(None, None)
        # Exact-sum subset selection; bounded by the requested mark count.
        order(pool)
        states: dict[int, list[dict]] = {0: []}
        for question in pool:
            value = _marks(question)
            if value is None or value > total_marks:
                continue
            for subtotal, chosen in list(states.items())[::-1]:
                new_total = subtotal + value
                if new_total <= total_marks and new_total not in states:
                    states[new_total] = chosen + [question]
        achievable = max(states)
        selected = states.get(total_marks, states[achievable])
        section_questions = []
        for question in selected:
            used.add(str(question["question_id"]))
            section_questions.append(_paper_question(question, next_number, priorities))
            next_number += 1
        sections_out.append({"name": "Questions", "questions": section_questions,
                             "question_count": len(section_questions), "marks": achievable})
        if achievable < total_marks:
            shortages.append({"section": "Questions", "requested_marks": total_marks,
                              "available_marks": achievable, "reason": "Insufficient suitable parsed questions to reach the requested total."})
    else:
        expected_total = sum(section["question_count"] * section["marks_per_question"] for section in blueprint["sections"])
        if expected_total != total_marks:
            raise ValueError(f"Blueprint sections total {expected_total} marks, but requested total is {total_marks}")
        for section in blueprint["sections"]:
            pool = order(candidates(section["marks_per_question"], section.get("question_type")))
            selected = pool[:section["question_count"]]
            rows = []
            for question in selected:
                used.add(str(question["question_id"]))
                rows.append(_paper_question(question, next_number, priorities))
                next_number += 1
            sections_out.append({"name": section["name"], "instructions": section.get("instructions"),
                                 "questions": rows, "question_count": len(rows),
                                 "marks": len(rows) * section["marks_per_question"]})
            if len(rows) < section["question_count"]:
                shortages.append({"section": section["name"], "requested_questions": section["question_count"],
                                  "available_questions": len(rows), "marks_per_question": section["marks_per_question"],
                                  "reason": "Insufficient suitable parsed questions."})

    actual_marks = sum(section["marks"] for section in sections_out)
    paper_id = str(uuid.uuid4())
    filename = f"question-paper-{paper_id}.docx"
    metadata = metadata or {}
    paper = {"paper_id": paper_id, "title": title.strip(), "subject": subject_id, "exam_type": exam_type.strip(),
             "duration": duration.strip(), "requested_total_marks": total_marks, "total_marks": actual_marks,
             **{key: value for key, value in metadata.items() if key in {"university_name", "college_name", "department_name", "academic_year", "semester", "subject_name", "subject_code", "examination_date", "include_processing_accuracy_notice"}},
             "blueprint_id": blueprint["blueprint_id"], "instructions": blueprint.get("instructions", []),
             "sections": sections_out, "shortages": shortages, "seed": seed,
             "processing_accuracy_notice": PROCESSING_ACCURACY_NOTICE,
             "filename": filename,
             "generated_at": datetime.now(timezone.utc).isoformat(),
             "generated_by": generated_by,
             "file_path": filename,
             "docx_path": filename, "docx_url": f"/api/question-papers/{paper_id}/docx"}
    with tempfile.TemporaryDirectory(prefix=f"masterdeorca-paper-{paper_id}-") as temporary:
        temp_docx = Path(temporary) / filename
        _docx(paper, temp_docx)
        docx_blob = temp_docx.read_bytes()
    now = datetime.now(timezone.utc).isoformat()
    with database.connect() as db:
        db.execute(
            """INSERT INTO generated_papers(
                paper_id,subject_name,storage_key,paper_json,docx_blob,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (paper_id, paper.get("subject"), f"{paper_id}.docx",
             json.dumps(paper, ensure_ascii=False), docx_blob, now, now),
        )
    return paper


def _paper_summary(record: dict, *, docx_available: bool, stat_mtime: datetime | None = None) -> dict:
    generated_at = record.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        generated_at = stat_mtime.isoformat() if stat_mtime is not None else ""
    return {"paper_id": record.get("paper_id"),
            "filename": record.get("filename") or f"question-paper-{record.get('paper_id')}.docx",
            "subject": record.get("subject"), "paper_title": record.get("title"),
            "exam_type": record.get("exam_type"), "total_marks": record.get("total_marks"),
            "duration": record.get("duration"), "generated_at": generated_at,
            "generated_by": record.get("generated_by"), "file_path": record.get("file_path"),
            "docx_available": docx_available,
            "docx_url": record.get("docx_url") or f"/api/question-papers/{record.get('paper_id')}/docx"}


def list_generated_papers(*, page: int = 1, page_size: int = 25, subject: str | None = None,
                          visible_for: str | None = None) -> dict:
    """Newest-first, paginated summaries of permanently stored generated papers."""
    with database.connect() as db:
        query = "SELECT paper_json,docx_blob FROM generated_papers WHERE 1=1"
        params = []
        if subject is not None:
            query += " AND subject_name=?"
            params.append(subject)
        query += " ORDER BY created_at DESC"
        rows = db.execute(query, params).fetchall()
    items_all = []
    for r in rows:
        try:
            record = json.loads(r["paper_json"])
        except Exception:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("paper_id"), str):
            continue
        if visible_for is not None and record.get("generated_by") != visible_for:
            continue
        items_all.append(_paper_summary(record, docx_available=r["docx_blob"] is not None))
    total = len(items_all)
    items = items_all[(page - 1) * page_size:(page - 1) * page_size + page_size]
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def get_generated_paper(paper_id: str) -> dict | None:
    """Full stored record for one generated paper, or None when unknown."""
    record = None
    with database.connect() as db:
        row = db.execute("SELECT paper_json,docx_blob FROM generated_papers WHERE paper_id=?", (paper_id,)).fetchone()
        if row:
            try:
                record = json.loads(row["paper_json"])
                record["docx_available"] = row["docx_blob"] is not None
            except Exception:
                record = None
    if not isinstance(record, dict) or record.get("paper_id") != paper_id:
        return None
    return record


def get_generated_paper_docx(paper_id: str) -> bytes | None:
    with database.connect() as db:
        row = db.execute(
            "SELECT docx_blob FROM generated_papers WHERE paper_id=?",
            (paper_id,),
        ).fetchone()
    return bytes(row["docx_blob"]) if row and row["docx_blob"] is not None else None


def delete_generated_paper(paper_id: str) -> bool:
    """Remove a history record and its stored DOCX. False when unknown."""
    record = get_generated_paper(paper_id)
    with database.connect() as db:
        cur = db.execute("DELETE FROM generated_papers WHERE paper_id=?", (paper_id,))
    if cur.rowcount == 0 and record is None:
        return False
    return True


def _paper_question(question: dict, index: int, priorities: dict[str, int]) -> dict:
    pages = question.get("source_pages")
    page_number = question.get("page_number", question.get("source_page"))
    if page_number is None and isinstance(pages, list) and pages:
        page_number = pages[0]
    return {"question_id": str(question["question_id"]), "number": index,
            "original_question_number": question.get("original_question_number"),
            "text": _question_text(question), "marks": _marks(question),
            "question_type": question.get("question_type") or question.get("type") or question.get("kind"),
            "section_name": question.get("section_name"),
            "page_number": page_number, "source_pages": pages,
            "source_pdf": question.get("source_pdf"), "source_path": question.get("source_path"),
            "priority": priorities.get(str(question["question_id"]), 1),
            "assets": _question_assets(question), "tables": question.get("tables", [])}


def _question_assets(question: dict) -> list:
    assets = list(question.get("assets") or [])
    for path in question.get("figure_paths") or []:
        if path not in assets:
            assets.append(path)
    return assets


def _safe_asset_path(subject_id: str, source_pdf: object, asset: object,
                     temporary_root: Path) -> Path | None:
    stored = question_bank._stored_figure(subject_id, source_pdf, asset)
    if stored is None:
        return None
    directory = temporary_root / "figures"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / Path(stored["artifact_key"]).name
    target.write_bytes(stored["data"])
    return target


def _save_draft(draft: dict) -> dict:
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    now = draft["updated_at"]
    with database.connect() as db:
        db.execute(
            """INSERT INTO paper_drafts(draft_id,subject_name,status,draft_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(draft_id) DO UPDATE SET
               subject_name=excluded.subject_name,status=excluded.status,
               draft_json=excluded.draft_json,updated_at=excluded.updated_at""",
            (draft["draft_id"], draft.get("subject"), draft.get("status", "draft"),
             json.dumps(draft, ensure_ascii=False), draft.get("created_at") or now, now),
        )
    return draft


def _mutate_draft(draft_id: str, mutation) -> dict:
    with database.connect() as db:
        row = db.execute("SELECT draft_json FROM paper_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if row is None:
        raise KeyError(draft_id)
    try:
        draft = json.loads(row["draft_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Draft storage is invalid") from exc
    result = mutation(draft)
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    with database.connect() as db:
        db.execute(
            """UPDATE paper_drafts SET subject_name=?,status=?,draft_json=?,updated_at=?
               WHERE draft_id=?""",
            (draft.get("subject"), draft.get("status", "draft"),
             json.dumps(draft, ensure_ascii=False), draft["updated_at"], draft_id),
        )
    return result if result is not None else draft


def update_draft(draft_id: str, changes: dict) -> dict:
    def mutate(draft):
        metadata_fields = {"university_name", "college_name", "department_name", "academic_year", "semester", "subject_name", "subject_code", "examination_date"}
        allowed = {"title", "exam_type", "duration", "total_marks", "sections", "instructions", "include_processing_accuracy_notice", *metadata_fields}
        if set(changes) - allowed:
            raise ValueError("Draft metadata may update title, exam_type, duration, total_marks, sections, or instructions")
        for field in ("title", "exam_type", "duration"):
            if field in changes:
                if not isinstance(changes[field], str) or not changes[field].strip():
                    raise ValueError(f"{field} must not be empty")
                draft[field] = changes[field].strip()
        for field in metadata_fields:
            if field in changes:
                value = changes[field]
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(f"{field} must be non-empty text or null")
                draft[field] = value.strip() if isinstance(value, str) else None
        if "include_processing_accuracy_notice" in changes:
            if type(changes["include_processing_accuracy_notice"]) is not bool:
                raise ValueError("include_processing_accuracy_notice must be true or false")
            draft["include_processing_accuracy_notice"] = changes["include_processing_accuracy_notice"]
        if "total_marks" in changes:
            if type(changes["total_marks"]) is not int or not 1 <= changes["total_marks"] <= 1000:
                raise ValueError("total_marks must be a positive integer no greater than 1000")
            draft["total_marks"] = changes["total_marks"]
        if "instructions" in changes:
            if not isinstance(changes["instructions"], list) or any(not isinstance(row, str) for row in changes["instructions"]):
                raise ValueError("instructions must be an array of strings")
            draft["instructions"] = changes["instructions"]
        if "sections" in changes:
            proposed = changes["sections"]
            old = draft["sections"]
            if not isinstance(proposed, list) or len(proposed) != len(old):
                raise ValueError("sections must preserve the draft's section count")
            if any(not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip() for row in proposed):
                raise ValueError("Each section requires a non-empty name")
            if len({row["name"].strip().casefold() for row in proposed}) != len(proposed):
                raise ValueError("Section names must be unique")
            for current, proposed_section in zip(old, proposed):
                target_marks = proposed_section.get("marks_per_question", current["marks_per_question"])
                if target_marks is not None and (type(target_marks) is not int or target_marks < 1):
                    raise ValueError("marks_per_question must be a positive integer or null")
                if any(target_marks is not None and q["marks"] != target_marks for q in current["questions"]):
                    raise ValueError("Section marks must match every question already assigned to that section")
                current["name"] = proposed_section["name"].strip()
                current["instructions"] = proposed_section.get("instructions", current.get("instructions"))
                current["marks_per_question"] = target_marks
        _validate_draft_totals(draft, enforce_total=False)
        for section in draft["sections"]:
            section["question_count"] = len(section["questions"])
    return _mutate_draft(draft_id, mutate)


def get_draft(draft_id: str) -> dict | None:
    try:
        uuid.UUID(draft_id)
    except KeyError:
        return None
    except (ValueError, TypeError, AttributeError):
        return None
    with database.connect() as db:
        row = db.execute("SELECT draft_json FROM paper_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["draft_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Draft storage is invalid") from exc
    return value if isinstance(value, dict) and value.get("draft_id") == str(uuid.UUID(draft_id)) else None


def _find_draft_question(draft: dict, question_id: str) -> tuple[dict, dict] | None:
    for section in draft["sections"]:
        for question in section["questions"]:
            if question["question_id"] == question_id:
                return section, question
    return None


def _validate_draft_totals(draft: dict, *, enforce_total: bool = True) -> None:
    sections = draft.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("Draft must contain at least one section")
    seen: set[str] = set()
    total = 0
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("name"), str) or not section["name"].strip():
            raise ValueError("Every draft section must have a name")
        target_marks = section.get("marks_per_question")
        if target_marks is not None and (type(target_marks) is not int or target_marks < 1):
            raise ValueError(f"Section {section['name']} marks_per_question must be positive or null")
        rows = section.get("questions")
        if not isinstance(rows, list):
            raise ValueError(f"Section {section['name']} questions must be an array")
        for row in rows:
            if not isinstance(row, dict) or not row.get("question_id"):
                raise ValueError("Every draft question must reference a question-bank ID")
            qid = str(row["question_id"])
            if qid in seen:
                raise ValueError(f"Question {qid} appears more than once in the draft")
            seen.add(qid)
            if type(row.get("marks")) is not int or row["marks"] < 1 or (target_marks is not None and row["marks"] != target_marks):
                raise ValueError(f"Question {qid} marks must match section {section['name']} ({target_marks})")
            if not isinstance(row.get("text"), str) or not row["text"].strip():
                raise ValueError(f"Question {qid} text must not be empty")
            total += row["marks"]
    if enforce_total and (type(draft.get("total_marks")) is not int or total != draft["total_marks"]):
        raise ValueError(f"Draft questions total {total} marks; requested total is {draft.get('total_marks')}")


def create_draft(*, title: str, subject_id: str, exam_type: str, total_marks: int,
                 duration: str, blueprint: dict, seed: int | None = None,
                 metadata: dict | None = None, created_by: str | None = None) -> dict:
    """Select from the subject bank through the established generator and persist an editable draft."""
    generated = generate_paper(title=title, subject_id=subject_id, exam_type=exam_type,
                               total_marks=total_marks, duration=duration, blueprint=blueprint, seed=seed,
                               metadata=metadata)
    if generated["shortages"] or generated["total_marks"] != total_marks:
        with database.connect() as db:
            db.execute("DELETE FROM generated_papers WHERE paper_id=?", (generated["paper_id"],))
        details = "; ".join(f"{row['section']}: requested {row.get('requested_questions', total_marks)}; available {row.get('available_questions', row.get('available_marks', 0))}" for row in generated["shortages"])
        raise ValueError("Insufficient eligible questions to build draft" + (f" ({details})" if details else f" (requested {total_marks}, available {generated['total_marks']} marks)"))
    with database.connect() as db:
        db.execute("DELETE FROM generated_papers WHERE paper_id=?", (generated["paper_id"],))
    sections = []
    source_blueprint = blueprint
    for index, output_section in enumerate(generated["sections"]):
        spec = (source_blueprint.get("sections") or [{}])[index] if not source_blueprint.get("auto_select") else {}
        rows = []
        for question in output_section["questions"]:
            bank_question = next((row for row in question_bank.read_subject(subject_id)
                                  if str(row.get("question_id")) == question["question_id"]), None)
            if bank_question is None:
                raise ValueError(f"Question {question['question_id']} no longer exists in the subject Question Bank")
            rows.append({**question, "bank_question_id": question["question_id"],
                         "text": question["text"], "assets": bank_question.get("assets", []),
                         "tables": bank_question.get("tables", [])})
        sections.append({"name": output_section["name"], "instructions": output_section.get("instructions"),
                         "question_count": spec.get("question_count", len(rows)),
                         "marks_per_question": spec.get("marks_per_question") if not source_blueprint.get("auto_select") else None,
                         "questions": rows})
    # The temporary final-generation record has been removed; only the draft is durable.
    draft = {"draft_id": str(uuid.uuid4()), "title": generated["title"], "subject": subject_id,
             "exam_type": generated["exam_type"], "duration": generated["duration"],
             **{key: generated.get(key) for key in ("university_name", "college_name", "department_name", "academic_year", "semester", "subject_name", "subject_code", "examination_date")},
             "include_processing_accuracy_notice": bool((metadata or {}).get("include_processing_accuracy_notice", False)),
             "total_marks": total_marks, "blueprint_id": generated["blueprint_id"],
             "instructions": generated["instructions"], "sections": sections,
             "created_by": created_by, "created_at": datetime.now(timezone.utc).isoformat(),
             "status": "draft"}
    _validate_draft_totals(draft)
    return _save_draft(draft)


def add_draft_question(draft_id: str, question_id: str, section_name: str) -> dict:
    def mutate(draft):
        section = next((row for row in draft["sections"] if row["name"] == section_name), None)
        if section is None:
            raise ValueError("Target section does not exist in this draft")
        if _find_draft_question(draft, question_id):
            raise ValueError("Question is already in this draft")
        bank_question = next((row for row in question_bank.read_subject(draft["subject"])
                          if str(row.get("question_id")) == question_id), None)
        if bank_question is None:
            raise KeyError("Question not found in this subject's Question Bank")
        if section["marks_per_question"] is not None and _marks(bank_question) != section["marks_per_question"]:
            raise ValueError(f"Question marks must exactly match section marks ({section['marks_per_question']})")
        _overrides = question_priorities.get_priority_overrides({question_id})
        priorities = {question_id: _overrides.get(question_id, bank_question.get("priority") or 1)}
        item = _paper_question(bank_question, sum(len(s["questions"]) for s in draft["sections"]) + 1, priorities)
        item.update(bank_question_id=question_id, text=item["text"])
        section["questions"].append(item)
        _validate_draft_totals(draft, enforce_total=False)
        for section in draft["sections"]:
            section["question_count"] = len(section["questions"])
    return _mutate_draft(draft_id, mutate)


def update_draft_question(draft_id: str, question_id: str, changes: dict) -> dict:
    def mutate(draft):
        found = _find_draft_question(draft, question_id)
        if not found:
            raise KeyError("Question not found in draft")
        section, question = found
        allowed = {"text", "marks", "section"}
        if set(changes) - allowed:
            raise ValueError("Only text, marks, and section can be edited for a draft question")
        # Use the Question Bank's shared field validation so both editors accept
        # the same text and marks representation. Draft edits remain draft-local.
        question_bank.validate_question_changes({key: value for key, value in changes.items() if key in {"text", "marks"}})
        if "text" in changes:
            if not isinstance(changes["text"], str) or not changes["text"].strip():
                raise ValueError("Question text must not be empty")
            question["text"] = changes["text"].strip()
        if "marks" in changes:
            value = changes["marks"]
            if type(value) is not int or value < 1:
                raise ValueError("Question marks must be a positive integer")
            if section["marks_per_question"] is not None and value != section["marks_per_question"]:
                raise ValueError("Question marks must match its section marks")
            question["marks"] = value
        if "section" in changes and changes["section"] != section["name"]:
            target = next((row for row in draft["sections"] if row["name"] == changes["section"]), None)
            if target is None:
                raise ValueError("Target section does not exist in this draft")
            if target["marks_per_question"] is not None and question["marks"] != target["marks_per_question"]:
                raise ValueError("Question marks must exactly match target section marks")
            section["questions"].remove(question)
            target["questions"].append(question)
        _validate_draft_totals(draft, enforce_total=False)
        for section in draft["sections"]:
            section["question_count"] = len(section["questions"])
    return _mutate_draft(draft_id, mutate)


def regenerate_draft_question(draft_id: str, question_id: str) -> dict:
    """Replace one draft question with an unused bank question of the same marks."""
    draft = get_draft(draft_id)
    if draft is None:
        raise KeyError(draft_id)
    location = None
    for section in draft.get("sections", []):
        for index, question in enumerate(section.get("questions", [])):
            if str(question.get("question_id")) == str(question_id):
                location = (section, index, question)
    if location is None:
        raise KeyError(question_id)
    section, index, old_question = location
    marks = old_question.get("marks")
    used_ids = {
        str(question.get("question_id"))
        for other_section in draft.get("sections", [])
        for question in other_section.get("questions", [])
        if question.get("question_id") and str(question.get("question_id")) != str(question_id)
    }
    pool = [
        candidate for candidate in question_bank.read_subject(draft["subject"])
        if _marks(candidate) == marks
        and _question_text(candidate)
        and str(candidate.get("question_id")) not in used_ids
    ]
    if not pool:
        raise ValueError("No replacement question is available for this slot")
    replacement = pool[0]
    _replacement_id = str(replacement.get("question_id"))
    _overrides = question_priorities.get_priority_overrides({_replacement_id})
    priorities = {_replacement_id: _overrides.get(_replacement_id, replacement.get("priority") or 1)}
    new_question = _paper_question(replacement, old_question.get("number", index + 1), priorities)
    new_question["number"] = old_question.get("number", index + 1)
    section["questions"][index] = new_question
    _validate_draft_totals(draft, enforce_total=False)
    for current_section in draft["sections"]:
        current_section["question_count"] = len(current_section["questions"])
    _save_draft(draft)
    return new_question


def remove_draft_question(draft_id: str, question_id: str) -> dict:
    def mutate(draft):
        found = _find_draft_question(draft, question_id)
        if not found:
            raise KeyError("Question not found in draft")
        found[0]["questions"].remove(found[1])
        found[0]["question_count"] = len(found[0]["questions"])
    return _mutate_draft(draft_id, mutate)


def reorder_draft_questions(draft_id: str, question_ids: list[str]) -> dict:
    def mutate(draft):
        current = {q["question_id"]: (section["name"], q) for section in draft["sections"] for q in section["questions"]}
        if len(question_ids) != len(current) or set(question_ids) != set(current):
            raise ValueError("question_ids must list every draft question exactly once")
        ordered = [(current[qid][0], current[qid][1]) for qid in question_ids]
        section_order = list(dict.fromkeys(name for name, _ in ordered))
        closed: set[str] = set()
        previous = None
        for name in (section for section, _ in ordered):
            if name != previous:
                if name in closed:
                    raise ValueError("Question order must keep each section's questions together")
                if previous is not None:
                    closed.add(previous)
                previous = name
        for section in draft["sections"]:
            section["questions"].clear()
        for index, (section_name, question) in enumerate(ordered, 1):
            question["number"] = index
            next(section for section in draft["sections"] if section["name"] == section_name)["questions"].append(question)
        by_name = {section["name"]: section for section in draft["sections"]}
        draft["sections"] = [by_name[name] for name in section_order] + [
            section for section in draft["sections"] if section["name"] not in section_order]
    return _mutate_draft(draft_id, mutate)


def export_draft_docx(draft_id: str, *, include_processing_accuracy_notice: bool | None = None) -> bytes | None:
    draft = get_draft(draft_id)
    if draft is None:
        return None
    _validate_draft_totals(draft)
    paper = {"title": draft["title"], "subject": draft["subject"], "exam_type": draft["exam_type"],
             "duration": draft["duration"], "requested_total_marks": draft["total_marks"],
             "instructions": draft.get("instructions", []), "sections": draft["sections"],
             **{key: draft.get(key) for key in ("university_name", "college_name", "department_name", "academic_year", "semester", "subject_name", "subject_code", "examination_date")},
             "include_processing_accuracy_notice": draft.get("include_processing_accuracy_notice", False) if include_processing_accuracy_notice is None else include_processing_accuracy_notice}
    with tempfile.TemporaryDirectory(prefix=f"masterdeorca-draft-{draft_id}-") as temporary:
        path = Path(temporary) / f"{uuid.UUID(draft_id)}.docx"
        _docx(paper, path)
        content = path.read_bytes()
    with database.connect() as db:
        db.execute(
            "UPDATE paper_drafts SET docx_blob=?,updated_at=? WHERE draft_id=?",
            (content, datetime.now(timezone.utc).isoformat(), draft_id),
        )
    return content


def get_draft_docx(draft_id: str) -> bytes | None:
    with database.connect() as db:
        row = db.execute("SELECT docx_blob FROM paper_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    return bytes(row["docx_blob"]) if row and row["docx_blob"] is not None else None

