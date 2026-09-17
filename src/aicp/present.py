"""Terminal presentation: the spinner animation and the framed panel/table
renderer, ported from ``~/scripts/lib/spinner.sh`` and
``~/scripts/lib/box_render.py``.

Pure Python, no subprocess: the zsh original shells out to ``box_render.py``
once per render (a real, measured fork cost per panel drawn); this port pays
that cost once, at import, and never again.

Both pieces are TTY/``NO_COLOR``-gated the same way the shell/Python
originals were: the spinner animation only draws when stderr is a TTY and
``NO_COLOR`` is unset, falling back to ASCII frames off a UTF-8 locale.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from ._utils import BOLD, DIM, RESET
from .i18n import t

__all__ = [
    "Spinner",
    "render",
    "render_panel",
    "render_table",
    "spinner_capture",
    "spinner_enabled",
    "spinner_run",
    "width",
]

# ── spinner (lib/spinner.sh port) ────────────────────────────────────────────

_FRAMES_BRAILLE = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_FRAMES_ASCII = ("|", "/", "-", "\\")
_SPINNER_INTERVAL = 0.08
_SPINNER_COLOR = "\033[38;5;87m"


def spinner_enabled(stream=None) -> bool:
    """True when the animation should be drawn: *stream* (default stderr) is
    a TTY and ``NO_COLOR`` is unset — the same gate the shell original uses."""
    stream = stream if stream is not None else sys.stderr
    try:
        if not stream.isatty():
            return False
    except Exception:  # noqa: BLE001 - a stream with no working isatty() is never a TTY
        return False
    return not os.environ.get("NO_COLOR")


def _locale_is_utf8() -> bool:
    """Braille needs a UTF-8-capable locale; anything else gets ASCII frames.

    Checked in the same precedence as the shell original: LC_ALL, then
    LC_CTYPE, then LANG.
    """
    value = os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or ""
    return "utf-8" in value.lower() or "utf8" in value.lower()


def _frames() -> tuple[str, ...]:
    return _FRAMES_BRAILLE if _locale_is_utf8() else _FRAMES_ASCII


class Spinner:
    """Animate a message on stderr until stopped. Disabled (a no-op) when
    :func:`spinner_enabled` is False — matching spinner.sh's TTY/NO_COLOR gate.

    Usage::

        spinner = Spinner("Fetching…")
        spinner.start()
        ...
        spinner.stop()

    or as a context manager: ``with Spinner("Fetching…"): ...``.
    """

    def __init__(self, message: str = "Working…", *, stream=None) -> None:
        self._message = message
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = spinner_enabled(self._stream)
        self._frames = _frames()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def update(self, message: str) -> None:
        with self._lock:
            self._message = message

    def _run(self) -> None:
        i = 0
        n = len(self._frames)
        while not self._stop_event.is_set():
            with self._lock:
                message = self._message
            frame = self._frames[i % n]
            self._stream.write(f"\r{_SPINNER_COLOR}{frame}{RESET} {message}\033[K")
            self._stream.flush()
            i += 1
            self._stop_event.wait(_SPINNER_INTERVAL)
        self._stream.write("\r\033[K")
        self._stream.flush()

    def start(self) -> None:
        if not self._enabled:
            return
        self.stop()  # matches spinner_start's own leading spinner_stop
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None

    def __enter__(self) -> Spinner:  # noqa: PYI034 - Self needs 3.11+; this package supports 3.10
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def spinner_run(message: str, cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Animate *message* while *cmd* runs; relay its exit status.

    Port of spinner.sh's ``spinner_run``. Output is inherited (not captured) —
    use :func:`spinner_capture` to redirect it to a log instead.
    """
    spinner = Spinner(message)
    spinner.start()
    check = kwargs.pop("check", False)
    try:
        return subprocess.run(cmd, check=check, **kwargs)
    finally:
        spinner.stop()


def spinner_capture(
    logfile: str | Path, message: str, cmd: Sequence[str], **kwargs
) -> subprocess.CompletedProcess[str]:
    """Animate *message* while *cmd* runs, sending its combined stdout/stderr
    to *logfile* instead of the screen. Port of spinner.sh's ``spinner_capture``.
    """
    spinner = Spinner(message)
    spinner.start()
    check = kwargs.pop("check", False)
    try:
        with open(logfile, "w", encoding="utf-8") as handle:
            return subprocess.run(
                cmd, stdout=handle, stderr=subprocess.STDOUT, check=check, **kwargs
            )
    finally:
        spinner.stop()


# ── box render (lib/box_render.py port) ──────────────────────────────────────

FRAME = "\033[38;5;240m"
HEADER = "\033[48;5;238;38;5;255m"
STRIPE = "\033[48;5;235;38;5;252m"
# Resolved at import like every other message: the column widths below are
# measured from it, and a table cannot re-measure itself mid-run.
VALUE_HEADER = t("panel_value_header", "VALUE")

_ANSI = re.compile(r"\033\[[0-9;]*m")


def width(text: str) -> int:
    """Visible columns of *text*: ANSI escapes stripped, East-Asian
    Wide/Fullwidth glyphs count two columns, combining marks count none."""
    return sum(
        0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        for ch in _ANSI.sub("", text)
    )


