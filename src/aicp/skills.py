"""Cross-platform installer for aicp's vendored skills.

aicp sends the AI CLI the literal prompts ``/commit`` and ``/safe-git-push``.
Those only mean anything if the skill exists in *that CLI's own* config
directory — otherwise the CLI receives a slash command it doesn't understand
and does something unintended. So this module installs the byte-identical
vendored copies under ``skills/`` into each configured CLI.

Three rules make it safe:

* **Targets are keyed on** :attr:`aicp.contracts.CLI.config_dir`, never on the
  binary name — ``agy`` reads ``~/.gemini``, and ``~/.agy`` does not exist.
  :data:`PLATFORMS` is that table, keyed by the config dir's own name.
* **A file with no aicp sidecar is the user's** (:data:`FOREIGN`). It is never
  overwritten without an explicit ``force=True``; a ``--yes``-style
  non-interactive run installs only what's missing. See
  :data:`aicp.contracts.SKILL_VERSION_SUFFIX` for the sidecar rules.
* **A CLI whose config dir doesn't exist is not installed.** Skip it; never
  create the directory.

All public functions take ``home=`` and otherwise resolve :func:`Path.home`
at call time (i.e. ``$HOME`` as it is *now*), so a test with a fake ``$HOME``
can never reach the developer's real config dirs. The per-CLI directory name
still comes from ``config_dir.name`` — from the contract, not from the binary
name.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .contracts import CLI, ROSTER, SKILL_VERSION_MARKER, SKILL_VERSION_SUFFIX

__all__ = [
    "CURRENT",
    "FOREIGN",
    "INSTALLED",
    "KEPT",
    "MISSING",
    "NOT_INSTALLED",
    "OURS_OLDER",
    "OVERWRITTEN",
    "PLATFORMS",
    "SKILLS",
    "UPGRADED",
    "UP_TO_DATE",
    "InstallResult",
    "Platform",
    "Skill",
    "SkillStatus",
    "detect",
    "full_status",
    "install",
    "missing_skills",
    "status_json",
]

# ── states (detect) and actions (install) ────────────────────────────────────

MISSING = "missing"  # nothing at the target
CURRENT = "current"  # our sidecar, version matches (or newer)
OURS_OLDER = "ours_older"  # our sidecar, older version
FOREIGN = "foreign"  # no sidecar -> the user's own file
NOT_INSTALLED = "not_installed"  # the CLI's config dir doesn't exist

INSTALLED = "installed"
UPGRADED = "upgraded"
UP_TO_DATE = "up_to_date"
KEPT = "kept_foreign"  # foreign file left alone (no force)
OVERWRITTEN = "overwritten"  # foreign file replaced, backup left behind

BACKUP_SUFFIX = ".bak"


# ── the vendored skills ──────────────────────────────────────────────────────


def _vendor_dir() -> Path:
    here = Path(__file__).resolve().parent
    # Installed layout first (skills/ shipped inside the package), then the
    # source checkout (repo-root skills/, next to src/).
    for candidate in (here / "skills", here.parent.parent / "skills"):
        if candidate.is_dir():
            return candidate
    return here.parent.parent / "skills"


#: Root of the byte-identical vendored copies.
VENDOR_DIR = _vendor_dir()


@dataclass(frozen=True)
class Skill:
    """One vendored skill and where it lands inside a CLI's config dir."""

    name: str
    source: Path
    #: Destination relative to the CLI's config dir.
    rel_target: Path
    is_dir: bool


SKILLS: dict[str, Skill] = {
    "commit": Skill(
        name="commit",
        source=VENDOR_DIR / "commit.md",
        rel_target=Path("skills/commit/SKILL.md"),
        is_dir=False,
    ),
    "safe-git-push": Skill(
        name="safe-git-push",
        source=VENDOR_DIR / "safe-git-push",
        rel_target=Path("skills/safe-git-push"),
        is_dir=True,
    ),
}


