"""Cross-platform helpers shared across aicp — macOS, Linux, Windows.

Patterns (platform flags, ANSI/Windows VT enabling, ``have()``) mirror
``~/Documents/Workspace/ai-accounts``'s ``src/ai_accounts/_utils.py`` so the
two projects feel like one family; nothing is imported across the two repos,
each ships its own copy (same posture the zsh original documents for
``lib/spinner.sh`` and ``lib/box_render.py``).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[38;5;82m"
YELLOW = "\033[38;5;220m"
RED = "\033[38;5;203m"
CYAN = "\033[38;5;87m"
MAGENTA = "\033[38;5;213m"
BLUE = "\033[38;5;75m"


def _enable_windows_ansi() -> bool:
    """Turn on virtual-terminal processing so ANSI escapes render on Windows.

    No-op (returns True) on non-Windows. On modern Windows 10+ consoles this
    flips ENABLE_VIRTUAL_TERMINAL_PROCESSING for both stdout and stderr.
    """
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ENABLE_VT = 0x0004
        ok = False
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            if kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT):
                ok = True
        return ok
    except Exception:  # noqa: BLE001 - best-effort VT enable, any ctypes failure means "no"
        return False


# Enable VT once at import time so ANSI output renders on Windows terminals.
_WIN_ANSI_OK = _enable_windows_ansi()


def color_supported(stream=None) -> bool:
    """Whether ANSI color/animation should be drawn on *stream* (default stderr)."""
    stream = stream if stream is not None else sys.stderr
    try:
        if not stream.isatty():
            return False
    except Exception:  # noqa: BLE001 - a stream with no working isatty() is never a TTY
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if IS_WINDOWS:
        return _WIN_ANSI_OK or bool(os.environ.get("WT_SESSION"))
    return True


def have(cmd: str) -> bool:
    """True if *cmd* resolves to an executable on PATH."""
    import shutil

    return shutil.which(cmd) is not None


def home_config_dir(env_var: str, default: Path) -> Path:
    """Resolve a config path: ``$<env_var>`` override, else *default*."""
    override = os.environ.get(env_var)
    return Path(override) if override else default


# ── interruptible subprocess ─────────────────────────────────────────────────
#
# The zsh original's ``_aicp_timeout`` wraps every CLI invocation with GNU
# `timeout --foreground` specifically so Ctrl+C reaches the child directly:
# without --foreground, `timeout` puts the child in a NEW process group, and
# the terminal only delivers SIGINT to the FOREGROUND process group — so the
# signal never reached the CLI, the CLI never exited, and the whole script
# hung waiting on it (a real, previously-observed bug, not a theoretical one;
# see the long comment above `_aicp_timeout` in bin/aicp). The documented cost
# there is that a timeout then only signals the CLI itself, not any
# grandchildren it spawned — an interruptible Ctrl+C is worth more than
# reaping those.
#
# run_interruptible reproduces the same posture without shelling out to GNU
# timeout at all:
#   POSIX    the child is placed in THIS process's own process group (i.e. no
#            new group is created for it — the same effect --foreground
#            achieves), so Ctrl+C at the terminal reaches it directly, same as
#            if it had been run with no wrapper at all.
#   Windows  there is no equivalent "foreground process group" concept.
#            CREATE_NEW_PROCESS_GROUP lets this process send CTRL_C_EVENT to
#            the child WITHOUT also killing itself (plain Ctrl+C on Windows
#            targets every process attached to the same console) — but that
#            means the child does NOT receive the console's own Ctrl+C
#            directly; the caller must catch KeyboardInterrupt and forward
#            CTRL_C_EVENT itself. This is an honest limitation, not a full
#            port of the POSIX behavior: a child that ignores CTRL_C_EVENT (or
#            is a console subsystem app that doesn't install a handler) will
#            not stop, and there is no equivalent of --foreground's direct
#            delivery. Callers on Windows needing a hard kill should follow up
#            with Popen.terminate()/kill() rather than assume the event lands.
def run_interruptible(cmd, **kwargs) -> subprocess.CompletedProcess[str]:
    """Run *cmd* so Ctrl+C reaches it directly (POSIX) or can be forwarded
    (Windows), returning a ``subprocess.CompletedProcess`` like
    ``subprocess.run`` would.

    T2 layers a timeout and a kill-after grace period on top of this (its own
    fallback-chain budget per CLI); T4 uses it bare for slow, uninterruptible
    calls like ``git fetch``. Extra ``**kwargs`` are passed through to
    ``subprocess.Popen`` unchanged (e.g. ``cwd``, ``env``, ``stdout``).
    """
    kwargs.setdefault("text", True)
    if IS_WINDOWS:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, **kwargs)
        try:
            stdout, stderr = proc.communicate()
        except KeyboardInterrupt:
            try:
                proc.send_signal(signal.CTRL_C_EVENT)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - CTRL_C_EVENT delivery can fail many ways; fall back to a hard terminate
                proc.terminate()
            stdout, stderr = proc.communicate()
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    # POSIX: start_new_session=False (the default) keeps the child in this
    # process's own process group — no new group is created for it, so the
    # terminal's SIGINT (delivered to the whole foreground process group)
    # reaches the child the same way it would with no wrapper at all. This is
    # the direct equivalent of GNU `timeout --foreground`.
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate()
    except KeyboardInterrupt:
        # The child already received the same SIGINT (same process group);
        # wait for it to unwind rather than second-guessing it with a kill.
        stdout, stderr = proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
