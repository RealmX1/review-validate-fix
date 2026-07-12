#!/usr/bin/env python3
"""验证本机 RVF stable 部署，并汇总相对上一次部署的 commit delta。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


PLUGIN_RELATIVE_FILES = (
    ".codex-plugin/plugin.json",
    ".rvf-pyroot",
    "core/transcript/models.py",
    "core/subagents/models.py",
    "core/reviewer_harness_selection/__init__.py",
    "core/temporary_disable_window.py",
    "adapters/codex/subagent.py",
    "adapters/claude_code/subagent.py",
    "skills/review-validate-fix/scripts/stop_hook_router.py",
    "skills/review-validate-fix/scripts/trajectory_distill.py",
    "skills/review-validate-fix/scripts/rvf_agent_decision_cli.py",
    "skills/review-validate-fix/scripts/rvf_decision_token_store.py",
    "skills/review-validate-fix/scripts/stop_decision_candidate_summary.py",
    "skills/review-validate-fix/scripts/review_strictness_reference_calculator.py",
    "skills/review-validate-fix/scripts/agentic_initiation_injection_attempt_ledger.py",
    "skills/review-validate-fix/scripts/agentic_initiation_guardian_dispatch.py",
    "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py",
    "skills/review-validate-fix/state/deployments/latest-deployment.json",
)
SMOKE_SCRIPT_RELATIVE_PATHS = (
    "skills/review-validate-fix/scripts/trajectory_distill.py",
    "skills/review-validate-fix/scripts/rvf_agent_decision_cli.py",
    "skills/review-validate-fix/scripts/agentic_initiation_guardian_dispatch.py",
)
DEPLOY_HISTORY_RELATIVE_PATH = Path(
    "skills/review-validate-fix/state/deployments/deployments.jsonl"
)
DEPLOY_STAMP_RE = re.compile(r"\[deployed [0-9a-f]{12}(?:-dirty)?\]$")


class VerificationError(RuntimeError):
    """部署内容或配置不满足 stable 契约。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"无法读取 JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError(f"JSON 顶层不是 object: {path}")
    return payload


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationError(
            f"git {' '.join(args)} 失败: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _hook_commands_for_event(payload: Any, event: str) -> list[str]:
    commands: list[str] = []
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return commands
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "command":
                continue
            command = entry.get("command")
            if isinstance(command, str):
                commands.append(command)
    return commands


def _command_targets_script(command: str, expected: str) -> bool:
    try:
        return expected in shlex.split(command)
    except ValueError:
        return False


def _first_h1(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line
    except OSError as exc:
        raise VerificationError(f"无法读取 skill heading: {path}: {exc}") from exc
    raise VerificationError(f"SKILL.md 缺少 H1: {path}")


def _verify_payload_root(
    root: Path,
    source_head: str,
    *,
    label: str,
    require_deploy_log: bool = True,
) -> dict[str, Any]:
    required_files = (
        PLUGIN_RELATIVE_FILES
        if require_deploy_log
        else tuple(
            relative
            for relative in PLUGIN_RELATIVE_FILES
            if relative != "skills/review-validate-fix/state/deployments/latest-deployment.json"
        )
    )
    missing = [relative for relative in required_files if not (root / relative).is_file()]
    _require(not missing, f"payload 缺文件: root={root}; missing={missing}")

    skill_paths = sorted(root.glob("skills/*/SKILL.md"))
    _require(bool(skill_paths), f"payload 没有 skills/*/SKILL.md: {root}")
    unstamped = [str(path) for path in skill_paths if not DEPLOY_STAMP_RE.search(_first_h1(path))]
    _require(not unstamped, f"deployed heading stamp 缺失: {unstamped}")

    if require_deploy_log:
        latest_path = root / "skills/review-validate-fix/state/deployments/latest-deployment.json"
        latest = _read_json(latest_path)
        deployed_head = latest.get("source", {}).get("head")
        _require(
            deployed_head == source_head,
            f"部署 HEAD 不匹配: root={root}; deployed={deployed_head}; source={source_head}",
        )
        options = latest.get("options")
        _require(isinstance(options, dict), f"deploy log 缺 options: {latest_path}")
        _require(options.get("configure_stop_hook") is True, "deploy log 未确认 Stop hook")
        _require(
            options.get("configure_user_prompt_submit_hook") is True,
            "deploy log 未确认 UserPromptSubmit hook",
        )

    smoke_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for relative in SMOKE_SCRIPT_RELATIVE_PATHS:
        script = root / relative
        completed = subprocess.run(
            [sys.executable, str(script), "-h"],
            capture_output=True,
            text=True,
            env=smoke_env,
            check=False,
        )
        _require(
            completed.returncode == 0,
            f"import-smoke 失败: {script}: {completed.stderr.strip() or completed.stdout.strip()}",
        )

    return {
        "label": label,
        "root": str(root),
        "checked_file_count": len(required_files),
        "skill_stamp_count": len(skill_paths),
        "smoke_count": len(SMOKE_SCRIPT_RELATIVE_PATHS),
    }


def _require_payload_matches(reference: Path, candidate: Path, *, label: str) -> None:
    compared = (
        relative
        for relative in PLUGIN_RELATIVE_FILES
        if relative != "skills/review-validate-fix/state/deployments/latest-deployment.json"
    )
    mismatched = [
        relative
        for relative in compared
        if (reference / relative).read_bytes() != (candidate / relative).read_bytes()
    ]
    _require(not mismatched, f"{label} 内容不一致: {mismatched}")


def _deployment_delta(repo: Path, history_path: Path) -> dict[str, Any]:
    try:
        records = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"无法读取 deploy history: {history_path}: {exc}") from exc
    _require(bool(records), f"deploy history 为空: {history_path}")
    heads = [record.get("source", {}).get("head") for record in records]
    current = heads[-1]
    previous = heads[-2] if len(heads) >= 2 else None
    result: dict[str, Any] = {
        "current_head": current,
        "previous_head": previous,
        "forward_commits": [],
        "backward_commits": [],
        "comparison_error": None,
    }
    if not previous or not current:
        return result
    try:
        result["forward_commits"] = _git(
            repo, "log", "--oneline", "--no-decorate", f"{previous}..{current}"
        ).splitlines()
        result["backward_commits"] = _git(
            repo, "log", "--oneline", "--no-decorate", f"{current}..{previous}"
        ).splitlines()
    except VerificationError as exc:
        result["comparison_error"] = str(exc)
    return result


def verify_local_deploy(
    *, repo: Path, plugin_root: Path, cache_root: Path, hooks_path: Path
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    plugin_root = plugin_root.expanduser().resolve()
    cache_root = cache_root.expanduser().resolve()
    hooks_path = hooks_path.expanduser().resolve()
    _require((repo / ".git").exists(), f"不是 canonical source repo: {repo}")
    source_head = _git(repo, "rev-parse", "HEAD")

    payloads = [
        _verify_payload_root(plugin_root, source_head, label="Codex plugin"),
        _verify_payload_root(cache_root, source_head, label="Codex cache"),
    ]
    latest_path = plugin_root / "skills/review-validate-fix/state/deployments/latest-deployment.json"
    latest = _read_json(latest_path)
    options = latest.get("options")
    destinations = latest.get("destinations")
    _require(isinstance(options, dict), f"deploy log 缺 options: {latest_path}")
    _require(isinstance(destinations, dict), f"deploy log 缺 destinations: {latest_path}")
    if options.get("sync_claude_plugin") is True:
        for label, key in (
            ("Claude marketplace", "claude_plugin_marketplace"),
            ("Claude cache", "claude_plugin_cache"),
        ):
            value = destinations.get(key)
            _require(isinstance(value, str) and bool(value), f"deploy log 缺 {label} 路径")
            root = Path(value).expanduser().resolve()
            payloads.append(
                _verify_payload_root(
                    root,
                    source_head,
                    label=label,
                    require_deploy_log=False,
                )
            )
            _require_payload_matches(plugin_root, root, label=label)

    hooks = _read_json(hooks_path)
    stop_commands = _hook_commands_for_event(hooks, "Stop")
    ups_commands = _hook_commands_for_event(hooks, "UserPromptSubmit")
    expected_stop = str(
        plugin_root / "skills/review-validate-fix/scripts/stop_hook_router.py"
    )
    expected_ups = str(
        plugin_root / "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"
    )
    _require(
        any(_command_targets_script(command, expected_stop) for command in stop_commands),
        "stable Stop hook 未指向 plugin root",
    )
    _require(
        any(_command_targets_script(command, expected_ups) for command in ups_commands),
        "stable UserPromptSubmit hook 未指向 plugin root",
    )

    delta = _deployment_delta(repo, plugin_root / DEPLOY_HISTORY_RELATIVE_PATH)
    return {
        "status": "ok",
        "source_head": source_head,
        "payloads": payloads,
        "hooks_path": str(hooks_path),
        "stop_hook": expected_stop,
        "user_prompt_submit_hook": expected_ups,
        "deployment_delta": delta,
    }


def _print_text(result: dict[str, Any]) -> None:
    print("RVF local deploy verification OK")
    print("本次部署 HEAD:", str(result["source_head"])[:12])
    for payload in result["payloads"]:
        print(
            f"payload: {payload['root']} "
            f"files={payload['checked_file_count']} "
            f"stamps={payload['skill_stamp_count']} smokes={payload['smoke_count']}"
        )
    delta = result["deployment_delta"]
    previous = delta.get("previous_head")
    if not previous:
        print("（首次记录的部署，无前序部署可对比）")
        return
    print("上次部署 HEAD:", str(previous)[:12])
    if delta.get("comparison_error"):
        print("部署区间计算失败:", delta["comparison_error"])
        return
    forward = delta["forward_commits"]
    backward = delta["backward_commits"]
    if not forward and not backward:
        print("本次与上一次部署为同一 HEAD，无新增 commit。")
    if forward:
        print(f"新投入使用 {len(forward)} 个 commit（上次 → 本次）：")
        for commit in forward:
            print("  +", commit)
    if backward:
        print(f"另有 {len(backward)} 个 commit 不在本次 HEAD 上：")
        for commit in backward:
            print("  -", commit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--plugin-root", type=Path, default=Path.home() / "plugins/review-validate-fix"
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=(
            Path.home()
            / ".codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0"
        ),
    )
    parser.add_argument("--hooks-path", type=Path, default=Path.home() / ".codex/hooks.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify_local_deploy(
            repo=args.repo,
            plugin_root=args.plugin_root,
            cache_root=args.cache_root,
            hooks_path=args.hooks_path,
        )
    except VerificationError as exc:
        print(f"RVF local deploy verification FAILED: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
