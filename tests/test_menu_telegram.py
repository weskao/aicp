"""The Telegram bot token / chat ID rows of ``--config``.

Rows, not subcommands — same reasoning as Skills/Agents/Doctor/Export/Import
(``test_menu_skills.py``/``test_menu_settings_io.py``), whose ``menu``
fixture and ``_row_number`` helper this file mirrors. The token lives in
``aicp.telegram_store`` (the OS credential store) and never in
``~/.aicp/config.json``; the chat id is ordinary configuration and lives in
that file — the split codex-reset-watch makes. conftest's autouse
``_isolate_real_credential_store`` already backs that store with an
in-memory fake for every test in the whole suite; this file only adds what
is specific to these two rows.

Mostly driven through the NUMBERED fallback, same as every other row here —
also the CI surface, and the one with no controlling terminal to worry about.
The arrow-key TUI's inline editor is driven directly at the end, with
``read_edit_key`` fed from a list.

Local fixtures only — ``tests/conftest.py`` is never edited from here.
"""

from __future__ import annotations

import io
import json

import pytest
import telegram_kit

from aicp import menu as menu_mod
from aicp import telegram_store
from aicp.config import resolve
from aicp.menu import ROWS, MenuState, config_menu


def _row_number(msgid: str) -> int:
    return next(i for i, row in enumerate(ROWS, start=1) if row.label[0] == msgid)


TOKEN_ROW = _row_number("config_telegram_token")
CHAT_ID_ROW = _row_number("config_telegram_chat_id")


@pytest.fixture(autouse=True)
def _no_real_tty(monkeypatch):
    """``telegram_kit.read_hidden`` opens ``/dev/tty`` directly, bypassing
    the ``StringIO`` stdin every test here feeds — harmless on a real CI
    runner (no controlling tty at all, so it degrades to ``None``
    immediately and falls through to ``read_line``) but a genuine hang risk
    on a developer's own machine running this suite from an interactive
    shell, where ``/dev/tty`` really is reachable and getpass really would
    wait for a human who was never going to type anything. Pin every test
    here to the CI behaviour unconditionally.
    """
    monkeypatch.setattr(telegram_kit, "read_hidden", lambda *_a, **_kw: None)


@pytest.fixture
def no_credential_store(monkeypatch):
    """Simulate a machine with no keychain/libsecret/DPAPI at all.

    Patches the shared, process-wide ``telegram_kit.backend`` — memoized
    with ``functools.lru_cache``, so patching the underlying probe
    (``_detect_backend``) alone would leak whichever answer was first
    computed into every later test. codex-reset-watch's own test suite
    patches the same function for the same reason.
    """
    monkeypatch.setattr(telegram_kit, "backend", lambda: None)


@pytest.fixture
def menu(tmp_path, monkeypatch):
    """Factory: run the menu against a throwaway config with *keys* typed at
    its prompt. Returns ``(exit_code, output, config_path)``."""

    def _run(keys: str):
        cfg = tmp_path / "menu.aicprc"
        out = io.StringIO()
        code = config_menu(stdin=io.StringIO(keys), stdout=out)
        return code, out.getvalue(), cfg

    monkeypatch.setenv("AICP_CONFIG", str(tmp_path / "menu.aicprc"))
    return _run


def _stored_chat_id(cfg) -> str | None:
    return json.loads(cfg.read_text()).get("aicp_telegram_chat_id") if cfg.exists() else None


def _row_line(out: str, row_text: str) -> str:
    return next(ln for ln in out.splitlines() if row_text in ln)


# ── the rows exist; only the chat id reaches config.json ────────────────────


def test_both_rows_are_in_the_row_model(menu):
    _, out, _ = menu("q\n")
    assert f"{TOKEN_ROW}) Telegram bot token" in out
    assert f"{CHAT_ID_ROW}) Telegram chat ID" in out


def test_the_token_row_never_writes_config_json(menu):
    _, _, cfg = menu(f"{TOKEN_ROW}\n12345:abc\nq\n")
    assert not cfg.exists()


