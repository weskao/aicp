"""Cross-run quota cooldown: ``{"<cli>": <expiry epoch>}`` in ``~/.aicp/quota.json``.

:attr:`aicp.runner.StepResult.quota_clis` excludes an exhausted CLI for the rest
of ONE run. That is too short: a token/rate-limit wall usually stands for hours,
so every later run walked into it again and paid a full budget per step for a CLI
that could not succeed. This module is the persistent half — a CLI that reported
a supported quota signal is skipped until its window expires.

Deliberately its OWN file rather than a key in ``state.json``:
:func:`aicp.skills._save_state` rewrites that file as ``{"version", "skills"}``
and would drop anything else at the top level, and :mod:`aicp.runner` imports no
sibling feature module (see its docstring), which rules out reaching for the
skills installer's reader. A peer of ``timing.log`` in aicp's own directory keeps
both properties.

The window is ``AICP_QUOTA_COOLDOWN`` seconds. ``0`` switches the feature off
entirely — the escape hatch for a CLI wrongly sidelined — and any value that is
not a plain integer in ``(0, _MAX_COOLDOWN]`` falls back to :data:`DEFAULT_COOLDOWN`,
which is :mod:`aicp.budget`'s posture: a bad config value never bricks aicp.

Nothing here is ever allowed to break a run. A missing, corrupt or unwritable
file reads as "no cooldowns", so the worst a failure can cost is one wasted CLI
invocation — never a changed exit code, and never a blocked commit.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

__all__ = ["DEFAULT_COOLDOWN", "cooldown_seconds", "cooling", "quota_path", "record"]

#: One hour. Short on purpose: over-skipping sidelines a CLI that has already
#: recovered, while under-skipping costs a single wasted invocation, so the
#: asymmetry favors erring short and self-healing fast.
DEFAULT_COOLDOWN = 3600

#: A week. A cooldown longer than any real quota window is almost certainly a
#: typo, and accepting it would sideline a CLI for good with nothing on screen
#: to explain why.
_MAX_COOLDOWN = 7 * 24 * 3600


def quota_path() -> Path:
    """``~/.aicp/quota.json`` — beside ``timing.log``, never in a CLI's own dir."""
    return Path.home() / ".aicp" / "quota.json"


def cooldown_seconds() -> int:
    """The configured window: ``0`` (disabled), else 1..:data:`_MAX_COOLDOWN`."""
    raw = os.environ.get("AICP_QUOTA_COOLDOWN")
    if raw is None:
        return DEFAULT_COOLDOWN
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_COOLDOWN
    if value == 0:
        return 0
    return value if 0 < value <= _MAX_COOLDOWN else DEFAULT_COOLDOWN


def _expiry(value: object) -> float | None:
    """*value* as an epoch, or None if the file holds something else there.

    ``isinstance(True, int)`` is true in Python, so a JSON ``true`` would other-
    wise read as the epoch 1 and count as a (long-expired) entry.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _load(path: Path) -> dict[str, object]:
    """Every recorded cooldown. Missing/corrupt/unreadable all mean "none"."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def cooling(cli: str, *, path: Path | None = None, now: float | None = None) -> int:
    """Seconds left on *cli*'s cooldown, or 0 if it is free to run."""
    if cooldown_seconds() <= 0:
        return 0
    expiry = _expiry(_load(path or quota_path()).get(cli))
    if expiry is None:
        return 0
    return max(0, int(expiry - (time.time() if now is None else now)))


def record(cli: str, *, path: Path | None = None, now: float | None = None) -> None:
    """Start *cli*'s cooldown, pruning expired entries. Never raises."""
    window = cooldown_seconds()
    if window <= 0:
        return
    stamp = time.time() if now is None else now
    target = path or quota_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        live = {
            name: expiry
            for name, value in _load(target).items()
            if (expiry := _expiry(value)) is not None and expiry > stamp
        }
        live[cli] = int(stamp + window)
        # A plain write, not the atomic replace state.json needs: a torn file
        # already reads as "no cooldowns" via _load, so the guard that makes a
        # corrupt file harmless makes atomicity buy nothing on top.
        target.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return
