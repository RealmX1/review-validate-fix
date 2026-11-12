"""只读观察精确 run 的实质恢复输入；心跳与 controller 身份不参与预算。"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from .machine_evidence import verified_execution_plan, prepared_workspace_dependency_identity
from .persistent_artifacts import git_text, read_json, verify_repository_identity
from .native_owned_process_observation import (
    native_host_boot_identity, native_process_identity, native_process_start_identity_matches,
    recorded_native_process_identity_is_valid, ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT,
)


def canonical_json_material_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as original:
        for block in iter(lambda: original.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def machine_evidence_request_binding(run_directory: Path, record: dict) -> dict:
    binding_file = run_directory / "kanban-command-execution-binding.json"
    task_identity = read_json(binding_file) if binding_file.exists() else {
        "kind": "unmanaged_repository_execution", "repository": record["repo"],
        "git_directory": record["git_directory"], "git_common_directory": record["git_common_directory"]}
    task_identity = {**task_identity, "owned_command_execution": record.get("command_execution_identity"),
        "owned_execution_revocation_file": record.get("command_execution_revocation_file"),
        "starting_point_task_binding": record.get("starting_point_evidence", {}).get("binding")}
    return {"run_id": record["run_id"], "run_directory": str(run_directory.resolve()),
        "task_identity": task_identity, "candidate_commit": record["candidate_commit"],
        "candidate_tree": git_text(Path(record["repo"]), "rev-parse", record["candidate_commit"] + "^{tree}"),
        "execution_plan_sha256": record["execution_plan_sha256"],
        "decision_facts_sha256": record["decision_facts_sha256"]}


def isolated_registered_machine_command_environment(environment_directory: Path) -> dict:
    blocked_prefixes = ("KANBAN_", "CLINE_KANBAN_", "CODEX_", "CLAUDE_", "ANTHROPIC_", "OPENAI_",
        "AZURE_OPENAI_", "CURSOR_", "KIMI_", "MOONSHOT_", "DEEPSEEK_", "GEMINI_", "GOOGLE_API_",
        "RVF_", "GIT_", "OMP_", "AGENT_", "GOOGLE_", "GCP_", "AWS_", "AZURE_")
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith(blocked_prefixes) and not key.endswith(("_API_KEY", "_TOKEN"))
        and key not in {"HOME", "PWD", "OLDPWD", "SHLVL", "_", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"}}
    environment.update({"HOME": str(environment_directory / "home"),
        "CODEX_HOME": str(environment_directory / "codex-configuration"),
        "CODEX_SQLITE_HOME": str(environment_directory / "codex-conversation-storage"),
        "XDG_CONFIG_HOME": str(environment_directory / "xdg-configuration"),
        "XDG_CACHE_HOME": str(environment_directory / "xdg-cache"),
        "XDG_DATA_HOME": str(environment_directory / "xdg-content"),
        # Unix socket 路径有长度上限；临时 IPC 与需跨重启保留的证据目录分开。
        "TMPDIR": str(Path("/tmp") / ("rvf-machine-command-ipc-" + canonical_json_material_sha256(str(environment_directory.resolve()))[:16])),
        "RVF_SUPPRESS_STOP_HOOK": "1"})
    return environment


def registered_dependency_sources_identity(plan: dict) -> dict:
    context = plan.get("machine_context") or {}
    paths = context.get("dependency_source_paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) or not Path(path).is_absolute() for path in paths):
        raise ValueError("dependency_source_paths 需要冻结的绝对路径列表")
    sources = {}
    complete = bool(paths)
    for source in sorted(set(paths)):
        root = Path(source)
        # is_dir 会跟随目录符号链接；链接本身无法证明目标内容，必须先标未证明。
        if root.is_symlink():
            complete = False
            sources[source] = canonical_json_material_sha256([[str(root), "symlink_dependency_unproven", os.readlink(root)]])
            continue
        originals = sorted(root.rglob("*")) if root.is_dir() else [root]
        observations = []
        for path in originals:
            if path.is_symlink():
                complete = False
                observations.append([str(path), "symlink_dependency_unproven", os.readlink(path)])
            elif path.is_file():
                observations.append([str(path), path.stat().st_mode & 0o777, file_sha256(path)])
            elif not path.is_dir():
                complete = False
                observations.append([str(path), "unavailable"])
        sources[source] = canonical_json_material_sha256(observations)
    return {"sources": sources, "identity_proven": complete}


def registered_host_recovery_inputs(run_directory: Path, plan: dict) -> dict:
    environment = isolated_registered_machine_command_environment(run_directory / "machine-evidence-isolated-environment")
    executables = []
    for command in [*plan.get("preparation_commands", []), *plan.get("machine_evidence_commands", []), *plan.get("validation_commands", [])]:
        executable = shutil.which(command[0], path=environment.get("PATH"))
        executables.append({"argv": command, "executable": executable,
            "executable_sha256": file_sha256(Path(executable)) if executable else None})
    return {"environment_sha256": canonical_json_material_sha256(environment), "platform": platform.platform(),
        "python": sys.version, "registered_executables": executables,
        "registered_dependency_sources": registered_dependency_sources_identity(plan)}


def task_review_recovery_original_without_progress_metadata(path: Path) -> object:
    if path.suffix != ".json":
        return file_sha256(path)
    def without_progress(value):
        if isinstance(value, dict):
            return {key: without_progress(item) for key, item in value.items() if key not in {
                "observed_at", "last_observed_at", "updated_at", "started_at", "finished_at", "created_at",
                "observedAtMs", "checked_at", "last_checked_at", "next_check_at", "unchanged_unavailable_check_count",
                "credential_identity_recheck_not_before", "controller_nonce", "process_identity", "process_identity_error", "retry_not_before_epoch_ms",
                "original", "cwd", "workspace", "original_execution_directory", "reconciled_from", "collector_source_sha256",
                "qualified_source_evidence_file", "qualified_source_evidence_sha256"}}
        return [without_progress(item) for item in value] if isinstance(value, list) else value
    return without_progress(read_json(path))


def observe_task_review_recovery_evidence(run_directory: Path) -> dict:
    run_directory = run_directory.expanduser().resolve()
    try:
        record = read_json(run_directory / "run.json")
        verify_repository_identity(record)
        plan = verified_execution_plan(run_directory, record)
        if not plan:
            raise ValueError("缺少冻结执行计划")
        originals = {}
        machine_facts = set()
        patterns = ("reviewer-*/attempt-*/result.json", "reviewer-*/attempt-*/outcome.json",
            "reviewer-*/attempt-*/reconciled-native-completion.json", "fixer/attempt-*/result.json",
            "fixer/attempt-*/outcome.json", "machine-evidence/attempt-*/evidence.json",
            "machine-evidence/attempt-*/commands/**/stdout.txt", "machine-evidence/attempt-*/commands/**/stderr.txt",
            "reviewer-machine-evidence-requests/*/attempt-*/evidence.json",
            "reviewer-machine-evidence-requests/*/attempt-*/commands/**/stdout.txt",
            "reviewer-machine-evidence-requests/*/attempt-*/commands/**/stderr.txt",
            "incomplete-fixer-machine-validation/round-*/attempt-*/evidence.json",
            "context-response-originals-by-sha256/*", "main-agent-context-originals-by-sha256/*",
            "main-agent-context-supplements/*.json", "execution-revocation.json")
        patterns = (*patterns, "registered-reviewer-machine-evidence-requests/*.json")
        for pattern in patterns:
            for path in sorted(run_directory.glob(pattern)):
                value = task_review_recovery_original_without_progress_metadata(path)
                if path.name == "evidence.json" or path.suffix == ".txt":
                    machine_facts.add(canonical_json_material_sha256(value))
                else:
                    originals[str(path.relative_to(run_directory))] = value
        originals["qualified_machine_fact_materials"] = sorted(machine_facts)
        for pattern in ("machine-evidence/attempt-*/prepared-workspace-dependency-identity-before.json",
                        "reviewer-machine-evidence-requests/*/attempt-*/prepared-workspace-dependency-identity-before.json"):
            for original in run_directory.glob(pattern):
                if (original.parent / "evidence.json").exists():
                    continue
                expected = read_json(original)
                workspace = original.parent / "workspace"
                originals[str(original.relative_to(run_directory))] = {
                    "expected": expected,
                    "observed": prepared_workspace_dependency_identity(workspace, expected["preparation_created_paths"]) if workspace.exists() else "unavailable"}
        for name in ("reviewer-configurations.json", "fixer-configuration.json", "main-agent-context-continuation-request.json"):
            path = run_directory / name
            if path.exists():
                originals[name] = task_review_recovery_original_without_progress_metadata(path)
        for path in sorted(run_directory.glob("reviewer-machine-evidence-requests/*/attempt-*/evidence.json")):
            evidence = read_json(path)
            retry_at = evidence.get("retry_not_before_epoch_ms")
            attempts = sorted(path.parent.parent.glob("attempt-*/collection-intent.json"))
            same_input_count = sum(read_json(intent).get("recovery_input_sha256") == read_json(path.parent / "collection-intent.json").get("recovery_input_sha256") for intent in attempts)
            if retry_at and time.time() * 1000 >= retry_at and same_input_count < 3:
                originals[str(path.relative_to(run_directory)) + ":bounded_retry_due"] = True
        registry = run_directory / "registered-supervised-command-directories.json"
        native_command_facts = set()
        resource_cleanup_facts = []
        if registry.exists():
            for directory in read_json(registry)["command_directories"]:
                original = Path(directory).resolve() / "command-supervision.json"
                original.relative_to(run_directory)
                if original.exists():
                    supervision = read_json(original)
                    if supervision.get('ownership_observation_coverage_status') == 'unknown':
                        resource_cleanup_facts.append({'command_directory': str(original.parent),
                            'host_boot_identity': native_host_boot_identity()})
                    if supervision.get('command_process_lifetime_contract') == ROLE_COMMAND_PROCESS_LIFETIME_CONTRACT:
                        from .role_owned_process_cleanup import role_owned_process_cleanup_observation
                        cleanup = role_owned_process_cleanup_observation(original.parent)
                        material = {'command_directory': str(original.parent), 'status': cleanup['status']}
                        if cleanup['status'] == 'unknown':
                            material.update(host_boot_identity=native_host_boot_identity(),
                                owned_processes=[native_process_identity(item['pid']) for item in cleanup.get('owned_processes', [])],
                                bounded_retry_due=cleanup.get('unchanged_recovery_attempt_count', 0) < 3
                                    and time.time() >= cleanup.get('retry_not_before', float('inf')))
                        elif cleanup['status'] == 'pending' and supervision.get('host_boot_identity'):
                            material['host_boot_identity'] = native_host_boot_identity()
                            if material['host_boot_identity'] == supervision['host_boot_identity']:
                                registered_supervisor_and_command_native_identity_matches = {}
                                for process_kind in ('supervisor', 'command'):
                                    registered_identity = supervision.get(process_kind + '_native_identity')
                                    if (recorded_native_process_identity_is_valid(registered_identity)
                                            and registered_identity['pid'] == supervision.get(process_kind + '_pid')):
                                        registered_supervisor_and_command_native_identity_matches[process_kind] = native_process_start_identity_matches(
                                            native_process_identity(registered_identity['pid']), registered_identity)
                                material['registered_supervisor_and_command_native_identity_matches'] = registered_supervisor_and_command_native_identity_matches
                        resource_cleanup_facts.append(material)
                    native_command_facts.add(canonical_json_material_sha256({
                        key: supervision.get(key) for key in ("argv", "status", "returncode", "source_tree_after", "stdout_sha256", "stderr_sha256")
                    } if supervision.get("status") == "completed" else {"argv": supervision.get("argv"), "status": "command_result_not_proven"}))
        originals["native_registered_command_result_materials"] = sorted(native_command_facts)
        originals['role_owned_resource_cleanup_materials'] = resource_cleanup_facts
        for request_file in sorted(run_directory.glob("*/attempt-*/request.json")):
            config = read_json(request_file).get("config", {})
            for source in [config.get("configuration_file_path"), *config.get("credential_evidence_paths", [])]:
                if source:
                    path = Path(source).expanduser()
                    originals[str(path)] = file_sha256(path) if path.is_file() else "unavailable"
            names = config.get("credential_environment_variable_names", [])
            originals["credential_environment:" + canonical_json_material_sha256(names)] = canonical_json_material_sha256({name: os.environ.get(name) for name in names})
        for eligibility_file in [run_directory / "reviewer-candidate-eligibility.json", *run_directory.glob("*/reviewer-eligibility.json")]:
            if not eligibility_file.exists():
                continue
            for eligibility in read_json(eligibility_file).get("observations", []):
                sources = [*eligibility.get("credential_evidence_paths", []), *eligibility.get("configuration_evidence_paths", []),
                    eligibility.get("evidence_path"), eligibility.get("account_status_evidence_path")]
                for source in sources:
                    if source:
                        path = Path(source).expanduser()
                        originals[str(path)] = task_review_recovery_original_without_progress_metadata(path) if path.is_file() else "unavailable"
                recovery = eligibility.get("recovery", {})
                retry_at = recovery.get("retry_not_before_epoch_ms")
                if retry_at and recovery.get("maximum_unchanged_retries", 0) > 0 and time.time() * 1000 >= retry_at:
                    originals[str(eligibility_file.relative_to(run_directory)) + ":bounded_retry_due"] = True
        for name in ("requirements", "patch", "review_input", "decision_facts"):
            original_name = {"patch": "task-changes.patch", "review_input": "review-input.md", "decision_facts": "decision-facts.json"}.get(name, "requirements.md")
            if file_sha256(run_directory / original_name) != record[name + "_sha256"]:
                raise ValueError("冻结输入原件摘要不匹配")
        return {"status": "known", "evidenceSha256": canonical_json_material_sha256({
            "binding": machine_evidence_request_binding(run_directory, record),
            "host_inputs": registered_host_recovery_inputs(run_directory, plan), "originals": originals})}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return {"status": "unknown", "evidenceSha256": None, "missing_evidence": str(error),
            "recovery_condition": "bound_originals_and_readonly_observation_available"}
