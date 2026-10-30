"""快照评审的两个主入口共用流程；提交和 base 同步由主会话处理。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .persistent_artifacts import exclusive_lock, git, git_text, read_json, verify_repository_identity, write_bytes, write_json
from .role_process_execution import launch_role, process_lock_is_held, reconcile_completed_role_attempt
from .task_snapshot import capture_worktree_tree
from .execution_lifecycle import (
    ExecutionRevoked, append_stage_event, ensure_execution_not_revoked, execution_is_revoked,
    publish_business_result, register_owned_process, revoke_execution,
    persist_execution_revocation,
    finish_owned_process_registration, register_supervised_command_directory,
)
from .machine_evidence import collect_shared_machine_evidence, collect_incomplete_fixer_machine_validation, prepare_isolated_workspace, verified_execution_plan, ensure_previous_supervised_commands_exited


def skill_directory() -> Path:
    for root in Path(__file__).resolve().parents:
        for candidate in (root / "plugins/review-validate-fix/skills/review-validate-fix", root / "skills/review-validate-fix"):
            if (candidate / "prompts/reviewer.md").is_file():
                return candidate
    raise RuntimeError("无法定位插件角色模板")


def reviewer_configuration_matches_harness_constraint(config: dict, constraint: str) -> bool:
    from .harness_cli_adapters import is_codex_exec_command
    if constraint == "codex_external_cli":
        return is_codex_exec_command(config["command"])
    if constraint == "non_codex_external_cli":
        from .harness_cli_adapters import (
            is_claude_print_command, is_cursor_print_command,
            is_kimi_prompt_command, is_oh_my_pi_print_command,
        )
        # 只接受可识别的直接 harness；通用解释器和自定义启动器无法证明身份。
        return config.get("output_format") != "codex_json" and any(
            identify(config["command"]) for identify in (
                is_claude_print_command, is_cursor_print_command,
                is_kimi_prompt_command, is_oh_my_pi_print_command))
    if constraint != "any_external_cli":
        raise ValueError(f"未知 reviewer harness 约束：{constraint}")
    return True


def default_reviewer_configurations(*, harness_constraint: str = "any_external_cli") -> list[dict]:
    directory = skill_directory()
    from core.rvf_user_local_data_paths import resolved_rvf_reviewer_registry_path
    registry = read_json(resolved_rvf_reviewer_registry_path(directory / "config/reviewer-registry.json"))
    available = []
    for item in sorted(registry["harnesses"].values(), key=lambda item: -item.get("priority_default", 0)):
        if not item.get("enabled"):
            continue
        config = load_reviewer_configuration(directory / item["config_path"])
        if (config.get("enabled", True) and reviewer_configuration_matches_harness_constraint(config, harness_constraint)
                and shutil.which(config["command"][0])):
            available.append((item.get("underlying_provider_account_quota_pool_identity"), config))
    # 优先不同额度池；每个角色仍使用独立新进程与工作树。
    if len(available) > 1:
        second = next((i for i, item in enumerate(available[1:], 1) if item[0] != available[0][0]), 1)
        available[1], available[second] = available[second], available[1]
    configs = [item[1] for item in available]
    if len(configs) == 1:
        configs.append(configs[0])
    if len(configs) < 2 and harness_constraint == "any_external_cli":
        raise RuntimeError("没有可执行的 reviewer CLI；可通过 --reviewer-config 提供其他独立 CLI")
    return configs


def load_reviewer_configuration(path: Path) -> dict:
    from .harness_cli_adapters import load_effective_alternative_reviewer_config
    config, _ = load_effective_alternative_reviewer_config(path)
    executable_name = Path(config["command"][0]).name
    effective_environment = dict(os.environ)
    for name in config.get("env_unset", []):
        effective_environment.pop(name, None)
    effective_environment.update(config.get("env_set", {}))
    if effective_environment.get("HOME"):
        effective_home = Path(effective_environment["HOME"])
    else:
        import pwd
        effective_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    credential_paths = {
        "codex": [str(Path(effective_environment.get("CODEX_HOME") or effective_home / ".codex") / "auth.json")],
        "claude": [str(Path(effective_environment.get("CLAUDE_CONFIG_DIR") or effective_home / ".claude") / ".credentials.json")],
    }
    return {**config, "credential_evidence_paths": config.get("credential_evidence_paths", credential_paths.get(executable_name, [])), 'configuration_file_path':str(path.resolve())}


def persistent_configuration(config: dict) -> dict:
    frozen=dict(config)
    credential_paths = frozen.get("credential_evidence_paths", [])
    if not isinstance(credential_paths, list) or any(not isinstance(path, str) or "\0" in path for path in credential_paths):
        raise ValueError("credential_evidence_paths 必须是凭据证据路径字符串列表")
    if credential_paths:
        observations = {}
        for path in credential_paths:
            try:
                observations[path] = hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
            except OSError as error:
                observations[path] = type(error).__name__
        frozen["credential_evidence_sha256"] = hashlib.sha256(json.dumps(observations, sort_keys=True).encode()).hexdigest()
    secrets=frozen.pop('env_set',{})
    if secrets:
        frozen['credential_environment_variable_names']=sorted(secrets)
        frozen['credential_environment_sha256']=hashlib.sha256(json.dumps(secrets,sort_keys=True).encode()).hexdigest()
    elif frozen.get('credential_environment_variable_names'):
        current = {name: os.environ[name] for name in frozen['credential_environment_variable_names'] if name in os.environ}
        if len(current) == len(frozen['credential_environment_variable_names']):
            frozen['credential_environment_sha256'] = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
    return frozen


def configuration_digest(config: dict) -> str:
    executable = shutil.which(config["command"][0])
    executable_identity = hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None
    return hashlib.sha256(json.dumps({"configuration": persistent_configuration(config), "executable_sha256": executable_identity}, sort_keys=True).encode()).hexdigest()


def _role_attempt_status(attempt: Path) -> str | None:
    reconcile_completed_role_attempt(attempt)
    outcome_file = attempt / "outcome.json"
    reconciliation_file = attempt / "reconciled-native-completion.json"
    # 补证状态用于接续，原始失败记录保持不变。
    if reconciliation_file.exists():
        return read_json(reconciliation_file).get("status")
    return read_json(outcome_file).get("status") if outcome_file.exists() else None


def _role_result(role_directory: Path, *, latest: bool = False, include_incomplete: bool = False) -> dict | None:
    partial_findings = []
    attempts = sorted(role_directory.glob("attempt-*"), reverse=latest)
    for attempt in attempts:
        status = _role_attempt_status(attempt)
        if status is None:
            continue
        if (status == "completed" or include_incomplete and status == "incomplete") and (attempt / "result.json").exists():
            result = read_json(attempt / "result.json")
            if not latest and "findings" in result:
                result["findings"] = partial_findings + result["findings"]
            return {"attempt_directory": str(attempt), "result": result}
        if latest and not include_incomplete:
            return None
        if (attempt / "result.json").exists():
            partial_findings.extend(read_json(attempt / "result.json").get("findings", []))
    return None


def verified_context_response_original_path(run_directory: Path, response_sha256: str | None) -> Path | None:
    if response_sha256 is None:
        return None
    original = run_directory / "context-response-originals-by-sha256" / f"{response_sha256}.md"
    if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != response_sha256:
        raise ValueError("补证原件缺失或内容已改变；恢复匹配摘要的原件后接续，保留现有尝试")
    return original


def _start_role(run_directory: Path, record: dict, role_directory: Path, role: str, configurations: list[dict], findings: list[dict], *, force_new: bool = False) -> tuple[dict | None, subprocess.Popen | None]:
    ensure_execution_not_revoked(run_directory)
    completed = _role_result(role_directory, latest=role == "fixer")
    if completed and not force_new:
        return completed, None
    role_directory.mkdir(exist_ok=True)
    attempts = sorted(role_directory.glob("attempt-*"))
    if any(process_lock_is_held(attempt) for attempt in attempts):
        return None, None
    for attempt in attempts:
        if (attempt/'request.json').exists() and not (attempt/'outcome.json').exists():
            if not (attempt/'model-invocation.json').exists():
                return None,launch_role(attempt)
            write_json(attempt/'outcome.json',{'status':'failed','retryable':True,'error':'执行中断，原进程已经结束但结果未知；保留原始输出并补跑此腿'})
    chosen = None
    candidate_commit = record.get("validated_candidate_commit", record["candidate_commit"]) if role == "fixer" else record["candidate_commit"]
    fix_application_base_commit = None
    validation_failure = read_json(run_directory / "validation.json") if role == "fixer" and (run_directory / "validation.json").exists() else {}
    if validation_failure.get("status") == "failed":
        candidate_commit = validation_failure["tested_commit"]
    if role == "fixer":
        progress = _role_result(role_directory, latest=True, include_incomplete=True)
        if progress and (validation_failure.get("status") != "failed" or _role_attempt_status(Path(progress["attempt_directory"])) == "incomplete"):
            progress_intent = _snapshot_fixer_progress(run_directory, record, progress)
            candidate_commit = progress_intent["after"]
            if not _matches_tree(Path(record["repo"]), candidate_commit, progress_intent["paths"], run_directory):
                fix_application_base_commit = progress_intent["before"]
    for config in configurations:
        identity = configuration_digest(config)
        matching_configuration_attempt_outcomes = [(read_json(attempt / "request.json"), read_json(attempt / "outcome.json"))
            for attempt in attempts if (attempt / "request.json").exists() and (attempt / "outcome.json").exists()
            and read_json(attempt / "request.json")["configuration_digest"] == identity]
        if any(outcome.get("recovery", {}).get("category") in {"configuration", "deterministic"}
               for _, outcome in matching_configuration_attempt_outcomes):
            continue
        # 准备失败只在每次 controller 接续时复核一次，不占用模型传输重试预算。
        if any(outcome.get("recovery", {}).get("category") == "workspace_preparation"
               and request.get("preparation_recovery_cycle") == record.get("preparation_recovery_cycle")
               for request, outcome in matching_configuration_attempt_outcomes):
            continue
        # 凭据故障预算独立于候选、机器事实和补证摘要，避免无关环境变化绕过冷却。
        credential_failures = [outcome for _, outcome in matching_configuration_attempt_outcomes
            if outcome.get("recovery", {}).get("category") in {"authentication", "usage_limit"}]
        if credential_failures:
            if len(credential_failures) >= 2 or time.time() < credential_failures[-1].get("retry_not_before", float("inf")):
                continue
            chosen = config
            break
        matching = [attempt for attempt in attempts if (attempt / "request.json").exists()
                    and read_json(attempt / "request.json")["configuration_digest"] == identity
                    and (read_json(attempt / "request.json")["candidate_commit"] == candidate_commit
                         or ((attempt / "outcome.json").exists()
                             and read_json(attempt / "outcome.json").get("status") == "incomplete"))
                    and read_json(attempt / "request.json").get("findings", []) == findings
                    and read_json(attempt/'request.json').get('context_response_sha256') == record.get('context_response_sha256')
                    and read_json(attempt/'request.json').get('shared_machine_evidence_recovery_sha256') == record.get('shared_machine_evidence_recovery_sha256')
                    and not ((attempt / "outcome.json").exists() and read_json(attempt / "outcome.json").get("recovery", {}).get("category") == "workspace_preparation")
                    # 历史超时原件保留，但不消耗当前无总时限执行的故障重试资格。
                    and not ((attempt / "outcome.json").exists() and read_json(attempt / "outcome.json").get("recovery", {}).get("category") == "deadline_exhausted")
                    and not ((attempt/'outcome.json').exists() and read_json(attempt/'outcome.json').get('status')=='cancelled')]
        if any((attempt / "outcome.json").exists() and read_json(attempt / "outcome.json").get("retryable") is False for attempt in matching):
            continue
        if len(matching) < 2:
            chosen = config
            break
    if chosen is None:
        write_json(role_directory / "progress.json", {"status": "waiting", "last_observed_at": time.time(),
            "waiting_reason": "registered_configuration_or_recovery_evidence_required",
            "recovery_condition": "configuration_credentials_machine_evidence_changed_or_retry_due"})
        return None, None
    if role == "fixer" and record.get("execution_plan_sha256"):
        plan = read_json(run_directory / "execution-plan.json")
        # CLI 传输失败复用同一语义轮；完成后再修复才消耗下一轮，配置变化不重置预算。
        same_round = bool(attempts and not force_new and _role_attempt_status(attempts[-1]) in {"failed", "incomplete", None})
        used = record.get("semantic_repair_rounds_used", 0)
        if not same_round and used >= plan["maximum_semantic_repair_rounds"]:
            record["repair_budget_exhausted"] = True
            write_json(run_directory / "run.json", record)
            return None, None
        record["semantic_repair_rounds_used"] = used if same_round else used + 1
        write_json(run_directory / "run.json", record)
    attempt_directory = role_directory / f"attempt-{len(attempts) + 1:03d}"
    attempt_directory.mkdir()
    workspace = attempt_directory / "workspace"
    result_file = attempt_directory / "role-output.json"
    request = {
        "role": role, "config": persistent_configuration(chosen), "configuration_digest": configuration_digest(chosen),
        "workspace": str(workspace), "input_file": str(attempt_directory / "request.json"),
        "repository": record['repo'],
        "result_file": str(result_file), "findings": findings,
        "candidate_commit": candidate_commit,
        "fix_application_base_commit": fix_application_base_commit,
        "run_directory": str(run_directory),
        "validation_failure": validation_failure if validation_failure.get("status") == "failed" else None,
        "preparation_commands": read_json(run_directory / "execution-plan.json").get("preparation_commands", []) if record.get("execution_plan_sha256") else [],
        "semantic_repair_round": record.get("semantic_repair_rounds_used") if role == "fixer" else None,
        "preparation_recovery_cycle": record.get("preparation_recovery_cycle"),
        "shared_machine_evidence": record.get("shared_machine_evidence"),
        "shared_machine_evidence_recovery_sha256": record.get("shared_machine_evidence_recovery_sha256"),
        "incomplete_fixer_machine_validation_evidence": record.get("incomplete_fixer_machine_validation_evidence") if role == "fixer" else None,
        "context_response_sha256": record.get('context_response_sha256'),
        "context_response_file": str(verified_context_response_original_path(run_directory, record['context_response_sha256'])) if record.get('context_response_sha256') else None,
    }
    template = (skill_directory() / "prompts" / ("reviewer.md" if role == "reviewer" else "validate-fix.md")).read_text()
    if role == "reviewer":
        inputs = f"\n评审输入：{run_directory / 'review-input.md'}\n"
        partials=[attempt/'result.json' for attempt in attempts if (attempt/'result.json').exists()]
        if partials:
            inputs+='本腿先前发现与缺口：'+', '.join(str(path) for path in partials)+'；保留发现并接续未完成检查。\n'
    else:
        inputs = f"\n需求原文：{run_directory / 'requirements.md'}\n匿名发现：{run_directory / 'findings.json'}\n"
        partials = [attempt / "result.json" for attempt in attempts if (attempt / "result.json").exists()]
        if partials:
            request["previous_result_files"] = [str(path) for path in partials]
            inputs += "先前验证修复处置：" + ", ".join(str(path) for path in partials) + "；当前工作树已保留此前修复，逐条接续未完成问题。\n"
        if request["validation_failure"]:
            inputs += f"机器验证失败（不属于 reviewer finding）：{run_directory / 'validation.json'}；当前基线为真实 tested_commit，读取失败命令与日志并修复，然后由执行器重新验证。\n"
    if record.get("execution_plan_sha256"):
        inputs += f"机器上下文与预登记命令：{run_directory / 'execution-plan.json'}。共享机器事实按需读取 {run_directory / 'machine-evidence'}，只适用于各证据声明的完整树和环境。\n"
    if record.get('context_response_sha256'):
        inputs+=f"通用补证：{request['context_response_file']}；历次通用补证原件按需读取 {run_directory/'context-response-originals-by-sha256'}，不改变冻结需求与范围，不包含另一腿的结论。\n"
    prompt = template.strip() + "\n" + inputs + f"\n结果可写入 {result_file}，或在最终回复中只输出结果 JSON。\n"
    write_bytes(attempt_directory / "prompt.md", prompt.encode())
    write_json(attempt_directory / "request.json", request)
    append_stage_event(run_directory, "role_dispatch", role=role_directory.name, attempt=str(attempt_directory), candidate_commit=candidate_commit)
    return None, launch_role(attempt_directory,environment_overrides=chosen.get('env_set',{}))


def _run_roles(run_directory: Path, record: dict, roles: list[tuple[str, str, list[dict]]], findings: list[dict], *, force_new: bool = False) -> list[dict | None]:
    active = {}
    try:
        for name, role, configs in roles:
            _, process = _start_role(run_directory, record, run_directory / name, role, configs, findings, force_new=force_new)
            if process:
                active[name] = process
        while active:
            ensure_execution_not_revoked(run_directory)
            for name, process in list(active.items()):
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
                del active[name]
                role_directory = run_directory / name
                role_config = next(item for item in roles if item[0] == name)
                progress = _role_result(role_directory, latest=True, include_incomplete=True) if role_config[1] == "fixer" else None
                if (progress and progress["result"]["status"] == "incomplete"
                        and Path(progress["attempt_directory"]) == sorted(role_directory.glob("attempt-*"))[-1]):
                    continue
                if _role_result(role_directory, latest=role_config[1] == "fixer") is None:
                    _, replacement = _start_role(run_directory, record, role_directory, role_config[1], role_config[2], findings)
                    if replacement:
                        active[name] = replacement
    except (KeyboardInterrupt, ExecutionRevoked):
        persist_execution_revocation(run_directory, "用户停止角色执行")
        for process in active.values():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in active.values():
            if process.poll() is None:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        record['status'] = 'cancelled' if execution_is_revoked(run_directory) else 'interrupted'
        write_json(run_directory / 'run.json', record)
        raise
    if force_new and record.get("repair_budget_exhausted"):
        return [None for _ in roles]
    return [_role_result(run_directory / name, latest=role == "fixer") for name, role, _ in roles]


def anonymous_findings(results: list[dict]) -> list[dict]:
    merged = {}
    for result in results:
        for original in result["result"]["findings"]:
            finding = {key: original[key] for key in ("problem_key", "path", "line", "message", "severity", "evidence", "scope_relation") if key in original}
            identity = finding.get("problem_key") or json.dumps({key: finding.get(key) for key in ("path", "message")}, sort_keys=True, ensure_ascii=False)
            finding_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
            if finding_id in merged:
                if finding != {key: value for key, value in merged[finding_id].items() if key != "finding_id"}:
                    observations = merged[finding_id].setdefault("additional_evidence", [])
                    if finding not in observations:
                        observations.append(finding)
            else:
                merged[finding_id] = {"finding_id": finding_id, **finding}
    return list(merged.values())


def _matches_tree(repo: Path, expected: str, paths: list[str], run_directory: Path) -> bool:
    current_tree = capture_worktree_tree(repo, paths, run_directory, head=expected)
    return not git(repo, "diff", "--name-only", expected, current_tree, "--", *paths)


def _snapshot_fixer_progress(run_directory: Path, record: dict, result: dict) -> dict:
    repo = Path(record["repo"])
    attempt_directory = Path(result["attempt_directory"])
    intent_file = attempt_directory / "fix-application.json"
    patch_file = attempt_directory / "fixer.patch"
    requires_changes = any(item["status"] == "fixed" for item in result["result"]["dispositions"])
    if intent_file.exists():
        intent = read_json(intent_file)
        intent["applied"] = False
    else:
        request = read_json(attempt_directory / "request.json")
        before = request.get("fix_application_base_commit") or request["candidate_commit"]
        workspace = attempt_directory / "workspace"
        preparation_paths_file = attempt_directory / "preparation-created-untracked-paths.json"
        fixed_tree = capture_worktree_tree(workspace, ["."], run_directory,
            excluded_preparation_paths=read_json(preparation_paths_file)["paths"] if preparation_paths_file.exists() else None)
        fixed_commit = git_text(repo, "commit-tree", fixed_tree, "-p", request["candidate_commit"], input_bytes=b"RVF validated fixes\n", environment={
            "GIT_AUTHOR_NAME": "RVF Fixes", "GIT_AUTHOR_EMAIL": "fixes@rvf.local",
            "GIT_COMMITTER_NAME": "RVF Fixes", "GIT_COMMITTER_EMAIL": "fixes@rvf.local"})
        git(repo, "update-ref", record["snapshot_reference"] + "-fix", fixed_commit)
        patch = git(repo, "diff", "--binary", "--full-index", before, fixed_commit)
        paths = [path for path in git(repo, "diff", "--name-only", "-z", before, fixed_commit).decode().split("\0") if path]
        write_bytes(patch_file, patch)
        intent = {"before": before, "after": fixed_commit, "paths": paths, "applied": False}
    intent["requires_changes"] = requires_changes
    write_json(intent_file, intent)
    return intent


def _prepare_and_apply_fix(run_directory: Path, record: dict, result: dict) -> bool:
    ensure_execution_not_revoked(run_directory)
    repo = Path(record["repo"])
    attempt_directory = Path(result["attempt_directory"])
    intent_file = attempt_directory / "fix-application.json"
    patch_file = attempt_directory / "fixer.patch"
    intent = _snapshot_fixer_progress(run_directory, record, result)
    applied_patch_file = patch_file
    head = git_text(repo, "rev-parse", "HEAD")
    prior_base = record.get("candidate_base_head", record["original_head"])
    if intent["paths"] and head != prior_base and not _matches_tree(repo, intent["after"], intent["paths"], run_directory):
        try:
            git(repo, "merge-base", "--is-ancestor", prior_base, head)
            rebased = {}
            for side in ("before", "after"):
                tree = git_text(repo, "merge-tree", "--write-tree", f"--merge-base={prior_base}", intent[side], head).splitlines()[0]
                rebased[side] = git_text(repo, "commit-tree", tree, "-p", head, input_bytes=b"RVF repair on advanced local HEAD\n")
            if not any(_matches_tree(repo, rebased[side], intent["paths"], run_directory) for side in ("before", "after")):
                raise ValueError("外部 HEAD 之外仍存在未评审的任务改动")
            intent["original_snapshots"] = {side: intent[side] for side in ("before", "after")}
            intent.update(rebased)
            applied_patch_file = attempt_directory / "fixer-on-advanced-head.patch"
            write_bytes(applied_patch_file, git(repo, "diff", "--binary", "--full-index", intent["before"], intent["after"]))
            intent["applied_patch_file"] = str(applied_patch_file)
            record["candidate_base_head"] = head
        except (RuntimeError, ValueError):
            intent["error"] = "外部 HEAD 无法与修复安全合并，保留原始补丁"
            write_json(intent_file, intent)
            write_json(run_directory / "fix-application.json", intent)
            write_bytes(run_directory / "fixer.patch", patch_file.read_bytes())
            return False
    elif intent.get("applied_patch_file"):
        applied_patch_file = Path(intent["applied_patch_file"])
    requires_changes = intent["requires_changes"]
    if not intent["paths"]:
        intent["applied"] = not requires_changes
        if requires_changes:
            intent["error"] = "fixer 声称 fixed 但未产生任何任务改动"
    elif _matches_tree(repo, intent["after"], intent["paths"], run_directory):
        intent["applied"] = True
    elif _matches_tree(repo, intent["before"], intent["paths"], run_directory):
        patch = applied_patch_file.read_bytes()
        git(repo, "apply", "--check", "-", input_bytes=patch)
        # 与撤销写入共用锁；Git 继承锁以覆盖 controller 退出后的真实副作用窗口。
        with exclusive_lock(run_directory / "execution-revocation.lock") as application_lock:
            ensure_execution_not_revoked(run_directory)
            git(repo, "apply", "-", input_bytes=patch, pass_fds=(application_lock.fileno(),))
        intent["applied"] = True
    write_json(intent_file, intent)
    write_bytes(run_directory / "fixer.patch", applied_patch_file.read_bytes())
    write_json(run_directory / "fix-application.json", intent)
    if intent["applied"]:
        record["validated_candidate_commit"] = intent["after"]
        record["effective_scope_paths"] = sorted(set(record.get("effective_scope_paths", record["scope_paths"]) + intent["paths"]))
    return intent["applied"]


def _merge_fixer_findings(run_directory: Path, findings: list[dict]) -> None:
    additional_file = run_directory / "fixer-new-findings.json"
    existing = read_json(additional_file)["findings"] if additional_file.exists() else []
    reported = []
    for attempt in sorted((run_directory / "fixer").glob("attempt-*")):
        if _role_attempt_status(attempt) not in {"completed", "incomplete"} or not (attempt / "result.json").exists():
            continue
        reported.extend(item for item in read_json(attempt / "result.json").get("new_findings", [])
                        if item.get("scope_relation") != "outside_task_scope")
    current = anonymous_findings([{"result": {"findings": reported}}])
    merged = {item["finding_id"]: item for item in [*existing, *current]}
    if merged:
        write_json(additional_file, {"findings": list(merged.values())})
    known = {item["finding_id"] for item in findings}
    findings.extend(item for key, item in merged.items() if key not in known)
    write_json(run_directory / "findings.json", {"findings": findings})


def _validate(run_directory: Path, record: dict, commands: list[list[str]], lock_handle) -> dict:
    if not commands or any(not isinstance(command, list) or not command or not all(isinstance(value, str) for value in command) for command in commands):
        raise ValueError("最终验证命令必须是非空 argv 数组列表")
    repo = Path(record["repo"])
    candidate = record.get("validated_candidate_commit", record["candidate_commit"])
    scope = record.get("effective_scope_paths", record["scope_paths"])
    if not _matches_tree(repo, candidate, scope, run_directory):
        head = git_text(repo, "rev-parse", "HEAD")
        prior_base = record.get("candidate_base_head", record["original_head"])
        try:
            if head == prior_base:
                raise ValueError("HEAD 未变化，候选存在新的未评审改动")
            merged_tree = git_text(repo, "merge-tree", "--write-tree", f"--merge-base={prior_base}", candidate, head).splitlines()[0]
            current_tree = capture_worktree_tree(repo, scope, run_directory, head=head)
            if git(repo, "diff", "--name-only", merged_tree, current_tree, "--", *scope):
                raise ValueError("当前任务内容不等于已评审补丁与新 HEAD 的安全合并")
            candidate = git_text(repo, "commit-tree", current_tree, "-p", head, input_bytes=b"RVF candidate on advanced local HEAD\n", environment={
                "GIT_AUTHOR_NAME": "RVF Snapshot", "GIT_AUTHOR_EMAIL": "snapshot@rvf.local",
                "GIT_COMMITTER_NAME": "RVF Snapshot", "GIT_COMMITTER_EMAIL": "snapshot@rvf.local"})
            record.update({"validated_candidate_commit": candidate, "candidate_base_head": head})
            git(repo, "update-ref", record["snapshot_reference"] + "-head-rebuild", candidate)
            append_stage_event(run_directory, "external_head_rebuilt", previous_base=prior_base, tested_head=head, candidate_commit=candidate)
        except (RuntimeError, ValueError):
            return {"status": "candidate_changed", "returncode": None}
    # 只将任务路径叠加到当前 HEAD；范围外 WIP 不参与最终候选验证。
    head = git_text(repo, "rev-parse", "HEAD")
    tested_tree = capture_worktree_tree(repo, scope, run_directory, head=head)
    prior = run_directory / "validation.json"
    # 任意登记命令的依赖与外部服务身份尚未获得证明；历史通过不授予当前执行资格。
    history_root = run_directory / "final-validation"
    history_root.mkdir(exist_ok=True)
    ensure_previous_supervised_commands_exited(run_directory, history_root)
    attempt_directory = history_root / f"attempt-{len(list(history_root.glob('attempt-*'))) + 1:03d}"
    attempt_directory.mkdir()
    validation = {"status": "running", "candidate_commit": candidate, "commands": commands, "completed_commands": 0,
                  "tested_tree": tested_tree, "tested_head": head, "attempt_directory": str(attempt_directory)}
    from .execution_lifecycle import runtime_environment_identity
    validation["runtime_environment"] = runtime_environment_identity()
    tested_commit = git_text(repo, "commit-tree", tested_tree, "-p", head,
        input_bytes=b"RVF final validation snapshot\n", environment={
            "GIT_AUTHOR_NAME": "RVF Validation", "GIT_AUTHOR_EMAIL": "validation@rvf.local",
            "GIT_COMMITTER_NAME": "RVF Validation", "GIT_COMMITTER_EMAIL": "validation@rvf.local"})
    workspace = attempt_directory / "workspace"
    git(repo, "worktree", "add", "--detach", str(workspace), tested_commit)
    validation.update({"workspace": str(workspace), "tested_commit": tested_commit})
    if record.get("execution_plan_sha256"):
        prepare_isolated_workspace(run_directory, workspace, read_json(run_directory / "execution-plan.json")["preparation_commands"], attempt_directory,
                                   inherited_lock_descriptors=(lock_handle.fileno(),))
    def save_validation():
        write_json(attempt_directory / "validation.json", validation)
        write_json(prior, validation)
    save_validation()
    preparation_paths = attempt_directory / 'preparation-created-untracked-paths.json'
    excluded = read_json(preparation_paths)['paths'] if preparation_paths.exists() else None
    for i in range(validation["completed_commands"], len(commands)):
        ensure_execution_not_revoked(run_directory)
        logs = attempt_directory / f"command-{i + 1:03d}"
        logs.mkdir()
        source_before = capture_worktree_tree(workspace, ['.'], logs, excluded_preparation_paths=excluded)
        command_record = {'argv': commands[i], 'cwd': str(workspace), 'source_tree_before': source_before}
        if source_before != tested_tree:
            validation['status'] = 'candidate_changed'
            command_record.update({'status': 'candidate_changed', 'execution_started': False})
            write_json(logs / 'execution.json', command_record)
            break
        with (logs / "stdout.txt").open("wb") as stdout, (logs / "stderr.txt").open("wb") as stderr:
            with exclusive_lock(run_directory / 'execution-revocation.lock'):
                ensure_execution_not_revoked(run_directory)
                register_supervised_command_directory(run_directory, logs)
                launch_command = [sys.executable, str(Path(__file__).with_name('retained_command_supervisor.py')),
                    str(run_directory), str(logs), json.dumps([lock_handle.fileno()]), json.dumps(commands[i])]
                try:
                    process = subprocess.Popen(launch_command, cwd=workspace, stdout=stdout, stderr=stderr, pass_fds=(lock_handle.fileno(),), start_new_session=True)
                except OSError as error:
                    command_record.update({'status': 'launch_failed', 'execution_started': False, 'finished_at': time.time(),
                        'native_launch_error': {'type': type(error).__name__, 'errno': error.errno, 'message': str(error)}})
                    write_json(logs / 'execution.json', command_record)
                    validation['status'] = 'launch_failed'
                    save_validation()
                    error.original_evidence_paths = [str(logs / 'execution.json')]
                    raise
                register_owned_process(run_directory, process.pid, "final_validation", owns_process_group=True)
            try:
                from .execution_lifecycle import wait_for_owned_process
                returncode = wait_for_owned_process(process, run_directory)
            except (KeyboardInterrupt, ExecutionRevoked):
                persist_execution_revocation(run_directory, "用户停止最终验证")
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                validation["status"] = "interrupted"
                save_validation()
                record["status"] = "interrupted"
                write_json(run_directory / "run.json", record)
                raise
        supervision_file = logs / 'command-supervision.json'
        if supervision_file.exists() and read_json(supervision_file).get('status') == 'descendant_exit_unproven':
            validation['status'] = 'machine_evidence_pending'
            save_validation()
            error = RuntimeError('最终验证命令的后代退出证据未知；等待原生归属查询恢复')
            error.original_evidence_paths = [str(supervision_file)]
            raise error
        validation["returncode"] = returncode
        source_after = capture_worktree_tree(workspace, ['.'], logs, excluded_preparation_paths=excluded)
        command_record.update({'returncode': returncode, 'source_tree_after': source_after,
            'status': 'passed' if returncode == 0 else 'failed'})
        if source_after != tested_tree:
            command_record['status'] = validation['status'] = 'candidate_changed'
        write_json(logs / 'execution.json', command_record)
        if validation['status'] == 'candidate_changed':
            break
        if returncode:
            validation["status"] = "failed"
            break
        validation["completed_commands"] = i + 1
        save_validation()
    else:
        unchanged = (not git(workspace, "diff", "--name-only", tested_commit)
                     and _matches_tree(repo, candidate, scope, run_directory)
                     and capture_worktree_tree(repo, scope, run_directory) == tested_tree)
        validation["status"] = "passed" if unchanged else "candidate_changed"
    save_validation()
    return validation


def _resume_review_iteration(run_directory: Path, *, reviewer_configs: list[dict] | None = None,
                  fix: bool = False, fixer_config: dict | None = None,
                  validation_commands: list[list[str]] | None = None,
                  context_response_file: Path | None = None) -> dict:
    run_directory = run_directory.expanduser().resolve()
    with exclusive_lock(run_directory / "workflow.lock", blocking=False) as lock_handle:
        record = read_json(run_directory / "run.json")
        ensure_execution_not_revoked(run_directory)
        repo = verify_repository_identity(record)
        if context_response_file is not None:
            verified_context_response_original_path(run_directory, record.get('context_response_sha256'))
            response=context_response_file.read_bytes()
            response_sha256 = hashlib.sha256(response).hexdigest()
            original = run_directory / 'context-response-originals-by-sha256' / f'{response_sha256}.md'
            if not original.exists():
                write_bytes(original, response)
            verified_context_response_original_path(run_directory, response_sha256)
            record['context_response_sha256']=response_sha256
            write_json(run_directory/'run.json',record)
        verified_context_response_original_path(run_directory, record.get('context_response_sha256'))
        if hashlib.sha256((run_directory / "review-input.md").read_bytes()).hexdigest() != record["review_input_sha256"]:
            raise ValueError("固定评审输入已改变；为新输入创建快照")
        if hashlib.sha256((run_directory / "requirements.md").read_bytes()).hexdigest() != record["requirements_sha256"] or hashlib.sha256((run_directory / "task-changes.patch").read_bytes()).hexdigest() != record["patch_sha256"]:
            raise ValueError("固定评审输入已改变；保留原 run 并为新输入创建快照")
        configurations_file = run_directory / "reviewer-configurations.json"
        uses_default_reviewer_pool = reviewer_configs is None and (not configurations_file.exists()
            or read_json(configurations_file).get("uses_default_reviewer_pool", False))
        if reviewer_configs is not None:
            if len(reviewer_configs) < 2:
                raise ValueError("需要两路独立 reviewer 配置（可使用同一 CLI 的两个新实例）")
            write_json(configurations_file, {"configurations": [persistent_configuration(config) for config in reviewer_configs]})
        elif configurations_file.exists() and not uses_default_reviewer_pool:
            reviewer_configs = read_json(configurations_file)["configurations"]
            reviewer_configs = [load_reviewer_configuration(Path(config['configuration_file_path']))
                                if config.get('configuration_file_path') and Path(config['configuration_file_path']).is_file()
                                else config for config in reviewer_configs]
            write_json(configurations_file, {"configurations": [persistent_configuration(config) for config in reviewer_configs]})
        else:
            constrained_codex_seat = "codex_external_cli" in record.get("reviewer_harness_constraints", [])
            reviewer_configs = default_reviewer_configurations(harness_constraint="non_codex_external_cli") if constrained_codex_seat else default_reviewer_configurations()
        constraints = record.get("reviewer_harness_constraints", ["any_external_cli"] * record["reviewer_count"])
        if len(constraints) != record["reviewer_count"]:
            raise ValueError("冻结的 reviewer harness 约束与数量不一致")
        if "codex_external_cli" in constraints and not any(reviewer_configuration_matches_harness_constraint(config, "codex_external_cli") for config in reviewer_configs):
            reviewer_configs = [*reviewer_configs, *default_reviewer_configurations(harness_constraint="codex_external_cli")]
        write_json(configurations_file, {"configurations": [persistent_configuration(config) for config in reviewer_configs],
                                         "uses_default_reviewer_pool": uses_default_reviewer_pool})
        reviewer_count = record["reviewer_count"]
        roles = []
        configuration_recovery = []
        for reviewer_index, constraint in enumerate(constraints):
            eligible = [config for config in reviewer_configs if reviewer_configuration_matches_harness_constraint(config, constraint)]
            primary_index = reviewer_index % len(eligible) if eligible else 0
            configurations = [eligible[primary_index], *eligible[:primary_index], *eligible[primary_index + 1:]] if eligible else []
            roles.append((f"reviewer-{reviewer_index + 1}", "reviewer", configurations))
            if not configurations and _role_result(run_directory / f"reviewer-{reviewer_index + 1}") is None:
                configuration_recovery.append({"role": f"reviewer-{reviewer_index + 1}", "harness_constraint": constraint,
                    "trigger": "reviewer_configuration_or_cli_availability_changed", "action": "resume_same_run",
                    "maximum_unchanged_retries": 0})
        record["reviewer_configuration_recovery"] = configuration_recovery
        write_json(run_directory / "run.json", record)
        results = _run_roles(run_directory, record, roles, [])
        if any(result is None for result in results):
            record["status"] = "review_incomplete"
            record["recovery_condition"] = "等待在运行的角色进程结束，或提供修复后的 CLI 配置；已完成的腿不会重跑"
        else:
            findings = anonymous_findings(results)
            _merge_fixer_findings(run_directory, findings)
            record["finding_count"] = len(findings)
            record["status"] = "ready_for_fix" if findings else "ready_for_validation"
            fixer_result = _role_result(run_directory / "fixer", latest=True)
            prior_validation = read_json(run_directory / "validation.json") if (run_directory / "validation.json").exists() else {}
            prior_application = read_json(run_directory / "fix-application.json") if (run_directory / "fix-application.json").exists() else {}
            needs_new_fix = bool(fix and fixer_result and (
                any(item["status"] == "unresolved" for item in fixer_result["result"]["dispositions"])
                or any(item.get("scope_relation") != "outside_task_scope" for item in fixer_result["result"].get("new_findings", []))
                or prior_validation.get("status") == "failed"
                or (prior_application.get("requires_changes") and not prior_application.get("paths") and not prior_application.get("applied"))
            ))
            if (findings or prior_validation.get("status") == "failed") and (fix or fixer_result):
                needs_new_fix = needs_new_fix or bool(fixer_result and prior_validation.get("status") == "failed")
                if not fixer_result or needs_new_fix:
                    configs = [fixer_config] if fixer_config else reviewer_configs
                    fixer_result = _run_roles(run_directory, record, [("fixer", "fixer", configs)], findings, force_new=needs_new_fix)[0]
                _merge_fixer_findings(run_directory, findings)
                record["finding_count"] = len(findings)
                if not fixer_result:
                    record["status"] = "fix_incomplete"
                    progress = _role_result(run_directory / "fixer", latest=True, include_incomplete=True)
                    if progress:
                        intent = _snapshot_fixer_progress(run_directory, record, progress)
                        if progress["result"].get("completion_blocker") == "registered_machine_validation" and record.get("execution_plan_sha256"):
                            evidence = collect_incomplete_fixer_machine_validation(run_directory, record, intent["after"])
                            if evidence is not None and record.get("incomplete_fixer_machine_validation_evidence") != str(evidence):
                                previous = verified_context_response_original_path(run_directory, record.get("context_response_sha256"))
                                response = (f"执行器已对保留修复树运行启动时冻结的最终验证，原始 incomplete 不变，语义结论仍由本角色判断。\n"
                                    f"机器原件：{evidence}\n原通用补证：{previous or '无'}\n"
                                    "核对完整树、命令、cwd 和日志后接续；不在受限沙箱重复同一机器命令。\n").encode()
                                digest = hashlib.sha256(response).hexdigest()
                                original = run_directory / "context-response-originals-by-sha256" / f"{digest}.md"
                                write_bytes(original, response)
                                record.update({"context_response_sha256": digest,
                                    "incomplete_fixer_machine_validation_evidence": str(evidence), "status": "machine_evidence_available"})
                else:
                    new_findings = fixer_result["result"].get("new_findings", [])
                    in_scope_new = anonymous_findings([{"result": {"findings": [item for item in new_findings if item.get("scope_relation") != "outside_task_scope"]}}])
                    if not _prepare_and_apply_fix(run_directory, record, fixer_result):
                        application = read_json(run_directory / "fix-application.json")
                        if application.get("requires_changes") and not application.get("paths"):
                            record["status"] = "fix_incomplete"
                        else:
                            record["status"] = "fix_application_pending"
                            record["recovery_condition"] = "主工作树中的修复目标恢复到原候选，或由主 Agent 合并并核对同一补丁后接续"
                    elif any(item["status"] == "unresolved" for item in fixer_result["result"]["dispositions"]) or in_scope_new:
                        record["status"] = "findings_unresolved"
                    else:
                        record["status"] = "ready_for_validation"
            if record["status"] == "ready_for_validation":
                current_candidate = record.get("validated_candidate_commit", record["candidate_commit"])
                if validation_commands is not None:
                    validation = _validate(run_directory, record, validation_commands, lock_handle)
                elif (run_directory / "validation.json").exists():
                    validation = read_json(run_directory / "validation.json")
                    if validation.get("candidate_commit") != current_candidate:
                        validation = {}
                    elif (validation.get("status") in {"running", "interrupted", "passed", "launch_failed"}
                          or validation.get("tested_tree") != capture_worktree_tree(
                              repo, record.get("effective_scope_paths", record["scope_paths"]), run_directory)):
                        validation = _validate(run_directory, record, validation["commands"], lock_handle)
                else:
                    validation = {}
                if validation.get("status") == "passed":
                    expected = record.get("validated_candidate_commit", record["candidate_commit"])
                    scope = record.get("effective_scope_paths", record["scope_paths"])
                    if (not _matches_tree(repo, expected, scope, run_directory)
                            or validation.get("tested_tree") != capture_worktree_tree(repo, scope, run_directory)):
                        record["status"] = "candidate_changed"
                    else:
                        record["status"] = "ready_to_commit"
                        head = git_text(repo, "rev-parse", "HEAD")
                        if (git_text(repo, "rev-parse", "HEAD^{tree}") == validation["tested_tree"]
                                and git(repo, "diff", "--name-only", record["base_commit"], head, "--", *scope)
                                and not git(repo, "diff", "--name-only", expected, head, "--", *scope)):
                            git(repo, "merge-base", "--is-ancestor", record["original_head"], head)
                            record.update({"status": "completed", "commit": head})
                elif validation:
                    record["status"] = "validation_failed" if validation["status"] == "failed" else "candidate_changed"
        if record["status"] not in ("review_incomplete", "fix_application_pending"):
            record.pop("recovery_condition", None)
        write_json(run_directory / "run.json", record)
        next_actions={
            'ready_for_fix':f'rvf resume-task-review --run-dir {run_directory} --fix',
            'ready_for_validation':f'rvf resume-task-review --run-dir {run_directory} --validation-commands <验证 argv JSON 文件>',
            'ready_to_commit':'主会话依据授权精确暂存并提交，再接续原 run 核对实际提交',
            'fix_application_pending':f'读取 {run_directory/"fixer.patch"} 并核对目标文件的并发改动；原补丁保留',
        }
        return {"run_directory": str(run_directory), "status": record["status"], "reviewer_count": reviewer_count,
                "finding_count": record.get("finding_count", 0), **({"commit": record["commit"]} if record.get("commit") else {}),
                **({'next_action':next_actions[record['status']]} if record['status'] in next_actions else {}),
                **({"recovery_condition": record["recovery_condition"]} if record.get("recovery_condition") else {})}


def resume_review(run_directory: Path, *, reviewer_configs: list[dict] | None = None,
                  fix: bool = False, fixer_config: dict | None = None,
                  validation_commands: list[list[str]] | None = None,
                  context_response_file: Path | None = None, closeout_report_file: Path | None = None) -> dict:
    run_directory = run_directory.expanduser().resolve()
    with exclusive_lock(run_directory / "controller.lock", blocking=False):
        record = read_json(run_directory / "run.json")
        if os.environ.get("KANBAN_COMMAND_EXECUTION_REVOCATION_FILE"):
            record["command_execution_revocation_file"] = os.environ["KANBAN_COMMAND_EXECUTION_REVOCATION_FILE"]
            write_json(run_directory / "run.json", record)
        if execution_is_revoked(run_directory):
            revoke_execution(run_directory, "接续时补齐已撤销执行的取消证据")
            return publish_business_result(run_directory, {"status": "cancelled", "run_directory": str(run_directory)})
        plan = {}
        if record.get("execution_plan_sha256"):
            plan = verified_execution_plan(run_directory, record)
            if fix and not plan["fix"]:
                raise ValueError("接续不能增加启动时未冻结的修复意图；请为新候选登记完整启动材料")
            if validation_commands is not None and validation_commands != plan["validation_commands"]:
                raise ValueError("接续不能替换已冻结的最终验证命令")
            fix = plan["fix"]
            validation_commands = plan["validation_commands"] or None
        if plan.get("preparation_commands"):
            verified_context_response_original_path(run_directory, record.get("context_response_sha256"))
            record["preparation_recovery_cycle"] = record.get("preparation_recovery_cycle", 0) + 1
            write_json(run_directory / "run.json", record)
        if closeout_report_file is not None:
            from .execution_lifecycle import record_closeout_report
            record_closeout_report(run_directory, record, plan, closeout_report_file)
        if reviewer_configs is not None:
            if len(reviewer_configs) < 2:
                raise ValueError("需要两路独立 reviewer 配置")
            write_json(run_directory / "reviewer-configurations.json", {"configurations": [persistent_configuration(config) for config in reviewer_configs]})
        if fixer_config is not None:
            write_json(run_directory / "fixer-configuration.json", persistent_configuration(fixer_config))
        elif (run_directory / "fixer-configuration.json").exists():
            fixer_config = read_json(run_directory / "fixer-configuration.json")
            if fixer_config.get("configuration_file_path"):
                fixer_config = load_reviewer_configuration(Path(fixer_config["configuration_file_path"]))
        register_owned_process(run_directory, os.getpid(), "workflow_controller")
        try:
            pending_reviewers = any(_role_result(run_directory / f"reviewer-{index + 1}") is None for index in range(record["reviewer_count"]))
            shared_evidence = collect_shared_machine_evidence(run_directory, record, plan) if pending_reviewers else None
            if shared_evidence:
                record["shared_machine_evidence"] = shared_evidence["original"]
                record["shared_machine_evidence_recovery_sha256"] = hashlib.sha256(json.dumps({
                    "identity": shared_evidence["identity_sha256"], "status": shared_evidence["status"],
                    "commands": [{key: command.get(key) for key in ("argv", "returncode", "stdout_sha256", "stderr_sha256")}
                                 for command in shared_evidence["commands"]],
                }, sort_keys=True).encode()).hexdigest()
                write_json(run_directory / "run.json", record)
            for _ in range(2 * plan.get("maximum_semantic_repair_rounds", 0) + 2):
                ensure_execution_not_revoked(run_directory)
                result = _resume_review_iteration(run_directory, reviewer_configs=reviewer_configs, fix=fix,
                    fixer_config=fixer_config, validation_commands=validation_commands, context_response_file=context_response_file)
                context_response_file = None
                append_stage_event(run_directory, "workflow_progress", status=result["status"])
                if not plan or not fix or result["status"] not in {"validation_failed", "findings_unresolved", "machine_evidence_available"}:
                    return publish_business_result(run_directory, result)
            return publish_business_result(run_directory, result)
        except ExecutionRevoked:
            revoke_execution(run_directory, "所属执行已撤销")
            return publish_business_result(run_directory, {"status": "cancelled", "run_directory": str(run_directory)})
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            record = read_json(run_directory / "run.json")
            record.update({"status": "machine_evidence_pending", "recovery": {
                "missing_evidence": str(error), "equivalent_sources": ["registered_commands", "native_role_output"],
                "original_evidence_paths": getattr(error, "original_evidence_paths", []),
                "trigger": "environment_or_configuration_changed", "action": "resume_same_run", "maximum_unchanged_retries": 1,
                **getattr(error, 'recovery', {}),
            }})
            write_json(run_directory / "run.json", record)
            return publish_business_result(run_directory, {"status": record["status"], "run_directory": str(run_directory)})
        except KeyboardInterrupt:
            revoke_execution(run_directory, "用户明确停止 controller")
            record = read_json(run_directory / "run.json")
            record["status"] = "cancelled"
            write_json(run_directory / "run.json", record)
            publish_business_result(run_directory, {"status": "cancelled", "run_directory": str(run_directory)})
            raise
        finally:
            finish_owned_process_registration(run_directory, os.getpid(), "workflow_controller")
