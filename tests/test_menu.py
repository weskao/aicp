"""``--config`` settings menu and ``--swap-ai`` picker.

Ports ``~/scripts/test_aicp.sh``'s ``--config`` block (its "the settings
menu" section) and the ``--swap-ai`` assertions next to ``AICP_CLI_ORDER``.
The promise under test is "saves as you go": every pick writes immediately,
so there is no save key to forget and quitting can never discard a change.

Both surfaces are driven through the NUMBERED fallback, exactly as the zsh
suite drives them — a test harness has no TTY to press arrow keys on, and
the fallback is the reference implementation's own design, not a lesser copy
bolted on. The non-TTY path therefore also carries the CI guarantee: it must
never block on a prompt.

Local fixtures only — ``tests/conftest.py`` is never edited from here.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

from aicp import menu as menu_module
from aicp._keyreader import is_interactive, key_session, read_key
from aicp._utils import BOLD, CYAN, GREEN
from aicp.config import resolve
from aicp.contracts import ROSTER
from aicp.menu import ROWS, MenuState, _order_motion, _panel, config_menu, swap_ai
from aicp.present import width


def _plain(text: str) -> str:
    """*text* with its colour stripped — what the eye reads on the line."""
    return re.sub(r"\033\[[0-9;]*m", "", text)

if sys.platform != "win32":
    import pty
    import termios
    import tty

ROSTER_NAMES = tuple(c.name for c in ROSTER)


@pytest.fixture
def menu(tmp_path, monkeypatch):
    """Factory: run the menu against a throwaway config with *keys* typed at
    its prompt. Returns ``(exit_code, output, config_path)``."""

    def _run(keys: str, *, initial: str | None = None, path: Path | None = None):
        cfg = path or tmp_path / "menu.aicprc"
        if initial is not None:
            cfg.write_text(initial, encoding="utf-8")
        monkeypatch.setenv("AICP_CONFIG", str(cfg))
        out = io.StringIO()
        code = config_menu(stdin=io.StringIO(keys), stdout=out)
        return code, out.getvalue(), cfg

    return _run


@pytest.fixture
def swap(tmp_path, monkeypatch):
    """Factory: run the ``--swap-ai`` picker with *keys* typed at its
    prompt. Returns ``(exit_code, output, config_path)``."""

    def _run(keys: str, *, initial: str | None = None):
        cfg = tmp_path / "swap.aicprc"
        if initial is not None:
            cfg.write_text(initial, encoding="utf-8")
        monkeypatch.setenv("AICP_CONFIG", str(cfg))
        out = io.StringIO()
        code = swap_ai(stdin=io.StringIO(keys), stdout=out)
        return code, out.getvalue(), cfg

    return _run


# ── saves as you go ──────────────────────────────────────────────────────────


def test_toggles_are_written_immediately(menu):
    code, _out, cfg = menu("1\n2\nq\n")
    assert code == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["AICP_DO_COMMIT"] == "0"
    assert data["AICP_DO_PUSH"] == "0"


def test_the_panel_and_its_persistence_promise_are_rendered(menu):
    _, out, _ = menu("q\n")
    assert "aicp config" in out
    assert "saves as you go" in out


def test_toggling_a_setting_twice_restores_it(menu):
    _, _, cfg = menu("1\n1\nq\n")
    assert json.loads(cfg.read_text(encoding="utf-8"))["AICP_DO_COMMIT"] == "1"


def test_a_toggle_survives_into_the_next_resolve(menu):
    menu("2\nq\n")
    assert resolve().do_push is False


def test_the_language_pick_repaints_the_menu_in_the_new_language(menu):
    _, out, cfg = menu("3\nq\n")
    assert json.loads(cfg.read_text(encoding="utf-8"))["AICP_LANG"] == "zh-TW"
    assert "aicp 設定" in out, "the menu must repaint in the language just picked"


def test_the_cli_row_rotates_the_chain_and_persists_it(menu):
    _, _out, cfg = menu("4\nq\n")
    rotated = " ".join((*ROSTER_NAMES[1:], ROSTER_NAMES[0]))
    assert json.loads(cfg.read_text(encoding="utf-8"))["AICP_CLI_ORDER"] == rotated


def test_cli_order_motion_slides_one_name_and_keeps_every_frame_one_width():
    """The slide starts on the old order, ends on the new one, and never
    changes shape on the way — a frame that resized would shove the panel's
    right-hand border around mid-animation."""
    before = list(ROSTER_NAMES)
    after = [*before[1:], before[0]]
    columns = width(" → ".join(after))

    frames = [frame for frame, _held in _order_motion(before, after, columns, CYAN)]

    assert _plain(frames[0]) == " → ".join(before)
    assert _plain(frames[-1]) == " → ".join(after)
    assert {width(frame) for frame in frames} == {columns}, "the panel must not resize mid-slide"
    assert len({_plain(frame) for frame in frames}) == len(frames), "every frame is one column on"
    assert f"{GREEN}{BOLD}{after[0]}" in frames[-1]  # the promoted CLI is lit


def test_cli_order_motion_runs_the_other_way_too():
    before = list(ROSTER_NAMES)
    after = [before[-1], *before[:-1]]

    frames = [f for f, _held in _order_motion(before, after, width(" → ".join(after)), CYAN)]

    assert _plain(frames[0]) == " → ".join(before)
    assert _plain(frames[-1]) == " → ".join(after)


@pytest.mark.parametrize("columns, lines", [(80, 24), (100, 24), (60, 24), (100, 14)])
def test_the_panel_fits_the_terminal_it_draws_on(columns, lines, monkeypatch):
    """Every line inside the terminal, in both languages.

    This is the redraw bug, not a cosmetic one: a frame wider than the
    terminal wraps, which silently doubles how many rows it occupies, and
    the in-place repaint then walks the cursor up too few rows and smears a
    fresh half-frame down the screen on every keypress.
    """
    monkeypatch.setattr(menu_module, "_terminal_size", lambda _out: os.terminal_size((columns, lines)))
    for lang in ("en", "zh-TW"):
        state = MenuState(Path("menu.aicprc"), True, True, lang, list(ROSTER_NAMES))

        frame = _panel(state, selected=4)

        assert max(width(line) for line in frame) <= columns, lang
        assert len(frame) < lines, lang


# ── a pick that does not exist changes nothing ───────────────────────────────


def test_an_out_of_range_number_is_rejected_and_writes_nothing(menu):
    code, out, cfg = menu("9\nq\n")
    assert code == 0
    assert "Enter one of the setting numbers" in out
    assert not cfg.exists(), "a rejected pick must write nothing at all"


def test_a_non_numeric_pick_is_rejected_and_writes_nothing(menu):
    _, out, cfg = menu("banana\nq\n")
    assert "Enter one of the setting numbers" in out
    assert not cfg.exists()


def test_quitting_without_a_pick_writes_nothing(menu):
    _, _, cfg = menu("q\n")
    assert not cfg.exists()


# ── the shared config file survives a write ──────────────────────────────────


def test_unrelated_config_lines_survive_a_menu_write(menu, tmp_path):
    _, _, cfg = menu(
        "1\nq\n",
        initial=json.dumps(
            {"AICP_CLI_ORDER": "codex copilot agy claude vibe", "AICP_TZ": "Etc/UTC"}
        ),
    )
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["AICP_TZ"] == "Etc/UTC"
    assert data["AICP_CLI_ORDER"] == "codex copilot agy claude vibe"
    assert data["AICP_DO_COMMIT"] == "0"


def test_the_menu_leaves_no_temp_file_beside_the_config(menu, tmp_path):
    _, _, cfg = menu("1\n2\n3\nq\n")
    assert list(cfg.parent.glob(f"{cfg.name}.tmp.*")) == []


# ── CI safety: a non-TTY stdin must never block ──────────────────────────────


def test_empty_non_tty_stdin_returns_immediately(menu):
    """EOF on a pipe is "quit", not "wait forever" — this tool runs in CI."""
    code, _, cfg = menu("")
    assert code == 0
    assert not cfg.exists()


def test_swap_ai_with_empty_non_tty_stdin_returns_immediately(swap):
    code, _, cfg = swap("")
    assert code == 0
    assert not cfg.exists()


def test_the_menu_runs_no_ai_cli(menu, stub_cli, call_log):
    stub_cli()
    menu("1\n2\n3\n4\nq\n")
    assert not call_log.exists()


# ── --swap-ai ────────────────────────────────────────────────────────────────


def test_swap_ai_lists_every_roster_cli(swap):
    _, out, _ = swap("1\n")
    for i, name in enumerate(ROSTER_NAMES, start=1):
        assert f"{i}) {name}" in out


def test_swap_ai_on_the_current_first_writes_nothing(swap):
    code, out, cfg = swap("1\n")
    assert code == 0
    assert not cfg.exists()
    assert "already #1" in out


def test_swap_ai_persists_the_pick_as_the_new_first(swap):
    code, _, cfg = swap("5\n")
    assert code == 0
    expected = "vibe agy codex claude copilot grok"
    assert json.loads(cfg.read_text(encoding="utf-8"))["AICP_CLI_ORDER"] == expected


def test_swap_ai_keeps_every_other_cli_in_the_chain(swap):
    _, _, cfg = swap("5\n")
    order = json.loads(cfg.read_text(encoding="utf-8"))["AICP_CLI_ORDER"].split()
    assert sorted(order) == sorted(ROSTER_NAMES)


@pytest.mark.parametrize("choice", ["0", "9", "banana", "-1"])
def test_swap_ai_rejects_an_invalid_choice_without_writing(swap, choice):
    code, out, cfg = swap(f"{choice}\n")
    assert code == 1
    assert "invalid choice" in out
    assert not cfg.exists()


def test_swap_ai_runs_no_ai_cli(swap, stub_cli, call_log):
    stub_cli()
    swap("5\n")
    assert not call_log.exists()


def test_swap_ai_reads_the_chain_from_the_existing_config(swap):
    _, out, _ = swap("1\n", initial=json.dumps({"AICP_CLI_ORDER": "vibe codex"}))
    assert "1) vibe" in out
    assert "already #1" in out


# ── the raw-key surface (the TUI half of _keyreader) ─────────────────────────


@pytest.fixture
def raw_pty():
    """Factory: a pty with *typed* already waiting on it, as a text stream.

    Raw mode is set BEFORE the write, which is a harness detail rather than a
    property of the reader: a pty still in canonical mode buffers bytes that
    carry no newline, and they do not reliably become readable when the mode
    is switched afterwards. A real terminal never has this problem — the user
    presses the key after the menu is already in raw mode.
    """
    opened: list = []

    def _open(typed: bytes):
        controller, follower = pty.openpty()
        tty.setraw(follower, termios.TCSANOW)
        os.write(controller, typed)
        stream = os.fdopen(follower, "r", buffering=1)
        opened.append((controller, stream))
        return stream

    yield _open
    for controller, stream in opened:
        stream.close()
        os.close(controller)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX termios path")
@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (b"\x1b[A", "up"),
        (b"\x1b[B", "down"),
        (b"\x1b[C", "right"),
        (b"\x1b[D", "left"),
        (b"\r", "enter"),
        (b"q", "quit"),
        (b"\x03", "quit"),  # raw mode disables ISIG, so Ctrl+C is a byte
        (b"r", "reset"),
    ],
)
def test_raw_key_reading_over_a_real_pty(raw_pty, typed, expected):
    """A pty is the only way to exercise the termios path — without this the
    whole arrow-key surface would ship untested, since every other test here
    deliberately drives the non-TTY fallback."""
    stream = raw_pty(typed)
    assert stream.isatty()
    assert read_key(stream, stream) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX termios path")
def test_terminal_mode_is_restored_after_a_key_is_read(raw_pty):
    """A shell left in -icanon -echo looks broken to whoever uses it next, so
    the restore is armed before raw mode is ever entered. ECHO is turned back
    on first so the starting mode is provably NOT the raw mode the reader
    installs — otherwise "restored" and "never changed" would look alike."""
    stream = raw_pty(b"q")
    attrs = termios.tcgetattr(stream.fileno())
    attrs[3] |= termios.ECHO
    termios.tcsetattr(stream.fileno(), termios.TCSANOW, attrs)
    before = termios.tcgetattr(stream.fileno())

    read_key(stream, stream)

    assert termios.tcgetattr(stream.fileno()) == before


def test_is_interactive_is_false_for_a_pipe():
    assert is_interactive(io.StringIO(), io.StringIO()) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX termios path")
def test_non_tty_fallback_is_taken_on_a_real_pipe_fd():
    """The ``is_interactive`` guard is what saves this case — not the ``except``.

    Every other non-TTY test here feeds a ``StringIO``, whose ``fileno()``
    raises ``UnsupportedOperation``: a subclass of ``OSError``/``ValueError``,
    so ``read_key``'s ``except`` clause silently stands in for the guard and
    such a test cannot tell the two apart. A real pipe has a working
    ``fileno()``, and ``termios.tcgetattr`` on it raises ``termios.error``,
    whose MRO is ``(error, Exception)`` — NOT caught by
    ``except (OSError, ValueError, ImportError)``. This is therefore the only
    shape that actually fails if the guard is removed.
    """
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writer, os.fdopen(read_fd, "r") as stream:
        writer.write("down\n")
        writer.flush()
        stream.fileno()  # a real fd, unlike StringIO — no shortcut to the except
        assert stream.isatty() is False
        assert read_key(stream, stream) == "down"


# ── row model: a later task appends Skills and Doctor rows ───────────────────


def test_rows_are_data_addressed_by_index(menu):
    # The last two are the appended action rows (Skills, Doctor): they run
    # something instead of persisting a setting, hence no config key. See
    # test_menu_skills.py.
    assert tuple(row.key for row in ROWS) == (
        "AICP_DO_COMMIT",
        "AICP_DO_PUSH",
        "AICP_LANG",
        "AICP_CLI_ORDER",
        "",
        "",
    )
    _, out, _ = menu("q\n")
    for i, _row in enumerate(ROWS, start=1):
        assert f"{i})" in out


def test_the_prompt_names_the_actual_row_count(menu):
    _, out, _ = menu("q\n")
    assert f"1-{len(ROWS)}" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX termios path")
def test_the_key_session_keeps_the_terminal_from_echoing_between_keys():
    """The bug behind "hold Enter and the panel multiplies".

    Raw mode per keypress leaves the terminal echoing between reads, so a key
    pressed while the order row is mid-slide is echoed onto the screen by the
    driver — an Enter pushes the frame down a row and every repaint after it
    stacks another header. The session has to hold that off for as long as it
    is drawing, hand the line discipline back for a typed prompt, and leave
    the terminal exactly as it found it.
    """
    controller, follower = pty.openpty()
    stream = os.fdopen(follower, "r", buffering=1)
    try:
        def flags():
            # ECHO and ICANON only: the rest of the struct carries transient
            # kernel state (PENDIN) that changes on its own and would make
            # this assert about the kernel rather than about the session.
            mode = termios.tcgetattr(follower)
            return bool(mode[3] & termios.ECHO), bool(mode[3] & termios.ICANON)

        before = flags()

        with key_session(stream, stream) as typed:
            assert flags() == (False, False), "a drawing menu must not let the driver echo"
            with typed():
                assert flags() == (True, True), "a typed prompt needs its line discipline"
            assert flags() == (False, False), "and the session takes it back afterwards"
        assert flags() == before, "the terminal is left as it was found"
    finally:
        stream.close()
        os.close(controller)


def test_a_non_tty_key_session_is_a_no_op():
    """CI has no terminal to put into cbreak, and must not fail trying."""
    with key_session(io.StringIO(), io.StringIO()) as typed, typed():
        pass
