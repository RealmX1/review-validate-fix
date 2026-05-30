# 跨 harness 架构与兼容性矩阵（as-built）

本文是 review-validate-fix（RVF）**当前实际形态**的权威说明：仓库如何在多个 host 上分发、各 host 支持到什么程度、core ↔ adapter 边界落在哪里。

它与 `docs/multi-harness-plugin-guideline/` 的关系：指南给的是**通用原则与建议态**（尤其 [`06-rvf-application.md`](../multi-harness-plugin-guideline/06-rvf-application.md) 的「建议形态」与 [`05-adapter-contract.md`](../multi-harness-plugin-guideline/05-adapter-contract.md) 的 6 维契约）；本文给的是这些原则在本仓库**落地后的实测形态**。两者冲突时，以本文描述的 as-built 为准。

---

## 1. 分发拓扑：(M+N) Marketplace + Nested manifest

本仓库不是「repo = 单个 plugin」（指南 06 隐含的 (P+R) 字面形态），而是「**一个 marketplace 持有一个 plugin**」：

| | (P+R)：repo = plugin | **(M+N)：marketplace 持 plugin（本仓库）** |
|---|---|---|
| plugin manifest 位置 | repo-root `.{claude,codex}-plugin/plugin.json` | nested `plugins/review-validate-fix/.{claude,codex}-plugin/plugin.json` |
| marketplace 文件 | 无（repo 本身即 plugin） | repo-root `.claude-plugin/marketplace.json` 列出 plugin |
| repo-root 还承载 | 仅 plugin 资产 | plugin payload + 工程代码（`core/`、`adapters/`、`scripts/`、`tests/`、`docs/`）+ 部署工具 |
| 适用场景 | 仓库只发一个 plugin、无额外工程层 | 仓库同时是 marketplace、带独立工程/测试/安装栈、未来可能再挂 plugin |

**本仓库为什么走 (M+N)**：repo-root 需要承载 host-agnostic 的 `core/`、`adapters/`、安装器 `scripts/install_to_codex.py`、契约/测试套件，这些不属于任何单个 plugin payload；plugin 自身只是 `plugins/review-validate-fix/` 这一棵子树。把 plugin manifest nested 在该子树下、用 repo-root marketplace.json 把它列出来，才能让 Claude Code / Codex 各自的 marketplace 机制枚举到它，同时不污染工程层。

关键文件：

- `.claude-plugin/marketplace.json` —— 源仓库 marketplace（(M+N) 的「M」），`plugins[0].source` 指向 `./plugins/review-validate-fix`。
- `plugins/review-validate-fix/.claude-plugin/plugin.json` —— Claude Code nested manifest（「N」）。
- `plugins/review-validate-fix/.codex-plugin/plugin.json` —— Codex nested manifest（「N」）。

三者的 `name`（plugin id）必须恒等、两份 plugin.json 的 `version` 必须恒等——由 `scripts/sync-manifest.sh` fail-fast 守护（见 §4）。

---

## 2. 兼容性矩阵（as-built）

| Host | skill / command | hook（trigger） | subagent | transcript / 归因分析 | 等级 |
|---|---|---|---|---|---|
| **Claude Code** | ✅ Native（plugin 已安装：3 command + 5 skill） | ✅ Trigger-only：`Stop` + `UserPromptSubmit` 两入口转发到 Codex core（路径 C） | ➖ 捕获侧 host-agnostic（S2：Claude `Task` 子代理被发现/计数）；调用侧不在 Claude 内启 `Task`（review 经 Kanban / 转发执行） | ✅ host-agnostic（S1 distill 两栈 + S1.5 write-op 归一 + S2 子代理归一） | **Adapter-backed（trigger-only, bridged to Codex core）** |
| **Codex CLI** | ✅ Native（plugin 已安装） | ⚠ Installer-registered：装到 `~/.codex/hooks.json`（Codex 不扫 plugin 根，见反模式 ③）；router→dispatcher→core | ✅ Native（analyzer 经 `codex exec`；reviewer 经外部 config 命令） | ✅ host-agnostic（Codex 栈为归一基线） | **Native（hook 由安装器注册）** |
| **Codex GUI fork（私有）** | ⚠ 未核验，推测同 CLI | ⚠ legacy backup-of-backup（仅 Kanban 不可用时） | ⚠ 未核验 | ⚠ 未核验 | **未核验 / legacy fallback** |
| **OpenCode** | ✅ via `agentskills.io` skill 文档 | ❌ | ❌ | ❌ | **Reference-only** |
| **Cursor** | ✅ via `agentskills.io` skill 文档 | ❌ | ❌ | ❌ | **Reference-only** |
| **Hermes / OpenClaw** | ✅ via `agentskills.io` skill 文档 | ❌ | ❌ | ❌ | **Reference-only** |

矩阵口径说明：

