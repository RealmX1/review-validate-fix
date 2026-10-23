#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
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

# 先由轻量选择器自举，再进入固定解释器的工具子进程环境。
if [ "${RVF_TOOL_RUNTIME_CONTRACT_SHELL_REENTRY:-}" != "$repo_root/scripts/check_skill_contracts.sh" ]; then
  bootstrap_python="${RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE:-}"
  if [ -z "$bootstrap_python" ]; then
    for candidate in "$repo_root/.venv/bin/python" "${HOME}/.local/bin/python3.12" python3.12 python3; do
      if command -v "$candidate" >/dev/null 2>&1; then
        bootstrap_python="$(command -v "$candidate")"
        break
      fi
    done
  fi
  if [ -z "$bootstrap_python" ] || [ ! -x "$bootstrap_python" ]; then
    printf 'python_executable_unusable: 无法启动 RVF Python 环境预检：%s\n' "$bootstrap_python" >&2
    exit 2
  fi
  if [ "$verbose" -eq 1 ]; then
    set -- --verbose
  fi
  exec "$bootstrap_python" -B "$repo_root/scripts/rvf_tool_runtime_python_environment.py" \
    --run-contract-shell "$repo_root/scripts/check_skill_contracts.sh" "$@"
fi
"$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -B "$repo_root/scripts/rvf_tool_runtime_python_environment.py" >/dev/null

# 测试卫生守卫（F5·H5b）：本次套件独占一个唯一名的 TMPDIR，于是每个测试的临时仓库 / run / HOME
# 路径都带着这个名字；脚本末尾扫真实 RVF 状态根，出现引用它的新文件即判测试泄漏。判据与原理见
# scripts/check_contract_suite_left_no_test_artifacts_in_real_rvf_state_roots.py 的模块 docstring。
contract_suite_started_at_epoch_seconds="$("$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -c 'import time; print(time.time())')"
contract_suite_private_temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/rvf-contract-suite-private-tmp.XXXXXX")"
trap 'rm -rf "$contract_suite_private_temporary_directory"' EXIT
export TMPDIR="$contract_suite_private_temporary_directory"

timestamp_ms() {
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -c 'import time; print(time.time_ns() // 1000000)'
}

record_step_timing() {
  local label="$1"
  local command_status="$2"
  local duration_ms="$3"
  local execution_mode="${4:-serial}"
  if [ -n "${RVF_CONTRACT_TIMING_JSONL:-}" ] && [ "${RVF_CONTRACT_TIMING_SCRIPT:-}" = "$0" ]; then
    "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" - "$RVF_CONTRACT_TIMING_JSONL" "$label" "$command_status" "$duration_ms" "$execution_mode" <<'PY' || true
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

contract_host_cpu_count() {
  local detected=""
  if command -v sysctl >/dev/null 2>&1; then
    detected="$(sysctl -n hw.ncpu 2>/dev/null || true)"
  fi
  if [ -z "$detected" ] && command -v nproc >/dev/null 2>&1; then
    detected="$(nproc 2>/dev/null || true)"
  fi
  case "$detected" in
    ''|*[!0-9]*|0)
      # 探测不到核数时按保守的 4 核对待，宁可少并发也不要在小机器上超额订阅。
      printf '4'
      ;;
    *)
      printf '%s' "$detected"
      ;;
  esac
}

contract_host_one_minute_load_average_integer() {
  # 输出 1 分钟 load average 的整数部分；探测不到则输出空串（调用方据此跳过负载退让）。
  local raw=""
  if [ -r /proc/loadavg ]; then
    raw="$(cat /proc/loadavg 2>/dev/null || true)"
  elif command -v sysctl >/dev/null 2>&1; then
    # darwin: `{ 17.54 15.65 13.99 }`
    raw="$(sysctl -n vm.loadavg 2>/dev/null || true)"
  fi
  raw="${raw#\{}"
  raw="${raw# }"
  local first_field="${raw%% *}"
  local integer_part="${first_field%%.*}"
  case "$integer_part" in
    ''|*[!0-9]*)
      printf ''
      ;;
    *)
      printf '%s' "$integer_part"
      ;;
  esac
}

# 并发度按本机核数与当前负载推导，而不是写死。写死的 8 在 10 核开发机上恰好合适，但契约检查
# 常常跟别的 agent / 构建同时跑：2026-07-25 的 `/rvf-local-deploy` 就是在 load 50+ 时把测试
# 里的 idle-timeout 保险丝挤爆，报成 `plugin_contract_check` 失败。并发退让只是缓解——真正
# 让保险丝不再敏感的是测试侧的 HANG_PREVENTION_IDLE_TIMEOUT_SECONDS 分类。
#
# 系数从 `cpu-2`（10 核 → 8）降到 `cpu/3`（10 核 → 3）。依据是 2026-07-28 在隔离 worktree +
# 隔离 HOME 里做的资源画像：本函数的消费者只有 run_parallel_test_steps，而那里是滑动 FIFO
# 窗口——窗口满时 `wait` 的是**队首**而不是任意先完成者（macOS 自带 bash 3.2 没有 `wait -n`），
# 队首一慢窗口就塌陷。实测名义 jobs=8 的有效并发只有 3.82，于是 jobs=5/6/7/8 四档墙钟**完全
# 相同**（82.9s），但峰值 CPU 从 3.39 核一路涨到 5.85 核、峰值进程 32→46。高出来的那截纯属
# 白烧，买不到任何墙钟。
#
#   jobs=3   墙钟 126.9s   峰值 2.48 核(25%)   24 进程
#   jobs=5   墙钟  82.9s   峰值 3.39 核(34%)   32 进程
#   jobs=8   墙钟  82.9s   峰值 5.85 核(59%)   46 进程   ← 旧默认
#
# 取 3 而非 5，是为了把机器留给开发者的其他工作：多等 44 秒换峰值占用减半。想要回原来的速度
# 直接 `RVF_CONTRACT_PARALLEL_JOBS=5`，显式值优先级最高。
#
# 若哪天把队首塌陷修掉（换 Python 编排或要求 bash 4+ 的 `wait -n`），有效并发才会真正逼近
# jobs，那时这个系数值得重新标定——在那之前，调高它只会多占资源。
contract_parallel_jobs() {
  local explicit_value="${RVF_CONTRACT_PARALLEL_JOBS:-}"
  case "$explicit_value" in
    '')
      ;;
    *[!0-9]*)
      # 非数字视为未设置，落回推导。
      ;;
    0)
      printf '1'
      return
      ;;
    *)
      printf '%s' "$explicit_value"
      return
      ;;
  esac

  local cpu_count
  cpu_count="$(contract_host_cpu_count)"
  local derived_jobs=$((cpu_count / 3))
  if [ "$derived_jobs" -lt 2 ]; then
    derived_jobs=2
  fi

  local one_minute_load
  one_minute_load="$(contract_host_one_minute_load_average_integer)"
  if [ -n "$one_minute_load" ] && [ "$one_minute_load" -gt "$cpu_count" ]; then
    derived_jobs=$((derived_jobs / 2))
    if [ "$derived_jobs" -lt 2 ]; then
      derived_jobs=2
    fi
  fi

  printf '%s' "$derived_jobs"
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

  # 一行可解析的并发画像：契约失败时 `rvf_local_deploy.py` 会把它记进 deploy receipt，
  # 省掉「事后跑 uptime 猜当时机器有多忙」的考古。
  printf 'RVF_CONTRACT_PARALLEL_EXECUTION jobs=%s cpu=%s load1m=%s\n' \
    "$jobs" \
    "$(contract_host_cpu_count)" \
    "$(contract_host_one_minute_load_average_integer)"

  local labels=()
  local scripts=()
  local args=()
  labels+=("tests: install_to_codex")
  scripts+=("$tests_dir/test_install_to_codex.py")
  args+=("")
  labels+=("tests: rvf_handoff_intake")
  scripts+=("$tests_dir/test_rvf_handoff_intake.py")
  args+=("")
  labels+=("tests: review_reopen_marker")
  scripts+=("$tests_dir/test_review_reopen_marker.py")
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
      labels+=("tests: stop_hook_dispatcher")
      args+=("")
    else
      labels+=("tests: stop_hook_dispatcher shard $((shard_index + 1))/$dispatcher_shards")
      args+=("--shard-count $dispatcher_shards --shard-index $shard_index")
    fi
    scripts+=("$tests_dir/test_stop_hook_dispatcher.py")
  done
  for ((shard_index = 0; shard_index < stop_hook_shards; shard_index++)); do
    if [ "$stop_hook_shards" -eq 1 ]; then
      labels+=("tests: stop_review_validate_fix")
      args+=("")
    else
      labels+=("tests: stop_review_validate_fix shard $((shard_index + 1))/$stop_hook_shards")
      args+=("--shard-count $stop_hook_shards --shard-index $shard_index")
    fi
    scripts+=("$tests_dir/test_stop_review_validate_fix.py")
  done

  if ! contract_parallel_tests_enabled || [ "$jobs" -le 1 ]; then
    local serial_index
    for serial_index in "${!labels[@]}"; do
      if [ -n "${args[$serial_index]}" ]; then
        run_step "${labels[$serial_index]}" "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "${scripts[$serial_index]}" ${args[$serial_index]}
      else
        run_step "${labels[$serial_index]}" "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "${scripts[$serial_index]}"
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
        "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "${scripts[$index]}" ${args[$index]} >"${output_files[$index]}" 2>&1 || command_status=$?
      else
        "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "${scripts[$index]}" >"${output_files[$index]}" 2>&1 || command_status=$?
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
  "config/alternative-reviewer.cursor.json"
  "config/alternative-reviewer.claude.json"
  "config/alternative-reviewer.codex.json"
  "config/reviewer-registry.json"
  "references/zero-external-reviewer-last-resort-in-harness-fallback.md"
  "scripts/dispatch_reviewers.py"
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
  "scripts/rvf_prep_file.py"
  "scripts/rvf_user_prompt_submit.py"
  "scripts/rvf_handoff_intake.py"
  "scripts/rvf_land.py"
  "scripts/validation_result_cache.py"
  "scripts/write_reviewed_task_candidate_evidence.py"
  "scripts/rvf_merge_dispositions.py"
  "scripts/rvf_out_of_scope_findings_kanban_backlog_submission.py"
  "scripts/workspace_snapshot.py"
  "scripts/stop_hook_dispatcher.py"
  "scripts/codex_gui_without_kanban_task_stop_hook_policy.py"
  "scripts/stop_hook_router.py"
  "scripts/stop_review_validate_fix.py"
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
  "plugins/review-validate-fix/skills/rvf-land/SKILL.md"
  "plugins/review-validate-fix/skills/rvf-reopen/SKILL.md"
  "core/session_scope_allocation/reviewable_unit_diff_tracker.py"
  "core/session_scope_allocation/session_change_manifest.py"
  "core/session_scope_allocation/round_baseline_marker.py"
  "core/session_scope_allocation/review_highwater_marker.py"
  "core/session_scope_allocation/committed_round_detection.py"
  "core/session_scope_allocation/session_scope_gate_pure_helpers.py"
  "core/session_scope_allocation/shell_command_named_write_target_paths_extraction.py"
  "core/session_scope_allocation/agent_transcript_tool_call_time_windows_extraction.py"
  "core/session_scope_allocation/agent_sessions_that_worked_in_same_worktree_discovery.py"
  "core/session_scope_allocation/layered_session_write_attribution_for_unattributed_dirty_paths.py"
  "tests/test_layered_session_write_attribution.py"
  "plugins/review-validate-fix/skills/review-validate-fix/scripts/review_reopen_marker.py"
  "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_rescope.py"
  "core/run_ledger/run_ledger.py"
  "core/rvf_user_local_data_paths.py"
  "core/review_start_boundary/review_candidate_snapshot.py"
  "tests/test_review_candidate_snapshot_without_native_rewind.py"
  "core/rvf_shared_runtime_constants/rvf_shared_runtime_constants.py"
  "core/provider_health/provider_health_guard.py"
  "adapters/codex/codex_provider_login.py"
  "adapters/codex/codex_host_adapter.py"
  "adapters/claude_code/claude_code_host_adapter.py"
  "core/parent_conversation_context/parent_conversation_origin_freeze.py"
  "core/review_prompt_assembly/review_validate_fix_fork_prompt.py"
  "core/review_dispatch/review_validate_fix_dispatch_flow.py"
  "core/cline_kanban_terminal_delivery/cline_kanban_terminal_delivery_receipt.py"
  "core/stop_hook_output_audience/stop_hook_output_audience_contract.py"
  "adapters/kimi/kimi_code_cli_host_adapter.py"
  "adapters/kimi/session_layout.py"
  "adapters/kimi/subagent.py"
  "plugins/review-validate-fix/hooks/_kimi_hook_entry.py"
  "plugins/review-validate-fix/hooks/kimi_stop.py"
  "plugins/review-validate-fix/hooks/kimi_user_prompt_submit.py"
  "plugins/review-validate-fix/.kimi-plugin/plugin.json"
  "tests/test_kimi_hook_entry_normalization.py"
  "adapters/kimi/transcript.py"
  "core/stop_hook_reentry_budget/stop_hook_reentry_budget.py"
  "core/stop_hook_reentry_budget/__init__.py"
  "scripts/kimi_plugin_manifest_activation_state_check.py"
  "tests/test_stop_hook_reentry_budget.py"
  "tests/test_kimi_plugin_activation_state_check.py"
  "tests/test_kimi_plugin_registration_installer.py"
  "scripts/check_host_identity_and_reviewer_registry_harness_id_vocabulary_consistency.py"
  "tests/test_host_identity_reviewer_registry_vocabulary_gate.py"
  "scripts/check_plugin_contracts.py"
  "scripts/check_skill_contracts.sh"
  "scripts/install_to_codex.py"
  "scripts/rvf_local_deploy.py"
  "scripts/rvf_tool_runtime_python_environment.py"
  "tests/test_rvf_tool_runtime_python_environment.py"
  "scripts/merge_rvf_state_roots.py"
  "tests/test_stop_hook_dispatcher.py"
  "tests/test_codex_gui_without_kanban_task_stop_hook_policy.py"
  "tests/test_finalized_run_in_progress_marker_reconciliation.py"
  "tests/test_stop_review_validate_fix.py"
  "tests/test_install_to_codex.py"
  "tests/test_rvf_local_deploy_trampoline.py"
  "tests/test_verify_local_deploy.py"
  "tests/test_developer_local_main_installation.py"
  "tests/test_official_cli_developer_plugin_installation.py"
  "tests/test_official_cli_installation_hooks.py"
  "tests/test_official_cli_local_deploy_verification.py"
  "tests/test_rvf_user_local_data_paths.py"
  "tests/test_merge_rvf_state_roots.py"
  "tests/test_cline_kanban_terminal_delivery_receipt.py"
  "tests/test_rvf_withdrawn_dispatch_tombstone.py"
  "tests/test_review_support_scripts.py"
  "tests/test_review_reopen_marker.py"
  "tests/test_rvf_handoff_intake.py"
  "tests/test_rvf_land_single_step_prepare_and_commit.py"
  "tests/test_validation_result_cache_v2.py"
  "tests/test_reviewed_task_candidate_evidence.py"
  "tests/test_rvf_merge_dispositions_structured_merge_phase.py"
  "tests/test_review_initiation_decision_point_is_sole_self_injection.py"
  "tests/test_stop_hook_output_audience_contract.py"
  "tests/test_session_scoped_declined_candidate_fingerprint_cooldown.py"
  "tests/test_kimi_host_adapter_and_transcript.py"
  "tests/test_agentic_initiation_scope_of_work_contract.py"
  "tests/test_reviewer_result_artifact_lifecycle.py"
  "tests/test_rvf_fix_causality.py"
  "tests/test_out_of_scope_findings_kanban_backlog_submission.py"
  "tests/test_codex_hook_trust_fingerprint.py"
  "scripts/codex_hook_trust_fingerprint.py"
  "tests/test_current_session_host_adapter_resolution.py"
  "tests/test_reviewer_prompt_delivery_contract.py"
  "tests/test_reviewer_structured_output_extraction.py"
  "tests/test_reviewer_health_probe_parity.py"
  "tests/test_reviewer_registry_additive_merge.py"
  "tests/test_reviewer_quota_pool_exclusive_selection.py"
  "tests/test_reviewer_harness_probe_result_cache.py"
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
run_step "shell syntax: scripts/sync-manifest.sh" bash -n "$repo_root/scripts/sync-manifest.sh"
run_step "manifest sync: scripts/sync-manifest.sh" bash "$repo_root/scripts/sync-manifest.sh"

