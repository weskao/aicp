"""The secret scanner — the one gate that must hold before any AI CLI runs.

Ports ``~/scripts/test_aicp.sh``'s secret-scan sections (its "secret scan",
"untracked binary files", and "must not fork a subshell per scanned line"
blocks) to pytest. Every sample below is a synthetic, structurally-valid
lookalike — the same strings the shell suite uses — never a real credential.

Fixtures live here, locally: ``tests/conftest.py`` is a four-way contention
point and is never edited by a single task.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aicp.secrets import PATTERNS, report_lines, scan

# ── local helpers ────────────────────────────────────────────────────────────

# Synthetic lookalikes: structurally valid for each pattern, deliberately
# meaningless. Mirrors test_aicp.sh's aws_secret/gh_secret constants.
SAMPLES: tuple[tuple[str, str], ...] = (
    ("openai", "sk-abcdefghijklmnopqrstuvwxyz0123"),
    ("openai-proj", "sk-proj-abcdefghijklmnopqrstuvwxyz0123"),
    ("github-pat", "ghp_1234567890abcdefghijklmno"),
    ("github-server", "ghs_1234567890abcdefghijklmno"),
    ("github-fine-grained", "github_pat_1234567890abcdefghijklmno"),
    ("aws-akia", "AKIAABCDEFGHIJKLMNOP"),
    ("aws-asia", "ASIAABCDEFGHIJKLMNOP"),
    ("bearer", "Bearer abcdefghijklmnopqrstuvwxyz0123"),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----"),
)

# The entropy heuristic was deliberately rejected upstream (see the comment
# above AICP_SECRET_PATTERNS in bin/aicp): it was the main false-positive
# source, and this repo's own strings would trip it.
BENIGN: tuple[str, ...] = (
    "sk-automation",
    "Bearer short",
    "AKIAZ demo",
    "ghp_short",
    "a perfectly ordinary configuration line",
)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def commit(repo: Path, message: str = "change") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def all_output(result) -> str:
    return "\n".join(report_lines(result))


@pytest.fixture
def repo(git_repo: Path) -> Path:
    """The base throwaway repo, re-exported under a shorter name."""
    return git_repo


# ── the 7 patterns, and the false positives they must not chase ──────────────


def test_seven_patterns_are_declared() -> None:
    """Exactly the 7 fixed prefix/format checks the zsh original ships."""
    assert len(PATTERNS) == 7


@pytest.mark.parametrize("label,sample", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_each_pattern_is_flagged(repo: Path, label: str, sample: str) -> None:
    (repo / "new.env").write_text(f"value={sample}\n", encoding="utf-8")
    result = scan(repo)
    assert result.hits, f"{label} was not flagged"
    assert not result.ok


@pytest.mark.parametrize("line", BENIGN)
def test_benign_lookalikes_are_not_flagged(repo: Path, line: str) -> None:
    (repo / "notes.txt").write_text(f"{line}\n", encoding="utf-8")
    result = scan(repo)
    assert result.hits == ()
    assert result.ok


# ── scan surface ─────────────────────────────────────────────────────────────


def test_tracked_modification_is_scanned(repo: Path) -> None:
    (repo / "config.txt").write_text("existing config\n", encoding="utf-8")
    commit(repo, "add config")
    with (repo / "config.txt").open("a", encoding="utf-8") as fh:
        fh.write(f"aws_key={SAMPLES[5][1]}\n")

    result = scan(repo)
    assert any("config.txt:2" in h for h in result.hits), result.hits


def test_untracked_file_is_scanned(repo: Path) -> None:
    (repo / "new-file.env").write_text(f"token={SAMPLES[2][1]}\n", encoding="utf-8")
    assert any("new-file.env:1" in h for h in scan(repo).hits)


def test_secret_in_a_brand_new_subdirectory_is_scanned(repo: Path) -> None:
    """`--untracked-files=all`: the default mode collapses a new directory
    into one `?? dir/` entry and would let everything inside through."""
    (repo / "svc" / "conf").mkdir(parents=True)
    (repo / "svc" / "conf" / "prod.env").write_text(
        f"token={SAMPLES[2][1]}\n", encoding="utf-8"
    )
    hits = scan(repo).hits
    assert any("svc/conf/prod.env:1" in h for h in hits), hits


def test_running_from_a_subdirectory_behaves_the_same(repo: Path) -> None:
    """git reports status paths relative to the TOPLEVEL, never the cwd —
    resolving them against the cwd silently skipped every untracked file."""
    (repo / "svc").mkdir()
    (repo / "svc" / "prod.env").write_text(f"token={SAMPLES[2][1]}\n", encoding="utf-8")

    from_root = scan(repo)
    from_sub = scan(repo / "svc")
    assert from_root.hits == from_sub.hits
    assert any("svc/prod.env:1" in h for h in from_sub.hits)


def test_added_line_starting_with_plus_plus_is_scanned(repo: Path) -> None:
    """An added CONTENT line beginning with `++` prints as `+++ ...`, which
    is indistinguishable from a real file header unless the parser tracks
    git's own `diff --git` segment start."""
    (repo / "notes.md").write_text("intro\n", encoding="utf-8")
    commit(repo, "add notes")
    with (repo / "notes.md").open("a", encoding="utf-8") as fh:
        fh.write(f"++ token={SAMPLES[2][1]}\n")

    hits = scan(repo).hits
    assert any("notes.md:2" in h for h in hits), hits


