#!/usr/bin/env bash
set -euo pipefail

repo="${1:-.}"

if ! cd "$repo" 2>/dev/null; then
  printf 'NOT_FOUND %s\n' "$repo"
  exit 2
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  printf 'NO_GIT %s\n' "$repo"
  exit 2
fi

status="$(git status --porcelain 2>/dev/null || true)"
if [ -z "$status" ]; then
  printf 'CLEAN %s\n' "$(git rev-parse --show-toplevel)"
  exit 0
fi

printf 'DIRTY %s\n' "$(git rev-parse --show-toplevel)"
git status --short
exit 1
