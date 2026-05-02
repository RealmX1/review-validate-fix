---
name: review-validate-fix
description: Use when the user asks for a post-work code review loop, review validation, validate/fix subagents, handoff context, optional skip-review / no-handoff modes, or migration from the old Claude review-validate-fix slash command and Stop hook into a Codex skill.
---

# Review Validate Fix

本 skill 默认用于对当前对话的 session-scoped 未提交改动执行一轮 double review -> merge -> validate/fix -> handoff。用户手动调用 `$review-validate-fix` 时，可以显式要求主会话提供自定义 review scope（例如指定文件、目录、commit range、已完成设计或 clean repo 中要审查的实现面）；这种 manual scoped review 不得被 clean repo 阻塞。它替代旧 Claude `/review-validate-fix` slash command 与 Stop hook；不要依赖 Claude Stop hook、`CLAUDE_SESSION_ID`、`.claude/hooks/state`、Claude-only agent 参数，或任何单一 vendor 的 agent 名称。Codex 环境中优先使用 `scripts/session_manifest.py` 从当前 transcript 生成 session ownership manifest；`git diff HEAD` 是证据，不是默认 scope 来源。

本 skill 只应由用户显式调用，例如 `$review-validate-fix`。`agents/openai.yaml` 将 `policy.allow_implicit_invocation` 设为 `false`，避免 agent 或模型因为相似上下文自动启用它。

例外：如果用户已经配置本 skill 附带的 Codex Stop hook，该 hook 可以在明确的 dirty repo 停止点创建新的 Cline Kanban task，或在 Cline Kanban 当前 task 中通过 host 定制的真实 follow-up user message 启动 review loop。Codex GUI/app-server `thread/fork` + `turn/start` 只保留为 legacy backup-of-backup，不是默认路径。这属于用户预配置脚本生成的显式 prompt 边界，不是模型隐式启用 skill，也不改变 `allow_implicit_invocation: false` 的约束。

Codex CLI / GUI 入口：安装器会在 `~/.codex/config.toml` 启用 `rvf@local-codex-plugins`，并删除旧 `~/.codex/skills/review-validate-fix` 目录，避免同一个 workflow 在 GUI skill picker 中出现两次。plugin package id 有意使用 `rvf`，真正的手动入口仍是 skill 名 `review-validate-fix`；这样 Codex CLI 的 `$review-validate-fix` mention popup 只命中 skill，不再同时出现同名 `[Plugin]` 与 `[Skill]` 候选。不要为了补 CLI 的 `/review-validate-fix` 直达项而重新生成同名本机 skill。

## 入口判断

1. 在目标仓库运行 `git status --porcelain`，或使用 `scripts/review_validate_fix_gate.sh <repo>`。
2. 如果没有未提交改动，先检查本轮 `$review-validate-fix` prompt 是否明确给出了 manual custom scope，或明确要求主会话按用户提供的范围写 scope-of-work。若没有，才用中文说明没有可审查改动并结束；若有，则继续执行 manual scoped review，并在 scope-of-work 中明确写出“仓库当前 clean；本轮审查范围来自用户显式指定，而不是未提交 diff”。
3. 如果有改动，先查看 `git status --short -uall`、`git diff HEAD`，再读具体文件；但不要把 whole-repo dirty diff 当成默认 review scope。
4. Review 前必须先由主会话写一份 scope-of-work / session context 文件，概括用户意图、本 turn 实际完成的工作或用户显式指定的 manual review scope、主会话确认改过或需要审查的文件、每个文件中实际做了哪些编辑或本轮要重点审查的代码面、已跑验证命令、关键设计取舍和仍不确定的点。它不能只列 created/modified/deleted 文件；必须写明具体编辑内容或明确的审查目标，例如“在 X 函数新增 Y 分支”“把 Z 调用改为传入 W 参数”“clean repo 手动审查 A/B 模块的错误处理路径”。不要让 reviewer 只靠 `git diff HEAD` 猜 scope。
5. 如果 Stop event、fork prompt 或当前环境提供 Codex JSONL transcript path，先用 `scripts/session_manifest.py --repo <repo> --transcript <jsonl> --output <manifest>` 生成 session ownership manifest。manifest 中的 `owned_paths` / `owned_dirty_paths` 是默认 review scope；`unattributed_dirty_paths` 是背景 WIP，不得主动审查，除非它被 session-owned 改动直接连带影响。
6. 如果当前 fork prompt 或环境已经提供预冻结 artifacts，例如 `RVF_RUN_DIR` / `RVF_ARTIFACTS_DIR` / `RVF_REVIEW_ENV` / `RVF_REVIEW_PACKET` / `RVF_SCOPE_CONTRACT`，先 source 既有 `review-env.sh` 并复用这些文件；不得重新运行 `prepare_review_run.py` 创建新的 run，尤其不得把 run 写入 Cline Kanban task 的 `.cline/worktrees/...` worktree。否则，用 `scripts/prepare_review_run.py --repo <repo> --session-context <file> --transcript <jsonl>` 或 `--session-manifest <manifest>` 创建唯一 run 目录；该脚本会把 scope-of-work 和 manifest 复制到 run 目录的 `artifacts/inputs/`，并生成 self-contained review packet、packet metadata、review 前 workspace snapshot、不可变 `scope.contract.json`、`review-env.sh` 和 `review-agent-context.md`。需要手动生成 packet 时，可用 `scripts/build_review_packet.py --repo <repo> --session-context <file> --session-manifest <manifest> --output <packet> --metadata-output <metadata>`。
7. `prepare_review_run.py` 输出中的 `review_env_file` / `review_env` 是 subprocess 可直接使用的短期路径上下文；`review_agent_context_file` / `review_agent_context` 是给 Codex-native reviewer 子代理的程序化入口块。后续 prompt、命令示例和子代理交接应复用这个生成块，或引用其中的 `RVF_RUN_DIR`、`RVF_ARTIFACTS_DIR`、`RVF_INPUTS_DIR`、`RVF_SCOPE_CONTRACT`、`RVF_SCOPE_OF_WORK`、`RVF_SESSION_MANIFEST`、`RVF_REVIEW_PACKET`、`RVF_COMMAND_LOCK` 等变量；不要由主会话手写 export block，也不要在同一段 prompt 中反复展开 `state/runs/<run_id>/artifacts/...` 的绝对路径。
8. 如果没有 transcript/manifest，仍必须提供可靠 scope-of-work；如果既无法生成 manifest，也无法从当前会话可靠写出 scope-of-work / session context，不要编造，也不要降级为纯 diff review；fail-close，用中文向用户说明缺少本 turn 工作上下文。`--allow-missing-session-context` 只允许调试脚本或迁移排障时显式使用，正常 review loop 禁用。
9. 如果已知本 turn 的主修改文件、manual custom scope 文件或背景 WIP，生成 packet 时用 `--primary-file` / `--background-file` 标注 review scope，避免 reviewer 把历史 WIP 与本 turn 修改混为一谈。clean repo 的 manual scoped review 也应把用户指定路径作为 `--primary-file` 传入，让 packet 明确保留 scope anchor。
10. 避免使用固定 `/tmp/theseus-rvf-*` 路径保存 packet、snapshot 或 reviewer 输出；使用 `prepare_review_run.py` 的唯一 run 目录，或至少用 `mktemp -d`。