def test_secret_on_line_5001_is_caught_without_forking_per_line(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Functional: the hit survives 5000 misses. Structural: the scan does
    not spawn a process per line (the zsh original's measured 3.7s bug)."""
    big = repo / "big.conf"
    big.write_text(
        "".join(f"ordinary config line {i}\n" for i in range(1, 5001))
        + f"token={SAMPLES[2][1]}\n",
        encoding="utf-8",
    )

    import aicp.secrets as secrets_module

    calls: list[object] = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(secrets_module.subprocess, "run", counting_run)
    result = scan(repo)

    assert any("big.conf:5001" in h for h in result.hits), result.hits
    assert len(calls) <= 8, f"one subprocess per scanned line is the old bug: {len(calls)}"


# ── never print the secret itself ────────────────────────────────────────────


def test_the_secret_value_never_appears_in_the_report(repo: Path) -> None:
    secret = SAMPLES[5][1]
    (repo / "new.env").write_text(f"aws_key={secret}\n", encoding="utf-8")
    result = scan(repo)

    assert result.hits
    text = all_output(result)
    assert secret not in text
    assert all(secret not in h for h in result.hits)
    assert "new.env:1" in text
    assert "AWS access key ID" in text


# ── binary / unreadable content: warn, never silently skip, never abort ──────


def test_untracked_binary_is_warned_about_not_silently_skipped(repo: Path) -> None:
    (repo / "new.env").write_bytes(f"aws_key={SAMPLES[5][1]}\0\n".encode())
    result = scan(repo)

    assert result.hits == (), "a NUL file cannot be read, so nothing is confirmed"
    assert result.ok, "a binary file warns, it never aborts the run"
    assert "new.env" in result.unscanned
    text = all_output(result)
    assert "not scanned" in text
    assert SAMPLES[5][1] not in text


def test_tracked_binary_change_is_warned_about(repo: Path) -> None:
    (repo / "blob.dat").write_text("plain\n", encoding="utf-8")
    commit(repo, "add blob")
    (repo / "blob.dat").write_bytes(f"aws_key={SAMPLES[5][1]}\0\n".encode())

    result = scan(repo)
    assert result.ok
    assert "blob.dat" in result.unscanned
    assert SAMPLES[5][1] not in all_output(result)


def test_gitignored_binary_produces_no_warning(repo: Path) -> None:
    """Otherwise every repo with a build directory prints a wall of yellow."""
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "artifact.bin").write_bytes(b"blob\0data\n")

    result = scan(repo)
    assert result.unscanned == ()
    assert "not scanned" not in all_output(result)


def test_untouched_tracked_binary_produces_no_warning(repo: Path) -> None:
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0binary")
    commit(repo, "add asset")
    (repo / "readme.txt").write_text("an unrelated ordinary change\n", encoding="utf-8")

    result = scan(repo)
    assert result.unscanned == ()
    assert result.ok


# ── .gitattributes -diff: git prints no "+" lines at all for these ───────────


@pytest.mark.parametrize(
    "name",
    ["secrets.env", "prod and staging.env", "機密.env", "it's private.env"],
    ids=["plain", "with-and", "non-ascii", "quote-char"],
)
def test_diff_marked_files_are_still_scanned(repo: Path, name: str) -> None:
    """A `-diff` rule makes git print one prose line instead of a diff, so a
    plain-text file holding a real secret vanished from the scan entirely."""
    (repo / ".gitattributes").write_text(f'"{name}" -diff\n', encoding="utf-8")
    (repo / name).write_text("harmless=1\n", encoding="utf-8")
    commit(repo, "add marked file")
    with (repo / name).open("a", encoding="utf-8") as fh:
        fh.write(f"token={SAMPLES[2][1]}\n")

    result = scan(repo)
    assert result.hits, f"{name} was never scanned"
    assert any(f"{name}:2" in h for h in result.hits), result.hits
    assert SAMPLES[2][1] not in all_output(result)


# ── the documented bypass ────────────────────────────────────────────────────


def test_skip_env_bypasses_the_scan_and_says_so(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AICP_SKIP_SECRET_SCAN", "1")
    (repo / "new.env").write_text(f"token={SAMPLES[2][1]}\n", encoding="utf-8")

    result = scan(repo)
    assert result.skipped
    assert result.ok
    assert result.hits == ()
    assert "AICP_SKIP_SECRET_SCAN=1" in all_output(result)


def test_any_other_skip_value_does_not_bypass(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AICP_SKIP_SECRET_SCAN", "0")
    (repo / "new.env").write_text(f"token={SAMPLES[2][1]}\n", encoding="utf-8")
    assert scan(repo).hits


def test_a_clean_tree_scans_clean(repo: Path) -> None:
    result = scan(repo)
    assert result.ok
    assert result.hits == ()
    assert result.unscanned == ()
    assert report_lines(result) == []