run_step "python compile" "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m py_compile \
  "$repo_root/scripts/check_plugin_contracts.py" \
  "$repo_root/scripts/install_to_codex.py" \
  "$repo_root/scripts/rvf_local_deploy.py" \
  "$repo_root/scripts/rvf_tool_runtime_python_environment.py" \
  "$repo_root/scripts/merge_rvf_state_roots.py" \
  "$repo_root/scripts/check_contract_suite_left_no_test_artifacts_in_real_rvf_state_roots.py" \
  "$skill_dir/scripts/run_alternative_reviewer.py" \
  "$skill_dir/scripts/build_review_packet.py" \
  "$skill_dir/scripts/review_scope_excluded_path_prefixes.py" \
  "$skill_dir/scripts/write_review_result.py" \
  "$skill_dir/scripts/check_review_result.py" \
  "$skill_dir/scripts/command_lock.py" \
  "$skill_dir/scripts/prepare_review_run.py" \
  "$skill_dir/scripts/apply_worktree_bootstrap.py" \
  "$skill_dir/scripts/cline_kanban_client.py" \
  "$skill_dir/scripts/rvf_out_of_scope_findings_kanban_backlog_submission.py" \
  "$skill_dir/scripts/cancel_rvf_run.py" \
  "$skill_dir/scripts/rvf_command_line_shortcut_entrypoint.py" \
  "$repo_root/core/rvf_command_line_shortcut_shim_location.py" \
  "$repo_root/core/host_adapter/current_agent_session_identity_from_environment.py" \
  "$repo_root/core/run_ledger/run_ledger.py" \
  "$repo_root/core/rvf_user_local_data_paths.py" \
  "$repo_root/core/rvf_shared_runtime_constants/rvf_shared_runtime_constants.py" \
  "$repo_root/core/stop_hook_output_audience/stop_hook_output_audience_contract.py" \
  "$repo_root/adapters/kimi/kimi_code_cli_host_adapter.py" \
  "$repo_root/adapters/kimi/session_layout.py" \
  "$repo_root/adapters/kimi/subagent.py" \
  "$repo_root/plugins/review-validate-fix/hooks/_kimi_hook_entry.py" \
  "$repo_root/plugins/review-validate-fix/hooks/kimi_stop.py" \
  "$repo_root/plugins/review-validate-fix/hooks/kimi_user_prompt_submit.py" \
  "$repo_root/adapters/kimi/transcript.py" \
  "$repo_root/scripts/check_host_identity_and_reviewer_registry_harness_id_vocabulary_consistency.py" \
  "$repo_root/core/stop_hook_reentry_budget/stop_hook_reentry_budget.py" \
  "$repo_root/scripts/kimi_plugin_manifest_activation_state_check.py" \
  "$skill_dir/scripts/rvf_prep_file.py" \
  "$skill_dir/scripts/rvf_user_prompt_submit.py" \
  "$skill_dir/scripts/rvf_withdrawn_dispatch_tombstone.py" \
  "$skill_dir/scripts/rvf_handoff_intake.py" \
  "$skill_dir/scripts/rvf_land.py" \
  "$skill_dir/scripts/validation_result_cache.py" \
  "$skill_dir/scripts/write_reviewed_task_candidate_evidence.py" \
  "$skill_dir/scripts/rvf_merge_dispositions.py" \
  "$repo_root/core/session_scope_allocation/session_change_manifest.py" \
  "$skill_dir/scripts/workspace_snapshot.py" \
  "$skill_dir/scripts/stop_hook_dispatcher.py" \
  "$skill_dir/scripts/codex_gui_without_kanban_task_stop_hook_policy.py" \
  "$skill_dir/scripts/stop_review_validate_fix.py" \
  "$repo_root/core/session_scope_allocation/reviewable_unit_diff_tracker.py" \
  "$repo_root/core/session_scope_allocation/round_baseline_marker.py" \
  "$repo_root/core/session_scope_allocation/review_highwater_marker.py" \
  "$repo_root/core/session_scope_allocation/committed_round_detection.py" \
  "$repo_root/core/session_scope_allocation/session_scope_gate_pure_helpers.py" \
  "$repo_root/core/session_scope_allocation/shell_command_named_write_target_paths_extraction.py" \
  "$repo_root/core/session_scope_allocation/agent_transcript_tool_call_time_windows_extraction.py" \
  "$repo_root/core/session_scope_allocation/agent_sessions_that_worked_in_same_worktree_discovery.py" \
  "$repo_root/core/session_scope_allocation/layered_session_write_attribution_for_unattributed_dirty_paths.py" \
  "$skill_dir/scripts/agentic_initiation_review_scope_coverage_and_agent_explicit_path_claim.py" \
  "$repo_root/core/provider_health/provider_health_guard.py" \
  "$repo_root/adapters/codex/codex_provider_login.py" \
  "$repo_root/adapters/codex/codex_host_adapter.py" \
  "$repo_root/adapters/claude_code/claude_code_host_adapter.py" \
  "$repo_root/core/parent_conversation_context/parent_conversation_origin_freeze.py" \
  "$repo_root/core/review_prompt_assembly/review_validate_fix_fork_prompt.py" \
  "$repo_root/core/review_dispatch/review_validate_fix_dispatch_flow.py" \
  "$repo_root/core/cline_kanban_terminal_delivery/cline_kanban_terminal_delivery_receipt.py" \
  "$skill_dir/scripts/host_adapter_resolver.py" \
  "$skill_dir/scripts/review_reopen_marker.py" \
  "$skill_dir/scripts/rvf_rescope.py" \
  "$tests_dir/test_stop_hook_dispatcher.py" \
  "$tests_dir/test_stop_review_validate_fix.py" \
  "$tests_dir/test_install_to_codex.py" \
  "$tests_dir/test_rvf_local_deploy_trampoline.py" \
  "$tests_dir/test_verify_local_deploy.py" \
  "$tests_dir/test_developer_local_main_installation.py" \
  "$tests_dir/test_official_cli_developer_plugin_installation.py" \
  "$tests_dir/test_official_cli_installation_hooks.py" \
  "$tests_dir/test_official_cli_local_deploy_verification.py" \
  "$tests_dir/test_rvf_user_local_data_paths.py" \
  "$tests_dir/test_merge_rvf_state_roots.py" \
  "$tests_dir/test_cline_kanban_terminal_delivery_receipt.py" \
  "$tests_dir/test_rvf_withdrawn_dispatch_tombstone.py" \
  "$tests_dir/test_review_support_scripts.py" \
  "$tests_dir/test_review_reopen_marker.py" \
  "$tests_dir/test_rvf_handoff_intake.py" \
  "$tests_dir/test_rvf_land_single_step_prepare_and_commit.py" \
  "$tests_dir/test_review_initiation_decision_point_is_sole_self_injection.py" \
  "$tests_dir/test_reviewer_prompt_delivery_contract.py" \
  "$tests_dir/test_reviewer_structured_output_extraction.py" \
  "$tests_dir/test_reviewer_health_probe_parity.py" \
  "$tests_dir/test_reviewer_registry_additive_merge.py" \
  "$tests_dir/test_reviewer_quota_pool_exclusive_selection.py" \
  "$tests_dir/test_reviewer_harness_probe_result_cache.py"

run_parallel_test_steps
run_step "tests: rvf_analyze_formal_archive_completion" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_rvf_analyze_formal_archive_completion.py" \
    "$tests_dir/test_invoke_subagent.py" \
    "$tests_dir/test_rvf_analyze.py" \
    "$tests_dir/test_analysis_artifacts.py"
run_step "tests: codex_gui_without_kanban_task_stop_hook_policy" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_codex_gui_without_kanban_task_stop_hook_policy.py" \
    "$tests_dir/test_finalized_run_in_progress_marker_reconciliation.py"
run_step "tests: rvf_local_deploy" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_developer_local_main_installation.py" \
    "$tests_dir/test_official_cli_local_deploy_verification.py"
run_step "tests: rvf_tool_runtime_python_environment" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_rvf_tool_runtime_python_environment.py"
run_step "tests: merge_rvf_state_roots" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_merge_rvf_state_roots.py"
run_step "tests: cline_kanban_terminal_delivery_receipt" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_cline_kanban_terminal_delivery_receipt.py"
run_step "tests: rvf_withdrawn_dispatch_tombstone" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_rvf_withdrawn_dispatch_tombstone.py"
# 结构性 gate：投给当前活会话的自注入有且只有发起决策点横幅（INV-1 / INV-2）。行为断言，不是
# grep —— `$review-validate-fix` 这个字面量在仓里到处都是，要钉的是默认发起路径**吐出**的正文。
run_step "tests: review_initiation_decision_point_is_sole_self_injection" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_review_initiation_decision_point_is_sole_self_injection.py"
# 结构性 + 行为 gate：Stop 出口的受众分级。驱动 agent 的线上键（decision / hookSpecificOutput）
# 只能在 core/stop_hook_output_audience 的翻译层里构造（AST 判定，不是字面量扫描）；且同一条
# 决策在 Claude 下必须落非错误的 additionalContext、在 Codex 下必须落 decision:block。
run_step "tests: stop_hook_output_audience_contract" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_stop_hook_output_audience_contract.py"
run_step "tests: session_scoped_declined_candidate_fingerprint_cooldown" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_session_scoped_declined_candidate_fingerprint_cooldown.py"
# agentic 发起路径的 scope-of-work 契约：stub 不得自称手动入口；stub 标记机器可判（dispatch
# contract gate 据它拒派）；改好的 scope-of-work 有受支持的重渲入口把它渲进已冻结的 packet。
run_step "tests: agentic_initiation_scope_of_work_contract" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_agentic_initiation_scope_of_work_contract.py"
# session 归属分层补判（08ac4）：Bash / 子代理 / mv·cp 写的文件按「子代理写工具 → 命令文本 → agent 认领
# → 时间窗」补判归属；fast-forward 到 base 后不再截掉更早的未提交写入。漏审曾占本会话改动的 68%。
run_step "tests: layered_session_write_attribution" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_layered_session_write_attribution.py"
run_step "tests: compact_path_tree_rendering" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_compact_path_tree_rendering.py"
# land 单步化链路：验证缓存（含结构化 argv 工件 / 失败尾部）→ candidate evidence → rvf_land
# prepare/commit 端到端。前两份曾经不在任何 gate 里（改了缓存或 evidence 门槛也不会有测试变红），
# 而 rvf_land 的零执行复用恰恰建立在二者口径一致之上，所以三份一起跑。合并阶段结构化处置（T4）是
# evidence 的 canonical 计数事实源（全部 dropped 时 N=0 才能 land），同一步一起跑。
run_step "tests: rvf_land_validation_cache_and_candidate_evidence" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_validation_result_cache_v2.py" \
    "$tests_dir/test_reviewed_task_candidate_evidence.py" \
    "$tests_dir/test_rvf_land_single_step_prepare_and_commit.py" \
    "$tests_dir/test_rvf_merge_dispositions_structured_merge_phase.py"
