# Trajectory Capture · Claude Host 支持 + Pre-RVF origin.json 兜底

> 关联 plan：`/Users/bominzhang/.claude/plans/trajectory-capture-claude-host-fix.md`
> 关联前置：`docs/log/2026-05-09-rvf-post-user-prompt-shared-workflow-handoff.md`
> 触发上下文：RVF run `rvf-20260509T121926Z-stop-hook-ac9f5550` 复盘发现
> `trajectory.jsonl` pre/post 双端 0 records；issue→patch 关联无法建立。

## 改动范围

本轮落地 Plan **Phase 0–3 + 5 + 6 + 7**。**Phase 4 (child_session_id wiring)
deferred** 给独立 follow-up slice，原因：cline-kanban CLI / `RuntimeTaskSessionSummary`
schema 不暴露 Claude session_id；自回填路径需要 `install_to_claude.py`（当前
plugin 只装 Codex）。

### Code

| 文件 | 改动 |
|---|---|
| `plugins/.../scripts/trajectory_distill.py` | 新增 `detect_transcript_format`、`_distill_claude_record`、`distill_claude_jsonl`、`_claude_tool_call_artifact_refs`、`_claude_message_text_blocks`、`_extract_apply_patch_from_bash`；常量拆分 `HOST_CODEX` / `HOST_CLAUDE`；`HOST_KIND = HOST_CODEX` 留作向后兼容别名；docstring 改为 host-aware 描述。 |
| `plugins/.../scripts/trajectory_capture.py` | 新增 `_claude_user_message_text` / `find_rvf_start_in_claude_jsonl`；`_host_meta` 接受 `host_kind` 参数；`_write_pre_slice` / `_write_full_copy` / `_write_post_slice` 接受 `host_kind` 透传；`capture_run` 入口探测 transcript host 并分派到对应 marker finder + distiller；docstring 改为双 host 描述。 |
| `plugins/.../scripts/codex_stop_review_validate_fix.py` | 新增 `parent_thread_path_for_origin(event, *, ledger, repo, cwd)`：在 Codex `session_meta` 校验失败时退到任何存在的 `event_session_paths`，让 Claude transcript 也能被 origin.json 收录；emit `origin_metadata_transcript_path_fallback` / `origin_metadata_missing_transcript_path` ledger event；替换 `fork_review_validate_fix` 与 `launch_backend` kanban-followup 分支的 `parent_thread_path_from_event` 调用。 |

### Tests

新增：
- `tests/test_trajectory_distill_claude.py`（13 tests）—— format detection /
  `_claude_user_message_text` / `find_rvf_start_in_claude_jsonl` /
  `distill_claude_jsonl` 各 record kind / Bash apply_patch artifact_refs /
  unknown record types 跳过。
- `tests/test_trajectory_capture_claude_dispatch.py`（4 tests）——
  same-session-slice / same-session-full / forked mixed-host / `_host_meta(None)`
  默认 codex。

修改 `tests/test_codex_stop_review_validate_fix.py`：追加 3 tests 覆盖
`parent_thread_path_for_origin` 三条分支（Codex 命中、Claude fallback、空 event 诊断）。

既有 `tests/test_trajectory_split.py` / `tests/test_trajectory_distill.py` /
`tests/test_subagent_capture.py` / `tests/test_rvf_run_finalize.py` 全部保持
通过；未改 host 字面量断言（`HOST_CODEX = "codex"` 仍向后兼容）。

## 兼容性说明

1. **rollout 文件名沿用 `rollout.codex.jsonl`** —— 即便实际是 Claude
   transcript。这是临时折衷：`subagent_capture.py` 等下游 reader 假定该
   文件名；schema host 通过 `manifest.host` 字段表达。**TODO**（plan 已记录）：
   未来单独 cleanup commit 改名为 `rollout.host.jsonl` 或 `rollout.jsonl`，
   并同步所有 reader / 测试 fixture / 文档引用。
2. **`HOST_KIND` 兼容别名**：保留 `HOST_KIND = HOST_CODEX` 直到所有 import
   点改完。新代码请使用 `HOST_CODEX` / `HOST_CLAUDE`。
3. **Claude `host_originator` 始终为 None** —— Claude transcript 没有 Codex
   `session_meta.payload.originator` 等价字段，留 None 即可。
4. **`HOST_KIND` 单元测试** (`test_host_kind_constant_is_codex`) 仍验证别名值
   `"codex"` —— 别名删除时同步删测试。

## 已知限制 / Deferred

1. **Cline Kanban dispatch 场景 child transcript 无法定位** —— stop hook event
   给的是 parent Codex transcript，但实际想看的是 task agent 的 Claude
   transcript。需 prep file 显式回填 `child_session_id`。前置依赖（二选一或并行）：
   - (a) cline-kanban 仓 patch `RuntimeTaskSessionSummary` 加 `claudeSessionId`
     字段；
   - (b) RVF plugin 加 `install_to_claude.py`，task agent 通过 UserPromptSubmit
     hook 自回填。
2. **Claude `Edit` / `Write` / `NotebookEdit` artifact_refs.lines 为 None** ——
   Claude 工具 input 不含行号；causality 分析仅按 path 关联，无精确行号。
   未来若需要可读当前文件内容反推行号。
3. **`detect_transcript_format` fallback 行为** —— 文件无效 / 异常 schema /
   只含 `permission-mode` 等中性 record → 返回 None；调用方 fallback 到
   `HOST_CODEX` 保证既有 Codex-only 用例无回归。

## 验证

```sh
python3 -m py_compile \
  plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_capture.py \
  plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_distill.py \
  plugins/review-validate-fix/skills/review-validate-fix/scripts/codex_stop_review_validate_fix.py
python3 -m pytest \
  tests/test_trajectory_distill_claude.py \
  tests/test_trajectory_capture_claude_dispatch.py \
  tests/test_trajectory_split.py \
  tests/test_trajectory_distill.py \
  tests/test_subagent_capture.py \
  tests/test_rvf_run_finalize.py
python3 -m pytest \
  tests/test_codex_stop_review_validate_fix.py::test_parent_thread_path_for_origin_returns_codex_validated_path \
  tests/test_codex_stop_review_validate_fix.py::test_parent_thread_path_for_origin_falls_back_to_existing_file \
  tests/test_codex_stop_review_validate_fix.py::test_parent_thread_path_for_origin_emits_diagnostic_when_event_empty
bash scripts/check_skill_contracts.sh
python3 scripts/check_plugin_contracts.py
git diff --check
```
