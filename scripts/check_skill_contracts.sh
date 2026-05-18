#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
skill_dir="$repo_root/plugins/review-validate-fix/skills/review-validate-fix"
tests_dir="$repo_root/tests"
verbose=0

usage() {
  printf 'Usage: %s [--verbose]\n' "$(basename "$0")"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -v|--verbose)
      verbose=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

timestamp_ms() {
  python3 -c 'import time; print(time.time_ns() // 1000000)'
}

record_step_timing() {
  local label="$1"
  local command_status="$2"
  local duration_ms="$3"
  local execution_mode="${4:-serial}"
  if [ -n "${RVF_CONTRACT_TIMING_JSONL:-}" ] && [ "${RVF_CONTRACT_TIMING_SCRIPT:-}" = "$0" ]; then
    python3 - "$RVF_CONTRACT_TIMING_JSONL" "$label" "$command_status" "$duration_ms" "$execution_mode" <<'PY' || true
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
label = sys.argv[2]
returncode = int(sys.argv[3])
duration_ms = max(0, int(sys.argv[4]))
execution_mode = sys.argv[5]
record = {
    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "label": label,
    "status": "completed" if returncode == 0 else "failed",
    "returncode": returncode,
    "duration_ms": duration_ms,
    "execution_mode": execution_mode,
}
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
PY
  fi
}

run_step() {
  local label="$1"
  shift
  local started_ms
  local ended_ms
  local duration_ms
  local command_status=0
  started_ms="$(timestamp_ms)"
  if [ "$verbose" -eq 1 ]; then
    printf '==> %s\n' "$label"
    "$@" || command_status=$?
  else
    local output_file
    output_file="$(mktemp)"
    "$@" >"$output_file" 2>&1 || command_status=$?
  fi

  ended_ms="$(timestamp_ms)"
  duration_ms=$((ended_ms - started_ms))
  record_step_timing "$label" "$command_status" "$duration_ms" "serial"

  if [ "$command_status" -eq 0 ]; then
    if [ "$verbose" -eq 0 ]; then
      rm -f "$output_file"
    fi
    return 0
  fi

  printf '验证失败: %s\n' "$label" >&2
  if [ "$verbose" -eq 0 ]; then
    cat "$output_file" >&2
    rm -f "$output_file"
  fi
  return "$command_status"
}

contract_parallel_tests_enabled() {
  case "${RVF_CONTRACT_PARALLEL_TESTS:-1}" in
    0|false|False|FALSE|no|No|NO|off|Off|OFF)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

contract_parallel_jobs() {
  local value="${RVF_CONTRACT_PARALLEL_JOBS:-8}"
  case "$value" in
    ''|*[!0-9]*)
      printf '8'
      ;;
    0)
      printf '1'
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

contract_review_support_shards() {
  local value="${RVF_CONTRACT_REVIEW_SUPPORT_SHARDS:-4}"
  case "$value" in
    ''|*[!0-9]*)
      printf '4'
      ;;
    0)
      printf '1'
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

contract_stop_hook_shards() {
  local value="${RVF_CONTRACT_STOP_HOOK_SHARDS:-4}"
  case "$value" in
    ''|*[!0-9]*)
      printf '4'
      ;;
    0)
      printf '1'
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

contract_dispatcher_shards() {
  local value="${RVF_CONTRACT_DISPATCHER_SHARDS:-1}"
  case "$value" in
    ''|*[!0-9]*)
      printf '1'
      ;;
    0)
      printf '1'
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

