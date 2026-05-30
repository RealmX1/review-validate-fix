#!/usr/bin/env bash
# sync-manifest.sh —— 校验跨 harness 的三份 manifest 字段不变量保持一致（fail-fast）。
#
# 校验对象（(M+N) Marketplace + Nested 形态，详见 docs/architecture/cross-harness.md）：
#   1. plugins/review-validate-fix/.claude-plugin/plugin.json   Claude Code nested manifest
#   2. plugins/review-validate-fix/.codex-plugin/plugin.json    Codex nested manifest
#   3. .claude-plugin/marketplace.json                          源仓库 marketplace（plugins[0]）
#
# 强不变量（任一不成立即 exit 1）：
#   - plugin id（name）：三处必须完全一致（反模式 ② Plugin-id 漂移护栏）。
#   - version：两份 nested plugin.json 必须一致（marketplace plugins[0] 不带 version，跳过该处）。
#   - marketplace.plugins[0].source 必须指向 ./plugins/review-validate-fix。
#   - 三处 description 均非空。
#
# 有意不强校验（host-specific 设计，不是漂移）：
#   - description **内容**跨 host 故意不同（Claude 文案提 Cline Kanban、Codex 提 Codex
#     workspaces、marketplace 面向 Claude Code 安装）——只校验非空，不校验相等。
#   - skills / commands 字段形态（Claude 用数组、Codex 用字符串）属各 host resolver 约定的
#     格式差异，不在本脚本同步范围。
#
# 用法：
#   bash scripts/sync-manifest.sh            # 静默校验，通过则打印简短成功信息
#   bash scripts/sync-manifest.sh --verbose  # 额外打印每个被比对字段的实际值
set -euo pipefail

verbose=0
for arg in "$@"; do
  case "$arg" in
    --verbose|-v) verbose=1 ;;
    *)
      printf 'sync-manifest: 未知参数 %s\n' "$arg" >&2
      exit 2
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

claude_manifest="$repo_root/plugins/review-validate-fix/.claude-plugin/plugin.json"
codex_manifest="$repo_root/plugins/review-validate-fix/.codex-plugin/plugin.json"
marketplace="$repo_root/.claude-plugin/marketplace.json"
expected_source="./plugins/review-validate-fix"

if ! command -v jq >/dev/null 2>&1; then
  printf 'sync-manifest: 需要 jq，但未在 PATH 上找到\n' >&2
  exit 2
fi

fail() {
  printf 'manifest 不一致: %s\n' "$1" >&2
  exit 1
}

for f in "$claude_manifest" "$codex_manifest" "$marketplace"; do
  [ -f "$f" ] || fail "缺少 manifest 文件 ${f#"$repo_root"/}"
  jq empty "$f" >/dev/null 2>&1 || fail "非法 JSON：${f#"$repo_root"/}"
done

claude_name="$(jq -r '.name // ""' "$claude_manifest")"
codex_name="$(jq -r '.name // ""' "$codex_manifest")"
mkt_name="$(jq -r '.plugins[0].name // ""' "$marketplace")"

claude_version="$(jq -r '.version // ""' "$claude_manifest")"
codex_version="$(jq -r '.version // ""' "$codex_manifest")"

mkt_source="$(jq -r '.plugins[0].source // ""' "$marketplace")"

claude_desc="$(jq -r '.description // ""' "$claude_manifest")"
codex_desc="$(jq -r '.description // ""' "$codex_manifest")"
mkt_desc="$(jq -r '.plugins[0].description // ""' "$marketplace")"

# 不变量 1：plugin id 三处一致且非空。
[ -n "$claude_name" ] || fail "Claude nested manifest 的 name 为空"
if [ "$claude_name" != "$codex_name" ]; then
  fail "plugin id 漂移：Claude='$claude_name' vs Codex='$codex_name'"
fi
if [ "$claude_name" != "$mkt_name" ]; then
  fail "plugin id 漂移：plugin.json='$claude_name' vs marketplace.plugins[0]='$mkt_name'"
fi

# 不变量 2：两份 nested plugin.json 的 version 一致且非空（marketplace 无 version，跳过）。
[ -n "$claude_version" ] || fail "Claude nested manifest 的 version 为空"
if [ "$claude_version" != "$codex_version" ]; then
  fail "version 漂移：Claude='$claude_version' vs Codex='$codex_version'"
fi

# 不变量 3：marketplace.plugins[0].source 指向 nested plugin payload。
if [ "$mkt_source" != "$expected_source" ]; then
  fail "marketplace.plugins[0].source='$mkt_source'，期望 '$expected_source'"
fi

# 不变量 4：三处 description 均非空（内容有意 host-specific，不校验相等）。
[ -n "$claude_desc" ] || fail "Claude nested manifest 的 description 为空"
[ -n "$codex_desc" ] || fail "Codex nested manifest 的 description 为空"
[ -n "$mkt_desc" ] || fail "marketplace.plugins[0].description 为空"

if [ "$verbose" -eq 1 ]; then
  printf 'name      : %s（三处一致）\n' "$claude_name"
  printf 'version   : %s（两份 plugin.json 一致；marketplace 不带 version）\n' "$claude_version"
  printf 'source    : %s\n' "$mkt_source"
  printf 'desc.claude    : %s\n' "$claude_desc"
  printf 'desc.codex     : %s\n' "$codex_desc"
  printf 'desc.marketplace: %s\n' "$mkt_desc"
  printf '（description 内容有意 host-specific，仅校验非空）\n'
fi

printf 'manifest 同步检查通过：name/version/source 一致，description 均非空\n'
