#!/usr/bin/env zsh
# Usage: scripts/release.sh <version>
# Full GitHub Flow release: changelog → merge → tag → push → gh release

set -e

# ── Helper: Ensure a Homebrew package is installed ─────────────────────────
# Usage: ensure_brew_package <package_name> [<command_name>]
# If <command_name> is omitted, it defaults to <package_name>.
# Use the second arg when the binary name differs from the package
# (e.g. `ensure_brew_package webp cwebp`).
ensure_brew_package() {
  local pkg="$1"
  local cmd="${2:-$pkg}"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "⚠️  $cmd not detected, attempting to install $pkg via Homebrew..."
    if command -v brew >/dev/null 2>&1; then
      brew install "$pkg"
    else
      echo "❌ Homebrew not found, please install first: https://brew.sh"
      return 1
    fi
  fi
}

VERSION="$1"
if [[ -z "$VERSION" ]]; then
  echo "Usage: $0 <version>  (e.g. $0 1.2.3)"
  exit 1
fi
[[ "$VERSION" != v* ]] && VERSION="v${VERSION}"

# ── Preflight ─────────────────────────────────────────────────────────────────

ensure_brew_package gh || exit 1
ensure_brew_package git-cliff || exit 1

if ! gh auth status >/dev/null 2>&1; then
  echo "❌ gh is not authenticated — the GitHub Release step would fail."
  echo "   Run:  gh auth login   (GitHub.com → HTTPS → browser)"
  echo "   Then re-run this release command."
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "❌ Uncommitted changes — commit or stash first."
  exit 1
fi

if git rev-parse --verify "refs/tags/$VERSION" >/dev/null 2>&1; then
  echo "❌ Tag $VERSION already exists locally. Pick a different version or delete the tag first."
  exit 1
fi

if git ls-remote --tags origin "refs/tags/$VERSION" 2>/dev/null | grep -q "refs/tags/$VERSION$"; then
  echo "❌ Tag $VERSION already exists on origin. Pick a different version."
  exit 1
fi

if [[ -f pubspec.yaml ]]; then
  PUBSPEC_VER=$(grep -E '^version:' pubspec.yaml | awk '{print $2}' | cut -d'+' -f1)
  SHORT_VER="${VERSION#v}"
  if [[ "$PUBSPEC_VER" != "$SHORT_VER" ]]; then
    echo "❌ pubspec.yaml version ($PUBSPEC_VER) does not match release version ($SHORT_VER)."
    echo "   Update pubspec.yaml first, then re-run."
    exit 1
  fi
fi

# Python packages keep their distributable version in pyproject.toml.  Sync it
# into the release commit when it is a static project version.
SYNC_PYPROJECT_VERSION=false
if [[ -f pyproject.toml ]] && grep -qE '^version = "[^"]+"$' pyproject.toml; then
  PYPROJECT_VERSION=$(awk -F'"' '/^version = "[^"]+"$/ { print $2; exit }' pyproject.toml)
  SHORT_VERSION="${VERSION#v}"
  if [[ "$PYPROJECT_VERSION" != "$SHORT_VERSION" ]]; then
    SYNC_PYPROJECT_VERSION=true
  fi
fi

PACKAGE_FILES=()
sync_package_version() {
  [[ "$SYNC_PYPROJECT_VERSION" == true ]] || return

  RELEASE_VERSION="${VERSION#v}" perl -0pi -e \
    's/^version = "[^"]+"$/qq{version = "$ENV{RELEASE_VERSION}"}/me' pyproject.toml
  PACKAGE_FILES=(pyproject.toml)
  if [[ -f uv.lock ]]; then
    uv lock
    PACKAGE_FILES+=(uv.lock)
  fi
}

# Mirrors what CI would run on the tag push: catches a stale lockfile, lint,
# or test failure locally, before a tag is ever created or pushed. Skips
# silently on non-uv projects.
run_python_checks() {
  [[ -f pyproject.toml && -f uv.lock ]] || return
  command -v uv >/dev/null 2>&1 || return
  printf "${C_CYAN}🐍 Verifying Python project (uv sync --locked%s%s)...${C_RESET}\n" \
    "$(grep -q '^\[tool.ruff\]' pyproject.toml 2>/dev/null && echo ', ruff')" \
    "$([[ -d tests ]] && echo ', pytest')"
  uv sync --locked --all-groups
  grep -q '^\[tool.ruff\]' pyproject.toml 2>/dev/null && uv run ruff check .
  [[ -d tests ]] && uv run pytest -q
}

