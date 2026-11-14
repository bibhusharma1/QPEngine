import json

import httpx
import pytest

from src.masterdeorca.document_intelligence.lite.structurer import (
    BlockBatchClassification,
    StructureWithLLM,
    _normalize_block_classification_payload,
    _apply_text_priority_evidence,
)


@pytest.fixture(autouse=True)
def clear_nvidia_keys(monkeypatch):
    for key in ("NVIDIA_API", "NVIDIA_API_KEY", "NVIDIA_API_KEYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "config.settings.NVIDIA_STRUCTURE_RETRIES", 2
    )
    monkeypatch.setattr("config.settings.NVIDIA_STRUCTURE_MAX_TOKENS", 8192)
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


def test_mistral_nested_question_fields_normalize_to_existing_reference_schema():
    payload = _normalize_block_classification_payload({
        "questions": [{
            "question_number": "Q3",
            "block_ids": ["b1"],
            "instruction_block_ids": ["b2"],
            "sub_questions": [{
                "question_number": "Q3(a)",
                "block_ids": ["b3"],
                "instruction_block_ids": ["b4"],
                "sub_questions": [{
                    "question_number": "(i)",
                    "block_ids": ["b5"],
                    "marks": "2",
                }],
            }],
        }],
    })

    validated = BlockBatchClassification.model_validate(payload)
    question = validated.questions[0]
    assert question.question_number == "Q3"
    assert question.instruction_block_ids == ["b2"]
    assert question.sub_questions[0].label == "(a)"
    assert question.sub_questions[0].question_number == "Q3(a)"
    assert question.sub_questions[0].instruction_block_ids == ["b4"]
    assert question.sub_questions[0].block_ids == ["b3"]
    assert question.sub_questions[1].label == "(i)"
    assert question.sub_questions[1].parent_label == "(a)"
    assert question.sub_questions[1].block_ids == ["b5"]
    assert question.sub_questions[1].marks == "2"


def test_canonical_nested_question_schema_accepts_numbers_instructions_and_priority():
    from functions.QuesParser.ParsePipeLIne.schema import QuestionBank

    bank = QuestionBank.model_validate({"questions": [{
        "question_id": "q3",
        "question_number": "Q3",
        "original_text": "Main question",
        "source_pdf": "paper.pdf",
        "priority": 2,
        "instruction_block_ids": ["b-instruction"],
        "sub_questions": [{
            "sub_id": "q3a",
            "question_number": "Q3(a)",
            "original_text": "Part a",
            "instruction_block_ids": ["b-sub-instruction"],
            "sub_questions": [{
                "sub_id": "q3ai",
                "question_number": "Q3(a)(i)",
                "original_text": "Part i",
                "priority": 3,
            }],
        }],
    }]})
    question = bank.questions[0]
    assert question.original_question_number == "Q3"
    assert question.sub_questions[0].original_question_number == "Q3(a)"
    assert question.sub_questions[0].instruction_block_ids == ["b-sub-instruction"]
    assert question.sub_questions[0].sub_questions[0].priority == 3


def test_extracted_text_priority_overrides_model_value_and_requires_valid_scale():
    payload = _normalize_block_classification_payload({"questions": [{
        "question_number": "Q22",
        "block_ids": ["b1"],
        "priority": 1,
        "priority_evidence": "visual label",
    }]})
    _apply_text_priority_evidence(payload, [{
        "block_id": "b1",
        "original_text": "Q 22. Differentiate between CNN and RNN. Marks 3, priority 2",
    }])
    question = BlockBatchClassification.model_validate(payload).questions[0]
    assert question.priority == 2
    assert question.priority_evidence.casefold() == "priority 2"

    with pytest.raises(Exception):
        BlockBatchClassification.model_validate({"questions": [{
            "question_number": "Q1",
            "priority": 4,
            "priority_evidence": "Priority 4",
        }]})


def test_visual_priority_uses_page_image_and_preserves_evidence(monkeypatch):
    requests = []
    _mock_responses(monkeypatch, [{
        "questions": [{
            "question_number": "Q1",
            "block_ids": ["b1"],
            "priority": 3,
            "priority_evidence": "star marked beside question",
        }],
    }], requests)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-api")
    result = StructureWithLLM().classify_blocks(
        [{"block_id": "b1", "page_number": 1, "bbox": [0.1, 0.2, 0.7, 0.3], "text": "Q1 Explain."}],
        [], batch_id="visual-batch", page_images={1: b"page-image"},
    )
    content = requests[0][1]["json"]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert result["questions"][0]["priority_evidence"] == "star marked beside question"


