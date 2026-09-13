"""Tests for :mod:`aicp.skills` — the cross-platform skills installer.

Every test runs against a FAKE ``$HOME`` (conftest's autouse
``pinned_environment`` fixture) and additionally passes ``home=`` explicitly
to every ``aicp.skills`` call, so nothing here can read or write the
developer's real ``~/.claude``/``~/.codex``/``~/.copilot``/``~/.gemini``/
``~/.vibe`` even if a fixture regressed. ``test_fake_home_is_isolated``
pins that guarantee itself.

Local fixtures only (see conftest.py's hard rule): this module never invokes
an AI CLI binary, so it needs none of the ``stub_cli`` machinery.
"""

from __future__ import annotations

import builtins
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from aicp import __version__, skills
from aicp.contracts import (
    ROSTER,
    SKILL_VERSION_MARKER,
    SKILL_VERSION_SUFFIX,
    STATE_DIR_NAME,
    STATE_FILE_NAME,
)


def cli(name: str):
    """The frozen ROSTER entry for *name* (never construct one locally —
    ``agy``'s ``config_dir`` is ``~/.gemini``, and only ROSTER knows that)."""
    return next(c for c in ROSTER if c.name == name)


def markers_under(root: Path) -> list[Path]:
    """Every legacy in-place marker under *root*.

    Spelled as an explicit suffix test rather than ``rglob("*.aicp-version")``
    so it catches the bare dotfile form (``.aicp-version``) on every platform
    without depending on how a glob treats a leading dot or an empty ``*``.
    """
    return sorted(p for p in root.rglob("*") if p.name.endswith(SKILL_VERSION_SUFFIX))


def age_record(target: Path, home: Path, version: str = "0.0.1") -> None:
    """Re-stamp this target's record as an OLDER aicp install, hashes intact.

    Hashes intact is the point: an aged record whose files still match is
    ``ours_older`` (upgrade it), while an edited file is ``foreign`` however
    old the record is. Tests that want the latter edit the file instead.
    """
    records = skills._load_state(home)
    records[str(target)]["version"] = version
    skills._save_state(records, home)


def write_legacy_sidecar(target: Path, *, is_dir: bool, version: str) -> Path:
    """Recreate what aicp <= 0.1.0 left next to an installed skill."""
    sidecar = (
        target / SKILL_VERSION_MARKER
        if is_dir
        else target.with_name(target.name + SKILL_VERSION_SUFFIX)
    )
    sidecar.write_text(f"x-aicp-version: {version}\n", encoding="utf-8")
    return sidecar


@pytest.fixture
def home(pinned_environment) -> Path:
    """The fake ``$HOME`` conftest already pointed ``HOME`` at."""
    return pinned_environment


@pytest.fixture
def configured(home):
    """Factory: create fake config dirs for the named CLIs, return the home.

    A CLI whose config dir was NOT created stays "not installed" — which is
    exactly what the skip-don't-create rule is about.
    """

    def _make(*names: str) -> Path:
        for name in names:
            (home / cli(name).config_dir.name).mkdir(parents=True, exist_ok=True)
        return home

    return _make


# ── isolation ────────────────────────────────────────────────────────────────


def test_fake_home_is_isolated(home, tmp_path):
    """Non-negotiable: the HOME these tests resolve is under tmp_path, and the
    real per-CLI config dirs are nowhere near it."""
    assert Path.home() == home
    assert tmp_path in home.parents
    assert not (home / ".claude").exists()


# ── the install-target table ─────────────────────────────────────────────────


def test_claude_gets_safe_git_push_but_no_commit(configured):
    """claude resolves ``/commit`` from its own ``commands/commit.md``;
    installing a competing ``skills/commit`` there would create two
    definitions of the same slash command."""
    h = configured("claude")
    skills.install([cli("claude")], home=h)

    assert (h / ".claude/skills/safe-git-push/SKILL.md").is_file()
    assert not (h / ".claude/skills/commit").exists()


@pytest.mark.parametrize("name", ["codex", "copilot", "agy", "vibe", "grok"])
def test_non_claude_clis_get_both_skills(configured, name):
    h = configured(name)
    root = h / cli(name).config_dir.name
    skills.install([cli(name)], home=h)

    assert (root / "skills/commit/SKILL.md").is_file()
    assert (root / "skills/safe-git-push/SKILL.md").is_file()
    assert (root / "skills/safe-git-push/scripts/safe_push.py").is_file()


