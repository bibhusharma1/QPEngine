import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.masterdeorca.app import QuestionGenerationRequest
from src.masterdeorca.services import smart_qpg


@pytest.fixture
def syllabus():
    return {
        "status": "completed",
        "subject_id": "subject-1",
        "summary_data": {
            "modules": [
                {
                    "module_name": f"Module {roman}",
                    "sections": [{"heading": f"Section {roman}A", "topics": [f"Topic {roman}1", f"Topic {roman}2"],
                                  "topic_details": [
                                      {"topic": f"Topic {roman}1", "description": f"Description {roman}1"},
                                      {"topic": f"Topic {roman}2", "description": f"Description {roman}2"},
                                  ]}],
                }
                for roman in ("I", "II", "III", "IV")
            ],
            "question_generation_guidance": {
                "sections": [
                    {"module_name": f"Module {roman}", "heading": f"Section {roman}A",
                     "topic_guidance": [{"topic": f"Topic {roman}1", "question_formats": [f"format-{roman}"]}],
                     "coverage_suggestions": [f"coverage-{roman}"]}
                    for roman in ("I", "II", "III", "IV")
                ]
            },
        },
    }


def scope(syllabus, instructions, selected_topics=()):
    return smart_qpg._resolve_generation_scope(syllabus, {
        "instructions": instructions,
        "selected_topics": list(selected_topics),
    })


@pytest.mark.parametrize(("modules", "expected"), [
    (["Module I"], {"Module I"}),
    (["Module III"], {"Module III"}),
    (["Module I", "Module III"], {"Module I", "Module III"}),
    (["Module I", "Module II", "Module III", "Module IV"],
     {"Module I", "Module II", "Module III", "Module IV"}),
])
def test_module_selection_never_expands_to_other_modules(syllabus, modules, expected):
    result = scope(syllabus, {"selected_modules": modules})
    assert {item["module_name"] for item in result} == expected
    assert all(item["description"] == f"Description {item['topic'].removeprefix('Topic ')}" for item in result)
    assert len(result) == 2 * len(expected)


@pytest.mark.parametrize("modules", [
    [],
    ["Module I"], ["Module III"], ["Module I", "Module III"],
    ["Module I", "Module II", "Module III", "Module IV"],
])
def test_create_job_uses_entire_syllabus_even_with_legacy_module_selection(monkeypatch, syllabus, modules):
    statements = []

    class FakeDatabaseConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=()):
            statements.append((statement, params))

    monkeypatch.setattr(smart_qpg, "get_syllabus", lambda _syllabus_id: syllabus)
    monkeypatch.setattr(smart_qpg.database, "get_subject_by_id", lambda _subject_id: {"subject_name": "Test"})
    monkeypatch.setattr(smart_qpg.database, "connect", lambda: FakeDatabaseConnection())
    request = {
        "selected_modules": modules,
        "instructions": "Answer all questions",
        "total_marks": 40,
        "sections": [{"name": "A", "question_count": 8, "marks_per_question": 5,
                      "question_type": "Short", "difficulty": "Easy"}],
    }
    job_id = smart_qpg.create_job(user="teacher", subject_id="subject-1", syllabus_id="syllabus-1", request=request)
    assert job_id
    item_inserts = [params for statement, params in statements if "INSERT INTO question_generation_items" in statement]
    assert len(item_inserts) == 8
    assert {params[7] for params in item_inserts} == {f"Module {roman}" for roman in ("I", "II", "III", "IV")}
    job_insert = next(params for statement, params in statements if "INSERT INTO question_generation_jobs" in statement)
    hierarchy = json.loads(job_insert[5])["resolved_hierarchy"]
    assert {module["module_name"] for module in hierarchy} == {f"Module {roman}" for roman in ("I", "II", "III", "IV")}
    assert sum(len(section["topics"]) for module in hierarchy for section in module["sections"]) == 8


