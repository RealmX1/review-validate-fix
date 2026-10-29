"""角色进程独立执行、持久输出与有界失败恢复。"""
from __future__ import annotations

import json
import hashlib
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import harness_cli_adapters as cli
from .persistent_artifacts import exclusive_lock, git, git_text, read_json, write_bytes, write_json
from .execution_lifecycle import ExecutionRevoked, ensure_execution_not_revoked, register_owned_process, wait_for_owned_process
from .role_failure_recovery import classify_role_failure, native_invocation_facts
from .machine_evidence import WorkspacePreparationFailed, prepare_isolated_workspace
from .reviewer_account_eligibility import reviewer_execution_environment, reviewer_eligibility, record_formal_invocation_result


def process_lock_is_held(attempt_directory: Path) -> bool:
    try:
        with exclusive_lock(attempt_directory / "execution.lock", blocking=False):
            return False
    except RuntimeError:
        return True


def role_environment(request: dict) -> dict:
    environment = reviewer_execution_environment(request['config'])
    environment.update({"RVF_SUPPRESS_STOP_HOOK": "1", "RVF_ROLE_INPUT": request["input_file"],
                        "RVF_REVIEW_RESULT": request["result_file"]})
    return environment


def _role_redactions(request: dict, environment: dict) -> dict:
    return {name: environment[name] for name in request["config"].get("credential_environment_variable_names", [])
            if environment.get(name)}


def _redact_role_artifacts(attempt_directory: Path, request: dict, redactions: dict) -> None:
    # 成功、失败及恢复都清理同一组原始文件，不能只脱敏 stdout。
    paths = {attempt_directory / "stdout.txt", attempt_directory / "stderr.txt",
             attempt_directory / "result.json", attempt_directory / "model-invocation.json", Path(request["result_file"]),
             *attempt_directory.glob("preparation/command-*/stdout.txt"), *attempt_directory.glob("preparation/command-*/stderr.txt")}
    for path in paths:
        if path.exists():
            original = path.read_text(errors="replace")
            redacted = cli.redact_configured_environment_variable_values_in_json_lines(original, redactions)
            if redacted != original:
                write_bytes(path, redacted.encode())
    outcome_file = attempt_directory / "outcome.json"
    if outcome_file.exists():
        outcome = read_json(outcome_file)
        if isinstance(outcome.get("error"), str):
            redacted = cli.redact_configured_environment_variable_values(outcome["error"], redactions)
            if redacted != outcome["error"]:
                outcome["error"] = redacted
                write_json(outcome_file, outcome)


def extract_role_result(output: str, output_format: str) -> dict:
    extractors = {"text": lambda value: value, "codex_json": cli.extract_codex_json_result,
                  "claude_stream_json": cli.extract_claude_stream_result,
                  "cursor_stream_json": cli.extract_claude_stream_result,
                  "kimi_stream_json": cli.extract_kimi_stream_json_result,
                  "oh_my_pi_json": cli.extract_oh_my_pi_json_result}
    if output_format not in extractors:
        raise ValueError(f"不支持的输出格式：{output_format}")
    text = extractors[output_format](output).strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("角色结果必须是 JSON object")
    return payload


def validate_role_result(payload: dict, role: str, expected_findings: list[dict]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("角色结果必须是 JSON object")
    if payload.get("status") not in ("completed", "incomplete"):
        raise ValueError("角色结果缺少 completed/incomplete 状态")
    if role == "reviewer":
        findings = payload.get("findings")
        if not isinstance(findings, list) or not isinstance(payload.get("audit_summary"), str) or not payload["audit_summary"].strip():
            raise ValueError("reviewer 结果需要 findings 列表及实际审计摘要")
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("message"), str) or not finding["message"].strip():
                raise ValueError("每条 finding 需要可复核的问题说明")
            if "problem_key" in finding and not isinstance(finding["problem_key"], str):
                raise ValueError("finding problem_key 必须是字符串")
        if payload.get("requests") and payload["status"] == "completed":
            raise ValueError("仍有未解决请求的 reviewer 不能算完成")
    else:
        if payload.get("completion_blocker") is not None and (
                payload["status"] != "incomplete" or payload["completion_blocker"] != "registered_machine_validation"):
            raise ValueError("机器补证请求只允许 incomplete fixer 使用 registered_machine_validation")
        dispositions = payload.get("dispositions")
        if not isinstance(dispositions, list):
            raise ValueError("fixer 结果需要 dispositions 列表")
        identities = [item.get("finding_id") for item in dispositions if isinstance(item, dict)]
        if payload["status"] == "completed" and (len(identities) != len(set(identities)) or set(identities) != {item["finding_id"] for item in expected_findings}):
            raise ValueError("fixer 必须说明所有分配发现的处置结果")
        for item in dispositions:
            if not isinstance(item, dict) or item.get("status") not in ("fixed", "false_positive", "unresolved", "out_of_scope", "elevated") or not item.get("reason"):
                raise ValueError("fixer 处置状态或理由无效")
            if item.get("status") == "elevated" and item.get("elevation_impact") not in ("none", "rollback", "broad_rewrite"):
                raise ValueError("elevation 必须明确是否涉及回滚或大范围重写")
        new_findings = payload.get("new_findings", [])
        if not isinstance(new_findings, list):
            raise ValueError("fixer new_findings 必须是列表")
        for finding in new_findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("message"), str) or not finding["message"].strip():
                raise ValueError("每条新 finding 需要可复核的问题说明")
            if "problem_key" in finding and not isinstance(finding["problem_key"], str):
                raise ValueError("新 finding problem_key 必须是字符串")


