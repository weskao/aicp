"""Editing the agent registry: ``aicp --agents`` and :mod:`aicp.agentcfg`.

The load-bearing property here is not that an edit works — it is that a
*refused* edit changes nothing. ``~/.aicp/agents.json`` is read at import
time by every aicp run, so a half-written or invalid file is not a bad edit,
it is a tool that no longer starts. Several tests below therefore hash the
file before and after a rejected operation rather than only asserting on the
error text.

The second property is that the registry and the saved ``AICP_CLI_ORDER``
can never disagree: ``config.resolve_cli_chain`` refuses an order containing
an unknown name *in full*, silently replacing a hand-picked chain with the
default roster. Disabling an agent has to prune that order in the same
breath, and ``test_disabling_keeps_the_rest_of_a_hand_picked_order`` is the
regression test for exactly that.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aicp import agentcfg, agents, config
from aicp.cli import main

MINIMAX = (
    "executable=minimax",
    "config_dir=~/.minimax",
    "memory_file=AGENTS.md",
    "skills_dir=skills",
    "skills=safe-git-push",
    "args=--prompt,{prompt},--yes",
)


@pytest.fixture
def home(pinned_environment) -> Path:
    """The autouse fake ``$HOME``; ``~/.aicp`` does not exist yet."""
    return pinned_environment


@pytest.fixture
def cfg(home) -> Path:
    """The ``config.json`` path the chain-pruning tests read and write."""
    return home / ".aicp" / "config.json"


def registry(home: Path) -> Path:
    return agents.user_registry_path(home)


def entries(home: Path) -> dict:
    return json.loads(registry(home).read_text(encoding="utf-8"))["agents"]


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def names(home: Path) -> list[str]:
    return [row.name for row in agents.inventory(home)]


# ── the verbs ────────────────────────────────────────────────────────────────


def test_disable_drops_the_name_from_the_registry(home):
    change = agentcfg.apply("disable", "grok", home=home)
    assert change.verb == "disabled"
    assert "grok" not in change.names
    assert entries(home) == {"grok": {"disabled": True}}


def test_a_disabled_agent_is_still_listed_so_it_can_be_turned_back_on(home):
    agentcfg.apply("disable", "grok", home=home)
    row = next(r for r in agents.inventory(home) if r.name == "grok")
    assert row.state == agents.DISABLED
    assert row.executable == "grok", "the built-in binary still shows, for context"


def test_enable_removes_the_file_when_it_leaves_nothing_behind(home):
    agentcfg.apply("disable", "grok", home=home)
    change = agentcfg.apply("enable", "grok", home=home)
    assert change.verb == "enabled"
    assert "grok" in change.names
    # An empty `agents` object is rejected on read, so "no overrides" has to
    # be spelled as "no file" — not as a file holding an empty object.
    assert not registry(home).exists()


def test_set_adds_a_new_agent(home):
    change = agentcfg.apply("set", "minimax", MINIMAX, home=home)
    assert change.verb == "added"
    assert names(home)[-1] == "minimax"
    assert entries(home)["minimax"]["args"] == ["--prompt", "{prompt}", "--yes"]


def test_set_overrides_only_the_fields_it_names(home):
    agentcfg.apply("set", "claude", ["executable=/opt/claude"], home=home)
    assert entries(home) == {"claude": {"executable": "/opt/claude"}}
    row = next(r for r in agents.inventory(home) if r.name == "claude")
    assert (row.executable, row.state) == ("/opt/claude", agents.OVERRIDDEN)


def test_reset_reverts_a_built_in_but_removes_an_added_one(home):
    agentcfg.apply("set", "claude", ["executable=/opt/claude"], home=home)
    agentcfg.apply("set", "minimax", MINIMAX, home=home)

    assert agentcfg.apply("reset", "claude", home=home).verb == "reset"
    assert "claude" in names(home)

    assert agentcfg.apply("reset", "minimax", home=home).verb == "removed"
    assert "minimax" not in names(home)
    assert not registry(home).exists()


# ── a refused edit changes nothing ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("verb", "name", "fields"),
    [
        ("set", "minimax", ["args=--yes"]),  # no {prompt}
        ("set", "minimax", ["skills_dir=../../etc"]),  # escapes config_dir
        ("set", "minimax", ["config_dir=relative/path"]),  # not ~/ or absolute
        ("set", "minimax", ["bogus=1"]),  # not a field
        ("set", "minimax", ["executable="]),  # empty
        ("set", "minimax", []),  # nothing to set
        ("disable", "nope", []),  # unknown agent
        ("frobnicate", "grok", []),  # unknown verb
    ],
)
def test_a_refused_edit_leaves_the_file_byte_identical(home, verb, name, fields):
    agentcfg.apply("set", "minimax", MINIMAX, home=home)
    before = digest(registry(home))
    with pytest.raises(agentcfg.AgentEditError):
        agentcfg.apply(verb, name, fields, home=home)
    assert digest(registry(home)) == before


def test_the_last_agent_cannot_be_disabled(home):
    every = names(home)
    for name in every[:-1]:
        agentcfg.apply("disable", name, home=home)
    before = digest(registry(home))
    with pytest.raises(agentcfg.AgentEditError, match="last agent"):
        agentcfg.apply("disable", every[-1], home=home)
    assert digest(registry(home)) == before
    assert agents.AGENTS, "an empty roster would leave aicp with nothing to run"


def test_a_malformed_user_file_is_refused_rather_than_overwritten(home):
    registry(home).parent.mkdir(parents=True, exist_ok=True)
    registry(home).write_text("not json", encoding="utf-8")
    with pytest.raises(agentcfg.AgentEditError, match="unreadable"):
        agentcfg.apply("disable", "grok", home=home)
    assert registry(home).read_text(encoding="utf-8") == "not json"


def test_setting_fields_on_a_disabled_agent_is_refused_not_silently_ignored(home):
    """``disabled`` drops the name before any field is read, so an accepted
    ``set`` here would look like it worked and do nothing."""
    agentcfg.apply("disable", "grok", home=home)
    with pytest.raises(agentcfg.AgentEditError, match="enable it first"):
        agentcfg.apply("set", "grok", ["executable=/opt/grok"], home=home)


# ── the registry and the saved CLI order stay in step ────────────────────────


def test_disabling_prunes_the_name_from_the_saved_cli_order(home, cfg):
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"aicp_cli_order": "grok claude copilot"}), encoding="utf-8")
    change = agentcfg.apply("disable", "grok", home=home, config_path=cfg)
    assert change.pruned == ("grok",)
    assert config.load_config(cfg)["AICP_CLI_ORDER"] == "claude copilot"


def test_disabling_keeps_the_rest_of_a_hand_picked_order(home, cfg):
    """Without the prune, ``resolve_cli_chain`` rejects the whole line for one
    unknown name and falls back to roster order — so "disable grok" would
    quietly undo a deliberately reordered chain."""
    cfg.parent.mkdir(parents=True, exist_ok=True)
    picked = "grok claude copilot agy codex vibe"
    cfg.write_text(json.dumps({"aicp_cli_order": picked}), encoding="utf-8")
    agentcfg.apply("disable", "grok", home=home, config_path=cfg)
    saved = config.load_config(cfg)["AICP_CLI_ORDER"]
    assert saved.split() == [n for n in picked.split() if n != "grok"]
    assert saved.split()[0] == "claude", "the user's #1 pick survived"


def test_an_order_with_no_stale_name_is_left_alone(home, cfg):
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"aicp_cli_order": "claude copilot"}), encoding="utf-8")
    before = digest(cfg)
    assert agentcfg.apply("disable", "grok", home=home, config_path=cfg).pruned == ()
    assert digest(cfg) == before


# ── the CLI surface ──────────────────────────────────────────────────────────


def test_agents_lists_every_built_in_agent(capsys):
    assert main(["--agents"]) == 0
    out = capsys.readouterr().out
    for name in ("copilot", "agy", "codex", "claude", "vibe", "grok"):
        assert name in out
    assert "built-in" in out


def test_the_bare_word_form_works_like_the_dashed_one(home, capsys):
    assert main(["agents", "disable", "grok"]) == 0
    assert "grok" in capsys.readouterr().out
    assert entries(home) == {"grok": {"disabled": True}}


def test_an_operand_is_never_rewritten_into_a_flag(home, capsys):
    """``aicp agents disable json`` must reach the verb with "json" intact —
    the bare-word convenience only fills in a *missing* ``--``, and stops at
    the first word that names no flag."""
    main(["agents", "set", "json", *MINIMAX])
    capsys.readouterr()
    assert main(["agents", "disable", "json"]) == 0
    assert "json" in entries(home)
    assert entries(home)["json"]["disabled"] is True


def test_a_bad_verb_exits_nonzero_and_writes_nothing(home, capsys):
    assert main(["--agents", "frobnicate", "grok"]) == 1
    assert "frobnicate" in capsys.readouterr().err
    assert not registry(home).exists()


def test_a_verb_without_a_name_exits_nonzero(home, capsys):
    assert main(["--agents", "disable"]) == 1
    assert "name" in capsys.readouterr().err
    assert not registry(home).exists()


def test_a_refused_cli_edit_reports_on_stderr_and_exits_one(home, capsys):
    assert main(["--agents", "set", "claude", "args=--yes"]) == 1
    assert "{prompt}" in capsys.readouterr().err, "the error has to name what was wrong"
    assert not registry(home).exists()