def test_section_and_individual_topic_selection(syllabus):
    selected = {"selected_hierarchy": {"modules": [
        {"module_name": "Module III", "sections": [{"heading": "Section IIIA", "topics": ["Topic III1"]}]}
    ]}}
    result = scope(syllabus, selected, ["Topic III1"])
    assert result == [{"module_name": "Module III", "heading": "Section IIIA", "topic": "Topic III1",
                       "description": "Description III1"}]


def test_normalization_keeps_structured_topic_descriptions():
    normalized = smart_qpg._normalize_structure_payload({"modules": [{
        "module_name": "Module I", "sections": [{"heading": "Section IA", "topics": [
            {"topic": "Topic I1", "description": "Stored topic description"}
        ]}]
    }]}, "Test")
    summary = smart_qpg.SyllabusSummary.model_validate(normalized).model_dump()
    topic = summary["modules"][0]["sections"][0]
    assert topic["topics"] == ["Topic I1"]
    assert topic["topic_details"] == [{"topic": "Topic I1", "description": "Stored topic description"}]


def test_topic_names_can_select_unique_topics_in_different_modules(syllabus):
    result = scope(syllabus, {}, ["Topic I1", "Topic III2"])
    assert {(row["module_name"], row["topic"]) for row in result} == {
        ("Module I", "Topic I1"), ("Module III", "Topic III2")
    }


def test_no_module_selection_resolves_entire_syllabus(syllabus):
    result = scope(syllabus, {})
    assert {item["module_name"] for item in result} == {f"Module {roman}" for roman in ("I", "II", "III", "IV")}
    assert len(result) == 8


def test_no_selection_and_invalid_hierarchy_are_rejected(syllabus):
    assert len(scope(syllabus, "Answer all")) == 8
    invalid = {"selected_hierarchy": {"modules": [
        {"module_name": "Module I", "sections": [{"heading": "Section IIIA", "topics": ["Topic III1"]}]}
    ]}}
    with pytest.raises(ValueError, match="section does not belong"):
        scope(syllabus, invalid, ["Topic III1"])


def test_request_contract_keeps_hierarchy_inside_instructions():
    payload = {
        "subject_id": "subject-1", "syllabus_id": "syllabus-1",
        "instructions": {"general_instructions": ["Answer all"], "selected_hierarchy": {"modules": ["Module I"]}},
        "selected_topics": ["Topic I1"],
        "sections": [{"name": "A", "question_count": 2, "marks_per_question": 5,
                      "question_type": "Short", "difficulty": "Easy"}],
    }
    parsed = QuestionGenerationRequest.model_validate(payload)
    assert parsed.instructions["selected_hierarchy"]["modules"] == ["Module I"]
    direct = QuestionGenerationRequest.model_validate({**payload, "selected_modules": ["Module I"]})
    assert direct.selected_modules == ["Module I"]


def test_total_marks_mismatch_is_a_client_validation_error(monkeypatch, syllabus):
    monkeypatch.setattr(smart_qpg, "get_syllabus", lambda _: syllabus)
    monkeypatch.setattr(smart_qpg.database, "get_subject_by_id", lambda _: {"subject_id": "subject-1"})
    request = {
        "instructions": {"selected_hierarchy": {"modules": ["Module I"]}},
        "selected_topics": [], "total_marks": 12,
        "sections": [{"name": "A", "question_count": 2, "marks_per_question": 5,
                      "question_type": "Short", "difficulty": "Easy"}],
    }
    with pytest.raises(ValueError, match="Total marks must match"):
        smart_qpg.create_job(user="teacher", subject_id="subject-1", syllabus_id="syllabus-1", request=request)


def test_worker_prompt_contains_only_selected_scope_and_guidance(monkeypatch, syllabus):
    selected = {"module_name": "Module I", "heading": "Section IA", "topic": "Topic I1",
                "description": "Description I1"}
    captured = {}

    async def fake_chat(_client, _system, task, **_kwargs):
        captured.update(task)
        return {"questions": [{"question_text": "Explain Topic I1 with one example."}]}

    monkeypatch.setattr(smart_qpg, "_chat", fake_chat)
    job = {"subject_name": "Test", "request_json": json.dumps({
        "resolved_scope": [selected], "instructions": ["Answer all"]
    })}
    item = {"source_module": "Module I", "source_heading": "Section IA", "topic": "Topic I1",
            "section": "A", "marks": 5, "type": "Short", "difficulty": "Easy", "instructions": "Be concise"}
    asyncio.run(smart_qpg._generate_one(None, job, syllabus, item, [], [], asyncio.Lock()))
    assert captured["selected_syllabus_scope"] == [selected]
    assert captured["syllabus_topic_guidance"]["topic_guidance"]["question_formats"] == ["format-I"]
    assert captured["examination"]["instructions"] == ["Answer all"]
    assert "Module III" not in json.dumps(captured)


