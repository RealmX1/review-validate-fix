# Legacy Compatibility Notes

## 旧 Stop hook handoff 不兼容点

旧 `stop-review-validate-fix.sh` 的 handoff/time-travel 设计假设：Stop hook 触发后，用户可以从“hook 触发之前”的任意会话位置 fork 或回退，然后把 `<handoff-context>` 贴给 earlier-self。

这个假设不适用于 Claude Code 和 Codex 的原生设置：

- 可回退锚点是用户自己的输入位置。
- Stop hook 触发点是会话内部自动事件，不是用户输入锚点。
- 因此无法可靠回退到“Stop hook 刚触发前”这个任意内部位置。

迁移结论：

- 不要复活旧 Stop hook handoff 脚本。
- 不要把旧 hook 的 handoff 机制当作可操作的 time-travel 功能迁入 Codex skill。
- 可以保留 `<handoff-context>` 作为人工可读、可复制的上下文压缩格式，用于用户在可回退的输入边界或新会话中继续工作。
- 如果未来需要自动 review hook，必须重新设计锚点模型，而不是恢复旧 hook 的任意触发点回退假设。

## Codex Stop fork 兼容方案

当前默认方案不是恢复旧 Claude hook 的任意内部 time-travel。Codex Stop hook 现在默认通过 Codex app-server 创建 GUI fork：先发 `thread/fork`，再在 fork 出来的新会话中用 `turn/start` 提交以 `$review-validate-fix` 开头的新用户 prompt。新 prompt 带上 `RVF_FORKED_REVIEW_VALIDATE_FIX`、父 thread/session id、父 cwd 和目标 repo。

这个新 fork 会话提供一个真实的用户输入 checkpoint，可作为回退边界；但 checkpoint 位于“父会话完整停止之后 fork 出来的新 prompt”，不是 Stop hook 触发前任意内部事件的 snapshot。

不再使用 Terminal 或 `codex fork <session-id>` 作为自动路径：实测 `codex fork` 会启动 TUI 前端，同时可能在 Codex GUI 中显示同一 fork，形成双前端；另一些 Desktop Stop event 暴露的 session id 又不能被 CLI 会话索引找到，会在 Terminal 中报 `No saved session found`。

新 fork 会话完成 `$review-validate-fix` 后，如果 Stop 事件的 `last_assistant_message` 已包含 `<handoff-context>`，Stop hook 会通过 systemMessage 程序化提示用户复制最终回复里的 handoff block，再粘贴回原始 chat session。这个提示不由 agent 正文生成。

Codex Stop continuation 不再作为 fallback：设置 `CODEX_RVF_MODE=continuation` 时，hook 只报告该 fallback 已禁用和 GUI fork 创建失败，不再使用 `decision: "block"` 创建同线程 continuation prompt。实测该内容不会成为真正的新用户 prompt，只会作为 Stop hook system context 出现在轨迹中，容易误导主会话重复运行流程。

实现约束：

- hook 只做 gate、app-server fork/turn 注入和结束提示，不直接执行 review/fix。
- `stop_hook_active=true` 时必须跳过，避免递归。
- 当前 `cwd` 位于 dirty git repo/worktree 内时才直接触发；如果 `cwd` 不在任何 git repo/worktree 内，必须 fail-safe 跳过并要求主会话询问用户提供目标 repo 路径。
- 不扫描 `cwd` 子目录、trusted projects 或其他候选 repo 来猜测目标，避免审错仓库。
- app-server fork 优先使用 Stop event 暴露的 rollout path；只有没有可用 path 时才退回 thread/session id。
- 正式 review fork 不设置 `CODEX_RVF_SUPPRESS_STOP_HOOK=1`，否则结束时无法发出 handoff 复制提示；实验 fork 可以设置该 suppress 标记。
- `CODEX_RVF_FORK_MODE=gui` 是默认自动路径；`manual` / `dry-run` 只用于调试 prompt 与 app-server request。Terminal/CLI fork 自动启动已禁用。
