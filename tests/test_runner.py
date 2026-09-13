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
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from aicp import _utils, runner, timing

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


def wait_for(path: Path, timeout: float = 10.0) -> None:
    """Block until *path* exists — a stub CLI announcing it has really started."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was never written")


def pid_is_gone(pid: int, timeout: float = 10.0) -> bool:
    """Whether *pid* has stopped existing (a signal takes a moment to land).

    Only ever called on a process this test session itself spawned, so the
    process has already been reaped and the pid cannot be recycled behind us
    into a false "still alive".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - alive, owned by someone else
            return False
        time.sleep(0.02)
    return False


def run(prompt="/commit", chain=("copilot", "agy"), **kwargs) -> tuple[int, str]:
    """run_step with its output captured; returns (exit code, printed text)."""
    stream = io.StringIO()
    result = runner.run_step(prompt, chain, stream=stream, **kwargs)
    return result.rc, stream.getvalue()


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
        ("grok", ["grok", "-p", "/commit", "--always-approve", "--no-subagents"]),
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


# ── launching what invoke_argv named (Windows batch shims) ───────────────────
#
# invoke_argv keeps argv[0] a BARE name (the assertions above pin that), which
# only POSIX can launch. Windows CreateProcess ignores %PATHEXT% and cannot run
# a batch file at all, so every npm-installed CLI — all six of them — died with
# WinError 2 before these tests existed. _launch_command is the seam that fixes
# it, and it is checked on every OS by faking the platform, because the bug is
# invisible on the two platforms most of this suite runs on.

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows batch-shim launching"
)


@pytest.fixture
def fake_windows(monkeypatch):
    """Pretend to be Windows, with a resolver this test controls.

    ``shutil.which`` is what tells _launch_command the real extension, so the
    fake is the whole point: it decides whether the resolved target looks like
    a .exe or an npm .cmd shim.
    """

    def _resolve(resolved: str | None):
        monkeypatch.setattr(runner, "IS_WINDOWS", True)
        monkeypatch.setattr(runner.shutil, "which", lambda _name: resolved)

    return _resolve


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="on Windows this would pass for the wrong reason: which() finds no "
    "real copilot on a CI runner, so the unresolved branch returns argv too",
)
def test_launch_command_is_a_passthrough_off_windows():
    """POSIX launches a bare name itself — nothing here may rewrite argv."""
    argv = ["copilot", "-p", "/commit", "--allow-all"]
    assert runner._launch_command(argv) is argv


def test_launch_command_resolves_a_real_exe_to_its_full_path(fake_windows):
    """Not cosmetic: a bare name sends CreateProcess looking in the CURRENT
    DIRECTORY before the system directories, so a ``copilot.exe`` dropped in
    the repo being committed would win over the installed one."""
    fake_windows(r"C:\tools\copilot.exe")
    assert runner._launch_command(["copilot", "-p", "/commit"]) == [
        r"C:\tools\copilot.exe",
        "-p",
        "/commit",
    ]


def test_launch_command_runs_a_batch_shim_through_a_named_interpreter(
    fake_windows, monkeypatch
):
    """The actual WinError 2 fix: a .cmd needs cmd.exe, named explicitly.

    ``shell=True`` would do this too and is exactly what the project bans — it
    would hand cmd.exe the whole line to re-split. Here the interpreter is an
    absolute path from %ComSpec%, /d blocks the registry AutoRun command, /s
    pins cmd's quote handling to its one predictable rule, and every argument
    is individually quoted.
    """
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")
    fake_windows(r"C:\Users\me\AppData\Roaming\npm\copilot.cmd")

    line = runner._launch_command(["copilot", "-p", "/commit", "--allow-all"])

    assert line == (
        r'"C:\Windows\System32\cmd.exe" /d /s /c '
        r'""C:\Users\me\AppData\Roaming\npm\copilot.cmd" "-p" "/commit" "--allow-all""'
    )


def test_launch_command_falls_back_to_system32_when_comspec_is_unset(
    fake_windows, monkeypatch
):
    monkeypatch.delenv("ComSpec", raising=False)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    fake_windows(r"C:\npm\copilot.cmd")
    assert runner._launch_command(["copilot"]).startswith(
        r'"C:\Windows\System32\cmd.exe" /d /s /c '
    )


@pytest.mark.parametrize(
    "hostile", ['/commit"', "/commit%PATH%", "/com\rmit", "/com\nmit", "/com\x00mit"]
)
def test_launch_command_refuses_an_argument_cmd_would_interpret(fake_windows, hostile):
    """Quoting each argument neutralizes cmd's metacharacters except these two:
    a quote ends the quoting, and %VAR% expands inside quotes too. Neither has
    a sound escape on a cmd command line, so the launch refuses instead of
    pretending — the shell-injection surface the project bans, closed shut.
    """
    fake_windows(r"C:\npm\copilot.cmd")
    with pytest.raises(ValueError, match="cmd.exe"):
        runner._launch_command(["copilot", "-p", hostile])


