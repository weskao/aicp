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
    assert "5 active · 1 off" in out, out
    assert not (home / ".aicp" / "agents.json").exists(), "enable undid the write"


def test_the_agents_row_prunes_the_saved_cli_order_it_just_invalidated(menu, home):
    """The chain row and the registry are edited in the same session, so the
    order must not be left naming an agent that no longer resolves."""
    _, _, cfg = menu(
        f"{AGENTS_ROW}\n{_agent_number('grok')}\nq\n",
        initial=json.dumps({"aicp_cli_order": "grok claude copilot agy codex vibe"}),
    )
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_cli_order"].split() == [
        "claude",
        "copilot",
        "agy",
        "codex",
        "vibe",
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
    code = _tui(state, io.StringIO(f"{down_to_agents}enter\ndown\nenter\nquit\nquit\n"), io.StringIO())

    assert code == 0
    second = agents.inventory(home)[1]
    assert second.state == agents.DISABLED


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
