"""``--config`` settings menu and ``--swap-ai`` picker.

Ported from ``_aicp_config``/``_aicp_config_fallback``/``_aicp_cfg_*`` and
``_aicp_swap_ai``/``_aicp_print_cli_order`` in ``~/scripts/bin/aicp``.

**Saves as you go.** Every pick writes to ``.aicprc`` immediately, through
:func:`~aicp.config.persist_key`, so there is no save key to forget and
quitting can never discard a change. That also means every write goes
through the same preserve-unknown-lines, temp-file-then-rename path the
config layer owns — this module never writes the file itself.

**Two surfaces, one row model.** An arrow-key TUI on a terminal and a
numbered typed-choice fallback anywhere else (a pipe, CI, this repo's
suite). The fallback is not a lesser copy bolted on: it is the reference
implementation's own design, and the surface the tests drive, since a
harness has no TTY to press keys on.

.. rubric:: Adding a row

:data:`ROWS` is data, not parallel ``case`` arms. To add a setting, append
one :class:`Row` to that tuple and nothing else changes: numbering, drawing,
prompt text ("1-N"), bounds checking and dispatch are all derived from the
tuple's length and each row's own callables. A row needs a config ``key``,
a ``group`` heading (``None`` to share the previous row's), ``label``/``help``
as ``(msgid, english)`` pairs, a ``value`` renderer and a ``cycle`` mutator
that returns the string to persist. Nothing in this module hardcodes a count.

The Skills, Agents and Doctor rows were added exactly that way, and are the
reason a row may carry an ``action`` instead of a ``cycle``: they *do*
something (install skills, turn an agent on or off, show a health report)
rather than persist a setting, and they show live status in their value
column. Doctor is special among them: the arrow-key TUI expands the full
report inside the panel as soon as that row is selected, so Enter/←/→ on it
are deliberately no-ops; the numbered fallback still prints the same report
via the row's ``action``. They are rows and not subcommands on purpose —
``aicp`` and ``aicp --config`` are the only two things this tool ever asks
anyone to remember. Agents is the one that also has a scriptable twin,
``aicp --agents``, because adding an agent means supplying six fields, which
is a form and not a row.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import io
import os
import re
import sys
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import telegram_kit

from . import (
    __version__,
    agentcfg,
    agents,
    gitflow,
    i18n,
    logo,
    skills,
    telegram_store,
    update_check,
)
from ._keyreader import (
    is_interactive,
    key_session,
    pending,
    read_edit_key,
    read_key,
    read_line,
)
from ._utils import (
    BLUE,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    color_supported,
    typed_path,
)

# _KEY_RE and _SYSTEM_RESOLVED are read, not copied: the doctor reports on the
# loader's own verdict about a line, so it has to ask with the loader's own key
# rule, and has to name the keys that may never come from a file at all.
from .config import (
    _KEY_RE,
    _SYSTEM_RESOLVED,
    DEFAULT_LOGO,
    DENYLIST,
    LOGO_MODES,
    TELEGRAM_CHAT_ID_KEY,
    Settings,
    _read_json_object,
    _write_json_private,
    export_payload,
    import_updates,
    load_config,
    persist_key,
    reset_key,
    resolve,
    timeout_bin,
)
from .contracts import ROSTER
from .present import render_panel, width

__all__ = ["ROWS", "MenuState", "Row", "config_menu", "swap_ai", "update_prompt"]

_ROSTER_NAMES: tuple[str, ...] = tuple(c.name for c in ROSTER)
#: What the AI CLI order row puts between two names, and the unit the order
#: animation slides by — one name plus one separator is exactly one rotation.
_SEPARATOR = " → "
_ELLIPSIS = "…"
_MIN_VALUE_COLUMNS = 12
#: Long enough to read as motion rather than a jump, short enough that it is
#: over before a held-down arrow key feels laggy.
_MOTION_SECONDS = 0.15


def _t(lang: str, msgid: str, english: str, *args: object) -> str:
    """:func:`aicp.i18n.t` with the language passed in rather than resolved.

    ``i18n.t`` resolves ``AICP_LANG`` once at import time, which is right for
    a one-shot run and wrong here: the language row has to repaint the menu
    in the language just picked, in the same process, before anything else
    happens. Same catalogue, same fallback — a missing msgid degrades to the
    English at the call site, never to a blank line.
    """
    text = i18n.CATALOG.get(msgid, english) if lang != "en" else english
    return text % args if args else text


@dataclass
class MenuState:
    """The live values a menu session is editing, plus where they persist."""

    path: Path
    do_commit: bool
    do_push: bool
    lang: str
    chain: list[str]
    update_check: bool = True
    logo: str = DEFAULT_LOGO
    #: Rows the logo above the panel occupies (0 = none drawn). The panel
    #: sheds its notes against the terminal height minus this, so logo + panel
    #: always fit and the repaint arithmetic only ever concerns the panel.
    logo_rows: int = 0
    #: Probe caches for the two action rows. The panel repaints on every
    #: keypress, and both of those rows show live status in their value
    #: column — walking the filesystem and shelling out to git once per
    #: repaint would be a real, per-keystroke cost. Probed once per session
    #: instead, and dropped by the Skills action whenever it changes anything.
    skills_status: list[skills.SkillStatus] | None = None
    health: list[tuple[str, str]] | None = None
    agent_rows: list[agents.AgentRow] | None = None
    #: The Telegram chat id as config.json stores it (``""`` when unset).
    telegram_chat_id: str = ""
    #: ``(value text, accent)`` for the bot token row — same reasoning as the
    #: caches above: each read is a subprocess call to the credential store,
    #: and the panel repaints on every keypress.
    telegram_token: tuple[str, str] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> MenuState:
        return cls(
            path=settings.path,
            do_commit=settings.do_commit,
            do_push=settings.do_push,
            lang=settings.lang,
            chain=list(settings.cli_chain),
            update_check=settings.update_check,
            logo=settings.logo,
            telegram_chat_id=settings.values.get(TELEGRAM_CHAT_ID_KEY, ""),
        )


@dataclass(frozen=True)
class Row:
    """One setting, or one action. See the module docstring for how to add one.

    A row either *persists* (``key`` + ``cycle``) or *acts* (``action``, with
    an empty ``key``): the Skills and Doctor rows run something and write no
    setting at all. Doctor's ``action`` is only for the numbered fallback —
    the TUI expands its report in the panel on highlight instead. Everything
    else — numbering, the value column, bounds, dispatch — is identical
    either way, which is the whole point of keeping the menu a list of data
    rather than a switch statement.
    """

    key: str
    group: tuple[str, str] | None
    label: tuple[str, str]
    help: tuple[str, str]
    value: Callable[[MenuState], str]
    accent: Callable[[MenuState], str]
    cycle: Callable[[MenuState, int], str] | None = None
    #: ``(state, stdin, stdout)``; a ``returns_to_panel`` action returns
    #: ``(result line(s), rows printed)`` — see :func:`_export_action`.
    #: Must never block on a non-TTY stdin — ``read_line`` returns ``None``
    #: at EOF and every prompt here reads that as "no", because aicp runs
    #: in CI.
    action: Callable[[MenuState, IO[str], IO[str]], tuple[list[str], int] | None] | None = None
    #: An action row whose prompt+result is a one-shot outcome rather than a
    #: report meant to be read (Export/Import; Agents has its own bespoke
    #: sub-panel and never sets this) — the arrow-key TUI erases the row's
    #: own output plus the stale parent panel behind it, then redraws one
    #: fresh panel with the action's returned message(s) as its note,
    #: instead of stacking a new panel below what was just printed. The
    #: numbered fallback ignores this; it always scrolls, by design.
    returns_to_panel: bool = False
    #: A row whose value is typed rather than cycled — see :class:`TextEdit`.
    text_edit: TextEdit | None = None


@dataclass(frozen=True)
class TextEdit:
    """A typed value. The arrow-key TUI edits it in the row itself, the way
    codex-reset-watch's ``crw --config`` does; the numbered fallback prompts
    for a line. ``commit`` gets a non-blank answer (``-`` means clear) and
    returns its result line, or raises ``ValueError`` to reject it."""

    #: What an opened field starts with.
    seed: Callable[[MenuState], str]
    commit: Callable[[MenuState, str], str]
    #: The numbered fallback's prompt, current value included.
    prompt: Callable[[MenuState], str]
    #: Typed as bullets, and never prefilled — see :func:`_edit_inline`.
    secret: bool = False
    #: Longest value this field accepts — a paste-flood/held-key backstop,
    #: not a format check. ``None`` means uncapped.
    max_len: int | None = None


def _on_off(state: MenuState, flag: bool) -> str:
    return _t(state.lang, "config_on", "On") if flag else _t(state.lang, "config_off", "Off")


def _toggle_commit(state: MenuState, _direction: int) -> str:
    state.do_commit = not state.do_commit
    return "1" if state.do_commit else "0"


def _toggle_push(state: MenuState, _direction: int) -> str:
    state.do_push = not state.do_push
    return "1" if state.do_push else "0"


def _toggle_update_check(state: MenuState, _direction: int) -> str:
    state.update_check = not state.update_check
    return "1" if state.update_check else "0"


def _rotate_logo(state: MenuState, direction: int) -> str:
    state.logo = LOGO_MODES[(LOGO_MODES.index(state.logo) + direction) % len(LOGO_MODES)]
    return state.logo


def _logo_value(state: MenuState) -> str:
    names = {
        "color": ("config_logo_color", "Color"),
        "mono": ("config_logo_mono", "Mono"),
        "animated": ("config_logo_animated", "Animated"),
        "off": ("config_off", "Off"),
    }
    return _t(state.lang, *names[state.logo])


def _toggle_lang(state: MenuState, _direction: int) -> str:
    state.lang = "zh-TW" if state.lang == "en" else "en"
    return state.lang


def _rotate_chain(state: MenuState, direction: int) -> str:
    """Rotation, not a swap: ``--swap-ai`` trades the pick with whoever holds
    #1, which is right for "jump straight to this one" and wrong for an arrow
    key, where pressing → down the list should walk the whole chain and come
    back to where it started rather than ping-pong between two names."""
    if direction > 0:
        state.chain = [*state.chain[1:], state.chain[0]]
    else:
        state.chain = [state.chain[-1], *state.chain[:-1]]
    return " ".join(state.chain)


# ── Skills and Doctor: the two rows that act instead of persisting ───────────

_WARN = "⚠"
_OK = "✓"
_SKIP = "·"


def _ask(state: MenuState, stdin: IO[str], out: IO[str], msgid: str, english: str, *args: object) -> bool:
    """A yes/no prompt that defaults to NO — including at EOF.

    ``read_line`` returns ``None`` on a dead or empty pipe, so an unattended
    run answers "no" to everything instead of blocking: the safe default for
    both questions asked here (install nothing, overwrite nothing).
    """
    print(_t(state.lang, msgid, english, *args), file=out)
    answer = read_line(stdin)
    return answer is not None and answer.strip().lower() in ("y", "yes")


def _skill_states(state: MenuState) -> list[skills.SkillStatus]:
    if state.skills_status is None:
        state.skills_status = skills.full_status()
    return state.skills_status


def _skills_value(state: MenuState) -> str:
    """The Skills row's inline status, e.g. ``⚠ codex missing``."""
    live = [s for s in _skill_states(state) if s.state != skills.NOT_INSTALLED]
    if not live:
        return _t(state.lang, "skills_none", "no CLI configured")
    missing = sorted({s.cli for s in live if s.state == skills.MISSING})
    older = sorted({s.cli for s in live if s.state == skills.OURS_OLDER})
    if missing:
        return _t(state.lang, "skills_missing", f"{_WARN} %s missing", ", ".join(missing))
    if older:
        return _t(state.lang, "skills_older", f"{_WARN} %s outdated", ", ".join(older))
    if any(s.state == skills.FOREIGN for s in live):
        return _t(state.lang, "skills_yours", f"{_OK} yours, kept")
    return _t(state.lang, "skills_ok", f"{_OK} installed")


