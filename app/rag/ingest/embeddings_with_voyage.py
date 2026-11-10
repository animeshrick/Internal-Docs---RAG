"""
embeddings_with_voyage.py - embed the chunks with Voyage AI and save them in Supabase.

Note: Anthropic does not make its own embedding model. Its documentation recommends
Voyage AI, which is a separate company with its own account and API key.

Before the first run:
    1. Run sql/01_create_tables.sql in the Supabase SQL editor (creates the hr_chunks_voyage table).
    2. Fill SUPABASE_URL, SUPABASE_SECRET_KEY and VOYAGE_API_KEY in .env

Run:
    python app/embeddings_with_voyage.py --dry-run     (shows the plan, changes nothing)
    python app/embeddings_with_voyage.py               (embeds and saves)
"""
import os
import sys
import time

import requests

from app.core.config import get_required_setting
from app.core.supabase import REQUEST_TIMEOUT_SECONDS
from app.rag.ingest.index import ingest

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL_NAME = os.getenv("VOYAGE_MODEL", "voyage-3.5-lite")
TABLE_NAME = "hr_chunks_voyage"
EMBEDDING_SIZE = 1024           # must match vector(1024) in sql/01_create_tables.sql
BATCH_SIZE = 16                 # small batches keep us under the rate limit of an account without billing set up
MAXIMUM_ATTEMPTS = 5            # how many times we try one batch before giving up
SECONDS_TO_WAIT_AFTER_RATE_LIMIT = 21   # a limit of 3 requests per minute means one request every 20 seconds


def embed_documents(texts):
    """Send a list of texts to Voyage and get back one vector per text."""
    headers = {
        "Authorization": f"Bearer {get_required_setting('VOYAGE_API_KEY')}",
        "Content-Type": "application/json",
    }
    request_body = {
        "input": texts,
        "model": VOYAGE_MODEL_NAME,
        "input_type": "document",          # tells Voyage these are stored documents, not search questions
        "output_dimension": EMBEDDING_SIZE,
    }

    for attempt_number in range(1, MAXIMUM_ATTEMPTS + 1):
        response = requests.post(VOYAGE_API_URL, headers=headers, json=request_body, timeout=REQUEST_TIMEOUT_SECONDS)

        if response.status_code == 429:    # 429 = "too many requests"; wait and try again
            print(f"  rate limit reached, waiting {SECONDS_TO_WAIT_AFTER_RATE_LIMIT} seconds "
                  f"(attempt {attempt_number} of {MAXIMUM_ATTEMPTS})")
            time.sleep(SECONDS_TO_WAIT_AFTER_RATE_LIMIT)
            continue

        if not response.ok:
            raise RuntimeError(f"Voyage request failed ({response.status_code}): {response.text[:500]}")

        # Voyage returns {"data": [{"index": 0, "embedding": [...]}, ...]}. Sort by index to keep our order.
        items = sorted(response.json()["data"], key=lambda item: item["index"])
        embedding_vectors = [item["embedding"] for item in items]
        if len(embedding_vectors) != len(texts):
            raise RuntimeError(f"Sent {len(texts)} texts but received {len(embedding_vectors)} vectors.")
        if len(embedding_vectors[0]) != EMBEDDING_SIZE:
            raise RuntimeError(f"Expected {EMBEDDING_SIZE} numbers per vector, got {len(embedding_vectors[0])}.")
        return embedding_vectors

    raise RuntimeError("Voyage kept returning 'too many requests'. Add a payment method or wait and run again.")


if __name__ == "__main__":
    ingest(
        table_name=TABLE_NAME,
        embedding_model_name=VOYAGE_MODEL_NAME,
        embed_documents=embed_documents,
        batch_size=BATCH_SIZE,
        dry_run="--dry-run" in sys.argv,
    )
