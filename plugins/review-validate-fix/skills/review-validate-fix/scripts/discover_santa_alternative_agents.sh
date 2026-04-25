#!/usr/bin/env bash
set -euo pipefail

# This script is intentionally heuristic. It only surfaces candidates so the
# setup agent can ask the user which, if any, should become the alternative reviewer.

candidate_commands=(
  claude
  gemini
  aider
  opencode
  goose
  amp
  cursor-agent
  qwen
  qwen-code
)

printf '# Santa-method alternative reviewer discovery\n\n'

printf '## Candidate commands on PATH\n\n'
found_command=0
for cmd in "${candidate_commands[@]}"; do
  if path="$(command -v "$cmd" 2>/dev/null)"; then
    found_command=1
    printf -- '- `%s`: `%s`\n' "$cmd" "$path"
  fi
done
if [ "$found_command" -eq 0 ]; then
  printf 'No common coding-agent commands found on PATH.\n'
fi

printf '\n## MCP or agent-looking config files\n\n'
config_candidates=(
  "$HOME/.claude/settings.json"
  "$HOME/.gemini/settings.json"
  "$HOME/.lmstudio/mcp.json"
  "$HOME/.omlx/settings.json"
  "$HOME/.openclaw/settings.json"
  "$HOME/.cc-switch/settings.json"
)

config_files="$(
  {
    for file in "${config_candidates[@]}"; do
      [ -f "$file" ] && printf '%s\n' "$file"
    done
    find "$HOME" -maxdepth 4 \( -name '.mcp.json' -o -name 'mcp.json' \) -print 2>/dev/null || true
  } | awk 'NF && !seen[$0]++' | sort
)"

if [ -z "$config_files" ]; then
  printf 'No common MCP or agent config files found.\n'
else
  printf '%s\n' "$config_files" | while IFS= read -r file; do
    printf -- '- `%s`\n' "$file"
  done
fi

printf '\n## Next step\n\n'
printf 'Ask the user which candidate to use, or whether they have an additional coding agent not listed here.\n'