# reviewer 结果工件生命周期：三态互转对称且换 kind 必归档（旧规则 request→issues 是死结、
# request→no_issues 却静默覆写）；重派只重跑 kind: request 的腿，健康腿结果不被覆写。
run_step "tests: reviewer_result_artifact_lifecycle" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_reviewer_result_artifact_lifecycle.py"
# fix attempt 因果链：全量物化、越界修改的告警-确认放行、validate-only 只验证并提交 Kanban backlog、
# apply 状态门与内容漂移冲突。此前这个文件不在任何 gate 里，测试会静默不跑。
# 迁移用例在干净 HEAD 上就红（极简 v3 库缺 leases 表，backlog 任务 64943 跟踪），修好后删掉这条 -k 排除。
run_step "tests: rvf_fix_causality" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_rvf_fix_causality.py" \
    -k "not test_diff_tracker_creates_and_migrates_rvf_causality_tables"
# 范围外问题提交 Kanban backlog：卡片标记、自包含（不 fork 父会话）、主仓 project path、跨 worktree
# 指纹去重、Kanban 不可用时持久 pending 与重试、不替其他会话的未提交 WIP 提卡。
run_step "tests: out_of_scope_findings_kanban_backlog_submission" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_out_of_scope_findings_kanban_backlog_submission.py"
# Codex hook 信任指纹：改了 hook 命令不重新信任 ⇒ Codex 静默跳过该 hook（exec 下零警告），
# deploy 却报告成功。核对只观测、不改 live config。
run_step "tests: codex_hook_trust_fingerprint" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_codex_hook_trust_fingerprint.py"
run_step "codex hook trust fingerprint self-check" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/scripts/codex_hook_trust_fingerprint.py"
# 当前会话 host 解析：判据顺序必须「先否定 Claude、后采信 claude-code stamp」。把 Codex 误判成
# Claude 会让它收到 hookSpecificOutput（stop.command.output 是 additionalProperties:false）⇒
# 整条 hook 输出 Failed = RVF 在 Codex 上静默停摆；反向误判只是降级，代价不对称。
run_step "tests: current_session_host_adapter_resolution" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_current_session_host_adapter_resolution.py"
run_step "stop hook output audience contract self-check" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/core/stop_hook_output_audience/stop_hook_output_audience_contract.py"
# Kimi 的第三条线材：它的 block 判据是 JS 全等 `permissionDecision !== "deny"`，而 hook 输出
# schema 是 looseObject ⇒ 拼错字面量、或整条通道被误删，都会**静默降级成 allow 且零报错**
# （与 Codex「整条 Failed」相反，这里没有任何可观测的失败信号）。所以三处各钉一根钉：
require_repo_literal "core/stop_hook_output_audience/stop_hook_output_audience_contract.py" \
  'STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY'
require_repo_literal "core/stop_hook_output_audience/stop_hook_output_audience_contract.py" \
  'STOP_HOOK_PERMISSION_DECISION_DENY = "deny"'
require_repo_literal "core/stop_hook_output_audience/stop_hook_output_audience_contract.py" \
  '"permissionDecisionReason": agent_directive.agent_directive_body'
# 文档回归护栏：`docs/architecture/stop-hook-output-audience-contract.md` 的跨宿主表此前把
# 「Kimi 认 decision:block」记为 ✅（实为 ❌——顶层 decision 通不过解构、结果是 allow）。那条
# 错误会诱导出「Kimi 被误判成 Codex 只是降级」的危险判断，故把纠错段钉死，防止被改回去。
require_repo_literal "docs/architecture/stop-hook-output-audience-contract.md" \
  'Kimi **不**认 `decision:block`'
require_repo_literal "docs/architecture/stop-hook-output-audience-contract.md" \
  'Stop 上 `hookSpecificOutput.permissionDecision:"deny"`'
# Kimi host 身份与装配：三套 transcript 签名必须两两不相交（相交即静默误判），且每个可被
# 正向识别的 host 都必须有自己的装配分支（缺分支不会报错，只会静默落进 Codex 兜底）。
run_step "host transcript format detection self-check" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/core/host_adapter/host_transcript_format_detection.py"
# P-5 词表一致性：host 身份词表与 reviewer-registry 的 harness id 是两套独立演化的词表，
# 前两项（codex / claude_code）历史上恰好同名纯属巧合。Kimi 接入时巧合断裂——host 身份取
# ``kimi_code_cli`` 而 registry 是 ``kimi_code`` ⇒ 主 harness 排不出 alternative reviewer 池，
# 200-seed 实测 44% 的 Kimi 会话被 Kimi 自己评审，**全程零报错**（双评审的价值全在异构性上）。
# 判据是结构性的（遍历 KNOWN_HOST_IDENTITIES），所以加第四个 host 时同样当场炸。
run_step "host identity / reviewer registry vocabulary consistency" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/scripts/check_host_identity_and_reviewer_registry_harness_id_vocabulary_consistency.py"
# 上面那条 gate 的第二条断言只有在 VALID_MAIN_HARNESSES 真的派生自 registry 时才有检测力：
# 一旦被改回手写清单，它就退化成「清单碰巧写全了」的假绿。故把派生实现本身钉死。
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/prepare_review_run.py" \
  'VALID_MAIN_HARNESSES = _resolve_valid_main_harnesses()'
# 同一条断言的另一半：它必须按**仓库内 seed** registry 求值。曾经的实现裸 import
# prepare_review_run，而那个常量优先读运行时 user-local registry ⇒ 仓库完全正确、只是本机
# registry 落后一个 host，提交前 gate 就红，并造出「必须先部署才能让 gate 转绿」的循环依赖。
# 两个方向（不读运行时状态 / 手写清单仍当场红）都由这份测试钉住。
run_step "tests: host_identity_reviewer_registry_vocabulary_gate" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_host_identity_reviewer_registry_vocabulary_gate.py"
# S-B6 跨 turn 再入预算：宿主的保险丝对 Kimi 完全无效（`stop_hook_active` 恒 false，引擎的
# 续跑标记每 turn 复位）⇒「driven 一回合 → agent 停下 → 再 driven 一回合」在宿主侧没有上界。
# 本仓已实测过一次 286 次无上界续跑，所以这道闸必须由 RVF 自带。
run_step "stop hook reentry budget self-check" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/core/stop_hook_reentry_budget/stop_hook_reentry_budget.py"
run_step "tests: stop_hook_reentry_budget" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_stop_hook_reentry_budget.py"
# 预算的三条复位路径里，「digest 变化」最容易退化成恒真——驱动正文每次都带新铸的
# RVF_DECISION_TOKEN，拿正文做 digest 等于关掉这道闸。故把「用 reason_code 而非正文」钉死。
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" \
  'stable_agent_directive_scope_digest_source=reason_code'
# 开审前门禁需验证真实隔离 Git 与原生消息 schema，不能仅检查状态字段。
run_step "tests: review_start_independent_user_turn_boundary" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q "$tests_dir/test_review_start_independent_user_turn_boundary.py" \
    "$tests_dir/test_review_candidate_snapshot_without_native_rewind.py" \
    "$tests_dir/test_review_packet_from_frozen_candidate_snapshot.py" \
    "$tests_dir/test_review_start_boundary_request_retry.py" \
    "$tests_dir/test_review_without_pre_review_base_absorption.py"
# Kimi 激活态核对：installed.json 磁盘上**没有** state 字段（persist 只落 9 个），所以判定必须
# 自己复算；且单条 hook 不过 schema 时 Kimi 只 warn + 丢弃该条、plugin 仍报 ok ⇒「看起来健康、
# 实际 Stop hook 不存在」是零报错状态。核对脚本本身读 live infra，故只在 fixture 测试里跑。
run_step "tests: kimi_plugin_activation_state_check" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_kimi_plugin_activation_state_check.py"
# Kimi 注册安装器：注册在四条跨进程通道上全部不可达 ⇒ 只能写 installed.json。两条风险各钉一根钉：
# 全量重写会掀掉用户装的其它插件（必须幂等 additive merge）；state 写不进去（persist 只落 9 个
# 字段）⇒「安装器写完了」不等于「注册生效了」，闭环由独立复算的核对器给。
run_step "tests: kimi_plugin_registration_installer" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_kimi_plugin_registration_installer.py"
require_repo_literal "scripts/rvf_local_deploy.py" \
  '"--configure-kimi-plugin-registration",'
run_step "kimi session layout self-check" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/adapters/kimi/session_layout.py"
# Kimi wire 蒸馏 + adapter 行为。三条最容易静默出错的地方：origin.kind 区分真实输入与系统
# 注入（混同会污染 scope 归属）；think 的正文在 part.think 而非 part.text（取错静默变空串）；
# 写型工具参数是 path 而非 Claude 的 file_path（照抄会让 artifact_refs 恒为空且不报错）。
run_step "tests: kimi_host_adapter_and_transcript" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_kimi_host_adapter_and_transcript.py"
# 结构性 + 行为 gate（F5·H5a）：Stop 事件缺 last_assistant_message 时按 host 读 transcript 找 handoff
# 标记——漏接一个 host 不报错，只会让它的 RVF run 写完 handoff 却永不 finalize（Kimi 曾 4/4）。
run_step "tests: rvf_handoff_marker_detection_across_host_transcripts" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_rvf_handoff_marker_detection_across_host_transcripts.py"
# finalize 本身（标 units reviewed / 释放 lease / 推高水位）。这两份此前不在任何 gate 里，
# test_rvf_run_finalize 因此从 2026-06-25（a0f206c 的 no-op 守卫）起红了三个月没人看见。
run_step "tests: rvf_run_finalize" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_rvf_run_finalize.py" \
    "$tests_dir/test_automatic_closeout_bridge.py" \
    "$tests_dir/test_cancel_rvf_run_finalize.py"
# 测试卫生守卫本身的判据：认得出本次套件的泄漏、放得过活会话的并发写入（F5·H5b）。
run_step "tests: check_contract_suite_left_no_test_artifacts_in_real_rvf_state_roots" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_check_contract_suite_left_no_test_artifacts_in_real_rvf_state_roots.py"
require_repo_literal "adapters/kimi/transcript.py" \
  '_ARTIFACT_PATH_ARGUMENT_NAME = "path"'
require_repo_literal "adapters/kimi/transcript.py" \
  'CONTENT_PART_TYPE_THINK: "think"'
require_repo_literal "adapters/kimi/transcript.py" \
  'def distill_kimi_wire_jsonl'
# 三处 host 派发漏接任一处，都会静默退回 Codex 的实现去解析 Kimi 的数据。
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/subagent_capture.py" \
  'return distill_kimi_wire_jsonl'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/subagent_capture.py" \
  'return read_kimi_originator(src)'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/subagent_capture.py" \
  '_kimi_resolve_subagents('
# 上面三处只覆盖**子代理**轨迹。主轨迹另有两处独立派发（蒸馏 / RVF-起点 cut）、父会话 context
# 渲染一处、harness 身份归一一处 —— 同一个形状、同样静默：Codex 的解析器读 Kimi 的数据不抛，
# 只返回 0 条 / None / 空行。行为断言在 tests/test_kimi_host_adapter_and_transcript.py
# （KIMI-9/10/11/12，逐 KNOWN_HOST_IDENTITIES 断言分派各有其人）；这里再钉住派发点本身的存在。
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_capture.py" \
  'return distill_kimi_wire_jsonl'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_capture.py" \
  'return find_rvf_start_in_kimi_wire_jsonl'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_capture.py" \
  'cut = _host_find_rvf_start(post_host_kind)('
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_capture.py" \
  'distilled, distill_index = _host_distill(post_host_kind)('
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_parent_context.py" \
  'return _render_kimi_record'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_parent_context.py" \
  'renderer = _host_record_renderer(host)'
require_repo_literal "core/host_adapter/parent_harness_identity.py" \
  'PARENT_HARNESS_IDENTITY_KIMI_CODE = "kimi-code"'
require_repo_literal "core/host_adapter/parent_harness_identity.py" \
  '("kimi", PARENT_HARNESS_IDENTITY_KIMI_CODE),'
# kimi-code stamp 是 HOST-2 的 Kimi 对偶：transcript 合成落空时它是唯一的 Kimi 线索，没有它
# 整条链路兜底 Codex ⇒ 发 decision:block ⇒ Kimi 静默 allow（零报错）。
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/host_adapter_resolver.py" \
  '"kimi-code": HOST_KIMI_CODE,'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/host_adapter_resolver.py" \
  'HOST_BY_PLUGIN_ENTRY_SOURCE_PROVIDER_STAMP.get(source.get("provider"))'
# Kimi hook 入口：事件只有四个字段、**没有 transcript_path**，shim 必须合成它——否则 host 判成
# Codex ⇒ 发 decision:block ⇒ 被 Kimi 的 looseObject 静默吞掉 ⇒ RVF 完全失声且零报错。
run_step "tests: kimi_hook_entry_normalization" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_kimi_hook_entry_normalization.py"
require_repo_literal "plugins/review-validate-fix/.kimi-plugin/plugin.json" '"event": "Stop"'
require_repo_literal "plugins/review-validate-fix/.kimi-plugin/plugin.json" '"event": "UserPromptSubmit"'
require_repo_literal "plugins/review-validate-fix/hooks/_kimi_hook_entry.py" 'def normalize_kimi_event'
require_repo_literal "plugins/review-validate-fix/hooks/_kimi_hook_entry.py" 'def owns_invocation'

require_repo_literal "core/host_adapter/host_transcript_format_detection.py" \
  'HOST_KIMI_CODE = "kimi_code"'
require_repo_literal "adapters/kimi/kimi_code_cli_host_adapter.py" \
  'class KimiCodeCliHostAdapter'
require_repo_literal "adapters/kimi/kimi_code_cli_host_adapter.py" \
  'def parent_transcript_quoted_session_name'
