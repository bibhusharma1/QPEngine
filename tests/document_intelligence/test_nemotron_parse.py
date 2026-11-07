import pymupdf
import httpx
import pytest

from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse import NemotronParseClient


def _make_pdf(path, pages=2):
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 72), f"Page {i + 1}")
    doc.save(str(path))
    doc.close()
    return path


def test_request_format_document_part(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["ctype"] = request.headers.get("content-type", "")
        body = request.content.decode("latin-1")
        captured["body"] = body
        return httpx.Response(200, json={"content": "<x_0.1><y_0.2>Q.1 hi</x_0.1><y_0.2><class_Text>"})

    transport = httpx.MockTransport(handler)
    client = NemotronParseClient(transport=transport)
    client.keys = ["secret-key"]
    client.retries = 1
    client.backoff_base = 0.0

    result = client.parse_page_image(b"\x89PNG\r\n\x1a\nfake", "image/png", page_number=1)
    assert result["content"].startswith("<x_0.1>")
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/parse")
    assert captured["auth"] == "Bearer secret-key"
    assert "multipart/form-data" in captured["ctype"]
    assert 'name="document"' in captured["body"]
    assert 'filename="page1.png"' in captured["body"]
    assert 'name="model"' not in captured["body"]
    assert 'name="file"' not in captured["body"]


def test_retry_on_429_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"content": "ok"})

    transport = httpx.MockTransport(handler)
    client = NemotronParseClient(transport=transport)
    client.keys = ["k"]
    client.retries = 2
    client.backoff_base = 0.0

    out = client.parse_page_image(b"\x89PNG\r\n\x1a\nimg", "image/png", page_number=1)
    assert out == {"content": "ok"}
    assert calls["n"] == 2


def test_retry_after_is_respected(monkeypatch):
    calls = {"n": 0}
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, text="rate limited")
        return httpx.Response(200, json={"content": "ok"})

    monkeypatch.setattr(
        "src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_parse.time.sleep",
        delays.append,
    )
    client = NemotronParseClient(transport=httpx.MockTransport(handler))
    client.keys = ["k"]
    client.retries = 2
    client.backoff_base = 0.1

    assert client.parse_page_image(b"\x89PNG\r\n\x1a\nimg", "image/png", page_number=1) == {"content": "ok"}
    assert delays == [3.0]


def test_failure_raises_after_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text='{"object":"error","message":"bad"}')

    transport = httpx.MockTransport(handler)
    client = NemotronParseClient(transport=transport)
    client.keys = ["k"]
    client.retries = 2
    client.backoff_base = 0.0

    with pytest.raises(RuntimeError) as exc:
        client.parse_page_image(b"\x89PNG\r\n\x1a\nimg", "image/png", page_number=1)
    assert "400" in str(exc.value)
    assert calls["n"] == 1


def test_parse_document_normalized_combines_pages(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        # echo page number via filename not available; return marker content
        return httpx.Response(200, json={"content": "CONTENT_FOR_PAGE"})

    transport = httpx.MockTransport(handler)
    client = NemotronParseClient(transport=transport)
    client.keys = ["k"]
    client.retries = 1
    client.backoff_base = 0.0

    pdf = _make_pdf(tmp_path / "t.pdf", pages=2)
    extracted = client.parse_document_normalized(pdf)
    assert extracted.metadata["page_count"] == 2
    assert "Page 1" in extracted.metadata["raw"]["content"]
    assert "Page 2" in extracted.metadata["raw"]["content"]
    assert len(extracted.pages) == 2


def test_no_keys_raises():
    client = NemotronParseClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"content": "x"})))
    client.keys = []
    with pytest.raises(RuntimeError):
        client.parse_page_image(b"\x89PNG\r\n\x1a\nimg", "image/png", page_number=1)


def test_non_image_document_is_rejected_before_request():
    client = NemotronParseClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"content": "x"})))
    client.keys = ["k"]
    with pytest.raises(ValueError, match="not valid"):
        client.parse_page_image(b"%PDF-1.7", "image/png", page_number=1)
