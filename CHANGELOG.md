## [0.12.0] - 2026-09-25

### 🚀 Features

- **update-check:** Prompt to upgrade instead of just hinting
- **update:** Support GitHub tags and multi-step upgrades

### 🐛 Bug Fixes

- **update:** Stop naming a specific version in skip-until wording
## [0.11.0] - 2026-09-25

### 🚀 Features

- **update-check:** Overlap pypi check with the command's work
- **config:** Import and export settings

### 🐛 Bug Fixes

- **lint:** Parenthesize implicit string concat, silence S110 on background check

### 📚 Documentation

- **changelog:** Release v0.11.0
## [0.10.0] - 2026-09-24

### 🚀 Features

- Add update check

### 🐛 Bug Fixes

- **test:** Make only_git PATH work on Windows
- Update config menu skills and health rows

### 📚 Documentation

- **changelog:** Release v0.10.0
## [0.9.1] - 2026-09-22

### 🐛 Bug Fixes

- **menu:** Erase parent panel when leaving Agents

### 📚 Documentation

- **changelog:** Release v0.9.1

### 🧪 Testing

- Isolate only-git PATH from Homebrew CLIs
## [0.9.0] - 2026-09-19

### 🚀 Features

- **notify:** Use direct Telegram delivery and row highlight

### 🐛 Bug Fixes

- Switch notifications to bot api

### 📚 Documentation

- **changelog:** Release v0.9.0

### 🧪 Testing

- Isolate Telegram credentials
## [0.8.0] - 2026-09-18

### 🐛 Bug Fixes

- **agents:** Keep agent listing widths stable across toggles
- **cli:** Parenthesize concatenated f-string for ruff ISC004

### 📚 Documentation

- **readme:** Document configurable agent chain and skills field
- **changelog:** Release v0.8.0
## [0.7.0] - 2026-09-17

### 🚀 Features

- **agents:** Add cli and menu editing surface

### 🐛 Bug Fixes

- **menu:** Size panels to their own content

### 📚 Documentation

- **changelog:** Release v0.7.0
## [0.6.0] - 2026-09-17

### 📚 Documentation

- **changelog:** Release v0.6.0
## [0.5.1] - 2026-09-17

### 🐛 Bug Fixes

- **cli:** Remove unused noqa directive

### 📚 Documentation

- **changelog:** Release v0.5.1
## [0.5.0] - 2026-09-17

### 🚀 Features

- **cli:** Allow flags without their leading --

### 🐛 Bug Fixes

- **cli:** Remove duplicate commit handler row
- **secret-scan:** Ignore url path slugs

### 📚 Documentation

- Clarify skill installation ownership
- **changelog:** Release v0.5.0

### ⚙️ Miscellaneous Tasks

- **commit:** Remove model metadata
- **release:** Add scripted release flow
- **release:** Remove project release.sh, use global template
## [0.4.0] - 2026-09-14

### 🐛 Bug Fixes

- **release:** Sync uv.lock for v0.4.0

### 🚜 Refactor

- **agents:** Centralize agent definitions

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.4.0
## [0.3.1] - 2026-09-14

### 🐛 Bug Fixes

- **menu:** Stop config panel resizing on row selection

### 📚 Documentation

- **readme:** Document releasing and the wheel version check
- Remove CI section from README.md
- Add uv requirement note to README.md
- **changelog:** Release v0.3.1
## [0.3.0] - 2026-09-14

### 🐛 Bug Fixes

- **version:** Derive __version__ from package metadata

### 💼 Other

- **pypi:** [**breaking**] Publish as aicp-cli

### 📚 Documentation

- **readme:** Show install commands for pinning and upgrading

### ⚙️ Miscellaneous Tasks

- **release:** Publish wheels to PyPI via trusted publishing
- Assert the built wheel reports the right version
- **release:** V0.3.0
## [0.2.1] - 2026-09-13

### 📚 Documentation

- **changelog:** Release v0.2.1

### ⚙️ Miscellaneous Tasks

- Refresh uv lockfile for v0.2.0
## [0.2.0] - 2026-09-13

### 🚀 Features

- **cli:** Wire the entry point and bridge config to its consumers
- **menu:** Add Skills and Doctor rows with inline status
- **i18n:** Fill the zh-TW catalogue and retire menu.py's _ZH bridge
- [**breaking**] Support grok and quota-aware fallback
- **menu:** Display full CLI chain in config panel
- **skills:** Track installed skills via hashed state file
- **config:** Migrate config store to ~/.aicp/config.json
- **menu:** Bold the cursor row in the config panel
- **runner:** Persist quota exclusion across runs

### 🐛 Bug Fixes

- **aicp:** Double a trailing backslash when quoting a batch-shim argument
- **config:** [**breaking**] Deny AICP_CONFIG from .aicprc
- **cli:** Let AICP_LANG reach the translator, and refuse orphan sub-flags
- **menu:** Stop the config panel smearing during CLI-order changes
- **skills:** Reject unsafe record keys before hashing
- **config:** Normalize key casing between JSON and env
- **present:** Style config menu group headings
- **present:** Make config group headings legible

### 📚 Documentation

- **todo:** Record the Windows timeout-orphan defect as a known issue
- Record grok and quota behavior
- Add demo screenshot to readme
- Note follow-up security review for skills state store
- Add aicp performance improvement todo item

### 🧪 Testing

- Add intentional import failures for testing
- **config:** Cover case-folded and denylisted keys

### ⚙️ Miscellaneous Tasks

- Page Telegram on a failed push
- Ignore local omc state
- **release:** Bump version to 0.2.0
## [0.1.0] - 2026-09-12

### 🚀 Features

- **aicp:** Scaffold Python port with frozen contracts and test harness
- **skills:** Add cross-platform skills installer
- **aicp:** Port the git flow, secret scanner, and notifier
- **aicp:** Port hardened .aicprc layer and the --config/--swap-ai menu
- **aicp:** Port the timing log with 5MB x 5-file rotation
- **aicp:** Port the per-CLI timeout budget with history widening
- **aicp:** Port the fallback-chain runner with signal-safe timeouts

### 🐛 Bug Fixes

- **aicp:** Use CTRL_BREAK_EVENT and fix Windows stub argv logging
- **skills:** Overwrite a stale .bak on re-forced install
- **skills:** Never let a backup overwrite an earlier backup
- **aicp:** Harden the budget and notifier against values that crash a run
- **packaging:** Ship vendored skills in the wheel
- **aicp:** Stop the runner leaking children and writing through a symlink
- **aicp:** Bound the notifier so a hung tg-send.sh cannot block a run
- **aicp:** Launch npm .cmd shims on Windows instead of failing to start

### 📚 Documentation

- **aicp:** Write README, changelog, and .aicprc.example for 0.1.0

### 🧪 Testing

- **aicp:** Count Path.stat, and exercise the non-TTY guard on a real fd
- **aicp:** Bound the unreaped-child tests on elapsed time
- **aicp:** Delete git's read-only objects when tearing down a bare remote
- **aicp:** Skip the argv-passthrough test on the platform it does not describe
