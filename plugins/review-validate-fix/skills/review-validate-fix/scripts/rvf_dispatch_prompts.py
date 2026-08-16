#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


KANBAN_TASK_FLOWS = frozenset({"flow-2-branch", "flow-2-inplace"})
SELF_RISING_FLOWS = frozenset({"flow-1-self-rising"})
LEGACY_FORK_FLOWS = frozenset({"flow-3-inplace"})
MANUAL_FLOWS = frozenset({"flow-manual"})
AGENTIC_INITIATION_FLOW = "flow-agentic-initiation"
AGENTIC_INITIATION_FLOWS = frozenset({AGENTIC_INITIATION_FLOW})

SCOPE_OF_WORK_STUB_MARKER = "<!-- rvf:scope-of-work-stub -->"
"""机器可判的 stub 标记。

dispatch 的 contract gate 据它拒派「agent 还没写真实 scope-of-work」的轮次。用显式标记而不是
「猜正文长得像不像 stub」：后者会在 agent 只补了一行时误判成已写好，而这一步一旦漏判，
reviewer 拿到的就是一份自说自话的骨架，整轮评审白跑（2026-08-15 run 实测浪费约 13 分钟）。
"""

SCOPE_OF_WORK_STUB_MARKER_HEAD_LINE_LIMIT = 20
"""标记只在文档**顶部**这么多行内才算数（模板把它渲在第 3 行）。"""


def scope_of_work_is_untouched_stub(text: str | None) -> bool:
    """这份 scope-of-work 是否仍是 hook 生成、agent 未覆盖的 stub。

    判据 = **顶部若干行内一整行只有这个标记**，而不是全文 substring。两点理由：

    1. 与 stub 正文给 agent 的指引对齐。正文写的是「去掉本文件**顶部**的 stub 标记行」，同时又
       把同一个字面量用反引号复述在正文里指认要删哪一行。全文 substring 判定下，严格照指引操作
       的 agent 删掉顶部标记后仍被拒派 —— 指引与判据自相矛盾，agent 无路可走。
    2. 正文**引用**这个标记不等于这份 scope-of-work 还是 stub。本仓自审时 scope-of-work 里就会
       出现这个字面量（正在改的就是它），全文 substring 会把一份写满实质内容的正文误判成 stub。
    """
    if not isinstance(text, str):
        return False
    head = text.splitlines()[:SCOPE_OF_WORK_STUB_MARKER_HEAD_LINE_LIMIT]
    return any(line.strip() == SCOPE_OF_WORK_STUB_MARKER for line in head)


