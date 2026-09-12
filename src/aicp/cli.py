"""Command-line entry point (``aicp = "aicp.cli:main"`` in pyproject.toml).

Scaffolding placeholder: the fallback-chain runner, secret scanner, and
result-summary logic land in follow-up tasks (see ``aicp.contracts`` for the
frozen interface they build against). This stub only has to exist so the
console script installs and ``aicp --help`` / ``aicp --version`` work end to
end for the CI smoke test.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aicp",
        description="AI commit + push, with a git-verified result summary.",
    )
    parser.add_argument("--version", action="version", version=f"aicp {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    print(
        "aicp: not yet implemented in this port — see TODO.md and contracts.py",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