# 结构性护栏：Kimi 条目绝不能进 Claude 的 hooks.json——Codex 的 plugin loader 会把
# plugin-packaged hooks.json 也当 hooks 源加载，混进去就是三重触发。
forbid_repo_literal "plugins/review-validate-fix/hooks/hooks.json" 'kimi'
# 备选评审腿的 prompt 投递契约：kimi 的 `-p` 是带值 flag 且从不读 stdin，而整份 review packet
# 远超 ARG_MAX ⇒ 必须走「argv 只带短指针、正文留在磁盘」。同时钉住 `--dry-run` 打印的是终结后的
# 真实 argv，且仍停在 pre_run_health 之前（否则零成本预演变成每次烧一次真实 probe）。
run_step "tests: reviewer_prompt_delivery_contract" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_reviewer_prompt_delivery_contract.py"
# kimi / omp 结构化 stdout：kimi 无 result 信封（否则整份 JSONL 被当评审正文）；「重试」不等于
# 「失败」（在首次 429 判死会把可恢复重试变成硬失败腿）；omp 用 --mode json 时报错也退 0，
# 额度错误必须 typed 识别；omp 的 isError 不是 cursor spawnError 的对偶，刻意不据它 fail-over。
run_step "tests: reviewer_structured_output_extraction" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_reviewer_structured_output_extraction.py"
# 健康探针必须与派发命令走同一条后端路径。这条纪律原本只以 cursor 模板里的一段注释存在，
# claude/codex 两份则原样带着 `auth status` / `login status` —— 同一失效形状，只是还没发作。
# run rvf-20260811T170157Z：cursor 登录过期时 status 退 0 而 -p 退 1，RVF 因此把死腿选进两腿，
# 该轮实际只剩单路评审结论却全程自认为做了双评审。
run_step "tests: reviewer_health_probe_parity" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_reviewer_health_probe_parity.py"
# reviewer registry 是 additive merge 而非首次 seed：已部署过的机器上 user-local registry
# 早就存在，若沿用「不存在才 copy」，后续新增的评审腿（kimi_code / oh_my_pi 是第一批）永远
# 进不去那台机器，而部署照常报成功、派发照常从旧的 3 条腿里选 2 条 —— 无人报错的假绿。
# 反向也要钉住：merge 不得覆盖用户改过的条目（关掉的腿不能被一次部署重新打开）。
run_step "tests: reviewer_registry_additive_merge" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_reviewer_registry_additive_merge.py"
# 同额度池互斥：omp 腿被 pin 到 Codex 订阅，与 codex 腿同模型同账号 ⇒ 抽到这一对时双评审
# 退化成同一模型评两遍（实测 200 seed 下 16.5%），且该订阅额度耗尽会同时打掉两腿，而
# fail-over 假设各腿独立。指纹取「额度池」而非模型名：五条腿里只有 omp pin 了模型，
# 其余四条跟随各自 CLI 的默认模型、随时会变，写模型名第二天就会撒谎。
run_step "tests: reviewer_quota_pool_exclusive_selection" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_reviewer_quota_pool_exclusive_selection.py"
# probe 结果缓存：probe_available 每轮对每条 enabled harness 真跑一次 --preflight，那是真实
# 模型调用（omp 单次实测 input 39,499 tokens），5 条腿后每轮稳态代价可观。缓存只复用一次
# **真实** probe 的结论，绝不用廉价信号替代它——那正是 health parity gate 要防的形状。
# 三条方向性不变量必须钉死：读侧任何异常退化成 miss 而非「可用」；成功/失败 TTL 不对称；
# 派发失败当场作废缓存（否则一条「可用」缓存会在整个 TTL 内自我延续地把死腿喂回选腿层）。
run_step "tests: reviewer_harness_probe_result_cache" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_reviewer_harness_probe_result_cache.py"
run_step "tests: trajectory_distill" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q \
    "$tests_dir/test_trajectory_distill.py"

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
# $rvf-land 入口已统一为 namespaced skill（薄 shim command 已删，见 docs/architecture/cross-harness.md §3）
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" 'rvf_handoff_intake.py'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" 'rvf_worktree_differs_from_current'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" 'base-branch-sync 不是本 skill 的默认步骤'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" '用户已经明确要求后续运行'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" '不要停下询问“是否现在运行”'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" '如果用户没有明确要求后续 base 同步'
# land 单步化：三个 land 入口的机械步骤都必须经 rvf_land.py，而不是回到逐条手跑（每条都是一次 agent 请求）。
# $rvf-land 经 `rvf land` 快捷入口转给 rvf_land.py（省掉手拼脚本路径与手抄 run_dir）。
# 占位符字面量两侧各钉一次：SKILL 叫 agent 保留它、脚本按它替换验证结果，任一侧改名另一侧就静默失效。
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" 'rvf land prepare'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" 'rvf land commit'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" '[[rvf-land-validation-summary]]'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_land.py" 'RVF_LAND_COMMIT_MESSAGE_VALIDATION_SUMMARY_PLACEHOLDER = "[[rvf-land-validation-summary]]"'
require_repo_literal "plugins/review-validate-fix/skills/rvf-handoff-intake/SKILL.md" 'rvf_land.py prepare --cross-worktree'
require_repo_literal "plugins/review-validate-fix/commands/rvf-handoff-commit.md" 'rvf_land.py prepare --cross-worktree'
require_literal "SKILL.md" 'handoff-validation-commands.json'
# 失败再入 $rvf-reopen skill + 确定性后端脚本（防部署门漂移、漏部署）；入口已统一为 namespaced skill
require_repo_literal "plugins/review-validate-fix/skills/rvf-reopen/SKILL.md" 'rvf_rescope.py'
require_repo_literal "plugins/review-validate-fix/skills/rvf-reopen/SKILL.md" 'target_run_id'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_rescope.py" 'def resolve_target_run_id'
require_repo_literal "core/session_scope_allocation/reviewable_unit_diff_tracker.py" 'def invalidate_reviewed_units_for_run'
require_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'review_scope_reopened_for_failed_impl'
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
require_literal "SKILL.md" '固定分行标签结构'
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
require_literal "references/review-merge-policy.md" 'alternative-reviewer:<harness-id>'
require_literal "references/review-merge-policy.md" 'scripts/dispatch_reviewers.py --execute --repo <repo> --review-packet <packet> --session-context <file>'
# 健康探针必须与派发命令走同一条后端路径（run rvf-20260811T170157Z 的教训）。
# health_probe_parity_exemption 只给测试夹具用（夹具的 reviewer 是 `python3 -c "<脚本>"`，
# 两边 -c 载荷本来就必须不同、不存在可比对的 CLI 入口）。它一旦出现在出厂模板里，
# 就等于把这条 gate 在生产路径上关掉——因此这里逐份禁死。
for reviewer_template in \
  "config/alternative-reviewer.cursor.json" \
  "config/alternative-reviewer.claude.json" \
  "config/alternative-reviewer.codex.json" \
  "config/alternative-reviewer.kimi.json" \
  "config/alternative-reviewer.oh-my-pi.json"; do
  forbid_literal "$reviewer_template" 'health_probe_parity_exemption'
  require_literal "$reviewer_template" '"pre_run_health": true'
  require_literal "$reviewer_template" '"health_command"'
done
# 三份既有模板曾经带着旁路查询当健康探针：cursor 已在 2026-08-11 实盘发作，claude/codex 是
# 同一失效形状的未爆版本。禁死这三条字面量，防止「顺手改回去」。
forbid_literal "config/alternative-reviewer.claude.json" '"auth",'
forbid_literal "config/alternative-reviewer.codex.json" '"login",'
forbid_literal "config/alternative-reviewer.cursor.json" '"status"'
require_literal "config/alternative-reviewer.kimi.json" 'argv_prompt_flag_with_prompt_file_pointer'
require_literal "config/alternative-reviewer.kimi.json" 'alternative-reviewer:kimi-code'
require_literal "config/alternative-reviewer.kimi.json" 'kimi_stream_json'
forbid_literal "config/alternative-reviewer.kimi.json" '"--yolo"'
forbid_literal "config/alternative-reviewer.kimi.json" '"--auto"'
require_literal "config/alternative-reviewer.oh-my-pi.json" 'alternative-reviewer:oh-my-pi'
require_literal "config/alternative-reviewer.oh-my-pi.json" 'oh_my_pi_json'
require_literal "config/alternative-reviewer.oh-my-pi.json" 'oh_my_pi_default_model_role'
forbid_literal "config/alternative-reviewer.oh-my-pi.json" 'gpt-5.6-sol'
require_literal "config/alternative-reviewer.oh-my-pi.json" '"xhigh"'
# ⚠️ 上面这些 require_literal 是**整文件** grep，给不出「这个字面量在哪个字段」的保证——
# 同一个串出现在 health_command 或注释里就会绿。字段级的强保证在
# tests/test_reviewer_prompt_delivery_contract.py 的出厂模板结构断言里（已登记进 run_step）。
# 这里保留它们只作粗粒度绊线：整份文件里彻底消失时仍会当场红。
require_literal "scripts/run_alternative_reviewer.py" 'assert_health_command_shares_dispatch_flag_set'
require_literal "scripts/run_alternative_reviewer.py" 'finalize_reviewer_command_for_prompt_delivery'
require_literal "scripts/run_alternative_reviewer.py" 'STRUCTURED_JSON_LINES_OUTPUT_FORMATS'

