"""The --config logo: tiers fit, modes paint, the menu draws and redraws it."""

from __future__ import annotations

import io
import json
import os
import re

import pytest

from aicp import logo
from aicp import menu as menu_module
from aicp.config import resolve
from aicp.contracts import ROSTER
from aicp.menu import ROWS, MenuState, _tui, config_menu
from aicp.present import width

_ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]")
_ROSTER = [c.name for c in ROSTER]


def _size(monkeypatch, columns, lines):
    monkeypatch.setattr(menu_module, "_terminal_size", lambda _out: os.terminal_size((columns, lines)))


def _plain(text):
    return _ANSI.sub("", text)


def test_every_tier_is_rectangular():
    for rows in logo.TIERS:
        assert len({len(r) for r in rows}) == 1


def test_pick_follows_columns_and_rows():
    big, mid, _small = logo.TIERS
    reserve = 20
    need = lambda rows: (logo.INDENT + len(rows[0]) + 1, len(rows) + 1 + reserve)
    for rows in logo.TIERS:
        cols, lines = need(rows)
        assert logo.pick(cols, lines, reserve)[0] in {r[0] for r in logo.TIERS if len(r[0]) >= len(rows[0])}
        assert logo.pick(cols - 1, lines, reserve) != rows
        assert logo.pick(cols, lines - 1, reserve) != rows
    assert logo.pick(200, 100, reserve) == big
    assert logo.pick(200, need(mid)[1], reserve) == mid
    assert logo.pick(10, 100, reserve) == ()


@pytest.mark.parametrize("mode", ["color", "mono", "animated"])
@pytest.mark.parametrize("rows", logo.TIERS)
def test_painted_lines_never_exceed_the_tier_width(mode, rows):
    limit = logo.INDENT + len(rows[0])
    for line in logo.paint(rows, mode, True):
        assert width(line) <= limit
        assert line.endswith("\033[0m")


def test_without_colour_the_rows_are_plain_glyphs():
    out = logo.paint(logo.TIERS[0], "color", False)
    assert all("\033" not in line for line in out)
    assert [line.strip() for line in out] == [r.strip() for r in logo.TIERS[0]]


def test_mono_uses_one_hue_and_color_uses_a_gradient():
    hues = lambda lines: set(re.findall(r"38;5;(\d+)", "".join(lines)))
    assert hues(logo.paint(logo.TIERS[0], "mono", True)) == {"75"}
    assert len(hues(logo.paint(logo.TIERS[0], "color", True))) > 3


def test_animation_ends_on_the_static_colour_logo():
    rows = logo.TIERS[0]
    frames = logo.frames(rows)
    assert frames[-1] == logo.paint(rows, "color", True)
    assert frames[0] != frames[-1] and len({tuple(f) for f in frames}) > 5


def test_logo_setting_validates(tmp_path, monkeypatch, capsys):
    path = tmp_path / "c.json"
    monkeypatch.setenv("AICP_CONFIG", str(path))
    monkeypatch.delenv("AICP_LOGO", raising=False)
    assert resolve().logo == "animated"
    path.write_text(json.dumps({"aicp_logo": "mono"}), encoding="utf-8")
    assert resolve().logo == "mono"
    path.write_text(json.dumps({"aicp_logo": "neon"}), encoding="utf-8")
    assert resolve().logo == "animated"
    assert "AICP_LOGO" in capsys.readouterr().err


def _logo_row():
    return next(i for i, r in enumerate(ROWS, start=1) if r.key == "AICP_LOGO")


def test_menu_draws_the_logo_only_when_it_fits(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    for columns, lines, shown in [(80, 50, True), (80, 24, False), (20, 50, False)]:
        _size(monkeypatch, columns, lines)
        out = io.StringIO()
        config_menu(path=path, stdin=io.StringIO("q\n"), stdout=out)
        assert ("██╔══██╗" in out.getvalue() or "_ |_ _/" in out.getvalue()) is shown


def test_off_draws_nothing(tmp_path, monkeypatch):
    _size(monkeypatch, 80, 50)
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"aicp_logo": "off"}), encoding="utf-8")
    monkeypatch.setenv("AICP_CONFIG", str(path))
    out = io.StringIO()
    config_menu(path=path, stdin=io.StringIO("q\n"), stdout=out)
    assert "╔" not in out.getvalue() and "██" not in out.getvalue()


def test_the_logo_row_cycles_and_persists(tmp_path, monkeypatch):
    _size(monkeypatch, 80, 50)
    path = tmp_path / "c.json"
    config_menu(path=path, stdin=io.StringIO(f"{_logo_row()}\nq\n"), stdout=io.StringIO())
    assert json.loads(path.read_text(encoding="utf-8"))["aicp_logo"] == "off"


