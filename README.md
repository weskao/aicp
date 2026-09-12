# aicp

AI commit + push, with a git-verified result summary. Python port of
`~/scripts/bin/aicp` (the zsh original).

Runs an AI CLI to commit and push: tries a fallback chain of CLIs (copilot →
agy → codex → claude → vibe by default), sending the literal prompts
`/commit` then `/safe-git-push`; the first CLI that exits 0 wins. Every
number in the result summary is read back from git — the CLI's own output is
never trusted, since an AI CLI can exit 0 even when `/safe-git-push` aborted
internally.

## Status

This is an in-progress port. The project scaffold, the frozen shared
interface (`src/aicp/contracts.py`), the presentation layer (spinner +
box-render), cross-platform helpers, and the test harness are in place; the
actual runner, secret scanner, and CLI entry point are follow-up work — see
[`TODO.md`](TODO.md).

## Install (once published)

```sh
uv tool install aicp
```

## Develop

```sh
uv sync --locked --all-groups
uv run pytest
uv run ruff check .
```

## Configuration

Copy [`.aicprc.example`](.aicprc.example) to `~/.aicprc` to set fleet-wide
defaults (fallback CLI order, language, which steps run). Environment
variables always take precedence over `.aicprc`.

## License

MIT — see [`LICENSE`](LICENSE).
