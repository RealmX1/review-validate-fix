#!/usr/bin/env python3
"""只读回测：假如分层归属（08ac4 实现）当时就已部署，历史上 924 条漏审里每一条会被哪一层拦下。

输入是 08ac4 统计脚本的输出（``per-run-and-per-missed-path-statistics.json`` 的 ``missed_path_rows``）。每条漏审路径按
下面的顺序归到**第一个**能拦下它的层；这是上界估计，局限如下：

- **命令文本层的数字有循环性。** 漏审本身就是用同一套命令文本识别找出来的，所以它反映不了 ``python run.py``
  这类不透明命令的真实召回。新实现比统计脚本收得更紧：
  - 去掉了「内联脚本正文里提到路径」这条启发式；
  - ``git restore`` / ``checkout --`` 不再算作者证据。
  这两类在这里不记到命令文本层，改由后面的层兜。
- **单任务默认认领层拿不到历史文件的改动时刻**，排除不了「另一会话同时在写」的情况。可行性实测给的参照：本会话
  可能写入调用的时长里，只有 0.06% 与同 worktree 别的会话的可能写入调用重叠。所以这一层按上界计。
- 子代理写入：统计脚本把「只经子代理写入」的路径记为 ``subagent_only``。凡不是由命令文本推出的，就是子代理的
  Edit/Write，归第 1 层（强证据）。

输出写到 ``--output-directory``（默认 Kanban 交接目录，持久）。
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

DEFAULT_STATISTICS_INPUT_PATH = Path(
    "/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/"
    "from-task-08ac4-rvf-session-owned-attribution-coverage-gap-history-statistics-output-2026-10-04-revised-after-rvf-review/"
    "per-run-and-per-missed-path-statistics.json"
)
DEFAULT_OUTPUT_DIRECTORY = Path(
    "/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/"
    "from-task-08ac4-layered-session-write-attribution-backtest-2026-10-05"
)
SHELL_WRITE_KINDS_STILL_RECOGNIZED_BY_NEW_COMMAND_TEXT_LAYER = {
    "inline_python_or_node_script_write_call_literal_destination",
    "formatter_or_linter_write_mode_file_argument",
    "shell_redirection_overwrite_or_append_target",
    "sed_or_perl_in_place_edit_file_argument",
    "remove_or_touch_or_truncate_file_argument",
    "copy_or_install_command_destination",
    "move_command_source_or_destination",
    "tee_command_file_argument",
    "patch_text_file_header_in_heredoc_or_argument",
    "download_output_file_argument",
    "copy_recursive_directory_destination_prefix",
}
GIT_RESTORE_OR_CHECKOUT_COMMAND_MARKERS = ("git restore", "git checkout")


def catching_layer_for_missed_path(row: dict[str, Any]) -> str:
    if row.get("missed_cause") == "edit_write_before_head_commit_time_cutoff":
        return "head_commit_time_truncation_fix"
    if row.get("write_origin") == "subagent_only" and not row.get("write_evidence_derived_only_from_shell_command_parsing"):
        return "layer_1_subagent_file_write_tool_call"
    trigger_kind = row.get("shell_write_trigger_kind")
    excerpt = str(row.get("shell_write_trigger_command_excerpt") or "")
    if trigger_kind == "git_rm_mv_restore_checkout_file_argument" and not any(
        marker in excerpt for marker in GIT_RESTORE_OR_CHECKOUT_COMMAND_MARKERS
    ):
        return "layer_2_shell_command_named_write_target"
    if trigger_kind in SHELL_WRITE_KINDS_STILL_RECOGNIZED_BY_NEW_COMMAND_TEXT_LAYER:
        return "layer_2_shell_command_named_write_target"
    if "/.cline/worktrees/" in str(row.get("repository") or ""):
        return "layer_4_kanban_single_task_worktree_default_attribution_upper_bound"
    return "left_for_agent_explicit_claim_and_coverage_gate"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--statistics-input", type=Path, default=DEFAULT_STATISTICS_INPUT_PATH)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    arguments = parser.parse_args()
    statistics = json.loads(arguments.statistics_input.read_text(encoding="utf-8"))
    rows = statistics["missed_path_rows"]
    per_row = [{"run_id": row["run_id"], "path": row["path"], "layer": catching_layer_for_missed_path(row)} for row in rows]
    by_layer = collections.Counter(item["layer"] for item in per_row)
    runs_with_any_left = {item["run_id"] for item in per_row if item["layer"] == "left_for_agent_explicit_claim_and_coverage_gate"}
    caught = sum(count for layer, count in by_layer.items() if layer != "left_for_agent_explicit_claim_and_coverage_gate")
    result = {
        "missed_path_count": len(rows),
        "missed_paths_caught_by_some_layer_upper_bound": caught,
        "missed_paths_by_first_catching_layer": dict(by_layer.most_common()),
        "runs_still_needing_agent_claim_or_accept": len(runs_with_any_left),
        "per_missed_path": per_row,
    }
    arguments.output_directory.mkdir(parents=True, exist_ok=True)
    output_path = arguments.output_directory / "layered-session-write-attribution-backtest.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "per_missed_path"}, ensure_ascii=False, indent=2))
    print(f"written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
