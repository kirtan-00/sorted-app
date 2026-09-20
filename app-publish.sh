#!/bin/bash
# Push the current tree to github.com/kirtan-00/sorted-app as one commit (no history, screenshots
# and scratch dirs excluded by .gitignore). The full history stays in this repo until it is scrubbed.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PUB="$HERE/../app-public"
cd "$HERE"
find "$PUB" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git archive HEAD | tar -x -C "$PUB"
cd "$PUB"
git add -A && git add -f models/*.onnx
git commit -q -m "${1:-update}" || { echo "nothing to publish"; exit 0; }
git push -q origin main
echo "published: https://github.com/kirtan-00/sorted-app"
