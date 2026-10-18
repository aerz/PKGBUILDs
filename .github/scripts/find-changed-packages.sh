#!/usr/bin/env bash
# List the packages (top-level directories) with changed published files
# between two commits. Published = every non-hidden file directly inside a
# package directory (the same set publish.yml syncs to the AUR). Packages
# listed in .github/excluded-packages are filtered out.
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

exclude_file="$(dirname -- "${BASH_SOURCE[0]}")/../excluded-packages"
excluded=""
if [[ -f "$exclude_file" ]]; then
  excluded=$(sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d' "$exclude_file")
fi

git diff --name-only --diff-filter=ACMR "$base" "$head" |
  awk -F/ -v excluded="$excluded" '
    BEGIN {
      n = split(excluded, names, "\n")
      for (i = 1; i <= n; i++) skip[names[i]] = 1
    }
    NF == 2 && $1 !~ /^\./ && $2 !~ /^\./ && !($1 in skip) { print $1 }
  ' |
  LC_ALL=C sort -u |
  jq --raw-input --slurp --compact-output 'split("\n") | map(select(length > 0))'
