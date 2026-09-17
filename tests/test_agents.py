"""Exercise registry edits through the real command and skill consumers."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import aicp
from aicp.agents import _load_user_overrides, load_agents, merge_user_agents


@pytest.mark.parametrize("location", ["home", "absolute", "environment"])
def test_registry_edit_reaches_commands_and_skills(tmp_path, location):
    package = Path(aicp.__file__).parent
    registry = package / "agents.json"
    assert registry.is_file(), "agent definitions must ship with the package"
    shutil.copytree(package, tmp_path / "aicp")
    from aicp.skills import VENDOR_DIR

    shutil.copytree(VENDOR_DIR, tmp_path / "aicp/_skills_data", dirs_exist_ok=True)
    data = json.loads(registry.read_text())
    root = Path.home() / "custom agent 測試" / "config"
    data["agents"]["codex"].update(
        executable=sys.executable,
        config_dir="~/custom agent 測試/config" if location == "home" else str(root),
        skills_dir="custom-skills",
        args=["-c", "import sys; print(sys.argv[1])", "{prompt}"],
    )
    if location == "environment":
        data["agents"]["codex"].update(
            config_dir="~/unused", config_dir_env="AICP_TEST_AGENT_HOME"
        )
    (tmp_path / "aicp/agents.json").write_text(json.dumps(data))
    result = subprocess.run(
        [sys.executable, "-c", """
from pathlib import Path
import os
from aicp import cli, gitflow, runner, skills
from aicp.contracts import ROSTER
agent = next(c for c in ROSTER if c.name == 'codex')
root = Path(os.environ['AICP_TEST_AGENT_HOME'])
root.mkdir(parents=True)
assert skills.config_root(agent) == root
assert gitflow.preflight(['codex'])[0]
cli._nudge(['codex'])
assert skills.missing_skills(agent) == ('commit', 'safe-git-push')
skills.install([agent])
target = root / 'custom-skills/commit/SKILL.md'
assert target.is_file()
assert skills.detect(agent, 'commit') == skills.CURRENT
assert skills.missing_skills(agent) == ()
assert skills.full_status([agent])[0].target == target
prompt = 'literal $HOME; {prompt} with spaces'
assert runner.invoke_argv('codex', prompt)[-1] == prompt
assert runner.run_step(prompt, ['codex']).rc == 0
"""],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "PATH": "",
            "PYTHONUTF8": "1",
            "AICP_TEST_AGENT_HOME": str(root),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
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


def _write(path: Path, agents: dict) -> Path:
    path.write_text(json.dumps({"version": 1, "agents": agents}))
    return path


def test_merge_user_agents_adds_a_new_agent(tmp_path):
    base = load_agents(Path(aicp.__file__).with_name("agents.json"))
    override = _write(
        tmp_path / "agents.json",
        {
            "minimax": {
                "executable": "minimax",
                "config_dir": "~/.minimax",
                "memory_file": "AGENTS.md",
                "skills_dir": "skills",
                "skills": ["safe-git-push"],
                "args": ["--prompt", "{prompt}", "--yes"],
            }
        },
    )
    merged = merge_user_agents(base, override)
    assert set(merged) == set(base) | {"minimax"}
    assert merged["minimax"].executable == "minimax"


def test_merge_user_agents_overrides_only_listed_fields(tmp_path):
    base = load_agents(Path(aicp.__file__).with_name("agents.json"))
    override = _write(tmp_path / "agents.json", {"claude": {"executable": "/opt/claude"}})
    merged = merge_user_agents(base, override)
    assert merged["claude"].executable == "/opt/claude"
    assert merged["claude"].config_dir == base["claude"].config_dir
    assert merged["claude"].args == base["claude"].args


def test_merge_user_agents_disabled_drops_the_agent(tmp_path):
    base = load_agents(Path(aicp.__file__).with_name("agents.json"))
    override = _write(tmp_path / "agents.json", {"grok": {"disabled": True}})
    merged = merge_user_agents(base, override)
    assert "grok" not in merged
    assert set(merged) == set(base) - {"grok"}


def test_merge_user_agents_new_agent_requires_every_field(tmp_path):
    base = load_agents(Path(aicp.__file__).with_name("agents.json"))
    override = _write(tmp_path / "agents.json", {"minimax": {"executable": "minimax"}})
    with pytest.raises(ValueError, match="config_dir"):
        merge_user_agents(base, override)


def test_load_user_overrides_warns_and_falls_back_on_malformed_file(tmp_path, capsys):
    base = load_agents(Path(aicp.__file__).with_name("agents.json"))
    (tmp_path / ".aicp").mkdir()
    (tmp_path / ".aicp" / "agents.json").write_text("not json")
    result = _load_user_overrides(base, home=tmp_path)
    assert result == base
    assert "ignoring" in capsys.readouterr().err


def test_load_user_overrides_is_a_noop_with_no_file(tmp_path):
    base = load_agents(Path(aicp.__file__).with_name("agents.json"))
    assert _load_user_overrides(base, home=tmp_path) == base


def test_user_agents_json_reaches_roster_and_cli_order(tmp_path):
    home = tmp_path / "home"
    (home / ".aicp").mkdir(parents=True)
    _write(
        home / ".aicp" / "agents.json",
        {
            "grok": {"disabled": True},
            "minimax": {
                "executable": "minimax",
                "config_dir": "~/.minimax",
                "memory_file": "AGENTS.md",
                "skills_dir": "skills",
                "skills": ["safe-git-push"],
                "args": ["--prompt", "{prompt}", "--yes"],
            },
        },
    )
    result = subprocess.run(
        [sys.executable, "-c", """
from aicp.contracts import ROSTER
from aicp.config import resolve_cli_chain
names = [c.name for c in ROSTER]
assert 'grok' not in names, names
assert 'minimax' in names, names
assert resolve_cli_chain('minimax claude')[0] == 'minimax'
print('ok')
"""],
        env={**os.environ, "HOME": str(home), "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_config_root_preserves_absolute_paths_and_live_env(tmp_path, monkeypatch):
    from dataclasses import replace

    from aicp.agents import AGENTS

    agent = replace(AGENTS["grok"], config_dir=str(tmp_path / "absolute"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "override"))
    assert agent.config_root() == tmp_path / "override"
    assert agent.config_root(tmp_path / "home") == tmp_path / "absolute"
    monkeypatch.delenv("GROK_HOME")
    assert agent.config_root() == tmp_path / "absolute"