## 运行选项

- 本 skill 支持显式 `pass_type` / `mode`，默认是 `full`：执行 double review -> merge -> validate/fix -> handoff。只有用户明确写出 `$review-validate-fix` 且没有指定更窄模式时，才进入默认 `full` 流程。
- `pass_type: review_only` / `mode: review_only` 是只读 reviewer 子 pass：只允许读取、搜索、运行不会主动写回源码的验证命令，并最终通过 `$RVF_REVIEW_RESULT` 写 canonical review result artifact。禁止修改 repo 源文件、禁止 validate/fix、禁止 stage/commit、禁止生成 handoff.md，也不要输出 handoff 摘要。
- `pass_type: validate_fix` / `mode: validate_fix` 只处理主会话分配的 canonical issue 包：允许按 `references/validate-then-fix-prompt.md` 对 `REAL` 问题做最小修复，但仍禁止重新执行 double review、扩大审查范围或生成 handoff.md。
- `mode: research_checkpoint_no_handoff` / `no-handoff research checkpoint` 不是 review loop：只输出用户要求的研究 checkpoint / 汇总，不启动 review、validate/fix 或 handoff。即使上下文中提到 `$review-validate-fix`、review、fix 或 handoff，也不得生成 handoff.md。
- 如果本 skill 文本被放进子代理或研究代理上下文，而该代理的当前任务明确是只读 review、普通研究、checkpoint、ledger 维护或 no-handoff 汇总，当前任务的窄模式优先于默认 `full`。不要因为上下文出现 `$review-validate-fix` 叙事就升级为完整流程。
- 默认开启 review：除非用户在本轮 `$review-validate-fix` prompt 中明确写出 `skip review`、`no review`、`review off`、`handoff only`、`跳过 review` 或等价中文表达，否则必须执行 santa-method double review。
- 默认开启 handoff：除非用户在本轮明确写出 `no handoff`、`skip handoff`、`handoff off`、`不要 handoff`、`不生成交接` 或等价中文表达，否则必须在 run artifact 中创建并持续维护 `handoff.md`。最终回复前先运行 `python3 scripts/rvf_handoff.py open <handoff.md 绝对路径>`（如果当前 cwd 不是 skill 目录，使用本 skill 的绝对脚本路径）尝试用默认编辑器打开文件；最终回复先输出 `RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，随后用 1-3 句极短中文说明 reviewers 做了什么、validate/fixers 做了什么；不要重复 handoff 文件正文。
- 主会话启动 RVF 子代理时，所有 Codex-native reviewer、Codex-only fallback reviewer、validate/fix 子代理，以及为 `RVF_*_REQUEST` 派生的受控子任务，默认都必须使用当前可用的最佳模型，并显式设置 `reasoning_effort=high`。若当前接口不暴露 model / reasoning effort 参数，或用户、平台、本轮运行环境明确限制，必须在 run ledger / handoff / 最终汇总中记录原因，不得静默降级。
- 如果用户同时要求跳过 review 且关闭 handoff，只做入口检查、必要的用户指定 validate/fix 工作和中文结果汇总；不要生成 reviewer 来源记录或 handoff blob。
- 这些开关只影响本轮 skill 调用，不写入持久配置，也不要因为上轮用户偏好自动沿用。

## Codex Stop Hook

- 核心设计支柱：Stop hook 自动化必须让触发 hook 的父会话在 hook 完成后停止，并默认在新的 Cline Kanban task 或当前 Kanban task follow-up 中留下 review checkpoint。Codex GUI fork 只允许作为 legacy backup-of-backup，保留父会话完整上下文并由 hook 注入以 `$review-validate-fix` 开头的首个用户 prompt；不要把当前会话 continuation 当作自动路径。
- installed Stop hook 入口为 plugin skill 内的 `scripts/codex_stop_hook_dispatcher.py`，它在 RVF 源仓库的主会话 Stop 时先检查并安装本 repo 的 plugin，再把同一份 Stop event JSON 转交给 `scripts/codex_stop_review_validate_fix.py`。真实 fork gate 逻辑仍只放在 `codex_stop_review_validate_fix.py`；不要在 hook 脚本里直接执行 review/fix。
- `codex_stop_review_validate_fix.py` 内部必须保持两层结构：`evaluate_stop_event()` 统一处理 suppress、递归、handoff、already-fork、subagent、session gate、dirty gate 和 session-owned dirty scope；`launch_backend()` 只执行归一后的 backend。不要新增公开 `CODEX_RVF_BACKEND`，现有 `CODEX_RVF_MODE` / `CODEX_RVF_FORK_MODE` 只在入口归一为内部 backend。
- Stop hook 一旦决定启动 RVF backend，必须在真正创建 Kanban task、follow-up 消息或 legacy GUI fallback 前执行 backend 对应的 provider health guard。Codex provider 必须先通过 `codex login status`，失败或显示过期时 fail-close 并提示用户运行 `codex login`；可用 `CODEX_RVF_PROVIDER_HEALTH_CHECK=0` 临时跳过，用 `CODEX_RVF_AUTO_CODEX_LOGIN=1` 在失败时尝试后台触发 `codex login`。
- 如果 Stop event 提供 transcript path，dispatcher 必须先生成 session manifest；只有当前 chat session 存在 `owned_dirty_paths` 时，才执行 dev sync、安装和 installed hook 转交。若 dirty repo 只有 `unattributed_dirty_paths` 或没有 session-owned dirty paths，dispatcher 应输出跳过 payload 并停止自动 review，避免把其他 session / agent 的 WIP 归到当前 session。
- dispatcher 同步只允许在 Stop event 的 git root 等于 `CODEX_RVF_DEV_REPO`、事件不是 subagent，且 session manifest 显示存在当前 session-owned dirty paths 时运行；同步失败必须输出不会触发模型续跑的 hook payload，并给出 `summary.json` 路径，避免继续使用 stale installed plugin skill。不要用非零 stderr 表达这类失败，因为 Codex Desktop 可能把 stderr 包成当前会话的 `<hook_prompt>` continuation。
- dev-only sync chain 必须只从 `CODEX_RVF_DEV_REPO` 解析并运行仓库级 `scripts/check_plugin_contracts.py` 与 `scripts/install_to_codex.py`；这些脚本和未来 dev-only helper 不得部署进 plugin runtime/cache。runtime 代码若需要开发期能力，只能经 `CODEX_RVF_DEV_REPO` subprocess 跨边界调用，不能 import 或复制 dev-only 模块。
- dispatcher、Stop hook、manual run、external reviewer 和 command lock 使用统一 run ledger：`state/latest.json` 只作为 pointer，真实排障入口是 `state/runs/<run_id>/summary.json` 和 `events.jsonl`；显式 suppress 的 Stop hook 也必须写入最小 ledger，并在用户可见 `systemMessage` 保留 `summary=<summary_path>`；prompt、review packet、stdout/stderr 等大文本必须作为 `artifacts/` 文件引用，不写入 hook stdout。可用 `CODEX_RVF_LOG_ROOT` 覆盖日志根目录，`CODEX_RVF_STATE_DIR` 只作为兼容别名；这些日志只服务排障，不是 review scope 证据本身。
- 默认 `CODEX_RVF_MODE=fork` 且 `CODEX_RVF_FORK_MODE=auto`：dirty gate 通过后，脚本会从 Stop event / 环境自动发现 backend；当前运行在 Kanban task 内并提供 `KANBAN_TASK_ID` / `CLINE_KANBAN_TASK_ID` / `task_id` 时走 `kanban-followup`，否则创建新的 Cline Kanban task。Codex GUI fork 是 legacy backup-of-backup：只有自动模式下 Cline Kanban task 启动失败，或用户显式配置 `CODEX_RVF_FORK_MODE=gui` 时才使用，并且 summary 必须标记 `legacy_gui_fallback`。
- 显式 `CODEX_RVF_FORK_MODE=cline-kanban`（别名 `cline` / `kanban` / `ck`）：dirty gate 通过后，脚本不调用 Codex GUI fork，也不后台启动隐藏 `codex exec`。hook 必须先运行 `prepare_review_run.py` 生成 review packet、session manifest、workspace snapshot，并额外冻结 worktree bootstrap artifact：`worktree-bootstrap.patch`、`worktree-bootstrap-files/`、`worktree-bootstrap.json`；这些 bootstrap artifact 只允许包含当前 session-owned dirty paths，遇到无法安全重放的情况必须 fail-close，不启动 Kanban task。hook 随后用 `scripts/cline_kanban_client.py` 调用官方 `kanban` CLI：先 `kanban task list --project-path <repo>` 检查服务可用，失败时通过 tmux 启动 `CODEX_RVF_CLINE_KANBAN_START_CMD`（默认 `npx -y kanban@0.1.66 --no-open`，session 默认 `rvf-cline-kanban`，等待默认 `90` 秒），再 `kanban task create --project-path <repo> --base-ref <base> --prompt <generated-prompt>` 和 `kanban task start --project-path <repo> --task-id <id>`。RunLedger summary 必须写入 `cline_kanban_task_id`、`workspace_path`、`cline_kanban_base_ref`、`worktree_bootstrap_path` 和 task prompt artifact。Kanban task prompt 必须先在 task worktree 内解析 repo root，设置 `RVF_RUN_DIR` / `RVF_ARTIFACTS_DIR` / `CODEX_RVF_LOG_ROOT` / `CODEX_RVF_RUN_ID` / `CODEX_RVF_RUN_DIR`，source `$RVF_ARTIFACTS_DIR/review-env.sh`，再把 `RVF_REPO` 覆盖为 task worktree root，并用 `$RVF_WORKTREE_BOOTSTRAP` 调用 `scripts/apply_worktree_bootstrap.py`。后续说明必须继续复用 `$RVF_REVIEW_PACKET`、`$RVF_SESSION_MANIFEST`、`$RVF_WORKTREE_BOOTSTRAP`、`$RVF_ARTIFACTS_DIR/handoff.md` 等变量，执行完整 `$review-validate-fix` 并维护 handoff；不要在同一个 Kanban prompt 里反复展开 `state/runs/<run_id>/artifacts/...` 的绝对路径，也不要在 task worktree 里新建 `prepare-run` run。
- 显式 `CODEX_RVF_FORK_MODE=kanban-followup`（别名 `kanban-message` / `kanban-inject`）：dirty gate 通过后，脚本不调用 Codex GUI fork，也不创建新的 Kanban task。它要求当前 Stop 事件或环境提供 `KANBAN_TASK_ID`（或兼容的 `CLINE_KANBAN_TASK_ID` / `task_id`），然后通过 `scripts/cline_kanban_client.py message` 调用定制的 `kanban task message --project-path <repo> --task-id <id> --prompt-file <prompt> --source review-validate-fix --idempotency-key <run_id>`。该 Kanban CLI 必须走 host 的真实 user-message channel，等价于把一条 follow-up 用户消息送入当前 task 的 active coding-agent chat session；不得降级为 card activity、task metadata、hook context、`contextModification` 或 system message。注入 prompt 必须包含 `RVF_KANBAN_FOLLOWUP_TRIGGER`，Stop hook 只把 latest user message 中的该 marker 作为 one-shot 递归保护；不要把它加入全 transcript suppress。注入失败必须 fail-close 并报告 `kanban_followup_unavailable` 或 `kanban_followup_missing_task_id`，不得 fallback 到 continuation、新 task 或 GUI fork。
- Cline Kanban 模式应利用每张卡独立 git worktree、Kanban diff viewer/checkpoints/inline comments、Commit/Open PR 交付入口和可选 auto-review。默认不自动提交、不自动开 PR；只有 `CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED=1` 时才把 `CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE`（`commit` / `pr` / `move_to_trash`）传给 Kanban。推荐把 Kanban 的 Codex agent command 配为 `kanban hooks codex-wrapper --real-binary <codex>` 以显示更细的 Codex activity；未配置时仍允许创建和启动 task。
- 停止正在运行的 Cline Kanban RVF run 时，读取 `references/cancel-rvf-run.md` 并使用 `scripts/cancel_rvf_run.py`；用户主动停止必须调用 `kanban task trash --task-id <id>`，标为 `cancelled`，不得把 `SIGTERM` 或 negative return code 记为 `failed`。
- 如果 legacy GUI fallback 需要使用 app-server，且 Desktop control socket 不可用，默认 `CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY=auto` 会优先复用可连通的 RVF bridge app-server，必要时尝试启动 bridge app-server，并在 summary 中标记 `socket_source=bridge` / `gui_visibility=unverified-bridge-only`。如果复用 bridge 后 fork/turn 返回配置加载或权限类 app-server 错误，hook 必须只终止 RVF 专用 socket 上的旧 bridge listener、重启 bridge 并重试一次。不要回退为 Stop continuation prompt：它不会创建真正的新用户 prompt，只会作为 hook system context 出现在当前轨迹中，容易误导主会话重新运行流程。需要禁止 bridge fallback 时，显式设置 `CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY=report|manual|fail`。
- `CODEX_RVF_MODE=continuation` 已废弃：脚本只报告 continuation fallback 已禁用和 GUI fork 创建失败，不再返回 `decision: "block"`，也不再注入 `$review-validate-fix` prompt。
- `CODEX_RVF_FORK_MODE=manual` 或 `dry-run` 只用于调试 prompt / app-server request 生成，不启动 Terminal。
- fork 诊断必须使用 `scripts/diagnose_codex_fork.py` 手动运行；`RVF_FORK_EXPERIMENT` 不得作为 Stop hook 主路径里的自动 fork 分支。
- `CODEX_RVF_MODE=off`：dirty gate 通过也只输出 systemMessage 并跳过自动触发。
- 当前 chat session 可用显式用户消息行管理 hook：`RVF_STOP_HOOK: off` 会把本 session 标记为 disabled，后续 Stop hook 对同一 session 只跳过 RVF fork/review gate；它不阻止 dispatcher 在本 RVF 源仓库主会话、且 transcript 显示当前 session 拥有 dirty 改动时先检查并安装当前 plugin。`RVF_STOP_HOOK: on` 会清除该 session 标记并恢复，且本次 Stop event 会继续走正常 dirty/session scope/backend gate；如果本 session 仍有 session-owned dirty scope，它可以在同一轮创建 GUI fork 或 Cline Kanban task。`RVF_STOP_HOOK: status` 只报告当前 session 状态。这些 session 状态写入 `state/session-hook/`，只影响当前 chat session 的 RVF 执行 gate，不修改全局 `~/.codex/hooks.json`。这些 `RVF_STOP_HOOK:*` 行只属于 Stop hook 会话控制元数据；除非用户明确申明其诉求与对RVF_STOP_HOOK本身的分析、更新有关，主会话或 fork 会话可以将其忽略。
- 手动 `$review-validate-fix` 完成后，主会话应在同一个 `state/session-hook/<session-id>.json` 里写入 `manual_rvf_completed_at` 和 `manual_rvf_run_id`。后续 Stop hook 仍允许 dispatcher/dev sync 运行，但 installed hook 看到同 session 的 marker 后必须只跳过 RVF 主 workflow，并返回 `manual_rvf_already_ran`；不要把这个 gate 混同为 `CODEX_RVF_SUPPRESS_STOP_HOOK`，后者仍表示跳过整个 hook。
- 如果 `stop_hook_active=true`，必须直接跳过，避免 Stop hook 或 fork 递归。
- 如果 Stop 事件来自 Codex subagent，必须直接跳过。post-work review 只能由主会话显式触发；研究、review、validate/fix 等子代理结束时不得被 Stop hook 拖入新的 `$review-validate-fix` fork。
- 如果环境变量 `CODEX_RVF_SUPPRESS=1` 或 `CODEX_RVF_SUPPRESS_STOP_HOOK=1`，必须直接跳过；该开关用于 research marathon 等主会话已接管调度的场景。
- Stop hook matcher 当前不能按 repo 过滤，因此脚本必须先使用 `scripts/review_validate_fix_gate.sh` 做 dirty gate。
- 如果当前 `cwd` 不在任何 git repo/worktree 内，Stop hook 不得扫描 `cwd` 的子目录、`~/.codex/config.toml` trusted projects 或其他候选 repo 来猜目标；必须 fail-safe 跳过，并通过 `systemMessage` 要求主会话询问用户提供目标 repo 路径。
- fork prompt 必须包含目标仓库路径、`RVF_FORKED_REVIEW_VALIDATE_FIX`、父 thread/session id、父 cwd，以及 `RVF_PARENT_CONVERSATION_NAME` / `RVF_PARENT_CONVERSATION_REF`、`RVF_PARENT_CONVERSATION_NAME_SOURCE`、`RVF_PARENT_CODEX_URL`、`RVF_PARENT_TRANSCRIPT_PATH`、`RVF_ORIGIN_METADATA` 等 origin metadata，让新 fork 能在正确仓库执行、在结束时识别自身，并在 handoff 中反查原始 Codex chat；legacy GUI fork 也不得把 `RVF_PARENT_SESSION_ID` 当成 conversation name source。
- 如果 Stop 事件提供 `transcript_path` / `session_path` / `conversation_path`，app-server fork 必须优先用该 rollout path 调用 `thread/fork`；Desktop 暴露的环境 thread id 可能不是可由外部 app-server 直接索引的 saved session id。
- 新 fork 会话或手动 RVF 会话结束时，如果 Stop 事件的 `last_assistant_message` 或 transcript 末尾包含 `RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，hook 必须在 dev sync gate 和 dirty/fork gate 前把该 markdown 文件作为完成信号处理，默认自动打开它，并停止后续自动 fork。这是兜底 advisory；完整手动 run，尤其是 Cline Kanban native task 内的手动 `$review-validate-fix`，不能只依赖 Stop hook，必须在最终回复前显式调用 `scripts/rvf_handoff.py open <handoff.md 绝对路径>`。可用 `CODEX_RVF_OPEN_HANDOFF=0` 关闭自动打开；可用 `CODEX_RVF_IDE_OPEN_CMD` 指定 coding agent IDE 打开命令，未设置时使用系统默认打开方式。
- app-server fork 会在 Stop 事件提供 `model` 时显式传入 model，并从 Stop 事件、`CODEX_RVF_FORK_REASONING_EFFORT` 或 `~/.codex/config.toml` 的 `model_reasoning_effort` 推出 reasoning effort 后在 `turn/start` 中传入。若父会话使用了 hook 不可见的临时 reasoning override，则无法完全保证继承。

