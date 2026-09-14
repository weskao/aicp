"""Exercise registry edits through the real command and skill consumers."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import aicp
from aicp.agents import load_agents


def test_registry_edit_reaches_commands_and_skills(tmp_path):
    package = Path(aicp.__file__).parent
    registry = package / "agents.json"
    assert registry.is_file(), "agent definitions must ship with the package"
    shutil.copytree(package, tmp_path / "aicp")
    from aicp.skills import VENDOR_DIR

    shutil.copytree(VENDOR_DIR, tmp_path / "aicp/_skills_data", dirs_exist_ok=True)
    data = json.loads(registry.read_text())
    data["agents"]["codex"].update(
        executable=sys.executable,
        config_dir="~/custom/agent",
        skills_dir="custom-skills",
        args=["-c", "import sys; print(sys.argv[1])", "{prompt}"],
    )
    (tmp_path / "aicp/agents.json").write_text(json.dumps(data))
    result = subprocess.run(
        [sys.executable, "-c", """
from pathlib import Path
from aicp import cli, gitflow, runner, skills
from aicp.contracts import ROSTER
agent = next(c for c in ROSTER if c.name == 'codex')
root = Path.home() / 'custom/agent'
root.mkdir(parents=True)
assert agent.config_dir == root
assert gitflow.preflight(['codex'])[0]
cli._nudge(['codex'])
assert skills.missing_skills(agent) == ('commit', 'safe-git-push')
skills.install([agent])
target = root / 'custom-skills/commit/SKILL.md'
assert target.is_file()
assert skills.detect(agent, 'commit') == skills.CURRENT
assert skills.missing_skills(agent) == ()
assert skills.full_status([agent])[0].target == target
assert skills.config_root(agent, Path.home()) == root
prompt = 'literal $HOME; {prompt} with spaces'
assert runner.invoke_argv('codex', prompt)[-1] == prompt
assert runner.run_step(prompt, ['codex']).rc == 0
"""],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "missing the commit, safe-git-push skill" in result.stdout


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("args", ["-p"]),
        ("args", ["{prompt}", "{prompt}"]),
        ("args", "-p {prompt}"),
        ("args", ["{prompt}", 1]),
        ("executable", ""),
        ("config_dir", "relative/to/cwd"),
        ("skills_dir", "../outside"),
        ("skills_dir", "/outside"),
        ("skills_dir", "."),
        ("config_dir_env", ["INVALID"]),
    ],
)
def test_rejects_invalid_registry_fields(tmp_path, field, value):
    data = json.loads(Path(aicp.__file__).with_name("agents.json").read_text())
    data["agents"]["codex"][field] = value
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=field):
        load_agents(path)


def test_config_root_preserves_absolute_paths_and_live_env(tmp_path, monkeypatch):
    from dataclasses import replace

    from aicp.agents import AGENTS

    agent = replace(AGENTS["grok"], config_dir=str(tmp_path / "absolute"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "override"))
    assert agent.config_root() == tmp_path / "override"
    assert agent.config_root(tmp_path / "home") == tmp_path / "absolute"
    monkeypatch.delenv("GROK_HOME")
    assert agent.config_root() == tmp_path / "absolute"
