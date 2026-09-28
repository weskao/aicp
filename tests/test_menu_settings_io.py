"""The "Export settings" / "Import settings" rows of ``--config``.

A Tools row, not a subcommand, same as Skills/Agents/Doctor — see
``test_menu_skills.py``, whose fixtures and ``_row_number`` helper this file
mirrors. Driven through the NUMBERED fallback: each row's path prompt reads
from the same stdin the row-picker does, so a full interaction is one
multi-line ``keys`` string.

Local fixtures only — ``tests/conftest.py`` is never edited from here.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from aicp import menu as menu_module
from aicp.menu import ROWS, config_menu


def _row_number(msgid: str) -> int:
    return next(i for i, row in enumerate(ROWS, start=1) if row.label[0] == msgid)


EXPORT_ROW = _row_number("config_settings_export")
IMPORT_ROW = _row_number("config_settings_import")


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


def test_export_and_import_are_separate_rows(menu):
    _, out, _ = menu("q\n")
    assert f"{EXPORT_ROW}) Export settings" in out
    assert f"{IMPORT_ROW}) Import settings" in out


def test_export_writes_the_current_settings_to_the_given_file(menu, tmp_path):
    dest = tmp_path / "out.json"
    code, out, cfg = menu(
        f"{EXPORT_ROW}\n{dest}\nq\n",
        initial=json.dumps({"AICP_DO_COMMIT": "0", "AICP_LANG": "zh-TW"}),
    )
    assert code == 0
    assert "wrote" in out or "已寫入" in out
    assert json.loads(dest.read_text(encoding="utf-8")) == {
        "aicp_do_commit": "0",
        "aicp_lang": "zh-TW",
    }
    assert cfg.exists(), "export must not disturb the live config file"


def test_export_never_writes_a_denylisted_key(menu, tmp_path):
    dest = tmp_path / "out.json"
    menu(f"{EXPORT_ROW}\n{dest}\nq\n", initial=json.dumps({"AICP_CONFIG": "/evil"}))
    assert "aicp_config" not in json.loads(dest.read_text(encoding="utf-8"))


def test_import_loads_the_given_file(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_PUSH": "0"}), encoding="utf-8")
    code, out, cfg = menu(f"{IMPORT_ROW}\n{source}\nq\n")
    assert code == 0
    assert "imported" in out or "已匯入" in out
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_do_push"] == "0"


def test_import_merges_into_the_existing_file_rather_than_replacing_it(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_PUSH": "0"}), encoding="utf-8")
    _, _out, cfg = menu(
        f"{IMPORT_ROW}\n{source}\nq\n",
        initial=json.dumps({"AICP_TZ": "Etc/UTC"}),
    )
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["aicp_do_push"] == "0"
    assert data["aicp_tz"] == "Etc/UTC"


def test_import_reports_skipped_keys_without_writing_them(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_CONFIG": "/evil", "AICP_LANG": "en"}), encoding="utf-8")
    _, out, cfg = menu(f"{IMPORT_ROW}\n{source}\nq\n")
    assert "AICP_CONFIG" in out
    assert "aicp_config" not in json.loads(cfg.read_text(encoding="utf-8"))
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_lang"] == "en"


class _Keys(io.StringIO):
    """Serves typed lines; a line that is exactly ``^C`` is Ctrl+C instead."""

    def readline(self, *args):
        line = super().readline(*args)
        if line == "^C\n":
            raise KeyboardInterrupt
        return line


@pytest.mark.parametrize("row", [EXPORT_ROW, IMPORT_ROW])
def test_ctrl_c_cancels_and_writes_nothing(tmp_path, monkeypatch, row):
    cfg = tmp_path / "menu.aicprc"
    monkeypatch.setenv("AICP_CONFIG", str(cfg))
    out = io.StringIO()
    stdin = _Keys(f"{row}\n^C\nq\n")
    code = config_menu(stdin=stdin, stdout=out)
    assert code == 0
    assert not cfg.exists()
    # Cancelled back to the menu, not out of it: the panel is drawn again.
    assert out.getvalue().count(f"{row}) ") == 2


def test_empty_enter_asks_again_instead_of_cancelling(menu, tmp_path):
    dest = tmp_path / "out.json"
    _, out, _cfg = menu(f"{EXPORT_ROW}\n\n{dest}\nq\n")
    assert out.count("Save to?") == 2
    assert dest.exists()


def test_import_reports_when_the_source_has_nothing_importable(menu, tmp_path):
    missing = tmp_path / "missing.json"
    _, out, cfg = menu(f"{IMPORT_ROW}\n{missing}\nq\n")
    assert "nothing importable" in out or "沒有可匯入" in out
    assert not cfg.exists()


def test_export_reports_failure_instead_of_writing(monkeypatch, tmp_path):
    """Same posture as ``persist_key``: a write that cannot happen is a
    reported failure, and the menu keeps running rather than crashing."""
    blocked = tmp_path / "a-file"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setenv("AICP_CONFIG", str(tmp_path / "menu.aicprc"))
    out = io.StringIO()
    code = config_menu(stdin=io.StringIO(f"{EXPORT_ROW}\n{blocked / 'out.json'}\nq\n"), stdout=out)
    assert code == 0
    assert "could not write" in out.getvalue() or "無法寫入" in out.getvalue()


def test_import_refreshes_the_toggle_rows_shown_by_the_menu(menu, tmp_path):
    """An imported value must repaint, not just persist — the same promise
    every other row's ``cycle`` already makes."""
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_COMMIT": "0"}), encoding="utf-8")
    _, out, _cfg = menu(f"{IMPORT_ROW}\n{source}\nq\n")
    commit_lines = [ln for ln in out.splitlines() if "Run the /commit step" in ln]
    # Printed twice: once before the import (still On), once in the panel
    # the menu loop repaints right after — that second one must show Off.
    assert "Off" in commit_lines[-1]


