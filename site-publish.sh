#!/bin/bash
# Push site/ to github.com/kirtan-00/sorted (GitHub Pages, https://kirtan-00.github.io/sorted/).
# The public repo holds only the site, one commit per publish, no app history and no client material.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PUB="$HERE/../site-public"
rsync -a --delete --exclude .git "$HERE/site/" "$PUB/"
touch "$PUB/.nojekyll"
cd "$PUB"
git add -A
git commit -q -m "site: ${1:-update}" || { echo "nothing to publish"; exit 0; }
git push -q origin main
echo "published: https://kirtan-00.github.io/sorted/"
