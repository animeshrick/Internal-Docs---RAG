"""
index.py - writing chunks and their vectors into Supabase.

The generic PostgREST plumbing lives in app/core/supabase.py; this module is the
part that knows about chunks.

The main function is ingest(). It does this:
    1. read chunks.jsonl
    2. ask Supabase which chunks are already stored (and with which hash and model)
    3. embed only the chunks that are new or changed
    4. delete stored chunks that no longer exist in chunks.jsonl
    5. check that the row count in Supabase matches the chunk count
"""
import logging
import json

import requests

from app.core.config import CHUNKS_FILE_PATH
from app.core.supabase import (
    IDS_PER_DELETE_REQUEST,
    REQUEST_TIMEOUT_SECONDS,
    ROWS_PER_PAGE,
    build_headers,
    check_response,
    get_table_url,
    split_into_batches,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ reading
def load_chunks(chunks_file_path=CHUNKS_FILE_PATH):
    """Read chunks.jsonl (one JSON chunk per line) into a list."""
    chunks = []
    with open(chunks_file_path, encoding="utf8") as chunks_file:
        for line in chunks_file:
            if line.strip():
                chunks.append(json.loads(line))

    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("chunks.jsonl contains duplicate chunk_id values. Re-run chunking.py.")
    return chunks


def fetch_existing_rows(table_name):
    """Return {chunk_id: {"content_hash": ..., "embedding_model": ...}} for rows already in Supabase."""
    existing_rows = {}
    offset = 0
    while True:
        response = requests.get(
            get_table_url(table_name),
            headers=build_headers(),
            params={"select": "chunk_id,content_hash,embedding_model", "limit": ROWS_PER_PAGE, "offset": offset},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        check_response(response, "Reading existing rows")
        page_of_rows = response.json()
        for row in page_of_rows:
            existing_rows[row["chunk_id"]] = row
        if len(page_of_rows) < ROWS_PER_PAGE:
            return existing_rows
        offset += ROWS_PER_PAGE


def count_rows(table_name):
    """Ask Supabase how many rows the table has."""
    response = requests.get(
        get_table_url(table_name),
        headers=build_headers({"Prefer": "count=exact"}),
        params={"select": "chunk_id", "limit": 1},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Counting rows")
    # The answer looks like "0-0/176"; the number after the slash is the total.
    return int(response.headers["Content-Range"].split("/")[1])


# ------------------------------------------------------------------ deciding what to do
def find_chunks_to_embed(chunks, existing_rows, embedding_model_name):
    """A chunk needs embedding when it is new, its text changed, or it was made by another model."""
    chunks_to_embed = []
    for chunk in chunks:
        stored_row = existing_rows.get(chunk["chunk_id"])
        is_new = stored_row is None
        text_changed = stored_row is not None and stored_row["content_hash"] != chunk["hash"]
        model_changed = stored_row is not None and stored_row["embedding_model"] != embedding_model_name
        if is_new or text_changed or model_changed:
            chunks_to_embed.append(chunk)
    return chunks_to_embed


def find_stale_chunk_ids(chunks, existing_rows):
    """Chunk ids stored in Supabase that are no longer in chunks.jsonl (section removed, document drafted ...)."""
    current_chunk_ids = {chunk["chunk_id"] for chunk in chunks}
    return [stored_id for stored_id in existing_rows if stored_id not in current_chunk_ids]


# ------------------------------------------------------------------ writing
def build_row(chunk, embedding_vector, embedding_model_name):
    """Turn one chunk plus its vector into one database row."""
    return {
        "chunk_id": chunk["chunk_id"],
        "doc_id": chunk["metadata"]["doc_id"],
        "status": chunk["metadata"].get("status"),
        "content": chunk["text"],
        "content_hash": chunk["hash"],
        "embedding_model": embedding_model_name,
        "metadata": chunk["metadata"],
        "embedding": embedding_vector,
    }


def upsert_rows(table_name, rows):
    """Insert new rows and replace rows that already have the same chunk_id."""
    response = requests.post(
        get_table_url(table_name),
        headers=build_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        params={"on_conflict": "chunk_id"},
        data=json.dumps(rows),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Saving rows")


def delete_rows(table_name, chunk_ids):
    """Delete rows by chunk_id, a few at a time."""
    for batch_of_ids in split_into_batches(chunk_ids, IDS_PER_DELETE_REQUEST):
        # PostgREST list syntax: chunk_id=in.("id one","id two")
        quoted_ids = ",".join(f'"{chunk_id}"' for chunk_id in batch_of_ids)
        response = requests.delete(
            get_table_url(table_name),
            headers=build_headers(),
            params={"chunk_id": f"in.({quoted_ids})"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        check_response(response, "Deleting stale rows")


# ------------------------------------------------------------------ the whole flow
def ingest(table_name, embedding_model_name, embed_documents, batch_size, dry_run=False):
    """
    table_name           : Supabase table to fill (hr_chunks or hr_chunks_voyage)
    embedding_model_name : stored with each row, so we notice when the model changes
    embed_documents      : function that takes a list of texts and returns a list of vectors
    batch_size           : how many texts to embed per request
    dry_run              : only print what would happen; no embedding, no writes
    """
    chunks = load_chunks()
    existing_rows = fetch_existing_rows(table_name)
    chunks_to_embed = find_chunks_to_embed(chunks, existing_rows, embedding_model_name)
    stale_chunk_ids = find_stale_chunk_ids(chunks, existing_rows)

    logger.info("chunks in file: %d | already up to date: %d | to embed: %d | stale to delete: %d",
                len(chunks), len(chunks) - len(chunks_to_embed), len(chunks_to_embed), len(stale_chunk_ids))
    if dry_run:
        logger.info("dry run: nothing was changed")
        return

    if stale_chunk_ids:
        delete_rows(table_name, stale_chunk_ids)

    for batch_number, batch_of_chunks in enumerate(split_into_batches(chunks_to_embed, batch_size), start=1):
        texts_to_embed = [chunk["text"] for chunk in batch_of_chunks]
        embedding_vectors = embed_documents(texts_to_embed)
        rows = [build_row(chunk, vector, embedding_model_name)
                for chunk, vector in zip(batch_of_chunks, embedding_vectors)]
        upsert_rows(table_name, rows)   # saved batch by batch, so a crash can be resumed by running again
        logger.info("batch %d: saved %d chunks", batch_number, len(rows))

    rows_in_supabase = count_rows(table_name)
    if rows_in_supabase == len(chunks):
        logger.info("rows in Supabase: %d, as expected", rows_in_supabase)
    else:
        logger.warning("rows in Supabase: %d but chunks.jsonl has %d. Check for chunks with a status you "
                       "filtered out, or run again.", rows_in_supabase, len(chunks))
