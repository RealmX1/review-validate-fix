#!/usr/bin/env python3
from __future__ import annotations

from independent_backup_route import guard_normal_mutation, tracked_subprocess_run


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
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* import
from core.review_run_qualification_mutation_lock import guard_review_run_qualification_mutation, run_directory_for_qualification_artifact
from core.session_scope_allocation.committed_round_detection import (  # noqa: E402
    frozen_committed_round_base_exclusion_ref,
    resolve_local_review_base_branch_ref,
)
from review_start_boundary_gate import require_review_start_boundary_for_prepare, bind_review_start_boundary_to_one_run
import concurrent.futures
import rvf_prep_file
from rvf_dispatch_prompts import dispatch_scope_of_work_text
from core.run_ledger.run_ledger import normalize_rvf_backend, rvf_state_fields, start_run
from core.run_ledger.review_run_lifecycle import atomic_write_json, read_json_object, merge_review_run_summary
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
REVIEW_KINDS = {"implementation", "base_drift_integration"}


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
    completed = tracked_subprocess_run(
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
    valid = tracked_subprocess_run(
        ["git", "check-ref-format", full_ref],
        check=False,
        capture_output=True,
        text=True,
        env=git_repository_location_clean_environment(),
    )
    if valid.returncode != 0:
        raise ValueError(f"invalid local base ref: {local_base_ref!r}")
    return full_ref



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
    automatic_closeout_eligibility: Mapping[str, Any] | None = None,
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
    if automatic_closeout_eligibility is not None:
        lines.extend([
            "", "普通审查由 review_candidate_snapshot 冻结输入；原生 receipt 只服务自动回退。",
            "自动回退资格：" + json.dumps(dict(automatic_closeout_eligibility), ensure_ascii=False),
            "资格不可用或 automatic-closeout-registration.json 未确认 registered 时，继续 review/fix/handoff，交接后人工 land/base 同步；不手填原生成功凭据。",
        ])
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
    skill = Path(__file__).resolve().parents[1] / "SKILL.md"
    if skill.is_file():
        body = skill.read_text(encoding="utf-8")
        begin = "<!-- rvf-independent-backup-route:begin -->"
        end = "<!-- rvf-independent-backup-route:end -->"
        if begin in body and end in body:
            lines.extend(["", begin + body.split(begin, 1)[1].split(end, 1)[0] + end])
    return "\n".join(lines) + "\n"


def run_git(repo: Path, args: list[str], *, text: bool = True) -> str | bytes:
    completed = tracked_subprocess_run(
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
    completed = tracked_subprocess_run(
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
    candidate_evidence: Path | None = None,
    integration_candidate_receipt: Path | None = None,
) -> dict[str, Any]:
    if review_kind not in REVIEW_KINDS:
        raise ValueError(f"unsupported review kind: {review_kind}")
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
        completed = tracked_subprocess_run(
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


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(arguments["path"]))
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
        "excluded_path_prefixes": excluded_path_prefixes,
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


@guard_normal_mutation("repo", "prepare-review-run")
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
    decision_token: str | None = None,
    preparation_execution_identity_observer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    root = git_root(repo)

    verified_boundary_receipt = require_review_start_boundary_for_prepare(
        repo=str(root), receipt=(scope_contract_extensions or {}).get("review_start_boundary_receipt"),
        candidate_snapshot=(scope_contract_extensions or {}).get("review_candidate_snapshot"),
        decision_token=decision_token,
    )
    scope_contract_extensions = {**dict(scope_contract_extensions or scope_contract_v3_extensions()), "review_candidate_snapshot": verified_boundary_receipt,
        "review_start_boundary_receipt": verified_boundary_receipt.get("native_review_start_boundary_receipt"),
        "automatic_closeout_eligibility": verified_boundary_receipt["automatic_closeout_eligibility"]}
    resolved_local_base_ref = resolve_local_review_base_branch_ref(
        root,
        explicit_local_base_ref=local_base_ref or verified_boundary_receipt.get("local_review_base_ref"),
        committed_base_ref=committed_base_ref,
    )
    effective_scope_contract_extensions = dict(scope_contract_extensions or scope_contract_v3_extensions())
    fixed_comparison_tip = verified_boundary_receipt.get("comparison_base_tip")
    default_comparison_tip = verified_boundary_receipt["worktree_head_commit_sha"]
    if resolved_local_base_ref:
        full_ref = _local_base_ref_full_name(resolved_local_base_ref)
        boundary_base_ref = verified_boundary_receipt.get("local_review_base_ref")
        if not boundary_base_ref or _local_base_ref_full_name(boundary_base_ref) != full_ref:
            raise ValueError("本地 base ref 与开审分界不一致")
        fixed_base_tip = verified_boundary_receipt.get("local_review_base_tip")
        if not fixed_base_tip:
            raise ValueError("开审分界没有固定的本地 base tip")
        task_head = verified_boundary_receipt["worktree_head_commit_sha"]
        common_ancestors = run(["git", "merge-base", "--all", task_head, fixed_base_tip], cwd=root).splitlines()
        if len(common_ancestors) != 1:
            raise ValueError("审查候选需要唯一且明确的共同 base 祖先")
        if effective_scope_contract_extensions.get("review_kind", "implementation") == "implementation":
            default_comparison_tip = common_ancestors[0]
            defaults = {
                "base_ref": resolved_local_base_ref, "remote_name": "origin",
                "reviewed_base_tip": common_ancestors[0],
                "reviewed_task_tip": task_head, "candidate_task_tip": task_head,
            }
            for name, value in defaults.items():
                configured = effective_scope_contract_extensions.get(name)
                if name == "base_ref" and configured:
                    if _local_base_ref_full_name(configured) != full_ref:
                        raise ValueError("scope 的本地 base ref 与开审分界不一致")
                elif name != "remote_name" and configured and configured != value:
                    raise ValueError(f"scope 的 {name} 与固定审查坐标不一致")
                if not configured:
                    effective_scope_contract_extensions[name] = value
        committed_base_ref = frozen_committed_round_base_exclusion_ref(root, resolved_local_base_ref, fixed_base_tip)
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
    if effective_scope_contract_extensions.get("review_kind", "implementation") == "implementation":
        # 显式比较输入必须与分界坐标一致；后续子进程只接收固定 OID，不能重读活 ref。
        for comparison_input in (base_ref, committed_baseline):
            if not comparison_input:
                continue
            input_tip = run(["git", "rev-parse", "--verify", f"{comparison_input}^{{commit}}"], cwd=root).strip()
            if fixed_comparison_tip and input_tip != fixed_comparison_tip:
                raise ValueError("比较基线与开审分界不一致")
            fixed_comparison_tip = input_tip
        fixed_comparison_tip = fixed_comparison_tip or default_comparison_tip
        comparison_ancestors = run(["git", "merge-base", "--all", fixed_comparison_tip,
                                    verified_boundary_receipt["worktree_head_commit_sha"]], cwd=root).splitlines()
        if comparison_ancestors != [fixed_comparison_tip]:
            raise ValueError("比较基线与开审分界不一致：不是固定审查候选的祖先")
        configured_comparison_tip = effective_scope_contract_extensions.get("comparison_base_tip")
        if configured_comparison_tip and configured_comparison_tip != fixed_comparison_tip:
            raise ValueError("scope 的比较基线与开审分界不一致")
        effective_scope_contract_extensions["comparison_base_tip"] = fixed_comparison_tip
        base_ref = committed_baseline = fixed_comparison_tip
    elif base_ref and not committed_baseline:
        committed_baseline = base_ref
    canonical_backend = normalize_rvf_backend(rvf_backend) or "manual"
    rvf_run_id, rvf_run_dir = bind_review_start_boundary_to_one_run(
        receipt=verified_boundary_receipt, run_id=rvf_run_id, run_dir=rvf_run_dir,
    )
    if preparation_execution_identity_observer is not None:
        preparation_execution_identity_observer({
            "repo": root, "run_id": rvf_run_id, "run_dir": rvf_run_dir,
            "verified_session_id": verified_boundary_receipt.get("session_id"),
        })
    ledger = start_run(
        "prepare-run",
        repo=str(root),
        cwd=str(root),
        run_id=rvf_run_id,
        run_dir=rvf_run_dir,
    )
    if ledger.available:
        # 首次 prepare 尚未写出 scope 时，也保留已验证的精确身份供中断恢复核验。
        identity_summary = {"run_id": ledger.run_id, "run_dir": str(ledger.run_dir), "repo": str(root)}
        if verified_boundary_receipt.get("session_id"):
            identity_summary["session_id"] = verified_boundary_receipt["session_id"]
        merge_review_run_summary(ledger.run_dir, identity_summary)
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
        "--frozen-candidate-commit",
        verified_boundary_receipt["checkpoint_commit"],
    ]
    for context_only_path in verified_boundary_receipt.get("packet_context_only_paths", []):
        packet_cmd.extend(["--frozen-packet-context-only-path", context_only_path])
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
        automatic_closeout_eligibility=verified_boundary_receipt["automatic_closeout_eligibility"],
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
        candidate_evidence=artifact_paths["candidate_evidence"],
        integration_candidate_receipt=artifact_paths[
            "integration_candidate_receipt"
        ],
    )
    for prefix in (
        "candidate_evidence",
        "integration_candidate_receipt",
    ):
        if declared_digests[prefix] != extensions[f"{prefix}_digest"]:
            raise ValueError(f"prep {prefix} digest does not match its canonical artifact")
    return extensions


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
        candidate_snapshot=rvf_run.get("review_candidate_snapshot"),
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
    contract_extensions["review_start_boundary_receipt"] = verified_boundary_receipt.get("native_review_start_boundary_receipt")
    contract_extensions["review_candidate_snapshot"] = verified_boundary_receipt
    contract_extensions["automatic_closeout_eligibility"] = verified_boundary_receipt["automatic_closeout_eligibility"]

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

    def _prepare_once(identity_observer: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
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
            preparation_execution_identity_observer=identity_observer,
        )

    def _runner() -> dict[str, Any]:
        execution_identity: dict[str, Any] = {}
        try:
            return _prepare_once(execution_identity.update)
        except KeyboardInterrupt:
            # 只消费实际 preparation 执行抛出的原生中断；future 等待者被中断不证明 worker 结束。
            if execution_identity:
                _record_native_preparation_interruption(**execution_identity, transcript=transcript_path, policy_event=payload)
            raise

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
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    except ReviewStartBoundaryNotVerified as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        completed_state = {**state, "status": exc.status, "completed_at": _shared_workflow_now(), "error": exc.reason}
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