BRANCH=$(git branch --show-current)
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
# Normalize origin to an HTTPS URL so it survives in markdown links.
# Handles: git@github.com:owner/repo.git, ssh://git@github.com/owner/repo.git, https://github.com/owner/repo.git
REMOTE=$(git remote get-url origin 2>/dev/null \
  | sed -E -e 's|^git@github\.com:|https://github.com/|' \
            -e 's|^ssh://git@github\.com/|https://github.com/|' \
            -e 's|\.git$||')

# Resolve git-cliff config: a project-local cliff.toml wins; otherwise fall
# back to the shared global config so every project emits the same format.
# The global file lives at ~/.claude/scripts/cliff.toml.global, but git-cliff
# picks its config parser by file extension — so --config must go through the
# .toml-named symlink that scripts/bootstrap.sh maintains at
# ~/.config/git-cliff/cliff.toml.
CLIFF_CONFIG=()
if [[ -f cliff.toml ]]; then
  CLIFF_CONFIG=(--config cliff.toml)
elif [[ -f "$HOME/.config/git-cliff/cliff.toml" ]]; then
  CLIFF_CONFIG=(--config "$HOME/.config/git-cliff/cliff.toml")
fi

# Detect buildable artifacts (Flutter projects with matching Makefile targets).
# For each target found, `make <target>` is run after confirmation; any file
# in dist/ whose name contains the release version is attached to the GitHub
# Release. If a project wants .dmg instead of .zip for macOS, change the
# Makefile's `macos` target — this script just attaches what's produced.
RELEASE_TARGETS=()
if [[ -f pubspec.yaml ]] && [[ -f Makefile ]]; then
  for t in macos apk ipa bundle; do
    if grep -qE "^${t}:" Makefile 2>/dev/null; then
      RELEASE_TARGETS+=("$t")
    fi
  done
fi

# ── Preview ───────────────────────────────────────────────────────────────────

C_RESET='\033[0m'; C_CYAN='\033[1;36m'; C_YELLOW='\033[1;33m'
C_GREEN='\033[0;32m'; C_BOLD='\033[1m'; C_DIM='\033[2m'

printf "\n"
printf "${C_CYAN}┌─ Release Preview ───────────────────────────────────────${C_RESET}\n"
printf "${C_CYAN}│${C_RESET}  Version : ${C_BOLD}${C_YELLOW}%s${C_RESET}\n" "$VERSION"
if [[ "$BRANCH" == "main" ]]; then
  printf "${C_CYAN}│${C_RESET}  Action  : ${C_DIM}pull main → changelog → tag → push → gh release${C_RESET}\n"
else
  printf "${C_CYAN}│${C_RESET}  Action  : ${C_DIM}merge '%s' → main → changelog → tag → push → gh release → delete branch${C_RESET}\n" "$BRANCH"
