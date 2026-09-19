"""Shared pytest harness for the whole aicp test suite.

Reproduces, in pytest, what ``~/scripts/test_aicp.sh`` does by hand (a tmp
git repo, stub AI CLIs on PATH, isolation from the developer's real
``~/.aicprc``/``~/.aicp/timing.log``) — see that file's harness section
(roughly its first ~270 lines) for the shell-test patterns these fixtures
port.

**Fixture -> consumer map** (who each one is actually for):

- ``pinned_environment`` (autouse)      — every task; deterministic locale/
  width/isolation, no opt-in required.
- ``git_repo``                          — T2 (runner), T4 (git-verified
  result summary): a throwaway repo with one commit, nothing pending.
- ``git_repo_synced``                   — T4 primarily: repo + a real bare
  remote, already pushed, so "in sync" is genuinely true rather than
  asserted against an unreachable ref.
- ``stub_cli``                          — T2 (fallback-chain runner) and T4
  (anything that shells out to a CLI to verify a result). Fakes all 6
  roster binary names onto PATH; T5 (the skills installer) needs NONE of
  this — it never invokes an AI CLI binary, so it only needs
  ``pinned_environment``'s HOME/config isolation.
- ``call_log``                          — paired with ``stub_cli``; reads
  back what was recorded.

**Hard rule:** add task-specific fixtures LOCALLY, in your own test module, as
a plain ``@pytest.fixture`` — never here.

This rule used to read "never edit this file" outright, because T2-T5 were
being written in parallel and a conflicting edit here blocked every other
task's tests from collecting. That contention has ended, so the rule is now
what it always meant: no task-specific fixtures. A small, generic helper every
module would otherwise copy (:func:`rmtree`) belongs here and may be added.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# ── locale / width / isolation (autouse — every task gets this for free) ────


@pytest.fixture(autouse=True)
def pinned_environment(tmp_path, monkeypatch):
    """Deterministic locale, terminal width, and filesystem isolation.

    - ``LANG``/``LC_ALL``/``LC_MESSAGES`` pinned to a UTF-8 English locale,
      like ai-accounts' conftest, so width/locale-dependent output (braille
      spinner frames, CJK width) doesn't depend on the machine running the
      suite.
    - ``COLUMNS`` pinned wide, so no test's box-drawing assertion depends on
      the ambient terminal width of whoever runs pytest.
    - ``HOME`` pointed at a throwaway directory, and ``AICP_CONFIG``/
      ``AICP_TIMING_LOG`` pointed at throwaway paths under it — so no test
      run, however it fails, ever reads or writes the developer's real
      ``~/.aicprc`` or ``~/.aicp/timing.log``.
    - ``TG_BOT_TOKEN``/``TG_CHAT_ID`` cleared — ``aicp.notify`` reads these
      straight from the environment (see its module docstring), and this is
      exactly the kind of machine that already has a real bot token exported
      for ``tg-send.sh``. Without this, any test that calls ``notify()``
      without explicitly mocking the send would fire a real Telegram message.
    """
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.delenv("NO_COLOR", raising=False)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    if sys.platform == "win32":
        monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("AICP_CONFIG", str(fake_home / "nonexistent.aicprc"))
    monkeypatch.setenv("AICP_TIMING_LOG", str(fake_home / ".aicp" / "timing.log"))
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    yield fake_home


# ── git repo fixtures ─────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def rmtree(path: Path) -> None:
    """``shutil.rmtree`` that also works on a git object store on Windows.

    git writes loose objects read-only. POSIX needs write permission on the
    *containing directory* to unlink a file and ignores the file's own mode, so
    macOS and Linux delete a bare repo without noticing; Windows honors the
    read-only attribute on the file itself and fails the unlink with
    ``PermissionError: [WinError 5]``. Clearing the bit first is the portable
    fix, and needs no ``onerror``/``onexc`` branch (the two spellings of
    rmtree's error hook, which swapped names in 3.12).

    Used by the tests that delete a bare remote to simulate a failed fetch.
    """
    if sys.platform == "win32":
        for child in Path(path).rglob("*"):
            child.chmod(stat.S_IWRITE)
    shutil.rmtree(path)


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A throwaway git repo with one commit, nothing pending — the base
    fixture for anything that just needs "a real repo to operate in"."""
    return _init_repo(tmp_path / "repo")


@pytest.fixture
def git_repo_synced(tmp_path) -> tuple[Path, Path]:
    """``(repo, bare_remote)`` — *repo* has ``origin`` set to *bare_remote*
    and ``main`` already pushed, so the pair is genuinely in sync (as
    opposed to asserting "in sync" against a remote ref that was never
    actually resolved)."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = _init_repo(tmp_path / "repo")
    _git("remote", "add", "origin", str(bare), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    return repo, bare


# ── stub AI CLIs ──────────────────────────────────────────────────────────────

#: Every fallback CLI binary name aicp.contracts.ROSTER knows about. Kept as
#: a plain tuple here (not imported from aicp.contracts) so this harness has
#: no import-order dependency on the package under test — but see
#: test_cross_platform.py's roster-parity test, which pins the two in sync.
ALL_CLIS: tuple[str, ...] = ("copilot", "agy", "codex", "claude", "vibe", "grok")

_SH_STUB = """#!/bin/sh
{{
  printf '%s' "{cli}"
  for a in "$@"; do printf '\\t%s' "$a"; done
  printf '\\n'
}} >> "{call_log}"
exit {exit_code}
"""

_CMD_STUB = """@echo off
setlocal
set "line={cli}"
:aicp_stub_loop
if "%~1"=="" goto :aicp_stub_done
set "line=%line%	%~1"
shift
goto :aicp_stub_loop
:aicp_stub_done
>> "{call_log}" echo %line%
exit /b {exit_code}
"""


@pytest.fixture
def call_log(tmp_path) -> Path:
    """Path to the call-log file :func:`stub_cli` fixtures write into."""
    return tmp_path / "calls.log"


@pytest.fixture
def stub_cli(tmp_path, call_log, monkeypatch):
    """Factory: install fake executables for AI CLI binary names onto PATH.

    ``stub_cli()`` (defaults) stubs all 6 :data:`ALL_CLIS` names, each
    exiting 0 and appending one TAB-separated line — ``<cli>\\t<arg1>\\t
    <arg2>\\t...`` (the CLI's own arguments, not argv[0]/the program name
    itself) — to :func:`call_log` per invocation. Every fallback name
    is stubbed, not just the one a test expects to be tried first: a repo
    state that makes the chain skip past the first CLI must never fall
    through to a real CLI actually installed on the machine running the
    suite (the same lesson ``test_aicp.sh`` documents for its own
    ``stub_dir``).

    Parameters let a test dial in specific behavior:

    - ``exit_code`` — every stubbed CLI's exit status (default 0).
    - ``clis`` — override the set of names stubbed (default: all of them).
    - ``per_cli`` — ``{name: exit_code}`` to give individual CLIs a
      different status than the shared default (e.g. make ``copilot`` fail
      so the chain falls through to ``agy``).

    Returns the directory the stubs were written to; it is already prepended
    to ``PATH`` for the duration of the test.
    """

    def _install(
        *,
        exit_code: int = 0,
        clis: tuple[str, ...] = ALL_CLIS,
        per_cli: dict[str, int] | None = None,
    ) -> Path:
        bin_dir = tmp_path / "stub-bin"
        bin_dir.mkdir(exist_ok=True)
        per_cli = per_cli or {}
        for cli in clis:
            code = per_cli.get(cli, exit_code)
            if sys.platform == "win32":
                path = bin_dir / f"{cli}.cmd"
                path.write_text(_CMD_STUB.format(cli=cli, call_log=call_log, exit_code=code))
            else:
                path = bin_dir / cli
                path.write_text(_SH_STUB.format(cli=cli, call_log=call_log, exit_code=code))
                path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        return bin_dir

    return _install
