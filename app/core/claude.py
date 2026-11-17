"""
core/claude.py - the one shared Claude (Anthropic) API client for every AI call in this app.

Replaces Groq (2026-11-17). Everything that used to split work across a bigger "answer" model
and a smaller/faster "rewrite" model now runs on a single model, Claude Haiku 5.5 - it was
already the fast/cheap tier, so there is no more tiering to do.

Two callers use this:
    - rag/answer/llm.py       - plain text answers (the rewrite step and the policy answer step).
    - rag/answer/assistant.py - the chat router's tool-calling (picks one tool per message).

Both send messages and get back the raw Anthropic Messages API response; extract_text() and
extract_tool_call() below pull out what each caller actually needs from it.
"""
import logging
import time

import requests

from app.core.config import get_required_setting, get_setting

logger = logging.getLogger(__name__)

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# A model name can be changed in .env without touching the code. Defaults to Claude Haiku 5.5 -
# the one model this app uses for everything now (router, rewrite and answer alike).
CLAUDE_MODEL_NAME = get_setting("CLAUDE_MODEL") or "claude-haiku-5-5"

REQUEST_TIMEOUT_SECONDS = 60
MAXIMUM_ATTEMPTS = 4
SECONDS_TO_WAIT_BEFORE_RETRY = 10
# 429 = rate limit, 5xx = Anthropic's own server trouble, 529 = Anthropic's "overloaded" status.
RETRY_STATUS_CODES = (429, 500, 502, 503, 529)


def call_claude(messages, max_tokens, temperature=0.0, system=None, tools=None):
    """
    Sends one request to Claude's Messages API and returns the raw response JSON. The caller
    reads whichever of extract_text()/extract_tool_call() below it needs.

    messages: [{"role": "user"/"assistant", "content": "..."}] - a system prompt is its own
    "system" argument here, never a message with role "system" (unlike the old Groq/OpenAI-style
    call, Claude's API does not accept a system message inside the messages list).

    temperature is accepted for compatibility with every call site (all of which still pass
    temperature=0.0, wanting the most consistent answers) but is NOT sent to Claude: Haiku 5.5
    rejects it outright with a 400 ("temperature is deprecated for this model") - found when
    wiring this up against the real API, not documented anywhere obvious beforehand.
    """
    headers = {
        "x-api-key": get_required_setting("ANTHROPIC_API_KEY"),
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    request_body = {"model": CLAUDE_MODEL_NAME, "messages": messages, "max_tokens": max_tokens}
    if system:
        request_body["system"] = system
    if tools:
        request_body["tools"] = tools

    for attempt_number in range(1, MAXIMUM_ATTEMPTS + 1):
        response = requests.post(CLAUDE_API_URL, headers=headers, json=request_body, timeout=REQUEST_TIMEOUT_SECONDS)

        if response.status_code in RETRY_STATUS_CODES and attempt_number < MAXIMUM_ATTEMPTS:
            logger.warning("Claude is busy (status %s), waiting %ds (attempt %d of %d)",
                           response.status_code, SECONDS_TO_WAIT_BEFORE_RETRY, attempt_number, MAXIMUM_ATTEMPTS)
            time.sleep(SECONDS_TO_WAIT_BEFORE_RETRY)
            continue

        if not response.ok:
            raise RuntimeError(f"Claude request failed ({response.status_code}): {response.text[:500]}")
        return response.json()


def extract_text(response_json):
    """Every text block of a reply, joined and stripped. Empty string if the model used only a tool."""
    return "".join(block["text"] for block in response_json.get("content", []) if block.get("type") == "text").strip()


def extract_tool_call(response_json):
    """The first tool_use block of a reply, as {"name": ..., "input": {...}}, or None if the
    model answered in plain text instead of picking a tool. Claude's "input" is already a parsed
    dict (unlike Groq/OpenAI's tool-call arguments, which came back as a JSON string to decode)."""
    for block in response_json.get("content", []):
        if block.get("type") == "tool_use":
            return {"name": block["name"], "input": block.get("input") or {}}
    return None
