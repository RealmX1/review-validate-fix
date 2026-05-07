#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def render_startup_scope_text(
    *,
    cwd: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    prompt_path: str,
    run_id: str,
    run_dir: Path,
) -> str:
    transcript = str(parent_thread_path) if parent_thread_path is not None else "<unknown>"
    return (
        "# Scope of Work: Cline Kanban RVF startup\n\n"
        "本文件由 Stop hook 在创建 Cline Kanban task 前生成，用于冻结 task 启动时的 review 输入。\n\n"
        f"- 目标仓库：`{cwd}`\n"
        f"- parent session id：`{parent_session_id}`\n"
        f"- parent transcript path：`{transcript}`\n"
        f"- run id：`{run_id}`\n"
        f"- run dir：`{run_dir}`\n"
        f"- fork prompt：`{prompt_path}`\n\n"
        "Kanban task 的 scope 只能以本 run artifacts 中已经生成的 scope.contract.json 作为最终 scope contract；"
        "review packet、session manifest、workspace snapshot 和 worktree bootstrap 仅作为冻结证据、"
        "ownership evidence、tracker audit context 或 worktree 重放输入。不要在排队后用实时 worktree 重新定义 scope。"
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
