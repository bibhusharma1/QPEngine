import json

import httpx
import pytest

from src.masterdeorca.document_intelligence.lite.structurer import StructureWithLLM


@pytest.fixture(autouse=True)
def clear_nvidia_keys(monkeypatch):
    for key in ("NVIDIA_API", "NVIDIA_API_KEY", "NVIDIA_API_KEYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "config.settings.NVIDIA_STRUCTURE_RETRIES", 2
    )
    monkeypatch.setattr("config.settings.NVIDIA_STRUCTURE_BACKOFF_BASE", 0)
    monkeypatch.setattr("config.settings.NVIDIA_API_KEYS", [])


def _answer():
    return {
        "schema_version": "1.1.0",
        "metadata": {},
        "questions": [{
            "question_id": "q1",
            "original_text": "Continued question",
            "cleaned_text": "Continued question",
            "source_pages": [1, 2],
        }],
    }


def _mock_responses(monkeypatch, responses, requests):
    def fake_post(url, **kwargs):
        requests.append((url, kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(response)}}]
        })

    monkeypatch.setattr("src.masterdeorca.document_intelligence.lite.structurer.httpx.post", fake_post)


def test_nvidia_chat_completions_returns_schema_validated_question_bank(monkeypatch):
    requests = []
    _mock_responses(monkeypatch, [_answer()], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    result = StructureWithLLM().structure(
        raw_response={"content": "page 1 and page 2", "pages": [{"page": 1}, {"page": 2}]},
        normalized={"raw_content": "Page 1 question continues\nPage 2 question end"},
        source_pdf="paper.pdf",
    )

    url, request = requests[0]
    assert url.endswith("/v1/chat/completions")
    assert request["headers"]["Authorization"] == "Bearer test-nvidia-api"
    assert request["json"]["model"] == "nvidia/nemotron-3.5-super-vl-120b-a12b"
    assert request["json"]["response_format"]["type"] == "json_schema"
    assert request["json"]["response_format"]["json_schema"]["name"] == "question_bank_record"
    assert request["json"]["max_tokens"] == 4096
    assert request["json"]["reasoning_effort"] == "none"
    prompt = request["json"]["messages"][1]["content"]
    assert "Page 1 question continues" in prompt
    assert "Page 2 question end" in prompt
    assert result["questions"][0]["source_pdf"] == "paper.pdf"
    assert result["metadata"]["providers_used"] == [
        "nvidia_nemotron_parse",
        "nvidia_nemotron_super_vl",
    ]
    assert result["metadata"]["total_pages_processed"] == 2


def test_invalid_json_fails_without_retrying_or_repeating_extraction(monkeypatch, caplog):
    requests = []
    malformed = httpx.Response(
        200, json={"choices": [{"message": {"content": '{"questions":'}}]}
    )
    _mock_responses(monkeypatch, [malformed, _answer()], requests)
    monkeypatch.setenv("NVIDIA_API_KEY", "legacy-nvidia-key")

    with pytest.raises(RuntimeError, match="json_decode") as error:
        StructureWithLLM().structure(
            {"pages": [{"page": 1}, {"page": 2}]},
            {"raw_content": "already extracted content"},
            "paper.pdf",
        )

    assert len(requests) == 1
    assert "line=" in str(error.value)


def test_markdown_fenced_json_is_parsed(monkeypatch):
    requests = []
    fenced = httpx.Response(
        200,
        json={"choices": [{"message": {"content": f"```json\n{json.dumps(_answer())}\n```"}}]},
    )
    _mock_responses(monkeypatch, [fenced], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    result = StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")

    assert result["questions"][0]["question_id"] == "q1"


def test_parsed_structured_field_is_used_when_present(monkeypatch):
    requests = []
    _mock_responses(
        monkeypatch,
        [httpx.Response(200, json={
            "choices": [{"message": {"content": None, "parsed": _answer()}}]
        })],
        requests,
    )
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    result = StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")

    assert result["questions"][0]["question_id"] == "q1"


@pytest.mark.parametrize(
    ("message", "failure"),
    [
        ({"content": None}, "message.content is null"),
        ({"content": ""}, "message.content is empty"),
        ({"content": 42}, "message content must be text"),
        ({"refusal": "I cannot do that"}, "contains a refusal"),
        ({"tool_calls": [{"id": "call_1"}]}, "tool calls"),
    ],
)
def test_unusable_message_responses_fail_without_retry(monkeypatch, message, failure):
    requests = []
    response = httpx.Response(200, json={"choices": [{
        "finish_reason": "stop",
        "message": message,
    }]})
    _mock_responses(monkeypatch, [response, _answer()], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    with pytest.raises(RuntimeError, match=failure):
        StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")

    assert len(requests) == 1


def test_unexpected_response_shape_fails_without_retry(monkeypatch):
    requests = []
    _mock_responses(monkeypatch, [httpx.Response(200, json={"detail": "unexpected"}), _answer()], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    with pytest.raises(RuntimeError, match="no non-empty choices"):
        StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")

    assert len(requests) == 1


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (
            httpx.Response(
                200,
                json={
                    "object": "error",
                    "type": "invalid_request_error",
                    "message": "invalid options",
                },
            ),
            "api_error",
        ),
        (httpx.Response(200, text="not JSON"), "non-JSON HTTP 200"),
    ],
)
def test_http_200_api_or_body_errors_are_classified_without_retry(
    monkeypatch, response, failure
):
    requests = []
    _mock_responses(monkeypatch, [response, _answer()], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    with pytest.raises(RuntimeError, match=failure):
        StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")

    assert len(requests) == 1


def test_transient_api_failure_retries_then_succeeds(monkeypatch, caplog):
    requests = []
    _mock_responses(
        monkeypatch,
        [httpx.Response(503, text="temporarily unavailable"), _answer()],
        requests,
    )
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")
    caplog.set_level("INFO")

    result = StructureWithLLM().structure({}, {"raw_content": "cached extraction"}, "paper.pdf")

    assert len(requests) == 2
    assert result["questions"]
    assert "retry=True" in caplog.text


def test_transport_timeout_retries_then_succeeds(monkeypatch):
    requests = []
    _mock_responses(
        monkeypatch,
        [httpx.ReadTimeout("timed out"), _answer()],
        requests,
    )
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    result = StructureWithLLM().structure({}, {"raw_content": "cached extraction"}, "paper.pdf")

    assert len(requests) == 2
    assert result["questions"]


def test_truncated_response_uses_corrective_question_level_retry(monkeypatch, caplog):
    requests = []
    truncated = httpx.Response(200, json={
        "choices": [{
            "finish_reason": "length",
            "message": {"content": '{"questions":'},
        }],
        "usage": {"prompt_tokens": 200, "completion_tokens": 8192, "total_tokens": 8392},
    })
    _mock_responses(monkeypatch, [truncated, _answer()], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")
    caplog.set_level("INFO")

    result = StructureWithLLM().structure(
        {"question": {"label": "Q1", "text": "Question"}},
        {"figure_assets": []},
        "paper.pdf",
        question_number="Q1",
        total_questions=100,
    )

    prompts = [request["json"]["messages"][1]["content"] for _, request in requests]
    assert len(requests) == 2
    assert "previous response was truncated" not in prompts[0].lower()
    assert "previous response was truncated" in prompts[1].lower()
    assert result["questions"]
    assert "completion_tokens" in caplog.text


def test_reasoning_content_is_not_used_as_question_json(monkeypatch):
    requests = []
    response = httpx.Response(200, json={
        "choices": [{
            "finish_reason": "stop",
            "message": {
                "content": None,
                "reasoning_content": json.dumps(_answer()),
            },
        }],
    })
    _mock_responses(monkeypatch, [response, _answer()], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    with pytest.raises(RuntimeError, match="message.content is null"):
        StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")

    assert len(requests) == 1


def test_invalid_bank_retries_with_schema_feedback_and_redacts_key(monkeypatch):
    requests = []
    _mock_responses(monkeypatch, [{"questions": []}, {"questions": []}], requests)
    monkeypatch.setenv("NVIDIA_API", "test-secret")

    with pytest.raises(RuntimeError, match="NVIDIA Nemotron structuring failed") as error:
        StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")

    assert len(requests) == 2
    assert "test-secret" not in str(error.value)
    assert "non-empty questions array" in str(error.value)
    correction = requests[1][1]["json"]["messages"][1]["content"]
    assert "Validation errors:" in correction
    assert "Previous response:" in correction


def test_pydantic_schema_errors_are_logged_and_retried(monkeypatch, caplog):
    requests = []
    _mock_responses(
        monkeypatch,
        [{"questions": [{"original_text": 123}]}, _answer()],
        requests,
    )
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    result = StructureWithLLM().structure({}, {}, "paper.pdf", question_number="Q4")

    assert len(requests) == 2
    assert result["questions"][0]["question_id"] == "q1"
    assert "questions.0.structured.question_id" in caplog.text
    assert "string_type" in caplog.text
    correction = requests[1][1]["json"]["messages"][1]["content"]
    assert "questions.0.structured.question_id" in correction
    assert "questions.0.structured.original_text" in correction
    assert "Previous response:" in correction
    assert '"original_text": 123' in correction


@pytest.mark.parametrize("value", [None, 42, {"content": "not text"}])
def test_strip_fences_rejects_non_text_input(value):
    from src.masterdeorca.document_intelligence.lite.structurer import _strip_fences

    with pytest.raises(ValueError, match="expected textual JSON content"):
        _strip_fences(value)


def test_structurer_links_only_known_cropped_assets(monkeypatch):
    requests = []
    assets = [{
        "asset_id": "known-asset",
        "asset_type": "figure",
        "classification": "figure",
        "path": "assets/figures/figure.png",
        "source_page": 2,
        "bbox": [0.1, 0.2, 0.8, 0.9],
        "description": "figure cropped from page 2",
        "verified": True,
    }]
    answer = _answer()
    answer["questions"][0]["assets"] = [{
        "asset_id": "known-asset",
        "asset_type": "figure",
    }]
    answer["questions"][0]["source_pages"] = [2]
    _mock_responses(monkeypatch, [answer], requests)
    monkeypatch.setenv("NVIDIA_API", "test-nvidia-api")

    result = StructureWithLLM().structure(
        {"pages": [{"page": 1}, {"page": 2}]},
        {"raw_content": "See figure", "figure_assets": assets},
        "paper.pdf",
    )

    linked = result["questions"][0]["assets"][0]
    assert linked["asset_id"] == "known-asset"
    assert linked["path"] == assets[0]["path"]
    assert linked["source_page"] == 2
    assert "normalized bbox" in linked["description"]
    assert result["metadata"]["total_assets_linked"] == 1


def test_no_nvidia_key_fails_clearly(monkeypatch):
    with pytest.raises(RuntimeError, match="configure NVIDIA_API"):
        StructureWithLLM().structure({}, {"raw_content": "question"}, "paper.pdf")
