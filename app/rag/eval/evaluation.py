"""
evaluation.py - measure how well the retriever finds the right chunk.

For every question in eval/questions.json we run the retriever and check whether one of the
expected sections appears in the top results.

Run:
    python app/evaluation.py            (scores, misses, similarity of the best chunk)
    python app/evaluation.py --sweep    (tries different keyword/semantic weights)

Scores:
    hit at 1 : the right chunk is the very first result
    hit at 3 : the right chunk is in the first 3 results
    hit at 5 : the right chunk is in the first 5 results
    MRR      : average of 1/position of the first right chunk (1.0 is perfect, 0 means never found)
"""
import json
from collections import defaultdict

from app.core.config import PROJECT_ROOT

QUESTIONS_FILE_PATH = PROJECT_ROOT / "eval" / "questions.json"
RESULTS_FILE_PATH = PROJECT_ROOT / "eval" / "results.json"
NUMBER_OF_RESULTS = 5


def load_questions(questions_file_path=QUESTIONS_FILE_PATH):
    with open(questions_file_path, encoding="utf8") as questions_file:
        return json.load(questions_file)


def find_first_hit_position(returned_chunk_ids, expected_sections):
    """
    Return the position (1 = first) of the first returned chunk that belongs to an expected section.
    Return None when no expected section was found.
    A chunk belongs to a section when its id starts with the expected text.
    """
    for position, chunk_id in enumerate(returned_chunk_ids, start=1):
        for expected_section in expected_sections:
            if chunk_id.startswith(expected_section):
                return position
    return None


def evaluate_question(question_record, search_function):
    """Run one question and describe what happened."""
    results = search_function(question_record["question"], NUMBER_OF_RESULTS)
    returned_chunk_ids = [result["chunk_id"] for result in results]
    first_hit_position = find_first_hit_position(returned_chunk_ids, question_record["expected_sections"])
    return {
        "id": question_record["id"],
        "question": question_record["question"],
        "kind": question_record["kind"],
        "expected_sections": question_record["expected_sections"],
        "returned_chunk_ids": returned_chunk_ids,
        "first_hit_position": first_hit_position,
        "top_score": results[0]["score"] if results else None,
        "top_similarity": results[0].get("similarity") if results else None,
    }


def summarize(question_results):
    """Turn the per-question results into percentages. Questions with no answer in the documents are skipped."""
    scored = [item for item in question_results if item["expected_sections"]]
    total = len(scored)
    if total == 0:
        return {"questions_scored": 0}

    def percent_with_hit_within(limit):
        hits = sum(1 for item in scored if item["first_hit_position"] and item["first_hit_position"] <= limit)
        return round(100 * hits / total, 1)

    reciprocal_ranks = [1 / item["first_hit_position"] if item["first_hit_position"] else 0 for item in scored]
    return {
        "questions_scored": total,
        "hit_at_1_percent": percent_with_hit_within(1),
        "hit_at_3_percent": percent_with_hit_within(3),
        "hit_at_5_percent": percent_with_hit_within(5),
        "mrr": round(sum(reciprocal_ranks) / total, 3),
    }


def summarize_by_kind(question_results):
    """Same summary, split by question type, so we can see which type of question is weak."""
    grouped = defaultdict(list)
    for item in question_results:
        if item["expected_sections"]:
            grouped[item["kind"]].append(item)
    return {kind: summarize(items) for kind, items in grouped.items()}


# (keyword_weight, semantic_weight) pairs tried by "python app/evaluation.py --sweep"
WEIGHT_COMBINATIONS = [(1.0, 1.0), (1.0, 2.0), (1.0, 3.0), (2.0, 1.0), (0.5, 1.0)]


def make_search_function(keyword_weight, semantic_weight, question_vectors_by_text, search_with_vector):
    """Build a search function with fixed weights that reuses the question vectors computed once."""
    def search_function(question, number_of_results):
        return search_with_vector(question, question_vectors_by_text[question], number_of_results,
                                  keyword_weight, semantic_weight)
    return search_function


def run_weight_sweep():
    """Try several keyword/semantic weights and print one line of scores for each."""
    from app.rag.ingest.embeddings import embed_one_text
    from app.rag.answer.retriever import search_chunks_with_vector

    questions = load_questions()
    print("Embedding each question once...")
    question_vectors_by_text = {item["question"]: embed_one_text(item["question"]) for item in questions}

    print("\nkeyword_weight  semantic_weight   hit@1   hit@3   hit@5    MRR")
    for keyword_weight, semantic_weight in WEIGHT_COMBINATIONS:
        search_function = make_search_function(keyword_weight, semantic_weight,
                                               question_vectors_by_text, search_chunks_with_vector)
        question_results = [evaluate_question(item, search_function) for item in questions]
        summary = summarize(question_results)
        print(f"{keyword_weight:>14}  {semantic_weight:>15}   {summary['hit_at_1_percent']:>5}   "
              f"{summary['hit_at_3_percent']:>5}   {summary['hit_at_5_percent']:>5}   {summary['mrr']:>5}")


def run_evaluation(search_function):
    question_results = []
    for question_record in load_questions():
        question_results.append(evaluate_question(question_record, search_function))
        print(f"  checked question {question_record['id']}")
    return question_results


if __name__ == "__main__":
    import sys

    if "--sweep" in sys.argv:
        run_weight_sweep()
        sys.exit()

    from app.rag.answer.retriever import search_chunks

    question_results = run_evaluation(search_chunks)

    print("\nOVERALL")
    print(json.dumps(summarize(question_results), indent=2))
    print("\nBY QUESTION TYPE")
    for kind, summary in summarize_by_kind(question_results).items():
        print(f"  {kind:<10} {summary}")

    print("\nQUESTIONS WHERE THE RIGHT CHUNK WAS NOT IN THE TOP 5")
    for item in question_results:
        if item["expected_sections"] and item["first_hit_position"] is None:
            print(f"  [{item['id']}] {item['question']}")
            print(f"      expected: {item['expected_sections']}")
            print(f"      got     : {item['returned_chunk_ids']}")

    # Closeness in meaning of the best chunk. Compare the two groups to find a cut-off for "I don't know".
    answerable_similarities = sorted(item["top_similarity"] for item in question_results
                                     if item["expected_sections"] and item["top_similarity"] is not None)
    print("\nTOP SIMILARITY, QUESTIONS WITH AN ANSWER (lowest five):", [round(v, 3) for v in answerable_similarities[:5]])
    print("TOP SIMILARITY, QUESTIONS NOT IN THE DOCUMENTS:")
    for item in question_results:
        if not item["expected_sections"]:
            print(f"  [{item['id']}] {item['question']}  similarity {item['top_similarity']}")

    with open(RESULTS_FILE_PATH, "w", encoding="utf8") as results_file:
        json.dump(question_results, results_file, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to {RESULTS_FILE_PATH}")
