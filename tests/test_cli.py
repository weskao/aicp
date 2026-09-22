"""Black-box tests for the entry point — ``aicp.cli``.

Ports the remaining shell cases from ``~/scripts/test_aicp.sh`` that only the
wired-up entry point can exercise: its flag parser (unknown flag, ``--undo``),
the branch/repo guards, the two config gates around ``/commit`` and
``/safe-git-push``, and "a clean in-sync repo answers from git alone".

Everything here drives :func:`aicp.cli.main` exactly as the console script
does — a real argv list, a real cwd, real git, and the five stub CLIs from
``conftest.py`` on PATH — so nothing asserts against an internal it could
mock into agreeing with itself.

Fixtures live HERE, locally: ``tests/conftest.py`` is a four-way contention
point and is never edited by a single task (see its docstring).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import rmtree

from aicp import budget, cli, config, gitflow, i18n, runner
from aicp import notify as notify_mod

# ── local helpers ────────────────────────────────────────────────────────────

#: Structurally valid, deliberately meaningless — the same synthetic lookalike
#: ``test_aicp.sh`` and ``test_secrets.py`` use. Never a real credential.
SECRET_SAMPLE = "AKIAABCDEFGHIJKLMNOP"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def calls(call_log: Path) -> list[str]:
    """Every stub-CLI invocation recorded so far (empty when none ran)."""
    return call_log.read_text().splitlines() if call_log.exists() else []


def prompts(call_log: Path) -> list[str]:
    """Just the slash-command prompt of each invocation, in order.

    The stub records ``<cli>\\t<arg>\\t<arg>…``; every aicp prompt is a slash
    command and no flag starts with ``/``, so picking those fields out works
    for both the ``-p <prompt>`` CLIs and codex's positional form.
    """
    return [
        field
        for line in calls(call_log)
        for field in line.split("\t")
        if field.startswith("/")
    ]


# ── local fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def restore_process_globals():
    """Undo what the D3 bridge exports.

    :func:`aicp.cli.export_settings` writes straight into ``os.environ`` and
    reassigns :data:`aicp.i18n.LANGUAGE` — that IS the bridge, and in a real
    run the process exits moments later. In a test session both would outlive
    the test and reconfigure every module that reads them afterwards
    (``test_config.py`` is the one that notices the environment first), so this
    module puts them back itself; ``monkeypatch`` only undoes changes
    ``monkeypatch`` made, and it made neither of these.
    """
    before = dict(os.environ)
    language = i18n.LANGUAGE
    yield
    os.environ.clear()
    os.environ.update(before)
    i18n.LANGUAGE = language


@pytest.fixture
def aicprc(pinned_environment, monkeypatch) -> Path:
    """An existing (empty) ``.aicprc``, so the first-run menu never fires.

    Real Telegram sends are already blocked for every test by
    ``pinned_environment`` clearing ``TG_BOT_TOKEN``/``TG_CHAT_ID``.
    """
    path = config.config_path()
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def run(monkeypatch):
    """Call :func:`aicp.cli.main` from inside *repo*, as the console script does."""

    def _run(repo: Path, *argv: str) -> int:
        monkeypatch.chdir(repo)
        return cli.main(list(argv))

    return _run


@pytest.fixture
def repo_ahead(git_repo_synced) -> Path:
    """Synced repo + one unpushed commit + one uncommitted file.

    Both steps have something to do, which is what makes the two config gates
    (``AICP_DO_COMMIT`` / ``AICP_DO_PUSH``) observable independently.
    """
    repo, _bare = git_repo_synced
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "feat: unpushed")
    (repo / "b.txt").write_text("still editing\n", encoding="utf-8")
    return repo


@pytest.fixture
def repo_behind(git_repo_synced, tmp_path) -> Path:
    """Clean repo whose remote has moved ahead — nothing to commit, but the
    rebase/pull case ``/safe-git-push`` exists to handle."""
    repo, bare = git_repo_synced
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "c.txt").write_text("c\n", encoding="utf-8")
    git(other, "add", "c.txt")
    git(other, "commit", "-q", "-m", "feat: remote work")
    git(other, "push", "-q")
    return repo


# ── flag parsing ─────────────────────────────────────────────────────────────


def test_an_unknown_flag_exits_nonzero_and_invokes_no_cli(
    run, aicprc, git_repo, stub_cli, call_log, capsys
):
    stub_cli()
    assert run(git_repo, "--not-a-flag") != 0
    assert calls(call_log) == []
    assert "--not-a-flag" in capsys.readouterr().err


def test_help_exits_zero_and_runs_nothing(run, aicprc, git_repo, stub_cli, call_log, capsys):
    stub_cli()
    assert run(git_repo, "--help") == 0
    assert calls(call_log) == []
    assert "--undo" in capsys.readouterr().out


def test_version_exits_zero(run, aicprc, git_repo, capsys):
    assert run(git_repo, "--version") == 0
    assert "aicp" in capsys.readouterr().out


def test_flags_are_not_abbreviation_matched(run, aicprc, git_repo, stub_cli, call_log):
    """``--conf`` must not silently become ``--config``."""
    stub_cli()
    assert run(git_repo, "--conf") != 0
    assert calls(call_log) == []


def test_a_bare_flag_name_works_the_same_as_its_dashed_form(
    run, aicprc, git_repo, stub_cli, call_log, monkeypatch
):
    """``aicp config`` == ``aicp --config``."""
    stub_cli()
    opened: list[int] = []
    monkeypatch.setattr(cli.menu, "config_menu", lambda **_kw: opened.append(1) or 0)
    assert run(git_repo, "config") == 0
    assert len(opened) == 1
    assert calls(call_log) == []


def test_a_bare_word_that_is_not_a_known_flag_still_fails(
    run, aicprc, git_repo, stub_cli, call_log
):
    stub_cli()
    assert run(git_repo, "not-a-flag") != 0
    assert calls(call_log) == []


def test_help_mentions_the_optional_dashes(run, aicprc, git_repo, stub_cli, call_log, capsys):
    stub_cli()
    assert run(git_repo, "--help") == 0
    assert "aicp config" in capsys.readouterr().out


# ── repo / branch guards ─────────────────────────────────────────────────────


def test_outside_a_git_repo_refuses(run, aicprc, tmp_path, stub_cli, call_log):
    stub_cli()
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert run(outside) == 1
    assert calls(call_log) == []


def test_detached_head_is_refused_by_the_main_flow(
    run, aicprc, git_repo, stub_cli, call_log
):
    stub_cli()
    git(git_repo, "checkout", "-q", "--detach")
    assert run(git_repo) == 1
    assert calls(call_log) == []


def test_detached_head_is_refused_by_undo(run, aicprc, git_repo, stub_cli, call_log):
    stub_cli()
    head = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    git(git_repo, "checkout", "-q", "--detach")
    assert run(git_repo, "--undo") == 1
    assert git(git_repo, "rev-parse", "HEAD").stdout.strip() == head
    assert calls(call_log) == []


def test_undo_runs_no_ai_cli_on_the_happy_path(
    run, aicprc, repo_ahead, stub_cli, call_log
):
    stub_cli()
    parent = git(repo_ahead, "rev-parse", "HEAD^").stdout.strip()
    assert run(repo_ahead, "--undo") == 0
    assert git(repo_ahead, "rev-parse", "HEAD").stdout.strip() == parent
    assert calls(call_log) == []


# ── the two config gates ─────────────────────────────────────────────────────


def test_do_commit_off_skips_commit_but_still_pushes(
    run, aicprc, repo_ahead, stub_cli, call_log, monkeypatch
):
    stub_cli()
    monkeypatch.setenv("AICP_DO_COMMIT", "0")
    # The stub pushes nothing, so the run legitimately ends ahead of the
    # remote: exit 1, not 0. Asserting it is what pins _report's verdict.
    assert run(repo_ahead) == 1
    assert "/commit" not in prompts(call_log)
    assert "/safe-git-push" in prompts(call_log)


def test_do_push_off_still_commits(
    run, aicprc, repo_ahead, stub_cli, call_log, monkeypatch
):
    stub_cli()
    monkeypatch.setenv("AICP_DO_PUSH", "0")
    assert run(repo_ahead) == 1
    assert "/commit" in prompts(call_log)
    assert "/safe-git-push" not in prompts(call_log)


def test_a_clean_in_sync_repo_skips_both_steps_and_exits_zero(
    run, aicprc, git_repo_synced, stub_cli, call_log
):
    stub_cli()
    repo, _bare = git_repo_synced
    assert run(repo) == 0
    assert calls(call_log) == []


def test_a_branch_behind_the_remote_still_runs_safe_git_push(
    run, aicprc, repo_behind, stub_cli, call_log
):
    stub_cli()
    assert run(repo_behind) == 1  # the stub rebased nothing — still behind
    assert "/commit" not in prompts(call_log)
    assert "/safe-git-push" in prompts(call_log)


def test_flow_prints_the_configured_chain_before_the_first_attempt(
    run, aicprc, repo_ahead, stub_cli, capsys
):
    stub_cli(clis=("copilot",))

    run(repo_ahead)

    out = capsys.readouterr().out
    assert "chain: copilot → agy → codex → claude → vibe → grok" in out
    assert out.index("chain:") < out.index("▸ /commit")
    assert out.count("copilot") >= 3  # chain plus the two successful step handlers


def test_result_presents_each_step_handler_or_a_dash_when_skipped(
    run, aicprc, git_repo_synced, stub_cli, capsys
):
    stub_cli()
    repo, _bare = git_repo_synced

    assert run(repo) == 0

    out = capsys.readouterr().out
    assert "Commit handler" in out
    assert "Push handler" in out
    assert out.count("—") >= 2


def test_result_presents_the_winning_handler_for_each_attempt(
    run, aicprc, repo_ahead, monkeypatch, stub_cli, capsys
):
    stub_cli()

    def fake_run_step(prompt, *_args, **_kwargs):
        return runner.StepResult(0, winner="writer" if prompt == "/commit" else "pusher")

    monkeypatch.setattr(cli.runner, "run_step", fake_run_step)
    run(repo_ahead)

    out = capsys.readouterr().out
    assert "Commit handler" in out and "writer" in out
    assert "Push handler" in out and "pusher" in out
    assert out.count("Commit handler") == 1


# ── the git-verified RESULT table ────────────────────────────────────────────


def test_an_unresolvable_remote_is_never_reported_as_in_sync(
    run, aicprc, git_repo_synced, stub_cli, capsys
):
    """A remote that cannot be resolved must fail, not read as "0 ahead, 0
    behind" — the false positive the whole verification exists to catch."""
    repo, bare = git_repo_synced
    stub_cli()
    rmtree(bare)
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert run(repo) == 1
    out = capsys.readouterr().out
    assert "cannot verify the push" in out
    assert "in sync" not in out