# Claude harness 模板（曾是被 0ba4a1d 覆盖掉的 active 默认，现为 registry 中的 claude_code 模板）
require_literal "config/alternative-reviewer.claude.json" 'alternative-reviewer:claude-code'
require_literal "config/alternative-reviewer.claude.json" 'ANTHROPIC_API_KEY'
require_literal "config/alternative-reviewer.claude.json" 'Bash,Read'
require_literal "config/alternative-reviewer.claude.json" 'claude_stream_json'
require_literal "config/alternative-reviewer.claude.json" 'stream-json'
forbid_literal "config/alternative-reviewer.claude.json" 'ANTHROPIC_BASE_URL'
forbid_literal "config/alternative-reviewer.claude.json" '"env_set"'
forbid_literal "config/alternative-reviewer.claude.json" '127.0.0.1:8080'
# Cursor harness 模板 = run_alternative_reviewer.py 的 DEFAULT_CONFIG（无 --config 时默认）
require_literal "config/alternative-reviewer.cursor.json" 'alternative-reviewer:cursor-cli'
require_literal "config/alternative-reviewer.cursor.json" 'cursor_stream_json'
require_literal "config/alternative-reviewer.cursor.json" 'stream-json'
forbid_literal "config/alternative-reviewer.cursor.json" 'dontAsk'
forbid_literal "config/alternative-reviewer.cursor.json" '--append-system-prompt'
forbid_literal "config/alternative-reviewer.cursor.json" 'alternative code reviewer'
forbid_literal "config/alternative-reviewer.cursor.json" 'santa-method double review'
require_literal "config/alternative-reviewer.cursor.json" 'allow_repo_cwd'
require_literal "config/alternative-reviewer.cursor.json" 'idle_timeout_seconds'
require_literal "config/alternative-reviewer.cursor.json" 'activity_check_interval_seconds'
require_literal "config/alternative-reviewer.cursor.json" 'activity_probe_command'
require_literal "config/alternative-reviewer.cursor.json" 'max_runtime_seconds'
require_literal "config/alternative-reviewer.cursor.json" 'pre_run_health'
# Codex harness 模板
require_literal "config/alternative-reviewer.codex.json" 'alternative-reviewer:codex-cli'
require_literal "config/alternative-reviewer.codex.json" 'codex_json'
# reviewer-registry.json：本机 external reviewer harness 注册表（单一事实源）
require_literal "config/reviewer-registry.json" 'schema_version'
require_literal "config/reviewer-registry.json" 'label_prefix'
require_literal "config/reviewer-registry.json" 'config_path'
require_literal "config/reviewer-registry.json" 'external_cli'
require_literal "config/reviewer-registry.json" 'priority_default'
require_literal "config/reviewer-registry.json" 'alternative-reviewer:cursor-cli'
require_literal "config/reviewer-registry.json" 'alternative-reviewer:claude-code'
require_literal "config/reviewer-registry.json" 'alternative-reviewer:codex-cli'
require_literal "config/reviewer-registry.json" 'alternative-reviewer:kimi-code'
require_literal "config/reviewer-registry.json" 'alternative-reviewer:oh-my-pi'
# 同额度池互斥的声明字段（选腿层据它避开「同一模型 + 同一订阅」的两腿组合）。
require_literal "config/reviewer-registry.json" 'underlying_provider_account_quota_pool_identity'
# ⚠️ 上面这些 require_literal 是整文件 grep，只挡「整条 harness 或整个字段消失」这类粗差错；
# 它证明不了「每条 harness 都声明了指纹」「registry 与磁盘上的 config 文件一一对应」这类
# 字段级事实——同一个串出现在 description 或某条注释里就会绿。字段级保证在
# tests/test_reviewer_quota_pool_exclusive_selection.py（QP-1/QP-2/QP-3）与
# tests/test_reviewer_registry_additive_merge.py，两者都已登记进下面的 run_step。
# dispatch_reviewers.py：路由 + 派发单一入口
require_literal "scripts/dispatch_reviewers.py" 'def route'
require_literal "scripts/dispatch_reviewers.py" 'reviewer-plan.json'
require_literal "scripts/dispatch_reviewers.py" 'available_reviewer_harness_mismatch'
require_literal "scripts/dispatch_reviewers.py" 'cursor_unavailable'
require_literal "scripts/dispatch_reviewers.py" 'needs_last_resort_fallback'
require_literal "scripts/dispatch_reviewers.py" '--plan-only'
require_literal "scripts/dispatch_reviewers.py" '--execute'
require_literal "scripts/dispatch_reviewers.py" '--require-external'
require_literal "scripts/dispatch_reviewers.py" '--reviewer-id'
require_literal "scripts/dispatch_reviewers.py" 'RVF_MAIN_HARNESS'
# 额度耗尽检测 + cooldown 回退：新 warning code / 退出码契约
require_literal "scripts/dispatch_reviewers.py" 'harness_limit_cooldown_active'
require_literal "scripts/dispatch_reviewers.py" 'main_harness_usage_limit_exhausted'
require_literal "scripts/dispatch_reviewers.py" 'all_harnesses_usage_limited'
require_literal "scripts/dispatch_reviewers.py" 'fallbacks'
require_literal "scripts/harness_limit_cooldown.py" 'def parse_reset_hint'
require_literal "scripts/harness_limit_cooldown.py" 'def record'
require_literal "scripts/run_alternative_reviewer.py" 'usage_limit_exhausted'
require_literal "scripts/run_alternative_reviewer.py" 'EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG'
require_literal "scripts/run_alternative_reviewer.py" 'def looks_like_usage_limit'
require_literal "scripts/run_alternative_reviewer.py" '--reviewer-id'
require_literal "scripts/prepare_review_run.py" 'RVF_MAIN_HARNESS'
require_literal "scripts/prepare_review_run.py" 'main-harness.json'
require_literal "scripts/run_alternative_reviewer.py" 'env_unset'
require_literal "scripts/run_alternative_reviewer.py" 'env_set'
require_literal "scripts/run_alternative_reviewer.py" 'build_alternative_reviewer_subprocess_environment'
require_literal "scripts/run_alternative_reviewer.py" 'alternative-reviewer-config-overrides'
require_literal "scripts/run_alternative_reviewer.py" 'user_local_config_override'
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
require_repo_literal "tests/_rvf_review_support/alternative_reviewer_cli_adapter_behaviors.py" 'test_alternative_reviewer_user_local_override_applies_only_to_reviewer_process'
require_repo_literal "tests/_rvf_review_support/alternative_reviewer_cli_adapter_behaviors.py" 'test_alternative_reviewer_user_local_override_health_failure_fails_closed'
require_repo_literal "tests/_rvf_review_support/alternative_reviewer_cli_adapter_behaviors.py" 'test_alternative_reviewer_environment_merge_precedence'
require_repo_literal "tests/_rvf_review_support/alternative_reviewer_cli_adapter_behaviors.py" 'test_alternative_reviewer_user_local_override_contract_rejects_invalid_configs'
require_repo_literal "tests/_rvf_review_support/alternative_reviewer_cli_adapter_behaviors.py" 'test_alternative_reviewer_user_local_override_short_value_does_not_break_stream_json'
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
require_repo_literal "core/run_ledger/run_ledger.py" 'command-lock'
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
require_repo_literal "core/run_ledger/run_ledger.py" 'class RunLedger'
require_repo_literal "core/run_ledger/run_ledger.py" 'cline-kanban'
require_repo_literal "core/run_ledger/run_ledger.py" 'RVF_STATE_PHASES'
require_repo_literal "core/run_ledger/run_ledger.py" 'kanban-task'
require_repo_literal "core/run_ledger/run_ledger.py" 'rvf_state_fields'
require_repo_literal "core/run_ledger/run_ledger.py" 'events.jsonl'
require_repo_literal "core/run_ledger/run_ledger.py" 'summary.json'
require_repo_literal "core/run_ledger/run_ledger.py" 'reason_code'
require_repo_literal "core/run_ledger/run_ledger.py" 'RVF_RUN_DIR'
require_repo_literal "core/run_ledger/run_ledger.py" 'log_unavailable'
require_repo_literal "core/session_scope_allocation/session_change_manifest.py" 'owned_paths'
require_repo_literal "core/session_scope_allocation/session_change_manifest.py" 'unattributed_dirty_paths'
require_literal "scripts/build_review_packet.py" 'metadata-output'
require_literal "scripts/build_review_packet.py" 'session-manifest'
require_literal "scripts/build_review_packet.py" 'primary-file'
require_literal "scripts/build_review_packet.py" 'max-packet-bytes'
require_literal "scripts/build_review_packet.py" 'session context is required'
require_literal "scripts/build_review_packet.py" 'session_context_provided'
require_literal "scripts/workspace_snapshot.py" 'WORKSPACE_CHANGED'
require_literal "references/review-merge-policy.md" 'zero-external-reviewer-last-resort-in-harness-fallback.md'
require_literal "references/review-merge-policy.md" 'clean context'
require_literal "references/review-merge-policy.md" 'reviewer.summary.json'
require_literal "references/review-merge-policy.md" 'need-clean-review-context'
require_literal "references/review-merge-policy.md" 'reviewer-registry.json'
require_literal "references/review-merge-policy.md" 'codex-mimic-reviewer-a'
require_literal "references/review-merge-policy.md" 'source-agnostic'
require_literal "references/review-merge-policy.md" 'CONTRACT_VIOLATION'
require_literal "references/review-merge-policy.md" 'WORKSPACE_CHANGED_DURING_REVIEW'
require_literal "references/review-merge-policy.md" 'check_review_result.py'
require_literal "references/review-merge-policy.md" 'kind: request'
# 合并阶段结构化处置（RVF 降本 T4）：每条 reviewer issue 都有可机读处置；强制点在 upsert_canonical_issue
# （CLI upsert 与 rvf fix prepare 共用），只卡 fix prepare 会被 CLI upsert 绕过。
require_literal "references/review-merge-policy.md" 'rvf merge write'
require_literal "references/review-merge-policy.md" 'reviewer_issue_index_base: 1'
# 范围外问题不放跑、不当场修：照常通过 review、validator 只验证不修、属实即提交 Kanban backlog。
require_literal "prompts/reviewer.md" 'outside_current_round_scope'
require_literal "prompts/reviewer.md" '--current-round-scope-audit-summary'
require_literal "references/review-standards/reviewer.md" 'outside_current_round_scope'
require_literal "references/review-merge-policy.md" 'validate_only_then_submit_to_kanban_backlog'
require_literal "references/review-merge-policy.md" 'issue_indices_in_excluded_paths_one_based'
require_literal "prompts/validate-fix.md" 'validated_and_submitted_to_kanban_backlog'
require_literal "prompts/validate-fix.md" '--kanban-backlog-finding-file'
require_literal "prompts/validate-fix.md" '--acknowledged-protected-or-background-path-modification'
require_literal "references/handoff-template.md" '<B> 转 backlog'
require_literal "references/handoff-template.md" '## 范围外问题（已提交 Kanban backlog）'
require_literal "references/handoff-template.md" '## 已确认的越界修改'
require_literal "references/handoff-template.md" '[submitted_to_kanban_backlog]'
require_literal "SKILL.md" 'rvf backlog submit'
require_literal "SKILL.md" '--confirm-acknowledged-protected-or-background-path-modification'
require_repo_literal "plugins/review-validate-fix/skills/rvf-land/SKILL.md" 'rvf backlog submit'
require_literal "scripts/rvf_out_of_scope_findings_kanban_backlog_submission.py" 'INTEGRATION-POINT(kanban dedicated support agent finding-relation triage)'
require_literal "scripts/rvf_out_of_scope_findings_kanban_backlog_submission.py" 'KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_STATUS=not_triaged'
require_literal "SKILL.md" 'rvf merge write'
require_literal "prompts/validate-fix.md" 'main_agent_self_review'
require_literal "scripts/rvf_fix_issue.py" 'rvf_merge_dispositions.resolve_fix_issue_source_refs_for_upsert(run_dir, issue_id)'
require_literal "scripts/write_reviewed_task_candidate_evidence.py" 'rvf_merge_dispositions.require_passing_merge_dispositions('
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" '"merge-table.md"'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_agent_decision_cli.py" '"merge-table.md"'
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
require_literal "setup/mcp-setup-startup.md" '没有可用 external reviewer'
require_literal "references/review-merge-policy.md" 'routing_rule: R3'
require_literal "references/zero-external-reviewer-last-resort-in-harness-fallback.md" 'codex-mimic-reviewer-a'
require_literal "references/zero-external-reviewer-last-resort-in-harness-fallback.md" 'codex-mimic-reviewer-b'
require_literal "references/zero-external-reviewer-last-resort-in-harness-fallback.md" 'needs_last_resort_fallback'
require_literal "setup/mcp-setup-startup.md" 'setup 未完成不能阻塞'
forbid_literal "SKILL.md" '用户明确声明没有任何额外 coding agent'
forbid_literal "references/review-merge-policy.md" '用户明确声明没有其他 coding agent'
forbid_literal "setup/mcp-setup-startup.md" '用户也明确声明没有额外 coding agent'
# 禁止回归：默认 double-review 路径不得再把 in-harness `codex-reviewer` 文档化为默认第一腿。
# 默认两路 external 由 dispatch_reviewers.py 派发；in-harness mimic 仅是 R3 最后兜底。
forbid_literal "SKILL.md" 'codex-reviewer'
forbid_literal "references/review-merge-policy.md" 'codex-reviewer'
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
require_literal "scripts/stop_review_validate_fix.py" 'CODEX_RVF_FORK_MODE=cline-kanban'
require_literal "scripts/stop_review_validate_fix.py" 'kanban-followup'
require_repo_literal "core/rvf_shared_runtime_constants/rvf_shared_runtime_constants.py" 'RVF_KANBAN_FOLLOWUP_TRIGGER'
require_literal "scripts/stop_review_validate_fix.py" 'RVF_DISPATCH=token='
require_literal "scripts/stop_review_validate_fix.py" 'rvf_dispatch_prep_file_path'
require_literal "scripts/stop_review_validate_fix.py" 'rvf_dispatch_target_worktree'
require_literal "scripts/stop_review_validate_fix.py" 'cline-kanban-started'
require_literal "scripts/stop_review_validate_fix.py" 'cline_kanban_task_started'
require_literal "scripts/stop_hook_dispatcher.py" 'dev_sync_step_specs'
forbid_literal "scripts/stop_hook_dispatcher.py" 'install_to_codex.py'
require_repo_literal "README.md" 'Dev-only 标准'
require_repo_literal "README.md" 'RVF_CONTRACT_PARALLEL_TESTS'
require_repo_literal "README.md" 'RVF_CONTRACT_REVIEW_SUPPORT_SHARDS'
require_repo_literal "README.md" 'RVF_CONTRACT_STOP_HOOK_SHARDS'
require_repo_literal "README.md" '--shard-count'
require_repo_literal "README.md" 'Dev-only 标准'
require_repo_literal "scripts/install_to_codex.py" 'BUNDLE_IGNORED_NAMES'
require_repo_literal "tests/test_official_cli_installation_hooks.py" 'test_installer_has_no_manual_official_cache_or_installed_metadata_writer'
require_repo_literal "tests/test_stop_hook_dispatcher.py" 'test_dev_sync_step_specs_resolve_repo_level_dev_scripts'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-start-cmd'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-task-cmd'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-start-timeout'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-tmux-session'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-base-ref'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-auto-review-enabled'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-auto-review-mode'
require_repo_literal "scripts/install_to_codex.py" '--cline-kanban-start-in-plan-mode'
require_repo_literal "scripts/install_to_codex.py" '--configure-user-prompt-submit-hook'
require_repo_literal "scripts/install_to_codex.py" 'latest-official-cli-installation.json'
require_repo_literal "scripts/install_to_codex.py" 'review-validate-fix-local-main'
# 打包来源是 RVF 自有的 deploy 源 worktree，不是 local main 工作树；receipt 里不得把它
# 标成 local_main_worktree（那正是本设计要消灭的「身份撒谎」）。
require_repo_literal "scripts/install_to_codex.py" '--deployment-source-worktree'
require_repo_literal "scripts/install_to_codex.py" '"deployment_source_worktree"'
forbid_repo_literal "scripts/install_to_codex.py" '"local_main_worktree"'
# Installed skill 只持有稳定 local-main trampoline；安装与 cache 写入归宿主官方 CLI。
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" '--expected-local-main-commit HEAD --json'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" '降级到手写'
# 完成证据必须交代 payload 的真实来源，以及 runner 自身是否就是被部署的那一版。
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'deployment_source_worktree_synced_to_commit'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'runner_matches_local_main_head'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'local_main_dirty_paths'
forbid_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'scripts/install_to_codex.py'
forbid_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'verify_local_deploy.py'
require_repo_literal "scripts/rvf_local_deploy.py" '--configure-stop-hook'
require_repo_literal "scripts/rvf_local_deploy.py" '--configure-user-prompt-submit-hook'
require_repo_literal "scripts/rvf_local_deploy.py" 'LOCK_EX | fcntl.LOCK_NB'
# payload 只能来自 RVF 自有的 detached deploy 源 worktree：local main 的工作树全程只读 ref，
# 未提交内容从结构上进不了发布件（既不靠 stash 挪走，也不靠 dirty 硬拦）。
require_repo_literal "scripts/rvf_local_deploy.py" 'deployment-source-worktree-at-local-main-head'
require_repo_literal "scripts/rvf_local_deploy.py" '"worktree",'
require_repo_literal "scripts/rvf_local_deploy.py" '"--detach",'
require_repo_literal "scripts/rvf_local_deploy.py" '"clean", "-xdff"'
require_repo_literal "scripts/rvf_local_deploy.py" 'deployment_source_worktree_synced_to_commit'
require_repo_literal "scripts/rvf_local_deploy.py" 'runner_matches_local_main_head'
require_repo_literal "scripts/rvf_local_deploy.py" 'local_main_dirty_paths'
forbid_repo_literal "scripts/rvf_local_deploy.py" '--allow-dirty'
forbid_repo_literal "scripts/rvf_local_deploy.py" 'parser.add_argument("--repo"'
# 不得回退到 stash 路线：它会在部署窗口内把 local main 上正在工作的人/agent 的文件抽走，
# 且 refs/stash 是全仓共享的（20+ linked worktree 会互相顶掉栈顶）。
forbid_repo_literal "scripts/rvf_local_deploy.py" 'stash'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py" 'official cache 与 active plugin payload 不一致'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py" 'RVF_USER_LOCAL_DATA_ROOT'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py" 'deployment_source_worktree'
forbid_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py" 'local_main_worktree'
require_repo_literal "tests/test_official_cli_local_deploy_verification.py" 'test_verifier_accepts_official_cli_installation_and_shared_user_state'
require_repo_literal "tests/test_official_cli_local_deploy_verification.py" 'test_verifier_rejects_missing_official_codex_plugin'
# 重复注册必须判红，而不是只判「缺失」。存在性断言看不见第二份 RVF：本机因此从 2026-07-21 起
# 一直同时装着两套插件，两棵不同 commit 的树各自跑 hook、各写各的 review-highwater 高水位。
require_repo_literal "tests/test_official_cli_local_deploy_verification.py" 'test_verifier_rejects_second_codex_rvf_plugin_registration'
require_repo_literal "tests/test_official_cli_local_deploy_verification.py" 'test_verifier_rejects_second_claude_rvf_plugin_registration'
require_repo_literal "tests/test_official_cli_local_deploy_verification.py" 'test_verifier_rejects_second_codex_plugin_cache_payload_tree'
require_repo_literal "tests/test_official_cli_developer_plugin_installation.py" 'test_official_cli_install_removes_noncanonical_rvf_plugin_registrations'
require_repo_literal "tests/test_official_cli_developer_plugin_installation.py" 'test_official_cli_install_keeps_non_rvf_plugins_and_reports_no_removals'
require_repo_literal "tests/test_codex_hook_trust_fingerprint.py" 'test_second_rvf_registration_in_codex_config_blocks_rather_than_observes'
# 安装器必须枚举整个 review-validate-fix@* 命名空间，不得退回等值匹配单一 selector。
require_repo_literal "scripts/install_to_codex.py" 'noncanonical_review_validate_fix_plugin_selectors_in_official_cli_listing'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py" '存在多份 RVF 插件注册'
# 唯一性失败的**处置**同样是契约：绝不回滚（回滚会移除刚装好的 canonical、只留下残留那份），
# 且认证要的是「canonical 在册且唯一」的正向证明——「核不到」「canonical 没注册」都不得放行。
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py" 'RegistrationUniquenessError'
require_repo_literal "scripts/rvf_local_deploy.py" 'CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS'
require_repo_literal "tests/test_official_cli_local_deploy_verification.py" 'test_registration_uniqueness_failure_exits_with_dedicated_code_not_generic_verification_code'
require_repo_literal "tests/test_official_cli_local_deploy_verification.py" 'test_verifier_registration_uniqueness_exit_code_matches_deploy_runner_routing_constant'
require_repo_literal "tests/test_codex_hook_trust_fingerprint.py" 'test_codex_config_without_canonical_registration_is_not_reported_as_sole_registration'
require_repo_literal "tests/test_codex_hook_trust_fingerprint.py" 'test_canonical_selector_only_in_hook_trust_table_is_not_reported_as_sole_registration'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_registration_uniqueness_verification_failure_refuses_certification_without_rollback'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deploy_refuses_certification_when_registration_uniqueness_is_unavailable'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_validate_source_rejects_runner_loaded_from_task_worktree'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_validate_source_reports_dirty_local_main_without_blocking'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_validate_source_reports_runner_diverged_from_local_main_head'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_validate_source_rejects_expected_commit_mismatch'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_public_runner_cli_has_no_repo_or_dirty_bypass'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_dirty_local_main_deploys_from_head_without_touching_worktree'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_is_reused_and_scrubbed_between_deploys'
# 落点被占用时一律 fail-closed（绝不删）：外来仓库 worktree、本仓但挂了分支的 worktree 都要拒绝，
# 只有「不存在」与「空目录」才放行；最后一条守住 fail-closed 没把正常首建回路一起堵死。
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_fails_closed_on_foreign_worktree_residue'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_fails_closed_on_branch_attached_own_worktree'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_is_created_into_vacant_landing_site_with_stale_registration'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_fails_closed_when_head_does_not_converge'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_fails_closed_when_residual_paths_remain'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_validate_source_pins_runner_baseline_to_frozen_deployment_commit'
# 「同仓 + detached」这组形状任何人都手工造得出来，不构成归属证明；符号链接会让 is_dir()
# 看到一棵合规的树而把 reset --hard + clean -xdff 落到链接目标上。销毁回路只认 RVF 亲手
# 建树时写下的归属证据（落在该 worktree 私有 admin dir，reset/clean 抹不掉、prune 会带走）。
require_repo_literal "scripts/rvf_local_deploy.py" 'DEPLOYMENT_SOURCE_WORKTREE_OWNERSHIP_MARKER_FILENAME'
require_repo_literal "scripts/rvf_local_deploy.py" '_record_deployment_source_worktree_ownership'
require_repo_literal "scripts/rvf_local_deploy.py" '_deployment_source_worktree_has_rvf_ownership_evidence'
require_repo_literal "scripts/rvf_local_deploy.py" '--absolute-git-dir'
require_repo_literal "scripts/rvf_local_deploy.py" 'is_symlink()'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_fails_closed_on_own_detached_worktree_without_ownership_evidence'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_deployment_source_worktree_fails_closed_on_symlink_landing_site'
# 删除动作必须彻底消失：落点校验不得回退成「清理」。
forbid_repo_literal "scripts/rvf_local_deploy.py" 'rmtree'
# 成功部署必须回答「已安装 plugin 现在反映了哪些源 commit」，而不只给一个版本串。OLD 的唯一
# 事实源是部署开始时读到的 official installation receipt：用当前 HEAD 冒充 OLD 会让每次部署
# 都塌成 unchanged。回退/分叉与基线不可解析这两种边界必须有名字，才不会被含糊成「前进」。
require_repo_literal "scripts/rvf_local_deploy.py" 'source_commit_delta'
require_repo_literal "scripts/rvf_local_deploy.py" '禁止用当前 HEAD 冒充 OLD'
require_repo_literal "scripts/rvf_local_deploy.py" 'rollback_or_diverged'
require_repo_literal "scripts/rvf_local_deploy.py" 'unknown_revision'
require_repo_literal "scripts/rvf_local_deploy.py" 'boundary_explanation'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'source_commit_delta'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" '禁止用当前 HEAD 冒充 OLD'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'latest-official-cli-installation.json'
require_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'previous-installation-before-latest-attempt.json'
# 唯一部署标记仍是 official installation receipt：不得新增第二套标记文件，也不得手写 receipt。
forbid_repo_literal "scripts/rvf_local_deploy.py" 'last-deployed-source-commit'
forbid_repo_literal "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md" 'last-deployed-source-commit'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_reports_no_baseline_when_no_prior_installation_receipt'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_lists_forward_commits_since_previously_installed_commit'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_reports_unchanged_when_previously_installed_commit_matches'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_reports_rollback_or_diverged_with_both_directions'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_reports_unknown_revision_when_previous_commit_absent_from_repository'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_treats_abbreviated_previous_commit_as_unchanged'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_degrades_instead_of_raising_on_undecodable_commit_subject'
# commit subject 与 receipt 里的 old_commit 都是外部可控串：人读路径原样打印会让 ESC/OSC
# 直达终端，足以清屏盖掉前面的真实结论。转义只许发生在打印侧，JSON 回执保持原始值。
require_repo_literal "scripts/rvf_local_deploy.py" '_neutralize_terminal_control_sequences_for_human_readable_output'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_source_commit_delta_human_output_neutralizes_terminal_control_sequences'
require_repo_literal "tests/test_developer_local_main_installation.py" 'test_run_local_deploy_success_result_carries_source_commit_delta_from_installation_receipt'
require_repo_literal "tests/test_official_cli_developer_plugin_installation.py" 'test_transaction_restores_previous_active_release_and_official_installation'
require_repo_literal "tests/test_official_cli_installation_hooks.py" 'test_configure_hooks_deduplicates_rvf_entries_and_injects_user_local_roots'
require_repo_literal "tests/test_stop_hook_dispatcher.py" 'test_dev_sync_step_specs_never_include_an_installer'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_cline_kanban_mode_creates_and_starts_task_with_same_run'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_kanban_followup_mode_injects_current_task_message'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'dispatch_prep_payload'
require_repo_literal "tests/test_review_support_scripts.py" 'test_cline_kanban_client_create_and_start_task'
require_repo_literal "tests/test_review_support_scripts.py" 'test_prepare_review_run_writes_worktree_bootstrap'
require_repo_literal "tests/test_review_support_scripts.py" 'test_apply_worktree_bootstrap_replays_tracked_and_untracked'
require_literal "scripts/stop_review_validate_fix.py" 'CODEX_RVF_FORK_REASONING_EFFORT'
require_literal "scripts/stop_review_validate_fix.py" '$review-validate-fix'
require_literal "scripts/stop_review_validate_fix.py" 'stop_hook_active'
require_literal "scripts/stop_review_validate_fix.py" 'systemMessage'
require_literal "scripts/stop_review_validate_fix.py" 'desktop-control-unavailable-report'
require_literal "scripts/stop_review_validate_fix.py" 'desktop-control-unavailable-fail'
require_repo_literal "core/rvf_shared_runtime_constants/rvf_shared_runtime_constants.py" 'RVF_FORK_EXPERIMENT'
require_repo_literal "core/rvf_shared_runtime_constants/rvf_shared_runtime_constants.py" 'RVF_FORKED_REVIEW_VALIDATE_FIX'
require_literal "scripts/stop_review_validate_fix.py" 'DEFAULT_RVF_MODE = "fork"'
require_literal "scripts/stop_review_validate_fix.py" 'DEFAULT_FORK_LAUNCH_MODE = "auto"'
require_literal "scripts/stop_review_validate_fix.py" 'CODEX_RVF_MODE'
require_literal "scripts/stop_review_validate_fix.py" 'StopDecision'
require_literal "scripts/stop_review_validate_fix.py" 'normalize_backend_from_env'
require_literal "scripts/stop_review_validate_fix.py" 'evaluate_stop_event'
require_literal "scripts/stop_review_validate_fix.py" 'launch_backend'
require_literal "scripts/stop_review_validate_fix.py" 'session_hook_gate_disabled'
require_literal "scripts/stop_review_validate_fix.py" 'session_hook_gate_enabled'
require_literal "scripts/stop_review_validate_fix.py" '不是关闭全局 Stop hook'
require_literal "scripts/stop_review_validate_fix.py" 'dispatcher 仍会运行'
require_literal "scripts/stop_review_validate_fix.py" 'manual-prepared'
require_literal "scripts/stop_review_validate_fix.py" 'no Terminal was launched'
# S9a：app-server fork-request 构造（thread/fork、turn/start）已抽到 adapters/codex
require_repo_literal "adapters/codex/codex_gui_fork_app_server_bridge.py" 'thread/fork'
require_repo_literal "adapters/codex/codex_gui_fork_app_server_bridge.py" 'turn/start'
require_literal "scripts/stop_review_validate_fix.py" 'Terminal/CLI fork launch is intentionally disabled'
require_literal "scripts/stop_review_validate_fix.py" 'CODEX_RVF_FORK_REASONING_EFFORT'
require_literal "scripts/stop_review_validate_fix.py" 'review-validate-fix-fork'
require_literal "scripts/stop_review_validate_fix.py" 'RVF_HANDOFF_FILE'
require_literal "scripts/diagnose_fork.py" 'run_fork_experiment'
require_literal "scripts/diagnose_fork.py" 'CODEX_RVF_FORK_EXPERIMENT_MODE'
require_literal "scripts/stop_review_validate_fix.py" 'fork'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_fork_experiment_marker_no_longer_triggers_stop_hook_fork'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_diagnose_fork_dry_run_writes_requests'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_dirty_repo_dry_run_injects_decision_point'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_auto_mode_creates_cline_kanban_task_by_default'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_auto_mode_reports_kanban_unavailable_without_default_gui_fallback'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_auto_mode_can_opt_into_legacy_gui_as_backup_of_backup'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_auto_mode_reports_stale_kanban_listener_without_gui_fallback'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_dirty_repo_manual_mode_injects_decision_point'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_dirty_repo_fork_dry_run'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_cline_kanban_mode_creates_and_starts_task_with_same_run'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_cline_kanban_mode_marks_unavailable_when_task_start_fails'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'reason=suppressed'
require_repo_literal "tests/test_review_support_scripts.py" 'test_cline_kanban_client_create_and_start_task'
require_repo_literal "tests/test_review_support_scripts.py" 'test_prepare_review_run_writes_worktree_bootstrap'
require_repo_literal "tests/test_review_support_scripts.py" 'test_apply_worktree_bootstrap_replays_tracked_and_untracked'
require_repo_literal "tests/test_review_support_scripts.py" 'test_run_ledger_summary_preserves_cline_kanban_fields'
require_repo_literal "tests/test_review_support_scripts.py" 'test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_stop_event_transcript_path_overrides_bad_env_thread_id'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_stop_event_log_path_is_not_used_as_fork_rollout_path'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_dirty_repo_continuation_mode_reports_removed_fallback'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_no_git_cwd_skips_even_with_dirty_trusted_repo'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_forked_rvf_session_gets_programmatic_handoff_advisory'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_handoff_marker_in_dirty_repo_does_not_create_new_fork'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_forked_rvf_session_waits_for_handoff_before_advisory'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_missing_cwd_skips_and_requests_target_repo'
require_repo_literal "tests/test_stop_review_validate_fix.py" 'test_log_unavailable_does_not_break_hook_payload'
forbid_repo_literal "scripts/install_to_codex.py" 'sync_codex_plugin_cache'
forbid_repo_literal "scripts/install_to_codex.py" 'sync_claude_plugin'
forbid_repo_literal "scripts/install_to_codex.py" 'installed_plugins.json'
forbid_repo_literal "scripts/install_to_codex.py" '.codex/plugins/cache'
forbid_repo_literal "scripts/install_to_codex.py" '.claude/plugins/cache'
require_repo_literal "scripts/install_to_codex.py" 'PLUGIN_NAME = "review-validate-fix"'
require_repo_literal "scripts/install_to_codex.py" 'install_developer_marketplace_with_official_clis'
require_repo_literal "scripts/install_to_codex.py" 'subprocess.run('
require_repo_literal ".agents/plugins/marketplace.json" 'review-validate-fix-local-main'
require_repo_literal ".claude-plugin/marketplace.json" 'review-validate-fix-local-main'
require_repo_literal ".claude-plugin/marketplace.json" './plugins/review-validate-fix'
require_repo_literal "tests/test_official_cli_developer_plugin_installation.py" 'test_official_cli_first_install_uses_marketplace_add_and_plugin_add'
require_repo_literal "tests/test_official_cli_developer_plugin_installation.py" 'test_official_cli_update_uses_codex_remove_add_and_claude_update'
forbid_repo_literal "scripts/install_to_codex.py" 'remove_legacy_codex_skill_dir'
forbid_repo_literal "scripts/install_to_codex.py" 'remove_legacy_plugin_cache'
forbid_repo_literal "scripts/install_to_codex.py" 'legacy_plugin_config_ids'
forbid_repo_literal "scripts/install_to_codex.py" 'remove_plugin_sections'
forbid_repo_literal "scripts/install_to_codex.py" 'PLUGIN_NAME = "rvf"'
forbid_repo_literal "plugins/review-validate-fix/.codex-plugin/plugin.json" '"name": "rvf"'
forbid_repo_literal "scripts/install_to_codex.py" 'migrated legacy stand''alone setup'
forbid_repo_literal "scripts/install_to_codex.py" 'Review Validate Fix CLI Launch''er'
forbid_repo_literal "scripts/install_to_codex.py" 'stand''alone'

