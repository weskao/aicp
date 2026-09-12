# TODO

## Backlog carried from the zsh original (verbatim, not implemented here)

The three sections below are copied verbatim from items 6-8 of
`~/scripts/AICP_TODO.md` (the zsh `bin/aicp`'s own backlog) — recorded here
as backlog for this Python port, not implemented as part of this scaffold.
References to zsh-specific internals (`_aicp_invoke`, `_ai_run`,
`_aicp_timing_log`, `.zshrc`, `test_aicp.sh`) describe the ORIGINAL shell
tool; when this backlog is picked up in the Python port they translate to
the equivalent Python module/function, not to editing the zsh original.

---

## 6. `grok` as a sixth fallback CLI

xAI's `grok` CLI. Not installed on this machine — binary name, headless-prompt
flag, and non-interactive trust/approval flag (the same class of gotcha as
`vibe --trust` and `codex --dangerously-bypass-approvals-and-sandbox`) are
**unverified**; confirm against `grok --help` before wiring `_aicp_invoke`.

Same shape as §3: add to `AICP_CLI_ROSTER`, add an `_aicp_invoke` case, append
last so today's default order is unchanged. The roster-subset rule §3 already
added for `AICP_CLI_ORDER` needs no further change — any name a saved order
leaves out is appended automatically, so this is nobody's second migration.

**Acceptance**
- [ ] Binary name and both flags above confirmed, not assumed.
- [ ] `grok` appears in `--swap-ai`'s menu and can be moved to #1.
- [ ] Roster grows 5 → 6 (or 4 → 5 if §3 hasn't landed yet); no warning on a
      saved `.aicprc` missing the new name.
- [ ] Every test stub directory stubs `grok` too, so no test can fall through
      to a real `grok` on the machine running the suite.

---

## 7. Surface which CLI ran, and treat quota-exhaustion as its own outcome

### a. Name the chain and the winner
`_ai_run` already prints one line per CLI it tries (✓/⏱/✗ + elapsed), so which
CLI is *currently* running is visible mid-attempt — but nothing prints the
active chain order before the first attempt, and nothing in the final summary
says which CLI actually produced the commit or the push. Add:
- One line before the first `_ai_run` call (in the run panel) naming the
  active chain, e.g. `chain: copilot → agy → codex → claude → vibe`.
- A `Handled by` row in the RESULT table (and the commit panel) naming the
  CLI that returned 0 for `/commit`, and separately for `/safe-git-push` —
  a dash when a step was skipped.

### b. Quota exhaustion as a distinct outcome, not a generic fail
A CLI can exit nonzero mid-task specifically because its usage/quota ran
out — indistinguishable today from any other failure (`outcome=fail` in
`_aicp_timing_log`), and retried from scratch on the very next step even
though it will exhaust again for the rest of the same run. Add:
- Recognize the quota/rate-limit signal each CLI actually emits — exit code
  vs. a stderr pattern, **unverified today for any of the five** — and tag
  that outcome `quota` instead of `fail`.
- A CLI marked `quota` is skipped for the rest of *this run* (both `/commit`
  and `/safe-git-push` share the exclusion; e.g. `/commit` finishing on
  `codex` after `agy` hit quota means `/safe-git-push` skips `agy` too,
  moving straight to the next untried CLI). Resets naturally on the next
  `aicp` invocation — no state persisted to disk.
- A distinct printed line, e.g. `⚠ agy quota/rate-limit exhausted — skipping
  for the rest of this run`, folded into the Telegram notification the same
  way a timeout already is.

**Acceptance**
- [ ] The run panel prints the active chain order before any CLI is
      attempted.
- [ ] RESULT (and the commit panel) name which CLI handled `/commit` and
      which handled `/safe-git-push`.
- [ ] A CLI whose quota/rate-limit is hit mid-`/commit` is not retried during
      the same run's `/safe-git-push` step; that step's chain visibly starts
      at the next untried CLI.
- [ ] The quota case prints its own message and Telegram note, distinct from
      a plain nonzero exit and from a timeout.
- [ ] A CLI that fails for a non-quota reason is unaffected — still retried
      per-step exactly as today.

---

## 8. Knock-on work, not optional

- `README.md` — `aicp` gets `--config`, a fifth CLI, a language knob and two
  new failure messages. The repo rule makes a stale README a bug, so §📋 count,
  §🛠️ detail and §🔔 Telegram all need the edit, in zh-TW.
- `.zshrc` — the alias's `## desc` still says
  `copilot→agy→codex→claude` and `Usage: aicp [-v|--verbose] [--undo]`; it
  already predates `--swap-ai`. Both copies of `.zshrc` change together, in one
  commit, and `zshrcpush` is the user's call.
- `test_aicp.sh` — eight places hardcode the four-CLI list; they become one
  shared `ALL_CLIS`. `--swap-ai` and `AICP_CLI_ORDER` currently have **no test
  coverage at all** (verified: `grep` finds neither in the suite) — the subset
  rule in §3 is a behaviour change to an untested code path, so it gets tests
  now rather than later.

---

## Known defect — Windows timeout orphans the CLI (HIGH, open)

Found during the Windows CI fix (2026-09-13), documented in `runner.py`'s
docstring, deliberately not fixed in that task.

On Windows an npm-installed CLI is a `.cmd` shim, so `_launch_command` runs it
through `%ComSpec%`. That puts an intermediate `cmd.exe` between aicp and the
CLI — and on the budget-timeout path `proc.terminate()` / `proc.kill()` then
hits `cmd.exe`, **not the CLI behind it**. The CLI is orphaned rather than
killed, which contradicts this module's stated "killed by its own budget"
invariant. POSIX is unaffected (no interpreter in between), and the Ctrl+C
path is unaffected on both (`CTRL_BREAK_EVENT` targets the whole group).

A correct fix needs Windows process-tree killing — `taskkill /T /F` or a Job
Object — plus a Windows-only test that spawns a live grandchild and asserts it
is gone. It was left out of the CI-fix task because neither can be verified
without a Windows host, and the risk of destabilising a just-turned-green CI
outweighed shipping it unverified.