def test_the_result_table_counts_commits_git_made_not_what_a_cli_claimed(
    run, aicprc, git_repo_synced, stub_cli, capsys, tmp_path
):
    """The stub prints a triumphant "pushed 7 commits!" and creates none; the
    table must report what git actually has."""
    repo, _bare = git_repo_synced
    bin_dir = stub_cli(clis=("copilot",))
    (bin_dir / "copilot").write_text(
        "#!/bin/sh\necho 'Done! Created 7 commits and pushed them.'\nexit 0\n",
        encoding="utf-8",
    )
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    run(repo)
    out = capsys.readouterr().out
    assert "New commits" in out
    assert "7" not in out.split("New commits")[1].splitlines()[0]


def test_a_clean_in_sync_repo_needs_no_ai_cli_installed(run, aicprc, git_repo_synced):
    """No ``stub_cli`` at all: preflight must not be a blanket gate at the top."""
    repo, _bare = git_repo_synced
    assert run(repo) == 0


def test_no_ai_cli_installed_with_pending_work_fails_before_the_scan(
    run, aicprc, git_repo, only_git_on_path, capsys
):
    # git stays reachable, no AI CLI of any name does — the state a machine
    # with none of the roster installed is actually in. (only_git_on_path
    # uses a throwaway bin dir; dirname(which("git")) is not isolation on
    # Homebrew, where codex/copilot sit next to git.)
    (git_repo / "pending.txt").write_text("work\n", encoding="utf-8")
    assert run(git_repo) == 1
    assert "no AI CLI found on PATH" in capsys.readouterr().out