def _validated_role_payload(request: dict, raw_output: str, redactions: dict) -> dict:
    result_path = Path(request["result_file"])
    artifact_payload = read_json(result_path) if result_path.exists() else None
    output_format = request["config"].get("output_format", "text")
    if output_format == "text":
        payload = artifact_payload if artifact_payload is not None else extract_role_result(raw_output, output_format)
    else:
        if cli.structured_output_terminated_cleanly(raw_output, output_format) is not True:
            raise ValueError("CLI 未提供正常结束事实，保留原始结果待恢复")
        try:
            extracted_payload = extract_role_result(raw_output, output_format)
        except cli.ReviewerStructuredOutputError:
            raise
        except ValueError:
            if artifact_payload is None:
                raise
            extracted_payload = None
        payload = artifact_payload if artifact_payload is not None else extracted_payload
    validate_role_result(payload, request["role"], request.get("findings", []))
    return cli.redact_configured_environment_variable_values_in_json_value(payload, redactions)


def reconcile_formal_invocation_availability(attempt_directory: Path, request: dict) -> None:
    eligibility_file = attempt_directory / 'reviewer-eligibility.json'
    invocation_file = attempt_directory / 'model-invocation.json'
    stdout_file = attempt_directory / 'stdout.txt'
    if not all(path.exists() for path in (eligibility_file, invocation_file, stdout_file)):
        return
    outcome_file = attempt_directory / 'outcome.json'
    outcome = read_json(outcome_file) if outcome_file.exists() else {}
    if outcome.get('status') == 'cancelled':
        return
    stdout = stdout_file.read_text(errors='replace')
    stderr_file = attempt_directory / 'stderr.txt'
    stderr = stderr_file.read_text(errors='replace') if stderr_file.exists() else ''
    output_format = request['config'].get('output_format', 'text')
    # 退出码缺失时只接受结构化原生错误或正常终止事实，不推断 stderr 的含义。
    failure = classify_role_failure(stdout, stderr if outcome.get('returncode') else '', outcome.get('returncode') or 0, output_format)
    success = (outcome.get('returncode') == 0 or outcome.get('returncode') is None and output_format != 'text') and (
        output_format == 'text' or cli.structured_output_terminated_cleanly(stdout, output_format) is True)
    record_formal_invocation_result(read_json(eligibility_file), started_at=read_json(invocation_file)['started_at'],
        success=success and not failure, failure_category=failure['category'] if failure else None,
        evidence_path=str(outcome_file if failure else invocation_file))