## Setup-only 资源

- `setup/mcp-setup-startup.md` 不是运行期 reference，只用于用户明确要求配置或重配 santa-method alternative reviewer / MCP / agent 集成时。
- setup agent 必须通过 `scripts/read_mcp_setup_once.sh` 读取该文件；脚本会写入 `state/mcp-setup-startup.viewed`，marker 存在时默认只返回“已读取过”的提示。
- 只有用户明确要求重新 setup、更换 alternative reviewer，或排查 alternative reviewer 配置 drift 时，才可用 `scripts/read_mcp_setup_once.sh --force` 重新读取。
- 正常执行 `$review-validate-fix`、Stop hook 自动化、review、merge、validate/fix、handoff 时，不得读取、引用或总结 `setup/mcp-setup-startup.md`。

## Review

- review pass 的 `pass_type` 永远是 `review_only`。无论它由 full 流程派生、由用户单独要求只读 review，还是出现在研究马拉松 checkpoint 中，都必须停在 canonical review result artifact；不得把自己升级为完整 `$review-validate-fix` 流程。
- RVF 使用 `references/review-standards/` 中的定制 Review Standards Pack。它提炼 `code-review-and-quality`、`code-simplification`、`security-and-hardening`、`performance-optimization` 的适用子集，但不采用原版 agent-skills 的 report/checklist 输出格式。主会话可读取完整 pack；reviewer 默认读取 `reviewer.md` 和按需专项 subset；validate/fix 默认读取 `validate-fix.md` 和 assigned issue 相关 subset。
- 默认执行 santa-method double review：始终并行启动两个独立 review pass。
- 如果用户显式要求跳过 review：
  - 不启动 Codex reviewer、alternative reviewer 或 Codex-only fallback。
  - 不读取 `references/review-prompt.md`，不执行 review merge，也不伪造 reviewer 来源。
  - 如果用户随 prompt 提供了明确 issue list，则把它们标为 `user-supplied-skip-review`，进入 Validate / Fix；这些 issue 仍必须逐项验证。
  - 如果用户没有提供 issue list，则跳过 Validate / Fix，直接进入最终汇总；handoff 默认仍开启，除非用户也显式关闭 handoff。
  - 最终汇总和 handoff 必须写明 `review_status: SKIPPED_BY_USER`。