# S1: NormalizedTranscript + transcript adapter 拆分 + vendor-on-install 哨兵 bootstrap。
require_repo_file ".rvf-pyroot"
require_repo_file "core/transcript/models.py"
require_repo_file "core/transcript/io.py"
require_repo_file "adapters/codex/transcript.py"
require_repo_file "adapters/claude_code/transcript.py"
require_repo_file "tests/test_normalized_transcript.py"
require_repo_file "tests/test_vendored_payload_import.py"
require_repo_literal "core/transcript/models.py" 'class TranscriptRecord'
require_repo_literal "core/transcript/models.py" 'class NormalizedTranscript'
require_repo_literal "scripts/install_to_codex.py" 'for package_name in ("core", "adapters")'
require_repo_literal "scripts/install_to_codex.py" 'generated_plugin_root / ".rvf-pyroot"'
require_repo_literal "scripts/install_to_codex.py" '.rvf-pyroot'
# generated bundle 是唯一 vendoring 收口；不得再写官方宿主 cache。
require_repo_literal "tests/test_official_cli_developer_plugin_installation.py" 'test_build_release_is_self_contained_and_excludes_mutable_state'
require_file "scripts/_rvf_pyroot.py"
require_literal "scripts/_rvf_pyroot.py" '.rvf-pyroot'
require_literal "scripts/trajectory_distill.py" 'import _rvf_pyroot'
require_literal "scripts/trajectory_distill.py" 'from core.transcript'
require_literal "scripts/trajectory_distill.py" 'from adapters.codex.transcript import'
require_literal "scripts/trajectory_distill.py" 'from adapters.claude_code.transcript import'

