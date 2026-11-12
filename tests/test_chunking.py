"""Tests for app/chunking.py"""
import copy

import pytest

from app.rag.ingest.chunking import CONFIG, check_chunks, chunk_all, chunk_document, est_tokens, slug, split_sentences

MAXIMUM_ALLOWED_TOKENS = CONFIG["max_tokens"] * 1.1     # same 10% tolerance as check_chunks


@pytest.fixture(scope="module")
def all_chunks(parsed_documents):
    chunks, _skipped_documents = chunk_all(parsed_documents)
    return chunks


def make_document(blocks, status="Approved", doc_type="policy"):
    """Build a tiny fake document so we can test one behaviour at a time."""
    return {
        "source": "fake.docx", "doc_id": "HR-TEST-001", "doc_type": doc_type,
        "title": "Test Policy", "summary": None, "status": status, "version": "1.0",
        "effective_date": "2026-01-01", "owner": "HR", "placeholders": [], "warnings": [],
        "blocks": blocks,
    }


def make_heading(text, path):
    return {"type": "heading", "text": text, "heading_path": path, "source": "fake.docx"}


def make_text(text, path):
    return {"type": "text", "text": text, "heading_path": path, "source": "fake.docx"}


# ---------------------------------------------------------------- helpers
def test_slug_makes_a_safe_id():
    assert slug("4. Legal alignment notes - as of 2026-10-28") == "4-legal-alignment-notes-as-of-2026-10-28"


def test_est_tokens_is_never_zero():
    assert est_tokens("") == 1


def test_split_sentences_splits_on_full_stops():
    assert split_sentences("One. Two. Three.") == ["One.", "Two.", "Three."]


# ---------------------------------------------------------------- the real documents
def test_chunker_produces_chunks_for_every_document(parsed_documents, all_chunks):
    doc_ids_with_chunks = {chunk["metadata"]["doc_id"] for chunk in all_chunks}
    assert doc_ids_with_chunks == {document["doc_id"] for document in parsed_documents}


def test_chunk_ids_are_unique(all_chunks):
    chunk_ids = [chunk["chunk_id"] for chunk in all_chunks]
    assert len(chunk_ids) == len(set(chunk_ids))


def test_no_chunk_is_too_large(all_chunks):
    for chunk in all_chunks:
        assert chunk["tokens"] <= MAXIMUM_ALLOWED_TOKENS, chunk["chunk_id"]


def test_every_chunk_has_a_body_after_its_heading_line(all_chunks):
    for chunk in all_chunks:
        first_line, _separator, body = chunk["text"].partition("\n")
        assert body.strip(), chunk["chunk_id"]


def test_every_chunk_starts_with_the_document_title(all_chunks):
    for chunk in all_chunks:
        first_line = chunk["text"].split("\n")[0]
        assert first_line.startswith(chunk["metadata"]["title"]), chunk["chunk_id"]


def test_no_chunk_has_null_metadata(all_chunks):
    for chunk in all_chunks:
        assert None not in chunk["metadata"].values(), chunk["chunk_id"]


def test_chunk_ids_and_hashes_are_stable_between_runs(parsed_documents, all_chunks):
    second_run_chunks, _skipped = chunk_all(parsed_documents)
    assert [c["chunk_id"] for c in all_chunks] == [c["chunk_id"] for c in second_run_chunks]
    assert [c["hash"] for c in all_chunks] == [c["hash"] for c in second_run_chunks]


def test_built_in_checks_report_no_errors(parsed_documents, all_chunks):
    problem_messages = check_chunks(parsed_documents, all_chunks)
    errors = [message for message in problem_messages if message.startswith("ERROR")]
    assert errors == []


def test_every_table_row_appears_in_a_table_chunk(parsed_documents, all_chunks):
    for document in parsed_documents:
        table_chunk_text = "\n".join(
            chunk["text"] for chunk in all_chunks
            if chunk["metadata"]["doc_id"] == document["doc_id"] and chunk["metadata"]["kind"] == "table"
        )
        for block in document["blocks"]:
            if block["type"] == "table":
                for row_sentence in block["lines"]:
                    assert row_sentence in table_chunk_text, f"{document['doc_id']}: {row_sentence[:50]}"


