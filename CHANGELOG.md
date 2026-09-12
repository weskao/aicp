# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- Project scaffold (`pyproject.toml`, `uv.lock`, CI/release workflows).
- `aicp.contracts` — the frozen shared interface (`CLI`, `ROSTER`,
  `NotifyFn`, the chain-parameter shape, the vendored-skill version-marker
  mechanism).
- `aicp._utils` — cross-platform helpers, including `run_interruptible`.
- `aicp.i18n` — the `en`/`zh-TW` message mechanism (catalogue itself still
  empty; a later change fills it in).
- `aicp.present` — the spinner and box-render presentation layer, ported
  from `lib/spinner.sh` and `lib/box_render.py`.
- pytest harness (`tests/conftest.py`) and real tests for the presentation
  layer and cross-platform helpers.
- Vendored `skills/commit.md` and `skills/safe-git-push/` (byte-identical
  copies, with `.aicp-version` sidecar markers).
