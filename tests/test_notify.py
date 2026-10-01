"""The injected notifier: Telegram when it is there, a printed line when not.

Sends straight to the Bot API through ``telegram_kit`` — no
``~/.claude`` checkout, no shell script, no subprocess. Credentials are
resolved fresh per call, configured (``aicp.telegram_store``) over
environment (``TG_BOT_TOKEN``/``TG_CHAT_ID``) — never from ``.aicprc``, a
checked-in, shareable file — and no failure may ever reach the caller: the
notifier is wired into the runner as a plain ``NotifyFn`` and a raised
exception there would take down a run that had already succeeded.

``pinned_environment`` (autouse, conftest.py) clears both env vars, and
``_isolate_real_credential_store`` (autouse, conftest.py) backs the store
with an in-memory fake, for every test in this suite — so nothing here can
fire a real Telegram message using whatever bot token happens to be exported
or stored on the machine running pytest.
"""

from __future__ import annotations

import telegram_kit

from aicp import telegram_store
from aicp.config import TELEGRAM_CHAT_ID_KEY
from aicp.notify import CHAT_ID_ENV, notify


def test_the_chat_id_is_read_under_the_key_the_menu_writes() -> None:
    assert CHAT_ID_ENV == TELEGRAM_CHAT_ID_KEY

# ── the send path ────────────────────────────────────────────────────────────


def test_sends_through_the_bot_api_when_credentials_are_present(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    calls = []
    monkeypatch.setattr(
        telegram_kit,
        "send_message",
        lambda token, chat_id, text, **kw: calls.append((token, chat_id, text)) or True,
    )

    notify("hello from aicp")

    assert calls == [("tok", "42", "hello from aicp")]
    assert "telegram unavailable" not in capsys.readouterr().out


def test_a_failed_send_degrades_to_the_printed_line(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    monkeypatch.setattr(telegram_kit, "send_message", lambda *a, **kw: False)

    notify("hello")

    assert "telegram unavailable" in capsys.readouterr().out


def test_credentials_are_read_fresh_each_call(monkeypatch) -> None:
    """Never cached at import — a run that exports the token mid-process
    would otherwise be ignored."""
    seen = []
    monkeypatch.setattr(
        telegram_kit,
        "send_message",
        lambda token, chat_id, text, **kw: seen.append(token) or True,
    )

    monkeypatch.setenv("TG_BOT_TOKEN", "first")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    notify("one")
    monkeypatch.setenv("TG_BOT_TOKEN", "second")
    notify("two")

    assert seen == ["first", "second"]


def test_a_stored_credential_is_used_over_the_environment(monkeypatch) -> None:
    """Configured wins — a stale TG_BOT_TOKEN in a shell profile must not
    keep notifying through a bot the user already replaced in --config."""
    monkeypatch.setenv("TG_BOT_TOKEN", "stale-env-token")
    monkeypatch.setenv("TG_CHAT_ID", "stale-env-chat")
    telegram_store.set(telegram_store.TOKEN_KEY, "configured-token")
    monkeypatch.setenv(CHAT_ID_ENV, "configured-chat")  # config.json, via cli.export_settings
    calls = []
    monkeypatch.setattr(
        telegram_kit,
        "send_message",
        lambda token, chat_id, text, **kw: calls.append((token, chat_id)) or True,
    )

    notify("hello")

    assert calls == [("configured-token", "configured-chat")]


def test_a_stored_token_falls_back_to_the_env_chat_id_independently(monkeypatch) -> None:
    """Each credential falls back on its own — a stored token with no stored
    chat id still uses TG_CHAT_ID rather than refusing to send at all."""
    monkeypatch.setenv("TG_CHAT_ID", "env-chat")
    telegram_store.set(telegram_store.TOKEN_KEY, "configured-token")
    calls = []
    monkeypatch.setattr(
        telegram_kit,
        "send_message",
        lambda token, chat_id, text, **kw: calls.append((token, chat_id)) or True,
    )

    notify("hello")

    assert calls == [("configured-token", "env-chat")]


# ── the fallback path ────────────────────────────────────────────────────────


def test_missing_credentials_degrade_to_a_printed_line_without_any_network_call(
    monkeypatch, capsys
) -> None:
    """No TG_BOT_TOKEN/TG_CHAT_ID (the pinned_environment default): the real
    ``telegram_kit.send_message`` short-circuits on missing credentials,
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


def test_rich_message_is_tried_before_plain(monkeypatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    sent = []
    monkeypatch.setattr("aicp.notify._send_rich", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(telegram_kit, "send_message", lambda *a, **kw: sent.append("plain") or True)

    notify("| a | b |")

    assert sent == [("tok", "42", "| a | b |")]
