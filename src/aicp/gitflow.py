"""Everything aicp asks git, and the guards that refuse when git can't answer.

Port of ``_aicp_branch``, ``_aicp_diverged``, ``_aicp_preflight``,
``_aicp_precheck``, ``_aicp_commit_panel``, ``_aicp_undo`` and the RESULT-table
block of ``aicp()`` in ``~/scripts/bin/aicp``.

One rule runs through all of it: **every figure is re-read from git, never
taken from an AI CLI's own output** — a CLI that says "pushed!" proves
nothing. Its corollary is the one that keeps biting: a FAILED FETCH IS NOT
"IN SYNC". A fetch that fails (offline, auth expired, remote deleted) leaves
the remote-tracking ref exactly where the last successful fetch left it, so
it still resolves and ``rev-list`` still answers — about the past. The
dangerous answer is the stale "0 ahead, 0 behind", which reads as "already in
sync" and skips the push entirely. Every path here treats "cannot verify" as
a failure, never as a quiet success.

This module and :mod:`aicp.secrets` deliberately know nothing about the
runner, the config loader, the settings menu, or the skills installer;
messages are returned as line lists for the caller to print, so nothing here
needs a terminal either.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ._utils import BOLD, DIM, GREEN, RED, RESET, YELLOW, have, run_interruptible
from .i18n import t
from .secrets import report_lines, scan

__all__ = [
    "RemoteState",
    "ResultSummary",
    "commit_rows",
    "current_branch",
    "diverged",
    "precheck",
    "preflight",
    "remote_for",
    "remote_state",
    "resolve_tz",
    "result_summary",
    "undo",
]

DEFAULT_TZ = "Asia/Taipei"
DEFAULT_TZ_LABEL = "UTC+8"
# AICP_TZ ends up in a real `TZ=` assignment handed to a child process, so its
# shape is pinned to an IANA-zone-looking name rather than passed through.
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$")
#: Abbreviation length every displayed hash in this project commits to.
ABBREV = 8


def _git(cwd: Path | str | None, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError:  # git missing, or cwd gone
        return subprocess.CompletedProcess(args, 1, "", "")


def _out(cwd: Path | str | None, *args: str) -> str:
    done = _git(cwd, *args)
    return done.stdout.strip() if done.returncode == 0 else ""


# ── branch resolution ────────────────────────────────────────────────────────


def current_branch(cwd: Path | str | None = None) -> str | None:
    """The checked-out branch, or ``None`` on a detached HEAD / non-repo.

    Shared by the main flow and ``--undo``: both refuse outright rather than
    guess which branch a detached HEAD "meant".
    """
    return _out(cwd, "symbolic-ref", "--quiet", "--short", "HEAD") or None


def not_on_branch_lines() -> list[str]:
    return [
        f"{RED}"
        + t("not_on_branch", "✗ not on a branch (detached HEAD, or not a git repo)")
        + RESET
    ]


def remote_for(branch: str, cwd: Path | str | None = None) -> str:
    """``branch.<name>.remote``, defaulting to ``origin``."""
    return _out(cwd, "config", "--get", f"branch.{branch}.remote") or "origin"


# ── ahead / behind, and the fetch that has to be believed ────────────────────


@dataclass(frozen=True)
class RemoteState:
    """The result of comparing HEAD with ``<remote>/<branch>``.

    *fetched* is False when the fetch itself failed, *resolved* when the
    remote-tracking ref does not exist. Either one means the counts describe
    the past (or nothing), which is why :attr:`in_sync` requires both.
    """

    fetched: bool
    resolved: bool
    ahead: int = 0
    behind: int = 0

    @property
    def in_sync(self) -> bool:
        return self.fetched and self.resolved and self.ahead == 0 and self.behind == 0

    @property
    def verifiable(self) -> bool:
        return self.resolved


def fetch(remote: str, branch: str, cwd: Path | str | None = None) -> bool:
    """``git fetch -q <remote> <branch>``; True on success.

    Interruptible: fetching a large repo is the longest silent wait in a run,
    and the zsh original got a working Ctrl+C there for free from a global
    trap that this port has to arrange for itself.
    """
    try:
        done = run_interruptible(
            ["git", "fetch", "-q", remote, branch],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return done.returncode == 0


def remote_state(
    remote: str, branch: str, cwd: Path | str | None = None, *, do_fetch: bool = True
) -> RemoteState:
    """Fetch, then count ahead/behind against ``<remote>/<branch>``."""
    fetched = fetch(remote, branch, cwd) if do_fetch else True
    resolved = (
        _git(cwd, "rev-parse", "--verify", "--quiet", f"{remote}/{branch}").returncode == 0
    )
    if not resolved:
        return RemoteState(fetched=fetched, resolved=False)
    counts = _out(cwd, "rev-list", "--left-right", "--count", f"{remote}/{branch}...HEAD")
    behind, _, ahead = counts.partition("\t")
    return RemoteState(
        fetched=fetched,
        resolved=True,
        ahead=int(ahead or 0),
        behind=int(behind or 0),
    )


def diverged(remote: str, branch: str, cwd: Path | str | None = None) -> bool:
    """True when there is something to push or something to rebase onto —
    and also when the answer cannot be established at all, so an unreachable
    or never-pushed branch still reaches the step that handles a remote
    problem instead of being silently skipped."""
    return not remote_state(remote, branch, cwd).in_sync


# ── preflight: fail before the expensive part, not after it ──────────────────


def config_path() -> Path:
    """``$AICP_CONFIG``, else ``~/.aicp/config.json`` — resolved at call time
    (env only; naming the file this port would read is the whole job here,
    and reading its contents belongs to a different module entirely)."""
    override = os.environ.get("AICP_CONFIG")
    return Path(override) if override else Path.home() / ".aicp" / "config.json"


def preflight(chain: Sequence[str]) -> tuple[bool, list[str]]:
    """``(ok, lines)`` — is any CLI in *chain* installed at all?

    Called only from the paths that actually need a CLI, never once at the
    top: a clean, in-sync repo answers both of aicp's questions from git
    alone and succeeds with no AI CLI installed. A blanket pre-flight would
    turn that into a failure — a regression wearing a check's clothing.
    """
    if any(have(cli) for cli in chain):
        return True, []
    return False, [
        f"{RED}" + t("no_cli_found", "✗ no AI CLI found on PATH") + RESET,
        f"{DIM}" + t("no_cli_looked_for", "  looked for: %s", ", ".join(chain)) + RESET,
        f"{DIM}"
        + t("no_cli_install", "  install one, or set AICP_CLI_ORDER in %s", config_path())
        + RESET,
    ]


def precheck(
    chain: Sequence[str],
    cwd: Path | str | None = None,
    *,
    do_commit: bool = True,
) -> tuple[bool, list[str]]:
    """``(proceed, lines)`` — ask git what is pending, and only if something
    is, check a CLI exists and scan for secrets.

    The order is the point: git first because it is cheap and can end the
    question outright; the CLI check before the scan so a machine with no CLI
    does not pay for reading the whole tree; the scan last because it is the
    expensive one.
    """
    if not _out(cwd, "status", "--porcelain"):
        return True, []
    if not do_commit:
        return True, []
    ok, lines = preflight(chain)
    if not ok:
        return False, lines
    result = scan(cwd)
    return result.ok, report_lines(result)


# ── NEW COMMITS panel ────────────────────────────────────────────────────────


def resolve_tz() -> str:
    """``$AICP_TZ`` if it looks like an IANA zone name, else the default.

    Validated rather than trusted: the value is handed to a child process as
    a real ``TZ=`` assignment.
    """
    raw = os.environ.get("AICP_TZ") or DEFAULT_TZ
    return raw if _TZ_RE.match(raw) else DEFAULT_TZ


def tz_label() -> str:
    return os.environ.get("AICP_TZ_LABEL") or DEFAULT_TZ_LABEL


def commit_rows(
    before: str, after: str, cwd: Path | str | None = None
) -> list[tuple[str, str]]:
    """``(hash, "MM-DD HH:MM  subject")`` per commit in ``before..after``.

    Oldest first, 8-char hashes, and the SUBJECT ONLY — the body is
    deliberately never shown, being multi-line prose that would wreck the
    frame. The fixed-width timestamp leads so every subject starts at the
    same column; the zone is named once in the panel title instead of on
    every row. Rows go straight into ``present.render(..., mode="panel",
    zebra=True)``, which bands from the second row down.
    """
    env = dict(os.environ, TZ=resolve_tz())
    try:
        done = subprocess.run(
            [
                "git",
                "log",
                "--no-color",
                "--reverse",
                f"--abbrev={ABBREV}",
                "--date=format-local:%m-%d %H:%M",
                "--format=%h%x1f%ad%x1f%s",
                f"{before}..{after}",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError:
        return []
    if done.returncode != 0:
        return []
    rows = []
    for line in done.stdout.splitlines():
        short, _, rest = line.partition("\x1f")
        date, _, subject = rest.partition("\x1f")
        rows.append((short, f"{date}  {subject}"))
    return rows


# ── the git-verified RESULT table ────────────────────────────────────────────


@dataclass(frozen=True)
class ResultSummary:
    """What actually happened, read back from git after the CLIs have run.

    ``ahead``/``behind`` carry ``-1`` as the "comparison impossible" sentinel
    (the remote-tracking ref does not resolve) — never 0, which would claim a
    push that never happened.
    """

    repo: str
    branch: str
    remote: str
    created: int
    ahead: int
    behind: int
    fetch_failed: bool

    @property
    def verifiable(self) -> bool:
        return self.ahead >= 0

    @property
    def in_sync(self) -> bool:
        return self.verifiable and self.ahead == 0 and self.behind == 0

    @property
    def fetch_note(self) -> str:
        """The caveat printed above the table when the fetch failed.

        A push updates its own remote-tracking ref locally, so Ahead/Behind
        are usually still right after a failed fetch; what they can no longer
        account for is anything the remote gained meanwhile. The numbers are
        still shown — with this said out loud rather than presented as
        freshly verified.
        """
        if not self.fetch_failed:
            return ""
        return (
            f"{YELLOW}"
            + t(
                "fetch_note",
                "⚠ git fetch %s %s failed — Ahead/Behind below are read from the last known remote state, not a fresh one",
                self.remote,
                self.branch,
            )
            + RESET
        )

    @property
    def status(self) -> str:
        if not self.verifiable:
            return t("state_remote_missing", "✗ %s/%s not found", self.remote, self.branch)
        if self.in_sync:
            return t("state_in_sync", "✅ in sync")
        return t("state_not_pushed", "⚠️  not pushed")

    def rows(self) -> list[tuple[str, str]]:
        """Plain (label, value) pairs for ``present.render(..., "RESULT")``."""
        cell = "?" if not self.verifiable else None
        return [
            (t("result_repo", "Repo"), self.repo),
            (t("result_branch", "Branch"), self.branch),
            (t("result_remote", "Remote"), f"{self.remote}/{self.branch}"),
            (t("result_new_commits", "New commits"), str(self.created)),
            (t("result_ahead", "Ahead"), cell or str(self.ahead)),
            (t("result_behind", "Behind"), cell or str(self.behind)),
            (t("result_status", "Status"), self.status),
        ]


def result_summary(
    before: str,
    after: str,
    remote: str,
    branch: str,
    cwd: Path | str | None = None,
) -> ResultSummary:
    """Re-read the whole outcome from git: how many commits were really
    created, and where HEAD really stands against the remote."""
    created = _out(cwd, "rev-list", "--count", f"{before}..{after}")
    toplevel = _out(cwd, "rev-parse", "--show-toplevel")
    state = remote_state(remote, branch, cwd)
    return ResultSummary(
        repo=Path(toplevel).name if toplevel else "",
        branch=branch,
        remote=remote,
        created=int(created or 0),
        ahead=state.ahead if state.resolved else -1,
        behind=state.behind if state.resolved else -1,
        fetch_failed=not state.fetched,
    )


# ── --undo ───────────────────────────────────────────────────────────────────


def undo(cwd: Path | str | None = None) -> tuple[bool, list[str]]:
    """``git reset --soft HEAD^`` — the escape hatch for a bad commit message
    or a wrong stage. Returns ``(ok, lines)`` and runs no AI CLI on any path.

    ``--soft`` only rewinds HEAD and the index pointer, so the change lands
    back in the index exactly as it was mid-edit. Refusal is the point: once
    a commit reaches the remote, other clones or CI may already be building
    on it, so this must never be the command that quietly rewinds shared
    history. Four refusals, all with HEAD untouched:

    * detached HEAD — there is no branch to compare against;
    * no parent commit — nothing to reset onto;
    * the commit is already on the remote;
    * the remote cannot be verified (deleted remote, failed fetch, never
      pushed). "Cannot verify" is not "verified safe": the whole point of the
      guard is to never rewind a commit that might already be public, so an
      unprovable case is treated as the risky one.

    The ahead/behind comparison is spelled out here rather than delegated to
    :func:`diverged`, which answers the wider "is there ANY difference"
    (true on ahead OR behind OR unresolvable); undo needs the narrower
    ``ahead == 0``, i.e. HEAD carries nothing the remote doesn't have.
    """
    branch = current_branch(cwd)
    if branch is None:
        return False, not_on_branch_lines()

    if _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^").returncode != 0:
        return False, [
            f"{RED}"
            + t("undo_no_parent", "✗ nothing to undo — HEAD has no parent commit")
            + RESET
        ]

    remote = remote_for(branch, cwd)
    state = remote_state(remote, branch, cwd)
    if not state.fetched or not state.resolved:
        return False, [
            f"{RED}"
            + t(
                "undo_unverifiable",
                "✗ refusing --undo: cannot verify %s/%s (unreachable, or never pushed) — could be rewriting pushed history",
                remote,
                branch,
            )
            + RESET
        ]
    if state.ahead == 0:
        return False, [
            f"{RED}"
            + t(
                "undo_already_pushed",
                "✗ refusing --undo: the last commit is already on %s/%s — undo would rewrite pushed history",
                remote,
                branch,
            )
            + RESET
        ]

    short = _out(cwd, "rev-parse", f"--short={ABBREV}", "HEAD")
    subject = _out(cwd, "log", "-1", "--format=%s")
    if _git(cwd, "reset", "--soft", "HEAD^").returncode != 0:
        return False, [
            f"{RED}" + t("undo_reset_failed", "✗ git reset --soft HEAD^ failed") + RESET
        ]
    return True, [
        f"{GREEN}{BOLD}" + t("undo_done", "✓ undone:") + f"{RESET} {YELLOW}{short}{RESET} {subject}",
        f"{DIM}"
        + t("undo_note", "  changes are back in the index — not lost, not pushed.")
        + RESET,
    ]
