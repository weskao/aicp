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
something (install skills, turn an agent on or off, print a health report)
rather than persist a setting, and they show live status in their value
column. They are rows and not subcommands on purpose — ``aicp`` and ``aicp
--config`` are the only two things this tool ever asks anyone to remember.
Agents is the one that also has a scriptable twin, ``aicp --agents``, because
adding an agent means supplying six fields, which is a form and not a row.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import __version__, agentcfg, agents, gitflow, i18n, skills
from ._keyreader import is_interactive, key_session, pending, read_key, read_line
from ._utils import BLUE, BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, color_supported

# _KEY_RE and _SYSTEM_RESOLVED are read, not copied: the doctor reports on the
# loader's own verdict about a line, so it has to ask with the loader's own key
# rule, and has to name the keys that may never come from a file at all.
from .config import (
    _KEY_RE,
    _SYSTEM_RESOLVED,
    DENYLIST,
    Settings,
    _read_json_object,
    load_config,
    persist_key,
    resolve,
    timeout_bin,
)
from .contracts import ROSTER
from .present import render_panel, width

__all__ = ["ROWS", "MenuState", "Row", "config_menu", "swap_ai"]

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
    #: Probe caches for the two action rows. The panel repaints on every
    #: keypress, and both of those rows show live status in their value
    #: column — walking the filesystem and shelling out to git once per
    #: repaint would be a real, per-keystroke cost. Probed once per session
    #: instead, and dropped by the Skills action whenever it changes anything.
    skills_status: list[skills.SkillStatus] | None = None
    health: list[tuple[str, str]] | None = None
    agent_rows: list[agents.AgentRow] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> MenuState:
        return cls(
            path=settings.path,
            do_commit=settings.do_commit,
            do_push=settings.do_push,
            lang=settings.lang,
            chain=list(settings.cli_chain),
            update_check=settings.update_check,
        )


@dataclass(frozen=True)
class Row:
    """One setting, or one action. See the module docstring for how to add one.

    A row either *persists* (``key`` + ``cycle``) or *acts* (``action``, with
    an empty ``key``): the Skills and Doctor rows run something and write no
    setting at all. Everything else — numbering, the value column, bounds,
    dispatch — is identical either way, which is the whole point of keeping
    the menu a list of data rather than a switch statement.
    """

    key: str
    group: tuple[str, str] | None
    label: tuple[str, str]
    help: tuple[str, str]
    value: Callable[[MenuState], str]
    accent: Callable[[MenuState], str]
    cycle: Callable[[MenuState, int], str] | None = None
    #: ``(state, stdin, stdout)``. Must never block on a non-TTY stdin —
    #: ``read_line`` returns ``None`` at EOF and every prompt here reads that
    #: as "no", because aicp runs in CI.
    action: Callable[[MenuState, IO[str], IO[str]], None] | None = None


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
    rows = _agent_rows(state)
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
        value = f"{color}{mark}{RESET} {row.executable}{pad}  {DIM}{state_label}{state_pad}{RESET}"
        body.append((label, value))
    notes = [message] if message else []
    notes.append(
        f"{DIM}" + _t(state.lang, "agents_tui_keys", "↑↓ select · ⏎ toggle on/off · q back") + f"{RESET}"
    )
    return render_panel(body, _t(state.lang, "config_agents", "Agents"), CYAN, notes=notes)


def _agents_tui(state: MenuState, stdin: IO[str], out: IO[str]) -> list[str]:
    """Arrow-key loop opened by the Agents row: ↑↓ moves, ⏎/←/→ toggles the
    highlighted agent on/off through :func:`agentcfg.apply` — saved
    immediately, same as every other row — and q leaves. Returns the last
    frame it drew; the caller must erase that many rows *plus* the parent
    panel still sitting above it, then redraw the parent once — erasing only
    this frame leaves the old parent on screen and the next Enter stacks
    another copy.
    """
    selected = 1
    message: str | None = None
    lines = _agents_lines(state, selected)
    for line in lines:
        print(line, file=out)
    while True:
        key = read_key(stdin, out)
        if key == "quit":
            return lines
        message = None
        rows = _agent_rows(state)
        if key == "up":
            selected = selected - 1 if selected > 1 else len(rows)
        elif key == "down":
            selected = selected + 1 if selected < len(rows) else 1
        elif key in ("left", "right", "enter"):
            picked = rows[selected - 1]
            verb = "enable" if picked.state == agents.DISABLED else "disable"
            try:
                change = agentcfg.apply(verb, picked.name, config_path=state.path)
            except agentcfg.AgentEditError as exc:
                message = f"{RED}✗ {exc}{RESET}"
            else:
                state.chain = [name for name in state.chain if name in change.names] + [
                    name for name in change.names if name not in state.chain
                ]
                state.agent_rows = None
                state.skills_status = None
                state.health = None
                if change.pruned:
                    message = (
                        f"{DIM}"
                        + _t(
                            state.lang,
                            "agents_pruned",
                            "  dropped from the saved CLI order: %s",
                            " ".join(change.pruned),
                        )
                        + f"{RESET}"
                    )
        else:
            continue
        out.write(f"\033[{_frame_rows(lines, out)}A\033[J")
        lines = _agents_lines(state, selected, message)
        for line in lines:
            print(line, file=out)


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


