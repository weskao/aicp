"""Pre-``/commit`` secret scan — the one gate that runs before any AI CLI.

Direct port of ``_aicp_secret_scan``/``_aicp_secret_hit``/
``_aicp_scan_text_file`` in ``~/scripts/bin/aicp``. Two properties are
load-bearing and neither is an implementation detail:

1. **A hit aborts the run and prints only ``file:line`` plus the pattern's
   human name.** The matched text is tested against a pattern and then
   dropped on the floor — a real secret can never reach stdout, a log, or
   Telegram. Callers render :func:`report_lines`, never the file content.
2. **There is no entropy heuristic.** It was rejected upstream as the main
   false-positive source (this repo's own ``sk-automation`` would trip it).
   New pattern → append to :data:`PATTERNS`, nowhere else.

Scan surface follows from a hard constraint: aicp never runs ``git add``
itself — staging happens inside the AI CLI's own /commit prompt — so a
brand-new file, the single most likely place to paste a secret, is a ``??``
in ``git status`` and invisible to any ``git diff``. Three surfaces, then:
the added lines of ``git diff HEAD -U0``; every tracked file git called
"binary" in ``--numstat`` (genuinely binary, or only marked ``-diff`` in
.gitattributes); and every untracked file. Ignored files are never scanned —
git omits them and they were never going to be committed.

Performance: the zsh original forked a subshell PER SCANNED LINE (measured
3.7s for one 5000-line file, all fork overhead, paid before any AI CLI was
even reached). Everything here scans in-process; the only subprocesses are
the three git queries above.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ._utils import BOLD, DIM, RED, RESET, YELLOW
from .i18n import t

__all__ = ["PATTERNS", "ScanResult", "report_lines", "scan"]

# Prefix/format checks only, copied verbatim from AICP_SECRET_PATTERNS in
# bin/aicp. The human name is the only half ever printed.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-(proj-)?[A-Za-z0-9_-]{20,}"), "OpenAI-style API key (sk-…)"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub personal access token (ghp_…)"),
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), "GitHub App/server token (ghs_…)"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub fine-grained PAT (github_pat_…)"),
    (re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}"), "AWS access key ID (AKIA…/ASIA…)"),
    (re.compile(r"[Bb]earer [A-Za-z0-9._-]{20,}"), "bearer token (Bearer …)"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----"), "private key block"),
)

#: How much of a file is inspected for a NUL byte before calling it binary —
#: the same "first block decides" rule `grep -I` applies.
_BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class ScanResult:
    """What the scan found. *hits* are already-redacted "file:line  looks
    like <name>" strings; *unscanned* names files whose content could not be
    read as text (warned about, never silently skipped, never fatal)."""

    hits: tuple[str, ...] = ()
    unscanned: tuple[str, ...] = ()
    skipped: bool = False

    @property
    def ok(self) -> bool:
        """True when the run may proceed to an AI CLI."""
        return not self.hits


def _hit(file: str, lineno: int, content: str) -> str | None:
    """The redaction boundary: *content* is matched and discarded, and only
    the location plus the pattern's name survive into the returned string."""
    for pattern, name in PATTERNS:
        if pattern.search(content):
            return f"{file}:{lineno}  looks like {name}"
    return None


def _git(cwd: Path, *args: str) -> str:
    """Run a git query in *cwd*, returning stdout ("" on any failure).

    Text mode is deliberately off: paths come back NUL-separated and may hold
    any byte a filesystem allows, so decoding is done per-field below with
    ``errors="replace"`` rather than letting a stray byte kill the scan.
    """
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, check=False
        )
    except OSError:
        return ""
    return done.stdout.decode("utf-8", "replace")


def _scan_text_file(reported: str, abs_file: Path) -> list[str] | None:
    """Scan a whole file's current content. ``None`` means "not text" — the
    same verdict ``grep -qI`` returns, and the caller records it as unscanned
    rather than passing over it in silence."""
    try:
        with abs_file.open("rb") as handle:
            head = handle.read(_BINARY_SNIFF_BYTES)
            if b"\0" in head:
                return None
            rest = handle.read()
    except OSError:
        return None
    text = (head + rest).decode("utf-8", "replace")
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        found = _hit(reported, lineno, line)
        if found:
            hits.append(found)
    return hits


def _scan_diff(cwd: Path) -> list[str]:
    """Added lines of ``git diff HEAD --unified=0``.

    -U0 keeps only changed lines in each hunk body, so a hunk header's own
    ``+start`` is every added line's number and no unrelated context line (an
    old, already-committed secret nobody is touching) can trip a false abort.

    A ``+++ `` header is only ever real immediately after git's own
    unambiguous ``diff --git a/... b/...`` segment start: an added CONTENT
    line whose text begins with ``++`` is printed as a literal ``+++ ...``
    and would otherwise be swallowed as a file header, secret and all.
    """
    hits: list[str] = []
    file = ""
    lineno = 0
    in_header = False
    # core.quotePath=false keeps a non-ASCII path readable in the reported
    # hit instead of arriving C-quoted as \346\251\237…
    for line in _git(
        cwd, "-c", "core.quotePath=false", "diff", "HEAD", "--no-color", "--unified=0"
    ).splitlines():
        if line.startswith("diff --git "):
            in_header = True
        elif line.startswith("+++ "):
            if in_header:
                file = line[4:].removeprefix("b/")
                in_header = False
            else:
                found = _hit(file, lineno, line[1:])
                if found:
                    hits.append(found)
                lineno += 1
        elif line.startswith("@@ "):
            after_plus = line.partition("+")[2]
            digits = re.match(r"\d*", after_plus).group()
            lineno = int(digits or 0)
            in_header = False
        elif line.startswith("+"):
            found = _hit(file, lineno, line[1:])
            if found:
                hits.append(found)
            lineno += 1
    return hits