def _reband(text: str, band: str) -> str:
    """*text* with *band* re-armed after every RESET it embeds — a colored
    cell ends in RESET, which clears the background too; without re-arming, a
    banded row would lose its stripe from that cell onward."""
    return text.replace(RESET, RESET + band) if band else text


def render_panel(
    rows: Sequence[tuple[str, str]],
    title: str,
    accent: str,
    zebra: bool = False,
    notes: Sequence[str] = (),
    min_inner: int = 0,
) -> list[str]:
    """Render *rows* (label, value pairs) as a titled rounded-frame panel.

    ``zebra`` shades every other row full-width; a single-row panel never
    bands (nothing to alternate against). ``notes`` are extra full-width,
    already-colored lines drawn inside the same frame below the rows (a blank
    separator first, then one line per note) — for content that isn't a
    (label, value) pair, such as the config menu's per-row help text and its
    key-hint footer. A row whose ``value`` is empty is a section heading
    (e.g. the config menu's "Steps"/"General"/"Tools" groups) — bold, then a
    dim rule out to the frame, spanning the full width instead of sharing the
    label/value columns. Weight and the rule carry the separation, not a
    fifth hue: bold against DIM labels reads as a heading at a glance, while
    CYAN/GREEN/MAGENTA stay free to mean what a *value* means elsewhere (a
    changeable setting, a state, the repo name), and the frame's own colour
    stays chrome. Bold on the terminal's default foreground — the same
    treatment as *title* — rather than a hardcoded white, so the heading
    survives a light-background theme.

    ``min_inner`` is a floor on the frame's inner width, unrelated to
    content: a caller opening a second, related panel (the config menu's
    Agents sub-panel) passes the primary panel's own width here so the two
    never disagree — switching between them must not resize the border.
    """
    label_w = max(width(k) for k, v in rows if v)
    value_w = max((width(v) for _, v in rows if v), default=0)
    inner = max(
        label_w + value_w + 6,
        width(title) + 3,
        min_inner,
        *(width(n) + 4 for n in notes),
        *(width(k) + 4 for k, v in rows if not v),
    )
    dashes = inner - width(title) - 3
    out = [f"{accent}╭─ {BOLD}{title}{RESET}{accent} {'─' * dashes}╮{RESET}"]
    for i, (label, value) in enumerate(rows):
        if not value:
            rule = max(inner - 4 - width(label), 0)
            tail = f" {DIM}{'─' * rule}{RESET} " if rule else " " * (inner - 2 - width(label))
            out.append(f"{accent}│{RESET}  {BOLD}{label}{RESET}{tail}{accent}│{RESET}")
            continue
        pad = " " * (label_w - width(label))
        vpad = " " * (value_w - width(value))
        trail = " " * (inner - 4 - label_w - value_w)
        content = f"  {DIM}{label}{pad}{RESET}  {value}{vpad}{trail}"
        band = STRIPE if (zebra and len(rows) > 1 and i % 2) else ""
        body = f"{band}{_reband(content, band)}{RESET}" if band else content
        out.append(f"{accent}│{RESET}{body}{accent}│{RESET}")
    if notes:
        out.append(f"{accent}│{RESET}{' ' * inner}{accent}│{RESET}")
        for note in notes:
            pad = " " * (inner - 2 - width(note))
            out.append(f"{accent}│{RESET}  {note}{pad}{accent}│{RESET}")
    out.append(f"{accent}╰{'─' * inner}╯{RESET}")
    return out


def render_table(rows: Sequence[tuple[str, str]], title: str) -> list[str]:
    """Render *rows* (label, value pairs) as a two-column table with a
    banded header row and zebra-striped body rows."""
    widths = [
        max(width(title), *(width(k) for k, _ in rows)),
        max(width(VALUE_HEADER), *(width(v) for _, v in rows)),
    ]

    def rule(left: str, mid: str, right: str) -> str:
        return FRAME + left + mid.join("─" * (w + 2) for w in widths) + right + RESET

    def row(cells: Sequence[str], band: str = "") -> str:
        parts = [
            f" {_reband(c, band)}{' ' * max(w - width(c), 0)} " for c, w in zip(cells, widths)
        ]
        edge = f"{FRAME}│{RESET}"
        if not band:
            return edge + edge.join(parts) + edge
        return f"{edge}{band}{(FRAME + '│' + band).join(parts)}{RESET}{edge}"

    return [
        rule("╭", "┬", "╮"),
        row([f"{BOLD}{title}{RESET}", f"{BOLD}{VALUE_HEADER}{RESET}"], HEADER),
        rule("├", "┼", "┤"),
        *(row([k, v], STRIPE if i % 2 else "") for i, (k, v) in enumerate(rows)),
        rule("╰", "┴", "╯"),
    ]


def render(
    rows: Sequence[tuple[str, str]],
    title: str,
    *,
    mode: str = "table",
    accent: str = FRAME,
    zebra: bool = False,
) -> list[str]:
    """Dispatch to :func:`render_panel`/:func:`render_table`, drawing nothing
    for empty *rows* — the single entry point callers should use (mirrors
    ``box_render.py``'s CLI, which exits 0 with no output for empty stdin,
    rather than calling the two renderers directly and having to remember
    the empty-input guard themselves)."""
    if not rows:
        return []
    return render_panel(rows, title, accent, zebra) if mode == "panel" else render_table(rows, title)