def _skills_accent(state: MenuState) -> str:
    return YELLOW if _WARN in _skills_value(state) else GREEN


def _report(state: MenuState, results: list[skills.InstallResult], out: IO[str]) -> None:
    """Say what actually happened — and never call a kept file a failure."""
    texts = {
        skills.INSTALLED: ("skills_installed", "%s installed into %s"),
        skills.UPGRADED: ("skills_upgraded", "%s upgraded for %s"),
        skills.KEPT: ("skills_kept", "%s: kept your own copy — aicp will use it"),
    }
    for result in results:
        if result.action == skills.OVERWRITTEN:
            print(
                f"  {GREEN}{_OK}{RESET} "
                + _t(
                    state.lang,
                    "skills_overwritten",
                    "%s replaced for %s — your copy is at %s",
                    result.skill,
                    result.cli,
                    result.backup,
                ),
                file=out,
            )
        elif result.action in texts:
            msgid, english = texts[result.action]
            args = (
                (result.skill, result.cli)
                if result.action != skills.KEPT
                else (f"{result.cli}/{result.skill}",)
            )
            print(f"  {GREEN}{_OK}{RESET} " + _t(state.lang, msgid, english, *args), file=out)
    # The install just changed what is on disk, so both rows' cached probes
    # are stale — including the Doctor row's, which reports on skills too.
    state.skills_status = None
    state.health = None


def _skills_action(state: MenuState, stdin: IO[str], out: IO[str]) -> None:
    """Per-CLI status, then the installer's own two questions.

    Both default to no. The second one only exists because a same-name skill
    with no aicp sidecar is the user's own file: aicp keeps it, and says so
    as the good outcome it is.
    """
    every = _skill_states(state)
    live = [s for s in every if s.state != skills.NOT_INSTALLED]
    labels = {
        skills.MISSING: (_WARN, YELLOW, "skills_state_missing", "not installed yet"),
        skills.FOREIGN: (_OK, GREEN, "skills_state_foreign", "your own file — aicp will use it"),
    }
    print(file=out)
    # The Skills row's own label, on purpose: one word, one msgid. A second id
    # for the same English is how a report and the row it belongs to drift.
    print(_t(state.lang, "config_skills", "Skills"), file=out)
    for status in live:
        if status.state == skills.CURRENT:
            mark, color, text = _OK, GREEN, _t(state.lang, "skills_state_current", "installed (%s)", status.version)
        elif status.state == skills.OURS_OLDER:
            mark, color, text = _WARN, YELLOW, _t(
                state.lang, "skills_state_older", "from an older aicp (%s) — can be upgraded", status.version
            )
        else:
            mark, color, msgid, english = labels[status.state]
            text = _t(state.lang, msgid, english)
        print(f"  {color}{mark}{RESET} {status.cli:<8} {status.skill:<14} {text}", file=out)
    # One line for every CLI that has no config dir, rather than two rows
    # each: they are not a problem to solve, and on a six-CLI roster they
    # would otherwise be most of the list.
    skipped = sorted({s.cli for s in every if s.state == skills.NOT_INSTALLED})
    if skipped:
        print(
            f"  {DIM}{_SKIP} "
            + _t(state.lang, "skills_skipped", "not installed here: %s", ", ".join(skipped))
            + f"{RESET}",
            file=out,
        )
    print(file=out)

    stale = [s for s in live if s.state in (skills.MISSING, skills.OURS_OLDER)]
    foreign = [s for s in live if s.state == skills.FOREIGN]
    if stale and _ask(
        state, stdin, out, "skills_install_q", "Install/upgrade %s skill(s)? [y/N]: ", len(stale)
    ):
        _report(state, skills.install(), out)
    if foreign:
        print(
            f"{DIM}"
            + _t(
                state.lang,
                "skills_keep_note",
                "Keeping your own %s skill(s) — aicp will use them, which is fine.",
                len(foreign),
            )
            + f"{RESET}",
            file=out,
        )
        if _ask(
            state,
            stdin,
            out,
            "skills_force_q",
            "Replace them with aicp's copies? Yours are backed up first [y/N]: ",
        ):
            _report(state, skills.install(force=True), out)
    if not stale and not foreign:
        print(f"{DIM}" + _t(state.lang, "skills_nothing", "Nothing to do.") + f"{RESET}", file=out)
    print(file=out)


def _agent_rows(state: MenuState) -> list[agents.AgentRow]:
    if state.agent_rows is None:
        state.agent_rows = agents.inventory()
    return state.agent_rows


def _agents_value(state: MenuState) -> str:
    off = sum(1 for row in _agent_rows(state) if row.state == agents.DISABLED)
    live = len(_agent_rows(state)) - off
    if off:
        return _t(state.lang, "agents_count_off", f"{_OK} %s active · %s off", live, off)
    return _t(state.lang, "agents_count", f"{_OK} %s active", live)


def _agents_action(state: MenuState, stdin: IO[str], out: IO[str]) -> None:
    """The registry, then one question: which agent to turn on or off.

    Adding an agent needs six fields, which is a form, not a menu row — the
    footer points at ``aicp --agents set`` for that. Toggling is the part
    worth an arrow key, and the part that has to stay in step with the saved
    CLI order, which :func:`aicp.agentcfg.apply` keeps for us.
    """
    rows = _agent_rows(state)
    print(file=out)
    print(_t(state.lang, "config_agents", "Agents"), file=out)  # the row's own label
    for i, row in enumerate(rows, start=1):
        mark, color = (
            (_SKIP, DIM) if row.state == agents.DISABLED else (_OK, GREEN)
        )
        label = _t(state.lang, *agentcfg.STATE_LABELS[row.state])
        print(
            f"  {i}) {color}{mark}{RESET} {row.name:<10} {row.executable:<22} {DIM}{label}{RESET}",
            file=out,
        )
    print(file=out)
    print(
        _t(state.lang, "agents_toggle_q", "Turn which one on/off? [1-%s, ⏎ to skip]: ", len(rows)),
        file=out,
    )
    choice = read_line(stdin)
    if choice is None or choice == "":
        print(
            f"{DIM}"
            + _t(
                state.lang,
                "agents_edit_hint",
                "  add or edit one with: aicp --agents set <name> executable=… config_dir=…",
            )
            + f"{RESET}",
            file=out,
        )
        print(file=out)
        return
    if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
        print(f"{RED}{_t(state.lang, 'swap_invalid', '✗ invalid choice: %s', choice)}{RESET}", file=out)
        print(file=out)
        return

    picked = rows[int(choice) - 1]
    verb = "enable" if picked.state == agents.DISABLED else "disable"
    try:
        change = agentcfg.apply(verb, picked.name, config_path=state.path)
    except agentcfg.AgentEditError as exc:
        print(f"{RED}✗ {exc}{RESET}", file=out)
        print(file=out)
        return

    print(
        f"  {GREEN}"
        + (
            _t(state.lang, "agents_done_enabled", "✓ %s enabled", change.name)
            if change.verb == "enabled"
            else _t(state.lang, "agents_done_disabled", "✓ %s disabled", change.name)
        )
        + RESET,
        file=out,
    )
    if change.pruned:
        print(
            f"{DIM}"
            + _t(
                state.lang,
                "agents_pruned",
                "  dropped from the saved CLI order: %s",
                " ".join(change.pruned),
            )
            + f"{RESET}",
            file=out,
        )
    # The roster this session was built from just changed, so every cached
    # view of it is stale — including the chain, which the AI CLI order row
    # would otherwise persist with the name that was just removed still in it.
    state.chain = [name for name in state.chain if name in change.names] + [
        name for name in change.names if name not in state.chain
    ]
    state.agent_rows = None
    state.skills_status = None
    state.health = None
    print(
        f"{DIM}" + _t(state.lang, "agents_next_run", "  the next aicp run uses it") + f"{RESET}",
        file=out,
    )
    print(file=out)


def _agents_lines(state: MenuState, selected: int, message: str | None = None) -> list[str]:
    """The Agents sub-panel: one row per registry entry, cursor-navigable
    exactly like the top-level panel — this is a second small ``_panel``, not
    a report, because a toggle here is meant to be watched happening rather
    than read off a printed list afterwards. Sized to its own content (a
    short roster, not the terminal width) and stable across keypresses: the
    row text never changes length as the cursor moves, only which one is
    bold. The executable column is padded to its own widest entry, and the
    state label to the widest of all *possible* states (not just the ones
    currently in play) — so toggling a row's state can never widen or narrow
    the panel that toggle is being watched in. The fixed-width-column trick
    :func:`render_table` gets for free and a two-column ``(label, value)``
    panel does not.
    """
    rows = _ordered_agents(state)
    exec_w = max((width(row.executable) for row in rows), default=0)
    state_w = max(width(_t(state.lang, *msg)) for msg in agentcfg.STATE_LABELS.values())
    body: list[tuple[str, str]] = []
    for i, row in enumerate(rows, start=1):
        marker = "›" if selected == i else " "
        mark, color = (_SKIP, DIM) if row.state == agents.DISABLED else (_OK, GREEN)
        label = f"{marker} {i}) {row.name}"
        if selected == i:
            label = f"{RESET}{BOLD}{label}{RESET}"
        state_label = _t(state.lang, *agentcfg.STATE_LABELS[row.state])
        pad = " " * (exec_w - width(row.executable))
        state_pad = " " * (state_w - width(state_label))
        # A fixed two-column slot, so moving #1 never re-measures the panel.
        first = f"{CYAN}#1{RESET}" if i == 1 and row.state != agents.DISABLED else "  "
        value = (
            f"{color}{mark}{RESET} {row.executable}{pad}  {DIM}{state_label}{state_pad}{RESET}  {first}"
        )
        body.append((label, value))
    notes = [message] if message else []
    notes += [
        f"{DIM}"
        + _t(state.lang, "agents_tui_keys", "↑↓ select · ←→ move · t to #1 · space on/off")
        + f"{RESET}",
        f"{DIM}"
        + _t(state.lang, "agents_tui_keys2", "e edit · a add · r reset · ⏎ actions · q/Ctrl+C back · auto-save")
        + f"{RESET}",
    ]
    return render_panel(body, _t(state.lang, "config_agents", "Agents"), CYAN, notes=notes)


#: The Agents sub-panel's actions: the key each one dispatches, the hotkey
#: shown beside it, its label. The ⏎ menu is drawn from this table and
#: dispatches through the same keys, so menu and hotkeys cannot drift apart.
AGENT_ACTIONS: tuple[tuple[str, str, tuple[str, str]], ...] = (
    ("top", "t", ("agents_act_top", "Move to #1")),
    ("left", "←", ("agents_act_up", "Move up")),
    ("right", "→", ("agents_act_down", "Move down")),
    ("space", "space", ("agents_act_toggle", "Turn on/off")),
    ("edit", "e", ("agents_act_edit", "Edit settings…")),
    ("reset", "r", ("agents_act_reset", "Reset to built-in")),
)
_AGENT_KEYS = {key for key, _hint, _label in AGENT_ACTIONS} | {"add"}


def _ordered_agents(state: MenuState) -> list[agents.AgentRow]:
    """Try order, top to bottom: the chain first, disabled agents last."""
    rank = {name: i for i, name in enumerate(state.chain)}
    return sorted(
        _agent_rows(state),
        key=lambda row: (row.state == agents.DISABLED, rank.get(row.name, len(rank))),
    )


def _agent_actions_lines(state: MenuState, name: str, selected: int) -> list[str]:
    body = []
    for i, (_key, hint, label) in enumerate(AGENT_ACTIONS, start=1):
        text = f"{'›' if i == selected else ' '} {_t(state.lang, *label)}"
        if i == selected:
            text = f"{RESET}{BOLD}{text}{RESET}"
        body.append((text, f"{DIM}{hint}{RESET}"))
    notes = [f"{DIM}" + _t(state.lang, "agents_act_keys", "↑↓ select · ⏎ run · q cancel") + f"{RESET}"]
    title = f"{_t(state.lang, 'config_agents', 'Agents')} ▸ {name}"
    return render_panel(body, title, CYAN, notes=notes)