def _records(raw: str) -> list[str]:
    """Split a ``-z`` git output into its NUL-terminated records."""
    return [record for record in raw.split("\0") if record]


def scan(cwd: Path | str | None = None) -> ScanResult:
    """Scan everything this run could commit, from *cwd* (default: the
    process's own).

    ``AICP_SKIP_SECRET_SCAN=1`` bypasses the whole scan and says so — the
    documented one-shot escape for a false positive.
    """
    if os.environ.get("AICP_SKIP_SECRET_SCAN") == "1":
        return ScanResult(skipped=True)

    cwd = Path(cwd) if cwd is not None else Path.cwd()
    # git reports every path relative to the TOPLEVEL, never the caller's
    # cwd, and aicp is a bare alias with no `cd` — so running from a
    # subdirectory is the normal case, and each path has to be resolved back
    # against the toplevel before it is opened.
    toplevel = _git(cwd, "rev-parse", "--show-toplevel").strip()
    root = Path(toplevel) if toplevel else cwd

    hits: list[str] = _scan_diff(cwd)
    unscanned: list[str] = []

    # A changed TRACKED file git calls "binary" — genuinely (an embedded NUL)
    # or only because a .gitattributes `-diff`/`binary` rule marked an
    # otherwise-plain-text path opaque — shows no "+" line above at all, just
    # one prose "Binary files a/X and b/X differ". Parsing that prose was
    # itself a bypass (a path containing " and ", or any path git quotes,
    # vanished silently); `--numstat -z` sidesteps it entirely: a
    # line-count-unknown file reports "-" in both columns, and -z disables
    # path quoting, so the path field is the literal filesystem name.
    for record in _records(_git(cwd, "diff", "HEAD", "--numstat", "-z")):
        added, _, rest = record.partition("\t")
        deleted, _, path = rest.partition("\t")
        if (added, deleted) != ("-", "-") or not path:
            continue
        abs_file = root / path
        # A DELETED binary reports the same "-\t-" with nothing left on disk:
        # no content survives to leak, so there is nothing to warn about.
        if not abs_file.is_file():
            continue
        found = _scan_text_file(path, abs_file)
        if found is None:
            unscanned.append(path)
        else:
            hits.extend(found)

    # --untracked-files=all: the default mode collapses a brand-new directory
    # into one "?? dir/" entry, which would let a secret in any new file
    # inside any new module or config folder through unscanned.
    for record in _records(
        _git(cwd, "status", "--porcelain", "--untracked-files=all", "-z")
    ):
        if not record.startswith("?? "):
            continue
        path = record[3:]
        abs_file = root / path
        if not abs_file.is_file():
            continue
        found = _scan_text_file(path, abs_file)
        if found is None:
            # A brand-new .env saved as UTF-16 (NUL bytes throughout) used to
            # pass the whole scan in total silence — "could not be scanned"
            # presented as "nothing to find". /commit stages untracked files,
            # so this half is if anything the likelier place for a paste.
            unscanned.append(path)
        else:
            hits.extend(found)

    return ScanResult(hits=tuple(hits), unscanned=tuple(unscanned))


def report_lines(result: ScanResult) -> list[str]:
    """What to print for *result* — locations and pattern names only.

    Nothing here is derived from a matched line's content, which is why a
    secret cannot reach stdout, a log, or Telegram through this path.
    """
    if result.skipped:
        return [
            f"{DIM}{t('scan_skipped', '▸ secret scan skipped (AICP_SKIP_SECRET_SCAN=1)')}{RESET}"
        ]
    if result.ok:
        if not result.unscanned:
            return []
        # Warn, never abort: nothing could be read, so nothing is confirmed —
        # but silence is the defect this closes. An untouched tracked binary
        # never reaches `git diff HEAD`, and an ignored file never reaches
        # `git status`, so a repo full of either produces zero noise here.
        lines = [
            "",
            f"{YELLOW}{BOLD}"
            + t(
                "scan_binary_warn",
                "⚠ secret scan: %s file(s) with binary content — not scanned, verify manually:",
                len(result.unscanned),
            )
            + RESET,
        ]
        lines += [f"  {YELLOW}{name}{RESET}" for name in result.unscanned]
        return lines

    lines = [
        "",
        f"{RED}{BOLD}"
        + t(
            "scan_found",
            "✗ secret scan found %s likely secret(s) — aborting before /commit, no AI CLI called",
            len(result.hits),
        )
        + RESET,
    ]
    lines += [f"  {RED}{hit}{RESET}" for hit in result.hits]
    lines.append(
        f"{DIM}"
        + t(
            "scan_advise",
            "  remove or mask the secret, then re-run; a false positive can be bypassed once with AICP_SKIP_SECRET_SCAN=1.",
        )
        + RESET
    )
    return lines
