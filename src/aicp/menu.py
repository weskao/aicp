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

The Skills and Doctor rows were added exactly that way, and are the reason
a row may carry an ``action`` instead of a ``cycle``: they *do* something
(install skills, print a health report) rather than persist a setting, and
they show live status in their value column. They are rows and not
subcommands on purpose — ``aicp`` and ``aicp --config`` are the only two
things this tool ever asks anyone to remember.
"""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import gitflow, i18n, skills
from ._keyreader import is_interactive, read_key, read_line
from ._utils import BLUE, CYAN, DIM, GREEN, RED, RESET, YELLOW

# _KEY_RE and _SYSTEM_RESOLVED are read, not copied: the doctor reports on the
# loader's own verdict about a line, so it has to ask with the loader's own key
# rule, and has to name the keys that may never come from a file at all.
from .config import (
    _KEY_RE,
    _SYSTEM_RESOLVED,
    DENYLIST,
    Settings,
    load_config,
    persist_key,
    resolve,
    timeout_bin,
)
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
    "config_skills": "Skills",
    "config_doctor": "健康檢查",
    "config_help_skills": "把 aicp 的 /commit 與 /safe-git-push 裝進每個 AI CLI。你自己的同名檔案一律保留。",
    "config_help_doctor": "檢查平常不會出聲的問題：timeout、.aicprc 被略過的行、skills、git remote。",
    "skills_none": "沒有已設定的 CLI",
    "skills_missing": "⚠ %s 缺少",
    "skills_older": "⚠ %s 需更新",
    "skills_yours": "✓ 保留你自己的",
    "skills_ok": "✓ 已安裝",
    "skills_title": "Skills",
    "skills_skipped": "沒有安裝：%s",
    "config_group_tools": "工具",
    "skills_state_missing": "尚未安裝",
    "skills_state_current": "已安裝（%s）",
    "skills_state_older": "來自舊版 aicp（%s）— 可以更新",
    "skills_state_foreign": "你自己的檔案 — aicp 會直接沿用",
    "skills_install_q": "要安裝／更新這 %s 個 skill 嗎？（y／N）：",
    "skills_keep_note": "保留你自己的 %s 個 skill — aicp 會直接沿用，這不是錯誤。",
    "skills_force_q": "要改用 aicp 的版本嗎？你的檔案會先備份（y／N）：",
    "skills_installed": "%s 已安裝到 %s",
    "skills_upgraded": "%s 已為 %s 更新",
    "skills_overwritten": "%s 已為 %s 換成 aicp 的版本 — 你的原檔在 %s",
    "skills_kept": "%s：保留你自己的版本 — aicp 會直接沿用",
    "skills_nothing": "沒有需要處理的項目。",
    "doctor_clear": "✓ 一切正常",
    "doctor_one": "⚠ 1 項提醒",
    "doctor_many": "⚠ %s 項提醒",
    "health_title": "健康檢查",
    "health_footer": "提醒只是讓你知道，不是失敗 — aicp 照樣可以跑。",
    "health_timeout_ok": "每次 CLI 呼叫的 timeout 用 %s",
    "health_timeout_missing": "PATH 上沒有 timeout／gtimeout — 每次 AI CLI 呼叫都沒有時間上限",
    "health_no_config": "還沒有 %s — 全部採用內建預設值",
    "health_config_ok": "%s：讀到 %s 個設定",
    "health_env_only": "%s 在 %s 裡會被忽略 — 這個項目只能從環境變數／PATH 取得",
    "health_dropped": "%s 被 %s 略過 — 值裡有不允許的字元",
    "health_skills_none": "找不到任何 AI CLI 的設定目錄 — 沒有地方可以安裝 skills",
    "health_skill_missing": "%s：%s skill 尚未安裝 — 用 Skills 那一列安裝",
    "health_skill_older": "%s：%s skill 來自舊版 aicp — 用 Skills 那一列更新",
    "health_skill_foreign": "%s：%s skill 是你自己的 — aicp 會直接沿用",
    "health_skills_ok": "所有已設定的 CLI 都有 skills",
    "health_no_branch": "目前不在分支上（detached HEAD 或不是 git repo）— aicp 需要分支才能 push",
    "health_no_remote": "這個 repo 沒有 git remote — 可以 commit，但沒有地方 push",
    "health_git_ok": "分支 %s → remote %s",
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
    #: Probe caches for the two action rows. The panel repaints on every
    #: keypress, and both of those rows show live status in their value
    #: column — walking the filesystem and shelling out to git once per
    #: repaint would be a real, per-keystroke cost. Probed once per session
    #: instead, and dropped by the Skills action whenever it changes anything.
    skills_status: list[skills.SkillStatus] | None = None
    health: list[tuple[str, str]] | None = None

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
    print(_t(state.lang, "skills_title", "Skills"), file=out)
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
    # each: they are not a problem to solve, and on a five-CLI roster they
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


def _config_health(state: MenuState) -> list[tuple[str, str]]:
    """Which ``.aicprc`` lines the loader refused — otherwise entirely silent.

    Derived from :func:`~aicp.config.load_config`'s own verdict rather than a
    second copy of its rules: a key that is in the file and not in what it
    returned is, by definition, a line that did not take effect.
    """
    try:
        text = state.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [(_OK, _t(state.lang, "health_no_config", "no %s yet — built-in defaults apply", state.path))]
    # The loader announces a denied key on stderr as it goes; this report is
    # about to say the same thing in the panel, so the second copy is noise —
    # and the panel repaints, which would print it again on every keypress.
    with contextlib.redirect_stderr(io.StringIO()):
        accepted = load_config(state.path)
    refused: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].rstrip()
        if _KEY_RE.match(key) and key not in accepted and key not in refused:
            refused.append(key)
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
    print(_t(state.lang, "health_title", "Health check"), file=out)
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
        label=("config_doctor", "Health check"),
        help=(
            "config_help_doctor",
            "What is otherwise silent: timeout, skipped .aicprc lines, skills, git remote.",
        ),
        value=_doctor_value,
        accent=lambda s: YELLOW if _WARN in _doctor_value(s) else GREEN,
        action=_doctor_action,
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


def _write(state: MenuState, row: Row, direction: int, stdin: IO[str], out: IO[str]) -> bool:
    if row.action is not None:
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
            if not _write(state, ROWS[int(choice) - 1], 1, stdin, out):
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
            row = ROWS[selected - 1]
            if not _write(state, row, -1 if key == "left" else 1, stdin, out):
                return 1
            if row.action is not None:
                # An action prints below the frame; redraw under its output
                # rather than scrolling back up over what it just said.
                lines = _panel(state, selected)
                for line in lines:
                    print(line, file=out)
                continue
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
