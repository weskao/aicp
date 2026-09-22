"""The hardened ``~/.aicp/config.json`` layer: a config file is DATA, and
nothing else.

Ported from ``_aicp_load_config`` / ``_aicp_persist_key`` in
``~/scripts/bin/aicp``, then migrated from ``~/.aicprc`` (KEY=value text) to
``~/.aicp/config.json`` — a JSON object, written atomically and owner-only —
matching the storage mechanism the sibling ``ai-accounts`` project uses for
its own ``~/.ai-accounts/config.json``. Every security control below predates
that move and is unchanged by it: only the file's serialization envelope
changed, not what a value is allowed to do. A legacy ``~/.aicprc`` is folded
in once (see :func:`_maybe_migrate`) and left in place, never deleted.

That loader's header comment is the design record; what follows is the short
version of why each control exists, because every one of them was added in
response to a hole that was verified live, not imagined.

**Parsed as data, never sourced or eval'd.** ``source``/``eval`` on a config
file runs arbitrary code, and a config file is exactly the kind of file that
arrives synced from someone else's dotfiles repo. Only ``$HOME/.aicp/config.json``
(or ``AICP_CONFIG``) is read; no repo-local file is ever consulted, on
purpose — merely running ``aicp`` inside someone else's clone must never
execute config lines they wrote.

"Never eval'd" is necessary but is NOT sufficient on its own: it stops a
value from running as syntax, and says nothing about a value that is itself
later used AS a command or a path. Hence three more layers:

1. **Key allowlist** — only ``AICP_[A-Z0-9_]*`` (case-insensitively: on disk
   a key is written ``aicp_do_commit``, matching the environment's
   ``AICP_DO_COMMIT`` only once case-folded on read — see
   :func:`_read_json_object`). A line naming ``PATH`` is inert text to this
   loader, not an assignment. (Telegram credentials, ``TG_BOT_TOKEN`` and
   ``TG_CHAT_ID``, aren't ``AICP_``-prefixed at all, so they never reach this
   loader in the first place — see :mod:`aicp.notify`.)
2. **Value charset allowlist** — letters, digits and
   ``/ . _ : @ + -`` plus whitespace. ``=`` and ``,`` are excluded because in
   the zsh original a value like ``PATH=0`` reaching an arithmetic context
   would assign into the real ``$PATH``; ``$``/backtick/``;``/``|``/parens
   are excluded as defence in depth for whatever the next consumer of these
   values does with them.
3. **Denylist** (:data:`DENYLIST`) — ``AICP_TIMING_LOG`` (reaches
   ``mkdir -p``, ``>>``, ``mv -f``, ``rm -f``) and ``AICP_CONFIG`` (names the
   file this loader reads, and the file :func:`persist_key` then
   ``mkdir -p``s and atomically replaces) are ENVIRONMENT-VARIABLE ONLY. Both
   are plain literal paths that sail through the charset allowlist, and both
   were real holes. Whoever adds the next knob that flows into an exec path
   or a path-mutating sink adds its name here — the charset allowlist does
   not protect against this class at all.

   ``AICP_CONFIG`` is the subtlest of the three, because a file naming
   *itself* looks inert: nothing in this module acts on the value. The zsh
   original is immune by accident of ordering — ``: "${AICP_CONFIG:=...}"``
   runs before its loader, so the loader's "already set, environment wins"
   check always skips a file-supplied copy. This port has no such ordering
   guarantee once a caller (``cli.export_settings``) puts accepted values
   back into ``os.environ``: the NEXT ``config_path()`` would resolve to the
   file's chosen path, and the next ``persist_key`` — an ordinary
   ``--config`` menu write — would create directories and atomically replace
   a file the user never named. A ``.aicprc`` arriving from someone else's
   dotfiles repo is exactly the delivery mechanism this loader was hardened
   against, so the key is denied at the source rather than filtered at each
   consumer.

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

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ROSTER

#: The pre-JSON config file this loader migrates from, once, on first read of
#: the default (non-``AICP_CONFIG``-overridden) location. Never written back
#: to and never deleted — see :func:`_maybe_migrate`.
_LEGACY_NAME = ".aicprc"

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
#: synced from someone else's dotfiles repo. ``AICP_CONFIG`` belongs here for
#: the reason spelled out in this module's docstring — a file must not be able
#: to rename the file the next write lands on.
DENYLIST = frozenset({"AICP_TIMING_LOG", "AICP_CONFIG"})

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
    update_check: bool = True
    lang: str = "en"
    tz: str = _DEFAULT_TZ
    cli_chain: tuple[str, ...] = _ROSTER_NAMES
    values: Mapping[str, str] = field(default_factory=dict)


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """``AICP_CONFIG`` if set, else ``~/.aicp/config.json``. The only file
    ever read (a legacy ``~/.aicprc`` is migrated in once — see
    :func:`_maybe_migrate` — never read directly by this function)."""
    env = os.environ if env is None else env
    override = env.get("AICP_CONFIG")
    return Path(override) if override else Path(os.path.expanduser("~")) / ".aicp" / "config.json"


def _accept(key: str, value: str) -> str | None:
    """*value* if *key* legitimately supplies it, else ``None``.

    Shared by the JSON reader and the legacy line-parser: the key allowlist,
    denylist, system-resolved exclusion and value charset allowlist are one
    rule set regardless of which file format supplied the candidate pair.
    """
    if not _KEY_RE.match(key):
        return None
    if key in DENYLIST:
        _warn(
            f"config.json: ignoring {key} (exec-path/path-mutation knob, "
            "environment-variable only)"
        )
        return None
    if key in _SYSTEM_RESOLVED:
        _warn(f"config.json: ignoring {key} (resolved from PATH, never from config)")
        return None
    value = value.strip()
    if not _VALUE_RE.match(value):
        return None
    return value


def _read_json_object(path: Path) -> dict:
    """*path* parsed as a JSON object — ``{}`` when absent, unreadable, not
    valid JSON, or not an object at the top level.

    Keys are upper-cased on the way in: on disk (and in whatever a user
    hand-edits) a key is ``aicp_do_commit``, but every other rule in this
    module — :data:`DENYLIST`, :data:`_SYSTEM_RESOLVED`, :data:`_KEY_RE`, the
    environment lookup in :func:`resolve` — is written once, in the
    ``AICP_DO_COMMIT`` form shared with the environment. Case-folding here,
    at the one place a JSON object turns into a plain dict, means the rest of
    the module never has to know the file's on-disk casing differs from the
    environment's.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key.upper(): value for key, value in data.items()}


