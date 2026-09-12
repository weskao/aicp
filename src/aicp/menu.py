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

.. rubric:: Adding a row (a later task adds Skills and Doctor rows)

:data:`ROWS` is data, not parallel ``case`` arms. To add a setting, append
one :class:`Row` to that tuple and nothing else changes: numbering, drawing,
prompt text ("1-N"), bounds checking and dispatch are all derived from the
tuple's length and each row's own callables. A row needs a config ``key``,
a ``group`` heading (``None`` to share the previous row's), ``label``/``help``
as ``(msgid, english)`` pairs, a ``value`` renderer and a ``cycle`` mutator
that returns the string to persist. Nothing in this module hardcodes four.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import i18n
from ._keyreader import is_interactive, read_key, read_line
from ._utils import BLUE, CYAN, DIM, GREEN, RED, RESET
from .config import Settings, persist_key, resolve
from .contracts import ROSTER
from .present import render_panel

__all__ = ["ROWS", "MenuState", "Row", "config_menu", "swap_ai"]

_ROSTER_NAMES: tuple[str, ...] = tuple(c.name for c in ROSTER)

# zh-TW for this module's own msgids. A bridge, not a second catalogue:
# :func:`_t` consults ``i18n.CATALOG`` FIRST, so when the task that fills the
# shared catalogue lands, its entries win and these become dead weight to
# delete. House style follows bin/aicp: fullwidth ：，（）, a half-width space
# around embedded Latin runs, command nouns left untranslated.
_ZH: dict[str, str] = {
    "config_title": "aicp 設定",
    "config_group_steps": "步驟",
    "config_group_general": "一般",
    "config_do_commit": "執行 /commit 步驟",
    "config_do_push": "執行 /safe-git-push 步驟",
    "config_language": "語言",
    "config_cli_first": "第一個 AI CLI",
    "config_on": "開",
    "config_off": "關",
    "config_help_commit": "關：完全不 stage 或 commit — 只推送已經 commit 的內容。",
    "config_help_push": "關：commit 後就停下，分支會領先遠端。",
    "config_help_lang": "所有訊息的語言，包含 Telegram 通知。",
    "config_help_cli": "優先嘗試；其餘維持原本順序。沒安裝的會自動略過。",
    "config_keys_plain": "1-%s 變更 · q 離開 · 隨改隨存",
    "config_keys": "↑↓ 選擇 · ←→ 變更 · ⏎ 切換 · q 離開 · 隨改隨存",
    "config_prompt": "請選擇要變更的設定（1-%s，q 離開）：",
    "config_bad_number": "⚠ 請輸入上面列出的設定編號。",
    "persist_failed": "✗ 寫入 %s 失敗",
    "swap_current_order": "目前的 fallback 順序：",
    "swap_current_first": "  ← 目前的 #1",
    "swap_prompt": "請選擇要移到 #1 的 CLI（1-%s）：",
    "swap_invalid": "✗ 無效的選擇：%s",
    "swap_already_first": "▸ %s 已經是 #1 — 不需變更",
    "swap_new_order": "✓ 新的順序：",
    "swap_saved": "  已儲存到 %s — 下一次 aicp 就會採用",
}


def _t(lang: str, msgid: str, english: str, *args: object) -> str:
    """Translate for *lang*, chosen per call rather than at import.

    ``i18n.t`` resolves ``AICP_LANG`` once at import time, which is right for
    a one-shot run and wrong here: the language row has to repaint the menu
    in the language just picked, in the same process, before anything else
    happens. Lookup order is the shared catalogue first, this module's bridge
    second, the English at the call site last — a missing msgid degrades to
    English, never to a blank line.
    """
    text = english
    if lang != "en":
        text = i18n.CATALOG.get(msgid) or _ZH.get(msgid, english)
    return text % args if args else text


@dataclass
class MenuState:
    """The live values a menu session is editing, plus where they persist."""

    path: Path
    do_commit: bool
    do_push: bool
    lang: str
    chain: list[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> MenuState:
        return cls(
            path=settings.path,
            do_commit=settings.do_commit,
            do_push=settings.do_push,
            lang=settings.lang,
            chain=list(settings.cli_chain),
        )


@dataclass(frozen=True)
class Row:
    """One setting. See this module's docstring for how to add another."""

    key: str
    group: tuple[str, str] | None
    label: tuple[str, str]
    help: tuple[str, str]
    value: Callable[[MenuState], str]
    accent: Callable[[MenuState], str]
    cycle: Callable[[MenuState, int], str]


def _on_off(state: MenuState, flag: bool) -> str:
    return _t(state.lang, "config_on", "On") if flag else _t(state.lang, "config_off", "Off")


def _toggle_commit(state: MenuState, _direction: int) -> str:
    state.do_commit = not state.do_commit
    return "1" if state.do_commit else "0"


def _toggle_push(state: MenuState, _direction: int) -> str:
    state.do_push = not state.do_push
    return "1" if state.do_push else "0"


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
        key="AICP_CLI_ORDER",
        group=None,
        label=("config_cli_first", "First AI CLI"),
        help=(
            "config_help_cli",
            "Tried first; the rest keep their order behind it. Missing ones are skipped.",
        ),
        value=lambda s: s.chain[0],
        accent=lambda _s: CYAN,
        cycle=_rotate_chain,
    ),
)


def _panel(state: MenuState, selected: int | None = None) -> list[str]:
    """The framed settings box, numbered for the typed-choice surface."""
    rows: list[tuple[str, str]] = []
    for i, row in enumerate(ROWS, start=1):
        if row.group:
            rows.append((_t(state.lang, *row.group), ""))
        marker = "›" if selected == i else " "
        rows.append(
            (
                f"{marker} {i}) {_t(state.lang, *row.label)}",
                f"{row.accent(state)}{row.value(state)}{RESET}",
            )
        )
    return render_panel(rows, _t(state.lang, "config_title", "aicp config"), BLUE)


def _write(state: MenuState, row: Row, direction: int, out: IO[str]) -> bool:
    value = row.cycle(state, direction)
    if persist_key(row.key, value, state.path):
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
        for line in _panel(state):
            print(line, file=out)
        print(
            f"{DIM}{_t(state.lang, 'config_keys_plain', '1-%s change · q quit · saves as you go', len(ROWS))}{RESET}",
            file=out,
        )
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
            if not _write(state, ROWS[int(choice) - 1], 1, out):
                return 1
        else:
            print(
                f"{RED}{_t(state.lang, 'config_bad_number', '⚠ Enter one of the setting numbers shown above.')}{RESET}",
                file=out,
            )
        print(file=out)


def _tui(state: MenuState, stdin: IO[str], out: IO[str]) -> int:
    """Arrow-key surface. Repaints in place by walking back up the frame it
    just drew; ``\\033[J`` erases to the end of the screen because switching
    to a language with narrower rows would otherwise leave the previous,
    wider frame's right-hand border on screen as a second column of │."""
    selected = 1
    lines = _panel(state, selected)
    for line in lines:
        print(line, file=out)
    while True:
        key = read_key(stdin, out)
        if key == "quit":
            return 0
        if key == "up":
            selected = selected - 1 if selected > 1 else len(ROWS)
        elif key == "down":
            selected = selected + 1 if selected < len(ROWS) else 1
        elif key in ("left", "right", "enter"):
            if not _write(state, ROWS[selected - 1], -1 if key == "left" else 1, out):
                return 1
        else:
            continue
        out.write(f"\033[{len(lines)}A\033[J")
        lines = _panel(state, selected)
        for line in lines:
            print(line, file=out)


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
