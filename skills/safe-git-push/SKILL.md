---
name: safe-git-push
description: Safely synchronize and push the current git branch. Always fetches before pushing, never uses `git pull`, never creates an accidental self-merge like "Merge remote-tracking branch 'origin/develop' into develop", preserves intentional cross-branch merge topology instead of flattening it with a blind rebase, and never force-pushes or rewrites shared history. Use this whenever a `git push` is needed, especially on a shared branch (develop/main) or after the remote may have moved.
---

# Safe Git Push

Fetch → compare local vs. remote by ancestry → integrate only if it can be
done without inventing a self-merge or flattening a real merge → push. All
of that logic lives in `scripts/safe_push.py`; this file just explains when
each branch of the decision runs and what to do with the result.

## Run it

```bash
python3 scripts/safe_push.py
# ambiguous remote (no upstream set, multiple remotes exist):
python3 scripts/safe_push.py --remote origin
```

Exit code `0` = pushed, fast-forwarded, or already in sync (message says
which). Exit code `1` = stopped without touching anything — read the
`Safe push aborted: <reason>` line on stderr and handle it by hand.

## Decision summary

| Situation after fetch | What the script does |
|---|---|
| Remote branch doesn't exist yet | First push, `--set-upstream` |
| Local == remote | Nothing to push, reports and exits |
| Local strictly ahead | Push directly, no rebase/merge |
| Local strictly behind | `git merge --ff-only`, then nothing to push |
| Diverged, local history has a likely self-merge not yet pushed | **Stops** — won't guess whether to keep or drop it |
| Diverged, otherwise | Backs up HEAD to a throwaway ref, `git rebase --rebase-merges=no-rebase-cousins <remote>` (preserves real cross-branch merges instead of flattening them), verifies the merge count and ancestry didn't change, then pushes |
| Rebase conflicts | Aborts the rebase, restores the branch, reports the conflicting files, stops |
| Push rejected (someone else pushed in between) | Re-fetches and re-analyzes, up to 2 retries, never force-pushes |
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
