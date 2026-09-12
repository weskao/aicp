"""Command-line entry point (``aicp = "aicp.cli:main"`` in pyproject.toml).

Port of ``aicp()`` and the argument parser at the foot of ``~/scripts/bin/aicp``.

This module is the only place the feature modules meet. Everything it needs
already exists and is tested — the fallback-chain runner, the ``.aicprc``
layer, the git guards, the secret scanner, the skills probe, the settings
menu — so the job here is wiring, in the one order that is load-bearing:

* **git first, an AI CLI only if git says there is work.** A clean, in-sync
  repo answers both of aicp's questions from git alone and exits 0 with no AI
  CLI installed at all; a blanket preflight at the top would turn that into a
  failure. :func:`aicp.gitflow.precheck` owns that ordering (status, then the
  CLI check, then the expensive tree scan) and is called instead of being
  re-implemented here.
* **Every figure in the RESULT table is re-read from git**, never taken from a
  CLI's own output — :func:`aicp.gitflow.result_summary` does the re-reading.
* **The notifier is injected, not imported by its consumer.** ``contracts.py``
  makes this module the designated call site: :func:`aicp.notify.notify` is
  passed into :func:`aicp.runner.run_step` as a parameter, which is what keeps
  the runner testable with no network.

It is also the designated **config -> consumer bridge** — see
:func:`export_settings`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from . import __version__, config, gitflow, menu, notify, present, runner, skills
from ._utils import BLUE, BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW, have
from .contracts import ROSTER
from .i18n import t

__all__ = ["build_parser", "export_settings", "main"]

_BY_NAME = {entry.name: entry for entry in ROSTER}

#: How many commits each side of the "not in sync" tail prints, on screen and
#: in the notification — the zsh original's `head -5`.
_TAIL = 5


# ── the config -> consumer bridge (defect D3) ────────────────────────────────


def export_settings(settings: config.Settings) -> None:
    """Put the resolved ``AICP_*`` values into ``os.environ``.

    ``config.resolve()`` validates knobs that **no consumer ever reads from
    it**: :mod:`aicp.budget` and :mod:`aicp.gitflow` read ``os.environ``
    directly, and correctly so — ``contracts.py`` forbids them importing the
    config layer. Without this bridge an ``AICP_TZ`` or ``AICP_TIMEOUT_BASE``
    written in ``.aicprc`` was parsed, validated, reported in ``Settings`` —
    and then silently ignored by the only two modules that act on it.

    ``setdefault`` is the whole precedence rule: a real environment variable is
    already in ``os.environ`` and is therefore left exactly as it was, so
    **environment still beats file**. Only a key the file alone supplied is
    added.

    Safe by construction with respect to :data:`aicp.config.DENYLIST`:
    ``settings.values`` is what :func:`aicp.config.load_config` accepted, and
    that loader drops ``AICP_TG_SEND`` / ``AICP_TIMING_LOG`` before they ever
    reach here — so a file can still never smuggle an exec-path or
    path-mutation knob into the environment through this door. Whoever adds
    the next such knob adds it to ``DENYLIST``, not to a filter here.
    """
    for key, value in settings.values.items():
        os.environ.setdefault(key, value)


# ── small local helpers ──────────────────────────────────────────────────────


def _git_out(*args: str) -> str:
    """``git <args>`` stdout, or "" if git fails — the cwd is the repo."""
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    except OSError:  # git missing, or cwd gone
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _echo(lines: Sequence[str]) -> None:
    for line in lines:
        print(line)


def _dim(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


def _spin(label: str, verbose: bool):
    """Animate *label* while an otherwise silent step runs.

    ``--verbose`` streams instead, the same rule ``run_step`` follows; and
    :class:`aicp.present.Spinner` disables itself off a TTY, so a non-TTY run
    (this suite, CI, cron) prints exactly what it printed before.
    """
    return contextlib.nullcontext() if verbose else present.Spinner(label)


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return f"~{path[len(home):]}" if home and path.startswith(home) else path


def _log_lines(rev_range: str) -> list[str]:
    return _git_out("log", "--oneline", rev_range).splitlines()[:_TAIL]


def _nudge(chain: Sequence[str]) -> None:
    """One dim, non-blocking line when the CLI about to run has no skills.

    Hot path: only the CLI that will actually be tried first is probed, and
    :func:`aicp.skills.missing_skills` is existence-only (one ``stat`` per
    target, no reads, no sidecar parsing).
    """
    for name in chain:
        if not have(name):
            continue
        entry = _BY_NAME.get(name)
        if entry is None:
            return
        missing = skills.missing_skills(entry)
        if missing:
            _dim(
                t(
                    "skill_nudge",
                    "▸ %s is missing the %s skill — run aicp --config to install it",
                    name,
                    ", ".join(missing),
                )
            )
        return


# ── argument parsing ─────────────────────────────────────────────────────────


class _Parser(argparse.ArgumentParser):
    """argparse's own ``error`` exits 2 and prints its full usage; the zsh
    original named the offending argument and exited 1. Keep that contract —
    it is the one this tool's callers and tests already rely on."""

    def error(self, message: str) -> NoReturn:
        print(f"aicp: {message}", file=sys.stderr)
        self.print_usage(sys.stderr)
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="aicp",
        description="AI commit + push, with a git-verified result summary.",
        # No abbreviation matching: `--conf` must not silently become
        # `--config` (which runs the settings menu instead of a commit).
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"aicp {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="stream each CLI's output live instead of a spinner",
    )
    parser.add_argument(
        "--undo", action="store_true",
        help="undo the last commit (git reset --soft HEAD^); refuses once it is "
             "on the remote. Runs no AI CLI",
    )
    parser.add_argument(
        "--swap-ai", action="store_true",
        help="move a CLI to #1 in the fallback chain and save it to .aicprc",
    )
    parser.add_argument(
        "--config", action="store_true",
        help="settings menu: the two steps, the language, the leading CLI",
    )
    parser.add_argument(
        "--doctor", action="store_true",
        help="report where each vendored skill stands for each CLI",
    )
    parser.add_argument("--json", action="store_true", help="--doctor as JSON")
    parser.add_argument(
        "--install-skills", action="store_true",
        help="install the vendored skills into every configured CLI",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="--install-skills: actually write (without it, only a preview)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="--install-skills: replace a foreign file, keeping a .bak",
    )
    return parser


