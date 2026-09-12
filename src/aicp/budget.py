"""Per-CLI timeout budget: a floor that grows with the diff, widened by history.

Port of ``_aicp_budget``/``_aicp_history_budget`` in ``~/scripts/bin/aicp``.

    floor     AICP_TIMEOUT_BASE      covers cold start plus a small prompt
    growth    AICP_TIMEOUT_PER_FILE  per changed or untracked file
              AICP_TIMEOUT_PER_100L  per 100 changed lines in tracked files
    ceiling   AICP_TIMEOUT_MAX       so a real hang is still caught
    override  AICP_STEP_TIMEOUT      pins the budget, skipping formula AND history

The budget is computed per CLI, inside the fallback loop rather than once above
it, so a fast CLI's own history never inflates a slow CLI's budget. History may
only WIDEN the budget above the formula, never shrink it below — and the
widened value is deliberately NOT re-capped at ``AICP_TIMEOUT_MAX``: that
ceiling exists to bound the no-history default, while a recorded successful run
is direct evidence this CLI legitimately needs that long.

Every value is validated as a plain non-negative integer before any arithmetic.
A malformed value falls back to :data:`FLOOR` with a stated note instead of
crashing: a bad value in a config file would otherwise brick aicp in every repo
on the machine, not just the one invocation that set it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aicp import timing
from aicp.i18n import t

__all__ = ["FLOOR", "Budget", "compute"]

FLOOR = 180  # the zsh original's own documented AICP_TIMEOUT_BASE default

_DEFAULTS = {
    "AICP_TIMEOUT_BASE": 180,
    "AICP_TIMEOUT_PER_FILE": 15,
    "AICP_TIMEOUT_PER_100L": 5,
    "AICP_TIMEOUT_MAX": 1800,
}

_INT = re.compile(r"^[0-9]+$")  # zsh's <->; str.isdigit() would accept "²"


@dataclass(frozen=True)
class Budget:
    """Seconds allowed for one CLI invocation, plus the note explaining it."""

    seconds: int
    note: str


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return _DEFAULTS[name]
    return int(raw) if _INT.match(raw) else None


def _git_count(args: list[str], cwd: Path | None) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _pending(cwd: Path | None) -> tuple[int, int]:
    """``(changed files, changed lines)`` per git, or ``(0, 0)`` if it can't say."""
    files = len(_git_count(["status", "--porcelain"], cwd).splitlines())
    lines = 0
    for row in _git_count(["diff", "HEAD", "--numstat"], cwd).splitlines():
        added, _, rest = row.partition("\t")
        deleted = rest.partition("\t")[0]
        # Binary files report "-" for both counts; awk read those as 0 too.
        lines += sum(int(n) for n in (added, deleted) if _INT.match(n))
    return files, lines


def _history(cli: str) -> int:
    """This CLI's largest successful run, multiplied out — 0 if there is none."""
    raw_lines = os.environ.get("AICP_TIMEOUT_HISTORY_LINES", "500")
    lines = int(raw_lines) if _INT.match(raw_lines) else 0
    try:
        mult = float(os.environ.get("AICP_TIMEOUT_HISTORY_MULT", "1.3"))
    except ValueError:
        return 0  # unusable multiplier: no widening, same as no history at all
    best = timing.max_ok_seconds(cli, lines=lines)
    # `(max * mult) + 0.999999` truncated — the zsh/awk expression, i.e. ceil.
    return int(best * mult + 0.999999) if best > 0 else 0


def compute(cli: str, *, cwd: Path | None = None) -> Budget:
    """The seconds *cli* gets for one step, and the note shown beside it."""
    pinned = os.environ.get("AICP_STEP_TIMEOUT")
    if pinned:
        if _INT.match(pinned):
            return Budget(int(pinned), t("budget_note_fixed", "fixed"))
        return Budget(
            FLOOR, t("budget_note_bad_step", "bad AICP_STEP_TIMEOUT — using floor")
        )

    knobs = {name: _int_env(name) for name in _DEFAULTS}
    if any(value is None for value in knobs.values()):
        return Budget(
            FLOOR, t("budget_note_bad_env", "bad AICP_TIMEOUT_* value — using floor")
        )

    files, lines = _pending(cwd)
    formula = (
        knobs["AICP_TIMEOUT_BASE"]
        + knobs["AICP_TIMEOUT_PER_FILE"] * files
        + knobs["AICP_TIMEOUT_PER_100L"] * lines // 100
    )
    formula = min(formula, knobs["AICP_TIMEOUT_MAX"])

    history = _history(cli)
    if history > formula:
        return Budget(
            history,
            t(
                "budget_note_history",
                "%s files · %s lines · history %ss",
                files,
                lines,
                history,
            ),
        )
    return Budget(formula, t("budget_note_plain", "%s files · %s lines", files, lines))
