"""Direct Telegram Bot API sender: stdlib HTTP only, no project dependency.

Never touches the real network — ``urllib.request.urlopen`` is monkeypatched
in every "success" case below.
"""

from __future__ import annotations

import urllib.error

import pytest

from aicp.telegram_notify import send_telegram


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def test_missing_token_never_attempts_a_send(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: pytest.fail("must not open a connection"),
    )
    assert send_telegram("", "42", "x") is False


def test_missing_chat_id_never_attempts_a_send(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: pytest.fail("must not open a connection"),
    )
    assert send_telegram("tok", "", "x") is False


def test_a_successful_response_returns_true(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: _FakeResponse()
    )
    assert send_telegram("tok", "42", "hello") is True


def test_a_network_failure_returns_false_and_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    assert send_telegram("tok", "42", "hello") is False


def test_demo_self_check_passes() -> None:
    from aicp.telegram_notify import demo

    demo()  # asserts internally; must not raise