def test_chat_returns_transient_api_error_to_per_question_retry(monkeypatch):
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_API_ATTEMPTS", 3)
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_BACKOFF_BASE", 0)
    calls = []

    class ServiceUnavailable(Exception):
        status_code = 503

    async def create(**_kwargs):
        calls.append(1)
        raise ServiceUnavailable("temporary")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    diagnostics = {"http_attempts": 0}
    with pytest.raises(smart_qpg._GenerationRequestError) as error:
        asyncio.run(smart_qpg._chat(client, "system", {}, diagnostics=diagnostics))
    assert error.value.category == "server_error" and error.value.retryable
    assert len(calls) == diagnostics["http_attempts"] == 1


def test_transient_generation_errors_retry_with_shared_question_budget(monkeypatch, syllabus):
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_MAX_RETRIES", 2)
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_BACKOFF_BASE", 0)
    calls, transitions = [], []

    async def fake_chat(_client, _system, _task, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise smart_qpg._GenerationRequestError("rate_limit", "429 from NVIDIA", retryable=True)
        return {"questions": [{"question_text": "Explain how an intelligent agent selects an action."}]}

    async def on_retry(number, category, _exc, delay):
        transitions.append((number, category, delay))

    monkeypatch.setattr(smart_qpg, "_chat", fake_chat)
    job = {"job_id": "job-1", "subject_name": "Test", "request_json": json.dumps({
        "resolved_scope": [], "instructions": [], "sections": []})}
    item = {"item_id": "item-1", "source_module": "Module I", "source_heading": "Section IA",
            "topic": "Topic I1", "section": "A", "marks": 5, "type": "Short", "difficulty": "Easy"}
    diagnostics = {"started_at": __import__("time").perf_counter(), "http_attempts": 0}
    question = asyncio.run(smart_qpg._generate_one(None, job, syllabus, item, [], [], asyncio.Lock(), diagnostics,
                                                   on_retry=on_retry))
    assert question["question_text"].startswith("Explain how")
    assert calls == [1, 1, 1]
    assert transitions == [(1, "rate_limit", 0), (2, "rate_limit", 0)]
    assert diagnostics["generation_attempts"] == 3 and diagnostics["retries_used"] == 2


def test_permanent_request_errors_are_not_retried(monkeypatch, syllabus):
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_MAX_RETRIES", 2)
    calls = []

    async def fake_chat(*_args, **_kwargs):
        calls.append(1)
        raise smart_qpg._GenerationRequestError("permanent_request_error", "401 unauthorized", retryable=False,
                                                status_code=401)

    monkeypatch.setattr(smart_qpg, "_chat", fake_chat)
    job = {"job_id": "job-1", "subject_name": "Test", "request_json": json.dumps({"sections": []})}
    item = {"item_id": "item-1", "source_module": "Module I", "source_heading": "Section IA",
            "topic": "Topic I1", "section": "A", "marks": 5, "type": "Short", "difficulty": "Easy"}
    with pytest.raises(smart_qpg._GenerationRequestError):
        asyncio.run(smart_qpg._generate_one(None, job, syllabus, item, [], [], asyncio.Lock()))
    assert len(calls) == 1


def test_request_failure_categories_and_retry_after_are_bounded():
    rate_limit = SimpleNamespace(status_code=429,
        response=SimpleNamespace(headers={"retry-after": "30"}))
    failure = smart_qpg._request_failure(rate_limit)
    assert failure.category == "rate_limit" and failure.retryable and failure.retry_after == 8
    permanent = smart_qpg._request_failure(SimpleNamespace(status_code=401, response=None))
    assert permanent.category == "permanent_request_error" and not permanent.retryable


def test_chat_rejects_truncated_json_response():
    async def create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length",
            message=SimpleNamespace(content='{"questions":['))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(smart_qpg._InvalidModelOutput, match="truncated"):
        asyncio.run(smart_qpg._chat(client, "system", {}, diagnostics={"http_attempts": 0}))


