"""The injected notifier — Telegram when it is there, a printed line when not.

Port of ``_aicp_notify`` in ``~/scripts/bin/aicp``. Telegram lives in a
different project (``$HOME/.claude``), so it is strictly optional: without it
the same text is printed instead of being sent, and aicp keeps working for
anyone who only has this repo.

:func:`notify` is the real implementation behind ``contracts.NotifyFn`` — it
is passed INTO the runner at the CLI entry point, never imported by it, so
the runner stays free of any network or subprocess dependency. Two
consequences are deliberate:

* **Nothing raises out of here.** A notification is the last step of a run
  that has usually already succeeded; a missing script, an unreadable path, a
  non-zero exit or a killed child all degrade to the printed line.
* **The script path comes from the environment only**, resolved per call.
  ``AICP_TG_SEND`` is never read from ``.aicprc``: a config file is a
  checked-in, shareable artifact, and a path in it would be an arbitrary
  program this module then executes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ._utils import DIM, RESET, have
from .i18n import t

__all__ = ["notify", "tg_send_script"]


def tg_send_script() -> Path:
    """``$AICP_TG_SEND``, else ``$HOME/.claude/scripts/tg-send.sh``."""
    override = os.environ.get("AICP_TG_SEND")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "scripts" / "tg-send.sh"


def _send(message: str) -> bool:
    script = tg_send_script()
    try:
        if not script.is_file():
            return False
        # tg-send.sh is a bash script; run it under bash the way the zsh
        # original does, falling back to executing it directly where bash is
        # not on PATH (a Windows box without Git Bash).
        cmd = (
            ["bash", str(script), "send", message]
            if have("bash")
            else [str(script), "send", message]
        )
        done = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


def notify(message: str) -> None:
    """Send *message*, or say out loud that it could not be sent.

    The NotifyFn the CLI entry point wires into the runner. Never raises, and
    never prints the message itself on the failure path — only that the send
    did not happen.
    """
    if _send(message):
        return
    print(f"{DIM}" + t("telegram_unavailable", "(telegram unavailable — message not sent)") + RESET)
