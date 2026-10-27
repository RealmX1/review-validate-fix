"""快照评审的两个主入口共用流程；提交和 base 同步由主会话处理。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .persistent_artifacts import exclusive_lock, git, git_text, read_json, verify_repository_identity, write_bytes, write_json
from .role_process_execution import launch_role, process_lock_is_held, reconcile_completed_role_attempt
from .task_snapshot import capture_worktree_tree


def skill_directory() -> Path:
    for root in Path(__file__).resolve().parents:
        for candidate in (root / "plugins/review-validate-fix/skills/review-validate-fix", root / "skills/review-validate-fix"):
            if (candidate / "prompts/reviewer.md").is_file():
                return candidate
    raise RuntimeError("无法定位插件角色模板")


def default_reviewer_configurations() -> list[dict]:
    directory = skill_directory()
    from core.rvf_user_local_data_paths import resolved_rvf_reviewer_registry_path
    registry = read_json(resolved_rvf_reviewer_registry_path(directory / "config/reviewer-registry.json"))
    available = []
    for item in sorted(registry["harnesses"].values(), key=lambda item: -item.get("priority_default", 0)):
        if not item.get("enabled"):
            continue
        config = load_reviewer_configuration(directory / item["config_path"])
        if config.get("enabled", True) and shutil.which(config["command"][0]):
            available.append((item.get("underlying_provider_account_quota_pool_identity"), config))
    # 优先不同额度池；每个角色仍使用独立新进程与工作树。
    if len(available) > 1:
        second = next((i for i, item in enumerate(available[1:], 1) if item[0] != available[0][0]), 1)
        available[1], available[second] = available[second], available[1]
    configs = [item[1] for item in available]
    if len(configs) == 1:
        configs.append(configs[0])
    if len(configs) < 2:
        raise RuntimeError("没有可执行的 reviewer CLI；可通过 --reviewer-config 提供其他独立 CLI")
    return configs


def load_reviewer_configuration(path: Path) -> dict:
    from .harness_cli_adapters import load_effective_alternative_reviewer_config
    config, _ = load_effective_alternative_reviewer_config(path)
    return {**config,'configuration_file_path':str(path.resolve())}


def persistent_configuration(config: dict) -> dict:
    frozen=dict(config)
    secrets=frozen.pop('env_set',{})
    if secrets:
        frozen['credential_environment_variable_names']=sorted(secrets)
        frozen['credential_environment_sha256']=hashlib.sha256(json.dumps(secrets,sort_keys=True).encode()).hexdigest()
    return frozen


def configuration_digest(config: dict) -> str:
    return hashlib.sha256(json.dumps(persistent_configuration(config), sort_keys=True).encode()).hexdigest()


def _role_attempt_status(attempt: Path) -> str | None:
    reconcile_completed_role_attempt(attempt)
    outcome_file = attempt / "outcome.json"
    if not outcome_file.exists():
        return None
    reconciliation_file = attempt / "reconciled-native-completion.json"
    # 补证状态用于接续，原始失败记录保持不变。
    if reconciliation_file.exists():
        return read_json(reconciliation_file).get("status")
    return read_json(outcome_file).get("status")


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


def _start_role(run_directory: Path, record: dict, role_directory: Path, role: str, configurations: list[dict], findings: list[dict], *, force_new: bool = False) -> tuple[dict | None, subprocess.Popen | None]:
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
    if role == "fixer":
        progress = _role_result(role_directory, latest=True, include_incomplete=True)
        if progress:
            candidate_commit = _snapshot_fixer_progress(run_directory, record, progress)["after"]
    for config in configurations:
        identity = configuration_digest(config)
        matching = [attempt for attempt in attempts if (attempt / "request.json").exists()
                    and read_json(attempt / "request.json")["configuration_digest"] == identity
                    and (read_json(attempt / "request.json")["candidate_commit"] == candidate_commit
                         or ((attempt / "outcome.json").exists()
                             and read_json(attempt / "outcome.json").get("status") == "incomplete"))
                    and read_json(attempt / "request.json").get("findings", []) == findings
                    and read_json(attempt/'request.json').get('context_response_sha256') == record.get('context_response_sha256')
                    and not ((attempt/'outcome.json').exists() and read_json(attempt/'outcome.json').get('status')=='cancelled')]
        if any((attempt / "outcome.json").exists() and read_json(attempt / "outcome.json").get("retryable") is False for attempt in matching):
            continue
        if len(matching) < 2:
            chosen = config
            break
    if chosen is None:
        return None, None
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
        "context_response_sha256": record.get('context_response_sha256'),
        "context_response_file": str(run_directory/'context-response.md') if record.get('context_response_sha256') else None,
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
    if record.get('context_response_sha256'):
        inputs+=f"通用补证：{run_directory/'context-response.md'}；不改变冻结需求与范围，不包含另一腿的结论。\n"
    prompt = template.strip() + "\n" + inputs + f"\n结果可写入 {result_file}，或在最终回复中只输出结果 JSON。\n"
    write_bytes(attempt_directory / "prompt.md", prompt.encode())
    write_json(attempt_directory / "request.json", request)
    return None, launch_role(attempt_directory,environment_overrides=chosen.get('env_set',{}))


def _run_roles(run_directory: Path, record: dict, roles: list[tuple[str, str, list[dict]]], findings: list[dict], *, force_new: bool = False) -> list[dict | None]:
    active = {}
    try:
        for name, role, configs in roles:
            _, process = _start_role(run_directory, record, run_directory / name, role, configs, findings, force_new=force_new)
            if process:
                active[name] = process
        while active:
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
    except KeyboardInterrupt:
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
        record['status'] = 'interrupted'
        write_json(run_directory / 'run.json', record)
        raise
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
        before = record.get("validated_candidate_commit", record["candidate_commit"])
        workspace = attempt_directory / "workspace"
        fixed_tree = capture_worktree_tree(workspace, ["."], run_directory)
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
    repo = Path(record["repo"])
    attempt_directory = Path(result["attempt_directory"])
    intent_file = attempt_directory / "fix-application.json"
    patch_file = attempt_directory / "fixer.patch"
    intent = _snapshot_fixer_progress(run_directory, record, result)
    requires_changes = intent["requires_changes"]
    if not intent["paths"]:
        intent["applied"] = not requires_changes
        if requires_changes:
            intent["error"] = "fixer 声称 fixed 但未产生任何任务改动"
    elif _matches_tree(repo, intent["after"], intent["paths"], run_directory):
        intent["applied"] = True
    elif _matches_tree(repo, intent["before"], intent["paths"], run_directory):
        patch = patch_file.read_bytes()
        git(repo, "apply", "--check", "-", input_bytes=patch)
        git(repo, "apply", "-", input_bytes=patch)
        intent["applied"] = True
    write_json(intent_file, intent)
    write_bytes(run_directory / "fixer.patch", patch_file.read_bytes())
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
        return {"status": "candidate_changed", "returncode": None}
    # 只将任务路径叠加到当前 HEAD；范围外 WIP 不参与最终候选验证。
    head = git_text(repo, "rev-parse", "HEAD")
    tested_tree = capture_worktree_tree(repo, scope, run_directory, head=head)
    prior = run_directory / "validation.json"
    old = read_json(prior) if prior.exists() else {}
    history_root = run_directory / "final-validation"
    history_root.mkdir(exist_ok=True)
    attempt_directory = history_root / f"attempt-{len(list(history_root.glob('attempt-*'))) + 1:03d}"
    attempt_directory.mkdir()
    validation = {"status": "running", "candidate_commit": candidate, "commands": commands, "completed_commands": 0,
                  "tested_tree": tested_tree, "tested_head": head, "attempt_directory": str(attempt_directory)}
    previous_workspace = Path(old["workspace"]) if old.get("workspace") else None
    if (old.get("status") in {"running", "interrupted"} and old.get("candidate_commit") == candidate
            and old.get("tested_tree") == tested_tree and old.get("commands") == commands
            and previous_workspace and previous_workspace.exists()
            and not git(previous_workspace, "diff", "--name-only", old["tested_commit"])):
        workspace = previous_workspace
        tested_commit = old["tested_commit"]
        validation["completed_commands"] = old.get("completed_commands", 0)
        validation["resumed_from"] = old.get("attempt_directory")
    else:
        tested_commit = git_text(repo, "commit-tree", tested_tree, "-p", head,
            input_bytes=b"RVF final validation snapshot\n", environment={
                "GIT_AUTHOR_NAME": "RVF Validation", "GIT_AUTHOR_EMAIL": "validation@rvf.local",
                "GIT_COMMITTER_NAME": "RVF Validation", "GIT_COMMITTER_EMAIL": "validation@rvf.local"})
        workspace = attempt_directory / "workspace"
        git(repo, "worktree", "add", "--detach", str(workspace), tested_commit)
    validation.update({"workspace": str(workspace), "tested_commit": tested_commit})
    def save_validation():
        write_json(attempt_directory / "validation.json", validation)
        write_json(prior, validation)
    save_validation()
    for i in range(validation["completed_commands"], len(commands)):
        logs = attempt_directory / f"command-{i + 1:03d}"
        logs.mkdir()
        with (logs / "stdout.txt").open("wb") as stdout, (logs / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(commands[i], cwd=workspace, stdout=stdout, stderr=stderr, pass_fds=(lock_handle.fileno(),), start_new_session=True)
            try:
                returncode = process.wait()
            except KeyboardInterrupt:
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
        validation["returncode"] = returncode
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


def resume_review(run_directory: Path, *, reviewer_configs: list[dict] | None = None,
                  fix: bool = False, fixer_config: dict | None = None,
                  validation_commands: list[list[str]] | None = None,
                  context_response_file: Path | None = None) -> dict:
    run_directory = run_directory.expanduser().resolve()
    with exclusive_lock(run_directory / "workflow.lock", blocking=False) as lock_handle:
        record = read_json(run_directory / "run.json")
        repo = verify_repository_identity(record)
        if context_response_file is not None:
            response=context_response_file.read_bytes()
            write_bytes(run_directory/'context-response.md',response)
            record['context_response_sha256']=hashlib.sha256(response).hexdigest()
            write_json(run_directory/'run.json',record)
        if hashlib.sha256((run_directory / "review-input.md").read_bytes()).hexdigest() != record["review_input_sha256"]:
            raise ValueError("固定评审输入已改变；为新输入创建快照")
        if hashlib.sha256((run_directory / "requirements.md").read_bytes()).hexdigest() != record["requirements_sha256"] or hashlib.sha256((run_directory / "task-changes.patch").read_bytes()).hexdigest() != record["patch_sha256"]:
            raise ValueError("固定评审输入已改变；保留原 run 并为新输入创建快照")
        configurations_file = run_directory / "reviewer-configurations.json"
        if reviewer_configs is not None:
            if len(reviewer_configs) < 2:
                raise ValueError("需要两路独立 reviewer 配置（可使用同一 CLI 的两个新实例）")
            write_json(configurations_file, {"configurations": [persistent_configuration(config) for config in reviewer_configs]})
        elif configurations_file.exists():
            reviewer_configs = read_json(configurations_file)["configurations"]
            reviewer_configs = [load_reviewer_configuration(Path(config['configuration_file_path']))
                                if config.get('configuration_file_path') and Path(config['configuration_file_path']).is_file()
                                else config for config in reviewer_configs]
            write_json(configurations_file, {"configurations": [persistent_configuration(config) for config in reviewer_configs]})
        else:
            reviewer_configs = default_reviewer_configurations()
            write_json(configurations_file, {"configurations": [persistent_configuration(config) for config in reviewer_configs]})
        reviewer_count = record["reviewer_count"]
        roles = []
        for reviewer_index in range(reviewer_count):
            primary_index = reviewer_index % len(reviewer_configs)
            configurations = [reviewer_configs[primary_index], *reviewer_configs[:primary_index], *reviewer_configs[primary_index + 1:]]
            roles.append((f"reviewer-{reviewer_index + 1}", "reviewer", configurations))
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
            if findings and (fix or fixer_result):
                if not fixer_result or needs_new_fix:
                    configs = [fixer_config] if fixer_config else reviewer_configs
                    fixer_result = _run_roles(run_directory, record, [("fixer", "fixer", configs)], findings, force_new=needs_new_fix)[0]
                _merge_fixer_findings(run_directory, findings)
                record["finding_count"] = len(findings)
                if not fixer_result:
                    record["status"] = "fix_incomplete"
                    progress = _role_result(run_directory / "fixer", latest=True, include_incomplete=True)
                    if progress:
                        _snapshot_fixer_progress(run_directory, record, progress)
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
                    elif (validation.get("status") in {"running", "interrupted"}
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
