"""Raw single-key input, with a guaranteed non-blocking degradation.

Three surfaces, one module: POSIX ``termios``/``tty``, Windows ``msvcrt``,
and — the one that actually matters for correctness — a plain line-oriented
fallback whenever stdin is not a TTY.

**Why the fallback is load-bearing.** aicp runs in CI, in a pipe, and under
this project's own pytest suite, none of which have a terminal to press keys
on. A menu that reached for raw mode there would either raise (no ``fileno``)
or, far worse, block forever waiting for a keystroke that is never coming.
So :func:`is_interactive` is checked BEFORE any raw-mode call, and
:func:`read_line` treats EOF as "quit" rather than as something to wait on.
:func:`read_key` mirrors that: on a non-TTY it reads a line and maps it, and
an empty read is ``"quit"``, never a wait.

Key names returned are semantic (``up``/``down``/``left``/``right``/
``enter``/``quit``/``reset``/``yes``/``other``) so the menu never sees a byte.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Iterator
from typing import IO

__all__ = ["is_interactive", "key_session", "pending", "read_key", "read_line"]

#: What :func:`key_session` hands back: call it to wrap a prompt that reads a
#: typed line, which needs the line discipline the session suspends.
_Typed = Callable[[], "contextlib.AbstractContextManager[None]"]

# Windows sends arrows as a two-byte sequence introduced by one of these.
_WIN_PREFIXES = ("\x00", "\xe0")
_WIN_ARROWS = {"H": "up", "P": "down", "K": "left", "M": "right"}
_VT_ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}


def is_interactive(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> bool:
    """True only when BOTH streams are a real terminal.

    Both, not either: a menu that repaints needs somewhere to draw as much as
    it needs somewhere to read from, and a redirected stdout with a live
    stdin still has no frame to walk the cursor back up through.
    """
    streams = (stdin or sys.stdin, stdout or sys.stdout)
    try:
        return all(s is not None and s.isatty() for s in streams)
    except (AttributeError, ValueError, OSError):
        return False


def read_line(stdin: IO[str] | None = None) -> str | None:
    """One stripped line, or ``None`` at EOF (never a block on a dead pipe)."""
    stream = stdin or sys.stdin
    try:
        line = stream.readline()
    except (OSError, ValueError):
        return None
    if line == "":
        return None
    return line.strip()


def _classify(text: str) -> str:
    """Map typed text from the non-raw path onto a semantic key name."""
    lowered = text.strip().lower()
    if lowered in ("", "q", "quit"):
        return "quit"
    return {
        "up": "up", "k": "up",
        "down": "down", "j": "down",
        "left": "left", "h": "left",
        "right": "right", "l": "right",
        "r": "reset",
        "y": "yes",
    }.get(lowered, "enter" if lowered in ("enter", "\n") else "other")


def _read_key_posix(stream: IO[str]) -> str:
    import os
    import termios
    import tty

    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    try:
        # TCSANOW, not tty.setraw's TCSAFLUSH default: flushing discards
        # typeahead, so a key pressed while the menu was still repainting
        # would be silently swallowed rather than acted on next.
        tty.setraw(fd, termios.TCSANOW)
        ch = os.read(fd, 1).decode("utf-8", "replace")
        if ch == "\x1b":
            # An arrow is ESC [ X; a bare ESC is the user backing out. The
            # follow-up bytes are already buffered by the terminal, so this
            # read cannot hang on a real escape sequence.
            rest = os.read(fd, 2).decode("utf-8", "replace")
            return _VT_ARROWS.get(rest[-1:], "other") if rest.startswith("[") else "quit"
    finally:
        # Restoring the terminal is not optional and not only for the happy
        # path: a shell left in -icanon -echo looks broken to whoever uses it
        # next, so the restore is armed before raw mode is ever entered.
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return _from_char(ch)


def _read_key_windows() -> str:
    import msvcrt

    ch = msvcrt.getwch()
    if ch in _WIN_PREFIXES:
        return _WIN_ARROWS.get(msvcrt.getwch(), "other")
    return _from_char(ch)


def _from_char(ch: str) -> str:
    if ch in ("\r", "\n", " "):
        return "enter"
    # Raw mode disables ISIG, so Ctrl+C arrives as a byte rather than a
    # signal — cancelling a menu changed nothing and is not a failure.
    if ch in ("q", "Q", "\x03", "\x04", ""):
        return "quit"
    if ch in ("r", "R"):
        return "reset"
    if ch in ("y", "Y"):
        return "yes"
    return "other"


@contextlib.contextmanager
def key_session(stdin: IO[str], stdout: IO[str]) -> Iterator[_Typed]:
    """Hold the terminal in cbreak mode for a whole arrow-key session.

    Raw mode per keypress leaves the terminal echoing **between** reads, and
    a menu that animates spends real time between reads: a key pressed while
    a frame is moving is echoed onto the screen by the terminal driver
    itself, an Enter echoes a newline that pushes the whole frame down a
    row, and every repaint after it walks the cursor up to the wrong place —
    stacking a fresh header on screen for each one. That is the "hold Enter
    and the panel multiplies" bug, and no amount of care in the drawing code
    can fix it, because it is not the drawing code writing.

    cbreak and not raw: ``tty.setraw`` also turns off output processing, and
    a panel printed with no NL→CRNL translation comes out as a staircase.
    cbreak touches the input side only — and leaves Ctrl+C a signal, which
    the caller handles rather than reading as a byte.

    Yields the context manager to wrap any prompt that reads a typed line:
    :func:`read_line` needs the canonical mode this suspends.
    """
    if sys.platform == "win32" or not is_interactive(stdin, stdout):
        yield contextlib.nullcontext
        return
    import termios
    import tty

    fd = stdin.fileno()
    saved = termios.tcgetattr(fd)

    @contextlib.contextmanager
    def typed() -> Iterator[None]:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        try:
            yield
        finally:
            tty.setcbreak(fd, termios.TCSANOW)

    try:
        # TCSANOW, not setcbreak's TCSAFLUSH default: flushing discards
        # typeahead, and a key pressed while the menu was still drawing is
        # one the user meant, not one to swallow.
        tty.setcbreak(fd, termios.TCSANOW)
        yield typed
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def pending(stdin: IO[str]) -> bool:
    """Whether a keypress is already waiting to be read.

    An animation is time the menu is not listening, so it asks: with another
    key already queued, the frames still to draw are ones nobody will look
    at, and dropping them is what keeps a held-down key feeling immediate
    instead of replaying a backlog of slides.
    """
    try:
        if sys.platform == "win32":
            import msvcrt

            return msvcrt.kbhit()
        import select

        return bool(select.select([stdin], [], [], 0)[0])
    except (AttributeError, ImportError, OSError, ValueError):
        return False


def read_key(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> str:
    """One semantic key press. Falls back to a typed line on a non-TTY."""
    stream = stdin or sys.stdin
    if not is_interactive(stream, stdout):
        line = read_line(stream)
        return "quit" if line is None else _classify(line)
    if sys.platform == "win32":
        return _read_key_windows()
    try:
        return _read_key_posix(stream)
    except (OSError, ValueError, ImportError):
        line = read_line(stream)
        return "quit" if line is None else _classify(line)