- review 阶段优先使用能力隔离，而不是事后追责：
  - Codex-native reviewer 优先用探索型 agent；如果当前 Codex agent API 暴露工具或 capability allowlist，就保留读取、检索、shell/test 能力，不授予直接编辑、patch、repo 源文件写入、stage、commit 或 validate/fix 相关能力。允许 reviewer 通过 `$RVF_WRITE_REVIEW_RESULT` 写 `$RVF_REVIEW_RESULT`；这是 review protocol output，不是 fix/writeback。
  - 启动 Codex-native reviewer 子代理时，必须使用 clean context；不要继承父线程历史、`<subagent_notification>`、先完成 reviewer 的输出、主会话 commentary 或 validate/fix 结果。reviewer prompt 里只给目标 repo 绝对路径、`review_agent_context_file` / `review-env.sh`、同一份 `scope.contract.json`、scope-of-work、session manifest、review packet、command lock 入口、result writer/checker 和 reviewer-specific `RVF_REVIEW_RESULT`，并要求它在目标 repo 下读取文件和运行命令，不得默认落到 installed plugin skill 目录、临时目录、另一个 clone 或另一个 git worktree。
  - Codex-native reviewer prompt 应直接复用 `prepare_review_run.py` 生成的 `review_agent_context` 或让 reviewer 读取 `review_agent_context_file`，不要由主会话手写 export block。该生成块已经包含 repo、`review-env.sh` 加载命令、scope、manifest、packet、command lock、result writer/checker 和 result artifact 的变量化入口。启动多个 reviewer 时，主会话或 runner 必须先设置唯一 `RVF_REVIEWER_ID` 或直接覆盖 `RVF_REVIEW_RESULT`。
  - Reviewer 输出在所有 reviewer 完成或超时前不得注入另一路 reviewer 的上下文。每路 reviewer 的 prompt、stdout、stderr、normalized、`review-result.json`、`review-result.summary.json`、summary 应写到 `artifacts/reviewers/<reviewer-id>/`；普通 double-review 不把这些输出交给另一 reviewer。若用户明确要求分析 RVF 历史或 subagent 轨迹，则这些 artifact 可以作为该任务的输入。
  - 如果当前 Codex `spawn_agent` 接口没有显式 capability allowlist（只有 agent type / model / reasoning 等参数），不要把 prompt 当成硬沙箱；仍可让 reviewer 读取仓库、运行测试/lint/build，但必须在 prompt 中明确禁止直接写文件、修复、stage/commit 和 handoff。
  - external alternative reviewer 应允许读取仓库并运行测试命令；配置层面只剥离直接编辑/写入工具。不要因为 reviewer 需要 shell 或 repo cwd 就降级为 fallback。
