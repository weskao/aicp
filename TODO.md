# TODO

## Known defect — Windows timeout orphans the CLI (HIGH, open)

On Windows, an npm-installed CLI is a `.cmd` shim launched through `%ComSpec%`.
On the budget-timeout path, terminating that intermediate `cmd.exe` can orphan
the CLI behind it. POSIX is unaffected, and Ctrl+C still targets the Windows
process group.

Fix this with Windows process-tree termination (`taskkill /T /F` or a Job
Object) and a Windows-only test that starts a live grandchild and proves it is
gone. Do not claim this timeout path kills the CLI until that test runs on
Windows.

## Deeper security review of the skills state store (MEDIUM, open)

`src/aicp/skills.py`'s `$HOME/.aicp/state.json` mechanism has only had a
basic security pass so far (see `952d76d`, which closed one found
path-traversal issue in `_resolve()`/`_matches()`: an absolute or `..`
record key could make `detect()`/`install()` hash a file outside the
install target, or hang on a FIFO). That pass was reactive — one issue
found by manually reasoning about `_matches()`'s untrusted input — not a
systematic audit of the whole module (state file lifecycle, legacy sidecar
migration, `_copy()`/`_write()`'s own symlink-following behavior when
installing, and `full_status()`/`status_json()`'s handling of a hostile
state.json).

Follow up with a dedicated, more thorough security scan of `skills.py` and
its tests, using the Fable 5.1 model. Do not consider this module fully
reviewed until that pass runs.
