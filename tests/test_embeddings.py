"""Tests for app/supabase_store.py, app/embeddings.py and app/embeddings_with_voyage.py

No real network calls are made. A small fake stands in for Supabase and for Voyage.
"""
import json

import pytest

import embeddings
import embeddings_with_voyage
import supabase_store


# ---------------------------------------------------------------- fakes
class FakeResponse:
    """The few things of a requests.Response that our code uses."""

    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json_data


class FakeSupabase:
    """An in-memory table that answers the GET / POST / DELETE calls made by supabase_store."""

    def __init__(self):
        self.rows = {}                  # chunk_id -> row
        self.delete_calls = 0

    def get(self, url, headers=None, params=None, timeout=None):
        all_rows = list(self.rows.values())
        start = params.get("offset", 0)
        page = all_rows[start:start + params["limit"]]
        wanted_columns = params["select"].split(",")
        page = [{column: row[column] for column in wanted_columns} for row in page]
        return FakeResponse(json_data=page, headers={"Content-Range": f"0-0/{len(all_rows)}"})

    def post(self, url, headers=None, params=None, data=None, timeout=None, json=None):
        assert params == {"on_conflict": "chunk_id"}
        assert "merge-duplicates" in headers["Prefer"]
        for row in json_module_loads(data):
            self.rows[row["chunk_id"]] = row
        return FakeResponse(status_code=201)

    def delete(self, url, headers=None, params=None, timeout=None):
        self.delete_calls += 1
        # params look like {"chunk_id": 'in.("a","b")'}
        id_list_text = params["chunk_id"][len("in.("):-1]
        for chunk_id in id_list_text.replace('"', "").split(","):
            self.rows.pop(chunk_id, None)
        return FakeResponse(status_code=204)


def json_module_loads(text):
    return json.loads(text)


def make_chunk(chunk_id, text="Some policy text.", hash_value="hash-1", status="Approved"):
    return {
        "chunk_id": chunk_id, "text": text, "tokens": 5, "hash": hash_value,
        "metadata": {"doc_id": chunk_id.split("#")[0], "status": status, "title": "Test Policy"},
    }


def fake_embed_documents(texts):
    """A pretend model: every text becomes the same 3-number vector."""
    return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-key")
    monkeypatch.setattr(supabase_store.requests, "get", fake.get)
    monkeypatch.setattr(supabase_store.requests, "post", fake.post)
    monkeypatch.setattr(supabase_store.requests, "delete", fake.delete)
    return fake


