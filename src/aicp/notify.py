"""The injected notifier — Telegram when it is there, a printed line when not.

Sends straight to the Telegram Bot API via :mod:`telegram_kit` (stdlib HTTP,
zero project dependencies) — no ``~/.claude`` checkout, no shell script, no
subprocess. aicp keeps working for anyone who only has this repo; without
credentials it just prints the line instead of sending it.

:func:`notify` is the real implementation behind ``contracts.NotifyFn`` — it
is passed INTO the runner at the CLI entry point, never imported by it, so
the runner stays free of any network dependency. Nothing raises out of here:
a notification is the last step of a run that has usually already succeeded,
so missing credentials, a network failure or a bad response all degrade to
the printed line.

**Credentials are resolved per call, configured over environment.** Both are
read fresh from :mod:`aicp.telegram_store` (the OS credential store — see its
module docstring) — ``aicp --config``'s two Telegram rows write there — and
:func:`telegram_kit.resolve_credentials` falls back to ``TG_BOT_TOKEN``/
``TG_CHAT_ID`` for either one that is not configured, the same names
``~/.claude/scripts/tg-send.sh`` already used, so an existing Telegram setup
still carries over with no reconfiguration. Configured wins over env on
purpose: a stale ``TG_BOT_TOKEN`` in a shell profile must not keep notifying
through a bot the user already replaced in ``--config``. Neither value is
ever read from ``.aicprc``: a config file is a checked-in, shareable
artifact, and a bot token in it would be a leaked secret the moment that
file is synced or committed.
"""

from __future__ import annotations

import telegram_kit

from ._utils import DIM, RESET
from .i18n import t
from .telegram_store import CHAT_ID_KEY, TOKEN_KEY
from .telegram_store import get as _get_secret

__all__ = ["notify"]


def _send(message: str) -> bool:
    token, chat_id = telegram_kit.resolve_credentials(_get_secret(TOKEN_KEY), _get_secret(CHAT_ID_KEY))
    return telegram_kit.send_message(token, chat_id, message)


def notify(message: str) -> None:
    """Send *message*, or say out loud that it could not be sent.

    The NotifyFn the CLI entry point wires into the runner. Never raises, and
    never prints the message itself on the failure path — only that the send
    did not happen.
    """
    if _send(message):
        return
    print(f"{DIM}" + t("telegram_unavailable", "(telegram unavailable — message not sent)") + RESET)
