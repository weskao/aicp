"""aicp — AI commit + push, with a git-verified result summary.

Python port of ``~/scripts/bin/aicp``. See ``aicp.contracts`` for the frozen
interface shared across this package's modules.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # The distribution name, not the import name — these differ: `aicp` was
    # taken on PyPI in 2021 by an unrelated project. Reading it from installed
    # metadata keeps pyproject.toml the single source of truth, so the version
    # in `--version` and the `--config` title can never drift from the release.
    __version__ = version("aicp-cli")
except PackageNotFoundError:  # pragma: no cover - unbuilt checkout
    __version__ = "0+unknown"