- `alternative reviewer` 可以是用户配置的任意外部 coding agent（例如某个 CLI、MCP 暴露的 agent、IDE agent 或本地 wrapper）。不要在本 skill 中硬编码具体 vendor、模型名或命令名。
- 如果 `config/alternative-reviewer.json` 已配置且 `scripts/run_alternative_reviewer.py --check` 通过，则使用一个 Codex-native reviewer 加一个 `alternative-reviewer:<agent-name>`；需要确认认证/健康状态时优先用 `scripts/run_alternative_reviewer.py --preflight`，它会在配置了 `health_command` 时一并检查。对需要唤醒本机登录态的 CLI，可配置 `pre_run_health: true` 让 runner 在正式调用 reviewer 前静默执行一次 health command。运行 external reviewer 时用 `scripts/run_alternative_reviewer.py --repo <repo> --review-packet <packet> --session-context <file>`，让 reviewer 能结合 packet 与本地测试结果审查；runner 会注入并校验 `RVF_REVIEW_RESULT`。
- external alternative reviewer 仍允许运行测试、lint、typecheck、build 或复现命令。不要把“可能产生测试缓存/报告/临时文件”误当成禁止运行命令的理由。
- external alternative reviewer 默认必须自行完成审查；除非本轮 prompt 明确要求等待人工步骤，否则不要期待开发者手动运行命令、提供额外操作或协助它完成 review。
- external alternative reviewer 的等待机制是可观测活动空闲超时：`scripts/run_alternative_reviewer.py` 从 `config/alternative-reviewer.json` 读取 `idle_timeout_seconds`、`activity_check_interval_seconds`、可选 `activity_probe_command` / `activity_probe_timeout_seconds` 与可选 `max_runtime_seconds`；默认配置每 5 秒检查一次 stdout/stderr 是否有新活动，连续 300 秒没有可观测活动时先结合 probe 判断 liveness，只有 probe inactive、probe 多次失败、进程退出或超过 max runtime 才终止该 reviewer，返回 exit code `124` 并输出 `RVF_EXTERNAL_REVIEWER_TIMEOUT ...`。probe 输出只写入 liveness metadata 和 probe history，不进入 review scope、review packet 或 issue merge。默认不设置总运行时上限；如果本机配置确实需要总上限，应使用宽松的一小时级别限制。对支持事件流的外部 CLI，配置层应优先启用事件流输出刷新活动时间；Claude stream-json 中已开始但尚未返回 tool_result 的 Bash 工具调用视为正在等待长命令运行，不按普通静默期超时。stdout/stderr 只作为 diagnostic，canonical review outcome 来自 result artifact；除非用户要求 external-only fail-close，否则把真正超时、不可启动或缺少 valid artifact 的 external reviewer 视为不可用并走 Codex-only fallback。
- 对可能与主会话或另一个 reviewer 冲突的命令，优先使用 `scripts/command_lock.py --repo <repo> --name <stable-lock-name> -- <command ...>` 做 repo-scoped 锁保护。典型场景包括共享 dev server 端口、会写同一缓存/coverage/report 目录的长测试、包管理器安装/构建、会独占设备或全局资源的命令。command lock 会通过统一 run ledger 记录 `lock_wait_started`、`lock_acquired`、`lock_timeout` 与 `lock_released` 事件。
- 如果 reviewer 判断某个命令需要锁但当前 prompt 或环境没有提供可用锁，它必须通过 `$RVF_WRITE_REVIEW_RESULT lock-request --out "$RVF_REVIEW_RESULT" ...` 写 request artifact；这不是完成的 review 结果，主会话应提供锁包装后的命令或更新 prompt 后重试该 reviewer。不要把 lock request 合并为 bug finding。
- 如果 reviewer 需要专项标准、测量、受控子任务或缺失上下文，它可以通过 `$RVF_WRITE_REVIEW_RESULT standard-request`、`measurement-request`、`subtask-request` 或 `context-request` 写 request artifact；这些 request 不得与 clean 或 issue result 混写。默认由主会话满足、驳回或 spawn 子任务，并记录 run ledger 后让 reviewer 重试。只有平台能继承 run id、scope、manifest、packet 和 no-handoff/no-review-loop 约束时，才允许最多一层 nested subagent。
- 如果 alternative reviewer 未配置、配置未完成、命令不可用或本轮无法启动，默认使用 Codex-only fallback；不要询问用户、不要中断 review loop、不要降级为单 reviewer。
- Codex-only fallback 必须并行启动两个 Codex-native 子代理模拟 santa-method：两个子代理使用同一份 review prompt、同一个 scope-of-work 文件路径和同一份 review packet 路径，彼此不看对方输出，并用 `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b` 作为来源标签。
- 只有用户在本轮明确要求必须使用外部 alternative reviewer、且不接受 Codex-only fallback 时，才因 alternative reviewer 不可用而 fail-close。
- 两个 reviewer 使用同一份 review prompt、同一个 scope-of-work 文件路径、同一份 session manifest 文件路径（如果有）和同一份 review packet 路径，但彼此不看对方输出。主会话不要把同一大段 scope 文本分别粘贴给两个 reviewer；把文件路径或本轮 `RVF_*` 变量交给它们读取即可，减少 prompt 重复。scope-of-work / session context 是主会话对本 turn 已完成工作的交接说明；session manifest 是机器提取的 ownership anchor；reviewer 应结合它们判断 intent/scope，再用 packet、diff、status、文件读取和验证命令独立核实。reviewer 的默认假设是：另一个独立 reviewer 可能正并行工作；因此命令需按锁规则协调。reviewer 的审查范围以 session manifest 的 owned paths 和主会话提供的 scope-of-work 为准，不得把整个 `git diff HEAD` 当作 full-scope analysis 来源；除非主会话明确要求 full diff review，否则只审查 scope 内改动及其直接连带影响。
- 完成态 Review artifact 契约必须严格为：
  - 无问题：`kind: no_issues`。
  - 有问题：`kind: issues`，每条含 `path`、`line`、`message`。
