#!/usr/bin/env python3
"""消费 base-branch-sync 写出的 base↔task intent 复核 artifact（跨仓计划 K4 / N5）。

上游是 my-ai-setup ``base-branch-sync`` 的 ``PARADIGM=ABSORB_ONLY`` artifact writer：它在把 local
base 单向吸收进 task worktree 之后，把「这批 base delta 想干什么」「哪些路径只是机械适配」「还有
哪些没想清楚的问题」写成一份 canonical JSON + ``.sha256`` sidecar，落在 **per-worktree git-dir**
的 ``base-branch-sync/`` 下。本模块把它读进来，交给 reviewer / fixer 当 context。

**三条铁律（全部来自 2026-08-26 的裁决，改代码前先读完）**

1. **永不阻断。** ``consumed`` / ``missing`` / ``invalid`` 三种情形走**同一条**返回路径，只是
   ``status`` 不同。本模块的任何函数都不得因为「artifact 不在 / 不合法」而 raise 到调用方——
   「文件没写出来」与「状态不安全」不是一回事，拿前者当后者用就是再造一条 fail-closed 死路，
   而那正是本轮计划要拆掉的病。非法性检测归**写入端**（my-ai-setup 侧）的运行时自检。
2. **标注必须机器可校验。** 「在 agent 回复末尾说一句缺了 artifact」没有任何机器可校验性，纯靠
   agent 自觉；因此 ``intent_artifact_status`` 必须落成 run 工件字段 + 跨 run 的 append-only 日志
   （见 ``record_intent_artifact_consumption_outcome``），事后可审计、可统计。
3. **按精确文件名读，不枚举目录。** 与本仓既有 9 个 ``<git-dir>/base-branch-sync/…`` 消费点同款。

**subject binding**（防「上一轮留下的、合法但陈旧的 artifact」）：artifact 的六个身份字段必须逐字
等于**本轮已校验的 bootstrap receipt** 的同名字段。receipt 自身已由 ``validated_bootstrap_receipt``
对齐**当前** state，因此这条绑定可传递地把 artifact 也钉到当前 base epoch 上。

schema 走**独立线**，不与 bootstrap receipt 合并（硬约束：不得改 receipt schema）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validation_result_cache import (  # noqa: E402
    canonical_json_bytes,
    clean_git_environment,
)


INTENT_ARTIFACT_SCHEMA_NAME = "base-task-intent-reconciliation"
INTENT_ARTIFACT_SCHEMA_VERSION = 1
INTENT_ARTIFACT_FILENAME = "base-task-intent-reconciliation.json"
INTENT_ARTIFACT_RELATIVE_PATH = f"base-branch-sync/{INTENT_ARTIFACT_FILENAME}"

# ``intent_artifact_status`` 的三个取值——消费端行为完全相同，只有标注不同。
INTENT_ARTIFACT_STATUS_CONSUMED = "consumed"
INTENT_ARTIFACT_STATUS_MISSING = "missing"
INTENT_ARTIFACT_STATUS_INVALID = "invalid"

# §11.7 subject binding 的本仓实现，共七项（跨仓 2026-08-26T08:07:52Z 定稿）。
#
# 七项里只有五项能靠「与 bootstrap receipt 同名字段逐字相等」实现，另两项**在 receipt 里根本不存在**，
# 必须换一种等价强度的写法（已实测，见下）：
#
#   * ``paradigm`` —— bootstrap receipt 的 21 个键里没有它。绑定意图是「别把 FULL_HANDBACK 轮次
#     的 artifact 当成 intake 轮次的用」，因此实现成**必须逐字等于 ABSORB_ONLY 字面量**：RVF 的
#     pre-review intake 只可能消费这一种 paradigm，比「与某个不存在的字段比对」更强。
#   * ``bootstrap_receipt_digest`` —— receipt 按 schema **明令禁止内嵌自身 digest**
#     （my-ai-setup ``…-optimistic-handback.py:701`` 直接对 ``receipt_digest_sha256`` 判错），
#     所以它只能与**本轮 prepare 现算出来的那份 receipt digest** 比对，由调用方传入。
#     这是把 artifact 钉到「那一次具体吸收」上的直接锚，不依赖 receipt 自身是否被正确对齐。
#
# ``remote_name`` 不在原文的七项里（原文是「至少」），但它零成本且能多挡一类错配，故保留。
SUBJECT_BINDING_FIELD_NAMES = (
    "task_worktree_realpath",
    "git_common_directory_realpath",
    "base_ref",
    "remote_name",
    "absorbed_base_tip",
    "task_tip_after_base_absorption",
)

# RVF pre-review intake 唯一可消费的 paradigm；FULL_HANDBACK 轮次的 artifact 必须判 invalid。
INTAKE_ROUND_PARADIGM = "ABSORB_ONLY"

# 注入 reviewer / fixer context 的三个 payload 字段。
INTENT_PAYLOAD_SUMMARY_FIELD = "base_delta_intent_summary"
INTENT_PAYLOAD_MECHANICAL_PATHS_FIELD = "mechanical_adaptation_paths"
INTENT_PAYLOAD_UNRESOLVED_QUESTIONS_FIELD = "unresolved_questions"

ENV_RVF_HOME = "RVF_HOME"
ENV_INTENT_ARTIFACT_CONSUMPTION_OUTCOME_LOG_ROOT = (
    "RVF_INTENT_ARTIFACT_CONSUMPTION_OUTCOME_LOG_ROOT"
)
INTENT_ARTIFACT_CONSUMPTION_OUTCOME_LOG_DIRNAME = "intent-artifact-consumption-outcomes"

STATUS_ARTIFACT_FILENAME = "base-task-intent-reconciliation-status.json"

_SCOPE_OF_WORK_SECTION_HEADING = "## Base delta intent（base-branch-sync 单向吸收后写出）"


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    if now.microsecond:
        return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _rvf_home() -> Path:
    configured = os.environ.get(ENV_RVF_HOME)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rvf"


def intent_artifact_consumption_outcome_log_root(root: str | Path | None = None) -> Path:
    """append-only 日志落盘根：显式参数 > 专用 env > ``$RVF_HOME``/``~/.rvf`` 下固定子目录。

    **必须在 ``~/.rvf/`` 下、不在任何 per-worktree git-dir 里**（跨仓契约的硬要求）：intent artifact
    本身落在 per-worktree git-dir，worktree 一删就没——那正是过渡机制退出条件今天不可判定的原因，
    把统计日志也塞进去等于没解决。
    """
    if root is not None:
        return Path(root).expanduser()
    configured = os.environ.get(ENV_INTENT_ARTIFACT_CONSUMPTION_OUTCOME_LOG_ROOT)
    if configured:
        return Path(configured).expanduser()
    return _rvf_home() / INTENT_ARTIFACT_CONSUMPTION_OUTCOME_LOG_DIRNAME


def _git_absolute_git_dir(repo: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=repo,
        env=clean_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return Path(completed.stdout.strip())


def expected_intent_artifact_path(repo: Path) -> Path | None:
    """artifact 的唯一合法落点；取不到 git-dir 时返回 None（调用方按 ``missing`` 处理）。"""
    git_dir = _git_absolute_git_dir(repo)
    if git_dir is None:
        return None
    return git_dir / "base-branch-sync" / INTENT_ARTIFACT_FILENAME


def repository_name_for_outcome_log(git_common_directory: str | Path) -> str:
    """日志分片文件名：取 git common dir 的父目录名（同一仓库的所有 worktree 共用一个分片）。"""
    common_dir = Path(str(git_common_directory))
    name = common_dir.parent.name or common_dir.name or "repo"
    sanitized = "".join(
        character if character.isalnum() or character in "._-" else "-" for character in name
    )
    return sanitized[:80] or "repo"


def _validated_canonical_artifact_with_sidecar(path: Path) -> tuple[dict[str, Any], str]:
    """canonical JSON bytes + ``.sha256`` sidecar 自洽校验；不合规一律 ``ValueError``。

    与 ``prepare_review_run.canonical_json_artifact_identity`` / ``write_reviewed_task_candidate_
    evidence.validated_canonical_artifact`` 同语义。这里另起一份而不是 import，是为了不让本模块与
    ``prepare_review_run`` 互相 import（后者要 import 本模块）——本仓既有的「消费者侧镜像 helper」
    先例同理。哈希口径共用 ``validation_result_cache.canonical_json_bytes``，不会漂移。
    """
    if path.is_symlink() or not path.is_file():
        raise ValueError("intent artifact must be a regular non-symlink file")
    encoded = path.read_bytes()
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("intent artifact must be a JSON object")
    if encoded != canonical_json_bytes(payload):
        raise ValueError("intent artifact must use canonical JSON bytes")
    sidecar = Path(f"{path}.sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ValueError("intent artifact digest sidecar is missing or unsafe")
    digest = hashlib.sha256(encoded).hexdigest()
    if sidecar.read_bytes() != f"{digest}\n".encode("ascii"):
        raise ValueError("intent artifact digest mismatch")
    return payload, digest


def _validated_payload_shape(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_name") != INTENT_ARTIFACT_SCHEMA_NAME:
        raise ValueError("intent artifact schema_name does not match")
    if payload.get("schema_version") != INTENT_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("intent artifact schema_version is not supported")
    summary = payload.get(INTENT_PAYLOAD_SUMMARY_FIELD)
    if not isinstance(summary, str):
        raise ValueError(f"intent artifact {INTENT_PAYLOAD_SUMMARY_FIELD} must be a string")
    for field_name in (
        INTENT_PAYLOAD_MECHANICAL_PATHS_FIELD,
        INTENT_PAYLOAD_UNRESOLVED_QUESTIONS_FIELD,
    ):
        values = payload.get(field_name)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"intent artifact {field_name} must be an array of non-empty strings")


def _validated_subject_binding(
    payload: Mapping[str, Any],
    bootstrap_receipt: Mapping[str, Any],
    bootstrap_receipt_digest: str | None,
) -> None:
    mismatched = [
        field_name
        for field_name in SUBJECT_BINDING_FIELD_NAMES
        if payload.get(field_name) != bootstrap_receipt.get(field_name)
    ]
    if payload.get("paradigm") != INTAKE_ROUND_PARADIGM:
        mismatched.append("paradigm")
    # digest 未知时**不能**当成通过：那会让 anti-replay 锚静默失效。
    if (
        not isinstance(bootstrap_receipt_digest, str)
        or not bootstrap_receipt_digest
        or payload.get("bootstrap_receipt_digest") != bootstrap_receipt_digest
    ):
        mismatched.append("bootstrap_receipt_digest")
    if mismatched:
        raise ValueError(
            "intent artifact subject does not bind to this round's bootstrap receipt: "
            + ", ".join(sorted(mismatched))
        )


def consumed_base_task_intent_reconciliation(
    repo: Path,
    bootstrap_receipt: Mapping[str, Any] | None,
    bootstrap_receipt_digest: str | None = None,
) -> dict[str, Any]:
    """读取并校验 intent artifact。**永不 raise**，永远返回一个带 ``status`` 的 dict。

    返回 ``{"status", "reason", "path", "digest", "payload"}``：
      * ``consumed`` —— 全部通过，``payload`` 非 None；
      * ``missing``  —— 仓库未 opt-in、无 receipt、git-dir 取不到、或文件与 sidecar 都不存在；
      * ``invalid``  —— 文件在，但 canonical/sidecar/schema/subject-binding 任一不过。
    ``invalid`` 与 ``missing`` 在调用方一律等价放行，区别只在标注与统计。
    """
    if bootstrap_receipt is None:
        return {
            "status": INTENT_ARTIFACT_STATUS_MISSING,
            "reason": "repository has no validated bootstrap receipt for this round",
            "path": None,
            "digest": None,
            "payload": None,
        }
    artifact_path = expected_intent_artifact_path(repo)
    if artifact_path is None:
        return {
            "status": INTENT_ARTIFACT_STATUS_MISSING,
            "reason": "task worktree has no resolvable git directory",
            "path": None,
            "digest": None,
            "payload": None,
        }
    sidecar_path = Path(f"{artifact_path}.sha256")
    if not artifact_path.exists() and not sidecar_path.exists():
        return {
            "status": INTENT_ARTIFACT_STATUS_MISSING,
            "reason": (
                "base-branch-sync did not write an intent reconciliation artifact for this "
                "absorption (the two sides deploy on different cadences; this is not an error)"
            ),
            "path": str(artifact_path),
            "digest": None,
            "payload": None,
        }
    try:
        payload, digest = _validated_canonical_artifact_with_sidecar(artifact_path)
        _validated_payload_shape(payload)
        _validated_subject_binding(payload, bootstrap_receipt, bootstrap_receipt_digest)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        return {
            "status": INTENT_ARTIFACT_STATUS_INVALID,
            "reason": f"{type(error).__name__}: {error}",
            "path": str(artifact_path),
            "digest": None,
            "payload": None,
        }
    return {
        "status": INTENT_ARTIFACT_STATUS_CONSUMED,
        "reason": None,
        "path": str(artifact_path),
        "digest": digest,
        "payload": dict(payload),
    }


def intent_context_markdown(outcome: Mapping[str, Any]) -> str:
    """渲染注入 scope-of-work 的一节。

    **``consumed`` 才渲染正文块**；``missing``/``invalid`` 渲染一句显式声明——这样「本轮没有 intent
    artifact」在 reviewer 眼里是一条确定信息，而不是一段可能被误读成「没有 base delta」的沉默。
    """
    status = outcome.get("status")
    lines = [_SCOPE_OF_WORK_SECTION_HEADING, ""]
    lines.append(f"- `intent_artifact_status`: `{status}`")
    if status != INTENT_ARTIFACT_STATUS_CONSUMED:
        reason = outcome.get("reason") or "(no reason recorded)"
        lines.append(f"- 原因：{reason}")
        lines.append(
            "- **本轮没有可用的 base delta intent artifact。**这不影响审查范围，也不代表 base "
            "没有变化；只是上游没有提供 intent 复核输入。不要据此推断 base delta 为空。"
        )
        lines.append("")
        return "\n".join(lines)
    payload = outcome.get("payload") or {}
    lines.append(f"- 来源 artifact：`{outcome.get('path')}`（digest `{outcome.get('digest')}`）")
    lines.append("")
    lines.append("### base delta 的意图")
    lines.append("")
    lines.append(str(payload.get(INTENT_PAYLOAD_SUMMARY_FIELD, "")).strip() or "_(上游未填写)_")
    lines.append("")
    lines.append("### 机械适配路径（归因分类用，不是审查范围）")
    lines.append("")
    mechanical_paths = payload.get(INTENT_PAYLOAD_MECHANICAL_PATHS_FIELD) or []
    if mechanical_paths:
        lines.append(
            "以下路径上**纯属对 base delta 的机械适配**的改动，归因于 base 吸收，不计入对本次实现的"
            "评价；同一路径上**非**机械适配的改动仍照常审查。这是归因分类，**不**改变 finding 范围。"
        )
        lines.append("")
        lines.append(
            "⚠ **该清单是上游主 agent 的自报值，当前没有任何机器校验**（交叉校验属上游写入端职责，"
            "尚未落地）。因此：**不得**据它裁剪审查范围、**不得**据它跳过任何检查——它只允许影响"
            "「这处改动该归因给谁」的措辞。自报错一条，就会把一处**实现**改动误判成 base 适配；"
            "凡是与你实际读到的 diff 对不上的条目，按你读到的为准，并在 finding 里点出该出入。"
        )
        lines.append("")
        for path_value in mechanical_paths:
            lines.append(f"- `{path_value}`")
    else:
        lines.append("_(上游未标注任何机械适配路径)_")
    lines.append("")
    lines.append("### 上游遗留的未决问题")
    lines.append("")
    unresolved_questions = payload.get(INTENT_PAYLOAD_UNRESOLVED_QUESTIONS_FIELD) or []
    if unresolved_questions:
        for question in unresolved_questions:
            lines.append(f"- {question}")
    else:
        lines.append("_(上游未留下未决问题)_")
    lines.append("")
    return "\n".join(lines)


def status_artifact_payload(outcome: Mapping[str, Any], *, run_id: str | None) -> dict[str, Any]:
    return {
        "schema_name": "rvf-intent-artifact-consumption-status",
        "schema_version": 1,
        "rvf_run_id": run_id,
        "intent_artifact_status": outcome.get("status"),
        "reason": outcome.get("reason"),
        "intent_artifact_path": outcome.get("path"),
        "intent_artifact_digest": outcome.get("digest"),
        "recorded_at_utc": _utc_now_iso(),
    }


def record_intent_artifact_consumption_outcome(
    *,
    outcome: Mapping[str, Any],
    git_common_directory: str,
    task_worktree: str,
    rvf_run_id: str | None,
    root: str | Path | None = None,
) -> Path | None:
    """向跨 run 的 append-only 日志追加一行。**三种 status 都写**，不是只写成功那种。

    **best-effort**：任何 OSError 一律吞掉并返回 None。这是可观测性记录，不得反过来把 prepare 拖红
    ——给它加 fail-closed 分支就是凭空造出本计划正在修的那类死路。
    """
    record = {
        "appended_at_utc": _utc_now_iso(),
        "repository_name": repository_name_for_outcome_log(git_common_directory),
        "rvf_run_id": rvf_run_id,
        "task_worktree": task_worktree,
        "intent_artifact_status": outcome.get("status"),
        "reason": outcome.get("reason"),
        "intent_artifact_digest": outcome.get("digest"),
    }
    path = intent_artifact_consumption_outcome_log_root(root) / f"{record['repository_name']}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(path, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError:
        return None
    return path


def read_intent_artifact_consumption_outcomes(
    repository_name: str, *, root: str | Path | None = None
) -> list[dict[str, Any]]:
    """读回某仓库的全部消费结局（供过渡机制退出条件第 4 条统计）。坏行跳过，不 raise。"""
    path = intent_artifact_consumption_outcome_log_root(root) / f"{repository_name}.jsonl"
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records
