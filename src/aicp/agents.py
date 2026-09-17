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
from pathlib import Path


@dataclass(frozen=True)
class Agent:
    executable: str
    config_dir: str
    memory_file: str
    skills_dir: str
    skills: tuple[str, ...]
    args: tuple[str, ...]
    config_dir_env: str = ""

    def config_root(self, home: Path | None = None) -> Path:
        if home is None and (override := os.environ.get(self.config_dir_env)):
            return Path(override).expanduser()
        if self.config_dir.startswith("~/"):
            return (Path.home() if home is None else home) / self.config_dir[2:]
        return Path(self.config_dir)


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
    if not merged["config_dir"].startswith("~/") and not Path(merged["config_dir"]).is_absolute():
        raise ValueError(f"{path}: {name}: config_dir must be absolute or start with ~/")
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
    )


def load_agents(path: Path) -> dict[str, Agent]:
    """Validate a full (built-in shaped) registry file."""
    entries = _read_registry(path)
    return {name: _build_agent(path, name, entry, base=None) for name, entry in entries.items()}


def merge_user_agents(agents: dict[str, Agent], path: Path) -> dict[str, Agent]:
    """Overlay ``path``'s entries onto *agents*: partial overrides for known
    names, full entries for new ones, ``disabled: true`` to drop a name."""
    merged = dict(agents)
    for name, entry in _read_registry(path).items():
        if not isinstance(entry, dict):
            raise TypeError(f"{path}: {name}: expected an agent object")
        if entry.get("disabled") is True:
            merged.pop(name, None)
            continue
        merged[name] = _build_agent(path, name, entry, base=merged.get(name))
    return merged


def _load_user_overrides(agents: dict[str, Agent], home: Path | None = None) -> dict[str, Agent]:
    path = (home or Path.home()) / ".aicp" / "agents.json"
    if not path.is_file():
        return agents
    try:
        return merge_user_agents(agents, path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"aicp: ignoring {path} ({exc}) — using built-in agents only", file=sys.stderr)
        return agents


AGENTS = _load_user_overrides(load_agents(Path(__file__).with_name("agents.json")))


def executable(name: str) -> str:
    """Resolve a roster ID to its binary; leave non-agent commands alone."""
    agent = AGENTS.get(name)
    return agent.executable if agent else name
