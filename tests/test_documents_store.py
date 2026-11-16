"""Tests for app/hr/documents/store.py. requests.get/post/patch/delete are replaced with fakes."""
import pytest
import requests

from app.hr.documents import store as documents_store


class FakeResponse:
    def __init__(self, body=None, status_code=200):
        self._body = body if body is not None else []
        self.status_code = status_code
        self.text = str(self._body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def supabase_settings(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")


def test_list_documents_orders_newest_first(monkeypatch):
    seen = {}

    def fake_get(url, headers, params, timeout):
        seen["url"], seen["params"] = url, params
        return FakeResponse([{"id": 2}, {"id": 1}])

    monkeypatch.setattr(requests, "get", fake_get)
    rows = documents_store.list_documents()

    assert seen["url"] == "https://example.supabase.co/rest/v1/shared_documents"
    assert seen["params"]["order"] == "id.desc"
    assert [row["id"] for row in rows] == [2, 1]


def test_get_document_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, headers, params, timeout: FakeResponse([]))
    assert documents_store.get_document(999) is None


def test_get_document_filters_by_id(monkeypatch):
    seen = {}

    def fake_get(url, headers, params, timeout):
        seen.update(params)
        return FakeResponse([{"id": 5, "original_filename": "a.pdf"}])

    monkeypatch.setattr(requests, "get", fake_get)
    row = documents_store.get_document(5)

    assert seen["id"] == "eq.5"
    assert row["original_filename"] == "a.pdf"


def test_insert_document_sends_the_fields_and_returns_the_saved_row(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent["url"], sent["json"] = url, json
        return FakeResponse([{"id": 11, "original_filename": "report.pdf"}])

    monkeypatch.setattr(requests, "post", fake_post)
    row = documents_store.insert_document({"owner_id": 1, "original_filename": "report.pdf"})

    assert sent["url"] == "https://example.supabase.co/rest/v1/shared_documents"
    assert sent["json"] == {"owner_id": 1, "original_filename": "report.pdf"}
    assert row["id"] == 11


def test_delete_document_filters_by_id(monkeypatch):
    seen = {}

    def fake_delete(url, headers, params, timeout):
        seen["url"], seen["params"] = url, params
        return FakeResponse()

    monkeypatch.setattr(requests, "delete", fake_delete)
    documents_store.delete_document(7)

    assert seen["url"] == "https://example.supabase.co/rest/v1/shared_documents"
    assert seen["params"]["id"] == "eq.7"


def test_list_requests_for_document_filters_by_document_id(monkeypatch):
    seen = {}

    def fake_get(url, headers, params, timeout):
        seen.update(params)
        return FakeResponse([])

    monkeypatch.setattr(requests, "get", fake_get)
    documents_store.list_requests_for_document(3)
    assert seen["document_id"] == "eq.3"


def test_list_requests_by_requester_filters_by_requester_id(monkeypatch):
    seen = {}

    def fake_get(url, headers, params, timeout):
        seen.update(params)
        return FakeResponse([])

    monkeypatch.setattr(requests, "get", fake_get)
    documents_store.list_requests_by_requester(4)
    assert seen["requester_id"] == "eq.4"


def test_list_pending_requests_filters_by_status(monkeypatch):
    seen = {}

    def fake_get(url, headers, params, timeout):
        seen.update(params)
        return FakeResponse([])

    monkeypatch.setattr(requests, "get", fake_get)
    documents_store.list_pending_requests()
    assert seen["status"] == "eq.pending"


def test_find_request_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, headers, params, timeout: FakeResponse([]))
    assert documents_store.find_request(123) is None


def test_insert_request_sends_the_fields(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent["url"], sent["json"] = url, json
        return FakeResponse([{"id": 1, "status": "pending"}])

    monkeypatch.setattr(requests, "post", fake_post)
    row = documents_store.insert_request({"document_id": 1, "requester_id": 2, "status": "pending"})

    assert sent["url"] == "https://example.supabase.co/rest/v1/document_access_requests"
    assert sent["json"] == {"document_id": 1, "requester_id": 2, "status": "pending"}
    assert row["status"] == "pending"


def test_update_request_patches_by_id_and_returns_the_row(monkeypatch):
    sent = {}

    def fake_patch(url, headers, params, json, timeout):
        sent["url"], sent["params"], sent["json"] = url, params, json
        return FakeResponse([{"id": 9, "status": "approved"}])

    monkeypatch.setattr(requests, "patch", fake_patch)
    row = documents_store.update_request(9, {"status": "approved"})

    assert sent["url"] == "https://example.supabase.co/rest/v1/document_access_requests"
    assert sent["params"]["id"] == "eq.9"
    assert sent["json"] == {"status": "approved"}
    assert row["status"] == "approved"


def test_update_request_returns_none_when_nothing_matched(monkeypatch):
    monkeypatch.setattr(requests, "patch", lambda url, headers, params, json, timeout: FakeResponse([]))
    assert documents_store.update_request(9, {"status": "denied"}) is None
