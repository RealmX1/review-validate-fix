#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from compact_path_tree_rendering import render_compact_path_tree_block  # noqa: E402
from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: E402
from core.session_scope_allocation.frozen_review_unit_evidence import (  # noqa: E402
    verify_frozen_unit_evidence, write_frozen_evidence_file, project_tracked_hunk_by_content_identity,
)
from core.review_start_boundary.review_start_boundary_verification import repository_git_environment  # noqa: E402
from review_scope_excluded_path_prefixes import (  # noqa: E402
    REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME,
    load_review_scope_excluded_path_prefixes,
)


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def run_git(repo: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=repository_git_environment(),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def git_root(repo: Path) -> Path:
    return Path(run_git(repo, ["rev-parse", "--show-toplevel"]).strip()).resolve()


def commit_is_ancestor_of_head(repo: Path, commit: str, head_ref: str = "HEAD") -> bool:
    """True iff <commit> is an ancestor of HEAD (or HEAD itself).

    Mirrors core.session_scope_allocation.committed_round_detection._commit_is_ancestor_of_head;
    kept local so the packet builder does not import the Stop hook / core detection module just
    for this guard. Uses its own check=False subprocess because run_git raises on the non-zero
    exit that simply means "not an ancestor". `git merge-base --is-ancestor A B` exits 0 when A
    is an ancestor of B.
    """
    if not commit:
        return False
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, head_ref],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=repository_git_environment(),
    )
    return completed.returncode == 0


def resolve_owned_diff_base_ref(
    repo: Path,
    session_manifest: dict[str, Any] | None,
    base_ref_override: str | None = None,
    *, head_ref: str = "HEAD",
) -> str:
    """Resolve the diff base for the path-restricted owned-diff.

    Precedence: explicit override (--base-ref) > manifest committed_round_baseline > "HEAD".
    A candidate is honored only when it is a non-empty string AND an ancestor of HEAD, so a
    clean (already-committed) worktree renders `git diff <baseline>` (committed work plus any
    still-dirty edits since the baseline) instead of an empty `git diff HEAD`. When no committed
    baseline applies, the literal "HEAD" reproduces today's `git diff HEAD` byte-for-byte (zero
    regression for the pure-dirty common case). The ancestor guard fails safe to "HEAD" when the
    baseline is not reachable from HEAD (branch reset/rebased behind it), avoiding reverse diffs.
    """
    candidates: list[str] = []
    if isinstance(base_ref_override, str) and base_ref_override.strip():
        candidates.append(base_ref_override.strip())
    if session_manifest is not None:
        manifest_baseline = session_manifest.get("committed_round_baseline")
        if isinstance(manifest_baseline, str) and manifest_baseline.strip():
            candidates.append(manifest_baseline.strip())
    for candidate in candidates:
        if commit_is_ancestor_of_head(repo, candidate, head_ref):
            return candidate
    return "HEAD"


class FrozenCandidatePacketGitSource:
    """只从固定 tree/blob 读取 reviewer 代码输入，不观察活动文件或真实 index。"""

    def __init__(self, repo: Path, commit: str):
        if len(commit) not in (40, 64) or any(character not in "0123456789abcdef" for character in commit):
            raise ValueError("冻结 packet 必须使用完整且不可变的候选 commit OID")
        if run_git(repo, ["rev-parse", "--verify", commit + "^{commit}"]).strip() != commit:
            raise ValueError("冻结 packet 的候选 commit 无效")
        parents = run_git(repo, ["rev-list", "--parents", "-n", "1", commit]).split()
        if len(parents) != 2:
            raise ValueError("冻结 packet 的候选快照必须绑定唯一原始 HEAD")
        self.repository_root = repo
        self.frozen_candidate_commit_sha = commit
        self.frozen_origin_worktree_head_commit_sha = parents[1]
        self.frozen_added_paths_with_omitted_contents: set[str] = set()
        self.frozen_packet_context_only_paths: set[str] = set()
        self.frozen_added_paths_outside_review_scope: set[str] = set()
        self.frozen_diff_presentation_config_arguments: list[str] = []
        # 保留分配侧影响 hunk 分段的参数；属性和外部 driver 仍完全隔离。
        for setting in ("diff.algorithm", "diff.indentheuristic", "diff.interhunkcontext"):
            configured = subprocess.run(["git", "config", "--get", setting], cwd=repo,
                                        env=repository_git_environment(), capture_output=True, text=True, check=False)
            if configured.returncode == 0:
                self.frozen_diff_presentation_config_arguments.extend(["-c", setting + "=" + configured.stdout.strip()])
            elif configured.returncode != 1:
                raise RuntimeError("无法读取分配侧的 Git diff 渲染参数")

    def render_raw_diff_from_immutable_git_objects(self, arguments: list[str]) -> str:
        """以临时 bare 元数据和原仓只读对象库渲染 raw diff，隔离所有活动 attributes。"""
        objects = Path(run_git(self.repository_root, ["rev-parse", "--git-path", "objects"]).strip())
        if not objects.is_absolute():
            objects = self.repository_root / objects
        environment = {**repository_git_environment(), "GIT_CONFIG_GLOBAL": "/dev/null",
                       "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_ATTR_NOSYSTEM": "1"}
        with tempfile.TemporaryDirectory(prefix="rvf-frozen-packet-git-") as directory:
            initialize_command = ["git", "-c", "core.attributesFile=/dev/null", "init", "--bare", "--quiet", "--template="]
            if len(self.frozen_candidate_commit_sha) == 64:
                initialize_command.append("--object-format=sha256")
            initialized = subprocess.run([*initialize_command, directory],
                                         env=environment, capture_output=True, text=True, check=False)
            if initialized.returncode:
                raise RuntimeError("无法初始化冻结 packet 的只读 Git 渲染环境")
            environment.update(GIT_DIR=directory, GIT_OBJECT_DIRECTORY=str(objects.resolve()),
                               GIT_INDEX_FILE=str(Path(directory) / "empty-render-index"))
            result = subprocess.run(["git", "-c", "core.attributesFile=/dev/null", *self.frozen_diff_presentation_config_arguments, *arguments], cwd=self.repository_root, env=environment,
                                    capture_output=True, text=True, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "冻结 Git diff 渲染失败")
            return result.stdout

    def list_frozen_candidate_added_paths(self, excluded_prefixes: list[str]) -> list[str]:
        # 快照相对原始 HEAD 新增的文件也包含已暂存新增项；两者都需要冻结正文。
        raw = self.render_raw_diff_from_immutable_git_objects(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--diff-filter=A", "--name-only", "-z",
                                  self.frozen_origin_worktree_head_commit_sha, self.frozen_candidate_commit_sha, "--", ".", *exclude_pathspecs(excluded_prefixes)])
        return sorted(path for path in raw.split("\0") if path and path not in self.frozen_packet_context_only_paths)

    def read_frozen_tree_entry(self, path: str) -> list[str] | None:
        raw = run_git(self.repository_root, ["ls-tree", "-z", self.frozen_candidate_commit_sha, "--", ":(literal)" + path])
        return raw.split("\0", 1)[0].split("\t", 1)[0].split() if raw else None

    def read_frozen_blob_text_and_metadata(self, path: str, max_bytes: int, *, follow_symlinks: bool = False, require_valid_utf8_text: bool = False) -> tuple[str | None, dict[str, Any]]:
        entry = self.read_frozen_tree_entry(path)
        if follow_symlinks:
            remaining_components = deque(path.split("/"))
            resolved_components: list[str] = []
            expanded_symlink_count = 0
            while remaining_components:
                component = remaining_components.popleft()
                if component in ("", "."):
                    continue
                if component == "..":
                    if not resolved_components:
                        raise ValueError("冻结审查排除策略的符号链接指向仓库外")
                    resolved_components.pop()
                    continue
                prefix = "/".join([*resolved_components, component])
                entry = self.read_frozen_tree_entry(prefix)
                if entry is None:
                    break
                if entry[0] == "120000":
                    expanded_symlink_count += 1
                    if expanded_symlink_count > 40:
                        raise ValueError("冻结审查排除策略包含循环符号链接")
                    target = run_git(self.repository_root, ["cat-file", "blob", entry[2]])
                    if posixpath.isabs(target):
                        root_prefix = str(self.repository_root).rstrip("/")
                        if target == root_prefix:
                            target = ""
                        elif target.startswith(root_prefix + "/"):
                            target = target[len(root_prefix) + 1:]
                        else:
                            raise ValueError("冻结审查排除策略的符号链接指向仓库外")
                        resolved_components.clear()
                    # 先展开链接，再按实际目录处理后续 '..'；不能预先 normpath。
                    remaining_components.extendleft(reversed(target.split("/")))
                elif remaining_components and entry[1] != "tree":
                    entry = None
                    break
                else:
                    resolved_components.append(component)
            else:
                entry = self.read_frozen_tree_entry("/".join(resolved_components)) if resolved_components else None
        if entry is None or entry[1] != "blob":
            return None, {"size": 0, "sha256": None, "omitted": True, "reason": "冻结 tree 中不是普通 blob"}
        size = int(run_git(self.repository_root, ["cat-file", "-s", entry[2]]).strip())
        digest = hashlib.sha256()
        chunks = []
        process = subprocess.Popen(["git", "cat-file", "blob", entry[2]], cwd=self.repository_root,
                                   env=repository_git_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None
        with process.stdout:
            for chunk in iter(lambda: process.stdout.read(64 * 1024), b""):
                digest.update(chunk)
                if size <= max_bytes:
                    chunks.append(chunk)
        _, stderr = process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode("utf-8", "replace") or "无法读取冻结 blob")
        info = {"size": size, "sha256": digest.hexdigest(), "git_mode": entry[0], "git_object_id": entry[2]}
        if size > max_bytes:
            return None, {**info, "omitted": True, "reason": f"size exceeds max_file_bytes={max_bytes}"}
        content = b"".join(chunks)
        if b"\0" in content:
            return None, {**info, "omitted": True, "reason": "binary file"}
        return content.decode("utf-8", "strict" if require_valid_utf8_text else "replace"), {**info, "omitted": False}


