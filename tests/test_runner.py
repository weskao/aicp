"""Fallback-chain runner: invoke table, outcome buckets, signals, timeouts.

Ports the signal/timeout half of ``~/scripts/test_aicp.sh`` section 3 plus its
abort-bucket checks near the top of the ``bin/aicp`` block. The signal tests
are the reason this module exists: a step killed by a signal must abort the
whole chain (exit 130, no fallthrough), while a step killed by its budget must
fall through to the next CLI.
"""

from __future__ import annotations

import ast
import io
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from aicp import runner, timing

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signal semantics (/bin/sh stubs, SIGINT to a process group)",
)


@pytest.fixture
def sh_stub(tmp_path, monkeypatch):
    """Factory for /bin/sh stub CLIs with arbitrary bodies, prepended to PATH.

    conftest's ``stub_cli`` only varies the exit code; the timeout and
    self-signalling cases need a real script body (``sleep``, ``kill -INT``).
    """
    bin_dir = tmp_path / "sh-stub-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def _write(cli: str, body: str) -> Path:
        path = bin_dir / cli
        path.write_text(f"#!/bin/sh\n{textwrap.dedent(body)}")
        path.chmod(0o755)
        return path

    _write.dir = bin_dir  # type: ignore[attr-defined]
    return _write


@pytest.fixture
def timing_log(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "runner-timing.log"
    monkeypatch.setenv("AICP_TIMING_LOG", str(path))
    return path


def called(call_log: Path) -> list[str]:
    if not call_log.exists():
        return []
    return [line.split("\t")[0] for line in call_log.read_text().splitlines()]


def outcomes(timing_log: Path) -> list[list[str]]:
    return [line.split("\t") for line in timing_log.read_text().splitlines()]


def run(prompt="/commit", chain=("copilot", "agy"), **kwargs) -> tuple[int, str]:
    """run_step with its output captured; returns (exit code, printed text)."""
    stream = io.StringIO()
    rc = runner.run_step(prompt, chain, stream=stream, **kwargs)
    return rc, stream.getvalue()


# ── invoke table (ported flag-for-flag from _aicp_invoke) ────────────────────


@pytest.mark.parametrize(
    ("cli", "expected"),
    [
        ("agy", ["agy", "-p", "/commit", "--dangerously-skip-permissions", "--new-project"]),
        (
            "claude",
            ["claude", "-p", "/commit", "--dangerously-skip-permissions", "--strict-mcp-config"],
        ),
        (
            "copilot",
            ["copilot", "-p", "/commit", "--allow-all", "--disable-builtin-mcps", "--no-auto-update"],
        ),
        ("codex", ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "/commit"]),
        ("vibe", ["vibe", "-p", "/commit", "--auto-approve", "--trust"]),
    ],
)
def test_invoke_argv_matches_the_zsh_flags(cli, expected):
    assert runner.invoke_argv(cli, "/commit") == expected


def test_every_roster_cli_has_an_invoke_entry():
    from aicp.contracts import ROSTER

    assert all(runner.invoke_argv(cli.name, "/commit") for cli in ROSTER)


def test_an_unknown_cli_name_has_no_invocation():
    assert runner.invoke_argv("not-a-cli", "/commit") is None


# ── outcome buckets ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rc", "timed_out", "expected"),
    [
        (0, False, "ok"),
        (1, False, "fail"),
        (124, False, "fail"),  # a CLI's own exit 124 is not a timeout
        (124, True, "timeout"),
        (130, False, "abort"),
        (137, False, "abort"),
    ],
)
def test_classify_matches_the_zsh_buckets(rc, timed_out, expected):
    assert runner.classify(rc, timed_out=timed_out) == expected


def test_a_negative_returncode_is_normalized_to_128_plus_signal():
    # Python reports a signal-killed child as -N; zsh reports 128+N.
    assert runner.normalize_rc(-signal.SIGINT) == 130
    assert runner.normalize_rc(0) == 0


# ── fallback chain ───────────────────────────────────────────────────────────


def test_the_first_cli_to_exit_zero_wins(stub_cli, call_log, timing_log):
    stub_cli()
    rc, out = run(chain=("copilot", "agy"))
    assert rc == 0
    assert called(call_log) == ["copilot"]
    assert outcomes(timing_log)[0][4] == "ok"
    assert "copilot" in out


def test_a_cli_missing_from_path_is_skipped(stub_cli, call_log):
    stub_cli(clis=("agy",))
    rc, _ = run(chain=("copilot", "agy"))
    assert rc == 0
    assert called(call_log) == ["agy"]


