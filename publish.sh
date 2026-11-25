#!/usr/bin/env bash
# Nightly publish: build the results page, scan it for secrets, push docs/ to GitHub.
# Run on the server that holds the bot databases. Your real settings file stays OUTSIDE the repo.
#
#   RESULTS_CONFIG   path to your private settings file
#                    (default: ~/.config/neextdrop/results_config.json)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
CONFIG="${RESULTS_CONFIG:-$HOME/.config/neextdrop/results_config.json}"

if [ ! -f "$CONFIG" ]; then
  echo "Settings file not found: $CONFIG" >&2
  exit 1
fi

# Build into a scratch folder first, so a failed or unsafe build never touches docs/.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
python3 build_results.py --config "$CONFIG" --out "$TMP/site"

# Secret guard: refuse to publish if the output looks like it contains credentials.
PATTERN='api[_-]?key|secret|passw(or)?d|BEGIN [A-Z ]*PRIVATE KEY|[0-9]{8,10}:[A-Za-z0-9_-]{35}|sk-[A-Za-z0-9]{20,}|/home/[a-z0-9_-]+/|/mnt/'
if grep -rIinoE "$PATTERN" "$TMP/site"; then
  echo "Possible secret or server path found in the output. NOT publishing." >&2
  exit 2
fi

# Replace docs/ content with the fresh build (keep .nojekyll).
find docs -mindepth 1 ! -name '.nojekyll' -delete
cp -r "$TMP/site/." docs/
touch docs/.nojekyll

git add -A docs
if git diff --cached --quiet; then
  echo "No changes to publish."
  exit 0
fi
git commit -q -m "Update results $(date -u +%Y-%m-%d)"
git push -q origin HEAD
echo "Published."
