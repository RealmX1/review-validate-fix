"""Codex Kanban 自动收尾桥：先证明原生分界，成功绑定 run 后才激活等待，完成后写持久通知。

不输出 hook 指令，不选择 latest run，不改写原生 transcript。完整候选 gate 仍由开审边界验证器负责。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

import cline_kanban_client as kanban
import _rvf_pyroot  # noqa: F401
from adapters.codex.review_start_independent_user_turn_evidence import codex_native_rewind_boundary_capability_is_verified
from core.review_start_boundary.review_start_boundary_verification import PENDING, INVALIDATED, ReviewStartBoundaryNotVerified


def rvf_home() -> Path:
    return Path(os.environ.get('RVF_HOME') or Path.home() / '.rvf').expanduser()


def _write_complete_json_without_overwriting(path: Path, value: Mapping[str, Any]) -> None:
    """先落完整文件，再原子挂名；并发与重试均不能覆盖已存在的完成事实。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.writing')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        if os.name != 'nt':
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def capture_before_start(payload: Mapping[str, Any], *, transcript_path: str | None, task_id: str | None) -> dict[str, Any] | None:
    if not task_id or not transcript_path:
        return None
    try:
        with Path(transcript_path).expanduser().open(encoding='utf-8') as transcript:
            first = json.loads(transcript.readline())
        metadata = first.get('payload', {}) if isinstance(first, dict) and first.get('type') == 'session_meta' else {}
    except (OSError, ValueError):
        metadata = {}
    if not isinstance(metadata, Mapping) or not codex_native_rewind_boundary_capability_is_verified(metadata):
        raise ReviewStartBoundaryNotVerified(
            PENDING, '此 Codex 版本或宿主形态的独立消息与原生回退能力尚未取证',
            reason_code='native_rewind_capability_unverified', recovery_action='repair_host_capability',
        )
    repo = Path(str(payload['worktree'])).resolve()
    task_cmd = os.environ.get('CODEX_RVF_CLINE_KANBAN_TASK_CMD') or kanban.DEFAULT_TASK_CMD
    # project-path 必须是 main checkout；不能把 task worktree 交给 Kanban 注册查找。
    result = kanban.run_command(['git', '-c', 'core.hooksPath=/dev/null', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=repo)
    project = Path(result.stdout.strip()).parent
    command = kanban.task_command(task_cmd, 'capture-rvf-review-start-boundary', '--project-path', str(project), '--task-id', task_id, '--decision-token', str(payload['token']))
    result = kanban.run_command(command, cwd=repo, env=kanban.runtime_env(kanban.task_runtime_port(task_cmd=task_cmd)), timeout=60, check=False)
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ReviewStartBoundaryNotVerified(
            PENDING, f'宿主捕获命令没有返回有效 JSON（exit {result.returncode}）：{result.stderr.strip()}',
            reason_code='host_cli_response_invalid', recovery_action='repair_host_capability',
        ) from exc
    if isinstance(response, dict) and response.get('status') in {'pending', 'invalidated'}:
        raise ReviewStartBoundaryNotVerified(
            INVALIDATED if response['status'] == 'invalidated' else PENDING,
            str(response.get('reason') or '宿主尚未证明独立开审分界'),
            reason_code=str(response.get('reason_code') or 'boundary_evidence_unverified'),
            recovery_action=str(response.get('recovery_action') or 'inspect_boundary_diagnostic'),
        )
    if result.returncode or not isinstance(response, dict) or response.get('status') != 'captured' or not isinstance(response.get('receipt'), dict):
        raise ReviewStartBoundaryNotVerified(
            PENDING, f'宿主捕获命令未成功提供完整 receipt（exit {result.returncode}）：{result.stderr.strip()}',
            reason_code='host_cli_response_invalid', recovery_action='repair_host_capability',
        )
    receipt = response['receipt']
    receipt_worktree = receipt.get('worktree')
    if receipt.get('decision_token') != payload['token'] or receipt.get('session_id') != payload['session_id'] or not isinstance(receipt_worktree, str) or not receipt_worktree or Path(receipt_worktree).resolve() != repo:
        raise ReviewStartBoundaryNotVerified(
            INVALIDATED, '宿主返回的 RVF 分界身份不一致',
            reason_code='captured_boundary_identity_mismatch', recovery_action='inspect_boundary_diagnostic',
        )
    return receipt


def bind_run(receipt: Mapping[str, Any] | None, run_dir: Path, *, run_id: str) -> None:
    if receipt is None:
        return
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('自动收尾绑定必须提供真实 run_id')
    from review_start_boundary_gate import bind_review_start_boundary_to_one_run

    # 与 prepare/dispatch 共用不可变事实，真实 run_id 由调用方传递，不能由目录名猜测。
    _, run_dir = bind_review_start_boundary_to_one_run(receipt=receipt, run_id=run_id, run_dir=run_dir)
    receipt_path = run_dir / 'inputs' / 'automatic-closeout-review-start-receipt.json'
    _write_complete_json_without_overwriting(receipt_path, receipt)
    if json.loads(receipt_path.read_text(encoding='utf-8')) != dict(receipt):
        raise ValueError('自动收尾的开审分界与已冻结 receipt 不一致')


def activate_bound_run(receipt: Mapping[str, Any]) -> None:
    # 捕获分界只保存不可变身份；宿主从已落盘的精确 run-binding 核验后才进入完成等待。
    # 因此 bootstrap 等待、可重试失败与无可审范围都不会留下等待不存在 run 的活跃记录。
    repo = Path(str(receipt['worktree'])).resolve()
    task_cmd = os.environ.get('CODEX_RVF_CLINE_KANBAN_TASK_CMD') or kanban.DEFAULT_TASK_CMD
    result = kanban.run_command(['git', '-c', 'core.hooksPath=/dev/null', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=repo)
    project = Path(result.stdout.strip()).parent
    command = kanban.task_command(
        task_cmd, 'activate-rvf-automatic-closeout-after-run-binding',
        '--project-path', str(project), '--task-id', str(receipt['kanban_task_id']),
        '--decision-token', str(receipt['decision_token']),
    )
    result = kanban.run_command(command, cwd=repo, env=kanban.runtime_env(kanban.task_runtime_port(task_cmd=task_cmd)), timeout=60)
    activated_receipt = json.loads(result.stdout)['receipt']
    if activated_receipt != receipt:
        raise ValueError('宿主激活的 RVF 开审分界与已绑定 receipt 不一致')


def _completion_notification_identity(run_dir: Path, finalized: Mapping[str, Any]) -> dict[str, Any] | None:
    if finalized.get('errors') or finalized.get('decision_kind') not in {'handoff', 'handoff-advisory'}:
        return
    run_dir = run_dir.resolve()
    path = run_dir / 'inputs' / 'automatic-closeout-review-start-receipt.json'
    if not path.is_file():
        return
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if not finalized.get('completed_at'):
        return
    from review_start_boundary_gate import review_start_boundary_receipt_path, bind_review_start_boundary_to_one_run
    binding_path = review_start_boundary_receipt_path(str(receipt['decision_token'])).with_suffix('.run-binding.json')
    binding = json.loads(binding_path.read_text(encoding='utf-8'))
    if finalized.get('run_id') != binding.get('run_id') or binding.get('run_dir') != str(run_dir) or Path(str(finalized.get('run_dir'))).resolve() != run_dir or Path(str(finalized.get('repo'))).resolve() != Path(receipt['worktree']).resolve():
        raise ValueError('RVF 完成身份与开审分界不一致')
    bind_review_start_boundary_to_one_run(receipt=receipt, run_id=binding['run_id'], run_dir=run_dir)
    return {
        'schema': 'rvf-automatic-closeout-completion/v1', 'decision_token': receipt['decision_token'],
        'session_id': receipt['session_id'], 'worktree': receipt['worktree'],
        'run_id': binding['run_id'], 'run_dir': str(run_dir),
        'handoff_path': str(run_dir / 'artifacts' / 'handoff.md'),
    }


def freeze_completion_notification(run_dir: Path, finalized: Mapping[str, Any]) -> None:
    """仅在首次 finalize 落完成锁前冻结；通知故障后也不能重读 handoff 重新背书。"""
    run_dir = run_dir.resolve()
    identity = _completion_notification_identity(run_dir, finalized)
    if identity is None:
        return
    frozen_path = run_dir / 'artifacts' / 'automatic-closeout-frozen-completion-notification.json'
    if frozen_path.exists():
        return
    handoff = Path(identity['handoff_path'])
    if not handoff.is_file():
        return
    _write_complete_json_without_overwriting(frozen_path, {
        **identity, 'handoff_sha256': hashlib.sha256(handoff.read_bytes()).hexdigest(),
    })


def publish_completion(run_dir: Path, finalized: Mapping[str, Any]) -> None:
    run_dir = run_dir.resolve()
    identity = _completion_notification_identity(run_dir, finalized)
    if identity is None:
        return
    frozen_path = run_dir / 'artifacts' / 'automatic-closeout-frozen-completion-notification.json'
    # 缺失冻结证据时保持关闭；缓存完成和重启恢复都无权从当前 handoff 补签。
    if not frozen_path.is_file():
        return
    frozen = json.loads(frozen_path.read_text(encoding='utf-8'))
    digest = frozen.get('handoff_sha256') if isinstance(frozen, dict) else None
    if (
        not isinstance(frozen, dict)
        or any(frozen.get(key) != value for key, value in identity.items())
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in '0123456789abcdef' for character in digest)
    ):
        raise ValueError('冻结的 RVF 完成凭据无效或身份不一致')
    notification_path = rvf_home() / 'automatic-closeout-completion-notifications' / f"{identity['decision_token']}.json"
    _write_complete_json_without_overwriting(notification_path, frozen)
    if json.loads(notification_path.read_text(encoding='utf-8')) != frozen:
        raise ValueError('已发布的 RVF 完成通知与冻结凭据不一致')