# ── the CI escape hatches ────────────────────────────────────────────────────


def _doctor(*, as_json: bool) -> int:
    if as_json:
        print(json.dumps(skills.status_json(), indent=2, ensure_ascii=False))
        return 0
    rows = [(f"{s.cli} · {s.skill}", s.version or s.state) for s in skills.full_status()]
    _echo(present.render(rows, t("skills_title", "SKILLS")))
    return 0


def _install_skills(*, yes: bool, force: bool) -> int:
    if not yes:
        rows = [(f"{s.cli} · {s.skill}", s.state) for s in skills.full_status()]
        _echo(present.render(rows, t("skills_title", "SKILLS")))
        _dim(t("install_needs_yes", "  re-run with --install-skills --yes to write"))
        return 0
    for result in skills.install(force=force):
        backup = f"  (backup: {result.backup})" if result.backup else ""
        print(f"  {CYAN}{result.cli}{RESET} · {result.skill}: {result.action}{backup}")
    return 0


# ── the main flow (port of aicp()) ───────────────────────────────────────────


def _run_panel(repo: str, top: str, branch: str, remote: str) -> None:
    _echo(
        present.render(
            [
                (
                    t("panel_repo", "repo"),
                    f"{MAGENTA}{BOLD}{repo}{RESET}  {DIM}{_tilde(top)}{RESET}",
                ),
                (
                    t("panel_branch", "branch"),
                    f"{CYAN}{branch}{RESET} {DIM}→{RESET} {CYAN}{remote}/{branch}{RESET}",
                ),
            ],
            "aicp",
            mode="panel",
            accent=BLUE,
        )
    )
    print()


