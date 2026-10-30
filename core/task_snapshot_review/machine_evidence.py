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
        REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE, REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT, native_process_identity, owned_process_snapshot,
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
            if facts.get('status') == 'completed':
                continue
            if (facts.get('process_ownership_scope') != REGISTERED_COMMAND_PROCESS_OWNERSHIP_SCOPE or
                    facts.get('command_process_lifetime_contract') != REGISTERED_COMMAND_PROCESS_LIFETIME_CONTRACT):
                raise RuntimeError('历史未完成命令缺少受控进程契约，不能自动补写为已退出')
            if facts.get('command_execution_started') is False and not facts.get('command_pid'):
                observation.update({'status': 'not_started', 'native_launch_error': facts['command_native_launch_error']})
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
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            recorded.update(getattr(error, 'observed_owned_processes', {}))
            observation.update({'error': str(error), 'owned_processes': list(recorded.values())})
        write_json(reconciliation_file, observation)
        if observation['status'] != 'exited':
            error = WorkspacePreparationFailed(reconciliation_file)
            error.recovery = {'trigger': observation['recovery_condition'], 'action': 'resume_same_run',
                'equivalent_sources': ['native_process_start_identity', 'native_descendant_ownership_snapshot'],
                'maximum_unchanged_retries': 1}
            raise error


def execute_registered_commands(run_directory: Path, workspace: Path, commands: list[list[str]],
                                artifacts: Path, purpose: str, *, environment: dict | None = None,
                                inherited_lock_descriptors: tuple[int, ...] = (),
                                excluded_preparation_paths: list[str] | None = None,
                                expected_source_tree: str | None = None) -> dict:
    plan = verified_execution_plan(run_directory)
    registered = [*plan.get("preparation_commands", []), *plan.get("machine_evidence_commands", []), *plan.get("validation_commands", [])]
    if plan and any(command not in registered for command in commands):
        raise ValueError("执行器只能代跑启动时登记的命令")
    records = []
    for index, command in enumerate(commands):
        ensure_execution_not_revoked(run_directory)
        verified_execution_plan(run_directory)
        command_directory = artifacts / f"command-{index + 1:03d}"
        command_directory.mkdir(parents=True, exist_ok=True)
        source_tree_before = (capture_worktree_tree(workspace, ["."], command_directory,
            excluded_preparation_paths=excluded_preparation_paths)
            if purpose in {"shared_machine_evidence", "incomplete_fixer_machine_validation"} else None)
        record = {"argv": command, "cwd": str(workspace), "started_at": time.time(), "status": "running"}
        if source_tree_before is not None:
            record["source_tree_before"] = source_tree_before
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
                returncode = wait_for_owned_process(process, run_directory, timeout=None)
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


def collect_shared_machine_evidence(run_directory: Path, record: dict, plan: dict) -> dict | None:
    commands = plan.get("machine_evidence_commands", [])
    if not commands:
        return None
    with exclusive_lock(run_directory / "machine-evidence.lock") as collection_lock:
        root = run_directory / "machine-evidence"
        root.mkdir(exist_ok=True)
        ensure_previous_supervised_commands_exited(run_directory, root)
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