@pytest.mark.parametrize(
    ("raw", "quoted"),
    [
        ("plain", '"plain"'),
        ("with space", '"with space"'),
        ("ends\\", '"ends\\\\"'),        # 1 trailing backslash -> 2
        ("ends\\\\", '"ends\\\\\\\\"'),    # 2 -> 4
        ("mid\\dle", '"mid\\dle"'),      # only a TRAILING run is doubled
    ],
)
def test_cmd_quote_doubles_only_a_trailing_backslash_run(raw, quoted):
    """The boundary desync two layers below the _CMD_UNSAFE screen.

    cmd.exe hands the line to the batch shim, the shim hands it to the binary
    it wraps, and that binary splits argv on the Microsoft C-runtime rule where
    a backslash escapes a following quote. An odd trailing run would escape the
    closing quote and let one argument swallow the next. Backslashes anywhere
    else are already literal and must be left alone.
    """
    assert runner._cmd_quote(raw) == quoted


def test_a_trailing_backslash_cannot_desync_the_argument_boundary(fake_windows, monkeypatch):
    """End of the same story, at the level the launcher actually emits."""
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")
    fake_windows(r"C:\npm\copilot.cmd")

    line = runner._launch_command(["copilot", "-p", "trailing\\", "--allow-all"])

    # the prompt's own backslash is doubled, so "--allow-all" stays its own
    # argument instead of being absorbed into the prompt
    assert r'"trailing\\" "--allow-all"' in line


def test_a_batch_shim_is_never_launched_through_a_shell():
    """``shell=True`` would make the per-argument quoting above pointless.

    Asserted against the AST, not with a substring search: runner.py names
    ``shell=True`` in prose precisely to explain why it does not use it, and a
    grep cannot tell an explanation from a call.
    """
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            assert not (
                keyword.arg == "shell" and getattr(keyword.value, "value", False) is True
            ), f"runner.py builds a shell command line at line {node.lineno}"


@WINDOWS_ONLY
def test_a_batch_shim_with_spaces_everywhere_still_gets_its_arguments(
    tmp_path, call_log, monkeypatch, timing_log
):
    r"""The case the /s switch exists for, end to end on a real cmd.exe.

    Without /s, cmd applies a five-condition rule to the quotes on the line and
    falls back to "strip the first quote and the last one" as soon as more than
    one argument is quoted — which is the moment an install path with a space
    (``C:\Program Files\nodejs``) meets a prompt with a space. The line is then
    silently re-cut in the wrong places and the CLI is handed garbage.
    """
    bin_dir = tmp_path / "stub bin"
    bin_dir.mkdir()
    from conftest import _CMD_STUB

    (bin_dir / "copilot.cmd").write_text(
        _CMD_STUB.format(cli="copilot", call_log=call_log, exit_code=0)
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    rc, _ = run(prompt="fix the flaky test", chain=("copilot",))

    assert rc == 0
    assert call_log.read_text().splitlines()[0].split("\t") == [
        "copilot",
        "-p",
        "fix the flaky test",
        "--allow-all",
        "--disable-builtin-mcps",
        "--no-auto-update",
    ]


# ── outcome buckets ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cli", "rc", "timed_out", "output", "expected"),
    [
        ("codex", 130, False, "usage_limit_reached", "abort"),
        ("codex", 0, False, "usage_limit_reached", "ok"),
        ("codex", 124, True, "usage_limit_reached", "timeout"),
        ("codex", 1, False, "usage_limit_reached", "quota"),
        ("copilot", 1, False, "usage_limit_reached", "fail"),
        ("agy", 1, False, "Usage limit reached", "fail"),
        ("codex", 1, False, "ordinary error", "fail"),
        ("codex", 124, False, "ordinary error", "fail"),
        ("codex", 137, False, "ordinary error", "abort"),
    ],
)
def test_classify_uses_abort_success_timeout_quota_failure_precedence(
    cli, rc, timed_out, output, expected
):
    assert runner.classify(cli, rc, timed_out=timed_out, output=output) == expected


@pytest.mark.parametrize(
    ("cli", "message"),
    [
        ("codex", "usage_limit_reached"),
        ("codex", "The usage limit has been reached"),
        ("claude", '{"type":"rate_limit_error"}'),
        ("claude", "Usage limit reached"),
        ("vibe", "Rate limit exceeded. Please wait a moment before trying again."),
        ("vibe", "Rate limits exceeded. Please wait a moment before trying again."),
        ("grok", "You've hit the rate limit for your plan."),
        ("grok", "You've reached your free Grok Build usage limit for now."),
        (
            "grok",
            "API rate limit. Ask a team admin to purchase more credits for higher limits",
        ),
    ],
)
def test_only_supported_exact_quota_messages_are_classified(cli, message):
    assert runner.classify(cli, 1, timed_out=False, output=message) == "quota"


