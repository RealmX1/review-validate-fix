# RVF 跨 harness 插件改造 · S0–S4 实施日志

> 关联 plan（已转为本日志、原本体留存供考古）：`~/.claude/plans/hazy-shimmying-puppy.md`（"RVF 改造为跨 harness 插件（M+N Marketplace+Nested）：S0–S4（含 S1.5）完整 plan · v3"）
> 关联设计体系：[`docs/workflow-plugin-design-system.md`](../workflow-plugin-design-system.md)
> 关联架构：[`docs/architecture/cross-harness.md`](../architecture/cross-harness.md)
> 关联前置 handoff：`docs/log/2026-05-28-claude-code-cross-harness-adaptation-handoff.md`（缺口复盘来源）、[`docs/log/2026-05-10-trajectory-capture-claude-host-support.md`](2026-05-10-trajectory-capture-claude-host-support.md)
> 工作分支：`feat/cross-harness-plugin`

本日志记录把 review-validate-fix（RVF）从「Codex-原生」改造为「跨 harness 插件」的 S0–S4 全部切片**实际落地形态**——每个切片做了什么、commit、关键决策与 audit 修正、闭合的缺口。它是 as-built 记录（不是 plan 的逐行复制）；plan 的 v1/v2/v3 revision 考古、`适应预期`、风险推演留在原 plan 文件。

---

## 1. 起点与目标

**问题**：RVF 的触发面、transcript 解析、子代理捕获/调用、分析归因层都直接消费 Codex 工具名（`apply_patch`/`spawn_agent`）与 `~/.codex/` 布局，对 Claude-host run 系统性漏算；插件 manifest 还有 plugin-id 漂移（Codex 侧叫 `rvf`）、缺源仓库 marketplace.json。

**目标**：按 `docs/multi-harness-plugin-guideline/` 的 6 维 adapter 契约，把 host-specific 逻辑下沉到 `adapters/{codex,claude_code}/`、host-agnostic 模型抽到 `core/`，并统一 plugin id、补齐分发形态——**不重写 8600 行 stop hook 业务逻辑**，只重构「输出层 / 入口层 / 分析层」。

**分发拓扑决策**：本仓库不是「repo = plugin」（(P+R)），而是「**marketplace 持 1 plugin**」（(M+N)）——repo-root `.claude-plugin/marketplace.json` 列出 plugin、plugin manifest nested 在 `plugins/review-validate-fix/.{claude,codex}-plugin/`、repo-root 同时承载 `core/`/`adapters/`/安装器/测试。理由见 [`docs/architecture/cross-harness.md`](../architecture/cross-harness.md) §1。

---

## 2. 切片实施记录

切片链（依赖序）：S0 v2 → S1 → {S1.5, S2} → S3 → S4。

| 切片 | commit | land main | 一句话 |
|---|---|---|---|
| S0 v2 | `1736114` | ✅ | 统一 plugin id（Codex `rvf`→`review-validate-fix`）+ 补 marketplace.json + 立 core/adapters 骨架 + 删 BC helper |
| S1 | `0b3b2af` | ✅ | `NormalizedTranscript` dataclass + transcript adapter 物理拆分 + vendor-on-install 哨兵 bootstrap |
| S1.5 | `b1f4530` | — | 分析层 host 归一：write-op 计数（A1）+ 主轨迹 call_id（B 主）+ same-session-full ts 子区间窗口（C） |
| S2-observe | `b083f65` | — | 子代理捕获 host 归一（A2）+ `candidate_patch_call_ids` 只读补全（B 子代理半） |
| S2-invoke | `e2f0e9a` | — | headless 子代理调用向量 host 分派下沉 adapter（薄 wrapper） |
| S3 | `aef32a3` | — | Claude hook 双入口守卫单源化（G）；hooks 不迁（方案 A） |
| S4 | `bc57973` | — | 兼容性矩阵 + manifest sync 工具 + 指南 marketplace 变体注 |

> land 状态：S0 v2 + S1 已 fast-forward 进本地 main（`0b3b2af`）；S1.5→S4 已提交、**未 land main**。同步回 main 由 `/base-branch-sync`（`0b3b2af → bc57973`，纯本地无 push）完成——用户稍后手动触发。

### S0 v2 — plugin id 统一 + marketplace.json + 骨架（`1736114`）

