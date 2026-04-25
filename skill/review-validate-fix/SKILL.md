---
name: review-validate-fix
description: Use when the user asks for a post-work code review loop, review validation, validate/fix subagents, handoff context, optional skip-review / no-handoff modes, or migration from the old Claude review-validate-fix slash command and Stop hook into a Codex skill.
---

# Review Validate Fix

本 skill 用于对当前仓库的未提交改动执行一轮 double review -> merge -> validate/fix -> handoff。它替代旧 Claude `/review-validate-fix` slash command 与 Stop hook；不要依赖 Claude Stop hook、`CLAUDE_SESSION_ID`、`.claude/hooks/state`、Claude-only agent 参数，或任何单一 vendor 的 agent 名称。

本 skill 只应由用户显式调用，例如 `$review-validate-fix`。`agents/openai.yaml` 将 `policy.allow_implicit_invocation` 设为 `false`，避免 agent 或模型因为相似上下文自动启用它。

例外：如果用户已经配置本 skill 附带的 Codex Stop fork hook，该 hook 可以在明确的 dirty repo 停止点调用 `codex fork <parent-session-id> <prompt>`，创建一个以 `$review-validate-fix` 开头的新 fork 会话。这属于用户预配置脚本生成的显式 prompt 边界，不是模型隐式启用 skill，也不改变 `allow_implicit_invocation: false` 的约束。旧的 Codex Stop continuation hook 仍作为 `CODEX_RVF_MODE=continuation` fallback 保留。

## 入口判断

1. 在目标仓库运行 `git status --porcelain`，或使用 `scripts/review_validate_fix_gate.sh <repo>`。
2. 如果没有未提交改动，用中文说明没有可审查改动并结束。
3. 如果有改动，先查看 `git status --short -uall`、`git diff HEAD`，再读具体文件。
4. Review 前优先用 `scripts/prepare_review_run.py --repo <repo> --session-context <file>` 创建唯一 run 目录；该脚本会生成 self-contained review packet、packet metadata 和 review 前 workspace snapshot。需要手动生成 packet 时，可用 `scripts/build_review_packet.py --repo <repo> --session-context <file> --output <packet> --metadata-output <metadata>`。packet 必须覆盖 tracked diff、完整 untracked 文件列表，以及可内联的 untracked 文件内容；不要只依赖 `git diff HEAD`。
5. 如果已知本 turn 的主修改文件或背景 WIP，生成 packet 时用 `--primary-file` / `--background-file` 标注 review scope，避免 reviewer 把历史 WIP 与本 turn 修改混为一谈。
6. 避免使用固定 `/tmp/theseus-rvf-*` 路径保存 packet、snapshot 或 reviewer 输出；使用 `prepare_review_run.py` 的唯一 run 目录，或至少用 `mktemp -d`。

## 运行选项

- 本 skill 支持显式 `pass_type` / `mode`，默认是 `full`：执行 double review -> merge -> validate/fix -> handoff。只有用户明确写出 `$review-validate-fix` 且没有指定更窄模式时，才进入默认 `full` 流程。
- `pass_type: review_only` / `mode: review_only` 是只读 reviewer 子 pass：只允许读取、搜索、运行不会主动写回源码的验证命令，并最终只输出精确 `NO_ISSUES` 或编号 issue list。禁止修改文件、禁止 validate/fix、禁止 stage/commit、禁止生成 `<handoff-context>`，也不要输出 handoff 摘要。
- `pass_type: validate_fix` / `mode: validate_fix` 只处理主会话分配的 canonical issue 包：允许按 `references/validate-then-fix-prompt.md` 对 `REAL` 问题做最小修复，但仍禁止重新执行 double review、扩大审查范围或生成 `<handoff-context>`。
- `mode: research_checkpoint_no_handoff` / `no-handoff research checkpoint` 不是 review loop：只输出用户要求的研究 checkpoint / 汇总，不启动 review、validate/fix 或 handoff。即使上下文中提到 `$review-validate-fix`、review、fix 或 handoff，也不得生成 `<handoff-context>`。
- 如果本 skill 文本被放进子代理或研究代理上下文，而该代理的当前任务明确是只读 review、普通研究、checkpoint、ledger 维护或 no-handoff 汇总，当前任务的窄模式优先于默认 `full`。不要因为上下文出现 `$review-validate-fix` 叙事就升级为完整流程。
- 默认开启 review：除非用户在本轮 `$review-validate-fix` prompt 中明确写出 `skip review`、`no review`、`review off`、`handoff only`、`跳过 review` 或等价中文表达，否则必须执行 santa-method double review。
- 默认开启 handoff：除非用户在本轮明确写出 `no handoff`、`skip handoff`、`handoff off`、`不要 handoff`、`不生成交接` 或等价中文表达，否则最终回复末尾必须生成 `<handoff-context>`。
- 如果用户同时要求跳过 review 且关闭 handoff，只做入口检查、必要的用户指定 validate/fix 工作和中文结果汇总；不要生成 reviewer provenance 或 handoff blob。
- 这些开关只影响本轮 skill 调用，不写入持久配置，也不要因为上轮用户偏好自动沿用。