# ── the secret gate ──────────────────────────────────────────────────────────


def test_a_secret_hit_exits_nonzero_and_invokes_no_ai_cli(
    run, aicprc, git_repo, stub_cli, call_log, capsys
):
    stub_cli()
    (git_repo / "leak.env").write_text(f"AWS_KEY={SECRET_SAMPLE}\n", encoding="utf-8")
    assert run(git_repo) == 1
    assert calls(call_log) == []
    out = capsys.readouterr().out
    assert "leak.env" in out
    assert SECRET_SAMPLE not in out


# ── first run ────────────────────────────────────────────────────────────────


def test_first_run_without_an_aicprc_opens_the_menu_once(
    run, git_repo_synced, stub_cli, monkeypatch
):
    stub_cli()
    repo, _bare = git_repo_synced
    opened: list[int] = []
    monkeypatch.setattr(cli.menu, "config_menu", lambda **_kw: opened.append(1))
    assert not config.config_path().exists()
    assert run(repo) == 0
    assert len(opened) == 1


def test_an_existing_aicprc_does_not_open_the_menu(
    run, aicprc, git_repo_synced, stub_cli, monkeypatch
):
    stub_cli()
    repo, _bare = git_repo_synced
    opened: list[int] = []
    monkeypatch.setattr(cli.menu, "config_menu", lambda **_kw: opened.append(1))
    assert run(repo) == 0
    assert opened == []


