"""Bundled agent definitions shared by execution and skill management.

The package's ``agents.json`` is the base registry. ``~/.aicp/agents.json``,
if present, is merged over it at import time: an entry for a name already in
the base registry overrides only the fields it lists (unspecified fields
keep the built-in value); an entry for a new name must supply every field,
same as the base registry; ``{"disabled": true}`` drops that name from the
chain entirely. A malformed user file is warned about on stderr and ignored
rather than crashing every invocation — see :func:`_load_user_overrides`.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

#: Home-relative config prefixes that a live environment variable relocates.
_LIVE_BASES = {"~/.config/": "XDG_CONFIG_HOME", "~/AppData/Roaming/": "APPDATA"}


@dataclass(frozen=True)
class Agent:
    executable: str
    config_dir: str
    memory_file: str
    skills_dir: str
    skills: tuple[str, ...]
    args: tuple[str, ...]
    config_dir_env: str = ""
    #: Used instead of ``config_dir`` on Windows when set (devin: %APPDATA%).
    config_dir_windows: str = ""

    def config_root(self, home: Path | None = None) -> Path:
        if home is None and (override := os.environ.get(self.config_dir_env)):
            return Path(override).expanduser()
        config_dir = self.config_dir_windows if sys.platform == "win32" and self.config_dir_windows else self.config_dir
        if config_dir.startswith("~/"):
            for prefix, var in _LIVE_BASES.items():
                # Live environment only (home=None); relative values are ignored, per the XDG spec.
                if home is None and config_dir.startswith(prefix) and os.path.isabs(base := os.environ.get(var, "")):
                    return Path(base) / config_dir[len(prefix) :]
            return (Path.home() if home is None else home) / config_dir[2:]
        return Path(config_dir)


def _read_registry(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"{path}: unsupported agent registry version")
    entries = data.get("agents")
    if not isinstance(entries, dict) or not entries:
        raise ValueError(f"{path}: expected a nonempty agents object")
    return entries


def _build_agent(path: Path, name: str, entry: dict, base: Agent | None) -> Agent:
    """Validate *entry*, falling back to *base*'s fields for anything it omits.

    ``base=None`` (the built-in registry, or a brand-new name) requires every
    field to be present, same as before this function existed.
    """
    if not isinstance(entry, dict):
        raise TypeError(f"{path}: {name}: expected an agent object")
    merged: dict[str, object] = {
        "executable": base.executable if base else None,
        "config_dir": base.config_dir if base else None,
        "memory_file": base.memory_file if base else None,
        "skills_dir": base.skills_dir if base else None,
        "skills": list(base.skills) if base else None,
        "args": list(base.args) if base else None,
        "config_dir_env": base.config_dir_env if base else "",
        # An overridden config_dir must win on Windows too, not the built-in Windows path.
        "config_dir_windows": base.config_dir_windows if base and "config_dir" not in entry else "",
    }
    merged.update({k: v for k, v in entry.items() if k != "disabled"})
    for key in ["executable", "config_dir", "memory_file", "skills_dir"]:
        value = merged.get(key)
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError(f"{path}: {name}: invalid {key}")
    for key in ("args", "skills"):
        value = merged.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and "\0" not in item for item in value
        ):
            raise ValueError(f"{path}: {name}: invalid {key}")
    if merged["args"].count("{prompt}") != 1:
        raise ValueError(f"{path}: {name}: args must contain one {{prompt}} argument")
    for key in ("config_dir", "config_dir_windows"):
        value = merged.get(key)
        if key == "config_dir_windows" and value == "":
            continue
        absolute = PureWindowsPath if key == "config_dir_windows" else Path
        if not isinstance(value, str) or "\0" in value or (not value.startswith("~/") and not absolute(value).is_absolute()):
            raise ValueError(f"{path}: {name}: {key} must be absolute or start with ~/")
    skills_dir = Path(merged["skills_dir"])
    if skills_dir.anchor or ".." in skills_dir.parts or skills_dir == Path("."):
        raise ValueError(f"{path}: {name}: skills_dir must stay inside config_dir")
    env = merged.get("config_dir_env", "")
    if not isinstance(env, str) or (env and not env.isidentifier()):
        raise ValueError(f"{path}: {name}: invalid config_dir_env")
    return Agent(
        executable=merged["executable"],
        config_dir=merged["config_dir"],
        memory_file=merged["memory_file"],
        skills_dir=merged["skills_dir"],
        skills=tuple(merged["skills"]),
        args=tuple(merged["args"]),
        config_dir_env=env,
        config_dir_windows=merged["config_dir_windows"],
    )


def load_agents(path: Path) -> dict[str, Agent]:
    """Validate a full (built-in shaped) registry file."""
    entries = _read_registry(path)
    return {name: _build_agent(path, name, entry, base=None) for name, entry in entries.items()}


def merge_entries(
    agents: dict[str, Agent], entries: dict[str, dict], path: Path
) -> dict[str, Agent]:
    """Overlay raw *entries* onto *agents*: partial overrides for known names,
    full entries for new ones, ``disabled: true`` to drop a name.

    Takes the entries rather than a file so :mod:`aicp.agentcfg` can validate
    a candidate registry it has not written yet — the whole reason an edit can
    be refused without touching what is already on disk. *path* only names the
    source in error messages.
    """
    merged = dict(agents)
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise TypeError(f"{path}: {name}: expected an agent object")
        if entry.get("disabled") is True:
            merged.pop(name, None)
            continue
        merged[name] = _build_agent(path, name, entry, base=merged.get(name))
    return merged


def merge_user_agents(agents: dict[str, Agent], path: Path) -> dict[str, Agent]:
    """:func:`merge_entries` over the registry file at *path*."""
    return merge_entries(agents, _read_registry(path), path)


BUILTIN_PATH = Path(__file__).with_name("agents.json")


def user_registry_path(home: Path | None = None) -> Path:
    """Where a user's own overrides live. One place, so the reader, the writer
    and the tests can never disagree about it."""
    return (home or Path.home()) / ".aicp" / "agents.json"


def read_user_entries(path: Path) -> dict[str, dict]:
    """The user file's raw entries, or ``{}`` when there is no file."""
    return _read_registry(path) if path.is_file() else {}


