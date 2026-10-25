#!/usr/bin/env python3
"""Content-addressed RVF validation execution with durable, auditable evidence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401
from core.review_run_qualification_mutation_lock import guard_review_run_qualification_mutation, run_directory_for_qualification_artifact  # noqa: E402
from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths  # noqa: E402


SCHEMA_VERSION = 2
CONTENT_MANIFEST_ALGORITHM = "git-ls-files-content-manifest-v1"
SECRET_LIKE_ENVIRONMENT_KEY = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL|AUTH|COOKIE)",
    re.IGNORECASE,
)
PACKAGE_MANAGER_EXECUTABLES = {"bun", "npm", "npx", "pnpm", "yarn"}
VALIDATION_CONTEXT_KINDS = {"task_worktree", "base_worktree"}
# run 目录只认显式 ``--rvf-run-dir``，不回退任何环境变量：reviewer 子进程被显式注入 RVF_RUN_DIR、Codex
# task shell 也会 export 它，回退会把外来测试记录写进活 run 的验证索引与 argv 工件（land 随后重放它们）。
# 更高层的「当前 run」推断由 rvf CLI 入口负责（``rvf validate`` 显式补参数）。
# land 重跑验证的结构化事实源：与 validation-records.json 同目录、同锁写入。land 按它的 argv 经
# 本缓存重放，不再从 handoff markdown 里解析命令字符串。argv 与 handoff 验证段是同一份内容，
# 环境变量只记键名不记值（值进环境指纹的哈希）。
HANDOFF_VALIDATION_COMMANDS_ARTIFACT_NAME = "handoff-validation-commands.json"
HANDOFF_VALIDATION_COMMANDS_SCHEMA_NAME = "rvf-handoff-validation-commands"
HANDOFF_VALIDATION_COMMANDS_SCHEMA_VERSION = 1
# 失败输出尾部落在 run 自己的 artifacts 下（与 handoff 同级、同保密级别），不进全局缓存：
# 缓存 record 按设计只存 hash 与脱敏元数据。缓存命中一条「已失败」记录时从这里取回尾部。
VALIDATION_FAILURE_OUTPUT_TAILS_DIRECTORY_NAME = "validation-failure-output-tails"
# EXPERIMENT_NODE: 失败时回显 stdout/stderr 各多少行尾部——够 agent 定位失败原因、又不把大段日志灌进上下文
VALIDATION_FAILURE_OUTPUT_TAIL_LINE_COUNT = 60


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_git_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name, None)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_KEY_1": "core.excludesFile",
            "GIT_CONFIG_VALUE_1": os.devnull,
        }
    )
    return environment


def git(repo: Path, *arguments: str, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.excludesFile=/dev/null", *arguments],
        cwd=repo,
        env=clean_git_environment(),
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if isinstance(completed.stderr, str) else completed.stderr.decode(errors="replace")
        raise RuntimeError(stderr.strip() or f"git {arguments[0]} failed")
    return completed.stdout


def repository_identity(repo: Path) -> tuple[Path, Path]:
    root = Path(str(git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
    common_dir = Path(
        str(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip()
    ).resolve()
    return root, common_dir


def tracked_gitlink_object_ids(root: Path) -> dict[bytes, bytes]:
    raw_index_entries = git(root, "ls-files", "--stage", "-z", "--cached", text=False)
    if not isinstance(raw_index_entries, bytes):
        raise RuntimeError("git ls-files --stage returned text unexpectedly")
    gitlink_object_ids: dict[bytes, bytes] = {}
    for encoded_index_entry in raw_index_entries.split(b"\0"):
        if not encoded_index_entry:
            continue
        try:
            encoded_identity, path_bytes = encoded_index_entry.split(b"\t", 1)
            encoded_mode, encoded_object_id, encoded_stage = encoded_identity.split(b" ", 2)
        except ValueError as exc:
            raise RuntimeError("git ls-files --stage returned an invalid index entry") from exc
        if encoded_stage == b"0" and encoded_mode == b"160000":
            try:
                gitlink_object_ids[path_bytes] = bytes.fromhex(encoded_object_id.decode("ascii"))
            except (UnicodeError, ValueError) as exc:
                raise RuntimeError("git ls-files returned an invalid gitlink object id") from exc
    return gitlink_object_ids


def candidate_content_manifest_sha256(repo: Path) -> str:
    root, _common_dir = repository_identity(repo)
    raw = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", text=False)
    if not isinstance(raw, bytes):
        raise RuntimeError("git ls-files returned text unexpectedly")
    gitlink_object_ids = tracked_gitlink_object_ids(root)
    digest = hashlib.sha256()
    for path_bytes in sorted(path for path in raw.split(b"\0") if path):
        gitlink_object_id = gitlink_object_ids.get(path_bytes)
        if gitlink_object_id is not None:
            digest.update(b"P")
            digest.update(struct.pack(">Q", len(path_bytes)))
            digest.update(path_bytes)
            digest.update(b"G")
            digest.update(struct.pack(">Q", len(gitlink_object_id)))
            digest.update(gitlink_object_id)
            continue
        filesystem_path = root / os.fsdecode(path_bytes)
        try:
            metadata = filesystem_path.lstat()
        except FileNotFoundError:
            continue
        digest.update(b"P")
        digest.update(struct.pack(">Q", len(path_bytes)))
        digest.update(path_bytes)
        if stat.S_ISREG(metadata.st_mode):
            content = filesystem_path.read_bytes()
            digest.update(b"F")
            digest.update(b"X" if metadata.st_mode & 0o111 else b"-")
            digest.update(struct.pack(">Q", len(content)))
            digest.update(content)
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.fsencode(os.readlink(filesystem_path))
            digest.update(b"L")
            digest.update(struct.pack(">Q", len(target)))
            digest.update(target)
        else:
            raise RuntimeError(f"unsupported filesystem entry in validation manifest: {os.fsdecode(path_bytes)!r}")
    return digest.hexdigest()


def executable_version(command: list[str], environment: Mapping[str, str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else None


def environment_fingerprint(
    command: list[str],
    declared_environment_keys: list[str],
) -> tuple[str, dict[str, Any]]:
    for name in declared_environment_keys:
        if SECRET_LIKE_ENVIRONMENT_KEY.search(name):
            raise ValueError(f"secret-like environment key is not allowed in validation evidence: {name}")
    environment = clean_git_environment()
    executable_name = Path(command[0]).name if command else ""
    package_manager_version = (
        executable_version([command[0], "--version"], environment)
        if executable_name in PACKAGE_MANAGER_EXECUTABLES
        else None
    )
    private_payload = {
        "declared_environment": {name: os.environ.get(name) for name in sorted(set(declared_environment_keys))},
        "node_version": executable_version(["node", "--version"], environment),
        "package_manager": executable_name if package_manager_version else None,
        "package_manager_version": package_manager_version,
        "operating_system": platform.system(),
        "operating_system_release": platform.release(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
    }
    public_payload = {
        key: value for key, value in private_payload.items() if key != "declared_environment"
    }
    public_payload["declared_environment_keys"] = sorted(set(declared_environment_keys))
    return sha256_hex(canonical_json_bytes(private_payload)), public_payload


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    finally:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(arguments["path"]))
def write_json_with_digest(path: Path, payload: Mapping[str, Any]) -> str:
    encoded = canonical_json_bytes(payload)
    digest = sha256_hex(encoded)
    atomic_write(path, encoded)
    atomic_write(path.with_suffix(path.suffix + ".sha256"), f"{digest}\n".encode("ascii"))
    return digest


def validated_record(path: Path, expected_cache_key: str) -> tuple[dict[str, Any], str]:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if path.is_symlink() or sidecar.is_symlink() or not path.is_file() or not sidecar.is_file():
        raise ValueError("corrupt validation cache record: record and sidecar must be regular files")
    try:
        encoded = path.read_bytes()
        expected_digest = sidecar.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt validation cache record: {exc}") from exc
    actual_digest = sha256_hex(encoded)
    if expected_digest != f"{actual_digest}\n".encode("ascii"):
        raise ValueError("corrupt validation cache record: digest mismatch")
    required_fields = {
        "name": str,
        "status": str,
        "exit_code": int,
        "candidate_content_manifest_sha256": str,
        "command_sha256": str,
        "environment_fingerprint_sha256": str,
        "requires_base_worktree_context": bool,
        "validation_context_kind": str,
    }
    if (
        not isinstance(payload, dict)
        or encoded != canonical_json_bytes(payload)
        or payload.get("schema") != "rvf-validation-result-cache-record"
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("cache_key") != expected_cache_key
        or any(not isinstance(payload.get(name), expected_type) for name, expected_type in required_fields.items())
        or payload.get("status") not in {"passed", "failed"}
        or payload.get("validation_context_kind") not in VALIDATION_CONTEXT_KINDS
        or (
            payload.get("requires_base_worktree_context")
            and not isinstance(payload.get("validation_context_worktree_realpath_sha256"), str)
        )
        or (
            not payload.get("requires_base_worktree_context")
            and payload.get("validation_context_worktree_realpath_sha256") is not None
        )
    ):
        raise ValueError("corrupt validation cache record: identity mismatch")
    return payload, actual_digest


def validation_record_reference(record: Mapping[str, Any], record_digest: str) -> dict[str, Any]:
    return {
        "name": record["name"],
        "status": record["status"],
        "exit_code": record["exit_code"],
        "command_sha256": record["command_sha256"],
        "candidate_content_manifest_sha256": record["candidate_content_manifest_sha256"],
        "environment_fingerprint_sha256": record["environment_fingerprint_sha256"],
        "cache_record_path": record["cache_record_path"],
        "cache_record_sha256": record_digest,
        "requires_base_worktree_context": record["requires_base_worktree_context"],
        "validation_context_kind": record["validation_context_kind"],
        "validation_context_worktree_realpath_sha256": record[
            "validation_context_worktree_realpath_sha256"
        ],
    }


def validated_validation_record_reference(
    reference: Mapping[str, Any],
    *,
    expected_git_common_directory: Path | None = None,
) -> tuple[dict[str, Any], str]:
    record_path = Path(str(reference.get("cache_record_path", "")))
    if (
        not record_path.is_absolute()
        or record_path.name != "record.json"
        or record_path.parent.parent.name != "entries"
        or not re.fullmatch(r"[0-9a-f]{64}", record_path.parent.parent.parent.name)
        or record_path.parent.parent.parent.parent.name != "validation-result-cache-v2"
    ):
        raise ValueError("validation cache record path does not use the canonical cache layout")
    expected_cache_key = record_path.parent.name
    if not re.fullmatch(r"[0-9a-f]{64}", expected_cache_key):
        raise ValueError("validation cache record path does not encode its cache identity")
    record, record_digest = validated_record(record_path, expected_cache_key)
    if expected_git_common_directory is not None:
        expected_common_identity = sha256_hex(
            os.fsencode(str(expected_git_common_directory.resolve()))
        )
        if record_path.parent.parent.parent.name != expected_common_identity:
            raise ValueError(
                "validation cache record path targets a different Git common directory"
            )
    if dict(reference) != validation_record_reference(record, record_digest):
        raise ValueError(f"validation record reference mismatch: {record_path}")
    return record, record_digest


@guard_review_run_qualification_mutation("run_dir")
def append_run_validation_record(
    run_dir: Path | None,
    record: Mapping[str, Any],
    record_digest: str,
    handoff_validation_command_entry: Mapping[str, Any] | None = None,
) -> None:
    if run_dir is None:
        return
    artifacts = run_dir.expanduser().resolve() / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    lock_path = artifacts / ".validation-records.lock"
    index_path = artifacts / "validation-records.json"
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            index_sidecar = index_path.with_suffix(index_path.suffix + ".sha256")
            if not index_path.exists() and not index_sidecar.exists():
                index = {"schema_version": 1, "validation_records": []}
            else:
                if (
                    index_path.is_symlink()
                    or index_sidecar.is_symlink()
                    or not index_path.is_file()
                    or not index_sidecar.is_file()
                ):
                    raise ValueError("corrupt run validation record index")
                encoded_index = index_path.read_bytes()
                index = json.loads(encoded_index.decode("utf-8"))
                if (
                    not isinstance(index, dict)
                    or encoded_index != canonical_json_bytes(index)
                    or index_sidecar.read_bytes()
                    != f"{sha256_hex(encoded_index)}\n".encode("ascii")
                ):
                    raise ValueError("corrupt run validation record index")
            records = index.get("validation_records") if isinstance(index, dict) else None
            if not isinstance(records, list):
                raise ValueError("corrupt run validation record index")
            reference = validation_record_reference(record, record_digest)
            records = [item for item in records if not (isinstance(item, dict) and item.get("name") == record["name"])]
            records.append(reference)
            write_json_with_digest(index_path, {"schema_version": 1, "validation_records": records})
            if handoff_validation_command_entry is not None:
                _record_handoff_validation_command_locked(run_dir, handoff_validation_command_entry)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _read_canonical_json_artifact_with_digest(path: Path, label: str) -> dict[str, Any] | None:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not path.exists() and not sidecar.exists():
        return None
    if path.is_symlink() or sidecar.is_symlink() or not path.is_file() or not sidecar.is_file():
        raise ValueError(f"corrupt {label}: artifact and sidecar must be regular files")
    try:
        encoded = path.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt {label}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or encoded != canonical_json_bytes(payload)
        or sidecar.read_bytes() != f"{sha256_hex(encoded)}\n".encode("ascii")
    ):
        raise ValueError(f"corrupt {label}: digest mismatch")
    return payload


def handoff_validation_commands_artifact_path(run_dir: Path) -> Path:
    return run_dir.expanduser().resolve() / "artifacts" / HANDOFF_VALIDATION_COMMANDS_ARTIFACT_NAME


def load_handoff_validation_commands(run_dir: Path) -> list[dict[str, Any]] | None:
    """读 land 重放用的结构化 argv 工件；工件不存在返回 None，损坏则 raise ValueError。"""
    payload = _read_canonical_json_artifact_with_digest(
        handoff_validation_commands_artifact_path(run_dir), "handoff validation commands artifact"
    )
    if payload is None:
        return None
    entries = payload.get("validation_commands")
    if (
        payload.get("schema") != HANDOFF_VALIDATION_COMMANDS_SCHEMA_NAME
        or payload.get("schema_version") != HANDOFF_VALIDATION_COMMANDS_SCHEMA_VERSION
        or not isinstance(entries, list)
    ):
        raise ValueError("corrupt handoff validation commands artifact: schema mismatch")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("argv"), list)
            or not entry["argv"]
            or not all(isinstance(argument, str) for argument in entry["argv"])
            or not isinstance(entry.get("working_directory_relative_to_worktree"), str)
            or not isinstance(entry.get("environment_keys"), list)
            or entry.get("validation_context_kind") not in VALIDATION_CONTEXT_KINDS
            or not isinstance(entry.get("requires_base_worktree_context"), bool)
        ):
            raise ValueError("corrupt handoff validation commands artifact: invalid entry")
    return entries


def _record_handoff_validation_command_locked(run_dir: Path, command_entry: Mapping[str, Any]) -> None:
    """按 name upsert 一条结构化 argv；同名替换原位，保持 handoff-validation-NNN 的首次出现顺序。

    调用方必须已持有 run 的 ``.validation-records.lock``。
    """
    path = handoff_validation_commands_artifact_path(run_dir)
    existing_entries = load_handoff_validation_commands(run_dir) or []
    replaced = False
    updated_entries: list[dict[str, Any]] = []
    for entry in existing_entries:
        if entry.get("name") == command_entry["name"]:
            updated_entries.append(dict(command_entry))
            replaced = True
        else:
            updated_entries.append(entry)
    if not replaced:
        updated_entries.append(dict(command_entry))
    write_json_with_digest(
        path,
        {
            "schema": HANDOFF_VALIDATION_COMMANDS_SCHEMA_NAME,
            "schema_version": HANDOFF_VALIDATION_COMMANDS_SCHEMA_VERSION,
            "validation_commands": updated_entries,
        },
    )


def summarize_run_validation_index(
    run_dir: Path,
    *,
    current_candidate_content_manifest_sha256: str,
    expected_git_common_directory: Path,
) -> dict[str, Any]:
    """run 自己的 validation index 相对**当前**内容的摘要。

    ``all_records_passed_for_current_content`` 为真 ⇔ index 非空、每条引用都能校验回全局缓存
    record、全部 passed 且内容摘要等于当前摘要——与 candidate evidence 的验证门槛逐条同口径
    （``write_reviewed_task_candidate_evidence.validated_validation_records``），因此它为真时
    land 可以零执行复用。
    """
    index_path = run_dir.expanduser().resolve() / "artifacts" / "validation-records.json"
    try:
        index = _read_canonical_json_artifact_with_digest(index_path, "run validation record index")
    except ValueError as exc:
        return {
            "index_status": "corrupt",
            "error": str(exc),
            "record_count": 0,
            "records": [],
            "all_records_passed_for_current_content": False,
        }
    if index is None:
        return {
            "index_status": "missing",
            "record_count": 0,
            "records": [],
            "all_records_passed_for_current_content": False,
        }
    references = index.get("validation_records")
    if not isinstance(references, list):
        references = []
    records: list[dict[str, Any]] = []
    all_passed_for_current_content = bool(references)
    for reference in references:
        name = reference.get("name") if isinstance(reference, dict) else None
        try:
            if not isinstance(reference, dict):
                raise ValueError("validation record reference must be an object")
            validated_validation_record_reference(
                reference, expected_git_common_directory=expected_git_common_directory
            )
        except (OSError, ValueError) as exc:
            records.append({"name": name, "status": "corrupt", "error": str(exc)})
            all_passed_for_current_content = False
            continue
        matches_current_content = (
            reference["candidate_content_manifest_sha256"] == current_candidate_content_manifest_sha256
        )
        passed = reference["status"] == "passed" and reference["exit_code"] == 0
        records.append(
            {
                "name": name,
                "status": reference["status"],
                "exit_code": reference["exit_code"],
                "matches_current_candidate_content": matches_current_content,
            }
        )
        all_passed_for_current_content = all_passed_for_current_content and passed and matches_current_content
    return {
        "index_status": "present",
        "record_count": len(records),
        "records": records,
        "all_records_passed_for_current_content": all_passed_for_current_content,
    }


def _output_tail_text(output: bytes) -> str:
    lines = output.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-VALIDATION_FAILURE_OUTPUT_TAIL_LINE_COUNT:])


def _validation_failure_output_tail_path(run_dir: Path, name: str) -> Path:
    safe_name = name if re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name) else sha256_hex(name.encode("utf-8"))
    return (
        run_dir.expanduser().resolve()
        / "artifacts"
        / VALIDATION_FAILURE_OUTPUT_TAILS_DIRECTORY_NAME
        / f"{safe_name}.txt"
    )


def _render_validation_failure_output_tail(name: str, exit_code: int, stdout_tail: str, stderr_tail: str) -> str:
    return (
        f"=== validation {name} failed (exit {exit_code}) — stdout 尾部 "
        f"(最多 {VALIDATION_FAILURE_OUTPUT_TAIL_LINE_COUNT} 行) ===\n{stdout_tail}\n"
        f"=== validation {name} — stderr 尾部 "
        f"(最多 {VALIDATION_FAILURE_OUTPUT_TAIL_LINE_COUNT} 行) ===\n{stderr_tail}\n"
    )


def _persist_validation_failure_output_tail(
    run_dir: Path | None, name: str, cache_key: str, rendered_tail: str
) -> None:
    if run_dir is None:
        return
    atomic_write(
        _validation_failure_output_tail_path(run_dir, name),
        f"cache_key: {cache_key}\n{rendered_tail}".encode("utf-8"),
    )


def _persisted_validation_failure_output_tail(run_dir: Path | None, name: str, cache_key: str) -> str | None:
    if run_dir is None:
        return None
    path = _validation_failure_output_tail_path(run_dir, name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    header, _separator, body = text.partition("\n")
    # 只回显与这条缓存 record 同一身份的那次失败输出，防止把别的内容/命令的旧失败张冠李戴。
    return body if header == f"cache_key: {cache_key}" else None


def run_validation(args: argparse.Namespace) -> int:
    repo_root, common_dir = repository_identity(Path(args.repo).expanduser().resolve())
    working_directory_argument = Path(args.working_directory).expanduser() if args.working_directory else repo_root
    working_directory = (
        working_directory_argument
        if working_directory_argument.is_absolute()
        else repo_root / working_directory_argument
    ).resolve()
    try:
        cwd_relative = working_directory.relative_to(repo_root).as_posix() or "."
    except ValueError as exc:
        raise ValueError("validation working directory must remain inside the target worktree") from exc
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("validation command is required after --")
    if args.validation_context_kind == "base_worktree" and not args.requires_base_worktree_context:
        raise ValueError(
            "base_worktree validation context requires --requires-base-worktree-context"
        )

    rvf_run_dir = Path(args.rvf_run_dir).expanduser() if args.rvf_run_dir else None

    content_digest = candidate_content_manifest_sha256(repo_root)
    environment_digest, public_environment = environment_fingerprint(command, args.environment_key)
    handoff_validation_command_entry = {
        "name": args.name,
        "argv": command,
        "working_directory_relative_to_worktree": cwd_relative,
        "environment_keys": sorted(set(args.environment_key)),
        "validation_context_kind": args.validation_context_kind,
        "requires_base_worktree_context": bool(args.requires_base_worktree_context),
    }
    command_payload = {"argv": command, "working_directory_relative_to_worktree": cwd_relative}
    command_digest = sha256_hex(canonical_json_bytes(command_payload))
    validation_context_worktree_realpath_sha256 = (
        sha256_hex(os.fsencode(str(repo_root)))
        if args.requires_base_worktree_context
        else None
    )
    cache_identity = {
        "schema_version": SCHEMA_VERSION,
        "name": args.name,
        "candidate_content_manifest_algorithm": CONTENT_MANIFEST_ALGORITHM,
        "candidate_content_manifest_sha256": content_digest,
        "command_sha256": command_digest,
        "environment_fingerprint_sha256": environment_digest,
        "requires_base_worktree_context": bool(args.requires_base_worktree_context),
        "validation_context_kind": args.validation_context_kind,
        "validation_context_worktree_realpath_sha256": validation_context_worktree_realpath_sha256,
    }
    cache_key = sha256_hex(canonical_json_bytes(cache_identity))
    common_identity = sha256_hex(os.fsencode(str(common_dir)))
    cache_root = (
        resolved_rvf_user_local_data_paths().runtime_state_root
        / "validation-result-cache-v2"
        / common_identity
    )
    entry_dir = cache_root / "entries" / cache_key
    record_path = entry_dir / "record.json"
    lock_path = cache_root / "locks" / f"{cache_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if record_path.exists() or record_path.with_suffix(".json.sha256").exists():
            try:
                record, record_digest = validated_record(record_path, cache_key)
            except ValueError as exc:
                payload = {"cache_status": "corrupt_fail_closed", "error": str(exc), "cache_record_path": str(record_path)}
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 76
            append_run_validation_record(
                rvf_run_dir, record, record_digest, handoff_validation_command_entry
            )
            print(
                json.dumps(
                    {
                        "cache_status": "hit",
                        **record,
                        **_run_validation_index_summary_field(rvf_run_dir, content_digest, common_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if record["status"] == "failed":
                persisted_tail = _persisted_validation_failure_output_tail(rvf_run_dir, args.name, cache_key)
                print(
                    persisted_tail
                    if persisted_tail is not None
                    else (
                        f"validation {args.name} 命中一条已失败的缓存记录（exit {record['exit_code']}）；"
                        "该次失败的输出尾部不在本 run 的 artifacts 里（缓存只存 hash）。"
                        "修改内容后本 wrapper 会自动重跑并回显新的失败尾部。"
                    ),
                    file=sys.stderr,
                )
            return int(record["exit_code"])

        command_environment = clean_git_environment()
        completed = subprocess.run(
            command,
            cwd=working_directory,
            env=command_environment,
            check=False,
            capture_output=True,
        )
        post_content_digest = candidate_content_manifest_sha256(repo_root)
        if post_content_digest != content_digest:
            print(
                json.dumps(
                    {
                        "cache_status": "candidate_changed_during_validation",
                        "before_candidate_content_manifest_sha256": content_digest,
                        "after_candidate_content_manifest_sha256": post_content_digest,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 74
        record = {
            "schema": "rvf-validation-result-cache-record",
            "schema_version": SCHEMA_VERSION,
            "cache_key": cache_key,
            "name": args.name,
            "status": "passed" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "candidate_content_manifest_algorithm": CONTENT_MANIFEST_ALGORITHM,
            "candidate_content_manifest_sha256": content_digest,
            "command_sha256": command_digest,
            "command_metadata": {
                "working_directory_relative_to_worktree": cwd_relative,
                "argument_count": len(command),
                "executable_name": Path(command[0]).name,
            },
            "environment_fingerprint_sha256": environment_digest,
            "environment": public_environment,
            "requires_base_worktree_context": bool(args.requires_base_worktree_context),
            "validation_context_kind": args.validation_context_kind,
            "validation_context_worktree_realpath_sha256": validation_context_worktree_realpath_sha256,
            "cache_record_path": str(record_path),
            "stdout_sha256": sha256_hex(completed.stdout),
            "stdout_byte_count": len(completed.stdout),
            "stderr_sha256": sha256_hex(completed.stderr),
            "stderr_byte_count": len(completed.stderr),
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        record_digest = write_json_with_digest(record_path, record)
        append_run_validation_record(
            rvf_run_dir, record, record_digest, handoff_validation_command_entry
        )
        print(
            json.dumps(
                {
                    "cache_status": "executed",
                    **record,
                    "cache_record_sha256": record_digest,
                    **_run_validation_index_summary_field(rvf_run_dir, content_digest, common_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if completed.returncode != 0:
            # 失败时直接回显尾部：否则 agent 只拿到 hash，会在缓存外再跑一遍来看失败原因。
            rendered_tail = _render_validation_failure_output_tail(
                args.name,
                completed.returncode,
                _output_tail_text(completed.stdout),
                _output_tail_text(completed.stderr),
            )
            _persist_validation_failure_output_tail(rvf_run_dir, args.name, cache_key, rendered_tail)
            print(rendered_tail, file=sys.stderr, end="")
        return completed.returncode


def _run_validation_index_summary_field(
    rvf_run_dir: Path | None, content_digest: str, common_dir: Path
) -> dict[str, Any]:
    if rvf_run_dir is None:
        return {}
    return {
        "run_validation_index_summary": summarize_run_validation_index(
            rvf_run_dir,
            current_candidate_content_manifest_sha256=content_digest,
            expected_git_common_directory=common_dir,
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RVF content-addressed validation evidence cache.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    manifest = subparsers.add_parser("manifest", help="Print the effective filesystem content manifest.")
    manifest.add_argument("--repo", default=".")
    run_parser = subparsers.add_parser("run", help="Run or reuse one orchestrator-declared validation suite.")
    run_parser.add_argument("--repo", default=".")
    run_parser.add_argument("--name", required=True)
    run_parser.add_argument("--working-directory")
    run_parser.add_argument("--environment-key", action="append", default=[])
    run_parser.add_argument("--requires-base-worktree-context", action="store_true")
    run_parser.add_argument(
        "--validation-context-kind",
        choices=sorted(VALIDATION_CONTEXT_KINDS),
        default="task_worktree",
    )
    run_parser.add_argument(
        "--rvf-run-dir",
        help="RVF run 目录；只认显式参数，缺省时不写任何 run（不回退 RVF_RUN_DIR）。",
    )
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.subcommand == "manifest":
            digest = candidate_content_manifest_sha256(Path(args.repo).expanduser().resolve())
            print(
                json.dumps(
                    {
                        "candidate_content_manifest_algorithm": CONTENT_MANIFEST_ALGORITHM,
                        "candidate_content_manifest_sha256": digest,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        return run_validation(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