# S9c: host 探测组合根（HOST_CODEX/HOST_CLAUDE/detect_transcript_format）从 trajectory_distill
# facade 上提到 core/host_adapter/——host 身份真相源住 core 注入契约包，不再住名为 "distill" 的脚本。
require_repo_file "core/host_adapter/host_transcript_format_detection.py"
require_repo_literal "core/host_adapter/host_transcript_format_detection.py" 'def detect_transcript_format'
require_repo_literal "core/host_adapter/host_transcript_format_detection.py" 'HOST_CODEX = "codex"'
require_repo_literal "core/host_adapter/host_transcript_format_detection.py" 'HOST_CLAUDE = "claude_code"'
require_literal "scripts/stop_review_validate_fix.py" 'from core.host_adapter.host_transcript_format_detection import'
# 真相源已迁走：trajectory_distill facade 不得再定义 host 探测原语（防回灌到旧位置）。
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_distill.py" 'def detect_transcript_format'

# S9（收尾）：HostAdapter 注入契约——core 业务逻辑唯一认识的 host 表面，S10 装配 concrete
# bundle + resolver 时注入。守住 v1 注入面 4 方法名 + 包级 re-export，防 S10 relocation 时漂移。
require_repo_file "core/host_adapter/host_adapter_protocol.py"
require_repo_literal "core/host_adapter/host_adapter_protocol.py" 'class HostAdapter(Protocol)'
require_repo_literal "core/host_adapter/host_adapter_protocol.py" 'def host_display_name'
require_repo_literal "core/host_adapter/host_adapter_protocol.py" 'def session_deep_link'
require_repo_literal "core/host_adapter/host_adapter_protocol.py" 'def cline_kanban_agent_id'
require_repo_literal "core/host_adapter/host_adapter_protocol.py" 'def detect_host_skip_mode'
require_repo_literal "core/host_adapter/host_adapter_protocol.py" 'def parent_transcript_quoted_session_name'
# 第 6 个方法此前漏钉（既有缺口）。它决定每个 host 拿哪条出口线材——漏钉意味着有人删掉它时
# 契约 gate 不会响，而后果是某个 host 在线上静默失声。
require_repo_literal "core/host_adapter/host_adapter_protocol.py" 'def stop_hook_agent_directive_channel'
# 既存缺口：该模块有 _self_check()（内含钉死方法集的 assert actual == expected），但此前
# 全仓**从不执行它**——gate 只钉字面量、测试里 0 引用、直接 `python3 <file>` 又因 core 不在
# sys.path 上而 ModuleNotFoundError。于是「加第 7 个方法会当场触发那条断言」这层保护实际
# 并不存在。用 env 补上 PYTHONPATH 真正跑它。
run_step "host adapter protocol self-check" \
  env PYTHONPATH="$repo_root" "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/core/host_adapter/host_adapter_protocol.py"
require_repo_literal "core/host_adapter/__init__.py" 'from core.host_adapter.host_adapter_protocol import HostAdapter'

# S10f: provider 健康探测中性内核迁 core/provider_health；codex 登录/二进制发现下沉
# adapters/codex/codex_provider_login。core runner 经 requirement 的 command 元组 +
# output_failure_predicate 保持 host-agnostic；codex 特异性（codex_bin / login status /
# auth-marker / login 拉起）全住 adapter，引擎只 import + 注入 predicate。
require_repo_file "core/provider_health/provider_health_guard.py"
require_repo_literal "core/provider_health/provider_health_guard.py" 'class ProviderHealthRequirement'
require_repo_literal "core/provider_health/provider_health_guard.py" 'output_failure_predicate'
require_repo_literal "core/provider_health/provider_health_guard.py" 'def run_provider_health_requirement'
require_repo_file "adapters/codex/codex_provider_login.py"
require_repo_literal "adapters/codex/codex_provider_login.py" 'def codex_bin'
require_repo_literal "adapters/codex/codex_provider_login.py" 'def maybe_start_codex_login'
require_repo_literal "adapters/codex/codex_provider_login.py" 'def codex_login_output_indicates_failure'
require_literal "scripts/stop_review_validate_fix.py" 'from core.provider_health.provider_health_guard import'
require_literal "scripts/stop_review_validate_fix.py" 'from adapters.codex.codex_provider_login import'
require_literal "scripts/stop_review_validate_fix.py" 'output_failure_predicate=codex_login_output_indicates_failure'
# S10f 反模式护栏：core runner 不得回退成按 provider == "codex" 判断输出健康（host-token 泄漏）；
# 真相源已迁走，引擎不得再定义这些 codex provider 原语（防回灌到旧位置）。
forbid_repo_literal "core/provider_health/provider_health_guard.py" 'provider == "codex"'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'def codex_bin'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'def maybe_start_codex_login'

# S10 装配（HostAdapter 注入面）：host_adapter_resolver 按 detect_transcript_format 装配
# Codex / Claude concrete bundle 注入 core——host 判断只在 resolver 一处。引擎
# default_cline_kanban_agent_id 经 bundle.cline_kanban_agent_id() 取 host-镜像默认值，
# 不再内联 host_kind 分支（中性 pin / 兜底仍留 resolve_cline_kanban_agent_id）。
require_repo_file "adapters/codex/codex_host_adapter.py"
require_repo_literal "adapters/codex/codex_host_adapter.py" 'class CodexHostAdapter'
require_repo_literal "adapters/codex/codex_host_adapter.py" 'codex_goal_mode_context_from_event'
require_repo_file "adapters/claude_code/claude_code_host_adapter.py"
require_repo_literal "adapters/claude_code/claude_code_host_adapter.py" 'class ClaudeCodeHostAdapter'
require_file "scripts/host_adapter_resolver.py"
require_literal "scripts/host_adapter_resolver.py" 'def resolve_host_adapter'
require_literal "scripts/stop_review_validate_fix.py" 'from host_adapter_resolver import resolve_host_adapter'
require_literal "scripts/stop_review_validate_fix.py" 'resolve_host_adapter(host).cline_kanban_agent_id()'
# resolver self-check：两 bundle 满足 HostAdapter Protocol、装配口径正确、各方法行为正确。
run_step "host adapter resolver self-check" "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$skill_dir/scripts/host_adapter_resolver.py"

# S10e 父会话来源冻结（PARENT_CONTEXT 簇 → core）：中性冻结核迁
# core/parent_conversation_context；引擎 parent_conversation_origin 退化为 assembly wrapper
# （预解析 host 元数据 + 注入 HostAdapter + 返回后注 host 归属键）。首条-user-message 摘录
# 下沉 CodexHostAdapter.parent_transcript_quoted_session_name（Codex-schema 抽取归 adapter，
# Claude 恒 None）。
require_repo_file "core/parent_conversation_context/parent_conversation_origin_freeze.py"
require_repo_literal "core/parent_conversation_context/parent_conversation_origin_freeze.py" 'def freeze_parent_conversation_origin'
require_repo_literal "core/parent_conversation_context/parent_conversation_origin_freeze.py" 'def source_origin_for_kanban_task'
require_repo_literal "core/parent_conversation_context/parent_conversation_origin_freeze.py" 'def parent_origin_summary_fields'
require_repo_literal "adapters/codex/codex_host_adapter.py" 'def parent_transcript_quoted_session_name'
require_repo_literal "adapters/claude_code/claude_code_host_adapter.py" 'def parent_transcript_quoted_session_name'
require_literal "scripts/stop_review_validate_fix.py" 'from core.parent_conversation_context.parent_conversation_origin_freeze import'
require_literal "scripts/stop_review_validate_fix.py" 'freeze_parent_conversation_origin('
# 反模式护栏：中性冻结核不得回灌 host 身份字面（前缀经 session_ref_prefix 注入、深链经
# host_adapter.session_deep_link；host 归属键由 wrapper 注入，core 内不得出现）。
forbid_repo_literal "core/parent_conversation_context/parent_conversation_origin_freeze.py" 'codex://local'

# S10g fork prompt 父会话来源块装配（PROMPT_ASSEM 簇 → core）：value_or_unavailable +
# parent_origin_prompt_block + add_parent_origin_to_rvf_fork_prompt 迁 core/review_prompt_assembly；
# 引擎留两个保签名 assembly wrapper（解析 HostAdapter.host_display_name() 后注入
# host_display_label）；旧 parent_conversation_host_label 折入 host_display_name() 注入、已删。
require_repo_file "core/review_prompt_assembly/review_validate_fix_fork_prompt.py"
require_repo_literal "core/review_prompt_assembly/review_validate_fix_fork_prompt.py" 'def parent_origin_prompt_block'
require_repo_literal "core/review_prompt_assembly/review_validate_fix_fork_prompt.py" 'def add_parent_origin_to_rvf_fork_prompt'
require_repo_literal "core/review_prompt_assembly/review_validate_fix_fork_prompt.py" 'host_display_label: str'
require_literal "scripts/stop_review_validate_fix.py" 'from core.review_prompt_assembly.review_validate_fix_fork_prompt import'
require_literal "scripts/stop_review_validate_fix.py" 'resolve_host_adapter(parent_origin.get("host_kind")).host_display_name()'
# 反模式护栏：装配核不得内联 host 判定（host 显示名经注入的 host_display_label 传入，
# core 内不得出现 host 身份常量）。
forbid_repo_literal "core/review_prompt_assembly/review_validate_fix_fork_prompt.py" 'HOST_CLAUDE'
forbid_repo_literal "core/review_prompt_assembly/review_validate_fix_fork_prompt.py" 'def parent_conversation_host_label'

# S10h-a review-cycle marker 迁 core（SESSION_ALLOC 依赖前置）：round_baseline_marker +
# review_highwater_marker 是 host-中性（stdlib + core.run_ledger，git 为唯一 subprocess）的
# review-cycle 标记模块，迁 core/session_scope_allocation/ 使 committed-round baseline 解析
# （S10h-b/c）可从 core self-contained 消费（否则 core→scripts 反向边）。
require_repo_literal "core/session_scope_allocation/round_baseline_marker.py" 'def resolve_round_baseline_head'
require_repo_literal "core/session_scope_allocation/review_highwater_marker.py" 'def resolve_review_highwater_head'

