"""RVF 只发布业务处置，所属执行和一次结果交回由 Kanban 通用入口托管。"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from core.task_snapshot_review.persistent_artifacts import read_json, write_json


def start_managed_review(run_directory: Path, task_id: str, *, project_path: Path | None,
                         kanban_cli: Path | None, entrypoint: Path) -> dict:
    if project_path is None:
        raise ValueError("Kanban 托管需要显式 --kanban-project-path 主仓库路径")
    if kanban_cli is not None:
        kanban_cli = kanban_cli.expanduser().resolve()
    binding_file = run_directory / "kanban-command-execution-binding.json"
    binding = {"task_id": task_id, "project_path": str(project_path.resolve()), "kanban_cli": str(kanban_cli) if kanban_cli else None}
    if binding_file.exists() and read_json(binding_file) != binding:
        raise ValueError("同一 run 的 Kanban 托管绑定不能改成其他任务或项目")
    write_json(binding_file, binding)
    executable = str(kanban_cli) if kanban_cli else shutil.which("kanban")
    if not executable:
        raise ValueError("找不到 Kanban CLI；启动材料已冻结，安装支持所属执行的版本后复用原 run")
    record = read_json(run_directory / "run.json")
    spec = {"command": [sys.executable, str(entrypoint), "resume-task-review", "--run-dir", str(run_directory)],
            "resumeCommand": [sys.executable, str(entrypoint), "resume-task-review", "--run-dir", str(run_directory)],
            "resumeCommandPreservesBusinessSingleFlight": True,
            "cancelCommand": [sys.executable, str(entrypoint), "cancel-task-review", "--run-dir", str(run_directory)],
            "workingDirectory": record["repo"], "resultFile": str(run_directory / "business-result.json"),
            "materialSha256": hashlib.sha256((record["execution_plan_sha256"] + record["review_input_sha256"]).encode()).hexdigest(),
            "label": "RVF 独立评审、修复与验证"}
    spec_file = run_directory / "kanban-command-execution-specification.json"
    write_json(spec_file, spec)
    response = subprocess.run([executable, "task", "command-execution", "start", "--task-id", task_id,
        "--project-path", str(project_path.resolve()), "--idempotency-key", record["run_id"], "--spec-file", str(spec_file)],
        capture_output=True, text=True, timeout=60)
    receipt = {"returncode": response.returncode, "stdout": response.stdout, "stderr": response.stderr}
    history = run_directory / "kanban-command-execution-registration-attempts"
    history.mkdir(exist_ok=True)
    write_json(history / f"attempt-{len(list(history.glob('attempt-*.json'))) + 1:03d}.json", receipt)
    write_json(run_directory / "kanban-command-execution-start-receipt.json", receipt)
    if response.returncode:
        raise RuntimeError(f"Kanban 所属执行未获受理；冻结材料保留，不能另建 run 盲重启：{response.stderr or response.stdout}")
    result = json.loads(response.stdout)
    if not result.get("ok"):
        raise RuntimeError(f"Kanban 所属执行未获受理：{result}")
    return {"run_directory": str(run_directory), "status": "managed_execution_started", "command_execution": result}