def test_agy_installs_into_gemini_not_agy(configured):
    """The trap in contracts.CLI's docstring: the binary is ``agy``, the
    config dir is ``~/.gemini`` — ``~/.agy`` does not exist at all."""
    h = configured("agy")
    skills.install([cli("agy")], home=h)

    assert (h / ".gemini/skills/commit/SKILL.md").is_file()
    assert not (h / ".agy").exists()


def test_live_grok_home_overrides_the_default_root(home, monkeypatch, tmp_path):
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    grok = cli("grok")

    assert skills.config_root(grok) == grok_home
    assert skills.config_root(grok, home=home) == home / ".grok"
    assert [status.state for status in skills.full_status([grok])] == [
        skills.NOT_INSTALLED,
        skills.NOT_INSTALLED,
    ]


# ── per-CLI path rewriting ───────────────────────────────────────────────────


def test_copilot_install_has_no_claude_reference(configured):
    """The real-world bug this installer exists to fix: the user's existing
    sync tooling ships copilot's copy with unrewritten ``.claude/`` paths, so
    a project-local Copilot override is never found."""
    h = configured("copilot")
    skills.install([cli("copilot")], home=h)

    text = (h / ".copilot/skills/commit/SKILL.md").read_text(encoding="utf-8")
    assert ".claude" not in text
    assert "CLAUDE.md" not in text
    assert ".copilot/commands/commit.md" in text
    assert "AGENTS.md" in text