def test_config_flag_opens_the_menu_and_runs_no_ai_cli(
    run, aicprc, git_repo, stub_cli, call_log, monkeypatch
):
    stub_cli()
    opened: list[int] = []
    monkeypatch.setattr(cli.menu, "config_menu", lambda **_kw: opened.append(1) or 0)
    assert run(git_repo, "--config") == 0
    assert len(opened) == 1
    assert calls(call_log) == []


def test_swap_ai_runs_no_ai_cli(run, aicprc, git_repo, stub_cli, call_log, monkeypatch):
    stub_cli()
    swapped: list[int] = []
    monkeypatch.setattr(cli.menu, "swap_ai", lambda **_kw: swapped.append(1) or 0)
    assert run(git_repo, "--swap-ai") == 0
    assert len(swapped) == 1
    assert calls(call_log) == []


# ── the passive skills nudge ─────────────────────────────────────────────────


def test_a_missing_skill_nudges_at_config_without_blocking(
    run, aicprc, repo_ahead, stub_cli, call_log, capsys, pinned_environment
):
    stub_cli()
    (pinned_environment / ".copilot").mkdir()  # configured, but no skills installed
    run(repo_ahead)
    out = capsys.readouterr().out
    assert "aicp --config" in out
    assert "/commit" in prompts(call_log)  # non-blocking: the run still happened


def test_no_nudge_when_the_cli_is_not_configured(
    run, aicprc, repo_ahead, stub_cli, capsys
):
    stub_cli()  # no ~/.copilot at all
    run(repo_ahead)
    assert "aicp --config" not in capsys.readouterr().out


# ── the config -> consumer bridge (defect D3) ────────────────────────────────


def test_an_aicprc_timeout_reaches_budget(aicprc, git_repo):
    aicprc.write_text(json.dumps({"AICP_TIMEOUT_BASE": "999"}), encoding="utf-8")
    assert budget.compute("copilot", cwd=git_repo).seconds == budget.FLOOR
    cli.export_settings(config.resolve())
    assert budget.compute("copilot", cwd=git_repo).seconds == 999


def test_an_aicprc_timezone_reaches_gitflow(aicprc):
    aicprc.write_text(json.dumps({"AICP_TZ": "Asia/Tokyo"}), encoding="utf-8")
    assert gitflow.resolve_tz() == gitflow.DEFAULT_TZ
    cli.export_settings(config.resolve())
    assert gitflow.resolve_tz() == "Asia/Tokyo"


def test_a_real_environment_variable_still_beats_the_file(aicprc, git_repo, monkeypatch):
    aicprc.write_text(
        json.dumps({"AICP_TZ": "Asia/Tokyo", "AICP_TIMEOUT_BASE": "999"}), encoding="utf-8"
    )
    monkeypatch.setenv("AICP_TZ", "Europe/Paris")
    monkeypatch.setenv("AICP_TIMEOUT_BASE", "42")
    cli.export_settings(config.resolve())
    assert gitflow.resolve_tz() == "Europe/Paris"
    assert budget.compute("copilot", cwd=git_repo).seconds == 42


def test_the_bridge_never_imports_a_denylisted_key_from_the_file(aicprc, monkeypatch):
    """``AICP_TIMING_LOG`` reaches ``mkdir -p``/``>>``/a rename — file-supplied
    values of it must not arrive in the environment by the back door."""
    monkeypatch.delenv("AICP_TIMING_LOG", raising=False)
    aicprc.write_text(
        json.dumps({"AICP_TIMING_LOG": "/tmp/evil.log"}),
        encoding="utf-8",
    )
    cli.export_settings(config.resolve())
    assert "AICP_TIMING_LOG" not in os.environ


