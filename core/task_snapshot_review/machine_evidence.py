"""预登记机器命令的隔离执行与 run 内单飞证据。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .execution_lifecycle import (
    append_stage_event, ensure_execution_not_revoked, register_owned_process, process_start_identity, ExecutionRevoked,
    owned_process_start_time_and_group_match,
    runtime_environment_identity, wait_for_owned_process, register_supervised_command_directory,
)
from .persistent_artifacts import exclusive_lock, git, git_text, read_json, write_json
from .task_snapshot import capture_worktree_tree


class WorkspacePreparationFailed(RuntimeError):
    def __init__(self, original: Path):
        super().__init__("隔离环境准备失败；等待依赖或已登记配置变化")
        self.original_evidence_paths = [str(original)]


def verified_execution_plan(run_directory: Path, record: dict | None = None) -> dict:
    record = record if record is not None else read_json(run_directory / "run.json")
    expected_digest = record.get("execution_plan_sha256")
    if not expected_digest:
        return {}
    original = (run_directory / "execution-plan.json").read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_digest:
        raise ValueError("冻结启动材料摘要不匹配；恢复原件后接续")
    return json.loads(original)


def ensure_previous_supervised_commands_exited(run_directory: Path, collection_directory: Path) -> None:
    from .native_owned_process_observation import (
        REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE, REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT,
        native_host_boot_identity, native_process_identity, owned_process_snapshot,
    )
    registry = run_directory / 'registered-supervised-command-directories.json'
    for registered_directory in read_json(registry)['command_directories'] if registry.exists() else []:
        command_directory = Path(registered_directory).resolve()
        relative = command_directory.relative_to(run_directory.resolve())
        if 'workspace' in relative.parts or not relative.parts:
            raise ValueError('已登记监督目录不能指向候选源码或 run 根目录')
        if not command_directory.is_relative_to(collection_directory.resolve()):
            continue
        supervision_file = command_directory / 'command-supervision.json'
        reconciliation_file = command_directory / 'descendant-exit-reconciliation.json'
        previous = read_json(reconciliation_file) if reconciliation_file.exists() else {}
        if previous.get('status') == 'exited':
            continue
        observation = {'status': 'unknown', 'observed_at': time.time(),
            'recovery_condition': 'previous_supervisor_and_descendants_exit_proven',
            'action': 'resume_same_run', 'supervision_file': str(supervision_file)}
        recorded = {}
        try:
            recorded = {item['pid']: item for item in previous.get('owned_processes', [])}
            if not supervision_file.exists():
                execution = read_json(command_directory / 'execution.json')
                if execution.get('status') == 'launch_failed' and execution.get('execution_started') is False:
                    observation.update({'status': 'not_started', 'native_launch_error': execution['native_launch_error']})
                    write_json(reconciliation_file, observation)
                    continue
            facts = read_json(supervision_file)
            if facts.get('status') == 'completed' and facts.get('ownership_observation_coverage_status') != 'unknown':
                continue
            if (facts.get('process_ownership_scope') != REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE or
                    facts.get('command_process_lifetime_contract') != REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT):
                raise RuntimeError('历史未完成命令缺少受控进程契约，不能自动补写为已退出')
            if facts.get('command_execution_started') is False and not facts.get('command_pid'):
                observation.update({'status': 'not_started', 'native_launch_error': facts['command_native_launch_error']})
                write_json(reconciliation_file, observation)
                continue
            execution_token = facts.get('execution_token')
            if not isinstance(execution_token, str) or not execution_token:
                raise RuntimeError('缺少有效的已登记命令 execution_token')
            observation['execution_token'] = execution_token
            if facts.get('ownership_observation_coverage_status') == 'unknown':
                from .registered_command_owner_exit_receipt import observe_registered_command_owner_exit_receipt
                observation['ownership_observation_coverage_status'] = 'unknown'
                if facts.get('ownership_observation_gap_started_at') is not None:
                    observation['ownership_observation_gap_started_at'] = facts['ownership_observation_gap_started_at']
                owner_receipt = observe_registered_command_owner_exit_receipt(run_directory, facts)
                observation['owner_exit_receipt'] = owner_receipt
                if owner_receipt['status'] == 'exited':
                    observation.update(status='exited', source=owner_receipt['source'], owned_processes=[])
                    write_json(reconciliation_file, observation)
                    continue
            if facts.get('host_boot_identity') and facts['host_boot_identity'] != native_host_boot_identity():
                observation.update(status='exited', source='host_boot_changed')
                write_json(reconciliation_file, observation)
                continue
            if (facts.get('command_launch_intent_recorded') and not facts.get('command_pid')
                    and facts.get('command_returncode') is None):
                raise RuntimeError('命令派发登记中断，缺少直接命令身份或退出事实')
            current = native_process_identity(facts['supervisor_pid'])
            identity = facts.get('supervisor_native_identity')
            if not identity:
                # 监督者须先持久保存启动身份才获准派发；此前退出可证明命令未启动。
                if current is not None or facts.get('command_pid') is not None:
                    raise RuntimeError('缺少上一监督者原生启动身份')
                observation['status'] = 'exited'
            else:
                for item in facts.get('owned_processes', []):
                    recorded.setdefault(item['pid'], item)
                if facts.get('command_native_identity'):
                    recorded.setdefault(facts['command_pid'], facts['command_native_identity'])
                elif facts.get('command_pid') and facts['command_pid'] not in recorded and facts.get('command_returncode') is None:
                    raise RuntimeError('缺少直接命令的已登记原生身份或退出事实')
                descendants = owned_process_snapshot(facts['supervisor_pid'], recorded,
                    supervisor_native_identity=identity)
                supervisor_still_has_recorded_start_identity = bool(current
                    and current['pid'] == identity['pid'] and current['start_identity'] == identity['start_identity'])
                observation.update({'owned_processes': list(descendants.values()),
                    'status': 'pending' if supervisor_still_has_recorded_start_identity or descendants else 'exited'})
            if facts.get('ownership_observation_coverage_status') == 'unknown':
                observation['recovery_condition'] = 'native_host_boot_changed'
                raise RuntimeError('原生观测缺口仍未覆盖；当前空快照不构成完整后代退出证据')
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            recorded.update(getattr(error, 'observed_owned_processes', {}))
            observation.update({'status': 'unknown', 'error': str(error), 'owned_processes': list(recorded.values())})
        write_json(reconciliation_file, observation)
        if observation['status'] != 'exited':
            error = WorkspacePreparationFailed(reconciliation_file)
            error.recovery = {'trigger': observation['recovery_condition'], 'action': 'resume_same_run',
                'equivalent_sources': ['native_process_start_identity', 'native_descendant_ownership_snapshot'],
                'maximum_unchanged_retries': 1}
            raise error


def command_launch_was_not_started(execution: dict) -> bool:
    return execution.get("status") == "launch_failed" and execution.get("execution_started") is False


def unstarted_launch_retries_exhausted(directory: Path) -> bool:
    """同一次尝试里，已证明未启动的派发最多三次。"""
    return len(list((directory / "unstarted-launch-failures").glob("*.json"))) >= 2


def archive_unstarted_command_launch(directory: Path) -> None:
    execution_file = directory / "execution.json"
    execution = read_json(execution_file)
    if not command_launch_was_not_started(execution):
        raise WorkspacePreparationFailed(execution_file)
    archive = directory / "unstarted-launch-failures"
    archive.mkdir(exist_ok=True)
    write_json(archive / f"{len(list(archive.glob('*.json'))) + 1:03d}.json", execution)
    execution_file.unlink()


def unknown_registered_command_directory(run_directory: Path, argv: list[str]) -> Path | None:
    """同一 run 内，结果尚未证明的命令不能因请求组合不同而再次派发。"""
    registry_file = run_directory / "registered-supervised-command-directories.json"
    if not registry_file.exists():
        return None
    for registered in read_json(registry_file)["command_directories"]:
        directory = Path(registered)
        execution_file = directory / "execution.json"
        if not execution_file.is_file():
            continue
        try:
            execution = read_json(execution_file)
        except (OSError, ValueError):
            continue
        if execution.get("argv") != argv or command_launch_was_not_started(execution):
            continue
        supervision_file = directory / "command-supervision.json"
        if not supervision_file.is_file():
            return directory
        try:
            supervision = read_json(supervision_file)
        except (OSError, ValueError):
            return directory
        if supervision.get("status") != "completed" or supervision.get("argv") != argv:
            return directory
    return None


def _dispatched_registered_command_argvs(attempt: Path) -> list[list[str]]:
    argvs = []
    for pattern in ("commands/**/execution.json", "preparation/**/execution.json",
                    "commands/**/unstarted-launch-failures/*.json", "preparation/**/unstarted-launch-failures/*.json"):
        for path in attempt.glob(pattern):
            try:
                argv = read_json(path).get("argv")
            except (OSError, ValueError):
                continue
            if isinstance(argv, list):
                argvs.append(argv)
    return argvs


def execute_registered_commands(run_directory: Path, workspace: Path, commands: list[list[str]],
                                artifacts: Path, purpose: str, *, environment: dict | None = None,
                                inherited_lock_descriptors: tuple[int, ...] = (),
                                excluded_preparation_paths: list[str] | None = None,
                                expected_source_tree: str | None = None,
                                command_index_offset: int = 0) -> dict:
    plan = verified_execution_plan(run_directory)
    registered = [*plan.get("preparation_commands", []), *plan.get("machine_evidence_commands", []), *plan.get("validation_commands", [])]
    if plan and any(command not in registered for command in commands):
        raise ValueError("执行器只能代跑启动时登记的命令")
    records = []
    for index, command in enumerate(commands):
        ensure_execution_not_revoked(run_directory)
        verified_execution_plan(run_directory)
        # 角色准备在各自隔离工作区并行执行；未知结果去重只约束共享和 reviewer 机器命令。
        if purpose != "workspace_preparation":
            unknown_directory = unknown_registered_command_directory(run_directory, command)
            if unknown_directory is not None:
                raise WorkspacePreparationFailed(unknown_directory / "execution.json")
        command_directory = artifacts / f"command-{index + 1 + command_index_offset:03d}"
        command_directory.mkdir(parents=True, exist_ok=True)
        source_tree_before = (capture_worktree_tree(workspace, ["."], command_directory,
            excluded_preparation_paths=excluded_preparation_paths)
            if purpose in {"shared_machine_evidence", "incomplete_fixer_machine_validation", "reviewer_requested_machine_evidence"} else None)
        record = {"argv": command, "cwd": str(workspace), "started_at": time.time(), "status": "running"}
        if source_tree_before is not None:
            record["source_tree_before"] = source_tree_before
            record["excluded_preparation_paths"] = excluded_preparation_paths
            if expected_source_tree is not None and source_tree_before != expected_source_tree:
                record.update({"status": "candidate_changed", "execution_started": False})
                write_json(command_directory / "execution.json", record)
                records.append(record)
                append_stage_event(run_directory, "machine_command", purpose=purpose, **record)
                return {"status": "candidate_changed", "commands": records, "failed_command_directory": str(command_directory)}
        write_json(command_directory / "execution.json", record)
        with (command_directory / "stdout.txt").open("wb") as stdout, (command_directory / "stderr.txt").open("wb") as stderr:
            with exclusive_lock(run_directory / "execution-revocation.lock"):
                verified_execution_plan(run_directory)
                ensure_execution_not_revoked(run_directory)
                launch_command = command
                if inherited_lock_descriptors:
                    register_supervised_command_directory(run_directory, command_directory)
                    launch_command = [sys.executable, str(Path(__file__).with_name("retained_command_supervisor.py")),
                        str(run_directory), str(command_directory),
                        json.dumps(list(inherited_lock_descriptors)), json.dumps(command)]
                try:
                    process = subprocess.Popen(launch_command, cwd=workspace, env=environment, stdout=stdout, stderr=stderr,
                                               start_new_session=True, pass_fds=inherited_lock_descriptors)
                except OSError as error:
                    record.update({'status': 'launch_failed', 'execution_started': False, 'finished_at': time.time(),
                        'native_launch_error': {'type': type(error).__name__, 'errno': error.errno, 'message': str(error)}})
                    write_json(command_directory / 'execution.json', record)
                    error.original_evidence_paths = [str(command_directory / 'execution.json')]
                    raise
                register_owned_process(run_directory, process.pid, purpose, owns_process_group=True)
            try:
                try:
                    record["process_identity"] = process_start_identity(process.pid)
                except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                    record["process_identity_error"] = str(error)
                write_json(command_directory / "execution.json", record)
                returncode = wait_for_owned_process(process, run_directory, timeout=None,
                    observe_registered_process_descendants=not bool(inherited_lock_descriptors))
            finally:
                if process.poll() is None:
                    (os.kill if inherited_lock_descriptors else os.killpg)(process.pid, signal.SIGTERM)
                    try:
                        # 此处只限制取消后的清理等待，不限制命令执行时间。
                        process.wait(timeout=0.75 if inherited_lock_descriptors else 5)
                    except subprocess.TimeoutExpired:
                        supervision_file = command_directory / 'command-supervision.json'
                        if inherited_lock_descriptors and supervision_file.exists():
                            from .retained_command_supervisor import terminate_supervised_command_descendants
                            observations = terminate_supervised_command_descendants(command_directory)
                            if any(item['cancellation_status'] == 'unknown' for item in observations):
                                raise WorkspacePreparationFailed(supervision_file)
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        supervision_file = command_directory / 'command-supervision.json'
        if supervision_file.exists() and read_json(supervision_file).get('status') == 'descendant_exit_unproven':
            record.update({'returncode': returncode, 'finished_at': time.time(), 'status': 'descendant_exit_unproven'})
            write_json(command_directory / 'execution.json', record)
            raise WorkspacePreparationFailed(supervision_file)
        record.update({"returncode": returncode, "finished_at": time.time(), "status": "passed" if returncode == 0 else "failed"})
        record.update({f"{stream}_sha256": hashlib.sha256((command_directory / f"{stream}.txt").read_bytes()).hexdigest()
                       for stream in ("stdout", "stderr")})
        if source_tree_before is not None:
            source_tree_after = capture_worktree_tree(workspace, ["."], command_directory,
                excluded_preparation_paths=excluded_preparation_paths)
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
                               *, environment: dict | None = None,
                               inherited_lock_descriptors: tuple[int, ...] = ()) -> None:
    if not commands:
        return
    ensure_previous_supervised_commands_exited(run_directory, artifacts.parent)
    before = set(git(workspace, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    preparation_paths_file = artifacts / "preparation-created-untracked-paths.json"
    previously_created = set(read_json(preparation_paths_file)["paths"]) if preparation_paths_file.exists() else set()
    try:
        result = execute_registered_commands(run_directory, workspace, commands, artifacts / "preparation", "workspace_preparation",
            environment=environment, inherited_lock_descriptors=inherited_lock_descriptors)
    finally:
        after = set(git(workspace, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
        write_json(preparation_paths_file, {"paths": sorted(previously_created | (after - before))})
    write_json(artifacts / "workspace-preparation.json", result)
    if result["status"] != "passed":
        raise WorkspacePreparationFailed(artifacts / "workspace-preparation.json")
    if git(workspace, "diff", "HEAD", "--name-only"):
        raise ValueError("准备命令修改了候选源码；不能把准备环境冒充被评审树")


def prepare_or_reconcile_registered_machine_command_workspace(run_directory: Path, workspace: Path,
        commands: list[list[str]], artifacts: Path, *, environment: dict,
        inherited_lock_descriptors: tuple[int, ...]) -> dict | None:
    if not commands:
        return None
    ensure_previous_supervised_commands_exited(run_directory, artifacts)
    preparation_file = artifacts / "workspace-preparation.json"
    if not preparation_file.exists():
        executions = sorted(artifacts.glob("preparation/command-*/execution.json"))
        if executions:
            exclusions_file = artifacts / "preparation-created-untracked-paths.json"
            if list(artifacts.glob("commands/**/execution.json")) and not exclusions_file.exists():
                raise WorkspacePreparationFailed(exclusions_file)
            completed = []
            result = None
            for index, execution_file in enumerate(executions):
                execution = read_json(execution_file)
                if command_launch_was_not_started(execution):
                    if unstarted_launch_retries_exhausted(execution_file.parent):
                        raise WorkspacePreparationFailed(execution_file)
                    archive_unstarted_command_launch(execution_file.parent)
                    remaining = execute_registered_commands(run_directory, workspace, commands[index:],
                        artifacts / "preparation", "workspace_preparation", environment=environment,
                        inherited_lock_descriptors=inherited_lock_descriptors, command_index_offset=index)
                    result = {**remaining, "commands": [*completed, *remaining["commands"]]}
                    break
                native_file = execution_file.parent / "command-supervision.json"
                native = read_json(native_file) if native_file.exists() else {}
                if (index >= len(commands) or execution_file.parent.name != f"command-{index + 1:03d}"
                        or native.get("status") != "completed" or native.get("argv") != commands[index]):
                    raise WorkspacePreparationFailed(execution_file)
                for stream in ("stdout", "stderr"):
                    if native.get(stream + "_sha256") != hashlib.sha256((execution_file.parent / (stream + ".txt")).read_bytes()).hexdigest():
                        raise WorkspacePreparationFailed(native_file)
                completed.append({**execution, "returncode": native["returncode"],
                    "status": "passed" if native["returncode"] == 0 else "failed"})
                if native["returncode"]:
                    break
            if result is None:
                result = {"status": "failed" if completed[-1]["returncode"] else "passed", "commands": completed}
                if result["status"] == "passed" and len(completed) < len(commands):
                    remaining = execute_registered_commands(run_directory, workspace, commands[len(completed):],
                        artifacts / "preparation", "workspace_preparation", environment=environment,
                        inherited_lock_descriptors=inherited_lock_descriptors, command_index_offset=len(completed))
                    result = {**remaining, "commands": [*completed, *remaining["commands"]]}
            write_json(artifacts / "workspace-preparation-reconciliation.json", result)
            write_json(preparation_file, result)
            if not exclusions_file.exists():
                created = git(workspace, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
                write_json(exclusions_file, {"paths": sorted(path for path in created if path)})
        else:
            try:
                prepare_isolated_workspace(run_directory, workspace, commands, artifacts,
                    environment=environment, inherited_lock_descriptors=inherited_lock_descriptors)
            except WorkspacePreparationFailed:
                if not preparation_file.exists():
                    raise
    result = read_json(preparation_file)
    if ([command["argv"] for command in result["commands"]] != commands[:len(result["commands"])]
            or result["status"] == "passed" and len(result["commands"]) != len(commands)):
        raise WorkspacePreparationFailed(preparation_file)
    if git(workspace, "diff", "HEAD", "--name-only"):
        raise ValueError("准备命令修改了候选源码；不能把准备环境冒充被评审树")
    return result


def _reject_shared_command_log_mismatch(attempt: Path, cached: dict, commands: list[list[str]]) -> None:
    for index, command in enumerate(cached.get("commands", [])):
        directory = attempt / "commands" / f"command-{index + 1:03d}"
        try:
            mismatched = index >= len(commands) or command.get("argv") != commands[index] or any(
                command.get(stream + "_sha256") != hashlib.sha256((directory / (stream + ".txt")).read_bytes()).hexdigest()
                for stream in ("stdout", "stderr"))
        except OSError:
            raise WorkspacePreparationFailed(directory) from None
        if mismatched:
            raise WorkspacePreparationFailed(directory)


def _shared_cached_pass_is_currently_qualified(attempt: Path, cached: dict, commands: list[list[str]], identity: dict) -> bool:
    """复用共享通过前核对完整树、命令、环境、依赖证明、当前准备产物和日志，不改写原件。"""
    if cached.get("status") != "passed":
        return False
    # 依赖内容未证明时，历史通过不能取得当前资格，预算耗尽后也不交回。
    if not cached.get("dependency_identity_proven"):
        return False
    runtime = cached.get("runtime_environment")
    if (cached.get("tested_tree") != identity["tested_tree"]
            or runtime != cached.get("host_recovery_inputs_after") or runtime != identity["runtime_environment"]):
        return False
    recorded = cached.get("commands", [])
    if [item.get("argv") for item in recorded] != commands:
        return False
    for index, command in enumerate(recorded):
        if (command.get("status") != "passed" or command.get("returncode") != 0
                or command.get("source_tree_before") != identity["tested_tree"]
                or command.get("source_tree_after") != identity["tested_tree"]):
            return False
        dependencies = command.get("prepared_workspace_dependency_identity")
        cwd = command.get("cwd")
        if not isinstance(dependencies, dict) or not cwd:
            return False
        try:
            observed = prepared_workspace_dependency_identity(Path(cwd), dependencies.get("preparation_created_paths"))
        except (OSError, RuntimeError, ValueError):
            return False
        if observed != dependencies:
            return False
        directory = attempt / "commands" / f"command-{index + 1:03d}"
        try:
            if any(command.get(stream + "_sha256") != hashlib.sha256(
                    (directory / (stream + ".txt")).read_bytes()).hexdigest() for stream in ("stdout", "stderr")):
                return False
        except OSError:
            return False
    return True


def collect_shared_machine_evidence(run_directory: Path, record: dict, plan: dict) -> dict | None:
    commands = plan.get("machine_evidence_commands", [])
    if not commands:
        return None
    from .review_recovery_evidence_observation import registered_host_recovery_inputs
    with exclusive_lock(run_directory / "machine-evidence.lock") as collection_lock:
        root = run_directory / "machine-evidence"
        root.mkdir(exist_ok=True)
        ensure_previous_supervised_commands_exited(run_directory, root)
        commands_identity = []
        for command in [*plan.get("preparation_commands", []), *commands]:
            executable = shutil.which(command[0])
            commands_identity.append({"argv": command, "executable": executable,
                "executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None})
        environment = prepare_registered_machine_command_environment(run_directory)
        identity = {"tested_tree": git_text(Path(record["repo"]), "rev-parse", record["candidate_commit"] + "^{tree}"),
                    "commands": commands_identity, "runtime_environment": registered_host_recovery_inputs(run_directory, plan),
                    "machine_context": plan.get("machine_context"), "preparation_commands": plan.get("preparation_commands", [])}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        previous = sorted(root.glob("attempt-*"))
        for previous_attempt in previous:
            if not (previous_attempt / "collection-intent.json").exists():
                raise WorkspacePreparationFailed(previous_attempt)
            if (previous_attempt / "evidence.json").exists():
                continue
            # 控制器退出不等于命令没执行；从监督原件重建结果，不能重新发出同一命令。
            from .reviewer_machine_evidence_recovery import reconciled_registered_machine_command_from_native_supervision
            original_intent = read_json(previous_attempt / "collection-intent.json")
            original_identity = original_intent["identity"]
            previous_workspace = previous_attempt / "workspace"
            if not previous_workspace.exists():
                if original_identity != identity:
                    raise WorkspacePreparationFailed(previous_attempt)
                with exclusive_lock(run_directory / "execution-revocation.lock"):
                    ensure_execution_not_revoked(run_directory)
                    git(Path(record["repo"]), "worktree", "add", "--detach", str(previous_workspace), record["candidate_commit"])
            preparation = prepare_or_reconcile_registered_machine_command_workspace(run_directory,
                previous_workspace, plan.get("preparation_commands", []), previous_attempt,
                environment=environment, inherited_lock_descriptors=(collection_lock.fileno(),))
            if preparation and preparation["status"] != "passed":
                from .role_failure_recovery import classify_role_failure
                failed = previous_attempt / "preparation" / f"command-{len(preparation['commands']):03d}"
                failure = classify_role_failure("", (failed / "stderr.txt").read_text(errors="replace"), preparation["commands"][-1]["returncode"], "text")
                recovered = {**original_identity, "status": "failed", "commands": [], "preparation": preparation,
                    "identity_sha256": original_intent["identity_sha256"], "dependency_identity_proven": False,
                    "failure": failure, "original": str(previous_attempt / "evidence.json")}
                if failure["maximum_same_configuration_retries"]:
                    recovered["retry_not_before_epoch_ms"] = (time.time() + failure["retry_not_before_seconds"]) * 1000
                write_json(previous_attempt / "evidence.json", recovered)
                continue
            excluded_file = previous_attempt / "preparation-created-untracked-paths.json"
            excluded = read_json(excluded_file)["paths"] if excluded_file.exists() else None
            recovered_commands = []
            recovered_status = "passed"
            for index, argv in enumerate(commands):
                command_directory = previous_attempt / "commands" / f"command-{index + 1:03d}"
                if (command_directory / "execution.json").exists():
                    execution = read_json(command_directory / "execution.json")
                    if command_launch_was_not_started(execution):
                        if unstarted_launch_retries_exhausted(command_directory):
                            raise WorkspacePreparationFailed(command_directory / "execution.json")
                        archive_unstarted_command_launch(command_directory)
                        if not previous_workspace.exists():
                            git(Path(record["repo"]), "worktree", "add", "--detach", str(previous_workspace), record["candidate_commit"])
                        remainder = execute_registered_commands(run_directory, previous_workspace, commands[index:],
                            previous_attempt / "commands", "shared_machine_evidence", environment=environment,
                            inherited_lock_descriptors=(collection_lock.fileno(),), excluded_preparation_paths=excluded,
                            expected_source_tree=original_identity["tested_tree"], command_index_offset=index)
                        recovered_commands.extend(remainder["commands"])
                        recovered_status = remainder["status"]
                        break
                    recovered_command = reconciled_registered_machine_command_from_native_supervision(command_directory, argv, original_identity["tested_tree"])
                    recovered_commands.append(recovered_command)
                    if recovered_command["status"] != "passed":
                        recovered_status = recovered_command["status"]
                        break
                else:
                    if original_identity != identity:
                        recovered_status = "environment_changed"
                        break
                    if not previous_workspace.exists():
                        git(Path(record["repo"]), "worktree", "add", "--detach", str(previous_workspace), record["candidate_commit"])
                    remainder = execute_registered_commands(run_directory, previous_workspace, commands[index:],
                        previous_attempt / "commands", "shared_machine_evidence", environment=environment,
                        inherited_lock_descriptors=(collection_lock.fileno(),), excluded_preparation_paths=excluded,
                        expected_source_tree=original_identity["tested_tree"], command_index_offset=index)
                    recovered_commands.extend(remainder["commands"])
                    recovered_status = remainder["status"]
                    break
            current_inputs = registered_host_recovery_inputs(run_directory, plan)
            dependency_before_file = previous_attempt / "prepared-workspace-dependency-identity-before.json"
            dependency_before = read_json(dependency_before_file) if dependency_before_file.exists() else None
            dependency_after = prepared_workspace_dependency_identity(previous_workspace, excluded) if previous_workspace.exists() else None
            proven_sources = original_identity["runtime_environment"]["registered_dependency_sources"]["identity_proven"]
            dependency_identity_proven = bool(recovered_status == "passed" and proven_sources and dependency_before
                and dependency_before.get("identity_proven") and dependency_after == dependency_before
                and current_inputs == original_identity["runtime_environment"])
            if dependency_before:
                for command in recovered_commands:
                    command["prepared_workspace_dependency_identity"] = dependency_before
            recovered = {**original_identity, "commands": recovered_commands,
                "status": recovered_status,
                "identity_sha256": original_intent["identity_sha256"], "cwd": str(previous_attempt / "workspace"),
                "host_recovery_inputs_after": current_inputs,
                "dependency_identity_proven": dependency_identity_proven, "original": str(previous_attempt / "evidence.json")}
            if recovered_status == "failed":
                from .role_failure_recovery import classify_role_failure
                failed_index = len(recovered_commands)
                failed = previous_attempt / "commands" / f"command-{failed_index:03d}"
                failure = classify_role_failure("", (failed / "stderr.txt").read_text(errors="replace"), recovered_commands[-1]["returncode"], "text")
                recovered["failure"] = failure
                if failure["maximum_same_configuration_retries"]:
                    recovered["retry_not_before_epoch_ms"] = (time.time() + failure["retry_not_before_seconds"]) * 1000
            write_json(previous_attempt / "evidence.json", recovered)
            return recovered
        matching = [attempt for attempt in previous
            if read_json(attempt / "collection-intent.json")["identity_sha256"] == digest]
        qualified_passes = []
        for attempt in matching:
            evidence_file = attempt / "evidence.json"
            if not evidence_file.exists():
                continue
            cached = read_json(evidence_file)
            if _shared_cached_pass_is_currently_qualified(attempt, cached, commands, identity):
                qualified_passes.append(cached)
        if qualified_passes:
            # 完整树、环境、依赖和日志仍匹配时直接复用，避免后一次失败替换已交付事实。
            return qualified_passes[-1]
        budget_exhausted = registered_machine_command_budget_is_exhausted(
            run_directory, identity["runtime_environment"], commands)
        if matching and (matching[-1] / "evidence.json").exists():
            cached = read_json(matching[-1] / "evidence.json")
            failure = cached.get("failure")
            retry_blocked = bool(failure) and (not failure["maximum_same_configuration_retries"]
                or time.time() * 1000 < cached.get("retry_not_before_epoch_ms", float("inf")))
            if cached.get("status") == "passed":
                if budget_exhausted:
                    # 预算已用完且上面没有当前合格原件时不再派发。未证明依赖的历史通过只保留原件，不交回为当前事实。
                    _reject_shared_command_log_mismatch(matching[-1], cached, commands)
                    return None
            elif budget_exhausted or retry_blocked:
                _reject_shared_command_log_mismatch(matching[-1], cached, commands)
                return cached
        elif budget_exhausted:
            return None
        # 没有依赖内容证明时跨接续重新采集；同一次执行内只采集一份事实。
        proven_dependencies = identity["runtime_environment"]["registered_dependency_sources"]["identity_proven"]
        attempt = root / f"attempt-{len(previous) + 1:03d}"
        workspace = attempt / "workspace"
        attempt.mkdir()
        write_json(attempt / "collection-intent.json", {"identity": identity, "identity_sha256": digest,
            "maximum_attempts_per_recovery_input": 3, "status": "registered"})
        git(Path(record["repo"]), "worktree", "add", "--detach", str(workspace), record["candidate_commit"])
        # 命令继承同一单飞锁；controller 崩溃后，接续必须等原命令退出才能重新采集。
        prepare_isolated_workspace(run_directory, workspace, plan.get("preparation_commands", []), attempt,
                                   environment=environment,
                                   inherited_lock_descriptors=(collection_lock.fileno(),))
        preparation_file = attempt / "preparation-created-untracked-paths.json"
        excluded = read_json(preparation_file)["paths"] if preparation_file.exists() else None
        dependency_before = prepared_workspace_dependency_identity(workspace, excluded)
        write_json(attempt / "prepared-workspace-dependency-identity-before.json", dependency_before)
        result = execute_registered_commands(run_directory, workspace, commands, attempt / "commands", "shared_machine_evidence",
                                             environment=environment, excluded_preparation_paths=excluded,
                                             expected_source_tree=identity["tested_tree"],
                                             inherited_lock_descriptors=(collection_lock.fileno(),))
        evidence = {**identity, **result, "identity_sha256": digest, "cwd": str(workspace),
                    "host_recovery_inputs_after": registered_host_recovery_inputs(run_directory, plan),
                    "dependency_identity_proven": proven_dependencies, "original": str(attempt / "evidence.json")}
        dependency_after = prepared_workspace_dependency_identity(workspace, excluded)
        evidence["dependency_identity_proven"] = bool(proven_dependencies and dependency_before["identity_proven"] and dependency_before == dependency_after)
        for command in evidence["commands"]:
            command["prepared_workspace_dependency_identity"] = dependency_before
        if evidence["host_recovery_inputs_after"] != identity["runtime_environment"]:
            evidence["status"] = "environment_changed"
        if result["status"] == "failed":
            from .role_failure_recovery import classify_role_failure
            failed = Path(result["failed_command_directory"])
            failure = classify_role_failure("", (failed / "stderr.txt").read_text(errors="replace"), result["commands"][-1]["returncode"], "text")
            evidence["failure"] = failure
            if failure["maximum_same_configuration_retries"]:
                evidence["retry_not_before_epoch_ms"] = (time.time() + failure["retry_not_before_seconds"]) * 1000
        write_json(attempt / "evidence.json", evidence)
        return evidence


def prepare_registered_machine_command_environment(run_directory: Path) -> dict:
    from .review_recovery_evidence_observation import isolated_registered_machine_command_environment
    environment = isolated_registered_machine_command_environment(run_directory / "machine-evidence-isolated-environment")
    for key in ("HOME", "CODEX_HOME", "CODEX_SQLITE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        directory = Path(environment[key])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise ValueError("隔离环境目录的实体或归属不匹配")
        if key == "TMPDIR" and directory.stat().st_mode & 0o077:
            raise ValueError("宿主命令临时 IPC 目录必须为本用户私有")
    return environment


def registered_machine_command_budget_is_exhausted(run_directory: Path, recovery_inputs: dict, commands: list[list[str]]) -> bool:
    counts = {json.dumps(command): 0 for command in commands}
    for pattern in ("machine-evidence/attempt-*/collection-intent.json",
                    "reviewer-machine-evidence-requests/*/attempt-*/collection-intent.json"):
        for path in run_directory.glob(pattern):
            intent = read_json(path)
            if intent.get("status") == "qualified_existing_facts":
                continue
            inputs = intent.get("host_recovery_inputs", intent.get("identity", {}).get("runtime_environment"))
            if inputs != recovery_inputs:
                continue
            dispatched = {json.dumps(argv) for argv in _dispatched_registered_command_argvs(path.parent)}
            for key in counts:
                if key in dispatched:
                    counts[key] += 1
    return any(count >= 3 for count in counts.values())


def prepared_workspace_dependency_identity(workspace: Path, preparation_created_paths: list[str] | None = None) -> dict:
    from .review_recovery_evidence_observation import file_sha256, canonical_json_material_sha256
    paths = set(git(workspace, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").decode().split("\0"))
    paths.update(preparation_created_paths or [])
    files = []
    proven = True
    for relative in sorted(paths - {""}):
        path = workspace / relative
        if path.is_symlink() or not path.is_file():
            proven = False
            files.append([relative, "dependency_content_unproven"])
            continue
        resolved = path.resolve()
        physical_source = str(resolved.relative_to(workspace.resolve())) if resolved.is_relative_to(workspace.resolve()) else str(resolved)
        files.append([relative, physical_source, path.stat().st_mode & 0o777, file_sha256(path)])
    return {"identity_sha256": canonical_json_material_sha256(files), "identity_proven": proven,
        "preparation_created_paths": sorted(preparation_created_paths or [])}


def _terminate_retained_collection_commands(directory: Path, reason: str) -> None:
    observations = []
    identity_check_deadline = time.monotonic() + 0.2
    execution_files = [path for pattern in ('commands/command-*/execution.json',
        'preparation/command-*/execution.json', 'attempt-*/commands/command-*/execution.json',
        'attempt-*/preparation/command-*/execution.json') for path in directory.glob(pattern)]
    for execution_file in execution_files:
        execution = read_json(execution_file)
        if execution.get("status") != "running":
            continue
        identity = execution.get("process_identity")
        status = "unknown"
        try:
            remaining_identity_check_seconds = identity_check_deadline - time.monotonic()
            if identity and remaining_identity_check_seconds > 0:
                current = process_start_identity(identity["pid"], timeout_seconds=remaining_identity_check_seconds)
                if current is None:
                    status = "process_group_exit_unproven"
                elif not owned_process_start_time_and_group_match(current, identity):
                    status = "identity_changed_no_signal"
                else:
                    os.killpg(identity["pid"], signal.SIGTERM if (execution_file.parent / 'command-supervision.json').exists() else signal.SIGKILL)
                    status = "termination_requested"
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
        observations.append({"execution_file": str(execution_file), "process_identity": identity, "status": status})
    write_json(directory / "collection-recovery.json", {"reason": reason, "processes": observations,
        "observed_at": time.time(), "recovery_condition": "owned_process_identity_or_exit_evidence_changed"})


@contextmanager
def _cancellable_incomplete_fixer_collection_lock(run_directory: Path, directory: Path):
    with (directory / "collection.lock").open("a+b") as handle:
        while True:
            try:
                ensure_execution_not_revoked(run_directory)
            except ExecutionRevoked:
                _terminate_retained_collection_commands(directory, "execution_revoked")
                raise
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def collect_incomplete_fixer_machine_validation(run_directory: Path, record: dict, candidate_commit: str) -> Path | None:
    plan = verified_execution_plan(run_directory, record)
    commands = plan.get("validation_commands", [])
    if not commands:
        return None
    directory = run_directory / "incomplete-fixer-machine-validation" / f"round-{record['semantic_repair_rounds_used']:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    with _cancellable_incomplete_fixer_collection_lock(run_directory, directory) as collection_lock:
        ensure_execution_not_revoked(run_directory)
        ensure_previous_supervised_commands_exited(run_directory, run_directory / 'incomplete-fixer-machine-validation')
        repo = Path(record["repo"])
        tested_tree = git_text(repo, "rev-parse", candidate_commit + "^{tree}")
        intent_file = directory / "collection-intent.json"
        evidence_file = directory / "evidence.json"
        recovery_identity = runtime_environment_identity()
        recovery_identity['shared_machine_evidence_recovery_sha256'] = record.get('shared_machine_evidence_recovery_sha256')
        recovery_identity['command_executables'] = [
            {'argv': command, 'sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None}
            for command in [*plan.get('preparation_commands', []), *commands]
            for executable in [shutil.which(command[0])]]
        recovery_sha256 = hashlib.sha256(json.dumps(recovery_identity, sort_keys=True).encode()).hexdigest()
        intent = read_json(intent_file) if intent_file.exists() else {
            "tested_tree": tested_tree, "tested_commit": candidate_commit, "collection_attempt_count": 0}
        if intent["tested_tree"] != tested_tree:
            if record['semantic_repair_rounds_used'] >= plan['maximum_semantic_repair_rounds']:
                record['repair_budget_exhausted'] = True
                write_json(run_directory / 'run.json', record)
                return None
            record['semantic_repair_rounds_used'] += 1
            write_json(run_directory / 'run.json', record)
            append_stage_event(run_directory, 'semantic_repair_tree_changed', tested_tree=tested_tree,
                semantic_repair_round=record['semantic_repair_rounds_used'])
            return collect_incomplete_fixer_machine_validation(run_directory, record, candidate_commit)
        if intent.get("evidence_sha256"):
            accepted_evidence_file = Path(intent['published_evidence_path'])
            if not accepted_evidence_file.exists() or hashlib.sha256(accepted_evidence_file.read_bytes()).hexdigest() != intent["evidence_sha256"]:
                raise RuntimeError("保留修复树的机器补证原件缺失或摘要不符；恢复原件后接续")
            previous_evidence = read_json(accepted_evidence_file)
            unchanged_recovery_evidence = intent.get('recovery_sha256') == recovery_sha256
            recovery_probe_due = (intent.get('retry_not_before') is not None
                and time.time() >= intent['retry_not_before'] and not intent.get('unchanged_recovery_probe_used'))
            reusable_pass = previous_evidence['status'] == 'passed' and previous_evidence.get('dependency_identity_proven') is True
            if unchanged_recovery_evidence and (reusable_pass or previous_evidence['status'] != 'passed' and not recovery_probe_due):
                return accepted_evidence_file
            if previous_evidence['status'] == 'candidate_changed':
                return accepted_evidence_file
            if unchanged_recovery_evidence:
                intent['unchanged_recovery_probe_used'] = True
        # 失败后仅在恢复证据变化时重采；中断与环境恢复共享有界次数，等待时间不消耗重试资格。
        if intent["collection_attempt_count"] >= 3:
            return None
        intent["collection_attempt_count"] += 1
        intent['recovery_sha256'] = recovery_sha256
        intent.pop('evidence_sha256', None)
        write_json(intent_file, intent)
        attempt_directory = directory / f"attempt-{intent['collection_attempt_count']:03d}"
        attempt_directory.mkdir()
        workspace = attempt_directory / "workspace"
        git(repo, "worktree", "add", "--detach", str(workspace), intent["tested_commit"])
        prepare_isolated_workspace(run_directory, workspace, plan.get("preparation_commands", []), attempt_directory,
            inherited_lock_descriptors=(collection_lock.fileno(),))
        preparation_paths = attempt_directory / "preparation-created-untracked-paths.json"
        excluded = read_json(preparation_paths)["paths"] if preparation_paths.exists() else None
        before = capture_worktree_tree(workspace, ["."], attempt_directory, excluded_preparation_paths=excluded)
        result = {"status": "candidate_changed", "commands": []}
        if before == tested_tree:
            result = execute_registered_commands(run_directory, workspace, commands, attempt_directory / "commands",
                "incomplete_fixer_machine_validation", inherited_lock_descriptors=(collection_lock.fileno(),),
                excluded_preparation_paths=excluded, expected_source_tree=tested_tree)
        after = capture_worktree_tree(workspace, ["."], attempt_directory, excluded_preparation_paths=excluded)
        if after != tested_tree:
            result["status"] = "candidate_changed"
        evidence = {**result, "tested_tree": tested_tree, "tested_commit": intent["tested_commit"],
            "source_tree_before": before, "source_tree_after": after, "workspace": str(workspace),
            "runtime_environment": runtime_environment_identity(), "dependency_identity_proven": False,
            "role_outcome_status": "incomplete", "semantic_repair_round": record["semantic_repair_rounds_used"],
            "collection_attempt": intent['collection_attempt_count'], "original": str(attempt_directory / 'evidence.json')}
        write_json(attempt_directory / 'evidence.json', evidence)
        if intent['collection_attempt_count'] == 1:
            write_json(evidence_file, evidence)
        intent['published_evidence_path'] = str(evidence_file if intent['collection_attempt_count'] == 1 else attempt_directory / 'evidence.json')
        intent["evidence_sha256"] = hashlib.sha256(Path(intent['published_evidence_path']).read_bytes()).hexdigest()
        if result['status'] == 'failed':
            from .role_failure_recovery import classify_role_failure
            failure_directory = Path(result['failed_command_directory'])
            failure = classify_role_failure('', (failure_directory / 'stderr.txt').read_text(errors='replace'),
                result['commands'][-1]['returncode'], 'text')
            intent['recovery'] = {'trigger': 'environment_changed_or_bounded_probe_due', 'action': 'resume_same_run',
                'maximum_unchanged_retries': 1, 'failure': failure}
            intent['retry_not_before'] = (time.time() + max(5, failure['retry_not_before_seconds'])
                if failure['maximum_same_configuration_retries'] else None)
        else:
            intent.pop('retry_not_before', None)
            intent.pop('recovery', None)
        write_json(intent_file, intent)
        return Path(intent['published_evidence_path'])
