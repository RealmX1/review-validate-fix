# Multi-harness reviewer 路由 + 派发实现日志（S1–S6）

日期：2026-06-21

设计稿：`docs/rvf-multi-harness-reviewer-routing-plan.md`（顶部含 8 个 open-question 实现决议批注）。
落地 commit：`338b768`（feat）+ 本日志 commit。Kanban worktree：`/Users/bominzhang/.cline/worktrees/fee43/review-validate-fix`。

## 目标

把 RVF santa-method 双 review 的派发从「主会话 + 单文件 `config/alternative-reviewer.json`」升级为
「registry + 路由脚本 + plan artifact + 并行执行器」：默认恒派**两路 external CLI reviewer**
（cursor + 非主 dispatch harness），移除 `codex-reviewer` in-harness 默认第一腿，禁止
「1 external + 1 主 harness in-harness subagent」混合派发。

## 触发起因（HEAD 契约已红）

`0ba4a1d`（"enable cursor-agent as active alternative reviewer"）把 active config 换成 cursor，
但留下三处仍断言旧 claude 字面量失同步 → `bash scripts/check_skill_contracts.sh` 退出 `rc=1`
（`契约缺失: config/alternative-reviewer.json 中找不到 alternative-reviewer:claude-code`）。
恢复契约绿是首要回归判据。

## 实现切片

- **S1 Config + registry 基座**：从 `0ba4a1d~1` 恢复 Claude 配置为 `config/alternative-reviewer.claude.json`；
  新增 `config/reviewer-registry.json`（cursor/claude_code/codex 三 harness，`harness_id → label_prefix /
  config_path / dispatch_mode / enabled / priority_default`）；删除漂移源 `alternative-reviewer.json`；
  `run_alternative_reviewer.py` 的 `DEFAULT_CONFIG` 重指 `alternative-reviewer.cursor.json`（standalone 行为不变）；
  `install_to_codex.py` 本机保留集 `alternative-reviewer.json → reviewer-registry.json`（三份 per-harness 模板随仓库同步）。
- **S2 路由 + plan artifact**：新增 `scripts/dispatch_reviewers.py`，纯函数 `route()` 实现 R0–R4：
  - R0（|A|≥3）两路非主 external，cursor 必选一腿；R1（|A|==2）这两路 external（M∈A 也以 external 跑，不退 in-harness）；
    R2（|A|==1）同 harness 双实例（reviewer_id `-a`/`-b`）；R3（|A|==0）`needs_last_resort_fallback`；
    R4 cursor 缺席记 `cursor_unavailable`（仅 |A|≥2）。
  - 结构化 warnings（`available_reviewer_harness_mismatch` / `only_main_harness_available` /
    `cursor_unavailable` / `no_external_reviewer_available` / `collision_risk`）；`--plan-only` 写
    `artifacts/reviewers/reviewer-plan.json`。
- **S3 并行执行 + 内核 `--reviewer-id`**：`run_alternative_reviewer.py` 新增 `--reviewer-id`（默认仍 `reviewer_id_from_label`，
  向后兼容），R2 同 harness 双实例据此不撞 `artifacts/reviewers/<id>/`；`dispatch_reviewers.py --execute` 并行复用
  执行内核。并行 spike 由设计判定：三模板均 headless ephemeral/no-persistence（`claude --no-session-persistence`、
  `codex --ephemeral`、`cursor-agent -p`），默认并行安全；保留 `SEQUENTIAL_HARNESSES`+`sequential_execution` 机制备用。
- **S4 主 harness 解析**：`prepare_review_run.py` 解析主 harness（复用 `detect_transcript_format`，cursor 永不被探测命中）、
  写 `artifacts/inputs/main-harness.json`、`review_env_exports` 导出 `RVF_MAIN_HARNESS`。
- **S5 文档/契约/测试**：重写 `review-merge-policy.md` / `SKILL.md §Review` / `handoff-template.md` /
  `mcp-setup-startup.md` / `README.md`；`check_skill_contracts.sh` 字面量同步到 registry + 三 config + dispatch；
  新增 4 个 dispatch 测试并注册进 `review_support_test_cases()`（规避未注册静默不跑陷阱）。