def _agent_action_menu(state: MenuState, name: str, stdin: IO[str], out: IO[str]) -> str | None:
    """⏎ on an agent: pick from :data:`AGENT_ACTIONS`. Returns the picked
    action's key (a hotkey pressed here counts too), or None on q. Erases
    itself before returning."""
    selected = 1
    lines = _agent_actions_lines(state, name, selected)
    for line in lines:
        print(line, file=out)
    while True:
        key = read_key(stdin, out)
        if key == "up":
            selected = selected - 1 if selected > 1 else len(AGENT_ACTIONS)
        elif key == "down":
            selected = selected + 1 if selected < len(AGENT_ACTIONS) else 1
        elif key == "enter" or key == "quit" or key in _AGENT_KEYS:
            out.write(f"\033[{_frame_rows(lines, out)}A\033[J")
            if key == "enter":
                return AGENT_ACTIONS[selected - 1][0]
            return None if key == "quit" else key
        else:
            continue
        out.write(f"\033[{_frame_rows(lines, out)}A\033[J")
        lines = _agent_actions_lines(state, name, selected)
        for line in lines:
            print(line, file=out)


def _agent_form(
    state: MenuState,
    fields: Sequence[str],
    current: dict[str, str] | None,
    stdin: IO[str],
    out: IO[str],
    taken: Collection[str] = (),
) -> tuple[dict[str, str] | None, int]:
    """One typed line per field. With *current*, ⏎ keeps a value and only the
    fields that changed come back; without it every field but
    ``config_dir_env`` is required and is asked again until answered — as is
    a ``name`` that is malformed or already in *taken*.
    Returns ``(answers, rows printed)``; answers is None when EOF or Ctrl+C
    cut the form off, so nothing half-typed is ever applied."""
    answers: dict[str, str] = {}
    printed = 0
    for field in fields:
        optional = current is not None or field == "config_dir_env"
        while True:
            if current is not None:
                hint = f" {DIM}[{current.get(field, '')}]{RESET}"
            elif optional:
                hint = f" {DIM}({_t(state.lang, 'agents_form_optional', 'optional')}){RESET}"
            else:
                hint = ""
            answer, rows = _prompt_line(f"  {field:<14}{hint}: ", stdin, out)
            printed += rows
            if answer is None:
                return None, printed
            if field == "name" and answer:
                # Refused here, before six more fields are typed for nothing.
                if answer in taken:
                    problem = _t(state.lang, "agents_add_exists", "✗ %s already exists — select it and press e", answer)
                elif not agentcfg.NAME.fullmatch(answer):
                    problem = _t(state.lang, "agents_bad_name", "✗ use letters, digits, . _ - only")
                else:
                    break
                print(f"  {RED}{problem}{RESET}", file=out)
                printed += _frame_rows([f"  {problem}"], out)
                continue
            if answer or optional:
                break
        if answer and (current is None or answer != current.get(field)):
            answers[field] = answer
    return answers, printed


def _agent_values(name: str) -> dict[str, str]:
    """*name*'s effective fields as the form shows them: lists comma-joined."""
    path = agents.user_registry_path()
    merged = agents.merge_entries(
        agents.load_agents(agents.BUILTIN_PATH), agents.read_user_entries(path), path
    )
    agent = merged[name]
    values = {}
    for field in agentcfg.EDITABLE:
        value = getattr(agent, field)
        values[field] = ",".join(value) if isinstance(value, tuple) else value
    return values


def _apply_agent(state: MenuState, verb: str, name: str, assignments: Sequence[str] = ()) -> str:
    """Run one :func:`agentcfg.apply` edit and resync the session with it."""
    try:
        change = agentcfg.apply(verb, name, assignments, config_path=state.path)
    except agentcfg.AgentEditError as exc:
        return f"{RED}✗ {exc}{RESET}"
    # The roster this session was built from just changed, so every cached
    # view of it is stale — including the chain, which would otherwise be
    # persisted by the next move with a name that no longer resolves.
    state.chain = [n for n in state.chain if n in change.names] + [
        n for n in change.names if n not in state.chain
    ]
    state.agent_rows = None
    state.skills_status = None
    state.health = None
    message = f"{GREEN}{_t(state.lang, *agentcfg.DONE_LABELS[change.verb], change.name)}{RESET}"
    if change.pruned:
        message += f"{DIM} · " + _t(
            state.lang, "agents_pruned", "  dropped from the saved CLI order: %s", " ".join(change.pruned)
        ).strip() + RESET
    return message


def _move_agent(state: MenuState, name: str, key: str) -> str | None:
    """←/→ swap *name* with its neighbour, ``top`` with #1; saved at once.
    A disabled agent has no place in the chain, so it does not move."""
    if name not in state.chain:
        return None
    i = state.chain.index(name)
    j = 0 if key == "top" else i - 1 if key == "left" else i + 1
    if i == j or not 0 <= j < len(state.chain):
        return None
    chain = state.chain
    chain[i], chain[j] = chain[j], chain[i]
    if persist_key("AICP_CLI_ORDER", " ".join(chain), state.path):
        state.health = None
        return None
    chain[i], chain[j] = chain[j], chain[i]
    return f"{RED}{_t(state.lang, 'persist_failed', '✗ failed to write %s', state.path)}{RESET}"


def _agent_act(
    state: MenuState,
    key: str | None,
    row: agents.AgentRow,
    stdin: IO[str],
    out: IO[str],
    typed: Callable[[], contextlib.AbstractContextManager[None]],
) -> tuple[str | None, int, str]:
    """Do what *key* means for *row*. Returns ``(message, rows printed below
    the frame, name the cursor should land on)``."""
    lang = state.lang
    if key in ("top", "left", "right"):
        return _move_agent(state, row.name, key), 0, row.name
    if key == "space":
        verb = "enable" if row.state == agents.DISABLED else "disable"
        return _apply_agent(state, verb, row.name), 0, row.name
    if key == "edit":
        if row.state == agents.DISABLED:
            return _t(lang, "agents_edit_disabled", "✗ %s is off — turn it on first (space)", row.name), 0, row.name
        try:
            current = _agent_values(row.name)
        except (OSError, ValueError, TypeError) as exc:
            return f"{RED}✗ {exc}{RESET}", 0, row.name
        print(_t(lang, "agents_form_edit", "Edit %s — ⏎ keeps a value · Ctrl+C cancels", row.name), file=out)
        with typed():
            answers, printed = _agent_form(state, agentcfg.EDITABLE, current, stdin, out)
        printed += 1
        if answers is None:
            return None, printed, row.name
        if not answers:
            return f"{DIM}" + _t(lang, "agents_form_unchanged", "nothing changed") + RESET, printed, row.name
        assignments = [f"{field}={value}" for field, value in answers.items()]
        return _apply_agent(state, "set", row.name, assignments), printed, row.name
    if key == "add":
        print(
            _t(lang, "agents_form_add", "Add an AI CLI — ⏎ accepts a guessed default · skills and args are comma-separated · Ctrl+C cancels"),
            file=out,
        )
        taken = {r.name for r in _agent_rows(state)}
        with typed():
            name_answer, printed = _agent_form(state, ("name",), None, stdin, out, taken)
            if name_answer is not None:
                # Probed from disk once the name is known, so the rest of the
                # form can be accepted with ⏎ like an edit — see guess_defaults.
                guessed = agentcfg.guess_defaults(name_answer["name"])
                answers, more = _agent_form(state, agentcfg.EDITABLE, guessed, stdin, out)
                printed += more
        printed += 1
        if name_answer is None or answers is None:
            return None, printed, row.name
        name = name_answer["name"]
        merged = {**guessed, **answers}
        assignments = [f"{field}={value}" for field, value in merged.items()]
        return _apply_agent(state, "set", name, assignments), printed, name
    if key == "reset":
        if row.state == agents.BUILT_IN:
            message = _t(lang, "agents_nothing_to_reset", "%s has no override — nothing to reset", row.name)
            return f"{DIM}{message}{RESET}", 0, row.name
        if row.name in agents.load_agents(agents.BUILTIN_PATH):
            question = _t(lang, "agents_reset_q", "Reset %s to its built-in definition? [y/N]: ", row.name)
        else:
            question = _t(lang, "agents_remove_q", "Remove %s? [y/N]: ", row.name)
        with typed():
            answer, printed = _prompt_line(question, stdin, out)
        if answer is None or answer.lower() not in ("y", "yes"):
            return None, printed, row.name
        return _apply_agent(state, "reset", row.name), printed, row.name
    return None, 0, row.name


def _agents_tui(
    state: MenuState,
    stdin: IO[str],
    out: IO[str],
    typed: Callable[[], contextlib.AbstractContextManager[None]] = contextlib.nullcontext,
) -> list[str]:
    """Arrow-key loop opened by the Agents row, listed in try order: ↑↓
    selects, the :data:`AGENT_ACTIONS` hotkeys (and ``a``) act on the
    highlighted agent — saved immediately, same as every other row — ⏎ opens
    the same actions as a menu, q leaves. Returns the last frame it drew;
    the caller must erase that many rows *plus* the parent panel still
    sitting above it, then redraw the parent once — erasing only this frame
    leaves the old parent on screen and the next Enter stacks another copy.
    """
    selected = 1
    lines = _agents_lines(state, selected)
    for line in lines:
        print(line, file=out)
    #: Rows of ours on screen below the parent: the frame, plus whatever a
    #: form or question printed under it — all erased before the redraw.
    shown = _frame_rows(lines, out)
    while True:
        key = read_key(stdin, out)
        if key == "quit":
            return lines
        if key == "reset_all":
            key = "reset"  # In the Agents submenu, R retains r's row-reset action.
        message: str | None = None
        rows = _ordered_agents(state)
        if key == "up":
            selected = selected - 1 if selected > 1 else len(rows)
        elif key == "down":
            selected = selected + 1 if selected < len(rows) else 1
        elif key == "enter" or key in _AGENT_KEYS:
            picked = rows[selected - 1]
            if key == "enter":
                # The menu draws where the frame was, so the frame is gone.
                out.write(f"\033[{shown}A\033[J")
                shown = 0
                key = _agent_action_menu(state, picked.name, stdin, out)
            message, printed, focus = _agent_act(state, key, picked, stdin, out, typed)
            shown += printed
            rows = _ordered_agents(state)
            selected = next(
                (i for i, row in enumerate(rows, start=1) if row.name == focus),
                min(selected, len(rows)),
            )
        else:
            continue
        if shown:
            out.write(f"\033[{shown}A\033[J")
        lines = _agents_lines(state, selected, message)
        for line in lines:
            print(line, file=out)
        shown = _frame_rows(lines, out)


def _config_health(state: MenuState) -> list[tuple[str, str]]:
    """Which ``config.json`` keys the loader refused — otherwise entirely
    silent.

    Derived from :func:`~aicp.config.load_config`'s own verdict rather than a
    second copy of its rules: a key that is in the file and not in what it
    returned is, by definition, a key that did not take effect.
    """
    if not state.path.exists():
        return [(_OK, _t(state.lang, "health_no_config", "no %s yet — built-in defaults apply", state.path))]
    raw = _read_json_object(state.path)
    # The loader announces a denied key on stderr as it goes; this report is
    # about to say the same thing in the panel, so the second copy is noise —
    # and the panel repaints, which would print it again on every keypress.
    with contextlib.redirect_stderr(io.StringIO()):
        accepted = load_config(state.path)
    refused = [key for key in raw if _KEY_RE.match(key) and key not in accepted]
    if not refused:
        return [(_OK, _t(state.lang, "health_config_ok", "%s: %s setting(s) read", state.path.name, len(accepted)))]
    return [
        (
            _WARN,
            _t(
                state.lang,
                "health_env_only",
                "%s is ignored in %s — that knob is environment/PATH only",
                key,
                state.path.name,
            )
            if key in DENYLIST or key in _SYSTEM_RESOLVED
            else _t(
                state.lang,
                "health_dropped",
                "%s was dropped from %s — its value has characters that are not allowed",
                key,
                state.path.name,
            ),
        )
        for key in refused
    ]