def _commit_step(
    settings: config.Settings, chain: Sequence[str], before: str, *, verbose: bool
) -> int | None:
    """``None`` to carry on; an exit code to stop the run."""
    dirty = bool(_git_out("status", "--porcelain"))

    if not settings.do_commit:
        _dim(
            t(
                "skip_commit_config",
                "▸ /commit is switched off in config (AICP_DO_COMMIT=0) — skipping",
            )
        )
        return None
    if not dirty:
        _dim(t("skip_commit_clean", "▸ no uncommitted changes — skipping /commit"))
        return None

    _nudge(chain)
    rc = runner.run_step("/commit", chain, notify=notify.notify, verbose=verbose)
    if rc == runner.ABORT_RC:
        return rc
    if rc != 0:
        return 1

    # Shown here, attached to the step that produced it, rather than after the
    # push: the panel answers "what did /commit write?", which is stale by the
    # time the push summary arrives. The RESULT table still counts the whole
    # run, so a commit created by /safe-git-push (a rebase) is not lost.
    rows = gitflow.commit_rows(before, _git_out("rev-parse", "HEAD"))
    if rows:
        print()
        _echo(
            present.render(
                rows,
                f'{t("commits_panel_title", "NEW COMMITS")} · {gitflow.tz_label()}',
                mode="panel",
                accent=GREEN,
                zebra=True,
            )
        )
    return None


def _push_step(
    settings: config.Settings,
    chain: Sequence[str],
    remote: str,
    branch: str,
    *,
    verbose: bool,
) -> int | None:
    """``None`` to carry on; an exit code to stop the run."""
    if not settings.do_push:
        _dim(
            t(
                "skip_push_config",
                "▸ /safe-git-push is switched off in config (AICP_DO_PUSH=0) — skipping",
            )
        )
        return None

    # Ask git before asking an AI: only a divergence is worth a CLI call.
    # Behind counts too — that is the rebase/pull case /safe-git-push exists
    # for — so the step is skipped only when both sides are already identical.
    # An unresolvable remote ref is never read as "in sync": it runs the step.
    with _spin(t("spin_fetch", "git fetch %s/%s…", remote, branch), verbose):
        work_to_do = gitflow.diverged(remote, branch)

    if not work_to_do:
        _dim(
            t(
                "skip_push_synced",
                "▸ %s/%s already in sync — skipping /safe-git-push",
                remote,
                branch,
            )
        )
        return None

    ok, lines = gitflow.preflight(chain)
    if not ok:
        _echo(lines)
        return 1
    _nudge(chain)
    rc = runner.run_step("/safe-git-push", chain, notify=notify.notify, verbose=verbose)
    return rc if rc == runner.ABORT_RC else None


def _report(summary: gitflow.ResultSummary, repo: str, remote: str, branch: str) -> int:
    """Print the git-verified RESULT table and decide the exit code."""
    if summary.fetch_note:
        print(summary.fetch_note)
    _echo(present.render(summary.rows(), t("result_title", "RESULT")))

    if summary.in_sync:
        return 0

    if not summary.verifiable:
        print()
        print(
            f"{RED}"
            + t(
                "cannot_verify",
                "cannot verify the push: %s/%s does not resolve.",
                remote,
                branch,
            )
            + RESET
        )
        _dim(
            t(
                "cannot_verify_note",
                "  the remote may be unreachable, or the branch was never pushed.",
            )
        )
        notify.notify(
            t(
                "tg_unverifiable",
                "⚠️ aicp: cannot verify the push of %s on %s —— %s/%s does not resolve "
                "(remote unreachable, or the branch was never pushed). %s new commit(s).",
                branch,
                repo,
                remote,
                branch,
                summary.created,
            )
        )
        return 1

    local_title = t("local_not_pushed", "local not pushed:")
    remote_title = t("remote_not_merged", "remote not merged:")
    local = _log_lines(f"{remote}/{branch}..HEAD")
    incoming = _log_lines(f"HEAD..{remote}/{branch}")

    print()
    for title, entries in ((local_title, local), (remote_title, incoming)):
        print(f"{YELLOW}{title}{RESET}")
        for line in entries:
            print(f"  {DIM}{line}{RESET}")

    notify.notify(
        t(
            "tg_not_synced",
            "⚠️ aicp: %s on %s is not in sync with %s/%s",
            branch,
            repo,
            remote,
            branch,
        )
        + "\n\n"
        + f'| {t("tg_tbl_item", "Item")} | {t("tg_tbl_value", "Value")} |\n'
        + "| --- | --- |\n"
        + f'| {t("tg_tbl_new_commits", "New commits")} | {summary.created} |\n'
        + f'| {t("tg_tbl_ahead", "Ahead")} | {summary.ahead} |\n'
        + f'| {t("tg_tbl_behind", "Behind")} | {summary.behind} |\n\n'
        + local_title
        + "\n"
        + "\n".join(local)
        + "\n"
        + remote_title
        + "\n"
        + "\n".join(incoming)
    )
    return 1


