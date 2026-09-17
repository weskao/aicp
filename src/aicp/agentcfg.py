"""Editing ``~/.aicp/agents.json`` — the write side of :mod:`aicp.agents`.

Kept out of that module on purpose. Every aicp run imports ``agents`` (the
roster is built from it at import time) and nothing here runs unless
``--agents`` or the config menu's Agents row is used; separating them also
keeps ``agents`` free of an :mod:`aicp.config` import, which would be a cycle
— config's roster comes from agents.

**Nothing reaches disk until the whole candidate registry has passed the same
validation the next run will do.** A refused edit leaves the previous file
byte-identical, so a typo can never leave aicp unable to start.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path

from . import agents, config

__all__ = ["EDITABLE", "STATE_LABELS", "VERBS", "AgentEditError", "Change", "apply"]


class AgentEditError(ValueError):
    """A refused edit. Nothing was written."""


#: Which of the settable fields are comma-separated lists.
LIST_FIELDS = ("skills", "args")
#: Fields ``set`` accepts — read off :class:`~aicp.agents.Agent` rather than
#: listed again, so a field added there is settable the same day. ``disabled``
#: is not one of them: it is what the enable/disable verbs write.
EDITABLE = tuple(field.name for field in fields(agents.Agent))

VERBS = ("list", "enable", "disable", "set", "reset")

#: Display text per :func:`aicp.agents.inventory` state, as ``(msgid,
#: english)`` — the menu translates with its live language, the CLI with the
#: resolved one, both off this single table.
STATE_LABELS = {
    agents.BUILT_IN: ("agents_state_builtin", "built-in"),
    agents.OVERRIDDEN: ("agents_state_overridden", "overridden"),
    agents.ADDED: ("agents_state_added", "added"),
    agents.DISABLED: ("agents_state_disabled", "disabled"),
}


@dataclass(frozen=True)
class Change:
    """What :func:`apply` did."""

    name: str
    #: ``enabled`` / ``disabled`` / ``updated`` / ``reset`` / ``removed``.
    #: ``reset`` reverts to a built-in definition; ``removed`` is the same
    #: verb applied to an agent that only ever existed in the user file.
    verb: str
    #: The registry's names after the change, in resolution order.
    names: tuple[str, ...]
    #: Names dropped from a saved ``AICP_CLI_ORDER`` to keep it loadable.
    pruned: tuple[str, ...]


def parse_fields(assignments: Sequence[str]) -> dict[str, object]:
    """``["executable=x", "args=--p,{prompt}"]`` -> a registry entry fragment."""
    parsed: dict[str, object] = {}
    for item in assignments:
        key, sep, raw = item.partition("=")
        if not sep or key not in EDITABLE:
            raise AgentEditError(
                f"expected key=value, key one of {', '.join(EDITABLE)} — got: {item}"
            )
        # A list field splits on commas, so a value that needs a literal comma
        # has to be written into the file by hand. Nothing in a CLI invocation
        # or a skill name has ever needed one.
        parsed[key] = [part for part in raw.split(",") if part] if key in LIST_FIELDS else raw
    return parsed


def _prune_chain(names: tuple[str, ...], config_path: Path | str | None) -> tuple[str, ...]:
    """Drop names the registry no longer has from a saved ``AICP_CLI_ORDER``.

    Without this, disabling an agent leaves an unknown name in the saved
    order and :func:`aicp.config.resolve_cli_chain` refuses the whole line —
    silently resetting a hand-picked fallback order to the default roster.
    """
    saved = config.load_config(config_path).get("AICP_CLI_ORDER", "")
    listed = saved.split()
    dropped = tuple(name for name in listed if name not in names)
    if dropped:
        config.persist_key(
            "AICP_CLI_ORDER", " ".join(n for n in listed if n in names), config_path
        )
    return dropped


def apply(
    verb: str,
    name: str,
    assignments: Sequence[str] = (),
    *,
    home: Path | None = None,
    config_path: Path | str | None = None,
) -> Change:
    """Run *verb* against ``~/.aicp/agents.json`` and report what changed.

    ``enable`` clears a ``disabled`` flag, ``disable`` sets one, ``set``
    merges ``key=value`` fields, ``reset`` drops the whole user entry.
    """
    if verb not in VERBS or verb == "list":
        raise AgentEditError(f"unknown agents verb: {verb}")
    path = agents.user_registry_path(home)
    try:
        entries = agents.read_user_entries(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AgentEditError(f"{path} is unreadable ({exc}) — fix or delete it first") from exc

    builtin = agents.load_agents(agents.BUILTIN_PATH)
    known = {*builtin, *entries}
    if verb != "set" and name not in known:
        raise AgentEditError(f"unknown agent: {name} (known: {' '.join(sorted(known))})")

    candidate = {key: dict(value) for key, value in entries.items()}
    entry = candidate.setdefault(name, {})
    if verb == "disable":
        entry["disabled"] = True
    elif verb == "enable":
        entry.pop("disabled", None)
    elif verb == "set":
        if not assignments:
            raise AgentEditError(f"set needs at least one key=value for {name}")
        # Editing a disabled agent's fields would otherwise look like it did
        # nothing: `disabled` drops the name before any of them are read.
        if entry.get("disabled") is True:
            raise AgentEditError(f"{name} is disabled — enable it first, then set its fields")
        entry.update(parse_fields(assignments))
    else:  # reset
        candidate.pop(name)
    # An entry that says nothing is not an override; leaving it behind would
    # also crash the loader, which requires a full definition for a name the
    # built-in registry has never heard of.
    if not candidate.get(name, {"x": 1}):
        candidate.pop(name)

    try:
        merged = agents.merge_entries(builtin, candidate, path)
    except (ValueError, TypeError) as exc:
        raise AgentEditError(str(exc)) from exc
    if not merged:
        raise AgentEditError(f"refusing to disable {name}: it is the last agent left")

    if candidate:
        if not config._write_json_private(path, {"version": 1, "agents": candidate}):
            raise AgentEditError(f"failed to write {path}")
    else:
        # An empty `agents` object is rejected on read, so "no overrides left"
        # has to be spelled as "no file" rather than an empty one.
        path.unlink(missing_ok=True)

    names = tuple(merged)
    if verb == "reset":
        applied = "reset" if name in merged else "removed"
    elif verb == "set":
        applied = "updated" if name in known else "added"
    else:
        applied = {"enable": "enabled", "disable": "disabled"}[verb]
    return Change(name, applied, names, _prune_chain(names, config_path))