def test_an_aicprc_cannot_rename_the_file_the_next_write_lands_on(
    tmp_path, monkeypatch, pinned_environment, capsys
):
    """Regression: with ``AICP_CONFIG`` bridged into the environment, the NEXT
    ``config_path()`` would resolve to the file's chosen path and the next
    ``persist_key`` — an ordinary ``--config`` menu write — would mkdir and
    atomically replace a file the user never named.

    No ``AICP_CONFIG`` in the environment here on purpose: the file has to be
    found at its default location, so the only possible source for the key is
    the file itself.
    """
    monkeypatch.delenv("AICP_CONFIG", raising=False)
    real = pinned_environment / ".aicp" / "config.json"
    real.parent.mkdir(parents=True)
    hijacked = tmp_path / "hijacked" / "authorized_keys"
    real.write_text(json.dumps({"AICP_CONFIG": str(hijacked)}), encoding="utf-8")
    assert config.config_path() == real  # the file really is the one being read

    cli.export_settings(config.resolve())

    assert "ignoring AICP_CONFIG" in capsys.readouterr().err
    assert "AICP_CONFIG" not in os.environ
    assert config.config_path() == real
    assert gitflow.config_path() == real
    config.persist_key("AICP_DO_PUSH", "0", config.config_path())
    assert not hijacked.exists()
    assert not hijacked.parent.exists()


def test_an_aicprc_language_actually_reaches_the_translator(aicprc):
    """Regression: ``i18n.LANGUAGE`` is resolved at import — strictly before
    the bridge can run — so exporting ``AICP_LANG`` alone left every run
    English no matter what the ``--config`` menu had written."""
    aicprc.write_text(json.dumps({"AICP_LANG": "zh-TW"}), encoding="utf-8")
    assert i18n.LANGUAGE == "en"
    cli.export_settings(config.resolve())
    assert i18n.LANGUAGE == "zh-TW"


def test_a_real_language_environment_variable_still_beats_the_file(
    aicprc, monkeypatch
):
    aicprc.write_text(json.dumps({"AICP_LANG": "zh-TW"}), encoding="utf-8")
    monkeypatch.setenv("AICP_LANG", "en")
    cli.export_settings(config.resolve())
    assert i18n.LANGUAGE == "en"


def test_a_real_run_applies_the_bridge(run, aicprc, git_repo_synced, stub_cli):
    stub_cli()
    repo, _bare = git_repo_synced
    aicprc.write_text(json.dumps({"AICP_TZ": "Asia/Tokyo"}), encoding="utf-8")
    assert run(repo) == 0
    assert gitflow.resolve_tz() == "Asia/Tokyo"


# ── wiring contracts ─────────────────────────────────────────────────────────


def test_cli_wires_notify_into_run_step(run, aicprc, repo_ahead, monkeypatch, stub_cli):
    stub_cli()
    seen: dict[str, object] = {}

    def fake_run_step(prompt, chain, *, cwd=None, notify=None, verbose=False, stream=None):
        seen[prompt] = notify
        return runner.StepResult(0, winner=chain[0])

    monkeypatch.setattr(cli.runner, "run_step", fake_run_step)
    run(repo_ahead)
    assert seen["/commit"] is notify_mod.notify
    assert seen["/safe-git-push"] is notify_mod.notify


def test_runner_imports_no_sibling_feature_module():
    """contracts.py's rule: the runner takes the chain and the notifier as
    parameters, so it stays testable with no config machinery and no network."""
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # `from . import x` / `from .x import y`
                module = f"aicp.{module}" if module else "aicp"
            imported.add(module)
            if module == "aicp":
                imported.update(f"aicp.{alias.name}" for alias in node.names)
    forbidden = {
        "aicp.cli",
        "aicp.config",
        "aicp.gitflow",
        "aicp.menu",
        "aicp.notify",
        "aicp.secrets",
        "aicp.skills",
    }
    assert not forbidden & imported


def test_verbose_is_passed_through_to_the_runner(
    run, aicprc, repo_ahead, monkeypatch, stub_cli
):
    stub_cli()
    seen: list[bool] = []

    def fake_run_step(prompt, chain, *, cwd=None, notify=None, verbose=False, stream=None):
        seen.append(verbose)
        return runner.StepResult(0, winner=chain[0])

    monkeypatch.setattr(cli.runner, "run_step", fake_run_step)
    run(repo_ahead, "-v")
    assert seen and all(seen)


