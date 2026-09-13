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
* **Anything aicp did not write, byte for byte, is the user's**
  (:data:`FOREIGN`). It is never overwritten without an explicit
  ``force=True``; a ``--yes``-style non-interactive run installs only what's
  missing. That covers a file aicp installed and the user edited *afterwards*,
  which a version-only marker cannot see — see :func:`state_path` and
  :data:`aicp.contracts.STATE_DIR_NAME`.
* **A CLI whose config dir doesn't exist is not installed.** Skip it; never
  create the directory.
* **Nothing of aicp's is written into a CLI's config dir.** The bookkeeping
  lives in one ``$HOME/.aicp/state.json``; the only thing installed under a
  config dir is the skill itself. Pre-0.2 in-place ``.aicp-version`` sidecars
  are folded into that file and deleted on the next install.

All public functions take ``home=`` and otherwise resolve :func:`Path.home`
at call time (i.e. ``$HOME`` as it is *now*), so a test with a fake ``$HOME``
can never reach the developer's real config dirs. The per-CLI directory name
still comes from ``config_dir.name`` — from the contract, not from the binary
name.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .contracts import (
    CLI,
    ROSTER,
    SKILL_VERSION_MARKER,
    SKILL_VERSION_SUFFIX,
    STATE_DIR_NAME,
    STATE_FILE_NAME,
    STATE_SCHEMA_VERSION,
)

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
    "state_path",
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
    """Where the vendored copies live: installed wheel first, then checkout.

    ``_skills_data`` (not ``skills``) is the in-package name, so it can't
    shadow this module. Shipping it needs one key in pyproject.toml::

        [tool.hatch.build.targets.wheel.force-include]
        "skills" = "aicp/_skills_data"
    """
    here = Path(__file__).resolve().parent
    installed, checkout = here / "_skills_data", here.parent.parent / "skills"
    return installed if installed.is_dir() else checkout


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
    ".grok": Platform(".grok", "AGENTS.md", _BOTH),
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
    """This CLI's config dir, re-based on *home* or the live environment."""
    if home is not None:
        return Path(home) / cli.config_dir.name
    if cli.name == "grok" and (grok_home := os.environ.get("GROK_HOME")):
        return Path(grok_home)
    return Path.home() / cli.config_dir.name


def target_path(cli: CLI, skill: str, home: Path | None = None) -> Path:
    return config_root(cli, home) / SKILLS[skill].rel_target


# ── the state file ───────────────────────────────────────────────────────────


def state_path(home: Path | None = None) -> Path:
    """aicp's own bookkeeping — one file per user, never in a CLI's config dir.

    Keyed on ``$HOME`` alone, deliberately not on ``GROK_HOME`` or any per-CLI
    root (unlike :func:`config_root`): one record describes every install
    target across every CLI.
    """
    return (Path(home) if home is not None else Path.home()) / STATE_DIR_NAME / STATE_FILE_NAME


def _load_state(home: Path | None = None) -> dict[str, dict]:
    """Every recorded install, keyed by ``str(target)``.

    A missing, unreadable or malformed file means "nothing recorded" rather
    than an error: aicp has to keep working when its own state is gone, and
    the worst that follows is that installs read :data:`FOREIGN` and are left
    alone — never that the user's files are damaged.
    """
    try:
        data = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    records = data.get("skills") if isinstance(data, dict) else None
    return records if isinstance(records, dict) else {}


def _save_state(records: dict[str, dict], home: Path | None = None) -> None:
    """Write the state file atomically.

    Via a temp file in the same directory plus :func:`os.replace` (atomic on
    POSIX and Windows alike), because a half-written state.json would read as
    "nothing recorded" and turn every installed skill foreign.
    """
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": STATE_SCHEMA_VERSION, "skills": records}, indent=2, sort_keys=True
    )
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        # Closed before the replace: Windows will not rename a file that is
        # still open.
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _sha(data: bytes) -> str:
    """Content digest of exactly the bytes written to disk.

    hashlib, never a shelled-out ``sha256sum``/``certutil``: no subprocess, no
    per-platform binary, and no text-mode newline translation to get wrong.
    """
    return hashlib.sha256(data).hexdigest()


def _owned(skill: Skill) -> tuple[str, ...]:
    """The files *skill* installs, as target-relative POSIX keys.

    POSIX keys (``scripts/safe_push.py``, never ``scripts\\safe_push.py``) so
    a record written on Windows still matches the same tree on macOS/Linux.
    A single-file skill owns exactly ``"."`` — itself.
    """
    if not skill.is_dir:
        return (".",)
    return tuple(
        src.relative_to(skill.source).as_posix()
        for src in sorted(skill.source.rglob("*"))
        if src.is_file() and not _skip(src.name)
    )