def test_changing_the_logo_row_in_the_tui_erases_logo_and_panel(tmp_path, monkeypatch):
    _size(monkeypatch, 80, 50)
    state = MenuState(tmp_path / "c.json", True, True, "en", list(_ROSTER))
    keys = "down\n" * (_logo_row() - 1) + "right\nquit\n"
    out = io.StringIO()
    assert _tui(state, io.StringIO(keys), out) == 0
    assert state.logo == "off"
    # The erase walks up past the panel AND the logo's 7 rows (6 + blank).
    erase = re.findall(r"\033\[(\d+)A\033\[J", out.getvalue())
    panel_rows = len(menu_module._panel(state, _logo_row(), out))
    assert str(panel_rows + 7) in erase


def test_the_logo_is_centred_over_the_panel(tmp_path, monkeypatch):
    _size(monkeypatch, 120, 50)
    out = io.StringIO()
    config_menu(path=tmp_path / "c.json", stdin=io.StringIO("q\n"), stdout=out)
    lines = _plain(out.getvalue()).splitlines()
    panel = next(line for line in lines if line.startswith("╭"))
    row = next(line for line in lines if "██╔══██╗██║" in line)  # row 2 starts flush, no own padding
    assert len(row) - len(row.lstrip()) == (width(panel) - len(logo.TIERS[0][0])) // 2


@pytest.mark.parametrize("columns", [40, 60, 80, 100, 160])
def test_no_logo_line_reaches_the_last_column(tmp_path, monkeypatch, columns):
    _size(monkeypatch, columns, 50)
    out = io.StringIO()
    config_menu(path=tmp_path / "c.json", stdin=io.StringIO("q\n"), stdout=out)
    for line in _plain(out.getvalue()).splitlines():
        if "█" in line or "_" in line:
            assert width(line) <= columns - 1


def _idle_tui(tmp_path, monkeypatch, mode):
    """Run the TUI with colour on, the idle timer expiring once, then a key."""
    _size(monkeypatch, 100, 50)
    monkeypatch.setattr(menu_module, "color_supported", lambda _out: True)
    monkeypatch.setattr(menu_module.time, "sleep", lambda _s: None)
    waits = []

    def fake_pending(_stdin, timeout=0.0):
        if timeout:
            waits.append(timeout)
            return len(waits) > 1  # first idle wait times out, the second sees a key
        return False

    monkeypatch.setattr(menu_module, "pending", fake_pending)
    state = MenuState(tmp_path / "c.json", True, True, "en", list(_ROSTER))
    state.logo = mode
    out = io.StringIO()
    assert _tui(state, io.StringIO("quit\n"), out) == 0
    return waits, out.getvalue()


def test_animated_shimmers_again_after_the_idle_interval(tmp_path, monkeypatch):
    waits, text = _idle_tui(tmp_path, monkeypatch, "animated")
    assert waits[0] == menu_module._SHIMMER_EVERY == 5.0
    frames = len(re.findall(r"\033\[\d+A\r", text))  # each shimmer frame walks up to the logo
    assert frames == 2 * len(logo.frames(logo.TIERS[0])), "one entrance pass + one idle pass"


def test_only_animated_mode_waits_to_shimmer(tmp_path, monkeypatch):
    waits, text = _idle_tui(tmp_path, monkeypatch, "color")
    assert waits == [] and "\033[1;38;5;231m" not in text


def test_a_resize_clears_the_screen_and_redraws_for_the_new_size(tmp_path, monkeypatch):
    """A window shrunk mid-session: the old frame may have re-wrapped, so the
    cursor-up repaint cannot be trusted — clear, re-pick the tier, redraw."""
    size = [os.terminal_size((100, 50))]
    monkeypatch.setattr(menu_module, "_terminal_size", lambda _out: size[0])
    keys = iter(["down", "quit"])

    def read_key(_stdin, _out):
        size[0] = os.terminal_size((100, 28))  # 28 rows: only the 4-row tier fits now
        return next(keys)

    monkeypatch.setattr(menu_module, "read_key", read_key)
    state = MenuState(tmp_path / "c.json", True, True, "en", list(_ROSTER))
    out = io.StringIO()
    assert _tui(state, io.StringIO(), out) == 0
    text = out.getvalue()
    assert text.count("\033[H\033[2J") == 1
    before, after = text.split("\033[H\033[2J")
    assert "██╔══██╗" in before and "██╔══██╗" not in after
    assert "/_/ \\_\\___\\___|_|" in after
