import json
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
from pathlib import Path

import fitz
import httpx
import pytest
from docx import Document
from PIL import Image

from src.masterdeorca import app as app_module
from src.masterdeorca.document_intelligence import page_images
from src.masterdeorca.document_intelligence.lite import two_stage
from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import NemotronParseClient
from tests.test_api import _client


@pytest.fixture(autouse=True)
def clear_nvidia_keys(monkeypatch):
    for key in ("NVIDIA_API", "NVIDIA_API_KEY", "NVIDIA_API_KEYS"):
        monkeypatch.delenv(key, raising=False)


def _make_pdf(path: Path) -> Path:
    document = fitz.open()
    for number in range(1, 3):
        page = document.new_page()
        page.insert_text((72, 72), f"Q.{number} Explain test page {number}.")
        if number == 1:
            page.draw_rect(fitz.Rect(100, 120, 400, 400), color=(0, 0, 0), width=3)
    document.save(path)
    document.close()
    return path


def _make_docx(path: Path) -> Path:
    document = Document()
    document.add_paragraph("Q.1 Explain test page one.")
    document.add_page_break()
    document.add_paragraph("Q.2 Explain test page two.")
    document.save(path)
    return path


@pytest.mark.parametrize("input_format", ["pdf", "docx"])
def test_process_background_job_sends_only_page_images_before_nvidia_structuring(
    input_format, tmp_path, monkeypatch
):
    events = []
    requests = []

    def handle_nemotron(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-nvidia-key"
        message = BytesParser(policy=default).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        part = next(part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "document")
        filename = part.get_filename()
        mime = part.get_content_type()
        data = part.get_payload(decode=True)
        requests.append((filename, mime, data))
        events.append(("nemotron", filename))
        figure = (
            "<x_0.1><y_0.1><class_Figure>FIGURE 1</class_Figure>"
            "<x_0.9><y_0.9>\n"
            if len(requests) == 1
            else ""
        )
        return httpx.Response(
            200,
            json={"content": f"{figure}Q.{len(requests)} Explain test page {len(requests)}."},
        )

    transport = httpx.MockTransport(handle_nemotron)

    class TestNemotronClient(NemotronParseClient):
        def __init__(self):
            super().__init__(transport=transport)
            self.keys = ["test-nvidia-key"]

    monkeypatch.setattr(two_stage, "NemotronParseClient", TestNemotronClient)
    monkeypatch.setattr("config.settings.PARSER_MODE", "llm")
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    def fake_structuring_post(url, **kwargs):
        events.append(("nvidia_structurer", url))
        assert len([event for event in events if event[0] == "nemotron"]) == 2
        prompt = kwargs["json"]["messages"][1]["content"]
        if "Classify the supplied extracted blocks" in prompt:
            blocks = json.loads(
                prompt.split("Blocks:\n", 1)[1].split(
                    "\n\nFigure assets:\n", 1
                )[0]
            )
            assets = json.loads(prompt.split("\n\nFigure assets:\n", 1)[1])
            questions = []
            for block in blocks:
                text = block["text"]
                if not text.startswith("Q."):
                    continue
                question_number = text.split(maxsplit=1)[0].replace(".", "")
                questions.append({
                    "question_number": question_number,
                    "block_ids": [block["block_id"]],
                    "figure_asset_ids": [
                        asset["asset_id"]
                        for asset in assets
                        if asset["source_page"] == block["page_number"]
                    ],
                })
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps({"questions": questions})}}]},
            )

        question_json = prompt.split(
            "Identified question and its surrounding section context:\n", 1
        )[1].split("\n\nFigure assets relevant to this question:\n", 1)[0]
        question_context = json.loads(question_json)["question"]
        normalized_json = prompt.split(
            "Figure assets relevant to this question:\n", 1
        )[1]
        normalized = json.loads(normalized_json)
        assets = normalized["figure_assets"]
        if question_context["source_page"] == 1:
            assert assets
        answer = {
                "schema_version": "1.1.0",
                "metadata": {},
                "questions": [{
                    "question_id": "test-question",
                    "original_text": question_context["text"],
                    "cleaned_text": question_context["text"],
                    "source_pages": question_context["source_pages"],
                    "assets": ([{
                    "asset_id": assets[0]["asset_id"],
                    "asset_type": "figure",
                    }] if assets else []),
                }],
            }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(answer)}}]},
        )

    monkeypatch.setattr(
        "src.masterdeorca.document_intelligence.lite.structurer.httpx.post",
        fake_structuring_post,
    )

    if input_format == "pdf":
        source = _make_pdf(tmp_path / "source.pdf")
    else:
        source = _make_docx(tmp_path / "source.docx")
        rendered_pdf = _make_pdf(tmp_path / "docx-render.pdf")

        def fake_docx_to_pdf(_docx_path: Path, out_dir: Path) -> Path:
            target = out_dir / "converted.pdf"
            target.write_bytes(rendered_pdf.read_bytes())
            return target

        monkeypatch.setattr(page_images, "_docx_to_pdf", fake_docx_to_pdf)

    with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
        upload_route = "pdfs" if input_format == "pdf" else "documents"
        upload = client.post(
            f"/subjects/OperatingSys/{upload_route}",
            files=[("files", (source.name, source.read_bytes(), "application/octet-stream"))],
        )
        assert upload.status_code == 201

        response = client.post(
            f"/subjects/OperatingSys/documents/{source.name}/process",
            json={"force": True},
        )
        assert response.status_code == 202
        job = client.get(f"/jobs/{response.json()['job_id']}").json()

    assert job["status"] == "succeeded", job
    assert job["progress"]["total_questions"] == 2
    assert job["progress"]["processed_questions"] == 2
    assert job["progress"]["completion_percent"] == 100
    assert len(requests) == 2
    assert all(mime == "image/png" for _, mime, _ in requests)
    assert all(data.startswith(b"\x89PNG\r\n\x1a\n") for _, _, data in requests)
    assert [name for name, _, _ in requests] == ["page-00001.png", "page-00002.png"]
    assert [event[0] for event in events[:2]] == ["nemotron", "nemotron"]
    assert all(event[0] == "nvidia_structurer" for event in events[2:])
    assert len(events[2:]) == 1  # one block classification batch
    document = app_module.database.get_document("OperatingSys", source.name)
    parsed_artifact = app_module.database.get_artifact(
        document["document_id"], "questions_parsed.json"
    )
    assert parsed_artifact is not None
    with app_module.database.connect() as db:
        job_log = "\n".join(
            row["message"]
            for row in db.execute(
                "SELECT message FROM processing_logs WHERE job_id=? ORDER BY created_at",
                (job["job_id"],),
            ).fetchall()
        )
    assert f"rendered 2 page images from {input_format} input" in job_log
    assert "Nemotron request page 1/2 image=page-00001.png mime=image/png" in job_log
    assert "Nemotron response page 2/2 image=page-00002.png mime=image/png HTTP 200" in job_log
    assert job_log.index("HTTP 200") < job_log.index("NVIDIA structuring started")
    bank = json.loads(parsed_artifact["data"].decode("utf-8"))
    assert len(bank["questions"]) == 2
    assert [item["original_question_number"] for item in bank["questions"]] == ["Q1", "Q2"]
    assert bank["questions"][0]["assets"][0]["asset_id"]
    with app_module.database.connect() as db:
        figure_row = db.execute(
            "SELECT data FROM stored_artifacts WHERE document_id=? AND artifact_type='figure'",
            (document["document_id"],),
        ).fetchone()
    assert figure_row is not None
    with Image.open(BytesIO(bytes(figure_row["data"]))) as figure:
        assert figure.width > 0 and figure.height > 0
