#!/usr/bin/env python3
from __future__ import annotations


import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* import
from review_start_boundary_gate import require_review_start_boundary_for_prepare, bind_review_start_boundary_to_one_run
import concurrent.futures
import rvf_bootstrap_gate
import rvf_prep_file
from base_task_intent_reconciliation_artifact import (
    consumed_base_task_intent_reconciliation,
    intent_context_markdown,
    record_intent_artifact_consumption_outcome,
    status_artifact_payload as intent_artifact_status_artifact_payload,
    STATUS_ARTIFACT_FILENAME as INTENT_ARTIFACT_STATUS_FILENAME,
)
from rvf_dispatch_prompts import dispatch_scope_of_work_text
from core.run_ledger.run_ledger import normalize_rvf_backend, rvf_state_fields, start_run
from core.session_scope_allocation import reviewable_unit_diff_tracker
from core.host_adapter.host_transcript_format_detection import HOST_CODEX, detect_transcript_format
from core.rvf_user_local_data_paths import resolved_rvf_reviewer_registry_path
from core.review_start_boundary.review_start_boundary_verification import INVALIDATED, ReviewStartBoundaryNotVerified


def _resolve_valid_main_harnesses() -> frozenset[str]:
    """合法的主 dispatch harness 集合 = reviewer-registry 的 harness id 全集。

    **不要在这里手写清单。** RVF 里同一套 harness 名字有三个消费者：host 身份探测
    （``core/host_adapter``）、reviewer-registry（选腿池）、以及本集合（显式
    ``RVF_MAIN_HARNESS`` 覆盖的白名单）。三份曾各自演化，代价是 Kimi 的 host 身份取了
    ``kimi_code_cli`` 而 registry 是 ``kimi_code`` —— 主 harness 排不出池，200-seed 实测
    44% 的 Kimi 会话被 Kimi 自己评审，且全程零报错（计划 §14 / P-5）。手写第三份清单是
    同一缺陷的第三个入口：漏登记一个 host，它的显式 env 覆盖就被**静默丢弃**。

    ``scripts/check_host_identity_and_reviewer_registry_harness_id_vocabulary_consistency.py``
    是这条不变量的 gate。刻意**不**对 ``KNOWN_HOST_IDENTITIES`` 做并集兜底：并上去会让
    gate 那条断言结构性恒真，把「派生错了」变成查不出来的假绿。

    registry 不可读时返回空集（fail-safe 方向：显式覆盖失效、回落到 transcript 探测值），
    而不是猜一个清单出来。

    ⚠️ ``cursor`` 在 registry 里但**不是** host —— 它只经显式 ``RVF_MAIN_HARNESS=cursor``
    覆盖可达，transcript 探测永不返回它。这是词表方向单向的原因。
    """
    bundled_reviewer_registry_path = (
        Path(__file__).resolve().parent.parent / "config" / "reviewer-registry.json"
    )
    try:
        registry_path = resolved_rvf_reviewer_registry_path(bundled_reviewer_registry_path)
        registry_document = json.loads(registry_path.read_text(encoding="utf-8"))
        return frozenset(registry_document["harnesses"])
    except Exception:
        return frozenset()


VALID_MAIN_HARNESSES = _resolve_valid_main_harnesses()

SHARED_WORKFLOW_DEFAULT_TIMEOUT_SECONDS = 60.0
TARGET_FLOW_BACKEND = {
    "flow-2-branch": "kanban-task",
    "flow-2-inplace": "kanban-task",
    "flow-1-self-rising": "kanban-followup",
    "flow-3-inplace": "manual",
    "flow-manual": "manual",
    # agent 经 Stop hook 决策点显式 start：就地跑，无 fork / 无 kanban task，故 backend=manual。
    # 它与 flow-manual 的差别不在 backend 而在**来源**——后者是用户敲的手动入口。
    "flow-agentic-initiation": "manual",
}


SKILL_DIR = Path(__file__).resolve().parents[1]
BUILD_PACKET = SKILL_DIR / "scripts" / "build_review_packet.py"
WORKSPACE_SNAPSHOT = SKILL_DIR / "scripts" / "workspace_snapshot.py"
# session_change_manifest 已迁入 core/session_scope_allocation/（去-codex S10c）；作为子进程
# 直接调用，路径取自其同包邻居 reviewable_unit_diff_tracker 的 __file__（depth-robust，repo /
# 部署 payload / worktree 同口径，免固定 parents[N]）。常量沿用 SESSION_MANIFEST 助记名（同
# BUILD_PACKET 约定）。
SESSION_MANIFEST = Path(reviewable_unit_diff_tracker.__file__).resolve().parent / "session_change_manifest.py"
COMMAND_LOCK = SKILL_DIR / "scripts" / "command_lock.py"
WRITE_REVIEW_RESULT = SKILL_DIR / "scripts" / "write_review_result.py"
CHECK_REVIEW_RESULT = SKILL_DIR / "scripts" / "check_review_result.py"
SCOPE_CONTRACT_VERSION = 3
PRE_REVIEW_BASE_SYNC_MARKER = "RVF-PRE-REVIEW-BASE-SYNC-SKILL:"
REVIEW_KINDS = {"implementation", "base_drift_integration"}
class BootstrapRequiredBeforeRvf(RuntimeError):
    def __init__(self, payload: Mapping[str, Any]) -> None:
        super().__init__("bootstrap_required_before_rvf")
        self.payload = dict(payload)


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def git_repository_location_clean_environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def run(cmd: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment() if cmd and cmd[0] == "git" else None,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed.stdout


def git_root(repo: Path) -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"], cwd=repo).strip()).resolve()


def _local_base_ref_full_name(local_base_ref: str) -> str:
    candidate = local_base_ref.strip()
    if not candidate or candidate.startswith("-"):
        raise ValueError("local base ref must be a non-empty local branch name")
    if candidate.startswith("refs/heads/"):
        full_ref = candidate
    elif candidate.startswith("refs/"):
        raise ValueError("local base ref must resolve below refs/heads/")
    else:
        full_ref = f"refs/heads/{candidate}"
    valid = subprocess.run(
        ["git", "check-ref-format", full_ref],
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment(),
    )
    if valid.returncode != 0:
        raise ValueError(f"invalid local base ref: {local_base_ref!r}")
    return full_ref


def _local_branch_exists(repo: Path, candidate: str) -> bool:
    try:
        full_ref = _local_base_ref_full_name(candidate)
    except ValueError:
        return False
    completed = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", full_ref],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment(),
    )
    return completed.returncode == 0


def resolve_local_base_ref(
    repo: Path,
    *,
    explicit_local_base_ref: str | None,
    committed_base_ref: str | None,
) -> str | None:
    if explicit_local_base_ref:
        return explicit_local_base_ref.strip()
    if committed_base_ref:
        candidate = committed_base_ref.strip().lstrip("^")
        if _local_branch_exists(repo, candidate):
            return candidate
    origin_head = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment(),
    )
    if origin_head.returncode == 0:
        remote_ref = origin_head.stdout.strip()
        prefix = "refs/remotes/origin/"
        candidate = remote_ref[len(prefix):] if remote_ref.startswith(prefix) else ""
        if candidate and _local_branch_exists(repo, candidate):
            return candidate
    if _local_branch_exists(repo, "main"):
        return "main"
    return None


def pre_review_base_sync_state(repo: Path, local_base_ref: str | None) -> dict[str, str] | None:
    """Read the opt-in marker from the local base commit, never from a stale task checkout."""
    if not local_base_ref:
        return None
    full_ref = _local_base_ref_full_name(local_base_ref)
    local_base_tip = run(["git", "rev-parse", "--verify", f"{full_ref}^{{commit}}"], cwd=repo).strip()
    agents = subprocess.run(
        ["git", "show", f"{local_base_tip}:AGENTS.md"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment(),
    )
    if agents.returncode != 0:
        return None
    required_skill: str | None = None
    for line in agents.stdout.splitlines():
        if line.strip().startswith(PRE_REVIEW_BASE_SYNC_MARKER):
            value = line.split(PRE_REVIEW_BASE_SYNC_MARKER, 1)[1].strip()
            if value:
                required_skill = value.split()[0]
            break
    if required_skill is None:
        return None
    task_head = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", local_base_tip, task_head],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment(),
    )
    if ancestor.returncode == 0:
        return {
            "status": "absorbed",
            "required_skill": required_skill,
            "local_base_ref": local_base_ref,
            "local_base_tip": local_base_tip,
            "task_head": task_head,
        }
    if ancestor.returncode != 1:
        raise RuntimeError(ancestor.stderr.strip() or "git merge-base --is-ancestor failed")
    return {
        "status": "bootstrap_required_before_rvf",
        "required_skill": required_skill,
        "local_base_ref": local_base_ref,
        "local_base_tip": local_base_tip,
        "task_head": task_head,
    }


def refuse_explicit_local_base_ref_that_bypasses_opt_in(
    repo: Path,
    *,
    explicit_local_base_ref: str | None,
    committed_base_ref: str | None,
    resolved_local_base_ref: str | None,
) -> None:
    """堵 O1：不许用一个**存在但没有 marker 的旁支**当 base ref，从而绕开本仓的 opt-in 门禁。

    ``resolve_local_base_ref`` 把 ``explicit_local_base_ref`` 当最高优先级短路，而门禁读哪个 commit
    的 ``AGENTS.md`` 完全由它决定。畸形 ref 与不存在的 ref 早已 fail-closed（``_local_base_ref_full_name``
    与 ``git rev-parse --verify`` 各挡一层），**唯一**剩下的旁路形态是：仓库自己解析出来的 base 是
    opt-in 的，而显式传入的那个分支恰好没有 marker ⇒ ``pre_review_base_sync_state`` 返回 ``None``、
    整道门禁静默变成 ``not_applicable``。这与 docstring 承诺的「always read from the current local
    base commit」直接矛盾。

    因此这里独立解析一次（不带显式覆盖），只在「独立解析出的 base 是 opt-in、而显式那个不是」时拒绝。
    显式 ref 与独立解析**不同但同样 opt-in**（或两者都非 opt-in）不受影响——那是合法用法，
    比如默认分支不叫 main、又没有 ``origin/HEAD`` 的仓库。
    """
    if not explicit_local_base_ref:
        return
    independently_resolved = resolve_local_base_ref(
        repo, explicit_local_base_ref=None, committed_base_ref=committed_base_ref
    )
    if not independently_resolved or independently_resolved == resolved_local_base_ref:
        return
    if pre_review_base_sync_state(repo, independently_resolved) is None:
        return
    if pre_review_base_sync_state(repo, resolved_local_base_ref) is not None:
        return
    raise ValueError(
        "explicit local base ref bypasses the repository's opt-in pre-review base sync marker: "
        f"{resolved_local_base_ref!r} carries no marker while {independently_resolved!r} does"
    )


def git_diff_path_set(repo: Path, older_tip: str, newer_tip: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "-z", older_tip, newer_tip, "--"],
        cwd=repo,
        check=False,
        capture_output=True,
        env=git_repository_location_clean_environment(),
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.decode(errors="replace").strip() or "git diff failed")
    return sorted({os.fsdecode(path) for path in completed.stdout.split(b"\0") if path})