run_parallel_test_steps() {
  local jobs
  jobs="$(contract_parallel_jobs)"
  local review_support_shards
  local stop_hook_shards
  local dispatcher_shards
  if contract_parallel_tests_enabled && [ "$jobs" -gt 1 ]; then
    review_support_shards="$(contract_review_support_shards)"
    stop_hook_shards="$(contract_stop_hook_shards)"
    dispatcher_shards="$(contract_dispatcher_shards)"
  else
    review_support_shards=1
    stop_hook_shards=1
    dispatcher_shards=1
  fi

  local labels=()
  local scripts=()
  local args=()
  labels+=("tests: install_to_codex")
  scripts+=("$tests_dir/test_install_to_codex.py")
  args+=("")
  labels+=("tests: rvf_handoff_intake")
  scripts+=("$tests_dir/test_rvf_handoff_intake.py")
  args+=("")
  local shard_index
  for ((shard_index = 0; shard_index < review_support_shards; shard_index++)); do
    if [ "$review_support_shards" -eq 1 ]; then
      labels+=("tests: review_support_scripts")
      args+=("")
    else
      labels+=("tests: review_support_scripts shard $((shard_index + 1))/$review_support_shards")
      args+=("--shard-count $review_support_shards --shard-index $shard_index")
    fi
    scripts+=("$tests_dir/test_review_support_scripts.py")
  done
  for ((shard_index = 0; shard_index < dispatcher_shards; shard_index++)); do
    if [ "$dispatcher_shards" -eq 1 ]; then
      labels+=("tests: codex_stop_hook_dispatcher")
      args+=("")
    else
      labels+=("tests: codex_stop_hook_dispatcher shard $((shard_index + 1))/$dispatcher_shards")
      args+=("--shard-count $dispatcher_shards --shard-index $shard_index")
    fi
    scripts+=("$tests_dir/test_codex_stop_hook_dispatcher.py")
  done
  for ((shard_index = 0; shard_index < stop_hook_shards; shard_index++)); do
    if [ "$stop_hook_shards" -eq 1 ]; then
      labels+=("tests: codex_stop_review_validate_fix")
      args+=("")
    else
      labels+=("tests: codex_stop_review_validate_fix shard $((shard_index + 1))/$stop_hook_shards")
      args+=("--shard-count $stop_hook_shards --shard-index $shard_index")
    fi
    scripts+=("$tests_dir/test_codex_stop_review_validate_fix.py")
  done

  if ! contract_parallel_tests_enabled || [ "$jobs" -le 1 ]; then
    local serial_index
    for serial_index in "${!labels[@]}"; do
      if [ -n "${args[$serial_index]}" ]; then
        run_step "${labels[$serial_index]}" python3 "${scripts[$serial_index]}" ${args[$serial_index]}
      else
        run_step "${labels[$serial_index]}" python3 "${scripts[$serial_index]}"
      fi
    done
    return
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  local pids=()
  local active_indices=()
  local output_files=()
  local status_files=()
  local duration_files=()
  local index

  for index in "${!labels[@]}"; do
    output_files[$index]="$tmp_dir/$index.out"
    status_files[$index]="$tmp_dir/$index.status"
    duration_files[$index]="$tmp_dir/$index.duration"
    if [ "$verbose" -eq 1 ]; then
      printf '==> %s (parallel)\n' "${labels[$index]}"
    fi
    (
      command_status=0
      started_ms="$(timestamp_ms)"
      if [ -n "${args[$index]}" ]; then
        python3 "${scripts[$index]}" ${args[$index]} >"${output_files[$index]}" 2>&1 || command_status=$?
      else
        python3 "${scripts[$index]}" >"${output_files[$index]}" 2>&1 || command_status=$?
      fi
      ended_ms="$(timestamp_ms)"
      printf '%s\n' "$command_status" >"${status_files[$index]}"
      printf '%s\n' "$((ended_ms - started_ms))" >"${duration_files[$index]}"
      exit 0
    ) &
    pids[$index]=$!
    active_indices+=("$index")

    if [ "${#active_indices[@]}" -ge "$jobs" ]; then
      wait "${pids[${active_indices[0]}]}"
      active_indices=("${active_indices[@]:1}")
    fi
  done

  for index in "${active_indices[@]}"; do
    wait "${pids[$index]}"
  done

  local overall_status=0
  local command_status
  local duration_ms
  for index in "${!labels[@]}"; do
    command_status="$(cat "${status_files[$index]}")"
    duration_ms="$(cat "${duration_files[$index]}")"
    record_step_timing "${labels[$index]}" "$command_status" "$duration_ms" "parallel"
    if [ "$command_status" -eq 0 ]; then
      if [ "$verbose" -eq 1 ] && [ -s "${output_files[$index]}" ]; then
        cat "${output_files[$index]}"
      fi
      continue
    fi
    if [ "$overall_status" -eq 0 ]; then
      overall_status="$command_status"
    fi
    printf '验证失败: %s\n' "${labels[$index]}" >&2
    cat "${output_files[$index]}" >&2
  done

  rm -rf "$tmp_dir"
  return "$overall_status"
}

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

