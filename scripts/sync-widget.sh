#!/usr/bin/env bash
# Copy the Talpy Den widget's backend and Talpy (the pet) into vendor/.
#
#   scripts/sync-widget.sh PATH_TO_WIDGET_CHECKOUT [COMMIT]
#
# The widget (github.com/wzaielamri/talpy-den) is the one source
# of the Python backend and of Talpy. Only committed files are copied
# (git archive), never a working tree's uncommitted changes or __pycache__,
# and vendor/WIDGET_COMMIT records which commit they came from.
set -euo pipefail

src=${1:?usage: scripts/sync-widget.sh PATH_TO_WIDGET_CHECKOUT [COMMIT]}
rev=${2:-HEAD}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [ "$rev" = HEAD ] && [ -n "$(git -C "$src" status --porcelain)" ]; then
  echo "The widget checkout has uncommitted changes; commit them first (or name a COMMIT)." >&2
  exit 1
fi
commit=$(git -C "$src" rev-parse --verify "$rev^{commit}")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$src" archive "$commit" package/contents/code package/contents/ui/talpy package/contents/ui/codeview.mjs \
  package/contents/ui/codeview.d.mts statusline-snippet.sh | tar -x -C "$work"

rm -rf "$here/vendor/code" "$here/vendor/pets" "$here/vendor/talpy" "$here/vendor/codeview.mjs" "$here/vendor/codeview.d.mts"
mkdir -p "$here/vendor"
cp -r "$work/package/contents/code" "$here/vendor/code"
cp -r "$work/package/contents/ui/talpy" "$here/vendor/talpy"
cp "$work/package/contents/ui/codeview.mjs" "$work/package/contents/ui/codeview.d.mts" "$here/vendor/"
cp "$work/statusline-snippet.sh" "$here/vendor/statusline-snippet.sh"
echo "$commit" > "$here/vendor/WIDGET_COMMIT"
echo "vendor/ now holds the widget at ${commit:0:12}."