def exclude_pathspecs(exclude_prefixes: list[str]) -> list[str]:
    """把「审查范围排除前缀」翻成 git exclude pathspec，语义 = 字符串前缀（与候选集侧
    ``path_matches_review_scope_excluded_path_prefix`` 的 ``startswith`` 一致）。

    glob magic 下 ``*`` 不跨 ``/``，单条 ``<prefix>*`` 只盖住前缀所在那一层：已跟踪的嵌套文件、
    父目录已跟踪时的嵌套未跟踪文件都会漏进 status / diff / untracked。所以每个前缀发两条：
    ``<prefix>*`` 盖同层路径，``<prefix>*/**`` 盖以它开头的目录下任意深度的路径，两条之并恰为
    「以该前缀开头的全部路径」。前缀里的 glob 元字符先转义，保证按字面匹配。
    """
    pathspecs: list[str] = []
    for prefix in exclude_prefixes:
        escaped = prefix.replace("\\", "\\\\")
        for char in "*?[]":
            escaped = escaped.replace(char, f"\\{char}")
        pathspecs.append(f":(exclude,top,glob){escaped}*")
        pathspecs.append(f":(exclude,top,glob){escaped}*/**")
    return pathspecs


def untracked_files(repo: Path, exclude_prefixes: list[str]) -> list[str]:
    args = ["git", "ls-files", "--others", "--exclude-standard", "-z"]
    if exclude_prefixes:
        args.extend(["--", ".", *exclude_pathspecs(exclude_prefixes)])
    completed = subprocess.run(
        args,
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace").strip() or "git ls-files failed")
    return sorted(item.decode("utf-8", "surrogateescape") for item in completed.stdout.split(b"\0") if item)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def markdown_fence(text: str) -> str:
    longest = 0
    current = 0
    for char in text:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "`" * max(3, longest + 1)


def read_text(path: Path, max_bytes: int) -> tuple[str | None, dict[str, Any]]:
    size = path.stat().st_size
    digest = sha256_file(path)
    info: dict[str, Any] = {"size": size, "sha256": digest}
    if size > max_bytes:
        info.update({"omitted": True, "reason": f"size exceeds max_file_bytes={max_bytes}"})
        return None, info
    data = path.read_bytes()
    if b"\0" in data:
        info.update({"omitted": True, "reason": "binary file"})
        return None, info
    info["omitted"] = False
    return data.decode("utf-8", "replace"), info


def note_from_info(info: dict[str, Any]) -> str:
    if info.get("omitted"):
        return f"omitted: {info['reason']}; {info['size']} bytes; sha256={info['sha256']}"
    return f"{info['size']} bytes; sha256={info['sha256']}"


def load_session_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"session manifest must be a JSON object: {path}")
    return payload


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def session_owned_paths(manifest: dict[str, Any] | None) -> list[str]:
    if manifest is None:
        return []
    return sorted(
        set(string_list(manifest.get("owned_paths")))
        | set(string_list(manifest.get("owned_dirty_paths")))
        | set(string_list(manifest.get("owned_committed_round_paths")))
    )


def session_owned_dirty_paths(manifest: dict[str, Any] | None) -> list[str]:
    if manifest is None:
        return []
    if "owned_dirty_paths" not in manifest:
        return session_owned_paths(manifest)
    dirty = string_list(manifest.get("owned_dirty_paths"))
    return sorted(set(dirty))


def session_owned_committed_round_paths(manifest: dict[str, Any] | None) -> list[str]:
    """Paths the session committed this round (clean worktree). On a clean worktree these
    live ONLY in owned_committed_round_paths — owned_dirty_paths is empty — so the owned-diff
    must union these in or already-committed work is silently dropped from the packet."""
    if manifest is None:
        return []
    return sorted(set(string_list(manifest.get("owned_committed_round_paths"))))


def validate_session_manifest(manifest: dict[str, Any] | None, root: Path, path: Path | None) -> None:
    if manifest is None:
        return
    manifest_repo = manifest.get("repo")
    if not isinstance(manifest_repo, str) or not manifest_repo.strip():
        raise ValueError(f"session manifest is missing repo: {path}")
    if Path(manifest_repo).expanduser().resolve() != root:
        raise ValueError(f"session manifest repo does not match current repo: {manifest_repo} != {root}")
    if not session_owned_paths(manifest):
        raise ValueError(f"session manifest has no owned paths; refusing to build empty Session-Owned scope: {path}")