def _resolve(target: Path, rel: str) -> Path:
    """A record key back to a real path — ``"."`` means the target itself."""
    return target if rel == "." else target / rel


def _matches(target: Path, files: dict[str, str]) -> bool:
    """True when every recorded file still hashes to what aicp wrote.

    Only recorded files are checked, so anything the user ADDED alongside them
    — an extra script, a ``.bak``, a ``.DS_Store`` — is not mistaken for a
    modification of our install.
    """
    for rel, digest in files.items():
        try:
            if _sha(_resolve(target, rel).read_bytes()) != digest:
                return False
        except OSError:
            return False
    return True


def _disk_hashes(skill: Skill, target: Path) -> dict[str, str]:
    """Hash the files *skill* owns as they currently sit at *target*."""
    hashes: dict[str, str] = {}
    for rel in _owned(skill):
        try:
            hashes[rel] = _sha(_resolve(target, rel).read_bytes())
        except OSError:
            continue
    return hashes


def _expected(skill: Skill, platform: Platform) -> dict[str, str]:
    """Hashes of what installing *skill* for *platform* would write right now.

    The fallback for a target with no record: a wiped or fresh ``$HOME`` must
    not turn every installed skill foreign and refuse to update it ever again.
    """
    if not skill.source.exists():
        return {}
    if not skill.is_dir:
        return {".": _sha(_rewrite(skill.source.read_bytes(), platform))}
    return {
        rel: _sha(_rewrite((skill.source / rel).read_bytes(), platform))
        for rel in _owned(skill)
    }


def _record_install(target: Path, hashes: dict[str, str], home: Path | None) -> None:
    records = _load_state(home)
    records[str(target)] = {"version": __version__, "files": hashes}
    _save_state(records, home)


# ── legacy in-place sidecars (aicp <= 0.1.0) ─────────────────────────────────


def _legacy_sidecar(skill: Skill, target: Path) -> Path:
    if skill.is_dir:
        return target / SKILL_VERSION_MARKER
    return target.with_name(target.name + SKILL_VERSION_SUFFIX)


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


def _legacy_record(skill: Skill, target: Path, platform: Platform) -> dict | None:
    """A pre-state.json install, read off its in-place sidecar.

    Hashes come from what is on disk NOW — the sidecar never recorded content,
    which is the whole reason it is being replaced — with one guard. A sidecar
    claiming the version aicp is running over content aicp would NOT write
    means the file was changed after it was installed, by the user or by
    another tool entirely. Returning None there drops it to the content check,
    which calls it FOREIGN and leaves it alone, instead of laundering someone
    else's file into "ours" and overwriting it on the next version bump.

    Content that differs because it is genuinely OLD still records normally,
    so a pre-0.2 install upgrades rather than freezing at foreign.
    """
    version = _read_version(_legacy_sidecar(skill, target))
    if version is None:
        return None
    hashes = _disk_hashes(skill, target)
    if _as_tuple(version) >= _as_tuple(__version__):
        expected = _expected(skill, platform)
        if expected and hashes != expected:
            return None
    return {"version": version, "files": hashes}


def _migrate_legacy(
    skill: Skill, target: Path, platform: Platform, home: Path | None
) -> None:
    """Fold a legacy sidecar into the state file and delete it.

    The sidecar goes even when the state file already covers this target:
    leaving aicp's bookkeeping inside a CLI's config dir is the thing being
    fixed, not a detail of it.
    """
    sidecar = _legacy_sidecar(skill, target)
    if not sidecar.is_file():
        return
    records = _load_state(home)
    if str(target) not in records:
        record = _legacy_record(skill, target, platform)
        if record is not None:
            records[str(target)] = record
            _save_state(records, home)
    sidecar.unlink(missing_ok=True)


# ── detection ────────────────────────────────────────────────────────────────


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))


def _inspect(cli: CLI, skill: str, home: Path | None) -> tuple[str, str | None]:
    """``(state, recorded version)`` for one skill — the shared slow path."""
    root = config_root(cli, home)
    if not root.is_dir():
        return NOT_INSTALLED, None
    spec = SKILLS[skill]
    target = root / spec.rel_target
    if not target.exists():
        return MISSING, None

    platform = _platform(cli)
    record = _load_state(home).get(str(target)) or _legacy_record(spec, target, platform)
    if not record:
        # Nothing recorded. Content identical to what this version installs is
        # still ours (a wiped state file, a config dir copied to a new
        # machine); anything else is the user's.
        expected = _expected(spec, platform)
        if expected and _matches(target, expected):
            return CURRENT, __version__
        return FOREIGN, None

    if not _matches(target, record.get("files") or {}):
        return FOREIGN, None  # ours once, hand-edited since — never overwrite
    version = record["version"]
    state = OURS_OLDER if _as_tuple(version) < _as_tuple(__version__) else CURRENT
    return state, version


