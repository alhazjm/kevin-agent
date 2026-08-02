#!/usr/bin/env bash
#
# Re-vendor the Muted Card System (github.com/alhazjm/cards) into pwa/vendor/.
#
# Why vendored rather than a CDN or an npm dependency: the PWA is a single HTML
# file with no bundler and no package.json, it is served as a Render static
# site, and it is meant to keep working offline. A CDN <script> would add a
# third-party runtime dependency and a network hop on the render path; npm
# would need a build step this app does not have. Two copied files have no
# moving parts.
#
# The two files are GENERATED/UPSTREAM — never hand-edit them here. Fix the
# upstream repo, then re-run this script. The banner each file carries records
# the commit it came from so drift is visible in a diff.
#
# Usage:  pwa/vendor/sync-cards.sh [path-to-cards-checkout]   (default ../cards)

set -euo pipefail

SRC="${1:-$(cd "$(dirname "$0")/../../.." && pwd)/cards}"
DEST="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SRC/dist/cards.global.js" ]; then
  echo "error: no cards checkout at $SRC (expected $SRC/dist/cards.global.js)" >&2
  echo "       clone github.com/alhazjm/cards, or pass its path as \$1" >&2
  exit 1
fi

REV="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# Read the version with sed rather than node: this runs under Git Bash on
# Windows too, where node cannot resolve an MSYS-style /c/... path.
VER="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SRC/package.json" | head -1)"
VER="${VER:-unknown}"
STAMP="vendored from github.com/alhazjm/cards @ ${REV} (v${VER}) — do not edit here"

for f in dist/cards.global.js css/cards.css; do
  out="$DEST/$(basename "$f")"
  printf '/* %s */\n' "$STAMP" > "$out"
  cat "$SRC/$f" >> "$out"
  echo "wrote $(basename "$out")  <-  $f"
done

echo "done: $STAMP"