def test_export_to_a_folder_names_the_file_itself(menu, tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    menu(f"{EXPORT_ROW}\n{folder}\nq\n", initial=json.dumps({"AICP_LANG": "en"}))
    (written,) = folder.glob("aicp-settings-*.json")
    assert json.loads(written.read_text(encoding="utf-8")) == {"aicp_lang": "en"}


def test_export_adds_json_to_a_bare_name(menu, tmp_path):
    menu(f"{EXPORT_ROW}\n{tmp_path / 'mine'}\nq\n", initial=json.dumps({"AICP_LANG": "en"}))
    assert (tmp_path / "mine.json").exists()


@pytest.mark.parametrize("quote", ["'", '"'])
def test_quoted_paths_are_unquoted(menu, tmp_path, monkeypatch, quote):
    """Finder/Explorer "copy as path" wraps the path in quotes; kept literally,
    that wrote a relative ``"/Users/...`` tree into the working directory."""
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "with space"
    folder.mkdir()
    menu(f"{EXPORT_ROW}\n{quote}{folder}{quote}\nq\n", initial=json.dumps({"AICP_LANG": "en"}))
    assert list(folder.glob("aicp-settings-*.json"))
    assert not (tmp_path / quote).exists()


# ── Arrow-key TUI: same erase-and-return as the Agents row ─────────────────


@pytest.fixture
def home(pinned_environment) -> Path:
    """The fake ``$HOME`` conftest already pointed ``HOME`` at — mirrors
    ``test_menu_skills.py``'s fixture of the same name."""
    return Path.home()


def _ups(text: str) -> list[int]:
    """Every ``\\033[<N>A`` cursor-up distance CSI actually wrote, in order."""
    ups = []
    needle, i = "\033[", 0
    while True:
        j = text.find(needle, i)
        if j < 0:
            break
        k = text.find("A", j)
        if k > j and text[j + 2 : k].isdigit():
            ups.append(int(text[j + 2 : k]))
        i = j + 2
    return ups


def test_export_returns_to_the_main_panel_instead_of_stacking_a_new_one(home):
    """Same bug as ``test_leaving_agents_erases_the_parent_panel_before_
    redraw`` in ``test_menu_skills.py``, applied to Export: the arrow-key
    TUI must erase its own prompt+result and the stale parent panel behind
    it, then settle on ONE panel — not leave the old panel sitting above a
    freshly drawn one, multiplying with every use.
    """
    from aicp import agents
    from aicp.menu import MenuState, _frame_rows, _panel, _tui

    state = MenuState(
        home / ".aicp" / "menu.aicprc", True, True, "en", [row.name for row in agents.inventory(home)]
    )
    parent_rows = _frame_rows(_panel(state, selected=EXPORT_ROW, out=io.StringIO()), io.StringIO())
    down_to_export = "down\n" * (EXPORT_ROW - 1)
    dest = home / "out.json"
    buf = io.StringIO()

    code = _tui(state, io.StringIO(f"{down_to_export}enter\n{dest}\nquit\n"), buf)

    assert code == 0
    assert dest.exists()
    text = buf.getvalue()
    assert "wrote" in text or "已寫入" in text
    ups = _ups(text)
    assert any(u >= parent_rows for u in ups), f"expected an erase of at least the parent ({parent_rows}); ups: {ups}"


class _EchoingTty(io.StringIO):
    """A stdin that echoes onto *screen* the way a cooked-mode terminal does —
    the typed line plus its newline, or a bare ``^C`` — none of which passes
    through the action's own ``out``."""

    def __init__(self, keys: str, screen: io.StringIO) -> None:
        super().__init__(keys)
        self._screen = screen

    def readline(self, *args):
        line = super().readline(*args)
        if line == "^C\n":
            self._screen.write("^C")
            raise KeyboardInterrupt
        self._screen.write(line)
        return line


@pytest.mark.parametrize(
    ("action", "keys"),
    [
        (menu_module._export_action, "\n{dest}\n"),  # ⏎ re-asks, then a path
        (menu_module._export_action, "^C\n"),
        (menu_module._import_action, "{dest}\n"),
        (menu_module._import_action, "\n^C\n"),
    ],
)
def test_returned_row_count_covers_the_terminal_echo(tmp_path, monkeypatch, action, keys):
    """The TUI erases exactly the rows an Export/Import reports before it
    redraws the panel; one row short and a copy of the panel's top border is
    left stacked above the new one, once per prompt answered."""
    monkeypatch.setenv("AICP_CONFIG", str(tmp_path / "menu.aicprc"))
    state = menu_module.MenuState.from_settings(menu_module.resolve())
    screen = io.StringIO()
    stdin = _EchoingTty(keys.format(dest=tmp_path / "out.json"), screen)
    _messages, rows = action(state, stdin, screen)
    drawn = screen.getvalue().split("\n")[:-1]  # the cursor sits on the empty last row
    assert rows == menu_module._frame_rows(drawn, screen)