def test_a_plain_failure_falls_through_to_the_next_cli(stub_cli, call_log, timing_log):
    stub_cli(per_cli={"copilot": 5})
    rc, _ = run(chain=("copilot", "agy"))
    assert rc == 0
    assert called(call_log) == ["copilot", "agy"]
    assert [r[4] for r in outcomes(timing_log)] == ["fail", "ok"]
    assert outcomes(timing_log)[0][5] == "5"


def test_when_every_cli_fails_the_step_fails(stub_cli, call_log):
    stub_cli(exit_code=3)
    rc, out = run(chain=("copilot", "agy"))
    assert rc == 1
    assert called(call_log) == ["copilot", "agy"]
    assert "no AI CLI" in out


def test_an_empty_or_unavailable_chain_fails_without_running_anything(call_log):
    rc, _ = run(chain=())
    assert rc == 1
    assert called(call_log) == []


def test_an_unknown_chain_name_is_skipped_not_treated_as_success(stub_cli, call_log):
    stub_cli()
    rc, _ = run(chain=("not-a-cli", "agy"))
    assert rc == 0
    assert called(call_log) == ["agy"]


def test_the_prompt_reaches_the_cli(stub_cli, call_log):
    stub_cli()
    run(prompt="/safe-git-push", chain=("copilot",))
    assert "/safe-git-push" in call_log.read_text()


# ── signals: abort, never fall through ───────────────────────────────────────


def test_a_signal_exit_aborts_the_chain(stub_cli, call_log, timing_log):
    stub_cli(exit_code=130)
    rc, out = run(chain=("copilot", "agy"))
    assert rc == runner.ABORT_RC == 130
    assert called(call_log) == ["copilot"]  # no fallthrough
    assert outcomes(timing_log)[0][4] == "abort"
    assert "signal 2" in out


@POSIX_ONLY
def test_a_cli_actually_killed_by_a_signal_aborts_the_chain(sh_stub, tmp_path, timing_log):
    log = tmp_path / "sig-calls.log"
    sh_stub("copilot", f'echo copilot >> "{log}"\nkill -INT $$\n')
    sh_stub("agy", f'echo agy >> "{log}"\nexit 0\n')
    rc, _ = run(chain=("copilot", "agy"))
    assert rc == 130
    assert called(log) == ["copilot"]
    assert outcomes(timing_log)[0][4] == "abort"
    assert outcomes(timing_log)[0][5] == "130"


@POSIX_ONLY
def test_ctrl_c_reaches_a_cli_running_under_the_budget(sh_stub, tmp_path, timing_log):
    """The regression `timeout --foreground` existed to fix.

    If the child were put in a NEW process group, the terminal's SIGINT would
    never reach it: the sleep would run its full 30s and this test would time
    out waiting. Keeping it in aicp's own group makes Ctrl+C land directly.
    """
    log = tmp_path / "pg-calls.log"
    sh_stub("copilot", f'echo copilot >> "{log}"\nsleep 30\n')
    sh_stub("agy", f'echo agy >> "{log}"\nexit 0\n')
    driver = (
        "import sys; from aicp.runner import run_step; "
        "sys.exit(run_step('/commit', ['copilot', 'agy']))"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(runner.__file__).parents[2])}
    child = subprocess.Popen(
        [sys.executable, "-c", driver],
        env=env,
        start_new_session=True,  # its own group, so our SIGINT hits only it
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while not log.exists() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        os.killpg(os.getpgid(child.pid), signal.SIGINT)
        assert child.wait(timeout=10) == 130
    finally:
        if child.poll() is None:
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
            child.wait()
    assert called(log) == ["copilot"]  # aborted, never fell through to agy
    # The zsh original's INT trap exits before _ai_run can log this attempt, so
    # a terminal Ctrl+C left no row at all while a CLI that exited 130 on its
    # own did. The port records both — same bucket for the same event — which
    # costs nothing: the history reader counts only `ok` rows.
    assert outcomes(timing_log)[0][4:] == ["abort", "130"]


# ── timeouts: kill the CLI, then fall through ────────────────────────────────


@POSIX_ONLY
def test_a_hung_cli_is_killed_by_its_budget_and_the_chain_falls_through(
    sh_stub, tmp_path, timing_log, monkeypatch
):
    log = tmp_path / "to-calls.log"
    sh_stub("copilot", f'echo copilot >> "{log}"\nsleep 30\n')
    sh_stub("agy", f'echo agy >> "{log}"\nexit 0\n')
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "1")
    started = time.time()
    rc, out = run(chain=("copilot", "agy"))
    assert time.time() - started < 20  # killed, not left to hang for 30s
    assert rc == 0
    assert called(log) == ["copilot", "agy"]
    first = outcomes(timing_log)[0]
    assert first[4] == "timeout"
    assert first[5] == str(runner.TIMEOUT_RC)
    assert "timed out" in out


