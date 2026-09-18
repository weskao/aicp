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

from . import (
    __version__,
    agentcfg,
    agents,
    config,
    gitflow,
    i18n,
    menu,
    notify,
    present,
    runner,
    skills,
)
from ._utils import BLUE, BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW, have
from .agents import executable
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

    ``lang`` is bridged separately because its consumer does not read the
    environment per call: :data:`aicp.i18n.LANGUAGE` is resolved once at
    import, which is strictly before this function can run, so exporting
    ``AICP_LANG`` alone would leave every run English no matter what the
    ``--config`` menu wrote. ``settings.lang`` is already validated against
    the same rule :func:`aicp.i18n._resolve_language` applies, and this is its
    only reader.

    Safe by construction with respect to :data:`aicp.config.DENYLIST`:
    ``settings.values`` is what :func:`aicp.config.load_config` accepted, and
    that loader drops every denied key — ``AICP_TG_SEND`` (reaches ``bash
    "$value"``), ``AICP_TIMING_LOG`` (path-mutating sinks) and ``AICP_CONFIG``
    (would re-point the next ``persist_key`` write) — before they ever reach
    here. So a file can never smuggle an exec-path or path-mutation knob into
    the environment through this door. Whoever adds the next such knob adds it
    to ``DENYLIST``, not to a filter here.

    Two things this deliberately does NOT close, both stated rather than left
    to be discovered:

    * ``AICP_SKIP_SECRET_SCAN=1`` in a ``.aicprc`` really does switch that
      run's secret scan off. That is parity with the zsh original (whose
      loader ``typeset -g``s the same key) and is what ``.aicprc.example``
      documents; it is accepted knowingly, not overlooked. Denying it would be
      a behaviour change worth making on purpose, not as a side effect here.
    * ``os.environ`` is inherited by children, where zsh's ``typeset -g`` was
      a shell global that was not exported. Every ``AICP_*`` key a file
      supplies is therefore visible to git and to the AI CLI subprocess. The
      blast radius is bounded by the loader's ``AICP_*`` key allowlist, so no
      unrelated variable can be set this way.
    """
    for key, value in settings.values.items():
        os.environ.setdefault(key, value)
    i18n.LANGUAGE = settings.lang


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
        if not have(executable(name)):
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


def _normalize_argv(argv: Sequence[str], parser: argparse.ArgumentParser) -> list[str]:
    """Let a long flag's ``--`` be omitted: ``aicp config`` == ``aicp --config``.

    The allowed bare words are read off *parser* itself (every ``--xxx`` it
    defines), so a flag added to :func:`build_parser` gains this for free with
    no second list to keep in sync. A word already starting with ``-`` is
    left alone — this only fills in a *missing* prefix, never reinterprets
    one that is already there (``--conf`` still isn't ``--config``: that's
    ``allow_abbrev=False``'s job, untouched by this).
    """
    long_flags = {
        opt[2:]
        for action in parser._actions
        for opt in action.option_strings
        if opt.startswith("--")
    }
    normalized: list[str] = []
    for i, arg in enumerate(argv):
        if arg in long_flags:
            normalized.append(f"--{arg}")
        elif arg.startswith("-"):
            normalized.append(arg)
        else:
            # The first bare word that names no flag begins the operands
            # (`aicp agents disable <name>`), and an agent that happens to be
            # called "json" must not be rewritten into --json.
            normalized.extend(argv[i:])
            break
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="aicp",
        # argparse's own furniture ("usage:", "options:", -h's text) stays in
        # English: it is argparse's, not aicp's, and gettext-patching a stdlib
        # module to translate four words is not worth the surprise.
        description=t("help_description", "AI commit + push, with a git-verified result summary."),
        epilog=t(
            "help_epilog",
            "every flag above also works without its leading `--` "
            "(`aicp config` is the same as `aicp --config`).",
        ),
        # No abbreviation matching: `--conf` must not silently become
        # `--config` (which runs the settings menu instead of a commit).
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"aicp {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help=t("help_verbose", "stream each CLI's output live instead of a spinner"),
    )
    parser.add_argument(
        "--undo", action="store_true",
        help=t(
            "help_undo",
            "undo the last commit (git reset --soft HEAD^); refuses once it is "
            "on the remote. Runs no AI CLI",
        ),
    )
    parser.add_argument(
        "--swap-ai", action="store_true",
        help=t("help_swap_ai", "move a CLI to #1 in the fallback chain and save it to config.json"),
    )
    parser.add_argument(
        "--config", action="store_true",
        help=t("help_config", "settings menu: the two steps, the language, the CLI order"),
    )
    parser.add_argument(
        "--doctor", action="store_true",
        help=t("help_doctor", "report where each vendored skill stands for each CLI"),
    )
    parser.add_argument(
        "--agents", nargs="*", metavar="ARG",
        help=t(
            "help_agents",
            "list the agent registry; or enable/disable/set/reset one in ~/.aicp/agents.json",
        ),
    )
    parser.add_argument("--json", action="store_true", help=t("help_json", "--doctor as JSON"))
    parser.add_argument(
        "--install-skills", action="store_true",
        help=t("help_install_skills", "install the vendored skills into every configured CLI"),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=t("help_yes", "--install-skills: actually write (without it, only a preview)"),
    )
    parser.add_argument(
        "--force", action="store_true",
        help=t("help_force", "--install-skills: replace a foreign file, keeping a .bak"),
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


def _agents_list() -> int:
    inventory = agents.inventory()
    exec_w = max((present.width(row.executable) for row in inventory), default=0)
    rows = [
        (
            row.name,
            f"{row.executable}{' ' * (exec_w - present.width(row.executable))}"
            f"  {DIM}·  {t(*agentcfg.STATE_LABELS[row.state])}{RESET}",
        )
        for row in inventory
    ]
    _echo(present.render(rows, t("agents_title", "AGENTS")))
    _dim(t("agents_hint", "  aicp --agents set <name> executable=… · disable <name> · reset <name>"))
    return 0


def _agents(argv: Sequence[str]) -> int:
    """``--agents`` and its verbs. Writes nothing on any error path."""
    verb, rest = (argv[0] if argv else "list"), list(argv[1:])
    if verb == "list":
        return _agents_list()
    if verb not in agentcfg.VERBS:
        print(
            f"{RED}"
            + t("agents_bad_verb", "✗ unknown agents verb: %s (use: %s)", verb, " ".join(agentcfg.VERBS))
            + RESET,
            file=sys.stderr,
        )
        return 1
    if not rest:
        print(
            f"{RED}" + t("agents_needs_name", "✗ %s needs an agent name", verb) + RESET,
            file=sys.stderr,
        )
        return 1
    try:
        change = agentcfg.apply(verb, rest[0], rest[1:])
    except agentcfg.AgentEditError as exc:
        print(f"{RED}✗ {exc}{RESET}", file=sys.stderr)
        return 1
    done = {
        "enabled": ("agents_done_enabled", "✓ %s enabled"),
        "disabled": ("agents_done_disabled", "✓ %s disabled"),
        "updated": ("agents_done_updated", "✓ %s updated"),
        "added": ("agents_done_added", "✓ %s added"),
        "reset": ("agents_done_reset", "✓ %s reset to its built-in definition"),
        "removed": ("agents_done_removed", "✓ %s removed"),
    }[change.verb]
    print(f"{GREEN}{t(*done, change.name)}{RESET}")
    if change.pruned:
        _dim(
            t(
                "agents_pruned",
                "  dropped from the saved CLI order: %s",
                " ".join(change.pruned),
            )
        )
    _dim(t("agents_next_run", "  the next aicp run uses it"))
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


def _run_panel(repo: str, top: str, branch: str, remote: str, chain: Sequence[str]) -> None:
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
    _dim(t("active_chain", "▸ chain: %s", " → ".join(chain)))
    print()


def _commit_step(
    settings: config.Settings, chain: Sequence[str], before: str, *, verbose: bool
) -> runner.StepResult | None:
    """Run /commit when needed and return its handler metadata."""
    if not settings.do_commit:
        _dim(
            t(
                "skip_commit_config",
                "▸ /commit is switched off in config (AICP_DO_COMMIT=0) — skipping",
            )
        )
        return None
    # Read AFTER the config gate, so a run with /commit switched off pays for
    # no status call at all.
    if not _git_out("status", "--porcelain"):
        _dim(t("skip_commit_clean", "▸ no uncommitted changes — skipping /commit"))
        return None

    _nudge(chain)
    result = runner.run_step("/commit", chain, notify=notify.notify, verbose=verbose)
    if result.rc != 0:
        return result

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
    return result


def _push_step(
    settings: config.Settings,
    chain: Sequence[str],
    remote: str,
    branch: str,
    *,
    verbose: bool,
) -> int | runner.StepResult | None:
    """Run /safe-git-push when needed; ``int`` means preflight failed."""
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
    return runner.run_step("/safe-git-push", chain, notify=notify.notify, verbose=verbose)


def _report(
    summary: gitflow.ResultSummary,
    repo: str,
    remote: str,
    branch: str,
    *,
    commit_handler: str | None,
    push_handler: str | None,
) -> int:
    """Print the git-verified RESULT table and decide the exit code."""
    if summary.fetch_note:
        print(summary.fetch_note)
    _echo(
        present.render(
            [
                *summary.rows(),
                (t("commit_handler", "Commit handler"), commit_handler or "—"),
                (t("push_handler", "Push handler"), push_handler or "—"),
            ],
            t("result_title", "RESULT"),
        )
    )

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
        # Same three labels as the RESULT table, so the same msgids: the
        # Telegram row and the terminal row must never read differently.
        + f'| {t("result_new_commits", "New commits")} | {summary.created} |\n'
        + f'| {t("result_ahead", "Ahead")} | {summary.ahead} |\n'
        + f'| {t("result_behind", "Behind")} | {summary.behind} |\n\n'
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
    excluded: set[str] = set()

    def active_chain() -> tuple[str, ...]:
        return tuple(cli for cli in chain if cli not in excluded)

    branch = gitflow.current_branch()
    if branch is None:
        _echo(gitflow.not_on_branch_lines())
        return 1
    remote = gitflow.remote_for(branch)
    top = _git_out("rev-parse", "--show-toplevel")
    repo = Path(top).name if top else ""
    before = _git_out("rev-parse", "HEAD")

    _run_panel(repo, top, branch, remote, chain)

    # precheck owns the order that matters (git status, then "is any CLI
    # installed", then the expensive secret scan) and refuses the run outright
    # on a secret hit — no AI CLI is invoked on that path.
    # ponytail: _commit_step re-reads `git status --porcelain` to decide
    # "dirty" — one extra, already-warm call rather than widening precheck's
    # frozen return shape. Revisit only if a repo is big enough to notice.
    with _spin(t("spin_precheck", "checking the working tree…"), verbose):
        proceed, lines = gitflow.precheck(chain, do_commit=settings.do_commit)
    _echo(lines)
    if not proceed:
        return 1

    commit_result = _commit_step(settings, active_chain(), before, verbose=verbose)
    if commit_result is not None:
        excluded.update(commit_result.quota_clis)
        if commit_result.rc != 0:
            return commit_result.rc
    print()

    push_result = _push_step(settings, active_chain(), remote, branch, verbose=verbose)
    if isinstance(push_result, int):
        return push_result
    if push_result is not None:
        excluded.update(push_result.quota_clis)
        if push_result.rc == runner.ABORT_RC:
            return push_result.rc
    print()

    # Trust git, not the CLI: read the real state back before reporting.
    after = _git_out("rev-parse", "HEAD")
    with _spin(t("spin_fetch", "git fetch %s/%s…", remote, branch), verbose):
        summary = gitflow.result_summary(before, after, remote, branch)
    return _report(
        summary,
        repo,
        remote,
        branch,
        commit_handler=commit_result.winner if commit_result is not None else None,
        push_handler=push_result.winner if isinstance(push_result, runner.StepResult) else None,
    )


# ── entry point ──────────────────────────────────────────────────────────────


#: Sub-flag -> the flag it qualifies. A sub-flag on its own is refused rather
#: than ignored: `aicp --json` silently running a full commit and push, because
#: the user meant `--doctor --json`, is the one misread worth three lines.
_SUB_FLAGS = {"json": "doctor", "yes": "install_skills", "force": "install_skills"}


def _reject_orphan_sub_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for sub, parent in _SUB_FLAGS.items():
        if getattr(args, sub) and not getattr(args, parent):
            parser.error(
                f"--{sub} only applies to --{parent.replace('_', '-')}"
            )


def _dispatch(args: argparse.Namespace) -> int:
    settings = config.resolve()
    export_settings(settings)

    if args.doctor:
        return _doctor(as_json=args.json)
    if args.agents is not None:
        return _agents(args.agents)
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
    raw = sys.argv[1:] if argv is None else argv
    try:
        args = parser.parse_args(_normalize_argv(raw, parser))
        _reject_orphan_sub_flags(parser, args)
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
