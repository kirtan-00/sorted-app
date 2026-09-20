#!/bin/bash
# Install sorted.command: one double-click sets up everything sorted needs.
#
# What it does, in order (each step is skipped when already done, so a second run is fast):
#   1. refuses Intel Macs and macOS older than 14
#   2. installs uv (the Python package manager) into ~/.local/bin if it is missing
#   3. makes .venv here with uv's own arm64 Python 3.11 (never the Mac's python.org universal one,
#      which LaunchServices can start as x86_64 and then no wheel loads)
#   4. installs sorted and its libraries into .venv (runtime only, no test tools)
#   5. fetches the models: MobileCLIP-S1 into the Hugging Face cache (the face models ship in models/)
#      and proves they load
#   6. ffmpeg + ffprobe for videos: Homebrew's if there is a Homebrew, otherwise a static arm64
#      build dropped into .venv/bin
#   7. opens sorted.app
#
# Touches only: this folder (.venv, .uv, install.log), ~/.local/bin (uv) and the Hugging Face
# cache (~/.cache/huggingface). uv's own download cache and its Python live in .uv/ here, not in
# your home folder. Nothing is added to your shell profile.
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

printf '\nfinished: %s\n' "$(date)"
printf '\nsorted is installed. Double-click sorted.app to start.\n\n'
if [ -z "${SORTED_INSTALL_NO_OPEN:-}" ]; then
  open "$REPO/sorted.app"
fi
