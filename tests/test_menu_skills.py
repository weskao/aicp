"""The Skills, Agents and Doctor rows of ``--config``.

Rows, not subcommands: the whole point is that only ``aicp`` and ``aicp
--config`` are ever memorised. So these are driven exactly like every other
row in ``test_menu.py`` — through the NUMBERED fallback, which is also the CI
surface, hence the "never blocks on a pipe" tests at the bottom. The Agents
row's scriptable twin, ``aicp --agents``, is covered in ``test_agentcfg.py``;
what is tested here is the part unique to the menu — that a toggle refreshes
the row's cached status and keeps the saved CLI order loadable.

Every test runs against conftest's fake ``$HOME`` (``pinned_environment``),
so a config dir only exists here if the test made it: the roster's other
CLIs are legitimately "not installed" and drop out of the report on their
own. Local fixtures only — ``tests/conftest.py`` is never edited from here.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from aicp import skills
from aicp.menu import ROWS, config_menu


def _row_number(msgid: str) -> int:
    """Where a row sits in the menu, by its label id rather than a literal.

    ROWS is data and the menu derives its numbering from the tuple's length;
    pinning a digit here just means every added row breaks a dozen tests that
    are not about ordering at all.
    """
    return next(i for i, row in enumerate(ROWS, start=1) if row.label[0] == msgid)


SKILLS_ROW = _row_number("config_skills")
AGENTS_ROW = _row_number("config_agents")
DOCTOR_ROW = _row_number("config_doctor")

FOREIGN_TEXT = "# my own commit skill\n"


@pytest.fixture
def home(pinned_environment) -> Path:
    """The fake ``$HOME`` conftest already pointed ``HOME`` at."""
    return Path.home()


@pytest.fixture
def menu(tmp_path, monkeypatch):
    """Factory: run the menu against a throwaway config with *keys* typed at
    its prompt. Returns ``(exit_code, output, config_path)``."""

    def _run(keys: str, *, initial: str | None = None):
        cfg = tmp_path / "menu.aicprc"
        if initial is not None:
            cfg.write_text(initial, encoding="utf-8")
        monkeypatch.setenv("AICP_CONFIG", str(cfg))
        out = io.StringIO()
        code = config_menu(stdin=io.StringIO(keys), stdout=out)
        return code, out.getvalue(), cfg

    return _run


@pytest.fixture
def codex(home) -> Path:
    """A configured ``codex`` whose skills are all missing."""
    root = home / ".codex"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def codex_foreign(codex) -> Path:
    """…plus the user's OWN commit skill (no aicp sidecar) already there."""
    target = codex / skills.SKILLS["commit"].rel_target
    target.parent.mkdir(parents=True)
    target.write_text(FOREIGN_TEXT, encoding="utf-8")
    return target


# ── the rows exist, and carry their status inline ────────────────────────────


def test_both_rows_are_appended_to_the_row_model(menu):
    _, out, _ = menu("q\n")
    assert f"{SKILLS_ROW}) Skills" in out
    assert f"{DOCTOR_ROW}) Health check" in out


def test_the_skills_row_carries_its_status_inline(menu, codex):
    _, out, _ = menu("q\n")
    line = next(ln for ln in out.splitlines() if f"{SKILLS_ROW}) Skills" in ln)
    assert "codex" in line and "missing" in line


def test_the_doctor_row_carries_its_status_inline(menu, monkeypatch):
    monkeypatch.setattr("aicp.menu.timeout_bin", lambda: None)
    _, out, _ = menu("q\n")
    line = next(ln for ln in out.splitlines() if f"{DOCTOR_ROW}) Health check" in ln)
    assert "warning" in line


def test_the_prompt_and_bounds_grew_with_the_new_rows(menu):
    code, out, cfg = menu(f"{len(ROWS) + 1}\nq\n")
    assert code == 0
    assert f"1-{len(ROWS)}" in out
    assert "Enter one of the setting numbers" in out
    assert not cfg.exists(), "a rejected pick must write nothing at all"


