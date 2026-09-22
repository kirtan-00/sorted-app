#!/bin/bash
# Install sorted.command: one double-click sets up everything sorted needs.
#
# What it does, in order (each step is skipped when already done, so a second run is fast):
#   1. refuses Intel Macs and macOS older than 14, and writes BUILD (the git short sha next to VERSION,
#      the app's build stamp) when it is missing and git can say
#   2. installs uv (the Python package manager) into ~/.local/bin if it is missing
#   3. makes .venv here with uv's own arm64 Python 3.11 (never the Mac's python.org universal one,
#      which LaunchServices can start as x86_64 and then no wheel loads)
#   4. installs sorted and its libraries into .venv (runtime only, no test tools)
#   5. fetches the models: MobileCLIP-S1 into the Hugging Face cache (the face models ship in models/)
#      and proves they load
#   6. ffmpeg + ffprobe for videos: Homebrew's if there is a Homebrew, otherwise a static arm64
#      build dropped into .venv/bin
#   7. makes sorted.app in this folder openable
#   8. puts the app in ~/Applications/sorted.app (a tiny launcher that starts the app here; Spotlight,
#      Launchpad and project-file double-clicks find it there) and an alias to it on the Desktop
#      (re-runs replace both; a sorted.app the installer did not make is left alone)
#   9. counts the install: one anonymous ping (random id in .install-id, macOS version, chip)
#  10. opens sorted.app
#
# Touches only: this folder (.venv, .uv, .install-id, install.log, BUILD), ~/.local/bin (uv), the Hugging Face
# cache (~/.cache/huggingface) and ~/Desktop/sorted.app. uv's own download cache and its Python
# live in .uv/ here, not in your home folder. Nothing is added to your shell profile.
#
# Static ffmpeg source (used only when Homebrew is absent or its install fails):
#   https://ffmpeg.martin-riedl.de  (macOS arm64 release builds, listed on ffmpeg.org/download)
#   pinned build 1787073674_9.0.1 = FFmpeg 9.0.1, about 28 MB per binary; falls back to the
#   site's "latest" redirect if the pinned build has been pruned.
#
# Test hooks (not needed by a tester): SORTED_INSTALL_NO_OPEN=1 skips the final open,
# SORTED_NO_BREW=1 pretends there is no Homebrew.

set -u
set -o pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LOG="$REPO/install.log"
UV_URL="https://astral.sh/uv/install.sh"
FF_BASE="https://ffmpeg.martin-riedl.de"
FF_PINNED="$FF_BASE/download/macos/arm64/1787073674_9.0.1"
FF_LATEST="$FF_BASE/redirect/latest/macos/arm64/release"
FACE_BASE="https://github.com/opencv/opencv_zoo/raw/main/models"

step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() {
  printf '\nInstall stopped: %s\n' "$*"
  printf 'The full log is at %s. Send it to purohit.krick@gmail.com and it gets fixed.\n' "$LOG"
  exit 1
}

cd "$REPO" || fail "could not enter $REPO"
[ -f "$REPO/pyproject.toml" ] || fail "this file must stay inside the sorted folder (pyproject.toml is missing next to it)"

# Everything printed also lands in install.log.
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

printf 'sorted installer\nfolder: %s\nstarted: %s\n' "$REPO" "$(date)"

# 1. The Mac itself ------------------------------------------------------------------------------
step "Checking this Mac"
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]; then
  fail "sorted needs an Apple silicon Mac (M1 or newer). This one is an Intel Mac."
fi
OS_VER="$(sw_vers -productVersion)"
OS_MAJOR="${OS_VER%%.*}"
if [ "${OS_MAJOR:-0}" -lt 14 ] 2>/dev/null; then
  fail "sorted needs macOS 14 or newer. This Mac runs macOS $OS_VER."
fi
note "Apple silicon, macOS $OS_VER"
# The build stamp: VERSION is the number, BUILD the git short sha of this tree. app-publish.sh stamps BUILD for
# the published copy; a checkout without one gets it here when git can say.
if [ ! -s "$REPO/BUILD" ]; then
  SHA="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"
  if [ -n "$SHA" ]; then printf '%s\n' "$SHA" > "$REPO/BUILD"; fi