def validated_bootstrap_receipt(
    repo: Path, state: Mapping[str, str], receipt_path: Path
) -> dict[str, Any]:
    expected_path = (
        Path(run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo).strip())
        / "base-branch-sync"
        / "local-base-into-task-worktree-bootstrap-receipt.json"
    )
    if receipt_path.absolute() != expected_path.absolute():
        raise ValueError("bootstrap receipt must use the task absolute git-dir path")
    _receipt_value, _receipt_digest = canonical_json_artifact_identity(receipt_path)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    common_dir = Path(
        run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo).strip()
    ).resolve()
    required_values = {
        "schema_name": "local-base-into-task-worktree-bootstrap-receipt",
        "schema_version": 1,
        "absorbed_base_tip": state["local_base_tip"],
        "base_ref": state["local_base_ref"],
        "git_common_directory_realpath": str(common_dir),
        "task_worktree_realpath": str(repo.resolve()),
        "boundary_status": "VERIFIED",
        "lock_status": "RELEASED",
        "target_repository_hooks_status": "DISABLED",
    }
    if any(payload.get(name) != value for name, value in required_values.items()):
        raise ValueError("bootstrap receipt identity does not match the opt-in repository")
    if payload.get("wip_status") not in {
        "NOT_REQUIRED_TASK_WORKTREE_CLEAN",
        "RESTORED_AND_DROPPED",
    }:
        raise ValueError("bootstrap receipt WIP status is not reusable")
    tips: dict[str, str] = {}
    for name in (
        "base_task_merge_base_tip",
        "absorbed_base_tip",
        "task_tip_before_base_absorption",
        "task_tip_after_base_absorption",
    ):
        value = payload.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise ValueError(f"bootstrap receipt {name} is invalid")
        run(["git", "cat-file", "-e", f"{value}^{{commit}}"], cwd=repo)
        tips[name] = value
    actual_merge_base = run(
        [
            "git",
            "merge-base",
            tips["task_tip_before_base_absorption"],
            tips["absorbed_base_tip"],
        ],
        cwd=repo,
    ).strip()
    if actual_merge_base != tips["base_task_merge_base_tip"]:
        raise ValueError("bootstrap receipt merge-base identity is invalid")
    for ancestor_tip in (
        tips["absorbed_base_tip"],
        tips["task_tip_before_base_absorption"],
        tips["task_tip_after_base_absorption"],
    ):
        descendant = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor_tip, state["task_head"]],
            cwd=repo,
            env=git_repository_location_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
        )
        if descendant.returncode != 0:
            raise ValueError("current task HEAD does not descend from the bootstrap receipt")
    expected_base_delta_paths = git_diff_path_set(
        repo, tips["base_task_merge_base_tip"], tips["absorbed_base_tip"]
    )
    expected_task_change_paths = git_diff_path_set(
        repo, tips["base_task_merge_base_tip"], tips["task_tip_before_base_absorption"]
    )
    expected_interaction_paths = sorted(
        set(expected_base_delta_paths).intersection(expected_task_change_paths)
    )
    for name, expected_paths in (
        ("base_delta_paths", expected_base_delta_paths),
        ("task_change_paths", expected_task_change_paths),
        ("interaction_primary_paths", expected_interaction_paths),
    ):
        if payload.get(name) != expected_paths:
            raise ValueError(f"bootstrap receipt {name} does not match the fixed Git tips")
    return payload


def default_bootstrap_receipt(repo: Path, state: Mapping[str, str]) -> Path | None:
    absolute_git_dir = Path(run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo).strip())
    receipt_path = (
        absolute_git_dir
        / "base-branch-sync"
        / "local-base-into-task-worktree-bootstrap-receipt.json"
    )
    if not receipt_path.exists() and not Path(f"{receipt_path}.sha256").exists():
        return None
    validated_bootstrap_receipt(repo, state, receipt_path)
    return receipt_path


def safe_repo_name(repo: Path) -> str:
    name = repo.name or "repo"
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in name)[:80] or "repo"


def default_base_dir() -> Path:
    return Path(tempfile.gettempdir()) / "review-validate-fix-runs"


def review_env_exports(
    *,
    repo: Path,
    run_id: str,
    run_dir: Path,
    log_root: Path,
    artifacts_dir: Path,
    inputs_dir: Path,
    scope_contract_path: Path,
    scope_of_work_path: Path | None,
    session_manifest_path: Path | None,
    packet_path: Path,
    metadata_path: Path,
    snapshot_path: Path,
    bootstrap_metadata_path: Path | None,
    tracker_dir: Path | None = None,
    tracker_repo_key: str | None = None,
    tracker_scope_path: Path | None = None,
    rvf_backend: str | None = None,
    parent_context_path: Path | None = None,
    main_harness: str | None = None,
) -> tuple[dict[str, str], str]:
    env: dict[str, str] = {
        "RVF_REPO": str(repo),
        "RVF_RUN_ID": run_id,
        "RVF_RUN_DIR": str(run_dir),
        "RVF_ARTIFACTS_DIR": str(artifacts_dir),
        "RVF_INPUTS_DIR": str(inputs_dir),
        "RVF_SCOPE_CONTRACT": str(scope_contract_path),
        "RVF_REVIEW_PACKET": str(packet_path),
        "RVF_REVIEW_PACKET_METADATA": str(metadata_path),
        "RVF_BEFORE_WORKSPACE_SNAPSHOT": str(snapshot_path),
        "RVF_COMMAND_LOCK": str(COMMAND_LOCK),
        "RVF_WRITE_REVIEW_RESULT": str(WRITE_REVIEW_RESULT),
        "RVF_CHECK_REVIEW_RESULT": str(CHECK_REVIEW_RESULT),
        "RVF_REVIEW_RESULT": str(artifacts_dir / "reviewers" / "reviewer" / "review-result.json"),
        "CODEX_RVF_LOG_ROOT": str(log_root),
    }
    if main_harness:
        env["RVF_MAIN_HARNESS"] = main_harness
    canonical_backend = normalize_rvf_backend(rvf_backend)
    if canonical_backend is not None:
        env["RVF_BACKEND"] = canonical_backend
    if bootstrap_metadata_path is not None:
        env["RVF_WORKTREE_BOOTSTRAP"] = str(bootstrap_metadata_path)
    if scope_of_work_path is not None:
        env["RVF_SCOPE_OF_WORK"] = str(scope_of_work_path)
        env["RVF_SESSION_CONTEXT"] = str(scope_of_work_path)
    if session_manifest_path is not None:
        env["RVF_SESSION_MANIFEST"] = str(session_manifest_path)
    if tracker_dir is not None:
        env["RVF_TRACKER_DIR"] = str(tracker_dir)
    if tracker_repo_key:
        env["RVF_TRACKER_REPO_KEY"] = tracker_repo_key
    if tracker_scope_path is not None:
        env["RVF_TRACKER_SCOPE"] = str(tracker_scope_path)
    # 父会话对话 context（fail-open，可能缺失）。仅在 artifact 存在时导出，
    # 与 cline-kanban task prompt 的 RVF_PARENT_CONVERSATION_CONTEXT 标记一致。
    if parent_context_path is not None and parent_context_path.exists():
        env["RVF_PARENT_CONVERSATION_CONTEXT"] = str(parent_context_path)

    lines = [
        "# Source this file in review subprocesses to avoid repeating long RVF paths.",
        f"export RVF_REPO={shlex.quote(env['RVF_REPO'])}",
        f"export RVF_RUN_ID={shlex.quote(env['RVF_RUN_ID'])}",
        f"export RVF_RUN_DIR={shlex.quote(env['RVF_RUN_DIR'])}",
        f"export CODEX_RVF_LOG_ROOT={shlex.quote(env['CODEX_RVF_LOG_ROOT'])}",
    ]
    if artifacts_dir == run_dir / "artifacts":
        lines.append('export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"')
    else:
        lines.append(f"export RVF_ARTIFACTS_DIR={shlex.quote(env['RVF_ARTIFACTS_DIR'])}")
    if inputs_dir == artifacts_dir / "inputs":
        lines.append('export RVF_INPUTS_DIR="$RVF_ARTIFACTS_DIR/inputs"')
    else:
        lines.append(f"export RVF_INPUTS_DIR={shlex.quote(env['RVF_INPUTS_DIR'])}")
    if scope_contract_path == inputs_dir / "scope.contract.json":
        lines.append('export RVF_SCOPE_CONTRACT="$RVF_INPUTS_DIR/scope.contract.json"')
    else:
        lines.append(f"export RVF_SCOPE_CONTRACT={shlex.quote(env['RVF_SCOPE_CONTRACT'])}")
    if scope_of_work_path is not None:
        lines.append('export RVF_SCOPE_OF_WORK="$RVF_ARTIFACTS_DIR/scope-of-work.md"')
        lines.append('export RVF_SESSION_CONTEXT="$RVF_SCOPE_OF_WORK"')
    if session_manifest_path is not None:
        lines.append('export RVF_SESSION_MANIFEST="$RVF_ARTIFACTS_DIR/session-manifest.json"')
    if main_harness:
        lines.append(f"export RVF_MAIN_HARNESS={shlex.quote(main_harness)}")
    if canonical_backend is not None:
        lines.append(f"export RVF_BACKEND={shlex.quote(canonical_backend)}")
    lines.extend(
        [
            'export RVF_REVIEW_PACKET="$RVF_ARTIFACTS_DIR/review-packet.md"',
            'export RVF_REVIEW_PACKET_METADATA="$RVF_ARTIFACTS_DIR/review-packet.metadata.json"',
            'export RVF_BEFORE_WORKSPACE_SNAPSHOT="$RVF_ARTIFACTS_DIR/before-workspace-snapshot.json"',
            'export RVF_WORKTREE_BOOTSTRAP="$RVF_ARTIFACTS_DIR/worktree-bootstrap.json"',
            f"export RVF_COMMAND_LOCK={shlex.quote(env['RVF_COMMAND_LOCK'])}",
            f"export RVF_WRITE_REVIEW_RESULT={shlex.quote(env['RVF_WRITE_REVIEW_RESULT'])}",
            f"export RVF_CHECK_REVIEW_RESULT={shlex.quote(env['RVF_CHECK_REVIEW_RESULT'])}",
            'export RVF_REVIEW_RESULT="$RVF_ARTIFACTS_DIR/reviewers/${RVF_REVIEWER_ID:-reviewer}/review-result.json"',
        ]
    )
    if tracker_dir is not None:
        lines.append(f"export RVF_TRACKER_DIR={shlex.quote(str(tracker_dir))}")
    if tracker_repo_key:
        lines.append(f"export RVF_TRACKER_REPO_KEY={shlex.quote(tracker_repo_key)}")
    if tracker_scope_path is not None:
        if tracker_scope_path == inputs_dir / "tracker-scope.json":
            lines.append('export RVF_TRACKER_SCOPE="$RVF_INPUTS_DIR/tracker-scope.json"')
        else:
            lines.append(f"export RVF_TRACKER_SCOPE={shlex.quote(str(tracker_scope_path))}")
    if "RVF_PARENT_CONVERSATION_CONTEXT" in env:
        if parent_context_path == artifacts_dir / "parent-conversation-context.md":
            lines.append('export RVF_PARENT_CONVERSATION_CONTEXT="$RVF_ARTIFACTS_DIR/parent-conversation-context.md"')
        else:
            lines.append(
                f"export RVF_PARENT_CONVERSATION_CONTEXT={shlex.quote(env['RVF_PARENT_CONVERSATION_CONTEXT'])}"
            )
    lines.append("")
    return env, "\n".join(lines)


