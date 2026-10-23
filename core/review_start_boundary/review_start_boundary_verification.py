"""只验证宿主证据，不把 Agent 表态或投递状态升级为开审资格。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from core.review_start_boundary.explicit_manual_review_command_recognition import manual_review_command_text_is_eligible

RECEIPT_SCHEMA = "rvf-independent-user-turn-boundary/v1"
PENDING = "review_start_boundary_pending"
INVALIDATED = "review_start_boundary_invalidated"
VERIFIED = "review_start_boundary_verified"
CHECKPOINT_BINDING_PREFIX = "RVF_REVIEW_START_BOUNDARY_BINDING="
CHECKPOINT_BINDING_FIELDS = ("schema", "decision_token", "session_id", "native_user_message_id", "native_turn_id", "boundary_message_sha256", "candidate_fingerprint", "candidate_filesystem_state_sha256", "worktree_head_commit_sha", "comparison_base_tip", "comparison_base_ref", "comparison_base_ref_tip", "kanban_task_id", "idempotency_key")


class ReviewStartBoundaryNotVerified(ValueError):
    def __init__(self, status: str, reason: str, *, reason_code: str = "boundary_evidence_unverified", recovery_action: str = "inspect_boundary_diagnostic"):
        self.status = status
        self.reason = reason
        self.reason_code = reason_code
        self.recovery_action = recovery_action
        super().__init__(f"{status}: {reason}")


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def read_receipt(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewStartBoundaryNotVerified(PENDING, "独立消息与 checkpoint 的 receipt 尚不可读") from exc
    if not isinstance(value, dict):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "分界 receipt 必须是对象")
    return value


def git_output(repo: str, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=repo,
        capture_output=True, text=True, check=False, env=repository_git_environment(),
    )
    if result.returncode:
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "分界引用的 Git 内容快照无法验证")
    return result.stdout.strip()


def repository_git_environment() -> dict[str, str]:
    # 不继承调用方的临时 index、其它 checkout 或 object database 定位。
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def candidate_filesystem_state_sha256(repo: str, candidates: list[Mapping[str, Any]]) -> str:
    """捕获文件身份和内核修改时间，内容写回原样也不能复用旧候选分界。"""
    observations = []
    for candidate in sorted(candidates, key=lambda item: str(item.get("path"))):
        if not candidate.get("included_in_candidate_set"):
            continue
        path = candidate.get("path")
        if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ReviewStartBoundaryNotVerified(INVALIDATED, "候选文件身份记录含仓库外路径")
        file_path = Path(repo) / path
        try:
            observation = file_path.lstat()
            metadata = [observation.st_dev, observation.st_ino, observation.st_mode, observation.st_size, observation.st_mtime_ns, observation.st_ctime_ns]
            if file_path.is_symlink() and file_path.exists():
                target = file_path.stat()
                metadata.extend([target.st_dev, target.st_ino, target.st_size, target.st_mtime_ns, target.st_ctime_ns])
        except FileNotFoundError:
            metadata = None
        observations.append([path, metadata])
    return hashlib.sha256(json.dumps(observations, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def verify_independent_user_turn_boundary(
    receipt: Mapping[str, Any], *, token_payload: Mapping[str, Any], repo: str,
    session_id: str | None, native_boundary_reader: Callable[..., Mapping[str, Any]],
    candidate_summary_builder: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """每次读取原生活动分支与当前候选；receipt 中的 verified/accepted 字段没有裁决权。"""
    def invalid(reason: str) -> None:
        raise ReviewStartBoundaryNotVerified(INVALIDATED, reason)

    if receipt.get("schema") != RECEIPT_SCHEMA:
        invalid("宿主尚未提供所需版本的独立消息 receipt")
    if not session_id or session_id != token_payload.get("session_id") or receipt.get("session_id") != session_id:
        invalid("调用会话、token 与原生消息的 session 不一致")
    if receipt.get("decision_token") != token_payload.get("token"):
        invalid("receipt 不属于这枚决策 token")
    for path in (receipt.get("worktree"), token_payload.get("worktree")):
        if not isinstance(path, str) or Path(path).resolve() != Path(repo).resolve():
            invalid("调用 worktree、token 与分界快照不一致")
    expected_fingerprint = token_payload.get("candidate_fingerprint")
    if receipt.get("candidate_fingerprint") != expected_fingerprint:
        invalid("receipt 与 token 的候选版本不一致")
    for name in ("native_user_message_id", "native_turn_id", "transcript_path", "boundary_message_sha256", "checkpoint_commit", "checkpoint_tree", "worktree_head_commit_sha"):
        if not isinstance(receipt.get(name), str) or not receipt[name]:
            invalid(f"分界 receipt 缺少 {name}")
    native = native_boundary_reader(receipt=receipt)
    if native.get("capability_unverified"):
        raise ReviewStartBoundaryNotVerified(
            PENDING, str(native.get("reason")), reason_code="native_rewind_capability_unverified", recovery_action="repair_host_capability",
        )
    if not native.get("independent_user_turn_verified"):
        raise ReviewStartBoundaryNotVerified(
            INVALIDATED, str(native.get("reason") or "宿主没有证明独立可回退用户消息"),
            reason_code=str(native.get("reason_code") or "native_independent_user_turn_evidence_missing"),
            recovery_action=str(native.get("recovery_action") or "inspect_boundary_diagnostic"),
        )
    try:
        timestamps = [datetime.fromisoformat(str(value).replace("Z", "+00:00")) for value in (token_payload.get("minted_at"), native.get("native_message_received_at"), token_payload.get("expires_at"))]
        # EXPERIMENT_NODE: 用真实 UPS 样本验证手动消息先于铸币的 120 秒容忍窗。
        manual_submission_precedes_mint = receipt.get("source") == "explicit_user_command" and 0 <= (timestamps[0] - timestamps[1]).total_seconds() <= 120
        outside_token_window = not (timestamps[0] <= timestamps[1] <= timestamps[2] or manual_submission_precedes_mint)
    except (ValueError, TypeError):
        invalid("缺少可核验的原生消息接收时间")
    if outside_token_window:
        invalid("消息早于 token 或在 token 到期后才送达")
    text = native.get("message_text")
    if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != receipt["boundary_message_sha256"]:
        invalid("原生用户消息正文与投递记录不一致")
    if receipt.get("source") == "explicit_user_command":
        if token_payload.get("initiated_by_explicit_user_request") is not True or token_payload.get("explicit_user_request_provenance") != "developer_manual_trigger_via_user_prompt_submit":
            invalid("手动命令的原始来源与 token 不一致")
        if not manual_review_command_text_is_eligible(text, has_verified_manual_ups_provenance=True):
            invalid("原始消息不是明确的 RVF 手动命令")
    elif receipt.get("source") == "rvf_automatic_decision_message":
        if token_payload.get("initiated_by_explicit_user_request") is not False or token_payload.get("explicit_user_request_provenance"):
            invalid("自动消息不能被记成开发者显式授权")
        literal = f"RVF_DECISION_TOKEN={token_payload['token']}"
        if "\n" in text.strip() or text.count(literal) != 1 or not text.startswith("RVF 发起决策点 · " ):
            invalid("自动决策点必须是独立边界标记，不能混入其它用户输入")
    else:
        invalid("不接受 Agent 自述或未知来源作为分界")
    commit = receipt["checkpoint_commit"]
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        invalid("checkpoint 必须绑定不可变 commit，不能只提供 latest ref")
    if git_output(repo, "rev-parse", f"{commit}^{{tree}}") != receipt["checkpoint_tree"]:
        invalid("checkpoint tree 已不匹配")
    binding_lines = [line[len(CHECKPOINT_BINDING_PREFIX):] for line in git_output(repo, "show", "-s", "--format=%B", commit).splitlines() if line.startswith(CHECKPOINT_BINDING_PREFIX)]
    try:
        binding = json.loads(binding_lines[0]) if len(binding_lines) == 1 else None
    except ValueError:
        binding = None
    if binding != {key: receipt.get(key) for key in CHECKPOINT_BINDING_FIELDS}:
        invalid("不可变 checkpoint 没有绑定这条消息、token 与候选；不能复用 latest checkpoint")
    if git_output(repo, "rev-parse", "HEAD") != receipt["worktree_head_commit_sha"]:
        invalid("HEAD 在分界之后改变，须重新请求分界")
    try:
        current = candidate_summary_builder()
    except Exception as exc:
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "当前候选无法重新核验") from exc
    if current.get("candidate_fingerprint") != expected_fingerprint:
        invalid("候选在分界之后改变，须重新请求分界")
    if receipt.get("candidate_filesystem_state_sha256") != candidate_filesystem_state_sha256(repo, current.get("candidates", [])):
        invalid("候选文件在分界之后被修改或重建，即便内容已写回原样也须重新请求分界")
    summary = token_payload.get("candidate_summary") or {}
    if receipt.get("comparison_base_tip") != summary.get("committed_round_baseline") or current.get("committed_round_baseline") != summary.get("committed_round_baseline"):
        invalid("比较基线与分界不一致")
    if current.get("kanban_task_id") != summary.get("kanban_task_id") or receipt.get("kanban_task_id") != summary.get("kanban_task_id"):
        invalid("当前 Kanban task 与消息/token 不一致")
    base_ref = summary.get("committed_round_base_ref")
    if receipt.get("comparison_base_ref") != base_ref or current.get("committed_round_base_ref") != base_ref:
        invalid("比较基线的本地 ref 与分界不一致")
    if base_ref:
        fixed_base_tip = receipt.get("comparison_base_ref_tip")
        if not isinstance(fixed_base_tip, str) or len(fixed_base_tip) not in (40, 64) or any(character not in "0123456789abcdef" for character in fixed_base_tip):
            invalid("分界必须记录不可变的本地 base commit")
        git_output(repo, "rev-parse", "--verify", fixed_base_tip + "^{commit}")
    for candidate in current.get("candidates", []):
        if not candidate.get("included_in_candidate_set"):
            continue
        path = candidate.get("path")
        if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
            invalid("候选路径不是仓库内的相对路径")
        tree_entry = git_output(repo, "ls-tree", commit, "--", path).split()
        mode = tree_entry[0] if tree_entry else None
        live_path = Path(repo) / path
        snapshot_path = path
        if live_path.is_symlink():
            if mode != "120000" or git_output(repo, "show", f"{commit}:{path}") != os.readlink(live_path):
                invalid(f"checkpoint 未捕获当前符号链接：{path}")
            resolved_path = live_path.resolve()
            if not resolved_path.is_relative_to(Path(repo).resolve()):
                invalid(f"仓库外符号链接的内容不能由此 checkpoint 证明：{path}")
            snapshot_path = resolved_path.relative_to(Path(repo).resolve()).as_posix()
        elif mode == "160000":
            if len(tree_entry) < 3 or git_output(str(live_path), "rev-parse", "HEAD") != tree_entry[2] or git_output(str(live_path), "status", "--porcelain"):
                invalid(f"submodule 在分界之后改变或存在未冻结修改：{path}")
            continue
        elif live_path.is_file():
            expected_mode = "100755" if live_path.stat().st_mode & 0o111 else "100644"
            if mode != expected_mode:
                invalid(f"checkpoint 未捕获候选文件类型或执行权限：{path}")
        elif live_path.exists():
            invalid(f"checkpoint 不能证明此候选路径的当前类型：{path}")
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "show", f"{commit}:{snapshot_path}"], cwd=repo, capture_output=True, check=False, env=repository_git_environment())
        digest = hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else "absent"
        if digest != candidate.get("content_sha256"):
            invalid(f"checkpoint 未捕获本次候选内容：{path}")
    return dict(receipt)
