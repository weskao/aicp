"""Per-CLI timeout budget: formula, cap, history widening, malformed input.

Ports ``~/scripts/test_aicp.sh`` section 3 (the budget/history block), which
drives ``_aicp_budget``/``_aicp_history_budget`` through the whole CLI; here
the same arithmetic is asserted directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aicp import budget


@pytest.fixture(autouse=True)
def pinned_knobs(monkeypatch):
    """Every AICP_TIMEOUT_* knob cleared, so each test states its own."""
    for name in (
        "AICP_STEP_TIMEOUT",
        "AICP_TIMEOUT_BASE",
        "AICP_TIMEOUT_PER_FILE",
        "AICP_TIMEOUT_PER_100L",
        "AICP_TIMEOUT_MAX",
        "AICP_TIMEOUT_HISTORY_LINES",
        "AICP_TIMEOUT_HISTORY_MULT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def knobs(monkeypatch):
    def _set(**values: object) -> None:
        for name, value in values.items():
            monkeypatch.setenv(f"AICP_TIMEOUT_{name.upper()}", str(value))

    return _set


@pytest.fixture
def history(tmp_path, monkeypatch):
    """Seed a timing log at AICP_TIMING_LOG; returns its path."""
    log = tmp_path / "history.log"
    monkeypatch.setenv("AICP_TIMING_LOG", str(log))

    def _seed(*raw: str) -> Path:
        log.write_text("".join(raw))
        return log

    return _seed


def row(cli: str, elapsed: str, outcome: str, rc: str = "0") -> str:
    return f"2026-01-01T00:00:00Z\t{cli}\t/commit\t{elapsed}\t{outcome}\t{rc}\n"


def pending(repo: Path, *names: str) -> None:
    for name in names:
        (repo / name).write_text("")


# ── formula ──────────────────────────────────────────────────────────────────


def test_the_budget_grows_from_the_floor_with_pending_files(git_repo, knobs):
    pending(git_repo, "a", "b", "c")
    knobs(base=10, per_file=5, per_100l=0, max=1000)
    result = budget.compute("copilot", cwd=git_repo)
    assert result.seconds == 25
    assert "3 files" in result.note


def test_the_budget_is_capped_at_the_ceiling(git_repo, knobs):
    pending(git_repo, "a", "b", "c")
    knobs(base=10, per_file=5, per_100l=0, max=20)
    assert budget.compute("copilot", cwd=git_repo).seconds == 20


def test_changed_lines_add_per_100_lines(git_repo, knobs):
    (git_repo / "f.txt").write_text("")
    subprocess.run(["git", "add", "f.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "add"],
        cwd=git_repo,
        check=True,
    )
    (git_repo / "f.txt").write_text("line\n" * 250)
    knobs(base=0, per_file=0, per_100l=10, max=10_000)
    result = budget.compute("copilot", cwd=git_repo)
    assert result.seconds == 25  # 10 * 250 // 100
    assert "250 lines" in result.note


def test_defaults_match_the_zsh_floor_and_growth(git_repo):
    pending(git_repo, "pending.txt")
    assert budget.compute("copilot", cwd=git_repo).seconds == 195  # 180 + 1*15


def test_outside_a_git_repo_the_formula_still_yields_the_floor(tmp_path, knobs):
    knobs(base=10, per_file=5, per_100l=5, max=1000)
    assert budget.compute("copilot", cwd=tmp_path).seconds == 10


# ── AICP_STEP_TIMEOUT pin ────────────────────────────────────────────────────


def test_step_timeout_pins_a_fixed_budget_and_skips_the_formula(git_repo, monkeypatch):
    pending(git_repo, "a", "b", "c")
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "7")
    result = budget.compute("copilot", cwd=git_repo)
    assert (result.seconds, result.note) == (7, "fixed")


def test_step_timeout_still_pins_even_with_a_huge_ok_history(git_repo, monkeypatch, history):
    history(row("copilot", "99999", "ok"))
    pending(git_repo, "pending.txt")
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "7")
    result = budget.compute("copilot", cwd=git_repo)
    assert (result.seconds, result.note) == (7, "fixed")


@pytest.mark.parametrize("bad", ["abc", "-5", "7s", " ", "1.5"])
def test_a_malformed_step_timeout_falls_back_to_the_floor(git_repo, monkeypatch, bad):
    monkeypatch.setenv("AICP_STEP_TIMEOUT", bad)
    result = budget.compute("copilot", cwd=git_repo)
    assert result.seconds == budget.FLOOR
    assert "AICP_STEP_TIMEOUT" in result.note


@pytest.mark.parametrize("knob", ["BASE", "PER_FILE", "PER_100L", "MAX"])
def test_a_malformed_timeout_knob_falls_back_to_the_floor(git_repo, monkeypatch, knob):
    monkeypatch.setenv(f"AICP_TIMEOUT_{knob}", "not-a-number")
    result = budget.compute("copilot", cwd=git_repo)
    assert result.seconds == budget.FLOOR
    assert "AICP_TIMEOUT_" in result.note


# ── history widening ─────────────────────────────────────────────────────────


def test_a_slow_ok_history_widens_the_budget_above_the_formula(git_repo, knobs, history):
    history(row("copilot", "500", "ok"))
    pending(git_repo, "pending.txt")
    knobs(base=10, per_file=0, per_100l=0, max=1000)
    result = budget.compute("copilot", cwd=git_repo)
    assert result.seconds == 650  # 500 * 1.3, rounded up
    assert "history 650s" in result.note


def test_history_widening_is_not_capped_by_the_ceiling(git_repo, knobs, history):
    history(row("copilot", "500", "ok"))
    knobs(base=10, per_file=0, per_100l=0, max=100)
    assert budget.compute("copilot", cwd=git_repo).seconds == 650


def test_a_fast_ok_history_never_shrinks_the_budget(git_repo, history):
    history(row("copilot", "1", "ok"))
    pending(git_repo, "pending.txt")
    result = budget.compute("copilot", cwd=git_repo)
    assert result.seconds == 195
    assert "history" not in result.note


def test_timeout_abort_and_fail_rows_never_inflate_the_budget(git_repo, knobs, history):
    history(
        row("copilot", "9999", "timeout", "124"),
        row("copilot", "9999", "abort", "130"),
        row("copilot", "9999", "fail", "1"),
    )
    pending(git_repo, "pending.txt")
    knobs(base=10, per_file=0, per_100l=0, max=1000)
    result = budget.compute("copilot", cwd=git_repo)
    assert result.seconds == 10
    assert "9999" not in result.note


def test_history_is_per_cli(git_repo, knobs, history):
    history(row("claude", "9999", "ok"))
    knobs(base=10, per_file=0, per_100l=0, max=1000)
    assert budget.compute("copilot", cwd=git_repo).seconds == 10
    assert budget.compute("claude", cwd=git_repo).seconds == 12999  # 9999 * 1.3


def test_the_history_multiplier_is_configurable(git_repo, knobs, history, monkeypatch):
    history(row("copilot", "100", "ok"))
    knobs(base=10, per_file=0, per_100l=0, max=1000)
    monkeypatch.setenv("AICP_TIMEOUT_HISTORY_MULT", "2")
    assert budget.compute("copilot", cwd=git_repo).seconds == 200


@pytest.mark.parametrize("mult", ["not-a-number", ""])
def test_a_malformed_multiplier_disables_widening_rather_than_crashing(
    git_repo, knobs, history, monkeypatch, mult
):
    history(row("copilot", "500", "ok"))
    knobs(base=10, per_file=0, per_100l=0, max=1000)
    monkeypatch.setenv("AICP_TIMEOUT_HISTORY_MULT", mult)
    assert budget.compute("copilot", cwd=git_repo).seconds == 10


# ── a broken log is never an outage of the timeout protection ────────────────


def test_a_missing_timing_log_still_yields_a_numeric_budget(git_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("AICP_TIMING_LOG", str(tmp_path / "does-not-exist" / "timing.log"))
    assert budget.compute("copilot", cwd=git_repo).seconds == budget.FLOOR


def test_an_empty_timing_log_still_yields_a_numeric_budget(git_repo, history):
    history("")
    assert budget.compute("copilot", cwd=git_repo).seconds == budget.FLOOR


def test_a_malformed_timing_log_still_yields_a_numeric_budget(git_repo, history):
    history(
        "not even close to the right schema\n",
        row("copilot", "not-a-number", "ok"),
        "2026-01-01T00:00:01Z\tcopilot\t/commit\ttruncated-line",
    )
    assert budget.compute("copilot", cwd=git_repo).seconds == budget.FLOOR


def test_an_unreadable_timing_log_still_yields_a_numeric_budget(git_repo, monkeypatch, tmp_path):
    # A directory where a file is expected: every read of it raises.
    (tmp_path / "log-dir").mkdir()
    monkeypatch.setenv("AICP_TIMING_LOG", str(tmp_path / "log-dir"))
    assert budget.compute("copilot", cwd=git_repo).seconds == budget.FLOOR