- **S6 移除 legacy 默认 + 外置最后兜底**：移除 `codex-reviewer` 默认第一腿（含 `forbid_literal codex-reviewer` 禁止回归）；
  新增 `references/zero-external-reviewer-last-resort-in-harness-fallback.md`，承载 in-harness mimic 绝对最后兜底
  （R3，`codex-mimic-reviewer-a/b`，与 external 同契约），merge-policy / mcp-setup 仅留指针。

## 8 个 open-question 决议（详见设计稿顶部批注）

主=Cursor 不做自动探测（仅显式覆盖，Q3）；`codex-reviewer` 完全移除不留 env 开关（Q2）；
`|A|==1 且 only!=M` warning 后仍双 external 不 fail-close；并行默认安全；registry 独立文件且 install 保留（Q4）；
R3 默认走 mimic 兜底、仅 `--require-external` fail-close；source label 统一 `alternative-reviewer:<harness-id>`。

## rvf-land sanity-check（RVF run rvf-20260621T071851Z-stop-hook-c6813f7a）

两路 external（R0 cursor+codex，由本轮被审的新 `dispatch_reviewers.py` 自己派发）共报 3 项 medium、逐条对照源码确认全 REAL，就地修复 3/3（0 误报 0 升级）：

1. **dispatch 不回填 review-env.sh 的 repo/packet/scope**：SKILL 契约是 `source review-env.sh` 后只 `--execute`，
   但 `dispatch_reviewers.py` 原本只认显式 CLI 参数 → 子进程 reviewer 缺 `--repo/--review-packet` 失败。
   修复：`repo/review_packet/session_context/scope_contract` 缺省时从 `RVF_REPO` / `RVF_REVIEW_PACKET` /
   `RVF_SCOPE_OF_WORK`(/`RVF_SESSION_CONTEXT`) / `RVF_SCOPE_CONTRACT` 回填。
2. **prepare 把显式 `RVF_MAIN_HARNESS=cursor` 覆盖回 codex**：原本无条件用 transcript 探测结果。
   修复：honor 显式 env（`VALID_MAIN_HARNESSES` 守卫），优先级 env-override > transcript > 默认 codex。
3. **install_to_codex 残留 5 处已删除 `alternative-reviewer.json` 引用**：清理干净（现 grep=0）。
   并新增回归测试 `test_dispatch_reviewers_execute_backfills_review_env`（已注册）。

## 验证

- `bash scripts/check_skill_contracts.sh` → **rc=0**（开工时 rc=1，首要回归判据闭合）；`check_plugin_contracts.py` → rc=0。
- 5 个 dispatch 用例（路由矩阵 R0–R4 / 同 harness 双实例 distinct id / plan schema / 并行双 external / env-backfill 回归）全绿。
- `tests/test_install_to_codex.py` 24 passed。
- 真机 `dispatch_reviewers.py --plan-only` 实 probe（三 CLI 全可用）→ R0 cursor+claude_code，无 warning。
- GitNexus impact（DEFAULT_CONFIG / main / review_env_exports / prepare_run）全 LOW；`detect_changes` 报 CRITICAL
  系 `install_to_codex.py:main` 部署入口 hub 天然扇出，所有变更 symbol 均在本 task 11 文件内、非越界。

## 回灌

`base-branch-sync`：本地 main（`0ba4a1d`）是 task commit 的严格祖先 → fast-forward。base worktree
（`/Users/bominzhang/Documents/GitHub/review-validate-fix`）原有一个未跟踪的 `docs/rvf-multi-harness-reviewer-routing-plan.md`
（PLAN-READY 原稿，被本 commit 的 tracked 超集版本取代）；经用户确认「删除原稿后 ff」（已 /tmp 备份），ff main。

## 未引入 backward-compat shim

`--reviewer-id` / `main_harness` kwarg 均为带默认值的前向新增；删文件 + DEFAULT_CONFIG repoint 是干净迁移，
无 backward-compat 层，故无需 `dev_backward_compatibility/` 记录。
