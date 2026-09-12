"""``--undo``: the escape hatch, and the four refusals that make it safe.

Ports ``~/scripts/test_aicp.sh``'s "aicp --undo" section plus the stale-ref
half of its "a failed git fetch" section. Every refusal asserts the same three
things: nonzero result, HEAD did not move, and no AI CLI was invoked — undo
runs `git reset --soft HEAD^` and nothing else, ever.

"Cannot verify" is never treated as "verified safe": the guard exists to never
rewind a commit that might already be public, so an unprovable case is the
risky one, not the convenient one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aicp import gitflow


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def add_commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


@pytest.fixture
def no_cli_called(stub_cli, call_log: Path):
    """All five roster binaries stubbed; asserts none of them ever ran."""
    stub_cli()

    def assert_none() -> None:
        called = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
        assert called == "", f"--undo invoked an AI CLI: {called!r}"

    return assert_none


# ── the success path ─────────────────────────────────────────────────────────


def test_undo_of_an_unpushed_commit(
    git_repo_synced: tuple[Path, Path], no_cli_called
) -> None:
    repo, _ = git_repo_synced
    parent = head(repo)
    undone = add_commit(repo, "wip.txt", "feat: unpushed commit")

    ok, lines = gitflow.undo(repo)

    assert ok is True
    assert head(repo) == parent, "HEAD lands on its parent"
    assert (
        git(repo, "diff", "--cached", "--name-only").stdout.strip() == "wip.txt"
    ), "--soft leaves the change staged, not lost"
    text = "\n".join(lines)
    assert undone[:8] in text, "the undone commit's short hash is reported"
    assert "feat: unpushed commit" in text
    no_cli_called()


# ── the four refusals ────────────────────────────────────────────────────────


def test_undo_refuses_an_already_pushed_commit(
    git_repo_synced: tuple[Path, Path], no_cli_called
) -> None:
    repo, _ = git_repo_synced
    add_commit(repo, "done.txt", "feat: already pushed")
    git(repo, "push", "-q", "origin", "main")
    before = head(repo)

    ok, lines = gitflow.undo(repo)

    assert ok is False
    assert head(repo) == before
    assert "already on origin/main" in "\n".join(lines)
    no_cli_called()


def test_undo_refuses_when_there_is_no_parent_commit(
    git_repo: Path, no_cli_called
) -> None:
    before = head(git_repo)
    ok, lines = gitflow.undo(git_repo)

    assert ok is False
    assert head(git_repo) == before
    assert "no parent commit" in "\n".join(lines)
    no_cli_called()


def test_undo_refuses_when_the_remote_cannot_be_verified(
    git_repo: Path, no_cli_called
) -> None:
    git(git_repo, "remote", "add", "origin", str(git_repo.parent / "missing.git"))
    git(git_repo, "config", "branch.main.remote", "origin")
    before = add_commit(git_repo, "wip.txt", "feat: unverifiable")

    ok, lines = gitflow.undo(git_repo)

    assert ok is False
    assert head(git_repo) == before
    assert "cannot verify" in "\n".join(lines)
    no_cli_called()


def test_undo_refuses_when_the_fetch_failed_even_though_the_stale_ref_resolves(
    git_repo_synced: tuple[Path, Path], no_cli_called
) -> None:
    """The worst version of the stale-ref trap: the remote-tracking ref still
    resolves after a deleted remote, and would make a pushed commit look
    local — the exact case the guard exists for."""
    repo, bare = git_repo_synced
    before = add_commit(repo, "wip.txt", "feat: local only")
    shutil.rmtree(bare)

    ok, lines = gitflow.undo(repo)

    assert ok is False
    assert head(repo) == before
    assert "cannot verify" in "\n".join(lines)
    no_cli_called()


def test_undo_refuses_on_a_detached_head(
    git_repo_synced: tuple[Path, Path], no_cli_called
) -> None:
    repo, _ = git_repo_synced
    add_commit(repo, "wip.txt", "feat: unpushed")
    before = head(repo)
    git(repo, "checkout", "-q", "--detach", before)

    ok, lines = gitflow.undo(repo)

    assert ok is False
    assert head(repo) == before
    assert "not on a branch" in "\n".join(lines)
    no_cli_called()


def test_undo_ignores_an_uncommitted_file_and_still_calls_no_cli(
    git_repo_synced: tuple[Path, Path], no_cli_called
) -> None:
    """--undo never falls through into the commit/push flow: an untracked
    file in the tree must not drag the secret scan or /commit into it."""
    repo, _ = git_repo_synced
    parent = head(repo)
    add_commit(repo, "wip.txt", "feat: unpushed")
    (repo / "scratch.txt").write_text("untracked scratch\n", encoding="utf-8")

    ok, _ = gitflow.undo(repo)

    assert ok is True
    assert head(repo) == parent
    no_cli_called()