fi
note "sorted $(cat "$REPO/VERSION" 2>/dev/null || echo '?')$( [ -s "$REPO/BUILD" ] && printf ' (%s)' "$(cat "$REPO/BUILD")" )"

# 2. uv ------------------------------------------------------------------------------------------
step "uv (Python package manager)"
UV="$HOME/.local/bin/uv"
if [ ! -x "$UV" ] && command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
fi
if [ -x "$UV" ]; then
  note "already here: $UV ($("$UV" --version 2>/dev/null))"
else
  note "downloading uv from $UV_URL into ~/.local/bin"
  mkdir -p "$HOME/.local/bin"
  # UV_UNMANAGED_INSTALL = fixed folder, no shell profile edits, no receipt in ~/.config.
  curl -LsSf --max-time 300 "$UV_URL" | UV_UNMANAGED_INSTALL="$HOME/.local/bin" sh \
    || fail "could not download uv. Is the internet connection working?"
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || fail "uv did not land in ~/.local/bin"
  note "installed $("$UV" --version)"
fi
# Keep uv's downloads and its Python inside this folder.
export UV_CACHE_DIR="$REPO/.uv/cache"
export UV_PYTHON_INSTALL_DIR="$REPO/.uv/python"
mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"

# 3. .venv with Python 3.11 ------------------------------------------------------------------------
step "Python 3.11 in .venv"
PY="$REPO/.venv/bin/python"
if [ -x "$PY" ] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' 2>/dev/null \
   && ! file "$(readlink -f "$PY")" | grep -q universal; then
  note "already here: $("$PY" --version)"
else
  [ -d "$REPO/.venv" ] && note "replacing the old .venv"
  # uv finds a Python 3.11 on the Mac, or downloads its own CPython (about 25 MB) into .uv/python.
  "$UV" venv --managed-python --python 3.11 --clear "$REPO/.venv" || fail "could not create .venv (see the lines above)"
  note "made .venv with $("$PY" --version)"
fi

# 4. sorted and its libraries ----------------------------------------------------------------------
step "Installing sorted and its libraries into .venv (first time: about 260 MB to download, 1.2 GB on disk)"
"$UV" pip install --python "$PY" -e "$REPO" || fail "could not install the libraries (see the lines above)"
note "installed"

# 5. Models --------------------------------------------------------------------------------------
step "Models"
for f in face_detection_yunet_2023mar.onnx face_recognition_sface_2021dec.onnx; do
  if [ ! -s "$REPO/models/$f" ]; then
    note "fetching $f from opencv_zoo"
    case "$f" in
      face_detection*) sub="face_detection_yunet" ;;
      *) sub="face_recognition_sface" ;;
    esac
    curl -LsSf --max-time 600 -o "$REPO/models/$f" "$FACE_BASE/$sub/$f" || fail "could not download $f"
  fi
done
note "face models (YuNet + SFace) in models/"

CHECK='
import os
from photosort import faces, embed
faces.FaceEngine()
e = embed.get_embedder()
e.encode_text(["a photo of a beach"])
print("FETCHED" if os.environ.get("HF_HUB_OFFLINE") == "0" else "CACHED", e.device)
'
# The app runs with HF_HUB_OFFLINE=1. embed.py goes online once, on its own, when the weights are not
# in the cache yet; the printed word says which happened.
note "loading the models (the first time this fetches MobileCLIP-S1, about 325 MB, from huggingface.co)"
OUT="$(HF_HUB_OFFLINE=1 "$PY" -c "$CHECK")" || fail "could not load the models (see the lines above)"
case "$OUT" in
  FETCHED*)
    note "fetched MobileCLIP-S1 into the Hugging Face cache; checking it loads offline"
    HF_HUB_OFFLINE=1 "$PY" -c "$CHECK" | grep -q '^CACHED' || fail "MobileCLIP-S1 downloaded but does not load offline"
    ;;
  CACHED*) note "MobileCLIP-S1 already in the Hugging Face cache" ;;
  *) fail "unexpected model check output: $OUT" ;;
