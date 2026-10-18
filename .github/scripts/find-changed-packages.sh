#!/usr/bin/env bash
# List the packages (top-level directories) with changed published files
# between two commits. Published = every non-hidden file directly inside a
# package directory (the same set publish.yml syncs to the AUR).
# Output: JSON array, e.g. ["foo","bar"].
#
# Usage: find-changed-packages.sh <base> <head>
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <base> <head>" >&2
  exit 2
fi

base=$1
head=$2

# On the first push of a branch the base SHA is all zeros.
if [[ $base =~ ^0+$ ]]; then
  base=$(git mktree </dev/null)
fi

git diff --name-only --diff-filter=ACMR "$base" "$head" |
  awk -F/ 'NF == 2 && $1 !~ /^\./ && $2 !~ /^\./ { print $1 }' |
  LC_ALL=C sort -u |
  jq --raw-input --slurp --compact-output 'split("\n") | map(select(length > 0))'
