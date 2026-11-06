"""
supabase_store.py - the part of ingestion that is the same for every embedding model.

It talks to Supabase through its REST API (PostgREST) using the plain "requests" library,
so there is no extra Supabase package to install.

The main function is ingest(). It does this:
    1. read chunks.jsonl
    2. ask Supabase which chunks are already stored (and with which hash and model)
    3. embed only the chunks that are new or changed
    4. delete stored chunks that no longer exist in chunks.jsonl
    5. check that the row count in Supabase matches the chunk count
"""
import json

import requests

from config import CHUNKS_FILE_PATH, get_required_setting

REQUEST_TIMEOUT_SECONDS = 60
ROWS_PER_PAGE = 1000          # Supabase returns at most 1000 rows per request
IDS_PER_DELETE_REQUEST = 50   # keeps the delete URL short


# ------------------------------------------------------------------ small helpers
def build_headers(extra_headers=None):
    """Headers that every Supabase request needs."""
    secret_key = get_required_setting("SUPABASE_SECRET_KEY")
    # The new secret keys (sb_secret_...) are not JWTs, so Supabase wants them in the "apikey" header only.
    headers = {"apikey": secret_key, "Content-Type": "application/json"}
    if not secret_key.startswith("sb_"):
        # An old-style service_role key is a JWT and is also sent as a Bearer token.
        headers["Authorization"] = f"Bearer {secret_key}"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def get_table_url(table_name):
    supabase_url = get_required_setting("SUPABASE_URL").rstrip("/")
    return f"{supabase_url}/rest/v1/{table_name}"


def check_response(response, action_description):
    """Stop with a readable error if Supabase answered with a failure code."""
    if not response.ok:
        raise RuntimeError(f"{action_description} failed ({response.status_code}): {response.text[:500]}")


def split_into_batches(items, batch_size):
    """[1,2,3,4,5] with batch_size 2 -> [[1,2],[3,4],[5]]"""
    return [items[start:start + batch_size] for start in range(0, len(items), batch_size)]


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


def call_database_function(function_name, arguments):
    """Run a function stored in the database (created in sql/02_hybrid_search_function.sql)."""
    supabase_url = get_required_setting("SUPABASE_URL").rstrip("/")
    response = requests.post(
        f"{supabase_url}/rest/v1/rpc/{function_name}",
        headers=build_headers(),
        data=json.dumps(arguments),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, f"Calling the database function {function_name}")
    return response.json()


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

    print(f"Chunks in file      : {len(chunks)}")
    print(f"Already up to date  : {len(chunks) - len(chunks_to_embed)}")
    print(f"To embed and save   : {len(chunks_to_embed)}")
    print(f"To delete (stale)   : {len(stale_chunk_ids)}")
    if dry_run:
        print("Dry run: nothing was changed.")
        return

    if stale_chunk_ids:
        delete_rows(table_name, stale_chunk_ids)

    for batch_number, batch_of_chunks in enumerate(split_into_batches(chunks_to_embed, batch_size), start=1):
        texts_to_embed = [chunk["text"] for chunk in batch_of_chunks]
        embedding_vectors = embed_documents(texts_to_embed)
        rows = [build_row(chunk, vector, embedding_model_name)
                for chunk, vector in zip(batch_of_chunks, embedding_vectors)]
        upsert_rows(table_name, rows)   # saved batch by batch, so a crash can be resumed by running again
        print(f"  batch {batch_number}: saved {len(rows)} chunks")

    rows_in_supabase = count_rows(table_name)
    print(f"Rows in Supabase    : {rows_in_supabase} (expected {len(chunks)})")
    if rows_in_supabase != len(chunks):
        print("WARNING: the counts differ. Check for chunks with a status you filtered out, or run again.")