# ── the install-target table ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Platform:
    """Per-CLI install data, keyed by ``CLI.config_dir.name``.

    ``memory_file`` and ``dir_name`` drive the path rewriting: the vendored
    text talks about ``.claude/`` and ``CLAUDE.md``, which mean nothing to any
    other CLI (the bug this fixes: copilot's existing synced copy still points
    at ``.claude/commands/commit.md``, so a project-local override is never
    found).

    ``skills`` is *data*, not a branch: claude is absent from ``commit``'s
    install list because it already resolves ``/commit`` from
    ``commands/commit.md`` — a second definition under ``skills/`` would
    compete with it.
    """

    dir_name: str
    memory_file: str
    skills: tuple[str, ...]


_BOTH = ("commit", "safe-git-push")

PLATFORMS: dict[str, Platform] = {
    ".claude": Platform(".claude", "CLAUDE.md", ("safe-git-push",)),
    ".codex": Platform(".codex", "AGENTS.md", _BOTH),
    ".copilot": Platform(".copilot", "AGENTS.md", _BOTH),
    ".gemini": Platform(".gemini", "GEMINI.md", _BOTH),  # the agy binary
    ".vibe": Platform(".vibe", "AGENTS.md", _BOTH),
}


# ── results ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillStatus:
    cli: str
    skill: str
    target: Path
    state: str
    version: str | None


@dataclass(frozen=True)
class InstallResult:
    cli: str
    skill: str
    target: Path
    state: str
    action: str
    backup: Path | None = None


# ── paths ────────────────────────────────────────────────────────────────────


def _platform(cli: CLI) -> Platform:
    return PLATFORMS[cli.config_dir.name]


def config_root(cli: CLI, home: Path | None = None) -> Path:
    """This CLI's config dir, re-based on *home* (or the live ``$HOME``)."""
    return (Path(home) if home is not None else Path.home()) / cli.config_dir.name


def target_path(cli: CLI, skill: str, home: Path | None = None) -> Path:
    return config_root(cli, home) / SKILLS[skill].rel_target


def _sidecar(skill: Skill, target: Path) -> Path:
    """Sidecar per contracts.py: inside a directory skill, next to a file."""
    if skill.is_dir:
        return target / SKILL_VERSION_MARKER
    return target.with_name(target.name + SKILL_VERSION_SUFFIX)


# ── detection ────────────────────────────────────────────────────────────────


def _read_version(sidecar: Path) -> str | None:
    try:
        text = sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "x-aicp-version" and value.strip():
            return value.strip()
    return None


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))


def detect(cli: CLI, skill: str, home: Path | None = None) -> str:
    """State of *skill* for *cli*: one of the module's five state constants.

    Reads the sidecar — this is the menu/doctor path, not the hot path (see
    :func:`missing_skills` for that).
    """
    root = config_root(cli, home)
    if not root.is_dir():
        return NOT_INSTALLED
    spec = SKILLS[skill]
    target = root / spec.rel_target
    if not target.exists():
        return MISSING
    version = _read_version(_sidecar(spec, target))
    if version is None:
        return FOREIGN
    return OURS_OLDER if _as_tuple(version) < _as_tuple(__version__) else CURRENT


def missing_skills(cli: CLI, home: Path | None = None) -> tuple[str, ...]:
    """Hot-path probe: which of this CLI's skills are simply not there.

    Existence only — one ``stat`` for the config dir plus one per skill
    target, no reads, no sidecar parsing, no checksums. A foreign file at the
    target counts as present. An unconfigured CLI yields ``()``: nothing to
    nudge about when the CLI itself isn't installed.
    """
    root = config_root(cli, home)
    if not root.is_dir():
        return ()
    return tuple(
        name
        for name in _platform(cli).skills
        if not (root / SKILLS[name].rel_target).exists()
    )


