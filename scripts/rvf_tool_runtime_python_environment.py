#!/usr/bin/env python3
"""为 RVF 部署和契约检查选择已有 Python，并固定工具子进程环境。

选择器自身只用标准库，可由系统 Python 3.9 自举；业务检查必须在已通过预检的
Python >=3.11 中执行。此模块不负责生产 hooks 的解释器配置。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Mapping


PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE = "RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE"
PYTHON_SELECTION_SOURCE_ENVIRONMENT_VARIABLE = "RVF_TOOL_RUNTIME_PYTHON_SELECTION_SOURCE"
CONTRACT_SHELL_REENTRY_ENVIRONMENT_VARIABLE = "RVF_TOOL_RUNTIME_CONTRACT_SHELL_REENTRY"
REQUIRED_PYTHON_MODULES = ("tomllib", "pytest")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ENVIRONMENT_PROBE = """
import importlib, json, sys
result = {
    "python_executable_absolute_path": sys.executable,
    "python_version": sys.version.split()[0],
    "python_version_info": list(sys.version_info[:3]),
    "python_environment_prefix": sys.prefix,
    "python_base_environment_prefix": getattr(sys, "base_prefix", sys.prefix),
    "required_module_preflight_results": {},
}
if sys.version_info >= (3, 11):
    for name in ("tomllib", "pytest"):
        try:
            importlib.import_module(name)
            result["required_module_preflight_results"][name] = "available"
        except Exception as exc:
            result["required_module_preflight_results"][name] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(result))
