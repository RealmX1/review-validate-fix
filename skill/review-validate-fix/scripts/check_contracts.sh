#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

hash_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$file" | awk '{print $NF}'
  else
    printf 'NO_HASH_TOOL'
  fi
}

require_file() {
  local file="$1"
  if [ ! -f "$skill_dir/$file" ]; then
    printf '缺少文件: %s\n' "$file" >&2
    exit 1
  fi
}

require_literal() {
  local file="$1"
  local literal="$2"
  if ! grep -Fq -- "$literal" "$skill_dir/$file"; then
    printf '契约缺失: %s 中找不到 %s\n' "$file" "$literal" >&2
    exit 1
  fi
}

forbid_literal() {
  local file="$1"
  local literal="$2"
  if grep -Fq -- "$literal" "$skill_dir/$file"; then
    printf '禁止的旧契约仍存在: %s 中不应出现 %s\n' "$file" "$literal" >&2
    exit 1
  fi
}

required_files=(
  "SKILL.md"
  "references/legacy-claude-command.md"
  "references/legacy-claude-stop-hook.md"
  "references/legacy-claude-mark-activity.sh"
  "references/legacy-compatibility-notes.md"
  "references/review-merge-policy.md"
  "references/review-prompt.md"
  "references/validate-then-fix-prompt.md"
  "references/handoff-template.md"
  "config/alternative-reviewer.json"
  "setup/mcp-setup-startup.md"
  "scripts/review_validate_fix_gate.sh"
  "scripts/run_alternative_reviewer.py"
  "scripts/build_review_packet.py"
  "scripts/check_review_output.py"
  "scripts/command_lock.py"
  "scripts/prepare_review_run.py"
  "scripts/workspace_snapshot.py"
  "scripts/codex_stop_review_validate_fix.py"
  "scripts/test_codex_stop_review_validate_fix.py"
  "scripts/test_review_support_scripts.py"
  "scripts/discover_santa_alternative_agents.sh"
  "scripts/read_mcp_setup_once.sh"
  "scripts/check_contracts.sh"
  "scripts/parse_elevation_detail.py"
  "agents/openai.yaml"
)

for file in "${required_files[@]}"; do
  require_file "$file"
done

if [ -e "$skill_dir/references/mcp-setup-startup.md" ]; then
  printf 'setup-only 文档不应位于运行期 references/: references/mcp-setup-startup.md\n' >&2
  exit 1
fi

for script in \
  "scripts/review_validate_fix_gate.sh" \
  "scripts/discover_santa_alternative_agents.sh" \
  "scripts/read_mcp_setup_once.sh" \
  "scripts/check_contracts.sh"
do
  bash -n "$skill_dir/$script"
done

python3 -m py_compile \
  "$skill_dir/scripts/run_alternative_reviewer.py" \
  "$skill_dir/scripts/build_review_packet.py" \
  "$skill_dir/scripts/check_review_output.py" \
  "$skill_dir/scripts/command_lock.py" \
  "$skill_dir/scripts/prepare_review_run.py" \
  "$skill_dir/scripts/workspace_snapshot.py" \
  "$skill_dir/scripts/codex_stop_review_validate_fix.py" \
  "$skill_dir/scripts/test_codex_stop_review_validate_fix.py" \
  "$skill_dir/scripts/test_review_support_scripts.py"

python3 "$skill_dir/scripts/test_review_support_scripts.py"

