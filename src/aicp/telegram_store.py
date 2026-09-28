"""aicp's own credential store: :class:`telegram_kit.CredentialStore` under
the ``aicp`` service.

The mechanism — Keychain / Secret Service / DPAPI, refuse rather than fall
back to plaintext or reversible obfuscation when none is available, the
secret on stdin only, never in an argument vector — is telegram_kit's own
documented and tested contract; this module exists so the rest of aicp (the
``--config`` menu, :mod:`aicp.notify`) and its tests call and patch one small,
local surface rather than ``telegram_kit`` directly. Neither key this module
stores ever reaches ``~/.aicp/config.json`` — see that file's own module
docstring for why a Telegram credential is never accepted there at all.
"""

from __future__ import annotations

import telegram_kit

#: Keychain service / Secret Service attribute identifying this program's items.
SERVICE = "aicp"

#: Same key telegram_kit itself uses, so a token this app stores is the one
#: ``telegram_kit.notify()`` would also find under this service name.
TOKEN_KEY = telegram_kit.TOKEN_KEY
#: Not secret — telegram_kit's own docs call the chat id "ordinary
#: configuration" — but it lives in this same store rather than
#: ``config.json`` anyway: that file is a checked-in, shareable artifact
#: (see ``aicp.config``'s module docstring), and a personal chat id has no
#: business in something a user might sync or commit.
CHAT_ID_KEY = "telegram_chat_id"

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