def _flow(settings: config.Settings, *, verbose: bool) -> int:
    chain = settings.cli_chain

    branch = gitflow.current_branch()
    if branch is None:
        _echo(gitflow.not_on_branch_lines())
        return 1
    remote = gitflow.remote_for(branch)
    top = _git_out("rev-parse", "--show-toplevel")
    repo = Path(top).name if top else ""
    before = _git_out("rev-parse", "HEAD")

    _run_panel(repo, top, branch, remote)

    # precheck owns the order that matters (git status, then "is any CLI
    # installed", then the expensive secret scan) and refuses the run outright
    # on a secret hit — no AI CLI is invoked on that path.
    # ponytail: _commit_step re-reads `git status --porcelain` to decide
    # "dirty", one extra warm call rather than widening precheck's frozen
    # return shape; revisit only if a repo is big enough to notice.
    with _spin(t("spin_precheck", "checking the working tree…"), verbose):
        proceed, lines = gitflow.precheck(chain, do_commit=settings.do_commit)
    _echo(lines)
    if not proceed:
        return 1

    stop = _commit_step(settings, chain, before, verbose=verbose)
    if stop is not None:
        return stop
    print()

    stop = _push_step(settings, chain, remote, branch, verbose=verbose)
    if stop is not None:
        return stop
    print()

    # Trust git, not the CLI: read the real state back before reporting.
    after = _git_out("rev-parse", "HEAD")
    with _spin(t("spin_fetch", "git fetch %s/%s…", remote, branch), verbose):
        summary = gitflow.result_summary(before, after, remote, branch)
    return _report(summary, repo, remote, branch)


# ── entry point ──────────────────────────────────────────────────────────────


def _dispatch(args: argparse.Namespace) -> int:
    settings = config.resolve()
    export_settings(settings)

    if args.doctor:
        return _doctor(as_json=args.json)
    if args.install_skills:
        return _install_skills(yes=args.yes, force=args.force)
    if args.config:
        return menu.config_menu() or 0
    if args.swap_ai:
        return menu.swap_ai() or 0
    if args.undo:
        ok, lines = gitflow.undo()
        _echo(lines)
        return 0 if ok else 1

    # First run: there is nothing to configure from, so ask once rather than
    # committing with defaults the user never saw. Non-interactively the menu
    # reads EOF as "quit", so CI is never blocked by this.
    if not settings.path.exists():
        menu.config_menu()
        settings = config.resolve()
        export_settings(settings)

    return _flow(settings, verbose=args.verbose)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help/--version (0), or a bad flag (1)
        return int(exc.code or 0)

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print(
            f"{YELLOW}⚠{RESET} "
            + t("interrupted", "interrupted (Ctrl+C) — aborting, no further steps run")
        )
        return runner.ABORT_RC


if __name__ == "__main__":
    raise SystemExit(main())
