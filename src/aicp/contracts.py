"""aicp's FROZEN shared interface — the contract T2-T5 build against.

This module is scaffolded once (by the task that ports the presentation layer
and test harness) and then frozen: four downstream tasks start in parallel
immediately afterward and cannot ask its author anything, so every shape here
is deliberate and every consumer is named. Changing a name or shape here
after that point is a breaking change to all four.

Ported from the zsh original at ``~/scripts/bin/aicp`` (see
``AICP_CLI_ROSTER`` there for the roster this mirrors) and its sibling
``~/scripts/test_aicp.sh`` (``ALL_CLIS``) for the fallback-chain order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .agents import AGENTS

__all__ = [
    "CLI",
    "ROSTER",
    "SKILL_VERSION_MARKER",
    "SKILL_VERSION_SUFFIX",
    "NotifyFn",
]


@dataclass(frozen=True)
class CLI:
    """One entry in aicp's fallback chain of AI CLIs.

    Consumer: T2 (the runner that invokes each CLI in turn) and T3 (whatever
    resolves/persists the active chain order, e.g. ``--swap-ai``/``--config``
    equivalents) both key off ``name``; T2 additionally needs ``config_dir``
    to detect whether a CLI is configured at all before trying to run it.

    ``name`` is the stable agent ID; agents.json supplies its executable.
    ``config_dir`` is where that CLI keeps its own config/session state — and
    is deliberately a SEPARATE field, not derived from ``name``, because it
    does not always match: the ``agy`` binary (this project's wrapper name
    for Google's Gemini CLI) reads its configuration from ``~/.gemini``, NOT
    from ``~/.agy`` (which does not exist on disk at all). Never assume
    ``config_dir == Path.home() / f".{name}"`` — always read the field.
    """

    name: str
    config_dir: Path


# Default fallback order, based on AICP_CLI_ROSTER in the current
# ~/scripts/bin/aicp (copilot agy codex claude vibe), with Grok appended for
# saved-chain compatibility. T3 is the consumer that reorders/persists a
# chain derived from this; T2 and T4 must never hardcode this tuple themselves
# — import it.
# Resolve defaults here; environment overrides stay live in config_root.
ROSTER: tuple[CLI, ...] = tuple(
    CLI(name=name, config_dir=agent.config_root(Path.home()))
    for name, agent in AGENTS.items()
)

# The resolved fallback chain — whatever order a run will actually try its
# CLIs in — is passed between modules as a plain Sequence[str] of binary
# names (a subset/permutation of {c.name for c in ROSTER}), e.g.
# ("copilot", "agy", "codex", "claude", "vibe") or a user-reordered subset.
#
#   T3 PRODUCES it   — resolves AICP_CLI_ORDER / .aicprc / --swap-ai state
#                       against ROSTER into this concrete Sequence[str].
#   T2 CONSUMES it    — the runner's fallback loop iterates it as a plain
#                       parameter (e.g. `def run(chain: Sequence[str], ...)`).
#   T4 CONSUMES it    — anything reporting/naming "which CLI is active" (e.g.
#                       a "chain: copilot -> agy -> ..." status line) takes it
#                       as a parameter too.
#
# Never reach into a config object for it (no `config.cli_chain`, no
# `settings.chain`) — every consumer takes it as an explicit argument, so it
# can be constructed fresh in a test with no config machinery at all.


# Injected notifier: whatever "tell the user" mechanism a run wants to use
# (Telegram today, something else later) is passed in as a plain callable,
# never imported directly.
#
# Consumer: T2's runner (and anything else that needs to notify mid-run, e.g.
# on a per-CLI timeout) accepts `notify: NotifyFn = _noop` as a FUNCTION
# PARAMETER with a no-op default — it must never `import aicp.notify` (T4's
# module) itself. T4 owns the real implementation (Telegram send, or the
# printed-fallback path when that's unavailable) and passes it in at the call
# site (e.g. the CLI entry point in cli.py wires T4's notifier into T2's
# runner). This keeps T2 testable with zero network/subprocess dependencies
# and keeps T2 and T4 decoupled in both directions.
NotifyFn = Callable[[str], None]


# ── installed-skill bookkeeping ──────────────────────────────────────────────
#
# aicp records what it installed in ONE file of its own, and writes nothing
# else anywhere near a CLI's config dir:
#
#   $HOME/.aicp/state.json
#   {
#     "version": 1,
#     "skills": {
#       "<absolute install target>": {
#         "version": "<aicp.__version__ at install time>",
#         "files": {"<path relative to the target>": "<sha256 hex>", ...}
#       }
#     }
#   }
#
# A single-file skill records its one file under the key ".". The state file
# is keyed on $HOME alone — never on GROK_HOME or any per-CLI root — because
# it is one record per user covering every target across every CLI.
#
# Consumer: T5 (the skills installer). Two properties matter, and they are
# why the record stores HASHES rather than just a version:
#
#   * "is this ours?"       — a target with no record, whose content does not
#                             match what this version would write, is the
#                             user's. Never overwrite it without force.
#   * "is it still ours?"   — a target we DID install, whose recorded files no
#                             longer hash to what we wrote, has been edited by
#                             hand since. It is treated as the user's too. A
#                             version-only marker cannot see this, which is
#                             exactly how a hand-edited skill used to get
#                             silently displaced on the next version bump.
#
# Only recorded files are hashed, so anything the user ADDS alongside them
# (an extra script, a .DS_Store, a .bak) is not mistaken for a modification.
STATE_DIR_NAME = ".aicp"  # under $HOME
STATE_FILE_NAME = "state.json"
STATE_SCHEMA_VERSION = 1

# LEGACY — aicp <= 0.1.0 wrote an in-place sidecar next to every installed
# skill instead of the state file above: "<path>.aicp-version" for a
# single-file skill, "<dir>/.aicp-version" at a directory skill's own root,
# containing one line "x-aicp-version: <version>\n". These names survive only
# so the installer can find such a marker, fold it into state.json, and DELETE
# it. Nothing writes them any more; do not reintroduce one.
SKILL_VERSION_SUFFIX = ".aicp-version"  # legacy single-file: "<path>" + this suffix
SKILL_VERSION_MARKER = ".aicp-version"  # legacy directory: this name at the dir root