- Codex nested manifest `name`：`rvf` → `review-validate-fix`（消除反模式 ② Plugin-id 漂移）。
- 补源仓库 `.claude-plugin/marketplace.json`；`install_to_codex.py` `sync_claude_marketplace_metadata()` 复制到 marketplace 目标，fresh user install 真能 work。
- 立 `core/` + `adapters/{claude_code,codex}/` 顶层骨架（含 README 6 维契约说明）。
- 删 4 个旧 BC helper（`remove_legacy_codex_skill_dir` 等，AGENTS.md「未分发不留 BC」）；清理记入 `dev_backward_compatibility/2026-05-14-s0-v2-*.md`。
- 自审修复一处过时 help 文案（`f8d5a26`，后 cherry-pick 进 main `00123bb`）。

### S1 — NormalizedTranscript + adapter 拆分 + vendor-on-install（`0b3b2af`）

- `core/transcript/{models,io}.py`：`NormalizedTranscript` / `TranscriptRecord` dataclass，`to_dict()` 逐 kind 复刻原 dict 键序（**JSONL byte-equal 硬约束**）。
- `trajectory_distill.py` 物理拆为 `adapters/codex/transcript.py`（Codex 栈）+ `adapters/claude_code/transcript.py`（Claude 栈，复用 codex 的 apply_patch helper）；`trajectory_distill.py` 收为 thin facade（re-export 6-importer 符号并集 + host 探测 + CLI），`distill_*_jsonl` 边界仍出 `list[dict]`。
- **vendor-on-install**（关键打包决策）：源真相留 repo-root `core/`+`adapters/`，安装时由 `install_to_codex.py` 的 `deploy_payload()`（= `copy_tree` + `vendor_pyroot()`）vendor 进 payload；运行期经 `.rvf-pyroot` 哨兵 + `scripts/_rvf_pyroot.py` bootstrap 定位（不数 `parents[N]` 层）。单一 chokepoint：从 `PLUGIN_SRC` 产 payload 只能走 `deploy_payload`（契约 forbid 裸 `copy_tree(PLUGIN_SRC`）。
- 纯重构、零行为变更：现有 4 个 transcript 测试不改即过 = byte-equal 实证；新增 `test_normalized_transcript.py`（round-trip）+ `test_vendored_payload_import.py`（vendored 产物自包含 import）。
- 事故教训：开发中误把未提交改动 vendor 进真实 `~/.codex`（未设 HOME）——手动跑 `install_to_codex.py` 务必 `HOME=$(mktemp -d)` 隔离。

### S1.5 — 分析层 host 归一（`b1f4530`，handoff A1 + B 主 + C）

- **关键 audit 发现**：S1 的 adapters 已让 record 携带 `tool`(Edit/Write/MultiEdit)/`call_id`/`artifact_refs` → S1.5 **纯分析层改动，零 schema/dataclass 改动**（plan 原设想的「新增归一键 schema bump」不需要）。
- **A1**：`analysis_artifacts.py` 3 处 `tool=="apply_patch"` → host-无关谓词 `_is_write_op`（`kind=="tool_call"` 且 `artifact_refs` 非空）。Codex 只有 apply_patch 产 refs → 零回归；Claude Edit/Write/MultiEdit 自然计入。
- **B 主轨迹**：`_patches_from_trajectory` 的 `tool` 由硬编码改 `record.get("tool")`，`patches[]` 接回真实工具名 + call_id。
- **C**：`_rvf_window_start`（复用 `summary.json::timestamp`，缺则 run_id ts 退化）；`same-session-full` 时只计 `ts >= window` 并忽略 index 全量。**用 ts 窗口而非 phase_marker**——实测 phase_marker 全是 `system:*` 结构性标记、不标 RVF 边界。
- 真机只读验证：record 2639→35（窗口化）、patch_event 0→6（Claude write-op 接回）。

### S2-observe — 子代理捕获 host 归一（`b083f65`，handoff A2 + B 子代理半）

- **A2**：`core/subagents/models.py`（host 中性 `SpawnRecord` + JSONL 原语）+ `adapters/codex/subagent.py`（迁 Codex glob/discover）+ `adapters/claude_code/subagent.py`（发现 Claude `<uuid>/subagents/agent-*.jsonl`）；`subagent_capture.py` 收为 host 分派 facade；契约 `forbid '.codex/sessions'` 钉死 facade 不再写死 Codex 布局。调用点传 `host_kind` + `original_transcript`。
- **B 子代理半**：原设想「改 `rvf_fix_attempt.py` 传 call_id」**被证伪**（stop 时子代理 trajectory 未蒸馏、无 call_id）→ 改 `analysis_artifacts._enrich_candidate_patch_call_ids` **analysis 层只读补全**（按 path 接 ledger patch_events ↔ 子代理 write-op call_id）。不改 DB/`rvf_fix_attempt`/`diff_tracker`。
- 真机只读验证：6 子代理、9 host-agnostic write-op（带 artifact_refs + call_id）。

