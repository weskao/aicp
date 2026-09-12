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

__all__ = ["ABORT_RC", "KILL_AFTER", "TIMEOUT_RC", "classify", "invoke_argv", "run_step"]

TIMEOUT_RC = 124  # GNU timeout's own "the command timed out" status
ABORT_RC = 130    # 128 + SIGINT
KILL_AFTER = 10   # grace period between SIGTERM and SIGKILL (timeout --kill-after=10)

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
    argv: list[str], seconds: int, *, cwd: Path | None, capture: Path | None
) -> tuple[int, bool]:
    """Run *argv* under a *seconds* budget; returns ``(rc, timed_out)``.

    ``seconds <= 0`` means no timeout at all (GNU timeout reads 0 the same
    way), and takes the bare :func:`~aicp._utils.run_interruptible` path.
    """
    kwargs: dict[str, object] = {"cwd": cwd}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    with _capture_to(capture) as handle:
        if handle is not None:
            kwargs["stdout"] = handle
            kwargs["stderr"] = subprocess.STDOUT
        if seconds <= 0:
            return normalize_rc(run_interruptible(argv, **kwargs).returncode), False

        proc = subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]
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
        return normalize_rc(proc.returncode), False


@contextlib.contextmanager
def _capture_to(path: Path | None) -> Iterator[object | None]:
    """A handle for the child's combined output, or None to inherit the screen."""
    if path is None:
        yield None
        return
    with open(path, "w", encoding="utf-8") as handle:
        yield handle


def _replay(log: Path, out) -> None:
    """Echo the last 5 lines of a failed CLI's captured output."""
    try:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
    except OSError:
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
    log = Path(tempfile.gettempdir()) / f"aicp.{os.getpid()}.log"
    try:
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
                rc, timed_out = _invoke(
                    argv, allowed.seconds, cwd=cwd, capture=None if verbose else log
                )
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
                notify(
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

            if not verbose:
                _replay(log, out)

        print(f"{RED}" + t("no_cli_for_step", "  ✗ no AI CLI could run this step") + RESET, file=out)
        return 1
    finally:
        try:
            log.unlink()
        except OSError:
            pass