require_repo_file() {
  local file="$1"
  if [ ! -f "$repo_root/$file" ]; then
    printf '缺少仓库文件: %s\n' "$file" >&2
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

require_repo_literal() {
  local file="$1"
  local literal="$2"
  if ! grep -Fq -- "$literal" "$repo_root/$file"; then
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

forbid_repo_literal() {
  local file="$1"
  local literal="$2"
  if grep -Fq -- "$literal" "$repo_root/$file"; then
    printf '禁止的旧契约仍存在: %s 中不应出现 %s\n' "$file" "$literal" >&2
    exit 1
  fi
}

required_files=(
  "SKILL.md"
  "prompts/reviewer.md"
  "prompts/validate-fix.md"
  "protocols/README.md"
  "internals/README.md"
  "internals/stop-hook-workflow.md"
  "internals/runtime-contracts.md"
  "debug/troubleshooting.md"
  "references/review-merge-policy.md"
  "references/review-prompt.md"
  "references/review-standards/index.md"
  "references/review-standards/main-agent.md"
  "references/review-standards/reviewer.md"
  "references/review-standards/validate-fix.md"
  "references/review-standards/simplification-subset.md"
  "references/review-standards/security-subset.md"
  "references/review-standards/performance-subset.md"
  "references/review-standards/protocol-extensions.md"
  "references/validate-then-fix-prompt.md"
  "references/handoff-template.md"
  "references/cancel-rvf-run.md"
  "config/alternative-reviewer.json"
  "setup/mcp-setup-startup.md"
  "scripts/review_validate_fix_gate.sh"
  "scripts/run_alternative_reviewer.py"
  "scripts/build_review_packet.py"
  "scripts/write_review_result.py"
  "scripts/check_review_result.py"
  "scripts/command_lock.py"
  "scripts/prepare_review_run.py"
  "scripts/apply_worktree_bootstrap.py"
  "scripts/cline_kanban_client.py"
  "scripts/cancel_rvf_run.py"
  "scripts/rvf_logging.py"
  "scripts/rvf_prep_file.py"
  "scripts/rvf_user_prompt_submit.py"
  "scripts/rvf_handoff_intake.py"
  "scripts/session_manifest.py"
  "scripts/workspace_snapshot.py"
  "scripts/codex_stop_hook_dispatcher.py"
  "scripts/codex_stop_hook_router.py"
  "scripts/codex_stop_review_validate_fix.py"
  "scripts/discover_santa_alternative_agents.sh"
  "scripts/read_mcp_setup_once.sh"
  "scripts/parse_elevation_detail.py"
  "agents/openai.yaml"
)

for file in "${required_files[@]}"; do
  require_file "$file"
done

repo_required_files=(
  "plugins/review-validate-fix/commands/rvf-handoff-commit.md"
  "scripts/check_plugin_contracts.py"
  "scripts/check_skill_contracts.sh"
  "scripts/install_to_codex.py"
  "tests/test_codex_stop_hook_dispatcher.py"
  "tests/test_codex_stop_review_validate_fix.py"
  "tests/test_install_to_codex.py"
  "tests/test_review_support_scripts.py"
  "tests/test_rvf_handoff_intake.py"
)

for file in "${repo_required_files[@]}"; do
  require_repo_file "$file"
done

if [ -e "$skill_dir/references/mcp-setup-startup.md" ]; then
  printf 'setup-only 文档不应位于运行期 references/: references/mcp-setup-startup.md\n' >&2
  exit 1
fi

if find "$skill_dir/scripts" -maxdepth 1 -type f \( -name 'test_*.py' -o -name '*_test.py' \) | grep -q .; then
  printf '测试脚本不应位于 plugin 运行期 scripts/ 目录\n' >&2
  exit 1
fi

if find "$skill_dir" -path "$skill_dir/state" -prune -o \( \
  -name 'install_to_codex.py' -o \
  -name 'check_plugin_contracts.py' -o \
  -name 'check_skill_contracts.sh' -o \
  -path '*/dev-only/*' -o \
  -path '*/dev_only/*' -o \
  -path '*/.rvf-dev-only/*' \
\) -print | grep -q .; then
  printf 'dev-only 文件不应位于可部署 plugin runtime 中\n' >&2
  exit 1
fi

for script in \
  "check_contracts.sh" \
  "check_plugin_contracts.py" \
  "check_skill_contracts.sh"
do
  if [ -e "$skill_dir/scripts/$script" ]; then
    printf '仓库级契约脚本不应位于 plugin 运行期 scripts/ 目录: scripts/%s\n' "$script" >&2
    exit 1
  fi
done

for script in \
  "scripts/review_validate_fix_gate.sh" \
  "scripts/discover_santa_alternative_agents.sh" \
  "scripts/read_mcp_setup_once.sh"
do
  run_step "shell syntax: $script" bash -n "$skill_dir/$script"
done
run_step "shell syntax: scripts/check_skill_contracts.sh" bash -n "$repo_root/scripts/check_skill_contracts.sh"

run_step "python compile" python3 -m py_compile \
  "$repo_root/scripts/check_plugin_contracts.py" \
  "$repo_root/scripts/install_to_codex.py" \
  "$skill_dir/scripts/run_alternative_reviewer.py" \
  "$skill_dir/scripts/build_review_packet.py" \
  "$skill_dir/scripts/write_review_result.py" \
  "$skill_dir/scripts/check_review_result.py" \
  "$skill_dir/scripts/command_lock.py" \
  "$skill_dir/scripts/prepare_review_run.py" \
  "$skill_dir/scripts/apply_worktree_bootstrap.py" \
  "$skill_dir/scripts/cline_kanban_client.py" \
  "$skill_dir/scripts/cancel_rvf_run.py" \
  "$skill_dir/scripts/rvf_logging.py" \
  "$skill_dir/scripts/rvf_prep_file.py" \
  "$skill_dir/scripts/rvf_user_prompt_submit.py" \
  "$skill_dir/scripts/rvf_handoff_intake.py" \
  "$skill_dir/scripts/session_manifest.py" \
  "$skill_dir/scripts/workspace_snapshot.py" \
  "$skill_dir/scripts/codex_stop_hook_dispatcher.py" \
  "$skill_dir/scripts/codex_stop_review_validate_fix.py" \
  "$tests_dir/test_codex_stop_hook_dispatcher.py" \
  "$tests_dir/test_codex_stop_review_validate_fix.py" \
  "$tests_dir/test_install_to_codex.py" \
  "$tests_dir/test_review_support_scripts.py" \
  "$tests_dir/test_rvf_handoff_intake.py"

run_parallel_test_steps

require_literal "prompts/reviewer.md" 'RVF_WRITE_REVIEW_RESULT'
require_literal "prompts/reviewer.md" 'RVF_CHECK_REVIEW_RESULT'
require_literal "prompts/reviewer.md" 'RVF_REVIEW_RESULT'
require_literal "prompts/reviewer.md" 'kind: no_issues'
require_literal "prompts/reviewer.md" 'clean context'
require_literal "prompts/reviewer.md" 'RVF_SCOPE_CONTRACT'
require_literal "prompts/reviewer.md" 'need-clean-review-context'
require_literal "prompts/validate-fix.md" 'rvf_fix_attempt.py stop'
require_literal "prompts/validate-fix.md" '--status fixed'
require_literal "prompts/validate-fix.md" '--status false_positive'
require_literal "prompts/validate-fix.md" '--status elevated'
require_literal "prompts/validate-fix.md" 'elevation-detail.json'
require_literal "prompts/validate-fix.md" 'scope.contract.json'
require_literal "prompts/validate-fix.md" 'fix_allowlist'
require_literal "prompts/validate-fix.md" '并行 agent'
require_literal "references/review-prompt.md" 'prompts/reviewer.md'
require_literal "references/validate-then-fix-prompt.md" 'prompts/validate-fix.md'
require_literal "references/handoff-template.md" 'handoff.md'
require_literal "references/handoff-template.md" 'RVF_HANDOFF_FILE'
require_literal "references/handoff-template.md" 'Reviewers：'
require_literal "references/handoff-template.md" 'Validate/fixers：'
require_repo_literal "plugins/review-validate-fix/commands/rvf-handoff-commit.md" 'rvf_handoff_intake.py'
require_repo_literal "plugins/review-validate-fix/commands/rvf-handoff-commit.md" '即使最终没有采纳 RVF run 提出的任何 suggestion'
require_repo_literal "plugins/review-validate-fix/commands/rvf-handoff-commit.md" 'rvf_worktree_differs_from_current'
require_repo_literal "plugins/review-validate-fix/commands/rvf-handoff-commit.md" 'intake_hints'
require_literal "SKILL.md" '本 skill 只处理显式 `$review-validate-fix`、`/review-validate-fix` 或 `:review-validate-fix` 调用'
require_literal "SKILL.md" 'policy.allow_implicit_invocation'
require_literal "SKILL.md" 'Scope-of-work 不要只列 created/modified/deleted 文件'
require_literal "SKILL.md" 'scripts/prepare_review_run.py --repo <repo>'
require_literal "SKILL.md" 'prompt artifacts'
require_literal "SKILL.md" 'Stop hook、backend selection、Kanban dispatch、GUI fallback'
require_literal "SKILL.md" 'internals/'
require_literal "SKILL.md" 'debug/'
require_literal "SKILL.md" 'handoff.md'
require_literal "SKILL.md" 'RVF_HANDOFF_FILE'
require_literal "SKILL.md" 'reviewers 和 validate/fixers'
require_literal "references/handoff-template.md" 'Handoff 默认开启'
require_literal "references/handoff-template.md" 'review_status'
require_literal "references/handoff-template.md" 'user-supplied-skip-review'
require_literal "agents/openai.yaml" 'skip review'
require_literal "agents/openai.yaml" 'no handoff'
require_literal "SKILL.md" '必须至少启动一个 `validate_fix` 子代理'
require_literal "prompts/validate-fix.md" '必须启动至少一个 `pass_type: validate_fix` 子代理'
require_literal "prompts/validate-fix.md" '不得因为问题看起来简单'
require_literal "prompts/validate-fix.md" '“为了省时间”或“问题很小”不是例外'
require_literal "references/handoff-template.md" '本地执行：<原因>'
require_literal "SKILL.md" 'scope.contract.json'
require_literal "SKILL.md" 'scripts/check_review_result.py'
require_literal "references/review-merge-policy.md" 'santa-method alternative reviewer'
require_literal "references/review-merge-policy.md" 'scripts/run_alternative_reviewer.py --repo <repo> --review-packet <packet> --session-context <file>'
require_literal "config/alternative-reviewer.json" 'alternative-reviewer:claude-code'
require_literal "config/alternative-reviewer.json" 'ANTHROPIC_API_KEY'
require_literal "config/alternative-reviewer.json" 'Bash,Read'
forbid_literal "config/alternative-reviewer.json" 'dontAsk'
forbid_literal "config/alternative-reviewer.json" '--append-system-prompt'
forbid_literal "config/alternative-reviewer.json" 'alternative code reviewer'
forbid_literal "config/alternative-reviewer.json" 'santa-method double review'
require_literal "config/alternative-reviewer.json" 'allow_repo_cwd'
require_literal "config/alternative-reviewer.json" 'idle_timeout_seconds'
require_literal "config/alternative-reviewer.json" 'activity_check_interval_seconds'
require_literal "config/alternative-reviewer.json" 'activity_probe_command'
require_literal "config/alternative-reviewer.json" 'max_runtime_seconds'
require_literal "config/alternative-reviewer.json" 'pre_run_health'
require_literal "config/alternative-reviewer.json" 'stream-json'
require_literal "config/alternative-reviewer.json" 'claude_stream_json'
require_literal "scripts/run_alternative_reviewer.py" 'env_unset'
require_literal "scripts/run_alternative_reviewer.py" 'normalize_review_output'
require_literal "scripts/run_alternative_reviewer.py" 'review_packet'
require_literal "scripts/run_alternative_reviewer.py" '--preflight'
require_literal "scripts/run_alternative_reviewer.py" 'pre_run_health'
require_literal "scripts/run_alternative_reviewer.py" 'RVF_COMMAND_LOCK'
require_literal "scripts/run_alternative_reviewer.py" 'RVF_SCOPE_CONTRACT'
require_literal "scripts/run_alternative_reviewer.py" 'RVF_REVIEW_RESULT'
require_literal "scripts/run_alternative_reviewer.py" 'RVF_WRITE_REVIEW_RESULT'
require_literal "scripts/run_alternative_reviewer.py" 'check_review_result_artifact'
require_literal "scripts/run_alternative_reviewer.py" 'RVF_EXTERNAL_REVIEWER_TIMEOUT'
require_literal "scripts/run_alternative_reviewer.py" 'max_runtime_seconds'
require_literal "scripts/run_alternative_reviewer.py" 'activity_probe_command'
require_literal "scripts/run_alternative_reviewer.py" 'probe_history'
require_literal "scripts/run_alternative_reviewer.py" 'artifacts_dir / "reviewers"'
require_literal "scripts/run_alternative_reviewer.py" 'reviewer.summary.json'
require_literal "scripts/run_alternative_reviewer.py" 'next_wait_seconds'
require_literal "scripts/run_alternative_reviewer.py" 'unique=True'
require_literal "scripts/run_alternative_reviewer.py" 'extract_claude_stream_result'
require_literal "scripts/run_alternative_reviewer.py" '--rvf-run-id'
require_literal "scripts/run_alternative_reviewer.py" '--rvf-run-dir'
require_repo_literal "tests/test_review_support_scripts.py" 'test_alternative_reviewer_claude_stream_json_extracts_result'
require_repo_literal "tests/test_review_support_scripts.py" 'test_alternative_reviewer_legacy_claude_config_gets_stream_json'
require_repo_literal "tests/test_review_support_scripts.py" 'test_alternative_reviewer_non_claude_stream_json_command_is_not_patched'
require_repo_literal "tests/test_review_support_scripts.py" 'test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout'
require_repo_literal "tests/test_review_support_scripts.py" 'test_alternative_reviewer_repeated_run_keeps_prior_artifacts'
require_repo_literal "tests/test_review_support_scripts.py" 'test_alternative_reviewer_long_command_wait_uses_check_interval'
require_literal "scripts/write_review_result.py" 'no-issues'
require_literal "scripts/write_review_result.py" 'lock-request'
require_literal "scripts/write_review_result.py" 'standard-request'
require_literal "scripts/write_review_result.py" 'measurement-request'
require_literal "scripts/write_review_result.py" 'subtask-request'
require_literal "scripts/write_review_result.py" 'context-request'
require_literal "scripts/check_review_result.py" 'no_issues'
require_literal "scripts/check_review_result.py" 'lock_request'
require_literal "scripts/check_review_result.py" 'excluded_path_prefixes'
require_repo_literal "tests/test_review_support_scripts.py" 'test_review_result_artifact_no_issues_and_issues'
require_repo_literal "tests/test_review_support_scripts.py" 'test_review_result_artifact_rejects_malformed_and_mixed_state'
require_literal "scripts/command_lock.py" 'fcntl.flock'
require_literal "scripts/command_lock.py" 'RVF_LOCK_DIR'
require_literal "scripts/command_lock.py" 'lock_acquired'
require_literal "scripts/command_lock.py" 'lock_timeout'
require_literal "scripts/command_lock.py" 'lock_released'
require_literal "scripts/rvf_logging.py" 'command-lock'
require_literal "scripts/prepare_review_run.py" 'review-packet.metadata.json'
require_literal "scripts/prepare_review_run.py" 'session-manifest.json'
require_literal "scripts/prepare_review_run.py" 'scope.contract.json'
require_literal "scripts/prepare_review_run.py" 'RVF_SCOPE_CONTRACT'
require_literal "scripts/prepare_review_run.py" 'RVF_REVIEW_RESULT'
require_literal "scripts/prepare_review_run.py" 'RVF_WRITE_REVIEW_RESULT'
require_literal "scripts/prepare_review_run.py" 'RVF_CHECK_REVIEW_RESULT'
require_literal "scripts/prepare_review_run.py" 'manual-all-uncommitted'
require_literal "scripts/prepare_review_run.py" 'session-owned'
require_literal "scripts/prepare_review_run.py" 'custom'
require_literal "scripts/prepare_review_run.py" 'allow-missing-session-context'
require_literal "scripts/prepare_review_run.py" '--rvf-run-id'
require_literal "scripts/prepare_review_run.py" '--rvf-run-dir'
require_literal "scripts/prepare_review_run.py" '--rvf-backend'
require_literal "scripts/rvf_logging.py" 'class RunLedger'
require_literal "scripts/rvf_logging.py" 'cline-kanban'
require_literal "scripts/rvf_logging.py" 'RVF_STATE_PHASES'
require_literal "scripts/rvf_logging.py" 'kanban-task'
require_literal "scripts/rvf_logging.py" 'rvf_state_fields'
require_literal "scripts/rvf_logging.py" 'events.jsonl'
require_literal "scripts/rvf_logging.py" 'summary.json'
require_literal "scripts/rvf_logging.py" 'reason_code'
require_literal "scripts/rvf_logging.py" 'CODEX_RVF_RUN_DIR'
require_literal "scripts/rvf_logging.py" 'log_unavailable'
require_literal "scripts/session_manifest.py" 'owned_paths'
require_literal "scripts/session_manifest.py" 'unattributed_dirty_paths'
require_literal "scripts/build_review_packet.py" 'metadata-output'
require_literal "scripts/build_review_packet.py" 'session-manifest'
require_literal "scripts/build_review_packet.py" 'primary-file'
require_literal "scripts/build_review_packet.py" 'max-packet-bytes'
require_literal "scripts/build_review_packet.py" 'session context is required'
require_literal "scripts/build_review_packet.py" 'session_context_provided'
require_literal "scripts/workspace_snapshot.py" 'WORKSPACE_CHANGED'
require_literal "references/review-merge-policy.md" 'codex-reviewer'
require_literal "references/review-merge-policy.md" 'clean context'
require_literal "references/review-merge-policy.md" 'reviewer.summary.json'
require_literal "references/review-merge-policy.md" 'need-clean-review-context'
require_literal "references/review-merge-policy.md" 'alternative-reviewer:<agent-name>'
require_literal "references/review-merge-policy.md" 'codex-mimic-reviewer-a'
require_literal "references/review-merge-policy.md" 'source-agnostic'
require_literal "references/review-merge-policy.md" 'CONTRACT_VIOLATION'
require_literal "references/review-merge-policy.md" 'WORKSPACE_CHANGED_DURING_REVIEW'
require_literal "references/review-merge-policy.md" 'check_review_result.py'
require_literal "references/review-merge-policy.md" 'kind: request'
require_literal "prompts/reviewer.md" 'lock-request'
require_literal "prompts/reviewer.md" 'standard-request'
require_literal "prompts/reviewer.md" 'measurement-request'
require_literal "prompts/reviewer.md" 'subtask-request'
require_literal "prompts/reviewer.md" 'context-request'
require_literal "prompts/reviewer.md" 'command lock'
require_literal "references/review-standards/index.md" 'RVF Review Standards Pack'
require_literal "references/review-standards/main-agent.md" '默认策略：子代理可以请求子任务，但由主会话 spawn'
require_literal "references/review-standards/main-agent.md" '当前可用的最佳模型'
require_literal "references/review-standards/main-agent.md" 'reasoning_effort=high'
require_literal "references/review-standards/reviewer.md" 'standard-request'
require_literal "references/review-standards/validate-fix.md" 'RVF_*_REQUEST'
require_literal "references/review-standards/simplification-subset.md" "Chesterton's Fence"
require_literal "references/review-standards/security-subset.md" 'Three-tier boundary system'
require_literal "references/review-standards/performance-subset.md" 'Measure before optimizing'
require_literal "references/review-standards/protocol-extensions.md" 'subtask-request'
require_literal "prompts/reviewer.md" '不要让 reviewer 只靠 `git diff HEAD` 猜 scope'
require_literal "SKILL.md" '`git diff HEAD` 是证据来源，不是默认 review scope'
require_literal "prompts/validate-fix.md" 'source-agnostic'
require_literal "prompts/validate-fix.md" 'RVF_STANDARD_REQUEST'
require_literal "prompts/validate-fix.md" 'RVF_MEASUREMENT_REQUEST'
require_literal "prompts/validate-fix.md" 'RVF_SUBTASK_REQUEST'
require_literal "prompts/validate-fix.md" 'RVF_CONTEXT_REQUEST'
require_literal "prompts/validate-fix.md" 'references/review-standards/validate-fix.md'
require_literal "prompts/validate-fix.md" '不要生成 handoff'
require_literal "setup/mcp-setup-startup.md" '先问用户'
require_literal "setup/mcp-setup-startup.md" '没有可用 alternative reviewer'
require_literal "references/review-merge-policy.md" '默认使用 Codex-only fallback'
require_literal "setup/mcp-setup-startup.md" 'setup 未完成不能阻塞'
forbid_literal "SKILL.md" '用户明确声明没有任何额外 coding agent'
forbid_literal "references/review-merge-policy.md" '用户明确声明没有其他 coding agent'
forbid_literal "setup/mcp-setup-startup.md" '用户也明确声明没有额外 coding agent'
require_literal "SKILL.md" 'setup/'
require_literal "scripts/read_mcp_setup_once.sh" '已读取过'
require_literal "scripts/discover_santa_alternative_agents.sh" 'candidate_commands'
require_literal "agents/openai.yaml" 'allow_implicit_invocation: false'
require_literal "SKILL.md" '本 skill 只处理显式'
require_literal "SKILL.md" 'manual review scope'
require_literal "agents/openai.yaml" 'explicitly provided manual review scope'
require_literal "internals/stop-hook-workflow.md" 'Stop hook'
require_literal "internals/stop-hook-workflow.md" 'Cline Kanban'
require_literal "internals/stop-hook-workflow.md" 'GUI fork'
require_literal "internals/runtime-contracts.md" 'Backend selection'
require_literal "internals/runtime-contracts.md" 'Agents should use generated context files'
require_literal "debug/troubleshooting.md" 'scripts/diagnose_stop_hook_scope.py --summary <summary.json>'
require_literal "scripts/cline_kanban_client.py" 'DEFAULT_TASK_CMD = "kanban task"'
require_literal "scripts/cline_kanban_client.py" 'RVF does not use npx for its default Kanban path'
require_literal "scripts/cline_kanban_client.py" 'task create'
require_literal "scripts/cline_kanban_client.py" 'task start'
require_literal "scripts/cline_kanban_client.py" 'task trash'
require_literal "scripts/cline_kanban_client.py" 'task message'
require_literal "scripts/cline_kanban_client.py" 'CODEX_RVF_CLINE_KANBAN_TASK_CMD'
require_literal "scripts/cline_kanban_client.py" 'CODEX_RVF_CLINE_KANBAN_START_CMD'
require_literal "scripts/apply_worktree_bootstrap.py" 'git apply'
require_literal "scripts/apply_worktree_bootstrap.py" 'copied_untracked_files'
require_literal "scripts/prepare_review_run.py" 'worktree-bootstrap.patch'
require_literal "scripts/prepare_review_run.py" 'worktree-bootstrap-files'
require_literal "scripts/prepare_review_run.py" 'worktree-bootstrap.json'
require_literal "scripts/cancel_rvf_run.py" 'cline-kanban-rvf-cancelled'
require_literal "scripts/cancel_rvf_run.py" 'trash_task'
require_literal "references/cancel-rvf-run.md" 'cline-kanban-rvf-cancelled'
require_literal "scripts/codex_stop_review_validate_fix.py" 'CODEX_RVF_FORK_MODE=cline-kanban'
require_literal "scripts/codex_stop_review_validate_fix.py" 'kanban-followup'
require_literal "scripts/codex_stop_review_validate_fix.py" 'RVF_KANBAN_FOLLOWUP_TRIGGER'
require_literal "scripts/codex_stop_review_validate_fix.py" 'RVF_DISPATCH=token='
require_literal "scripts/codex_stop_review_validate_fix.py" 'rvf_dispatch_prep_file_path'
require_literal "scripts/codex_stop_review_validate_fix.py" 'rvf_dispatch_target_worktree'
require_literal "scripts/codex_stop_review_validate_fix.py" 'cline-kanban-started'
require_literal "scripts/codex_stop_review_validate_fix.py" 'cline_kanban_task_started'
require_literal "scripts/codex_stop_hook_dispatcher.py" 'DEV_SYNC_INSTALL_SCRIPT = Path("scripts") / "install_to_codex.py"'
require_literal "scripts/codex_stop_hook_dispatcher.py" 'dev_sync_step_specs'
require_repo_literal "README.md" 'Dev-only 标准'
require_repo_literal "README.md" 'RVF_CONTRACT_PARALLEL_TESTS'
require_repo_literal "README.md" 'RVF_CONTRACT_REVIEW_SUPPORT_SHARDS'
require_repo_literal "README.md" 'RVF_CONTRACT_STOP_HOOK_SHARDS'
require_repo_literal "README.md" '--shard-count'
require_repo_literal "README.md" 'Dev-only 标准'
require_repo_literal "scripts/install_to_codex.py" 'DEV_ONLY_NAMES'
require_repo_literal "tests/test_install_to_codex.py" 'test_copy_tree_excludes_dev_only_paths'
require_repo_literal "tests/test_codex_stop_hook_dispatcher.py" 'test_dev_sync_step_specs_resolve_repo_level_dev_scripts'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-start-cmd'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-task-cmd'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-start-timeout'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-tmux-session'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-base-ref'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-auto-review-enabled'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-auto-review-mode'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-start-in-plan-mode'
require_repo_literal "scripts/install_to_codex.py" '--configure-user-prompt-submit-hook'
require_repo_literal "scripts/install_to_codex.py" 'latest-deployment.json'
require_repo_literal "scripts/install_to_codex.py" 'rvf-local-deploy'
require_repo_literal "tests/test_install_to_codex.py" 'test_main_records_deploy_log_with_rvf_context'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'deployments/latest-deployment.json'
require_literal "scripts/codex_stop_hook_dispatcher.py" '--cline-kanban-start-cmd'
require_literal "scripts/codex_stop_hook_dispatcher.py" '--cline-kanban-task-cmd'
require_repo_literal "tests/test_install_to_codex.py" 'test_configure_stop_hook_can_write_cline_kanban_mode'
require_repo_literal "tests/test_install_to_codex.py" 'test_configure_user_prompt_submit_hook_deduplicates_existing_rvf_hooks'
require_repo_literal "tests/test_install_to_codex.py" 'test_main_can_configure_user_prompt_submit_hook'
require_repo_literal "tests/test_codex_stop_hook_dispatcher.py" 'test_dev_sync_preserves_cline_kanban_installer_args'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_cline_kanban_mode_creates_and_starts_task_with_same_run'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_kanban_followup_mode_injects_current_task_message'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'dispatch_prep_payload'
require_repo_literal "tests/test_review_support_scripts.py" 'test_cline_kanban_client_create_and_start_task'
require_repo_literal "tests/test_review_support_scripts.py" 'test_prepare_review_run_writes_worktree_bootstrap'
require_repo_literal "tests/test_review_support_scripts.py" 'test_apply_worktree_bootstrap_replays_tracked_and_untracked'
require_literal "scripts/codex_stop_review_validate_fix.py" 'CODEX_RVF_FORK_REASONING_EFFORT'
require_literal "scripts/codex_stop_review_validate_fix.py" '$review-validate-fix'
require_literal "scripts/codex_stop_review_validate_fix.py" 'stop_hook_active'
require_literal "scripts/codex_stop_review_validate_fix.py" 'systemMessage'
require_literal "scripts/codex_stop_review_validate_fix.py" 'desktop-control-unavailable-report'
require_literal "scripts/codex_stop_review_validate_fix.py" 'desktop-control-unavailable-fail'
require_literal "scripts/codex_stop_review_validate_fix.py" 'RVF_FORK_EXPERIMENT'
require_literal "scripts/codex_stop_review_validate_fix.py" 'RVF_FORKED_REVIEW_VALIDATE_FIX'
require_literal "scripts/codex_stop_review_validate_fix.py" 'DEFAULT_RVF_MODE = "fork"'
require_literal "scripts/codex_stop_review_validate_fix.py" 'DEFAULT_FORK_LAUNCH_MODE = "auto"'
require_literal "scripts/codex_stop_review_validate_fix.py" 'CODEX_RVF_MODE'
require_literal "scripts/codex_stop_review_validate_fix.py" 'StopDecision'
require_literal "scripts/codex_stop_review_validate_fix.py" 'normalize_backend_from_env'
require_literal "scripts/codex_stop_review_validate_fix.py" 'evaluate_stop_event'
require_literal "scripts/codex_stop_review_validate_fix.py" 'launch_backend'
require_literal "scripts/codex_stop_review_validate_fix.py" 'session_hook_gate_disabled'
require_literal "scripts/codex_stop_review_validate_fix.py" 'session_hook_gate_enabled'
require_literal "scripts/codex_stop_review_validate_fix.py" '不是关闭全局 Stop hook'
require_literal "scripts/codex_stop_review_validate_fix.py" 'dispatcher 仍会运行'
require_literal "scripts/codex_stop_review_validate_fix.py" 'manual-prepared'
require_literal "scripts/codex_stop_review_validate_fix.py" 'no Terminal was launched'
require_literal "scripts/codex_stop_review_validate_fix.py" 'thread/fork'
require_literal "scripts/codex_stop_review_validate_fix.py" 'turn/start'
require_literal "scripts/codex_stop_review_validate_fix.py" 'Terminal/CLI fork launch is intentionally disabled'
require_literal "scripts/codex_stop_review_validate_fix.py" 'CODEX_RVF_FORK_REASONING_EFFORT'
require_literal "scripts/codex_stop_review_validate_fix.py" 'review-validate-fix-fork'
require_literal "scripts/codex_stop_review_validate_fix.py" 'RVF_HANDOFF_FILE'
require_literal "scripts/diagnose_codex_fork.py" 'run_fork_experiment'
require_literal "scripts/diagnose_codex_fork.py" 'CODEX_RVF_FORK_EXPERIMENT_MODE'
require_literal "scripts/codex_stop_hook_dispatcher.py" '--no-open-handoff'
require_literal "scripts/codex_stop_hook_dispatcher.py" '--ide-open-cmd'
require_repo_literal "scripts/install_to_codex.py" '--no-open-handoff'
require_repo_literal "scripts/install_to_codex.py" '--ide-open-cmd'
require_literal "scripts/codex_stop_review_validate_fix.py" 'fork'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_fork_experiment_marker_no_longer_triggers_stop_hook_fork'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_diagnose_codex_fork_dry_run_writes_requests'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_dry_run_prepares_legacy_gui_requests'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_auto_mode_creates_cline_kanban_task_by_default'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_auto_mode_reports_kanban_unavailable_without_default_gui_fallback'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_auto_mode_can_opt_into_legacy_gui_as_backup_of_backup'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_auto_mode_reports_stale_kanban_listener_without_gui_fallback'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_manual_mode_only_prepares_prompt'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_fork_dry_run'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_cline_kanban_mode_creates_and_starts_task_with_same_run'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_cline_kanban_mode_marks_unavailable_when_task_start_fails'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'reason=suppressed'
require_repo_literal "tests/test_review_support_scripts.py" 'test_cline_kanban_client_create_and_start_task'
require_repo_literal "tests/test_review_support_scripts.py" 'test_prepare_review_run_writes_worktree_bootstrap'
require_repo_literal "tests/test_review_support_scripts.py" 'test_apply_worktree_bootstrap_replays_tracked_and_untracked'
require_repo_literal "tests/test_review_support_scripts.py" 'test_run_ledger_summary_preserves_cline_kanban_fields'
require_repo_literal "tests/test_review_support_scripts.py" 'test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_stop_event_transcript_path_overrides_bad_env_thread_id'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_stop_event_log_path_is_not_used_as_fork_rollout_path'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_dirty_repo_continuation_mode_reports_removed_fallback'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_no_git_cwd_skips_even_with_dirty_trusted_repo'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_forked_rvf_session_gets_programmatic_handoff_advisory'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_handoff_marker_in_dirty_repo_does_not_create_new_fork'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_forked_rvf_session_waits_for_handoff_before_advisory'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_missing_cwd_skips_and_requests_target_repo'
require_repo_literal "tests/test_codex_stop_review_validate_fix.py" 'test_log_unavailable_does_not_break_hook_payload'
require_repo_literal "scripts/install_to_codex.py" 'sync_codex_plugin_cache'
require_repo_literal "scripts/install_to_codex.py" 'sync_claude_plugin'
require_repo_literal "scripts/install_to_codex.py" 'sync_claude_marketplace_metadata'
require_repo_literal "scripts/install_to_codex.py" 'ensure_codex_plugin_enabled'
require_repo_literal "scripts/install_to_codex.py" '[plugins."'
require_repo_literal "scripts/install_to_codex.py" '".codex"'
require_repo_literal "scripts/install_to_codex.py" '"cache"'
require_repo_literal "scripts/install_to_codex.py" 'PLUGIN_NAME = "review-validate-fix"'
require_repo_literal "scripts/install_to_codex.py" 'CLAUDE_MARKETPLACE_SRC'
require_repo_literal ".claude-plugin/marketplace.json" 'review-validate-fix-local'
require_repo_literal ".claude-plugin/marketplace.json" './plugins/review-validate-fix'
require_repo_literal "tests/test_install_to_codex.py" 'test_ensure_codex_plugin_enabled_updates_user_config'
require_repo_literal "tests/test_install_to_codex.py" 'test_ensure_codex_plugin_enabled_writes_under_custom_marketplace'
require_repo_literal "tests/test_install_to_codex.py" 'test_main_writes_claude_marketplace_json'
require_repo_literal "tests/test_install_to_codex.py" 'stale cached skill'
forbid_repo_literal "scripts/install_to_codex.py" 'remove_legacy_codex_skill_dir'
forbid_repo_literal "scripts/install_to_codex.py" 'remove_legacy_plugin_cache'
forbid_repo_literal "scripts/install_to_codex.py" 'legacy_plugin_config_ids'
forbid_repo_literal "scripts/install_to_codex.py" 'remove_plugin_sections'
forbid_repo_literal "scripts/install_to_codex.py" 'PLUGIN_NAME = "rvf"'
forbid_repo_literal "plugins/review-validate-fix/.codex-plugin/plugin.json" '"name": "rvf"'
forbid_repo_literal "scripts/install_to_codex.py" 'migrated legacy stand''alone setup'
forbid_repo_literal "scripts/install_to_codex.py" 'Review Validate Fix CLI Launch''er'
forbid_repo_literal "scripts/install_to_codex.py" 'stand''alone'

if [ "$verbose" -eq 1 ]; then
  printf 'contract check OK\n'
  printf 'hashes:\n'
  for file in "${required_files[@]}"; do
    printf '%s  %s\n' "$(hash_file "$skill_dir/$file")" "$file"
  done
else
  printf '契约检查通过\n'
fi