### S2-invoke — 调用向量 host 分派下沉（`e2f0e9a`，薄 wrapper）

- **原计划大半证伪**：原写「抽 `invoke_subagent` runner + 迁 4 处 Codex Popen」。逐站点核出：3 处非子代理（`codex login`/`app-server`/`open codex://`）、reviewer（`run_alternative_reviewer.py`）command 来自外部 config 且已双 host 适配、validate/fix 委托 kanban（RVF 不 Popen `codex exec`）。**真·in-process host-dispatched invoke 只有 analyzer 一处**。
- 落地：`rvf_analyze_thread.build_analyze_command(host)` 的 per-host argv 构造下沉 `adapters/{codex,claude_code}/subagent.py::build_analyze_command`；host 中性 `InvokeCommand` 入 `core/subagents/models.py`（对称观测侧 `SpawnRecord`）。返回 tuple 字节不变、零行为变更。
- **未建 `core/decisions/`**（core→skill 反依赖 + 与 S2-observe 形态不一致）；**B-② DB call_id population 判 moot**（RVF 不启动 fix 子代理、无 invoke 链路；causality.json 已 S2-observe 补全，`rvf_fix_patch_events.call_id` 保持 null 不影响下游）。

### S3 — Claude hook 守卫单源化（`aef32a3`，handoff G，方案 A）

- **用户 4 方案对比后选方案 A**：hooks **不迁**（marketplace `${CLAUDE_PLUGIN_ROOT}` 解析安全；否决迁 adapters/ 树 = Risk #5 高风险、对 G 零帮助）+ **守卫单源化**。
- 两入口 `stop.py`/`user_prompt_submit.py` 逐字复制的 `_is_codex_invocation` 守卫（~25 行 ×2）+ `main()` 转发骨架收敛为单一 sibling 契约 `hooks/_claude_hook_entry.py`：`is_foreign_invocation`（唯一守卫）+ `run_claude_hook(*, event_name, core_script, timeout_env, default_timeout, silent_success)`。两入口收薄 shim。
- 纯重构：6 条诊断消息（`label = f"Claude {event_name} hook"` 派生）+ 3 个 `CODEX_RVF_*` env + 超时键/默认值（Stop=115、UPS=85）+ `silent_success` 两分支差异**全部字节保留**。
- **刻意 stdlib-only**：契约不依赖 `core`/`adapters`——hook 是最该 fail-open 的安全面，不给它加 vendored import 失败模式。
- **deferred**：彻底「注册层零守卫」（让 Codex 不加载 bundled hooks.json、删运行期守卫）= 方案 B，受 Risk #14 实测 + 勿污染 `~/.codex` 约束；保留双触发回归测试当安全网。

### S4 — 兼容性矩阵 + manifest sync + 指南补议（`bc57973`）

- 新建 `scripts/sync-manifest.sh`：jq fail-fast 校验三份 manifest 的 `name`（三处一致）+ `version`（两份 plugin.json 一致；marketplace 无 version）+ `source` 指向 + description 非空。
- 新建 `docs/architecture/cross-harness.md`：as-built 兼容性矩阵 + (M+N) vs (P+R) + payload 实测枚举 + S1–S3 core/adapter 边界表。
- 改 `README.md`（跨 harness 段 + 枚举补全）/ 指南 `06`（脱离反模式② + marketplace 变体节）/ `04`（② 样本注脱钩）/ `check_skill_contracts.sh`（接线运行 sync-manifest + 11 pin）。
- **两处 audit 修正**：① 枚举实为 **3 命令 + 5 skill**（plan 字面「2/2」过期）；② description **有意 host-specific**，sync-manifest **不强校验相等**只校验非空（plan 字面「校验 description 一致」会误杀有意分歧）。
- 无 `.github/` → 按条件分支把 sync-manifest 接进 `check_skill_contracts.sh` 本地触发（未造 GH Actions）。

---

## 3. 2026-05-28 handoff 缺口闭合对照

来源：7 个 RVF run 跨 run 复盘，发现分析/归因层仍 Codex-原生、系统性漏算 Claude-host run。