## Codex Stop Fork Hook

- hook 脚本为 `scripts/codex_stop_review_validate_fix.py`，只负责判断是否要把 `$review-validate-fix` 作为新的 Codex fork prompt 提交；不要在 hook 脚本里直接执行 review/fix。
- 默认 `CODEX_RVF_MODE=fork` 且 `CODEX_RVF_FORK_MODE=terminal`：dirty gate 通过后，脚本生成 prompt、launcher 和日志，并用 Terminal 启动 `codex fork <parent-session-id> <prompt>`。当前 Codex Desktop 对 Stop hook 的 `systemMessage` 在某些路径上可能不显示，因此默认必须执行可见的 fork action，而不是只准备文件。
- 显式 `CODEX_RVF_FORK_MODE=manual`：只生成可手动运行的 launcher，不自动打开 Terminal。该模式适合调试，但不应用作依赖自动 post-work review 的默认配置。
- `CODEX_RVF_FORK_MODE=dry-run`：只测试 fork prompt 和日志生成，不启动 fork。
- fallback `CODEX_RVF_MODE=continuation`：使用 Codex Stop continuation hook 的 `decision: "block"` / continuation prompt 机制，在同一会话继续运行；该模式不提供独立 fork checkpoint。
- `CODEX_RVF_MODE=off`：dirty gate 通过也只输出 systemMessage 并跳过自动触发。
- 如果 `stop_hook_active=true`，必须直接跳过，避免 Stop continuation 或 fork 递归。
- 如果 Stop 事件来自 Codex subagent，必须直接跳过。post-work review 只能由主会话显式触发；研究、review、validate/fix 等子代理结束时不得被 Stop hook 拖入新的 `$review-validate-fix` continuation。
- 如果环境变量 `CODEX_RVF_SUPPRESS=1` 或 `CODEX_RVF_SUPPRESS_STOP_HOOK=1`，必须直接跳过；该开关用于 research marathon 等主会话已接管调度的场景。
- Stop hook matcher 当前不能按 repo 过滤，因此脚本必须先使用 `scripts/review_validate_fix_gate.sh` 做 dirty gate。
- 如果当前 `cwd` 不在 git repo 中，可以扫描 `~/.codex/config.toml` 中 trust_level 为 `trusted` 的项目；只有唯一 dirty trusted repo 时才自动 fork，多个候选必须 fail-safe 跳过并给出提示。
- fork prompt 必须包含目标仓库路径、`RVF_FORKED_REVIEW_VALIDATE_FIX`、父 session id 和父 cwd，让新 fork 能在正确仓库执行并在结束时识别自身。
- 新 fork 会话结束时，如果 Stop 事件的 `last_assistant_message` 已包含 `<handoff-context>`，hook 通过 systemMessage 程序化提示用户复制最终回复中的 handoff block 并粘贴回原始 chat session；不要让 agent 在正文里手写这个提示。该结束提示只适用于实际 fork 会话，manual prepare 本身不会运行 review。
- fork launcher 会在 Stop 事件提供 `model` 时显式传入 `-m <model>`，并从 Stop 事件、`CODEX_RVF_FORK_REASONING_EFFORT` 或 `~/.codex/config.toml` 的 `model_reasoning_effort` 推出 reasoning effort 后通过 `-c model_reasoning_effort=...` 传入。若父会话使用了 hook 不可见的临时 reasoning override，则无法完全保证继承。

