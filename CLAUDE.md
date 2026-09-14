# CLAUDE.md

## Cross-platform compatibility

Keep tools platform-agnostic. OS-specific behavior (clipboard, package installation, ANSI) belongs in a shared helper, funneled through one function, so individual tool modules stay clean.

Support macOS, Windows, and Linux for user-facing platform behavior.
