"""
embeddings.py - embed the chunks with Supabase's built-in gte-small model and save them in Supabase.

Why this file: no API key, no cost per call, and no server of your own.
The model runs inside a Supabase Edge Function (supabase/functions/embed/index.ts).

Before the first run:
    1. Run sql/01_create_tables.sql in the Supabase SQL editor.
    2. Deploy the function:  supabase functions deploy embed --no-verify-jwt
    3. Fill SUPABASE_URL and SUPABASE_SECRET_KEY in .env

Run:
    python app/embeddings.py --dry-run     (shows the plan, changes nothing)
    python app/embeddings.py               (embeds and saves)
"""
import sys
import time

import requests

from app.core.config import get_required_setting
from app.core.supabase import build_headers, check_response, REQUEST_TIMEOUT_SECONDS
from supabase_store import ingest

TABLE_NAME = "hr_chunks"
EMBEDDING_MODEL_NAME = "supabase-gte-small"
EMBEDDING_SIZE = 384            # gte-small always returns 384 numbers; the table column must match
BATCH_SIZE = 8                  # how many chunks are saved to the database at once

# A free Edge Function has little compute (error 546 = out of resources), so we send ONE text per request.
# These status codes usually mean "busy or just starting up", so we wait and try the same text again.
RETRY_STATUS_CODES = (429, 502, 503, 504, 546)
MAXIMUM_ATTEMPTS = 4
SECONDS_TO_WAIT_BEFORE_RETRY = 5


def embed_one_text(text):
    """Send one text to the Edge Function and return its 384-number vector."""
    function_url = get_required_setting("SUPABASE_URL").rstrip("/") + "/functions/v1/embed"

    for attempt_number in range(1, MAXIMUM_ATTEMPTS + 1):
        response = requests.post(
            function_url,
            headers=build_headers(),
            json={"inputs": [text]},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in RETRY_STATUS_CODES and attempt_number < MAXIMUM_ATTEMPTS:
            print(f"  function busy (status {response.status_code}), waiting "
                  f"{SECONDS_TO_WAIT_BEFORE_RETRY} seconds (attempt {attempt_number} of {MAXIMUM_ATTEMPTS})")
            time.sleep(SECONDS_TO_WAIT_BEFORE_RETRY)
            continue

        check_response(response, "Embedding with the Supabase Edge Function")
        embedding_vector = response.json()["embeddings"][0]
        if len(embedding_vector) != EMBEDDING_SIZE:
            raise RuntimeError(f"Expected {EMBEDDING_SIZE} numbers per vector, got {len(embedding_vector)}.")
        return embedding_vector


def embed_documents(texts):
    """Return one vector per text, in the same order."""
    return [embed_one_text(text) for text in texts]


if __name__ == "__main__":
    ingest(
        table_name=TABLE_NAME,
        embedding_model_name=EMBEDDING_MODEL_NAME,
        embed_documents=embed_documents,
        batch_size=BATCH_SIZE,
        dry_run="--dry-run" in sys.argv,
    )
