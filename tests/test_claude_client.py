"""Tests for app/core/claude.py - the shared low-level Claude (Anthropic) HTTP client.
No real Anthropic calls are made. This is the one place the request shape, auth header,
retry logic, and the two response-extraction helpers are tested; callers (llm.py, assistant.py)
monkeypatch call_claude itself and don't need to re-test any of this."""
import pytest

from app.core import claude as claude_client


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json_body


def text_response(reply_text):
    return {"content": [{"type": "text", "text": reply_text}]}


@pytest.fixture(autouse=True)
def anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


# ---------------------------------------------------------------- call_claude
def test_call_claude_sends_model_messages_and_system(monkeypatch):
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"], sent["headers"], sent["body"] = url, headers, json
        return FakeResponse(json_body=text_response("hello"))

    monkeypatch.setattr(claude_client.requests, "post", fake_post)
    response = claude_client.call_claude([{"role": "user", "content": "hi"}], max_tokens=50, system="be brief")
    assert claude_client.extract_text(response) == "hello"
    assert sent["url"] == claude_client.CLAUDE_API_URL
    assert sent["headers"]["x-api-key"] == "test-key"
    assert sent["headers"]["anthropic-version"] == claude_client.ANTHROPIC_VERSION
    assert "Authorization" not in sent["headers"]          # Claude uses x-api-key, never Bearer
    assert sent["body"]["model"] == claude_client.CLAUDE_MODEL_NAME
    assert sent["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["body"]["system"] == "be brief"
    assert "temperature" not in sent["body"]   # Haiku 5.5 rejects this param with a 400
    assert sent["body"]["max_tokens"] == 50


def test_call_claude_leaves_out_system_and_tools_when_not_given(monkeypatch):
    sent = {}
    monkeypatch.setattr(claude_client.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: (sent.update(json), FakeResponse(json_body=text_response("ok")))[1])
    claude_client.call_claude([{"role": "user", "content": "hi"}], max_tokens=10)
    assert "system" not in sent
    assert "tools" not in sent


def test_call_claude_sends_tools_when_given(monkeypatch):
    sent = {}
    monkeypatch.setattr(claude_client.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: (sent.update(json), FakeResponse(json_body=text_response("ok")))[1])
    tools = [{"name": "search_policy", "description": "...", "input_schema": {"type": "object", "properties": {}}}]
    claude_client.call_claude([{"role": "user", "content": "hi"}], max_tokens=10, tools=tools)
    assert sent["tools"] == tools


def test_call_claude_waits_and_retries_on_rate_limit(monkeypatch):
    answers = [FakeResponse(status_code=429), FakeResponse(json_body=text_response("ok"))]
    monkeypatch.setattr(claude_client.requests, "post", lambda *args, **kwargs: answers.pop(0))
    waited = []
    monkeypatch.setattr(claude_client.time, "sleep", waited.append)
    response = claude_client.call_claude([], 10)
    assert claude_client.extract_text(response) == "ok"
    assert waited == [claude_client.SECONDS_TO_WAIT_BEFORE_RETRY]


def test_call_claude_retries_on_overloaded_status(monkeypatch):
    # 529 is Anthropic's own "overloaded" status - not a plain HTTP code other APIs use.
    answers = [FakeResponse(status_code=529), FakeResponse(json_body=text_response("ok"))]
    monkeypatch.setattr(claude_client.requests, "post", lambda *args, **kwargs: answers.pop(0))
    monkeypatch.setattr(claude_client.time, "sleep", lambda seconds: None)
    response = claude_client.call_claude([], 10)
    assert claude_client.extract_text(response) == "ok"


def test_call_claude_stops_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr(claude_client.requests, "post", lambda *args, **kwargs: FakeResponse(status_code=429, text="slow down"))
    monkeypatch.setattr(claude_client.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="429"):
        claude_client.call_claude([], 10)


def test_call_claude_does_not_retry_a_wrong_key(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_client.requests, "post",
                        lambda *args, **kwargs: (calls.append(1), FakeResponse(status_code=401, text="bad key"))[1])
    with pytest.raises(RuntimeError, match="401"):
        claude_client.call_claude([], 10)
    assert len(calls) == 1


# ---------------------------------------------------------------- extract_text / extract_tool_call
def test_extract_text_joins_text_blocks():
    response = {"content": [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}]}
    assert claude_client.extract_text(response) == "Hello world"


def test_extract_text_is_empty_when_the_model_only_used_a_tool():
    response = {"content": [{"type": "tool_use", "name": "search_policy", "input": {}}]}
    assert claude_client.extract_text(response) == ""


def test_extract_tool_call_returns_name_and_input():
    response = {"content": [{"type": "tool_use", "name": "apply_leave", "input": {"leave_type": "casual"}}]}
    tool_call = claude_client.extract_tool_call(response)
    assert tool_call == {"name": "apply_leave", "input": {"leave_type": "casual"}}


def test_extract_tool_call_is_none_for_a_plain_text_reply():
    response = text_response("just an answer")
    assert claude_client.extract_tool_call(response) is None


def test_extract_tool_call_defaults_input_to_an_empty_dict_when_missing():
    response = {"content": [{"type": "tool_use", "name": "list_pending_approvals"}]}
    assert claude_client.extract_tool_call(response) == {"name": "list_pending_approvals", "input": {}}
