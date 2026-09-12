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
import re
import subprocess
import sys
import time

MAX_PUSH_RETRIES = 2


def run(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"$ {' '.join(cmd)}\n{p.stderr.strip()}")
    return p


def run_ok(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def stop(msg):
    print(f"Safe push aborted: {msg}", file=sys.stderr)
    print("No destructive git operation was performed; no shared history was rewritten.", file=sys.stderr)
    sys.exit(1)


def git_dir():
    return run(["git", "rev-parse", "--git-dir"]).stdout.strip()


def check_preconditions():
    if not run_ok(["git", "rev-parse", "--is-inside-work-tree"]):
        stop("not inside a git work tree.")

    gd = git_dir()
    import os
    in_progress = [
        name for name, path in {
            "rebase (merge)": f"{gd}/rebase-merge",
            "rebase (apply)": f"{gd}/rebase-apply",
            "merge": f"{gd}/MERGE_HEAD",
            "cherry-pick": f"{gd}/CHERRY_PICK_HEAD",
            "revert": f"{gd}/REVERT_HEAD",
            "bisect": f"{gd}/BISECT_LOG",
        }.items() if os.path.exists(path)
    ]
    if in_progress:
        stop(f"a {in_progress[0]} is already in progress. Resolve or abort it first.")

    branch_p = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                               capture_output=True, text=True)
    if branch_p.returncode != 0:
        stop("HEAD is detached; cannot safely guess which branch to push.")
    branch = branch_p.stdout.strip()

    dirty = run(["git", "status", "--porcelain"]).stdout.strip()
    if dirty:
        print("Note: working tree has uncommitted changes; they are never included "
              "in a push and are left untouched.", file=sys.stderr)

    if run(["git", "rev-parse", "--is-shallow-repository"]).stdout.strip() == "true":
        print("Note: shallow repository; ancestry checks below may be incomplete.", file=sys.stderr)

    return branch


def resolve_remote(branch, remote_override):
    if remote_override:
        return remote_override
    remote = run(["git", "config", "--get", f"branch.{branch}.remote"], check=False).stdout.strip()
    if remote:
        return remote
    remotes = [r for r in run(["git", "remote"]).stdout.splitlines() if r.strip()]
    if len(remotes) == 1:
        return remotes[0]
    if not remotes:
        stop("no git remote configured.")
    stop(f"branch '{branch}' has no upstream and multiple remotes exist ({', '.join(remotes)}); "
         f"rerun with --remote <name>.")


