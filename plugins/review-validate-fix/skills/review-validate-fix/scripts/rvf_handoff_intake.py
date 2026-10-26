#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from compact_path_tree_rendering import render_compact_path_tree_block
from validation_result_cache import clean_git_environment
from write_reviewed_task_candidate_evidence import (
    EVIDENCE_RELATIVE_PATH,
    StaleReviewedTaskCandidateEvidence,
    validated_reviewed_task_candidate_evidence,
)


RVF_RUN_RE = re.compile(r"\b(rvf-(?:review-start-[a-f0-9]{16}|\d{8}T\d{6}Z-[A-Za-z0-9_.:-]+))\b")
PATH_VALUE_RE = re.compile(r"^\s*-\s*([^:\n]+):\s*`?([^`\n]+?)`?\s*$")
HEADING_RE = re.compile(r"^##+\s+(.+?)\s*$")
VALIDATION_SECTION_HEADINGS = {
    "Validation",
    "验证",
    "Scoped verification (in-contract)",
    "Full-suite results (unrelated)",
}
SCOPED_LIST_HEADINGS = {
    "Session-owned files reviewed",
    "Session-owned paths reviewed",
    "主审查文件",
    "Session-owned files",
    "reviewed scope paths",
}
ARTIFACT_CANDIDATES = {
    "scope_of_work": ("scope-of-work.md", "inputs/scope-of-work.md"),
    "session_manifest": ("session-manifest.json", "inputs/session-manifest.json"),
    "review_packet": ("review-packet.md", "inputs/review-packet.md"),
    "worktree_bootstrap": ("worktree-bootstrap.json", "inputs/worktree-bootstrap.json"),
    "scope_contract": ("scope.contract.json", "inputs/scope.contract.json"),
}
CONFLICT_WORDS = (
    "cross-session",
    "conflict",
    "protected",
    "background",
    "left untouched",
    "no longer contains",
    "跨 session",
    "跨-session",
    "冲突",
    "背景",
)
INTAKE_HINT_LABELS = {
    "reviewed scope paths": "reviewed_scope_paths",
    "protected / background / cross-session paths": "protected_paths",
    "accepted changes": "accepted_changes",
    "rejected / not accepted changes": "rejected_changes",
    "main-session validation commands": "main_session_validation_commands",
}
# 只剥离「指向别处仓库」的 git 位置变量；hooks、身份、签名、全局 core.excludesFile 等用户配置保持与
# 手动 git 一致。
GIT_REPOSITORY_LOCATION_ENVIRONMENT_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


