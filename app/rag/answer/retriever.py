"""
retriever.py - find the chunks that best answer a question.

How it works:
    1. The question is turned into a vector with the same model that embedded the documents (gte-small).
    2. The database function hybrid_search_hr_chunks runs a keyword search and a vector search
       and merges them (see sql/02_hybrid_search_function.sql).
    3. Only Approved documents (and the company profile) can be returned.

Try it from the terminal:
    python app/retriever.py "How many casual leaves do I get?"
"""
import sys

from app.rag.ingest.embeddings import embed_one_text
from app.core.supabase import call_database_function

DEFAULT_NUMBER_OF_RESULTS = 5
# Chosen with the evaluation sweep (python app/evaluation.py --sweep):
# meaning counts twice as much as keywords. This raised hit@1 from 59.6% to 70.2% with no loss at hit@5.
DEFAULT_KEYWORD_WEIGHT = 1.0
DEFAULT_SEMANTIC_WEIGHT = 2.0
DATABASE_FUNCTION_NAME = "hybrid_search_hr_chunks"


def search_chunks_with_vector(question, question_vector, number_of_results=DEFAULT_NUMBER_OF_RESULTS,
                              keyword_weight=DEFAULT_KEYWORD_WEIGHT, semantic_weight=DEFAULT_SEMANTIC_WEIGHT):
    """
    Same as search_chunks, but the question was already turned into a vector.
    Used by the evaluation, which tries several weights without embedding each question again.

    keyword_weight / semantic_weight: how much each search counts when the two lists are merged.
    """
    found_rows = call_database_function(DATABASE_FUNCTION_NAME, {
        "query_text": question,
        "query_embedding": question_vector,
        "match_count": number_of_results,
        "keyword_weight": keyword_weight,
        "semantic_weight": semantic_weight,
    })

    # Give the rows simple, readable field names for the rest of the application.
    results = []
    for row in found_rows:
        results.append({
            "chunk_id": row["chunk_id"],
            "doc_id": row["doc_id"],
            "title": row["metadata"].get("title"),
            "section": row["metadata"].get("section"),
            "version": row["metadata"].get("version"),
            "effective_date": row["metadata"].get("effective_date"),
            "text": row["content"],
            "score": row["score"],               # only for ordering; it is based on rank positions
            "similarity": row["similarity"],     # real closeness in meaning, 0 to 1
            "found_by_keyword": row["keyword_rank"] is not None,
            "found_by_meaning": row["semantic_rank"] is not None,
        })
    return results


def search_chunks(question, number_of_results=DEFAULT_NUMBER_OF_RESULTS):
    """Return the best chunks for a question, best first. Each item is a dictionary."""
    if not question or not question.strip():
        raise ValueError("The question is empty.")
    clean_question = question.strip()
    question_vector = embed_one_text(clean_question)
    return search_chunks_with_vector(clean_question, question_vector, number_of_results)


def print_results(question, results):
    """Show the results in a readable way (used when running this file from the terminal)."""
    print(f"\nQuestion: {question}\n")
    for position, result in enumerate(results, start=1):
        found_by = ", ".join(
            name for name, flag in (("keyword", result["found_by_keyword"]), ("meaning", result["found_by_meaning"])) if flag
        )
        print(f"{position}. {result['chunk_id']}   similarity {result['similarity']:.3f}   found by: {found_by}")
        first_lines = result["text"].split("\n")
        print(f"   {first_lines[0]}")
        print(f"   {' '.join(first_lines[1:])[:160]}...\n")


if __name__ == "__main__":
    question_from_user = " ".join(sys.argv[1:]) or "How many casual leaves do I get?"
    print_results(question_from_user, search_chunks(question_from_user))
