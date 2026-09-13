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

    ``name`` is the literal binary invoked on PATH (``shutil.which(name)``).
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
ROSTER: tuple[CLI, ...] = (
    CLI(name="copilot", config_dir=Path.home() / ".copilot"),
    CLI(name="agy", config_dir=Path.home() / ".gemini"),  # the trap — see CLI's docstring
    CLI(name="codex", config_dir=Path.home() / ".codex"),
    CLI(name="claude", config_dir=Path.home() / ".claude"),
    CLI(name="vibe", config_dir=Path.home() / ".vibe"),
    CLI(name="grok", config_dir=Path.home() / ".grok"),
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


# ── vendored-skill version marker ────────────────────────────────────────────
#
# skills/commit.md and skills/safe-git-push/ are BYTE-IDENTICAL copies of
# ~/.claude/commands/commit.md and ~/.claude/skills/safe-git-push/ — no marker
# is ever written INTO those files, since that would make them stop being
# byte-identical to the source they were vendored from.
#
# Instead, the marker is a SIDECAR file placed next to each vendored copy:
#   - a single-file skill (commit.md)      -> "<path>.aicp-version" next to it,
#     e.g. skills/commit.md.aicp-version
#   - a directory skill (safe-git-push/)   -> "<dir>/.aicp-version" at the
#     skill directory's own root, e.g. skills/safe-git-push/.aicp-version
#     (covers every file vendored under that directory as one unit — it is
#     one vendoring operation, not one marker per file inside it)
#
# Sidecar content is exactly one line: "x-aicp-version: <version>\n", where
# <version> is this package's own version (see aicp.__version__ / the
# [project].version key in pyproject.toml at vendor time).
#
# Consumer: T5 (the skills installer) implements the detector — given a
# candidate install target, check for a sidecar named per the two rules
# above; if present, parse the `x-aicp-version: (\S+)` line to decide whether
# an already-installed copy came from aicp and, if so, which version, before
# deciding whether to overwrite it. No sidecar present means "not ours, or a
# hand-edited copy" — never overwrite silently in that case.
SKILL_VERSION_SUFFIX = ".aicp-version"  # single-file skill: "<path>" + this suffix
SKILL_VERSION_MARKER = ".aicp-version"  # directory skill: this filename at the dir root