def test_the_new_rows_persist_nothing_to_the_config(menu, codex):
    """They act; they are not settings. Picking one must not write .aicprc."""
    _, _, cfg = menu(f"{SKILLS_ROW}\nn\n{DOCTOR_ROW}\nq\n")
    assert not cfg.exists()


# ── Skills: the user's own file wins ─────────────────────────────────────────


def test_a_foreign_skill_is_kept_by_default(menu, codex_foreign):
    _, out, _ = menu(f"{SKILLS_ROW}\nn\nn\nq\n")
    assert codex_foreign.read_text(encoding="utf-8") == FOREIGN_TEXT
    assert list(codex_foreign.parent.glob("*.bak*")) == []
    assert "aicp will use" in out, "keeping your own skill is not a failure"


def test_installing_the_missing_ones_still_keeps_a_foreign_skill(menu, codex_foreign):
    """Answering yes to "install what's missing" is not consent to overwrite."""
    _, _, _ = menu(f"{SKILLS_ROW}\ny\nn\nq\n")
    assert codex_foreign.read_text(encoding="utf-8") == FOREIGN_TEXT
    assert (codex_foreign.parent.parent / "safe-git-push").is_dir()


def test_selecting_skills_installs_a_missing_skill(menu, codex):
    _, out, _ = menu(f"{SKILLS_ROW}\ny\nq\n")
    target = codex / skills.SKILLS["commit"].rel_target
    assert target.exists()
    assert skills.detect(_cli("codex"), "commit") == skills.CURRENT
    assert "commit installed into codex" in out


def test_the_inline_status_refreshes_after_an_install(menu, codex):
    """The row's probe is cached per session (the panel repaints on every
    keypress); installing something has to drop that cache."""
    _, out, _ = menu(f"{SKILLS_ROW}\ny\nq\n")
    rendered = [ln for ln in out.splitlines() if f"{SKILLS_ROW}) Skills" in ln]
    assert "missing" in rendered[0]
    assert "missing" not in rendered[-1], "the row must not show a stale probe"


def test_a_cli_without_a_config_dir_is_never_created(menu, codex):
    menu(f"{SKILLS_ROW}\ny\nq\n")
    assert not (Path.home() / ".vibe").exists()


# ── Doctor: what is otherwise silent ─────────────────────────────────────────


def test_the_doctor_row_reports_a_missing_timeout_binary(menu, monkeypatch):
    monkeypatch.setattr("aicp.menu.timeout_bin", lambda: None)
    _, out, _ = menu(f"{DOCTOR_ROW}\nq\n")
    assert "no timeout/gtimeout on PATH" in out
    assert "no time limit" in out, "say what the missing binary actually costs"


def test_the_doctor_row_reports_a_dropped_config_key(menu):
    _, out, _ = menu(
        f"{DOCTOR_ROW}\nq\n",
        initial=json.dumps({"AICP_JUNK": "va$lue", "AICP_DO_PUSH": "1"}),
    )
    assert "AICP_JUNK" in out


def test_the_doctor_row_reports_a_denylisted_key(menu):
    _, out, _ = menu(
        f"{DOCTOR_ROW}\nq\n", initial=json.dumps({"AICP_TIMING_LOG": "/tmp/x.log"})
    )
    assert "AICP_TIMING_LOG" in out


def test_the_report_is_not_stale_after_another_row_writes_the_config(menu):
    """Row 1 creates ``.aicprc`` mid-session; the report is about that very
    file, so it must be re-probed rather than served from the cache taken
    when the panel was first drawn."""
    _, out, cfg = menu(f"1\n{DOCTOR_ROW}\nq\n")
    assert cfg.exists()
    assert "built-in defaults apply" not in out, "the doctor read a stale probe"
    assert f"{cfg.name}: 1 setting(s) read" in out


def test_warnings_do_not_read_as_errors(menu, monkeypatch):
    monkeypatch.setattr("aicp.menu.timeout_bin", lambda: None)
    _, out, _ = menu(f"{DOCTOR_ROW}\nq\n")
    assert "✗" not in out, "a warning is not a failure"


