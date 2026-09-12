"""Branch resolution, ahead/behind, preflight, and the git-verified summary.

Ports ``~/scripts/test_aicp.sh``'s "_aicp_preflight", "a failed git fetch must
never read as a verified remote state", and "the shared 'not on a branch'
guard" sections. Every number asserted here comes from git, never from an AI
CLI's own output — that is the whole point of the RESULT table.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aicp import gitflow
from aicp.contracts import ROSTER


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def make_commit(repo: Path, name: str, message: str, body: str = "") -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", name)
    args = ["commit", "-q", "-m", message]
    if body:
        args += ["-m", body]
    git(repo, *args)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def only_git_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH holding git and nothing else — no AI CLI is installed."""
    git_bin = shutil.which("git")
    assert git_bin, "the suite needs git on PATH"
    monkeypatch.setenv("PATH", str(Path(git_bin).parent))


CHAIN = tuple(c.name for c in ROSTER)


# ── branch resolution / detached HEAD ────────────────────────────────────────


def test_current_branch(git_repo: Path) -> None:
    assert gitflow.current_branch(git_repo) == "main"


def test_detached_head_has_no_branch(git_repo: Path) -> None:
    head = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    git(git_repo, "checkout", "-q", "--detach", head)
    assert gitflow.current_branch(git_repo) is None


