"""Cross-run quota cooldown: record an exhausted CLI, skip it until it recovers.

The in-run exclusion (``StepResult.quota_clis``) only lasts for one aicp
invocation, so the next run paid for the exhausted CLI all over again — a full
budget burned per step for a CLI that cannot succeed. These tests pin the
persistent half: what is written, how long it lasts, and the several ways it is
allowed to be missing without ever breaking a run.
"""

from __future__ import annotations

import json

import pytest

from aicp import quota


@pytest.fixture
def quota_file(tmp_path):
    return tmp_path / "quota.json"


# ── the cooldown window ──────────────────────────────────────────────────────


def test_a_recorded_cli_is_cooling_for_the_configured_window(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota.record("agy", path=quota_file, now=1000.0)

    assert quota.cooling("agy", path=quota_file, now=1000.0) == 3600
    assert quota.cooling("agy", path=quota_file, now=2800.0) == 1800


def test_cooling_reports_zero_once_the_window_has_passed(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota.record("agy", path=quota_file, now=1000.0)

    assert quota.cooling("agy", path=quota_file, now=4601.0) == 0


def test_an_unrecorded_cli_is_never_cooling(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota.record("agy", path=quota_file, now=1000.0)

    assert quota.cooling("codex", path=quota_file, now=1000.0) == 0


def test_a_missing_file_means_no_cooldowns(quota_file):
    assert quota.cooling("agy", path=quota_file, now=1000.0) == 0


def test_the_default_window_is_one_hour(quota_file, monkeypatch):
    monkeypatch.delenv("AICP_QUOTA_COOLDOWN", raising=False)
    quota.record("agy", path=quota_file, now=1000.0)

    assert quota.cooling("agy", path=quota_file, now=1000.0) == 3600


# ── the escape hatch, and bad values ─────────────────────────────────────────


def test_a_zero_cooldown_disables_the_feature_entirely(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "0")
    quota.record("agy", path=quota_file, now=1000.0)

    assert quota.cooling("agy", path=quota_file, now=1000.0) == 0
    assert not quota_file.exists(), "a disabled cooldown should leave no file behind"


@pytest.mark.parametrize("bad", ["", "soon", "-1", "1.5", "99999999999999999999999"])
def test_an_unparseable_cooldown_falls_back_to_the_default(quota_file, monkeypatch, bad):
    # budget.py's stated posture, applied here too: a bad config value never
    # bricks aicp, it falls back to a value that is definitely valid.
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", bad)
    quota.record("agy", path=quota_file, now=1000.0)

    assert quota.cooling("agy", path=quota_file, now=1000.0) == 3600


# ── never breaks a run ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    ['{"agy": "soon"}', "{not json", "[]", '"a string"', "", '{"agy": null}'],
)
def test_a_corrupt_quota_file_means_no_cooldowns(quota_file, monkeypatch, content):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota_file.write_text(content, encoding="utf-8")

    assert quota.cooling("agy", path=quota_file, now=1000.0) == 0


def test_recording_over_a_corrupt_file_still_works(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota_file.write_text("{not json", encoding="utf-8")

    quota.record("agy", path=quota_file, now=1000.0)

    assert quota.cooling("agy", path=quota_file, now=1000.0) == 3600


def test_an_unwritable_path_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")

    quota.record("agy", path=blocker / "quota.json", now=1000.0)  # must not raise

    assert quota.cooling("agy", path=blocker / "quota.json", now=1000.0) == 0


# ── file hygiene ─────────────────────────────────────────────────────────────


def test_expired_entries_are_pruned_on_the_next_write(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota.record("agy", path=quota_file, now=1000.0)

    quota.record("codex", path=quota_file, now=9000.0)  # agy's window is long gone

    assert json.loads(quota_file.read_text(encoding="utf-8")).keys() == {"codex"}


def test_recording_a_second_cli_keeps_the_first(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota.record("agy", path=quota_file, now=1000.0)

    quota.record("codex", path=quota_file, now=1200.0)

    assert quota.cooling("agy", path=quota_file, now=1200.0) == 3400
    assert quota.cooling("codex", path=quota_file, now=1200.0) == 3600


def test_re_recording_a_cli_extends_its_window(quota_file, monkeypatch):
    monkeypatch.setenv("AICP_QUOTA_COOLDOWN", "3600")
    quota.record("agy", path=quota_file, now=1000.0)

    quota.record("agy", path=quota_file, now=3000.0)

    assert quota.cooling("agy", path=quota_file, now=3000.0) == 3600


def test_the_default_path_lives_in_the_aicp_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert quota.quota_path() == tmp_path / ".aicp" / "quota.json"