def test_doctor_details_expand_when_the_row_is_highlighted(monkeypatch):
    """Arrow onto Health check → the full report is already in the panel.

    Enter must not be required to see it (and is a no-op on that row in the
    TUI). The numbered fallback still prints via the row action; this covers
    the highlight path the suite can drive without a TTY.
    """
    from aicp.contracts import ROSTER
    from aicp.menu import MenuState, _panel

    monkeypatch.setattr("aicp.menu.timeout_bin", lambda: None)
    state = MenuState(
        Path("menu.aicprc"), True, True, "en", [c.name for c in ROSTER]
    )

    on_doctor = "\n".join(_panel(state, selected=DOCTOR_ROW))
    on_other = "\n".join(_panel(state, selected=1))

    assert "no timeout/gtimeout on PATH" in on_doctor
    assert "no time limit" in on_doctor
    assert "no timeout/gtimeout on PATH" not in on_other


def test_highlighting_doctor_does_not_widen_the_panel(monkeypatch):
    """Same width contract as every other row — long health lines are fitted
    to the help-text reserve, not allowed to stretch the frame."""
    from aicp.contracts import ROSTER
    from aicp.menu import MenuState, _panel
    from aicp.present import width

    monkeypatch.setattr("aicp.menu.timeout_bin", lambda: None)
    state = MenuState(
        Path("menu.aicprc"), True, True, "en", [c.name for c in ROSTER]
    )

    widths = {width(_panel(state, selected=i)[0]) for i in range(1, len(ROWS) + 1)}

    assert len(widths) == 1, "highlighting Doctor must not change the frame width"


# ── Agents: the row that turns an AI CLI off and on ──────────────────────────


def _agent_number(name: str) -> int:
    """Where *name* sits in the Agents row's own list — derived, so a change
    to the built-in roster moves this rather than breaking it."""
    from aicp import agents

    return next(i for i, row in enumerate(agents.inventory(), start=1) if row.name == name)


def test_the_agents_row_toggles_an_agent_and_refreshes_its_own_status(menu, home):
    grok = _agent_number("grok")
    code, out, _ = menu(f"{AGENTS_ROW}\n{grok}\n{AGENTS_ROW}\n{grok}\nq\n")
    assert code == 0
    assert "grok disabled" in out and "grok enabled" in out
    # The row's value column is probed once and cached; a toggle has to drop
    # that cache or the menu keeps reporting the roster it started with.
    assert "7 active · 1 off" in out, out
    assert not (home / ".aicp" / "agents.json").exists(), "enable undid the write"


def test_the_agents_row_prunes_the_saved_cli_order_it_just_invalidated(menu, home):
    """The chain row and the registry are edited in the same session, so the
    order must not be left naming an agent that no longer resolves."""
    _, _, cfg = menu(
        f"{AGENTS_ROW}\n{_agent_number('grok')}\nq\n",
        initial=json.dumps({"aicp_cli_order": "grok claude copilot agy codex vibe opencode devin"}),
    )
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_cli_order"].split() == [
        "claude",
        "copilot",
        "agy",
        "codex",
        "vibe",
        "opencode",
        "devin",
    ]


def test_an_out_of_range_agent_number_changes_nothing(menu, home):
    _, out, _ = menu(f"{AGENTS_ROW}\n99\nq\n")
    assert "invalid choice" in out
    assert not (home / ".aicp" / "agents.json").exists()


def test_the_arrow_key_tui_opens_an_agents_loop_that_toggles_in_place(home):
    """The TUI surface gets its own ↑↓/⏎ loop instead of the numbered
    fallback's typed-choice prompt — driven here through ``_tui`` directly
    (as ``_panel``/``_order_motion`` are elsewhere in this suite), since
    ``read_key``'s non-TTY fallback already maps plain lines onto the same
    semantic keys a real terminal would send.
    """
    from aicp import agents
    from aicp.menu import MenuState, _tui

    state = MenuState(
        home / ".aicp" / "menu.aicprc", True, True, "en", [row.name for row in agents.inventory(home)]
    )
    down_to_agents = "down\n" * (AGENTS_ROW - 1)
    # Inside the Agents loop: move to the 2nd row, toggle it off, leave the
    # loop, then quit the outer menu.
    code = _tui(state, io.StringIO(f"{down_to_agents}enter\ndown\nspace\nquit\nquit\n"), io.StringIO())

    assert code == 0
    second = agents.inventory(home)[1]
    assert second.state == agents.DISABLED