@pytest.mark.parametrize(
    ("cli", "message"),
    [
        ("codex", "usage limit reached"),
        ("claude", '{"type": "rate_limit_error"}'),
        ("vibe", "rate limit exceeded"),
        ("grok", "rate limit exceeded"),
    ],
)
def test_quota_matching_does_not_expand_to_nearby_guesses(cli, message):
    assert runner.classify(cli, 1, timed_out=False, output=message) == "fail"


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


def test_run_step_exposes_the_winner(stub_cli):
    stub_cli(per_cli={"copilot": 5})
    result = runner.run_step("/commit", ("copilot", "agy"), stream=io.StringIO())
    assert result == runner.StepResult(rc=0, winner="agy", quota_clis=())


@POSIX_ONLY
def test_quota_falls_through_and_is_returned_and_logged(sh_stub, timing_log):
    sh_stub("codex", "echo usage_limit_reached\nexit 1\n")
    sh_stub("claude", "exit 0\n")
    stream = io.StringIO()

    result = runner.run_step("/commit", ("codex", "claude"), stream=stream)

    assert result == runner.StepResult(rc=0, winner="claude", quota_clis=("codex",))
    assert [row[4] for row in outcomes(timing_log)] == ["quota", "ok"]
    assert "quota/rate-limit exhausted" in stream.getvalue()


@POSIX_ONLY
def test_verbose_output_remains_live_and_can_still_classify_quota(
    sh_stub, timing_log, capfd
):
    sh_stub("codex", "echo usage_limit_reached\nexit 1\n")
    sh_stub("claude", "exit 0\n")

    result = runner.run_step("/commit", ("codex", "claude"), verbose=True)

    assert result.quota_clis == ("codex",)
    assert result.winner == "claude"
    assert "usage_limit_reached" in capfd.readouterr().out
    assert [row[4] for row in outcomes(timing_log)] == ["quota", "ok"]


@POSIX_ONLY
def test_verbose_timeout_still_falls_through_without_waiting_for_the_child(
    sh_stub, monkeypatch
):
    sh_stub("copilot", "sleep 30\n")
    sh_stub("agy", "exit 0\n")
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "1")

    started = time.monotonic()
    result = runner.run_step("/commit", ("copilot", "agy"), verbose=True)

    assert result == runner.StepResult(0, winner="agy")
    assert time.monotonic() - started < 20


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
        "sys.exit(run_step('/commit', ['copilot', 'agy']).rc)"
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
def test_a_timed_out_cli_is_actually_killed_not_merely_abandoned(
    sh_stub, tmp_path, timing_log, monkeypatch
):
    """The assertion the "fell through" tests above do NOT make.

    Deleting ``proc.terminate()``, the grace wait and ``proc.kill()`` outright
    left every other test in this file green: they only check that the chain
    moved on, which it does either way. The stub records its own pid so this
    one checks the thing that actually matters — the hung CLI is dead — and
    the elapsed bound catches the other shape of the same bug, where the
    runner reaps the child by waiting out its full 30s sleep.
    """
    pidfile = tmp_path / "timed-out.pid"
    sh_stub("copilot", f'echo $$ > "{pidfile}"\nsleep 30\n')
    sh_stub("agy", "exit 0\n")
    monkeypatch.setenv("AICP_STEP_TIMEOUT", "1")

    started = time.monotonic()
    rc, _ = run(chain=("copilot", "agy"))
    elapsed = time.monotonic() - started

    assert rc == 0
    assert elapsed < 20, "the runner waited the child out instead of killing it"
    assert pid_is_gone(int(pidfile.read_text())), "the timed-out CLI is still running"


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


# ── the capture log is private, and never a planted symlink ──────────────────


@pytest.fixture
def private_tmpdir(tmp_path, monkeypatch) -> Path:
    """Point ``tempfile`` at a throwaway directory this test owns.

    ``tempfile.gettempdir()`` caches, so setting ``$TMPDIR`` after the first
    call would be ignored — the module-level ``tempdir`` is the documented
    override and the one gettempdir()/mkstemp() both honour.
    """
    private = tmp_path / "tmpdir"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return private


