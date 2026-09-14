"""aicp — AI commit + push, with a git-verified result summary.

Python port of ``~/scripts/bin/aicp``. See ``aicp.contracts`` for the frozen
interface shared across this package's modules.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aicp")
except PackageNotFoundError:  # pragma: no cover - unbuilt checkout
    __version__ = "0+unknown"
