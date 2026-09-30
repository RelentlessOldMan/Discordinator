"""Retry, backoff, and error-mapping tests for the Discord REST client.

No network: an httpx.MockTransport feeds canned responses and time.sleep is
patched out, so the retry logic (network error -> retry, 429 -> backoff) and the
status-code -> DiscordError mapping (401/403/404/5xx) are exercised
deterministically and instantly. Run:  python tests/test_discord_client.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

import discordinator.discord_client as dc  # noqa: E402
from discordinator.discord_client import API_BASE, DiscordClient, DiscordError  # noqa: E402

_passed = 0
_orig_sleep = dc.time.sleep


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  ok: {msg}")


def _client(handler) -> DiscordClient:
    """A DiscordClient whose transport is a mock; sleeps are no-ops so retry
    backoff adds no real delay."""
    dc.time.sleep = lambda *_a, **_k: None
    client = DiscordClient(token="test-token")
    client._client = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler))
    return client


def test_error_mapping() -> None:
    print("HTTP error codes map to clear DiscordError messages:")
    cases = {
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not found",
        500: "500",
    }
    for code, needle in cases.items():
        client = _client(lambda req, c=code: httpx.Response(c, json={}))
        try:
            client.whoami()
            raise AssertionError(f"expected DiscordError for {code}")
        except DiscordError as e:
            check(needle in str(e), f"{code} -> DiscordError containing {needle!r}")
        finally:
            client.close()


def test_success_passthrough() -> None:
    print("a 2xx response is returned and parsed:")
    client = _client(lambda req: httpx.Response(200, json={"id": "1", "username": "bot"}))
    try:
        me = client.whoami()
        check(me == {"id": "1", "username": "bot"}, "whoami returns the parsed JSON body")
    finally:
        client.close()


def test_retry_on_network_error_then_success() -> None:
    print("a transient network error is retried, then succeeds:")
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("boom", request=req)  # first attempt fails
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    try:
        check(client.whoami() == {"ok": True}, "retries after a network error and returns success")
        check(state["n"] == 2, "exactly one retry was needed (2 attempts total)")
    finally:
        client.close()


def test_retry_exhausted_network_error() -> None:
    print("persistent network errors surface as a DiscordError:")
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        raise httpx.ConnectError("down", request=req)

    client = _client(handler)
    try:
        client.whoami()
        raise AssertionError("expected DiscordError after exhausting retries")
    except DiscordError as e:
        check("Network error" in str(e), "exhausted network retries -> 'Network error' DiscordError")
        check(state["n"] == 4, "gave up after 4 attempts (the retry budget)")
    finally:
        client.close()


def test_rate_limit_backoff_then_success() -> None:
    print("a 429 triggers a backoff and retry using retry_after:")
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"done": True})

    client = _client(handler)
    try:
        check(client.whoami() == {"done": True}, "retries after a 429 and returns success")
        check(state["n"] == 2, "one backoff-and-retry was enough")
    finally:
        client.close()


def test_rate_limit_persistent() -> None:
    print("persistent 429s surface as a DiscordError (not an infinite loop):")
    client = _client(lambda req: httpx.Response(429, json={"retry_after": 0.01}))
    try:
        client.whoami()
        raise AssertionError("expected DiscordError after repeated 429s")
    except DiscordError as e:
        check("Rate limited" in str(e), "repeated 429 -> 'Rate limited' DiscordError")
    finally:
        client.close()


def test_send_message_chunks_and_labels() -> None:
    print("send_message splits long text and prefixes the label on every chunk:")
    posts: list[dict] = []

    def handler(req):
        import json as _json
        posts.append(_json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"id": str(len(posts))})

    client = _client(handler)
    try:
        sent = client.send_message("chan", "a" * 2500, label="M")
        check(len(sent) == 2, "2500 chars split into two messages")
        check(all(p["content"].startswith("[M] ") for p in posts),
              "the [M] label prefixes every chunk (so relay self-filtering still works)")
        check(all(len(p["content"]) <= dc.MAX_MESSAGE_LEN for p in posts),
              "each posted chunk (incl. label) stays within the Discord length limit")
    finally:
        client.close()


def main() -> int:
    try:
        test_error_mapping()
        test_success_passthrough()
        test_retry_on_network_error_then_success()
        test_retry_exhausted_network_error()
        test_rate_limit_backoff_then_success()
        test_rate_limit_persistent()
        test_send_message_chunks_and_labels()
    finally:
        dc.time.sleep = _orig_sleep  # restore, don't leak the patch
    print(f"\nALL {_passed} DISCORD-CLIENT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