# ---------------------------------------------------------------- small helpers
def test_split_into_batches():
    assert supabase_store.split_into_batches([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert supabase_store.split_into_batches([], 3) == []


def test_load_chunks_rejects_duplicate_ids(tmp_path):
    chunks_file = tmp_path / "chunks.jsonl"
    chunks_file.write_text(json.dumps(make_chunk("A#x#00")) + "\n" + json.dumps(make_chunk("A#x#00")) + "\n")
    with pytest.raises(ValueError):
        supabase_store.load_chunks(chunks_file)


def test_get_required_setting_stops_when_missing(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_SETTING", raising=False)
    from config import get_required_setting
    with pytest.raises(RuntimeError):
        get_required_setting("SOME_MISSING_SETTING")


def test_build_row_has_every_table_column():
    row = supabase_store.build_row(make_chunk("A#x#00"), [0.1, 0.2], "model-a")
    assert set(row) == {"chunk_id", "doc_id", "status", "content", "content_hash",
                        "embedding_model", "metadata", "embedding"}
    assert row["doc_id"] == "A"
    assert row["content_hash"] == "hash-1"


# ---------------------------------------------------------------- deciding what to embed
def test_new_chunk_is_embedded():
    chunks = [make_chunk("A#x#00")]
    assert supabase_store.find_chunks_to_embed(chunks, {}, "model-a") == chunks


def test_unchanged_chunk_is_skipped():
    chunks = [make_chunk("A#x#00", hash_value="same")]
    existing = {"A#x#00": {"content_hash": "same", "embedding_model": "model-a"}}
    assert supabase_store.find_chunks_to_embed(chunks, existing, "model-a") == []


def test_changed_text_is_embedded_again():
    chunks = [make_chunk("A#x#00", hash_value="new")]
    existing = {"A#x#00": {"content_hash": "old", "embedding_model": "model-a"}}
    assert supabase_store.find_chunks_to_embed(chunks, existing, "model-a") == chunks


def test_changed_model_is_embedded_again_even_if_text_is_same():
    chunks = [make_chunk("A#x#00", hash_value="same")]
    existing = {"A#x#00": {"content_hash": "same", "embedding_model": "old-model"}}
    assert supabase_store.find_chunks_to_embed(chunks, existing, "new-model") == chunks


def test_stale_chunk_ids_are_found():
    chunks = [make_chunk("A#x#00")]
    existing = {"A#x#00": {}, "A#removed#00": {}}
    assert supabase_store.find_stale_chunk_ids(chunks, existing) == ["A#removed#00"]


# ---------------------------------------------------------------- the whole flow with a fake Supabase
def run_ingest(monkeypatch, chunks, batch_size=2, dry_run=False):
    monkeypatch.setattr(supabase_store, "load_chunks", lambda: chunks)
    supabase_store.ingest("hr_chunks", "model-a", fake_embed_documents, batch_size, dry_run=dry_run)


def test_first_run_saves_every_chunk(fake_supabase, monkeypatch):
    chunks = [make_chunk(f"A#x#{number:02d}") for number in range(5)]
    run_ingest(monkeypatch, chunks)
    assert set(fake_supabase.rows) == {chunk["chunk_id"] for chunk in chunks}
    assert fake_supabase.rows["A#x#00"]["embedding"] == [0.1, 0.2, 0.3]


def test_second_run_embeds_nothing(fake_supabase, monkeypatch):
    chunks = [make_chunk(f"A#x#{number:02d}") for number in range(3)]
    run_ingest(monkeypatch, chunks)

    calls = []
    monkeypatch.setattr(supabase_store, "upsert_rows", lambda table, rows: calls.append(rows))
    run_ingest(monkeypatch, chunks)
    assert calls == []                      # nothing was uploaded the second time


def test_only_the_changed_chunk_is_uploaded(fake_supabase, monkeypatch):
    chunks = [make_chunk("A#x#00"), make_chunk("A#x#01")]
    run_ingest(monkeypatch, chunks)

    chunks[1] = make_chunk("A#x#01", text="Edited text.", hash_value="hash-2")
    uploaded = []
    real_upsert = supabase_store.upsert_rows
    monkeypatch.setattr(supabase_store, "upsert_rows",
                        lambda table, rows: (uploaded.extend(rows), real_upsert(table, rows)))
    run_ingest(monkeypatch, chunks)
    assert [row["chunk_id"] for row in uploaded] == ["A#x#01"]
    assert fake_supabase.rows["A#x#01"]["content"] == "Edited text."


def test_removed_chunk_is_deleted_from_supabase(fake_supabase, monkeypatch):
    run_ingest(monkeypatch, [make_chunk("A#x#00"), make_chunk("A#gone#00")])
    run_ingest(monkeypatch, [make_chunk("A#x#00")])
    assert set(fake_supabase.rows) == {"A#x#00"}


def test_dry_run_changes_nothing(fake_supabase, monkeypatch):
    run_ingest(monkeypatch, [make_chunk("A#x#00")], dry_run=True)
    assert fake_supabase.rows == {}


def test_more_rows_than_one_page_are_all_read(fake_supabase, monkeypatch):
    monkeypatch.setattr(supabase_store, "ROWS_PER_PAGE", 2)
    for number in range(5):
        fake_supabase.rows[f"A#x#{number:02d}"] = {
            "chunk_id": f"A#x#{number:02d}", "content_hash": "h", "embedding_model": "m"}
    assert len(supabase_store.fetch_existing_rows("hr_chunks")) == 5


def test_delete_is_split_into_small_requests(fake_supabase):
    for number in range(120):
        fake_supabase.rows[f"A#x#{number:03d}"] = {}
    supabase_store.delete_rows("hr_chunks", list(fake_supabase.rows))
    assert fake_supabase.rows == {}
    assert fake_supabase.delete_calls == 3          # 120 ids at 50 per request


def test_failed_request_gives_a_readable_error(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-key")
    monkeypatch.setattr(supabase_store.requests, "get",
                        lambda *args, **kwargs: FakeResponse(status_code=401, text="Invalid API key"))
    with pytest.raises(RuntimeError, match="Invalid API key"):
        supabase_store.fetch_existing_rows("hr_chunks")


# ---------------------------------------------------------------- embeddings.py (Supabase gte-small)
def set_supabase_settings(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-key")


def test_supabase_embedding_sends_one_text_per_request(monkeypatch):
    set_supabase_settings(monkeypatch)
    sent_bodies = []

    def fake_post(url, headers=None, json=None, timeout=None):
        assert url == "https://example.supabase.co/functions/v1/embed"
        sent_bodies.append(json)
        return FakeResponse(json_data={"embeddings": [[0.0] * 384]})

    monkeypatch.setattr(embeddings.requests, "post", fake_post)
    vectors = embeddings.embed_documents(["one", "two"])
    assert sent_bodies == [{"inputs": ["one"]}, {"inputs": ["two"]}]
    assert len(vectors) == 2 and len(vectors[0]) == 384


def test_supabase_embedding_rejects_wrong_vector_size(monkeypatch):
    set_supabase_settings(monkeypatch)
    monkeypatch.setattr(embeddings.requests, "post",
                        lambda *args, **kwargs: FakeResponse(json_data={"embeddings": [[0.0] * 10]}))
    with pytest.raises(RuntimeError, match="384"):
        embeddings.embed_documents(["one"])


def test_supabase_embedding_retries_when_function_is_out_of_resources(monkeypatch):
    set_supabase_settings(monkeypatch)
    answers = [FakeResponse(status_code=546), FakeResponse(json_data={"embeddings": [[0.0] * 384]})]
    monkeypatch.setattr(embeddings.requests, "post", lambda *args, **kwargs: answers.pop(0))
    waited = []
    monkeypatch.setattr(embeddings.time, "sleep", waited.append)
    assert len(embeddings.embed_documents(["one"])) == 1
    assert waited == [embeddings.SECONDS_TO_WAIT_BEFORE_RETRY]


def test_supabase_embedding_stops_after_the_last_attempt(monkeypatch):
    set_supabase_settings(monkeypatch)
    monkeypatch.setattr(embeddings.requests, "post", lambda *args, **kwargs: FakeResponse(status_code=546, text="limit"))
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="546"):
        embeddings.embed_documents(["one"])


def test_supabase_embedding_does_not_retry_a_wrong_key(monkeypatch):
    set_supabase_settings(monkeypatch)
    calls = []
    monkeypatch.setattr(embeddings.requests, "post",
                        lambda *args, **kwargs: (calls.append(1), FakeResponse(status_code=401, text="Unauthorized"))[1])
    with pytest.raises(RuntimeError, match="401"):
        embeddings.embed_documents(["one"])
    assert len(calls) == 1


# ---------------------------------------------------------------- embeddings_with_voyage.py
def voyage_answer(count, size=1024):
    # Return the items in reverse order on purpose: our code must sort them by "index".
    items = [{"index": position, "embedding": [float(position)] * size} for position in range(count)]
    return FakeResponse(json_data={"data": list(reversed(items))})


def test_voyage_keeps_the_original_order(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    monkeypatch.setattr(embeddings_with_voyage.requests, "post", lambda *args, **kwargs: voyage_answer(3))
    vectors = embeddings_with_voyage.embed_documents(["a", "b", "c"])
    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0]


def test_voyage_request_uses_document_input_type(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["body"], sent["headers"] = json, headers
        return voyage_answer(1)

    monkeypatch.setattr(embeddings_with_voyage.requests, "post", fake_post)
    embeddings_with_voyage.embed_documents(["a"])
    assert sent["body"]["input_type"] == "document"
    assert sent["body"]["output_dimension"] == 1024
    assert sent["headers"]["Authorization"] == "Bearer test-key"


def test_voyage_waits_and_retries_after_rate_limit(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    answers = [FakeResponse(status_code=429), FakeResponse(status_code=429), voyage_answer(1)]
    monkeypatch.setattr(embeddings_with_voyage.requests, "post", lambda *args, **kwargs: answers.pop(0))
    waited = []
    monkeypatch.setattr(embeddings_with_voyage.time, "sleep", waited.append)
    vectors = embeddings_with_voyage.embed_documents(["a"])
    assert len(vectors) == 1
    assert waited == [embeddings_with_voyage.SECONDS_TO_WAIT_AFTER_RATE_LIMIT] * 2


def test_voyage_gives_up_after_too_many_rate_limits(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    monkeypatch.setattr(embeddings_with_voyage.requests, "post", lambda *args, **kwargs: FakeResponse(status_code=429))
    monkeypatch.setattr(embeddings_with_voyage.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="too many requests"):
        embeddings_with_voyage.embed_documents(["a"])


def test_voyage_other_errors_stop_immediately(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "bad-key")
    monkeypatch.setattr(embeddings_with_voyage.requests, "post",
                        lambda *args, **kwargs: FakeResponse(status_code=401, text="Invalid key"))
    with pytest.raises(RuntimeError, match="401"):
        embeddings_with_voyage.embed_documents(["a"])


def test_voyage_rejects_wrong_vector_size(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    monkeypatch.setattr(embeddings_with_voyage.requests, "post", lambda *args, **kwargs: voyage_answer(1, size=256))
    with pytest.raises(RuntimeError, match="1024"):
        embeddings_with_voyage.embed_documents(["a"])


# ---------------------------------------------------------------- headers for the two key styles
def test_new_secret_key_is_sent_only_in_the_apikey_header(monkeypatch):
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_abc")
    headers = supabase_store.build_headers()
    assert headers["apikey"] == "sb_secret_abc"
    assert "Authorization" not in headers


def test_old_service_role_key_is_also_sent_as_bearer_token(monkeypatch):
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "eyJhbGciOi.old.jwt")
    headers = supabase_store.build_headers()
    assert headers["Authorization"] == "Bearer eyJhbGciOi.old.jwt"


# ---------------------------------------------------------------- retriever.py
import retriever  # noqa: E402


def make_database_row(chunk_id, keyword_rank, semantic_rank):
    return {
        "chunk_id": chunk_id, "doc_id": chunk_id.split("#")[0], "content": "Title > Section\nBody text.",
        "metadata": {"title": "Leave Policy", "section": "2. Leave types", "version": "1.2",
                     "effective_date": "2026-01-01"},
        "score": 0.03, "similarity": 0.81, "keyword_rank": keyword_rank, "semantic_rank": semantic_rank,
    }


def test_retriever_sends_question_and_vector_and_formats_results(monkeypatch):
    sent = {}
    monkeypatch.setattr(retriever, "embed_one_text", lambda text: [0.5] * 384)

    def fake_database_call(function_name, arguments):
        sent["function_name"], sent["arguments"] = function_name, arguments
        return [make_database_row("HR-LEAVE-001#leave-types#00", keyword_rank=1, semantic_rank=None),
                make_database_row("HR-LEAVE-001#policy-at-a-glance#00", keyword_rank=None, semantic_rank=2)]

    monkeypatch.setattr(retriever, "call_database_function", fake_database_call)
    results = retriever.search_chunks("  How many casual leaves?  ", number_of_results=2)

    assert sent["function_name"] == "hybrid_search_hr_chunks"
    assert sent["arguments"]["query_text"] == "How many casual leaves?"     # spaces trimmed
    assert sent["arguments"]["match_count"] == 2
    assert len(sent["arguments"]["query_embedding"]) == 384
    assert results[0]["found_by_keyword"] and not results[0]["found_by_meaning"]
    assert results[1]["found_by_meaning"] and not results[1]["found_by_keyword"]
    assert results[0]["title"] == "Leave Policy" and results[0]["version"] == "1.2"
    assert results[0]["similarity"] == 0.81
    assert sent["arguments"]["keyword_weight"] == 1.0 and sent["arguments"]["semantic_weight"] == 2.0   # the new defaults


def test_retriever_passes_custom_weights_and_reuses_the_vector(monkeypatch):
    sent = {}
    monkeypatch.setattr(retriever, "call_database_function",
                        lambda name, arguments: (sent.update(arguments), [])[1])
    retriever.search_chunks_with_vector("question", [0.1] * 384, 3, keyword_weight=0.5, semantic_weight=2.0)
    assert sent["keyword_weight"] == 0.5 and sent["semantic_weight"] == 2.0
    assert sent["query_embedding"] == [0.1] * 384


def test_retriever_rejects_an_empty_question():
    with pytest.raises(ValueError):
        retriever.search_chunks("   ")


def test_database_function_call_goes_to_the_rpc_address(monkeypatch):
    set_supabase_settings(monkeypatch)
    sent = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        sent["url"], sent["body"] = url, json.loads(data)
        return FakeResponse(json_data=[])

    monkeypatch.setattr(supabase_store.requests, "post", fake_post)
    supabase_store.call_database_function("hybrid_search_hr_chunks", {"query_text": "leave"})
    assert sent["url"] == "https://example.supabase.co/rest/v1/rpc/hybrid_search_hr_chunks"
    assert sent["body"] == {"query_text": "leave"}