esac
note "models load (MobileCLIP-S1 on ${OUT#* }, YuNet + SFace), ok"

# 6. ffmpeg + ffprobe ----------------------------------------------------------------------------
step "ffmpeg and ffprobe (for videos)"
runs() { "$1" -version >/dev/null 2>&1; }

have_ffmpeg() {
  # The app looks on PATH (with .venv/bin first) and then in /opt/homebrew/bin and /usr/local/bin.
  local d
  local dirs=("$REPO/.venv/bin")
  [ -z "${SORTED_NO_BREW:-}" ] && dirs+=(/opt/homebrew/bin /usr/local/bin)
  for d in "${dirs[@]}"; do
    if [ -x "$d/ffmpeg" ] && [ -x "$d/ffprobe" ] && runs "$d/ffmpeg" && runs "$d/ffprobe"; then
      FOUND_FF="$d"; return 0
    fi
  done
  return 1
}

fetch_static_ffmpeg() {
  local tmp="$REPO/.uv/ffmpeg-download" name url
  rm -rf "$tmp"; mkdir -p "$tmp"
  for name in ffmpeg ffprobe; do
    url="$FF_PINNED/$name.zip"
    if ! curl -LsSf --max-time 600 -o "$tmp/$name.zip" "$url"; then
      note "pinned build not available, taking the latest release build"
      url="$FF_LATEST/$name.zip"
      curl -LsSf --max-time 600 -o "$tmp/$name.zip" "$url" || { note "could not download $name from $FF_BASE"; return 1; }
    fi
    note "downloaded $name.zip ($(du -h "$tmp/$name.zip" | cut -f1 | tr -d ' ')) from $url"
    unzip -qo "$tmp/$name.zip" -d "$tmp" || { note "could not unzip $name.zip"; return 1; }
    [ -f "$tmp/$name" ] || { note "$name.zip did not contain $name"; return 1; }
    chmod +x "$tmp/$name"
    xattr -d com.apple.quarantine "$tmp/$name" 2>/dev/null
    if ! file "$tmp/$name" | grep -q arm64; then note "$name is not an arm64 binary"; return 1; fi
    if ! runs "$tmp/$name"; then
      # An unsigned binary can be refused; an ad-hoc signature fixes that.
      codesign -s - -f "$tmp/$name" >/dev/null 2>&1
      runs "$tmp/$name" || { note "$name downloaded but does not run"; return 1; }
    fi
    mv -f "$tmp/$name" "$REPO/.venv/bin/$name"
  done
  rm -rf "$tmp"
  return 0
}

FOUND_FF=""
if have_ffmpeg; then
  note "already here: $FOUND_FF/ffmpeg ($("$FOUND_FF/ffmpeg" -version 2>/dev/null | head -1 | cut -d' ' -f1-3))"