## Setup-only 资源

- `setup/mcp-setup-startup.md` 不是运行期 reference，只用于用户明确要求配置或重配 santa-method alternative reviewer / MCP / agent 集成时。
- setup agent 必须通过 `scripts/read_mcp_setup_once.sh` 读取该文件；脚本会写入 `state/mcp-setup-startup.viewed`，marker 存在时默认只返回“已读取过”的提示。
- 只有用户明确要求重新 setup、更换 alternative reviewer，或排查 alternative reviewer 配置 drift 时，才可用 `scripts/read_mcp_setup_once.sh --force` 重新读取。
- 正常执行 `$review-validate-fix`、Stop continuation、review、merge、validate/fix、handoff 时，不得读取、引用或总结 `setup/mcp-setup-startup.md`。

## Review

- review pass 的 `pass_type` 永远是 `review_only`。无论它由 full 流程派生、由用户单独要求只读 review，还是出现在研究马拉松 checkpoint 中，都必须停在 `NO_ISSUES` 或 issue list；不得把自己升级为完整 `$review-validate-fix` 流程。
- 默认执行 santa-method double review：始终并行启动两个独立 review pass。
- 如果用户显式要求跳过 review：
  - 不启动 Codex reviewer、alternative reviewer 或 Codex-only fallback。
  - 不读取 `references/review-prompt.md`，不执行 review merge，也不伪造 reviewer provenance。
  - 如果用户随 prompt 提供了明确 issue list，则把它们标为 `user-supplied-skip-review`，进入 Validate / Fix；这些 issue 仍必须逐项验证。
  - 如果用户没有提供 issue list，则跳过 Validate / Fix，直接进入最终汇总；handoff 默认仍开启，除非用户也显式关闭 handoff。
  - 最终汇总和 handoff 必须写明 `review_status: SKIPPED_BY_USER`。
- review 阶段优先使用能力隔离，而不是事后追责：
  - Codex-native reviewer 优先用探索型 agent；如果当前 Codex agent API 暴露工具或 capability allowlist，就保留读取、检索、shell/test 能力，不授予直接编辑、patch、文件写入、stage、commit 或 validate/fix 相关能力。
  - 如果当前 Codex `spawn_agent` 接口没有显式 capability allowlist（只有 agent type / model / reasoning 等参数），不要把 prompt 当成硬沙箱；仍可让 reviewer 读取仓库、运行测试/lint/build，但必须在 prompt 中明确禁止直接写文件、修复、stage/commit 和 handoff。
  - external alternative reviewer 应允许读取仓库并运行测试命令；配置层面只剥离直接编辑/写入工具。不要因为 reviewer 需要 shell 或 repo cwd 就降级为 fallback。