#: What :func:`inventory` says about a name. Stable ids, not display text —
#: ``agentcfg.STATE_LABELS`` maps them to translatable strings.
BUILT_IN = "builtin"
OVERRIDDEN = "overridden"
ADDED = "added"
DISABLED = "disabled"


@dataclass(frozen=True)
class AgentRow:
    """One line of :func:`inventory`."""

    name: str
    executable: str
    state: str


def inventory(home: Path | None = None) -> list[AgentRow]:
    """Every agent name aicp knows of, built-in order first, and where it
    came from. Disabled names are listed too — hiding them is how someone
    loses the ability to turn one back on."""
    path = user_registry_path(home)
    builtin = load_agents(BUILTIN_PATH)
    entries = read_user_entries(path)
    merged = merge_entries(builtin, entries, path)
    rows = []
    for name in [*builtin, *(n for n in entries if n not in builtin)]:
        entry = entries.get(name)
        if entry is not None and entry.get("disabled") is True:
            base = builtin.get(name)
            rows.append(AgentRow(name, base.executable if base else "—", DISABLED))
        else:
            state = BUILT_IN if entry is None else (OVERRIDDEN if name in builtin else ADDED)
            rows.append(AgentRow(name, merged[name].executable, state))
    return rows


def _load_user_overrides(agents: dict[str, Agent], home: Path | None = None) -> dict[str, Agent]:
    path = user_registry_path(home)
    if not path.is_file():
        return agents
    try:
        return merge_user_agents(agents, path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"aicp: ignoring {path} ({exc}) — using built-in agents only", file=sys.stderr)
        return agents


AGENTS = _load_user_overrides(load_agents(BUILTIN_PATH))


def executable(name: str) -> str:
    """Resolve a roster ID to its binary; leave non-agent commands alone."""
    agent = AGENTS.get(name)
    return agent.executable if agent else name