def detect(cli: CLI, skill: str, home: Path | None = None) -> str:
    """State of *skill* for *cli*: one of the module's five state constants.

    Reads content to hash it — this is the menu/doctor path, not the hot path
    (see :func:`missing_skills` for that).
    """
    return _inspect(cli, skill, home)[0]


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
            state, version = _inspect(cli, name, home)
            out.append(
                SkillStatus(cli.name, name, target_path(cli, name, home), state, version)
            )
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
    """Point the vendored text at *this* CLI's config dir and memory file.

    Plain substring replacement, which is safe only because of an invariant
    of the vendored text: every ``.claude`` / ``CLAUDE.md`` in it is a path
    or filename reference (4 of them, all in ``skills/commit.md``). Re-vendor
    something containing e.g. ``.claude-foo`` and this needs word boundaries.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data  # not text; copy verbatim
    return (
        text.replace(".claude", platform.dir_name)
        .replace("CLAUDE.md", platform.memory_file)
        .encode("utf-8")
    )


def _backup_path(dest: Path) -> Path:
    """The first free ``<name>.bak``, ``<name>.bak.1``, ``<name>.bak.2``…

    Numbered, because a backup is never allowed to overwrite a backup: a
    forced install parks the user's own file at ``<name>.bak``, and a *later*
    version bump turns that install into an ``ours_older`` upgrade, which
    would otherwise move our own vendored copy over the only surviving copy
    of their original.
    """
    candidate = dest.with_name(dest.name + BACKUP_SUFFIX)
    index = 1
    while candidate.exists():
        candidate = dest.with_name(f"{dest.name}{BACKUP_SUFFIX}.{index}")
        index += 1
    return candidate


def _write(dest: Path, data: bytes, *, backup: bool) -> Path | None:
    """Write *data* to *dest*, moving any existing file aside first."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    saved: Path | None = None
    if backup and dest.exists():
        saved = _backup_path(dest)
        # os.replace rather than shutil.move: shutil.move's os.rename raises
        # FileExistsError on Windows if the destination is taken, and this
        # one is only free as of the check above.
        os.replace(dest, saved)
    dest.write_bytes(data)
    return saved


def _skip(name: str) -> bool:
    """Sidecars are rewritten separately; macOS sprinkles .DS_Store around."""
    return (
        name == SKILL_VERSION_MARKER
        or name.endswith(SKILL_VERSION_SUFFIX)
        or name == ".DS_Store"
    )


def _copy(
    skill: Skill, target: Path, platform: Platform, *, home: Path | None = None
) -> Path | None:
    """Install *skill* at *target*, rewritten for *platform*.

    Returns the first backup made, if any. Hashes are taken from the bytes
    actually written (post-:func:`_rewrite`, so they are per-platform) and
    recorded together at the end, so the record always describes the files
    that landed.
    """
    if not skill.source.exists():
        # Without this, a directory skill whose source is missing (an
        # unpackaged wheel — see _vendor_dir) would copy zero files and still
        # record an install, so detect() would report CURRENT forever.
        raise FileNotFoundError(f"vendored skill is missing: {skill.source}")

    first_backup: Path | None = None
    hashes: dict[str, str] = {}
    if skill.is_dir:
        for src in sorted(skill.source.rglob("*")):
            if not src.is_file() or _skip(src.name):
                continue
            rel = src.relative_to(skill.source).as_posix()
            data = _rewrite(src.read_bytes(), platform)
            saved = _write(target / rel, data, backup=True)
            hashes[rel] = _sha(data)
            first_backup = first_backup or saved
    else:
        data = _rewrite(skill.source.read_bytes(), platform)
        first_backup = _write(target, data, backup=True)
        hashes["."] = _sha(data)
    _record_install(target, hashes, home)
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
            # Before detecting, not after: a legacy sidecar is removed even
            # when the install turns out to be UP_TO_DATE and nothing else is
            # written.
            _migrate_legacy(spec, target, platform, home)
            state = detect(cli, name, home)
            backup: Path | None = None

            if state == NOT_INSTALLED:
                action = NOT_INSTALLED
            elif state == CURRENT:
                action = UP_TO_DATE
            elif state == FOREIGN and not force:
                action = KEPT
            else:
                backup = _copy(spec, target, platform, home=home)
                action = {
                    MISSING: INSTALLED,
                    OURS_OLDER: UPGRADED,
                    FOREIGN: OVERWRITTEN,
                }[state]

            results.append(InstallResult(cli.name, name, target, state, action, backup))
    return results
