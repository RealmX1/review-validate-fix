"""精确 run 的撤销、所属进程身份、阶段事实及业务处置。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

from .persistent_artifacts import exclusive_lock, read_json, write_json


class ExecutionRevoked(InterruptedError):
    pass


def normalized_execution_plan(plan: dict) -> dict:
    allowed = {"fix", "validation_commands", "preparation_commands", "machine_evidence_commands",
               "maximum_semantic_repair_rounds", "machine_context", "implementation_plan"}
    if not isinstance(plan, dict) or set(plan) - allowed:
        raise ValueError("启动材料包含未知字段")
    result = {"fix": False, "validation_commands": [], "preparation_commands": [], "machine_evidence_commands": [],
              "maximum_semantic_repair_rounds": 3, **plan}
    if type(result["fix"]) is not bool or type(result["maximum_semantic_repair_rounds"]) is not int or result["maximum_semantic_repair_rounds"] < 1:
        raise ValueError("修复意图必须是布尔值，语义修复轮数必须为正整数")
    for key in ("validation_commands", "preparation_commands", "machine_evidence_commands"):
        if not isinstance(result[key], list) or any(not isinstance(command, list) or not command or
                not all(isinstance(argument, str) and "\0" not in argument for argument in command) for command in result[key]):
            raise ValueError(f"{key} 必须是 argv 数组列表")
    if "implementation_plan" in result and (not isinstance(result["implementation_plan"], list) or
            not all(isinstance(item, str) and item.strip() for item in result["implementation_plan"])):
        raise ValueError("implementation_plan 必须是非空字符串条目列表")
    return json.loads(json.dumps(result))


def execution_is_revoked(run_directory: Path) -> bool:
    if (run_directory / "execution-revocation.json").exists():
        return True
    record = read_json(run_directory / "run.json")
    revocation_file = record.get("command_execution_revocation_file")
    return bool(revocation_file and Path(revocation_file).exists())


def ensure_execution_not_revoked(run_directory: Path) -> None:
    if execution_is_revoked(run_directory):
        raise ExecutionRevoked("所属执行已持久撤销；不能派发、恢复、应用补丁或投递结果")


def process_start_identity(pid: int, *, timeout_seconds: float = 5) -> dict | None:
    completed = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid=", "-o", "command="],
                               capture_output=True, text=True, timeout=timeout_seconds)
    if completed.returncode == 1 and not completed.stdout.strip():
        return None
    if completed.returncode or not completed.stdout.strip():
        raise RuntimeError("进程启动身份查询不可用，不能据此推断进程退出")
    return {"pid": pid, "start_and_command": " ".join(completed.stdout.split())}


def owned_process_start_time_and_group_match(current: dict, recorded: dict) -> bool:
    if current.get("pid") != recorded.get("pid"):
        return False
    # exec 只改变命令；信号资格绑定已观察的启动时刻与进程组，无法解析时保留未知。
    pattern = r"^(\S+\s+\S+\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(\d+)(?:\s|$)"
    current_start = re.match(pattern, current.get("start_and_command") or "")
    recorded_start = re.match(pattern, recorded.get("start_and_command") or "")
    return bool(current_start and recorded_start and current_start.groups() == recorded_start.groups())


def register_owned_process(run_directory: Path, pid: int, purpose: str, *, owns_process_group: bool = False) -> None:
    try:
        identity = process_start_identity(pid)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        identity = {"pid": pid, "start_and_command": None, "identity_evidence_unavailable": str(error)}
    if identity is None:
        return
    with exclusive_lock(run_directory / "owned-processes.lock"):
        path = run_directory / "owned-processes.json"
        records = read_json(path).get("processes", []) if path.exists() else []
        records.append({**identity, "purpose": purpose, "owns_process_group": owns_process_group,
                        "registered_at": time.time(), "is_active_registration": True})
        write_json(path, {"processes": records})


def finish_owned_process_registration(run_directory: Path, pid: int, purpose: str) -> None:
    with exclusive_lock(run_directory / "owned-processes.lock"):
        path = run_directory / "owned-processes.json"
        if not path.exists():
            return
        records = read_json(path)["processes"]
        for record in records:
            if record["pid"] == pid and record["purpose"] == purpose:
                record.update({"is_active_registration": False, "finished_at": time.time()})
        write_json(path, {"processes": records})


def persist_execution_revocation(run_directory: Path, reason: str) -> None:
    with exclusive_lock(run_directory / "execution-revocation.lock"):
        path = run_directory / "execution-revocation.json"
        if not path.exists():
            write_json(path, {"reason": reason, "revoked_at": time.time()})


def register_supervised_command_directory(run_directory: Path, command_directory: Path) -> None:
    directory = command_directory.resolve()
    relative = directory.relative_to(run_directory.resolve())
    if 'workspace' in relative.parts or not relative.parts:
        raise ValueError('命令监督目录必须位于 run 控制目录，不能位于候选源码中')
    with exclusive_lock(run_directory / 'registered-supervised-command-directories.lock'):
        registry = run_directory / 'registered-supervised-command-directories.json'
        directories = read_json(registry)['command_directories'] if registry.exists() else []
        if str(directory) not in directories:
            write_json(registry, {'command_directories': [*directories, str(directory)]})


def revoke_execution(run_directory: Path, reason: str) -> dict:
    persist_execution_revocation(run_directory, reason)
    # 监督者已退出也不能丢弃跨 session 后代；原生启动身份重新核验后才发送信号。
    from .retained_command_supervisor import terminate_supervised_command_descendants
    supervised_descendants = []
    registry = run_directory / 'registered-supervised-command-directories.json'
    try:
        directories = read_json(registry)['command_directories'] if registry.exists() else []
        for registered_directory in directories:
            try:
                directory = Path(registered_directory).resolve()
                relative = directory.relative_to(run_directory.resolve())
                if 'workspace' in relative.parts or not relative.parts:
                    raise ValueError('已登记监督目录指向候选源码或 run 根目录')
                if (directory / 'command-supervision.json').exists():
                    supervised_descendants.extend(terminate_supervised_command_descendants(directory))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                supervised_descendants.append({'command_directory': registered_directory,
                    'cancellation_status': 'unknown', 'error': str(error)})
    except (OSError, ValueError, KeyError, TypeError) as error:
        supervised_descendants.append({'cancellation_status': 'unknown', 'error': str(error)})
    owned = run_directory / "owned-processes.json"
    observations = []
    for process in read_json(owned).get("processes", []) if owned.exists() else []:
        if process["pid"] == os.getpid() or process.get("is_active_registration") is not True:
            continue
        try:
            if not process.get("start_and_command"):
                raise RuntimeError("缺少原进程身份，保留取消未知并等待等价证据")
            current = process_start_identity(process["pid"])
            if current is None:
                status = "local_process_exited"
            elif not owned_process_start_time_and_group_match(current, process):
                status = "identity_changed_no_signal"
            else:
                (os.killpg if process["owns_process_group"] else os.kill)(process["pid"], signal.SIGTERM)
                status = "termination_requested"
        except (OSError, subprocess.SubprocessError, RuntimeError):
            status = "unknown"
        observations.append({**process, "local_cancellation_status": status, "native_cancellation_status": "unknown"})
    pending = [item for item in observations if item["local_cancellation_status"] == "termination_requested"]
    deadline = time.monotonic() + 2
    while pending:
        remaining = []
        for item in pending:
            try:
                current = process_start_identity(item["pid"])
                if current is None:
                    item["local_cancellation_status"] = "local_process_exited"
                elif not owned_process_start_time_and_group_match(current, item):
                    item["local_cancellation_status"] = "identity_changed_no_signal"
                elif time.monotonic() >= deadline:
                    (os.killpg if item["owns_process_group"] else os.kill)(item["pid"], signal.SIGKILL)
                    item["local_cancellation_status"] = "force_termination_requested"
                else:
                    remaining.append(item)
            except (OSError, subprocess.SubprocessError, RuntimeError):
                item["local_cancellation_status"] = "unknown"
        pending = remaining
        if pending:
            time.sleep(0.05)
    evidence = {"status": "cancelled", "disposition": "cancelled", "processes": observations,
                "supervised_descendants": supervised_descendants,
                "recovery": {"trigger": "owned_process_identity_or_native_receipt_changed", "action": "reconcile_cancellation",
                             "maximum_unchanged_retries": 1}, "observed_at": time.time()}
    write_json(run_directory / "cancellation-observations.json", evidence)
    return evidence


def wait_for_owned_process(process: subprocess.Popen, run_directory: Path, *, timeout: float | None = None) -> int:
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        ensure_execution_not_revoked(run_directory)
        observe_owned_process_group_descendants(run_directory, process.pid)
        try:
            return process.wait(timeout=min(0.2, max(0.001, deadline - time.monotonic())) if deadline else 0.2)
        except subprocess.TimeoutExpired:
            if deadline and time.monotonic() >= deadline:
                raise


def observe_owned_process_group_descendants(run_directory: Path, group_id: int) -> None:
    try:
        table = subprocess.run(["ps", "-axo", "pid=,pgid=,lstart=,command="], capture_output=True, text=True, timeout=5)
        if table.returncode:
            return
        descendants = []
        for line in table.stdout.splitlines():
            fields = line.split(None, 7)
            if len(fields) == 8 and fields[1] == str(group_id) and fields[0] != str(group_id):
                descendants.append({"pid": int(fields[0]), "start_and_command": " ".join([*fields[2:7], fields[1], *fields[7].split()]),
                                    "purpose": "owned_process_group_descendant", "owns_process_group": False,
                                    "registered_at": time.time(), "is_active_registration": True})
        if not descendants:
            return
        with exclusive_lock(run_directory / "owned-processes.lock"):
            path = run_directory / "owned-processes.json"
            records = read_json(path).get("processes", []) if path.exists() else []
            known = {(item["pid"], item["start_and_command"]) for item in records}
            added = [item for item in descendants if (item["pid"], item["start_and_command"]) not in known]
            if added:
                write_json(path, {"processes": [*records, *added]})
    except (OSError, subprocess.SubprocessError):
        return


def append_stage_event(run_directory: Path, event: str, **facts) -> None:
    with exclusive_lock(run_directory / "stage-events.lock"):
        path = run_directory / "stage-events.json"
        events = read_json(path).get("events", []) if path.exists() else []
        events.append({"event": event, "observed_at": time.time(), **facts})
        write_json(path, {"events": events})


def runtime_environment_identity() -> dict:
    # 不将凭据原文写盘；身份不同必须重新采集，身份缺证不能宣称跨工作树可复用。
    return {"platform": platform.platform(), "python": sys.version, "python_executable": str(Path(sys.executable).resolve()),
            "environment_sha256": hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest()}


def record_closeout_report(run_directory: Path, record: dict, plan: dict, report_file: Path) -> None:
    report = read_json(report_file)
    items = report.get("implementation_items", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("description"), str)
            or item.get("status") not in {"completed", "deferred", "not_completed"} or not item.get("evidence") for item in items):
        raise ValueError("收尾报告需要逐项 implementation 状态与证据")
    if not isinstance(report.get("work_recap"), str) or not report["work_recap"].strip():
        raise ValueError("收尾报告需要具体工作 recap")
    expected = plan.get("implementation_plan")
    supplied = [item["description"] for item in items]
    if len(supplied) != len(set(supplied)) or (expected is not None and set(supplied) != set(expected)):
        raise ValueError("收尾报告必须覆盖冻结计划的全部条目，且不能重复")
    digest = hashlib.sha256(report_file.read_bytes()).hexdigest()
    original = run_directory / "closeout-report-originals-by-sha256" / f"{digest}.json"
    write_json(original, report)
    record["implementation_plan_completion"] = ("complete" if all(item["status"] == "completed" for item in items) else "partial") if items else "unknown"
    record["implementation_plan_evidence"] = items
    record["work_recap"] = report["work_recap"]
    record["closeout_report_original"] = str(original)
    write_json(run_directory / "run.json", record)


def review_closeout(record: dict, run_directory: Path) -> dict:
    result_files = sorted((run_directory / "fixer").glob("attempt-*/result.json"))
    dispositions = read_json(result_files[-1]).get("dispositions", []) if result_files else []
    elevations = [item for item in dispositions if item.get("status") == "elevated"]
    disruptive = any(item.get("elevation_impact") in {"rollback", "broad_rewrite"} for item in elevations)
    successful = record["status"] in {"ready_to_commit", "completed"}
    return {"automatic_commit_allowed": successful and not disruptive and not execution_is_revoked(run_directory), "elevations": elevations,
            "implementation_plan_completion": record.get("implementation_plan_completion", "unknown"),
            "implementation_plan_evidence": record.get("implementation_plan_evidence", []),
            "work_recap": {"summary": record.get("work_recap"), "scope_paths": record.get("effective_scope_paths", record["scope_paths"]),
                           "finding_count": record.get("finding_count", 0),
                           "semantic_repair_rounds_used": record.get("semantic_repair_rounds_used", 0)},
            "validation": read_json(run_directory / "validation.json") if (run_directory / "validation.json").exists() else "unknown",
            "commit": record.get("commit"), "base_sync": "not_requested"}


def publish_business_result(run_directory: Path, result: dict) -> dict:
    record = read_json(run_directory / "run.json")
    closeout = review_closeout(record, run_directory)
    if execution_is_revoked(run_directory):
        disposition = "cancelled"
    elif (record.get("repair_budget_exhausted")
          or any(read_json(path).get("recovery", {}).get("category") == "deadline_exhausted" for path in [
              *run_directory.glob("*/attempt-*/outcome.json"), *run_directory.glob("*/deadline-exhausted.json")])
          or any(item.get("elevation_impact") in {"rollback", "broad_rewrite"} for item in closeout["elevations"])):
        disposition = "agent_judgment_required"
    elif result["status"] in {"ready_to_commit", "completed"}:
        disposition = "agent_review_ready"
    elif result["status"] in {"fix_application_pending", "candidate_changed", "findings_unresolved"}:
        disposition = "agent_judgment_required"
    else:
        disposition = "machine_evidence_pending"
    result = {**result, "disposition": disposition, "closeout": closeout}
    if disposition == "machine_evidence_pending":
        attempts = sorted(run_directory.glob("*/attempt-*/request.json"))
        paths = [str(path.parent / "outcome.json") for path in attempts]
        paths.extend(str(path.parent / filename) for path in attempts for filename in
                     ("stdout.txt", "stderr.txt", "model-invocation.json", "progress.json"))
        machine_retry_times = []
        eligibility_recovery_limits = []
        eligibility_files = ([run_directory / 'reviewer-candidate-eligibility.json',
                              *run_directory.glob('reviewer-*/reviewer-eligibility.json')]
                             if result['status'] == 'review_incomplete' else
                             [run_directory / 'fixer/reviewer-eligibility.json'] if result['status'] == 'fix_incomplete' else [])
        for eligibility_file in eligibility_files:
            if not eligibility_file.exists():
                continue
            paths.append(str(eligibility_file))
            payload = read_json(eligibility_file)
            for eligibility in payload.get('observations', [payload]):
                paths.extend(eligibility.get('credential_evidence_paths', []))
                paths.extend(eligibility.get('configuration_evidence_paths', []))
                if eligibility.get('evidence_path'):
                    paths.append(eligibility['evidence_path'])
                if eligibility.get('account_status_evidence_path'):
                    paths.append(eligibility['account_status_evidence_path'])
                if eligibility.get('can_dispatch') is False:
                    recovery = eligibility.get('recovery', {})
                    eligibility_recovery_limits.append(recovery.get('maximum_unchanged_retries', 0))
                    if recovery.get('retry_not_before_epoch_ms'):
                        machine_retry_times.append(recovery['retry_not_before_epoch_ms'])
        if eligibility_recovery_limits:
            for deadline_file in run_directory.glob('*/role-deadline.json'):
                deadline = read_json(deadline_file)['deadline_at'] * 1000
                if deadline > time.time() * 1000:
                    machine_retry_times.append(deadline)
        for intent in run_directory.glob("incomplete-fixer-machine-validation/round-*/collection-intent.json"):
            paths.extend([str(intent), str(intent.parent / "evidence.json")])
            collection = read_json(intent)
            if (collection.get('retry_not_before') and not collection.get('unchanged_recovery_probe_used')
                    and collection.get('collection_attempt_count', 0) < 3):
                machine_retry_times.append(collection['retry_not_before'] * 1000)
        configurations = [read_json(path).get("config", {}).get("configuration_file_path") for path in attempts]
        paths.extend(str(Path(evidence).expanduser()) for path in attempts for evidence in read_json(path).get("config", {}).get("credential_evidence_paths", []))
        paths.extend(path for path in configurations if path)
        paths.extend(str(run_directory / filename) for filename in ("reviewer-configurations.json", "fixer-configuration.json", "context-response-originals-by-sha256"))
        latest_attempts = {path.parent.parent: path.parent for path in attempts}
        failures = [read_json(attempt / "outcome.json") for attempt in latest_attempts.values() if (attempt / "outcome.json").exists()]
        retry_times = machine_retry_times + [item["retry_not_before"] * 1000 for item in failures if item.get("retry_not_before") and
                       item.get("recovery", {}).get("category") == "workspace_preparation"]
        result["recovery"] = {**record.get("recovery", {"missing_evidence": result["status"],
            "equivalent_sources": ["role_native_output", "registered_configuration", "validation_command_logs"],
            "trigger": "evidence_or_configuration_changed", "action": "resume_same_run", "maximum_unchanged_retries": 1}),
            **({'maximum_unchanged_retries': max(eligibility_recovery_limits)} if eligibility_recovery_limits else {}),
            "evidence_paths": sorted(set(paths)), **({"retry_not_before_epoch_ms": min(retry_times)} if retry_times else {})}
    write_json(run_directory / "business-result.json", result)
    return result
