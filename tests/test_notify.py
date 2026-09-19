"""The injected notifier: Telegram when it is there, a printed line when not.

Sends straight to the Bot API through ``aicp.telegram_notify`` — no
``~/.claude`` checkout, no shell script, no subprocess. Credentials
(``TG_BOT_TOKEN``/``TG_CHAT_ID``) come from the environment ONLY — never from
``.aicprc``, a checked-in, shareable file — and no failure may ever reach the
caller: the notifier is wired into the runner as a plain ``NotifyFn`` and a
raised exception there would take down a run that had already succeeded.

``pinned_environment`` (autouse, conftest.py) clears both variables for every
test in this suite, so nothing here can fire a real Telegram message using
whatever bot token happens to be exported on the machine running pytest.
"""

from __future__ import annotations

from aicp import telegram_notify as telegram_notify_module
from aicp.notify import notify

# ── the send path ────────────────────────────────────────────────────────────


def test_sends_through_the_bot_api_when_credentials_are_present(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    calls = []
    monkeypatch.setattr(
        telegram_notify_module,
        "send_telegram",
        lambda token, chat_id, text, **kw: calls.append((token, chat_id, text)) or True,
    )

    notify("hello from aicp")

    assert calls == [("tok", "42", "hello from aicp")]
    assert "telegram unavailable" not in capsys.readouterr().out


def test_a_failed_send_degrades_to_the_printed_line(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    monkeypatch.setattr(telegram_notify_module, "send_telegram", lambda *a, **kw: False)

    notify("hello")

    assert "telegram unavailable" in capsys.readouterr().out


def test_credentials_are_read_fresh_each_call(monkeypatch) -> None:
    """Never cached at import — a run that exports the token mid-process
    would otherwise be ignored."""
    seen = []
    monkeypatch.setattr(
        telegram_notify_module,
        "send_telegram",
        lambda token, chat_id, text, **kw: seen.append(token) or True,
    )

    monkeypatch.setenv("TG_BOT_TOKEN", "first")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    notify("one")
    monkeypatch.setenv("TG_BOT_TOKEN", "second")
    notify("two")

    assert seen == ["first", "second"]


# ── the fallback path ────────────────────────────────────────────────────────


def test_missing_credentials_degrade_to_a_printed_line_without_any_network_call(
    monkeypatch, capsys
) -> None:
    """No TG_BOT_TOKEN/TG_CHAT_ID (the pinned_environment default): the real
    ``telegram_notify.send_telegram`` short-circuits on missing credentials,
    so this never even attempts a connection."""

    def _boom(*a, **kw):
        raise AssertionError("must not attempt a send with no credentials")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    notify("hello")


def test_missing_credentials_print_the_fallback_line(capsys) -> None:
    notify("hello")

    out = capsys.readouterr().out
    assert "telegram unavailable" in out
    assert "hello" not in out, "the fallback announces the failure, not the payload"


def test_one_missing_credential_also_degrades(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    # TG_CHAT_ID left unset by pinned_environment.

    notify("hello")

    assert "telegram unavailable" in capsys.readouterr().out


# ── contract compatibility ───────────────────────────────────────────────────


def test_notify_is_notifyfn_compatible(capsys) -> None:
    """``NotifyFn = Callable[[str], None]``: one positional string, no
    meaningful return, no exception — whatever the environment looks like."""
    from aicp.contracts import NotifyFn

    fn: NotifyFn = notify
    assert fn("a message") is None
    capsys.readouterr()
