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
from dataclasses import replace
from pathlib import Path

import pytest

from aicp import __version__, skills
from aicp.contracts import ROSTER, SKILL_VERSION_MARKER, SKILL_VERSION_SUFFIX


def cli(name: str):
    """The frozen ROSTER entry for *name* (never construct one locally —
    ``agy``'s ``config_dir`` is ``~/.gemini``, and only ROSTER knows that)."""
    return next(c for c in ROSTER if c.name == name)


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


@pytest.mark.parametrize("name", ["codex", "copilot", "agy", "vibe"])
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
    sidecar = h / (".codex/skills/commit/SKILL.md" + SKILL_VERSION_SUFFIX)
    sidecar.write_text("x-aicp-version: 0.0.1\n", encoding="utf-8")
    dir_sidecar = h / ".codex/skills/safe-git-push" / SKILL_VERSION_MARKER
    dir_sidecar.write_text("x-aicp-version: 0.0.1\n", encoding="utf-8")

    assert skills.detect(cli("codex"), "commit", home=h) == skills.OURS_OLDER
    assert skills.detect(cli("codex"), "safe-git-push", home=h) == skills.OURS_OLDER


def test_state_foreign_when_no_sidecar(configured):
    """No aicp sidecar → the user's own file. The important one."""
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
    # Now the later version bump: our sidecar goes stale, so a plain
    # (non-force) install upgrades — and wants to back up again.
    target.with_name(target.name + SKILL_VERSION_SUFFIX).write_text(
        "x-aicp-version: 0.0.1\n", encoding="utf-8"
    )
    target.write_text("aicp 0.0.1 body\n", encoding="utf-8")

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
    assert (target / SKILL_VERSION_MARKER).is_file()
    assert (target / "scripts/safe_push.py").is_file()


def test_ours_older_is_upgraded_without_force(configured):
    h = configured("codex")
    skills.install([cli("codex")], home=h)
    target = h / ".codex/skills/commit/SKILL.md"
    sidecar = target.with_name(target.name + SKILL_VERSION_SUFFIX)
    sidecar.write_text("x-aicp-version: 0.0.1\n", encoding="utf-8")
    target.write_text("stale body\n", encoding="utf-8")

    results = skills.install([cli("codex")], home=h)

    assert "Conventional Commits" in target.read_text(encoding="utf-8")
    assert sidecar.read_text(encoding="utf-8") == f"x-aicp-version: {__version__}\n"
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
