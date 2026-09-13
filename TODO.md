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