def _skills_health(state: MenuState) -> list[tuple[str, str]]:
    live = [s for s in _skill_states(state) if s.state != skills.NOT_INSTALLED]
    if not live:
        return [
            (_OK, _t(state.lang, "health_skills_none", "no AI CLI config dir found — nothing to install skills into"))
        ]
    lines = [
        (
            _WARN if status.state in (skills.MISSING, skills.OURS_OLDER) else _OK,
            _t(
                state.lang,
                {
                    skills.MISSING: "health_skill_missing",
                    skills.OURS_OLDER: "health_skill_older",
                    skills.FOREIGN: "health_skill_foreign",
                }[status.state],
                {
                    skills.MISSING: "%s: the %s skill is not installed — the Skills row installs it",
                    skills.OURS_OLDER: "%s: the %s skill is from an older aicp — the Skills row upgrades it",
                    skills.FOREIGN: "%s: the %s skill is your own — aicp will use it",
                }[status.state],
                status.cli,
                status.skill,
            ),
        )
        for status in live
        if status.state != skills.CURRENT
    ]
    return lines or [(_OK, _t(state.lang, "health_skills_ok", "skills are installed for every configured CLI"))]


def _git_health(state: MenuState) -> list[tuple[str, str]]:
    branch = gitflow.current_branch()
    if branch is None:
        return [
            (
                _WARN,
                _t(
                    state.lang,
                    "health_no_branch",
                    "not on a branch here (detached HEAD, or not a git repo) — aicp needs one to push",
                ),
            )
        ]
    # gitflow._out is this project's one git-subprocess call site (it already
    # swallows a missing git and a vanished cwd); a second copy here would be
    # the same three lines with worse error handling.
    if not gitflow._out(None, "remote"):
        return [
            (_WARN, _t(state.lang, "health_no_remote", "this repo has no git remote — nothing to push to"))
        ]
    return [(_OK, _t(state.lang, "health_git_ok", "on %s, remote %s", branch, gitflow.remote_for(branch)))]


def _health(state: MenuState) -> list[tuple[str, str]]:
    """Everything that is otherwise silent, as ``(mark, message)`` pairs."""
    if state.health is None:
        binary = timeout_bin()
        state.health = [
            (_OK, _t(state.lang, "health_timeout_ok", "per-CLI timeout uses %s", binary))
            if binary
            else (
                _WARN,
                _t(
                    state.lang,
                    "health_timeout_missing",
                    "no timeout/gtimeout on PATH — each AI CLI call runs with no time limit",
                ),
            ),
            *_config_health(state),
            *_skills_health(state),
            *_git_health(state),
        ]
    return state.health


def _doctor_value(state: MenuState) -> str:
    """The Doctor row's inline status, e.g. ``⚠ 3 warnings``."""
    count = sum(1 for mark, _ in _health(state) if mark == _WARN)
    if not count:
        return _t(state.lang, "doctor_clear", f"{_OK} all clear")
    if count == 1:
        return _t(state.lang, "doctor_one", f"{_WARN} 1 warning")
    return _t(state.lang, "doctor_many", f"{_WARN} %s warnings", count)


def _is_doctor(row: Row) -> bool:
    return row.label[0] == "config_doctor"


def _doctor_detail_notes(state: MenuState, budget: int) -> list[str]:
    """Full health-report lines, fitted to *budget* so selecting Doctor cannot
    widen the panel past the help-text reserve every other row already sized
    for (see ``test_selecting_a_different_row_does_not_resize_the_panel``)."""
    lines = _health(state)
    notes: list[str] = []
    for mark, text in lines:
        color = YELLOW if mark == _WARN else GREEN
        body_budget = max(budget - width(f"{mark} "), 1)
        notes.append(f"{color}{mark}{RESET} {_fit(text, body_budget)}")
    if any(mark == _WARN for mark, _ in lines):
        notes.append(
            f"{DIM}"
            + _fit(
                _t(
                    state.lang,
                    "health_footer",
                    "Warnings are things to know about, not failures — aicp runs either way.",
                ),
                budget,
            )
            + f"{RESET}"
        )
    return notes


def _doctor_action(state: MenuState, _stdin: IO[str], out: IO[str]) -> None:
    """Print the report for the numbered fallback.

    The arrow-key TUI expands the same lines inside the panel when Doctor is
    selected, so it never calls this. Asks nothing — nothing to block on.
    """
    lines = _health(state)
    print(file=out)
    print(_t(state.lang, "config_doctor", "Health check"), file=out)  # the row's own label
    for mark, text in lines:
        print(f"  {YELLOW if mark == _WARN else GREEN}{mark}{RESET} {text}", file=out)
    if any(mark == _WARN for mark, _ in lines):
        print(
            f"{DIM}"
            + _t(
                state.lang,
                "health_footer",
                "Warnings are things to know about, not failures — aicp runs either way.",
            )
            + f"{RESET}",
            file=out,
        )
    print(file=out)


def _export_target(text: str) -> Path:
    """Where an export lands: a folder gets an auto-named file inside it, and
    a bare name gets ``.json`` — nobody should have to know the format."""
    path = typed_path(text)
    if path.is_dir() or text.rstrip("'\"").endswith(("/", os.sep)):
        return path / f"aicp-settings-{datetime.datetime.now().astimezone().date().isoformat()}.json"
    return path if path.suffix else path.with_suffix(".json")


def _prompt_line(prompt: str, stdin: IO[str], out: IO[str]) -> tuple[str | None, int]:
    """Print *prompt* and read the line typed after it — every prompt a
    ``--config`` sub-screen asks goes through here.

    Returns ``(answer, rows printed)``; answer is None on EOF or Ctrl+C. The
    rows include the terminal's own echo of the answer (or ``^C``), which
    never passes through *out* — counting only what was printed leaves the
    erase one row short per prompt, and the next redraw stacks a copy of the
    panel's top border on the leftover row.
    """
    print(prompt, end="", file=out)
    out.flush()
    try:
        answer = read_line(stdin)
    except KeyboardInterrupt:
        answer, echo = None, "^C"
    else:
        echo = answer or ""
    if answer is None:
        print(file=out)  # neither EOF nor ^C moves off the prompt's line
    return answer, _frame_rows([prompt + echo], out)


def _ask_path(prompt: str, stdin: IO[str], out: IO[str]) -> tuple[str | None, int]:
    """:func:`_prompt_line` until a non-empty line comes back, after one blank
    separator row. Only Ctrl+C (or EOF) cancels — a stray ⏎ re-asks rather
    than silently backing out."""
    print(file=out)
    rows = 1
    while True:
        text, asked = _prompt_line(prompt, stdin, out)
        rows += asked
        if text is None or text:
            return text, rows


def _settle(out: IO[str], rows: int, messages: list[str]) -> tuple[list[str], int]:
    """Print a ``returns_to_panel`` action's result line(s) and closing blank
    row, and return them with the action's total row count."""
    for message in messages:
        print(message, file=out)
    print(file=out)
    return messages, rows + _frame_rows(messages, out) + 1


def _export_action(state: MenuState, stdin: IO[str], out: IO[str]) -> tuple[list[str], int]:
    """Save the current settings to a file.

    No secret ever passes through this: this layer's config.json holds only
    timeouts, toggles and the CLI order — nothing a keychain would guard —
    so :func:`~aicp.config.export_payload` needs no filtering.

    Returns ``(result line(s), rows printed)`` (no lines when cancelled) —
    the numbered fallback shows the lines inline like every other action,
    and the arrow-key TUI erases exactly that many rows, then reuses the
    same strings as the panel note it settles on instead of guessing one out
    of whatever scrolled past.
    """
    text, rows = _ask_path(
        _t(
            state.lang,
            "settings_export_q",
            "Save to? Paste a folder (file is named for you) or a file name [Ctrl+C to cancel]: ",
        ),
        stdin,
        out,
    )
    messages: list[str] = []
    if text:
        target = _export_target(text)
        data = export_payload(state.path)
        if _write_json_private(target, data):
            messages.append(
                f"  {GREEN}"
                + _t(state.lang, "settings_export_done", "✓ wrote %s (%s setting(s))", target.resolve(), len(data))
                + RESET
            )
        else:
            messages.append(
                f"  {RED}" + _t(state.lang, "settings_export_failed", "✗ could not write %s", target) + RESET
            )
    return _settle(out, rows, messages)


def _import_action(state: MenuState, stdin: IO[str], out: IO[str]) -> tuple[list[str], int]:
    """Load settings from a file written by :func:`_export_action`.

    Same return contract as :func:`_export_action` — see its docstring.
    """
    text, rows = _ask_path(
        _t(state.lang, "settings_import_q", "Load which file? Paste or drag it here [Ctrl+C to cancel]: "),
        stdin,
        out,
    )
    if not text:
        return _settle(out, rows, [])
    source = typed_path(text)
    accepted, skipped = import_updates(_read_json_object(source))
    if not accepted:
        message = f"  {RED}" + _t(state.lang, "settings_import_empty", "✗ nothing importable in %s", source) + RESET
        return _settle(out, rows, [message])
    merged = _read_json_object(state.path)
    merged.update(accepted)
    if not _write_json_private(state.path, merged):
        message = (
            f"  {RED}"
            + _t(state.lang, "settings_import_failed", "✗ could not save imported settings to %s", state.path)
            + RESET
        )
        return _settle(out, rows, [message])
    # Rebuilt from the file we just wrote, exactly like the initial
    # MenuState.from_settings(resolve()) — so a toggle row an import just
    # changed repaints correctly instead of showing the pre-import value.
    refreshed = MenuState.from_settings(resolve())
    state.do_commit = refreshed.do_commit
    state.do_push = refreshed.do_push
    state.lang = refreshed.lang
    state.chain = refreshed.chain
    state.update_check = refreshed.update_check
    state.logo = refreshed.logo
    state.health = None
    messages = [
        f"  {GREEN}" + _t(state.lang, "settings_import_done", "✓ imported %s setting(s)", len(accepted)) + RESET
    ]
    if skipped:
        messages.append(
            f"  {DIM}"
            + _t(state.lang, "settings_import_skipped", "skipped: %s", ", ".join(sorted(skipped)))
            + RESET
        )
    return _settle(out, rows, messages)


# ── Telegram: the bot token and chat id ──────────────────────────────────────
# The token lives in aicp.telegram_store (the OS credential store), never in
# config.json; the chat id is ordinary configuration, stored in config.json
# as TELEGRAM_CHAT_ID_KEY — the same split codex-reset-watch makes.

