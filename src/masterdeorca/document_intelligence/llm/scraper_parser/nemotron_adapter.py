from __future__ import annotations

import json
import re
from typing import Any

from functions.QuesParser.ParsePipeLIne.schema import (
    QuestionBank,
    QuestionRecord,
    ProcessingMetadata,
    SCHEMA_VERSION,
    ReviewStatus,
    generate_question_id,
)


class NemotronToExistingAdapter:
    def __init__(self, source_pdf: str = "document.pdf"):
        self.source_pdf = source_pdf

    def parse_nemotron(self, raw: dict[str, Any] | str) -> QuestionBank:
        data = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                data = {"content": raw}
        content = data.get("content") or data.get("text") or ""
        if isinstance(content, (dict, list)):
            content = json.dumps(content)
        questions = []
        lines = str(content).splitlines()
        current_section = None
        qnum = 0
        i = 0
        while i < len(lines):
            line = lines[i]
            if re.search(r"<class_Page-header>|<class_Page-footer>", line):
                i += 1
                continue
            msec = re.search(r"<class_Section-header>(.+?)</class_Section-header>", line)
            if msec:
                current_section = msec.group(1).strip()
                i += 1
                continue
            mq = re.match(r'^\s*Q\.?\s*(\d+)(?:\s*\(([a-z])\))?\s*[\.\)]?\s+(.+)$', line, re.I)
            if mq:
                qnum += 1
                _letter = mq.group(2)
                text = mq.group(3).strip()
                j = i + 1
                body = []
                while j < len(lines):
                    l2 = lines[j]
                    if re.match(r'^\s*Q\.?\s*\d', l2):
                        break
                    if re.search(r"<class_Page-header>|<class_Page-footer>", l2):
                        j += 1
                        continue
                    body.append(l2)
                    j += 1
                full = text + "\n" + "\n".join(body)
                mm = re.search(r'\[(\d+(?:\.\d+)?)\s*(?:marks?|M)\]|\((\d+(?:\.\d+)?)\s*(?:marks?|M)\)', full, re.I)
                marks = None
                if mm:
                    marks = float(mm.group(1) or mm.group(2))
                qid = generate_question_id(self.source_pdf, full, qnum)
                q = QuestionRecord(
                    question_id=qid,
                    original_text=full.strip(),
                    cleaned_text=full.strip(),
                    original_label=(f"Q.{mq.group(1)}" + (f"({_letter})" if _letter else "")),
                    original_question_number=(mq.group(1) + (_letter or "")),
                    marks=marks,
                    marks_notation=mm.group(0) if mm else None,
                    section_name=current_section,
                    source_pdf=self.source_pdf,
                    source_pages=[qnum],
                    confidence=1.0,
                    review_status=ReviewStatus.PENDING,
                )
                questions.append(q)
                i = j
                continue
            i += 1
        bank = QuestionBank(
            schema_version=SCHEMA_VERSION,
            metadata=ProcessingMetadata(
                providers_used=["nemotron-3.5-parse"],
                total_pages_processed=1,
                total_questions_found=len(questions),
                total_assets_linked=0,
                total_tables_linked=0,
                incomplete_records=0,
                source_manifests=[],
            ),
            questions=questions,
        )
        return bank