def git_environment_with_user_configuration_without_repository_location_overrides() -> dict[str, str]:
    """列脏文件、``rvf_land`` 的 stage / commit / commit 后 clean 检查共用这一份环境。

    列脏文件若用 ``clean_git_environment``（全局配置被置空），用户全局 ignore 的文件（如
    ``**/.claude/settings.local.json``）会被当成 scope 外脏文件报 exit 12，而 commit 后的 clean 检查
    看不见它们；agent 照提示加 ``--extra-path`` 就会把本地配置提交进去。两处必须同一口径。
    """
    environment = dict(os.environ)
    for name in GIT_REPOSITORY_LOCATION_ENVIRONMENT_VARIABLES:
        environment.pop(name, None)
    return environment


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_git(repo: Path, args: list[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            env=clean_git_environment(),
        )
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.rstrip("\n")


def run_git_bytes(
    repo: Path, args: list[str], environment: dict[str, str] | None = None
) -> tuple[int, bytes, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
            env=environment if environment is not None else clean_git_environment(),
        )
    except OSError as exc:
        return 127, b"", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout, completed.stderr.decode("utf-8", "replace").rstrip("\n")


def git_root(repo: Path) -> Path | None:
    code, stdout, _stderr = run_git(repo, ["rev-parse", "--show-toplevel"])
    if code != 0 or not stdout:
        return None
    return Path(stdout)


def git_common_dir(repo: Path) -> Path | None:
    code, stdout, _stderr = run_git(repo, ["rev-parse", "--git-common-dir"])
    if code != 0 or not stdout:
        return None
    path = Path(stdout)
    if not path.is_absolute():
        root = git_root(repo)
        if root is not None:
            path = root / path
    try:
        return path.resolve()
    except OSError:
        return path


def decode_git_path(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def parse_status_z(stdout: bytes) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    parts = stdout.split(b"\0")
    index = 0
    while index < len(parts):
        record = parts[index]
        if not record:
            index += 1
            continue
        status = decode_git_path(record[:2])
        path = decode_git_path(record[3:]) if len(record) > 3 else ""
        old_path = ""
        if "R" in status or "C" in status:
            index += 1
            if index < len(parts) and parts[index]:
                old_path = decode_git_path(parts[index])
        entries.append({"status": status, "path": path, "old_path": old_path})
        index += 1
    return entries


def repo_snapshot(repo: Path) -> dict[str, Any]:
    root = git_root(repo)
    if root is None:
        return {"repo": str(repo), "is_git_repo": False}
    _code_branch, branch, _stderr_branch = run_git(root, ["branch", "--show-current"])
    _code_head, head, _stderr_head = run_git(root, ["rev-parse", "--short", "HEAD"])
    code_status, status_out, status_err = run_git_bytes(
        root,
        ["status", "--porcelain=v1", "-z", "-uall"],
        {**git_environment_with_user_configuration_without_repository_location_overrides(), "GIT_OPTIONAL_LOCKS": "0"},
    )
    return {
        "repo": str(root),
        "is_git_repo": True,
        "branch": branch,
        "head": head,
        "git_common_dir": str(git_common_dir(root) or ""),
        "status_returncode": code_status,
        "status_stderr": status_err,
        "dirty_paths": parse_status_z(status_out),
    }


def clean_path_value(value: str) -> str:
    return value.strip().strip("`").strip()


def value_is_available(value: str | None) -> bool:
    return bool(value and value.strip().lower() not in {"unknown", "unavailable", "<unknown>", "<unavailable>"})


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    key_map = {
        "target repo": "target_repo",
        "目标仓库": "target_repo",
        "rvf worktree / target repo": "target_repo",
        "rvf task worktree": "target_repo",
        "run dir": "run_dir",
        "run id": "run_id",
        "run_id": "run_id",
        "transcript path": "transcript_path",
        "transcript": "transcript_path",
        "original transcript": "transcript_path",
        "origin metadata": "origin_metadata_path",
        "origin metadata path": "origin_metadata_path",
        "codex url": "codex_url",
        "original codex url": "codex_url",
        "review packet": "review_packet",
        "session manifest": "session_manifest",
        "worktree bootstrap": "worktree_bootstrap",
        "scope of work": "scope_of_work",
        "scope contract": "scope_contract",
        "scope-of-work": "scope_of_work",
    }
    for line in text.splitlines():
        match = PATH_VALUE_RE.match(line)
        if not match:
            continue
        raw_key, raw_value = match.groups()
        normalized_key = key_map.get(normalize_key(raw_key))
        if normalized_key:
            values[normalized_key] = clean_path_value(raw_value)
    return values


def parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            current = heading.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def parse_scoped_paths(text: str, sections: dict[str, str]) -> list[str]:
    paths: list[str] = []
    in_scoped_list = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped.endswith(":"):
            label = stripped[2:-1].strip()
            in_scoped_list = label in SCOPED_LIST_HEADINGS
            continue
        if in_scoped_list:
            if not stripped.startswith("- "):
                if stripped:
                    in_scoped_list = False
                continue
            paths.append(clean_path_value(stripped[2:]))
    for section_name, body in sections.items():
        if section_name not in {"Scope", "Review scope"}:
            continue
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("- `") and stripped.endswith("`"):
                paths.append(clean_path_value(stripped[2:]))
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path and path not in seen and not path.startswith("$"):
            seen.add(path)
            deduped.append(path)
    return deduped


def parse_validation_commands(sections: dict[str, str]) -> list[str]:
    commands: list[str] = []
    for heading, body in sections.items():
        if heading not in VALIDATION_SECTION_HEADINGS:
            continue
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            item = stripped[2:].strip()
            if item.startswith("`") and "`" in item[1:]:
                command = item[1 : item.index("`", 1)]
            else:
                command = clean_path_value(item.split(" -> ", 1)[0].strip())
            if command and command not in commands:
                commands.append(command)
    return commands


def load_candidate_evidence(
    repo: Path,
    *,
    expected_run_id: str | None,
    expected_scope_contract_path: Path | None,
) -> tuple[str, Path | None, str | None, list[dict[str, Any]]]:
    code, absolute_git_dir, _stderr = run_git(repo, ["rev-parse", "--absolute-git-dir"])
    if code != 0 or not absolute_git_dir:
        return "missing", None, None, []
    evidence_path = Path(absolute_git_dir) / EVIDENCE_RELATIVE_PATH
    if not evidence_path.exists() and not Path(f"{evidence_path}.sha256").exists():
        return "missing", evidence_path, None, []
    try:
        payload, _validated_path, digest = validated_reviewed_task_candidate_evidence(
            repo, evidence_path
        )
    except StaleReviewedTaskCandidateEvidence:
        try:
            _payload, _validated_path, digest = validated_reviewed_task_candidate_evidence(
                repo, evidence_path, require_current_subject=False
            )
        except (OSError, RuntimeError, ValueError):
            return "invalid", evidence_path, None, []
        return "stale", evidence_path, digest, []
    except (OSError, RuntimeError, ValueError):
        return "invalid", evidence_path, None, []
    review = payload.get("review")
    scope = payload.get("scope")
    # 当前候选证据只能证明它自己的归属，不能替缺少身份的交接自行选择一轮。
    if not expected_run_id:
        return "unbound_run", evidence_path, digest, []
    if (
        not isinstance(review, dict) or review.get("run_id") != expected_run_id
    ):
        return "foreign_run", evidence_path, digest, []
    if expected_scope_contract_path is None:
        return "unbound_scope", evidence_path, digest, []
    if expected_scope_contract_path is not None:
        evidence_scope_path = scope.get("contract_path") if isinstance(scope, dict) else None
        if (
            not isinstance(evidence_scope_path, str)
            or Path(evidence_scope_path).resolve()
            != expected_scope_contract_path.resolve()
        ):
            return "foreign_scope", evidence_path, digest, []
    records = payload.get("validation_records")
    return "valid", evidence_path, digest, records if isinstance(records, list) else []


def infer_run_dir(values: dict[str, str], run_id: str | None) -> Path | None:
    if value_is_available(values.get("run_dir")):
        return Path(values["run_dir"]).expanduser()
    origin = values.get("origin_metadata_path")
    if value_is_available(origin):
        path = Path(origin).expanduser()
        if path.name == "origin.json" and path.parent.name == "artifacts":
            return path.parent.parent
    if run_id:
        # 与铸 run 的一侧同一个裁决点：显式 LOG_ROOT / STATE_DIR 覆盖优先，否则落到统一状态根的
        # run-logs。只认环境变量会让「handoff 只写了 run id」的情形在默认配置下永远找不到 run。
        # 惰性 import：UPS hook（rvf_user_prompt_submit）在 try/except 里 import 本模块只为用两个
        # 纯函数，模块级多一条 core.* 依赖，一旦在 hook 环境里失败就会静默关掉它的误触发守卫。
        import _rvf_pyroot  # noqa: F401,PLC0415 — 供 core.* import
        from core.rvf_user_local_data_paths import resolved_rvf_runtime_log_root  # noqa: PLC0415

        candidate = resolved_rvf_runtime_log_root() / "runs" / run_id
        if candidate.exists():
            return candidate
    return None


def artifact_paths(run_dir: Path | None, values: dict[str, str]) -> dict[str, str | None]:
    if run_dir is None:
        return {
            key: values.get(key) if value_is_available(values.get(key)) else None
            for key in ARTIFACT_CANDIDATES
        }
    artifacts = run_dir / "artifacts"
    result: dict[str, str | None] = {}
    for key, candidates in ARTIFACT_CANDIDATES.items():
        found: Path | None = None
        for rel in candidates:
            candidate = artifacts / rel
            if candidate.exists():
                found = candidate
                break
        result[key] = str(found) if found is not None else (
            values.get(key) if value_is_available(values.get(key)) else None
        )
    return result


def load_manifest(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    payload = read_json(Path(path_value).expanduser())
    return payload if isinstance(payload, dict) else None


def conflict_hints(text: str) -> list[str]:
    hints: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(word in lowered for word in CONFLICT_WORDS):
            hints.append(line.strip())
    return hints


def parse_intake_hints(sections: dict[str, str]) -> dict[str, list[str]]:
    body = sections.get("Handoff intake hints") or sections.get("Deterministic intake hints") or ""
    hints: dict[str, list[str]] = {value: [] for value in INTAKE_HINT_LABELS.values()}
    current: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        item = stripped[2:].strip()
        if item.endswith(":"):
            current = INTAKE_HINT_LABELS.get(item[:-1].strip())
            continue
        if ":" in item:
            label, value = item.split(":", 1)
            key = INTAKE_HINT_LABELS.get(label.strip())
            if key is not None:
                value = clean_path_value(value)
                if value and value.lower() not in {"unknown", "unavailable"}:
                    hints[key].append(value)
                current = key
                continue
        if current is not None:
            hints[current].append(clean_path_value(item))
    return hints


def build_payload(text: str, repo: Path, handoff_path: Path | None) -> dict[str, Any]:
    values = parse_key_values(text)
    sections = parse_sections(text)
    run_id_match = RVF_RUN_RE.search(text)
    run_id = values.get("run_id") or (run_id_match.group(1) if run_id_match else None)
    intake_hints = parse_intake_hints(sections)
    run_dir = infer_run_dir(values, run_id)
    formal_handoff_run_dir = handoff_path.parent.parent if handoff_path is not None and handoff_path.parent.name == "artifacts" else None
    if formal_handoff_run_dir is not None:
        # 正式文件的目录绑定优先于正文中最先出现的历史 run；显式矛盾另行拒绝。
        inferred_run_dir = run_dir
        run_dir = formal_handoff_run_dir
    artifacts = artifact_paths(run_dir, values)
    summary = read_json(run_dir / "summary.json") if run_dir is not None else None
    if not isinstance(summary, dict):
        summary = None
    run_identity_errors: list[str] = []
    if formal_handoff_run_dir is not None:
        if value_is_available(values.get("run_dir")) and inferred_run_dir is not None and run_dir.resolve() != inferred_run_dir.resolve():
            run_identity_errors.append("交接声明的 run 目录与正式交接路径不符")
    identity_sources = {}
    if value_is_available(values.get("run_id")):
        identity_sources["declared_run_id"] = values["run_id"]
    if isinstance(summary, dict) and value_is_available(summary.get("run_id")):
        identity_sources["summary_run_id"] = summary["run_id"]
    scope_path = artifacts.get("scope_contract")
    scope_contract = read_json(Path(scope_path)) if scope_path else None
    if isinstance(scope_contract, dict) and value_is_available(scope_contract.get("run_id")):
        identity_sources["scope_contract_run_id"] = scope_contract["run_id"]
    if len(set(identity_sources.values())) > 1:
        run_identity_errors.append("交接、summary 与 scope contract 的 run 身份不一致")
    run_id = identity_sources.get("summary_run_id") or identity_sources.get("scope_contract_run_id") or run_id
    run_identity_status = (
        "invalid" if run_identity_errors else "verified"
        if identity_sources.get("summary_run_id") or identity_sources.get("scope_contract_run_id")
        else "unavailable"
    )
    manifest = load_manifest(artifacts.get("session_manifest") or values.get("session_manifest"))
    scoped_paths = parse_scoped_paths(text, sections)
    if not scoped_paths and intake_hints["reviewed_scope_paths"]:
        scoped_paths = intake_hints["reviewed_scope_paths"]
    if not scoped_paths and isinstance(manifest, dict):
        manifest_paths = manifest.get("owned_dirty_paths") or manifest.get("owned_paths")
        if isinstance(manifest_paths, list):
            scoped_paths = [item for item in manifest_paths if isinstance(item, str)]

    current = repo_snapshot(repo)
    target_repo_text = values.get("target_repo") or (summary or {}).get("repo")
    target_snapshot: dict[str, Any] | None = None
    same_git_common_dir: bool | None = None
    if isinstance(target_repo_text, str) and target_repo_text.strip():
        target_path = Path(target_repo_text).expanduser()
        target_snapshot = repo_snapshot(target_path)
        current_common = current.get("git_common_dir")
        target_common = target_snapshot.get("git_common_dir")
        if current_common and target_common:
            same_git_common_dir = current_common == target_common

    scoped = set(scoped_paths)
    current_dirty = current.get("dirty_paths") if isinstance(current.get("dirty_paths"), list) else []
    dirty_by_path: dict[str, dict[str, str]] = {}
    for entry in current_dirty:
        if not isinstance(entry, dict):
            continue
        for key in ("path", "old_path"):
            path = entry.get(key)
            if isinstance(path, str) and path:
                dirty_by_path[path] = entry
    scoped_status = [
        {"path": path, "status": dirty_by_path.get(path, {"status": "", "path": path})["status"]}
        for path in scoped_paths
    ]
    unrelated_dirty = [
        entry
        for entry in current_dirty
        if (
            isinstance(entry, dict)
            and entry.get("path") not in scoped
            and entry.get("old_path") not in scoped
        )
    ]

    expected_scope_contract_path = (
        Path(artifacts["scope_contract"])
        if isinstance(artifacts.get("scope_contract"), str)
        else None
    )
    evidence_status, evidence_path, evidence_digest, validation_records = load_candidate_evidence(
        repo,
        expected_run_id=run_id,
        expected_scope_contract_path=expected_scope_contract_path,
    )
    if run_identity_errors:
        evidence_status, validation_records = "foreign_run", []
    if evidence_status == "valid":
        validation_evidence_source = "candidate_evidence"
        validation_commands: list[str] = []
    elif evidence_status in {"invalid", "stale", "foreign_run", "foreign_scope", "unbound_run", "unbound_scope"}:
        validation_evidence_source = f"{evidence_status}_candidate_evidence"
        validation_commands = parse_validation_commands(sections)
    else:
        validation_evidence_source = "legacy_handoff_markdown"
        validation_commands = parse_validation_commands(sections)
    return {
        "handoff_path": str(handoff_path.expanduser()) if handoff_path else None,
        "run_id": run_id,
        "run_identity_status": run_identity_status,
        "run_identity_errors": run_identity_errors,
        "run_identity_sources": identity_sources,
        "run_dir": str(run_dir) if run_dir is not None else None,
        "origin": {
            "codex_url": values.get("codex_url"),
            "transcript_path": values.get("transcript_path"),
            "origin_metadata_path": values.get("origin_metadata_path"),
        },
        "target_repo": target_repo_text,
        "target_repo_snapshot": target_snapshot,
        "current_repo_snapshot": current,
        "target_repo_same_git_common_dir_as_current": same_git_common_dir,
        "rvf_worktree_differs_from_current": (
            target_snapshot is not None
            and target_snapshot.get("repo") != current.get("repo")
            and same_git_common_dir is True
        ),
        "artifact_paths": artifacts,
        "reviewed_scope_paths": scoped_paths,
        "scoped_status_in_current_repo": scoped_status,
        "unrelated_dirty_paths_in_current_repo": unrelated_dirty,
        "candidate_evidence_path": str(evidence_path) if evidence_path is not None else None,
        "candidate_evidence_digest": evidence_digest,
        "candidate_evidence_status": evidence_status,
        "candidate_evidence_identity_recovery_condition": (
            "本次交接的精确 run 身份和 scope contract 恢复可核验后自动重验；不借用其他轮次的记录"
            if evidence_status in {"unbound_run", "unbound_scope"} else None
        ),
        "validation_evidence_source": validation_evidence_source,
        "validation_records": validation_records,
        "validation_commands": validation_commands,
        "conflict_hints": conflict_hints(text),
        "intake_hints": intake_hints,
        "summary_status": (summary or {}).get("status"),
        "summary_reason_code": (summary or {}).get("reason_code"),
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        "RVF handoff intake",
        f"- run_id: {payload.get('run_id') or '<unknown>'}",
        f"- run_dir: {payload.get('run_dir') or '<unknown>'}",
        f"- target_repo: {payload.get('target_repo') or '<unknown>'}",
        f"- current_repo: {payload.get('current_repo_snapshot', {}).get('repo') or '<unknown>'}",
        f"- same_git_common_dir: {payload.get('target_repo_same_git_common_dir_as_current')}",
        f"- rvf_worktree_differs_from_current: {payload.get('rvf_worktree_differs_from_current')}",
        "",
    ]
    lines.append("路径相对 current_repo；分组 git_status 适用于组内全部文件。")
    for heading, status_entries in (
        ("Reviewed scope paths:", payload.get("scoped_status_in_current_repo") or []),
        ("Unrelated dirty paths in current repo:", payload.get("unrelated_dirty_paths_in_current_repo") or []),
    ):
        lines.extend(["", heading])
        status_groups: dict[str, list[str]] = {}
        for item in status_entries:
            status_groups.setdefault(item.get("status") or "--", []).append(item["path"])
        for status, paths in sorted(status_groups.items()):
            lines.extend([
                f"\ngit_status={json.dumps(status, ensure_ascii=False)}（{len(paths)} 文件）",
                render_compact_path_tree_block(paths),
            ])
        if not status_groups:
            lines.append("- <none>")
    hints = payload.get("conflict_hints") or []
    if hints:
        lines.append("")
        lines.append("Conflict hints from handoff:")
        for hint in hints:
            lines.append(f"- {hint}")
    return "\n".join(lines)


def read_handoff_input(args: argparse.Namespace) -> tuple[str, Path | None]:
    sources = [bool(args.handoff), bool(args.handoff_text_file), bool(args.stdin)]
    if sum(sources) != 1:
        raise SystemExit("必须且只能提供 --handoff、--handoff-text-file 或 --stdin 之一")
    if args.stdin:
        return sys.stdin.read(), None
    path = Path(args.handoff or args.handoff_text_file).expanduser()
    text = path.read_text(encoding="utf-8")
    return text, path if args.handoff else None


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 RVF handoff 接收前的确定性摘要。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--handoff", help="handoff markdown 文件路径。")
    group.add_argument("--handoff-text-file", help="包含 pasted handoff 内容的临时文本文件。")
    group.add_argument("--stdin", action="store_true", help="从 stdin 读取 handoff 内容。")
    parser.add_argument("--repo", default=".", help="当前主会话 repo/worktree，默认当前目录。")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args()

    text, handoff_path = read_handoff_input(args)
    payload = build_payload(text, Path(args.repo).expanduser(), handoff_path)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(payload))
    return 12 if payload["run_identity_status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
