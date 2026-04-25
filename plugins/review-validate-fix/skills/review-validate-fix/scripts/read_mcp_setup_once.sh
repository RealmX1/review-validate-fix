#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
doc="$skill_dir/setup/mcp-setup-startup.md"
marker="${CODEX_RVF_MCP_SETUP_MARKER:-$skill_dir/state/mcp-setup-startup.viewed}"
force=0

if [ "${1:-}" = "--force" ]; then
  force=1
elif [ $# -gt 0 ]; then
  printf '用法: %s [--force]\n' "$0" >&2
  exit 2
fi

if [ ! -f "$doc" ]; then
  printf '缺少 setup-only 文档: %s\n' "$doc" >&2
  exit 1
fi

if [ -f "$marker" ] && [ "$force" -ne 1 ]; then
  printf 'setup/mcp-setup-startup.md 已读取过；正常运行 $review-validate-fix 时不要再次读取或引用它。\n'
  printf '如用户明确要求重新 setup 或更换 alternative reviewer，请运行: %s --force\n' "$0"
  exit 3
fi

mkdir -p "$(dirname "$marker")"
{
  date -u '+viewed_at=%Y-%m-%dT%H:%M:%SZ'
  printf 'doc=%s\n' "$doc"
} > "$marker"

cat "$doc"
