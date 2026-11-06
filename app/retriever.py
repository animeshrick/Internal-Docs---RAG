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

from embeddings import embed_one_text
from supabase_store import call_database_function

DEFAULT_NUMBER_OF_RESULTS = 5
DATABASE_FUNCTION_NAME = "hybrid_search_hr_chunks"


def search_chunks(question, number_of_results=DEFAULT_NUMBER_OF_RESULTS):
    """Return the best chunks for a question, best first. Each item is a dictionary."""
    if not question or not question.strip():
        raise ValueError("The question is empty.")

    question_vector = embed_one_text(question.strip())

    found_rows = call_database_function(DATABASE_FUNCTION_NAME, {
        "query_text": question.strip(),
        "query_embedding": question_vector,
        "match_count": number_of_results,
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
            "score": row["score"],
            "found_by_keyword": row["keyword_rank"] is not None,
            "found_by_meaning": row["semantic_rank"] is not None,
        })
    return results


def print_results(question, results):
    """Show the results in a readable way (used when running this file from the terminal)."""
    print(f"\nQuestion: {question}\n")
    for position, result in enumerate(results, start=1):
        found_by = ", ".join(
            name for name, flag in (("keyword", result["found_by_keyword"]), ("meaning", result["found_by_meaning"])) if flag
        )
        print(f"{position}. {result['chunk_id']}   score {result['score']:.4f}   found by: {found_by}")
        first_lines = result["text"].split("\n")
        print(f"   {first_lines[0]}")
        print(f"   {' '.join(first_lines[1:])[:160]}...\n")


if __name__ == "__main__":
    question_from_user = " ".join(sys.argv[1:]) or "How many casual leaves do I get?"
    print_results(question_from_user, search_chunks(question_from_user))
