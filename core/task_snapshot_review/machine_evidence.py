"""预登记机器命令的隔离执行与 run 内单飞证据。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .execution_lifecycle import (
    append_stage_event, ensure_execution_not_revoked, register_owned_process,
    runtime_environment_identity, wait_for_owned_process,
)
from .persistent_artifacts import exclusive_lock, git, git_text, read_json, write_json
from .task_snapshot import capture_worktree_tree


class WorkspacePreparationFailed(RuntimeError):
    def __init__(self, original: Path):
        super().__init__("隔离环境准备失败；等待依赖或已登记配置变化")
        self.original_evidence_paths = [str(original)]


def execute_registered_commands(run_directory: Path, workspace: Path, commands: list[list[str]],
                                artifacts: Path, purpose: str, *, environment: dict | None = None, deadline_at: float | None = None,
                                inherited_lock_descriptors: tuple[int, ...] = ()) -> dict:
    plan = read_json(run_directory / "execution-plan.json") if (run_directory / "execution-plan.json").exists() else {}
    registered = [*plan.get("preparation_commands", []), *plan.get("machine_evidence_commands", []), *plan.get("validation_commands", [])]
    if plan and any(command not in registered for command in commands):
        raise ValueError("执行器只能代跑启动时登记的命令")
    records = []
    for index, command in enumerate(commands):
        ensure_execution_not_revoked(run_directory)
        if deadline_at is not None and deadline_at <= time.time():
            raise subprocess.TimeoutExpired(command, 0)
        command_directory = artifacts / f"command-{index + 1:03d}"
        command_directory.mkdir(parents=True, exist_ok=True)
        source_tree_before = capture_worktree_tree(workspace, ["."], command_directory) if purpose == "shared_machine_evidence" else None
        record = {"argv": command, "cwd": str(workspace), "started_at": time.time(), "status": "running"}
        write_json(command_directory / "execution.json", record)
        with (command_directory / "stdout.txt").open("wb") as stdout, (command_directory / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=workspace, env=environment, stdout=stdout, stderr=stderr,
                                       start_new_session=True, pass_fds=inherited_lock_descriptors)
            register_owned_process(run_directory, process.pid, purpose, owns_process_group=True)
            try:
                try:
                    returncode = wait_for_owned_process(process, run_directory, timeout=None if deadline_at is None else max(0, deadline_at - time.time()))
                except subprocess.TimeoutExpired:
                    returncode = 124
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        record.update({"returncode": returncode, "finished_at": time.time(), "status": "passed" if returncode == 0 else "failed"})
        record.update({f"{stream}_sha256": hashlib.sha256((command_directory / f"{stream}.txt").read_bytes()).hexdigest()
                       for stream in ("stdout", "stderr")})
        if source_tree_before is not None:
            source_tree_after = capture_worktree_tree(workspace, ["."], command_directory)
            record.update({"source_tree_before": source_tree_before, "source_tree_after": source_tree_after})
            if source_tree_before != source_tree_after:
                record["status"] = "candidate_changed"
        write_json(command_directory / "execution.json", record)
        records.append(record)
        append_stage_event(run_directory, "machine_command", purpose=purpose, **record)
        if record["status"] == "candidate_changed":
            return {"status": "candidate_changed", "commands": records, "failed_command_directory": str(command_directory)}
        if returncode:
            return {"status": "failed", "commands": records, "failed_command_directory": str(command_directory)}
    return {"status": "passed", "commands": records}


def prepare_isolated_workspace(run_directory: Path, workspace: Path, commands: list[list[str]], artifacts: Path,
                               *, environment: dict | None = None, deadline_at: float | None = None,
                               inherited_lock_descriptors: tuple[int, ...] = ()) -> None:
    if not commands:
        return
    before = set(git(workspace, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    preparation_paths_file = artifacts / "preparation-created-untracked-paths.json"
    previously_created = set(read_json(preparation_paths_file)["paths"]) if preparation_paths_file.exists() else set()
    try:
        result = execute_registered_commands(run_directory, workspace, commands, artifacts / "preparation", "workspace_preparation",
            environment=environment, deadline_at=deadline_at, inherited_lock_descriptors=inherited_lock_descriptors)
    finally:
        after = set(git(workspace, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
        write_json(preparation_paths_file, {"paths": sorted(previously_created | (after - before))})
    write_json(artifacts / "workspace-preparation.json", result)
    if result["status"] != "passed":
        raise WorkspacePreparationFailed(artifacts / "workspace-preparation.json")
    if git(workspace, "diff", "HEAD", "--name-only"):
        raise ValueError("准备命令修改了候选源码；不能把准备环境冒充被评审树")


def collect_shared_machine_evidence(run_directory: Path, record: dict, plan: dict) -> dict | None:
    commands = plan.get("machine_evidence_commands", [])
    if not commands:
        return None
    with exclusive_lock(run_directory / "machine-evidence.lock") as collection_lock:
        root = run_directory / "machine-evidence"
        root.mkdir(exist_ok=True)
        commands_identity = []
        for command in [*plan.get("preparation_commands", []), *commands]:
            executable = shutil.which(command[0])
            commands_identity.append({"argv": command, "executable": executable,
                "executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None})
        identity = {"tested_tree": git_text(Path(record["repo"]), "rev-parse", record["candidate_commit"] + "^{tree}"),
                    "commands": commands_identity, "runtime_environment": runtime_environment_identity(),
                    "machine_context": plan.get("machine_context"), "preparation_commands": plan.get("preparation_commands", [])}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        previous = sorted(root.glob("attempt-*"))
        # 调用方声明的依赖标签不是安装内容证明；跨接续重新采集，同次执行内只采集一份事实。
        proven_dependencies = False
        attempt = root / f"attempt-{len(previous) + 1:03d}"
        workspace = attempt / "workspace"
        attempt.mkdir()
        git(Path(record["repo"]), "worktree", "add", "--detach", str(workspace), record["candidate_commit"])
        # 命令继承同一单飞锁；controller 崩溃后，接续必须等原命令退出才能重新采集。
        prepare_isolated_workspace(run_directory, workspace, plan.get("preparation_commands", []), attempt,
                                   inherited_lock_descriptors=(collection_lock.fileno(),))
        result = execute_registered_commands(run_directory, workspace, commands, attempt / "commands", "shared_machine_evidence",
                                             inherited_lock_descriptors=(collection_lock.fileno(),))
        evidence = {**identity, **result, "identity_sha256": digest, "cwd": str(workspace),
                    "dependency_identity_proven": proven_dependencies, "original": str(attempt / "evidence.json")}
        write_json(attempt / "evidence.json", evidence)
        return evidence
