#!/usr/bin/env python3
"""从 canonical checkout 串行执行 RVF 本机部署并返回结构化结果。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


CANONICAL_REPO = Path("/Users/bominzhang/Documents/GitHub/review-validate-fix")
PLUGIN_NAME = "review-validate-fix"
PLUGIN_VERSION = "0.1.0"
DEPLOY_SCOPE_PREFIXES = (
    "plugins/review-validate-fix/",
    "core/",
    "adapters/",
)
DEPLOY_SCOPE_FILES = {
    ".rvf-pyroot",
    "scripts/check_plugin_contracts.py",
    "scripts/check_skill_contracts.sh",
    "scripts/install_to_codex.py",
    "scripts/rvf_local_deploy.py",
}
RESULT_RELATIVE_PATH = Path(".rvf/local-deploy/latest-result.json")
PARTIAL_REMEDIATION = (
    "修复失败原因后重新运行 canonical runner；不要降级使用 installed payload。"
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


def _status_paths(repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise DeployError("无法读取 canonical git status", exit_code=2)
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


def _deploy_scope_dirty_paths(repo: Path) -> list[str]:
    return sorted(
        path
        for path in _status_paths(repo)
        if path in DEPLOY_SCOPE_FILES
        or any(path.startswith(prefix) for prefix in DEPLOY_SCOPE_PREFIXES)
    )


def _latest_deployment_head(root: Path) -> str | None:
    path = root / "skills/review-validate-fix/state/deployments/latest-deployment.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    head = payload.get("source", {}).get("head") if isinstance(payload, dict) else None
    return head if isinstance(head, str) and head else None


def _installed_heads(home: Path) -> dict[str, str | None]:
    return {
        "plugin": _latest_deployment_head(home / f"plugins/{PLUGIN_NAME}"),
        "cache": _latest_deployment_head(
            home
            / ".codex/plugins/cache/local-codex-plugins"
            / PLUGIN_NAME
            / PLUGIN_VERSION
        ),
    }


def _write_result_receipt(home: Path, result: dict[str, Any]) -> Path:
    path = home / RESULT_RELATIVE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        raise DeployError(f"无法写入部署结果 receipt: {path}: {exc}") from exc
    return path


def _is_stale(heads: dict[str, str | None], canonical_head: str) -> bool:
    return any(head != canonical_head for head in heads.values())


def _required_paths(repo: Path) -> dict[str, Path]:
    return {
        "contract": repo / "scripts/check_plugin_contracts.py",
        "installer": repo / "scripts/install_to_codex.py",
        "verifier": (
            repo
            / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
        ),
    }


def _preflight(repo: Path, *, allow_dirty: bool) -> tuple[str, dict[str, Path]]:
    if not (repo / ".git").exists():
        raise DeployError(f"canonical repo 不存在或不是 Git checkout: {repo}", exit_code=2)
    required = _required_paths(repo)
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise DeployError(f"canonical deploy 入口缺失: {missing}", exit_code=2)
    dirty_paths = _deploy_scope_dirty_paths(repo)
    if dirty_paths and not allow_dirty:
        raise DeployError(
            f"canonical deploy scope 含未提交改动: {dirty_paths}",
            exit_code=2,
        )
    return _git(repo, "rev-parse", "HEAD"), required


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
    repo: Path,
    env: dict[str, str],
) -> str:
    completed = subprocess.run(
        argv,
        cwd=repo,
        env=env,
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
    repo: Path,
    home: Path,
    allow_dirty: bool = False,
    skip_claude_plugin: bool = False,
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    home = home.expanduser().resolve()
    canonical_head, paths = _preflight(repo, allow_dirty=allow_dirty)
    env = {**os.environ, "HOME": str(home)}
    commands: list[list[str]] = []

    contract_command = [sys.executable, str(paths["contract"])]
    installer_command = [
        sys.executable,
        str(paths["installer"]),
        "--plugin-parent",
        str(home / "plugins"),
        "--configure-stop-hook",
        "--configure-user-prompt-submit-hook",
    ]
    if skip_claude_plugin:
        installer_command.append("--skip-claude-plugin")
    verifier_command = [
        sys.executable,
        str(paths["verifier"]),
        "--repo",
        str(repo),
        "--plugin-root",
        str(home / f"plugins/{PLUGIN_NAME}"),
        "--cache-root",
        str(
            home
            / ".codex/plugins/cache/local-codex-plugins"
            / PLUGIN_NAME
            / PLUGIN_VERSION
        ),
        "--hooks-path",
        str(home / ".codex/hooks.json"),
        "--json",
    ]

    with _deployment_lock(home) as lock_path:
        before = _installed_heads(home)
        receipt_path = home / RESULT_RELATIVE_PATH

        def result_for(
            *,
            status: str,
            phase: str,
            installed_after: dict[str, str | None] | None = None,
            error: str | None = None,
            remediation: str | None = None,
            **extra: Any,
        ) -> dict[str, Any]:
            result: dict[str, Any] = {
                "status": status,
                "phase": phase,
                "canonical_repo": str(repo),
                "canonical_head": canonical_head,
                "runner_path": str(Path(__file__).resolve()),
                "installed_before": before,
                "installed_after": installed_after if installed_after is not None else before,
                "stale_before": _is_stale(before, canonical_head),
                "lock_path": str(lock_path),
                "receipt_path": str(receipt_path),
                "commands": commands,
            }
            if error is not None:
                result["error"] = error
            if remediation is not None:
                result["remediation"] = remediation
            result.update(extra)
            return result

        commands.append(contract_command)
        _write_result_receipt(
            home,
            result_for(status="running", phase="plugin_contract_check"),
        )
        try:
            contract_output = _run_phase(
                "plugin contract check", contract_command, repo=repo, env=env
            )
        except DeployError as exc:
            failed = result_for(
                status="failed",
                phase="plugin_contract_check",
                installed_after=_installed_heads(home),
                error=str(exc),
            )
            _write_result_receipt(home, failed)
            raise DeployError(str(exc), exit_code=exc.exit_code, result=failed) from exc

        commands.append(installer_command)
        _write_result_receipt(home, result_for(status="running", phase="installer"))
        try:
            installer_output = _run_phase(
                "installer", installer_command, repo=repo, env=env
            )
        except DeployError as exc:
            partial = result_for(
                status="partial",
                phase="installer",
                installed_after=_installed_heads(home),
                error=str(exc),
                remediation=PARTIAL_REMEDIATION,
            )
            _write_result_receipt(home, partial)
            raise DeployError(str(exc), exit_code=exc.exit_code, result=partial) from exc

        commands.append(verifier_command)
        _write_result_receipt(
            home,
            result_for(
                status="running",
                phase="installed_runtime_verifier",
                installed_after=_installed_heads(home),
            ),
        )
        try:
            verifier_output = _run_phase(
                "installed runtime verifier", verifier_command, repo=repo, env=env
            )
        except DeployError as exc:
            partial = result_for(
                status="partial",
                phase="installed_runtime_verifier",
                installed_after=_installed_heads(home),
                error=str(exc),
                remediation=PARTIAL_REMEDIATION,
            )
            _write_result_receipt(home, partial)
            raise DeployError(str(exc), exit_code=exc.exit_code, result=partial) from exc
        try:
            verification = json.loads(verifier_output)
        except json.JSONDecodeError as exc:
            partial = result_for(
                status="partial",
                phase="installed_runtime_verifier",
                installed_after=_installed_heads(home),
                error="installed runtime verifier 未返回 JSON",
                remediation=PARTIAL_REMEDIATION,
            )
            _write_result_receipt(home, partial)
            raise DeployError(
                "installed runtime verifier 未返回 JSON",
                result=partial,
            ) from exc

        after = _installed_heads(home)
        if any(head != canonical_head for head in after.values()):
            message = f"部署后 installed HEAD 未收敛到 canonical: {after} != {canonical_head}"
            partial = result_for(
                status="partial",
                phase="convergence",
                installed_after=after,
                error=message,
                remediation=PARTIAL_REMEDIATION,
            )
            _write_result_receipt(home, partial)
            raise DeployError(message, result=partial)

        completed = result_for(
            status="ok",
            phase="completed",
            installed_after=after,
            contract_output=contract_output,
            installer_output=installer_output.splitlines(),
            verification=verification,
        )
        _write_result_receipt(home, completed)
        return completed


def _print_text(result: dict[str, Any]) -> None:
    print("RVF canonical local deploy OK")
    print("canonical HEAD:", str(result["canonical_head"])[:12])
    print("installed before:", result["installed_before"])
    print("installed after:", result["installed_after"])
    print("stale before:", result["stale_before"])
    for line in result["installer_output"]:
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=CANONICAL_REPO)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--skip-claude-plugin", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_local_deploy(
            repo=args.repo,
            home=args.home,
            allow_dirty=args.allow_dirty,
            skip_claude_plugin=args.skip_claude_plugin,
        )
    except DeployError as exc:
        print(f"RVF canonical local deploy FAILED: {exc}", file=sys.stderr)
        if args.json:
            failure = exc.result or {
                "status": "failed",
                "phase": "preflight",
                "error": str(exc),
                "canonical_repo": str(args.repo.expanduser().resolve()),
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