def test_nvidia_client_disables_sdk_retry_layer(monkeypatch):
    import openai

    captured = {}
    sentinel = object()

    def fake_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(openai, "AsyncOpenAI", fake_client)
    monkeypatch.setattr(smart_qpg.settings, "setting", lambda _name, _default="": "test-key")
    client = smart_qpg._nvidia_client()
    assert client is sentinel
    assert captured["max_retries"] == 0
    assert captured["timeout"] == smart_qpg.settings.QUESTION_GENERATION_TIMEOUT


def test_chat_does_not_retry_invalid_model_json_as_transport_error(monkeypatch):
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_API_ATTEMPTS", 3)
    calls = []

    async def create(**_kwargs):
        calls.append(1)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content="not JSON"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(smart_qpg._InvalidModelOutput):
        asyncio.run(smart_qpg._chat(client, "system", {}))
    assert len(calls) == 1


def test_invalid_question_output_has_at_most_two_retries(monkeypatch, syllabus):
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_MAX_RETRIES", 2)
    calls = []

    async def fake_chat(_client, _system, _task, **_kwargs):
        calls.append(1)
        return {"questions": []}

    monkeypatch.setattr(smart_qpg, "_chat", fake_chat)
    job = {"job_id": "job-1", "subject_name": "Test", "request_json": json.dumps({
        "resolved_scope": [], "instructions": []})}
    item = {"item_id": "item-1", "source_module": "Module I", "source_heading": "Section IA",
            "topic": "Topic I1", "section": "A", "marks": 5, "type": "Short", "difficulty": "Easy"}
    diagnostics = {"started_at": __import__("time").perf_counter(), "http_attempts": 0}
    with pytest.raises(smart_qpg._InvalidModelOutput, match="wrong question count"):
        asyncio.run(smart_qpg._generate_one(None, job, syllabus, item, [], [], asyncio.Lock(), diagnostics))
    assert len(calls) == 3
    assert diagnostics["generation_attempts"] == 3


def test_batch_does_not_retry_and_preserves_individual_validation_failure(monkeypatch, syllabus):
    calls = []

    async def fake_chat(_client, _system, task, **_kwargs):
        assignments = task["assignments"]
        calls.append([item["topic"] for item in assignments])
        return {"questions": [{"question_text": "Explain the foundations of artificial intelligence."},
                               {"question_text": "x"}]}

    monkeypatch.setattr(smart_qpg, "_chat", fake_chat)
    job = {"job_id": "job-1", "subject_name": "Test", "request_json": json.dumps({
        "resolved_scope": [], "instructions": [], "sections": []})}
    items = [
        {"item_id": "item-1", "source_module": "Module I", "source_heading": "Section IA",
         "topic": "Topic I1", "section": "A", "marks": 5, "type": "Short", "difficulty": "Easy"},
        {"item_id": "item-2", "source_module": "Module I", "source_heading": "Section IA",
         "topic": "Topic I2", "section": "B", "marks": 10, "type": "Essay", "difficulty": "Medium"},
    ]
    outcomes = asyncio.run(smart_qpg._generate_batch(None, job, syllabus, items, [], [], asyncio.Lock()))
    assert calls == [["Topic I1", "Topic I2"]]
    assert isinstance(outcomes[0], dict)
    assert isinstance(outcomes[1], smart_qpg.ValidationError)
    assert outcomes[0]["section"] == "A"