def _doctor_action(state: MenuState, _stdin: IO[str], out: IO[str]) -> None:
    """Print the report. Asks nothing, so there is nothing to block on."""
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
            "On: after each command, hint when a newer aicp-cli exists.",
        ),
        value=lambda s: _on_off(s, s.update_check),
        accent=lambda s: GREEN if s.update_check else DIM,
        cycle=_toggle_update_check,
    ),
    Row(
        key="AICP_CLI_ORDER",
        group=None,
        label=("config_cli_order", "AI CLI order"),
        help=(
            "config_help_cli",
            "Full fallback order, tried left to right; ←/→ rotates it. Missing CLIs are skipped.",
        ),
        value=lambda s: _SEPARATOR.join(s.chain),
        accent=lambda _s: CYAN,
        cycle=_rotate_chain,
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
            "Which AI CLIs aicp knows about. Turn one off, or add your own with --agents set.",
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
)


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
    labels = [f"  {i}) {_t(state.lang, *row.label)}" for i, row in enumerate(ROWS, start=1)]
    labels += [_t(state.lang, *row.group) for row in ROWS if row.group]
    return frame, max(frame - 6 - max(width(label) for label in labels), _MIN_VALUE_COLUMNS)


def _panel(
    state: MenuState,
    selected: int | None = None,
    out: IO[str] | None = None,
    order_value: str | None = None,
) -> list[str]:
    """The framed settings box, numbered for the typed-choice surface.

    Always titled with aicp's own version, so a bug report or a screenshot
    names the build it came from. ``selected`` (only ever given by the
    arrow-key TUI, which is the one surface with a single current row) adds
    two more lines inside the frame: that row's own help text — 各項目說明,
    reusing exactly the ``help`` every row already carries, never a second
    copy of it — and the arrow-key hint (操作說明). The numbered fallback has
    no single current row, so it gets only the digit-choice hint instead.

    ``order_value`` swaps in an already-rendered motion frame for the AI CLI
    order row (see :func:`_order_motion`); everything else about the panel is
    drawn exactly as it is at rest, so a frame mid-slide is the same shape as
    the frame it settles into.
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
        label = f"{marker} {i}) {_t(state.lang, *row.label)}"
        # The cursor row stands out by weight, not a new hue: RESET cancels
        # render_panel's own DIM before BOLD applies, matching the group
        # headings above and keeping every hue's existing meaning intact.
        if selected == i:
            label = f"{RESET}{BOLD}{label}{RESET}"
            selected_row = len(rows)
        rows.append(
            (
                label,
                f"{row.accent(state)}{value}{RESET}",
            )
        )
    title = f"{_t(state.lang, 'config_title', 'aicp config')} (v{__version__})"
    if selected is not None:
        # Reserve the widest help line ANY row can show, not just the selected
        # one's — otherwise the frame narrows and widens as the cursor moves
        # across rows with shorter and longer help text (render_panel sizes
        # its own width off the notes it is handed).
        help_budget = frame_columns - 4
        fitted_help = [_fit(_t(state.lang, *row.help), help_budget) for row in ROWS]
        reserve = max(width(h) for h in fitted_help)
        help_line = fitted_help[selected - 1]
        help_line += " " * (reserve - width(help_line))
        texts = [
            help_line,
            "",
            _t(state.lang, "config_keys_tui", "↑↓ select · ←→ change · ⏎ change/run · q/Ctrl-C quit · saves as you go"),
        ]
    else:
        texts = [_t(state.lang, "config_keys_plain", "1-%s change · q quit · saves as you go", len(ROWS))]
    notes = [f"{DIM}{_fit(text, frame_columns - 4)}{RESET}" for text in texts]
    # A frame taller than the terminal cannot be repainted in place either —
    # its top scrolls off, and the cursor can never walk back up to it. The
    # notes are what a short window gives up, help text first: the key hints
    # are the line someone stuck in an unfamiliar menu actually needs.
    while notes and len(rows) + len(notes) + 4 > _terminal_size(out).lines:
        notes.pop(0)
    return render_panel(rows, title, BLUE, notes=notes, highlight=selected_row)


def _write(
    state: MenuState,
    row: Row,
    direction: int,
    stdin: IO[str],
    out: IO[str],
    typed: Callable[[], contextlib.AbstractContextManager[None]] = contextlib.nullcontext,
) -> bool:
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


def _tui(state: MenuState, stdin: IO[str], out: IO[str]) -> int:
    """Arrow-key surface. Repaints in place by walking back up the frame it
    just drew; ``\\033[J`` erases to the end of the screen because switching
    to a language with narrower rows would otherwise leave the previous,
    wider frame's right-hand border on screen as a second column of │."""
    selected = 1
    lines = _panel(state, selected, out)
    for line in lines:
        print(line, file=out)
    try:
        with key_session(stdin, out) as typed:
            while True:
                key = read_key(stdin, out)
                if key == "quit":
                    return 0
                if key == "up":
                    selected = selected - 1 if selected > 1 else len(ROWS)
                elif key == "down":
                    selected = selected + 1 if selected < len(ROWS) else 1
                elif key in ("left", "right", "enter"):
                    row = ROWS[selected - 1]
                    before = list(state.chain)
                    if row.action is _agents_action:
                        # The one action row with its own arrow-key loop: a
                        # toggle is watched happening, not read off a report,
                        # so its frame is erased like any other value change
                        # rather than left standing like Skills/Doctor's.
                        # Agents is drawn *under* the still-visible parent, so
                        # leaving it has to walk up parent+child before the
                        # redraw — erasing only the child leaves the old
                        # parent on screen and the next Enter stacks another.
                        agent_lines = _agents_tui(state, stdin, out)
                        up = _frame_rows(lines, out) + _frame_rows(agent_lines, out)
                        out.write(f"\033[{up}A\033[J")
                        lines = _panel(state, selected, out)
                        for line in lines:
                            print(line, file=out)
                        continue
                    if not _write(state, row, -1 if key == "left" else 1, stdin, out, typed):
                        return 1
                    if row.action is not None:
                        # An action prints below the frame; redraw under its
                        # output rather than scrolling back up over what it
                        # just said.
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