def test_the_chat_id_goes_to_config_json_not_the_credential_store(menu):
    _, _, cfg = menu(f"{CHAT_ID_ROW}\n42\nq\n")
    assert _stored_chat_id(cfg) == "42"
    assert telegram_store.get("telegram_chat_id") == ""


def test_eof_mid_prompt_exits_cleanly_rather_than_crashing(menu):
    code, _, _ = menu(f"{TOKEN_ROW}\n")  # stream ends inside the prompt
    assert code == 0


# ── value column: the states a row can be in ──────────────────────────────────


def test_an_unset_token_with_no_env_reads_not_set(menu):
    _, out, _ = menu("q\n")
    assert "not set" in _row_line(out, "Telegram bot token")


def test_a_stored_token_is_masked_never_shown_in_full(menu):
    telegram_store.set(telegram_store.TOKEN_KEY, "12345:s3cretvalue")
    _, out, _ = menu("q\n")
    assert "s3cretvalue" not in out
    assert telegram_kit.mask_secret("12345:s3cretvalue") in _row_line(out, "Telegram bot token")


def test_falling_back_to_the_env_var_says_so_without_printing_it(menu, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "from-env-value")
    _, out, _ = menu("q\n")
    line = _row_line(out, "Telegram bot token")
    assert "TG_BOT_TOKEN" in line
    assert "from-env-value" not in out


def test_a_stored_value_wins_over_the_env_var_in_the_status_line(menu, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "stale-env-token")
    telegram_store.set(telegram_store.TOKEN_KEY, "12345:configured")
    _, out, _ = menu("q\n")
    line = _row_line(out, "Telegram bot token")
    assert telegram_kit.mask_secret("12345:configured") in line
    assert "TG_BOT_TOKEN" not in line


def test_no_credential_store_warns_and_names_the_env_var(menu, no_credential_store):
    _, out, _ = menu("q\n")
    assert "TG_BOT_TOKEN" in _row_line(out, "Telegram bot token")


def test_the_chat_id_row_shows_the_plain_value_unmasked(menu, tmp_path):
    (tmp_path / "menu.aicprc").write_text('{"aicp_telegram_chat_id": "918273645"}')
    _, out, _ = menu("q\n")
    assert "918273645" in _row_line(out, "Telegram chat ID")


# ── setting, keeping unchanged, and clearing ──────────────────────────────────


def test_typing_a_value_stores_it(menu):
    menu(f"{TOKEN_ROW}\n12345:newtoken\nq\n")
    assert telegram_store.get(telegram_store.TOKEN_KEY) == "12345:newtoken"


def test_setting_the_chat_id_stores_it_too(menu):
    _, _, cfg = menu(f"{CHAT_ID_ROW}\n-100555\nq\n")
    assert _stored_chat_id(cfg) == "-100555"


def test_a_chat_id_that_is_not_a_number_or_channel_is_rejected(menu):
    _, out, cfg = menu(f"{CHAT_ID_ROW}\nSDA`\nq\n")
    assert _stored_chat_id(cfg) is None
    assert "a chat ID is a number" in out


def test_a_dash_clears_the_chat_id(menu):
    _, _, cfg = menu(f"{CHAT_ID_ROW}\n42\n{CHAT_ID_ROW}\n-\nq\n")
    assert _stored_chat_id(cfg) == ""


def test_a_blank_answer_keeps_the_existing_value_unchanged(menu):
    """No bare-Enter clear path — same principle ai-accounts' own masked
    fields follow: an unrelated Enter must never wipe a real credential."""
    telegram_store.set(telegram_store.TOKEN_KEY, "keep-me")
    menu(f"{TOKEN_ROW}\n\nq\n")
    assert telegram_store.get(telegram_store.TOKEN_KEY) == "keep-me"


def test_a_dash_clears_the_stored_value(menu):
    telegram_store.set(telegram_store.TOKEN_KEY, "to-be-cleared")
    _, out, _ = menu(f"{TOKEN_ROW}\n-\nq\n")
    assert telegram_store.get(telegram_store.TOKEN_KEY) == ""
    assert "cleared" in out