- `alternative reviewer` 可以是用户配置的任意外部 coding agent（例如某个 CLI、MCP 暴露的 agent、IDE agent 或本地 wrapper）。不要在本 skill 中硬编码具体 vendor、模型名或命令名。
- 如果 `config/alternative-reviewer.json` 已配置且 `scripts/run_alternative_reviewer.py --check` 通过，则使用一个 Codex-native reviewer 加一个 `alternative-reviewer:<agent-name>`；需要确认认证/健康状态时优先用 `scripts/run_alternative_reviewer.py --preflight`，它会在配置了 `health_command` 时一并检查。运行 external reviewer 时用 `scripts/run_alternative_reviewer.py --repo <repo> --review-packet <packet> --session-context <file>`，让 reviewer 能结合 packet 与本地测试结果审查。
- external alternative reviewer 仍允许运行测试、lint、typecheck、build 或复现命令。不要把“可能产生测试缓存/报告/临时文件”误当成禁止运行命令的理由。
- external alternative reviewer 默认必须自行完成审查；除非本轮 prompt 明确要求等待人工步骤，否则不要期待开发者手动运行命令、提供额外操作或协助它完成 review。
- external alternative reviewer 的等待机制是可观测活动空闲超时：`scripts/run_alternative_reviewer.py` 从 `config/alternative-reviewer.json` 读取 `idle_timeout_seconds` 与 `activity_check_interval_seconds`，默认每 300 秒检查一次 stdout/stderr 是否有新活动；过去一个检查窗口内有新活动就刷新等待，连续 300 秒没有可观测活动则终止该 reviewer，返回 exit code `124` 并输出 `RVF_EXTERNAL_REVIEWER_TIMEOUT ...`。此时不要合并任何 partial reviewer 输出；除非用户要求 external-only fail-close，否则把本轮 external reviewer 视为不可用并走 Codex-only fallback。
- 对可能与主会话或另一个 reviewer 冲突的命令，优先使用 `scripts/command_lock.py --repo <repo> --name <stable-lock-name> -- <command ...>` 做 repo-scoped 锁保护。典型场景包括共享 dev server 端口、会写同一缓存/coverage/report 目录的长测试、包管理器安装/构建、会独占设备或全局资源的命令。
- 如果 reviewer 判断某个命令需要锁但当前 prompt 或环境没有提供可用锁，它必须输出 `RVF_LOCK_REQUEST name=<stable-lock-name> command=<command> reason=<why>` 作为唯一响应；这不是完成的 review 结果，主会话应提供锁包装后的命令或更新 prompt 后重试该 reviewer。不要把 `RVF_LOCK_REQUEST` 合并为 bug finding。
- 如果 alternative reviewer 未配置、配置未完成、命令不可用或本轮无法启动，默认使用 Codex-only fallback；不要询问用户、不要中断 review loop、不要降级为单 reviewer。
- Codex-only fallback 必须并行启动两个 Codex-native 子代理模拟 santa-method：两个子代理使用同一份 review prompt 和 session context，彼此不看对方输出，并在 provenance 中标为 `codex-mimic-reviewer-a` 和 `codex-mimic-reviewer-b`。
- 只有用户在本轮明确要求必须使用外部 alternative reviewer、且不接受 Codex-only fallback 时，才因 alternative reviewer 不可用而 fail-close。
- 两个 reviewer 使用同一份 review prompt 和 session context，但彼此不看对方输出。
- 完成态 Review 输出契约必须严格为：
  - 无问题：只输出 `NO_ISSUES`。
  - 有问题：输出编号 issue list，每条含 `路径:行号` 和 1-2 句中文说明。
- 非完成态锁请求契约为：只输出一行或多行 `RVF_LOCK_REQUEST ...`，由主会话提供 `scripts/command_lock.py` 包装命令后重试；锁请求不得与 `NO_ISSUES` 或 issue list 混在同一个输出中。
- 每个 reviewer 输出必须先用 `scripts/check_review_output.py` 或等价严格解析器校验；中文化“没有问题”、空响应、纯 prose、handoff、validate/fix verdict、修复说明或不可解析列表都不是合格 review 输出。
- 如果 reviewer 输出 `RVF_LOCK_REQUEST`，先满足或驳回该锁请求，再重试 reviewer；重试后的输出仍必须是完成态契约。锁请求本身不计入合格 double-review 来源。
- 如果 reviewer 输出契约违规，可用同一 review packet 重试一次并明确指出只允许 `NO_ISSUES`、编号 issue list，或纯 `RVF_LOCK_REQUEST`；再次违规则 fail-close，用中文询问用户如何处理，且不要把该 reviewer 当作合格 double-review 来源。
- review 前后可用 `scripts/workspace_snapshot.py capture/compare` 记录状态，尤其是 reviewer 会运行测试/lint/build 时。状态变化只表示 `WORKSPACE_CHANGED_DURING_REVIEW`，不推断 reviewer 主动编辑，也不自动使输出失格；主会话应检查变化是测试缓存/报告等可解释副作用，还是源文件、lockfile、snapshot 等需要人工处理的污染。不要自动 revert 用户或其他进程可能造成的改动。
- 详细 review prompt 见 `references/review-prompt.md`。
- 合并两个 reviewer 的输出时读取 `references/review-merge-policy.md`：合并重复项、分组紧密相关 issue，并为每个 processed issue 记录来源 reviewer。

## Validate / Fix

