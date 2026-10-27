"""分配与渲染共享的冻结 unit 证据；分页按字节读取，兼容超长行和二进制。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_FROZEN_EVIDENCE_PAGE_BYTES = 32 * 1024


def tracked_hunk_content_identity(path: str, change_type: str, lines: list[str]) -> str:
    return hashlib.sha256(b"\0".join([
        b"hunk", path.encode(), change_type.encode(), "\n".join(lines).encode(),
    ])).hexdigest()


def project_tracked_hunk_by_content_identity(diff: str, *, path: str, unit_id: str) -> str | None:
    """旧冻结对象也按同一内容身份恢复；相同坐标不代表相同 unit。"""
    preamble = []
    pending_header = None
    body: list[str] = []
    selected = None

    def finish() -> None:
        nonlocal selected
        if pending_header and tracked_hunk_content_identity(path, "modify", body) == unit_id:
            selected = "\n".join([*preamble, pending_header, *body]) + "\n"

    for line in diff.splitlines():
        if line.startswith("@@"):
            finish()
            pending_header, body = line, []
        elif pending_header:
            # 文件头已在 preamble 中；hunk 内以 +++/--- 开头的行仍是正文。
            body.append(line)
        else:
            preamble.append(line)
    finish()
    return selected


def read_worktree_path_git_blob_input(path: Path) -> bytes:
    """Git 对符号链接保存链接文本，不读取链接目标的文件内容。"""
    return os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()


def write_worktree_path_git_normalized_blob(repo: Path, path: str, content: bytes, *,
                                            is_symbolic_link: bool) -> str:
    """普通文件按路径应用 clean/行尾规则，符号链接按 Git 语义跳过过滤。"""
    filters = ["--no-filters"] if is_symbolic_link else ["--path=" + path]
    return subprocess.check_output(["git", "hash-object", "-w", *filters, "--stdin"],
                                   input=content, cwd=repo).decode().strip()


def freeze_observed_unit_evidence(repo: Path, observation: Any, specs: list[Any], *,
                                  source: str, baseline_commit: str, target_commit: str) -> list[dict[str, Any]]:
    """保留实际枚举过的 hunk，而非后来从累计 diff 猜测分配范围。"""
    postimage_blob = None
    evidence_gap = None if baseline_commit else "仓库尚无原始 HEAD，等待正式候选冻结"
    if observation.change_type != "delete":
        try:
            if source == "committed":
                postimage_blob = subprocess.check_output(
                    ["git", "rev-parse", f"{target_commit}:{observation.path}"], cwd=repo, text=True,
                ).strip()
            else:
                path = repo / observation.path
                content = read_worktree_path_git_blob_input(path)
                if (hashlib.sha256(content).hexdigest() != observation.observed_worktree_content_sha256
                        or path.is_symlink() != observation.observed_worktree_path_is_symbolic_link):
                    raise ValueError("枚举后文件内容已变化")
                # 复用从同一份原始字节生成的规范化 blob；后续还须与正式候选核对。
                postimage_blob = observation.git_normalized_postimage_blob
                if not postimage_blob:
                    raise ValueError("枚举时缺少 Git 规范化 blob 身份")
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            evidence_gap = str(exc)
    hunks = {hunk.canonical_hash: hunk for hunk in observation.hunks}
    records = []
    for spec in specs:
        hunk = hunks.get(spec.unit_id)
        patch = (hunk.anchor.header + "\n" + "\n".join(hunk.lines) + "\n") if hunk else None
        records.append({
            "unit_id": spec.unit_id, "path": spec.path, "kind": spec.kind,
            "change_type": spec.change_type, "source": source,
            "baseline_commit": baseline_commit, "target_commit": target_commit,
            "preimage_blob": spec.preimage_blob, "postimage_blob": postimage_blob,
            "postimage_sha256": spec.postimage_hash,
            "observed_worktree_content_sha256": observation.observed_worktree_content_sha256,
            "observed_worktree_path_is_symbolic_link": observation.observed_worktree_path_is_symbolic_link,
            "hunk_patch": patch,
            "hunk_patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch else None,
            "evidence_gap": evidence_gap,
        })
    return records


def _verify_frozen_hunk_patch_blob_line_ranges(repo: Path, patch: str, *,
                                               preimage_blob: str, postimage_blob: str) -> None:
    """逐侧核对原始片段及换行状态，不依赖可变的 Git diff 分段设置。"""
    patch_lines = patch.splitlines()
    header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)",
                          patch_lines[0] if patch_lines else "")
    if header is None:
        raise ValueError("冻结 hunk header 无效")
    before_lines: list[tuple[str, bool]] = []
    after_lines: list[tuple[str, bool]] = []
    previous_prefix = None
    has_change = False
    for line in patch_lines[1:]:
        if line == "\\ No newline at end of file":
            if previous_prefix is None:
                raise ValueError("冻结 hunk 换行标记缺少对应正文")
            if previous_prefix != "+":
                before_lines[-1] = (before_lines[-1][0], False)
            if previous_prefix != "-":
                after_lines[-1] = (after_lines[-1][0], False)
            previous_prefix = None
            continue
        if not line or line[0] not in " +-":
            raise ValueError("冻结 hunk 正文格式无效")
        prefix, content = line[0], line[1:]
        if prefix != "+":
            before_lines.append((content, True))
        if prefix != "-":
            after_lines.append((content, True))
        has_change = has_change or prefix in "+-"
        previous_prefix = prefix
    if not has_change:
        raise ValueError("冻结 hunk 缺少实际变更正文")
    for blob, start_text, count_text, body in (
        (preimage_blob, header[1], header[2], before_lines),
        (postimage_blob, header[3], header[4], after_lines),
    ):
        start, count = int(start_text), int(count_text) if count_text is not None else 1
        if len(body) != count:
            raise ValueError("冻结 hunk 正文行数与 header 不一致")
        content = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=repo, text=True)
        blob_lines = [(line[:-1], True) if line.endswith("\n") else (line, False)
                      for line in content.splitlines(keepends=True)]
        offset = start if count == 0 else start - 1
        if offset < 0 or offset + count > len(blob_lines) or blob_lines[offset:offset + count] != body:
            raise ValueError("冻结 hunk 正文或坐标与实际 blob 不一致")


def verify_frozen_unit_evidence(record: dict[str, Any], unit_id: str, path: str, source: Any, *,
                                resolved_postimage_content_sha256: str | None) -> None:
    """恢复冻结片段之前核验身份、来源及 blob；不把呈现漂移改成更宽的权限。"""
    if record.get("unit_id") != unit_id or record.get("path") != path or record.get("evidence_gap"):
        raise ValueError("冻结 unit 身份不一致或内容存在缺口")
    if record.get("postimage_sha256") and record["postimage_sha256"] != resolved_postimage_content_sha256:
        raise ValueError("冻结 unit 内容摘要与实际 blob 不一致")
    origin = source.frozen_origin_worktree_head_commit_sha
    if record.get("source") == "dirty":
        if record.get("baseline_commit") != origin:
            raise ValueError("dirty unit 未绑定候选原始 HEAD")
        target = source.frozen_candidate_commit_sha
    elif record.get("source") == "committed":
        target = record["target_commit"]
        result = subprocess.run(["git", "merge-base", "--is-ancestor", target, origin],
                                cwd=source.repository_root, capture_output=True, check=False)
        if result.returncode:
            raise ValueError("committed unit 未绑定候选历史")
    else:
        raise ValueError("冻结 unit 来源未知")
    for commit, field in ((record["baseline_commit"], "preimage_blob"), (target, "postimage_blob")):
        result = subprocess.run(["git", "rev-parse", "--verify", f"{commit}:{path}"],
                                cwd=source.repository_root, text=True, capture_output=True, check=False)
        actual = result.stdout.strip() if result.returncode == 0 else None
        if actual != record.get(field):
            raise ValueError("冻结 unit blob 与候选来源不一致")
    patch = record.get("hunk_patch")
    if patch:
        if hashlib.sha256(patch.encode()).hexdigest() != record.get("hunk_patch_sha256"):
            raise ValueError("冻结 hunk 摘要不一致")
        if tracked_hunk_content_identity(path, record["change_type"], patch.splitlines()[1:]) != unit_id:
            raise ValueError("冻结 hunk 内容与 unit 身份不一致")
        _verify_frozen_hunk_patch_blob_line_ranges(source.repository_root, patch,
            preimage_blob=record["preimage_blob"], postimage_blob=record["postimage_blob"])
    else:
        kind = record.get("kind")
        if record.get("change_type") == "add" and kind in {"tracked_hunk", "untracked_file"}:
            identity_parts = ["untracked", path, record.get("postimage_sha256") or ""]
        elif kind == "deleted_file":
            identity_parts = ["delete", path, record.get("preimage_blob") or ""]
        elif kind == "binary_file":
            identity_parts = ["binary", path, record.get("preimage_blob") or "", record.get("postimage_sha256") or ""]
        elif kind == "path_only":
            identity_parts = ["path_only", path]
        else:
            raise ValueError("已分配 hunk 缺少冻结内容")
        if hashlib.sha256("\0".join(identity_parts).encode()).hexdigest() != unit_id:
            raise ValueError("冻结文件内容与 unit 身份不一致")


def read_frozen_evidence_page(path: Path, *, expected_sha256: str, offset: int = 0,
                              max_bytes: int = DEFAULT_FROZEN_EVIDENCE_PAGE_BYTES) -> dict[str, Any]:
    if offset < 0 or max_bytes < 1 or max_bytes > DEFAULT_FROZEN_EVIDENCE_PAGE_BYTES:
        raise ValueError("分页 offset 必须非负，单页为 1..32768 字节")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
        size = handle.tell()
        if digest.hexdigest() != expected_sha256:
            raise ValueError("冻结证据摘要不一致")
        if offset > size:
            raise ValueError("分页 offset 超出证据内容")
        handle.seek(offset)
        content = handle.read(max_bytes)
    return {"offset": offset, "next_offset": offset + len(content), "total_bytes": size,
            "has_more": offset + len(content) < size, "content_base64": base64.b64encode(content).decode(),
            "content_text": content.decode("utf-8", "replace"), "sha256": expected_sha256}


def write_frozen_evidence_file(directory: Path, name: str, content: bytes) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    path = directory / f"{digest}.{name}"
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("既有冻结证据文件损坏")
    else:
        pending = directory / f"pending-frozen-review-evidence-{secrets.token_hex(8)}"
        try:
            with pending.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    return {"file": str(path.resolve()), "sha256": digest, "bytes": len(content)}