python3 - "$skill_dir/references/handoff-template.md" <<'PY'
import re
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
if not re.search(
    r"```markdown\n<handoff-context>\n[\s\S]*?</handoff-context>\n```",
    template,
):
    print(
        "契约缺失: handoff-template.md 必须用单个 ```markdown fenced block "
        "包裹完整 <handoff-context>...</handoff-context>",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY

require_literal "references/review-prompt.md" 'NO_ISSUES'
require_literal "references/validate-then-fix-prompt.md" 'REAL'
require_literal "references/validate-then-fix-prompt.md" 'FALSE_POSITIVE'
require_literal "references/validate-then-fix-prompt.md" 'ELEVATE'
require_literal "references/validate-then-fix-prompt.md" 'elevation-detail'
require_literal "references/handoff-template.md" '<handoff-context>'
require_literal "references/handoff-template.md" '</handoff-context>'
require_literal "references/handoff-template.md" '```markdown'
require_literal "SKILL.md" 'fenced markdown code block'
require_literal "SKILL.md" '不要把 `<handoff-context>` 作为未包裹的裸标签输出'
require_literal "SKILL.md" '运行选项'
require_literal "SKILL.md" '默认开启 review'
require_literal "SKILL.md" '默认开启 handoff'
require_literal "SKILL.md" 'SKIPPED_BY_USER'
require_literal "SKILL.md" 'user-supplied-skip-review'
require_literal "references/handoff-template.md" 'Handoff 默认开启'
require_literal "references/handoff-template.md" 'review_status'
require_literal "references/handoff-template.md" 'user-supplied-skip-review'
require_literal "agents/openai.yaml" 'skip review'
require_literal "agents/openai.yaml" 'no handoff'
require_literal "SKILL.md" 'validate-review 子代理'
require_literal "references/legacy-compatibility-notes.md" 'Stop hook 触发点是会话内部自动事件'
require_literal "SKILL.md" 'santa-method double review'
require_literal "SKILL.md" 'config/alternative-reviewer.json'
require_literal "SKILL.md" 'scripts/run_alternative_reviewer.py --check'
require_literal "SKILL.md" 'scripts/build_review_packet.py --repo <repo>'
require_literal "SKILL.md" 'scripts/prepare_review_run.py --repo <repo>'
require_literal "SKILL.md" 'scripts/check_review_output.py'
require_literal "SKILL.md" 'scripts/command_lock.py'
require_literal "SKILL.md" 'scripts/workspace_snapshot.py capture/compare'
require_literal "references/review-merge-policy.md" 'scripts/run_alternative_reviewer.py --repo <repo> --review-packet <packet> --session-context <file>'
require_literal "config/alternative-reviewer.json" 'alternative-reviewer:claude-code'
require_literal "config/alternative-reviewer.json" 'ANTHROPIC_API_KEY'
require_literal "config/alternative-reviewer.json" 'Bash,Read'
require_literal "config/alternative-reviewer.json" 'allow_repo_cwd'
require_literal "config/alternative-reviewer.json" 'no-direct-write'
require_literal "config/alternative-reviewer.json" 'RVF_LOCK_REQUEST'
require_literal "config/alternative-reviewer.json" 'idle_timeout_seconds'
require_literal "config/alternative-reviewer.json" 'activity_check_interval_seconds'
require_literal "scripts/run_alternative_reviewer.py" 'env_unset'
require_literal "scripts/run_alternative_reviewer.py" 'normalize_review_output'
require_literal "scripts/run_alternative_reviewer.py" 'review_packet'
require_literal "scripts/run_alternative_reviewer.py" '--preflight'
require_literal "scripts/run_alternative_reviewer.py" 'RVF_COMMAND_LOCK'
require_literal "scripts/run_alternative_reviewer.py" 'RVF_EXTERNAL_REVIEWER_TIMEOUT'
require_literal "scripts/check_review_output.py" 'NO_ISSUES'
require_literal "scripts/check_review_output.py" 'RVF_LOCK_REQUEST'
require_literal "scripts/check_review_output.py" 'lock_request'
require_literal "scripts/command_lock.py" 'fcntl.flock'
require_literal "scripts/command_lock.py" 'RVF_LOCK_DIR'
require_literal "scripts/prepare_review_run.py" 'review-packet.metadata.json'
require_literal "scripts/build_review_packet.py" 'metadata-output'
require_literal "scripts/build_review_packet.py" 'primary-file'
require_literal "scripts/build_review_packet.py" 'max-packet-bytes'
require_literal "scripts/workspace_snapshot.py" 'WORKSPACE_CHANGED'
require_literal "references/review-merge-policy.md" 'codex-reviewer'
require_literal "references/review-merge-policy.md" 'alternative-reviewer:<agent-name>'
require_literal "references/review-merge-policy.md" 'codex-mimic-reviewer-a'
require_literal "references/review-merge-policy.md" 'source-agnostic'
require_literal "references/review-merge-policy.md" 'CONTRACT_VIOLATION'
require_literal "references/review-merge-policy.md" 'WORKSPACE_CHANGED_DURING_REVIEW'
require_literal "references/review-merge-policy.md" 'RVF_LOCK_REQUEST'
require_literal "references/review-prompt.md" 'RVF_LOCK_REQUEST'
require_literal "references/review-prompt.md" 'command lock'
require_literal "references/validate-then-fix-prompt.md" 'source-agnostic'
require_literal "references/validate-then-fix-prompt.md" '不要生成 handoff'
require_literal "setup/mcp-setup-startup.md" '先问用户'
require_literal "setup/mcp-setup-startup.md" '没有可用 alternative reviewer'
require_literal "SKILL.md" '默认使用 Codex-only fallback'
require_literal "SKILL.md" '不要询问用户、不要中断 review loop、不要降级为单 reviewer'
require_literal "references/review-merge-policy.md" '默认使用 Codex-only fallback'
require_literal "setup/mcp-setup-startup.md" 'setup 未完成不能阻塞'
forbid_literal "SKILL.md" '用户明确声明没有任何额外 coding agent'
forbid_literal "references/review-merge-policy.md" '用户明确声明没有其他 coding agent'
forbid_literal "setup/mcp-setup-startup.md" '用户也明确声明没有额外 coding agent'
require_literal "SKILL.md" 'Setup-only 资源'
require_literal "SKILL.md" '正常执行 `$review-validate-fix`'
require_literal "SKILL.md" '不得读取、引用或总结 `setup/mcp-setup-startup.md`'
require_literal "scripts/read_mcp_setup_once.sh" '已读取过'
require_literal "scripts/discover_santa_alternative_agents.sh" 'candidate_commands'
require_literal "agents/openai.yaml" 'allow_implicit_invocation: false'
require_literal "SKILL.md" '只应由用户显式调用'
require_literal "SKILL.md" 'Codex Stop hook'
require_literal "SKILL.md" 'thread/fork'
require_literal "SKILL.md" 'turn/start'
require_literal "SKILL.md" 'RVF_FORKED_REVIEW_VALIDATE_FIX'
require_literal "SKILL.md" 'CODEX_RVF_MODE=fork'
require_literal "SKILL.md" 'CODEX_RVF_MODE=continuation'
require_literal "SKILL.md" 'CODEX_RVF_FORK_MODE=manual'
require_literal "SKILL.md" 'CODEX_RVF_FORK_MODE=gui'
require_literal "SKILL.md" 'CODEX_RVF_FORK_REASONING_EFFORT'
require_literal "references/legacy-compatibility-notes.md" 'Codex Stop fork'
require_literal "references/legacy-compatibility-notes.md" 'Codex Stop continuation'
require_literal "references/legacy-compatibility-notes.md" 'decision: "block"'
require_literal "scripts/codex_stop_review_validate_fix.py" '$review-validate-fix'
require_literal "scripts/codex_stop_review_validate_fix.py" 'stop_hook_active'
require_literal "scripts/codex_stop_review_validate_fix.py" 'decision'
require_literal "scripts/codex_stop_review_validate_fix.py" 'block'
require_literal "scripts/codex_stop_review_validate_fix.py" 'systemMessage'
require_literal "scripts/codex_stop_review_validate_fix.py" 'RVF_FORK_EXPERIMENT'
require_literal "scripts/codex_stop_review_validate_fix.py" 'RVF_FORKED_REVIEW_VALIDATE_FIX'
require_literal "scripts/codex_stop_review_validate_fix.py" 'DEFAULT_RVF_MODE = "fork"'
require_literal "scripts/codex_stop_review_validate_fix.py" 'DEFAULT_FORK_LAUNCH_MODE = "gui"'
require_literal "scripts/codex_stop_review_validate_fix.py" 'CODEX_RVF_MODE'
require_literal "scripts/codex_stop_review_validate_fix.py" 'manual-prepared'
require_literal "scripts/codex_stop_review_validate_fix.py" 'no Terminal was launched'
require_literal "scripts/codex_stop_review_validate_fix.py" 'thread/fork'
require_literal "scripts/codex_stop_review_validate_fix.py" 'turn/start'
require_literal "scripts/codex_stop_review_validate_fix.py" 'Terminal/CLI fork launch is intentionally disabled'
require_literal "scripts/codex_stop_review_validate_fix.py" 'CODEX_RVF_FORK_REASONING_EFFORT'
require_literal "scripts/codex_stop_review_validate_fix.py" 'review-validate-fix-fork'
require_literal "scripts/codex_stop_review_validate_fix.py" '<handoff-context>'
require_literal "scripts/codex_stop_review_validate_fix.py" 'last_assistant_message'
require_literal "scripts/codex_stop_review_validate_fix.py" 'fork'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_fork_experiment_marker_dry_run'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_forks_in_gui_by_default'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_manual_mode_only_prepares_prompt'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_fork_dry_run'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_stop_event_transcript_path_overrides_bad_env_thread_id'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_continuation_mode'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_no_git_unique_dirty_trusted_repo_forks_by_default'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_forked_rvf_session_gets_programmatic_handoff_advisory'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_forked_rvf_session_waits_for_handoff_before_advisory'
require_literal "scripts/test_codex_stop_review_validate_fix.py" 'test_no_git_multiple_dirty_trusted_repos_skips'

printf 'contract check OK\n'
printf 'hashes:\n'
for file in "${required_files[@]}"; do
  printf '%s  %s\n' "$(hash_file "$skill_dir/$file")" "$file"
done