def test_not_a_git_repo_has_no_branch(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert gitflow.current_branch(plain) is None


def test_remote_for_branch_defaults_to_origin(git_repo: Path) -> None:
    assert gitflow.remote_for("main", git_repo) == "origin"


def test_remote_for_branch_reads_git_config(git_repo: Path) -> None:
    git(git_repo, "config", "branch.main.remote", "upstream")
    assert gitflow.remote_for("main", git_repo) == "upstream"


# ── diverged / ahead-behind: a failed fetch is never "in sync" ───────────────


def test_in_sync_repo_is_not_diverged(git_repo_synced: tuple[Path, Path]) -> None:
    repo, _ = git_repo_synced
    assert gitflow.diverged("origin", "main", repo) is False


def test_local_commit_makes_it_diverged(git_repo_synced: tuple[Path, Path]) -> None:
    repo, _ = git_repo_synced
    make_commit(repo, "a.txt", "feat: local only")
    assert gitflow.diverged("origin", "main", repo) is True


def test_a_failed_fetch_is_never_reported_as_in_sync(
    git_repo_synced: tuple[Path, Path],
) -> None:
    """Deleting the bare remote is the cheapest offline simulation: the
    remote-tracking ref survives locally and still answers 0/0, but about the
    past. A stale "already in sync" SKIPS the push entirely."""
    repo, bare = git_repo_synced
    shutil.rmtree(bare)

    state = gitflow.remote_state("origin", "main", repo)
    assert state.fetched is False
    assert state.in_sync is False, "a stale ref must never read as verified in sync"
    assert gitflow.diverged("origin", "main", repo) is True


def test_an_unresolvable_remote_ref_is_diverged(git_repo: Path) -> None:
    git(git_repo, "remote", "add", "origin", str(git_repo / "does-not-exist.git"))
    state = gitflow.remote_state("origin", "main", git_repo)
    assert state.resolved is False
    assert state.in_sync is False
    assert gitflow.diverged("origin", "main", git_repo) is True


def test_ahead_behind_counts_come_from_git(git_repo_synced: tuple[Path, Path]) -> None:
    repo, bare = git_repo_synced
    clone = repo.parent / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    make_commit(clone, "remote.txt", "feat: from elsewhere")
    git(clone, "push", "-q", "origin", "main")
    make_commit(repo, "local.txt", "feat: mine")

    state = gitflow.remote_state("origin", "main", repo)
    assert state.fetched is True
    assert state.resolved is True
    assert (state.ahead, state.behind) == (1, 1)


# ── preflight: fail fast, before the expensive scan ──────────────────────────


def test_preflight_fails_naming_every_roster_cli(only_git_on_path: None) -> None:
    ok, lines = gitflow.preflight(CHAIN)
    assert ok is False
    text = "\n".join(lines)
    assert "no AI CLI found on PATH" in text
    for cli in CHAIN:
        assert cli in text, f"{cli} is not named in the failure"
    assert "AICP_CLI_ORDER" in text


def test_preflight_passes_when_one_cli_is_installed(stub_cli) -> None:
    stub_cli(clis=("codex",))
    ok, lines = gitflow.preflight(CHAIN)
    assert ok is True
    assert lines == []


def test_precheck_on_a_dirty_tree_with_no_cli_skips_the_secret_scan(
    git_repo: Path, only_git_on_path: None
) -> None:
    """The scan is the expensive step: reading every changed and untracked
    file. With nothing that could run /commit, it must not run at all."""
    (git_repo / "wip.txt").write_text("wip\n", encoding="utf-8")

    ok, lines = gitflow.precheck(CHAIN, git_repo)
    assert ok is False
    text = "\n".join(lines)
    assert "no AI CLI found on PATH" in text
    assert "secret scan" not in text


def test_precheck_on_a_clean_tree_needs_no_cli_at_all(
    git_repo: Path, only_git_on_path: None
) -> None:
    """A clean, in-sync repo answers both of aicp's questions from git alone
    and has always succeeded with no AI CLI installed."""
    ok, lines = gitflow.precheck(CHAIN, git_repo)
    assert ok is True
    assert lines == []


def test_precheck_runs_the_scan_once_a_cli_exists(git_repo: Path, stub_cli) -> None:
    stub_cli()
    (git_repo / "new.env").write_text(
        "token=ghp_1234567890abcdefghijklmno\n", encoding="utf-8"
    )
    ok, lines = gitflow.precheck(CHAIN, git_repo)
    assert ok is False
    assert "new.env:1" in "\n".join(lines)


# ── NEW COMMITS panel data ───────────────────────────────────────────────────


def test_commit_rows_use_an_eight_char_hash_and_the_subject_only(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AICP_TZ", "Asia/Taipei")
    before = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    make_commit(git_repo, "a.txt", "feat: first", body="a body paragraph nobody wants")
    after = make_commit(git_repo, "b.txt", "fix: second")

    rows = gitflow.commit_rows(before, after, git_repo)
    assert len(rows) == 2
    assert all(len(h) == 8 for h, _ in rows)
    assert rows[1][0] == after[:8]
    assert rows[0][1].endswith("feat: first"), rows[0]
    assert rows[1][1].endswith("fix: second"), rows[1]
    assert "a body paragraph" not in rows[0][1], "the body must never be shown"
    assert rows[0][0] == git(git_repo, "rev-parse", "--short=8", "HEAD^").stdout.strip()


def test_commit_rows_are_oldest_first(git_repo: Path) -> None:
    before = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    make_commit(git_repo, "a.txt", "feat: older")
    after = make_commit(git_repo, "b.txt", "feat: newer")
    rows = gitflow.commit_rows(before, after, git_repo)
    assert rows[0][1].endswith("feat: older")


def test_commit_rows_honour_aicp_tz(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    after = make_commit(git_repo, "a.txt", "feat: timestamped")

    monkeypatch.setenv("AICP_TZ", "Asia/Taipei")
    taipei = gitflow.commit_rows(before, after, git_repo)[0][1]
    monkeypatch.setenv("AICP_TZ", "UTC")
    utc = gitflow.commit_rows(before, after, git_repo)[0][1]
    assert taipei != utc, "AICP_TZ must reach the git log timestamp"


def test_a_bogus_aicp_tz_falls_back_instead_of_reaching_a_tz_assignment(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AICP_TZ ends up in a real ``TZ=`` assignment, so its shape is pinned."""
    monkeypatch.setenv("AICP_TZ", "Asia/Taipei; rm -rf /")
    assert gitflow.resolve_tz() == "Asia/Taipei"


def test_commit_rows_are_empty_when_nothing_was_committed(git_repo: Path) -> None:
    head = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    assert gitflow.commit_rows(head, head, git_repo) == []


# ── the git-verified RESULT table ────────────────────────────────────────────


def test_result_summary_of_an_in_sync_repo(git_repo_synced: tuple[Path, Path]) -> None:
    repo, _ = git_repo_synced
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    summary = gitflow.result_summary(head, head, "origin", "main", repo)

    assert (summary.created, summary.ahead, summary.behind) == (0, 0, 0)
    assert summary.in_sync is True
    assert summary.verifiable is True
    assert summary.fetch_failed is False
    assert summary.repo == repo.name
    assert dict(summary.rows())["Status"] == "✅ in sync"


def test_result_summary_counts_new_commits_from_git(
    git_repo_synced: tuple[Path, Path],
) -> None:
    repo, _ = git_repo_synced
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    make_commit(repo, "a.txt", "feat: one")
    after = make_commit(repo, "b.txt", "feat: two")

    summary = gitflow.result_summary(before, after, "origin", "main", repo)
    assert summary.created == 2
    assert summary.ahead == 2
    assert summary.behind == 0
    assert summary.in_sync is False
    assert dict(summary.rows())["New commits"] == "2"
    assert dict(summary.rows())["Status"] == "⚠️  not pushed"


def test_result_summary_marks_an_unresolvable_remote_with_a_sentinel(
    git_repo: Path,
) -> None:
    git(git_repo, "remote", "add", "origin", str(git_repo.parent / "missing.git"))
    head = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    summary = gitflow.result_summary(head, head, "origin", "main", git_repo)

    assert summary.verifiable is False
    assert summary.ahead == -1
    assert summary.in_sync is False, "'cannot verify' is never 'verified in sync'"
    cells = dict(summary.rows())
    assert cells["Ahead"] == "?"
    assert cells["Behind"] == "?"
    assert "not found" in cells["Status"]


def test_result_summary_states_a_failed_fetch_out_loud(
    git_repo_synced: tuple[Path, Path],
) -> None:
    repo, bare = git_repo_synced
    shutil.rmtree(bare)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    summary = gitflow.result_summary(head, head, "origin", "main", repo)

    assert summary.fetch_failed is True
    assert "git fetch" in summary.fetch_note
    assert "failed" in summary.fetch_note


# ── module boundaries ────────────────────────────────────────────────────────


@pytest.mark.parametrize("module", ["gitflow.py", "secrets.py", "notify.py"])
def test_the_security_core_imports_nothing_from_the_upper_layers(module: str) -> None:
    """gitflow/secrets/notify must stay importable with zero knowledge of the
    runner, the config loader, the menu, or the skills installer."""
    source = (Path(gitflow.__file__).parent / module).read_text(encoding="utf-8")
    for forbidden in ("runner", "config", "menu", "skills"):
        assert f"import {forbidden}" not in source
        assert f"from .{forbidden}" not in source
        assert f"from aicp.{forbidden}" not in source


def test_git_calls_are_not_run_through_a_shell() -> None:
    for module in ("gitflow.py", "secrets.py", "notify.py"):
        source = (Path(gitflow.__file__).parent / module).read_text(encoding="utf-8")
        assert "shell=True" not in source, f"{module} builds a shell command line"
