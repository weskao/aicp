---
name: safe-git-push
description: Safely push the current git branch — push first, and only if the remote rejects it fetch and integrate without `git pull`, self-merges, flattened merges, or force-push. Use whenever a `git push` is needed, especially on shared branches (develop/main).
---

# Safe Git Push

Plain push (never forced, so the server refuses anything non-fast-forward) →
only if rejected: fetch, compare local vs. remote by ancestry, integrate if it
can be done without inventing a self-merge or flattening a real merge, push
again. The common case (local ahead or first push) is one network round trip.
All of that logic lives in `scripts/safe_push.py`; this file just explains when
each branch of the decision runs and what to do with the result.

## Run it

`scripts/safe_push.py` ships next to this `SKILL.md`, not in the repo being
pushed. Run it by the absolute path of this skill's folder, from the repo:

```bash
python3 <this-skill-dir>/scripts/safe_push.py
# ambiguous remote (no upstream set, multiple remotes exist):
python3 <this-skill-dir>/scripts/safe_push.py --remote origin
```

If the script is missing there, stop and report it. Never write a replacement
script into the repo.

Exit code `0` = pushed, fast-forwarded, or already in sync (message says
which). A successful push is confirmed by git's own per-ref server report
(exit 0), so there's no extra `ls-remote` round trip. Exit code `1` = stopped
without touching anything — read the `Safe push aborted: <reason>` line on
stderr and handle it by hand.

## Decision summary

| Situation | What the script does |
|---|---|
| Remote branch doesn't exist yet | First push, `--set-upstream` |
| Local == remote | Push is a no-op, reports "already synchronized" |
| Local strictly ahead | Push accepted directly, no fetch, no rebase/merge |
| Local strictly behind (push rejected) | Fetch, `git merge --ff-only`, then nothing to push |
| Rejected, diverged, local history has a likely self-merge not yet pushed | **Stops** — won't guess whether to keep or drop it |
| Rejected, diverged, otherwise | Backs up HEAD to a throwaway ref, `git rebase --rebase-merges=no-rebase-cousins <remote>` (preserves real cross-branch merges instead of flattening them), verifies the merge count and ancestry didn't change, then pushes |
| Rebase conflicts | Aborts the rebase, restores the branch, reports the conflicting files, stops |
| Push rejected because the remote moved again | Re-fetches and re-analyzes, up to 2 retries, never force-pushes |
| Push rejected for any other reason (e.g. a server hook declined it) | Stops with the remote's message — retrying wouldn't help |
| Remote says protected branch / permission denied | Stops, tells you to use the normal PR/MR flow |

Never done, under any circumstance: `git pull`, a plain `git merge <remote-ref>`,
`git push --force`/`-f`/`--force-with-lease`/`--all`/`--mirror`/`--tags`/`--no-verify`,
`git reset --hard` on anything but the script's own just-created backup ref,
`git stash`/`clean`/`commit`, or switching branches. If any of those turns out
to be genuinely necessary, that's a deliberate follow-up task for you to run
yourself, not something this skill does silently.

## Known gaps (not automated — call out if they matter)

- Doesn't verify a submodule gitlink you're pushing already exists on the
  submodule's own remote. Rare; check manually if the diff touches submodules.
- Doesn't try to auto-resolve rebase conflicts — always stops and hands them
  back. Automating "safe" conflict resolution is exactly the kind of guess
  this skill exists to avoid making.
- Signed commits: rebasing recreates commits, so if `commit.gpgsign` is on,
  git re-signs them as part of the rebase; the script doesn't add anything
  beyond leaving that setting alone.