def test_a_successful_save_is_reported(menu):
    _, out, _ = menu(f"{TOKEN_ROW}\n12345:abc\nq\n")
    assert "saved" in out


def test_saving_with_no_credential_store_fails_and_names_the_env_var(menu, no_credential_store):
    _, out, _ = menu(f"{TOKEN_ROW}\n12345:abc\nq\n")
    assert telegram_store.get(telegram_store.TOKEN_KEY) == ""
    assert "TG_BOT_TOKEN" in out


def test_the_cached_status_refreshes_after_a_save_in_the_same_session(menu):
    """Same reasoning as Skills/Agents/Doctor's own cached probes: a value
    just saved must not still read "not set" on the very next repaint."""
    _, out, _ = menu(f"{TOKEN_ROW}\n12345:fresh\n{TOKEN_ROW}\n\nq\n")
    prompts = [ln for ln in out.splitlines() if ln.startswith("Bot token")]
    assert len(prompts) == 2
    assert "not set" in prompts[0]
    assert telegram_kit.mask_secret("12345:fresh") in prompts[1]


# ── the arrow-key TUI edits in the row itself (crw --config style) ───────────


@pytest.fixture
def inline(monkeypatch, tmp_path):
    """Factory: open the inline editor on *row_number* with *keys* typed.
    Returns ``(state, output)``."""

    def _run(row_number: int, keys: list[str]):
        monkeypatch.setenv("AICP_CONFIG", str(tmp_path / "menu.aicprc"))
        state = MenuState.from_settings(resolve())
        feed = iter(keys)
        monkeypatch.setattr(menu_mod, "read_edit_key", lambda _stdin: next(feed))
        out = io.StringIO()
        lines = menu_mod._panel(state, row_number, out)
        menu_mod._edit_inline(state, ROWS[row_number - 1], row_number, io.StringIO(), out, lines)
        return state, out.getvalue()

    return _run


def test_typing_a_chat_id_inline_shows_it_and_saves_it(inline, tmp_path):
    state, out = inline(CHAT_ID_ROW, [*"42", "enter"])
    assert "42▏" in out
    assert state.telegram_chat_id == "42"
    assert _stored_chat_id(tmp_path / "menu.aicprc") == "42"


def test_a_token_typed_inline_shows_as_bullets_only(inline):
    _, out = inline(TOKEN_ROW, [*"12:ab", "enter"])
    assert "•••••▏" in out
    assert "12:ab" not in out
    assert telegram_store.get(telegram_store.TOKEN_KEY) == "12:ab"


def test_enter_on_an_empty_token_field_keeps_the_stored_token(inline):
    telegram_store.set(telegram_store.TOKEN_KEY, "12:keep")
    inline(TOKEN_ROW, ["enter"])
    assert telegram_store.get(telegram_store.TOKEN_KEY) == "12:keep"


def test_dash_enter_clears_the_token_inline(inline):
    telegram_store.set(telegram_store.TOKEN_KEY, "12:gone")
    inline(TOKEN_ROW, ["-", "enter"])
    assert telegram_store.get(telegram_store.TOKEN_KEY) == ""


def test_escape_discards_what_was_typed(inline, tmp_path):
    state, _ = inline(CHAT_ID_ROW, [*"99", "escape"])
    assert state.telegram_chat_id == ""
    assert not (tmp_path / "menu.aicprc").exists()


def test_a_rejected_chat_id_keeps_the_field_open_until_fixed(inline):
    state, out = inline(CHAT_ID_ROW, [*"ab", "enter", "backspace", "backspace", *"7", "enter"])
    assert "a chat ID is a number" in out
    assert state.telegram_chat_id == "7"


def test_emptying_a_prefilled_chat_id_clears_it(inline, tmp_path):
    (tmp_path / "menu.aicprc").write_text('{"aicp_telegram_chat_id": "42"}')
    state, _ = inline(CHAT_ID_ROW, ["backspace", "backspace", "enter"])
    assert state.telegram_chat_id == ""
    assert _stored_chat_id(tmp_path / "menu.aicprc") == ""
