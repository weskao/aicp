"""The hardened ``.aicprc`` layer: a config file is DATA, and nothing else.

Ported from ``_aicp_load_config`` / ``_aicp_persist_key`` in
``~/scripts/bin/aicp``. That loader's header comment is the design record;
what follows is the short version of why each control exists, because every
one of them was added in response to a hole that was verified live, not
imagined.

**Parsed line by line, never sourced or eval'd.** ``source``/``eval`` on a
config file runs arbitrary code, and ``~/.aicprc`` is exactly the kind of
file that arrives synced from someone else's dotfiles repo. Only
``$HOME/.aicprc`` (or ``AICP_CONFIG``) is read; no repo-local file is ever
consulted, on purpose — merely running ``aicp`` inside someone else's clone
must never execute config lines they wrote.

"Never eval'd" is necessary but is NOT sufficient on its own: it stops a
value from running as syntax, and says nothing about a value that is itself
later used AS a command or a path. Hence three more layers:

1. **Key allowlist** — only ``AICP_[A-Z0-9_]*``. A line naming ``PATH`` is
   inert text to this loader, not an assignment.
2. **Value charset allowlist** — letters, digits and
   ``/ . _ : @ + -`` plus whitespace. ``=`` and ``,`` are excluded because in
   the zsh original a value like ``PATH=0`` reaching an arithmetic context
   would assign into the real ``$PATH``; ``$``/backtick/``;``/``|``/parens
   are excluded as defence in depth for whatever the next consumer of these
   values does with them.
3. **Denylist** (:data:`DENYLIST`) — ``AICP_TG_SEND`` (reaches
   ``bash "$value"``) and ``AICP_TIMING_LOG`` (reaches ``mkdir -p``, ``>>``,
   ``mv -f``, ``rm -f``) are ENVIRONMENT-VARIABLE ONLY. Both are plain
   literal paths that sail through the charset allowlist, and both were real
   holes. Whoever adds the next knob that flows into an exec path or a
   path-mutating sink adds its name here — the charset allowlist does not
   protect against this class at all.

``AICP_TIMEOUT_BIN`` is the fourth case and is deliberately NOT on the
denylist: it is never taken from configuration in the first place. See
:func:`timeout_bin` — it is resolved unconditionally from ``PATH``, so a
config-supplied decoy binary can never be invoked. Making that resolution
conditional (``value or which(...)``) would reopen the hole and requires
adding the key to :data:`DENYLIST` first.

Precedence everywhere, highest to lowest: **environment > .aicprc >
hardcoded default**. Every invalid value announces itself on stderr and
falls back to a safe default; nothing here ever aborts the run it is only
supposed to configure.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ROSTER

__all__ = [
    "DENYLIST",
    "Settings",
    "config_path",
    "load_config",
    "persist_key",
    "resolve",
    "resolve_cli_chain",
    "timeout_bin",
]

#: Exec-path and path-mutation knobs: settable from the real environment (a
#: boundary the user controls directly), never from a file that can arrive
#: synced from someone else's dotfiles repo.
DENYLIST = frozenset({"AICP_TG_SEND", "AICP_TIMING_LOG"})

#: Knobs resolved from the system, never from configuration — see
#: :func:`timeout_bin`. Kept as a set so the reason is greppable from both
#: ends.
_SYSTEM_RESOLVED = frozenset({"AICP_TIMEOUT_BIN"})

_KEY_RE = re.compile(r"^AICP_[A-Z0-9_]*$")
_VALUE_RE = re.compile(r"^[A-Za-z0-9/._:@+\s-]*$")
# An IANA zone name ("Asia/Taipei", "Etc/UTC", "UTC") and nothing that could
# also be read as a path: no leading ":", no leading "/", no "..".
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$")

_DEFAULT_TZ = "Asia/Taipei"
_LANGUAGES = ("en", "zh-TW")
_ROSTER_NAMES: tuple[str, ...] = tuple(c.name for c in ROSTER)


def _warn(message: str) -> None:
    print(f"aicp: {message}", file=sys.stderr)


@dataclass(frozen=True)
class Settings:
    """Every knob this layer owns, already validated.

    ``values`` carries the raw accepted ``AICP_*`` strings (environment over
    file) for knobs this module does not itself validate — the timeout
    family, which T2's budget calculator validates as plain non-negative
    integers right before it does arithmetic on them. A knob missing from
    ``values`` is a knob whose consumer applies its own hardcoded default.
    """

    path: Path
    do_commit: bool = True
    do_push: bool = True
    lang: str = "en"
    tz: str = _DEFAULT_TZ
    cli_chain: tuple[str, ...] = _ROSTER_NAMES
    values: Mapping[str, str] = field(default_factory=dict)


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """``AICP_CONFIG`` if set, else ``~/.aicprc``. The only file ever read."""
    env = os.environ if env is None else env
    override = env.get("AICP_CONFIG")
    return Path(override) if override else Path(os.path.expanduser("~")) / ".aicprc"


def load_config(path: Path | str | None = None) -> dict[str, str]:
    """Parse *path* into the ``AICP_*`` values it legitimately supplies.

    The file half of :func:`resolve` — no environment is consulted here.
    Comments, blank lines, lines without ``=``, non-``AICP_`` keys, denied
    keys and values outside the charset allowlist are all skipped
    individually: one bad line never costs the rest of the file.
    """
    path = config_path() if path is None else Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.rstrip()
        if not _KEY_RE.match(key):
            continue
        if key in DENYLIST:
            _warn(
                f".aicprc: ignoring {key} (exec-path/path-mutation knob, "
                "environment-variable only)"
            )
            continue
        if key in _SYSTEM_RESOLVED:
            _warn(f".aicprc: ignoring {key} (resolved from PATH, never from config)")
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not _VALUE_RE.match(value):
            continue
        values[key] = value
    return values


def _boolean(key: str, raw: str | None) -> bool:
    """``0``/``1`` only. Deliberately not a truthiness test: a hand-written
    ``false`` or ``no`` means OFF to whoever typed it, and reading every one
    of them as ON is the worse misread. Anything else is junk, is said out
    loud, and falls back to ON — a skipped push that happens is recoverable,
    one that silently did not is not."""
    if raw is None:
        return True
    if raw in ("0", "1"):
        return raw == "1"
    _warn(f"ignoring {key}={raw} (expected 0 or 1) — using 1")
    return True


def _language(raw: str | None) -> str:
    if raw is None or raw in _LANGUAGES:
        return raw or "en"
    _warn(f"ignoring AICP_LANG={raw} (expected en or zh-TW) — using en")
    return "en"


def _timezone(raw: str | None) -> str:
    if raw is None:
        return _DEFAULT_TZ
    if _TZ_RE.match(raw):
        return raw
    _warn(
        f"ignoring AICP_TZ={raw} (not an IANA zone name like Asia/Taipei) "
        f"— using {_DEFAULT_TZ}"
    )
    return _DEFAULT_TZ


def resolve_cli_chain(order: str | None) -> Sequence[str]:
    """Resolve *order* (an ``AICP_CLI_ORDER`` string) into the fallback chain.

    **This is the contract T2 and T4 consume** (see ``contracts.py``): a
    plain ``Sequence[str]`` of roster binary names, passed to them as an
    explicit argument. Neither ever reaches into a config object for it, and
    neither hardcodes :data:`~aicp.contracts.ROSTER` itself.

    A SUBSET is accepted on purpose, with the roster names it leaves out
    appended behind it in roster order: the roster grows over time, and a
    strict "must be an exact permutation" rule would silently invalidate
    every ``.aicprc`` already written every time it does. Listing a prefix is
    also the honest way to say "these first, then whatever else you know
    about". An unknown or repeated name is refused outright — a typo must
    never silently narrow the chain.
    """
    if not order or not order.strip():
        return _ROSTER_NAMES
    listed = order.split()
    unknown = [name for name in listed if name not in _ROSTER_NAMES]
    if unknown or len(set(listed)) != len(listed):
        _warn(
            f"ignoring AICP_CLI_ORDER={order} (unknown or repeated CLI name; "
            f"known: {' '.join(_ROSTER_NAMES)}) — using default order"
        )
        return _ROSTER_NAMES
    return (*listed, *(name for name in _ROSTER_NAMES if name not in listed))


def resolve(env: Mapping[str, str] | None = None) -> Settings:
    """Environment over ``.aicprc`` over hardcoded defaults, all validated."""
    env = os.environ if env is None else env
    path = config_path(env)
    values = load_config(path)
    # The environment always wins, key by key — a file can fill a gap, never
    # overwrite something the user exported for this one run. The one
    # exception is _SYSTEM_RESOLVED: the zsh original clobbers
    # AICP_TIMEOUT_BIN unconditionally, from BOTH sources, so it never
    # appears here at all and no consumer can accidentally prefer it over
    # timeout_bin()'s PATH lookup.
    for key, value in env.items():
        if _KEY_RE.match(key) and key not in _SYSTEM_RESOLVED:
            values[key] = value
    for key in _SYSTEM_RESOLVED:
        values.pop(key, None)

    return Settings(
        path=path,
        do_commit=_boolean("AICP_DO_COMMIT", values.get("AICP_DO_COMMIT")),
        do_push=_boolean("AICP_DO_PUSH", values.get("AICP_DO_PUSH")),
        lang=_language(values.get("AICP_LANG")),
        tz=_timezone(values.get("AICP_TZ")),
        cli_chain=tuple(resolve_cli_chain(values.get("AICP_CLI_ORDER"))),
        values=values,
    )


def timeout_bin() -> str | None:
    """The per-CLI-call timeout wrapper, resolved from ``PATH`` ONLY.

    Prefers GNU coreutils ``timeout``, falls back to Homebrew's ``gtimeout``,
    and returns ``None`` when neither is installed (a hang just isn't caught
    on that machine). Reading no configuration at all IS this knob's security
    boundary: the resolved value is later invoked as a command, so a
    ``.aicprc``-supplied path here would be arbitrary code execution once per
    CLI step. Do not make this conditional on a configured value without
    first adding ``AICP_TIMEOUT_BIN`` to :data:`DENYLIST`.
    """
    return shutil.which("timeout") or shutil.which("gtimeout")


def persist_key(key: str, value: str, path: Path | str | None = None) -> bool:
    """Rewrite (or append) one ``KEY=value`` line in *path*. False on failure.

    Every other line is copied through untouched, so a comment, or a knob
    this version has never heard of, survives a write from the settings menu.
    Write-to-temp-then-rename keeps a concurrent reader from ever seeing a
    half-written line, and the temp file is removed on failure rather than
    left beside the real config.

    Returns a bool rather than raising: the caller is an interactive menu
    that must report a failed write and keep running.
    """
    path = config_path() if path is None else Path(path)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        try:
            existing = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing = []
        prefix = f"{key}="
        replaced = False
        out: list[str] = []
        for line in existing:
            if line.startswith(prefix):
                out.append(f"{key}={value}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"{key}={value}")

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