def full_status(
    clis: Iterable[CLI] = ROSTER, home: Path | None = None
) -> list[SkillStatus]:
    """Slow, complete status (reads sidecars) for the menu / doctor view."""
    out: list[SkillStatus] = []
    for cli in clis:
        for name in _platform(cli).skills:
            target = target_path(cli, name, home)
            state = detect(cli, name, home)
            version = (
                _read_version(_sidecar(SKILLS[name], target))
                if state in (CURRENT, OURS_OLDER)
                else None
            )
            out.append(SkillStatus(cli.name, name, target, state, version))
    return out


def status_json(
    clis: Iterable[CLI] = ROSTER, home: Path | None = None
) -> dict[str, object]:
    """:func:`full_status` as a plain JSON-serializable dict (``--doctor --json``)."""
    return {
        "aicp_version": __version__,
        "skills": [
            {
                "cli": s.cli,
                "skill": s.skill,
                "target": str(s.target),
                "state": s.state,
                "version": s.version,
            }
            for s in full_status(clis, home)
        ],
    }


# ── writing ──────────────────────────────────────────────────────────────────


def _rewrite(data: bytes, platform: Platform) -> bytes:
    """Point the vendored text at *this* CLI's config dir and memory file."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data  # not text; copy verbatim
    return (
        text.replace(".claude", platform.dir_name)
        .replace("CLAUDE.md", platform.memory_file)
        .encode("utf-8")
    )


def _write(dest: Path, data: bytes, *, backup: bool) -> Path | None:
    """Write *data* to *dest*, moving any existing file aside first."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    saved: Path | None = None
    if backup and dest.exists():
        saved = dest.with_name(dest.name + BACKUP_SUFFIX)
        shutil.move(str(dest), str(saved))
    dest.write_bytes(data)
    return saved


def _is_sidecar(name: str) -> bool:
    return name == SKILL_VERSION_MARKER or name.endswith(SKILL_VERSION_SUFFIX)


def _copy(skill: Skill, target: Path, platform: Platform) -> Path | None:
    """Install *skill* at *target*, rewritten for *platform*.

    Returns the first backup made, if any. The vendored sidecar is skipped
    during the copy and written fresh at the end, so it always carries the
    version that actually installed the files.
    """
    first_backup: Path | None = None
    if skill.is_dir:
        for src in sorted(skill.source.rglob("*")):
            if not src.is_file() or _is_sidecar(src.name):
                continue
            saved = _write(
                target / src.relative_to(skill.source),
                _rewrite(src.read_bytes(), platform),
                backup=True,
            )
            first_backup = first_backup or saved
    else:
        first_backup = _write(
            target, _rewrite(skill.source.read_bytes(), platform), backup=True
        )
    _write(
        _sidecar(skill, target),
        f"x-aicp-version: {__version__}\n".encode(),
        backup=False,
    )
    return first_backup


def install(
    clis: Sequence[CLI] = ROSTER,
    *,
    home: Path | None = None,
    force: bool = False,
) -> list[InstallResult]:
    """Install the vendored skills into every configured CLI in *clis*.

    Non-interactive by design — this is what a ``--yes``-style run does:
    install what's :data:`MISSING`, upgrade what's :data:`OURS_OLDER`, and
    leave anything :data:`FOREIGN` exactly where it is. Only ``force=True``
    replaces a foreign file, and even then the original is moved to
    ``<name>.bak`` first. A CLI with no config dir is skipped, never created.
    """
    results: list[InstallResult] = []
    for cli in clis:
        platform = _platform(cli)
        for name in platform.skills:
            spec = SKILLS[name]
            target = target_path(cli, name, home)
            state = detect(cli, name, home)
            backup: Path | None = None

            if state == NOT_INSTALLED:
                action = NOT_INSTALLED
            elif state == CURRENT:
                action = UP_TO_DATE
            elif state == FOREIGN and not force:
                action = KEPT
            else:
                backup = _copy(spec, target, platform)
                action = {
                    MISSING: INSTALLED,
                    OURS_OLDER: UPGRADED,
                    FOREIGN: OVERWRITTEN,
                }[state]

            results.append(InstallResult(cli.name, name, target, state, action, backup))
    return results
