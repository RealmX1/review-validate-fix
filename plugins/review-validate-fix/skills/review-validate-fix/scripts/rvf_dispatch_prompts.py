#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


KANBAN_TASK_FLOWS = frozenset({"flow-2-branch", "flow-2-inplace"})
SELF_RISING_FLOWS = frozenset({"flow-1-self-rising"})
LEGACY_FORK_FLOWS = frozenset({"flow-3-inplace"})
MANUAL_FLOWS = frozenset({"flow-manual"})


def dispatch_scope_of_work_text(
    *,
    target_flow: str,
    cwd: str,
    parent_session_id: str | None,
    parent_thread_path: Path | None,
    prompt_path: str | None,
    run_id: str,
    run_dir: Path,
    user_prompt_excerpt: str | None = None,
) -> str:
    transcript = str(parent_thread_path) if parent_thread_path is not None else "<unknown>"
    parent_session = parent_session_id or "<unknown>"
    prompt_ref = prompt_path or "<not-applicable>"
    if target_flow in KANBAN_TASK_FLOWS:
        return (
            "# Scope of Work: Cline Kanban RVF startup\n\n"
            "本文件由 Stop hook 在创建 Cline Kanban task 前生成，用于冻结 task 启动时的 review 输入。\n"
            "post-user-prompt hook 在目标 task session 收到首条带 dispatch token 的 prompt 时会调用 shared "
            "prepare 入口；本 scope-of-work 是 prepare 的 session-context 输入。\n\n"
            f"- target_flow：`{target_flow}`\n"
            f"- 目标仓库：`{cwd}`\n"
            f"- parent session id：`{parent_session}`\n"
            f"- parent transcript path：`{transcript}`\n"
            f"- run id：`{run_id}`\n"
            f"- run dir：`{run_dir}`\n"
            f"- fork prompt：`{prompt_ref}`\n\n"
            "Kanban task 的 scope 只能以本 run artifacts 中已经生成的 scope.contract.json 作为最终 scope contract；"
            "review packet、session manifest、workspace snapshot 和 worktree bootstrap 仅作为冻结证据、"
            "ownership evidence、tracker audit context 或 worktree 重放输入。不要在排队后用实时 worktree 重新定义 scope。"
        )
    if target_flow in SELF_RISING_FLOWS:
        return (
            "# Scope of Work: Cline Kanban followup self-rising RVF\n\n"
            "本文件由 Stop hook 在向当前 Cline Kanban task session 注入 followup user message 前生成，"
            "post-user-prompt hook 在目标 session 收到该消息时会调用 shared prepare 入口。\n\n"
            f"- target_flow：`{target_flow}`\n"
            f"- 目标仓库：`{cwd}`\n"
            f"- source session id：`{parent_session}`\n"
            f"- source transcript path：`{transcript}`\n"
            f"- run id：`{run_id}`\n"
            f"- run dir：`{run_dir}`\n\n"
            "Followup 在原 task worktree 内运行；scope 应覆盖该 task 自上次 RVF 以来的累计 dirty work。"
            "shared prepare 完成后再由 agent 在 SKILL.md 指引下补充实质性 reasoning 内容。"
        )
    if target_flow in LEGACY_FORK_FLOWS:
        return (
            "# Scope of Work: legacy GUI/app-server RVF fork\n\n"
            "本文件由 Stop hook 在显式 legacy GUI fork 路径下生成。post-user-prompt hook 在 fork session 收到首条 prompt 时调用 shared prepare 入口。\n\n"
            f"- target_flow：`{target_flow}`\n"
            f"- 目标仓库：`{cwd}`\n"
            f"- parent session id：`{parent_session}`\n"
            f"- parent transcript path：`{transcript}`\n"
            f"- run id：`{run_id}`\n"
            f"- run dir：`{run_dir}`\n"
            f"- fork prompt：`{prompt_ref}`\n"
        )
    if target_flow in MANUAL_FLOWS:
        excerpt = (user_prompt_excerpt or "").strip()
        if excerpt:
            excerpt_block = "```\n" + excerpt + "\n```\n\n"
        else:
            excerpt_block = "_(user prompt excerpt unavailable)_\n\n"
        return (
            "# Scope of Work: manual RVF (post-user-prompt hook auto-prep)\n\n"
            "post-user-prompt hook 在用户显式触发 `/review-validate-fix` 等手动入口、且 prompt 缺少其他 dispatch origin marker 时自创本 prep file。"
            "Hook 已自动调用 shared prepare 入口生成 review-env / scope.contract / review packet。\n\n"
            f"- target_flow：`{target_flow}`\n"
            f"- 目标仓库：`{cwd}`\n"
            f"- session id：`{parent_session}`\n"
            f"- transcript path：`{transcript}`\n"
            f"- run id：`{run_id}`\n"
            f"- run dir：`{run_dir}`\n\n"
            "本文件是 hook 自动生成的最小 stub。Agent 必须在 reviewer dispatch 前用真实 reasoning 内容覆盖本文件，"
            "或新写一份 scope-of-work 并以 `prepare_review_run.py --session-context` 重新跑 prepare。\n\n"
            "User prompt 摘录：\n\n" + excerpt_block
        )
    return (
        "# Scope of Work: RVF dispatch (unspecified flow)\n\n"
        f"- target_flow：`{target_flow}`\n"
        f"- 目标仓库：`{cwd}`\n"
        f"- session id：`{parent_session}`\n"
        f"- transcript path：`{transcript}`\n"
        f"- run id：`{run_id}`\n"
        f"- run dir：`{run_dir}`\n"
    )


def cline_kanban_artifact_reference_lines() -> str:
    return (
        "然后读取并复用已经冻结的 RVF artifacts；命令和说明中继续使用这些变量，不要重复展开 run artifacts 目录：\n"
        "- review env: `$RVF_ARTIFACTS_DIR/review-env.sh`\n"
        "- review agent context: `$RVF_ARTIFACTS_DIR/review-agent-context.md`\n"
        "- scope contract: `$RVF_SCOPE_CONTRACT`\n"
        "- review packet: `$RVF_REVIEW_PACKET`\n"
        "- session manifest: `$RVF_SESSION_MANIFEST`\n"
        "- worktree bootstrap: `$RVF_WORKTREE_BOOTSTRAP`\n\n"
        "不得用 Kanban worktree 当前实时 diff 重新定义 scope；review scope 只能以 `$RVF_SCOPE_CONTRACT` "
        "为准，review packet 仅作为冻结 reviewer 输入，session manifest 只作为 ownership evidence 和 tracker 审计来源。"
    )