#: A user/group id, or a public channel's @username.
_CHAT_ID_RE = re.compile(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{4,}")


def _saved(state: MenuState, cleared: bool) -> str:
    if cleared:
        return f"  {GREEN}{_t(state.lang, 'config_telegram_cleared', f'{_OK} cleared')}{RESET}"
    return f"  {GREEN}{_t(state.lang, 'config_telegram_saved', f'{_OK} saved')}{RESET}"


def _telegram_token_status(state: MenuState) -> tuple[str, str]:
    """``(value text, accent)``: what is stored wins over what the environment
    supplies — :func:`telegram_kit.resolve_credentials` applies the same
    precedence at send time — which wins over saying there is nowhere to put
    a new one. Probed once per session, see :attr:`MenuState.telegram_token`."""
    if state.telegram_token is None:
        env_name = telegram_kit.TOKEN_ENV
        stored = telegram_store.get(telegram_store.TOKEN_KEY)
        if stored:
            state.telegram_token = telegram_kit.mask_secret(stored), GREEN
        elif os.environ.get(env_name, "").strip():
            state.telegram_token = _t(state.lang, "config_telegram_from_env", "using $%s", env_name), CYAN
        elif not telegram_store.available():
            state.telegram_token = (
                f"{_WARN} " + _t(state.lang, "config_telegram_no_store", "no store — set $%s", env_name),
                YELLOW,
            )
        else:
            state.telegram_token = _t(state.lang, "config_telegram_not_set", "not set"), DIM
    return state.telegram_token


def _telegram_chat_id_status(state: MenuState) -> tuple[str, str]:
    if state.telegram_chat_id:
        return state.telegram_chat_id, GREEN
    env_name = telegram_kit.CHAT_ID_ENV
    if os.environ.get(env_name, "").strip():
        return _t(state.lang, "config_telegram_from_env", "using $%s", env_name), CYAN
    return _t(state.lang, "config_telegram_not_set", "not set"), DIM


def _commit_token(state: MenuState, text: str) -> str:
    if text == "-":
        telegram_store.delete(telegram_store.TOKEN_KEY)
        state.telegram_token = None
        return _saved(state, cleared=True)
    if telegram_store.set(telegram_store.TOKEN_KEY, text):
        state.telegram_token = None
        return _saved(state, cleared=False)
    return (
        f"  {RED}"
        + _t(
            state.lang,
            "config_telegram_save_failed",
            f"{_WARN} could not store securely (no credential store) — set $%s instead",
            telegram_kit.TOKEN_ENV,
        )
        + RESET
    )


def _commit_chat_id(state: MenuState, text: str) -> str:
    value = "" if text == "-" else text
    if value and not _CHAT_ID_RE.fullmatch(value):
        raise ValueError(
            _t(
                state.lang,
                "config_telegram_chat_id_invalid",
                f"{_WARN} a chat ID is a number (e.g. -1001234567890) or an @channel name",
            )
        )
    if not persist_key(TELEGRAM_CHAT_ID_KEY, value, state.path):
        return f"  {RED}{_t(state.lang, 'persist_failed', '✗ failed to write %s', state.path)}{RESET}"
    state.telegram_chat_id = value
    state.health = None  # Doctor reports on the file just written
    return _saved(state, cleared=not value)


def _token_prompt(state: MenuState) -> str:
    return _t(
        state.lang,
        "config_telegram_token_prompt",
        "Bot token [%s] (⏎ to keep, '-' to clear): ",
        _telegram_token_status(state)[0],
    )


def _chat_id_prompt(state: MenuState) -> str:
    return _t(
        state.lang,
        "config_telegram_chat_id_prompt",
        "Chat ID [%s] (⏎ to keep, '-' to clear): ",
        _telegram_chat_id_status(state)[0],
    )


def _text_edit_fallback(state: MenuState, edit: TextEdit, stdin: IO[str], out: IO[str]) -> None:
    """The numbered menu's half of a :class:`TextEdit`: ⏎ keeps the current
    value — an unrelated Enter can never wipe a real credential — ``-``
    clears it, anything else is committed."""
    print(edit.prompt(state), end="", file=out)
    out.flush()
    answer = None
    if edit.secret:
        # getpass opens /dev/tty directly on POSIX, bypassing *stdin*: this
        # only succeeds on a real controlling terminal, and degrades to None
        # immediately in CI and under the test suite.
        answer = telegram_kit.read_hidden("")
    if answer is None:
        answer = read_line(stdin)
    print(file=out)
    if not answer or not answer.strip():
        return
    text = answer.strip()
    if edit.max_len is not None:
        text = text[: edit.max_len]
    try:
        message = edit.commit(state, text)
    except ValueError as exc:
        message = f"  {RED}{exc}{RESET}"
    print(message, file=out)


#: The menu, in display order. Append to extend — see the module docstring.
ROWS: tuple[Row, ...] = (
    Row(
        key="AICP_DO_COMMIT",
        group=("config_group_steps", "Steps"),
        label=("config_do_commit", "Run the /commit step"),
        help=(
            "config_help_commit",
            "Off: never stage or commit — only push what is already committed.",
        ),
        value=lambda s: _on_off(s, s.do_commit),
        accent=lambda s: GREEN if s.do_commit else DIM,
        cycle=_toggle_commit,
    ),
    Row(
        key="AICP_DO_PUSH",
        group=None,
        label=("config_do_push", "Run the /safe-git-push step"),
        help=(
            "config_help_push",
            "Off: commit and stop, leaving the branch ahead of its remote.",
        ),
        value=lambda s: _on_off(s, s.do_push),
        accent=lambda s: GREEN if s.do_push else DIM,
        cycle=_toggle_push,
    ),
    Row(
        key="AICP_LANG",
        group=("config_group_general", "General"),
        # Named in its own script in both languages, so the row stays
        # readable to someone who just switched to a language they cannot
        # read — the one moment they most need to find this row again.
        label=("config_language", "Language"),
        help=(
            "config_help_lang",
            "Language of every message, Telegram notifications included.",
        ),
        value=lambda s: "繁體中文" if s.lang == "zh-TW" else "English",
        accent=lambda _s: CYAN,
        cycle=_toggle_lang,
    ),
    Row(
        key="AICP_UPDATE_CHECK",
        group=None,
        label=("config_update_check", "Check for updates"),
        help=(
            "config_help_update_check",
            (
                "On: checks PyPI in the background (at most every 10 minutes), "
                "asks after the command whether to upgrade when a newer aicp-cli exists."
            ),
        ),
        value=lambda s: _on_off(s, s.update_check),
        accent=lambda s: GREEN if s.update_check else DIM,
        cycle=_toggle_update_check,
    ),
    Row(
        key="AICP_LOGO",
        group=None,
        label=("config_logo", "Logo"),
        help=(
            "config_help_logo",
            "The banner above this menu: color, mono, animated (a glint sweeps once), or off.",
        ),
        value=_logo_value,
        accent=lambda s: DIM if s.logo == "off" else GREEN,
        cycle=_rotate_logo,
    ),
    Row(
        key="AICP_CLI_ORDER",
        group=None,
        label=("config_cli_order", "AI CLI order"),
        help=(
            "config_help_cli",
            (
                "Full fallback order, tried left to right; ←/→ rotates it, "
                "the Agents row moves one at a time. Missing CLIs are skipped."
            ),
        ),
        value=lambda s: _SEPARATOR.join(s.chain),
        accent=lambda _s: CYAN,
        cycle=_rotate_chain,
    ),
    Row(
        key="",
        group=("config_group_notifications", "Notifications"),
        label=("config_telegram_token", "Telegram bot token"),
        help=(
            "config_help_telegram_token",
            "Sent as this bot. Kept in the OS credential store, never in a file.",
        ),
        value=lambda s: _telegram_token_status(s)[0],
        accent=lambda s: _telegram_token_status(s)[1],
        text_edit=TextEdit(
            seed=lambda _s: "", commit=_commit_token, prompt=_token_prompt, secret=True,
            max_len=telegram_kit.MAX_TOKEN_LEN,
        ),
    ),
    Row(
        key=TELEGRAM_CHAT_ID_KEY,
        group=None,
        label=("config_telegram_chat_id", "Telegram chat ID"),
        help=(
            "config_help_telegram_chat_id",
            "Where notifications go: a number or an @channel. Stored in config.json.",
        ),
        value=lambda s: _telegram_chat_id_status(s)[0],
        accent=lambda s: _telegram_chat_id_status(s)[1],
        text_edit=TextEdit(
            seed=lambda s: s.telegram_chat_id, commit=_commit_chat_id, prompt=_chat_id_prompt,
            max_len=telegram_kit.MAX_CHAT_ID_LEN,
        ),
    ),
    # Rows, not subcommands: only `aicp` and `aicp --config` are ever meant to
    # be memorised, so skills management and the health check live here.
    Row(
        key="",
        group=("config_group_tools", "Tools"),
        label=("config_skills", "Skills"),
        help=(
            "config_help_skills",
            "Installs aicp's /commit and /safe-git-push into each AI CLI. Your own files are kept.",
        ),
        value=_skills_value,
        accent=_skills_accent,
        action=_skills_action,
    ),
    Row(
        key="",
        group=None,
        label=("config_agents", "Agents"),
        help=(
            "config_help_agents",
            "The AI CLIs aicp tries, in order. ⏎ to reorder, turn off, edit or add one.",
        ),
        value=_agents_value,
        accent=lambda _s: CYAN,
        action=_agents_action,
    ),
    Row(
        key="",
        group=None,
        label=("config_doctor", "Health check"),
        help=(
            "config_help_doctor",
            "What is otherwise silent: timeout, skipped config.json keys, skills, git remote.",
        ),
        value=_doctor_value,
        accent=lambda s: YELLOW if _WARN in _doctor_value(s) else GREEN,
        action=_doctor_action,
    ),
    Row(
        key="",
        group=None,
        label=("config_settings_export", "Export settings"),
        help=(
            "config_help_settings_export",
            "Save these settings to a file, to back them up or copy to another machine.",
        ),
        value=lambda s: _t(s.lang, "config_settings_export_value", "save to file"),
        accent=lambda _s: CYAN,
        action=_export_action,
        returns_to_panel=True,
    ),
    Row(
        key="",
        group=None,
        label=("config_settings_import", "Import settings"),
        help=(
            "config_help_settings_import",
            "Load a file saved by Export settings; settings it does not mention are kept.",
        ),
        value=lambda s: _t(s.lang, "config_settings_import_value", "load from file"),
        accent=lambda _s: CYAN,
        action=_import_action,
        returns_to_panel=True,
    ),
)
#: Row numbers are right-aligned so "9)" and "10)" keep labels in one column.
_NUM_WIDTH = len(str(len(ROWS)))


def _terminal_size(out: IO[str]) -> os.terminal_size:
    """The terminal *out* draws on; 80×24 when it is not one.

    Measured from the stream being written to rather than through
    ``shutil.get_terminal_size``: the repaint arithmetic below is only
    correct against the surface it actually draws on, and a captured stdout
    (a pipe, this project's suite) has to resolve the same way on every run
    instead of inheriting whatever terminal happened to launch it.
    """
    try:
        return os.get_terminal_size(out.fileno())
    except (AttributeError, OSError, ValueError):
        return os.terminal_size((80, 24))


def _fit(text: str, columns: int) -> str:
    """*text* clipped to *columns* visible columns, ending in … when clipped."""
    if width(text) <= columns:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        used += width(char)
        if used > columns - 1:
            break
        kept.append(char)
    return "".join(kept) + _ELLIPSIS


def _fit_columns(state: MenuState, out: IO[str]) -> tuple[int, int]:
    """``(frame, value)`` column budgets for a panel that fits this terminal.

    The panel sizes itself to its content, and that content is routinely
    wider than the 80 columns a terminal gives by default: the Chinese help
    line and the six-CLI chain put it at 87-89. A frame wider than the
    terminal is not merely ugly — every one of its lines wraps, so a frame
    with 15 lines occupies 30 rows, the repaint below walks the cursor up 15
    and lands in the middle of its own last frame, and each keypress leaves
    another half-frame behind until the screen is a column of borders. So
    the content is fitted to the terminal first and the repaint arithmetic
    stays exact.
    """
    frame = _terminal_size(out).columns - 2
    labels = [f"  {i:>{_NUM_WIDTH}}) {_t(state.lang, *row.label)}" for i, row in enumerate(ROWS, start=1)]
    labels += [_t(state.lang, *row.group) for row in ROWS if row.group]
    return frame, max(frame - 6 - max(width(label) for label in labels), _MIN_VALUE_COLUMNS)


#: Rows the panel needs beside the logo: every row and group heading, the
#: frame, and the notes that survive trimming. Below this the logo steps
#: down a tier, then disappears, rather than squeezing the panel.
_PANEL_ROWS = len(ROWS) + sum(1 for row in ROWS if row.group) + 6
#: One shimmer is ~17 frames, so ~0.5 s: a soft pass, not a flash.
_SHIMMER_FRAME_SECONDS = 0.03
#: Idle seconds between two shimmers in ``animated`` mode (the default). Each
#: pass is ~0.5 s and any keypress cuts it short.
_SHIMMER_EVERY = 5.0

#: ``(rows, indent)`` of a logo on screen; ``None`` when none was drawn.
_Drawn = tuple[tuple[str, ...], int] | None


def _logo_tier(state: MenuState, out: IO[str]) -> tuple[str, ...]:
    """The tier to draw (``()`` = none), its height recorded for the panel's trim."""
    size = _terminal_size(out)
    rows = () if state.logo == "off" else logo.pick(size.columns, size.lines, _PANEL_ROWS)
    state.logo_rows = len(rows) + 1 if rows else 0
    return rows


def _print_logo(state: MenuState, out: IO[str], rows: tuple[str, ...], panel_width: int) -> _Drawn:
    """Print *rows* centred over a panel *panel_width* wide, then a blank line.

    Never past the terminal's last safe column: the panel fits the terminal,
    but the clamp keeps that true even if it some day does not.
    """
    if not rows:
        return None
    indent = max(0, min((panel_width - len(rows[0])) // 2, _terminal_size(out).columns - 1 - len(rows[0])))
    for line in logo.paint(rows, state.logo, color_supported(out), indent=indent):
        print(line, file=out)
    print(file=out)
    return rows, indent


def _shimmer(out: IO[str], drawn: tuple[tuple[str, ...], int], below: int, stdin: IO[str]) -> None:
    """Sweep the glint across the logo sitting *below* rows above the cursor.

    Rewrites only the logo's own rows and walks back down, so the panel is
    never touched. A keypress cuts the sweep short on the at-rest frame.
    """
    rows, indent = drawn
    up = len(rows) + 1 + below
    frames = logo.frames(rows, indent)
    for i, frame in enumerate(frames):
        if i < len(frames) - 1 and pending(stdin):
            frame = frames[-1]
        out.write(f"\033[{up}A\r" + "".join(f"{line}\033[K\n" for line in frame) + f"\033[{up - len(rows)}B\r")
        out.flush()
        if frame is frames[-1]:
            return
        time.sleep(_SHIMMER_FRAME_SECONDS)


def _draw(
    state: MenuState, selected: int, out: IO[str], stdin: IO[str], *, entrance: bool
) -> tuple[list[str], _Drawn]:
    """The logo (when one fits) centred over the panel, then the panel.

    The logo is never part of the panel's repaint, so the panel's arithmetic
    is untouched; :func:`_tui` erases and redraws both when the logo has to
    change. ``entrance`` plays one shimmer in ``animated`` mode.
    """
    rows = _logo_tier(state, out)
    lines = _panel(state, selected, out)
    if rows and state.logo_rows + _frame_rows(lines, out) >= _terminal_size(out).lines:
        # The panel wraps (a terminal narrower than its floor) or fills the
        # last row: the frame would scroll, stacking the logo in scrollback,
        # and the shimmer's walk up would land on the panel. Drop the logo.
        rows, state.logo_rows = (), 0
        lines = _panel(state, selected, out)
    drawn = _print_logo(state, out, rows, width(lines[0]))
    for line in lines:
        print(line, file=out)
    if drawn and entrance and state.logo == "animated" and color_supported(out):
        _shimmer(out, drawn, _frame_rows(lines, out), stdin)
    return lines, drawn


def _panel(
    state: MenuState,
    selected: int | None = None,
    out: IO[str] | None = None,
    order_value: str | None = None,
    messages: Sequence[str] = (),
    editing: str | None = None,
) -> list[str]:
    """The framed settings box, numbered for the typed-choice surface.

    Always titled with aicp's own version, so a bug report or a screenshot
    names the build it came from. ``selected`` (only ever given by the
    arrow-key TUI, which is the one surface with a single current row) adds
    more lines inside the frame: that row's own help text — 各項目說明,
    reusing exactly the ``help`` every row already carries, never a second
    copy of it — the arrow-key hint (操作說明), and, when Doctor is the
    current row, the full health report so the details appear on highlight
    without needing Enter. The numbered fallback has no single current row,
    so it gets only the digit-choice hint instead.

    ``messages`` is a ``returns_to_panel`` action's own result line(s),
    already colour-coded — shown as extra notes on the panel it settles
    back into after erasing its prompt+result, so the outcome is still
    readable instead of vanishing with the transcript it replaced.

    ``order_value`` swaps in an already-rendered motion frame for the AI CLI
    order row (see :func:`_order_motion`); everything else about the panel is
    drawn exactly as it is at rest, so a frame mid-slide is the same shape as
    the frame it settles into.

    ``editing`` is the text being typed into the selected row's value column
    (see :func:`_edit_inline`) — shown as bullets for a secret, and trimmed
    from the left so the caret stays in view.
    """
    out = out if out is not None else sys.stdout
    frame_columns, value_columns = _fit_columns(state, out)
    rows: list[tuple[str, str]] = []
    selected_row: int | None = None
    for i, row in enumerate(ROWS, start=1):
        if row.group:
            rows.append((_t(state.lang, *row.group), ""))
        marker = "›" if selected == i else " "
        value = (
            order_value
            if order_value is not None and row.key == "AICP_CLI_ORDER"
            else _fit(row.value(state), value_columns)
        )
        accent = row.accent(state)
        if editing is not None and selected == i and row.text_edit is not None:
            shown = ("•" * len(editing) if row.text_edit.secret else editing) + "▏"
            value = shown if width(shown) <= value_columns else _ELLIPSIS + shown[-(value_columns - 1) :]
            accent = GREEN
        label = f"{marker} {i:>{_NUM_WIDTH}}) {_t(state.lang, *row.label)}"
        # The cursor row stands out by weight, not a new hue: RESET cancels
        # render_panel's own DIM before BOLD applies, matching the group
        # headings above and keeping every hue's existing meaning intact.
        if selected == i:
            label = f"{RESET}{BOLD}{label}{RESET}"
            selected_row = len(rows)
        rows.append(
            (
                label,
                f"{accent}{value}{RESET}",
            )
        )
    title = f"{_t(state.lang, 'config_title', 'aicp config')} (v{__version__})"
    if selected is not None:
        # Reserve the widest help line ANY row can show, not just the selected
        # one's — otherwise the frame narrows and widens as the cursor moves
        # across rows with shorter and longer help text (render_panel sizes
        # its own width off the notes it is handed). Doctor's detail lines
        # are fitted to that same reserve so highlighting it cannot widen
        # the frame either.
        help_budget = frame_columns - 4
        fitted_help = [_fit(_t(state.lang, *row.help), help_budget) for row in ROWS]
        reserve = max(width(h) for h in fitted_help)
        help_line = fitted_help[selected - 1]
        help_line += " " * (reserve - width(help_line))
        notes = [f"{DIM}{help_line}{RESET}", ""]
        if messages:
            notes.extend(messages)
            notes.append("")
        if _is_doctor(ROWS[selected - 1]):
            notes.extend(_doctor_detail_notes(state, reserve))
            notes.append("")
        if editing is None:
            # Its own line, ahead of config_keys_tui below: the height-trim
            # loop further down always protects the LAST note (the one "stuck
            # in an unfamiliar menu" needs most), so the primary ↑↓/←→/⏎ hint
            # has to stay last and this one goes ahead of it, droppable first
            # on a short terminal.
            notes.append(
                f"{DIM}"
                + _fit(
                    _t(state.lang, "config_keys_tui2", "r reset row · R reset all"),
                    help_budget,
                )
                + f"{RESET}"
            )
        notes.append(
            f"{DIM}"
            + _fit(
                _t(
                    state.lang,
                    "config_keys_edit",
                    "type a value · ⏎ save · Esc cancel · '-' then ⏎ clears",
                )
                if editing is not None
                else _t(
                    state.lang,
                    "config_keys_tui",
                    "↑↓ select · ←→ change · ⏎ change/run · q/Ctrl+C quit · saves as you go",
                ),
                help_budget,
            )
            + f"{RESET}"
        )
    else:
        notes = [
            f"{DIM}"
            + _fit(
                _t(
                    state.lang,
                    "config_keys_plain",
                    "1-%s change · q quit · saves as you go",
                    len(ROWS),
                ),
                frame_columns - 4,
            )
            + f"{RESET}"
        ]
    # A frame taller than the terminal cannot be repainted in place either —
    # its top scrolls off, and the cursor can never walk back up to it. The
    # notes are what a short window gives up. Prefer dropping help text,
    # blanks, and ✓ detail lines before ⚠ warnings; the key-hint footer is
    # always last and is what someone stuck in an unfamiliar menu needs.
    while notes and len(rows) + len(notes) + 4 > _terminal_size(out).lines - state.logo_rows:
        drop = next(
            (i for i, note in enumerate(notes[:-1]) if _WARN not in note),
            0,
        )
        notes.pop(drop)
    return render_panel(rows, title, BLUE, notes=notes, highlight=selected_row)


def _write(
    state: MenuState,
    row: Row,
    direction: int,
    stdin: IO[str],
    out: IO[str],
    typed: Callable[[], contextlib.AbstractContextManager[None]] = contextlib.nullcontext,
) -> bool:
    if row.text_edit is not None:  # numbered fallback only; the TUI edits inline
        _text_edit_fallback(state, row.text_edit, stdin, out)
        return True
    if row.action is not None:
        # An action row asks its questions with read_line, which needs the
        # line discipline the arrow-key session holds suspended.
        with typed():
            row.action(state, stdin, out)
        return True  # an action row has nothing to persist
    if row.cycle is None:  # a row is either cycle or action, never neither
        return True
    value = row.cycle(state, direction)
    if persist_key(row.key, value, state.path):
        # Every persisting row lands here, and the Doctor row reports on the
        # very file that was just written — so the invalidation belongs at
        # this one choke point, not next to whichever row happened to change
        # something (which is how it went stale the first time).
        state.health = None
        return True
    print(
        f"{RED}{_t(state.lang, 'persist_failed', '✗ failed to write %s', state.path)}{RESET}",
        file=out,
    )
    return False


#: What each cycle row's shipped default is, keyed by its config key — the
#: ``r``/``R`` undo keys' only table. A cycle mutator only knows how to move
#: one step; there is no "set to N" version of it to reuse here, unlike the
#: text_edit rows below, which already have one (see :func:`_reset_row`).
_CYCLE_DEFAULTS: dict[str, Callable[[MenuState], None]] = {
    "AICP_DO_COMMIT": lambda s: setattr(s, "do_commit", True),
    "AICP_DO_PUSH": lambda s: setattr(s, "do_push", True),
    "AICP_LANG": lambda s: setattr(s, "lang", "en"),
    "AICP_LOGO": lambda s: setattr(s, "logo", DEFAULT_LOGO),
    "AICP_UPDATE_CHECK": lambda s: setattr(s, "update_check", True),
    "AICP_CLI_ORDER": lambda s: setattr(s, "chain", list(_ROSTER_NAMES)),
}


def _reset_row(state: MenuState, row: Row) -> bool:
    """Put *row* back to its shipped default and persist that immediately —
    the ``r``/``R`` undo keys' only job. False when *row* has nothing to
    reset (an action row: Skills, Agents, Doctor, Export, Import) or the
    write failed.

    A text_edit row reuses its own ``commit``, which already treats ``-`` as
    "clear" (see :class:`TextEdit`) — the same path a typed ``-`` takes. A
    cycle row has no such absolute setter, only a step, so this removes the
    key from config.json instead of writing out the default value: the file
    then reads exactly like a fresh install's, which is the more honest
    "default" of the two the menu could persist.
    """
    if row.text_edit is not None:
        row.text_edit.commit(state, "-")
        return True
    reset_default = _CYCLE_DEFAULTS.get(row.key)
    if reset_default is None:
        return False
    reset_default(state)
    if not reset_key(row.key, state.path):
        return False
    state.health = None  # Doctor reports on the file just written
    return True


def _is_resettable(row: Row) -> bool:
    """Whether ``r`` has anything to undo on *row* — the same rule
    :func:`_reset_row` applies, checked up front so a mistaken ``r`` on an
    action row (Skills, Agents, Doctor, Export, Import) skips straight to the
    "nothing to reset" note instead of asking a [y/N] question about nothing.
    """
    return row.text_edit is not None or row.key in _CYCLE_DEFAULTS


def _reset_row_preview(state: MenuState, row: Row) -> str:
    """What *row*'s value column will read right after ``r`` resets it — the
    confirm question's only per-row part.

    A text_edit row always clears to empty, the same as a typed ``-`` (see
    :func:`_reset_row`). ``AICP_CLI_ORDER``'s full default chain is long
    enough to wrap a one-line prompt, so it names the order instead of
    spelling it out. Every other row previews its own default by applying
    :data:`_CYCLE_DEFAULTS` to a throwaway copy of *state* and reading the
    row's own ``value`` off of it — reusing the row's existing formatting
    (On/Off, the language name) rather than a second, hand-written
    description that could drift from it.
    """
    if row.text_edit is not None:
        return _t(state.lang, "config_reset_row_empty", "empty")
    if row.key == "AICP_CLI_ORDER":
        return _t(state.lang, "config_reset_row_cli_default", "aicp default order")
    preview = copy.copy(state)
    _CYCLE_DEFAULTS[row.key](preview)
    return row.value(preview)


def _reset_row_action(
    state: MenuState, row: Row, stdin: IO[str], out: IO[str]
) -> tuple[list[str], int]:
    """``r`` on a resettable row: one [y/N] question naming the row and its
    default, gating the same write :func:`_reset_row` always did silently —
    a mistaken keypress must not blank a field. Reuses
    :func:`_reset_all_action`'s ``_prompt_line`` mechanism and
    ``(result line(s), rows printed)`` contract, one consistent confirm
    pattern rather than a second confirmation UI: anything but y/Y cancels
    with no change, exactly like ``R``.
    """
    print(file=out)
    label = _t(state.lang, *row.label)
    default = _reset_row_preview(state, row)
    question = _t(state.lang, "config_reset_row_q", 'Reset "%s" to %s? [y/N]: ', label, default)
    answer, rows = _prompt_line(question, stdin, out)
    rows += 1  # the leading blank line above
    if answer is None or answer.strip().lower() not in ("y", "yes"):
        return [], rows
    _reset_row(state, row)
    return [], rows


def _reset_all_action(state: MenuState, stdin: IO[str], out: IO[str]) -> tuple[list[str], int]:
    """``R``: every resettable row back to its default, gated by one [y/N] —
    the only way to undo more than the last change, since the menu saves as
    it goes and there is no save/discard step otherwise. Same
    ``(result line(s), rows printed)`` contract as a ``returns_to_panel``
    action (see :func:`_export_action`)."""
    print(file=out)
    question = _t(state.lang, "config_reset_all_q", "Reset ALL settings to defaults? [y/N]: ")
    answer, rows = _prompt_line(question, stdin, out)
    rows += 1  # the leading blank line above
    if answer is None or answer.strip().lower() not in ("y", "yes"):
        return [], rows
    for row in ROWS:
        _reset_row(state, row)
    message = (
        f"  {GREEN}{_OK}{RESET} "
        + _t(state.lang, "config_reset_all_done", "all settings reset to defaults")
    )
    return [message], rows


#: Update prompt rows, top to bottom — the answers update_check.offer acts on.
_UPDATE_ANSWERS = (update_check.UPDATE_NOW, update_check.SKIP, update_check.SKIP_VERSION)


def _update_lines(lang: str, found: update_check.UpdateAvailable, notes_url: str, selected: int) -> list[str]:
    """The update prompt, drawn like the Agents sub-panel: ``› N)`` marker,
    bold highlighted row, dim detail column, key-hint footer."""
    choices = (
        (_t(lang, "update_now", "Update now"), "uv tool upgrade aicp-cli"),
        (_t(lang, "update_skip", "Skip"), _t(lang, "update_skip_detail", "ask again next run")),
        (
            _t(lang, "update_skip_version", "Skip until next version"),
            _t(lang, "update_skip_version_detail", "ask again once a newer version ships"),
        ),
    )
    body: list[tuple[str, str]] = []
    for i, (name, detail) in enumerate(choices, start=1):
        marker = "›" if selected == i else " "
        label = f"{marker} {i}) {name}"
        if selected == i:
            label = f"{RESET}{BOLD}{label}{RESET}"
        body.append((label, f"{DIM}{detail}{RESET}"))
    notes = [
        _t(lang, "update_notes", "Release notes: %s", notes_url),
        f"{DIM}" + _t(lang, "update_keys", "↑↓ select · ⏎ confirm · q/Ctrl+C skip") + f"{RESET}",
    ]
    title = "✨ " + _t(
        lang, "update_available", "aicp %s is available (you have %s)", found.latest, found.current
    )
    return render_panel(body, title, CYAN, notes=notes)


def update_prompt(
    found: update_check.UpdateAvailable,
    notes_url: str,
    *,
    stdin: IO[str],
    out: IO[str],
) -> str:
    """The ``ask`` callback for :func:`aicp.update_check.offer`: returns
    UPDATE_NOW / SKIP / SKIP_VERSION and does nothing else. q or EOF is SKIP."""
    lang = i18n.LANGUAGE
    selected = 1
    lines = _update_lines(lang, found, notes_url, selected)
    for line in lines:
        print(line, file=out)
    with key_session(stdin, out):
        while True:
            key = read_key(stdin, out)
            if key == "quit":
                return update_check.SKIP
            if key in ("enter", "space"):
                return _UPDATE_ANSWERS[selected - 1]
            if key == "up":
                selected = selected - 1 if selected > 1 else len(_UPDATE_ANSWERS)
            elif key == "down":
                selected = selected + 1 if selected < len(_UPDATE_ANSWERS) else 1
            else:
                continue
            out.write(f"\033[{_frame_rows(lines, out)}A\033[J")
            lines = _update_lines(lang, found, notes_url, selected)
            for line in lines:
                print(line, file=out)


def config_menu(
    *,
    settings: Settings | None = None,
    path: Path | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Run the settings menu; 0 on a clean exit, 1 on a failed write."""
    state = MenuState.from_settings(settings or resolve())
    if path is not None:
        state.path = path
    out = stdout or sys.stdout
    stream = stdin or sys.stdin
    if is_interactive(stream, out):
        return _tui(state, stream, out)
    return _numbered(state, stream, out)


def _numbered(state: MenuState, stdin: IO[str], out: IO[str]) -> int:
    """Typed-choice surface. EOF is "quit" — never a block, so CI is safe."""
    rows = _logo_tier(state, out)
    if rows:
        _print_logo(state, out, rows, width(_panel(state, out=out)[0]))
    while True:
        for line in _panel(state, out=out):
            print(line, file=out)
        print(
            _t(
                state.lang,
                "config_prompt",
                "Pick a setting to change (1-%s, q to quit): ",
                len(ROWS),
            ),
            file=out,
        )
        choice = read_line(stdin)
        if choice is None or choice == "" or choice.lower() == "q":
            return 0
        if choice.isdigit() and 1 <= int(choice) <= len(ROWS):
            if not _write(state, ROWS[int(choice) - 1], 1, stdin, out):
                return 1
        else:
            print(
                f"{RED}{_t(state.lang, 'config_bad_number', '⚠ Enter one of the setting numbers shown above.')}{RESET}",
                file=out,
            )
        print(file=out)


def _tape(before: Sequence[str], after: Sequence[str]) -> tuple[str, int, int]:
    """The order before and after a rotation, laid end to end, plus the window
    offsets that read as each of them.

    A rotation moves every name along by one slot and wraps the one that
    falls off the end back round to the other side. Writing the name that
    wraps next to the order it left makes a strip where **one window of it is
    always a real order**, and sliding that window by one name turns the old
    order into the new one — every other name displaced on the way, the
    wrapping one leaving by one edge and arriving at the other. That is the
    entire animation: one strip, one offset, no per-name bookkeeping.
    """
    if after[0] == before[1]:  # → : the chain walks left, the head wraps to the tail
        return _SEPARATOR.join((*before, before[0])), 0, width(before[0] + _SEPARATOR)
    return _SEPARATOR.join((after[0], *before)), width(after[0] + _SEPARATOR), 0


def _lit(piece: str, bright: Sequence[bool], accent: str) -> str:
    """*piece* with the *bright* columns picked out, back to *accent* after."""
    parts: list[str] = []
    on = False
    for char, hot in zip(piece, bright):
        if hot != on:
            parts.append(f"{GREEN}{BOLD}" if hot else f"{RESET}{accent}")
            on = hot
        parts.append(char)
    return "".join(parts) + (f"{RESET}{accent}" if on else "")


def _eased(progress: float) -> float:
    """How much of the slide's time is spent by the time it is *progress*
    along. A shallow curve: the chain leaves briskly and eases into its new
    order instead of stopping dead on the last column."""
    return 1 - (1 - progress) ** (2 / 3)


def _order_motion(
    before: Sequence[str], after: Sequence[str], columns: int, accent: str
) -> list[tuple[str, float]]:
    """The chain sliding *before* → *after*: one frame per column it travels,
    each paired with how long to hold it.

    One column per frame is what makes this read as motion rather than as a
    jump, so the easing lives in the timing and not in the distance — a
    curve applied to the offset instead would round several frames onto the
    same column and stall there. Every frame is exactly *columns* wide, so
    the panel never changes shape mid-slide.
    """
    tape, start, end = _tape(before, after)
    bright = [False] * len(tape)
    promoted = after[0]
    at = tape.find(promoted)
    while at != -1:  # the wrapping name is on the strip twice — light both
        bright[at : at + len(promoted)] = [True] * len(promoted)
        at = tape.find(promoted, at + len(promoted))
    travel = abs(end - start)
    step = 1 if end > start else -1
    frames = []
    for moved in range(travel + 1):
        offset = start + step * moved
        # The settled frame is the last one and is held by whatever comes
        # next, not by the slide.
        held = (
            _MOTION_SECONDS * (_eased((moved + 1) / travel) - _eased(moved / travel))
            if moved < travel
            else 0.0
        )
        frames.append((_lit(tape[offset : offset + columns], bright[offset:], accent), held))
    return frames


def _order_line() -> int:
    """Which line of :func:`_panel`'s output carries the AI CLI order row.

    Derived from :data:`ROWS` rather than counted by hand, for the same
    reason nothing else in this module hardcodes a row number: the panel is
    one title line, then each row preceded by its group heading when it
    opens one.
    """
    line = 1
    for row in ROWS:
        if row.group:
            line += 1
        if row.key == "AICP_CLI_ORDER":
            return line
        line += 1
    raise AssertionError("no AICP_CLI_ORDER row")  # pragma: no cover - ROWS is a constant


def _animates(lines: list[str], out: IO[str]) -> bool:
    """Whether motion can be drawn at all.

    Colour, and a frame that is not wrapping: a wrapped line cannot be
    rewritten on its own (``\\033[K`` clears one screen row, not one logical
    line), and a terminal too narrow to hold the panel has a redraw problem
    to fix before it has an animation to watch.
    """
    return color_supported(out) and _frame_rows(lines, out) == len(lines)


def _repaint_line(out: IO[str], lines: list[str], index: int, text: str) -> None:
    """Rewrite one line of the frame already on screen, leaving the rest be.

    An animation step changes one row, and erasing the whole frame to redraw
    it 12 times a second is what makes motion flicker — so the cursor walks
    up to just that line, overwrites it, and comes straight back. Nothing is
    scrolled: no newline is ever written.
    """
    up = _frame_rows(lines[index:], out)
    out.write(f"\033[{up}A\r\033[K{text}\033[{up}B\r")


def _frame_rows(lines: list[str], out: IO[str]) -> int:
    """Physical terminal rows *lines* occupies once wrapped at the real
    terminal width.

    Not the same as ``len(lines)``: a row's visible width (CJK glyphs count
    two columns) routinely exceeds an 80-column terminal, so the terminal
    itself wraps that one logical line into two on-screen rows. Erasing by
    ``len(lines)`` then moves the cursor up too few rows, leaves the old
    frame's wrapped tail on screen, and the next redraw piles another tail
    on top of that one — the "whole panel smears down the screen" bug.
    """
    columns = max(_terminal_size(out).columns, 1)
    return sum(-(-width(line) // columns) or 1 for line in lines)


def _return_to_panel(
    state: MenuState,
    out: IO[str],
    selected: int,
    parent_lines: list[str],
    extra_rows: int,
    messages: Sequence[str] = (),
) -> list[str]:
    """Erase *parent_lines* plus whatever was drawn below them (a sub-panel,
    or a ``returns_to_panel`` action's counted prompt+result), then redraw
    the main panel once in its place — the Agents row's own erase-and-return
    step, shared so a future ``returns_to_panel`` action gets it by setting
    that flag instead of reimplementing the CSI arithmetic.
    """
    up = _frame_rows(parent_lines, out) + extra_rows
    out.write(f"\033[{up}A\033[J")
    lines = _panel(state, selected, out, messages=messages)
    for line in lines:
        print(line, file=out)
    return lines


def _redraw(
    state: MenuState,
    selected: int,
    out: IO[str],
    lines: list[str],
    messages: Sequence[str] = (),
    editing: str | None = None,
) -> list[str]:
    out.write(f"\033[{_frame_rows(lines, out)}A\033[J")
    lines = _panel(state, selected, out, messages=messages, editing=editing)
    for line in lines:
        print(line, file=out)
    return lines


def _edit_inline(
    state: MenuState, row: Row, selected: int, stdin: IO[str], out: IO[str], lines: list[str]
) -> list[str]:
    """Type *row*'s new value into its own value column, crw ``--config``
    style. ⏎ commits, Esc cancels, and a rejected value keeps the field open
    with the reason under it.

    A secret's field opens EMPTY: prefilling would show the token, and
    committing the mask back would overwrite the real one — so ⏎ on an empty
    secret field cancels rather than clears, and clearing takes a typed
    ``-``. A plain field opens prefilled, so emptying it and pressing ⏎
    clears it (so does ``-``); ⏎ on an unchanged value writes nothing.
    """
    edit = row.text_edit
    assert edit is not None
    seed = edit.seed(state)
    buffer = seed
    messages: list[str] = []
    while True:
        lines = _redraw(state, selected, out, lines, messages, editing=buffer)
        key = read_edit_key(stdin)
        if key == "escape":
            messages = []
            break
        if key == "enter":
            text = buffer.strip()
            if (not text and edit.secret) or (not edit.secret and text == seed):
                messages = []
                break
            try:
                messages = [edit.commit(state, text or "-")]
            except ValueError as exc:
                messages = [f"  {RED}{exc}{RESET}"]
                continue
            break
        if key == "backspace":
            buffer = buffer[:-1]
        elif edit.max_len is None or len(buffer) < edit.max_len:
            # Live-clamped, same as the digit cap elsewhere: a held key or a
            # pasted flood stops growing the buffer instead of ballooning it
            # one redraw at a time.
            buffer += key
        messages = []
    return _redraw(state, selected, out, lines, messages)


def _tui(state: MenuState, stdin: IO[str], out: IO[str]) -> int:
    """Arrow-key surface. Repaints in place by walking back up the frame it
    just drew; ``\\033[J`` erases to the end of the screen because switching
    to a language with narrower rows would otherwise leave the previous,
    wider frame's right-hand border on screen as a second column of │."""
    selected = 1
    lines, drawn = _draw(state, selected, out, stdin, entrance=True)
    shown_logo, shown_lang = state.logo, state.lang
    drawn_size = _terminal_size(out)
    #: False once something printed between the logo and the panel (the
    #: Skills report): the logo is no longer where the shimmer would look.
    logo_above = True
    try:
        with key_session(stdin, out) as typed:
            while True:
                if state.logo != shown_logo or (drawn and state.lang != shown_lang):
                    # The Logo row changed it (a step, r, R or an import), or
                    # a language switch resized the panel it is centred over:
                    # the logo sits above the panel's repaint region, so
                    # either means erasing both and drawing both again.
                    above = state.logo_rows if logo_above else 0
                    out.write(f"\033[{_frame_rows(lines, out) + above}A\033[J")
                    lines, drawn = _draw(state, selected, out, stdin, entrance=state.logo != shown_logo)
                    drawn_size, logo_above = _terminal_size(out), True
                shown_logo, shown_lang = state.logo, state.lang
                while (
                    drawn
                    and logo_above
                    and state.logo == "animated"
                    and color_supported(out)
                    and not pending(stdin, _SHIMMER_EVERY)
                ):
                    if _terminal_size(out) != drawn_size:
                        break  # rows re-wrapped: the logo is not where it was drawn
                    _shimmer(out, drawn, _frame_rows(lines, out), stdin)
                key = read_key(stdin, out)
                if _terminal_size(out) != drawn_size:
                    # Resized while waiting for that key. Every line on
                    # screen may have re-wrapped, so the row count the
                    # cursor-up walk relies on is gone: clear the screen and
                    # draw from the top, tier and centring re-picked for the
                    # new size, before the key's own repaint trusts the count.
                    out.write("\033[H\033[2J")
                    lines, drawn = _draw(state, selected, out, stdin, entrance=False)
                    drawn_size, logo_above = _terminal_size(out), True
                if key == "quit":
                    return 0
                if key == "up":
                    selected = selected - 1 if selected > 1 else len(ROWS)
                elif key == "down":
                    selected = selected + 1 if selected < len(ROWS) else 1
                elif key in ("left", "right", "enter", "space"):
                    row = ROWS[selected - 1]
                    before = list(state.chain)
                    if _is_doctor(row):
                        # Details are already expanded in the panel while this
                        # row is highlighted; Enter/←/→ must not reprint them.
                        continue
                    if row.text_edit is not None:
                        lines = _edit_inline(state, row, selected, stdin, out, lines)
                        continue
                    if row.action is _agents_action:
                        # The one action row with its own arrow-key loop: a
                        # toggle is watched happening, not read off a report,
                        # so its frame is erased like any other value change
                        # rather than left standing like Skills'.
                        # Agents is drawn *under* the still-visible parent, so
                        # leaving it has to walk up parent+child before the
                        # redraw — erasing only the child leaves the old
                        # parent on screen and the next Enter stacks another.
                        agent_lines = _agents_tui(state, stdin, out, typed)
                        lines = _return_to_panel(
                            state, out, selected, lines, _frame_rows(agent_lines, out)
                        )
                        continue
                    if row.action is not None and row.returns_to_panel:
                        # A one-shot outcome (Export/Import), not a report —
                        # same erase-and-return as Agents above, sharing its
                        # helper: the action counts its own rows (see _prompt_line)
                        # rather than leaving them stacked above yet another
                        # fresh panel, and its result line(s) carry over as
                        # that panel's note.
                        with typed():
                            messages, rows = row.action(state, stdin, out)
                        lines = _return_to_panel(state, out, selected, lines, rows, messages)
                        continue
                    if not _write(state, row, -1 if key == "left" else 1, stdin, out, typed):
                        return 1
                    if row.action is not None:
                        # An action prints below the frame; redraw under its
                        # output rather than scrolling back up over what it
                        # just said.
                        logo_above = False
                        lines = _panel(state, selected, out)
                        for line in lines:
                            print(line, file=out)
                        continue
                    if row.key == "AICP_CLI_ORDER" and _animates(lines, out):
                        # The whole chain slides one name over, so the change
                        # is watched rather than noticed after the fact. Only
                        # the order row is rewritten per step — the rest of
                        # the frame is already right, and redrawing it is
                        # what flickers.
                        index = _order_line()
                        columns = width(
                            _fit(_SEPARATOR.join(state.chain), _fit_columns(state, out)[1])
                        )
                        for frame, held in _order_motion(
                            before, state.chain, columns, row.accent(state)
                        ):
                            lines = _panel(state, selected, out, order_value=frame)
                            _repaint_line(out, lines, index, lines[index])
                            out.flush()
                            if pending(stdin):
                                break  # a key is already waiting; land on the
                                # settled frame now rather than making it queue
                                # behind a slide nobody is still watching
                            time.sleep(held)
                elif key == "reset":
                    row = ROWS[selected - 1]
                    if not _is_resettable(row):
                        # An action row (Skills, Agents, Doctor, Export,
                        # Import) has no value to undo — say so rather than
                        # silently ignoring the key.
                        note = (
                            f"{DIM}"
                            + _t(
                                state.lang,
                                "config_nothing_to_reset",
                                "Not a setting — nothing to reset",
                            )
                            + RESET
                        )
                        lines = _return_to_panel(state, out, selected, lines, 0, [note])
                        continue
                    with typed():
                        messages, rows = _reset_row_action(state, row, stdin, out)
                    lines = _return_to_panel(state, out, selected, lines, rows, messages)
                    continue
                elif key == "reset_all":
                    with typed():
                        messages, rows = _reset_all_action(state, stdin, out)
                    lines = _return_to_panel(state, out, selected, lines, rows, messages)
                    continue
                else:
                    continue
                out.write(f"\033[{_frame_rows(lines, out)}A\033[J")
                lines = _panel(state, selected, out)
                for line in lines:
                    print(line, file=out)
    except KeyboardInterrupt:
        # cbreak leaves Ctrl+C a signal rather than a byte, and quitting a
        # menu that saves as it goes has nothing to roll back.
        print(file=out)
        return 0


def swap_ai(
    *,
    settings: Settings | None = None,
    path: Path | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """``--swap-ai``: move the picked CLI to #1, trading places with whoever
    holds it. Never runs an AI CLI and never commits or pushes — the swap
    only; the next ordinary ``aicp`` run is what applies the new priority.

    The menu always lists the full roster, not just what is installed, so a
    prior swap or a missing binary never hides a choice (missing CLIs are
    skipped at call time by the runner instead).
    """
    state = MenuState.from_settings(settings or resolve())
    if path is not None:
        state.path = path
    out = stdout or sys.stdout
    chain = state.chain

    print(_t(state.lang, "swap_current_order", "Current fallback order:"), file=out)
    for i, name in enumerate(chain, start=1):
        first = _t(state.lang, "swap_current_first", "  ← current #1") if i == 1 else ""
        print(f"  {i}) {name}{DIM}{first}{RESET}", file=out)
    print(_t(state.lang, "swap_prompt", "Pick a CLI to move to #1 (1-%s): ", len(chain)), file=out)

    choice = read_line(stdin)
    if choice is None or choice == "":
        return 0
    if not choice.isdigit() or not 1 <= int(choice) <= len(chain):
        print(
            f"{RED}{_t(state.lang, 'swap_invalid', '✗ invalid choice: %s', choice)}{RESET}",
            file=out,
        )
        return 1
    picked = int(choice)
    if picked == 1:
        print(
            f"{DIM}{_t(state.lang, 'swap_already_first', '▸ %s is already #1 — no change', chain[0])}{RESET}",
            file=out,
        )
        return 0

    chain[picked - 1], chain[0] = chain[0], chain[picked - 1]
    if not persist_key("AICP_CLI_ORDER", " ".join(chain), state.path):
        print(
            f"{RED}{_t(state.lang, 'persist_failed', '✗ failed to write %s', state.path)}{RESET}",
            file=out,
        )
        return 1
    print(
        f"{GREEN}{_t(state.lang, 'swap_new_order', '✓ new order:')}{RESET} {' -> '.join(chain)}",
        file=out,
    )
    print(
        f"{DIM}{_t(state.lang, 'swap_saved', '  saved to %s — the next aicp run uses it', state.path)}{RESET}",
        file=out,
    )
    return 0


def cli_chain(settings: Settings | None = None) -> Sequence[str]:
    """The resolved fallback chain, for callers that already have Settings.

    Thin alias over ``Settings.cli_chain`` so a caller never has to know
    whether the order came from the environment, ``.aicprc`` or the default
    roster — see :func:`aicp.config.resolve_cli_chain`, the contract T2 and
    T4 consume.
    """
    return (settings or resolve()).cli_chain
