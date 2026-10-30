"""Hook 只提供一次发起决策；机器等待、诊断和清理不唤醒 Agent。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from core.stop_hook_output_audience import (
    AgentDirectiveStopHookOutput, render_stop_hook_payload,
    STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
)
from .persistent_artifacts import exclusive_lock, git, git_text, read_json, repository_identity, write_json
from adapters.cline_kanban.task_review_starting_point import resolve_task_review_starting_point


def handle_hook(event: dict, *, event_name: str, host_adapter) -> dict | None:
    if os.environ.get("RVF_SUPPRESS_STOP_HOOK") == "1":
        return None
    if event_name == "UserPromptSubmit":
        prompt = host_adapter.normalized_user_prompt_text(event.get("prompt", ""))
        task_id = os.environ.get('KANBAN_TASK_ID') or os.environ.get('CLINE_KANBAN_TASK_ID')
        if task_id and prompt.strip():
            try:
                resolve_task_review_starting_point(Path(event.get('cwd') or os.getcwd()), task_id,
                                                  event=host_adapter.user_prompt_recovery_event(event, prompt))
            except Exception as error:
                # 当前用户的显式入口独立于机器补证；诊断只进入开发者 stderr。
                print(f'RVF hook 补证诊断：{type(error).__name__}: {error}', file=sys.stderr)
        if not re.match(r"^\s*(?:\$|/|:)review-validate-fix(?:\s|$)", prompt):
            return None
        return host_adapter.user_prompt_context_payload(
            "本次用户明确调用 RVF。读取 review-validate-fix skill，使用固定任务基线和显式范围运行 "
            "rvf review-task-snapshot；复用已有精确 run 时使用 rvf resume-task-review。主会话直接验证与提交。")
    if event_name != "Stop" or not (os.environ.get("KANBAN_TASK_ID") or os.environ.get("CLINE_KANBAN_TASK_ID")):
        return None
    repo = Path(event.get("cwd") or os.getcwd())
    identity = repository_identity(repo)
    repo = Path(identity["repo"])
    task_id = os.environ.get('KANBAN_TASK_ID') or os.environ.get('CLINE_KANBAN_TASK_ID')
    evidence = resolve_task_review_starting_point(repo, task_id)
    if evidence.get('current_scope_reviewed'):
        return None
    dirty = git(repo, 'status', '--porcelain', '--untracked-files=all')
    if evidence['status'] == 'verified':
        if evidence['completion_evidence_status'] == 'unknown':
            print(f"RVF hook 完成覆盖诊断：{evidence.get('diagnostic', '等待补证')}", file=sys.stderr)
            if not evidence.get('new_content_since_unproven_completion'):
                return None
        from .active_run_observation import workspace_matches_registered_run
        if workspace_matches_registered_run(repo, task_id):
            return None
        if not dirty and evidence['committed_work_status'] != 'present':
            return None
        # 补齐创建来源不改变内容身份；incarnation 冲突仍由起点解析器核验。
        binding_identity = {key: value for key, value in evidence['binding'].items()
                            if key not in {'creation_commit', 'provenance_incarnation_id'}}
        fingerprint = hashlib.sha256(json.dumps({
            'binding': binding_identity, 'base_commit': evidence['base_commit'],
            'scope_paths': evidence['scope_paths'], 'worktree_tree': evidence['observed_worktree_tree'],
        }, sort_keys=True).encode()).hexdigest()
    elif dirty:
        from .active_run_observation import registered_task_run_directories, workspace_matches_registered_run
        if any(read_json(path / 'run.json').get('status') == 'completed' for path in registered_task_run_directories(identity, task_id)):
            print(f"RVF hook 完成关联诊断：{evidence.get('diagnostic', '等待补证')}", file=sys.stderr)
            return None
        if workspace_matches_registered_run(repo, task_id):
            return None
        changes = git(repo, "diff", "--binary", "--full-index", "--no-ext-diff", "HEAD")
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        for value in untracked.decode().split("\0"):
            if value:
                path = repo / value
                changes += value.encode() + b"\0" + (os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
        fingerprint = hashlib.sha256(git(repo, 'rev-parse', 'HEAD') + changes).hexdigest()
    else:
        return None
    key = hashlib.sha256((identity['git_directory'] + '\0' + task_id).encode()).hexdigest()
    state = resolved_rvf_user_local_data_paths().runtime_state_root / "task-review-decisions" / f"{key}.json"
    with exclusive_lock(state.with_suffix(".lock")):
        if state.exists() and read_json(state).get("candidate_fingerprint") == fingerprint:
            return None
        write_json(state, {"candidate_fingerprint":fingerprint})
    return render_stop_hook_payload(
        audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
        agent_directive=AgentDirectiveStopHookOutput(
            agent_directive_body="本任务存在改动。依据任务授权、风险及已有评审决定是否运行 RVF；需要时使用固定基线、明确任务路径和需求文件执行 rvf review-task-snapshot。已有 run 只接续缺失部分。保留背景 WIP；不需要评审时直接说明依据并完成当前交付。",
            developer_visible_notice="RVF：任务评审发起决策。"),
        developer_only_payload=None,
        agent_directive_channel=host_adapter.stop_hook_agent_directive_channel())
