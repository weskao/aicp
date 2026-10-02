"""The ``aicp --config`` logo: colour, mono, animated, or off.

Rows are embedded literals (FIGlet fonts ``ansi_shadow`` / ``small`` /
``pagga``), so there is no runtime font dependency. Three tiers, widest
first; :func:`pick` returns the first that fits the terminal, or ``()`` when
none does — a short or narrow window gets the panel and nothing else.

Colour is 256-colour only, like the rest of the UI (``_utils.BLUE`` etc.),
and every painted line ends in ``RESET``. With colour off the rows come back
as plain glyphs.
"""

from __future__ import annotations

from collections.abc import Sequence

from ._utils import BLUE, DIM, RESET

__all__ = ["DEFAULT_MODE", "INDENT", "MODES", "TIERS", "frames", "paint", "pick"]

MODES: tuple[str, ...] = ("color", "mono", "animated", "off")
DEFAULT_MODE = "color"

#: Columns of margin left of the logo — lines up with the panel's text.
INDENT = 2

#: Widest first. Every row of a tier is the same width.
TIERS: tuple[tuple[str, ...], ...] = (
    (
        " █████╗ ██╗ ██████╗██████╗ ",
        "██╔══██╗██║██╔════╝██╔══██╗",
        "███████║██║██║     ██████╔╝",
        "██╔══██║██║██║     ██╔═══╝ ",
        "██║  ██║██║╚██████╗██║     ",
        "╚═╝  ╚═╝╚═╝ ╚═════╝╚═╝     ",
    ),
    (
        "   _   ___ ___ ___ ",
        "  /_\\ |_ _/ __| _ \\",
        " / _ \\ | | (__|  _/",
        "/_/ \\_\\___\\___|_|  ",
    ),
    (
        "░█▀█░▀█▀░█▀▀░█▀█",
        "░█▀█░░█░░█░░░█▀▀",
        "░▀░▀░▀▀▀░▀▀▀░▀░░",
    ),
)

#: Strokes that are shadow or filler, not letter face: drawn one step darker.
_SHADOW = frozenset("╔╗╚╝═║╠╣╦╩╬░▒▓")
#: Blue → cyan, left to right (xterm-256 codes; 75 and 87 are ``_utils`` BLUE/CYAN's).
_RAMP = tuple(f"\033[38;5;{n}m" for n in (63, 69, 75, 81, 87))
_SHADOW_COLOR = "\033[38;5;67m"
#: The shimmer: the glint itself, and the two columns either side of it.
_GLINT = "\033[1;38;5;231m"
_GLOW = "\033[38;5;159m"
_GLINT_REACH = 2
#: Columns the glint advances per animation frame.
_GLINT_STEP = 2


def pick(columns: int, lines: int, reserve: int) -> tuple[str, ...]:
    """The widest tier that fits *columns* x *lines* beside *reserve* rows of UI.

    ``INDENT + width + 1``: the last cell is left empty, since filling it
    auto-wraps on many terminals. The extra row is the blank under the logo.
    """
    for rows in TIERS:
        if columns >= INDENT + len(rows[0]) + 1 and lines >= len(rows) + 1 + reserve:
            return rows
    return ()


def paint(rows: Sequence[str], mode: str, colour: bool, glint: int | None = None) -> list[str]:
    """*rows* indented and coloured for *mode*.

    ``glint`` is the column the animated shimmer is at (``None`` = at rest).
    Plain glyphs when *colour* is off or the mode is ``off``/unknown-to-paint;
    the caller decides whether to draw ``off`` at all.
    """
    pad = " " * INDENT
    if not colour or mode == "off":
        return [pad + row.rstrip() for row in rows]
    span = max(1, len(rows[0]) - 1)
    out = []
    for row in rows:
        parts = [pad]
        last = ""
        for x, ch in enumerate(row):
            if ch == " ":
                parts.append(ch)
                continue
            if mode == "mono":
                code = DIM + BLUE if ch in _SHADOW else BLUE
            elif glint is not None and abs(x - glint) <= _GLINT_REACH and ch not in _SHADOW:
                code = _GLINT if x == glint else _GLOW
            elif ch in _SHADOW:
                code = _SHADOW_COLOR
            else:
                code = _RAMP[round(x / span * (len(_RAMP) - 1))]
            if code != last:
                parts.append(code)
                last = code
            parts.append(ch)
        out.append("".join(parts) + RESET)
    return out


def frames(rows: Sequence[str]) -> list[list[str]]:
    """The shimmer: a glint sweeping left to right, ending at rest.

    The last frame is the static ``color`` painting, so the animation settles
    into exactly what the non-animated mode shows.
    """
    sweep = range(-_GLINT_REACH, len(rows[0]) + _GLINT_REACH, _GLINT_STEP)
    return [paint(rows, "animated", True, x) for x in sweep] + [paint(rows, "animated", True)]