"""


@dataclass(frozen=True)
class ValidatedToolRuntimePythonEnvironment:
    python_executable_absolute_path: str
    python_version: str
    interpreter_selection_source: str
    required_module_preflight_results: dict[str, str]

    def receipt_fields(self) -> dict[str, object]:
        return asdict(self)


class ToolRuntimePythonPreflightError(RuntimeError):
    def __init__(self, error_code: str, message: str, attempts: list[dict]) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.attempts = attempts

    def failure_result(self) -> dict[str, object]:
        return {
            "status": "failed",
            "phase": "python_runtime_preflight",
            "error_code": self.error_code,
            "error": str(self),
            "python_environment_preflight_attempts": self.attempts,
        }


def _absolute_python_executable(path: str | Path) -> Path:
    # 不 realpath：把 venv/bin/python 的符号链接展开会丢掉 pyvenv.cfg 所属环境。
    return Path(os.path.abspath(Path(path).expanduser()))


def _python_executable_candidates(
    repository_root: Path, environment: Mapping[str, str]
) -> Iterator[tuple[Path, str]]:
    yield repository_root / ".venv/bin/python", "rvf_repository_virtual_environment"
    directories = [
        Path(environment.get("HOME", str(Path.home()))) / ".local/bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        *[Path(part) for part in environment.get("PATH", os.defpath).split(os.pathsep) if part],
    ]
    # 已验证的 3.12 优先，但不是工具的最低版本要求。
    for directory in directories:
        yield directory / "python3.12", "installed_python_3_12"
    yield Path(sys.executable), "current_python_interpreter"
    for directory in directories:
        yield directory / "python3", "installed_python_discovery"
        if directory.is_dir():
            for candidate in sorted(directory.glob("python3.[0-9]*")):
                yield candidate, "installed_python_discovery"


def select_tool_runtime_python_environment(
    repository_root: Path = REPOSITORY_ROOT,
    environment: Mapping[str, str] | None = None,
) -> ValidatedToolRuntimePythonEnvironment:
    environment = os.environ if environment is None else environment
    repository_root = repository_root.resolve()
    explicit_python = environment.get(PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE)
    if explicit_python is not None:
        requested = Path(explicit_python).expanduser()
        if not explicit_python or not requested.is_absolute():
            raise ToolRuntimePythonPreflightError(
                "python_executable_unusable",
                f"{PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE} 必须是已有解释器的绝对路径。",
                [{"requested_python_executable": explicit_python}],
            )
        candidates = [(requested, environment.get(PYTHON_SELECTION_SOURCE_ENVIRONMENT_VARIABLE) or "explicit_environment")]
    else:
        candidates = _python_executable_candidates(repository_root, environment)

    attempts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for candidate, source in candidates:
        candidate = _absolute_python_executable(candidate)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            if explicit_python is None:
                continue
            observation = {"error_code": "python_executable_unusable"}
        else:
            # 同一基础解释器的别名只探测一次；不同 venv 即使链接到同一二进制也分别探测。
            virtual_environment_root = candidate.parent.parent
            identity = (str(candidate.resolve()), str(virtual_environment_root) if (virtual_environment_root / "pyvenv.cfg").exists() else "")
            if identity in seen:
                continue
            seen.add(identity)
            try:
                completed = subprocess.run(
                    [str(candidate), "-B", "-c", PYTHON_ENVIRONMENT_PROBE],
                    env=dict(environment), capture_output=True, text=True,
                    check=False, timeout=15,
                )
                if completed.returncode:
                    raise ValueError(completed.stderr.strip() or f"退出码 {completed.returncode}")
                observation = json.loads(completed.stdout)
                version = tuple(observation["python_version_info"])
                prefix = Path(observation["python_environment_prefix"])
                is_virtual_environment = str(prefix) != observation["python_base_environment_prefix"]
                if explicit_python is None and is_virtual_environment and prefix.resolve() != (repository_root / ".venv").resolve():
                    # 独立工具不能借用调用仓库或当前激活的外部 venv。
                    observation["error_code"] = "external_virtual_environment_excluded"
                elif version < (3, 11):
                    observation["error_code"] = "python_version_too_old"
                elif any(observation["required_module_preflight_results"].get(name) != "available" for name in REQUIRED_PYTHON_MODULES):
                    observation["error_code"] = "python_dependency_missing"
                else:
                    return ValidatedToolRuntimePythonEnvironment(
                        python_executable_absolute_path=str(_absolute_python_executable(observation["python_executable_absolute_path"])),
                        python_version=observation["python_version"],
                        interpreter_selection_source=source,
                        required_module_preflight_results=observation["required_module_preflight_results"],
                    )
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                observation = {"error_code": "python_executable_unusable", "detail": str(exc)}
        attempts.append({"requested_python_executable": str(candidate), **observation})
        if explicit_python is not None:
            break

    error_codes = {item["error_code"] for item in attempts}
    error_code = next((code for code in ("python_dependency_missing", "python_version_too_old", "python_executable_unusable") if code in error_codes), "python_runtime_unavailable")
    messages = {
        "python_dependency_missing": "Python 环境缺少必需依赖：需要同一环境提供 tomllib 和 pytest。",
        "python_version_too_old": "Python 版本不足：RVF 部署与契约检查需要 Python >=3.11。",
        "python_executable_unusable": "指定或发现的 Python 解释器无法执行或无法完成预检。",
        "python_runtime_unavailable": "未发现合格的已有 Python 环境；需要 Python >=3.11、tomllib 和 pytest。",
    }
    raise ToolRuntimePythonPreflightError(error_code, messages[error_code], attempts)


def ensure_current_process_uses_selected_python() -> ValidatedToolRuntimePythonEnvironment:
    selected = select_tool_runtime_python_environment()
    if _absolute_python_executable(sys.executable) != Path(selected.python_executable_absolute_path):
        environment = dict(os.environ)
        environment[PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE] = selected.python_executable_absolute_path
        environment[PYTHON_SELECTION_SOURCE_ENVIRONMENT_VARIABLE] = selected.interpreter_selection_source
        os.execve(selected.python_executable_absolute_path, [selected.python_executable_absolute_path, *sys.argv], environment)
    return selected


@contextmanager
def selected_python_subprocess_environment(
    selected: ValidatedToolRuntimePythonEnvironment,
    environment: Mapping[str, str],
) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="rvf-tool-runtime-python-command-forwarders-") as directory:
        forwarder = Path(directory) / "python3"
        forwarder.write_text(
            "#!/bin/sh\nexec " + shlex.quote(selected.python_executable_absolute_path) + ' "$@"\n',
            encoding="utf-8",
        )
        forwarder.chmod(0o700)
        child_environment = dict(environment)
        child_environment[PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE] = selected.python_executable_absolute_path
        child_environment[PYTHON_SELECTION_SOURCE_ENVIRONMENT_VARIABLE] = selected.interpreter_selection_source
        child_environment["PATH"] = directory + os.pathsep + environment.get("PATH", os.defpath)
        yield child_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-contract-shell", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        selected = select_tool_runtime_python_environment()
    except ToolRuntimePythonPreflightError as exc:
        print(json.dumps(exc.failure_result(), ensure_ascii=False), file=sys.stderr)
        return 2
    if args.run_contract_shell is None:
        print(json.dumps(selected.receipt_fields(), ensure_ascii=False))
        return 0
    if not args.run_contract_shell:
        parser.error("--run-contract-shell 需要 shell 脚本路径")
    contract_shell = Path(args.run_contract_shell[0]).resolve()
    with selected_python_subprocess_environment(selected, os.environ) as environment:
        environment[CONTRACT_SHELL_REENTRY_ENVIRONMENT_VARIABLE] = str(contract_shell)
        print("RVF 工具 Python：" + json.dumps(selected.receipt_fields(), ensure_ascii=False), file=sys.stderr)
        return subprocess.run(["bash", str(contract_shell), *args.run_contract_shell[1:]], env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