def test_submitted_syllabus_summary_must_match_stored_summary(syllabus):
    smart_qpg.validate_submitted_summary(syllabus["summary_data"], syllabus)
    modified = json.loads(json.dumps(syllabus["summary_data"]))
    modified["modules"][0]["sections"][0]["topics"][0] = "Untrusted topic"
    with pytest.raises(ValueError, match="does not match"):
        smart_qpg.validate_submitted_summary(modified, syllabus)


def test_generation_request_identity_ignores_runtime_and_selection_fields():
    first = {"paper_title": "Midterm", "sections": [{"name": "A"}],
             "selected_modules": ["Module I"], "syllabus_summary": {"modules": []},
             "generation_context_id": "one"}
    second = {"paper_title": "Midterm", "sections": [{"name": "A"}],
              "selected_modules": ["Module IV"], "syllabus_summary": {"modules": ["changed"]},
              "generation_context_id": "two", "resolved_scope": [{"topic": "runtime"}]}
    changed_exam = {**second, "paper_title": "Final"}
    assert smart_qpg._generation_request_identity(first) == smart_qpg._generation_request_identity(second)
    assert smart_qpg._generation_request_identity(first) != smart_qpg._generation_request_identity(changed_exam)


@pytest.mark.parametrize(("fail_topics", "expected_status", "expected_drafts", "expected_failures"), [
    (set(), "completed", 2, 0),
    ({"Topic I2"}, "partial", 1, 1),
    ({"Topic I1", "Topic I2"}, "failed", 0, 2),
])
def test_run_job_persists_questions_and_terminal_states(monkeypatch, fail_topics, expected_status,
                                                       expected_drafts, expected_failures):
    initially_failed_topics = set(fail_topics)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE subjects(subject_id TEXT, subject_name TEXT);
        CREATE TABLE question_generation_jobs(job_id TEXT, created_by TEXT, subject_id TEXT, syllabus_id TEXT,
            status TEXT, request_json TEXT, progress_json TEXT, error TEXT, docx_blob BLOB,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE question_generation_items(item_id TEXT, job_id TEXT, section TEXT, topic TEXT, marks INTEGER,
            type TEXT, difficulty TEXT, source_module TEXT, source_heading TEXT, position INTEGER, status TEXT,
            question_json TEXT, error TEXT, retries INTEGER, created_at TEXT, updated_at TEXT);
    """)
    request = {"sections": [{"name": "A", "instructions": ""}], "resolved_scope": [],
               "resolved_guidance": {}, "instructions": []}
    db.execute("INSERT INTO subjects VALUES(?,?)", ("s", "Test"))
    db.execute("INSERT INTO question_generation_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               ("j", "teacher", "s", "y", "queued", json.dumps(request), "{}", None, None, "t", "t"))
    for index, topic in enumerate(("Topic I1", "Topic I2")):
        db.execute("INSERT INTO question_generation_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f"i{index}", "j", "A", topic, 5, "Short", "Easy", "Module I", "Section IA",
                    index, "queued", None, None, 0, "t", "t"))
    db.commit()
    monkeypatch.setattr(smart_qpg.database, "connect", lambda: db)
    monkeypatch.setattr(smart_qpg, "_existing_examples", lambda *_args: ([], []))
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_OUTPUT_RETRIES", 1)
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_API_ATTEMPTS", 1)
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_MAX_RETRIES", 1)
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_CONCURRENCY", 1)
    monkeypatch.setattr(smart_qpg.settings, "QUESTION_GENERATION_BATCH_SIZE", 2)
    model_batches = []

    class FakeClient:
        async def close(self):
            return None

    async def create(**kwargs):
        task = json.loads(kwargs["messages"][1]["content"])
        assignments = task["assignments"]
        model_batches.append([assignment["topic"] for assignment in assignments])
        questions = []
        for assignment in assignments:
            topic = assignment["topic"]
            if topic in fail_topics:
                questions.append({"question_text": "x"})
                continue
            wording = ("Why do search agents evaluate candidate paths in Topic I1?" if topic == "Topic I1"
                       else "Discuss how environments shape an agent's observations in Topic I2?")
            questions.append({"question_text": wording})
        content = json.dumps({"questions": questions})
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])

    FakeClient.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    monkeypatch.setattr(smart_qpg, "_nvidia_client", FakeClient)
    asyncio.run(smart_qpg.run_job("j"))
    db.commit()
    job = db.execute("SELECT status,progress_json FROM question_generation_jobs WHERE job_id='j'").fetchone()
    statuses = [row["status"] for row in db.execute("SELECT status FROM question_generation_items WHERE job_id='j'")]
    questions = [row["question_json"] for row in db.execute("SELECT question_json FROM question_generation_items WHERE job_id='j'")]
    assert job["status"] == expected_status
    assert statuses.count("draft") == expected_drafts
    assert statuses.count("failed") == expected_failures
    assert sum(value is not None for value in questions) == expected_drafts
    progress = json.loads(job["progress_json"])
    assert progress["completed"] == expected_drafts
    assert progress["failed"] == expected_failures
    assert all(len(batch) == 1 for batch in model_batches)
    generated_topics = [batch[0] for batch in model_batches]
    assert generated_topics.count("Topic I1") == (2 if "Topic I1" in fail_topics else 1)
    assert generated_topics.count("Topic I2") == (2 if "Topic I2" in fail_topics else 1)
    if initially_failed_topics:
        attempts_before_retry = len(model_batches)
        fail_topics.clear()
        assert smart_qpg.retry_job("j")
        assert not smart_qpg.retry_job("j")
        asyncio.run(smart_qpg.run_job("j"))
        retried_topics = [batch[0] for batch in model_batches[attempts_before_retry:]]
        assert retried_topics == [topic for topic in ("Topic I1", "Topic I2")
                                  if topic in initially_failed_topics]
        assert db.execute("SELECT COUNT(*) FROM question_generation_items WHERE job_id='j' AND status='draft'").fetchone()[0] == 2


def test_item_status_updates_keep_persisted_progress_and_poll_counts_in_sync(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE question_generation_jobs(job_id TEXT, created_by TEXT, subject_id TEXT, syllabus_id TEXT,
            status TEXT, request_json TEXT, progress_json TEXT, error TEXT, docx_blob BLOB,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE question_generation_items(item_id TEXT, job_id TEXT, section TEXT, topic TEXT, marks INTEGER,
            type TEXT, difficulty TEXT, source_module TEXT, source_heading TEXT, position INTEGER, status TEXT,
            question_json TEXT, error TEXT, retries INTEGER, created_at TEXT, updated_at TEXT);
    """)
    db.execute("INSERT INTO question_generation_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               ("j", "teacher", "s", "y", "running", "{}", "{}", None, None, "t", "t"))
    for item_id in ("i1", "i2"):
        db.execute("INSERT INTO question_generation_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (item_id, "j", "A", "T", 5, "Short", "Easy", "M", "S", 0,
                    "queued", None, None, 0, "t", "t"))
    db.commit()
    monkeypatch.setattr(smart_qpg.database, "connect", lambda: db)

    running = smart_qpg._set_item_state("j", "i1", "running")
    assert running["queued"] == 1 and running["running"] == 1
    completed = smart_qpg._set_item_state("j", "i1", "draft", question={"question_text": "Valid question text"})
    assert completed["completed"] == 1 and completed["queued"] == 1
    smart_qpg._set_item_state("j", "i2", "running")
    failed = smart_qpg._set_item_state("j", "i2", "failed", error="validation failed", retries=1)
    assert failed["completed"] == 1 and failed["failed"] == 1
    db.commit()
    polled = smart_qpg.get_job("j")
    assert polled["total_questions"] == 2
    assert polled["completed_questions"] == 1
    assert polled["failed_questions"] == 1
    assert polled["queued_questions"] == polled["running_questions"] == 0


def test_only_one_worker_can_claim_a_queued_job(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE question_generation_jobs(job_id TEXT, status TEXT, updated_at TEXT)")
    db.execute("INSERT INTO question_generation_jobs VALUES('j','queued','t')")
    db.commit()
    monkeypatch.setattr(smart_qpg.database, "connect", lambda: db)
    assert smart_qpg._claim_job("j")
    assert not smart_qpg._claim_job("j")
