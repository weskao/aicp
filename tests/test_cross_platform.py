"""Cross-platform behavior tests for aicp._utils.

Covers platform-flag correctness and ``run_interruptible``'s two code paths —
the POSIX path (real assertions, run on every OS this suite executes on
except Windows) and the Windows path (skipped everywhere but Windows: it
depends on ``CREATE_NEW_PROCESS_GROUP``/``CTRL_BREAK_EVENT``, which don't
exist off Windows).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from conftest import ALL_CLIS

from aicp import _utils, contracts

# ── platform flags ───────────────────────────────────────────────────────────


def test_platform_flags_match_sys_platform():
    assert _utils.IS_WINDOWS == (sys.platform == "win32")
    assert _utils.IS_MACOS == (sys.platform == "darwin")
    assert _utils.IS_LINUX == sys.platform.startswith("linux")
    # Exactly one of the three is ever true on a supported CI runner.
    assert sum([_utils.IS_WINDOWS, _utils.IS_MACOS, _utils.IS_LINUX]) == 1


def test_stub_cli_roster_matches_contracts_roster():
    """conftest.py's ``ALL_CLIS`` (which every ``stub_cli`` fixture stubs) must
    stay a superset of ``contracts.ROSTER`` — if the frozen roster ever grows
    a 6th CLI (see TODO.md §6, "grok") and this list isn't updated too, a
    test could silently fall through to a REAL CLI actually installed on the
    machine running the suite, exactly the failure mode ``test_aicp.sh``'s
    own harness comment warns about."""
    assert {cli.name for cli in contracts.ROSTER} <= set(ALL_CLIS)


def test_have_finds_the_running_interpreter():
    assert _utils.have(os.path.basename(sys.executable)) or _utils.have("python3")


def test_have_is_false_for_a_nonexistent_command():
    assert _utils.have("definitely-not-a-real-aicp-test-binary-xyz") is False


def test_color_supported_false_off_a_tty():
    # pytest's captured stderr is not a TTY.
    assert _utils.color_supported() is False


def test_home_config_dir_prefers_env_override(tmp_path, monkeypatch):
    override = tmp_path / "custom.aicprc"
    monkeypatch.setenv("AICP_TEST_CONFIG", str(override))
    assert _utils.home_config_dir("AICP_TEST_CONFIG", tmp_path / "default") == override


def test_home_config_dir_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AICP_TEST_CONFIG_UNSET", raising=False)
    default = tmp_path / "default.aicprc"
    assert _utils.home_config_dir("AICP_TEST_CONFIG_UNSET", default) == default


# ── run_interruptible: exit-status relay (every platform) ───────────────────


def test_run_interruptible_relays_success():
    result = _utils.run_interruptible([sys.executable, "-c", "import sys; sys.exit(0)"])
    assert result.returncode == 0


def test_run_interruptible_relays_failure_status():
    result = _utils.run_interruptible([sys.executable, "-c", "import sys; sys.exit(5)"])
    assert result.returncode == 5


def test_run_interruptible_captures_stdout_when_piped():
    result = _utils.run_interruptible(
        [sys.executable, "-c", "print('hello')"], stdout=subprocess.PIPE
    )
    assert result.stdout.strip() == "hello"


# ── run_interruptible: POSIX path ────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group behavior")
def test_run_interruptible_keeps_child_in_own_process_group():
    """No NEW process group is created for the child (the direct equivalent
    of GNU `timeout --foreground`) — its pgid matches this test process's
    own, so a terminal SIGINT delivered to the foreground group would reach
    it directly."""
    result = _utils.run_interruptible(
        [sys.executable, "-c", "import os; print(os.getpgrp())"],
        stdout=subprocess.PIPE,
    )
    assert int(result.stdout.strip()) == os.getpgrp()


# ── run_interruptible: Windows path ──────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only process-group flag")
def test_run_interruptible_uses_new_process_group_on_windows():
    """CREATE_NEW_PROCESS_GROUP is required for CTRL_C_EVENT forwarding to
    reach only the child, not this process too."""
    calls = {}
    real_popen = subprocess.Popen

    def _spy(cmd, **kwargs):
        calls["creationflags"] = kwargs.get("creationflags", 0)
        return real_popen(cmd, **kwargs)

    from unittest import mock

    with mock.patch.object(subprocess, "Popen", _spy):
        _utils.run_interruptible([sys.executable, "-c", "pass"])

    assert calls["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
