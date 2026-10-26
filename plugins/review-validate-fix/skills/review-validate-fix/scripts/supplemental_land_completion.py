"""历史 Land 的追加证明：固定内容补审、隔离验证及可独立重放的验收。

此入口只生产证据，不提交、不封窗、不整合，也不改变任何执行授权。
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401
from core.review_start_boundary.review_candidate_snapshot import SNAPSHOT_BINDING_FIELDS, SNAPSHOT_BINDING_PREFIX, SNAPSHOT_SCHEMA
from independent_backup_route import (
    BackupRouteOwned, assert_child_process_groups_stopped, control_directory, current_boot_session_id, durable_json,
    marker_present, normal_worktree_mutation, tracked_subprocess_run,
)
from reviewed_candidate_completion_facts import (
    applied_scope_expansion_facts, completed_review_facts, handoff_completion_marker_facts,
    read_regular_json_object, safe_relative_paths,
)
from validation_result_cache import (
    CONTENT_MANIFEST_ALGORITHM, atomic_write, canonical_json_bytes, clean_git_environment,
    environment_fingerprint, load_handoff_validation_commands, repository_identity,
    validated_validation_record_reference, validation_record_reference,
)
from write_reviewed_task_candidate_evidence import reviewed_scope_path_universe, validated_canonical_artifact, validated_scope_contract

SCHEMA = "rvf-supplemental-land-completion-evidence"
VERSION = 1
ARTIFACT_DIRECTORY = "artifacts/supplemental-land-completion-evidence"
MAXIMUM_RECOVERY_ATTEMPTS_PER_INPUT_AND_PHASE = 3
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class MachineEvidencePending(ValueError):
    pass


class AppliedPatchIdentityMismatch(ValueError):
    pass


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def payload_digest(value: Any) -> str:
    return digest_bytes(canonical_json_bytes(value))


def git_bytes(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    environment = clean_git_environment()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = tracked_subprocess_run(
        ["git", "-c", "core.hooksPath=/dev/null", "--literal-pathspecs", *arguments],
        cwd=repository, env=environment, input=input_bytes, capture_output=True,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip() or f"Git 核验失败：{arguments[0]}")
    return result.stdout


def git_text(repository: Path, *arguments: str) -> str:
    return git_bytes(repository, *arguments).decode().strip()


def require_regular_file(path: Path, root: Path) -> bytes:
    if not path.is_relative_to(root) or any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root)):
        raise ValueError(f"证据路径不是安全普通文件：{path}")
    if not path.is_file():
        raise MachineEvidencePending(f"缺少机器证据：{path}")
    return path.read_bytes()


def capture_run_sources(run_directory: Path) -> dict[str, str]:
    """只快照判据所需原件；不复制可回收工作树或先前生成的补充证明。"""
    required = ["summary.json", "artifacts/inputs/scope.contract.json", "artifacts/handoff.md",
                "artifacts/reviewers/reviewer-plan.json"]
    optional = ["artifacts/inputs/scope.contract.reviewed.json", "artifacts/handoff-validation-commands.json",
                "artifacts/inputs/scope.contract.reviewed.json.sha256",
                "artifacts/handoff-validation-commands.json.sha256", "artifacts/validation-records.json",
                "artifacts/validation-records.json.sha256"]
    patterns = ["artifacts/reviewers/*/review-result.json", "artifacts/merge/**/*.json", "artifacts/merge/**/*.sha256",
                "artifacts/fix-issues/*.json", "artifacts/fix-attempts/*/attempt.json",
                "artifacts/fix-attempts/*/result.json", "artifacts/fix-attempts/*/fix.patch"]
    paths = set(required) | {name for name in optional if (run_directory / name).exists()}
    paths.update(str(path.relative_to(run_directory)) for pattern in patterns for path in run_directory.glob(pattern))
    return {name: base64.b64encode(require_regular_file(run_directory / name, run_directory)).decode()
            for name in sorted(paths)}


def restore_run_sources(directory: Path, sources: dict[str, str]) -> None:
    safe_relative_paths(list(sources))
    for relative, encoded in sources.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(encoded, validate=True))


def capture_backlog_sources(source_run: Path) -> dict[str, str]:
    paths = set()
    for result_path in (source_run / "artifacts/fix-attempts").glob("*/result.json"):
        receipt = read_regular_json_object(result_path).get("kanban_backlog_submission") or {}
        if receipt.get("ledger_path"):
            paths.add(receipt["ledger_path"])
        paths.update(group["pending_prompt_path"] for group in receipt.get("groups", []) if group.get("pending_prompt_path"))
    captured = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("backlog 原始材料路径无效")
        if path.is_file():
            captured[raw_path] = base64.b64encode(path.read_bytes()).decode()
            if path.suffix == ".jsonl":
                for line in path.read_text().splitlines():
                    entry = json.loads(line)
                    pending_path = Path(entry.get("pending_prompt_path", ""))
                    if entry.get("outcome") == "pending_kanban_runtime_unavailable" and pending_path.is_absolute() and pending_path.is_file() and not pending_path.is_symlink():
                        captured[str(pending_path)] = base64.b64encode(pending_path.read_bytes()).decode()
    return captured


def backlog_snapshot_reader(sources: dict[str, str], source_run: Path):
    ledger_paths = {receipt["ledger_path"] for result_path in (source_run / "artifacts/fix-attempts").glob("*/result.json")
                    for receipt in [read_regular_json_object(result_path).get("kanban_backlog_submission") or {}] if receipt.get("ledger_path")}
    def read(path: Path) -> dict:
        if str(path) not in sources:
            raise ValueError("backlog 不可变原始材料缺失")
        encoded = base64.b64decode(sources[str(path)], validate=True)
        if str(path) in ledger_paths:
            return {entry.get("finding_fingerprint"): entry for line in encoded.decode().splitlines()
                    if line.strip() for entry in [json.loads(line)] if isinstance(entry, dict)}
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            raise ValueError("backlog 快照不是 JSON object")
        return payload
    return read


def tree_entries(repository: Path, tree: str) -> dict[str, list[str]]:
    entries = {}
    for entry in git_bytes(repository, "ls-tree", "-rz", tree).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, _kind, oid = metadata.decode().split()
            entries[os.fsdecode(path)] = [mode, oid]
    safe_relative_paths(list(entries))
    return entries


def tree_content_manifest(repository: Path, tree: str) -> str:
    """与验证缓存相同口径，直接读取提交字节，不依赖当前工作树。"""
    entries = tree_entries(repository, tree)
    digest = hashlib.sha256()
    paths = sorted(entries, key=os.fsencode)
    blob_oids = [entries[path][1] for path in paths if entries[path][0] != "160000"]
    # 大仓库不能为每个 blob 启动一个 Git 进程；按 manifest 顺序批量读取原始对象。
    batch = git_bytes(repository, "cat-file", "--batch", input_bytes=("\n".join(blob_oids) + "\n").encode()) if blob_oids else b""
    offset = 0
    for path in paths:
        mode, oid = entries[path]
        raw_path = os.fsencode(path)
        digest.update(b"P" + struct.pack(">Q", len(raw_path)) + raw_path)
        if mode == "160000":
            raw = oid.encode()
            digest.update(b"G" + struct.pack(">Q", len(raw)) + raw)
        else:
            header_end = batch.index(b"\n", offset)
            actual_oid, kind, raw_size = batch[offset:header_end].decode().split()
            if actual_oid != oid or kind != "blob":
                raise ValueError("Git 批量对象读取身份不匹配")
            size = int(raw_size)
            offset = header_end + 1
            raw = memoryview(batch)[offset:offset + size]
            offset += size + 1
            if mode == "120000":
                digest.update(b"L" + struct.pack(">Q", len(raw)) + raw)
            elif mode in {"100644", "100755"}:
                digest.update(b"F" + (b"X" if mode == "100755" else b"-") + struct.pack(">Q", len(raw)) + raw)
            else:
                raise ValueError(f"不支持的 Git 文件类型：{path}")
    return digest.hexdigest()


def initialize_object_workspace(repository: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    git_text(directory, "init", "-q", "--object-format=" + git_text(repository, "rev-parse", "--show-object-format"))
    objects = Path(git_text(repository, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
    (directory / ".git/objects/info/alternates").write_text(str(objects) + "\n")
    return directory


def validate_original_binding(repository: Path, source_run: Path, expected: dict[str, str], backlog_sources: dict[str, str]) -> tuple[dict, dict, dict]:
    summary = read_regular_json_object(source_run / "summary.json")
    raw_contract = read_regular_json_object(source_run / "artifacts/inputs/scope.contract.json")
    historical_repository = Path(str(raw_contract.get("repo", ""))).resolve()
    contract, _path, _digest = validated_scope_contract(source_run, historical_repository)
    # 已有 restamp 必须保留原冻结 contract；不把事后新 tip 当作原审查输入。
    reviewed_path = source_run / "artifacts/inputs/scope.contract.reviewed.json"
    if reviewed_path.exists():
        reviewed, reviewed_digest = validated_canonical_artifact(reviewed_path, "原冻结 scope")
        history = contract.get("landing_restamp") or []
        if history:
            if (not isinstance(history, list) or not isinstance(history[0], dict)
                    or history[0].get("reviewed_contract_sha256") != reviewed_digest
                    or history[0].get("reviewed_scope_hash") != reviewed.get("scope_hash")
                    or history[0].get("run_id") != expected["run_id"]):
                raise ValueError("原冻结 scope 的 restamp 来源不匹配")
        elif reviewed_digest != _digest:
            raise ValueError("原冻结 scope 归档没有绑定当前 contract")
        # 在独立视图中复用同一完整 validator；不改写当前 contract 或冻结原件。
        with tempfile.TemporaryDirectory(prefix="rvf-frozen-scope-verification-") as directory:
            frozen_view = Path(directory)
            frozen_contract_path = frozen_view / "artifacts/inputs/scope.contract.json"
            frozen_contract_path.parent.mkdir(parents=True)
            frozen_contract_path.write_bytes(canonical_json_bytes(reviewed))
            contract, _path, _digest = validated_scope_contract(frozen_view, historical_repository)
    if summary.get("run_id") != expected["run_id"] or contract.get("run_id") != expected["run_id"]:
        raise ValueError("run 身份不匹配")
    if summary.get("repo") != str(historical_repository):
        raise ValueError("summary 与 scope 的仓库归属不匹配")
    snapshot = contract.get("review_candidate_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise MachineEvidencePending("原不可变候选快照缺失；恢复绑定 token 的快照后重验")
    if (snapshot.get("decision_token") != expected["decision_token"] or snapshot.get("kanban_task_id") != expected["task_id"]
            or snapshot.get("worktree") != str(historical_repository)):
        raise ValueError("候选快照 token/task/仓库身份不匹配")
    checkpoint = snapshot.get("checkpoint_commit", "")
    if not GIT_COMMIT.fullmatch(checkpoint):
        raise ValueError("原候选 commit 身份格式无效")
    try:
        checkpoint_tree = git_text(repository, "rev-parse", checkpoint + "^{tree}")
    except ValueError as exc:
        raise MachineEvidencePending(f"缺少原候选 Git 对象 {checkpoint}，从绑定 run 的可信归档恢复该对象后重新对账") from exc
    if checkpoint_tree != snapshot.get("checkpoint_tree"):
        raise ValueError("原候选 commit/tree 不匹配")
    binding_lines = [line[len(SNAPSHOT_BINDING_PREFIX):] for line in git_text(repository, "show", "-s", "--format=%B", checkpoint).splitlines()
                     if line.startswith(SNAPSHOT_BINDING_PREFIX)]
    if len(binding_lines) != 1 or json.loads(binding_lines[0]) != {key: snapshot.get(key) for key in SNAPSHOT_BINDING_FIELDS}:
        raise ValueError("候选 Git 对象没有绑定原 token/task/scope")
    parent = git_text(repository, "rev-parse", checkpoint + "^")
    if parent != snapshot.get("worktree_head_commit_sha"):
        raise ValueError("候选父提交不匹配")
    git_bytes(repository, "merge-base", "--is-ancestor", parent, expected["target_commit"])
    handoff = (source_run / "artifacts/handoff.md").read_bytes()
    if digest_bytes(handoff) != expected["handoff_sha256"]:
        raise ValueError("handoff 摘要不匹配")
    facts = completed_review_facts(source_run, handoff.decode(), allow_missing_completion_markers=True,
                                   backlog_artifact_reader=backlog_snapshot_reader(backlog_sources, source_run),
                                   original_run_directory=Path(expected["run_directory"]))
    applied_scope_expansion_facts(facts["applied_attempts"], reviewed_scope_path_universe(contract), source_run, contract=contract,
                                 original_run_directory=Path(expected["run_directory"]))
    return contract, snapshot, facts


def reconstruct_reviewed_tree(repository: Path, source_run: Path, snapshot: dict, facts: dict) -> dict:
    """按完整前像拓扑重放；不以时间排序证明应用，也不让偏移应用掩盖同路径漂移。"""
    tree = snapshot["checkpoint_tree"]
    pending = []
    uncertain = set()
    retained_commits = {snapshot["checkpoint_commit"]}
    for item in facts["applied_attempts"]:
        attempt = item["attempt"]
        changed = safe_relative_paths(item["result"].get("changed_paths", []))
        baseline = attempt.get("baseline_commit")
        patch = source_run / "artifacts/fix-attempts" / attempt["attempt_id"] / "fix.patch"
        raw_patch = require_regular_file(patch, source_run)
        try:
            if not isinstance(baseline, str) or not GIT_COMMIT.fullmatch(baseline):
                raise ValueError("缺失 baseline")
            git_text(repository, "read-tree", baseline)
            git_bytes(repository, "apply", "--cached", "--whitespace=nowarn", "--binary", "-", input_bytes=raw_patch)
            post_tree = git_text(repository, "write-tree")
            baseline_entries = tree_entries(repository, baseline)
            post_entries = tree_entries(repository, post_tree)
            actual = {path for path in baseline_entries.keys() | post_entries.keys() if baseline_entries.get(path) != post_entries.get(path)}
            if actual != changed:
                raise AppliedPatchIdentityMismatch("补丁真实路径与 result 不匹配")
            retained_commits.add(baseline)
            pending.append({"attempt_id": attempt["attempt_id"], "paths": changed, "before": baseline_entries,
                            "after": post_entries, "patch": raw_patch})
        except AppliedPatchIdentityMismatch:
            raise
        except ValueError:
            # 无法可靠重建的补丁不获复用资格；整个声明路径进入双盲补审。
            uncertain.update(changed)
            if not changed:
                raise MachineEvidencePending("已应用补丁缺少可重建路径，需要恢复原 baseline/patch")
    replayed = []
    while pending:
        entries = tree_entries(repository, tree)
        ready = [item for item in pending if not item["paths"] & uncertain
                 and all(entries.get(path) == item["before"].get(path) for path in item["paths"])]
        if not ready:
            uncertain.update(path for item in pending for path in item["paths"])
            break
        overlapping = {path for item in ready for path in item["paths"]
                       if sum(path in other["paths"] for other in ready) > 1}
        if overlapping:
            uncertain.update(path for item in ready if item["paths"] & overlapping for path in item["paths"])
            # 过滤前传播完整依赖路径闭包，保留被过滤补丁涉及的其他整路径补审义务。
            while True:
                dependent_patch_paths = {path for item in pending if item["paths"] & uncertain for path in item["paths"]}
                if dependent_patch_paths <= uncertain:
                    break
                uncertain.update(dependent_patch_paths)
            pending = [item for item in pending if not item["paths"] & uncertain]
            continue
        for item in sorted(ready, key=lambda entry: entry["attempt_id"]):
            git_text(repository, "read-tree", tree)
            git_bytes(repository, "apply", "--cached", "--whitespace=nowarn", "--binary", "-", input_bytes=item["patch"])
            tree = git_text(repository, "write-tree")
            replayed.append(item["attempt_id"])
            pending.remove(item)
    return {"reconstructed_tree": tree, "replayed_attempt_ids": replayed,
            "whole_path_review_required": sorted(uncertain), "retained_commits": sorted(retained_commits)}


def build_supplemental_review_subject(repository: Path, source_run: Path, expected: dict, backlog_sources: dict[str, str]) -> dict:
    contract, snapshot, facts = validate_original_binding(repository, source_run, expected, backlog_sources)
    reconstruction = reconstruct_reviewed_tree(repository, source_run, snapshot, facts)
    target_tree = git_text(repository, "rev-parse", expected["target_commit"] + "^{tree}")
    before = tree_entries(repository, reconstruction["reconstructed_tree"])
    after = tree_entries(repository, target_tree)
    paths = sorted({path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
                   | set(reconstruction["whole_path_review_required"]))
    patch = git_bytes(repository, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", "--full-index", "--unified=80",
                      reconstruction["reconstructed_tree"], target_tree)
    context = []
    for path in reconstruction["whole_path_review_required"]:
        for label, entries in (("重建前像", before), ("实际目标", after)):
            entry = entries.get(path)
            raw = git_bytes(repository, "cat-file", "blob", entry[1]) if entry and entry[0] != "160000" else b""
            context.append({"path": path, "side": label, "entry": entry, "content_base64": base64.b64encode(raw).decode()})
    subject = {**expected, "target_tree": target_tree, "candidate_content_manifest_algorithm": CONTENT_MANIFEST_ALGORITHM,
               "candidate_content_manifest_sha256": tree_content_manifest(repository, target_tree),
               "original_scope_hash": contract["scope_hash"], "checkpoint_commit": snapshot["checkpoint_commit"],
               "backlog_source_snapshots_sha256": payload_digest(backlog_sources),
               **reconstruction, "supplemental_review_paths": paths,
               "path_entries": [{"path": path, "before": before.get(path), "after": after.get(path)} for path in paths],
               "patch_base64": base64.b64encode(patch).decode(), "patch_sha256": digest_bytes(patch),
               "whole_path_context": context,
               "historical_completion_facts": {key: value for key, value in facts.items() if key != "applied_attempts"},
               "handoff_marker_facts": handoff_completion_marker_facts((source_run / "artifacts/handoff.md").read_text(), allow_missing=True)}
    subject["supplemental_review_subject_sha256"] = payload_digest(subject)
    return subject


def review_packet(subject: dict) -> bytes:
    return ("# 历史 Land 差异补审\n\n只审查下列固定差异及必要上下文。两路 reviewer 独立执行。"
            "这是事后补审，不能宣称提交前已审。目标仓库禁止修改或提交。\n\n"
            + json.dumps({key: subject[key] for key in ("run_id", "task_id", "decision_token", "target_commit", "target_tree",
                         "supplemental_review_subject_sha256", "supplemental_review_paths", "path_entries", "whole_path_context")}, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n\n```diff\n" + base64.b64decode(subject["patch_base64"]).decode(errors="replace") + "\n```\n").encode()


def collect_validation_snapshots(source_run: Path, common_directory: Path) -> list[dict]:
    path = source_run / "artifacts/validation-records.json"
    if not path.exists():
        return []
    index = read_regular_json_object(path)
    snapshots = []
    worktree_registrations = None
    for reference in index.get("validation_records", []):
        try:
            record, digest = validated_validation_record_reference(reference, expected_git_common_directory=common_directory)
            snapshot = {"reference": reference, "record": record, "record_sha256": digest}
            if record.get("requires_base_worktree_context") and record.get("validation_context_kind") == "base_worktree":
                if worktree_registrations is None:
                    # 由同仓 Git 的原生注册反查执行时路径摘要，不接受调用者声明的扩大路径。
                    # prunable 注册仍是历史归属材料；旧目录当前是否存在不改变过去的执行事实。
                    worktree_registrations = git_bytes(common_directory, "worktree", "list", "--porcelain", "-z").split(b"\0\0")
                matching = [entry for entry in worktree_registrations if entry.startswith(b"worktree ")
                            and digest_bytes(entry.split(b"\0", 1)[0][len(b"worktree "):])
                            == record["validation_context_worktree_realpath_sha256"]]
                if len(matching) == 1:
                    snapshot["base_worktree_context"] = {
                        "repository_git_common_directory": str(common_directory.resolve()),
                        "git_worktree_registration_base64": base64.b64encode(matching[0]).decode(),
                    }
            snapshots.append(snapshot)
        except (OSError, ValueError):
            # 缓存被清理或路径漂移时自动重验，不把缺失缓存升级成人工必答。
            continue
    return snapshots


def validate_base_worktree_context(snapshot: dict, expected_git_common_directory: Path) -> None:
    """用已核验原生缓存及 Git 注册固定历史上下文，离线核验不追读旧目录。"""
    record = snapshot["record"]
    context = snapshot.get("base_worktree_context")
    if not isinstance(context, dict):
        raise MachineEvidencePending("缺少与原生 base 路径摘要匹配的同仓 Git worktree 注册；恢复绑定注册材料后重新对账")
    common_directory = str(expected_git_common_directory.resolve())
    if context.get("repository_git_common_directory") != common_directory:
        raise ValueError("base 验证上下文属于另一 Git 仓库")
    registration = base64.b64decode(context["git_worktree_registration_base64"], validate=True)
    fields = registration.split(b"\0")
    if (not fields[0].startswith(b"worktree ") or any(not field for field in fields)
            or len([field for field in fields if field.startswith(b"worktree ")]) != 1
            or len([field for field in fields if field.startswith(b"HEAD ") and GIT_COMMIT.fullmatch(os.fsdecode(field[5:]))]) != 1):
        raise ValueError("base 验证的原生 worktree 注册无效")
    worktree_path = fields[0][len(b"worktree "):]
    if (not Path(os.fsdecode(worktree_path)).is_absolute()
            or digest_bytes(worktree_path) != record.get("validation_context_worktree_realpath_sha256")):
        raise ValueError("base 验证的原生路径摘要与 worktree 注册不匹配")
    record_path = Path(str(record.get("cache_record_path", "")))
    if (not record_path.is_absolute() or record_path.name != "record.json" or record_path.parent.name != record["cache_key"]
            or record_path.parent.parent.name != "entries"
            or record_path.parent.parent.parent.name != digest_bytes(os.fsencode(common_directory))
            or record_path.parent.parent.parent.parent.name != "validation-result-cache-v2"):
        raise ValueError("base 验证的原生缓存路径没有绑定同仓上下文身份")


def validate_validation_snapshots(subject: dict, commands: list[dict], snapshots: list[dict], *, check_current_environment: bool,
                                  expected_git_common_directory: Path) -> list[dict]:
    accepted = []
    for command in commands:
        command_digest = payload_digest({"argv": command["argv"], "working_directory_relative_to_worktree": command["working_directory_relative_to_worktree"]})
        matching = []
        for snapshot in snapshots:
            record, reference = snapshot["record"], snapshot["reference"]
            if payload_digest(record) != snapshot["record_sha256"] or validation_record_reference(record, snapshot["record_sha256"]) != reference:
                raise ValueError("验证原始记录摘要或引用不匹配")
            if record.get("schema") != "rvf-validation-result-cache-record" or record.get("schema_version") != 2:
                raise ValueError("验证记录 schema 无效")
            identity_keys = ("schema_version", "name", "candidate_content_manifest_algorithm", "candidate_content_manifest_sha256",
                             "command_sha256", "environment_fingerprint_sha256", "requires_base_worktree_context",
                             "validation_context_kind", "validation_context_worktree_realpath_sha256")
            if (record.get("candidate_content_manifest_algorithm") != CONTENT_MANIFEST_ALGORITHM
                    or record.get("cache_key") != payload_digest({key: record.get(key) for key in identity_keys})):
                raise ValueError("验证记录缓存身份不匹配")
            if (record.get("name") != command["name"] or record.get("candidate_content_manifest_sha256") != subject["candidate_content_manifest_sha256"]
                    or record.get("command_sha256") != command_digest or record.get("validation_context_kind") != command["validation_context_kind"]
                    or record.get("requires_base_worktree_context") != command["requires_base_worktree_context"]):
                continue
            if command["requires_base_worktree_context"]:
                if command["validation_context_kind"] == "base_worktree":
                    validate_base_worktree_context(snapshot, expected_git_common_directory)
                elif record.get("validation_context_worktree_realpath_sha256") != digest_bytes(os.fsencode(subject["historical_worktree"])):
                    continue
            elif record.get("validation_context_worktree_realpath_sha256") is not None:
                raise ValueError("验证上下文绑定无效")
            if check_current_environment and record.get("environment_fingerprint_sha256") != environment_fingerprint(command["argv"], command["environment_keys"])[0]:
                continue
            matching.append(snapshot)
        if not matching:
            raise MachineEvidencePending(f"缺少匹配目标内容、命令及上下文的通过验证：{command['name']}")
        latest = max(matching, key=lambda item: item["record"].get("executed_at", ""))
        record = latest["record"]
        if record.get("status") != "passed" or type(record.get("exit_code")) is not int or record["exit_code"] != 0:
            raise ValueError(f"目标验证失败：{command['name']}")
        accepted.append(latest)
    if not commands:
        raise MachineEvidencePending("缺少结构化验证命令，需要恢复原验证 argv")
    return accepted


def current_execution_eligibility(repository: Path, expected: dict) -> dict:
    # 此接口从不授予后续动作权限，消费者必须另行核验 Stop/paused/授权。
    blocked = marker_present(control_directory(repository) / "active-owner")
    return {"status": "execution_prohibited" if blocked else "requires_current_authorization_check",
            "reason_code": "backup_route_owns_execution" if blocked else "supplemental_evidence_does_not_grant_execution_authority",
            "stop_restrictions_unchanged": True, "task_id": expected["task_id"]}


def prepare_isolated_node_dependencies(workspace: Path, commands: list[dict], operation_directory: Path) -> None:
    """按目标提交的 npm lockfile 恢复隔离依赖，不借用旧 worktree 的 node_modules。"""
    node_commands = {"node", "npm", "npx", "pnpm", "yarn", "bun", "biome"}
    if not any(Path(argument).name in node_commands for command in commands for argument in command["argv"][:3]):
        return
    entries = tree_entries(workspace, "HEAD")
    for relative in sorted(entries):
        if Path(relative).name != "package-lock.json":
            continue
        package_directory = workspace / Path(relative).parent
        if not (package_directory / "package.json").is_file():
            continue
        lock_bytes = require_regular_file(workspace / relative, workspace)
        require_regular_file(package_directory / "package.json", workspace)
        identity = payload_digest({"lockfile_path": relative, "lockfile_sha256": digest_bytes(lock_bytes),
                                   "environment_sha256": environment_fingerprint(["npm", "ci"], [])[0]})
        receipt_path = operation_directory / "isolated-node-dependency-preparation" / (identity + ".json")
        if receipt_path.exists():
            receipt = read_regular_json_object(receipt_path)
            if receipt.get("status") == "passed" and (package_directory / "node_modules").is_dir():
                continue
        try:
            completed = tracked_subprocess_run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=package_directory, env=clean_git_environment(), capture_output=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MachineEvidencePending(f"隔离 npm 依赖尚不可用，恢复运行时/网络后自动接续：{relative}：{exc}") from exc
        receipt = {"input_fingerprint": identity, "lockfile_path": relative,
            "status": "passed" if completed.returncode == 0 else "awaiting_machine_evidence", "exit_code": completed.returncode,
            "stdout_base64": base64.b64encode(completed.stdout).decode(), "stderr_base64": base64.b64encode(completed.stderr).decode()}
        durable_json(receipt_path.parent / "execution-results" / (payload_digest(receipt) + ".json"), receipt)
        durable_json(receipt_path, receipt)
        if completed.returncode:
            raise MachineEvidencePending(f"隔离 npm 依赖安装未完成，恢复条件及输出：{receipt_path}")


def validated_supplemental_review(subject: dict, directory: Path, *, require_execution_receipt: bool = True,
                                  backlog_sources: dict | None = None, original_directory: Path | None = None,
                                  verify_live_target: bool = True) -> dict:
    for relative in ("artifacts/inputs/supplemental-review-binding.json", "artifacts/reviewers/reviewer-plan.json"):
        if not (directory / relative).exists():
            raise MachineEvidencePending(f"缺少补审工件：{relative}")
    binding = read_regular_json_object(directory / "artifacts/inputs/supplemental-review-binding.json")
    if binding != {"schema": "rvf-supplemental-review-binding", "schema_version": 1,
                   "subject_sha256": subject["supplemental_review_subject_sha256"], "packet_sha256": digest_bytes(review_packet(subject))}:
        raise ValueError("补审输入绑定不匹配")
    qualification = read_regular_json_object(directory / "artifacts/inputs/supplemental-review-dispatch-qualification.json")
    if (qualification.get("schema") != "rvf-supplemental-review-dispatch-qualification"
            or qualification.get("schema_version") != 1 or qualification.get("subject") != subject):
        raise ValueError("补审独立派发资格未绑定同一历史事实及内容")
    if (directory / "artifacts/inputs/review-packet.md").read_bytes() != review_packet(subject):
        raise ValueError("补审固定差异已变化")
    contract = read_regular_json_object(directory / "artifacts/inputs/scope.contract.json")
    if (contract.get("primary_files") != subject["supplemental_review_paths"]
            or contract.get("supplemental_review_subject_sha256") != subject["supplemental_review_subject_sha256"]
            or any(contract.get(key) != subject[key] for key in ("target_commit", "target_tree"))):
        raise ValueError("补审 scope 没有覆盖全部内容差异")
    expected_run_id = "supplemental-" + subject["supplemental_review_subject_sha256"]
    if contract.get("run_id") != expected_run_id or read_regular_json_object(directory / "summary.json").get("run_id") != expected_run_id:
        raise ValueError("补审 run 身份不匹配")
    if read_regular_json_object(directory / "summary.json").get("repo") != contract.get("repo"):
        raise ValueError("补审 run 的目标仓库不匹配")
    if verify_live_target:
        review_repository = Path(contract["repo"])
        if (git_text(review_repository, "rev-parse", "HEAD") != subject["target_commit"]
                or git_text(review_repository, "status", "--porcelain")):
            raise ValueError("补审实际仓库没有绑定干净的目标提交")
    plan = read_regular_json_object(directory / "artifacts/reviewers/reviewer-plan.json")
    if plan.get("status") != "completed":
        raise MachineEvidencePending("补审 reviewer plan 未完成；仅接续缺失腿")
    if require_execution_receipt:
        execution_path = directory / "artifacts/supplemental-review-execution.json"
        if not execution_path.exists():
            raise MachineEvidencePending("缺少补审执行时间回执；根据持久启动记录及两腿原始结果恢复")
        execution = read_regular_json_object(execution_path)
        if (execution.get("subject_sha256") != subject["supplemental_review_subject_sha256"]
                or any(execution.get(key) != subject[key] for key in ("target_commit", "target_tree"))):
            raise ValueError("补审执行回执未绑定固定内容")
        if execution.get("status") != "completed":
            raise MachineEvidencePending("补审执行回执尚未对账完成")
        started = datetime.fromisoformat(execution["started_at"])
        completed = datetime.fromisoformat(execution["completed_at"])
        if started.tzinfo is None or completed.tzinfo is None or completed < started:
            raise ValueError("补审实际执行时间无效")
    handoff = directory / "artifacts/handoff.md"
    # 双审本身不生成 handoff 时仍使用同一事实判据；绝不写入伪造完成标记。
    if backlog_sources is None:
        backlog_sources = capture_backlog_sources(directory)
    facts = completed_review_facts(directory, handoff.read_text() if handoff.exists() else "", allow_missing_completion_markers=True,
                                   backlog_artifact_reader=backlog_snapshot_reader(backlog_sources, directory),
                                   original_run_directory=original_directory or directory)
    if facts["applied_attempts"]:
        raise ValueError("补审不得应用新补丁；目标提交改变后须重新绑定补证")
    return {key: value for key, value in facts.items() if key != "applied_attempts"}


def ensure_supplemental_review_inputs(subject: dict, directory: Path, workspace: Path) -> None:
    inputs = directory / "artifacts/inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    binding = {"schema": "rvf-supplemental-review-binding", "schema_version": 1,
               "subject_sha256": subject["supplemental_review_subject_sha256"], "packet_sha256": digest_bytes(review_packet(subject))}
    contract = {"version": 3, "run_id": "supplemental-" + subject["supplemental_review_subject_sha256"], "repo": str(workspace),
                "scope_mode": "custom", "primary_files": subject["supplemental_review_paths"], "fix_allowlist": [],
                "protected_files": [], "background_files": [], "excluded_path_prefixes": [],
                "target_commit": subject["target_commit"], "target_tree": subject["target_tree"],
                "supplemental_review_subject_sha256": subject["supplemental_review_subject_sha256"]}
    for relative, encoded in (("artifacts/inputs/supplemental-review-binding.json", canonical_json_bytes(binding)),
                              ("artifacts/inputs/scope.contract.json", canonical_json_bytes(contract)),
                              ("artifacts/inputs/review-packet.md", review_packet(subject))):
        path = directory / relative
        if path.exists() and path.read_bytes() != encoded:
            raise ValueError(f"持久补审输入变化：{relative}")
        if not path.exists():
            atomic_write(path, encoded)
    # runner 会追加或更新状态；摘要只固定身份，保留原始状态及诊断字节。
    summary_path = directory / "summary.json"
    expected_summary_identity = {"run_id": contract["run_id"], "repo": str(workspace)}
    if summary_path.exists():
        summary = read_regular_json_object(summary_path)
        if any(summary.get(key) != value for key, value in expected_summary_identity.items()):
            raise ValueError("持久补审摘要的 run 或仓库身份变化")
    else:
        atomic_write(summary_path, canonical_json_bytes(expected_summary_identity))


def prepare_supplemental_review_dispatch_qualification(subject: dict, directory: Path, sources: dict, backlog_sources: dict) -> None:
    path = directory / "artifacts/inputs/supplemental-review-dispatch-qualification.json"
    payload = {"schema": "rvf-supplemental-review-dispatch-qualification", "schema_version": 1,
               "subject": subject, "original_run_sources": sources, "original_backlog_sources": backlog_sources}
    encoded = canonical_json_bytes(payload)
    if path.exists() and require_regular_file(path, directory) != encoded:
        raise ValueError("补审派发资格原件已改变")
    if not path.exists():
        atomic_write(path, encoded)


def require_supplemental_review_dispatch_qualification(path: Path, *, repo: str, scope_contract: str,
        review_packet_path: str, run_id: str, run_dir: str) -> None:
    """独立门禁仅用于追加补审；重新核验原始事实，不借用或复活普通开审 token。"""
    directory = Path(run_dir).absolute()
    repository = Path(repo).resolve()
    if (Path(path).absolute() != directory / "artifacts/inputs/supplemental-review-dispatch-qualification.json"
            or Path(scope_contract).absolute() != directory / "artifacts/inputs/scope.contract.json"
            or Path(review_packet_path).absolute() != directory / "artifacts/inputs/review-packet.md"):
        raise ValueError("补审派发路径没有绑定同一 run")
    qualification = json.loads(require_regular_file(path, directory))
    if qualification.get("schema") != "rvf-supplemental-review-dispatch-qualification" or qualification.get("schema_version") != 1:
        raise ValueError("补审派发资格 schema 无效")
    subject = qualification["subject"]
    expected = {key: subject[key] for key in ("task_id", "decision_token", "run_id", "run_directory", "handoff_sha256", "target_commit")}
    with tempfile.TemporaryDirectory(prefix="rvf-supplemental-dispatch-qualification-") as temporary:
        isolated = initialize_object_workspace(repository, Path(temporary) / "repository")
        source_run = Path(temporary) / "original-run"
        restore_run_sources(source_run, qualification["original_run_sources"])
        recomputed = build_supplemental_review_subject(isolated, source_run, expected, qualification["original_backlog_sources"])
        recomputed["historical_worktree"] = read_regular_json_object(source_run / "artifacts/inputs/scope.contract.json")["repo"]
        if recomputed != subject:
            raise ValueError("补审派发的完整内容与原 run 事实不一致")
    contract = read_regular_json_object(Path(scope_contract))
    if (run_id != "supplemental-" + subject["supplemental_review_subject_sha256"] or contract.get("run_id") != run_id
            or Path(contract["repo"]).resolve() != repository
            or contract.get("primary_files") != subject["supplemental_review_paths"]
            or contract.get("supplemental_review_subject_sha256") != subject["supplemental_review_subject_sha256"]
            or any(contract.get(key) != subject[key] for key in ("target_commit", "target_tree"))
            or Path(review_packet_path).read_bytes() != review_packet(subject)
            or git_text(repository, "rev-parse", "HEAD") != subject["target_commit"]
            or git_text(repository, "status", "--porcelain")):
        raise ValueError("补审派发未绑定干净的固定目标及同一 packet")


def load_supplemental_reviewer_plan_with_native_receipts(subject: dict, directory: Path, workspace: Path) -> dict | None:
    """只读对账逐腿原生退出事实，不加载路由、不 probe、不派发命令。"""
    plan_path = directory / "artifacts/reviewers/reviewer-plan.json"
    if not plan_path.exists():
        return None
    plan = read_regular_json_object(plan_path)
    for leg in plan.get("reviewers", []):
        receipt_path = directory / "artifacts/reviewer-command-outputs" / (leg["reviewer_id"] + ".execution-result.json")
        if not receipt_path.exists():
            continue
        receipt = read_regular_json_object(receipt_path)
        if (receipt.get("schema") != "rvf-reviewer-command-execution-result" or receipt.get("schema_version") != 1
                or receipt.get("run_id") != "supplemental-" + subject["supplemental_review_subject_sha256"]
                or receipt.get("repo") != str(workspace) or receipt.get("reviewer_id") != leg["reviewer_id"]
                or receipt.get("packet_sha256") != digest_bytes(review_packet(subject)) or type(receipt.get("returncode")) is not int):
            raise ValueError("重启后的 reviewer 原生执行回执身份不匹配")
        leg["returncode"] = receipt["returncode"]
        leg["returncode_source"] = "durable_reviewer_command_execution_result"
    return plan


def reconcile_completed_supplemental_review_without_new_execution(subject: dict, directory: Path, workspace: Path) -> bool:
    """先核对已有事实；两腿完整时仅恢复登记，不消耗新执行预算。"""
    import dispatch_reviewers
    plan = load_supplemental_reviewer_plan_with_native_receipts(subject, directory, workspace)
    if plan is None:
        return False
    reviewers = plan.get("reviewers", [])
    if len(reviewers) != 2 or not all(dispatch_reviewers._leg_is_effective(leg, directory / "artifacts") for leg in reviewers):
        return False
    ensure_supplemental_review_inputs(subject, directory, workspace)
    if plan.get("status") != "completed":
        plan.update(status="completed", effective_reviewer_count=2,
                    completion_source="durable_reviewer_command_execution_result_reconciliation")
        atomic_write(directory / "artifacts/reviewers/reviewer-plan.json", canonical_json_bytes(plan))
    try:
        validated_supplemental_review(subject, directory)
    except MachineEvidencePending:
        validated_supplemental_review(subject, directory, require_execution_receipt=False)
        execution_path = directory / "artifacts/supplemental-review-execution.json"
        execution = read_regular_json_object(execution_path)
        if (execution.get("subject_sha256") != subject["supplemental_review_subject_sha256"]
                or any(execution.get(key) != subject[key] for key in ("target_commit", "target_tree"))):
            raise ValueError("补审启动记录身份不匹配")
        execution.update(status="completed", completed_at=datetime.now(timezone.utc).isoformat(),
                         completion_time_kind="observed_after_reconciliation")
        atomic_write(execution_path, canonical_json_bytes(execution))
        validated_supplemental_review(subject, directory)
    return True


def recover_double_review(subject: dict, directory: Path, workspace: Path) -> None:
    """复用现有路由与双腿执行器；有结果的健康腿跨进程保留。"""
    import dispatch_reviewers
    ensure_supplemental_review_inputs(subject, directory, workspace)
    if reconcile_completed_supplemental_review_without_new_execution(subject, directory, workspace):
        return
    plan_path = directory / "artifacts/reviewers/reviewer-plan.json"
    plan = load_supplemental_reviewer_plan_with_native_receipts(subject, directory, workspace)
    registry = dispatch_reviewers.load_registry(dispatch_reviewers.DEFAULT_REGISTRY)
    available = dispatch_reviewers.probe_available(registry)
    if plan is not None:
        reviewers = plan.get("reviewers", [])
        indices = [index for index, leg in enumerate(reviewers) if not dispatch_reviewers._leg_is_effective(leg, directory / "artifacts")]
        if not reviewers:
            plan = dispatch_reviewers.route("codex", available, registry, require_external_only=True)
            indices = None
        elif plan.get("status") == "failed":
            plan = {**plan, "status": "planned"}
    else:
        plan = dispatch_reviewers.route("codex", available, registry, require_external_only=True)
        indices = None
    if plan.get("status") == "failed":
        atomic_write(plan_path, canonical_json_bytes(plan))
        raise MachineEvidencePending("补审 reviewer 暂不可用；恢复 harness 后自动重验")
    execution_path = directory / "artifacts/supplemental-review-execution.json"
    previous_execution = read_regular_json_object(execution_path) if execution_path.exists() else {}
    started = previous_execution.get("started_at") or datetime.now(timezone.utc).isoformat()
    atomic_write(plan_path, canonical_json_bytes(plan))
    atomic_write(directory / "artifacts/supplemental-review-execution.json", canonical_json_bytes(
        {"started_at": started, "subject_sha256": subject["supplemental_review_subject_sha256"], "status": "running",
         "target_commit": subject["target_commit"], "target_tree": subject["target_tree"]}))
    plan = dispatch_reviewers.execute_plan(
        plan, repo=str(workspace), review_packet=str(directory / "artifacts/inputs/review-packet.md"),
        session_context=None, scope_contract=str(directory / "artifacts/inputs/scope.contract.json"),
        run_id="supplemental-" + subject["supplemental_review_subject_sha256"], run_dir=str(directory),
        artifacts_dir=directory / "artifacts", registry=registry, main_harness="codex", available=available,
        initial_dispatch_indices=indices,
        reviewer_command_output_directory=directory / "artifacts/reviewer-command-outputs",
        supplemental_review_qualification_path=directory / "artifacts/inputs/supplemental-review-dispatch-qualification.json",
    )
    atomic_write(plan_path, canonical_json_bytes(plan))
    atomic_write(directory / "artifacts/supplemental-review-execution.json", canonical_json_bytes(
        {"started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
         "subject_sha256": subject["supplemental_review_subject_sha256"], "status": plan.get("status"),
         "target_commit": subject["target_commit"], "target_tree": subject["target_tree"]}))
    if plan.get("status") != "completed":
        raise MachineEvidencePending("差异双审未完成；读取 reviewer plan 并在条件恢复后接续")
    validated_supplemental_review(subject, directory)


def durable_publish(path: Path, encoded: bytes) -> None:
    """内容寻址 + 原子替换；中断遗留的缺失 sidecar 可按真实字节重新对账。"""
    if path.exists() and require_regular_file(path, path.parent) != encoded:
        raise ValueError("不可变补充材料发生内容冲突")
    if not path.exists():
        atomic_write(path, encoded)
    sidecar = Path(str(path) + ".sha256")
    expected = (digest_bytes(encoded) + "\n").encode()
    if sidecar.exists() and require_regular_file(sidecar, sidecar.parent) != expected:
        raise ValueError("不可变材料 sidecar 冲突")
    atomic_write(sidecar, expected)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record_recovery_attempt(state_path: Path, fingerprint: str, phase: str) -> dict:
    state = read_regular_json_object(state_path) if state_path.exists() else {"input_fingerprint": fingerprint, "phases": {}}
    if state["input_fingerprint"] != fingerprint:
        raise ValueError("恢复状态输入指纹不匹配")
    current = state["phases"].setdefault(phase, {"attempt_count": 0})
    if current["attempt_count"] >= MAXIMUM_RECOVERY_ATTEMPTS_PER_INPUT_AND_PHASE:
        raise MachineEvidencePending(f"{phase} 已达到同输入三次自动恢复上限；证据变化后重新对账")
    current.update(attempt_count=current["attempt_count"] + 1, status="running", boot_session_id=current_boot_session_id(),
                   started_at=datetime.now(timezone.utc).isoformat(),
                   recovery_condition="同绑定证据恢复后先核对已有结果，再执行缺失步骤；输入变化后重算指纹")
    state.setdefault("attempt_history", []).append({"phase": phase, **current})
    durable_json(state_path, state)
    return state


@contextmanager
def recovery_phase(state_path: Path, fingerprint: str, phase: str):
    state = record_recovery_attempt(state_path, fingerprint, phase)
    current = state["phases"][phase]
    try:
        yield
    except BaseException as exc:
        current.update(status="awaiting_machine_evidence" if isinstance(exc, MachineEvidencePending) else "invalid_evidence",
                       reason=str(exc), settled_at=datetime.now(timezone.utc).isoformat())
        state["attempt_history"][-1].update(current)
        durable_json(state_path, state)
        raise
    else:
        current.update(status="completed", settled_at=datetime.now(timezone.utc).isoformat())
        state["attempt_history"][-1].update(current)
        durable_json(state_path, state)


def read_bound_evidence(path: Path, expected_digest: str) -> dict:
    encoded = require_regular_file(path, path.parent)
    if not HEX_DIGEST.fullmatch(expected_digest) or digest_bytes(encoded) != expected_digest or path.name != expected_digest + ".json":
        raise ValueError("补充证明摘要或内容寻址位置不匹配")
    if require_regular_file(Path(str(path) + ".sha256"), path.parent) != (expected_digest + "\n").encode():
        raise ValueError("补充证明 sidecar 不匹配")
    payload = json.loads(encoded)
    if encoded != canonical_json_bytes(payload) or payload.get("schema") != SCHEMA or payload.get("schema_version") != VERSION:
        raise ValueError("补充证明 schema 或 canonical bytes 无效")
    return payload


def verify_evidence(repository: Path, run_directory: Path, expected: dict, path: Path, expected_digest: str,
                    *, unpublished_payload: dict | None = None) -> dict:
    payload = unpublished_payload if unpublished_payload is not None else read_bound_evidence(path, expected_digest)
    if (payload.get("schema") != SCHEMA or payload.get("schema_version") != VERSION
            or payload.get("does_not_attest_precommit_review") is not True or payload.get("does_not_authorize_followup_execution") is not True):
        raise ValueError("补充证明 schema 或权限边界无效")
    if path.parent != run_directory / ARTIFACT_DIRECTORY or payload.get("binding") != expected:
        raise ValueError("消费者预期绑定与补充证明不匹配")
    if payload.get("repository_git_common_directory") != str(repository_identity(repository)[1]):
        raise ValueError("补充证明属于另一 Git 仓库")
    target_tree = git_text(repository, "rev-parse", expected["target_commit"] + "^{tree}")
    bundle_path = path.parent / (payload["git_bundle_sha256"] + ".bundle")
    bundle = require_regular_file(bundle_path, path.parent)
    if digest_bytes(bundle) != payload["git_bundle_sha256"]:
        raise ValueError("持久 Git 对象包摘要不匹配")
    with tempfile.TemporaryDirectory(prefix="rvf-supplemental-verification-") as temporary:
        root = Path(temporary)
        isolated = root / "repository"
        isolated.mkdir()
        git_text(isolated, "init", "-q", "--object-format=" + git_text(repository, "rev-parse", "--show-object-format"))
        git_text(isolated, "fetch", "--quiet", str(bundle_path), "+refs/rvf/supplemental/*:refs/rvf/supplemental/*")
        source_run = root / "original-run"
        restore_run_sources(source_run, payload["source_snapshots"])
        subject = build_supplemental_review_subject(isolated, source_run, expected, payload["backlog_source_snapshots"])
        subject["historical_worktree"] = read_regular_json_object(source_run / "artifacts/inputs/scope.contract.json")["repo"]
        if subject != payload["subject"] or target_tree != subject["target_tree"]:
            raise ValueError("重放后的内容差异或真实目标 tree 不匹配")
        commands = load_handoff_validation_commands(source_run)
        if commands != payload["validation_commands"]:
            raise ValueError("验证义务与原 run 不匹配")
        validate_validation_snapshots(subject, commands or [], payload["validation_snapshots"], check_current_environment=False,
                                      expected_git_common_directory=repository_identity(repository)[1])
        if subject["supplemental_review_paths"]:
            review_directory = root / "supplemental-review"
            restore_run_sources(review_directory, payload["supplemental_review_snapshots"])
            qualification = read_regular_json_object(review_directory / "artifacts/inputs/supplemental-review-dispatch-qualification.json")
            if (qualification.get("original_run_sources") != payload["source_snapshots"]
                    or qualification.get("original_backlog_sources") != payload["backlog_source_snapshots"]):
                raise ValueError("补审派发资格使用了另一份原 run 材料")
            review_facts = validated_supplemental_review(subject, review_directory,
                backlog_sources=payload["supplemental_review_backlog_snapshots"],
                original_directory=Path(payload["supplemental_review_run_directory"]), verify_live_target=False)
            if review_facts != payload["supplemental_review_facts"]:
                raise ValueError("补审处置事实不匹配")
    return {"status": "acceptable", "historical_completion": {"status": "verified", "target_commit": expected["target_commit"], "target_tree": target_tree},
            "current_execution_eligibility": current_execution_eligibility(repository, expected),
            "evidence_path": str(path), "evidence_sha256": expected_digest}


def expected_binding(arguments: argparse.Namespace, repository: Path, run_directory: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{16}", arguments.decision_token) or not HEX_DIGEST.fullmatch(arguments.handoff_sha256):
        raise ValueError("token 或 handoff SHA256 无效")
    if not GIT_COMMIT.fullmatch(arguments.target_commit):
        raise ValueError("必须指定完整目标 commit OID；不接受分支、缩写或 latest")
    for value in (arguments.task_id, arguments.run_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or value == "latest":
            raise ValueError("task/run 必须是精确身份")
    git_text(repository, "cat-file", "-e", arguments.target_commit + "^{commit}")
    return {"task_id": arguments.task_id, "decision_token": arguments.decision_token, "run_id": arguments.run_id,
            "run_directory": str(run_directory), "handoff_sha256": arguments.handoff_sha256, "target_commit": arguments.target_commit}


def inspect_or_record(arguments: argparse.Namespace, repository: Path, run_directory: Path, expected: dict) -> dict:
    evidence_root = run_directory / ARTIFACT_DIRECTORY
    # 历史接受事实只依赖已绑定的不可变材料，不因原缓存后来被清理而失效。
    for path in sorted(evidence_root.glob("*.json")):
        encoded = require_regular_file(path, evidence_root)
        if digest_bytes(encoded) != path.stem:
            raise ValueError("补充证明内容寻址摘要不匹配")
        prior = json.loads(encoded)
        if prior.get("binding") != expected:
            continue
        if not Path(str(path) + ".sha256").exists() and arguments.supplemental_action == "record":
            verify_evidence(repository, run_directory, expected, path, path.stem, unpublished_payload=prior)
            durable_publish(path, encoded)
        return {**verify_evidence(repository, run_directory, expected, path, path.stem), "reused": True,
                "subject": prior["subject"], "input_fingerprint": prior["input_fingerprint"], "missing_obligations": []}
    sources = capture_run_sources(run_directory)
    arguments.supplemental_inspection_context = {"binding": expected,
        "input_fingerprint": payload_digest({"binding": expected, "source_snapshots": sources}),
        "recovery_condition": "绑定原始材料或候选 Git 对象恢复后，使用相同精确绑定重新对账"}
    backlog_sources = capture_backlog_sources(run_directory)
    commands = load_handoff_validation_commands(run_directory) or []
    common_directory = repository_identity(repository)[1]
    old_validations = collect_validation_snapshots(run_directory, common_directory)
    with tempfile.TemporaryDirectory(prefix="rvf-supplemental-inspection-") as temporary:
        isolated = initialize_object_workspace(repository, Path(temporary) / "repository")
        source_run = Path(temporary) / "original-run"
        restore_run_sources(source_run, sources)
        subject = build_supplemental_review_subject(isolated, source_run, expected, backlog_sources)
        subject["historical_worktree"] = read_regular_json_object(source_run / "artifacts/inputs/scope.contract.json")["repo"]
        fingerprint = payload_digest({"subject": subject, "sources": sources, "commands": commands, "validation_snapshots": old_validations,
                                      "execution_environment": [environment_fingerprint(entry["argv"], entry["environment_keys"])[0] for entry in commands]})
        operation_directory = evidence_root / "recovery-operations" / fingerprint
        review_directory = Path(arguments.supplemental_review_run_dir).absolute() if arguments.supplemental_review_run_dir else operation_directory / "supplemental-review-run"
        obligations = []
        accepted_validations = []
        try:
            accepted_validations = validate_validation_snapshots(subject, commands, old_validations, check_current_environment=True,
                                                                 expected_git_common_directory=common_directory)
        except MachineEvidencePending as exc:
            obligations.append({"phase": "validation", "reason": str(exc), "recovery_condition": "匹配目标的真实通过记录或隔离重验完成"})
        if subject["supplemental_review_paths"]:
            try:
                validated_supplemental_review(subject, review_directory)
            except (FileNotFoundError, MachineEvidencePending):
                obligations.append({"phase": "double_review", "reason": "固定内容差异需要两路独立补审及完整处置", "recovery_condition": "两腿完成且无 request/未完成处置"})
        if arguments.validation_run_dir:
            supplied_run = Path(arguments.validation_run_dir).absolute()
            supplied = collect_validation_snapshots(supplied_run, common_directory)
            accepted_validations = validate_validation_snapshots(subject, commands, supplied, check_current_environment=True,
                                                                 expected_git_common_directory=common_directory)
            obligations = [item for item in obligations if item["phase"] != "validation"]
        result = {"status": "awaiting_machine_evidence" if obligations else "acceptable", "binding": expected,
                  "input_fingerprint": fingerprint, "subject": subject, "missing_obligations": obligations,
                  "current_execution_eligibility": current_execution_eligibility(repository, expected),
                  "recovery": {"operation_directory": str(operation_directory), "maximum_attempts_per_phase": MAXIMUM_RECOVERY_ATTEMPTS_PER_INPUT_AND_PHASE,
                               "action": "record_supplemental_completion_with_machine_recovery",
                               "rvf_argv": ["land", "supplemental-completion", "record", "--repo", str(repository), "--run-dir", str(run_directory),
                                            *[value for key in ("task_id", "decision_token", "run_id", "handoff_sha256", "target_commit")
                                              for value in ("--" + key.replace("_", "-"), expected[key])], "--recover-machine-evidence"],
                               "triggers": [{"kind": "bound_source_or_execution_environment_changed", "input_fingerprint": fingerprint},
                                            {"kind": "machine_artifact_changed", "paths": [str(review_directory / "artifacts/reviewers/reviewer-plan.json"),
                                                 str(operation_directory / "isolated-validation-run/artifacts/validation-records.json")]},
                                            {"kind": "boot_session_changed", "action": "reconcile_before_retry_without_resetting_budget"}],
                               "recovery_condition": "先核对已有工件，仅执行缺失步骤；输入变化后重算指纹",
                               "progress_state_path": str(operation_directory / "machine-evidence-recovery-state.json")}}
        arguments.supplemental_inspection_context = result
        if arguments.supplemental_action == "inspect":
            return result
        if obligations:
            durable_json(operation_directory / "machine-evidence-waiting-obligations.json", result)
        if obligations and not arguments.recover_machine_evidence:
            return result
        operation_directory.mkdir(parents=True, exist_ok=True)
        state_path = operation_directory / "machine-evidence-recovery-state.json"
        workspace = operation_directory / "target-validation-and-review-workspace"
        if not (workspace / ".git").exists():
            initialize_object_workspace(repository, workspace)
            git_text(workspace, "checkout", "--quiet", "--detach", expected["target_commit"])
        if git_text(workspace, "rev-parse", "HEAD") != expected["target_commit"] or git_text(workspace, "status", "--porcelain"):
            raise ValueError("隔离目标工作树已变化，不能复用补审或验证")
        if tree_content_manifest(workspace, "HEAD") != subject["candidate_content_manifest_sha256"]:
            raise ValueError("隔离目标内容摘要不匹配")
        review_facts = None
        if subject["supplemental_review_paths"]:
            try:
                review_facts = validated_supplemental_review(subject, review_directory)
            except (FileNotFoundError, MachineEvidencePending):
                prepare_supplemental_review_dispatch_qualification(subject, review_directory, sources, backlog_sources)
                if reconcile_completed_supplemental_review_without_new_execution(subject, review_directory, workspace):
                    if state_path.exists():
                        state = read_regular_json_object(state_path)
                        if state.get("input_fingerprint") != fingerprint:
                            raise ValueError("恢复状态输入指纹不匹配")
                        phase_state = state["phases"].get("double_review")
                        if phase_state is not None:
                            phase_state.update(status="completed", reason="已有原生双腿完成事实已纯对账登记",
                                               reconciled_at=datetime.now(timezone.utc).isoformat(), completion_source="existing_native_receipts_without_new_execution")
                            state.setdefault("reconciliation_history", []).append({"phase": "double_review", **phase_state})
                            durable_json(state_path, state)
                else:
                    with recovery_phase(state_path, fingerprint, "double_review"):
                        recover_double_review(subject, review_directory, workspace)
                review_facts = validated_supplemental_review(subject, review_directory)
        if not accepted_validations:
            if not commands:
                raise MachineEvidencePending("缺少原验证 argv，恢复结构化命令工件后自动接续")
            validation_run = operation_directory / "isolated-validation-run"
            workspace_common_directory = repository_identity(workspace)[1]
            recovered = collect_validation_snapshots(validation_run, workspace_common_directory)
            try:
                accepted_validations = validate_validation_snapshots(subject, commands, recovered, check_current_environment=True,
                                                                     expected_git_common_directory=common_directory)
            except MachineEvidencePending:
                if any(command["requires_base_worktree_context"] for command in commands):
                    raise MachineEvidencePending("隔离目标无法冒充原 base worktree 上下文；恢复相同内容与原执行上下文的记录后接续")
                with recovery_phase(state_path, fingerprint, "validation"):
                    prepare_isolated_node_dependencies(workspace, commands, operation_directory)
                    for command in commands:
                        argv = [sys.executable, str(Path(__file__).with_name("validation_result_cache.py")), "run", "--repo", str(workspace),
                                "--name", command["name"], "--working-directory", command["working_directory_relative_to_worktree"],
                                "--validation-context-kind", command["validation_context_kind"], "--rvf-run-dir", str(validation_run)]
                        for key in command["environment_keys"]:
                            argv += ["--environment-key", key]
                        completed = tracked_subprocess_run([*argv, "--", *command["argv"]], capture_output=True, env=clean_git_environment())
                        output_path = operation_directory / "isolated-validation-command-outputs" / (payload_digest(command) + ".json")
                        durable_json(output_path, {"command_sha256": payload_digest(command), "exit_code": completed.returncode,
                                                  "stdout_base64": base64.b64encode(completed.stdout).decode(),
                                                  "stderr_base64": base64.b64encode(completed.stderr).decode()})
                        if completed.returncode:
                            produced = collect_validation_snapshots(validation_run, workspace_common_directory)
                            if not any(item["record"].get("name") == command["name"] for item in produced):
                                raise MachineEvidencePending(f"验证运行时未产生真实执行记录；恢复命令依赖后接续：{output_path}")
                            raise ValueError(f"隔离目标验证失败：{command['name']}（exit {completed.returncode}），原始输出：{output_path}")
                    recovered = collect_validation_snapshots(validation_run, workspace_common_directory)
                    accepted_validations = validate_validation_snapshots(subject, commands, recovered, check_current_environment=True,
                                                                     expected_git_common_directory=common_directory)
        if git_text(workspace, "status", "--porcelain") or git_text(workspace, "rev-parse", "HEAD") != expected["target_commit"]:
            raise ValueError("reviewer 或验证改变了隔离目标，拒绝验收")
        if capture_run_sources(run_directory) != sources or capture_backlog_sources(run_directory) != backlog_sources:
            raise ValueError("补证期间原 run 输入变化，重新 inspect 后接续")
        commits = sorted({*subject["retained_commits"], expected["target_commit"]})
        for index, commit in enumerate(commits):
            git_text(isolated, "update-ref", f"refs/rvf/supplemental/{index}", commit)
        bundle_path = Path(temporary) / "retained-git-objects.bundle"
        git_text(isolated, "bundle", "create", str(bundle_path), "--all")
        bundle = bundle_path.read_bytes()
        bundle_digest = digest_bytes(bundle)
        durable_publish(evidence_root / (bundle_digest + ".bundle"), bundle)
        supplemental_sources = capture_supplemental_review_sources(review_directory) if review_facts is not None else {}
        payload = {"schema": SCHEMA, "schema_version": VERSION, "binding": expected,
                   "repository_git_common_directory": str(common_directory), "input_fingerprint": fingerprint,
                   "subject": subject, "source_snapshots": sources, "backlog_source_snapshots": backlog_sources, "validation_commands": commands,
                   "validation_snapshots": accepted_validations, "supplemental_review_snapshots": supplemental_sources,
                   "supplemental_review_run_directory": str(review_directory),
                   "supplemental_review_backlog_snapshots": capture_backlog_sources(review_directory) if review_facts is not None else {},
                   "supplemental_review_facts": review_facts, "git_bundle_sha256": bundle_digest,
                   "supplemental_review_recorded_at": datetime.now(timezone.utc).isoformat(),
                   "does_not_attest_precommit_review": True, "does_not_authorize_followup_execution": True}
        encoded = canonical_json_bytes(payload)
        digest = digest_bytes(encoded)
        path = evidence_root / (digest + ".json")
        # 在发布前独立重放；发布后即使回执中断，下次也只复用此内容寻址结果。
        verified = verify_evidence(repository, run_directory, expected, path, digest, unpublished_payload=payload)
        durable_publish(path, encoded)
        durable_json(operation_directory / "completed-evidence-reference.json", {"evidence_path": str(path), "evidence_sha256": digest})
        return {**verified, "reused": False}


def capture_supplemental_review_sources(directory: Path) -> dict[str, str]:
    paths = ["summary.json", "artifacts/inputs/scope.contract.json", "artifacts/inputs/supplemental-review-binding.json",
             "artifacts/inputs/supplemental-review-dispatch-qualification.json",
             "artifacts/inputs/review-packet.md", "artifacts/reviewers/reviewer-plan.json"]
    paths += [str(path.relative_to(directory)) for pattern in ("artifacts/reviewers/*/review-result.json", "artifacts/merge/**/*.json", "artifacts/merge/**/*.sha256",
              "artifacts/reviewer-command-outputs/*.json", "artifacts/reviewer-command-outputs/execution-results/*.json",
              "artifacts/fix-issues/*.json", "artifacts/fix-attempts/*/*.json", "artifacts/supplemental-review-execution.json", "artifacts/handoff.md")
              for path in directory.glob(pattern)]
    return {relative: base64.b64encode(require_regular_file(directory / relative, directory)).decode() for relative in sorted(set(paths))}


def add_argument_parser(subparsers) -> None:
    parser = subparsers.add_parser("supplemental-completion", help="历史 Land 的只读诊断、补证与独立验收。")
    actions = parser.add_subparsers(dest="supplemental_action", required=True)
    for action in ("inspect", "record", "verify"):
        operation = actions.add_parser(action)
        for flag in ("repo", "task-id", "decision-token", "run-id", "run-dir", "handoff-sha256", "target-commit"):
            operation.add_argument("--" + flag, required=True)
        if action in {"inspect", "record"}:
            operation.add_argument("--supplemental-review-run-dir", help="消费已绑定同一固定差异的补审 run。")
            operation.add_argument("--validation-run-dir", help="消费同仓库、目标内容与执行上下文匹配的真实验证。")
        if action == "record":
            operation.add_argument("--recover-machine-evidence", action="store_true", help="在既有授权内自动补审并隔离验证固定目标。")
        if action == "verify":
            operation.add_argument("--evidence", required=True)
            operation.add_argument("--evidence-sha256", required=True)


def assert_prior_supplemental_process_groups_stopped(repository: Path, run_directory: Path) -> None:
    for path in (control_directory(repository) / "normal-mutations").glob("*.json"):
        reservation = read_regular_json_object(path)
        if (reservation.get("operation") == "supplemental-land-completion-record"
                and reservation.get("original_run_directory") == str(run_directory.resolve())):
            try:
                assert_child_process_groups_stopped(reservation)
            except BackupRouteOwned as exc:
                raise MachineEvidencePending(f"旧补证子进程尚未退出，停止条件满足后自动对账，不能重复派发：{path}：{exc}") from exc


def command(arguments: argparse.Namespace) -> int:
    try:
        repository, _common = repository_identity(Path(arguments.repo).expanduser().resolve())
        run_directory = Path(arguments.run_dir).expanduser().absolute()
        if run_directory.is_symlink():
            raise ValueError("run 目录不可是符号链接")
        expected = expected_binding(arguments, repository, run_directory)
        if arguments.supplemental_action == "verify":
            result = verify_evidence(repository, run_directory, expected, Path(arguments.evidence).absolute(), arguments.evidence_sha256)
        elif arguments.supplemental_action == "record":
            with normal_worktree_mutation(repository, "supplemental-land-completion-record", run_directory):
                lock_path = run_directory / "artifacts/.supplemental-land-completion.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        assert_prior_supplemental_process_groups_stopped(repository, run_directory)
                        result = inspect_or_record(arguments, repository, run_directory, expected)
                    except (MachineEvidencePending, FileNotFoundError) as exc:
                        result = {"binding": expected, **getattr(arguments, "supplemental_inspection_context", {}),
                                  "status": "awaiting_machine_evidence", "reason": str(exc)}
                    except (ValueError, TypeError, KeyError) as exc:
                        result = {"binding": expected, **getattr(arguments, "supplemental_inspection_context", {}),
                                  "status": "invalid_evidence", "reason": str(exc)}
                    settled = {"binding": expected, "result": result}
                    durable_json(run_directory / ARTIFACT_DIRECTORY / "record-command-results" / (payload_digest(settled) + ".json"), settled)
        else:
            result = inspect_or_record(arguments, repository, run_directory, expected)
    except BackupRouteOwned as exc:
        result = {"status": "execution_prohibited", "reason": str(exc)}
    except (MachineEvidencePending, FileNotFoundError) as exc:
        result = {**getattr(arguments, "supplemental_inspection_context", {}), "status": "awaiting_machine_evidence", "reason": str(exc),
                  "recovery_condition": "绑定的原始机器材料或缺失补审/验证证据恢复后，用相同精确参数重新对账"}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"status": "invalid_evidence", "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"acceptable": 0, "awaiting_machine_evidence": 3, "invalid_evidence": 4, "execution_prohibited": 5}[result["status"]]