@pytest.mark.parametrize(
    ("name", "dir_ref", "memory"),
    [
        ("agy", ".gemini/commands/commit.md", "GEMINI.md"),
        ("vibe", ".vibe/commands/commit.md", "AGENTS.md"),
        ("codex", ".codex/commands/commit.md", "AGENTS.md"),
    ],
)
def test_memory_file_and_config_dir_rewritten_per_cli(configured, name, dir_ref, memory):
    h = configured(name)
    skills.install([cli(name)], home=h)

    text = (h / cli(name).config_dir.name / "skills/commit/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert dir_ref in text
    assert memory in text
    assert ".claude" not in text


def test_claude_install_keeps_its_own_paths(configured):
    """Rewriting is a no-op for claude itself — nothing to break."""
    h = configured("claude")
    skills.install([cli("claude")], home=h)
    source = (skills.VENDOR_DIR / "safe-git-push/SKILL.md").read_text(encoding="utf-8")
    assert (h / ".claude/skills/safe-git-push/SKILL.md").read_text(
        encoding="utf-8"
    ) == source


# ── state detection: all four states ─────────────────────────────────────────


def test_state_missing(configured):
    h = configured("codex")
    assert skills.detect(cli("codex"), "commit", home=h) == skills.MISSING


def test_state_current_after_install(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    assert skills.detect(cli("codex"), "commit", home=h) == skills.CURRENT
    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.CURRENT


def test_state_ours_older(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    age_record(h / ".codex/skills/commit/SKILL.md", h)
    age_record(h / ".codex/skills/safe-git-push", h)

    assert skills.detect(cli("codex"), "commit", home=h) == skills.OURS_OLDER
    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.OURS_OLDER


def test_state_foreign_when_nothing_was_recorded(configured):
    """No record and content we'd never write → the user's own file."""
    h = configured("codex")
    target = h / ".codex/skills/commit/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("my own commit skill\n", encoding="utf-8")

    assert skills.detect(cli("codex"), "commit", home=h) == skills.FOREIGN


def test_state_foreign_for_a_directory_skill(configured):
    h = configured("codex")
    target = h / ".codex/skills/safe-git-push"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("mine\n", encoding="utf-8")

    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.FOREIGN


# ── the hand-edit hole: a version-only marker cannot see content ─────────────


def test_a_hand_edited_file_is_foreign_not_ours(configured):
    """THE bug this bookkeeping exists to close.

    A version-only marker says "aicp wrote this", never "this is still what
    aicp wrote" — so an edit made after install used to read as ours and get
    displaced on the next version bump.
    """
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    target = h / ".codex/skills/commit/SKILL.md"
    target.write_text("I tuned this myself\n", encoding="utf-8")

    assert skills.detect(cli("codex"), "commit", home=h) == skills.FOREIGN


def test_an_edit_inside_a_directory_skill_is_detected(configured):
    """Not just the entry file — every file aicp wrote is covered."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    (h / ".codex/skills/safe-git-push/scripts/safe_push.py").write_text(
        "# my own push logic\n", encoding="utf-8"
    )

    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.FOREIGN


def test_a_hand_edited_file_survives_a_version_upgrade(configured):
    """The data-loss path end to end: edit, then a version bump arrives."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    target = h / ".codex/skills/commit/SKILL.md"
    target.write_text("I tuned this myself\n", encoding="utf-8")
    age_record(target, h)

    results = skills.install([cli("codex")], home=h)

    assert target.read_text(encoding="utf-8") == "I tuned this myself\n"
    assert not target.with_name(target.name + ".bak").exists()
    assert [r.action for r in results if r.skill == "commit"] == [skills.KEPT]


def test_a_file_the_user_added_is_not_a_modification(configured):
    """Only the files aicp wrote are covered: a stray .DS_Store or an extra
    script of the user's own must not freeze the skill at foreign."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    installed = h / ".codex/skills/safe-git-push"
    (installed / ".DS_Store").write_bytes(b"\x00junk")
    (installed / "scripts/mine.py").write_text("print('hi')\n", encoding="utf-8")

    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.CURRENT


# ── the state file itself ────────────────────────────────────────────────────


def test_install_writes_no_marker_inside_any_cli_config_dir(configured):
    """The whole point: aicp's bookkeeping leaves the CLI's own dirs alone."""
    h = configured("codex", "claude")
    skills.install(home=h)

    assert markers_under(h / ".codex") == []
    assert markers_under(h / ".claude") == []


def test_state_lives_under_home_in_one_file(configured):
    h = configured("codex", "claude")
    skills.install(home=h)

    state = h / STATE_DIR_NAME / STATE_FILE_NAME
    assert state.is_file()
    recorded = json.loads(state.read_text(encoding="utf-8"))["skills"]
    assert str(h / ".codex/skills/commit/SKILL.md") in recorded
    assert str(h / ".claude/skills/safe-git-push") in recorded


def test_state_records_relative_paths_with_forward_slashes(configured):
    """Portability: a Windows install must not record ``scripts\\safe_push.py``
    and then fail to match the same tree read on macOS or Linux."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)

    entry = skills._load_state(h)[str(h / ".codex/skills/safe-git-push")]
    assert "scripts/safe_push.py" in entry["files"]
    assert not any("\\" in key for key in entry["files"])


def test_a_single_file_skill_records_its_one_file_under_dot(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)

    entry = skills._load_state(h)[str(h / ".codex/skills/commit/SKILL.md")]
    assert list(entry["files"]) == ["."]


def test_a_corrupt_state_file_never_crashes_aicp(configured):
    """aicp must still run when its own state is garbage — the worst outcome
    allowed is "nothing recorded", which is safe (foreign is never touched)."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    state = h / STATE_DIR_NAME / STATE_FILE_NAME
    state.write_text("{not json at all", encoding="utf-8")

    assert skills.detect(cli("codex"), "commit", home=h) == skills.CURRENT
    assert skills.install([cli("codex")], home=h)


def test_an_unrecorded_install_whose_content_matches_is_ours(configured):
    """A fresh $HOME (new machine, wiped state) must not turn every installed
    skill foreign and refuse to ever update it again."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    (h / STATE_DIR_NAME / STATE_FILE_NAME).unlink()

    assert skills.detect(cli("codex"), "commit", home=h) == skills.CURRENT
    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.CURRENT


def test_state_is_not_written_into_a_cli_config_dir(configured):
    """Keyed on $HOME alone — never GROK_HOME or any per-CLI root."""
    h = configured("grok")
    skills.install([cli("grok")], home=h)

    assert not (h / ".grok" / STATE_DIR_NAME).exists()
    assert (h / STATE_DIR_NAME / STATE_FILE_NAME).is_file()


# ── state.json is untrusted input: a hostile or corrupted record ────────────


def test_an_absolute_path_record_key_cannot_escape_the_target(configured, tmp_path):
    """pathlib's own footgun: ``Path(target) / "/etc/passwd"`` discards
    *target* entirely and returns ``/etc/passwd`` — a record whose "files" key
    is an absolute path must not turn into an arbitrary-file read.
    """
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    secret = tmp_path / "outside-secret"
    secret.write_text("do not read me\n", encoding="utf-8")
    target = h / ".codex/skills/commit/SKILL.md"

    records = skills._load_state(h)
    records[str(target)]["files"] = {str(secret): skills._sha(secret.read_bytes())}
    skills._save_state(records, h)

    # A "match" here would mean the traversal succeeded and the attacker's
    # planted hash was compared against the outside file's real content.
    assert skills.detect(cli("codex"), "commit", home=h) == skills.FOREIGN


def test_a_dotdot_record_key_cannot_escape_the_target(configured, tmp_path):
    """A directory-skill target, not a single-file one: escaping through a
    FILE component (``commit/SKILL.md/../..``) fails on its own — the OS
    refuses to treat a regular file as a directory (ENOTDIR) regardless of
    what this code does. A real directory has no such accidental floor, so
    this is the case that actually exercises the guard.
    """
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    secret = tmp_path / "outside-secret"
    secret.write_text("do not read me\n", encoding="utf-8")
    target = h / ".codex/skills/safe-git-push"
    # A lexically correct "../.." key, computed the same way ``target / rel``
    # then reading it would resolve it — not a hand-counted guess that could
    # miss the real secret and pass for the wrong reason (a nonexistent path,
    # not a blocked one).
    rel = os.path.relpath(secret, start=target)

    records = skills._load_state(h)
    records[str(target)]["files"] = {rel: skills._sha(secret.read_bytes())}
    skills._save_state(records, h)

    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.FOREIGN


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_traversal_key_does_not_hang_on_a_fifo(configured):
    """A record key resolving to a FIFO must not block detect() forever.

    Run in a daemon thread with a bounded join: if the bug is present this
    would otherwise hang the whole test process rather than fail it.
    """
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    fifo = h / "blocking-fifo"
    os.mkfifo(fifo)
    target = h / ".codex/skills/commit/SKILL.md"

    records = skills._load_state(h)
    records[str(target)]["files"] = {str(fifo): "0" * 64}
    skills._save_state(records, h)

    result: dict[str, str] = {}
    worker = threading.Thread(
        target=lambda: result.__setitem__(
            "state", skills.detect(cli("codex"), "commit", home=h)
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=2)
    if worker.is_alive():
        with open(fifo, "wb"):  # unstick the hung open() so the thread can exit
            pass
        pytest.fail("detect() hung reading a FIFO reached via a traversal record key")
    assert result["state"] == skills.FOREIGN


# ── migrating off the legacy in-place sidecar ────────────────────────────────


def test_a_legacy_sidecar_is_read_as_ours(configured):
    """Before migration runs, a pre-0.2 install must still report correctly —
    not suddenly read as foreign and refuse to upgrade."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    (h / STATE_DIR_NAME / STATE_FILE_NAME).unlink()
    target = h / ".codex/skills/commit/SKILL.md"
    write_legacy_sidecar(target, is_dir=False, version="0.0.1")

    assert skills.detect(cli("codex"), "commit", home=h) == skills.OURS_OLDER


def test_installing_migrates_a_legacy_sidecar_and_deletes_it(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    (h / STATE_DIR_NAME / STATE_FILE_NAME).unlink()
    target = h / ".codex/skills/commit/SKILL.md"
    sidecar = write_legacy_sidecar(target, is_dir=False, version="0.0.1")
    dir_target = h / ".codex/skills/safe-git-push"
    dir_sidecar = write_legacy_sidecar(dir_target, is_dir=True, version="0.0.1")

    skills.install([cli("codex")], home=h)

    assert not sidecar.exists()
    assert not dir_sidecar.exists()
    assert markers_under(h / ".codex") == []
    assert skills._load_state(h)[str(target)]["version"] == __version__


def test_migration_keeps_a_current_version_sidecar_from_forcing_a_rewrite(
    configured,
):
    """A legacy install already at this version is UP_TO_DATE — the sidecar is
    still removed, because leaving it behind is the thing being fixed."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    (h / STATE_DIR_NAME / STATE_FILE_NAME).unlink()
    target = h / ".codex/skills/commit/SKILL.md"
    sidecar = write_legacy_sidecar(target, is_dir=False, version=__version__)
    before = target.stat().st_mtime_ns

    results = skills.install([cli("codex")], home=h)

    assert not sidecar.exists()
    assert target.stat().st_mtime_ns == before
    assert [r.action for r in results if r.skill == "commit"] == [skills.UP_TO_DATE]


def test_a_legacy_sidecar_does_not_bless_content_we_would_not_write(configured):
    """Found in the wild: another tool (the user's own cc2vibe sync) had
    rewritten an installed skill's frontmatter, and aicp's sidecar was still
    sitting on it claiming the current version. Migrating that sidecar as-is
    would launder someone else's file into "ours" and overwrite it on the next
    version bump. A sidecar claiming THIS version over content this version
    would not write means the file was changed after install — leave it.
    """
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    (h / STATE_DIR_NAME / STATE_FILE_NAME).unlink()
    target = h / ".codex/skills/commit/SKILL.md"
    target.write_text("---\nuser-invocable: true\n---\nsomeone else's\n", encoding="utf-8")
    write_legacy_sidecar(target, is_dir=False, version=__version__)

    assert skills.detect(cli("codex"), "commit", home=h) == skills.FOREIGN

    results = skills.install([cli("codex")], home=h)

    assert target.read_text(encoding="utf-8").endswith("someone else's\n")
    assert [r.action for r in results if r.skill == "commit"] == [skills.KEPT]


def test_a_legacy_sidecar_from_an_older_version_is_still_upgraded(configured):
    """The other half: content that differs because it IS old must still
    upgrade, or every pre-0.2 install would freeze at foreign forever."""
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    (h / STATE_DIR_NAME / STATE_FILE_NAME).unlink()
    target = h / ".codex/skills/commit/SKILL.md"
    target.write_text("aicp 0.0.1 body\n", encoding="utf-8")
    write_legacy_sidecar(target, is_dir=False, version="0.0.1")

    results = skills.install([cli("codex")], home=h)

    assert "Conventional Commits" in target.read_text(encoding="utf-8")
    assert target.with_name(target.name + ".bak").read_text(encoding="utf-8") == (
        "aicp 0.0.1 body\n"
    )
    assert [r.action for r in results if r.skill == "commit"] == [skills.UPGRADED]


def test_a_legacy_sidecar_next_to_a_foreign_file_is_still_foreign(configured):
    """No sidecar, no record, content we'd never write — untouched."""
    h = configured("codex")
    target = h / ".codex/skills/commit/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")

    results = skills.install([cli("codex")], home=h)

    assert target.read_text(encoding="utf-8") == "mine\n"
    assert [r.action for r in results if r.skill == "commit"] == [skills.KEPT]


# ── install safety ───────────────────────────────────────────────────────────


def test_foreign_file_survives_a_default_install(configured):
    h = configured("codex")
    target = h / ".codex/skills/commit/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("my own commit skill\n", encoding="utf-8")

    results = skills.install([cli("codex")], home=h)

    assert target.read_text(encoding="utf-8") == "my own commit skill\n"
    assert not target.with_name(target.name + ".bak").exists()
    kept = [r for r in results if r.skill == "commit"]
    assert [r.action for r in kept] == [skills.KEPT]


def test_yes_style_run_installs_missing_but_never_overwrites_foreign(configured):
    """A ``--yes``-style non-interactive run: no prompt, missing items get
    installed, the foreign one is still left alone."""
    h = configured("codex")
    target = h / ".codex/skills/commit/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")

    results = skills.install([cli("codex")], home=h, force=False)

    assert target.read_text(encoding="utf-8") == "mine\n"
    # ...while the sibling skill that WAS missing did get installed.
    assert (h / ".codex/skills/safe-git-push/SKILL.md").is_file()
    assert {r.skill: r.action for r in results} == {
        "commit": skills.KEPT,
        "safe-git-push": skills.INSTALLED,
    }


def test_force_overwrites_foreign_and_leaves_a_bak(configured):
    h = configured("codex")
    target = h / ".codex/skills/commit/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")

    results = skills.install([cli("codex")], home=h, force=True)

    backup = target.with_name(target.name + ".bak")
    assert backup.read_text(encoding="utf-8") == "mine\n"
    assert "Conventional Commits" in target.read_text(encoding="utf-8")
    assert [r.action for r in results if r.skill == "commit"] == [skills.OVERWRITTEN]
    assert backup in [r.backup for r in results if r.skill == "commit"]


def test_a_backup_never_overwrites_an_earlier_backup(configured):
    """The data-loss path: a forced install parks the user's own file at
    ``.bak``; a later upgrade must not move our own copy over it."""
    h = configured("codex")
    target = h / ".codex/skills/commit/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("the user's own skill\n", encoding="utf-8")

    skills.install([cli("codex")], home=h, force=True)
    # Now the later version bump: the record goes stale but still describes
    # what is on disk, so a plain (non-force) install upgrades — and wants to
    # back up again.
    target.write_text("aicp 0.0.1 body\n", encoding="utf-8")
    skills._record_install(target, skills._disk_hashes(skills.SKILLS["commit"], target), h)
    age_record(target, h)

    results = skills.install([cli("codex")], home=h)

    assert target.with_name(target.name + ".bak").read_text(encoding="utf-8") == (
        "the user's own skill\n"
    )
    assert target.with_name(target.name + ".bak.1").read_text(encoding="utf-8") == (
        "aicp 0.0.1 body\n"
    )
    assert [r.backup for r in results if r.skill == "commit"] == [
        target.with_name(target.name + ".bak.1")
    ]


def test_a_missing_vendored_source_raises_instead_of_faking_success(
    configured, monkeypatch
):
    """A directory skill whose source is gone would otherwise copy nothing,
    still write the sidecar, and then report CURRENT forever."""
    h = configured("codex")
    monkeypatch.setitem(
        skills.SKILLS,
        "safe-git-push",
        replace(skills.SKILLS["safe-git-push"], source=h / "nowhere"),
    )

    with pytest.raises(FileNotFoundError, match="vendored skill is missing"):
        skills.install([cli("codex")], home=h)

    assert not (h / ".codex/skills/safe-git-push").exists()


def test_junk_files_are_not_installed(configured, tmp_path):
    """A .DS_Store in the vendored tree must not be copied into every CLI."""
    vendored = tmp_path / "vendored-sgp"
    vendored.mkdir()
    (vendored / "SKILL.md").write_text("body\n", encoding="utf-8")
    (vendored / ".DS_Store").write_bytes(b"\x00junk")
    h = configured("codex")
    installed = h / ".codex/skills/safe-git-push"

    skills._copy(
        replace(skills.SKILLS["safe-git-push"], source=vendored),
        installed,
        skills.PLATFORMS[".codex"],
    )

    assert (installed / "SKILL.md").is_file()
    assert not (installed / ".DS_Store").exists()


def test_force_backs_up_files_inside_a_directory_skill(configured):
    h = configured("codex")
    target = h / ".codex/skills/safe-git-push"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("mine\n", encoding="utf-8")

    skills.install([cli("codex")], home=h, force=True)

    assert (target / "SKILL.md.bak").read_text(encoding="utf-8") == "mine\n"
    assert markers_under(target) == []
    assert (target / "scripts/safe_push.py").is_file()


def test_ours_older_is_upgraded_without_force(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    target = h / ".codex/skills/commit/SKILL.md"
    target.write_text("stale body\n", encoding="utf-8")
    skills._record_install(target, skills._disk_hashes(skills.SKILLS["commit"], target), h)
    age_record(target, h)

    results = skills.install([cli("codex")], home=h)

    assert "Conventional Commits" in target.read_text(encoding="utf-8")
    assert skills._load_state(h)[str(target)]["version"] == __version__
    assert target.with_name(target.name + ".bak").read_text(encoding="utf-8") == (
        "stale body\n"
    )
    assert [r.action for r in results if r.skill == "commit"] == [skills.UPGRADED]


def test_current_install_is_a_no_op(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    target = h / ".codex/skills/commit/SKILL.md"
    before = target.stat().st_mtime_ns

    results = skills.install([cli("codex")], home=h)

    assert target.stat().st_mtime_ns == before
    assert {r.action for r in results} == {skills.UP_TO_DATE}


def test_missing_config_dir_is_skipped_and_never_created(home):
    """A CLI whose config_dir does not exist is "not installed" — skip it."""
    results = skills.install(ROSTER, home=home)

    assert [p for p in home.iterdir()] == []
    assert {r.action for r in results} == {skills.NOT_INSTALLED}


def test_install_defaults_to_the_whole_roster(configured):
    h = configured("vibe", "claude")
    results = skills.install(home=h)

    installed = {(r.cli, r.skill) for r in results if r.action == skills.INSTALLED}
    assert installed == {
        ("vibe", "commit"),
        ("vibe", "safe-git-push"),
        ("claude", "safe-git-push"),
    }


# ── hot-path probe ───────────────────────────────────────────────────────────


def test_probe_reports_missing_skills(configured):
    h = configured("codex")
    assert skills.missing_skills(cli("codex"), home=h) == ("commit", "safe-git-push")
    skills.install([cli("codex")], home=h)
    assert skills.missing_skills(cli("codex"), home=h) == ()


def test_probe_is_empty_for_an_unconfigured_cli(home):
    """Nothing to nudge about when the CLI itself isn't installed."""
    assert skills.missing_skills(cli("codex"), home=home) == ()


def test_probe_ignores_foreign_content(configured):
    """Cheap by design: existence only, no sidecar read — a foreign file at
    the target counts as present for the nudge."""
    h = configured("codex")
    target = h / ".codex/skills/commit/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")
    assert skills.missing_skills(cli("codex"), home=h) == ("safe-git-push",)


def test_probe_performs_no_file_reads_and_few_stats(configured, monkeypatch):
    """AC: the hot-path probe reads no files. Verified by making every read
    path raise, and by counting the stats it makes.

    ``Path.stat``, not ``os.stat``: on 3.10 ``pathlib`` binds ``os.stat`` into
    its ``_NormalAccessor`` at import time, so patching the ``os`` attribute is
    never observed and the count is a silent 0 (``_Accessor`` was removed in
    3.11, which is why only 3.10 saw it). ``Path.stat`` is what the probe
    actually reaches — ``is_dir``/``exists`` both route through it — on every
    supported version, so this counts behavior rather than a pathlib internal.
    """
    h = configured("codex")
    skills.install([cli("codex")], home=h)

    def _no_reads(*args, **kwargs):
        raise AssertionError("hot-path probe opened/read a file")

    stats: list[object] = []
    real_stat = Path.stat

    def _counting_stat(self, *args, **kwargs):
        stats.append(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _no_reads)
    monkeypatch.setattr(Path, "read_bytes", _no_reads)
    monkeypatch.setattr(Path, "open", _no_reads)
    monkeypatch.setattr(builtins, "open", _no_reads)
    monkeypatch.setattr(Path, "stat", _counting_stat)

    assert skills.missing_skills(cli("codex"), home=h) == ()

    # One stat per (config dir + skill target): a handful, not a walk.
    assert 1 <= len(stats) <= 8


# ── status / JSON ────────────────────────────────────────────────────────────


def test_full_status_reports_every_state(configured):
    h = configured("codex", "claude")
    skills.install([cli("codex")], home=h)
    foreign = h / ".claude/skills/safe-git-push"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("mine\n", encoding="utf-8")

    by_key = {
        (s.cli, s.skill): s
        for s in skills.full_status([cli("codex"), cli("claude"), cli("vibe")], home=h)
    }

    assert by_key[("codex", "commit")].state == skills.CURRENT
    assert by_key[("codex", "commit")].version == __version__
    assert by_key[("claude", "safe-git-push")].state == skills.FOREIGN
    assert by_key[("claude", "safe-git-push")].version is None
    assert ("claude", "commit") not in by_key  # claude has no commit skill
    assert by_key[("vibe", "commit")].state == skills.NOT_INSTALLED


def test_status_json_round_trips(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)

    payload = skills.status_json(home=h)
    reloaded = json.loads(json.dumps(payload))

    assert reloaded["aicp_version"] == __version__
    entry = next(
        e
        for e in reloaded["skills"]
        if e["cli"] == "codex" and e["skill"] == "commit"
    )
    assert entry["state"] == skills.CURRENT
    # Compared as a Path, not a string: Windows renders the separators as "\".
    assert Path(entry["target"]) == h / ".codex/skills/commit/SKILL.md"
    assert entry["version"] == __version__


def test_platform_table_covers_the_whole_roster():
    """Every ROSTER entry is keyed by its config_dir, never its binary name."""
    assert {c.config_dir.name for c in ROSTER} == set(skills.PLATFORMS)
    assert skills.PLATFORMS[".claude"].skills == ("safe-git-push",)
    assert "commit" in skills.PLATFORMS[".gemini"].skills
    assert "commit" in skills.PLATFORMS[".grok"].skills