def test_leaving_agents_erases_the_parent_panel_before_redraw(home):
    """q from Agents must clear the main config panel that stayed above it.

    Agents is drawn under the still-visible parent. Erasing only the child
    and then printing a fresh parent leaves the old one on screen, so the
    next Enter Agents stacks another copy — the "panels multiply" bug.
    StringIO cannot honour the cursor-up erase, so this asserts the CSI
    distance itself covers parent + child.
    """
    from aicp import agents
    from aicp.menu import MenuState, _agents_lines, _frame_rows, _panel, _tui

    state = MenuState(
        home / ".aicp" / "menu.aicprc", True, True, "en", [row.name for row in agents.inventory(home)]
    )
    out = io.StringIO()
    parent_rows = _frame_rows(_panel(state, selected=AGENTS_ROW, out=out), out)
    child_rows = _frame_rows(_agents_lines(state, selected=1), out)
    down_to_agents = "down\n" * (AGENTS_ROW - 1)
    buf = io.StringIO()

    code = _tui(state, io.StringIO(f"{down_to_agents}enter\nquit\nquit\n"), buf)

    assert code == 0
    text = buf.getvalue()
    combined = f"\033[{parent_rows + child_rows}A\033[J"
    stepped = f"\033[{child_rows}A\033[J\033[{parent_rows}A\033[J"
    ups = []
    needle, i = "\033[", 0
    while True:
        j = text.find(needle, i)
        if j < 0:
            break
        k = text.find("A", j)
        if k > j and text[j + 2 : k].isdigit():
            ups.append(text[j + 2 : k])
        i = j + 2
    assert combined in text or stepped in text, (
        f"expected erase of parent+child ({parent_rows}+{child_rows}); CSI ups: {ups}"
    )


# ── Agents sub-panel: reorder, edit, add, reset ─────────────────────────────


def _roster(home: Path) -> list[str]:
    from aicp import agents

    return [row.name for row in agents.inventory(home)]


def _agents_session(home: Path, keys: str, *, tail: str = "quit\nquit\n"):
    """Open the Agents sub-panel from the real ``_tui`` and type *keys* in
    it. ``tail`` leaves the sub-panel and the menu; pass ``""`` to end on EOF
    instead, so a form can be cut off mid-way."""
    from aicp.menu import MenuState, _tui

    state = MenuState(home / ".aicp" / "menu.aicprc", True, True, "en", _roster(home))
    out = io.StringIO()
    down_to_agents = "down\n" * (AGENTS_ROW - 1)
    code = _tui(state, io.StringIO(f"{down_to_agents}enter\n{keys}{tail}"), out)
    assert code == 0
    return state, out.getvalue()


def _saved_order(home: Path) -> list[str]:
    cfg = home / ".aicp" / "menu.aicprc"
    return json.loads(cfg.read_text(encoding="utf-8"))["aicp_cli_order"].split()


def _user_agents(home: Path) -> dict:
    from aicp import agents

    path = agents.user_registry_path(home)
    return json.loads(path.read_text(encoding="utf-8"))["agents"] if path.exists() else {}


def _answers(values: dict[str, str]) -> str:
    """One typed line per editable field, in the order the form asks."""
    from aicp.agentcfg import EDITABLE

    return "".join(f"{values.get(field, '')}\n" for field in EDITABLE)


def test_the_cursor_follows_an_agent_moved_up_twice(home):
    names = _roster(home)
    state, _ = _agents_session(home, "down\ndown\nleft\nleft\n")
    expected = [names[2], names[0], names[1], *names[3:]]
    assert state.chain == expected
    assert _saved_order(home) == expected


