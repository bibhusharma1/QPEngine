"""End-to-end verification of the Question Bank API's two query modes.

Mode A (no marks parameter) returns every question in the selected subject;
Mode B returns only the questions whose normalized marks equal the requested
value. Pagination and totals are computed after the optional filter, and
`available_marks` exposes the subject's actual distinct values, sorted
ascending, regardless of insertion or storage order.
"""

from __future__ import annotations

from tests.test_api import _client


def _test_client(tmp_path, monkeypatch):
    from src.masterdeorca import storage
    from src.masterdeorca.services.question_bank import _atomic_write

    monkeypatch.setattr(storage, "STORAGE_ROOT", tmp_path / "subjects")
    paths = storage.ensure_subject("OperatingSys")
    questions = []
    for index, marks in enumerate([5, None, 3, 2, 8, 2, 16, 3], start=1):
        questions.append({"question_id": f"q{index}", "original_text": f"Question {index}",
                          "marks": marks, "source_pdf": "paper.pdf"})
    _atomic_write(paths["base"] / "question_bank.json", {
        "subject": "OperatingSys", "updated_at": "now", "questions": questions})
    return paths


def test_all_questions_mode_returns_every_question_every_marks_value(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _test_client(tmp_path, monkeypatch)

        gathered, page = [], 1
        while True:
            response = client.get("/api/question-bank",
                                  params={"subject_id": "OperatingSys", "page": page, "page_size": 3})
            body = response.json()
            assert body["filters"] == {"subject_id": "OperatingSys", "marks": None}
            gathered.extend(body["items"])
            if page >= body["pagination"]["total_pages"]:
                break
            page += 1

        assert body["pagination"]["total_items"] == 8
        assert {item["question_id"] for item in gathered} == {f"q{index}" for index in range(1, 9)}
        # Every marks value actually present in the bank appears in Mode A results,
        # including null marks and the duplicated 2/3/5 records.
        assert sorted(str(item["marks"]) for item in gathered) == sorted(
            ["5", "None", "3", "2", "8", "2", "16", "3"])
        assert client.get("/api/question-bank",
                          params={"subject_id": "OperatingSys", "page": 9}).json()["items"] == []


def test_specific_marks_mode_filters_before_pagination(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _test_client(tmp_path, monkeypatch)
        response = client.get("/api/question-bank",
                              params={"subject_id": "OperatingSys", "marks": 3,
                                      "page": 1, "page_size": 1})
        body = response.json()
        assert [item["marks"] for item in body["items"]] == [3]
        assert body["pagination"] == {"page": 1, "page_size": 1, "total_items": 2, "total_pages": 2}
        second = client.get("/api/question-bank",
                            params={"subject_id": "OperatingSys", "marks": 3,
                                    "page": 2, "page_size": 1}).json()
        assert [item["marks"] for item in second["items"]] == [3]
        assert second["filters"] == {"subject_id": "OperatingSys", "marks": 3}


def test_available_marks_are_actual_distinct_values_sorted_ascending(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _test_client(tmp_path, monkeypatch)
        body = client.get("/api/question-bank",
                          params={"subject_id": "OperatingSys", "page_size": 1}).json()
        assert body["available_marks"] == [2, 3, 5, 8, 16]
        assert len(body["available_marks"]) == len(set(body["available_marks"]))
        # The options are unaffected by the requested filter; a value with zero
        # matching questions can be requested and simply returns an empty page.
        filtered = client.get("/api/question-bank",
                              params={"subject_id": "OperatingSys", "marks": 16}).json()
        assert filtered["available_marks"] == [2, 3, 5, 8, 16]


def test_page_size_changes_and_totals_in_both_modes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _test_client(tmp_path, monkeypatch)
        small = client.get("/api/question-bank",
                           params={"subject_id": "OperatingSys", "page": 3, "page_size": 1}).json()
        assert small["pagination"] == {"page": 3, "page_size": 1, "total_items": 8, "total_pages": 8}
        assert len(small["items"]) == 1
        large = client.get("/api/question-bank",
                           params={"subject_id": "OperatingSys", "page_size": 200}).json()
        assert large["pagination"]["total_pages"] == 1 and len(large["items"]) == 8
        marks_small = client.get("/api/question-bank",
                                 params={"subject_id": "OperatingSys", "marks": 2,
                                         "page_size": 1, "page": 2}).json()
        assert marks_small["pagination"] == {"page": 2, "page_size": 1, "total_items": 2, "total_pages": 2}


def test_invalid_subject_and_marks_values_return_existing_errors(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _test_client(tmp_path, monkeypatch)
        assert client.get("/api/question-bank").status_code == 422  # subject_id missing
        assert client.get("/api/question-bank", params={"subject_id": "NoSuchSubject"}).status_code == 404
        assert client.get("/api/question-bank", params={"subject_id": "bad name!"}).status_code == 422
        # An invalid marks value is a validation error, never "All Questions".
        invalid_marks = [("marks", "not-a-number"), ("marks", "0"), ("marks", "-2")]
        for key, value in invalid_marks:
            assert client.get("/api/question-bank",
                              params={"subject_id": "OperatingSys", key: value}).status_code == 422
            assert client.get("/api/question-bank",
                              params={"subject_id": "NoSuchSubject", key: value}).status_code in (404, 422)