else
  BREW=""
  if [ -z "${SORTED_NO_BREW:-}" ]; then
    if command -v brew >/dev/null 2>&1; then BREW="$(command -v brew)"
    elif [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew
    fi
  fi
  if [ -n "$BREW" ]; then
    note "Homebrew found, installing ffmpeg with it (this is the slow way, several minutes)"
    "$BREW" install ffmpeg || note "Homebrew could not install ffmpeg, taking the static build instead"
  fi
  if have_ffmpeg; then
    note "installed with Homebrew: $FOUND_FF/ffmpeg"
  else
    note "downloading static arm64 ffmpeg and ffprobe into .venv/bin (about 28 MB each)"
    fetch_static_ffmpeg || fail "could not set up ffmpeg (see the lines above)"
    have_ffmpeg || fail "ffmpeg landed in .venv/bin but does not run"
    note "installed: $FOUND_FF/ffmpeg ($("$FOUND_FF/ffmpeg" -version 2>/dev/null | head -1 | cut -d' ' -f1-3))"
  fi
fi

# 7. The app ------------------------------------------------------------------------------------
step "sorted.app"
chmod +x "$REPO/sorted.app/Contents/MacOS/sorted" 2>/dev/null
# A zip from the browser leaves every file quarantined; Finder would then refuse to open the app.
xattr -dr com.apple.quarantine "$REPO/sorted.app" 2>/dev/null
"$UV" cache prune --quiet 2>/dev/null
note "ready"

# 8. A sorted icon on the Desktop ----------------------------------------------------------------
# ~/Desktop/sorted.app is a small AppleScript applet (osacompile ships with macOS): double-click it
# and it runs the launcher in this folder; double-click a project file (sorted_<shoot>.sorted) and it
# runs the launcher with that file, which the app then opens. An applet gets the file from the Finder;
# a plain shell bundle never does. It carries the sorted icon and declares the project file type, so
# the Finder shows the icon on project files too. A marker file records which folder made it, so a
# re-run (or an install in a new folder) replaces it and a sorted.app somebody put on the Desktop by
# hand is left alone.
desktop_icon() {
  local desk="$HOME/Applications/sorted.app" marker="Contents/Resources/sorted-installed-from"
  local src="$REPO/sorted.app" launcher="$REPO/sorted.app/Contents/MacOS/sorted"
  DESKTOP_ICON=""
  mkdir -p "$HOME/Applications" 2>/dev/null || { note "could not create ~/Applications, skipping the app icon"; return 0; }
  if [ -e "$desk" ] && [ ! -f "$desk/$marker" ]; then
    note "there is already a sorted.app in ~/Applications that this installer did not make; leaving it alone"
    return 0
  fi
  rm -rf "$desk"
  # An older install put the launcher itself on the Desktop; it is replaced by an alias below.
  if [ -f "$HOME/Desktop/sorted.app/$marker" ]; then rm -rf "$HOME/Desktop/sorted.app"; fi
  local script; script="$(mktemp -t sorted-desktop-icon).applescript"
  # $launcher is expanded now, so the icon carries the absolute path of this folder.
  cat > "$script" <<APPLESCRIPT
on run
	do shell script quoted form of "$launcher" & " > /dev/null 2>&1 &"
end run
on open theFiles
	set f to item 1 of theFiles
	do shell script quoted form of "$launcher" & " " & quoted form of POSIX path of f & " > /dev/null 2>&1 &"
end open
APPLESCRIPT
  if ! osacompile -o "$desk" "$script" >/dev/null 2>&1; then
    note "could not build the Desktop icon, skipping it (the app still runs from $REPO/sorted.app)"; rm -rf "$desk" "$script"; return 0
  fi
  rm -f "$script"
  cp "$src/Contents/Resources/sorted.icns" "$desk/Contents/Resources/droplet.icns"
  rm -f "$desk/Contents/Resources/Assets.car"
  printf '%s\n' "$REPO" > "$desk/$marker"
  local plist="$desk/Contents/Info.plist" pb=/usr/libexec/PlistBuddy
  "$pb" -c "Delete :CFBundleIconName" "$plist" >/dev/null 2>&1
  "$pb" -c "Delete :CFBundleDocumentTypes" "$plist" >/dev/null 2>&1
  # One PlistBuddy call per key: many -c in one call abort part way on macOS 15.
  local ok=1 c
  for c in "Set :CFBundleName sorted" \
           "Add :CFBundleIdentifier string in.craywingz.sorted.desktop" \
           "Add :LSUIElement bool true" \
           "Add :CFBundleDocumentTypes array" \
           "Add :CFBundleDocumentTypes:0 dict" \
           "Add :CFBundleDocumentTypes:0:CFBundleTypeName string 'sorted project'" \
           "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Editor" \
           "Add :CFBundleDocumentTypes:0:LSHandlerRank string Owner" \
           "Add :CFBundleDocumentTypes:0:LSItemContentTypes array" \
           "Add :CFBundleDocumentTypes:0:LSItemContentTypes:0 string in.craywingz.sorted.project" \
           "Add :UTExportedTypeDeclarations array" \
           "Add :UTExportedTypeDeclarations:0 dict" \
           "Add :UTExportedTypeDeclarations:0:UTTypeIdentifier string in.craywingz.sorted.project" \
           "Add :UTExportedTypeDeclarations:0:UTTypeDescription string 'sorted project'" \
           "Add :UTExportedTypeDeclarations:0:UTTypeIconFile string droplet" \
           "Add :UTExportedTypeDeclarations:0:UTTypeConformsTo array" \
           "Add :UTExportedTypeDeclarations:0:UTTypeConformsTo:0 string public.data" \
           "Add :UTExportedTypeDeclarations:0:UTTypeTagSpecification dict" \
           "Add :UTExportedTypeDeclarations:0:UTTypeTagSpecification:public.filename-extension array" \
           "Add :UTExportedTypeDeclarations:0:UTTypeTagSpecification:public.filename-extension:0 string sorted"; do
    "$pb" -c "$c" "$plist" >/dev/null 2>&1 || ok=0
  done
  [ "$ok" = 1 ] || note "the Desktop icon is there but the Finder may not know project files yet"
  xattr -dr com.apple.quarantine "$desk" 2>/dev/null
  # Tell the Finder about the icon and the file type now, not at the next login.
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$desk" >/dev/null 2>&1
  touch "$desk"
  DESKTOP_ICON="$desk"
  note "sorted.app is in ~/Applications; it starts the app in $REPO, and opens sorted_<shoot>.sorted files by double-click"
  # The Desktop gets an alias (a Finder alias, so it shows the icon and survives the app being rebuilt).
  # Deleting the alias loses nothing: Spotlight (command space, "sorted") still finds the app.
  if [ -d "$HOME/Desktop" ]; then
    rm -f "$HOME/Desktop/sorted" "$HOME/Desktop/sorted alias" 2>/dev/null
    if osascript -e "tell application \"Finder\" to make new alias file at (POSIX file \"$HOME/Desktop\" as alias) to (POSIX file \"$desk\" as alias) with properties {name:\"sorted\"}" >/dev/null 2>&1; then
      note "a sorted alias is on the Desktop"
    else
      note "could not put an alias on the Desktop (Finder said no); open sorted from Spotlight or ~/Applications"
    fi
  fi
}
step "App icon"
desktop_icon

# 9. Count the install ---------------------------------------------------------------------------
# One anonymous ping to the beta counter, only once everything above succeeded. It carries a random
# id kept in .install-id here (so a re-run counts as an update, not a new install), the macOS
# version and the chip name. No name, no path, no photo. The app itself has no network code at all.
# Failure is silent: a Mac that is offline still gets a working install.
count_install() {
  local url="https://woasffpwdbwavwcrtllz.supabase.co/rest/v1/rpc/install_ping"
  local key="sb_publishable_aAR90Pzf1gza49CP2t7uKQ_xyDhkTZI"
  local idf="$REPO/.install-id" id kind macos chip body
  printf 'Counting this install (one anonymous ping, the app itself never phones home).\n'
  if [ -s "$idf" ] && grep -Eq '^[0-9a-f]{32}$' "$idf"; then
    kind="update"
  else
    uuidgen | tr -d - | tr A-F a-f > "$idf" 2>/dev/null || return 0
    kind="install"
  fi
  id="$(head -c 32 "$idf" 2>/dev/null)"
  [ "${#id}" = 32 ] || return 0
  macos="$(sw_vers -productVersion 2>/dev/null | tr -d '"\\' | head -c 40)"
  chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null | tr -d '"\\' | head -c 40)"
  body="$(printf '{"p_install_id":"%s","p_kind":"%s","p_macos":"%s","p_chip":"%s"}' "$id" "$kind" "$macos" "$chip")"
  curl -s -o /dev/null --max-time 5 -X POST "$url" \
    -H "apikey: $key" -H "authorization: Bearer $key" -H "content-type: application/json" \
    -d "$body" >/dev/null 2>&1 || true
  return 0
}
count_install

# 10. Open it -----------------------------------------------------------------------------------
printf '\nfinished: %s\n' "$(date)"
if [ -n "$DESKTOP_ICON" ]; then
  printf '\nsorted is installed. Open it any time from the sorted icon on your Desktop, or press command space and type sorted.\n\n'
else
  printf '\nsorted is installed. Double-click sorted.app in %s to start.\n\n' "$REPO"
fi
if [ -z "${SORTED_INSTALL_NO_OPEN:-}" ]; then
  open "$REPO/sorted.app"
fi