def fetch_branch(remote, branch):
    p = subprocess.run(
        ["git", "fetch", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
        capture_output=True, text=True,
    )
    if p.returncode == 0:
        return True  # remote branch exists and was fetched
    if "couldn't find remote ref" in p.stderr or "not found" in p.stderr.lower():
        return False  # remote branch doesn't exist yet -> first push
    stop(f"fetch from '{remote}' failed:\n{p.stderr.strip()}")


def is_ancestor(maybe_ancestor, ref):
    return run_ok(["git", "merge-base", "--is-ancestor", maybe_ancestor, ref])


SELF_MERGE_RE = re.compile(
    r"^Merge (remote-tracking )?branch '([^']*/)?{branch}'(\s+of\s+\S+)?\s+into\s+{branch}$"
)


def find_suspicious_self_merge(remote_ref, branch):
    log = run(["git", "log", "--merges", "--format=%H\t%s", f"{remote_ref}..HEAD"]).stdout
    pat = re.compile(SELF_MERGE_RE.pattern.format(branch=re.escape(branch)))
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if pat.match(subject.strip()):
            return sha, subject.strip()
    return None


def push(remote, branch, set_upstream):
    cmd = ["git", "-c", "push.followTags=false", "push"]
    if set_upstream:
        cmd.append("--set-upstream")
    cmd += [remote, f"HEAD:refs/heads/{branch}"]
    return subprocess.run(cmd, capture_output=True, text=True)


def verify_pushed(remote, branch):
    local_sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote_sha = run(["git", "ls-remote", remote, f"refs/heads/{branch}"]).stdout.split()[0]
    if local_sha != remote_sha:
        stop(f"push verification failed: remote is {remote_sha[:10]}, local HEAD is {local_sha[:10]}.")
    return local_sha


def attempt(branch, remote, has_upstream):
    remote_exists = fetch_branch(remote, branch)

    if not remote_exists:
        p = push(remote, branch, set_upstream=True)
        if p.returncode != 0:
            handle_push_failure(p.stderr)
        sha = verify_pushed(remote, branch)
        print(f"Safe push completed (first push).\nBranch: {branch}\nRemote: {remote}/{branch}\n"
              f"Remote HEAD: {sha}")
        return

    remote_ref = f"{remote}/{branch}"
    local_sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote_sha = run(["git", "rev-parse", remote_ref]).stdout.strip()

    if local_sha == remote_sha:
        print(f"Already synchronized with {remote_ref}. Nothing to push.")
        return

    if is_ancestor(remote_ref, "HEAD"):
        pass  # local ahead -> fall through to push directly
    elif is_ancestor("HEAD", remote_ref):
        run(["git", "merge", "--ff-only", remote_ref])
        print(f"Fast-forwarded to {remote_ref}. Nothing to push.")
        return
    else:
        integrate_diverged(remote_ref, branch)

    p = push(remote, branch, set_upstream=not has_upstream)
    if p.returncode != 0:
        handle_push_failure(p.stderr)
    sha = verify_pushed(remote, branch)
    print(f"Safe push completed.\nBranch: {branch}\nRemote: {remote_ref}\nRemote HEAD: {sha}")


def integrate_diverged(remote_ref, branch):
    suspicious = find_suspicious_self_merge(remote_ref, branch)
    if suspicious:
        sha, subject = suspicious
        stop(f"local history contains a likely self-merge ({sha[:10]} \"{subject}\") "
             f"not yet pushed. Not touching it automatically — review it by hand.")

    old_head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    backup_ref = f"refs/backup/safe-push/{branch}-{int(time.time())}"
    run(["git", "update-ref", backup_ref, old_head])

    old_merge_count = len(run(["git", "log", "--merges", "--format=%H", f"{remote_ref}..HEAD"]).stdout.splitlines())

    p = subprocess.run(["git", "rebase", "--rebase-merges=no-rebase-cousins", remote_ref],
                        capture_output=True, text=True)
    if p.returncode != 0:
        conflicts = run(["git", "diff", "--name-only", "--diff-filter=U"], check=False).stdout.strip()
        run(["git", "rebase", "--abort"], check=False)
        stop("rebase hit conflicts and was aborted; branch restored to its pre-rebase state.\n"
             f"Conflicting files:\n{conflicts}\nResolve manually, then rerun.")

    ok = is_ancestor(remote_ref, "HEAD") and not run_ok(["test", "-d", f"{git_dir()}/rebase-merge"])
    new_merge_count = len(run(["git", "log", "--merges", "--format=%H", f"{remote_ref}..HEAD"]).stdout.splitlines())
    if not ok or new_merge_count != old_merge_count:
        run(["git", "reset", "--hard", backup_ref])
        stop("post-rebase verification failed (remote not an ancestor, or merge topology changed); "
             f"restored from backup ref {backup_ref}.")

    run(["git", "update-ref", "-d", backup_ref])


PROTECTED_MARKERS = ("protected branch", "permission denied", "not allowed to push")


def handle_push_failure(stderr):
    low = stderr.lower()
    if any(m in low for m in PROTECTED_MARKERS):
        stop(f"remote rejected the push (protected branch):\n{stderr.strip()}\n"
             "Use the repository's normal PR/MR workflow instead.")
    if "non-fast-forward" in low or "fetch first" in low or "rejected" in low:
        raise Retry(stderr)
    stop(f"push failed:\n{stderr.strip()}")


class Retry(Exception):
    pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remote", help="Override the remote to use (needed when ambiguous).")
    args = ap.parse_args()

    branch = check_preconditions()
    has_upstream = bool(run(["git", "config", "--get", f"branch.{branch}.merge"], check=False).stdout.strip())
    remote = resolve_remote(branch, args.remote)

    for attempt_no in range(MAX_PUSH_RETRIES + 1):
        try:
            attempt(branch, remote, has_upstream)
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
