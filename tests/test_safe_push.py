"""Functional check of the vendored safe-git-push script against a real bare remote."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills/safe-git-push/scripts/safe_push.py"
)


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)


def safe_push(repo):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def clones(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))

    def clone(name):
        repo = tmp_path / name
        git(tmp_path, "clone", "-q", str(remote), str(repo))
        git(repo, "config", "user.email", "user@example.com")
        git(repo, "config", "user.name", "Test User")
        return repo

    a = clone("a")
    git(a, "checkout", "-q", "-b", "main")
    commit(a, "base")
    git(a, "push", "-q", "-u", "origin", "main")
    return a, clone("b"), remote


def test_ahead_pushes_without_fetching(clones):
    a, _, remote = clones
    commit(a, "one")
    r = safe_push(a)
    assert r.returncode == 0, r.stderr
    assert "Safe push completed." in r.stdout
    assert git(remote, "rev-parse", "main") == git(a, "rev-parse", "HEAD")


def test_in_sync_reports_nothing_to_push(clones):
    a, _, _ = clones
    r = safe_push(a)
    assert r.returncode == 0, r.stderr
    assert "Already synchronized" in r.stdout


def test_behind_fast_forwards_after_rejection(clones):
    a, b, remote = clones
    commit(b, "theirs")
    git(b, "push", "-q", "origin", "main")
    r = safe_push(a)
    assert r.returncode == 0, r.stderr
    assert "Fast-forwarded" in r.stdout
    assert git(a, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")


def test_diverged_rebases_then_pushes(clones):
    a, b, remote = clones
    commit(b, "theirs")
    git(b, "push", "-q", "origin", "main")
    commit(a, "mine")
    r = safe_push(a)
    assert r.returncode == 0, r.stderr
    assert "Push rejected" in r.stderr and "Safe push completed." in r.stdout
    assert git(remote, "rev-parse", "main") == git(a, "rev-parse", "HEAD")
    assert git(a, "log", "--format=%s", "-3") == "mine\ntheirs\nbase"


def test_rebase_conflict_exits_2_and_restores_branch(clones):
    a, b, _ = clones
    commit(b, "same")
    git(b, "push", "-q", "origin", "main")
    (a / "same").write_text("mine", encoding="utf-8")
    git(a, "add", "same")
    git(a, "commit", "-q", "-m", "mine")
    head = git(a, "rev-parse", "HEAD")
    r = safe_push(a)
    assert r.returncode == 2, r.stderr
    assert "rebase onto origin/main hit conflicts" in r.stderr and "same" in r.stderr
    assert git(a, "rev-parse", "HEAD") == head
    assert git(a, "status", "--porcelain") == ""

def test_first_push_sets_upstream(clones):
    a, _, remote = clones
    git(a, "checkout", "-q", "-b", "feature")
    commit(a, "feat")
    r = safe_push(a)
    assert r.returncode == 0, r.stderr
    assert "(first push)" in r.stdout
    assert git(a, "config", "branch.feature.remote") == "origin"
    assert git(remote, "rev-parse", "feature") == git(a, "rev-parse", "HEAD")