| 缺口 | 维度 | 闭合切片 | 形态 |
|---|---|---|---|
| **A1** | transcript（patch 计数恒 0） | S1.5 `b1f4530` | host-无关 `_is_write_op`（artifact_refs 非空，不看工具名） |
| **B** | transcript（causality call_id 空） | S1.5（主轨迹）+ S2-observe（`candidate_patch_call_ids`） | record 层 call_id 映射（S1）+ analysis 层只读补全；DB 列 null 判 moot（S2-invoke） |
| **C** | transcript（same-session-full 失真） | S1.5 `b1f4530` | ts 时间窗子区间（非 phase_marker） |
| **A2** | subagent（spawn_agent 恒 0） | S2-observe `b083f65` | Codex glob 迁 adapter + Claude `Task` 子代理发现 |
| **G** | hook entry（双触发） | S3 `aef32a3` | 守卫单源化（单一 `is_foreign_invocation`）；注册层零守卫 deferred |

**通用判据**：任一维度若仍需 `core/` 内写 `if host == "codex"` 分支即未闭合（`rg 'host\s*==\s*["'\'']codex' core/` 应命中 0）。

邻接缺口 **D**（scope-expansion 对 session-owned 新测试无 override）、**E**（ledger 一致性缝隙）host-无关、不在跨 harness 范围，独立排期。

---

## 4. as-built 端态

- 分发：(M+N) Marketplace + Nested；plugin id 三处统一 `review-validate-fix`，由 `sync-manifest.sh` 守护。
- core/adapter 边界：transcript（S1）/ write-op 计数 + 子区间（S1.5）/ 子代理捕获（S2-observe）/ 调用向量（S2-invoke）均 host-agnostic；hook host-ownership 契约单源化（S3）。
- 兼容性：Claude Code = Adapter-backed（trigger-only, bridged to Codex core）；Codex CLI = Native（hook 由安装器注册）；OpenCode/Cursor/Hermes/OpenClaw = Reference-only。详见 [`docs/architecture/cross-harness.md`](../architecture/cross-harness.md)。
- payload：3 slash command（review-validate-fix / rvf-handoff-commit / rvf-land）+ 5 skill（+ rvf-analyze / rvf-handoff-intake / rvf-local-deploy）+ 2 hook 入口 + 共享契约。

---

## 5. 遗留与后续（非阻塞）

1. **`/base-branch-sync`**：把 S1.5→S4 链 fast-forward 回本地 main（`0b3b2af → bc57973`，纯本地无 push）——用户稍后手动触发。
2. **dev channel 真机回归**：S1/S1.5/S2/S3 触 live hook 热路径与捕获/分析层，逻辑已被字节等价 + fixture 覆盖，但真机 Claude Stop/UPS 触发证明留开发者（触 live infra）。
3. **`rvf-local-deploy` vendored payload post-deploy 校验补丁**（小 docs）：S1 让部署后的 `trajectory_distill.py` 依赖 vendored `core/`+`adapters/`+`.rvf-pyroot`（新运行期不变量），但部署两道关（`check_plugin_contracts.py` 前门 + `rvf-local-deploy` SKILL Post-Deploy Checks）都不覆盖它 → 真机 vendoring 静默失败会「检查全过、运行期 ModuleNotFoundError」。建议 SKILL 加 `test -f .rvf-pyroot` / `core/transcript/models.py` / import-smoke + precondition 注。

> **2 与 3 的排期**：推迟到 **「Workflow plugin design system」repo**（[`docs/workflow-plugin-design-system.md`](../workflow-plugin-design-system.md)）的工作完成后再做；届时 RVF 这边的 validation 将被用作测试该体系「**validation as first-class citizen**」（每个 feature/fix 带 validation contract）设计理念的实例。

---

## 6. 范围外（不做的事）

- 不重写 8600 行 stop hook 业务逻辑（router/dispatcher/main 体）。
- 不为 OpenCode/Cursor/Hermes/OpenClaw 实装 adapter（保持 Reference-only）。
- 不写老 slot 兼容代码（未分发）；不引入 vibe-kanban（Kanban 语境一律 cline-kanban / kanban CLI）。
- 不主动 push 远端（全部切片只更新本地分支）。
- 不抬 manifest 到 repo-root（走 (M+N)）。
- 不为 `rvf_analyze.py` 增抽象层（独立于 S0–S4）。
- 不在本计划处理邻接缺口 D / E（host-无关，独立排期）。