- **Claude Code 的 hook 是 trigger-only**：`hooks/stop.py` / `hooks/user_prompt_submit.py` 是薄 shim，读 stdin → normalize → 转发到 Codex core 脚本（`codex_stop_review_validate_fix.py` / `rvf_user_prompt_submit.py`）。Claude Code **不重写** review 业务逻辑，只做触发器——这就是「bridged to Codex core」的含义。两入口共享单一 host-ownership 契约 `hooks/_claude_hook_entry.py`（S3，见 §3）。
- **Codex 的 hook 标 Installer-registered 而非 Native**：Codex runtime 只扫 `~/.codex/hooks.json`、不自动加载 plugin 根的 `hooks.json`（反模式 ③ / 上游 `openai/codex#16430`）。`scripts/install_to_codex.py --configure-stop-hook` 负责把 router 绝对路径写入 `~/.codex/hooks.json`。
- **OpenCode / Cursor / Hermes / OpenClaw 保持 Reference-only**：只发布 `agentskills.io` 兼容的 skill 文档，不接线 hook / subagent。要升级到 Adapter-backed，按 6 维契约新增一个 `adapters/<host>/`，不动 core。

---

## 3. plugin payload 枚举（实测）

`plugins/review-validate-fix/` 当前实际携带：

**Hooks（Claude Code 触发面，`hooks/hooks.json` 注册）**

| 文件 | 角色 |
|---|---|
| `hooks/hooks.json` | 注册 `Stop`→`stop.py`（timeout 120）、`UserPromptSubmit`→`user_prompt_submit.py`（timeout 90） |
| `hooks/stop.py` | Stop hook 薄 shim（转发 Codex core；inner timeout 默认 115s） |
| `hooks/user_prompt_submit.py` | UserPromptSubmit hook 薄 shim（inner timeout 默认 85s） |
| `hooks/_claude_hook_entry.py` | 两入口共享的单一 host-ownership 契约（S3）：`is_foreign_invocation` 守卫 + `run_claude_hook` 转发流程；stdlib-only 保 fail-open |

**Commands（3 个 slash command）**

| 命令 | 用途 |
|---|---|
| `commands/review-validate-fix.md` | 启动 RVF double-review / validate-fix / handoff 主工作流 |
| `commands/rvf-handoff-commit.md` | 分析 RVF handoff、采纳有效修复或已审工作、验证并提交 |
| `commands/rvf-land.md` | 收尾同一 worktree 中 future-self 已应用的 RVF 工作（不自动 base-branch-sync） |

**Skills（5 个 skill）**

| skill | 用途 |
|---|---|
| `skills/review-validate-fix/` | RVF 主工作流的 canonical 运行内容 |
| `skills/rvf-analyze/` | finalized run 的只读复盘（叙事 + issue↔patch 归因）；不启新 review |
| `skills/rvf-handoff-intake/` | handoff 接入：决定采纳哪些建议、验证、暂存相关文件、提交 |
| `skills/rvf-land/` | rvf-land 命令的实现体 |
| `skills/rvf-local-deploy/` | 从当前 checkout 部署/安装到本机 Codex plugin cache + 配置 stable hook |

> `plugin.json` 用目录 glob（Claude `commands:["./commands/"]` / `skills:["./skills/"]`；Codex `skills:"./skills/"`）自动发现，**新增命令/skill 无需改 manifest**；但本矩阵、README 维护模型与 `sync-manifest` 的枚举须随实测更新。

---

## 4. host-agnostic core / adapter 边界（S1–S3 落地）

跨 harness 的核心纪律（[`04-anti-patterns.md`](../multi-harness-plugin-guideline/04-anti-patterns.md) ④）：**core 不消费 host 工具名 / 不 hardcode host 布局**。本仓库已通过以下切片把分析/归因层归一：

| 维度 | core（host-中性） | adapter（host-specific） | 切片 |
|---|---|---|---|
| transcript 解析 | `core/transcript/{models,io}.py`（`NormalizedTranscript` / `TranscriptRecord`） | `adapters/{codex,claude_code}/transcript.py` | S1（`0b3b2af`） |
| write-op 计数 / 子区间 | `analysis_artifacts._is_write_op`（artifact_refs 非空，不看工具名）+ ts 窗口 | —（消费归一 record） | S1.5（`b1f4530`） |
| 子代理捕获 | `core/subagents/models.py`（`SpawnRecord`） | `adapters/{codex,claude_code}/subagent.py::resolve_subagents` | S2-observe（`b083f65`） |
| 子代理调用向量 | `core/subagents/models.py`（`InvokeCommand`） | `adapters/{codex,claude_code}/subagent.py::build_analyze_command` | S2-invoke（`e2f0e9a`） |
| hook host-ownership | —（hook 是触发面、不入 core） | `hooks/_claude_hook_entry.py`（守卫单源化） | S3（`aef32a3`） |

部署形态：`core/` + `adapters/` 是 repo-root 真相，安装时由 `install_to_codex.py` 的 `deploy_payload` → `vendor_pyroot` vendor 进 payload，运行期经 `.rvf-pyroot` 哨兵 + `scripts/_rvf_pyroot.py` bootstrap 定位（S1）。

**通用判据**：任一 host 维度若仍需在 `core/` 内写 `if host == "codex"` 式分支，即视为该维度归一未闭合（`rg 'host\s*==\s*["'\'']codex' core/` 应命中 0）。

`scripts/sync-manifest.sh` 是这套形态在 manifest 层的 fail-fast 守护：校验三份 manifest 的 `name`（三处一致）、`version`（两份 plugin.json 一致）、`source` 指向、description 非空；**有意不校验 description 内容相等**（跨 host 文案是设计，不是漂移）。`scripts/check_skill_contracts.sh` 会在本地契约检查中运行它。