# S10h-b committed-round 窗口下界检测迁 core：4 helper（_commit_is_ancestor_of_head /
# _local_branch_exists / _resolve_committed_round_base_ref / _resolve_committed_round_baseline）
# + COMMITTED_ROUND_BASE_REF_ENV 迁 core/session_scope_allocation/committed_round_detection.py，
# 内部消费已迁 core 的 round_baseline_marker / review_highwater_marker（真实 marker 消费者从
# 引擎下移到本 core 模块）；引擎 refresh_global_diff_tracker / maybe_route_committed_round_scope
# 改从 core import 两 resolver。
require_repo_literal "core/session_scope_allocation/committed_round_detection.py" 'def _resolve_committed_round_baseline'
require_repo_literal "core/session_scope_allocation/committed_round_detection.py" 'def _resolve_committed_round_base_ref'
require_repo_literal "core/session_scope_allocation/committed_round_detection.py" 'from core.session_scope_allocation.round_baseline_marker import resolve_round_baseline_head'
require_repo_literal "core/session_scope_allocation/committed_round_detection.py" 'from core.session_scope_allocation import review_highwater_marker'
require_literal "scripts/stop_review_validate_fix.py" 'from core.session_scope_allocation.committed_round_detection import'

# S10h-c stop-session-scope gate 纯 helper 迁 core（精简迁）：9 个零引擎耦合的纯值类型/分类器/
# patch-ownership 完整性计算 + 常量 REASON_PATCH_OWNERSHIP_INCOMPLETE 迁
# core/session_scope_allocation/session_scope_gate_pure_helpers.py。gate/allocator 的 event-边界
# 解析器 + ledger 驱动主体本轮刻意留引擎。引擎 re-import 7 个仍用符号；3 个私有 helper
# （_patch_ownership_expected / _allocated_*_from_result）仅被同迁的 details 消费、引擎不 re-import
# 避死 import。forbid 断言引擎不再 def 这些（防迁走后重复 def / 回归）。
require_repo_literal "core/session_scope_allocation/session_scope_gate_pure_helpers.py" 'def patch_ownership_incomplete_details'
require_repo_literal "core/session_scope_allocation/session_scope_gate_pure_helpers.py" 'def session_change_type_from_manifest'
require_repo_literal "core/session_scope_allocation/session_scope_gate_pure_helpers.py" 'class _StopContextError'
require_repo_literal "core/session_scope_allocation/session_scope_gate_pure_helpers.py" 'class SessionScopePrecheck'
require_repo_literal "core/session_scope_allocation/session_scope_gate_pure_helpers.py" 'REASON_PATCH_OWNERSHIP_INCOMPLETE = "patch_ownership_incomplete"'
require_literal "scripts/stop_review_validate_fix.py" 'from core.session_scope_allocation.session_scope_gate_pure_helpers import'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'def patch_ownership_incomplete_details'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'def session_change_type_from_manifest'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'def _string_list'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'class _StopContextError'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'REASON_PATCH_OWNERSHIP_INCOMPLETE = "patch_ownership_incomplete"'

# S10i-a-0（DISPATCH 首刀 · 纯机械）：backend/launch-mode 选择的纯中性 stdlib-only leaf
# rvf_dispatch_flow.py 从 scripts 迁入 core/review_dispatch/review_validate_fix_dispatch_flow.py
# （verbatim 字节等价；引擎改 `import core.review_dispatch...as dispatch_flow`，所有
# dispatch_flow.X 调用点零改）。forbid 断言引擎不再引旧 scripts-level 模块。
require_repo_literal "core/review_dispatch/review_validate_fix_dispatch_flow.py" 'def backend_from_values'
require_repo_literal "core/review_dispatch/review_validate_fix_dispatch_flow.py" 'def should_attempt_legacy_gui_fallback'
require_literal "scripts/stop_review_validate_fix.py" 'import core.review_dispatch.review_validate_fix_dispatch_flow as dispatch_flow'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'import rvf_dispatch_flow as dispatch_flow'

# S10i-c-0（去-codex rename）：run_codex_fork（plan R-2 实证的跨后端 fork 枢纽误名）→ 中性
# run_review_validate_fix_child_fork（函数体不动，R-2）。forbid 锁死旧名，维持 sibling 切片一致性
# （S10i-a-0 forbid 旧 import、S10h-c forbid 旧 defs）——补 claude-code dogfood 指出的 gate-completeness 缺口。
require_literal "scripts/stop_review_validate_fix.py" 'def run_review_validate_fix_child_fork('
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_review_validate_fix.py" 'run_codex_fork'

# S10i-d-0（去-codex adapter 抽取 · tracker_dashboard codex-meta 族）：两个纯 Codex-schema 读取器
# —— rollout 文件名→session_id 索引（codex_transcript_index_by_session_id）+ session_meta 来源字段
# （read_codex_session_origin_fields）—— 从中性 tracker_dashboard.py 下沉 adapters/codex/transcript.py
# （read 版重建在既有 session_meta_from_path 上去重、复用 codex_sessions_root 加 $CODEX_HOME 支持）。
# dashboard 编排器 _enrich_sessions_with_codex_meta 留原处、改从 adapter import 委派。forbid 锁死旧 def/常量回归。
require_repo_literal "adapters/codex/transcript.py" 'def codex_transcript_index_by_session_id'
require_repo_literal "adapters/codex/transcript.py" 'def read_codex_session_origin_fields'
require_literal "scripts/tracker_dashboard.py" 'from adapters.codex.transcript import'
require_literal "scripts/tracker_dashboard.py" 'codex_transcript_index_by_session_id'
require_literal "scripts/tracker_dashboard.py" 'read_codex_session_origin_fields'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/tracker_dashboard.py" 'def _build_codex_transcript_index'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/tracker_dashboard.py" 'def _read_codex_session_meta'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/tracker_dashboard.py" '_CODEX_SESSIONS_DIR'

# S10i-d-1（去-codex singleton dedup · codex_bin）：rvf_analyze_thread 内与 adapter canonical 字节等价的
# codex_bin 副本（注释误引已下沉的 engine.codex_bin）删除，改 import adapters/codex/codex_provider_login.codex_bin
# （S10f home）。forbid 本地 def 回归、require import。
require_literal "scripts/rvf_analyze_thread.py" 'from adapters.codex.codex_provider_login import codex_bin'
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_analyze_thread.py" 'def codex_bin'

# S1.5: 分析/归因层 host 归一（A1 write-op 计数 + C same-session-full 子区间窗口）。
require_literal "scripts/analysis_artifacts.py" 'def _is_write_op'
require_literal "scripts/analysis_artifacts.py" 'def _rvf_window_start'
require_literal "scripts/analysis_artifacts.py" 'trajectory_window_start'
# A1 反模式护栏：write-op 计数不得回退成按 host 工具名 == "apply_patch" 判断。
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/analysis_artifacts.py" '== "apply_patch"'
require_repo_literal "tests/test_analysis_artifacts.py" 'test_write_op_count_normalizes_claude_edit_write_multiedit'
require_repo_literal "tests/test_analysis_artifacts.py" 'test_same_session_full_windows_to_rvf_subinterval'

# S2 / handoff A2：子代理捕获 host 归一（Codex glob → adapter；Claude Task 子代理发现）。
require_repo_file "core/subagents/models.py"
require_repo_file "adapters/codex/subagent.py"
require_repo_file "adapters/claude_code/subagent.py"
require_repo_file "tests/test_claude_subagent_capture.py"
require_repo_literal "core/subagents/models.py" 'class SpawnRecord'
require_repo_literal "adapters/codex/subagent.py" 'def codex_sessions_root'
require_repo_literal "adapters/codex/subagent.py" 'def resolve_subagents'
require_repo_literal "adapters/claude_code/subagent.py" 'def resolve_subagents'
require_repo_literal "adapters/claude_code/subagent.py" 'agent-*.jsonl'
require_literal "scripts/subagent_capture.py" 'host_kind'
require_literal "scripts/subagent_capture.py" 'original_transcript'
require_literal "scripts/trajectory_capture.py" 'original_transcript=current_transcript'
# A2 反模式护栏：Codex sessions 布局已移入 adapter，facade 不得再写死该路径。
forbid_repo_literal "plugins/review-validate-fix/skills/review-validate-fix/scripts/subagent_capture.py" '.codex/sessions'
require_repo_literal "tests/test_claude_subagent_capture.py" 'test_capture_all_subagents_claude_discovers_and_distills'

# S2 / handoff B：candidate_patch_call_ids 由子代理 write-op call_id 只读补全。
require_literal "scripts/analysis_artifacts.py" 'def _enrich_candidate_patch_call_ids'
require_repo_literal "tests/test_analysis_artifacts.py" 'test_enrich_candidate_patch_call_ids_links_event_path_to_subagent_call_id'

# S2-invoke：headless 子代理调用向量 host 分派下沉 adapter（observe-side 对称）。
require_repo_file "tests/test_invoke_subagent.py"
require_repo_literal "core/subagents/models.py" 'class InvokeCommand'
require_repo_literal "adapters/codex/subagent.py" 'def build_analyze_command'
require_repo_literal "adapters/claude_code/subagent.py" 'def build_analyze_command'
require_literal "scripts/rvf_analyze_thread.py" '_codex_build_analyze_command'
require_literal "scripts/rvf_analyze_thread.py" '_claude_build_analyze_command'
require_repo_literal "tests/test_invoke_subagent.py" 'test_facade_dispatches_by_host_and_preserves_tuple_shape'

# S3 / handoff G：Claude hook 双入口共享单一 host-ownership 契约（守卫单源化）。
require_repo_file "plugins/review-validate-fix/hooks/_claude_hook_entry.py"
require_repo_literal "plugins/review-validate-fix/hooks/_claude_hook_entry.py" 'def is_foreign_invocation'
require_repo_literal "plugins/review-validate-fix/hooks/_claude_hook_entry.py" 'def run_claude_hook'
require_repo_literal "plugins/review-validate-fix/hooks/stop.py" 'from _claude_hook_entry import run_claude_hook'
require_repo_literal "plugins/review-validate-fix/hooks/user_prompt_submit.py" 'from _claude_hook_entry import run_claude_hook'
# G 守卫单源化护栏：两入口不得再各持一份复制的 _is_codex_invocation。
forbid_repo_literal "plugins/review-validate-fix/hooks/stop.py" '_is_codex_invocation'
forbid_repo_literal "plugins/review-validate-fix/hooks/user_prompt_submit.py" '_is_codex_invocation'
require_repo_literal "tests/test_review_support_scripts.py" 'test_claude_hook_entry_detects_foreign_invocation'
require_repo_literal "tests/test_review_support_scripts.py" 'test_claude_hook_entry_dispatches_claude_invocation'

# S4：跨 harness 兼容性矩阵 + manifest sync 工具 + 指南 marketplace 变体注。
require_repo_file "scripts/sync-manifest.sh"
require_repo_file "docs/architecture/cross-harness.md"
# sync-manifest 守护的不变量名/version/source 与"description 不强校验相等"的设计取舍。
require_repo_literal "scripts/sync-manifest.sh" '.plugins[0].source // ""'
require_repo_literal "scripts/sync-manifest.sh" 'plugin id 漂移'
require_repo_literal "scripts/sync-manifest.sh" 'version 漂移'

# Kimi manifest 漏声明 skills 是零报错状态（plugin state 仍是 ok、hook 照常触发、6 个 skill
# 全部不存在）。Kimi 只从 manifest.skills 发现 skill，不扫 skills/ 目录——对照 agents 有目录
# 兜底、skills 没有。2026-08-26 实盘命中过一次，故把「三份 host manifest 都声明且一致」钉死。
require_repo_literal "scripts/sync-manifest.sh" 'skills 声明漂移'
require_repo_literal "plugins/review-validate-fix/.kimi-plugin/plugin.json" '"./skills/"'
# 注册记录写错 source/github 会让 Kimi 0.38.0 的 /plugins 面板整个打不开，而 hook 仍在跑
# （enabledHooks 不过那道出站校验）⇒ 必须是独立一态，不能折进 not_registered。
require_repo_literal "scripts/kimi_plugin_manifest_activation_state_check.py" \
  'registry_record_rejected_by_host_schema'
require_repo_literal "scripts/install_to_codex.py" \
  'KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KIND_LOCAL_PATH = "local-path"'
require_repo_literal "docs/architecture/cross-harness.md" 'Adapter-backed'
require_repo_literal "docs/architecture/cross-harness.md" '(M+N)'
# README 与指南 06 须链入 as-built 矩阵、并对齐脱离反模式 ② 的现状。
require_repo_literal "README.md" 'docs/architecture/cross-harness.md'
require_repo_literal "README.md" '跨 harness 支持'
require_repo_literal "docs/multi-harness-plugin-guideline/06-rvf-application.md" 'Pattern A 的 marketplace 变体'
require_repo_literal "docs/multi-harness-plugin-guideline/06-rvf-application.md" '已对齐 S0 v2'
# 反模式 ② 已脱离：04 不得再宣称本仓库"当前正是该反模式的样本"。
forbid_repo_literal "docs/multi-harness-plugin-guideline/04-anti-patterns.md" '本仓库当前正是该反模式的样本'

# S8.0（去-codex 重构）：env-namespace 碰撞 gate。防 CODEX_RVF_* → RVF_* 改名半完成
# ——同一 base 在 CODEX_RVF_ 与裸 RVF_ 两命名空间都被 env 访问 = 运行时变量分裂、
# 静默损坏（无语法/import 错，未覆盖路径假绿）。ALLOWLIST 在脚本内随坍缩切片收窄。
"$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/scripts/check_env_namespace_collision.py"

# 必须是最后一个测试相关步骤：它核对的是「上面所有测试跑完之后」真实 RVF 状态根的样子。
run_step "real RVF state roots hold no artifacts from this contract suite" \
  "$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" "$repo_root/scripts/check_contract_suite_left_no_test_artifacts_in_real_rvf_state_roots.py" \
    --suite-private-temporary-directory "$contract_suite_private_temporary_directory" \
    --suite-started-at-epoch-seconds "$contract_suite_started_at_epoch_seconds"

if [ "$verbose" -eq 1 ]; then
  printf 'contract check OK\n'
  printf 'hashes:\n'
  for file in "${required_files[@]}"; do
    printf '%s  %s\n' "$(hash_file "$skill_dir/$file")" "$file"
  done
else
  printf '契约检查通过\n'
fi