def test_right_moves_the_selected_agent_down_one(home):
    names = _roster(home)
    _agents_session(home, "right\n")
    assert _saved_order(home) == [names[1], names[0], *names[2:]]


@pytest.mark.parametrize("keys", ["left\n", "up\nright\n"], ids=["first-left", "last-right"])
def test_moving_past_either_end_writes_nothing(home, keys):
    _agents_session(home, keys)
    assert not (home / ".aicp" / "menu.aicprc").exists()
    assert _user_agents(home) == {}


def test_a_disabled_agent_has_no_place_in_the_order_to_move(home):
    last = _roster(home)[-1]
    state, _ = _agents_session(home, "up\nspace\nleft\nleft\n")
    assert last not in state.chain
    assert _user_agents(home) == {last: {"disabled": True}}
    assert not (home / ".aicp" / "menu.aicprc").exists()


def test_t_swaps_the_selected_agent_with_the_first(home):
    names = _roster(home)
    _agents_session(home, "down\ndown\nt\n")
    assert _saved_order(home) == [names[2], names[1], names[0], *names[3:]]


def test_the_panel_lists_agents_in_try_order_with_disabled_ones_last(home):
    import re

    from aicp import agentcfg
    from aicp.menu import MenuState, _agents_lines

    names = _roster(home)
    agentcfg.apply("disable", names[0])
    chain = list(reversed(names[1:]))
    state = MenuState(home / ".aicp" / "menu.aicprc", True, True, "en", chain)
    lines = [re.sub(r"\033\[[0-9;]*m", "", line) for line in _agents_lines(state, selected=1)]

    rows = [(n, line) for line in lines for n in [*chain, names[0]] if re.search(rf"\) {n}\b", line)]
    assert [n for n, _ in rows] == [*chain, names[0]]
    assert [n for n, line in rows if "#1" in line] == [chain[0]]


def test_e_overrides_only_the_field_that_was_typed(home):
    at = _roster(home).index("claude")
    _agents_session(home, "down\n" * at + "e\n" + _answers({"executable": "/opt/claude"}))
    assert _user_agents(home) == {"claude": {"executable": "/opt/claude"}}


def test_a_refused_edit_writes_nothing_and_says_why(home):
    _, out = _agents_session(home, "e\n" + _answers({"args": "--prompt,--yes"}))
    assert _user_agents(home) == {}
    assert "args must contain one {prompt} argument" in out


def test_a_form_cut_off_by_eof_writes_nothing(home):
    _agents_session(home, "e\n/opt/claude\n", tail="")
    assert _user_agents(home) == {}


def test_a_adds_a_new_agent_in_full_at_the_end_of_the_chain(home):
    minimax = {
        "executable": "minimax",
        "config_dir": "~/.minimax",
        "memory_file": "AGENTS.md",
        "skills_dir": "skills",
        "skills": "safe-git-push",
        "args": "--prompt,{prompt},--yes",
    }
    state, _ = _agents_session(home, "a\nminimax\n" + _answers(minimax))
    assert _user_agents(home) == {
        "minimax": {**minimax, "skills": ["safe-git-push"], "args": ["--prompt", "{prompt}", "--yes"]}
    }
    assert state.chain[-1] == "minimax"


def test_a_leaves_config_dir_windows_optional_and_stores_it_when_typed(home):
    minimax = {
        "executable": "minimax",
        "config_dir": "~/.minimax",
        "memory_file": "AGENTS.md",
        "skills_dir": "skills",
        "skills": "safe-git-push",
        "args": "--prompt,{prompt},--yes",
    }
    _agents_session(home, "a\nminimax\n" + _answers({**minimax, "config_dir_windows": "~/AppData/Roaming/minimax"}))
    assert _user_agents(home)["minimax"]["config_dir_windows"] == "~/AppData/Roaming/minimax"


