"""Timing log: a 6-field TSV of how long each CLI took, and why it stopped.

Port of ``_aicp_timing_log``/``_aicp_timing_rotate`` in ``~/scripts/bin/aicp``.

Schema (TAB-separated, one row per CLI invocation)::

    <utc-iso8601-Z>  <cli>  <step>  <elapsed-seconds>  <outcome>  <rc>

``outcome`` is one of ``ok`` / ``timeout`` / ``quota`` / ``fail`` / ``abort``. The
distinction is load-bearing, not decorative: a timed-out run's "elapsed" is
just whatever the budget happened to be, so feeding it back into the budget
formula would widen the budget, which produces an even bigger recorded
timeout, forever. :func:`max_ok_seconds` is therefore the ONLY read path, and
it filters to ``ok`` — see :mod:`aicp.budget`.

The delimiter is a literal TAB, not ``|`` (which is an ERE alternation and
already bit the zsh original's secret scanner once).

Nothing here is ever allowed to break a run: an unwritable ``~/.aicp``, a full
disk, or any other failure is swallowed, so a logging problem can never change
aicp's exit code or abort a commit/push that would otherwise have worked.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["KEEP", "MAX_BYTES", "append", "log_path", "max_ok_seconds", "rotate"]

MAX_BYTES = 5 * 1024 * 1024
KEEP = 5

# Bounds how much of the log a history read touches, regardless of how large it
# grew — the zsh original uses `tail -n`; this is the same idea with a byte
# window (500 rows of this schema are well under 100KB).
_TAIL_WINDOW = 256 * 1024


def log_path() -> Path:
    """``$AICP_TIMING_LOG`` if set, else ``~/.aicp/timing.log``."""
    override = os.environ.get("AICP_TIMING_LOG")
    return Path(override) if override else Path.home() / ".aicp" / "timing.log"


def rotate(path: Path) -> None:
    """Shift ``log`` -> ``log.1`` -> ... -> ``log.5`` once *path* exceeds 5MB.

    Copy of the house ``rotate_log_if_needed`` pattern (ai-accounts-refresh.sh,
    git-repo-sync.sh) rather than a shared helper: aicp's stated posture is
    zero cross-project dependencies. Whatever sat in ``.5`` is dropped, so a
    ``.6`` is never created.
    """
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
    except OSError:
        return
    for i in range(KEEP, 0, -1):
        numbered = path.with_name(f"{path.name}.{i}")
        if not numbered.exists():
            continue
        if i == KEEP:
            numbered.unlink()
        else:
            os.replace(numbered, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def append(
    cli: str, step: str, elapsed: int, outcome: str, rc: int, *, path: Path | None = None
) -> None:
    """Append one row. Never raises — see the module docstring."""
    try:
        target = path or log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        rotate(target)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{cli}\t{step}\t{elapsed}\t{outcome}\t{rc}\n")
    except OSError:
        return


def _tail(path: Path, lines: int) -> list[str]:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - _TAIL_WINDOW))
        data = handle.read()
    rows = data.decode("utf-8", "replace").splitlines()
    if size > _TAIL_WINDOW and rows:
        rows = rows[1:]  # the window almost certainly split the first row
    return rows[-lines:] if lines > 0 else []


def max_ok_seconds(cli: str, *, lines: int = 500, path: Path | None = None) -> int:
    """Largest recorded elapsed among *cli*'s successful runs, else 0.

    Only ``outcome == "ok"`` rows with a plain-integer elapsed count. A
    missing, empty, unreadable, or malformed log simply yields 0, so history
    never wins over the formula and the budget is always still a valid number.
    """
    try:
        rows = _tail(path or log_path(), lines)
    except OSError:
        return 0
    best = 0
    for row in rows:
        fields = row.split("\t")
        if len(fields) != 6 or fields[1] != cli or fields[4] != "ok":
            continue
        if fields[3].isdigit():
            best = max(best, int(fields[3]))
    return best
