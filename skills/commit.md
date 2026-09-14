---
description: Stage and commit all changes, grouped into logical Conventional Commits batches.
---

# Commit

Git commit current changes, split into logical batches if needed.

## Project override (check FIRST)

Skills/custom commands resolve **personal-over-project** on a name clash, so
this global `/commit` shadows a project's own `.claude/commands/commit.md`. The
global CLAUDE.md "same-name precedence" rule already requires deferring to the
project version when one exists — honor it: if `.claude/commands/commit.md`
exists in the project, READ it and follow it instead of this file (pass
`$ARGUMENTS` through unchanged), and skip the rest of this file.

Only when the project has **no** `commit.md` of its own: if `$ARGUMENTS` looks
like an issue-tracker key (`[A-Z]+-[0-9]+`) or a `.../browse/<KEY>` URL AND the
project defines `.claude/commands/commit-bug.md`, invoke `/commit-bug` with the
original `$ARGUMENTS` and stop; otherwise fall through to the standard flow.

## Workflow

1. Run `git status` + `git diff --stat` to survey all uncommitted changes. Read full
   diffs only where grouping is not already obvious from the path — a file whose
   concern is ambiguous, or one you suspect spans concerns (step 6). Never open the
   full diff of the whole worktree up front.
2. **If any `.jsonl` files appear in the changes (modified, untracked, or staged), notify the user** — list the file(s), then skip them entirely and continue with all other commits as normal. Only perform git operations on `.jsonl` files (commit, discard, etc.) if the user explicitly names the file(s) and requests it.
3. **If `.gitignore` or `.gitignore_global` has any changes (new file, modified, or deleted), commit them alone first** before any other batch.
4. Group remaining changes into atomic batches — one concern per commit (feature code, tests, docs, config, etc.).
5. **Before staging each batch**, check `git status` for any pre-existing staged files (left column `M`/`A`/`D`). If any staged file does not belong to the current batch, run `git restore --staged <file>` to pull it out of the index first — otherwise it will be swept into the commit unintentionally.
6. For each file in a batch, decide the staging strategy:
   - **All hunks belong to the same concern** → `git add <file>` (stage the whole file)
   - **Hunks span multiple concerns** → stage only the relevant hunks via patch:
     1. `git diff <file>` — capture the full diff
     2. Extract only the hunk(s) belonging to this batch (preserve the file header lines and the correct `@@ … @@` context lines)
     3. Write the partial patch to a temp file, then `git apply --cached <tmpfile>`
     4. Commit; repeat for the remaining hunks in subsequent batches

   Then write a compliant commit message and commit.

If all changes form one coherent unit, make one commit. Never bundle unrelated changes.

## Commit Message Format

```text
<type>[(<scope>)]: <description>

[body]

[footer(s)]
```

**Types:** `feat` `fix` `docs` `style` `refactor` `test` `perf` `build` `ci` `chore` `hotfix` `revert`

Pick one from that list — **never invent a new type** (`update`, `remove`, `config`, `improve` are all invalid). No exact match → take the closest: source changed but behavior unchanged → `refactor`; source untouched (deps, generated files, moves, config) → `chore`.

**Hard constraints on `<description>`:** max 50 characters; no trailing period; lowercase.

**Body:** wrap at 72 characters per line.

**Footer:** if the change relates to a tracked issue, add `Closes #<issue>` in the footer.

> **Capitalization conflict:** Conventional Commits uses all-lowercase `description`; the seven-rules style capitalizes the first letter. Default here is lowercase — pick one per project and apply consistently.

## Breaking Change Detection

Before composing each commit message, scan the staged diff. Mark the commit with `!` after the type/scope **and** add a `BREAKING CHANGE:` footer if **any** of these apply:

- **Schema rename/removal** — YAML/JSON/proto/GraphQL/TS-type fields renamed, removed, or restructured in a file other code/agents/scripts parse.
- **Public API removal/rename** — exported function, class, slash command, agent, skill, plugin entry, or event template deleted or renamed.
- **Required input change** — optional → required, or required param type changes incompatibly.
- **Output format change** — stdout, return value, file format, or notification event payload changes in a way callers must adapt to.
- **Config restructure** — `settings.json` / `manifest.txt` / `.env` keys renamed or removed when downstream consumers depend on them.

Example:

```text
refactor(scope)!: short description

Body explaining the change.

BREAKING CHANGE: <one-line consequence summary>
<details if needed; wrap at 72 chars>
```

If ambiguous, **ask the user** before committing: *"Is this change breaking for callers? (yes/no)"*
