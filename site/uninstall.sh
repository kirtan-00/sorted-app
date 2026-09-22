#!/bin/bash
# sorted for Mac, uninstall:
#   curl -fsSL https://kirtan-00.github.io/sorted/uninstall.sh | bash
# Stops the app, removes ~/sorted and the sorted icon on the Desktop. Your scans (what the app learned
# about each shoot, in ~/Library/Application Support/photosort) are kept, so a later install finds them.
# To remove those too:
#   curl -fsSL https://kirtan-00.github.io/sorted/uninstall.sh | DATA=1 bash
set -u
DEST="$HOME/sorted"
printf '\n==> uninstalling sorted\n'
for pid in $(ps -axo pid=,command= | grep -F -- "$DEST/.venv/bin/python -m photosort.cli serve" | grep -v grep | awk '{print $1}'); do kill "$pid" 2>/dev/null || true; done
ICON="$HOME/Desktop/sorted.app"
if [ -f "$ICON/Contents/Resources/sorted-installed-from" ]; then
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -u "$ICON" >/dev/null 2>&1 || true
  rm -rf "$ICON" && printf '    removed the Desktop icon\n'
elif [ -e "$ICON" ]; then
  printf '    %s was not made by the installer, leaving it alone\n' "$ICON"
fi
if [ -d "$DEST" ]; then rm -rf "$DEST" && printf '    removed %s\n' "$DEST"; else printf '    %s was not there\n' "$DEST"; fi
rm -f "$HOME/Library/Logs/photosort.log"
if [ "${DATA:-}" = "1" ] || [ "${1:-}" = "--data" ]; then
  rm -rf "$HOME/Library/Application Support/photosort" && printf '    removed the scans in ~/Library/Application Support/photosort\n'
else
  printf '    kept your scans in ~/Library/Application Support/photosort (put DATA=1 before bash in the line to remove them too)\n'
fi
printf '    done. The uv tool in ~/.local/bin and the Python it downloaded were left in place; they are small and shared with other tools.\n'