def _write_json_private(path: Path, data: Mapping[str, object]) -> bool:
    """Atomically overwrite *path* with *data* as owner-only (0600) JSON.

    Also the writer :mod:`aicp.agentcfg` uses for ``~/.aicp/agents.json``:
    same directory, same owner-only posture, and the same guarantee that a
    crash mid-write cannot truncate what was already there.

    Keys are lower-cased on the way out — ``AICP_DO_COMMIT`` (the form every
    caller passes in, matching the environment) is written as
    ``aicp_do_commit``. This is the only place that happens, so every write
    path (:func:`persist_key`, the one-time :func:`_maybe_migrate`) gets the
    lower_case convention for free, including for keys this version has never
    heard of.

    Created 0600 up front rather than chmod'ed afterwards, so the file is
    never briefly readable by another local user, and swapped in with
    ``os.replace`` so a crash mid-write cannot truncate the previous
    contents. Returns ``False`` (never raises) on any ``OSError`` — the
    callers are a settings menu and a best-effort migration, neither of
    which may crash the run over a failed write.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        lowered = {key.lower(): value for key, value in data.items()}
        text = json.dumps(lowered, indent=2, sort_keys=True) + "\n"
        try:
            with os.fdopen(
                os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(text)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return True
    except OSError:
        return False


def _load_legacy_lines(path: Path) -> dict[str, str]:
    """Parse a pre-JSON ``.aicprc`` (KEY=value text) the same way this loader
    always has — used only by :func:`_maybe_migrate`, once."""
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
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        accepted = _accept(key, value)
        if accepted is not None:
            values[key] = accepted
    return values


def _maybe_migrate(path: Path) -> None:
    """Fold a legacy ``~/.aicprc`` into *path* once, iff *path* is the true
    default location (no ``AICP_CONFIG`` override) and does not exist yet.

    The legacy file is left in place untouched — this is a one-time copy,
    never a move, so a config synced across machines via the old path keeps
    working on whichever one hasn't migrated yet.
    """
    if path.exists():
        return
    legacy = Path(os.path.expanduser("~")) / _LEGACY_NAME
    if not legacy.exists():
        return
    migrated = _load_legacy_lines(legacy)
    if _write_json_private(path, migrated):
        _warn(f"migrated {legacy} to {path} (original left in place)")


def load_config(path: Path | str | None = None) -> dict[str, str]:
    """Parse *path* into the ``AICP_*`` values it legitimately supplies.

    The file half of :func:`resolve` — no environment is consulted here, and
    no migration is attempted (that is :func:`resolve`'s job, since it alone
    knows whether ``AICP_CONFIG`` was overridden). Non-string JSON values,
    non-``AICP_`` keys, denied keys and values outside the charset allowlist
    are all skipped individually: one bad key never costs the rest of the
    file.
    """
    path = config_path() if path is None else Path(path)
    raw = _read_json_object(path)
    values: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            continue
        accepted = _accept(key, value)
        if accepted is not None:
            values[key] = accepted
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
    """Environment over ``config.json`` over hardcoded defaults, all validated."""
    env = os.environ if env is None else env
    path = config_path(env)
    if "AICP_CONFIG" not in env:
        _maybe_migrate(path)
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
        update_check=_boolean("AICP_UPDATE_CHECK", values.get("AICP_UPDATE_CHECK")),
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
    """Merge ``{key: value}`` into *path*'s JSON object. False on failure.

    Every other key already in the file is kept, so a knob this version has
    never heard of survives a write from the settings menu — the same
    forward-compatibility promise the old KEY=value writer made, just without
    the comment lines JSON has no way to represent. Written atomically and
    owner-only via :func:`_write_json_private`.

    Returns a bool rather than raising: the caller is an interactive menu
    that must report a failed write and keep running.
    """
    path = config_path() if path is None else Path(path)
    data = _read_json_object(path)
    data[key] = value
    return _write_json_private(path, data)
