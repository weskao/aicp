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

**Credentials come from the environment only**, resolved per call:
``TG_BOT_TOKEN`` and ``TG_CHAT_ID`` — the same names
``~/.claude/scripts/tg-send.sh`` already used, so an existing Telegram setup
carries over with no reconfiguration. Neither is ever read from ``.aicprc``:
a config file is a checked-in, shareable artifact, and a bot token in it
would be a leaked secret the moment that file is synced or committed.
"""

from __future__ import annotations

import telegram_kit

from ._utils import DIM, RESET
from .i18n import t

__all__ = ["notify"]


def _send(message: str) -> bool:
    token, chat_id = telegram_kit.resolve_credentials("", "")
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
