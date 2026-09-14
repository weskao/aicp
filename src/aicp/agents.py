"""Bundled agent definitions shared by execution and skill management.

Only the package's agents.json is read; working-directory and user config
files cannot supply executable paths or arguments through this registry.
"""

from __future__ import annotations

import json
import os
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


def load_agents(path: Path) -> dict[str, Agent]:
    """Validate packaged data and preserve its default fallback order."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"{path}: unsupported agent registry version")
    entries = data.get("agents")
    if not isinstance(entries, dict) or not entries:
        raise ValueError(f"{path}: expected a nonempty agents object")
    agents = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise TypeError(f"{path}: {name}: expected an agent object")
        for key in ["executable", "config_dir", "memory_file", "skills_dir"]:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"{path}: {name}: invalid {key}")
        for key in ("args", "skills"):
            value = entry.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and "\0" not in item for item in value
            ):
                raise ValueError(f"{path}: {name}: invalid {key}")
        if entry["args"].count("{prompt}") != 1:
            raise ValueError(f"{path}: {name}: args must contain one {{prompt}} argument")
        if not entry["config_dir"].startswith("~/") and not Path(entry["config_dir"]).is_absolute():
            raise ValueError(f"{path}: {name}: config_dir must be absolute or start with ~/")
        skills_dir = Path(entry["skills_dir"])
        if skills_dir.anchor or ".." in skills_dir.parts or skills_dir == Path("."):
            raise ValueError(f"{path}: {name}: skills_dir must stay inside config_dir")
        env = entry.get("config_dir_env", "")
        if not isinstance(env, str) or (env and not env.isidentifier()):
            raise ValueError(f"{path}: {name}: invalid config_dir_env")
        agents[name] = Agent(
            executable=entry["executable"],
            config_dir=entry["config_dir"],
            memory_file=entry["memory_file"],
            skills_dir=entry["skills_dir"],
            skills=tuple(entry["skills"]),
            args=tuple(entry["args"]),
            config_dir_env=env,
        )
    return agents


AGENTS = load_agents(Path(__file__).with_name("agents.json"))


def executable(name: str) -> str:
    """Resolve a roster ID to its binary; leave non-agent commands alone."""
    agent = AGENTS.get(name)
    return agent.executable if agent else name
