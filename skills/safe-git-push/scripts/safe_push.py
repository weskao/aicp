#!/usr/bin/env python3
"""Safe git push: fetch, analyze the graph, integrate only when it can't
create an accidental self-merge or flatten intentional cross-branch merges,
then push. See ../SKILL.md for the decision summary.

Exit code 0 = pushed or already in sync. Exit code 1 = stopped (see stderr).
Never uses: git pull, git merge <remote-ref> (plain), git push --force*/--all/
--mirror/--tags/--no-verify, git reset --hard on anything but our own backup
ref, git stash/clean/commit, or a branch/checkout switch.
"""
import argparse
import os
import re
import subprocess
import sys
import time

MAX_PUSH_RETRIES = 2
PROTECTED_MARKERS = ("protected branch", "permission denied", "not allowed to push")
# The remote moved after our fetch; any other rejection (hook decline, etc.) won't fix itself.
RACE_MARKERS = ("non-fast-forward", "fetch first", "cannot lock ref")
IN_PROGRESS = {"rebase-merge": "rebase (merge)", "rebase-apply": "rebase (apply)", "MERGE_HEAD": "merge",
               "CHERRY_PICK_HEAD": "cherry-pick", "REVERT_HEAD": "revert", "BISECT_LOG": "bisect"}


class Retry(Exception):
    pass


def git(*args, check=True):
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"$ git {' '.join(args)}\n{p.stderr.strip()}")
    return p


def out(*args):
    return git(*args).stdout.strip()


def stop(msg):
    print(f"Safe push aborted: {msg}", file=sys.stderr)
    print("No destructive git operation was performed; no shared history was rewritten.", file=sys.stderr)
    sys.exit(1)


def check_preconditions():
    p = git("rev-parse", "--is-inside-work-tree", "--absolute-git-dir", "--is-shallow-repository", check=False)
    info = p.stdout.split("\n")
    if p.returncode != 0 or info[0] != "true":
        stop("not inside a git work tree.")
    gd, shallow = info[1], info[2]

    busy = [label for name, label in IN_PROGRESS.items() if os.path.exists(os.path.join(gd, name))]
    if busy:
        stop(f"a {busy[0]} is already in progress. Resolve or abort it first.")

    p = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if p.returncode != 0:
        stop("HEAD is detached; cannot safely guess which branch to push.")
    branch = p.stdout.strip()

    # -uno: skip the untracked-file scan (slow on big trees); untracked files can't be pushed anyway.
    if out("status", "--porcelain", "--untracked-files=no"):
        print("Note: working tree has uncommitted changes; they are never included "
              "in a push and are left untouched.", file=sys.stderr)
    if shallow == "true":
        print("Note: shallow repository; ancestry checks below may be incomplete.", file=sys.stderr)
    return branch, gd


def resolve_remote(branch, remote_override):
    if remote_override:
        return remote_override
    remote = git("config", "--get", f"branch.{branch}.remote", check=False).stdout.strip()
    if remote:
        return remote
    remotes = out("remote").split()
    if len(remotes) == 1:
        return remotes[0]
    if not remotes:
        stop("no git remote configured.")
    stop(f"branch '{branch}' has no upstream and multiple remotes exist ({', '.join(remotes)}); "
         f"rerun with --remote <name>.")


def fetch_branch(remote, branch):
    p = git("fetch", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}", check=False)
    if p.returncode == 0:
        return True  # remote branch exists and was fetched
    if "couldn't find remote ref" in p.stderr or "not found" in p.stderr.lower():
        return False  # remote branch doesn't exist yet -> first push
    stop(f"fetch from '{remote}' failed:\n{p.stderr.strip()}")