def reconcile_completed_role_attempt(attempt_directory: Path) -> None:
    outcome_file = attempt_directory / "outcome.json"
    reconciliation_file = attempt_directory / "reconciled-native-completion.json"
    result_file = attempt_directory / "result.json"
    request_file = attempt_directory / "request.json"
    stdout_file = attempt_directory / "stdout.txt"
    if not request_file.exists() or process_lock_is_held(attempt_directory):
        return
    request = read_json(request_file)
    try:
        reconcile_formal_invocation_availability(attempt_directory, request)
        redactions = _role_redactions(request, role_environment(request))
        _redact_role_artifacts(attempt_directory, request, redactions)
        outcome = read_json(outcome_file) if outcome_file.exists() else {}
        if reconciliation_file.exists() and result_file.exists():
            return
        if outcome.get("status") == "cancelled" or (outcome.get("returncode") is not None and outcome["returncode"] != 0) or (outcome.get("status") == "completed" and result_file.exists()):
            return
        if outcome.get("returncode") is None:
            if request["config"].get("output_format", "text") == "text" or not (attempt_directory / "model-invocation.json").exists():
                return
            ensure_execution_not_revoked(Path(request["run_directory"]))
        if not stdout_file.exists():
            return
        payload = _validated_role_payload(request, stdout_file.read_text(errors="replace"), redactions)
        if request["role"] == "reviewer":
            workspace = Path(request["workspace"])
            prepared = attempt_directory / "prepared-worktree-state.json"
            prepared_status = read_json(prepared)["status_porcelain"].encode() if prepared.exists() else b""
            if git(workspace, "status", "--porcelain", "--untracked-files=all") != prepared_status or git_text(workspace, "rev-parse", "HEAD") != request["candidate_commit"]:
                raise ValueError("reviewer 修改了独立候选工作树；该腿不计完成")
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
        return
    write_json(attempt_directory / "result.json", payload)
    write_json(reconciliation_file, {
        "status": payload["status"], "source": "original_stdout",
        "original_outcome": str(outcome_file) if outcome_file.exists() else None, "reconciled_at": time.time(),
    })


