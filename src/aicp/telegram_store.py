"""aicp's own credential store: :class:`telegram_kit.CredentialStore` under
the ``aicp`` service.

The mechanism — Keychain / Secret Service / DPAPI, refuse rather than fall
back to plaintext or reversible obfuscation when none is available, the
secret on stdin only, never in an argument vector — is telegram_kit's own
documented and tested contract; this module exists so the rest of aicp (the
``--config`` menu, :mod:`aicp.notify`) and its tests call and patch one small,
local surface rather than ``telegram_kit`` directly. Only the bot token is
stored here; the chat id is ordinary configuration and lives in
``~/.aicp/config.json`` (:data:`aicp.config.TELEGRAM_CHAT_ID_KEY`).
"""

from __future__ import annotations

import telegram_kit

#: Keychain service / Secret Service attribute identifying this program's items.
SERVICE = "aicp"

#: Same key telegram_kit itself uses, so a token this app stores is the one
#: ``telegram_kit.notify()`` would also find under this service name.
TOKEN_KEY = telegram_kit.TOKEN_KEY

_store = telegram_kit.CredentialStore(SERVICE)


def available() -> bool:
    return telegram_kit.available()


def backend_label() -> str:
    return telegram_kit.backend_label()


def get(key: str) -> str:
    return _store.get(key)


def set(key: str, value: str) -> bool:
    return _store.set(key, value)


def delete(key: str) -> bool:
    return _store.delete(key)
