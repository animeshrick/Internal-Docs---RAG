"""
supabase.py - the generic Supabase (PostgREST) client every store shares.

Nothing in here knows about chunks, employees or leave: it is plain table access.
Domain-specific reads and writes live in the store module of their own domain
(app/hr/leave/store.py, app/rag/ingest/index.py, ...).

It talks to Supabase through its REST API using the plain "requests" library,
so there is no extra Supabase package to install.
"""
import json

import requests

from app.core.config import get_required_setting

REQUEST_TIMEOUT_SECONDS = 60
ROWS_PER_PAGE = 1000          # Supabase returns at most 1000 rows per request
IDS_PER_DELETE_REQUEST = 50   # keeps the delete URL short


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


def read_rows(table_name, params, action_description):
    """GET rows from one table. The generic read used by every domain store."""
    response = requests.get(
        get_table_url(table_name),
        headers=build_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, action_description)
    return response.json()


def flatten_role(row):
    """PostgREST sends the embedded login as a dictionary or a list. We want row["role"] (or None).

    A response-shape fix, not a rule about salaries or people, so it lives here with the
    rest of the PostgREST plumbing.
    """
    login = row.pop("employee_auth", None)
    if isinstance(login, list):
        login = login[0] if login else None
    row["role"] = login["role"] if login else None
    return row


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