def test_zero_questions_with_explicit_heading_gets_targeted_correction(monkeypatch):
    requests = []
    _mock_responses(monkeypatch, [
        {"questions": [], "other_block_ids": ["b1"]},
        {"questions": [{"question_number": "Q1", "block_ids": ["b1"]}]},
    ], requests)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-api")

    result = StructureWithLLM().classify_blocks(
        [{"block_id": "b1", "page_number": 1, "text": "Q.1 Explain the topic."}],
        [], batch_id="batch-1",
    )

    assert len(requests) == 2
    assert "questions=0" in requests[1][1]["json"]["messages"][1]["content"]
    assert result["questions"][0]["question_number"] == "Q1"


def test_omitted_block_reference_keeps_partial_validated_result(monkeypatch):
    from src.masterdeorca.document_intelligence.lite.structurer import BlockReferenceValidationError

    requests = []
    _mock_responses(monkeypatch, [{
        "questions": [{"question_number": "Q1", "block_ids": ["b1"]}],
    }], requests)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-api")
    monkeypatch.setattr("config.settings.NVIDIA_STRUCTURE_RETRIES", 1)

    with pytest.raises(BlockReferenceValidationError) as error:
        StructureWithLLM().classify_blocks(
            [
                {"block_id": "b1", "page_number": 1, "text": "Q.1 Explain."},
                {"block_id": "b2", "page_number": 1, "text": "Continuation."},
            ],
            [], batch_id="omitted-batch",
        )

    assert error.value.omitted_blocks == ["b2"]
    assert error.value.partial_result["questions"][0]["block_ids"] == ["b1"]
    assert len(requests) == 1


def test_fabricated_block_reference_has_no_recoverable_partial_result(monkeypatch):
    from src.masterdeorca.document_intelligence.lite.structurer import BlockReferenceValidationError

    requests = []
    _mock_responses(monkeypatch, [{
        "questions": [{"question_number": "Q1", "block_ids": ["not-in-request"]}],
    }], requests)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-api")
    monkeypatch.setattr("config.settings.NVIDIA_STRUCTURE_RETRIES", 1)

    with pytest.raises(BlockReferenceValidationError) as error:
        StructureWithLLM().classify_blocks(
            [{"block_id": "b1", "page_number": 1, "text": "Q.1 Explain."}],
            [], batch_id="fabricated-batch",
        )

    assert error.value.invalid_blocks == ["not-in-request"]
    assert error.value.partial_result is None


def test_length_null_content_is_left_for_adaptive_split(monkeypatch):
    requests = []
    truncated = httpx.Response(200, json={
        "choices": [{
            "finish_reason": "length",
            "message": {"content": None, "reasoning_content": "internal reasoning"},
        }],
        "usage": {"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8191}},
    })
    _mock_responses(monkeypatch, [truncated], requests)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-api")

    with pytest.raises(RuntimeError, match="finish_reason=length"):
        StructureWithLLM().classify_blocks(
            [
                {"block_id": "b1", "page_number": 1, "text": "Q1"},
                {"block_id": "b2", "page_number": 1, "text": "Question text"},
            ],
            [], batch_id="batch-1",
        )

    assert len(requests) == 1
    assert requests[0][1]["json"]["reasoning_effort"] == "low"
    assert requests[0][1]["json"]["max_tokens"] == 8192


def test_http_400_reports_sanitized_provider_body_without_retry(monkeypatch, caplog):
    requests = []
    response = httpx.Response(
        400,
        text='{"error":{"message":"unsupported reasoning_effort=low",'
        '"authorization":"Bearer nvapi-sensitive"}}',
    )
    _mock_responses(monkeypatch, [response], requests)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-sensitive")

    with pytest.raises(RuntimeError, match="unsupported reasoning_effort=low"):
        StructureWithLLM().classify_blocks(
            [{"block_id": "b1", "page_number": 1, "text": "Q1 Explain."}],
            [],
            batch_id="batch-400",
        )

    assert len(requests) == 1
    assert "nvapi-sensitive" not in caplog.text
    assert "[REDACTED]" in caplog.text


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
    assert request["json"]["model"] == "mistralai/mistral-large-4-1t-a52b"
    assert request["json"]["response_format"]["type"] == "json_schema"
    assert request["json"]["response_format"]["json_schema"]["name"] == "question_bank_record"
    assert request["json"]["max_tokens"] == 8192
    assert request["json"]["reasoning_effort"] == "low"
    prompt = request["json"]["messages"][1]["content"]
    assert "Page 1 question continues" in prompt
    assert "Page 2 question end" in prompt
    assert result["questions"][0]["source_pdf"] == "paper.pdf"
    assert result["metadata"]["providers_used"] == [
        "nvidia_nemotron_parse",
        "nvidia_mistral_large_4",
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
