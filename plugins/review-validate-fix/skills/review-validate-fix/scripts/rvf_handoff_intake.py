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


RVF_RUN_RE = re.compile(r"\b(rvf-\d{8}T\d{6}Z-[A-Za-z0-9_.:-]+)")
PATH_VALUE_RE = re.compile(r"^\s*-\s*([^:\n]+):\s*`?([^`\n]+?)`?\s*$")
HEADING_RE = re.compile(r"^##+\s+(.+?)\s*$")
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
        )
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.rstrip("\n")


def run_git_bytes(repo: Path, args: list[str]) -> tuple[int, bytes, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
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
    code_status, status_out, status_err = run_git_bytes(root, ["status", "--porcelain=v1", "-z", "-uall"])
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
    body = sections.get("Validation") or sections.get("验证") or ""
    commands: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        item = stripped[2:].strip()
        command = clean_path_value(item.split(" -> ", 1)[0].strip())
        if command:
            commands.append(command)
    return commands


def infer_run_dir(values: dict[str, str], run_id: str | None) -> Path | None:
    if value_is_available(values.get("run_dir")):
        return Path(values["run_dir"]).expanduser()
    origin = values.get("origin_metadata_path")
    if value_is_available(origin):
        path = Path(origin).expanduser()
        if path.name == "origin.json" and path.parent.name == "artifacts":
            return path.parent.parent
    if run_id:
        for env_name in ("CODEX_RVF_LOG_ROOT", "CODEX_RVF_STATE_DIR"):
            root = os.environ.get(env_name)
            if root:
                candidate = Path(root).expanduser() / "runs" / run_id
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
    run_id = run_id_match.group(1) if run_id_match else None
    intake_hints = parse_intake_hints(sections)
    run_dir = infer_run_dir(values, run_id)
    if run_dir is None and handoff_path is not None and handoff_path.parent.name == "artifacts":
        run_dir = handoff_path.parent.parent
    artifacts = artifact_paths(run_dir, values)
    summary = read_json(run_dir / "summary.json") if run_dir is not None else None
    if not isinstance(summary, dict):
        summary = None
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

    return {
        "handoff_path": str(handoff_path.expanduser()) if handoff_path else None,
        "run_id": run_id,
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
        "validation_commands": parse_validation_commands(sections),
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
        "Reviewed scope paths:",
    ]
    for item in payload.get("scoped_status_in_current_repo") or []:
        lines.append(f"- {item.get('status') or '--'} {item.get('path')}")
    unrelated = payload.get("unrelated_dirty_paths_in_current_repo") or []
    lines.append("")
    lines.append("Unrelated dirty paths in current repo:")
    if unrelated:
        for item in unrelated:
            lines.append(f"- {item.get('status')} {item.get('path')}")
    else:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
