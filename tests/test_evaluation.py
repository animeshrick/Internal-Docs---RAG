"""Tests for app/evaluation.py and for the question file eval/questions.json"""
import json

import evaluation


# ---------------------------------------------------------------- finding the first hit
def test_first_hit_position_counts_from_one():
    returned = ["HR-A#x#00", "HR-LEAVE-001#3-leave-types-and-limits#01", "HR-B#y#00"]
    assert evaluation.find_first_hit_position(returned, ["HR-LEAVE-001#3-leave-types-and-limits"]) == 2


def test_whole_document_can_be_expected():
    assert evaluation.find_first_hit_position(["ORG-CORP-001#team#00"], ["ORG-CORP-001"]) == 1


def test_no_hit_returns_none():
    assert evaluation.find_first_hit_position(["HR-A#x#00"], ["HR-B#y"]) is None


def test_any_of_several_expected_sections_counts():
    assert evaluation.find_first_hit_position(["HR-B#y#00"], ["HR-A#x", "HR-B#y"]) == 1


# ---------------------------------------------------------------- scoring
def make_result(question_id, first_hit_position, kind="policy", expected=("HR-A#x",)):
    return {"id": question_id, "question": "q", "kind": kind, "expected_sections": list(expected),
            "returned_chunk_ids": [], "first_hit_position": first_hit_position, "top_score": 0.03}


def test_summary_percentages_and_mrr():
    results = [make_result(1, 1), make_result(2, 3), make_result(3, None), make_result(4, 5)]
    summary = evaluation.summarize(results)
    assert summary["questions_scored"] == 4
    assert summary["hit_at_1_percent"] == 25.0
    assert summary["hit_at_3_percent"] == 50.0
    assert summary["hit_at_5_percent"] == 75.0
    assert summary["mrr"] == round((1 + 1 / 3 + 0 + 1 / 5) / 4, 3)


def test_questions_not_in_documents_are_not_scored():
    results = [make_result(1, 1), make_result(2, None, kind="not_in_documents", expected=())]
    assert evaluation.summarize(results)["questions_scored"] == 1


def test_summary_by_kind_splits_the_questions():
    results = [make_result(1, 1, kind="number"), make_result(2, None, kind="paraphrase")]
    by_kind = evaluation.summarize_by_kind(results)
    assert by_kind["number"]["hit_at_1_percent"] == 100.0
    assert by_kind["paraphrase"]["hit_at_5_percent"] == 0.0


def test_evaluate_question_uses_the_search_function():
    question_record = {"id": 7, "question": "How many casual leaves?", "kind": "number",
                       "expected_sections": ["HR-LEAVE-001#policy-at-a-glance"]}

    def fake_search(question, number_of_results):
        assert number_of_results == 5
        return [{"chunk_id": "HR-GOV-000#x#00", "score": 0.02},
                {"chunk_id": "HR-LEAVE-001#policy-at-a-glance#00", "score": 0.01}]

    result = evaluation.evaluate_question(question_record, fake_search)
    assert result["first_hit_position"] == 2
    assert result["top_score"] == 0.02


# ---------------------------------------------------------------- the real question file
def test_question_file_is_well_formed():
    questions = evaluation.load_questions()
    assert 30 <= len(questions) <= 60
    assert len({question["id"] for question in questions}) == len(questions)
    for question in questions:
        assert question["question"].strip().endswith("?")
        assert set(question) == {"id", "question", "kind", "expected_sections", "answer_must_mention"}


def test_every_expected_section_exists_in_the_chunk_file(parsed_documents):
    chunk_ids = [json.loads(line)["chunk_id"]
                 for line in open(evaluation.PROJECT_ROOT / "data" / "parsed" / "chunks.jsonl", encoding="utf8")]
    for question in evaluation.load_questions():
        for expected_section in question["expected_sections"]:
            assert any(chunk_id.startswith(expected_section) for chunk_id in chunk_ids), \
                f"question {question['id']}: no chunk starts with {expected_section}"


def test_some_questions_are_deliberately_not_answerable():
    kinds = [question["kind"] for question in evaluation.load_questions()]
    assert kinds.count("not_in_documents") >= 2


# ---------------------------------------------------------------- weight sweep helpers
def test_make_search_function_uses_fixed_weights_and_saved_vector():
    calls = []

    def fake_search_with_vector(question, vector, number_of_results, keyword_weight, semantic_weight):
        calls.append((question, vector, number_of_results, keyword_weight, semantic_weight))
        return []

    search_function = evaluation.make_search_function(2.0, 1.0, {"How many leaves?": [0.5]}, fake_search_with_vector)
    search_function("How many leaves?", 5)
    assert calls == [("How many leaves?", [0.5], 5, 2.0, 1.0)]


def test_evaluate_question_records_top_similarity():
    question_record = {"id": 1, "question": "q", "kind": "number", "expected_sections": ["A#x"]}
    result = evaluation.evaluate_question(
        question_record, lambda question, number_of_results: [{"chunk_id": "A#x#00", "score": 0.03, "similarity": 0.77}])
    assert result["top_similarity"] == 0.77