def test_table_rows_are_never_mixed_into_text_chunks(all_chunks):
    for chunk in all_chunks:
        if chunk["metadata"]["kind"] == "table":
            assert chunk["metadata"]["block_types"] == ["table"], chunk["chunk_id"]


def test_leave_policy_at_a_glance_contains_casual_leave(all_chunks):
    # A real HR question: "how many casual leaves do I get?" must have an answer chunk.
    matching_chunks = [c for c in all_chunks
                       if c["metadata"]["doc_id"] == "HR-LEAVE-001" and "Casual leave: 8 paid working days" in c["text"]]
    assert matching_chunks


# ---------------------------------------------------------------- one behaviour at a time
def test_draft_document_is_skipped():
    document = make_document([make_text("Some rule.", ["Test Policy", "1. Rules"])], status="Draft")
    chunks, reason = chunk_document(document)
    assert chunks == []
    assert "Draft" in reason


def test_document_without_status_is_skipped_unless_reference():
    blocks = [make_text("Some fact.", ["Test Policy", "1. Facts"])]
    policy_chunks, policy_reason = chunk_document(make_document(blocks, status=None))
    reference_chunks, reference_reason = chunk_document(make_document(blocks, status=None, doc_type="reference"))
    assert policy_chunks == [] and policy_reason
    assert reference_chunks and reference_reason is None


def test_two_sections_with_the_same_title_get_different_ids():
    # Three sections in order: "General rules", "Other section", "General rules" again.
    # The blocks are grouped by section, so the repeated title forms a second group.
    blocks = [
        make_text("First body.", ["Test Policy", "General rules"]),
        make_text("Middle body.", ["Test Policy", "Other section"]),
        make_text("Second body.", ["Test Policy", "General rules"]),
    ]
    chunks, _reason = chunk_document(make_document(blocks))
    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    assert len(chunk_ids) == len(set(chunk_ids))                     # nothing overwritten
    assert "HR-TEST-001#general-rules#00" in chunk_ids               # first one keeps the plain id
    assert "HR-TEST-001#general-rules-2#00" in chunk_ids             # repeat gets the -2 suffix


def test_title_only_section_creates_no_chunk():
    document = make_document([make_heading("Test Policy", ["Test Policy"])])
    chunks, _reason = chunk_document(document)
    assert chunks == []


def test_huge_paragraph_is_split_and_loses_no_sentence():
    path = ["Test Policy", "1. Long section"]
    sentences = [f"This is sentence number {number} of a very long paragraph." for number in range(200)]
    document = make_document([make_text(" ".join(sentences), path)])
    chunks, _reason = chunk_document(document)
    assert len(chunks) > 1
    all_text = "\n".join(chunk["text"] for chunk in chunks)
    for sentence in sentences:
        assert sentence in all_text
    for chunk in chunks:
        assert chunk["tokens"] <= MAXIMUM_ALLOWED_TOKENS


def test_split_chunks_overlap():
    path = ["Test Policy", "1. Long section"]
    sentences = [f"This is sentence number {number} of a very long paragraph." for number in range(200)]
    document = make_document([make_text(" ".join(sentences), path)])
    chunks, _reason = chunk_document(document)
    last_sentence_of_first_chunk = chunks[0]["text"].split("\n", 1)[1].split(". ")[-1]
    assert last_sentence_of_first_chunk.strip(".") in chunks[1]["text"]


def test_changing_text_changes_the_hash():
    path = ["Test Policy", "1. Rules"]
    original = make_document([make_text("Leave is 10 days.", path)])
    edited = copy.deepcopy(original)
    edited["blocks"][0]["text"] = "Leave is 12 days."
    original_chunks, _ = chunk_document(original)
    edited_chunks, _ = chunk_document(edited)
    assert original_chunks[0]["chunk_id"] == edited_chunks[0]["chunk_id"]      # same place
    assert original_chunks[0]["hash"] != edited_chunks[0]["hash"]              # different content


def test_check_chunks_detects_a_lost_sentence():
    path = ["Test Policy", "1. Rules"]
    document = make_document([make_text("Keep this sentence. Lose this sentence.", path)])
    chunks, _reason = chunk_document(document)
    chunks[0]["text"] = chunks[0]["text"].replace("Lose this sentence.", "")   # simulate a bug
    problem_messages = check_chunks([document], chunks)
    assert any("missing from chunks" in message for message in problem_messages)