def diff_for_paths(repo: Path, paths: list[str], exclude_prefixes: list[str], base_ref: str = "HEAD", frozen_candidate_source: FrozenCandidatePacketGitSource | None = None) -> str:
    if not paths:
        return ""
    if frozen_candidate_source and base_ref == "HEAD":
        base_ref = frozen_candidate_source.frozen_origin_worktree_head_commit_sha
    args = ["diff", "--find-renames", base_ref]
    if frozen_candidate_source:
        args.extend(["--no-ext-diff", "--no-textconv", frozen_candidate_source.frozen_candidate_commit_sha])
    args.extend(["--", *([":(literal)" + path for path in paths] if frozen_candidate_source else paths)])
    if exclude_prefixes:
        args.extend(exclude_pathspecs(exclude_prefixes))
    if frozen_candidate_source:
        args.extend(":(exclude,literal)" + path for path in sorted(
            frozen_candidate_source.frozen_added_paths_with_omitted_contents | frozen_candidate_source.frozen_packet_context_only_paths
            | frozen_candidate_source.frozen_added_paths_outside_review_scope))
        return frozen_candidate_source.render_raw_diff_from_immutable_git_objects(args).rstrip()
    return run_git(repo, args).rstrip()


def review_packet_hunk_line_range_key(header: str) -> tuple[int, int, int, int] | str:
    """范围身份只取行区间，忽略 attributes/diff driver 产生的函数标题后缀。"""
    match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
    if match is None:
        return header.rstrip()
    return (int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1))


def filter_diff_for_hunk_headers(diff_text: str, allowed_headers: set[str], *, require_complete_projection: bool = False) -> str:
    if not allowed_headers:
        return diff_text.rstrip()
    allowed_ranges = {review_packet_hunk_line_range_key(header) for header in allowed_headers}
    matched_ranges = set()
    output: list[str] = []
    file_header: list[str] = []
    in_allowed_hunk = False
    in_any_hunk = False
    emitted_header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            file_header = [line]
            in_allowed_hunk = False
            in_any_hunk = False
            emitted_header = False
            continue
        if line.startswith("@@"):
            in_any_hunk = True
            line_range = review_packet_hunk_line_range_key(line)
            in_allowed_hunk = line_range in allowed_ranges
            if in_allowed_hunk:
                matched_ranges.add(line_range)
            if in_allowed_hunk and not emitted_header:
                output.extend(file_header)
                emitted_header = True
            if in_allowed_hunk:
                output.append(line)
            continue
        if not emitted_header and not in_any_hunk:
            file_header.append(line)
            continue
        if in_allowed_hunk:
            output.append(line)
    if require_complete_projection and allowed_ranges - matched_ranges:
        raise ValueError("冻结候选无法完整投影已分配的 hunk 范围；须核对分配侧与渲染侧的 diff 设置")
    return "\n".join(output).rstrip()


def diff_for_tracker_scope(repo: Path, tracker_scope: dict[str, Any], exclude_prefixes: list[str], base_ref: str = "HEAD", frozen_candidate_source: FrozenCandidatePacketGitSource | None = None) -> str:
    paths = [path for path in tracker_scope.get("paths", []) if isinstance(path, str)]
    if not paths:
        return ""
    # 真正跨已提交基线时，原始 hunk 坐标可能合并或重新编号，按已分配路径投影。
    # 纯 dirty 的 HEAD 即使已固定为 OID，也必须保持 hunk 过滤并完整核对授权区间。
    is_frozen_head_base = frozen_candidate_source is not None and base_ref == frozen_candidate_source.frozen_origin_worktree_head_commit_sha
    if base_ref != "HEAD" and not is_frozen_head_base:
        chunks = [diff_for_paths(repo, [path], exclude_prefixes, base_ref, frozen_candidate_source) for path in paths]
        return "\n".join(chunk for chunk in chunks if chunk).rstrip()
    hunk_headers_by_path: dict[str, set[str]] = {}
    path_level_paths: set[str] = set()
    for entry in tracker_scope.get("hunks", []):
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        header = entry.get("hunk_header")
        if isinstance(header, str) and header:
            hunk_headers_by_path.setdefault(path, set()).add(reviewable_unit_diff_tracker._normalize_header(header))
        else:
            path_level_paths.add(path)
    chunks: list[str] = []
    for path in paths:
        if frozen_candidate_source and path in (
            frozen_candidate_source.frozen_added_paths_with_omitted_contents | frozen_candidate_source.frozen_packet_context_only_paths
            | frozen_candidate_source.frozen_added_paths_outside_review_scope
        ):
            continue
        if path in path_level_paths or path not in hunk_headers_by_path:
            chunk = diff_for_paths(repo, [path], exclude_prefixes, base_ref, frozen_candidate_source)
        else:
            chunk = filter_diff_for_hunk_headers(
                diff_for_paths(repo, [path], exclude_prefixes, base_ref, frozen_candidate_source),
                hunk_headers_by_path[path],
                require_complete_projection=frozen_candidate_source is not None,
            )
        if chunk:
            chunks.append(chunk)
    return "\n".join(chunks).rstrip()


