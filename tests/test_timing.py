"""Timing log: 6-field TSV schema, 5MB x 5-file rotation, never breaks a run.

Ports ``~/scripts/test_aicp.sh`` section 6 (the "timing log" block) plus the
history-reading half of its section 3, which the budget consumes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aicp import timing

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
def log(tmp_path, monkeypatch) -> Path:
    """A throwaway timing log path, wired through ``AICP_TIMING_LOG``."""
    path = tmp_path / "timing-dir" / "timing.log"
    monkeypatch.setenv("AICP_TIMING_LOG", str(path))
    return path


def rows(path: Path) -> list[list[str]]:
    return [line.split("\t") for line in path.read_text().splitlines()]


# ── path resolution ──────────────────────────────────────────────────────────


def test_log_path_honors_the_env_override(log):
    assert timing.log_path() == log


def test_log_path_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("AICP_TIMING_LOG", raising=False)
    assert timing.log_path() == Path.home() / ".aicp" / "timing.log"


# ── schema ───────────────────────────────────────────────────────────────────


def test_append_writes_six_tab_separated_fields(log):
    timing.append("copilot", "/commit", 12, "ok", 0)
    (row,) = rows(log)
    assert len(row) == 6
    assert TS.match(row[0])
    assert row[1:] == ["copilot", "/commit", "12", "ok", "0"]


def test_append_is_additive(log):
    timing.append("copilot", "/commit", 1, "ok", 0)
    timing.append("agy", "/safe-git-push", 2, "fail", 5)
    assert [r[1] for r in rows(log)] == ["copilot", "agy"]


def test_append_creates_the_parent_directory(log):
    timing.append("copilot", "/commit", 1, "ok", 0)
    assert log.parent.is_dir()


def test_an_unwritable_log_path_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")  # mkdir of the parent cannot succeed
    monkeypatch.setenv("AICP_TIMING_LOG", str(blocker / "timing.log"))
    timing.append("copilot", "/commit", 1, "ok", 0)  # must not raise
    assert blocker.read_text() == ""


# ── rotation (5MB / 5 files) ─────────────────────────────────────────────────


def seed_oversized(log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as handle:
        handle.write(b"\0" * (timing.MAX_BYTES + 1))
    for i in range(1, timing.KEEP + 1):
        log.with_suffix(f".log.{i}").write_text(f"marker-{i}\n")


def test_rotation_shifts_and_drops_exactly_like_the_house_pattern(log):
    seed_oversized(log)
    timing.append("copilot", "/commit", 1, "ok", 0)

    assert log.with_suffix(".log.5").read_text() == "marker-4\n"
    assert log.with_suffix(".log.2").read_text() == "marker-1\n"
    assert not log.with_suffix(".log.6").exists()
    assert log.with_suffix(".log.1").stat().st_size > timing.MAX_BYTES
    assert 0 < log.stat().st_size < timing.MAX_BYTES
    assert rows(log)[0][1] == "copilot"


def test_a_small_log_is_never_rotated(log):
    timing.append("copilot", "/commit", 1, "ok", 0)
    timing.append("copilot", "/commit", 2, "ok", 0)
    assert not log.with_suffix(".log.1").exists()
    assert len(rows(log)) == 2


def test_rotate_on_a_missing_file_is_a_no_op(tmp_path):
    timing.rotate(tmp_path / "nope.log")  # must not raise


# ── history reads (what budget.py consumes) ──────────────────────────────────


def write_rows(log: Path, *raw: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(raw))


def row(cli: str, elapsed: str, outcome: str, rc: str = "0") -> str:
    return f"2026-01-01T00:00:00Z\t{cli}\t/commit\t{elapsed}\t{outcome}\t{rc}\n"


def test_max_ok_seconds_takes_the_largest_ok_row(log):
    write_rows(log, row("copilot", "10", "ok"), row("copilot", "500", "ok"))
    assert timing.max_ok_seconds("copilot", path=log) == 500


def test_max_ok_seconds_ignores_timeout_abort_and_fail_rows(log):
    write_rows(
        log,
        row("copilot", "9999", "timeout", "124"),
        row("copilot", "9999", "abort", "130"),
        row("copilot", "9999", "fail", "1"),
    )
    assert timing.max_ok_seconds("copilot", path=log) == 0


def test_max_ok_seconds_ignores_other_clis(log):
    write_rows(log, row("claude", "9999", "ok"))
    assert timing.max_ok_seconds("copilot", path=log) == 0


def test_max_ok_seconds_ignores_malformed_rows(log):
    write_rows(
        log,
        "not even close to the right schema\n",
        row("copilot", "not-a-number", "ok"),
        "2026-01-01T00:00:01Z\tcopilot\t/commit\ttruncated-line",
    )
    assert timing.max_ok_seconds("copilot", path=log) == 0


def test_max_ok_seconds_on_a_missing_or_empty_log_is_zero(tmp_path, log):
    assert timing.max_ok_seconds("copilot", path=tmp_path / "nope.log") == 0
    write_rows(log, "")
    assert timing.max_ok_seconds("copilot", path=log) == 0


def test_max_ok_seconds_only_reads_the_last_n_lines(log):
    write_rows(log, row("copilot", "9999", "ok"), *[row("copilot", "5", "ok")] * 20)
    assert timing.max_ok_seconds("copilot", path=log, lines=20) == 5
    assert timing.max_ok_seconds("copilot", path=log, lines=21) == 9999
