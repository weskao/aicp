# TODO

## Improve `aicp` command overall performance (MEDIUM, open)

Do a deep performance pass on the whole `aicp` command, not just one path.
Example symptom: an AI agent failed to complete the git commit stage
reliably, so it was avoided in the safe git push stage afterward — find
and fix cases like this where a slow/unreliable step gets silently
bypassed instead of fixed. Audit other stages for similar slowness or
failure-avoidance patterns.

## Support "devin" harness (open)

Detect and support the Devin AI agent environment (`~/.devin`).
Add a `devin` harness so `aicp` can install skills, rules, and config
into the paths and formats that Devin expects.

## Support "opencode" harness (open)

Detect and support the OpenCode agent environment (`~/.opencode`).
Add an `opencode` harness so `aicp` can install skills, rules, and config
into the paths and formats that OpenCode expects.
