"""Run one headless prompt through the fallback chain of AI CLIs.

Port of ``_ai_run``/``_aicp_invoke``/``_aicp_timeout`` in ``~/scripts/bin/aicp``.

Returns 0 on the first CLI that exits 0, :data:`ABORT_RC` if a CLI was killed
by a signal, 1 when every CLI failed on its own.

**Signal handling is the load-bearing part.** The zsh original wraps every
invocation in GNU ``timeout --foreground``, and ``--foreground`` is not
cosmetic: without it ``timeout`` puts the CLI in a NEW process group, the
terminal delivers Ctrl+C only to its FOREGROUND group, so SIGINT never reached
the CLI, the CLI never exited, and the script stayed blocked on a child it had
no way to stop — Ctrl+C looked dead. :func:`_invoke` keeps the child in this
process's own process group (POSIX) for exactly that reason. The documented
cost, inherited deliberately, is that a timeout then signals only the CLI
itself and not its grandchildren; an interruptible Ctrl+C is worth more than
reaping those.

The three outcome rules that follow from it:

* a step killed by a signal returns :data:`ABORT_RC` and NEVER falls through to
  the next CLI — 128+N means a user abort or a shutdown, not a reason to retry;
* a CLI exiting above 128 is treated the same way (``abort``);
* a CLI killed by its own budget DOES fall through — that one is aicp's doing,
  not the user's.

This module deliberately imports no sibling feature module (config, notify,
gitflow, secrets, skills): the fallback chain arrives as a plain
``Sequence[str]`` parameter and the notifier as a :data:`~aicp.contracts.NotifyFn`
parameter, so the runner is testable with no config machinery and no network.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from aicp import budget as budget_mod
from aicp import timing
from aicp._utils import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    IS_WINDOWS,
    RED,
    RESET,
    YELLOW,
    have,
    run_interruptible,
)
from aicp.contracts import NotifyFn
from aicp.i18n import t
from aicp.present import Spinner

__all__ = [
    "ABORT_RC",
    "KILL_AFTER",
    "TIMEOUT_RC",
    "classify",
    "invoke_argv",
    "normalize_rc",
    "run_step",
]

TIMEOUT_RC = 124  # GNU timeout's own "the command timed out" status
ABORT_RC = 130    # 128 + SIGINT
KILL_AFTER = 10   # grace period between SIGTERM and SIGKILL (timeout --kill-after=10)

# Above this, a budget is treated as "no timeout" rather than a deadline. A
# budget is only ever a digit string out of the environment or .aicprc, so it
# can be arbitrarily large, and Python cannot wait that long: a 309-digit value
# has no float form at all (OverflowError converting the timeout), and even a
# representable one above ~292 years is rejected by the selector the wait runs
# on (TypeError) — both of them AFTER the child is already spawned. A year is
# indistinguishable from "never" for one CLI invocation, and this keeps
# budget.py's stated invariant true: a bad config value never bricks aicp.
_MAX_DEADLINE = 365 * 24 * 3600

# Per-CLI flags, ported flag-for-flag from _aicp_invoke. Each branch skips MCP
# server startup where the CLI exposes a way to — /commit and /safe-git-push
# are plain git prompts, no MCP tool use — which measurably shortens cold start;
# codex and agy showed no consistent gain, so they keep their existing flags.
_FLAGS: dict[str, tuple[str, ...]] = {
    # agy resumes its last session/workspace by default, ignoring $PWD entirely
    # unless --new-project pins it to the caller's cwd.
    "agy": ("--dangerously-skip-permissions", "--new-project"),
    "claude": ("--dangerously-skip-permissions", "--strict-mcp-config"),
    "copilot": ("--allow-all", "--disable-builtin-mcps", "--no-auto-update"),
    # --trust is not the approval flag (--auto-approve is) and is not optional:
    # vibe asks once, interactively, before it will touch an untrusted
    # directory, and its own --help names --trust as the answer for
    # non-interactive automation. Without it the very first aicp run in a new
    # repo blocks on a prompt nobody can see behind the spinner until the budget
    # kills it — a hang presented as a timeout. Per-invocation, never written to
    # vibe's trusted_folders.toml, so aicp leaves no persistent trust behind.
    "vibe": ("--auto-approve", "--trust"),
}


def _noop(_message: str) -> None:
    """Default notifier: say nothing. T4 injects the real one at the call site."""


def invoke_argv(cli: str, prompt: str) -> list[str] | None:
    """The exact argv for *cli*, or None if this name has no invocation.

    An unknown name yields None rather than an empty command: the zsh `case`
    silently returned 0 for a name it did not know, which would be read here as
    "that CLI succeeded" without anything having run.
    """
    if cli == "codex":  # the only one that takes the prompt positionally
        return ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", prompt]
    flags = _FLAGS.get(cli)
    return None if flags is None else [cli, "-p", prompt, *flags]


# ── Windows: launching an npm shim ───────────────────────────────────────────
#
# `invoke_argv` builds argv[0] as the BARE binary name, which is all POSIX
# needs. On Windows it was unlaunchable. CreateProcess (what subprocess uses
# when shell=False) does not consult %PATHEXT%: given "copilot" it looks for
# "copilot.exe" and nothing else, and it cannot execute a batch file even when
# handed one, because a .cmd is a script that only cmd.exe can interpret.
#
# Every one of aicp's five CLIs is npm-installed, and npm's Windows shim is a
# .cmd — so `Popen(["copilot", ...])` raised `FileNotFoundError: [WinError 2]`
# on every Windows machine. `have()` could not catch it: shutil.which DOES
# honor %PATHEXT%, so the gate said the CLI was present and the very next line
# failed to start it. aicp on Windows could therefore never run a single step;
# a user would have seen every CLI in the chain die instantly and the run end
# with "✗ no AI CLI could run this step" despite the CLIs being installed.
#
# The fix resolves the binary once, so its real extension is known, and hands a
# batch shim to the interpreter that can run it. Deliberately NOT shell=True:
# that would build one string out of the whole command and hand cmd.exe a
# shell-injection surface the project bans. Here the interpreter is named
# explicitly, argv stays a list, and the arguments are individually quoted.
_BATCH_SUFFIXES = frozenset({".cmd", ".bat"})

# cmd.exe re-parses whatever command line it is given, and quoting each argument
# neutralizes every metacharacter except these: `"` ends the quoting, and `%`
# is expanded inside quotes too. There is no sound, portable escape for either
# one on a cmd command line, so a launch carrying them is refused rather than
# half-escaped. aicp never builds one — prompts are slash-command names and the
# flags are fixed literals in _FLAGS — so this is a tripwire, not a limit
# anyone meets.
# ponytail: fail closed; write a real cmd quoter only if aicp ever has to pass
# arbitrary free text as a prompt on Windows.
_CMD_UNSAFE = frozenset('"%\r\n\x00')


def _cmd_quote(part: str) -> str:
    """Wrap *part* in the quotes cmd.exe needs, keeping the closing one a
    delimiter.

    A trailing run of backslashes has to be doubled first. cmd.exe hands the
    line to a batch shim, the shim hands it to the binary it wraps (node, for
    every npm-installed CLI), and THAT binary splits its own argv on the
    Microsoft C-runtime rule, where ``\\"`` is a literal quote rather than a
    delimiter. An odd-length trailing run would therefore escape the closing
    quote and let one argument swallow the next — a boundary desync two layers
    below the one :data:`_CMD_UNSAFE` screens. ``subprocess.list2cmdline``
    doubles them for exactly this reason; so does this.
    """
    trailing = len(part) - len(part.rstrip("\\"))
    return '"' + part + "\\" * trailing + '"'


def _launch_command(argv: list[str]) -> list[str] | str:
    """*argv* rewritten so this platform can actually start it.

    Returns *argv* unchanged off Windows. On Windows, returns the resolved
    executable (so CreateProcess does no PATH search of its own), or — for a
    .cmd/.bat shim — a full ``cmd.exe /d /s /c "..."`` command line as a single
    string, which is how the quoting below survives being handed on verbatim.
    """
    if not IS_WINDOWS:
        return argv

    resolved = shutil.which(argv[0])
    if resolved is None:  # pragma: no cover - run_step gates on have() first
        return argv
    if os.path.splitext(resolved)[1].lower() not in _BATCH_SUFFIXES:
        return [resolved, *argv[1:]]

    parts = [resolved, *argv[1:]]
    bad = sorted({c for part in parts for c in part if c in _CMD_UNSAFE})
    if bad:
        raise ValueError(
            f"refusing to run {argv[0]} through cmd.exe: an argument contains "
            f"{''.join(bad)!r}, which cmd.exe would interpret rather than pass on"
        )
    # %ComSpec% is how Windows itself names the interpreter; the literal path is
    # only a floor for a stripped environment. /d skips the registry AutoRun
    # command, so a machine-wide AutoRun cannot inject itself into every aicp
    # step. /s makes cmd's quote handling the ONE predictable rule — strip the
    # outer pair, take the rest verbatim — instead of the five-condition dance
    # it otherwise does, which mangles the line as soon as two arguments happen
    # to be quoted (an install path with a space plus a prompt with a space).
    # Joined with a literal separator rather than os.path.join: this branch is
    # a Windows path being built, and os.path.join would spell it with forward
    # slashes on the machine most of this suite's platform-faking runs on.
    system_root = os.environ.get("SystemRoot") or "C:\\Windows"
    comspec = os.environ.get("ComSpec") or f"{system_root}\\System32\\cmd.exe"
    inner = " ".join(_cmd_quote(part) for part in parts)
    return f'"{comspec}" /d /s /c "{inner}"'


def normalize_rc(returncode: int) -> int:
    """Python reports a signal-killed child as ``-N``; zsh reports ``128+N``."""
    return 128 - returncode if returncode < 0 else returncode


def classify(rc: int, *, timed_out: bool) -> str:
    """The timing-log outcome bucket: ok / timeout / fail / abort.

    ``timed_out`` is the fact of having killed the child, not a guess from its
    status — so a CLI that exits 124 on its own is a plain ``fail``, where the
    zsh original (which could only compare against GNU timeout's exit code)
    had to call it a timeout.
    """
    if rc > 128:
        return "abort"
    if rc == 0:
        return "ok"
    if timed_out:
        return "timeout"
    return "fail"


def _invoke(
    argv: list[str], seconds: int, *, cwd: Path | None, capture: object | None
) -> tuple[int, bool]:
    """Run *argv* under a *seconds* budget; returns ``(rc, timed_out)``.

    ``seconds <= 0`` means no timeout at all (GNU timeout reads 0 the same
    way), and takes the bare :func:`~aicp._utils.run_interruptible` path; so
    does a budget above :data:`_MAX_DEADLINE`, for the reason stated there.

    The timed path cannot compose with ``run_interruptible``: enforcing a
    deadline needs the ``Popen`` handle (to SIGTERM, then SIGKILL, the child),
    and ``run_interruptible`` owns its own and exposes no timeout hook. The
    process-group posture below is therefore kept deliberately identical to
    it — same group on POSIX, ``CREATE_NEW_PROCESS_GROUP`` plus forwarded
    ``CTRL_BREAK_EVENT`` on Windows — and a change to either must be made in
    both places.

    On Windows a batch-shim CLI runs under a cmd.exe this function spawned (see
    :func:`_launch_command`), which widens the limitation this module's
    docstring already states: ``terminate()``/``kill()`` then reach cmd.exe and
    not the CLI behind it. ``CREATE_NEW_PROCESS_GROUP`` puts both in the same
    new group, so the ``CTRL_BREAK_EVENT`` path — the one that matters for
    Ctrl+C — still reaches the CLI itself.
    """
    command = _launch_command(argv)
    kwargs: dict[str, object] = {"cwd": cwd}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    if capture is not None:
        kwargs["stdout"] = capture
        kwargs["stderr"] = subprocess.STDOUT

    if seconds <= 0 or seconds > _MAX_DEADLINE:
        return normalize_rc(run_interruptible(command, **kwargs).returncode), False

    # `with Popen(...)`, plus a kill on the way out of any exception this
    # function does not otherwise handle — exactly what subprocess.run does,
    # and for the same reason: between spawn and wait, ANY escaping exception
    # (not just the two named below) would otherwise leave a live child with
    # nobody left to reap it.
    with subprocess.Popen(command, **kwargs) as proc:  # type: ignore[arg-type]
        try:
            proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            # SIGTERM first, SIGKILL after the grace period — timeout's
            # --kill-after=10. Only the CLI itself is signalled, not its
            # grandchildren; see this module's docstring.
            proc.terminate()
            try:
                proc.communicate(timeout=KILL_AFTER)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            return TIMEOUT_RC, True
        except KeyboardInterrupt:
            if IS_WINDOWS:
                # No foreground-process-group concept: the console's Ctrl+C did
                # not reach the child, so forward it (CTRL_BREAK_EVENT, the only
                # one Win32 can target at a specific group).
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - delivery can fail many ways
                    proc.terminate()
            # On POSIX the child already took the same SIGINT (same process
            # group); wait for it to unwind rather than second-guessing it.
            proc.communicate()
            raise
        except BaseException:
            proc.kill()
            raise
        return normalize_rc(proc.returncode), False


@contextlib.contextmanager
def _capture_file(enabled: bool) -> Iterator[object | None]:
    """One reusable handle for the child's combined output, or None to inherit
    the screen (verbose streams instead of capturing, so it opens nothing).

    ``mkstemp`` rather than a name built from the pid. ``aicp.<pid>.log`` is
    guessable, and ``open(path, "w")`` follows whatever is already sitting at
    that name — so on any box whose temp dir is shared (``/tmp`` on Linux and
    in CI; the sticky bit stops someone deleting another user's file, never
    creating a new name of their own first) a planted symlink got this user's
    CLI output written over a file of the attacker's choosing, at mode 0644.
    ``mkstemp`` opens ``O_EXCL`` at mode 0600: unguessable, unfollowable, and
    unreadable by anyone else.

    The handle is opened once per run and rewound between CLIs rather than
    reopened by path, so the name is never resolved a second time.
    """
    if not enabled:
        yield None
        return
    fd, name = tempfile.mkstemp(prefix="aicp.", suffix=".log")
    try:
        # "w+" so _replay can read back what the child wrote through the
        # inherited fd; errors="replace" because a CLI's output is arbitrary
        # bytes and a decode error here must never break the run.
        with os.fdopen(fd, "w+", encoding="utf-8", errors="replace") as handle:
            yield handle
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def _notify_quietly(notify: NotifyFn, message: str) -> None:
    """Send *message*, swallowing any failure — see the call site's comment."""
    try:
        notify(message)
    except Exception:  # noqa: BLE001 - an injected notifier's failure modes are not knowable here
        return


def _replay(capture, out) -> None:
    """Echo the last 5 lines of a failed CLI's captured output."""
    try:
        capture.seek(0)
        tail = capture.read().splitlines()[-5:]
    except (OSError, ValueError):
        return
    for line in tail:
        print(f"    {DIM}{line}{RESET}", file=out)


def run_step(
    prompt: str,
    chain: Sequence[str],
    *,
    cwd: Path | None = None,
    notify: NotifyFn = _noop,
    verbose: bool = False,
    stream=None,
) -> int:
    """Run *prompt* through *chain*, one styled line per CLI.

    The budget is computed fresh per CLI, inside the loop: each CLI widens its
    own budget from its own history, so the printed hint has to be recomputed
    right before whichever CLI is about to run.
    """
    out = stream if stream is not None else sys.stdout
    with _capture_file(not verbose) as capture:
        for cli in chain:
            argv = invoke_argv(cli, prompt)
            if argv is None or not have(cli):
                continue

            allowed = budget_mod.compute(cli, cwd=cwd)
            hint = t("budget_hint", "budget %ss · %s", allowed.seconds, allowed.note)
            print(f"{BOLD}▸ {prompt}{RESET}  {DIM}{hint}{RESET}", file=out)

            started = time.monotonic()
            spinner = Spinner(f"{cli} · {prompt}")
            try:
                if not verbose:
                    spinner.start()
                if capture is not None:
                    capture.seek(0)  # this CLI's output only, not the last one's
                    capture.truncate()
                rc, timed_out = _invoke(argv, allowed.seconds, cwd=cwd, capture=capture)
            except KeyboardInterrupt:
                timing.append(cli, prompt, int(time.monotonic() - started), "abort", ABORT_RC)
                print(
                    f"{YELLOW}⚠{RESET} "
                    + t("interrupted", "interrupted (Ctrl+C) — aborting, no further steps run"),
                    file=out,
                )
                return ABORT_RC
            finally:
                spinner.stop()

            elapsed = int(time.monotonic() - started)
            outcome = classify(rc, timed_out=timed_out)
            timing.append(cli, prompt, elapsed, outcome, rc)

            if outcome == "abort":
                note = t("step_signal_note", "signal %s", rc - 128)
                print(f"  {YELLOW}⚠{RESET} {CYAN}{cli}{RESET}{DIM}  {elapsed}s · {note}{RESET}", file=out)
                return ABORT_RC

            if outcome == "ok":
                print(f"  {GREEN}✓{RESET} {CYAN}{cli}{RESET}{DIM}  {elapsed}s{RESET}", file=out)
                return 0

            if outcome == "timeout":
                note = t("step_timeout_note", "timed out (> %ss)", allowed.seconds)
                print(f"  {YELLOW}⏱{RESET} {CYAN}{cli}{RESET}{DIM}  {elapsed}s · {note}{RESET}", file=out)
                # A notifier that fails must never abort a run that would
                # otherwise commit and push: the zsh `_aicp_notify` returns 1
                # at worst (it prints "(telegram unavailable)" and carries on)
                # and `_ai_run` ignores that status. T4 owns the injected
                # implementation, so its failure mode is not knowable here.
                _notify_quietly(
                    notify,
                    t(
                        "tg_timeout",
                        '⏱️ aicp: %s timed out running "%s" on %s (over the %ss budget, %s)'
                        " — falling through to the next CLI.",
                        Path(cwd or os.getcwd()).name,
                        prompt,
                        cli,
                        allowed.seconds,
                        allowed.note,
                    )
                )
            else:
                note = t("step_exit_note", "exit %s", rc)
                print(f"  {RED}✗{RESET} {CYAN}{cli}{RESET}{DIM}  {elapsed}s · {note}{RESET}", file=out)

            if capture is not None:
                _replay(capture, out)

        print(f"{RED}" + t("no_cli_for_step", "  ✗ no AI CLI could run this step") + RESET, file=out)
        return 1
