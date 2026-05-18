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

1. **rollout 文件名已 host-中性化（2026-05-19 cleanup commit 完成）** ——
   产物文件统一为 `rollout.jsonl` / `rollout.manifest.json`（原
   `rollout.codex.jsonl` / `rollout.codex.manifest.json`）；Codex / Claude
   共用同名，host 区分仍由 `manifest.host` 字段表达。`subagent_capture.py`
   等下游 reader 与全部测试 fixture / 文档引用已同步。
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

---

## Phase 4 Outcome（2026-05-18 · child_session_id wiring，Option C）

原 plan 把 Phase 4 列为 deferred，前置依赖写作 path (a) cline-kanban schema 暴露
`claudeSessionId` / path (b) 新建 `install_to_claude.py`。复核 `b94c7d6` 实代码后
该前提**已过期**：`scripts/install_to_codex.py` 早已含完整 Claude plugin 安装管线
（`update_claude_settings` / `sync_claude_marketplace_metadata` /
`update_claude_installed_plugins`），真实缺口仅是 **Claude 端未注册
UserPromptSubmit hook**。Claude 与 Codex 是两套不同 hook 投递机制：Codex 走
`~/.codex/hooks.json`（安装器写入），Claude 走 plugin 自带的
`plugins/review-validate-fix/hooks/hooks.json`（marketplace 同步时由
`install_to_codex.py` 的 `copytree` 一并携带；现存 Claude Stop hook 正是走这条路）。

故落地走 **Option C（plugin hooks 清单，零安装器 / 零 settings.json 改动）**：

- `hooks/hooks.json` 增 `UserPromptSubmit` 块（`timeout: 90`）；新增
  `hooks/user_prompt_submit.py` shim（仿 `stop.py`：读 stdin event、补
  source/cwd 默认、subprocess delegate 到 `rvf_user_prompt_submit.py`、
  fail-open；超时 env `CLAUDE_RVF_USER_PROMPT_HOOK_TIMEOUT` 默认 85）。
- `rvf_user_prompt_submit.py`：token 路径新增 `_backfill_child_session()`。
  guard = 当前 `event.session_id` 存在且 ≠ `prep.origin_session_id`
  （same-session manual / followup 不受影响、行为不变）。回填
  `child_session_id` / `child_transcript_path` 进 ① prep payload
  （`update_prep_file`，ledger trail + 幂等）② **持久 `origin.json`**
  （`origin_metadata_path` 或 `rvf_run.run_dir`/artifacts/origin.json）。
  选 origin.json 而非 prep 作功能通道：prep TTL 仅 300s，capture 时
  （task agent 跑完 RVF，常 >5min）prep 已被 `sweep_stale` 清掉。
- `trajectory_capture.capture_run`：`_read_origin` 后若 origin.json 带
  `child_session_id`/`child_transcript_path`、child≠`origin.session_id`、
  child transcript 文件存在 → override `current_transcript`=child、
  `event_session_id`=child_session_id → 既有 `forked` 判定自然为真 →
  复用既有 forked 分支（pre=parent Codex 全量、post=child Claude 全量、
  `post_host_kind` 自动探测=claude_code）。零新分支；docstring 已从
  「已知未覆盖」改写为「Cline Kanban dispatch 覆盖」。
- `rvf_prep_file.py` **未改**：payload 自由 dict，`update_prep_file` 任意
  合并，新键不在 `PROTECTED_UPDATE_FIELDS`，无 schema 强校验。

### 已知限制 / 后续

- Cline Kanban task agent 必须以装好 RVF marketplace plugin 的 Claude Code
  运行（与现存 Stop hook 同前提）；未装则 UserPromptSubmit hook 不触发，
  capture 退回原行为（parent transcript），无回归但 child 轨迹缺失。
- live flow-2 联调（真跑 dirty repo → 父 Stop hook → Cline Kanban task →
  task agent RVF 全链路验证 child 轨迹非空）仍未做，列为下一队列项。
- ~~rollout 文件名改名 cleanup~~ 已完成（2026-05-19 独立 commit：
  `rollout.codex.jsonl` → `rollout.jsonl`，`rollout.codex.manifest.json`
  → `rollout.manifest.json`）。
- **Codex/Claude UserPromptSubmit timeout 不对称（既有，非本变更引入）**：
  Codex 侧 `install_to_codex.py::configure_user_prompt_submit_hook` 写入
  `~/.codex/hooks.json` 的 `"timeout": 5`，而 Claude 侧本变更给 90s（shim
  85s / shared prepare 60s）。Slice I 让 UserPromptSubmit 承担同步 prepare
  后，Codex 5s 在 in-process prepare（最长 60s）期间会被 harness 杀掉——
  Phase 4 让 Claude 侧 UserPromptSubmit 承担实质 backfill 工作，放大了该不
  对称的运维困惑面。本变更未改动 Codex 路径；Codex 侧 timeout 评估属 Slice I
  残留议题，建议后续单独处理。

### 改动文件

`plugins/review-validate-fix/hooks/hooks.json`、
`plugins/review-validate-fix/hooks/user_prompt_submit.py`（新）、
`plugins/.../scripts/rvf_user_prompt_submit.py`、
`plugins/.../scripts/trajectory_capture.py`（+docstring）、
`tests/test_review_support_scripts.py`、
`tests/test_trajectory_capture_claude_dispatch.py`。

### 验证（2026-05-18，全绿）

```sh
python3 -m py_compile \
  plugins/.../scripts/{trajectory_capture,trajectory_distill,codex_stop_review_validate_fix,rvf_user_prompt_submit,rvf_prep_file}.py \
  plugins/review-validate-fix/hooks/{user_prompt_submit,stop}.py
/opt/homebrew/bin/python3 -m pytest -q \
  tests/test_trajectory_capture_claude_dispatch.py \
  tests/test_trajectory_distill_claude.py \
  tests/test_trajectory_distill.py tests/test_trajectory_split.py   # 40 passed
python3 tests/test_review_support_scripts.py        # incl. 2 new cases
python3 tests/test_codex_stop_review_validate_fix.py
python3 tests/test_codex_stop_hook_dispatcher.py
bash scripts/check_skill_contracts.sh
python3 scripts/check_plugin_contracts.py
git diff --check
```
未 commit，等用户 `/review-validate-fix` 自审后再决定提交。