- `NO_ISSUES` 进入 clean path，handoff 默认仍开启，除非用户显式关闭 handoff。
- 可解析 issue list 进入 validate/fix。
- 中文化“没有问题”、空响应、纯 prose 或不可解析列表都 fail-close：用中文询问用户如何处理，不静默当作 0 个问题。
- 每条 issue 必须先验证，再决定：
  - `REAL`：真问题且可独立最小修复。
  - `FALSE_POSITIVE`：不成立，不改文件。
  - `ELEVATE`：真问题但需要用户决策，不改文件。
- 分配 validate-review 子代理时按问题耦合度组织：不必一条 issue 一个 agent。共享根因、同一文件区域、同一测试路径或同一决策前提的问题，应合并成一个验证包交给同一个 validate-review 子代理；验证包仍要逐项输出 verdict。
- 主会话必须为 validate/fix 分组保留一张审计表，不只把分组信息写进子代理 prompt。每个分组记录 `validation_group_id`、包含的 canonical issue / processed id、分组理由、分配给哪个 validate/fix 子代理或本地执行、以及逐项 verdict 汇总。
- 发给 validate/fix 子代理的 issue context 必须 source-agnostic：不要包含“Codex 发现”“alternative reviewer 发现”“两个 reviewer 都发现”等来源标签，也不要暗示哪个模型或 agent 支持该 issue。来源 provenance 只保留在主会话的合并表 / handoff 中。
- validate/fix 子代理只处理主会话分配给它的 canonical issue 包；不得自行扩大 review 范围、重新执行 double review、生成 handoff 或处理未分配问题。
- 所有 validate/fix 子代理完成后，主会话的最终中文汇总必须包含“Validate/fix 分组”小节，说明 reviewer 标记出的 processed issues 是如何被分配成验证包的：每组列出 group id、包含的问题、合并验证原因和结果统计。即使某组只有一条 issue，也要说明它为何独立验证。
- 详细 validate/fix prompt 见 `references/validate-then-fix-prompt.md`。

## ELEVATE

- `ELEVATE` verdict 必须附带 `elevation-detail` fenced block。
- 可用 `scripts/parse_elevation_detail.py` 做确定性解析；解析失败时降级为普通中文说明，并明确缺少哪些字段。

## Handoff

- 最终用中文汇总 flag 数、真实修复数、误报数、升级数；如果用户跳过 review，也要汇总 review 状态。若本轮进入 validate/fix，最终汇总还必须包含“Validate/fix 分组”小节，披露主会话如何把 reviewer 标记的问题分组成验证包，以及每组的结果。
- Handoff 默认开启：成功完成本 skill 时，末尾必须生成一个 fenced markdown code block，opening fence 必须是 ```` ```markdown ````，code block 内包含完整 `<handoff-context>...</handoff-context>` blob，closing fence 必须是 ```` ``` ````；不要把 `<handoff-context>` 作为未包裹的裸标签输出。模板见 `references/handoff-template.md`。
- 用户明确关闭 handoff、当前是 `pass_type: review_only` / `pass_type: validate_fix` 子 pass，或当前是 `mode: research_checkpoint_no_handoff` 时，最终回复只给当前任务要求的中文结果，不输出 `<handoff-context>`，也不要输出空模板。
- Handoff 内容只写你能确认的事实；不要把仓库里既有 WIP 或其他 session 的改动混成本 turn 的改动。
- 不要复用旧 Stop hook 里的“从 hook 触发点 time-travel”假设。当前 Codex fork 方案创建的是停止后的新 fork 用户 prompt checkpoint；它可作为回退边界，但不是聊天中任意内部 Stop 事件的真正 time-travel snapshot。

## Legacy Context

- 旧 slash command 原文：`references/legacy-claude-command.md`
- 旧 Stop hook 原文：`references/legacy-claude-stop-hook.md`
- 旧 activity hook 原文：`references/legacy-claude-mark-activity.sh`
- 旧 hook handoff 兼容性说明：`references/legacy-compatibility-notes.md`
- 双 reviewer 合并策略：`references/review-merge-policy.md`

只在迁移、对照旧行为或排查历史 drift 时读取 legacy 文件；正常执行 review loop 优先使用本 skill 的 references 与 scripts。不要把 setup-only 资源当作 legacy 或运行期 reference。
