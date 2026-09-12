"""The Skills and Doctor rows of ``--config``.

Two rows, not two subcommands: the whole point is that only ``aicp`` and
``aicp --config`` are ever memorised. So these are driven exactly like every
other row in ``test_menu.py`` — through the NUMBERED fallback, which is also
the CI surface, hence the "never blocks on a pipe" tests at the bottom.

Every test runs against conftest's fake ``$HOME`` (``pinned_environment``),
so a config dir only exists here if the test made it: the roster's other
CLIs are legitimately "not installed" and drop out of the report on their
own. Local fixtures only — ``tests/conftest.py`` is never edited from here.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aicp import skills
from aicp.menu import ROWS, config_menu

SKILLS_ROW = 5
DOCTOR_ROW = 6

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
    assert len(ROWS) == 6
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
    code, out, cfg = menu("7\nq\n")
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


def test_the_doctor_row_reports_a_dropped_aicprc_line(menu):
    _, out, _ = menu(
        f"{DOCTOR_ROW}\nq\n", initial="AICP_JUNK=va$lue\nAICP_DO_PUSH=1\n"
    )
    assert "AICP_JUNK" in out


def test_the_doctor_row_reports_a_denylisted_key(menu):
    _, out, _ = menu(f"{DOCTOR_ROW}\nq\n", initial="AICP_TG_SEND=/tmp/x.sh\n")
    assert "AICP_TG_SEND" in out


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


# ── CI safety: neither row blocks on a pipe ──────────────────────────────────


@pytest.mark.parametrize("row", [SKILLS_ROW, DOCTOR_ROW])
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
