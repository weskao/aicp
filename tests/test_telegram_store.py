"""aicp's own Telegram credential store.

A thin wrapper over :class:`telegram_kit.CredentialStore` — the mechanism
(keychain/libsecret/DPAPI, refuse rather than fall back to plaintext, stdin
only) is telegram_kit's own tested contract, not re-tested here. This file
only proves the wrapper's small surface (the service name, and that
get/set/delete forward to whatever backs the store) — conftest's autouse
``_isolate_real_credential_store`` fixture is what makes that backing store
an in-memory fake instead of the developer's real keychain.
"""

from __future__ import annotations

from aicp import telegram_store


def test_the_service_name_is_aicp():
    assert telegram_store.SERVICE == "aicp"


def test_the_two_keys_match_the_ones_menu_and_notify_use():
    assert telegram_store.TOKEN_KEY == "telegram_bot_token"
    assert telegram_store.CHAT_ID_KEY == "telegram_chat_id"


def test_set_then_get_round_trips():
    assert telegram_store.set(telegram_store.TOKEN_KEY, "12345:fake") is True
    assert telegram_store.get(telegram_store.TOKEN_KEY) == "12345:fake"


def test_get_on_an_unset_key_is_an_empty_string():
    assert telegram_store.get("never_set") == ""


def test_an_empty_value_deletes_rather_than_storing_blank():
    telegram_store.set(telegram_store.CHAT_ID_KEY, "42")
    assert telegram_store.set(telegram_store.CHAT_ID_KEY, "") is True
    assert telegram_store.get(telegram_store.CHAT_ID_KEY) == ""


def test_delete_reports_whether_anything_was_removed():
    telegram_store.set(telegram_store.TOKEN_KEY, "x")
    assert telegram_store.delete(telegram_store.TOKEN_KEY) is True
    assert telegram_store.delete(telegram_store.TOKEN_KEY) is False