@POSIX_ONLY
def test_a_timeout_notifies_and_names_the_cli_and_budget(sh_stub, tmp_path, monkeypatch):
    sh_stub("copilot", "sleep 30\n")
    sh_stub("agy", "exit 0\n")
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "1")
    sent: list[str] = []
    run(chain=("copilot", "agy"), notify=sent.append)
    assert len(sent) == 1
    assert "copilot" in sent[0] and "/commit" in sent[0] and "1s" in sent[0]


@POSIX_ONLY
def test_a_failing_notifier_never_breaks_the_chain(sh_stub, tmp_path, monkeypatch):
    """The zsh ``_aicp_notify`` returns 1 at worst and ``_ai_run`` ignores it.

    A notifier that raises must therefore not abort a run that would otherwise
    have committed and pushed — the whole point of the step it interrupts is
    that the chain keeps going.
    """
    log = tmp_path / "notify-calls.log"
    sh_stub("copilot", "sleep 30\n")
    sh_stub("agy", f'echo agy >> "{log}"\nexit 0\n')
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "1")

    def explode(_message: str) -> None:
        raise RuntimeError("telegram is down")

    rc, _ = run(chain=("copilot", "agy"), notify=explode)
    assert rc == 0
    assert called(log) == ["agy"]


def test_a_plain_failure_never_notifies(stub_cli):
    stub_cli(exit_code=1)
    sent: list[str] = []
    run(chain=("copilot",), notify=sent.append)
    assert sent == []


@POSIX_ONLY
def test_a_zero_budget_means_no_timeout(sh_stub, monkeypatch):
    """GNU timeout treats 0 as "no timeout"; so does the port."""
    sh_stub("copilot", "sleep 0.3\nexit 0\n")
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "0")
    rc, _ = run(chain=("copilot",))
    assert rc == 0


# ── verbose vs captured output ───────────────────────────────────────────────


@POSIX_ONLY
def test_captured_output_is_replayed_only_on_failure(sh_stub, capfd):
    sh_stub("copilot", "echo boom-from-the-cli\nexit 1\n")
    sh_stub("agy", "echo quiet-success\nexit 0\n")
    rc, out = run(chain=("copilot", "agy"))
    assert rc == 0
    assert "boom-from-the-cli" in out  # the failure's log is replayed
    assert "quiet-success" not in out  # the success's is not
    assert capfd.readouterr().out == ""  # nothing streamed live


@POSIX_ONLY
def test_verbose_streams_child_output_live(sh_stub, capfd):
    sh_stub("copilot", "echo live-output\nexit 0\n")
    rc, out = run(chain=("copilot",), verbose=True)
    assert rc == 0
    assert "live-output" in capfd.readouterr().out
    assert "live-output" not in out


# ── the runner stays decoupled from its siblings ─────────────────────────────


def test_the_runner_imports_no_sibling_module():
    """The decoupling four workers depend on, enforced against the AST.

    Parsed rather than grepped: a substring check misses ``import aicp.config``
    (no ``from``), and this test is the only mechanism holding the rule while
    the sibling modules are written concurrently.
    """
    forbidden = {"config", "notify", "gitflow", "secrets", "skills"}
    tree = ast.parse(Path(runner.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    leaves = {name.rsplit(".", 1)[-1] for name in imported}
    assert not (leaves & forbidden), f"runner.py imports a sibling: {leaves & forbidden}"
    assert "aicp.budget" in imported and "aicp.timing" in imported  # the AST really parsed


@POSIX_ONLY
def test_the_default_notifier_is_a_no_op(sh_stub, monkeypatch, capfd):
    """Exercised on the timeout path — the one branch that actually notifies."""
    sh_stub("copilot", "sleep 30\n")
    sh_stub("agy", "exit 0\n")
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "1")
    rc, out = run(chain=("copilot", "agy"))  # no notify= passed at all
    assert rc == 0
    assert capfd.readouterr().out == ""  # the default notifier says nothing
    assert "timed out" in out


def test_an_unwritable_timing_log_never_breaks_a_run(stub_cli, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("AICP_TIMING_LOG", str(blocker / "timing.log"))
    stub_cli()
    assert run(chain=("copilot",))[0] == 0
    assert timing.log_path() == blocker / "timing.log"