def test_an_interrupt_exits_130(run, aicprc, repo_ahead, monkeypatch, stub_cli):
    stub_cli()

    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.runner, "run_step", boom)
    assert run(repo_ahead) == 130


def test_an_aborted_step_stops_the_run(run, aicprc, repo_ahead, monkeypatch, stub_cli):
    stub_cli()
    seen: list[str] = []

    def fake_run_step(prompt, chain, **_kwargs):
        seen.append(prompt)
        return runner.StepResult(runner.ABORT_RC)

    monkeypatch.setattr(cli.runner, "run_step", fake_run_step)
    assert run(repo_ahead) == runner.ABORT_RC
    assert seen == ["/commit"]


def test_quota_exclusion_spans_commit_and_push_but_resets_on_the_next_flow(
    run, aicprc, repo_ahead, monkeypatch, stub_cli
):
    stub_cli()
    seen: list[tuple[str, tuple[str, ...]]] = []

    def fake_run_step(prompt, chain, **_kwargs):
        active = tuple(chain)
        seen.append((prompt, active))
        if prompt == "/commit":
            return runner.StepResult(0, winner="agy", quota_clis=("copilot",))
        return runner.StepResult(0, winner=active[0])

    monkeypatch.setattr(cli.runner, "run_step", fake_run_step)

    assert run(repo_ahead) == 1
    assert run(repo_ahead) == 1
    assert [entry for entry in seen if entry[0] == "/commit"] == [
        ("/commit", config.resolve().cli_chain),
        ("/commit", config.resolve().cli_chain),
    ]
    push_chains = [chain for prompt, chain in seen if prompt == "/safe-git-push"]
    assert len(push_chains) == 2
    assert all("copilot" not in chain for chain in push_chains)


# ── the CI escape hatches ────────────────────────────────────────────────────


def test_doctor_json_is_parseable(run, aicprc, git_repo, capsys):
    assert run(git_repo, "--doctor", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skills"]


def test_doctor_runs_no_ai_cli(run, aicprc, git_repo, stub_cli, call_log):
    stub_cli()
    assert run(git_repo, "--doctor") == 0
    assert calls(call_log) == []


def test_install_skills_previews_without_yes(
    run, aicprc, git_repo, pinned_environment, capsys
):
    (pinned_environment / ".copilot").mkdir()
    assert run(git_repo, "--install-skills") == 0
    target = pinned_environment / ".copilot" / "skills" / "commit" / "SKILL.md"
    assert not target.exists()
    assert "--yes" in capsys.readouterr().out


def test_install_skills_with_yes_installs(run, aicprc, git_repo, pinned_environment):
    (pinned_environment / ".copilot").mkdir()
    assert run(git_repo, "--install-skills", "--yes") == 0
    assert (pinned_environment / ".copilot" / "skills" / "commit" / "SKILL.md").is_file()


def test_install_skills_keeps_a_foreign_file_unless_forced(
    run, aicprc, git_repo, pinned_environment
):
    """A file with no aicp sidecar is the user's own — never overwritten
    silently, and backed up even when forced."""
    target = pinned_environment / ".copilot" / "skills" / "commit" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("my own commit skill\n", encoding="utf-8")

    assert run(git_repo, "--install-skills", "--yes") == 0
    assert target.read_text(encoding="utf-8") == "my own commit skill\n"

    assert run(git_repo, "--install-skills", "--yes", "--force") == 0
    assert target.read_text(encoding="utf-8") != "my own commit skill\n"
    assert (target.parent / "SKILL.md.bak").read_text(
        encoding="utf-8"
    ) == "my own commit skill\n"


@pytest.mark.parametrize(
    ("orphan", "parent"),
    [("--json", "--doctor"), ("--yes", "--install-skills"), ("--force", "--install-skills")],
)
def test_a_sub_flag_without_its_parent_is_refused_not_ignored(
    run, aicprc, repo_ahead, stub_cli, call_log, capsys, orphan, parent
):
    """``aicp --json`` must not silently run a full commit and push."""
    stub_cli()
    assert run(repo_ahead, orphan) != 0
    assert calls(call_log) == []
    assert parent in capsys.readouterr().err