def build_frozen_scope_packet(source: FrozenCandidatePacketGitSource, scope: dict[str, Any] | None,
                              metadata: dict[str, Any], context: str, owned_paths: list[str],
                              untracked: list[str], max_file_bytes: int, base_ref: str,
                              excluded_prefixes: list[str], evidence_directory: Path | None,
                              history_diff: str) -> tuple[str, dict[str, Any]]:
    """正文只含正式分配内容；完整代码及机器审计索引留在持久文件。"""
    candidate = source.frozen_candidate_commit_sha
    origin = source.frozen_origin_worktree_head_commit_sha
    units = []
    chunks = []
    gaps = []
    frozen_blob_descriptors: dict[str, dict[str, Any]] = {}
    frozen_blob_text_edges: dict[str, str] = {}
    index: dict[str, Any] = {"schema_version": 1, "candidate_commit": candidate,
        "origin_head_commit": origin, "scope_hash": scope.get("scope_hash") if scope else None,
        "unit_ids": list(scope.get("unit_ids", [])) if scope else [],
        "tracker_lease_id": scope.get("lease_id") if scope else None, "units": units}

    def persist(name: str, content: bytes) -> dict[str, Any]:
        if evidence_directory is None:
            return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        return write_frozen_evidence_file(evidence_directory, name, content)

    def blob_descriptor(blob: str | None) -> dict[str, Any] | None:
        if not blob:
            return None
        if blob in frozen_blob_descriptors:
            return frozen_blob_descriptors[blob]
        content = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=source.repository_root,
                                          env=repository_git_environment())
        descriptor = {"git_object_id": blob, **persist("blob", content)}
        frozen_blob_descriptors[blob] = descriptor
        frozen_blob_text_edges[blob] = (content[:4096] + content[-4096:]).decode("utf-8", "replace")
        return descriptor

    def generated_source_relationship(path: str, after: dict[str, Any] | None) -> dict[str, Any]:
        if after is None:
            return {"status": "unknown"}
        edges = frozen_blob_text_edges[after["git_object_id"]]
        match = re.search(r"sourceMappingURL=([^\s*]+)", edges)
        if match and not re.match(r"[a-zA-Z]+:", match[1]):
            map_path = posixpath.normpath(posixpath.join(posixpath.dirname(path), match[1]))
            if map_path != ".." and not map_path.startswith(("../", "/")):
                entry = source.read_frozen_tree_entry(map_path)
                if entry and entry[1] == "blob":
                    return {"status": "known", "source_map_path": map_path,
                            "source_map_evidence": blob_descriptor(entry[2]),
                            "review_options": ["source_semantics", "build_consistency", "raw_artifact"]}
        match = re.search(r"(?:Generated|generated) from\s+([^\s*]+)", edges)
        if match:
            source_path = posixpath.normpath(match[1])
            if source_path != ".." and not source_path.startswith(("../", "/")):
                entry = source.read_frozen_tree_entry(source_path)
                if entry and entry[1] == "blob":
                    return {"status": "known", "declared_source_path": source_path,
                            "source_evidence": blob_descriptor(entry[2]),
                            "review_options": ["source_semantics", "build_consistency", "raw_artifact"]}
        return {"status": "unknown"}

    entries = [entry for entry in (scope or {}).get("hunks", []) if isinstance(entry, dict)]
    entries_by_unit = {entry.get("unit_id"): entry for entry in entries}
    if scope:
        entries = [entries_by_unit.get(uid, {"unit_id": uid}) for uid in scope["unit_ids"]]
    else:
        entries = [{"unit_id": None, "path": path} for path in owned_paths]

    for entry in entries:
        path = entry.get("path")
        if isinstance(path, str) and (any(path.startswith(prefix) for prefix in excluded_prefixes)
                                     or path in source.frozen_packet_context_only_paths):
            continue
        unit_id = entry.get("unit_id")
        descriptor: dict[str, Any] = {"unit_id": unit_id, "path": path, "evidence_status": "verified"}
        patch = ""
        record = entry.get("frozen_evidence")
        try:
            if scope and (not isinstance(unit_id, str) or not re.fullmatch(r"[0-9a-f]{64}", unit_id)):
                raise ValueError("已分配 unit 内容身份无效")
            if not isinstance(path, str) or not path or path in source.frozen_packet_context_only_paths:
                raise ValueError("已分配 unit 缺少可核验的代码路径")
            if isinstance(record, dict):
                descriptor["before"] = blob_descriptor(record.get("preimage_blob"))
                descriptor["after"] = blob_descriptor(record.get("postimage_blob"))
                verify_frozen_unit_evidence(record, unit_id, path, source,
                    resolved_postimage_content_sha256=(descriptor["after"] or {}).get("sha256"))
                descriptor.update({key: value for key, value in record.items() if key not in {"hunk_patch"}})
                if record.get("hunk_patch"):
                    patch = (f"diff --git a/{path} b/{path}\n"
                             f"--- a/{path}\n+++ b/{path}\n" + record["hunk_patch"])
                else:
                    target = candidate if record["source"] == "dirty" else record["target_commit"]
                    patch = source.render_raw_diff_from_immutable_git_objects([
                        "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index",
                        record["baseline_commit"], target, "--", ":(literal)" + path])
            else:
                header = entry.get("hunk_header")
                selected = []
                selected_blob_pair = None
                for baseline, target, label in ((origin, candidate, "dirty"), (base_ref, origin, "committed")):
                    if baseline == target:
                        continue
                    rendered = source.render_raw_diff_from_immutable_git_objects([
                        "diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--full-index",
                        baseline, target, "--", ":(literal)" + path])
                    before_blob = reviewable_unit_diff_tracker._resolve_blob_at_ref(source.repository_root, baseline, path)
                    after_blob = reviewable_unit_diff_tracker._resolve_blob_at_ref(source.repository_root, target, path)
                    if not header and unit_id:
                        after = blob_descriptor(after_blob)
                        identities = {reviewable_unit_diff_tracker._canonical_hash_path_only(path)}
                        if after and not before_blob:
                            identities.add(reviewable_unit_diff_tracker._canonical_hash_added(path, after["sha256"]))
                        if before_blob and not after_blob:
                            identities.add(reviewable_unit_diff_tracker._canonical_hash_deleted(path, before_blob))
                        if after:
                            identities.add(reviewable_unit_diff_tracker._canonical_hash_binary(path, before_blob or "", after["sha256"]))
                        if unit_id not in identities:
                            continue
                    if header and rendered:
                        rendered = project_tracked_hunk_by_content_identity(rendered, path=path, unit_id=unit_id) or ""
                        if not any(line.startswith("@@") for line in rendered.splitlines()):
                            continue
                    if rendered:
                        selected.append(rendered)
                        descriptor.setdefault("sources", []).append({"source": label, "baseline_commit": baseline, "target_commit": target})
                        selected_blob_pair = (before_blob, after_blob)
                        if unit_id:
                            break
                if not selected and unit_id:
                    raise ValueError("冻结候选无法完整投影已分配的 hunk 或文件 unit 范围")
                patch = "\n".join(selected)
                before_blob = selected_blob_pair[0] if selected_blob_pair else reviewable_unit_diff_tracker._resolve_blob_at_ref(source.repository_root, base_ref, path)
                after_blob = selected_blob_pair[1] if selected_blob_pair else reviewable_unit_diff_tracker._resolve_blob_at_ref(source.repository_root, candidate, path)
                descriptor["before"] = blob_descriptor(before_blob)
                descriptor["after"] = blob_descriptor(after_blob)
            descriptor["patch"] = persist("patch", patch.encode())
            descriptor["generated_source_relationship"] = generated_source_relationship(path, descriptor.get("after"))
            if len(patch.encode()) <= max_file_bytes:
                chunks.append(patch.rstrip())
            else:
                chunks.append(f"路径 `{path}` 的内容改为引用读取，仍属于完整 Scope：{descriptor['patch']}")
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            # 新分配证据有缺口时保持 unknown；不把整条路径误当作同一 unit 的替代证明。
            descriptor.update(evidence_status="unverified", evidence_gap=str(exc))
            if isinstance(record, dict):
                descriptor["unverified_unit_evidence_snapshot"] = persist("unverified-unit-evidence.json",
                    json.dumps(record, ensure_ascii=False, sort_keys=True).encode())
                if isinstance(record.get("hunk_patch"), str):
                    descriptor["patch"] = persist("patch", record["hunk_patch"].encode())
            gaps.append(descriptor)
        units.append(descriptor)

    # 路径级范围可保留 Git 的重命名展示，正文依旧只有一份；hunk 级范围始终逐 unit 投影。
    if not scope and all(len(chunk.encode()) <= max_file_bytes for chunk in chunks):
        pathspecs = [":(literal)" + path for path in owned_paths if path not in source.frozen_packet_context_only_paths
                     and not any(path.startswith(prefix) for prefix in excluded_prefixes)]
        chunks = [source.render_raw_diff_from_immutable_git_objects([
            "diff", "--no-ext-diff", "--no-textconv", "--find-renames", base_ref, candidate, "--", *pathspecs])] if pathspecs else []

    index["evidence_complete"] = not gaps
    index["history_diff"] = persist("repository-history.diff", history_diff.encode())
    if evidence_directory is not None:
        evidence_directory.mkdir(parents=True, exist_ok=True)
        index_descriptor = write_frozen_evidence_file(evidence_directory, "frozen-review-evidence-index.json",
            (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode())
        metadata["frozen_evidence_index"] = index_descriptor["file"]
        metadata["frozen_evidence_index_sha256"] = index_descriptor["sha256"]
    metadata["frozen_evidence_complete"] = not gaps
    metadata["frozen_evidence_gaps"] = gaps
    metadata["frozen_evidence_units"] = units
    metadata["untracked_files"] = []
    for path in untracked:
        if path not in set(owned_paths):
            continue
        _, info = source.read_frozen_blob_text_and_metadata(path, max_file_bytes)
        metadata["untracked_files"].append({"path": path, **info})
    metadata["inlined_untracked_count"] = sum(not item.get("omitted") for item in metadata["untracked_files"])
    metadata["omitted_untracked_count"] = sum(bool(item.get("omitted")) for item in metadata["untracked_files"])
    lines = ["# Review Packet", "", f"冻结候选：`{candidate}`。两位 reviewer 各自负责全部正式 Scope。", "",
             "按需选择阅读路径、工具与验证方法；生成物、二进制和大文件仍属于原 Scope。", ""]
    if context:
        lines.extend(["## Session Context", "", context, ""])
    if scope:
        lines.extend(["## Tracker Scope", "", f"Scope：`{scope['scope_hash']}`；lease：`{scope['lease_id']}`；unit_ids：", *[f"- `{uid}`" for uid in scope["unit_ids"]], ""])
    if metadata["session_manifest_provided"]:
        lines.extend(["## Session Manifest", "", f"归属与机器审计按需读取：{metadata['session_manifest_file']}", ""])
    lines.extend(["## Excluded Paths", "", *[f"- {prefix}" for prefix in excluded_prefixes], ""])
    conflicts = metadata.get("cross_session_conflicts") or []
    if conflicts:
        sessions = sorted({item["other_session_id"] for item in conflicts})
        lines.extend(["## Cross-Session Conflicts", "",
            f"发现 {len(conflicts)} 项并行归属或资源争用，涉及会话：{', '.join(sessions)}。详情见 Session Manifest 及 packet metadata。",
            "优先使用隔离资源或既有命令锁，并继续不依赖争用资源的审查。", ""])
    lines.extend(["## Allocated Git Diff" if scope else "## Session-Owned Git Diff" if metadata["session_manifest_provided"] else "## Git Diff HEAD", "",
                  *chunks, "", "## 冻结证据入口", "", str(metadata.get("frozen_evidence_index", "证据索引由 packet 调用方持久保存。")), "",
                  f"使用 `{Path(__file__).resolve().with_name('read_frozen_review_evidence.py')}` 按索引读取；默认每页 32768 字节，offset 按字节推进，支持超长行和二进制。", ""])
    if gaps:
        lines.extend(["未核验的证据缺口：", json.dumps(gaps, ensure_ascii=False), ""])
    packet = "\n".join(lines).rstrip() + "\n"
    metadata["packet_bytes"] = len(packet.encode())
    return packet, metadata


def build_packet(
    repo: Path,
    session_context: Path | None,
    session_manifest_path: Path | None,
    max_file_bytes: int,
    primary_files: list[str],
    background_files: list[str],
    exclude_prefixes: list[str] | None = None,
    allow_missing_session_context: bool = False,
    base_ref_override: str | None = None,
    frozen_candidate_commit: str | None = None,
    frozen_packet_context_only_paths: list[str] | None = None,
    evidence_directory: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    root = git_root(repo)
    frozen_source = FrozenCandidatePacketGitSource(root, frozen_candidate_commit) if frozen_candidate_commit else None
    if frozen_source:
        for path in frozen_packet_context_only_paths or []:
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("冻结 packet 上下文路径必须位于仓库内")
            frozen_source.frozen_packet_context_only_paths.add(path)
    frozen_ignore_text = None
    if frozen_source is not None:
        frozen_ignore_text = ""
        if frozen_source.read_frozen_tree_entry(REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME):
            frozen_ignore_text, info = frozen_source.read_frozen_blob_text_and_metadata(
                REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME, 1024 * 1024, follow_symlinks=True, require_valid_utf8_text=True,
            )
            if frozen_ignore_text is None:
                if info.get("reason") != "冻结 tree 中不是普通 blob":
                    raise ValueError("冻结的审查排除策略不可读")
                frozen_ignore_text = ""
    all_exclude_prefixes = load_review_scope_excluded_path_prefixes(
        root, exclude_prefixes or [], repo_local_ignore_file_text=frozen_ignore_text,
    )
    session_manifest = load_session_manifest(session_manifest_path)
    owned_diff_base_ref = resolve_owned_diff_base_ref(
        root, session_manifest, base_ref_override,
        head_ref=frozen_source.frozen_origin_worktree_head_commit_sha if frozen_source else "HEAD",
    )
    if frozen_source and owned_diff_base_ref == "HEAD":
        owned_diff_base_ref = frozen_source.frozen_origin_worktree_head_commit_sha
    generated = datetime.now(timezone.utc).isoformat()
    status_args = ["status", "--short", "-uall"]
    diff_args = ["diff", "--find-renames", "HEAD", "--"]
    if frozen_source:
        status_args = ["diff", "--no-ext-diff", "--no-textconv", "--name-status", owned_diff_base_ref, frozen_source.frozen_candidate_commit_sha]
        diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", owned_diff_base_ref, frozen_source.frozen_candidate_commit_sha, "--"]
    if all_exclude_prefixes:
        status_args.extend(["--", ".", *exclude_pathspecs(all_exclude_prefixes)])
        diff_args.extend([".", *exclude_pathspecs(all_exclude_prefixes)])
    untracked = frozen_source.list_frozen_candidate_added_paths(all_exclude_prefixes) if frozen_source else untracked_files(root, all_exclude_prefixes)
    validate_session_manifest(session_manifest, root, session_manifest_path)
    owned_paths = session_owned_paths(session_manifest)

    cross_session_conflicts: list[dict[str, Any]] = []
    tracker_status: str | None = None
    tracker_repo_key: str | None = None
    tracker_scope: dict[str, Any] | None = None
    if session_manifest is not None:
        tracker_meta = session_manifest.get("tracker") if isinstance(session_manifest.get("tracker"), dict) else None
        if tracker_meta is not None:
            tracker_status = tracker_meta.get("status") if isinstance(tracker_meta.get("status"), str) else None
            tracker_repo_key = tracker_meta.get("repo_key") if isinstance(tracker_meta.get("repo_key"), str) else None
            current_session = tracker_meta.get("session_id") or session_manifest.get("session_id") or ""
            if isinstance(current_session, str) and current_session:
                owned_units = reviewable_unit_diff_tracker.owned_units_from_manifest(session_manifest)
                if owned_units:
                    conflicts = reviewable_unit_diff_tracker.list_conflicts(
                        root,
                        current_session_id=current_session,
                        owned_units=owned_units,
                    )
                    cross_session_conflicts = [conflict.to_dict() for conflict in conflicts]
            scope_candidate = tracker_meta.get("tracker_scope")
            if isinstance(scope_candidate, dict) and (
                isinstance(scope_candidate.get("unit_ids"), list)
                and isinstance(scope_candidate.get("lease_id"), str)
                and isinstance(scope_candidate.get("scope_hash"), str)
                and isinstance(scope_candidate.get("paths"), list)
            ):
                tracker_scope = scope_candidate
    if frozen_source:
        if session_manifest is not None:
            visible_added_paths = set(tracker_scope["paths"]) if tracker_scope is not None else (
                set(session_owned_dirty_paths(session_manifest)) | set(session_owned_committed_round_paths(session_manifest))
            )
            frozen_source.frozen_added_paths_outside_review_scope = set(untracked) - visible_added_paths
        for path in untracked:
            entry = frozen_source.read_frozen_tree_entry(path)
            if entry and entry[1] == "blob" and int(run_git(root, ["cat-file", "-s", entry[2]]).strip()) > max_file_bytes:
                frozen_source.frozen_added_paths_with_omitted_contents.add(path)
        diff_args.extend(":(exclude,literal)" + path for path in sorted(
            frozen_source.frozen_added_paths_with_omitted_contents | frozen_source.frozen_packet_context_only_paths
            | frozen_source.frozen_added_paths_outside_review_scope))
        status_args.extend(":(exclude,literal)" + path for path in sorted(frozen_source.frozen_packet_context_only_paths))
        status = frozen_source.render_raw_diff_from_immutable_git_objects(status_args).rstrip()
        diff = frozen_source.render_raw_diff_from_immutable_git_objects(diff_args).rstrip()
    else:
        status = run_git(root, status_args).rstrip()
        diff = run_git(root, diff_args).rstrip()
    session_context_text = ""
    if session_context is None:
        if not allow_missing_session_context:
            raise ValueError(
                "session context is required: write a main-agent scope-of-work summary and pass "
                "--session-context <file>; use --allow-missing-session-context only for debug"
            )
    else:
        session_context_text = session_context.read_text(encoding="utf-8").strip()
        if not session_context_text and not allow_missing_session_context:
            raise ValueError(f"session context file is empty: {session_context}")
    metadata: dict[str, Any] = {
        "generated": generated,
        "frozen_candidate_commit": frozen_source.frozen_candidate_commit_sha if frozen_source else None,
        "repo": str(root),
        "max_file_bytes": max_file_bytes,
        "ignore_file": REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME,
        "excluded_path_prefixes": all_exclude_prefixes,
        "status_bytes": len(status.encode("utf-8")),
        "diff_bytes": len(diff.encode("utf-8")),
        "untracked_count": len(untracked),
        "session_context_provided": bool(session_context_text),
        "session_context_bytes": len(session_context_text.encode("utf-8")),
        "scope_of_work_file": str(session_context) if session_context is not None else None,
        "session_manifest_file": str(session_manifest_path) if session_manifest_path is not None else None,
        "session_manifest_provided": session_manifest is not None,
        "session_manifest_confidence": session_manifest.get("confidence") if session_manifest is not None else None,
        "session_owned_paths": owned_paths,
        "session_owned_path_count": len(owned_paths),
        "session_owned_dirty_paths": string_list(session_manifest.get("owned_dirty_paths")) if session_manifest is not None else [],
        "unattributed_dirty_paths": string_list(session_manifest.get("unattributed_dirty_paths")) if session_manifest is not None else [],
        "owned_untracked_count": 0,
        "background_untracked_count": 0,
        "primary_files": primary_files,
        "background_files": background_files,
        "untracked_files": [],
        "tracker_status": tracker_status,
        "tracker_repo_key": tracker_repo_key,
        "cross_session_conflicts": cross_session_conflicts,
        "tracker_scope_present": tracker_scope is not None,
        "tracker_scope_unit_count": len(tracker_scope["unit_ids"]) if tracker_scope is not None else 0,
        "tracker_scope_lease_id": tracker_scope["lease_id"] if tracker_scope is not None else None,
        "tracker_scope_hash": tracker_scope["scope_hash"] if tracker_scope is not None else None,
        "tracker_scope_paths": [
            path for path in tracker_scope["paths"] if isinstance(path, str)
        ] if tracker_scope is not None else [],
        "tracker_scope_source_session_id": tracker_scope.get("source_session_id")
        if tracker_scope is not None and isinstance(tracker_scope.get("source_session_id"), str)
        else None,
        "tracker_scope_takeover_from_session_id": tracker_scope.get("takeover_from_session_id")
        if tracker_scope is not None and isinstance(tracker_scope.get("takeover_from_session_id"), str)
        else None,
    }
    owned_dirty_paths = session_owned_dirty_paths(session_manifest)
    owned_committed_round_paths = session_owned_committed_round_paths(session_manifest)
    owned_diff_paths = [
        path for path in metadata["tracker_scope_paths"] if isinstance(path, str)
    ] if tracker_scope is not None else sorted(set(owned_dirty_paths) | set(owned_committed_round_paths))
    owned_path_set = set(owned_diff_paths)
    if tracker_scope is not None and frozen_source is None:
        owned_diff = diff_for_tracker_scope(root, tracker_scope, all_exclude_prefixes, owned_diff_base_ref, frozen_source)
    else:
        owned_diff = diff_for_paths(root, owned_diff_paths, all_exclude_prefixes, owned_diff_base_ref, frozen_source) if session_manifest is not None else ""
    owned_untracked = [path for path in untracked if path in owned_path_set]
    background_untracked = [path for path in untracked if path not in owned_path_set]
    metadata["session_owned_diff_paths"] = owned_diff_paths
    metadata["owned_diff_base_ref"] = owned_diff_base_ref
    metadata["owned_untracked_count"] = len(owned_untracked) if session_manifest is not None else len(untracked)
    metadata["background_untracked_count"] = len(background_untracked) if session_manifest is not None else 0

    if frozen_source is not None:
        return build_frozen_scope_packet(
            frozen_source, tracker_scope, metadata, session_context_text,
            owned_diff_paths if session_manifest is not None else sorted(set(primary_files) or set(
                run_git(root, ["diff", "--name-only", "--no-renames", owned_diff_base_ref, frozen_source.frozen_candidate_commit_sha]).splitlines())),
            untracked, max_file_bytes, owned_diff_base_ref, all_exclude_prefixes, evidence_directory, diff,
        )
    if evidence_directory is not None:
        metadata["repository_diff_evidence"] = write_frozen_evidence_file(evidence_directory, "repository-history.diff", diff.encode())

    lines: list[str] = [
        "# Review Packet",
        "",
        f"Generated: {generated}",
        "",
        "All paths below are relative to the repository root. This packet is the review input; reviewers should use the provided scope-of-work file as the scope anchor and use this packet as evidence.",
        "",
        "## Runtime environment notes",
        "",
        "- Reviewer/validator subagents may run inside RVF task worktrees where `.venv` is a symlink to the parent worktree. If `.venv/bin/python` cannot import the modules under review, prefer `python3 -m <module>` against the system interpreter or create a temporary venv in the task worktree; do NOT depend on the parent worktree's `.venv`.",
        "- For Python tests, prefer `python3 -m pytest <target>`. Direct `pytest` may be missing on PATH inside a fresh task worktree.",
        "- Do not treat a source checkout or plugin cache as runtime state; canonical mutable state is under `RVF_USER_LOCAL_DATA_ROOT` (default `~/.rvf/user-local-data/`).",
        (f"- 代码输入与排除策略来自固定 Git 候选 `{frozen_source.frozen_candidate_commit_sha}`；新增文件列表也包含快照中的已暂存新增项。"
         if frozen_source else "- 本 review-packet 反映 RVF dispatch 触发瞬间 origin worktree 的 dirty snapshot；origin 之后的继续编辑不会回流到本 task worktree。`unattributed dirty paths` 列于下方仅作上下文，**不属于本轮 review 范围**。"),
        "",
    ]

    if primary_files or background_files:
        lines.extend(["## Review Scope", ""])
        if primary_files:
            lines.extend(["Primary files for this turn:"])
            lines.append(render_compact_path_tree_block(primary_files))
            lines.append("")
        if background_files:
            lines.extend(["Background WIP files already present before this turn:"])
            lines.append(render_compact_path_tree_block(background_files))
            lines.append("")

    if session_context_text:
        lines.extend(["## Session Context", "", session_context_text, ""])

    if session_manifest is not None:
        lines.extend(
            [
                "## Session Manifest",
                "",
                "This manifest is the session-scoped ownership anchor. Reviewers must treat owned paths as the default review scope and treat unattributed dirty paths as background WIP unless a session-owned change directly depends on them.",
                "",
                f"- manifest file: `{session_manifest_path}`",
                f"- session id: `{session_manifest.get('session_id') or '(unknown)'}`",
                f"- confidence: `{session_manifest.get('confidence') or 'unknown'}`",
                "",
                "Session-owned paths:",
            ]
        )
        if owned_paths:
            lines.append(render_compact_path_tree_block(owned_paths))
        else:
            lines.append("(none)")
        lines.extend(["", "Unattributed dirty paths (background, not default review scope):"])
        unattributed = string_list(session_manifest.get("unattributed_dirty_paths"))
        if unattributed:
            lines.append(render_compact_path_tree_block(unattributed))
        else:
            lines.append("(none)")
        lines.append("")
        patch_ownership = session_manifest.get("patch_ownership")
        if isinstance(patch_ownership, dict):
            unresolved = patch_ownership.get("unresolved_owned_patch_hunks")
            expected_paths = string_list(patch_ownership.get("expected_apply_patch_paths"))
            expected_units = string_list(patch_ownership.get("expected_apply_patch_unit_ids"))
            lines.extend(
                [
                    "Patch ownership audit:",
                    f"- transcript max line: `{patch_ownership.get('transcript_max_line_number') or '(none)'}`",
                    f"- expected apply_patch paths: `{len(expected_paths)}`",
                    f"- expected apply_patch units: `{len(expected_units)}`",
                    f"- unresolved owned patch hunks: `{len(unresolved) if isinstance(unresolved, list) else 0}`",
                    "",
                ]
            )
        edit_claims = session_manifest.get("edit_claims")
        if isinstance(edit_claims, list):
            latest_user_lines = sorted(
                {
                    item.get("latest_user_line_number")
                    for item in edit_claims
                    if isinstance(item, dict) and isinstance(item.get("latest_user_line_number"), int)
                }
            )
            lines.extend(
                [
                    "Edit claim audit:",
                    f"- edit claim count: `{len(edit_claims)}`",
                    f"- latest user context line count: `{len(latest_user_lines)}`",
                    "",
                ]
            )

    if tracker_scope is not None:
        lines.extend(
            [
                "## Tracker Scope",
                "",
                "This section is the allocator-assigned scope anchor. Reviewers must treat the listed unit_ids as authoritative scope; `## Allocated Git Diff` below is the path-projection.",
                "",
                f"- lease id: `{tracker_scope['lease_id']}`",
                f"- scope hash: `{tracker_scope['scope_hash']}`",
                f"- transcript max line: `{tracker_scope.get('transcript_max_line_number') or '(none)'}`",
            ]
        )
        source_session = tracker_scope.get("source_session_id")
        if isinstance(source_session, str) and source_session.strip():
            lines.append(f"- source session: `{source_session}`")
        else:
            lines.append("- source session: (none)")
        takeover_session = tracker_scope.get("takeover_from_session_id")
        if isinstance(takeover_session, str) and takeover_session.strip():
            lines.append(f"- takeover from session: `{takeover_session}`")
        else:
            lines.append("- takeover from session: (none)")
        # 并集全审的两段构成：lease 过期后重新分配时，本轮 scope = 上一轮仍可审的 unit ∪ 当时
        # 工作树。两段的成熟度不同（一段已定稿、一段刚写），reviewer 必须能分辨，否则会把两批
        # 改动当成同一时刻的产物一起评判。
        unioned_raw = tracker_scope.get("unioned_prior_scope_unit_ids")
        unioned_prior_unit_ids = (
            [uid for uid in unioned_raw if isinstance(uid, str)]
            if isinstance(unioned_raw, list)
            else []
        )
        if unioned_prior_unit_ids:
            lines.extend(
                [
                    "",
                    f"⚠️ 本轮 scope 是**并集重分配**的结果（上一轮 lease 过期后恢复）："
                    f"其中 {len(unioned_prior_unit_ids)} 个 unit 来自**上一轮已圈定但未审完**的 scope，"
                    "其余为本次重新观察到的改动。两段写作时刻不同，请分别判断成熟度，"
                    "不要把后写的一段的问题归到前一段上。",
                    "",
                    "承接自上一轮 scope 的 unit：",
                ]
            )
            lines.extend(f"- `{uid}`" for uid in unioned_prior_unit_ids)
        lines.append("")
        scope_unit_ids = [uid for uid in tracker_scope["unit_ids"] if isinstance(uid, str)]
        scope_hunks_raw = tracker_scope.get("hunks")
        scope_hunks = [h for h in scope_hunks_raw if isinstance(h, dict)] if isinstance(scope_hunks_raw, list) else []
        if scope_hunks:
            hunks_by_unit: dict[str, list[dict[str, Any]]] = {}
            for entry in scope_hunks:
                uid = entry.get("unit_id")
                if isinstance(uid, str):
                    hunks_by_unit.setdefault(uid, []).append(entry)
            lines.append("Allocated units:")
            for uid in scope_unit_ids:
                entries = hunks_by_unit.get(uid, [])
                if entries:
                    head = entries[0]
                    path_text = head.get("path") if isinstance(head.get("path"), str) else "(unknown path)"
                    hunk_header = head.get("hunk_header") if isinstance(head.get("hunk_header"), str) else None
                    if hunk_header:
                        lines.append(f"- `{uid}` — path `{path_text}` — hunk `{hunk_header}`")
                    else:
                        lines.append(f"- `{uid}` — path `{path_text}` — (path-level)")
                else:
                    lines.append(f"- `{uid}` — (no hunk metadata)")
            lines.append("")
        else:
            lines.append("Allocated unit_ids:")
            for uid in scope_unit_ids:
                lines.append(f"- `{uid}`")
            lines.append("")
        scope_paths = [path for path in tracker_scope["paths"] if isinstance(path, str)]
        lines.append("Allocated paths:")
        if scope_paths:
            lines.append(render_compact_path_tree_block(scope_paths))
        else:
            lines.append("(none — units are hunk-only; see allocated units above)")
        lines.append("")

    if cross_session_conflicts:
        lines.extend(
            [
                "## Cross-Session Conflicts",
                "",
                "Other live RVF sessions in this clone hold claims that overlap with the current session-owned scope. Reviewers should treat these as scope contention: prefer issuing a `lock-request` review-result artifact and let the main session resolve before re-running.",
                "",
            ]
        )
        for conflict in cross_session_conflicts:
            anchor = f" `{conflict['hunk_header']}`" if conflict.get("hunk_header") else ""
            run_id = conflict.get("other_run_id") or "(unknown run)"
            branch = conflict.get("other_branch") or "(unknown branch)"
            worktree = conflict.get("other_worktree") or "(unknown worktree)"
            last_seen = conflict.get("last_seen_at") or "(unknown)"
            lines.append(
                f"- `{conflict['path']}`{anchor} — claimed by session `{conflict['other_session_id']}` "
                f"(run `{run_id}`, branch `{branch}`, worktree `{worktree}`, last_seen `{last_seen}`)"
            )
        lines.append("")

    lines.extend(
        [
            "## Packet Stats",
            "",
            f"- tracked diff bytes: {metadata['diff_bytes']}",
            f"- untracked files: {metadata['untracked_count']}",
            f"- session-owned paths: {metadata['session_owned_path_count']}",
            f"- max inline file bytes: {max_file_bytes}",
            "",
        ]
    )
    lines.extend(["## Excluded Paths", ""])
    if all_exclude_prefixes:
        lines.append(
            "Sources: built-in developer-tool-managed prefixes (e.g. `.serena/`), "
            f"`{REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME}` when present, "
            "plus any `--exclude-path-prefix` arguments."
        )
        lines.append("")
        lines.extend(f"- {prefix}" for prefix in all_exclude_prefixes)
        lines.append("")
    else:
        lines.extend(["(none)", ""])
    lines.extend(["## Git Status", "", "```text", status or "(clean)", "```", ""])
    if tracker_scope is not None:
        scope_paths = [path for path in tracker_scope["paths"] if isinstance(path, str)]
        if scope_paths:
            allocated_body = owned_diff or "(no tracked diff for allocated paths)"
        else:
            allocated_body = "(allocator did not allocate any path; see Tracker Scope hunks above for unit-level scope)"
        lines.extend(
            [
                "## Allocated Git Diff",
                "",
                "Path-limited diff over the allocator-assigned paths. Reviewers must treat the listed unit_ids in `## Tracker Scope` as authoritative scope; this diff is the path projection.",
                "",
                "```diff",
                allocated_body,
                "```",
                "",
            ]
        )
    elif session_manifest is not None:
        lines.extend(
            [
                "## Session-Owned Git Diff",
                "",
                "Path-limited diff for session-owned paths. This is the default code review scope.",
                "",
                "```diff",
                owned_diff or "(no tracked diff for session-owned paths)",
                "```",
                "",
            ]
        )
    else:
        lines.extend(["## Git Diff HEAD", "", "```diff", diff or "(no tracked diff)", "```", ""])
    lines.extend(["## Untracked Files", ""])

    if not untracked:
        lines.extend(["(none)", ""])
    else:
        if session_manifest is not None and background_untracked:
            lines.extend(
                [
                    "Background untracked paths below were not attributed to this session and are not inlined:",
                    "",
                ]
            )
            lines.append(render_compact_path_tree_block(background_untracked))
            lines.append("")
        inline_untracked = owned_untracked if session_manifest is not None else untracked
        if session_manifest is not None:
            lines.extend(["Session-owned untracked file contents:", ""])
        for rel in inline_untracked:
            path = root / rel
            lines.extend([f"### {rel}", ""])
            if frozen_source is not None:
                content, info = frozen_source.read_frozen_blob_text_and_metadata(rel, max_file_bytes)
            elif not path.is_file():
                metadata["untracked_files"].append({"path": rel, "omitted": True, "reason": "not a regular file"})
                lines.extend([f"omitted: not a regular file at packet build time", ""])
                continue
            else:
                content, info = read_text(path, max_file_bytes)
            info["path"] = rel
            metadata["untracked_files"].append(info)
            lines.extend([note_from_info(info), ""])
            if content is not None:
                fence = markdown_fence(content)
                lines.extend([f"{fence}text", content.rstrip("\n"), fence, ""])

    packet = "\n".join(lines).rstrip() + "\n"
    metadata["packet_bytes"] = len(packet.encode("utf-8"))
    metadata["inlined_untracked_count"] = sum(1 for item in metadata["untracked_files"] if not item.get("omitted"))
    metadata["omitted_untracked_count"] = sum(1 for item in metadata["untracked_files"] if item.get("omitted"))
    return packet, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a self-contained review packet for review-validate-fix.")
    parser.add_argument("--repo", required=True, help="Target git repository.")
    parser.add_argument("--frozen-candidate-commit", help="从此不可变候选 commit 渲染全部代码输入，不读取活动文件。")
    parser.add_argument("--frozen-packet-context-only-path", action="append", default=[], help="只用于解析冻结策略的上下文路径，不进入审查 diff 或新增正文。")
    parser.add_argument("--session-context", help="Required file containing the main-agent work summary.")
    parser.add_argument("--session-manifest", help="Optional JSON manifest describing paths owned by this Codex session.")
    parser.add_argument("--output", help="Write packet to this file instead of stdout.")
    parser.add_argument("--metadata-output", help="Write packet metadata JSON to this file.")
    parser.add_argument("--max-file-bytes", type=int, default=200_000, help="Max untracked file bytes to inline.")
    parser.add_argument("--max-packet-bytes", type=int, default=0, help="Fail if the generated packet exceeds this many bytes. 0 disables the check.")
    parser.add_argument("--primary-file", action="append", default=[], help="Path known to be primary work for this turn. May be repeated.")
    parser.add_argument("--background-file", action="append", default=[], help="Path known to be pre-existing background WIP. May be repeated.")
    parser.add_argument("--exclude-path-prefix", action="append", default=[], help=f"Path prefix to omit from status, diff, and untracked packet sections. May be repeated. {REVIEW_VALIDATE_FIX_REPO_LOCAL_IGNORE_FILE_NAME} and the built-in developer-tool-managed prefixes are also honored.")
    parser.add_argument(
        "--base-ref",
        help="Diff base for the path-restricted owned-diff. Overrides the manifest's committed_round_baseline "
        "when set; honored only if it is an ancestor of HEAD, else falls back to HEAD. Use to review "
        "already-committed work (git diff <base-ref>) without re-dirtying it.",
    )
    parser.add_argument(
        "--allow-missing-session-context",
        action="store_true",
        help="Debug-only escape hatch. Normal review runs must pass --session-context.",
    )
    args = parser.parse_args()

    try:
        repo = Path(args.repo).expanduser().resolve()
        session_context = Path(args.session_context).expanduser().resolve() if args.session_context else None
        if session_context is not None and not session_context.exists():
            raise ValueError(f"session context file not found: {session_context}")
        session_manifest = Path(args.session_manifest).expanduser().resolve() if args.session_manifest else None
        if session_manifest is not None and not session_manifest.exists():
            raise ValueError(f"session manifest file not found: {session_manifest}")
        packet, metadata = build_packet(
            repo,
            session_context,
            session_manifest,
            args.max_file_bytes,
            args.primary_file,
            args.background_file,
            args.exclude_path_prefix,
            args.allow_missing_session_context,
            args.base_ref,
            args.frozen_candidate_commit,
            args.frozen_packet_context_only_path,
            Path(args.output).expanduser().resolve().parent / "frozen-review-evidence" if args.output else None,
        )
        if args.max_packet_bytes and metadata["packet_bytes"] > args.max_packet_bytes:
            raise ValueError(
                f"packet size {metadata['packet_bytes']} exceeds max_packet_bytes={args.max_packet_bytes}; "
                "lower --max-file-bytes or split the run context"
            )
    except Exception as exc:
        return fail(str(exc), 2)

    if args.output:
        Path(args.output).expanduser().resolve().write_text(packet, encoding="utf-8")
    else:
        print(packet, end="")
    if args.metadata_output:
        Path(args.metadata_output).expanduser().resolve().write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
