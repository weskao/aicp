"""Tests for aicp.present — the spinner and box-render port.

Mirrors the assertions ``~/scripts/test_aicp.sh`` makes by hand against
``lib/spinner.sh`` and ``lib/box_render.py --self-test``.
"""

from __future__ import annotations

import sys

import pytest

from aicp import present

# ── spinner ───────────────────────────────────────────────────────────────────


def test_spinner_run_relays_success():
    result = present.spinner_run("t", [sys.executable, "-c", "import sys; sys.exit(0)"])
    assert result.returncode == 0


def test_spinner_run_relays_failure_status():
    result = present.spinner_run("t", [sys.executable, "-c", "import sys; sys.exit(7)"])
    assert result.returncode == 7


def test_spinner_capture_relays_status_and_writes_log(tmp_path):
    log = tmp_path / "cap.log"
    result = present.spinner_capture(
        log, "t", [sys.executable, "-c", "print('captured'); import sys; sys.exit(3)"]
    )
    assert result.returncode == 3
    assert log.read_text().strip() == "captured"


def test_spinner_is_disabled_off_a_tty():
    # pytest's own stderr capture is not a TTY, matching the shell suite's
    # "spinner is disabled off a TTY" check.
    assert present.spinner_enabled() is False


def test_spinner_stop_is_idempotent():
    spinner = present.Spinner("t")
    spinner.stop()  # never started — must not raise
    spinner.start()
    spinner.stop()
    spinner.stop()  # already stopped — must not raise


# ── box render: width() ──────────────────────────────────────────────────────


def test_width_ignores_ansi_escapes():
    assert present.width("abc") == 3
    assert present.width("\033[38;5;213mabc\033[0m") == 3


def test_width_counts_cjk_as_two_columns():
    assert present.width("分支") == 4


def test_width_counts_combining_marks_as_zero():
    assert present.width("é") == 1  # "é" as e + combining acute accent


# ── box render: alignment ────────────────────────────────────────────────────


def _assert_aligned(rows, title):
    for lines in (
        present.render_panel(rows, title, present.FRAME),
        present.render_table(rows, title),
    ):
        widths = {present.width(line) for line in lines}
        assert len(widths) == 1, f"ragged frame, widths={sorted(widths)}"


def test_alignment_plain_row():
    _assert_aligned([("repo", "value")], "T")


def test_alignment_colored_cell():
    _assert_aligned([("repo", "\033[38;5;213mcolored\033[0m"), ("b", "plain")], "T")


def test_alignment_cjk_both_columns():
    _assert_aligned([("分支", "主線"), ("repo", "x")], "T")


def test_alignment_header_wider_than_every_value():
    _assert_aligned([("a", "0"), ("b", "1")], "MUCH-LONGER-HEADER")


# ── box render: zebra striping ───────────────────────────────────────────────


def test_zebra_never_stripes_a_single_row():
    single = present.render_panel([("a", "0")], "T", present.FRAME, zebra=True)
    assert present.STRIPE not in single[1]


def test_zebra_stripes_alternate_rows():
    lines = present.render_panel([("a", "0"), ("b", "1")], "T", present.FRAME, zebra=True)
    assert present.STRIPE not in lines[1]  # even body row: plain
    assert present.STRIPE in lines[2]  # odd body row: striped


def test_zebra_does_not_change_line_width():
    lines = present.render_panel(
        [("a", "0"), ("b", "1"), ("c", "2")], "T", present.FRAME, zebra=True
    )
    widths = {present.width(line) for line in lines}
    assert len(widths) == 1


# ── box render: empty-input rendering ────────────────────────────────────────


def test_render_draws_nothing_for_empty_rows():
    assert present.render([], "T") == []
    assert present.render([], "T", mode="panel") == []


@pytest.mark.parametrize("mode", ["panel", "table"])
def test_render_dispatches_to_the_right_mode(mode):
    rows = [("a", "0"), ("b", "1")]
    direct = present.render_panel(rows, "T", present.FRAME) if mode == "panel" else present.render_table(rows, "T")
    assert present.render(rows, "T", mode=mode) == direct
