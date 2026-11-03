"""消费精确绑定的 reviewer 机器请求，持久限额并复用已有命令监督器。"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .execution_lifecycle import ensure_execution_not_revoked
from .machine_evidence import (WorkspacePreparationFailed, archive_unstarted_command_launch, command_launch_was_not_started,
    ensure_previous_supervised_commands_exited, execute_registered_commands, prepare_or_reconcile_registered_machine_command_workspace,
    unstarted_launch_retries_exhausted, verified_execution_plan)
from .machine_evidence import prepare_registered_machine_command_environment, registered_machine_command_budget_is_exhausted, prepared_workspace_dependency_identity
from .persistent_artifacts import exclusive_lock, git, read_json, write_json
from .review_recovery_evidence_observation import (file_sha256,
    machine_evidence_request_binding, canonical_json_material_sha256, registered_host_recovery_inputs)
from .role_failure_recovery import classify_role_failure
from .task_snapshot import capture_worktree_tree


def verified_registered_command_references(request: dict, binding: dict, plan: dict) -> list[dict]:
    if (not isinstance(request, dict) or request.get("kind") != "machine_evidence"
            or set(request) - {"kind", "binding", "registered_command_references", "reason"}
            or request.get("binding") != binding):
        raise ValueError("机器请求必须绑定本任务、精确 run、完整候选树及冻结计划")
    references = request.get("registered_command_references")
    if not isinstance(references, list) or not references:
        raise ValueError("机器请求缺少冻结命令引用")
    normalized = []
    for reference in references:
        if (not isinstance(reference, dict) or set(reference) != {"command_collection", "command_index"}
                or reference.get("command_collection") not in {"machine_evidence_commands", "validation_commands"}
                or type(reference.get("command_index")) is not int
                or not 0 <= reference["command_index"] < len(plan.get(reference["command_collection"], []))):
            raise ValueError("机器请求只接受共享或最终验证的零基索引，不能增加 argv")
        if reference not in normalized:
            normalized.append(reference)
    return sorted(normalized, key=lambda value: (value["command_collection"], value["command_index"]))


def register_reviewer_machine_evidence_request(run_directory: Path, *, source_role_result_file: Path,
        source_role_result_sha256: str, source_request_index: int, request: dict) -> dict:
    run_directory = run_directory.expanduser().resolve()
    source = source_role_result_file.expanduser().resolve()
    relative = source.relative_to(run_directory)
    if (len(relative.parts) != 3 or not re.fullmatch(r"reviewer-\d+", relative.parts[0])
            or not re.fullmatch(r"attempt-\d+", relative.parts[1]) or relative.name != "result.json"):
        raise ValueError("机器请求登记必须引用本 run 的 reviewer 结果原件")
    with exclusive_lock(run_directory / "main-agent-context-supplements.lock"), exclusive_lock(run_directory / "execution-revocation.lock"):
        ensure_execution_not_revoked(run_directory)
        if file_sha256(source) != source_role_result_sha256:
            raise ValueError("请求来源原件摘要不匹配")
        result = read_json(source)
        requests = result.get("requests", [])
        if result.get("status") != "incomplete" or not isinstance(requests, list) or type(source_request_index) is not int or not 0 <= source_request_index < len(requests):
            raise ValueError("来源必须是仍有请求的 incomplete 结果")
        record = read_json(run_directory / "run.json")
        plan = verified_execution_plan(run_directory, record)
        binding = machine_evidence_request_binding(run_directory, record)
        references = verified_registered_command_references(request, binding, plan)
        material = {"source_role_result_file": str(source), "source_role_result_sha256": source_role_result_sha256,
            "source_request_index": source_request_index, "source_request_sha256": canonical_json_material_sha256(requests[source_request_index]),
            "request": {"kind": "machine_evidence", "binding": binding, "registered_command_references": references}}
        registration_id = canonical_json_material_sha256(material)
        original = run_directory / "registered-reviewer-machine-evidence-requests" / f"{registration_id}.json"
        if original.exists() and read_json(original) != material:
            raise ValueError("已登记机器请求原件发生变化")
        if not original.exists():
            write_json(original, material)
        return {"registration_id": registration_id, "original": str(original)}


def registered_reviewer_machine_requests_for_original_result(run_directory: Path, source: Path) -> list[dict]:
    requests = []
    for original in sorted(run_directory.glob("registered-reviewer-machine-evidence-requests/*.json")):
        material = read_json(original)
        if material.get("source_role_result_file") != str(source):
            continue
        if material.get("source_role_result_sha256") != file_sha256(source):
            raise ValueError("已登记机器请求的来源原件发生变化")
        source_requests = read_json(source)["requests"]
        if material["source_request_sha256"] != canonical_json_material_sha256(source_requests[material["source_request_index"]]):
            raise ValueError("已登记机器请求与来源请求不匹配")
        requests.append(material["request"])
    return requests


def verified_reviewer_machine_evidence_collection_original(attempt: Path, binding: dict) -> dict:
    evidence = read_json(attempt / "evidence.json")
    if evidence.get("binding") != binding or (evidence.get("status") == "passed" and (
            evidence.get("source_tree_before") != binding["candidate_tree"] or evidence.get("source_tree_after") != binding["candidate_tree"])):
        raise ValueError("机器原件完整树或执行归属不匹配")
    intent = read_json(attempt / "collection-intent.json")
    run_directory = attempt.parents[2]
    plan = verified_execution_plan(run_directory)
    references = verified_registered_command_references(read_json(attempt.parent / "request.json"), binding, plan)
    if intent.get("binding") != binding or intent.get("commands") != [plan[ref["command_collection"]][ref["command_index"]] for ref in references]:
        raise ValueError("机器派发登记与冻结命令不匹配")
    if evidence.get("host_recovery_inputs") != intent["host_recovery_inputs"]:
        raise ValueError("机器原件环境与派发登记不匹配")
    commands = intent["commands"]
    seen = set()
    for command in evidence.get("commands", []):
        index = command.get("registered_command_index")
        if type(index) is not int or index in seen or not 0 <= index < len(commands):
            raise ValueError("机器原件命令索引缺失或重复")
        seen.add(index)
        directory = Path(command["original_execution_directory"]).resolve()
        relative = directory.relative_to(run_directory.resolve())
        if relative.parts[0] not in {"machine-evidence", "reviewer-machine-evidence-requests"} or "workspace" in relative.parts:
            raise ValueError("机器原件日志必须来自本 run 已登记命令目录")
        if command.get("argv") != commands[index]:
            raise ValueError("机器原件命令不匹配")
        for stream in ("stdout", "stderr"):
            if command.get(stream + "_sha256") != file_sha256(directory / (stream + ".txt")):
                raise ValueError("机器原始日志摘要不匹配")
        if command.get("qualified_source_evidence_file") and file_sha256(Path(command["qualified_source_evidence_file"])) != command.get("qualified_source_evidence_sha256"):
            raise ValueError("复用来源原件摘要不匹配")
        if evidence.get("dependency_identity_proven"):
            dependencies = command.get("prepared_workspace_dependency_identity")
            if not dependencies or prepared_workspace_dependency_identity(Path(command["cwd"]), dependencies["preparation_created_paths"]) != dependencies:
                raise ValueError("原件的实际准备产物或依赖内容已改变")
    if evidence["status"] == "passed" and len(evidence["commands"]) != len(commands):
        raise ValueError("机器原件缺少已登记命令结果")
    if evidence["status"] == "passed" and (evidence.get("host_recovery_inputs_after") != evidence["host_recovery_inputs"]
            or any(command.get("status") != "passed" or command.get("returncode") != 0
                or command.get("source_tree_before") != binding["candidate_tree"] or command.get("source_tree_after") != binding["candidate_tree"]
                for command in evidence["commands"])):
        raise ValueError("机器通过原件缺少完整树、稳定环境或成功退出证据")
    return evidence


def qualified_existing_machine_command_results_for_reviewer_request(run_directory: Path, binding: dict, recovery_inputs: dict, commands: list[list[str]]) -> list[dict]:
    if not recovery_inputs["registered_dependency_sources"]["identity_proven"]:
        return []
    sources = []
    for original in sorted(run_directory.glob("reviewer-machine-evidence-requests/*/attempt-*/evidence.json")):
        try:
            evidence = verified_reviewer_machine_evidence_collection_original(original.parent, binding)
            if (evidence.get("dependency_identity_proven") and evidence["host_recovery_inputs"] == evidence.get("host_recovery_inputs_after") == recovery_inputs):
                sources.extend((original, command) for command in evidence["commands"] if command["status"] == "passed")
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for original in sorted(run_directory.glob("machine-evidence/attempt-*/evidence.json")):
        try:
            evidence = read_json(original)
            if (not evidence.get("dependency_identity_proven") or evidence.get("tested_tree") != binding["candidate_tree"]
                    or evidence.get("runtime_environment") != evidence.get("host_recovery_inputs_after") or evidence["runtime_environment"] != recovery_inputs):
                continue
            for index, command in enumerate(evidence["commands"]):
                directory = original.parent / "commands" / f"command-{index + 1:03d}"
                if (command.get("status") != "passed" or command.get("returncode") != 0
                        or command.get("source_tree_before") != binding["candidate_tree"] or command.get("source_tree_after") != binding["candidate_tree"]
                        or any(command.get(stream + "_sha256") != file_sha256(directory / (stream + ".txt")) for stream in ("stdout", "stderr"))):
                    continue
                dependencies = command.get("prepared_workspace_dependency_identity")
                if not dependencies or prepared_workspace_dependency_identity(Path(command["cwd"]), dependencies["preparation_created_paths"]) != dependencies:
                    continue
                sources.append((original, {**command, "original_execution_directory": str(directory)}))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    qualified = {}
    for original, command in sources:
        for index, argv in enumerate(commands):
            if command["argv"] == argv:
                qualified[index] = {**command, "registered_command_index": index,
                    "qualified_source_evidence_file": str(original), "qualified_source_evidence_sha256": file_sha256(original)}
    return list(qualified.values())


def reconciled_registered_machine_command_from_native_supervision(directory: Path, argv: list[str], candidate_tree: str) -> dict:
    original = read_json(directory / "execution.json")
    supervision_file = directory / "command-supervision.json"
    supervision = read_json(supervision_file) if supervision_file.exists() else {}
    if (supervision.get("status") != "completed" or supervision.get("argv") != argv
            or original.get("source_tree_before") != candidate_tree
            or supervision.get("source_tree_after") != candidate_tree):
        raise WorkspacePreparationFailed(directory / "execution.json")
    returncode = supervision["returncode"]
    record = {"argv": argv, "cwd": original["cwd"], "status": "passed" if returncode == 0 else "failed", "returncode": returncode,
        "source_tree_before": original["source_tree_before"], "source_tree_after": supervision["source_tree_after"],
        "reconciled_from": str(supervision_file)}
    for stream in ("stdout", "stderr"):
        digest = file_sha256(directory / (stream + ".txt"))
        if supervision.get(stream + "_sha256") != digest:
            raise WorkspacePreparationFailed(supervision_file)
        record[stream + "_sha256"] = digest
    return record


def execute_or_reconcile_reviewer_machine_evidence_collection_attempt(run_directory: Path, record: dict, plan: dict, attempt: Path, lock) -> dict:
    intent = read_json(attempt / "collection-intent.json")
    binding = intent["binding"]
    ensure_previous_supervised_commands_exited(run_directory, attempt)
    if intent["status"] == "qualified_existing_facts":
        evidence = {"status": "passed", "commands": intent["reused_commands"], "binding": binding,
            "source_tree_before": binding["candidate_tree"], "source_tree_after": binding["candidate_tree"],
            "host_recovery_inputs": intent["host_recovery_inputs"], "host_recovery_inputs_after": intent["host_recovery_inputs"],
            "dependency_identity_proven": True, "execution_started": False, "original": str(attempt / "evidence.json")}
        write_json(attempt / "evidence.json", evidence)
        return evidence
    workspace = attempt / "workspace"
    environment = prepare_registered_machine_command_environment(run_directory)
    if not workspace.exists():
        with exclusive_lock(run_directory / "execution-revocation.lock"):
            ensure_execution_not_revoked(run_directory)
            git(Path(record["repo"]), "worktree", "add", "--detach", str(workspace), record["candidate_commit"])
    preparation_result = prepare_or_reconcile_registered_machine_command_workspace(run_directory, workspace,
        plan.get("preparation_commands", []), attempt, environment=environment,
        inherited_lock_descriptors=(lock.fileno(),))
    if preparation_result and preparation_result["status"] != "passed":
        failed_index = len(preparation_result["commands"])
        directory = attempt / "preparation" / f"command-{failed_index:03d}"
        command = preparation_result["commands"][-1]
        failure = classify_role_failure("", (directory / "stderr.txt").read_text(errors="replace"), command["returncode"], "text")
        evidence = {"status": "failed", "binding": binding, "commands": [], "execution_started": False,
            "preparation": preparation_result, "host_recovery_inputs": intent["host_recovery_inputs"],
            "dependency_identity_proven": False, "failure": failure, "original": str(attempt / "evidence.json")}
        if failure["maximum_same_configuration_retries"]:
            evidence["retry_not_before_epoch_ms"] = (time.time() + failure["retry_not_before_seconds"]) * 1000
        write_json(attempt / "evidence.json", evidence)
        return evidence
    preparation_file = attempt / "preparation-created-untracked-paths.json"
    excluded = read_json(preparation_file)["paths"] if preparation_file.exists() else None
    before = capture_worktree_tree(workspace, ["."], attempt, excluded_preparation_paths=excluded)
    dependency_before_file = attempt / "prepared-workspace-dependency-identity-before.json"
    if not dependency_before_file.exists():
        write_json(dependency_before_file, prepared_workspace_dependency_identity(workspace, excluded))
    dependency_before = read_json(dependency_before_file)
    current_dependencies = prepared_workspace_dependency_identity(workspace, excluded)
    commands = {item["registered_command_index"]: item for item in intent.get("reused_commands", [])}
    status = "passed"
    for index, argv in enumerate(intent["commands"]):
        ensure_execution_not_revoked(run_directory)
        if registered_host_recovery_inputs(run_directory, plan) != intent["host_recovery_inputs"]:
            status = "environment_changed"
            break
        if index in commands:
            if commands[index].get("prepared_workspace_dependency_identity") != current_dependencies:
                raise WorkspacePreparationFailed(dependency_before_file)
            continue
        artifacts = attempt / "commands" / f"registered-command-{index + 1:03d}"
        directory = artifacts / "command-001"
        if (directory / "execution.json").exists():
            execution = read_json(directory / "execution.json")
            if command_launch_was_not_started(execution):
                if unstarted_launch_retries_exhausted(directory):
                    raise WorkspacePreparationFailed(directory / "execution.json")
                archive_unstarted_command_launch(directory)
                result = execute_registered_commands(run_directory, workspace, [argv], artifacts, "reviewer_requested_machine_evidence",
                    environment=environment, inherited_lock_descriptors=(lock.fileno(),),
                    excluded_preparation_paths=excluded, expected_source_tree=binding["candidate_tree"])
                command = result["commands"][0]
            else:
                command = reconciled_registered_machine_command_from_native_supervision(directory, argv, binding["candidate_tree"])
        else:
            if registered_host_recovery_inputs(run_directory, plan) != intent["host_recovery_inputs"]:
                status = "environment_changed"
                break
            result = execute_registered_commands(run_directory, workspace, [argv], artifacts, "reviewer_requested_machine_evidence",
                environment=environment, inherited_lock_descriptors=(lock.fileno(),),
                excluded_preparation_paths=excluded, expected_source_tree=binding["candidate_tree"])
            command = result["commands"][0]
        commands[index] = {**command, "registered_command_index": index, "original_execution_directory": str(directory),
            "prepared_workspace_dependency_identity": dependency_before}
        if command["status"] != "passed":
            status = command["status"]
            break
    after = capture_worktree_tree(workspace, ["."], attempt, excluded_preparation_paths=excluded)
    if before != binding["candidate_tree"] or after != before:
        status = "candidate_changed"
    current_inputs = registered_host_recovery_inputs(run_directory, plan)
    dependency_after = prepared_workspace_dependency_identity(workspace, excluded)
    if current_inputs != intent["host_recovery_inputs"]:
        status = "environment_changed"
    evidence = {"status": status, "commands": [commands[index] for index in sorted(commands)],
        "binding": binding, "source_tree_before": before, "source_tree_after": after,
        "host_recovery_inputs": intent["host_recovery_inputs"], "host_recovery_inputs_after": current_inputs,
        "dependency_identity_proven": bool(intent["host_recovery_inputs"]["registered_dependency_sources"]["identity_proven"]
            and dependency_before["identity_proven"] and dependency_before == dependency_after),
        "collector_source_sha256": file_sha256(Path(__file__)),
        "original": str(attempt / "evidence.json")}
    if status == "failed":
        failed = next(item for item in evidence["commands"] if item["status"] == "failed")
        failure = classify_role_failure("", (Path(failed["original_execution_directory"]) / "stderr.txt").read_text(errors="replace"), failed["returncode"], "text")
        evidence["failure"] = failure
        if failure["maximum_same_configuration_retries"]:
            evidence["retry_not_before_epoch_ms"] = (time.time() + failure["retry_not_before_seconds"]) * 1000
    write_json(attempt / "evidence.json", evidence)
    return evidence


def collect_registered_reviewer_machine_evidence_request(run_directory: Path, record: dict, plan: dict, request: dict, delivered_paths: list[str]) -> Path | None:
    binding = machine_evidence_request_binding(run_directory, record)
    references = verified_registered_command_references(request, binding, plan)
    identity = canonical_json_material_sha256({"binding": binding, "registered_command_references": references})
    root = run_directory / "reviewer-machine-evidence-requests" / identity
    with exclusive_lock(run_directory / "machine-evidence.lock") as lock:
        with exclusive_lock(run_directory / "execution-revocation.lock"):
            ensure_execution_not_revoked(run_directory)
            root.mkdir(parents=True, exist_ok=True)
            write_json(root / "request.json", {"kind": "machine_evidence", "binding": binding,
                "registered_command_references": references})
        verified_execution_plan(run_directory, record)
        ensure_previous_supervised_commands_exited(run_directory, root)
        recovery_inputs = registered_host_recovery_inputs(run_directory, plan)
        recovery_sha256 = canonical_json_material_sha256(recovery_inputs)
        commands = [plan[ref["command_collection"]][ref["command_index"]] for ref in references]
        attempts = sorted(root.glob("attempt-*"))
        matching = [attempt for attempt in attempts
            if read_json(attempt / "collection-intent.json")["recovery_input_sha256"] == recovery_sha256]
        qualified = {}
        for prior in matching:
            if not (prior / "evidence.json").exists():
                continue
            try:
                qualified[prior] = verified_reviewer_machine_evidence_collection_original(prior, binding)
            except (OSError, ValueError, KeyError, TypeError) as error:
                write_json(root / "rejected-existing-evidence" / f"{prior.name}.json",
                    {"original": str(prior / "evidence.json"), "status": "unqualified", "reason": str(error)})
        existing_commands = qualified_existing_machine_command_results_for_reviewer_request(run_directory, binding, recovery_inputs, commands)
        missing_commands = [argv for index, argv in enumerate(commands) if index not in {item["registered_command_index"] for item in existing_commands}]
        if matching and matching[-1] in qualified:
            previous = qualified[matching[-1]]
            if previous["status"] == "passed":
                original = matching[-1] / "evidence.json"
                if str(original) in delivered_paths:
                    return original
                if previous.get("dependency_identity_proven") and previous["host_recovery_inputs"] == previous.get("host_recovery_inputs_after") == recovery_inputs:
                    return original
            failure = previous.get("failure") or {"maximum_same_configuration_retries": 0}
            retry_after = previous.get("retry_not_before_epoch_ms")
            if missing_commands and previous["status"] != "passed" and (not failure["maximum_same_configuration_retries"] or not retry_after or time.time() * 1000 < retry_after):
                return None
        if attempts and not (attempts[-1] / "evidence.json").exists():
            attempt = attempts[-1]
            evidence = execute_or_reconcile_reviewer_machine_evidence_collection_attempt(run_directory, record, plan, attempt, lock)
            return attempt / "evidence.json" if evidence["status"] == "passed" else None
        if missing_commands and len(matching) >= 3:
            return None
        if registered_machine_command_budget_is_exhausted(run_directory, recovery_inputs, missing_commands):
            return None
        attempt = root / f"attempt-{len(attempts) + 1:03d}"
        intent = {"binding": binding, "commands": commands, "recovery_input_sha256": recovery_sha256,
            "host_recovery_inputs": recovery_inputs, "maximum_attempts_per_recovery_input": 3, "status": "registered"}
        reused_commands = {item["registered_command_index"]: item for item in existing_commands}
        for prior in matching:
            if prior in qualified:
                previous = qualified[prior]
                if previous.get("dependency_identity_proven") and previous["host_recovery_inputs"] == previous.get("host_recovery_inputs_after") == recovery_inputs:
                    reused_commands.update({item["registered_command_index"]: item for item in previous["commands"] if item["status"] == "passed"})
        intent["reused_commands"] = list(reused_commands.values())
        if len(reused_commands) == len(commands):
            intent["status"] = "qualified_existing_facts"
        with exclusive_lock(run_directory / "execution-revocation.lock"):
            ensure_execution_not_revoked(run_directory)
            write_json(attempt / "collection-intent.json", intent)
        evidence = execute_or_reconcile_reviewer_machine_evidence_collection_attempt(run_directory, record, plan, attempt, lock)
        return attempt / "evidence.json" if evidence["status"] == "passed" else None


def collect_reviewer_machine_evidence_requests(run_directory: Path) -> bool:
    from .workflow import _role_result
    record = read_json(run_directory / "run.json")
    plan = verified_execution_plan(run_directory, record)
    if not plan:
        return False
    changed = False
    evidence_by_role = record.setdefault("reviewer_machine_evidence", {})
    digests_by_role = record.setdefault("reviewer_machine_evidence_recovery_sha256_by_role", {})
    for index in range(record["reviewer_count"]):
        role = f"reviewer-{index + 1}"
        directory = run_directory / role
        if _role_result(directory) is not None:
            continue
        incomplete = _role_result(directory, latest=True, include_incomplete=True)
        if not incomplete or incomplete["result"]["status"] != "incomplete":
            continue
        requests = incomplete["result"].get("requests", [])
        if not isinstance(requests, list):
            continue
        requests = [*requests, *registered_reviewer_machine_requests_for_original_result(run_directory, Path(incomplete["attempt_directory"]) / "result.json")]
        for request in requests:
            if not isinstance(request, dict) or request.get("kind") != "machine_evidence":
                continue
            try:
                evidence = collect_registered_reviewer_machine_evidence_request(run_directory, record, plan, request, evidence_by_role.get(role, []))
            except ValueError as error:
                write_json(run_directory / "reviewer-machine-evidence-request-rejections" / f"{canonical_json_material_sha256(request)}.json",
                    {"request": request, "reason": str(error), "status": "rejected"})
                continue
            if evidence:
                paths = evidence_by_role.setdefault(role, [])
                superseded = [path for path in paths if Path(path).parent.parent == evidence.parent.parent and path != str(evidence)]
                if superseded:
                    record.setdefault("reviewer_machine_evidence_history", []).extend(superseded)
                    paths[:] = [path for path in paths if path not in superseded]
                if str(evidence) not in paths:
                    paths.append(str(evidence))
                record.setdefault("reviewer_machine_evidence_original_sha256s", {})[str(evidence)] = file_sha256(evidence)
                digest = canonical_json_material_sha256([read_json(Path(path)) for path in paths])
                if digests_by_role.get(role) != digest:
                    digests_by_role[role] = digest
                    changed = True
    ensure_execution_not_revoked(run_directory)
    write_json(run_directory / "run.json", record)
    return changed


def verified_reviewer_machine_evidence_for_role(run_directory: Path, record: dict, role_name: str) -> list[str]:
    paths = record.get("reviewer_machine_evidence", {}).get(role_name, [])
    for original in paths:
        path = Path(original).resolve()
        path.relative_to((run_directory / "reviewer-machine-evidence-requests").resolve())
        if record.get("reviewer_machine_evidence_original_sha256s", {}).get(original) != file_sha256(path):
            raise WorkspacePreparationFailed(path)
        evidence = verified_reviewer_machine_evidence_collection_original(path.parent, machine_evidence_request_binding(run_directory, record))
        if evidence["status"] != "passed":
            raise WorkspacePreparationFailed(path)
    return paths


def pending_reviewer_machine_evidence_requests(run_directory: Path) -> list[dict]:
    from .workflow import _role_result
    record = read_json(run_directory / "run.json")
    pending = {}
    for index in range(record["reviewer_count"]):
        directory = run_directory / f"reviewer-{index + 1}"
        if _role_result(directory) is not None:
            continue
        incomplete = _role_result(directory, latest=True, include_incomplete=True)
        if not incomplete or incomplete["result"].get("status") != "incomplete":
            continue
        source = Path(incomplete["attempt_directory"]) / "result.json"
        requests = incomplete["result"].get("requests", [])
        if not isinstance(requests, list):
            continue
        registered = registered_reviewer_machine_requests_for_original_result(run_directory, source)
        registered_source_hashes = {read_json(path)["source_request_sha256"]
            for path in run_directory.glob("registered-reviewer-machine-evidence-requests/*.json")
            if read_json(path).get("source_role_result_file") == str(source)}
        requests = [request for request in requests if canonical_json_material_sha256(request) not in registered_source_hashes]
        for request in [*requests, *registered]:
            # 已登记的等价请求覆盖来源文本；文本本身始终不授予执行资格。
            binding = machine_evidence_request_binding(run_directory, record) if record.get("execution_plan_sha256") else None
            try:
                references = verified_registered_command_references(request, binding, verified_execution_plan(run_directory, record))
                identity = canonical_json_material_sha256({"binding": binding, "registered_command_references": references})
                root = run_directory / "reviewer-machine-evidence-requests" / identity
                evidence_files = sorted(root.glob("attempt-*/evidence.json"))
                latest = read_json(evidence_files[-1]) if evidence_files else {}
                intents = sorted(root.glob("attempt-*/collection-intent.json"))
                latest_input = read_json(intents[-1])["recovery_input_sha256"] if intents else None
                attempts_used = sum(read_json(intent)["recovery_input_sha256"] == latest_input for intent in intents)
                automatic_retry = bool(latest.get("retry_not_before_epoch_ms") and attempts_used < 3)
                evidence = {"missing_evidence_identity": identity, "status": latest.get("status", "command_result_unknown"),
                    "recovery_condition": "registered_environment_dependencies_or_native_command_result_changed",
                    "retry_not_before_epoch_ms": latest.get("retry_not_before_epoch_ms") if automatic_retry else None,
                    "requires_main_agent_handoff": not automatic_retry,
                    "original": str(root)}
                if latest.get("status") == "passed":
                    continue
            except (ValueError, TypeError, KeyError):
                identity = canonical_json_material_sha256({"run": record["run_id"], "request": request})
                evidence = {"missing_evidence_identity": identity, "status": "registered_command_source_unavailable",
                    "requires_main_agent_handoff": True,
                    "recovery_condition": "bound_registered_command_request_available", "original": str(source)}
            pending[identity] = evidence
    return list(pending.values())
