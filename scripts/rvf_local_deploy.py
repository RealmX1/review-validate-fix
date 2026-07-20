#!/usr/bin/env python3
"""从唯一、干净的 local main 运行 RVF 官方 CLI 本机部署。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
PARTIAL_REMEDIATION = (
    "修复失败原因后，从干净 local main 重新运行 runner；"
    "不得降级到手写 plugin cache 或 installed payload。"
)


class DeployError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.result = result


@dataclass(frozen=True)
class LocalMainDeploymentSource:
    worktree_path: Path
    commit: str


@dataclass(frozen=True)
class StructuredHookConfigurationSnapshot:
    path: Path
    existed: bool
    content: bytes | None


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DeployError(f"git {' '.join(args)} 失败: {detail}", exit_code=2)
    return completed.stdout.strip()


def _git_worktree_records(repository_hint: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current_record: dict[str, str] = {}
    for line in _git(repository_hint, "worktree", "list", "--porcelain").splitlines():
        if not line:
            if current_record:
                records.append(current_record)
                current_record = {}
            continue
        key, _, value = line.partition(" ")
        current_record[key] = value
    if current_record:
        records.append(current_record)
    return records


def _status_paths(repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise DeployError("无法读取 local main git status", exit_code=2)
    tokens = completed.stdout.decode("utf-8", errors="replace").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        status = token[:2]
        paths.append(token[3:])
        if "R" in status or "C" in status:
            if index < len(tokens) and tokens[index]:
                paths.append(tokens[index])
                index += 1
    return paths


def _git_operation_markers(local_main_worktree: Path) -> list[Path]:
    marker_names = (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-apply",
        "rebase-merge",
    )
    markers: list[Path] = []
    for marker_name in marker_names:
        marker_path = Path(
            _git(local_main_worktree, "rev-parse", "--git-path", marker_name)
        )
        if not marker_path.is_absolute():
            marker_path = local_main_worktree / marker_path
        if marker_path.exists():
            markers.append(marker_path.resolve())
    return markers


def validate_local_main_deployment_source(
    *,
    repository_hint: Path,
    runner_path: Path,
    expected_local_main_commit: str | None,
) -> LocalMainDeploymentSource:
    repository_hint = repository_hint.expanduser().resolve()
    local_main_records = [
        record
        for record in _git_worktree_records(repository_hint)
        if record.get("branch") == "refs/heads/main"
    ]
    if len(local_main_records) != 1:
        raise DeployError(
            "必须存在且只能存在一个 checkout refs/heads/main 的 local main worktree; "
            f"实际数量={len(local_main_records)}",
            exit_code=2,
        )
    local_main_worktree = Path(local_main_records[0]["worktree"]).expanduser().resolve()
    expected_runner_path = local_main_worktree / "scripts/rvf_local_deploy.py"
    if runner_path.expanduser().resolve() != expected_runner_path.resolve():
        raise DeployError(
            "deployment runner 必须来自 local main worktree: "
            f"expected={expected_runner_path}, actual={runner_path.expanduser().resolve()}",
            exit_code=2,
        )
    branch_ref = _git(local_main_worktree, "symbolic-ref", "--quiet", "HEAD")
    if branch_ref != "refs/heads/main":
        raise DeployError(
            f"local main worktree 未 checkout refs/heads/main: {branch_ref}",
            exit_code=2,
        )
    local_main_commit = _git(local_main_worktree, "rev-parse", "HEAD")
    local_main_ref_commit = _git(local_main_worktree, "rev-parse", "refs/heads/main")
    if local_main_commit != local_main_ref_commit:
        raise DeployError(
            "local main worktree HEAD 与 refs/heads/main 不一致",
            exit_code=2,
        )
    dirty_paths = _status_paths(local_main_worktree)
    if dirty_paths:
        raise DeployError(
            f"local main worktree 含未提交改动: {sorted(dirty_paths)}",
            exit_code=2,
        )
    operation_markers = _git_operation_markers(local_main_worktree)
    if operation_markers:
        raise DeployError(
            f"local main worktree 存在未完成 Git 操作: {operation_markers}",
            exit_code=2,
        )
    if expected_local_main_commit:
        expected_commit = _git(
            local_main_worktree,
            "rev-parse",
            "--verify",
            f"{expected_local_main_commit}^{{commit}}",
        )
        if expected_commit != local_main_commit:
            raise DeployError(
                "expected local main commit 与实际 HEAD 不一致: "
                f"expected={expected_commit}, actual={local_main_commit}",
                exit_code=2,
            )
    return LocalMainDeploymentSource(
        worktree_path=local_main_worktree,
        commit=local_main_commit,
    )


def _required_paths(local_main_worktree: Path) -> dict[str, Path]:
    paths = {
        "contract": local_main_worktree / "scripts/check_plugin_contracts.py",
        "installer": local_main_worktree / "scripts/install_to_codex.py",
        "verifier": (
            local_main_worktree
            / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
        ),
    }
    missing_paths = [str(path) for path in paths.values() if not path.is_file()]
    if missing_paths:
        raise DeployError(
            f"local main deploy 入口缺失: {missing_paths}",
            exit_code=2,
        )
    return paths


def _user_local_data_root(home: Path, environment: dict[str, str]) -> Path:
    configured = environment.get("RVF_USER_LOCAL_DATA_ROOT", "").strip()
    return Path(configured).expanduser() if configured else home / ".rvf/user-local-data"


def _write_result_receipt(path: Path, result: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
    return path


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _installed_release_summary(installation_receipt_path: Path) -> dict[str, Any] | None:
    receipt = _read_json_object(installation_receipt_path)
    if receipt is None:
        return None
    return {
        "local_main_commit": receipt.get("local_main_commit"),
        "generated_plugin_version": receipt.get("generated_plugin_version"),
        "status": receipt.get("status"),
    }


def _snapshot_structured_hook_configuration(
    hooks_path: Path,
) -> StructuredHookConfigurationSnapshot:
    hooks_path = hooks_path.expanduser().resolve()
    if not hooks_path.exists():
        return StructuredHookConfigurationSnapshot(
            path=hooks_path,
            existed=False,
            content=None,
        )
    try:
        content = hooks_path.read_bytes()
    except OSError as exc:
        raise DeployError(f"无法备份 Codex hooks 配置: {hooks_path}: {exc}") from exc
    return StructuredHookConfigurationSnapshot(
        path=hooks_path,
        existed=True,
        content=content,
    )


def _restore_structured_hook_configuration(
    snapshot: StructuredHookConfigurationSnapshot,
) -> None:
    try:
        if not snapshot.existed:
            if snapshot.path.exists():
                snapshot.path.unlink()
            return
        if snapshot.content is None:
            raise DeployError("Codex hooks backup 标记为存在但缺少内容")
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = snapshot.path.with_name(
            f".{snapshot.path.name}.{os.getpid()}.rollback.tmp"
        )
        temporary_path.write_bytes(snapshot.content)
        temporary_path.replace(snapshot.path)
    except OSError as exc:
        raise DeployError(f"无法恢复 Codex hooks 配置: {snapshot.path}: {exc}") from exc


@contextmanager
def _deployment_lock(home: Path) -> Iterator[Path]:
    lock_path = home / ".rvf/local-deploy.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeployError("已有 RVF local deploy 正在运行", exit_code=3) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_phase(
    label: str,
    argv: list[str],
    *,
    local_main_worktree: Path,
    environment: dict[str, str],
) -> str:
    completed = subprocess.run(
        argv,
        cwd=local_main_worktree,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DeployError(f"{label} 失败: {detail}")
    return completed.stdout.strip()


def run_local_deploy(
    *,
    repository_hint: Path,
    home: Path,
    expected_local_main_commit: str | None = None,
    runner_path: Path | None = None,
) -> dict[str, Any]:
    home = home.expanduser().resolve()
    source = validate_local_main_deployment_source(
        repository_hint=repository_hint,
        runner_path=runner_path or Path(__file__).resolve(),
        expected_local_main_commit=expected_local_main_commit,
    )
    paths = _required_paths(source.worktree_path)
    environment = {**os.environ, "HOME": str(home)}
    user_local_data_root = _user_local_data_root(home, environment)
    environment["RVF_USER_LOCAL_DATA_ROOT"] = str(user_local_data_root)
    installation_receipt_path = (
        user_local_data_root
        / "deployment-receipts/latest-official-cli-installation.json"
    )
    result_receipt_path = (
        user_local_data_root / "deployment-receipts/latest-local-deploy-result.json"
    )
    previous_installation_receipt_backup_path = (
        user_local_data_root
        / "deployment-receipts/previous-installation-before-latest-attempt.json"
    )
    hooks_path = home / ".codex/hooks.json"
    contract_command = [sys.executable, str(paths["contract"])]
    installer_command = [
        sys.executable,
        str(paths["installer"]),
        "--source-local-main-worktree",
        str(source.worktree_path),
        "--source-local-main-commit",
        source.commit,
        "--configure-stop-hook",
        "--configure-user-prompt-submit-hook",
        "--json",
    ]
    commands: list[list[str]] = []

    with _deployment_lock(home) as lock_path:
        installed_before = _installed_release_summary(installation_receipt_path)
        previous_installation_receipt = _read_json_object(
            installation_receipt_path
        )
        if previous_installation_receipt is not None:
            _write_result_receipt(
                previous_installation_receipt_backup_path,
                previous_installation_receipt,
            )
        hooks_snapshot = _snapshot_structured_hook_configuration(hooks_path)
        installation_mutation_phase_started = False

        def result_for(
            *,
            status: str,
            phase: str,
            error: str | None = None,
            **extra: Any,
        ) -> dict[str, Any]:
            result: dict[str, Any] = {
                "status": status,
                "phase": phase,
                "local_main_worktree": str(source.worktree_path),
                "local_main_commit": source.commit,
                "runner_path": str((runner_path or Path(__file__)).resolve()),
                "installed_before": installed_before,
                "installed_after": _installed_release_summary(
                    installation_receipt_path
                ),
                "stale_before": (
                    installed_before is None
                    or installed_before.get("local_main_commit") != source.commit
                ),
                "lock_path": str(lock_path),
                "receipt_path": str(result_receipt_path),
                "commands": commands,
            }
            if error is not None:
                result["error"] = error
                result["remediation"] = PARTIAL_REMEDIATION
            result.update(extra)
            return result

        commands.append(contract_command)
        _write_result_receipt(
            result_receipt_path,
            result_for(status="running", phase="plugin_contract_check"),
        )
        try:
            contract_output = _run_phase(
                "plugin contract check",
                contract_command,
                local_main_worktree=source.worktree_path,
                environment=environment,
            )
            commands.append(installer_command)
            _write_result_receipt(
                result_receipt_path,
                result_for(status="running", phase="official_cli_installer"),
            )
            installation_mutation_phase_started = True
            installer_output = _run_phase(
                "official CLI installer",
                installer_command,
                local_main_worktree=source.worktree_path,
                environment=environment,
            )
            try:
                installer_receipt = json.loads(installer_output)
            except json.JSONDecodeError as exc:
                raise DeployError("official CLI installer 未返回 JSON") from exc
            if not isinstance(installer_receipt, dict):
                raise DeployError("official CLI installer JSON 顶层不是 object")
            installer_receipt_path = installer_receipt.get("receipt_path")
            if isinstance(installer_receipt_path, str) and installer_receipt_path:
                installation_receipt_path = Path(installer_receipt_path).expanduser().resolve()
            verifier_command = [
                sys.executable,
                str(paths["verifier"]),
                "--installation-receipt",
                str(installation_receipt_path),
                "--hooks-path",
                str(hooks_path),
                "--home",
                str(home),
                "--json",
            ]
            commands.append(verifier_command)
            _write_result_receipt(
                result_receipt_path,
                result_for(status="running", phase="official_cli_verifier"),
            )
            verifier_output = _run_phase(
                "official CLI verifier",
                verifier_command,
                local_main_worktree=source.worktree_path,
                environment=environment,
            )
            try:
                verification = json.loads(verifier_output)
            except json.JSONDecodeError as exc:
                raise DeployError("official CLI verifier 未返回 JSON") from exc
            if not isinstance(verification, dict) or verification.get("status") != "ok":
                raise DeployError("official CLI verifier 未返回 status=ok")
            installed_after = _installed_release_summary(installation_receipt_path)
            if installed_after is None or installed_after.get("local_main_commit") != source.commit:
                raise DeployError(
                    "部署后 official installation receipt 未收敛到 local main commit"
                )
        except DeployError as exc:
            rollback_result: dict[str, Any] | None = None
            rollback_error: str | None = None
            if installation_mutation_phase_started:
                rollback_command = [sys.executable, str(paths["installer"])]
                if previous_installation_receipt is None:
                    rollback_command.append(
                        "--rollback-remove-current-installation"
                    )
                else:
                    rollback_command.extend(
                        [
                            "--rollback-to-installation-receipt",
                            str(previous_installation_receipt_backup_path),
                        ]
                    )
                rollback_command.append("--json")
                commands.append(rollback_command)
                try:
                    rollback_output = _run_phase(
                        "official CLI verification rollback",
                        rollback_command,
                        local_main_worktree=source.worktree_path,
                        environment=environment,
                    )
                    parsed_rollback_result = json.loads(rollback_output)
                    if not isinstance(parsed_rollback_result, dict):
                        raise DeployError("rollback JSON 顶层不是 object")
                    rollback_result = parsed_rollback_result
                except (DeployError, json.JSONDecodeError) as rollback_exc:
                    rollback_error = str(rollback_exc)
                try:
                    _restore_structured_hook_configuration(hooks_snapshot)
                except DeployError as hooks_rollback_exc:
                    hooks_error = str(hooks_rollback_exc)
                    rollback_error = (
                        f"{rollback_error}; {hooks_error}"
                        if rollback_error
                        else hooks_error
                    )
            failed = result_for(
                status="failed",
                phase=(
                    "plugin_contract_check"
                    if not commands or commands[-1] == contract_command
                    else "official_cli_install_or_verify"
                ),
                error=str(exc),
                rollback_status=(
                    "rollback_failed"
                    if rollback_error
                    else (
                        rollback_result.get("rollback_status")
                        if rollback_result is not None
                        else "not_required"
                    )
                ),
                rollback=rollback_result,
                rollback_error=rollback_error,
            )
            _write_result_receipt(result_receipt_path, failed)
            failure_message = str(exc)
            if rollback_error:
                failure_message = (
                    f"{failure_message}; verification rollback 失败: {rollback_error}"
                )
            raise DeployError(
                failure_message,
                exit_code=exc.exit_code,
                result=failed,
            ) from exc

        completed = result_for(
            status="ok",
            phase="completed",
            contract_output=contract_output,
            installer=installer_receipt,
            verification=verification,
        )
        _write_result_receipt(result_receipt_path, completed)
        return completed


def _print_text(result: dict[str, Any]) -> None:
    print("RVF local-main official CLI deploy OK")
    print("local main worktree:", result["local_main_worktree"])
    print("local main commit:", str(result["local_main_commit"])[:12])
    print("installed before:", result["installed_before"])
    print("installed after:", result["installed_after"])
    print("stale before:", result["stale_before"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-local-main-commit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_local_deploy(
            repository_hint=ROOT,
            home=Path.home(),
            expected_local_main_commit=args.expected_local_main_commit,
            runner_path=Path(__file__).resolve(),
        )
    except DeployError as exc:
        print(f"RVF local-main official CLI deploy FAILED: {exc}", file=sys.stderr)
        if args.json:
            failure = exc.result or {
                "status": "failed",
                "phase": "preflight",
                "error": str(exc),
                "runner_path": str(Path(__file__).resolve()),
            }
            print(json.dumps(failure, ensure_ascii=False, indent=2))
        return exc.exit_code
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