def push(remote, branch, set_upstream):
    # Exit 0 means the server accepted HEAD for this exact ref (report-status), so no
    # follow-up ls-remote round trip is needed to verify it.
    p = git("-c", "push.followTags=false", "push", *(["--set-upstream"] if set_upstream else []),
            remote, f"HEAD:refs/heads/{branch}", check=False)
    if p.returncode == 0:
        return out("rev-parse", "HEAD")
    low = p.stderr.lower()
    if any(m in low for m in PROTECTED_MARKERS):
        stop(f"remote rejected the push (protected branch):\n{p.stderr.strip()}\n"
             "Use the repository's normal PR/MR workflow instead.")
    if any(m in low for m in RACE_MARKERS):
        raise Retry(p.stderr.strip())
    stop(f"push failed:\n{p.stderr.strip()}")


def attempt(branch, remote, has_upstream, gd):
    remote_ref = f"{remote}/{branch}"
    if not fetch_branch(remote, branch):
        sha = push(remote, branch, set_upstream=True)
        print(f"Safe push completed (first push).\nBranch: {branch}\nRemote: {remote_ref}\nRemote HEAD: {sha}")
        return

    # One walk replaces two is-ancestor checks: "<only on remote>\t<only local>".
    behind, ahead = map(int, out("rev-list", "--left-right", "--count", f"{remote_ref}...HEAD").split())
    if not behind and not ahead:
        print(f"Already synchronized with {remote_ref}. Nothing to push.")
        return
    if not ahead:
        git("merge", "--ff-only", remote_ref)
        print(f"Fast-forwarded to {remote_ref}. Nothing to push.")
        return
    if behind:
        integrate_diverged(remote_ref, branch, gd)

    sha = push(remote, branch, set_upstream=not has_upstream)
    print(f"Safe push completed.\nBranch: {branch}\nRemote: {remote_ref}\nRemote HEAD: {sha}")


def integrate_diverged(remote_ref, branch, gd):
    b = re.escape(branch)
    self_merge = re.compile(rf"^Merge (remote-tracking )?branch '([^']*/)?{b}'(\s+of\s+\S+)?\s+into\s+{b}$")
    merges = out("log", "--merges", "--format=%H\t%s", f"{remote_ref}..HEAD").splitlines()
    for line in merges:
        sha, _, subject = line.partition("\t")
        if self_merge.match(subject.strip()):
            stop(f"local history contains a likely self-merge ({sha[:10]} \"{subject.strip()}\") "
                 f"not yet pushed. Not touching it automatically — review it by hand.")

    backup_ref = f"refs/backup/safe-push/{branch}-{int(time.time())}"
    git("update-ref", backup_ref, "HEAD")

    if git("rebase", "--rebase-merges=no-rebase-cousins", remote_ref, check=False).returncode != 0:
        conflicts = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.strip()
        git("rebase", "--abort", check=False)
        stop("rebase hit conflicts and was aborted; branch restored to its pre-rebase state.\n"
             f"Conflicting files:\n{conflicts}\nResolve manually, then rerun.")

    ok = (not os.path.isdir(os.path.join(gd, "rebase-merge"))
          and git("merge-base", "--is-ancestor", remote_ref, "HEAD", check=False).returncode == 0
          and int(out("rev-list", "--merges", "--count", f"{remote_ref}..HEAD")) == len(merges))
    if not ok:
        git("reset", "--hard", backup_ref)
        stop("post-rebase verification failed (remote not an ancestor, or merge topology changed); "
             f"restored from backup ref {backup_ref}.")
    git("update-ref", "-d", backup_ref)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remote", help="Override the remote to use (needed when ambiguous).")
    args = ap.parse_args()

    branch, gd = check_preconditions()
    has_upstream = bool(git("config", "--get", f"branch.{branch}.merge", check=False).stdout.strip())
    remote = resolve_remote(branch, args.remote)

    for attempt_no in range(MAX_PUSH_RETRIES + 1):
        try:
            attempt(branch, remote, has_upstream, gd)
            return
        except Retry as e:
            if attempt_no == MAX_PUSH_RETRIES:
                stop(f"push kept getting rejected (remote changed concurrently) after "
                     f"{MAX_PUSH_RETRIES + 1} attempts:\n{e}")
            print(f"Push rejected (remote changed); re-fetching and re-analyzing "
                  f"(attempt {attempt_no + 2}/{MAX_PUSH_RETRIES + 1})...", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        stop(str(e))