def test_a_pressing_enter_through_every_field_writes_the_guessed_defaults(home):
    """Nothing on disk under this name — every field but ``name`` falls back
    to :func:`agentcfg.guess_defaults`'s generic guess, and ⏎ accepts all of
    them, same as ⏎ keeping a value in the edit form."""
    _agents_session(home, "a\nminimax\n" + _answers({}))
    assert _user_agents(home) == {
        "minimax": {
            "executable": "minimax",
            "config_dir": "~/.config/minimax",
            "memory_file": "AGENTS.md",
            "skills_dir": "skills",
            "skills": ["commit", "safe-git-push"],
            "args": ["-p", "{prompt}"],
        }
    }


def test_a_guesses_a_config_dir_already_on_disk_and_what_is_inside_it(home):
    """A config dir matching the name, with a memory file and a singular
    ``skill`` dir already in it, is picked up over the generic fallback."""
    config_dir = home / ".config" / "minimax"
    config_dir.mkdir(parents=True)
    (config_dir / "CLAUDE.md").touch()
    (config_dir / "skill").mkdir()
    _agents_session(home, "a\nminimax\n" + _answers({}))
    written = _user_agents(home)["minimax"]
    assert written["memory_file"] == "CLAUDE.md"
    assert written["skills_dir"] == "skill"


def test_a_rejects_a_bad_or_taken_name_before_asking_the_other_fields(home):
    fields = _answers({"executable": "x", "config_dir": "~/.x", "memory_file": "AGENTS.md",
                       "skills_dir": "skills", "skills": "commit", "args": "{prompt}"})
    _agents_session(home, "a\nmy agent\nclaude\nminimax\n" + fields)
    assert set(_user_agents(home)) == {"minimax"}


@pytest.mark.parametrize(("answer", "kept"), [("y", False), ("n", True), ("", True)])
def test_r_asks_before_dropping_an_override(home, answer, kept):
    from aicp import agentcfg

    agentcfg.apply("set", "claude", ["executable=/opt/claude"])
    at = _roster(home).index("claude")
    _agents_session(home, "down\n" * at + f"r\n{answer}\n")
    assert ("claude" in _user_agents(home)) is kept


def test_r_on_an_agent_with_no_override_says_there_is_nothing_to_reset(home):
    _, out = _agents_session(home, "r\n")
    assert "nothing to reset" in out
    assert _user_agents(home) == {}


def test_enter_opens_an_action_menu_whose_items_do_what_their_hotkeys_do(home):
    from aicp.menu import AGENT_ACTIONS

    names = _roster(home)
    item = [key for key, _hint, _label in AGENT_ACTIONS].index("left")
    _agents_session(home, "down\nenter\n" + "down\n" * item + "enter\n")
    assert _saved_order(home) == [names[1], names[0], *names[2:]]


# ── CI safety: neither row blocks on a pipe ──────────────────────────────────


@pytest.mark.parametrize("row", [SKILLS_ROW, AGENTS_ROW, DOCTOR_ROW])
def test_neither_row_blocks_on_non_tty_stdin(menu, codex_foreign, row):
    """EOF mid-prompt is "no", not "wait forever" — this tool runs in CI."""
    code, _, cfg = menu(f"{row}\n")
    assert code == 0
    assert not cfg.exists()
    assert codex_foreign.read_text(encoding="utf-8") == FOREIGN_TEXT


def test_both_actions_render_in_the_language_just_picked(menu, codex_foreign, monkeypatch):
    """Row 3 switches to zh-TW mid-session, so every string these two rows
    print is formatted from ``i18n.CATALOG`` — a translation whose ``%s``
    count does not match its English original raises right here."""
    monkeypatch.setattr("aicp.menu.timeout_bin", lambda: None)
    code, out, _ = menu(f"3\n{SKILLS_ROW}\nn\nn\n{DOCTOR_ROW}\nq\n")
    assert code == 0
    assert "健康檢查" in out


def test_the_rows_run_no_ai_cli(menu, codex, stub_cli, call_log):
    stub_cli()
    menu(f"{SKILLS_ROW}\ny\n{DOCTOR_ROW}\nq\n")
    assert not call_log.exists()


def _cli(name: str):
    from aicp.contracts import ROSTER

    return next(c for c in ROSTER if c.name == name)