def execute_role(attempt_directory: Path, *, inherited_lock_descriptor: int | None = None) -> None:
    with exclusive_lock(attempt_directory / "execution.lock", blocking=False,inherited_descriptor=inherited_lock_descriptor) as execution_lock:
        if (attempt_directory / "outcome.json").exists():
            return
        request = read_json(attempt_directory / "request.json")
        run_directory = Path(request["run_directory"])
        workspace = Path(request["workspace"])
        config = request["config"]
        prompt = (attempt_directory / "prompt.md").read_text()
        command = list(config["command"])
        output_format = config.get("output_format", "text")
        started_at = time.time()
        write_json(attempt_directory / "progress.json", {"status": "running", "pid": os.getpid(), "started_at": started_at})
        outcome = {"status": "failed", "started_at": started_at}
        active_process=None
        cancellation_requested=False
        environment = os.environ
        eligibility = None
        def cancel_role(signum,frame):
            nonlocal cancellation_requested
            cancellation_requested=True
            outcome.update({'status':'cancelled','retryable':False})
            if active_process is not None and active_process.poll() is None:
                raise InterruptedError('已取消角色执行')
        signal.signal(signal.SIGTERM,cancel_role)
        signal.signal(signal.SIGINT,cancel_role)
        try:
            ensure_execution_not_revoked(run_directory)
            deadline_file = attempt_directory.parent / "role-deadline.json"
            if not deadline_file.exists():
                write_json(deadline_file, {"deadline_at": started_at + float(config.get("timeout_seconds", 1800))})
            deadline_at = read_json(deadline_file)["deadline_at"]
            if deadline_at <= time.time():
                raise ValueError("原角色总时限已经耗尽，配置切换不重置时限")
            register_owned_process(run_directory, os.getpid(), "role_worker", owns_process_group=True)
            environment = role_environment(request)
            eligibility = reviewer_eligibility(config, environment=environment, deadline_at=deadline_at, run_directory=run_directory,
                resolved_command=request.get('preselected_formal_model_command'),
                locked_pre_invocation_attempt_directory=attempt_directory)
            write_json(attempt_directory / 'reviewer-eligibility.json', eligibility)
            if not eligibility['can_dispatch']:
                outcome.update(recovery={'category': 'reviewer_unavailable', **eligibility.get('recovery', {})}, retryable=False)
                raise ValueError('正式派发前 reviewer 不可用：' + eligibility['reason'])
            if not workspace.exists():
                git(Path(request['repository']),'worktree','add','--detach',str(workspace),request['candidate_commit'],pass_fds=(execution_lock.fileno(),))
            elif not (attempt_directory/'model-invocation.json').exists():
                git(workspace,'reset','--hard',request['candidate_commit'],pass_fds=(execution_lock.fileno(),))
            prepare_isolated_workspace(run_directory, workspace, request.get("preparation_commands", []), attempt_directory, environment=environment, deadline_at=deadline_at,
                                       inherited_lock_descriptors=(execution_lock.fileno(),))
            prepared_status = git(workspace, "status", "--porcelain", "--untracked-files=all")
            write_json(attempt_directory / "prepared-worktree-state.json", {"status_porcelain": prepared_status.decode()})
            remaining_seconds = deadline_at - time.time()
            if remaining_seconds <= 0:
                raise ValueError("准备环境后原角色总时限已经耗尽")
            command = (list(request['preselected_formal_model_command']) if request.get('preselected_formal_model_command')
                else cli.resolve_formal_reviewer_model_command(config, command, env=environment, cwd=workspace, timeout=min(30, remaining_seconds)))
            resolved_model_command = list(command)
            command = cli.normalise_reviewer_command_for_output_format(command, output_format=output_format)
            if config.get("run_directory_grant_flag"):
                command = cli.ensure_run_directory_grant_argument(command, attempt_directory, grant_flag=config["run_directory_grant_flag"])
            command = cli.ensure_codex_exec_add_dir(command, attempt_directory)
            command, stdin_text = cli.finalize_reviewer_command_for_prompt_delivery(
                command, prompt_delivery=config.get("prompt_delivery", "stdin"), prompt_flag=config.get("prompt_flag"),
                prompt_file_path=attempt_directory / "prompt.md", prompt_text=prompt)
            write_bytes(attempt_directory / "stdin.txt", stdin_text.encode())
            if cancellation_requested:
                raise InterruptedError('已在派发模型前取消')
            remaining_seconds = read_json(deadline_file)["deadline_at"] - time.time()
            if remaining_seconds <= 0:
                outcome["recovery"] = {"category": "deadline_exhausted", "maximum_same_configuration_retries": 0}
                raise ValueError("原角色总时限已经耗尽，配置切换不重置时限")
            import shutil
            executable = shutil.which(command[0])
            cli_version = None
            if executable and Path(executable).name in {"codex", "claude", "kimi", "cursor-agent", "agent", "omp", "python", "python3"}:
                try:
                    version = subprocess.run([executable, "--version"], capture_output=True, text=True, env=environment, timeout=min(3, remaining_seconds))
                    if version.returncode == 0:
                        cli_version = (version.stdout or version.stderr).strip()[:4096]
                except (OSError, subprocess.SubprocessError):
                    pass
            remaining_seconds = read_json(deadline_file)["deadline_at"] - time.time()
            if remaining_seconds <= 0:
                raise ValueError("CLI 版本核验后原角色总时限已经耗尽")
            eligibility = reviewer_eligibility(config, environment=environment, cwd=workspace,
                deadline_at=deadline_at, run_directory=run_directory, resolved_command=resolved_model_command,
                reserve_recovery_for=str(attempt_directory), locked_pre_invocation_attempt_directory=attempt_directory)
            write_json(attempt_directory / 'reviewer-eligibility.json', eligibility)
            if not eligibility['can_dispatch']:
                outcome.update(recovery={'category': 'reviewer_unavailable', **eligibility.get('recovery', {})}, retryable=False)
                raise ValueError('模型派发前 reviewer 不可用：' + eligibility['reason'])
            ensure_execution_not_revoked(run_directory)
            if cancellation_requested:
                raise InterruptedError('已在资格复核后取消')
            remaining_seconds = deadline_at - time.time()
            if remaining_seconds <= 0:
                raise ValueError('资格复核后原角色总时限已经耗尽')
            invocation_started_at = time.time()
            write_json(attempt_directory/'model-invocation.json',{'started_at':invocation_started_at, "command": command,
                       "cli_version": cli_version, "cli_executable": executable,
                       "cli_executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None,
                       "output_format": output_format, "deadline_at": read_json(deadline_file)["deadline_at"]})
            with (attempt_directory / "stdin.txt").open("rb") as stdin, (attempt_directory / "stdout.txt").open("wb") as stdout, (attempt_directory / "stderr.txt").open("wb") as stderr:
                # harness 继承同一把锁；worker 意外退出时，恢复方先等待真实子进程结束。
                process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr, cwd=workspace,
                                           env=environment, start_new_session=True, pass_fds=(execution_lock.fileno(),))
                active_process=process
                register_owned_process(run_directory, process.pid, "role_cli", owns_process_group=True)
                if cancellation_requested:
                    cancel_role(signal.SIGTERM,None)
                try:
                    returncode = wait_for_owned_process(process, run_directory, timeout=remaining_seconds)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    returncode = 124
            raw_output = (attempt_directory / "stdout.txt").read_text(errors="replace")
            raw_error = (attempt_directory / "stderr.txt").read_text(errors="replace")
            outcome["returncode"] = returncode
            failure = classify_role_failure(raw_output, raw_error, returncode, output_format)
            if output_format != "text":
                outcome.update(native_invocation_facts(raw_output))
            if failure:
                outcome["recovery"] = failure
                outcome["retryable"] = failure["category"] == "transient"
                outcome["retry_not_before"] = time.time() + failure["retry_not_before_seconds"]
                try:
                    record_formal_invocation_result(eligibility, started_at=invocation_started_at,
                        failure_category=failure['category'], evidence_path=str(attempt_directory / 'outcome.json'))
                except OSError as error:
                    outcome['availability_evidence_persistence_error'] = type(error).__name__
                raise ValueError(f"角色故障分类：{failure['category']}；原生事实与错误通道已保留")
            if returncode:
                outcome["retryable"] = True
                raise ValueError(f"角色进程退出码 {returncode}；参见原始 stdout/stderr")
            if output_format == 'text' or cli.structured_output_terminated_cleanly(raw_output, output_format) is True:
                try:
                    record_formal_invocation_result(eligibility, started_at=invocation_started_at,
                        success=True, evidence_path=str(attempt_directory / 'model-invocation.json'))
                except OSError as error:
                    outcome['availability_evidence_persistence_error'] = type(error).__name__
            payload = _validated_role_payload(request, raw_output, _role_redactions(request, environment))
            if request["role"] == "reviewer" and (git(workspace, "status", "--porcelain", "--untracked-files=all") != prepared_status or git_text(workspace,"rev-parse","HEAD") != request["candidate_commit"]):
                raise ValueError("reviewer 修改了独立候选工作树；该腿不计完成")
            write_json(attempt_directory / "result.json", payload)
            outcome.update({"status": payload["status"], "result_file": str(attempt_directory / "result.json")})
            if payload['status']=='incomplete':
                outcome['retryable']=False
        except Exception as error:
            if isinstance(error, WorkspacePreparationFailed):
                outcome.update({"retryable": True, "retry_not_before": time.time() + 30,
                    "recovery": {"category": "workspace_preparation", "action": "recheck_registered_preparation",
                                 "original_evidence_paths": error.original_evidence_paths,
                                 "maximum_unchanged_retries": 1}})
            if isinstance(error, (ExecutionRevoked, InterruptedError)):
                outcome.update({"status": "cancelled", "retryable": False, "native_cancellation_status": "unknown"})
            outcome["error"] = str(error)
            if not cancellation_requested and (attempt_directory.parent / "role-deadline.json").exists() and read_json(attempt_directory.parent / "role-deadline.json")["deadline_at"] <= time.time():
                outcome.update({"recovery": {"category": "deadline_exhausted", "maximum_same_configuration_retries": 0}, "retryable": False})
            outcome.setdefault("retryable", False)
        finally:
            if active_process is not None and active_process.poll() is None:
                os.killpg(active_process.pid,signal.SIGTERM)
                try:
                    active_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(active_process.pid,signal.SIGKILL)
                    active_process.wait()
            redactions = _role_redactions(request, environment)
            try:
                _redact_role_artifacts(attempt_directory, request, redactions)
            except (OSError, ValueError) as error:
                outcome.update({"status": "failed", "error": str(error), "retryable": False})
            if "error" in outcome:
                outcome["error"] = cli.redact_configured_environment_variable_values(outcome["error"], redactions)
            outcome["finished_at"] = time.time()
            write_json(attempt_directory / "outcome.json", cli.redact_configured_environment_variable_values_in_json_value(outcome, redactions))


def launch_role(attempt_directory: Path, *, environment_overrides: dict | None = None) -> subprocess.Popen:
    worker = Path(__file__).resolve().parents[2] / "plugins/review-validate-fix/skills/review-validate-fix/scripts/execute_snapshot_review_role.py"
    # 已安装包的 core 与 scripts 层级不同，通过哨兵向上定位。
    for root in Path(__file__).resolve().parents:
        candidate = root / "skills/review-validate-fix/scripts/execute_snapshot_review_role.py"
        if candidate.is_file():
            worker = candidate
            break
    with (attempt_directory / "worker.log").open("ab") as log, (attempt_directory/'execution.lock').open('a+b') as ownership:
        fcntl.flock(ownership,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # 父进程关闭自己的 fd，锁由 child 持有，覆盖 spawn 后尚未启动 Python 的窗口。
        return subprocess.Popen([sys.executable, str(worker), str(attempt_directory),str(ownership.fileno())],
                                stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,pass_fds=(ownership.fileno(),),
                                env={**os.environ,**(environment_overrides or {})})