def _record_native_preparation_interruption(
    *, repo: Path, run_id: str | None, run_dir: Path | None,
    verified_session_id: str | None = None, transcript: Path | None = None,
    policy_event: Mapping[str, Any] | None = None,
) -> None:
    """异常清理只登记持久事实和请求，原异常继续抛出；不执行归档或模型。"""
    if not run_id or run_dir is None or not (run_dir / "summary.json").is_file():
        return
    observation_path = run_dir / "artifacts/preparation-native-interruption-observation.json"
    observation: dict[str, Any] = {
        "schema_version": 1, "run_id": run_id, "run_dir": str(run_dir.resolve()), "repo": str(repo.resolve()),
        "native_exception_kind": "KeyboardInterrupt", "observed_at": _shared_workflow_now(),
    }
    run_identity_verified = False
    recovery_index: Path | None = None
    try:
        summary = read_json_object(run_dir / "summary.json")
        if (run_dir.is_symlink() or (run_dir / "summary.json").is_symlink() or summary.get("run_id") != run_id
                or not isinstance(summary.get("repo"), str) or Path(summary["repo"]).resolve() != repo.resolve()):
            raise ValueError("原生 preparation 中断的精确 run 或仓库身份不符")
        run_identity_verified = True
        from review_run_terminal_archive import (
            request_review_run_terminal_archive, record_native_preparation_interruption_recovery_index,
        )

        event: dict[str, Any] = {"cwd": str(repo.resolve())}
        if transcript is not None:
            event["transcript_path"] = str(transcript)
        for name in ("suppress_review_validate_fix", "review_validate_fix_suppressed"):
            if (policy_event or {}).get(name) is True:
                event[name] = True
        observation["event"] = event
        known_session = verified_session_id or summary.get("session_id")
        if known_session:
            observation["session_id"] = known_session
            atomic_write_json(observation_path, observation)
            recovery_index = record_native_preparation_interruption_recovery_index(observation_path)
        contract_path = run_dir / "artifacts/inputs/scope.contract.json"
        if contract_path.is_symlink():
            raise ValueError("原生 preparation 中断的固定 scope 不能是符号链接")
        contract = read_json_object(contract_path)
        snapshot = contract.get("review_candidate_snapshot") or contract.get("review_start_boundary_receipt") or {}
        sessions = [value for value in (summary.get("session_id"), snapshot.get("session_id"), verified_session_id) if value]
        if not sessions or any(value != sessions[0] for value in sessions):
            raise ValueError("原生 preparation 中断缺少一致的已绑定会话证据")
        observation["session_id"] = sessions[0]
        observation["status"] = "native_interruption_recorded"
        atomic_write_json(observation_path, observation)
        if recovery_index is None:
            recovery_index = record_native_preparation_interruption_recovery_index(observation_path)
        event["session_id"] = sessions[0]
        outcome = request_review_run_terminal_archive(run_dir=run_dir, decision_kind="interrupted",
            reason="preparation 实际执行收到 KeyboardInterrupt；保留原中断并延后归档恢复", event=event,
            defer_resume=True)
        observation.update({"status": "terminal_archive_request_recorded", "request_status": outcome["status"]})
        atomic_write_json(observation_path, observation)
        recovery_index.unlink(missing_ok=True)
    except Exception as exc:
        # 保留可核验缺口，不让记账失败替换原 KeyboardInterrupt。
        if not run_identity_verified:
            return
        observation.update({"status": "waiting_for_evidence", "error": f"{type(exc).__name__}: {exc}",
                            "recovery_condition": "精确 run、仓库及绑定 session/scope 证据恢复可读后重验"})
        try:
            atomic_write_json(observation_path, observation)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an auditable review-validate-fix run directory.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument("--decision-token", help="普通审查的真实决策 token；不要求原生回退 receipt。")
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
            "固定候选所使用的本地比较分支；本参数不吸收 base。"
            "base 的吸收与冲突解决由最终 base-branch-sync 或未来 PR 合并阶段负责。"
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

    execution_identity: dict[str, Any] = {}
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
            decision_token=args.decision_token,
            preparation_execution_identity_observer=execution_identity.update,
        )
    except KeyboardInterrupt:
        if execution_identity:
            _record_native_preparation_interruption(**execution_identity, transcript=transcript)
        raise
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
