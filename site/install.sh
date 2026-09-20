#!/bin/bash
# sorted for Mac, one line install:
#   curl -fsSL https://kirtan-00.github.io/sorted/install.sh | bash
# Downloads the beta into ~/sorted, runs the installer there (which leaves a sorted icon on the
# Desktop), opens the app. Re-run any time.
# Files fetched by curl carry no quarantine flag, so macOS does not block the installer or the app.
set -e
DEST="$HOME/sorted"
# The branch zip is cached by GitHub for a while; ask for the exact latest commit instead so a fresh
# publish is picked up at once. Falls back to the branch zip if the API is unreachable.
SHA="$(curl -fsSL -H 'Accept: application/vnd.github+json' https://api.github.com/repos/kirtan-00/sorted-app/commits/main 2>/dev/null | sed -n 's/^  "sha": "\([0-9a-f]*\)",$/\1/p' | head -1)"
ZIP="https://github.com/kirtan-00/sorted-app/archive/${SHA:-refs/heads/main}.zip"
printf '\n==> sorted for Mac, public beta\n'
if [ "$(uname -m)" != "arm64" ]; then echo "    This beta needs an Apple silicon Mac (M1 to M4)."; exit 1; fi
if [ "$(sw_vers -productVersion | cut -d. -f1)" -lt 14 ]; then echo "    This beta needs macOS 14 or newer."; exit 1; fi
TMP="$(mktemp -d)"
printf '    downloading the app (about 42 MB)\n'
curl -fsSL -o "$TMP/sorted.zip" "$ZIP"
ditto -x -k "$TMP/sorted.zip" "$TMP/x"
SRC="$(find "$TMP/x" -maxdepth 1 -mindepth 1 -type d | head -1)"
mkdir -p "$DEST"
# keep .venv and caches from an earlier run; only the app files are refreshed
rsync -a "$SRC/" "$DEST/"
rm -rf "$TMP"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
chmod +x "$DEST/Install sorted.command" "$DEST/sorted.app/Contents/MacOS/"* 2>/dev/null || true
printf '    app is in %s\n' "$DEST"
exec bash "$DEST/Install sorted.command"
