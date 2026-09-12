# Changelog

All notable changes to this project are documented in this file, and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-13

First release. This is a Python port of the zsh `aicp` — a personal tool
that has lived at `~/scripts/bin/aicp` — not a new tool: the fallback-chain
order, the timeout/budget formula, the secret-scan patterns, and the
git-verified result summary all carry over from that original, re-tested
against a real pytest suite instead of `test_aicp.sh`.

### 🚀 Features

- Fallback chain of five AI CLIs — `copilot` → `agy` → `codex` → `claude` →
  `vibe` — invoked with the literal prompts `/commit` and `/safe-git-push`;
  the first CLI to exit 0 wins, missing ones are skipped.
- Git-verified result summary: every figure (new commits, ahead/behind,
  in-sync status) is read back from `git` after the CLIs run, never taken
  from a CLI's own exit code or output. A failed `git fetch` is never read
  as "already in sync."
- Pre-commit secret scan (seven fixed patterns, no entropy heuristic) that
  covers the pending diff, binary-marked tracked files, and every untracked
  file — since `/commit` stages untracked files itself, before any AI CLI is
  invoked. A hit aborts the run and only ever prints `file:line` plus the
  pattern's name.
- `--undo`: `git reset --soft HEAD^` on the last commit, refusing outright
  (HEAD untouched, no AI CLI called) on a detached HEAD, no parent commit, an
  already-pushed commit, or a remote that can't be verified.
- Cross-platform skills installer for `/commit` and `/safe-git-push`, keyed
  on each CLI's own config directory rather than its binary name (the `agy`
  binary reads `~/.gemini`, not `~/.agy`). A file with no aicp version
  marker is always treated as the user's own and left alone unless a forced
  install explicitly asks to replace it, backing the original up first.
- Per-CLI timeout budget: a floor that grows with the size of the pending
  change, widened (never shrunk) by that CLI's own run history, with a
  hard override and a ceiling.
- `.aicprc` configuration layer: parsed line by line, never sourced or
  eval'd, with a key allowlist, a value charset allowlist, and two knobs
  (`AICP_TG_SEND`, `AICP_TIMING_LOG`) refused from the file entirely because
  each one names a path that later gets executed or written.
- `en` / `zh-TW` message catalogue, resolved via `AICP_LANG`.
- Telegram notification on a per-CLI timeout, degrading to a printed line
  when no send script is configured — never raises, never holds up a run
  that already succeeded.
- Cross-platform process handling: Ctrl+C reaches a running CLI directly on
  POSIX, and is forwarded as `CTRL_BREAK_EVENT` on Windows, where every AI
  CLI's npm `.cmd`/`.bat` shim is resolved and launched through `cmd.exe`
  explicitly rather than relying on `CreateProcess`'s `PATHEXT` handling
  (which doesn't cover batch shims at all).

### 🧪 Testing

- pytest suite covering the config loader, the skills installer (against a
  faked `$HOME`, never the real one), `--undo`'s four refusals, the secret
  scanner, the timeout budget formula, and the `--config` settings menu's
  both surfaces (arrow-key TUI and the numbered fallback CI actually drives).
- CI runs the full suite, `ruff check`, and a packaged-wheel smoke test on
  macOS, Linux, and Windows alike.

### 📚 Documentation

- README covering install, the skills setup step and why it exists, the
  two-command surface (`aicp`, `aicp --config`), `--undo`, the automation
  escape hatches (`--doctor --json`, `--install-skills --yes`/`--force`),
  every `AICP_*` configuration knob, the secret scan, and platform notes.
- `.aicprc.example` with every real knob, commented out, matching its actual
  default.
