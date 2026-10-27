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

cd "$repo_root"
"$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -B scripts/check_plugin_contracts.py
bash scripts/sync-manifest.sh
"$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest -q tests
