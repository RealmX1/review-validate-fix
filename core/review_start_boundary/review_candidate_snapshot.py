"""普通审查的冻结候选快照，不声明原生消息或回退能力。"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core.review_start_boundary.review_start_boundary_verification import (
    INVALIDATED, ReviewStartBoundaryNotVerified, git_output, read_receipt,
    repository_git_environment, verify_candidate_git_snapshot,
)

SNAPSHOT_SCHEMA = "rvf-review-candidate-snapshot/v1"
SNAPSHOT_BINDING_PREFIX = "RVF_REVIEW_CANDIDATE_SNAPSHOT_BINDING="
SNAPSHOT_BINDING_FIELDS = (
    "schema", "decision_token", "session_id", "worktree", "candidate_fingerprint",
    "worktree_head_commit_sha", "comparison_base_tip", "comparison_base_ref",
    "comparison_base_ref_tip", "kanban_task_id", "local_review_base_ref", "local_review_base_tip",
    "packet_context_only_paths",
    "committed_scope_evidence",
)
REPOSITORY_LOCAL_REVIEW_SCOPE_IGNORE_FILE_NAME = ".review-validate-fix-ignore"


def _capture_repository_review_scope_policy_input_context(repo: Path) -> dict[str, tuple[str | None, bytes | None]]:
    """保留生效策略及路径解析所需节点，不把这些上下文加入审查候选。"""
    repository_root = repo.resolve()
    pending_path_components = [REPOSITORY_LOCAL_REVIEW_SCOPE_IGNORE_FILE_NAME]
    resolved_path_components: list[str] = []
    captured_entries: dict[str, tuple[str | None, bytes | None]] = {}
    visited_symlink_traversals = set()
    followed_symlink_count = 0
    while pending_path_components:
        component = pending_path_components.pop(0)
        if component in ("", "."):
            continue
        if component == "..":
            if not resolved_path_components:
                raise ReviewStartBoundaryNotVerified(INVALIDATED, "审查排除策略链接指向仓库外")
            resolved_path_components.pop()
            continue
        relative_path = Path(*resolved_path_components, component).as_posix()
        file_path = repository_root / relative_path
        try:
            file_state = file_path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            captured_entries[relative_path] = (None, None)
            break
        if stat.S_ISLNK(file_state.st_mode):
            traversal = (relative_path, tuple(pending_path_components))
            if traversal in visited_symlink_traversals or followed_symlink_count >= 40:
                raise ReviewStartBoundaryNotVerified(INVALIDATED, "审查排除策略包含循环符号链接")
            visited_symlink_traversals.add(traversal)
            followed_symlink_count += 1
            target = os.readlink(file_path)
            captured_entries[relative_path] = ("120000", os.fsencode(target))
            target_path = Path(target)
            if target_path.is_absolute():
                try:
                    target_path = target_path.relative_to(repository_root)
                except ValueError as exc:
                    raise ReviewStartBoundaryNotVerified(INVALIDATED, "审查排除策略链接指向仓库外") from exc
                resolved_path_components = []
            pending_path_components = list(target_path.parts) + pending_path_components
            continue
        resolved_path_components.append(component)
        if stat.S_ISDIR(file_state.st_mode):
            captured_entries[relative_path] = ("040000", None)
            continue
        if not stat.S_ISREG(file_state.st_mode):
            raise ReviewStartBoundaryNotVerified(INVALIDATED, "审查排除策略包含不可冻结的文件类型")
        git_mode = "100755" if file_state.st_mode & 0o111 else "100644"
        captured_entries[relative_path] = (git_mode, file_path.read_bytes())
        break
    return captured_entries


def _include_required_review_scope_context_directories_in_tree(
    tree: str, required_directory_paths: list[str], snapshot_git: Callable[..., str],
) -> str:
    """Git index 不保留空目录；只补齐策略逐组件解析所需的 tree 节点。"""
    if not required_directory_paths:
        return tree
    required_directory_children: dict[str, Any] = {}
    for relative_path in required_directory_paths:
        child_directories = required_directory_children
        for component in Path(relative_path).parts:
            child_directories = child_directories.setdefault(component, {})
    empty_tree = snapshot_git("mktree", "-z", input_bytes=b"")

    def include_child_directories(existing_tree: str, required_children: Mapping[str, Any]) -> str:
        if not required_children:
            return existing_tree
        entries: dict[str, tuple[str, str, str]] = {}
        for raw_entry in snapshot_git("ls-tree", "-z", existing_tree, strip_output=False).split("\0"):
            if raw_entry:
                metadata, name = raw_entry.split("\t", 1)
                mode, object_type, object_id = metadata.split()
                entries[name] = (mode, object_type, object_id)
        changed = False
        for name, child_requirements in required_children.items():
            existing_entry = entries.get(name)
            if existing_entry is not None and existing_entry[1] != "tree":
                raise ReviewStartBoundaryNotVerified(INVALIDATED, "冻结策略上下文的目录与文件类型冲突")
            child_tree = existing_entry[2] if existing_entry else empty_tree
            updated_child_tree = include_child_directories(child_tree, child_requirements)
            if existing_entry is None or updated_child_tree != child_tree:
                entries[name] = ("040000", "tree", updated_child_tree)
                changed = True
        if not changed:
            return existing_tree
        raw_entries = b"".join(
            f"{mode} {object_type} {object_id}\t{name}\0".encode("utf-8")
            for name, (mode, object_type, object_id) in entries.items()
        )
        return snapshot_git("mktree", "-z", input_bytes=raw_entries)

    return include_child_directories(tree, required_directory_children)


def candidate_snapshot_path(token: str) -> Path:
    if len(token) != 16 or any(c not in "0123456789abcdef" for c in token):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "非法候选快照 token")
    return Path(os.environ.get("RVF_HOME") or Path.home() / ".rvf") / "review-candidate-snapshots" / f"{token}.json"


def verify_review_candidate_snapshot(snapshot: Mapping[str, Any], *, token_payload: Mapping[str, Any],
                                     repo: str, candidate_summary_builder: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    if (snapshot.get("schema") != SNAPSHOT_SCHEMA or snapshot.get("decision_token") != token_payload.get("token")
            or snapshot.get("session_id") != token_payload.get("session_id")
            or Path(str(snapshot.get("worktree") or "")).resolve() != Path(repo).resolve()
            or snapshot.get("candidate_fingerprint") != token_payload.get("candidate_fingerprint")):
        raise ReviewStartBoundaryNotVerified(INVALIDATED, "候选快照与当前 token/session/worktree 不一致")
    return verify_candidate_git_snapshot(
        snapshot, token_payload=token_payload, repo=repo, candidate_summary_builder=candidate_summary_builder,
        binding_prefix=SNAPSHOT_BINDING_PREFIX, binding_fields=SNAPSHOT_BINDING_FIELDS,
        require_filesystem_identity=False,
    )


def freeze_review_candidate_snapshot(*, token_payload: Mapping[str, Any], repo: str,
                                     candidate_summary_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> dict[str, Any]:
    token = str(token_payload["token"])
    path = candidate_snapshot_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            prior = read_receipt(path)
            return verify_review_candidate_snapshot(prior, token_payload=token_payload, repo=repo,
                                                    candidate_summary_builder=lambda: candidate_summary_builder(prior))
        summary = token_payload.get("candidate_summary") or {}
        base_ref = summary.get("committed_round_base_ref")
        head = git_output(repo, "rev-parse", "HEAD")
        local_base = base_ref
        if not local_base:
            result = subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/main"],
                                    cwd=repo, env=repository_git_environment(), check=False)
            local_base = "main" if result.returncode == 0 else None
        snapshot = {
            "schema": SNAPSHOT_SCHEMA, "decision_token": token, "session_id": token_payload["session_id"],
            "worktree": str(Path(repo).resolve()), "candidate_fingerprint": token_payload["candidate_fingerprint"],
            "worktree_head_commit_sha": head, "comparison_base_tip": summary.get("committed_round_baseline"),
            "committed_scope_evidence": summary.get("committed_scope_evidence"),
            "comparison_base_ref": base_ref,
            "comparison_base_ref_tip": (summary.get("committed_scope_evidence") or {}).get("base_tip_commit") or (git_output(repo, "rev-parse", base_ref) if base_ref else None),
            "kanban_task_id": summary.get("kanban_task_id"), "local_review_base_ref": local_base,
            "local_review_base_tip": git_output(repo, "rev-parse", local_base) if local_base else None,
        }
        review_scope_policy_context = _capture_repository_review_scope_policy_input_context(Path(repo))
        current = candidate_summary_builder(snapshot)
        if current.get("candidate_fingerprint") != token_payload.get("candidate_fingerprint"):
            raise ReviewStartBoundaryNotVerified(INVALIDATED, "冻结前候选已改变，须重新选择本轮审查范围")
        candidates = [c for c in current.get("candidates", []) if c.get("included_in_candidate_set")]
        paths = set()
        for candidate in candidates:
            relative = candidate.get("path")
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ReviewStartBoundaryNotVerified(INVALIDATED, "候选快照含仓库外路径")
            paths.add(relative)
            file = Path(repo) / relative
            if file.is_symlink():
                target = file.resolve()
                if not target.is_relative_to(Path(repo).resolve()):
                    raise ReviewStartBoundaryNotVerified(INVALIDATED, "候选符号链接指向仓库外")
                target_relative_path = target.relative_to(Path(repo).resolve()).as_posix()
                # 从未存在的 dangling target 没有可加入的 pathspec；HEAD 中原有的目标仍须捕获删除。
                if target.exists() or git_output(repo, "ls-tree", head, "--", target_relative_path):
                    paths.add(target_relative_path)
        with tempfile.TemporaryDirectory(prefix="rvf-candidate-index-") as directory:
            environment = {**repository_git_environment(), "GIT_INDEX_FILE": str(Path(directory) / "index")}
            def snapshot_git(*arguments: str, input_bytes: bytes | None = None, strip_output: bool = True) -> str:
                result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=RVF candidate snapshot",
                                         "-c", "user.email=rvf-snapshot@localhost", "--literal-pathspecs", *arguments],
                                        cwd=repo, env=environment, input=input_bytes, capture_output=True, check=False)
                if result.returncode:
                    raise ReviewStartBoundaryNotVerified(INVALIDATED, "无法创建冻结候选 Git 快照")
                output = result.stdout.decode("utf-8")
                return output.strip() if strip_output else output
            snapshot_git("read-tree", head)
            if paths:
                snapshot_git("add", "--all", "--", *sorted(paths))
            candidate_tree_before_policy_context = snapshot_git("write-tree")
            for relative_path, (git_mode, raw_contents) in review_scope_policy_context.items():
                if git_mode == "040000":
                    # 路径当前是目录时，仅移除旧同名文件，不能把整目录的其它文件纳入上下文。
                    snapshot_git("update-index", "--force-remove", "--", relative_path)
                    continue
                snapshot_git("rm", "--cached", "--force", "--ignore-unmatch", "-r", "--", relative_path)
                if git_mode is not None:
                    # 生效策略可以被 Git ignore 或 clean filter 改写；上下文只接受原始字节。
                    blob = snapshot_git("hash-object", "-w", "--no-filters", "--stdin", input_bytes=raw_contents)
                    snapshot_git("update-index", "--add", "--cacheinfo", f"{git_mode},{blob},{relative_path}")
            snapshot["checkpoint_tree"] = _include_required_review_scope_context_directories_in_tree(
                snapshot_git("write-tree"),
                [relative_path for relative_path, (git_mode, _) in review_scope_policy_context.items() if git_mode == "040000"],
                snapshot_git,
            )
            context_changed_paths = snapshot_git(
                "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                candidate_tree_before_policy_context, snapshot["checkpoint_tree"], "--", strip_output=False,
            ).split("\0")
            context_path_set = {relative_path for relative_path, (git_mode, _) in review_scope_policy_context.items() if git_mode != "040000"}
            included_candidate_paths = {str(candidate["path"]) for candidate in candidates}
            # 上下文只供策略解析；文件类型替换产生的删除节点也必须从 reviewer 正文精确排除。
            snapshot["packet_context_only_paths"] = sorted((context_path_set | set(filter(None, context_changed_paths))) - included_candidate_paths)
            binding = {key: snapshot.get(key) for key in SNAPSHOT_BINDING_FIELDS}
            snapshot["checkpoint_commit"] = snapshot_git("commit-tree", snapshot["checkpoint_tree"], "-p", head,
                                                          "-m", SNAPSHOT_BINDING_PREFIX + json.dumps(binding, ensure_ascii=False))
            verify_review_candidate_snapshot(snapshot, token_payload=token_payload, repo=repo,
                                             candidate_summary_builder=lambda: candidate_summary_builder(snapshot))
            if _capture_repository_review_scope_policy_input_context(Path(repo)) != review_scope_policy_context:
                raise ReviewStartBoundaryNotVerified(INVALIDATED, "冻结期间审查排除策略上下文已改变，须重新选择审查范围")
            snapshot_git("update-ref", f"refs/rvf/review-candidate-snapshots/{token}", snapshot["checkpoint_commit"])
        write_candidate_snapshot_record(snapshot)
        return snapshot


def write_candidate_snapshot_record(snapshot: Mapping[str, Any]) -> None:
    path = candidate_snapshot_path(str(snapshot["decision_token"]))
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.pending-write")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(dict(snapshot), handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