- 非完成态 request 契约为 `kind: request`，由主会话处理后重试；request 不得与 clean 或 issue result 混在同一个 artifact 中，也不得进入 merge table。
- 每个 reviewer result artifact 必须先用 `scripts/check_review_result.py` 或等价解析器校验；artifact 缺失、损坏、schema invalid、path 越界、excluded path、clean/issues/request 混合状态都不是合格 review result。reviewer final prose、stdout、stderr、中文化“没有问题”、handoff、validate/fix verdict 或修复说明都只作为 diagnostic，不作为 canonical result。
- 如果 reviewer artifact 是 `kind: request`，先满足、驳回、spawn 子任务或提供上下文，再重试 reviewer；重试后的 artifact 仍必须是完成态契约。request 本身不计入合格 double-review 来源。
- 如果 reviewer artifact 存在严重契约违规，可用同一 review packet 和新的 reviewer result path 重试一次并明确要求调用 `$RVF_WRITE_REVIEW_RESULT` 和 `$RVF_CHECK_REVIEW_RESULT`；再次违规则 fail-close，用中文询问用户如何处理，且不要把该 reviewer 当作合格 double-review 来源。
- review 前后可用 `scripts/workspace_snapshot.py capture/compare` 记录状态，尤其是 reviewer 会运行测试/lint/build 时。状态变化只表示 `WORKSPACE_CHANGED_DURING_REVIEW`，不推断 reviewer 主动编辑，也不自动使输出失格；主会话应检查变化是测试缓存/报告等可解释副作用，还是源文件、lockfile、snapshot 等需要人工处理的污染。不要自动 revert 用户或其他进程可能造成的改动。
- 详细 review prompt 见 `references/review-prompt.md`。
- 合并两个 reviewer 的 artifact 时读取 `references/review-merge-policy.md`：合并重复项、分组紧密相关 issue，并为每个 processed issue 记录来源 reviewer。