def _candidate_skeleton_block(candidate_paths: list[str] | None) -> str:
    """把决策点候选摘要的路径列成骨架，供 agent 逐条填实质 reasoning。"""
    paths = [p for p in (candidate_paths or []) if isinstance(p, str) and p.strip()]
    if not paths:
        return "- _(决策点未带候选路径；请自行列出本轮改动并逐条说明)_\n"
    shown = paths[:40]
    lines = "".join(f"- `{path}` —— _(待填：这处改了什么、为什么、审的时候该盯哪里)_\n" for path in shown)
    if len(paths) > len(shown):
        lines += f"- _(另有 {len(paths) - len(shown)} 个候选路径未列出，见 scope contract)_\n"
    return lines


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
    candidate_paths: list[str] | None = None,
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
    if target_flow in AGENTIC_INITIATION_FLOWS:
        # 这条路径的来源是 **Stop hook 的发起决策点 + agent 显式 start**，不是用户敲的手动入口。
        # 历史上它复用 flow-manual 模板，产出的 stub 自称「用户显式触发 /review-validate-fix」，
        # 与 prep payload 里的 dispatch_origin=agentic_initiation_start 直接矛盾——reviewer 读到
        # 的来源是假的（2026-08-15 run 实测）。
        return (
            "# Scope of Work: agentic RVF initiation (agent 经决策点显式 start)\n\n"
            f"{SCOPE_OF_WORK_STUB_MARKER}\n\n"
            "本文件由 `rvf-agent start` 在 agent 接受 Stop hook 的发起决策点后生成，用于冻结本轮 "
            "review 输入。**不是**用户手动触发，也**不是**任何 followup/fork 派发。\n\n"
            f"- target_flow：`{target_flow}`\n"
            f"- 目标仓库：`{cwd}`\n"
            f"- session id：`{parent_session}`\n"
            f"- transcript path：`{transcript}`\n"
            f"- run id：`{run_id}`\n"
            f"- run dir：`{run_dir}`\n\n"
            "## 待填：本轮改了什么、为什么\n\n"
            + _candidate_skeleton_block(candidate_paths)
            + "\n"
            "**dispatch 前必须把上面的骨架填成真实 reasoning 内容，并删掉本文件顶部那一整行 stub 标记**"
            f"（`{SCOPE_OF_WORK_STUB_MARKER}`，就是标题下面单独成行的那一行；本段这句里的引用不必动，"
            "判据只认顶部单独成行的标记）——`rvf-agent dispatch` 有 contract gate，"
            "stub 未被覆盖会直接拒派，不会让 reviewer 空审一轮。\n"
            "改完后用 `rvf-agent scope rerender-scope-of-work --token <token>` 把新正文重渲进 "
            "review packet：packet 在 start 时就内联冻结了这份 stub，光改盘上的文件不会进 packet"
            "（重渲会把旧 packet 归档到 `scope-of-work-rerender-rounds/`，冻结输入仍可审计）。\n"
        )
    if target_flow in MANUAL_FLOWS:
        excerpt = (user_prompt_excerpt or "").strip()
        if excerpt:
            excerpt_block = "```\n" + excerpt + "\n```\n\n"
        else:
            excerpt_block = "_(user prompt excerpt unavailable)_\n\n"
        return (
            "# Scope of Work: manual RVF (post-user-prompt hook auto-prep)\n\n"
            f"{SCOPE_OF_WORK_STUB_MARKER}\n\n"
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


def cline_kanban_artifact_reference_lines(
    *,
    parent_conversation_context_ref: str | None = None,
) -> str:
    parent_context_line = ""
    parent_context_hint = ""
    if parent_conversation_context_ref:
        parent_context_line = (
            f"- 父会话对话 context: `{parent_conversation_context_ref}`（仅作背景，可在 review 前先读）\n"
        )
        parent_context_hint = (
            "本 child 是全新会话、看不到父会话对话，故 review 前可先读父会话对话 context "
            f"`{parent_conversation_context_ref}` 了解脉络；它仅作背景，"
            "**不得**用它重定义 scope（scope 仍以 `$RVF_SCOPE_CONTRACT` 为准）。"
        )
    return (
        "然后读取并复用已经冻结的 RVF artifacts；命令和说明中继续使用这些变量，不要重复展开 run artifacts 目录：\n"
        "- review env: `$RVF_ARTIFACTS_DIR/review-env.sh`\n"
        "- review agent context: `$RVF_ARTIFACTS_DIR/review-agent-context.md`\n"
        "- scope contract: `$RVF_SCOPE_CONTRACT`\n"
        "- review packet: `$RVF_REVIEW_PACKET`\n"
        "- session manifest: `$RVF_SESSION_MANIFEST`\n"
        "- worktree bootstrap: `$RVF_WORKTREE_BOOTSTRAP`\n"
        f"{parent_context_line}"
        "\n"
        "不得用 Kanban worktree 当前实时 diff 重新定义 scope；review scope 只能以 `$RVF_SCOPE_CONTRACT` "
        "为准，review packet 仅作为冻结 reviewer 输入，session manifest 只作为 ownership evidence 和 tracker 审计来源。"
        f"{parent_context_hint}"
    )