def review_agent_context_text(
    *,
    repo: Path,
    review_env_path: Path,
    scope_of_work_path: Path | None,
    session_manifest_path: Path | None,
) -> str:
    lines = [
        "## RVF Generated Reviewer Context",
        "",
        "This block was generated by `prepare_review_run.py`; do not rewrite artifact paths by hand.",
        f"- Target repo: `{repo}`",
        f"- Review env file: `{review_env_path}`",
        "",
        "For shell commands, load the review session variables first:",
        "",
        "```sh",
        f". {shlex.quote(str(review_env_path))}",
        "```",
        "",
        "Entry files:",
        "- scope contract: `$RVF_SCOPE_CONTRACT`",
    ]
    if scope_of_work_path is not None:
        lines.append("- scope-of-work: `$RVF_SCOPE_OF_WORK`")
    if session_manifest_path is not None:
        lines.append("- session manifest: `$RVF_SESSION_MANIFEST`")
    lines.extend(
        [
            "- review packet: `$RVF_REVIEW_PACKET`",
            "- command lock wrapper: `$RVF_COMMAND_LOCK`",
            "- review result writer: `$RVF_WRITE_REVIEW_RESULT`",
            "- review result checker: `$RVF_CHECK_REVIEW_RESULT`",
            "- reviewer result artifact: `$RVF_REVIEW_RESULT` (set `RVF_REVIEWER_ID` before sourcing when launching multiple reviewers)",
            "",
            "Scope precedence: read `$RVF_SCOPE_CONTRACT` first. If `primary_units` is non-empty, review that tracker unit scope; otherwise use `primary_files` plus scope-of-work. `$RVF_SESSION_MANIFEST` is ownership evidence and tracker audit context, not the final scope contract.",
            "",
            "Use the variables above in commands and notes instead of expanding the run artifacts directory. The result artifact is protocol output and is the only file a review-only agent may write intentionally.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_git(repo: Path, args: list[str], *, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=git_repository_location_clean_environment(),
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        stdout = completed.stdout if text else completed.stdout.decode("utf-8", "replace")
        raise RuntimeError(stderr.strip() or stdout.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def is_tracked_path(repo: Path, rel_path: str) -> bool:
    completed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel_path],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment(),
    )
    return completed.returncode == 0


def safe_stored_name(rel_path: str) -> str:
    normalized = rel_path.strip().replace("\\", "/").strip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    leaf = Path(normalized).name or "path"
    return f"{digest}-{leaf}"


def normalized_scope_list(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        path = value.strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path:
            normalized.append(path)
    return sorted(set(normalized))


def dirty_paths(repo: Path, exclude_prefixes: list[str]) -> list[str]:
    args = ["status", "--porcelain", "-uall"]
    if exclude_prefixes:
        args.extend(["--", ".", *[f":(exclude){prefix}" for prefix in exclude_prefixes]])
    status = run_git(repo, args)
    paths: list[str] = []
    for raw_line in str(status).splitlines():
        if len(raw_line) < 4:
            continue
        path = raw_line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().replace("\\", "/")
        if path:
            paths.append(path)
    return normalized_scope_list(paths)


def metadata_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


_HEX64_CHARS = set("0123456789abcdef")


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(ch in _HEX64_CHARS for ch in value.lower())


def load_tracker_scope(path: Path) -> dict[str, Any]:
    """Validate an allocator tracker_scope JSON file.

    Required keys: unit_ids (non-empty list of 64-hex sha256 strings), lease_id
    (non-empty string), scope_hash (64-hex or "sha256:<64-hex>"), paths (list of
    strings, may be empty).

    Optional keys: lease_ttl_seconds (int), hunks (list of dicts),
    source_session_id (str), takeover_from_session_id (str).

    Unknown keys are tolerated and passed through unchanged so future allocator
    versions can extend the payload without re-touching this consumer.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"tracker_scope: failed to read {path}: {exc}")
    if not isinstance(raw, dict):
        raise ValueError("tracker_scope: payload must be a JSON object")

    unit_ids_raw = raw.get("unit_ids")
    if not isinstance(unit_ids_raw, list) or not unit_ids_raw:
        raise ValueError("tracker_scope.unit_ids must be a non-empty list")
    unit_ids: list[str] = []
    for index, item in enumerate(unit_ids_raw):
        if not isinstance(item, str) or not _is_hex64(item):
            raise ValueError(
                f"tracker_scope.unit_ids[{index}] must be a 64-character hex string"
            )
        unit_ids.append(item.lower())

    lease_id = raw.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id.strip():
        raise ValueError("tracker_scope.lease_id must be a non-empty string")

    scope_hash_raw = raw.get("scope_hash")
    if not isinstance(scope_hash_raw, str) or not scope_hash_raw.strip():
        raise ValueError("tracker_scope.scope_hash must be a non-empty string")
    bare = scope_hash_raw[len("sha256:") :] if scope_hash_raw.startswith("sha256:") else scope_hash_raw
    if not _is_hex64(bare):
        raise ValueError(
            "tracker_scope.scope_hash must be 64 hex characters or 'sha256:<64-hex>'"
        )
    scope_hash = f"sha256:{bare.lower()}"

    paths_raw = raw.get("paths")
    if not isinstance(paths_raw, list):
        raise ValueError("tracker_scope.paths must be a list of strings")
    for index, item in enumerate(paths_raw):
        if not isinstance(item, str):
            raise ValueError(f"tracker_scope.paths[{index}] must be a string")
    paths = normalized_scope_list(paths_raw)

    if "hunks" in raw and not isinstance(raw.get("hunks"), list):
        raise ValueError("tracker_scope.hunks must be a list when present")
    if "source_session_id" in raw and raw["source_session_id"] is not None and not isinstance(raw["source_session_id"], str):
        raise ValueError("tracker_scope.source_session_id must be a string or null")
    if "takeover_from_session_id" in raw and raw["takeover_from_session_id"] is not None and not isinstance(raw["takeover_from_session_id"], str):
        raise ValueError("tracker_scope.takeover_from_session_id must be a string or null")

    payload: dict[str, Any] = dict(raw)
    payload["unit_ids"] = unit_ids
    payload["lease_id"] = lease_id
    payload["scope_hash"] = scope_hash
    payload["paths"] = paths
    return payload


REVIEW_PACKET_BUILD_COMMAND_FILENAME = "review-packet.build-command.json"
SCOPE_OF_WORK_RERENDER_ROUNDS_DIRNAME = "scope-of-work-rerender-rounds"


def write_review_packet_build_command(inputs_dir: Path, packet_cmd: list[str]) -> Path:
    """把这次 packet 渲染的确切参数冻在 ``inputs/`` 里，供后续重渲复用同一个 renderer。

    只存**脚本之后**的参数：解释器路径与 ``build_review_packet.py`` 的绝对路径在重放时按当时的
    环境重算，避免把一台机器的 ``sys.executable`` 钉死进工件。
    """
    path = inputs_dir / REVIEW_PACKET_BUILD_COMMAND_FILENAME
    path.write_text(
        json.dumps(
            {
                "renderer": "build_review_packet.py",
                "args": list(packet_cmd[2:]),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def rerender_scope_of_work_into_review_packet(artifact_dir: Path) -> dict[str, Any]:
    """把**盘上现有的** ``scope-of-work.md`` 重新渲进 review packet（绕过 prepare 的 completed 幂等）。

    存在的理由（2026-08-15 run 实测的死结）：``start`` 在 agent 还没写实质 scope-of-work 时就把
    stub **内联冻结**进了 review packet，而 ``prepare_run_from_prep_file`` 在 completed 态是幂等
    直接返回的——agent 照指引把真 scope-of-work 写好也进不了 packet，reviewer 读到的仍是 stub。

    做法：复用 ``inputs/review-packet.build-command.json`` 里冻结的**同一条** renderer 命令重跑，
    不新造渲染逻辑（新造必然与 prepare 漂移）。重渲前把旧 packet / metadata / scope-of-work 整组
    归档到 ``scope-of-work-rerender-rounds/round-<N>/``——「冻结输入可审计」不能因为重渲而破功。

    fail-closed：冻结命令缺失、不是本 artifact_dir 的、或 ``--output`` 指向别处，一律抛而不是
    猜一条命令重跑（猜错会把另一轮的 packet 覆盖掉）。
    """
    artifact_dir = artifact_dir.expanduser().resolve()
    inputs_dir = artifact_dir / "inputs"
    command_path = inputs_dir / REVIEW_PACKET_BUILD_COMMAND_FILENAME
    if not command_path.is_file():
        raise ValueError(
            f"缺少冻结的 packet 渲染命令 {command_path}——该 run 是在本能力落地前铸的，"
            "无法安全重渲（不猜命令：猜错会覆盖掉别轮的 packet）。"
        )
    try:
        recorded = json.loads(command_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"冻结的 packet 渲染命令不可读：{command_path}: {exc}") from exc
    args = recorded.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError(f"冻结的 packet 渲染命令格式非法：{command_path}")

    packet_path = artifact_dir / "review-packet.md"
    metadata_path = artifact_dir / "review-packet.metadata.json"
    scope_of_work_path = artifact_dir / "scope-of-work.md"
    # 比对 **resolve 之后** 的路径：冻结命令里记的是当时的字面路径，而 artifact_dir 这边已经
    # resolve 过（macOS 上 /var → /private/var 这类 symlink 会让字符串比对无谓地假红）。
    recorded_output = (
        Path(args[args.index("--output") + 1]).expanduser().resolve()
        if "--output" in args and args.index("--output") + 1 < len(args)
        else None
    )
    if recorded_output != packet_path:
        raise ValueError(
            f"冻结命令的 --output（{recorded_output}）不指向本 artifact_dir 的 review-packet.md"
            f"（{packet_path}）；拒绝重渲，避免写到别轮的工件上。"
        )
    if not scope_of_work_path.is_file():
        raise ValueError(f"没有可重渲的 scope-of-work：{scope_of_work_path}")

    rounds_dir = artifact_dir / SCOPE_OF_WORK_RERENDER_ROUNDS_DIRNAME
    rounds_dir.mkdir(parents=True, exist_ok=True)
    round_index = 1 + sum(1 for child in rounds_dir.iterdir() if child.is_dir())
    round_dir = rounds_dir / f"round-{round_index:02d}"
    round_dir.mkdir(parents=True, exist_ok=False)
    archived: list[str] = []
    for source in (
        packet_path,
        metadata_path,
        inputs_dir / "review-packet.md",
        inputs_dir / "review-packet.metadata.json",
        inputs_dir / "scope-of-work.md",
    ):
        if source.is_file():
            relative = source.relative_to(artifact_dir)
            destination = round_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            archived.append(relative.as_posix())

    run([sys.executable, str(BUILD_PACKET), *args])

    for source, destination in (
        (packet_path, inputs_dir / "review-packet.md"),
        (metadata_path, inputs_dir / "review-packet.metadata.json"),
        (scope_of_work_path, inputs_dir / "scope-of-work.md"),
    ):
        if source.is_file():
            shutil.copyfile(source, destination)

    return {
        "status": "rerendered",
        "artifact_dir": str(artifact_dir),
        "round": round_index,
        "archive_dir": str(round_dir),
        "archived": archived,
        "review_packet": str(packet_path),
        "scope_of_work": str(scope_of_work_path),
    }


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_json_artifact_identity(path: Path | None) -> tuple[str | None, str | None]:
    if path is None:
        return None, None
    expanded = path.expanduser()
    if expanded.is_symlink() or not expanded.is_file():
        raise ValueError(f"JSON artifact must be a regular file, not a symlink: {expanded}")
    resolved = expanded.resolve()
    try:
        encoded = expanded.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must contain an object: {resolved}")
    canonical_encoded = canonical_json_bytes(payload)
    if encoded != canonical_encoded:
        raise ValueError(f"JSON artifact must use canonical JSON bytes: {resolved}")
    digest = hashlib.sha256(canonical_encoded).hexdigest()
    sidecar = Path(f"{expanded}.sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ValueError(f"JSON artifact digest sidecar must be a regular file: {sidecar}")
    try:
        declared_digest = sidecar.read_bytes()
    except OSError as exc:
        raise ValueError(f"JSON artifact digest sidecar is required: {sidecar}") from exc
    if declared_digest != f"{digest}\n".encode("ascii"):
        raise ValueError(f"JSON artifact digest mismatch: {resolved}")
    return str(resolved), digest


def scope_contract_v3_extensions(
    *,
    review_kind: str = "implementation",
    base_ref: str | None = None,
    remote_name: str | None = None,
    comparison_base_tip: str | None = None,
    reviewed_base_tip: str | None = None,
    reviewed_task_tip: str | None = None,
    candidate_task_tip: str | None = None,
    base_delta_paths: list[str] | None = None,
    task_change_paths: list[str] | None = None,
    interaction_primary_paths: list[str] | None = None,
    base_delta_protected_paths: list[str] | None = None,
    bootstrap_receipt: Path | None = None,
    candidate_evidence: Path | None = None,
    integration_candidate_receipt: Path | None = None,
) -> dict[str, Any]:
    if review_kind not in REVIEW_KINDS:
        raise ValueError(f"unsupported review kind: {review_kind}")
    bootstrap_path, bootstrap_digest = canonical_json_artifact_identity(bootstrap_receipt)
    candidate_path, candidate_digest = canonical_json_artifact_identity(candidate_evidence)
    integration_path, integration_digest = canonical_json_artifact_identity(
        integration_candidate_receipt
    )
    return {
        "review_kind": review_kind,
        "base_ref": base_ref,
        "remote_name": remote_name,
        "comparison_base_tip": comparison_base_tip,
        "reviewed_base_tip": reviewed_base_tip,
        "reviewed_task_tip": reviewed_task_tip,
        "candidate_task_tip": candidate_task_tip,
        "base_delta_paths": normalized_scope_list(base_delta_paths or []),
        "task_change_paths": normalized_scope_list(task_change_paths or []),
        "interaction_primary_paths": normalized_scope_list(interaction_primary_paths or []),
        "base_delta_protected_paths": normalized_scope_list(base_delta_protected_paths or []),
        "bootstrap_receipt_path": bootstrap_path,
        "bootstrap_receipt_digest": bootstrap_digest,
        "candidate_evidence_path": candidate_path,
        "candidate_evidence_digest": candidate_digest,
        "integration_candidate_receipt_path": integration_path,
        "integration_candidate_receipt_digest": integration_digest,
    }


def validated_integration_candidate_receipt(
    repo: Path, receipt_path: Path
) -> dict[str, Any]:
    receipt_value, _receipt_digest = canonical_json_artifact_identity(receipt_path)
    if receipt_value is None:
        raise ValueError("integration candidate receipt is required")
    receipt = json.loads(Path(receipt_value).read_text(encoding="utf-8"))
    git_dir = Path(run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo).strip()).resolve()
    expected_receipt_path = (
        git_dir
        / "base-branch-sync"
        / "reviewed-task-handback-integration-candidate.json"
    )
    if Path(receipt_value) != expected_receipt_path:
        raise ValueError("integration candidate receipt must use the task git-dir path")
    common_dir = Path(
        run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo,
        ).strip()
    ).resolve()
    required_identity = {
        "schema_name": "reviewed-task-handback-integration-candidate",
        "schema_version": 1,
        "status": "INTEGRATION_DELTA_RVF_REQUIRED",
        "risk_classification": "MEDIUM",
        "git_common_directory_realpath": str(common_dir),
        "task_worktree_realpath": str(repo),
    }
    if any(receipt.get(name) != value for name, value in required_identity.items()):
        raise ValueError("integration candidate receipt identity/status is invalid")
    required_string_fields = (
        "base_ref",
        "remote_name",
        "reviewed_candidate_evidence_path",
        "reviewed_candidate_evidence_sha256",
        "reviewed_base_tip",
        "base_tip_at_candidate",
        "reviewed_task_tip",
        "candidate_task_tip",
    )
    if any(
        not isinstance(receipt.get(name), str) or not receipt[name]
        for name in required_string_fields
    ):
        raise ValueError("integration candidate receipt is missing required identity fields")
    configured_candidate_evidence_path = Path(
        receipt["reviewed_candidate_evidence_path"]
    )
    expected_candidate_evidence_path = (
        git_dir / "rvf" / "evidence" / "reviewed-task-candidate-evidence.json"
    )
    if configured_candidate_evidence_path != expected_candidate_evidence_path:
        raise ValueError("integration receipt parent evidence path is invalid")
    _candidate_path, candidate_digest = canonical_json_artifact_identity(
        configured_candidate_evidence_path
    )
    if candidate_digest != receipt["reviewed_candidate_evidence_sha256"]:
        raise ValueError("integration receipt parent evidence digest mismatch")
    for name in (
        "base_delta_paths",
        "base_delta_protected_paths",
        "task_change_paths",
        "interaction_primary_paths",
    ):
        values = receipt.get(name)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"integration candidate receipt {name} is invalid")
    current_head = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    if receipt["candidate_task_tip"] != current_head:
        raise ValueError("integration candidate receipt candidate task tip is stale")
    for tip_name in (
        "reviewed_base_tip",
        "base_tip_at_candidate",
        "reviewed_task_tip",
        "candidate_task_tip",
    ):
        run(["git", "cat-file", "-e", f"{receipt[tip_name]}^{{commit}}"], cwd=repo)

    def changed_paths(older_tip: str, newer_tip: str) -> list[str]:
        completed = subprocess.run(
            ["git", "diff", "--name-only", "-z", older_tip, newer_tip],
            cwd=repo,
            env=git_repository_location_clean_environment(),
            check=True,
            capture_output=True,
        )
        return sorted(
            path.decode("utf-8", "surrogateescape")
            for path in completed.stdout.split(b"\0")
            if path
        )

    expected_base_delta_paths = changed_paths(
        receipt["reviewed_base_tip"], receipt["base_tip_at_candidate"]
    )
    expected_task_change_paths = changed_paths(
        receipt["reviewed_base_tip"], receipt["reviewed_task_tip"]
    )
    if (
        receipt["base_delta_paths"] != expected_base_delta_paths
        or receipt["task_change_paths"] != expected_task_change_paths
        or receipt["interaction_primary_paths"]
        != sorted(set(expected_base_delta_paths) & set(expected_task_change_paths))
    ):
        raise ValueError("integration candidate receipt delta facts do not match fixed tips")
    return receipt


def write_scope_contract(
    *,
    path: Path,
    run_id: str,
    repo: Path,
    created_at: str,
    scope_mode: str,
    primary_files: list[str],
    background_files: list[str],
    protected_files: list[str],
    canonical_issues: list[dict[str, Any]],
    fix_allowlist: list[str],
    excluded_path_prefixes: list[str],
    start_snapshot_path: Path,
    review_packet_path: Path,
    session_manifest_path: Path | None,
    scope_of_work_path: Path | None,
    review_packet_metadata_path: Path,
    primary_units: list[str] | None = None,
    tracker_lease_id: str | None = None,
    tracker_scope_hash: str | None = None,
    tracker_transcript_max_line_number: int | None = None,
    scope_contract_extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    extensions = dict(scope_contract_extensions or scope_contract_v3_extensions())
    canonical_scope = {
        "version": SCOPE_CONTRACT_VERSION,
        "repo": str(repo),
        "scope_mode": scope_mode,
        "primary_files": primary_files,
        "background_files": background_files,
        "protected_files": protected_files,
        "canonical_issues": canonical_issues,
        "fix_allowlist": fix_allowlist,
        "excluded_path_prefixes": excluded_path_prefixes,
        **extensions,
    }
    scope_hash = hashlib.sha256(canonical_json_bytes(canonical_scope)).hexdigest()
    contract: dict[str, Any] = {
        "version": SCOPE_CONTRACT_VERSION,
        "run_id": run_id,
        "repo": str(repo),
        "created_at": created_at,
        "scope_mode": scope_mode,
        "scope_hash": scope_hash,
        "primary_files": primary_files,
        "background_files": background_files,
        "protected_files": protected_files,
        "canonical_issues": canonical_issues,
        "fix_allowlist": fix_allowlist,
        "primary_units": primary_units,
        "tracker_lease_id": tracker_lease_id,
        "tracker_scope_hash": tracker_scope_hash,
        "tracker_transcript_max_line_number": tracker_transcript_max_line_number,
        "start_snapshot_path": str(start_snapshot_path),
        "review_packet_path": str(review_packet_path),
        "session_manifest_path": str(session_manifest_path) if session_manifest_path is not None else None,
        "scope_of_work_path": str(scope_of_work_path) if scope_of_work_path is not None else None,
        "review_packet_metadata_path": str(review_packet_metadata_path),
        "canonical_scope": canonical_scope,
        **extensions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return contract


def copy_bootstrap_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _ignore_prefix_matches(path: str, exclude_prefixes: list[str]) -> bool:
    normalized = path.strip().replace("\\", "/").lstrip("/")
    for raw in exclude_prefixes:
        if not isinstance(raw, str) or not raw.strip():
            continue
        prefix = raw.strip().replace("\\", "/").lstrip("/")
        if prefix.endswith("/"):
            if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
                return True
        else:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
    return False


def _path_bootstrap_bytes(repo: Path, rel_path: str) -> int:
    target = repo / rel_path
    try:
        if target.is_symlink():
            return 0
        if target.is_file():
            return target.stat().st_size
        if target.is_dir():
            total = 0
            for child in target.rglob("*"):
                if child.is_file() and not child.is_symlink():
                    try:
                        total += child.stat().st_size
                    except OSError:
                        continue
            return total
    except OSError:
        return 0
    return 0


def build_worktree_bootstrap(
    *,
    repo: Path,
    artifact_dir: Path,
    packet_metadata: dict[str, Any],
) -> dict[str, Any]:
    if packet_metadata.get("tracker_scope_present"):
        session_owned_dirty = normalized_scope_list(metadata_list(packet_metadata.get("tracker_scope_paths")))
    else:
        session_owned_dirty = normalized_scope_list(
            metadata_list(packet_metadata.get("session_owned_dirty_paths"))
        )
    unattributed_dirty = normalized_scope_list(
        metadata_list(packet_metadata.get("unattributed_dirty_paths"))
    )
    exclude_prefixes = [
        prefix
        for prefix in metadata_list(packet_metadata.get("excluded_path_prefixes"))
        if isinstance(prefix, str) and prefix.strip()
    ]

    filtered_session_owned: list[str] = []
    filtered_unattributed: list[str] = []
    ignored_paths: list[str] = []
    for path in session_owned_dirty:
        if _ignore_prefix_matches(path, exclude_prefixes):
            ignored_paths.append(path)
        else:
            filtered_session_owned.append(path)
    for path in unattributed_dirty:
        if _ignore_prefix_matches(path, exclude_prefixes):
            ignored_paths.append(path)
            continue
        if path in filtered_session_owned:
            continue
        filtered_unattributed.append(path)

    owned_dirty = normalized_scope_list(filtered_session_owned + filtered_unattributed)
    bootstrap_kind = "full-dirty" if filtered_unattributed else "session-owned-only"

    patch_path = artifact_dir / "worktree-bootstrap.patch"
    files_dir = artifact_dir / "worktree-bootstrap-files"
    bootstrap_path = artifact_dir / "worktree-bootstrap.json"
    files_dir.mkdir(parents=True, exist_ok=True)

    tracked_paths = [path for path in owned_dirty if is_tracked_path(repo, path)]
    patch_text = ""
    if tracked_paths:
        diff_output = run_git(repo, ["diff", "--binary", "--find-renames", "HEAD", "--", *tracked_paths])
        patch_text = diff_output if isinstance(diff_output, str) else diff_output.decode("utf-8", "replace")
    patch_path.write_text(patch_text, encoding="utf-8")

    untracked_files: list[dict[str, str]] = []
    for rel_path in owned_dirty:
        if rel_path in tracked_paths:
            continue
        source = repo / rel_path
        if not source.exists() and not source.is_symlink():
            continue
        stored_name = safe_stored_name(rel_path)
        stored_path = files_dir / stored_name
        copy_bootstrap_path(source, stored_path)
        untracked_files.append({"path": rel_path, "stored_path": str(stored_path)})

    unattributed_bytes = sum(_path_bootstrap_bytes(repo, path) for path in filtered_unattributed)
    total_bootstrap_bytes = sum(_path_bootstrap_bytes(repo, path) for path in owned_dirty)

    head = str(run_git(repo, ["rev-parse", "HEAD"])).strip()
    payload = {
        "repo": str(repo),
        "base_ref": head,
        "patch_file": str(patch_path),
        "files_dir": str(files_dir),
        "owned_dirty_paths": owned_dirty,
        "session_owned_dirty_paths": filtered_session_owned,
        "unattributed_dirty_paths": filtered_unattributed,
        "ignored_dirty_paths": sorted(set(ignored_paths)),
        "tracked_paths": tracked_paths,
        "untracked_files": untracked_files,
        "bootstrap_kind": bootstrap_kind,
        "unattributed_path_count": len(filtered_unattributed),
        "unattributed_bytes": unattributed_bytes,
        "total_bootstrap_bytes": total_bootstrap_bytes,
        "apply_helper": str(SKILL_DIR / "scripts" / "apply_worktree_bootstrap.py"),
    }
    bootstrap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "metadata": payload,
        "metadata_path": bootstrap_path,
        "patch_path": patch_path,
        "files_dir": files_dir,
    }


def prepare_run(
    *,
    repo: Path,
    session_context: Path | None,
    session_manifest: Path | None,
    transcript: Path | None,
    base_dir: Path,
    max_file_bytes: int,
    max_packet_bytes: int,
    primary_files: list[str],
    background_files: list[str],
    exclude_path_prefixes: list[str],
    allow_missing_session_context: bool = False,
    rvf_run_id: str | None = None,
    rvf_run_dir: Path | None = None,
    rvf_backend: str | None = None,
    tracker_scope: Path | None = None,
    base_ref: str | None = None,
    committed_baseline: str | None = None,
    committed_base_ref: str | None = None,
    local_base_ref: str | None = None,
    scope_contract_extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = git_root(repo)

    verified_boundary_receipt = require_review_start_boundary_for_prepare(
        repo=str(root), receipt=(scope_contract_extensions or {}).get("review_start_boundary_receipt"),
    )
    scope_contract_extensions = {**dict(scope_contract_extensions or scope_contract_v3_extensions()), "review_start_boundary_receipt": verified_boundary_receipt}
    resolved_local_base_ref = resolve_local_base_ref(
        root,
        explicit_local_base_ref=local_base_ref,
        committed_base_ref=committed_base_ref,
    )
    refuse_explicit_local_base_ref_that_bypasses_opt_in(
        root,
        explicit_local_base_ref=local_base_ref,
        committed_base_ref=committed_base_ref,
        resolved_local_base_ref=resolved_local_base_ref,
    )
    pre_review_sync_state = pre_review_base_sync_state(root, resolved_local_base_ref)
    if pre_review_sync_state is not None and pre_review_sync_state["status"] == "bootstrap_required_before_rvf":
        raise BootstrapRequiredBeforeRvf(pre_review_sync_state)
    # N5/K4：把本轮已校验的 bootstrap receipt 与它的 digest 捕获到函数作用域，供 intent artifact
    # 的 subject binding 使用（digest 是 anti-replay 直接锚，receipt 自身按 schema 不得内嵌它）。
    validated_bootstrap_receipt_payload: dict[str, Any] | None = None
    validated_bootstrap_receipt_digest: str | None = None
    effective_scope_contract_extensions = dict(
        scope_contract_extensions or scope_contract_v3_extensions()
    )
    if pre_review_sync_state is not None:
        configured_base_ref = effective_scope_contract_extensions.get("base_ref")
        if configured_base_ref not in {None, resolved_local_base_ref}:
            raise ValueError("configured base_ref conflicts with the resolved local base ref")
        effective_scope_contract_extensions["base_ref"] = resolved_local_base_ref
        if not effective_scope_contract_extensions.get("remote_name"):
            effective_scope_contract_extensions["remote_name"] = "origin"
        default_tip_values = {
            "comparison_base_tip": pre_review_sync_state["local_base_tip"],
            "reviewed_base_tip": pre_review_sync_state["local_base_tip"],
            "reviewed_task_tip": pre_review_sync_state["task_head"],
            "candidate_task_tip": pre_review_sync_state["task_head"],
        }
        for name, value in default_tip_values.items():
            if not effective_scope_contract_extensions.get(name):
                effective_scope_contract_extensions[name] = value
        configured_receipt_path = effective_scope_contract_extensions.get("bootstrap_receipt_path")
        receipt_path = (
            Path(configured_receipt_path)
            if isinstance(configured_receipt_path, str)
            else default_bootstrap_receipt(root, pre_review_sync_state)
        )
        if receipt_path is not None:
            receipt_payload = validated_bootstrap_receipt(root, pre_review_sync_state, receipt_path)
            receipt_value, receipt_digest = canonical_json_artifact_identity(receipt_path)
            validated_bootstrap_receipt_payload = receipt_payload
            validated_bootstrap_receipt_digest = receipt_digest
            configured_receipt_digest = effective_scope_contract_extensions.get(
                "bootstrap_receipt_digest"
            )
            if configured_receipt_digest not in {None, receipt_digest}:
                raise ValueError("configured bootstrap receipt digest does not match its artifact")
            effective_scope_contract_extensions["bootstrap_receipt_path"] = receipt_value
            effective_scope_contract_extensions["bootstrap_receipt_digest"] = receipt_digest
            for name in (
                "base_delta_paths",
                "task_change_paths",
                "interaction_primary_paths",
            ):
                receipt_paths = receipt_payload[name]
                configured_paths = effective_scope_contract_extensions.get(name)
                if configured_paths:
                    if normalized_scope_list(configured_paths) != receipt_paths:
                        raise ValueError(f"configured {name} conflicts with the bootstrap receipt")
                else:
                    effective_scope_contract_extensions[name] = receipt_paths
    integration_receipt_value = effective_scope_contract_extensions.get(
        "integration_candidate_receipt_path"
    )
    if integration_receipt_value is not None:
        if effective_scope_contract_extensions.get("review_kind") != "base_drift_integration":
            raise ValueError(
                "integration candidate receipt requires review_kind=base_drift_integration"
            )
        integration_receipt = validated_integration_candidate_receipt(
            root, Path(str(integration_receipt_value))
        )
        derived_fields: dict[str, Any] = {
            "base_ref": integration_receipt["base_ref"],
            "remote_name": integration_receipt["remote_name"],
            "comparison_base_tip": integration_receipt["base_tip_at_candidate"],
            "reviewed_base_tip": integration_receipt["base_tip_at_candidate"],
            "reviewed_task_tip": integration_receipt["reviewed_task_tip"],
            "candidate_task_tip": integration_receipt["candidate_task_tip"],
            "base_delta_paths": integration_receipt["base_delta_paths"],
            "base_delta_protected_paths": integration_receipt[
                "base_delta_protected_paths"
            ],
            "task_change_paths": integration_receipt["task_change_paths"],
            "interaction_primary_paths": integration_receipt[
                "interaction_primary_paths"
            ],
            "candidate_evidence_path": integration_receipt[
                "reviewed_candidate_evidence_path"
            ],
            "candidate_evidence_digest": integration_receipt[
                "reviewed_candidate_evidence_sha256"
            ],
        }
        for name, derived_value in derived_fields.items():
            configured_value = effective_scope_contract_extensions.get(name)
            configured_value_is_empty = configured_value is None or configured_value == "" or configured_value == []
            if not configured_value_is_empty and configured_value != derived_value:
                raise ValueError(
                    f"configured {name} conflicts with the integration candidate receipt"
                )
            effective_scope_contract_extensions[name] = derived_value
    elif effective_scope_contract_extensions.get("review_kind") == "base_drift_integration":
        raise ValueError(
            "base_drift_integration requires a canonical integration candidate receipt"
        )
    # An explicit --base-ref (the documented manual lever for reviewing already-committed work)
    # must also drive the manifest's committed-round attribution; otherwise a clean worktree
    # produces empty owned paths and the packet build aborts with "no owned paths". Derive the
    # committed baseline from it when the caller did not already supply one.
    if base_ref and not committed_baseline:
        committed_baseline = base_ref
    canonical_backend = normalize_rvf_backend(rvf_backend) or "manual"
    rvf_run_id, rvf_run_dir = bind_review_start_boundary_to_one_run(
        receipt=verified_boundary_receipt, run_id=rvf_run_id, run_dir=rvf_run_dir,
    )
    ledger = start_run(
        "prepare-run",
        repo=str(root),
        cwd=str(root),
        run_id=rvf_run_id,
        run_dir=rvf_run_dir,
    )
    ledger.event(
        phase="prepare",
        event="started",
        status="started",
        reason_code="prepare_started",
        repo=str(root),
        cwd=str(root),
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    if ledger.available:
        artifact_dir = ledger.artifacts_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact_dir = Path(tempfile.mkdtemp(prefix=f"{timestamp}-{safe_repo_name(root)}-", dir=base_dir))

    inputs_dir = artifact_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    packet_path = artifact_dir / "review-packet.md"
    metadata_path = artifact_dir / "review-packet.metadata.json"
    snapshot_path = artifact_dir / "before-workspace-snapshot.json"
    scope_of_work_path = artifact_dir / "scope-of-work.md"
    session_manifest_path = artifact_dir / "session-manifest.json"
    input_packet_path = inputs_dir / "review-packet.md"
    input_metadata_path = inputs_dir / "review-packet.metadata.json"
    input_snapshot_path = inputs_dir / "before-workspace-snapshot.json"
    input_scope_of_work_path = inputs_dir / "scope-of-work.md"
    input_session_manifest_path = inputs_dir / "session-manifest.json"
    scope_contract_path = inputs_dir / "scope.contract.json"
    review_env_path = artifact_dir / "review-env.sh"
    review_agent_context_path = artifact_dir / "review-agent-context.md"

    packet_session_context = session_context
    if session_context is not None:
        shutil.copyfile(session_context, scope_of_work_path)
        packet_session_context = scope_of_work_path

    # ------------------------------------------------------------------ N5 / K4
    # 消费 base-branch-sync 的 intent artifact，注入 reviewer / fixer context。
    #
    # **只对 opt-in 仓库做**（``pre_review_sync_state is not None`` 即 opt-in）：非 opt-in 仓库
    # 既没有 base 单向吸收、也不会有 artifact，给它们写日志只会污染退出条件第 4 条的分母。
    #
    # **三种 status 一律放行**：consumed / missing / invalid 走同一条路径，绝不 raise。
    # 两侧部署节奏天然不同步（my-ai-setup 软链即时生效、RVF 要跑 /rvf-local-deploy），
    # 因此「artifact 不在」是常规事件而非异常——把它当异常就是再造一条 fail-closed 死路。
    intent_artifact_outcome: dict[str, Any] | None = None
    intent_artifact_status_path: Path | None = None
    if pre_review_sync_state is not None:
        intent_artifact_outcome = consumed_base_task_intent_reconciliation(
            root,
            validated_bootstrap_receipt_payload,
            validated_bootstrap_receipt_digest,
        )
        # 机器可校验的落点①：run 工件（事后可审计）。
        intent_artifact_status_path = artifact_dir / INTENT_ARTIFACT_STATUS_FILENAME
        intent_artifact_status_path.write_text(
            json.dumps(
                intent_artifact_status_artifact_payload(
                    intent_artifact_outcome, run_id=ledger.run_id
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        # 机器可校验的落点②：跨 run 的 append-only 日志（过渡机制退出条件第 4 条要它做统计）。
        # best-effort，永不阻断 prepare。
        intent_log_git_common_directory = (validated_bootstrap_receipt_payload or {}).get(
            "git_common_directory_realpath"
        )
        if not intent_log_git_common_directory:
            # `run()` 见非零退出即 raise。这条路径**只是可观测性记录**，绝不允许把 prepare 拖红
            # ——给它留一条能抛的缝，就等于在「一律放行」的契约里凿了个 fail-closed 出来。
            try:
                intent_log_git_common_directory = run(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    cwd=root,
                ).strip()
            except (OSError, RuntimeError):
                intent_log_git_common_directory = str(root)
        record_intent_artifact_consumption_outcome(
            outcome=intent_artifact_outcome,
            git_common_directory=intent_log_git_common_directory,
            task_worktree=str(root),
            rvf_run_id=ledger.run_id,
        )
        # 注入通道：追加到 scope-of-work 末尾。**只在已有 scope-of-work 时追加**——
        # 若本轮压根没传 session context，凭空造一个只含 intent 段的文件会改变
        # allow_missing_session_context 的既有语义。追加在末尾还保证 stub marker
        # （scope_of_work_is_untouched_stub 只看前 20 行）不会被我们误判成「已填实」。
        if packet_session_context is not None and scope_of_work_path.exists():
            with scope_of_work_path.open("a", encoding="utf-8") as handle:
                handle.write("\n" + intent_context_markdown(intent_artifact_outcome))
    # ---------------------------------------------------------------- end N5 / K4

    packet_session_manifest: Path | None = None
    source_session_manifest: str | None = None
    if transcript is not None:
        manifest_cmd = [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(root),
            "--transcript",
            str(transcript),
            "--output",
            str(session_manifest_path),
            "--tracker-run-id",
            ledger.run_id,
            # Bash / 子代理 / mv·cp 写的文件按分层证据补判归属（08ac4）；只在 start 与 prepare 开启。
            "--enable-layered-session-write-attribution",
        ]
        if committed_baseline:
            manifest_cmd.extend(["--committed-baseline", committed_baseline])
        if committed_base_ref:
            manifest_cmd.extend(["--committed-base-ref", committed_base_ref])
        run(manifest_cmd)
        packet_session_manifest = session_manifest_path
        source_session_manifest = f"transcript:{transcript}"
    elif session_manifest is not None:
        shutil.copyfile(session_manifest, session_manifest_path)
        packet_session_manifest = session_manifest_path
        source_session_manifest = str(session_manifest)

    tracker_scope_payload: dict[str, Any] | None = None
    artifact_tracker_scope_path: Path | None = None
    if tracker_scope is not None:
        if packet_session_manifest is None:
            raise ValueError(
                "--tracker-scope requires --session-manifest or --transcript so the "
                "tracker_scope payload can be spliced into manifest.tracker.tracker_scope"
            )
        tracker_scope_payload = load_tracker_scope(tracker_scope)
        try:
            manifest_dict = json.loads(session_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"failed to read session manifest for tracker_scope splice: {exc}")
        if not isinstance(manifest_dict, dict):
            raise ValueError("session manifest is not a JSON object; cannot splice tracker_scope")
        manifest_tracker = manifest_dict.get("tracker")
        if not isinstance(manifest_tracker, dict):
            manifest_tracker = {}
            manifest_dict["tracker"] = manifest_tracker
        manifest_tracker["tracker_scope"] = tracker_scope_payload
        session_manifest_path.write_text(
            json.dumps(manifest_dict, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifact_tracker_scope_path = artifact_dir / "tracker-scope.json"
        if tracker_scope.resolve() != artifact_tracker_scope_path.resolve():
            shutil.copyfile(tracker_scope, artifact_tracker_scope_path)

    packet_cmd = [
        sys.executable,
        str(BUILD_PACKET),
        "--repo",
        str(root),
        "--output",
        str(packet_path),
        "--metadata-output",
        str(metadata_path),
        "--max-file-bytes",
        str(max_file_bytes),
    ]
    if max_packet_bytes:
        packet_cmd.extend(["--max-packet-bytes", str(max_packet_bytes)])
    if packet_session_context is not None:
        packet_cmd.extend(["--session-context", str(packet_session_context)])
    if packet_session_manifest is not None:
        packet_cmd.extend(["--session-manifest", str(packet_session_manifest)])
    if allow_missing_session_context:
        packet_cmd.append("--allow-missing-session-context")
    if base_ref:
        packet_cmd.extend(["--base-ref", base_ref])
    for path in primary_files:
        packet_cmd.extend(["--primary-file", path])
    for path in background_files:
        packet_cmd.extend(["--background-file", path])
    for prefix in exclude_path_prefixes:
        packet_cmd.extend(["--exclude-path-prefix", prefix])
    run(packet_cmd)
    # 冻结这次 packet 渲染的**确切**参数，供 scope-of-work 重渲复用同一个 renderer。
    # 没有它，「agent 写完真 scope-of-work 之后怎么让它进 packet」就只能靠另造一份渲染逻辑
    # （必然与这里漂移），或者重跑整个 prepare（被 completed 态幂等直接短路，改了也没用）。
    write_review_packet_build_command(inputs_dir, packet_cmd)

    run(
        [
            sys.executable,
            str(WORKSPACE_SNAPSHOT),
            "capture",
            "--repo",
            str(root),
            "--output",
            str(snapshot_path),
        ]
    )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    shutil.copyfile(packet_path, input_packet_path)
    shutil.copyfile(metadata_path, input_metadata_path)
    shutil.copyfile(snapshot_path, input_snapshot_path)
    if scope_of_work_path.exists():
        shutil.copyfile(scope_of_work_path, input_scope_of_work_path)
    if session_manifest_path.exists():
        shutil.copyfile(session_manifest_path, input_session_manifest_path)
    resolved_input_tracker_scope_path: Path | None = None
    resolved_artifact_tracker_scope_path: Path | None = None
    if artifact_tracker_scope_path is not None and artifact_tracker_scope_path.exists():
        input_tracker_scope_path = inputs_dir / "tracker-scope.json"
        shutil.copyfile(artifact_tracker_scope_path, input_tracker_scope_path)
        resolved_input_tracker_scope_path = input_tracker_scope_path.resolve()
        resolved_artifact_tracker_scope_path = artifact_tracker_scope_path.resolve()
    resolved_input_packet_path = input_packet_path.resolve()
    resolved_input_metadata_path = input_metadata_path.resolve()
    resolved_input_snapshot_path = input_snapshot_path.resolve()
    resolved_input_scope_of_work_path = input_scope_of_work_path.resolve() if input_scope_of_work_path.exists() else None
    resolved_input_session_manifest_path = (
        input_session_manifest_path.resolve() if input_session_manifest_path.exists() else None
    )
    if metadata.get("session_manifest_provided"):
        scope_mode = "session-owned"
    elif primary_files or background_files or exclude_path_prefixes:
        scope_mode = "custom"
    else:
        scope_mode = "manual-all-uncommitted"
    manual_dirty_paths = dirty_paths(root, metadata_list(metadata.get("excluded_path_prefixes"))) if scope_mode == "manual-all-uncommitted" else []
    if tracker_scope_payload is not None:
        primary_scope_files = normalized_scope_list(tracker_scope_payload["paths"])
        non_allocated_dirty = [
            path
            for path in normalized_scope_list(
                metadata_list(metadata.get("session_owned_dirty_paths"))
                + metadata_list(metadata.get("unattributed_dirty_paths"))
            )
            if path not in set(primary_scope_files)
        ]
        background_scope_files = normalized_scope_list(background_files + non_allocated_dirty)
        contract_primary_units: list[str] | None = sorted(set(tracker_scope_payload["unit_ids"]))
        contract_tracker_lease_id: str | None = tracker_scope_payload["lease_id"]
        contract_tracker_lease_ttl_seconds: int | None = (
            tracker_scope_payload.get("lease_ttl_seconds")
            if isinstance(tracker_scope_payload.get("lease_ttl_seconds"), int)
            else None
        )
        contract_tracker_scope_hash: str | None = tracker_scope_payload["scope_hash"]
        contract_tracker_transcript_max_line_number: int | None = (
            tracker_scope_payload.get("transcript_max_line_number")
            if isinstance(tracker_scope_payload.get("transcript_max_line_number"), int)
            else None
        )
    else:
        primary_scope_files = normalized_scope_list(
            primary_files + metadata_list(metadata.get("session_owned_paths")) + manual_dirty_paths
        )
        background_scope_files = normalized_scope_list(
            background_files + metadata_list(metadata.get("unattributed_dirty_paths"))
        )
        contract_primary_units = None
        contract_tracker_lease_id = None
        contract_tracker_lease_ttl_seconds = None
        contract_tracker_scope_hash = None
        contract_tracker_transcript_max_line_number = None
    protected_files = background_scope_files
    created_at = datetime.now(timezone.utc).isoformat()
    scope_contract = write_scope_contract(
        path=scope_contract_path,
        run_id=ledger.run_id,
        repo=root,
        created_at=created_at,
        scope_mode=scope_mode,
        primary_files=primary_scope_files,
        background_files=background_scope_files,
        protected_files=protected_files,
        canonical_issues=[],
        fix_allowlist=primary_scope_files,
        excluded_path_prefixes=normalized_scope_list(metadata_list(metadata.get("excluded_path_prefixes"))),
        start_snapshot_path=resolved_input_snapshot_path,
        review_packet_path=resolved_input_packet_path,
        session_manifest_path=resolved_input_session_manifest_path,
        scope_of_work_path=resolved_input_scope_of_work_path,
        review_packet_metadata_path=resolved_input_metadata_path,
        primary_units=contract_primary_units,
        tracker_lease_id=contract_tracker_lease_id,
        tracker_scope_hash=contract_tracker_scope_hash,
        tracker_transcript_max_line_number=contract_tracker_transcript_max_line_number,
        scope_contract_extensions=effective_scope_contract_extensions,
    )
    bootstrap = build_worktree_bootstrap(
        repo=root,
        artifact_dir=artifact_dir,
        packet_metadata=metadata,
    )
    scope_path = scope_of_work_path.resolve() if session_context is not None else None
    manifest_path = session_manifest_path.resolve() if packet_session_manifest is not None else None

    tracker_dir_path: Path | None = None
    tracker_repo_key_value: str | None = None
    if manifest_path is not None and manifest_path.exists():
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest_payload = {}
        if isinstance(manifest_payload, dict):
            tracker_meta = manifest_payload.get("tracker") if isinstance(manifest_payload.get("tracker"), dict) else None
            if tracker_meta is not None:
                if isinstance(tracker_meta.get("tracker_dir"), str) and tracker_meta["tracker_dir"]:
                    tracker_dir_path = Path(tracker_meta["tracker_dir"]).expanduser()
                if isinstance(tracker_meta.get("repo_key"), str) and tracker_meta["repo_key"]:
                    tracker_repo_key_value = tracker_meta["repo_key"]
                heartbeat_session = tracker_meta.get("session_id") or manifest_payload.get("session_id")
                if isinstance(heartbeat_session, str) and heartbeat_session:
                    try:
                        reviewable_unit_diff_tracker.heartbeat(
                            root,
                            session_id=heartbeat_session,
                            run_id=ledger.run_id,
                            lease_id=contract_tracker_lease_id,
                            ttl_seconds=contract_tracker_lease_ttl_seconds,
                            rvf_state_phase="prepare",
                            rvf_backend=canonical_backend,
                        )
                    except Exception:
                        pass
    # 主 dispatch harness 解析（供 dispatch_reviewers.py 只读）。cursor 永远不会被
    # transcript 探测命中——只能经显式 RVF_MAIN_HARNESS 覆盖到达（Q3）。优先级与
    # dispatch_reviewers.resolve_main_harness 对齐：显式 env 覆盖 > transcript 探测 > 默认 codex。
    # 必须在这里 honor 显式 env，否则下面导出的 review-env.sh 会把用户设的
    # RVF_MAIN_HARNESS=cursor 覆盖回 codex，令 env 覆盖路径在端到端失效。
    override_main_harness = os.environ.get("RVF_MAIN_HARNESS")
    if override_main_harness not in VALID_MAIN_HARNESSES:
        override_main_harness = None
    detected_main_harness = detect_transcript_format(transcript) if transcript is not None else None
    main_harness = override_main_harness or detected_main_harness or HOST_CODEX
    main_harness_source = (
        "env-override" if override_main_harness else ("transcript" if detected_main_harness else "default")
    )
    main_harness_path = inputs_dir / "main-harness.json"
    main_harness_path.write_text(
        json.dumps(
            {
                "main_harness": main_harness,
                "detected": detected_main_harness,
                "override": override_main_harness,
                "source": main_harness_source,
                "transcript": str(transcript) if transcript is not None else None,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    review_env, review_env_text = review_env_exports(
        repo=root,
        run_id=ledger.run_id,
        run_dir=ledger.run_dir,
        log_root=ledger.root,
        artifacts_dir=artifact_dir,
        inputs_dir=inputs_dir,
        scope_contract_path=scope_contract_path,
        scope_of_work_path=scope_path,
        session_manifest_path=manifest_path,
        packet_path=packet_path,
        metadata_path=metadata_path,
        snapshot_path=snapshot_path,
        bootstrap_metadata_path=bootstrap["metadata_path"],
        tracker_dir=tracker_dir_path,
        tracker_repo_key=tracker_repo_key_value,
        tracker_scope_path=resolved_input_tracker_scope_path,
        rvf_backend=canonical_backend,
        parent_context_path=artifact_dir / "parent-conversation-context.md",
        main_harness=main_harness,
    )
    review_env_path.write_text(review_env_text, encoding="utf-8")
    review_agent_context = review_agent_context_text(
        repo=root,
        review_env_path=review_env_path,
        scope_of_work_path=scope_path,
        session_manifest_path=manifest_path,
    )
    review_agent_context_path.write_text(review_agent_context, encoding="utf-8")
    result = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "repo": str(root),
        "run_id": ledger.run_id,
        "run_dir": str(ledger.run_dir),
        "events_path": str(ledger.events_path),
        "summary_path": str(ledger.summary_path),
        "artifacts_dir": str(artifact_dir),
        "inputs_dir": str(inputs_dir),
        "main_harness": main_harness,
        "main_harness_file": str(main_harness_path),
        "scope_contract": str(scope_contract_path),
        "scope_contract_payload": scope_contract,
        # N5/K4：intent artifact 的消费结局。三种取值一律放行，这里只是把它做成机器事实。
        "intent_artifact_status": (
            intent_artifact_outcome.get("status")
            if intent_artifact_outcome is not None
            else None
        ),
        "intent_artifact_status_reason": (
            intent_artifact_outcome.get("reason")
            if intent_artifact_outcome is not None
            else None
        ),
        "intent_artifact_status_file": (
            str(intent_artifact_status_path)
            if intent_artifact_status_path is not None
            else None
        ),
        "review_packet": str(packet_path),
        "review_packet_metadata": str(metadata_path),
        "before_workspace_snapshot": str(snapshot_path),
        "input_review_packet": str(resolved_input_packet_path),
        "input_review_packet_metadata": str(resolved_input_metadata_path),
        "input_before_workspace_snapshot": str(resolved_input_snapshot_path),
        "input_scope_of_work_file": str(resolved_input_scope_of_work_path)
        if resolved_input_scope_of_work_path is not None
        else None,
        "input_session_manifest_file": str(resolved_input_session_manifest_path)
        if resolved_input_session_manifest_path is not None
        else None,
        "input_tracker_scope_file": str(resolved_input_tracker_scope_path)
        if resolved_input_tracker_scope_path is not None
        else None,
        "tracker_scope_file": str(resolved_artifact_tracker_scope_path)
        if resolved_artifact_tracker_scope_path is not None
        else None,
        "worktree_bootstrap": str(bootstrap["metadata_path"]),
        "worktree_bootstrap_patch": str(bootstrap["patch_path"]),
        "worktree_bootstrap_files_dir": str(bootstrap["files_dir"]),
        "worktree_bootstrap_metadata": bootstrap["metadata"],
        "scope_of_work_file": str(scope_path) if scope_path is not None else None,
        "session_manifest_file": str(manifest_path) if manifest_path is not None else None,
        "review_env_file": str(review_env_path),
        "review_env": review_env,
        "review_agent_context_file": str(review_agent_context_path),
        "review_agent_context": review_agent_context,
        "source_session_context": str(session_context) if session_context is not None else None,
        "source_session_manifest": source_session_manifest,
        "packet_bytes": metadata.get("packet_bytes"),
        "untracked_count": metadata.get("untracked_count"),
        "inlined_untracked_count": metadata.get("inlined_untracked_count"),
        "omitted_untracked_count": metadata.get("omitted_untracked_count"),
        "session_context": str(scope_path) if scope_path is not None else None,
        "session_context_provided": metadata.get("session_context_provided"),
        "session_context_bytes": metadata.get("session_context_bytes"),
        "session_manifest": str(manifest_path) if manifest_path is not None else None,
        "session_manifest_provided": metadata.get("session_manifest_provided"),
        "session_owned_path_count": metadata.get("session_owned_path_count"),
        "unattributed_dirty_paths": metadata.get("unattributed_dirty_paths"),
        "primary_files": primary_files,
        "background_files": background_files,
        "excluded_path_prefixes": metadata.get("excluded_path_prefixes"),
        "pre_review_base_sync_state": pre_review_sync_state,
        **rvf_state_fields(
            phase="prepare",
            backend=canonical_backend,
            backend_raw=rvf_backend or canonical_backend,
            scope_contract_path=scope_contract_path,
            scope_of_work_path=scope_path,
            review_packet_path=packet_path,
            session_manifest_path=manifest_path,
        ),
    }
    ledger.event(
        phase="prepare",
        event="completed",
        status="completed",
        reason_code="prepare_completed",
        repo=str(root),
        cwd=str(root),
        paths={
            "inputs": str(inputs_dir),
            "scope_contract": str(scope_contract_path),
            "review_packet": str(packet_path),
            "metadata": str(metadata_path),
            "snapshot": str(snapshot_path),
            "worktree_bootstrap": str(bootstrap["metadata_path"]),
            "scope_of_work": str(scope_of_work_path) if session_context is not None else None,
            "session_manifest": str(session_manifest_path) if packet_session_manifest is not None else None,
            "review_env": str(review_env_path),
            "review_agent_context": str(review_agent_context_path),
        },
        packet_bytes=metadata.get("packet_bytes"),
        **rvf_state_fields(
            phase="prepare",
            backend=canonical_backend,
            backend_raw=rvf_backend or canonical_backend,
            scope_contract_path=scope_contract_path,
            scope_of_work_path=scope_path,
            review_packet_path=packet_path,
            session_manifest_path=manifest_path,
        ),
    )
    ledger.summary(
        status="completed",
        reason_code="prepare_completed",
        message="review-validate-fix run prepared",
        **result,
    )
    return result


def _shared_workflow_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_prep_target_repo(payload: dict[str, Any]) -> Path:
    target_worktree = payload.get("target_worktree")
    if isinstance(target_worktree, str) and target_worktree.strip():
        candidate = Path(target_worktree).expanduser()
        if candidate.exists() and (candidate / ".git").exists():
            return candidate.resolve()
    for key in ("origin_cwd", "origin_repo"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidate = Path(value).expanduser()
            if candidate.exists():
                return candidate.resolve()
    raise ValueError("prep payload missing target_worktree / origin_cwd / origin_repo")


def _shared_workflow_artifacts(result: dict[str, Any]) -> dict[str, str | None]:
    return {
        # scope-of-work 重渲需要 artifact 根（冻结的 renderer 命令住在它的 inputs/ 下）。
        # 让消费者显式拿到它，而不是从 review_packet 的父目录反推。
        "artifacts_dir": result.get("artifacts_dir"),
        "scope_contract": result.get("scope_contract"),
        "review_packet": result.get("review_packet"),
        "review_packet_metadata": result.get("review_packet_metadata"),
        "review_env": result.get("review_env_file"),
        "review_agent_context": result.get("review_agent_context_file"),
        "worktree_bootstrap": result.get("worktree_bootstrap"),
        "session_manifest": result.get("session_manifest_file"),
        "scope_of_work": result.get("scope_of_work_file"),
        "intent_artifact_status_file": result.get("intent_artifact_status_file"),
    }


def scope_contract_v3_extensions_from_prep_payload(
    payload: Mapping[str, Any], rvf_run: Mapping[str, Any]
) -> dict[str, Any]:
    def value(name: str) -> Any:
        return rvf_run[name] if name in rvf_run else payload.get(name)

    review_kind = value("review_kind") or "implementation"
    if not isinstance(review_kind, str):
        raise ValueError("prep review_kind must be a string")
    base_ref = value("base_ref")
    remote_name = value("remote_name")
    for name, raw_value in (("base_ref", base_ref), ("remote_name", remote_name)):
        if raw_value is not None and (
            not isinstance(raw_value, str) or not raw_value.strip()
        ):
            raise ValueError(f"prep {name} must be a non-empty string")
    tips: dict[str, str | None] = {}
    for name in (
        "comparison_base_tip",
        "reviewed_base_tip",
        "reviewed_task_tip",
        "candidate_task_tip",
    ):
        raw = value(name)
        if raw is not None and (
            not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{40,64}", raw)
        ):
            raise ValueError(f"prep {name} must be a lowercase commit OID")
        tips[name] = raw
    path_lists: dict[str, list[str]] = {}
    for name in (
        "base_delta_paths",
        "task_change_paths",
        "interaction_primary_paths",
        "base_delta_protected_paths",
    ):
        raw = value(name)
        if raw is None:
            path_lists[name] = []
        elif not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError(f"prep {name} must be a list of strings")
        else:
            path_lists[name] = raw

    artifact_paths: dict[str, Path | None] = {}
    declared_digests: dict[str, str | None] = {}
    for prefix in (
        "bootstrap_receipt",
        "candidate_evidence",
        "integration_candidate_receipt",
    ):
        raw_path = value(f"{prefix}_path")
        raw_digest = value(f"{prefix}_digest")
        if (raw_path is None) != (raw_digest is None):
            raise ValueError(f"prep {prefix} path and digest must both be present")
        if raw_path is not None and (
            not isinstance(raw_path, str)
            or not isinstance(raw_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_digest)
        ):
            raise ValueError(f"prep {prefix} path/digest is invalid")
        artifact_paths[prefix] = Path(raw_path) if isinstance(raw_path, str) else None
        declared_digests[prefix] = raw_digest if isinstance(raw_digest, str) else None

    extensions = scope_contract_v3_extensions(
        review_kind=review_kind,
        base_ref=base_ref,
        remote_name=remote_name,
        **tips,
        **path_lists,
        bootstrap_receipt=artifact_paths["bootstrap_receipt"],
        candidate_evidence=artifact_paths["candidate_evidence"],
        integration_candidate_receipt=artifact_paths[
            "integration_candidate_receipt"
        ],
    )
    for prefix in (
        "bootstrap_receipt",
        "candidate_evidence",
        "integration_candidate_receipt",
    ):
        if declared_digests[prefix] != extensions[f"{prefix}_digest"]:
            raise ValueError(f"prep {prefix} digest does not match its canonical artifact")
    return extensions


def _bootstrap_gate_parent_attempt(
    existing_state: Any, rvf_run_id: str | None
) -> dict[str, str | None]:
    """本轮等待的**上一次尝试** run 血缘（供 `build_bootstrap_gate` 的 parent_attempt_* 参数）。

    同一个 run 上反复重放 prepare 不算「新一轮独立 RVF」，故 run_id 未变时把上一枚 gate 记的
    parent 原样带下去；只有当本轮确实换了 run（旧工件丢失后重开）才把旧 run 记成 parent。
    """
    previous = rvf_bootstrap_gate.gate_of(existing_state)
    if previous is None:
        return {"parent_attempt_run_id": None, "parent_attempt_run_dir": None}
    previous_run_id = previous.get("rvf_run_id")
    if previous_run_id and previous_run_id != rvf_run_id:
        return {
            "parent_attempt_run_id": previous_run_id,
            "parent_attempt_run_dir": previous.get("rvf_run_dir"),
        }
    return {
        "parent_attempt_run_id": previous.get("parent_attempt_run_id"),
        "parent_attempt_run_dir": previous.get("parent_attempt_run_dir"),
    }


def prepare_run_from_prep_file(
    prep: rvf_prep_file.PrepFileRecord,
    *,
    transcript_override: Path | None = None,
    extra_primary_files: list[str] | None = None,
    extra_background_files: list[str] | None = None,
    timeout_seconds: float = SHARED_WORKFLOW_DEFAULT_TIMEOUT_SECONDS,
    base_dir: Path | None = None,
    user_prompt_excerpt: str | None = None,
) -> dict[str, Any]:
    """Run prepare_run driven by the dispatch prep file. Idempotent on shared_workflow_state.

    Returns the prep payload's shared_workflow_state dict (after possibly updating the prep
    file). Raises on prep schema problems; prepare_run errors are caught and recorded as
    status=failed without raising.
    """
    payload = dict(prep.payload)
    rvf_run = payload.get("rvf_run") if isinstance(payload.get("rvf_run"), dict) else {}

    verified_boundary_receipt = require_review_start_boundary_for_prepare(
        repo=str(_resolve_prep_target_repo(payload)), receipt=rvf_run.get("review_start_boundary_receipt"),
        expected_session_id=str(payload.get("origin_session_id") or ""),
    )
    if verified_boundary_receipt.get("session_id") and payload.get("origin_session_id") != verified_boundary_receipt["session_id"]:
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "prep 来源会话与独立消息不一致")
    # 唯一归属先于 completed 缓存与 startup scope；拒绝重放不得留下正式工件。
    rvf_run_id, rvf_run_dir = bind_review_start_boundary_to_one_run(
        receipt=verified_boundary_receipt, run_id=rvf_run.get("run_id"), run_dir=rvf_run.get("run_dir"),
    )
    existing_state = rvf_run.get("shared_workflow_state") if isinstance(rvf_run, dict) else None
    if isinstance(existing_state, dict) and existing_state.get("status") == "completed":
        if existing_state.get("run_id") != rvf_run_id or existing_state.get("run_dir") != str(rvf_run_dir):
            raise ReviewStartBoundaryNotVerified(INVALIDATED, "prep 完成缓存与已冻结 run 身份不一致")
        # ⚠️ 这条幂等短路是「agent 写了真 scope-of-work 却进不了 packet」的结构性成因：packet 在
        # 首次 prepare 时就把 stub 内联冻结了，重跑 prepare 到这里直接返回、什么都不会重渲。
        # 要把新正文渲进 packet，走 ``rerender_scope_of_work_into_review_packet``（显式、带归档），
        # **不要**在这里加「检测到 scope-of-work 变了就重跑整轮」——那会顺带重算 scope/manifest/
        # tracker lease，把一次纯文档修订变成换了口径的另一轮。
        return dict(existing_state)

    target_flow = str(payload.get("target_flow") or "")
    rvf_backend = TARGET_FLOW_BACKEND.get(target_flow, "manual")
    tracker_scope_raw = rvf_run.get("tracker_scope_path") if isinstance(rvf_run, dict) else None
    tracker_scope = Path(tracker_scope_raw).expanduser() if isinstance(tracker_scope_raw, str) and tracker_scope_raw else None

    target_repo = _resolve_prep_target_repo(payload)

    # B1: the manual UPS hook stamps these when the session has committed-but-unreviewed work,
    # so prepare_run builds a committed-round manifest (git diff <baseline> in the packet)
    # instead of an empty git-diff-HEAD on a clean worktree — no git reset needed.
    committed_baseline = payload.get("committed_round_baseline")
    committed_baseline = committed_baseline.strip() if isinstance(committed_baseline, str) and committed_baseline.strip() else None
    committed_base_ref = payload.get("committed_round_base_ref")
    committed_base_ref = committed_base_ref.strip() if isinstance(committed_base_ref, str) and committed_base_ref.strip() else None
    local_base_ref_raw = payload.get("local_base_ref")
    local_base_ref = local_base_ref_raw.strip() if isinstance(local_base_ref_raw, str) and local_base_ref_raw.strip() else None
    contract_extensions = scope_contract_v3_extensions_from_prep_payload(payload, rvf_run)
    contract_extensions["review_start_boundary_receipt"] = rvf_run.get("review_start_boundary_receipt")

    transcript_path: Path | None = None
    transcript_candidate = transcript_override
    if transcript_candidate is None:
        for key in ("origin_transcript_path", "parent_thread_path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                transcript_candidate = Path(value).expanduser()
                break
    if transcript_candidate is not None and transcript_candidate.exists():
        transcript_path = transcript_candidate.resolve()

    artifacts_dir = rvf_run_dir / "artifacts" if rvf_run_dir is not None else None
    scope_of_work_path: Path | None = None
    if artifacts_dir is not None:
        candidate = artifacts_dir / "startup-scope-of-work.md"
        if candidate.exists():
            scope_of_work_path = candidate
    if scope_of_work_path is None and artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        scope_text = dispatch_scope_of_work_text(
            target_flow=target_flow,
            cwd=str(target_repo),
            parent_session_id=str(payload.get("origin_session_id") or ""),
            parent_thread_path=transcript_path,
            prompt_path=None,
            run_id=str(rvf_run_id or ""),
            run_dir=rvf_run_dir,
            user_prompt_excerpt=user_prompt_excerpt,
            # agentic 发起路径把决策点候选摘要的路径当骨架写进 stub，让 agent 逐条填而不是对着
            # 空白页现编（决策点已经算过这批路径，不重算）。
            candidate_paths=[
                str(path)
                for path in (payload.get("scope_of_work_candidate_paths") or [])
                if isinstance(path, str) and path.strip()
            ],
        )
        candidate = artifacts_dir / "startup-scope-of-work.md"
        candidate.write_text(scope_text, encoding="utf-8")
        scope_of_work_path = candidate

    base_dir_path = base_dir or default_base_dir()
    started_at = _shared_workflow_now()
    state: dict[str, Any] = {
        "started_at": started_at,
        "status": "pending",
        "target_flow": target_flow,
        "target_repo": str(target_repo),
        "rvf_backend": rvf_backend,
    }

    def _runner() -> dict[str, Any]:
        return prepare_run(
            repo=target_repo,
            session_context=scope_of_work_path,
            session_manifest=None,
            transcript=transcript_path,
            base_dir=base_dir_path,
            max_file_bytes=200_000,
            max_packet_bytes=0,
            primary_files=list(extra_primary_files or []),
            background_files=list(extra_background_files or []),
            exclude_path_prefixes=[],
            allow_missing_session_context=False,
            rvf_run_id=rvf_run_id,
            rvf_run_dir=rvf_run_dir,
            rvf_backend=rvf_backend,
            tracker_scope=tracker_scope,
            committed_baseline=committed_baseline,
            committed_base_ref=committed_base_ref,
            local_base_ref=local_base_ref,
            scope_contract_extensions=contract_extensions,
        )

    completed_state = state
    # Manage executor manually so a TimeoutError can return immediately. Using
    # `with ThreadPoolExecutor(...)` blocks at __exit__ until the worker thread
    # finishes, defeating the timeout. Cancelling pending futures and asking
    # shutdown not to wait lets the hook unblock; the background thread (which
    # may still be running prepare_run) is allowed to finish on its own.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_runner)
    try:
        result = future.result(timeout=timeout_seconds)
        completed_state = {
            **state,
            "status": "completed",
            "completed_at": _shared_workflow_now(),
            "artifacts": _shared_workflow_artifacts(result),
            "run_id": result.get("run_id"),
            "run_dir": result.get("run_dir"),
        }
        executor.shutdown(wait=True)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        completed_state = {
            **state,
            "status": "timeout",
            "completed_at": _shared_workflow_now(),
            "error": f"prepare_run exceeded {timeout_seconds:.0f}s timeout",
        }
    except ReviewStartBoundaryNotVerified as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        completed_state = {**state, "status": exc.status, "completed_at": _shared_workflow_now(), "error": exc.reason}
    except BootstrapRequiredBeforeRvf as exc:
        # 「等 local base bootstrap」不是失败，而是一个**可恢复的等待态**：给它独立的 status
        # （不与真失败混同）并附一枚白名单 gate 子记录，让两条入口（rvf-agent decision-token /
        # kanban follow-up）都能持久化、重验证、按同一 run 恢复。此路径在 start_run 之前抛出，
        # 故不建 run、不分配 lease、不派 reviewer——gate 只描述「等什么、等谁、在哪等」。
        executor.shutdown(wait=False, cancel_futures=True)
        completed_state = {
            **state,
            **exc.payload,
            "status": rvf_bootstrap_gate.WAITING_STATUS,
            "bootstrap_gate": rvf_bootstrap_gate.build_bootstrap_gate(
                target_repo,
                exc.payload,
                origin_kind=(
                    rvf_bootstrap_gate.BOOTSTRAP_ORIGIN_KANBAN_FOLLOWUP
                    if rvf_backend == "kanban-followup"
                    else rvf_bootstrap_gate.BOOTSTRAP_ORIGIN_DECISION_TOKEN
                ),
                origin_prep_token=prep.token,
                origin_kanban_task_id=payload.get("target_kanban_task_id"),
                origin_session_id=payload.get("origin_session_id"),
                rvf_run_id=rvf_run_id,
                rvf_run_dir=rvf_run_dir,
                **_bootstrap_gate_parent_attempt(existing_state, rvf_run_id),
            ),
            "completed_at": _shared_workflow_now(),
        }
    except Exception as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        completed_state = {
            **state,
            "status": "failed",
            "completed_at": _shared_workflow_now(),
            "error": f"{type(exc).__name__}: {exc}",
        }

    new_rvf_run = dict(rvf_run) if isinstance(rvf_run, dict) else {}
    new_rvf_run["shared_workflow_state"] = completed_state
    rvf_prep_file.update_prep_file(prep, {"rvf_run": new_rvf_run})
    return completed_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an auditable review-validate-fix run directory.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument("--session-context", help="Required file containing the main-agent work summary.")
    parser.add_argument("--session-manifest", help="Optional prebuilt session ownership manifest JSON.")
    parser.add_argument("--transcript", help="Optional Codex JSONL transcript used to build session-manifest.json.")
    parser.add_argument("--base-dir", default=str(default_base_dir()), help="Directory where a unique run directory will be created.")
    parser.add_argument("--output-json", help="Write run metadata JSON to this path. Prints JSON to stdout when omitted.")
    parser.add_argument("--rvf-run-id", help="Use an existing RVF run id instead of creating a new one.")
    parser.add_argument("--rvf-run-dir", help="Use this RVF run directory instead of resolving state/runs/<run_id>.")
    parser.add_argument("--rvf-backend", help="RVF entry backend: manual, kanban-followup, or kanban-task.")
    parser.add_argument("--max-file-bytes", type=int, default=200_000, help="Max untracked file bytes to inline.")
    parser.add_argument("--max-packet-bytes", type=int, default=0, help="Fail if the generated packet exceeds this many bytes. 0 disables the check.")
    parser.add_argument("--primary-file", action="append", default=[], help="Path known to be primary work for this turn. May be repeated.")
    parser.add_argument("--background-file", action="append", default=[], help="Path known to be pre-existing background WIP. May be repeated.")
    parser.add_argument("--exclude-path-prefix", action="append", default=[], help="Path prefix to omit from status, diff, and untracked packet sections. May be repeated.")
    parser.add_argument(
        "--tracker-scope",
        help=(
            "Path to an allocator tracker_scope JSON object. Splices into "
            "manifest.tracker.tracker_scope and unlocks scope.contract.json v2 fields. "
            "Requires --session-manifest or --transcript."
        ),
    )
    parser.add_argument(
        "--base-ref",
        help=(
            "Diff base for the packet's path-restricted owned-diff. Forwarded to "
            "build_review_packet.py --base-ref; overrides the manifest's committed_round_baseline "
            "and is honored only when it is an ancestor of HEAD (else falls back to HEAD). Use to "
            "review already-committed work (git diff <base-ref>) without re-dirtying it."
        ),
    )
    parser.add_argument(
        "--local-base-ref",
        help=(
            "Local branch whose committed AGENTS.md may opt into the pre-review one-way base sync gate. "
            "Regular repositories remain unchanged when omitted."
        ),
    )
    parser.add_argument(
        "--review-kind",
        choices=sorted(REVIEW_KINDS),
        default="implementation",
        help="Scope contract v3 review kind.",
    )
    parser.add_argument("--scope-base-ref")
    parser.add_argument("--scope-remote-name")
    parser.add_argument("--comparison-base-tip")
    parser.add_argument("--reviewed-base-tip")
    parser.add_argument("--reviewed-task-tip")
    parser.add_argument("--candidate-task-tip")
    parser.add_argument("--base-delta-path", action="append", default=[])
    parser.add_argument("--task-change-path", action="append", default=[])
    parser.add_argument("--interaction-primary-path", action="append", default=[])
    parser.add_argument("--base-delta-protected-path", action="append", default=[])
    parser.add_argument("--bootstrap-receipt", help="Canonical JSON bootstrap receipt to bind into scope v3.")
    parser.add_argument("--candidate-evidence", help="Canonical JSON candidate evidence to bind into scope v3.")
    parser.add_argument(
        "--integration-candidate-receipt",
        help="Canonical JSON handback receipt for base-drift integration review.",
    )
    parser.add_argument(
        "--allow-missing-session-context",
        action="store_true",
        help="Debug-only escape hatch. Normal review runs must pass --session-context.",
    )
    args = parser.parse_args()

    try:
        session_context = Path(args.session_context).expanduser().resolve() if args.session_context else None
        if session_context is None and not args.allow_missing_session_context:
            raise ValueError(
                "session context is required: write a main-agent scope-of-work summary and pass "
                "--session-context <file>; use --allow-missing-session-context only for debug"
            )
        if session_context is not None and not session_context.exists():
            raise ValueError(f"session context file not found: {session_context}")
        if (
            session_context is not None
            and not session_context.read_text(encoding="utf-8").strip()
            and not args.allow_missing_session_context
        ):
            raise ValueError(f"session context file is empty: {session_context}")
        session_manifest = Path(args.session_manifest).expanduser().resolve() if args.session_manifest else None
        transcript = Path(args.transcript).expanduser().resolve() if args.transcript else None
        if session_manifest is not None and transcript is not None:
            raise ValueError("pass either --session-manifest or --transcript, not both")
        if session_manifest is not None and not session_manifest.exists():
            raise ValueError(f"session manifest file not found: {session_manifest}")
        if transcript is not None and not transcript.exists():
            raise ValueError(f"transcript file not found: {transcript}")
        tracker_scope = Path(args.tracker_scope).expanduser().resolve() if args.tracker_scope else None
        if tracker_scope is not None and session_manifest is None and transcript is None:
            raise ValueError(
                "--tracker-scope requires --session-manifest or --transcript so the "
                "tracker_scope payload can be spliced into manifest.tracker.tracker_scope"
            )
        if tracker_scope is not None and not tracker_scope.exists():
            raise ValueError(f"tracker scope file not found: {tracker_scope}")
        contract_extensions = scope_contract_v3_extensions(
            review_kind=args.review_kind,
            base_ref=args.scope_base_ref,
            remote_name=args.scope_remote_name,
            comparison_base_tip=args.comparison_base_tip,
            reviewed_base_tip=args.reviewed_base_tip,
            reviewed_task_tip=args.reviewed_task_tip,
            candidate_task_tip=args.candidate_task_tip,
            base_delta_paths=args.base_delta_path,
            task_change_paths=args.task_change_path,
            interaction_primary_paths=args.interaction_primary_path,
            base_delta_protected_paths=args.base_delta_protected_path,
            bootstrap_receipt=Path(args.bootstrap_receipt) if args.bootstrap_receipt else None,
            candidate_evidence=Path(args.candidate_evidence) if args.candidate_evidence else None,
            integration_candidate_receipt=(
                Path(args.integration_candidate_receipt)
                if args.integration_candidate_receipt
                else None
            ),
        )
        result = prepare_run(
            repo=Path(args.repo).expanduser().resolve(),
            session_context=session_context,
            session_manifest=session_manifest,
            transcript=transcript,
            base_dir=Path(args.base_dir).expanduser().resolve(),
            max_file_bytes=args.max_file_bytes,
            max_packet_bytes=args.max_packet_bytes,
            primary_files=args.primary_file,
            background_files=args.background_file,
            exclude_path_prefixes=args.exclude_path_prefix,
            allow_missing_session_context=args.allow_missing_session_context,
            rvf_run_id=args.rvf_run_id,
            rvf_run_dir=Path(args.rvf_run_dir).expanduser().resolve() if args.rvf_run_dir else None,
            rvf_backend=args.rvf_backend,
            tracker_scope=tracker_scope,
            base_ref=args.base_ref,
            local_base_ref=args.local_base_ref,
            scope_contract_extensions=contract_extensions,
        )
    except BootstrapRequiredBeforeRvf as exc:
        payload = json.dumps(exc.payload, ensure_ascii=False, indent=2) + "\n"
        if args.output_json:
            Path(args.output_json).expanduser().resolve().write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
        return 3
    except Exception as exc:
        return fail(str(exc), 2)

    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output_json:
        Path(args.output_json).expanduser().resolve().write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