## Validate / Fix

- `kind: no_issues` 进入 clean path，handoff 默认仍开启，除非用户显式关闭 handoff。
- `kind: issues` 进入 validate/fix。
- artifact 缺失、损坏、schema invalid、纯 prose 或 final message 声称 clean 但 artifact 无效都 fail-close：用中文询问用户如何处理，不静默当作 0 个问题。
- 每条 issue 必须先验证，再决定：
  - `REAL`：真问题且可独立最小修复。
  - `FALSE_POSITIVE`：不成立，不改文件。
  - `ELEVATE`：真问题但需要用户决策，不改文件。
- 在默认 `full` 流程中，只要可解析 issue list 进入 validate/fix，主会话必须启动至少一个 `pass_type: validate_fix` 子代理处理验证包；不得因为问题看起来简单、修复明显、reviewer 已给出修复方向或主会话已经理解问题，就由主会话直接验证和修改文件。
- 只有在当前运行环境确实没有可用子代理接口、用户本轮明确要求主会话本地执行 validate/fix，或某个 validate/fix 子代理已返回 `REAL` 且只剩主会话可安全完成的机械收尾时，才允许本地执行；最终汇总和 handoff 必须写明本地执行原因。不能把“为了省时间”“问题很小”当成本地执行理由。
- 分配 validate/fix 子代理时按问题耦合度组织：不必一条 issue 一个 agent。共享根因、同一文件区域、同一测试路径或同一决策前提的问题，应合并成一个验证包交给同一个 validate/fix 子代理；验证包仍要逐项输出 verdict。
- 主会话必须为 validate/fix 分组保留一张审计表，不只把分组信息写进子代理 prompt。每个分组记录 `validation_group_id`、包含的 canonical issue / processed id、分组理由、分配给哪个 validate/fix 子代理；若触发允许本地执行的窄例外，还必须记录本地执行原因；以及逐项 verdict 汇总。
- 发给 validate/fix 子代理的 prompt 应包含同一份 `scope.contract.json` 路径和 `scope_hash`，把 `fix_allowlist` 当作默认可写范围。若最小真实修复确实需要修改 allowlist 外文件，先在回复中说明原因并让主会话决定是否扩大 scope。allowlist 外 dirty changes、并行 agent 新增文件、reviewer liveness/probe artifacts、背景 WIP 或 protected files 是预期存在的并行工作，不得清理、删除、格式化或顺手修复。
- 发给 validate/fix 子代理的 issue context 必须 source-agnostic：不要包含“Codex 发现”“alternative reviewer 发现”“两个 reviewer 都发现”等来源标签，也不要暗示哪个模型或 agent 支持该 issue。来源只保留在主会话的合并表 / handoff 中。
- validate/fix 子代理只处理主会话分配给它的 canonical issue 包；不得自行扩大 review 范围、重新执行 double review、生成 handoff 或处理未分配问题。
- validate/fix 子代理可以读取 `references/review-standards/validate-fix.md` 和 assigned issue 相关专项 subset。若缺少标准、测量、受控子任务或上下文，可输出纯 `RVF_*_REQUEST`；主会话处理后重试。request 不是 verdict，不得与 `REAL` / `FALSE_POSITIVE` / `ELEVATE` 混写。
- 所有 validate/fix 子代理完成后，主会话的最终中文汇总必须包含“Validate/fix 分组”小节，说明 reviewer 标记出的 processed issues 是如何被分配成验证包的：每组列出 group id、包含的问题、合并验证原因和结果统计。即使某组只有一条 issue，也要说明它为何独立验证。
- 详细 validate/fix prompt 见 `references/validate-then-fix-prompt.md`。