@POSIX_ONLY
def test_a_symlink_planted_at_the_guessable_capture_path_is_not_followed(
    sh_stub, private_tmpdir, tmp_path
):
    """The live exploit: ``aicp.<pid>.log`` is guessable and ``open(.., "w")``
    follows a symlink, so anyone who can create a file in a shared /tmp (the
    sticky bit stops them deleting other people's files, never creating a new
    name of their own) got the CLI's whole output written over a file of their
    choosing, with this user's permissions."""
    victim = tmp_path / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")
    (private_tmpdir / f"aicp.{os.getpid()}.log").symlink_to(victim)
    sh_stub("copilot", "echo output-that-must-not-escape\nexit 1\n")

    run(chain=("copilot",))

    assert victim.read_text(encoding="utf-8") == "keep me"


@POSIX_ONLY
def test_the_capture_log_is_created_private_and_unguessable(sh_stub, private_tmpdir, tmp_path):
    """Mode 0600 and a random name, checked while the file still exists.

    The stub lists the temp directory from inside the run — run_step deletes
    the log on the way out, so there is nothing left to stat afterwards.
    """
    listing = tmp_path / "listing.txt"
    sh_stub("copilot", f'ls -l "{private_tmpdir}" > "{listing}"\nexit 1\n')

    run(chain=("copilot",))

    rows = [row for row in listing.read_text().splitlines() if "aicp" in row]
    assert len(rows) == 1, f"expected exactly one capture log, got: {rows}"
    assert rows[0].startswith("-rw-------"), f"capture log is not mode 0600: {rows[0]}"
    assert f"aicp.{os.getpid()}.log" not in rows[0], "the capture-log name is still guessable"


def test_verbose_removes_its_capture_log_after_classification(stub_cli, private_tmpdir):
    """Verbose mode now tees through the private log, then removes it."""
    stub_cli()
    assert run(chain=("copilot",), verbose=True)[0] == 0
    assert list(private_tmpdir.iterdir()) == []


# ── no child outlives an unhandled error between spawn and wait ──────────────


def _explode_at_communicate(monkeypatch, pidfile: Path, marker: str) -> None:
    """Make the wait after spawn raise, the way a 309-digit budget does.

    ``AICP_STEP_TIMEOUT`` accepts any digit string, so a .aicprc can pin a
    budget with no float form at all and ``communicate(timeout=...)`` raises
    ``OverflowError`` between spawn and wait — an exception path neither
    ``TimeoutExpired`` nor ``KeyboardInterrupt`` covers.

    Only the spawn whose argv contains *marker* blows up: the budget shells out
    to git through this same method, and failing that one would abort the run
    before a CLI is ever started — proving nothing about the child.
    """
    real = subprocess.Popen.communicate

    def boom(self, *args, **kwargs):
        if marker not in str(self.args):
            return real(self, *args, **kwargs)
        wait_for(pidfile)  # the child is genuinely running before we blow up
        raise OverflowError("int too large to convert to float")

    monkeypatch.setattr(subprocess.Popen, "communicate", boom)


@POSIX_ONLY
def test_an_unhandled_error_in_the_runner_leaves_no_child_running(
    sh_stub, tmp_path, monkeypatch
):
    pidfile = tmp_path / "runner-child.pid"
    sh_stub("copilot", f'echo $$ > "{pidfile}"\nsleep 30\n')
    _explode_at_communicate(monkeypatch, pidfile, marker="copilot")

    started = time.monotonic()
    with pytest.raises(OverflowError):
        run(chain=("copilot",))
    elapsed = time.monotonic() - started

    assert pid_is_gone(int(pidfile.read_text())), "the runner leaked a running child"
    # `pid is gone` alone cannot see the kill: `with Popen(...)`'s __exit__ waits
    # unboundedly, so deleting proc.kill() reaches the same end state by sitting
    # out the stub's `sleep 30` — and against a genuinely long-running CLI it
    # would hang aicp forever with the suite still green. The clock is the only
    # witness: ~0.9s killed vs ~30s waited out.
    assert elapsed < 20, f"the child was waited out, not killed ({elapsed:.1f}s)"


@POSIX_ONLY
def test_an_unhandled_error_in_run_interruptible_leaves_no_child_running(
    tmp_path, monkeypatch
):
    """Same defect, same shape, the other call site.

    It lives here rather than in test_cross_platform.py because it is one half
    of the same fix as the test above it — both call sites spawn a child and
    only ever reaped it on the two exception types they name.
    """
    pidfile = tmp_path / "utils-child.pid"
    _explode_at_communicate(monkeypatch, pidfile, marker=str(pidfile))

    started = time.monotonic()
    with pytest.raises(OverflowError):
        _utils.run_interruptible(["/bin/sh", "-c", f'echo $$ > "{pidfile}"; sleep 30'])
    elapsed = time.monotonic() - started

    assert pid_is_gone(int(pidfile.read_text())), "run_interruptible leaked a running child"
    # See the elapsed-bound rationale on the runner twin above.
    assert elapsed < 20, f"the child was waited out, not killed ({elapsed:.1f}s)"


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