fi
if (( ${#RELEASE_TARGETS[@]} > 0 )); then
  printf "${C_CYAN}│${C_RESET}  Build   : ${C_DIM}make %s → attach matching dist/ artifacts${C_RESET}\n" "${RELEASE_TARGETS[*]}"
fi
printf "${C_CYAN}│${C_RESET}\n"
if [[ -n "$LAST_TAG" ]]; then
  printf "${C_CYAN}│${C_RESET}  Commits since ${C_GREEN}%s${C_RESET} :\n" "$LAST_TAG"
  git log "${LAST_TAG}..HEAD" --oneline --no-decorate | while IFS= read -r line; do
    printf "${C_CYAN}│${C_RESET}    ${C_DIM}%s${C_RESET}\n" "$line"
  done
else
  printf "${C_CYAN}│${C_RESET}  Commits ${C_DIM}(no previous tag)${C_RESET} :\n"
  git log --oneline --no-decorate -20 | while IFS= read -r line; do
    printf "${C_CYAN}│${C_RESET}    ${C_DIM}%s${C_RESET}\n" "$line"
  done
fi
printf "${C_CYAN}└─────────────────────────────────────────────────────────${C_RESET}\n"
printf "\n"
printf "Press Enter to confirm, Ctrl+C to cancel... "
read -r

# ── Compose user-facing release intro ─────────────────────────────────────────
# Two sources:
#   1. RELEASE_INTRO_FILE env var — pre-drafted intro (used by /release command;
#      Claude reads git history, drafts the intro, gets user approval, then
#      passes the approved file via this env var).
#   2. Fallback: open $EDITOR with a template for manual composition (used when
#      this script is run directly, bypassing /release).
# Leave file empty to skip the intro.

INTRO_FILTER='
  s/<!--.*?-->//gs;
  s/\A\s+|\s+\z//g;
  s/\n{3,}/\n\n/g;
  print;
'

if [[ -n "$RELEASE_INTRO_FILE" && -f "$RELEASE_INTRO_FILE" ]]; then
  INTRO_CONTENT=$(perl -0777 -ne "$INTRO_FILTER" "$RELEASE_INTRO_FILE")
  if [[ -n "$INTRO_CONTENT" ]]; then
    printf "${C_GREEN}✓ Using pre-supplied release intro (%d chars).${C_RESET}\n" "${#INTRO_CONTENT}"
  else
    printf "${C_DIM}  RELEASE_INTRO_FILE was empty — using auto-generated changelog only.${C_RESET}\n"
  fi
else
  INTRO_DIR=$(mktemp -d -t release-intro.XXXXXX)
  NOTES_INTRO="$INTRO_DIR/RELEASE_INTRO.md"
  cat > "$NOTES_INTRO" <<EOF
<!-- Compose the user-facing intro for ${VERSION}.
     Audience: users deciding whether to upgrade.
     Save & close to continue. Leave the whole file empty to skip the intro.
     HTML comments (like this one) are stripped automatically.

     Convention follows React / Tailwind / Bun major releases:

     1. PROSE PARAGRAPH (no heading): 1–3 sentences — what's the headline?
        Optional link to a blog post / upgrade guide.

     2. ## Breaking Changes (only if there ARE breaking changes):
        Bullet what existing users must change. Delete this whole section if none.

     Detailed Features / Fixes / etc. are auto-appended below by git-cliff —
     do NOT re-list them here. -->




<!-- ## Breaking Changes

- (Describe what existing users must change.)
- (Then delete the surrounding HTML comment markers to enable this section.)

-->
EOF

  printf "\n${C_CYAN}📝 Opening editor to compose release intro...${C_RESET}\n"
  printf "${C_DIM}   (Save & close to continue. Leave empty to skip.)${C_RESET}\n"
  "${EDITOR:-vi}" "$NOTES_INTRO"

  INTRO_CONTENT=$(perl -0777 -ne "$INTRO_FILTER" "$NOTES_INTRO")

  if [[ -n "$INTRO_CONTENT" ]]; then
    printf "${C_GREEN}✓ Intro composed (%d chars).${C_RESET}\n" "${#INTRO_CONTENT}"
  else
    printf "${C_DIM}  No intro provided — using auto-generated changelog only.${C_RESET}\n"
  fi
  rm -rf "$INTRO_DIR"
fi

# ── Build artifacts (before any git ops, so a build failure aborts cleanly) ──

RELEASE_FILES=()
if (( ${#RELEASE_TARGETS[@]} > 0 )); then
  SHORT_VER="${VERSION#v}"
  printf "${C_CYAN}📦 Building release artifacts...${C_RESET}\n"
  for t in "${RELEASE_TARGETS[@]}"; do
    printf "   ${C_DIM}→ make %s${C_RESET}\n" "$t"
    make "$t"
  done
  # zsh nullglob qualifier (N): pattern expands to nothing if no match.
  for f in dist/*${SHORT_VER}*.zip(N) dist/*${SHORT_VER}*.dmg(N) dist/*${SHORT_VER}*.apk(N) dist/*${SHORT_VER}*.aab(N) dist/*${SHORT_VER}*.ipa(N); do
    [[ -f "$f" ]] && RELEASE_FILES+=("$f")
  done
  if (( ${#RELEASE_FILES[@]} == 0 )); then
    printf "${C_YELLOW}⚠  No artifacts in dist/ matched version '%s' — release will be created without attachments.${C_RESET}\n" "$SHORT_VER"
    printf "${C_YELLOW}   (Check that pubspec.yaml version matches the release version.)${C_RESET}\n"
  else
    printf "${C_GREEN}✓ Artifacts ready (%d):${C_RESET}\n" "${#RELEASE_FILES[@]}"
    for f in "${RELEASE_FILES[@]}"; do printf "   ${C_DIM}%s${C_RESET}\n" "$f"; done
  fi
fi

# ── Release ───────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "$BRANCH" == "main" ]]; then
  git pull origin main
  sync_package_version
  run_python_checks
  git cliff "${CLIFF_CONFIG[@]}" --tag "$VERSION" --output CHANGELOG.md
  git add CHANGELOG.md "${PACKAGE_FILES[@]}"
  git commit -m "docs(changelog): release $VERSION"
  git tag -a "$VERSION" -m "Release $VERSION"
  git push origin main
  git push origin "$VERSION"
  NOTES_FILE=$(mktemp)
  if [[ -n "$INTRO_CONTENT" ]]; then
    printf "%s\n\n" "$INTRO_CONTENT" > "$NOTES_FILE"
  fi
  # --strip header drops "# Changelog" preamble; sed swaps the "## [ver] - date"
  # heading for "## What's Changed" (GitHub UI already shows tag + date).
  git cliff "${CLIFF_CONFIG[@]}" --tag "$VERSION" --latest --strip header \
    | sed "s/^## \[.*/## What's Changed/" >> "$NOTES_FILE"
  if [[ -n "$LAST_TAG" && -n "$REMOTE" ]]; then
    printf "\n**Full Changelog**: [%s...%s](%s/compare/%s...%s)\n" \
      "$LAST_TAG" "$VERSION" "$REMOTE" "$LAST_TAG" "$VERSION" >> "$NOTES_FILE"
  fi
  if (( ${#RELEASE_FILES[@]} > 0 )); then
    gh release create "$VERSION" --notes-file "$NOTES_FILE" "${RELEASE_FILES[@]}"
  else
    gh release create "$VERSION" --notes-file "$NOTES_FILE"
  fi
  rm -f "$NOTES_FILE"
  printf "${C_GREEN}✓ Tagged %s on main and created GitHub Release${C_RESET}\n" "$VERSION"

else
  sync_package_version
  run_python_checks
  if (( ${#PACKAGE_FILES[@]} > 0 )); then
    git add "${PACKAGE_FILES[@]}"
    git commit -m "chore(release): bump version to ${VERSION#v}"
  fi
  git push origin "$BRANCH" 2>/dev/null || true
  git checkout main
  git pull origin main
  git merge --no-ff "$BRANCH" -m "Merge $BRANCH into main"
  git cliff "${CLIFF_CONFIG[@]}" --tag "$VERSION" --output CHANGELOG.md
  git add CHANGELOG.md "${PACKAGE_FILES[@]}"
  git commit -m "docs(changelog): release $VERSION"
  git tag -a "$VERSION" -m "Release $VERSION"
  git push origin main
  git push origin "$VERSION"
  NOTES_FILE=$(mktemp)
  if [[ -n "$INTRO_CONTENT" ]]; then
    printf "%s\n\n" "$INTRO_CONTENT" > "$NOTES_FILE"
  fi
  # --strip header drops "# Changelog" preamble; sed swaps the "## [ver] - date"
  # heading for "## What's Changed" (GitHub UI already shows tag + date).
  git cliff "${CLIFF_CONFIG[@]}" --tag "$VERSION" --latest --strip header \
    | sed "s/^## \[.*/## What's Changed/" >> "$NOTES_FILE"
  if [[ -n "$LAST_TAG" && -n "$REMOTE" ]]; then
    printf "\n**Full Changelog**: [%s...%s](%s/compare/%s...%s)\n" \
      "$LAST_TAG" "$VERSION" "$REMOTE" "$LAST_TAG" "$VERSION" >> "$NOTES_FILE"
  fi
  if (( ${#RELEASE_FILES[@]} > 0 )); then
    gh release create "$VERSION" --notes-file "$NOTES_FILE" "${RELEASE_FILES[@]}"
  else
    gh release create "$VERSION" --notes-file "$NOTES_FILE"
  fi
  rm -f "$NOTES_FILE"
  git branch -d "$BRANCH"
  git push origin --delete "$BRANCH" 2>/dev/null || true
  printf "${C_GREEN}✓ Merged '%s' → main, tagged %s, GitHub Release created, deleted branch${C_RESET}\n" "$BRANCH" "$VERSION"
fi