## ELEVATE

- `ELEVATE` verdict 必须附带 `elevation-detail` fenced block。
- 可用 `scripts/parse_elevation_detail.py` 做确定性解析；解析失败时降级为普通中文说明，并明确缺少哪些字段。

## Handoff

- 最终用中文汇总 flag 数、真实修复数、误报数、升级数；如果用户跳过 review，也要汇总 review 状态。若本轮进入 validate/fix，最终汇总还必须包含“Validate/fix 分组”小节，披露主会话如何把 reviewer 标记的问题分组成验证包，以及每组的结果。
- Handoff 默认开启：成功完成本 skill 时，必须在当前 RVF run 目录的 `artifacts/handoff.md` 创建并持续维护 markdown 交接文件。文件在 prepare/run 初始化后先写 pending 状态，review、merge、validate/fix、最终汇总阶段覆盖更新同一文件；最终回复前先调用 `scripts/rvf_handoff.py open <handoff.md 绝对路径>` 尝试用默认编辑器打开；最终回复第一行输出 `RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，后面只追加 1-3 句极短中文说明 reviewers 和 validate/fixers 的工作结果，不要重复 handoff 文件正文。模板见 `references/handoff-template.md`。
- 用户明确关闭 handoff、当前是 `pass_type: review_only` / `pass_type: validate_fix` 子 pass，或当前是 `mode: research_checkpoint_no_handoff` 时，最终回复只给当前任务要求的中文结果，不创建 handoff.md，也不要输出空模板或 `RVF_HANDOFF_FILE`。
- Handoff 内容只写你能确认的事实；不要把仓库里既有 WIP 或其他 session 的改动混成本 turn 的改动。
- 不要复用旧 Stop hook 里的“从 hook 触发点 time-travel”假设。当前 Codex fork 方案创建的是停止后的新 fork 用户 prompt checkpoint；它可作为回退边界，但不是聊天中任意内部 Stop 事件的真正 time-travel snapshot。

## Legacy Context

- 旧 slash command 原文：`references/legacy-claude-command.md`
- 旧 Stop hook 原文：`references/legacy-claude-stop-hook.md`
- 旧 activity hook 原文：`references/legacy-claude-mark-activity.sh`
- Codex session-scoped tracking 计划：`references/session-scoped-change-tracking-plan.md`
- 旧 hook handoff 兼容性说明：`references/legacy-compatibility-notes.md`
- 双 reviewer 合并策略：`references/review-merge-policy.md`

只在迁移、对照旧行为或排查历史 drift 时读取 legacy 文件；正常执行 review loop 优先使用本 skill 的 references 与 scripts。不要把 setup-only 资源当作 legacy 或运行期 reference。
