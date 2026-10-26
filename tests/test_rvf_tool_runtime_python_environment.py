"""解释器预检、shell 子进程固定和部署失败边界的自动化回归。"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import rvf_tool_runtime_python_environment as runtime


def _simulated_python_environment(
    directory: Path,
    *,
    version: str = "3.12.1",
    missing_pytest: bool = False,
    virtual_environment_prefix: Path | None = None,
) -> Path:
    """用可执行探针夹具模拟机器上不同的已有环境，不运行伪造的业务检查。"""
    executable = directory / "bin/python3"
    executable.parent.mkdir(parents=True)
    observation = {
        "python_executable_absolute_path": str(executable),
        "python_version": version,
        "python_version_info": [int(part) for part in version.split(".")],
        "python_environment_prefix": str(virtual_environment_prefix or directory),
        "python_base_environment_prefix": str(directory),
        "required_module_preflight_results": {
            "tomllib": "available",
            "pytest": "ModuleNotFoundError: pytest" if missing_pytest else "available",
        },
    }
    executable.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(json.dumps(observation)) + "\n")
    executable.chmod(0o700)
    return executable


def _set_discovered_candidates(monkeypatch, candidates: list[Path]) -> None:
    monkeypatch.setattr(runtime, "_python_executable_candidates", lambda *_: ((path, "fixture_discovery") for path in candidates))


def test_system_39_is_rejected_before_available_312_is_selected(tmp_path, monkeypatch):
    old = _simulated_python_environment(tmp_path / "system", version="3.9.6")
    qualified = _simulated_python_environment(tmp_path / "qualified")
    _set_discovered_candidates(monkeypatch, [old, qualified])
    selected = runtime.select_tool_runtime_python_environment(tmp_path, {})
    assert selected.python_executable_absolute_path == str(qualified)
    assert selected.required_module_preflight_results == {"tomllib": "available", "pytest": "available"}


@pytest.mark.parametrize("failure_kind", ["old_version", "missing_pytest", "missing_executable", "relative_executable", "empty_executable"])
def test_explicit_unqualified_environment_fails_without_fallback(tmp_path, monkeypatch, failure_kind):
    expected_code = "python_executable_unusable"
    if failure_kind == "old_version":
        candidate = str(_simulated_python_environment(tmp_path / "old", version="3.9.6"))
        expected_code = "python_version_too_old"
    elif failure_kind == "missing_pytest":
        candidate = str(_simulated_python_environment(tmp_path / "new", missing_pytest=True))
        expected_code = "python_dependency_missing"
    else:
        candidate = {"missing_executable": str(tmp_path / "absent"), "relative_executable": "python3", "empty_executable": ""}[failure_kind]
    monkeypatch.setattr(runtime, "_python_executable_candidates", lambda *_: pytest.fail("显式配置不得回退到自动发现"))
    with pytest.raises(runtime.ToolRuntimePythonPreflightError) as caught:
        runtime.select_tool_runtime_python_environment(tmp_path, {runtime.PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE: candidate})
    assert caught.value.failure_result()["error_code"] == expected_code


def test_missing_pytest_does_not_allow_fallback_to_python_39(tmp_path, monkeypatch):
    new_without_pytest = _simulated_python_environment(tmp_path / "new", missing_pytest=True)
    old_with_pytest = _simulated_python_environment(tmp_path / "old", version="3.9.6")
    _set_discovered_candidates(monkeypatch, [new_without_pytest, old_with_pytest])
    with pytest.raises(runtime.ToolRuntimePythonPreflightError) as caught:
        runtime.select_tool_runtime_python_environment(tmp_path, {})
    result = caught.value.failure_result()
    assert result["error_code"] == "python_dependency_missing"
    assert [item["error_code"] for item in result["python_environment_preflight_attempts"]] == ["python_dependency_missing", "python_version_too_old"]


def test_calling_repository_virtual_environment_is_excluded(tmp_path, monkeypatch):
    caller = _simulated_python_environment(tmp_path / "caller", virtual_environment_prefix=tmp_path / "caller/.venv")
    independent = _simulated_python_environment(tmp_path / "independent")
    _set_discovered_candidates(monkeypatch, [caller, independent])
    assert runtime.select_tool_runtime_python_environment(tmp_path / "rvf", {}).python_executable_absolute_path == str(independent)
    explicit = runtime.select_tool_runtime_python_environment(tmp_path / "rvf", {runtime.PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE: str(caller)})
    assert explicit.python_executable_absolute_path == str(caller)


def test_no_installed_candidate_reports_runtime_unavailable(tmp_path, monkeypatch):
    _set_discovered_candidates(monkeypatch, [])
    with pytest.raises(runtime.ToolRuntimePythonPreflightError, match="未发现合格") as caught:
        runtime.select_tool_runtime_python_environment(tmp_path, {})
    assert caught.value.error_code == "python_runtime_unavailable"


def test_rvf_virtual_environment_keeps_its_symlink_and_site_packages(tmp_path):
    repository = tmp_path / "rvf with spaces"
    virtual_environment = repository / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(virtual_environment)], check=True)
    virtual_python = virtual_environment / "bin/python"
    site_packages = Path(subprocess.check_output(
        [str(virtual_python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip())
    # 显式提供当前测试环境的依赖路径，兼容全局、用户 site-packages 和父 venv。
    (site_packages / "rvf-test-dependency-paths.pth").write_text(
        "\n".join(str(Path(path).resolve()) for path in sys.path if path and Path(path).is_dir()) + "\n",
        encoding="utf-8",
    )
    environment = {"HOME": str(tmp_path), "PATH": os.defpath}
    selected = runtime.select_tool_runtime_python_environment(repository, environment)
    assert selected.interpreter_selection_source == "rvf_repository_virtual_environment"
    assert selected.python_executable_absolute_path == str(virtual_python)
    assert selected.required_module_preflight_results["pytest"] == "available"
    observation = json.loads(subprocess.check_output(
        [str(virtual_python), "-c", runtime.PYTHON_ENVIRONMENT_PROBE], env=environment, text=True,
    ))
    assert Path(observation["python_environment_prefix"]).resolve() == virtual_environment.resolve()
    assert observation["python_environment_prefix"] != observation["python_base_environment_prefix"]


def test_nested_shell_python_is_pinned_and_forwarder_is_cleaned(tmp_path):
    selected = runtime.select_tool_runtime_python_environment(environment={runtime.PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE: sys.executable})
    wrong_python = tmp_path / "wrong/bin/python3"
    wrong_python.parent.mkdir(parents=True)
    wrong_python.write_text("#!/bin/sh\nexit 99\n")
    wrong_python.chmod(0o700)
    original_path = str(wrong_python.parent) + os.pathsep + os.defpath
    environment = {**os.environ, "PATH": original_path}
    with runtime.selected_python_subprocess_environment(selected, environment) as child_environment:
        forwarder_directory = Path(child_environment["PATH"].split(os.pathsep)[0])
        output = subprocess.check_output(["bash", "-c", "python3 -c 'import sys; print(sys.executable)'"], env=child_environment, text=True)
        assert output.strip() == selected.python_executable_absolute_path
        assert child_environment["PATH"].split(os.pathsep)[1:] == original_path.split(os.pathsep)
    assert not forwarder_directory.exists()
    assert environment["PATH"] == original_path


@pytest.mark.parametrize("shell_arguments", [[], ["--verbose"]])
def test_public_shell_bootstrap_pins_its_nested_shell_without_running_full_suite(tmp_path, shell_arguments):
    repository = tmp_path / "rvf shell bootstrap with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/rvf_tool_runtime_python_environment.py", scripts)
    original_shell = (ROOT / "scripts/check_skill_contracts.sh").read_text()
    shell_prefix = original_shell.split('cd "$repo_root"', 1)[0]
    (scripts / "check_skill_contracts.sh").write_text(shell_prefix + 'printf "%s\\n" "$verbose"\n' + "bash -c \"python3 -c 'import sys; print(sys.executable)'\"\n")
    bad_python = tmp_path / "wrong-python/python3"
    bad_python.parent.mkdir()
    bad_python.write_text("#!/bin/sh\nexit 99\n")
    bad_python.chmod(0o700)
    environment = {**os.environ, runtime.PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE: sys.executable, "PATH": str(bad_python.parent) + os.pathsep + os.defpath}
    environment.pop(runtime.CONTRACT_SHELL_REENTRY_ENVIRONMENT_VARIABLE, None)
    completed = subprocess.run(["bash", str(scripts / "check_skill_contracts.sh"), *shell_arguments], env=environment, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["1" if shell_arguments else "0", sys.executable]






def test_deploy_preflight_failure_precedes_deployment_lock_and_reports_json(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("deploy_runtime_preflight_test", ROOT / "scripts/rvf_local_deploy.py")
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    monkeypatch.setenv(runtime.PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE, str(tmp_path / "missing-python"))
    monkeypatch.setattr(runner, "_deployment_lock", lambda *_: pytest.fail("预检失败不得获取部署锁"))
    assert runner.main(["--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "python_runtime_preflight"
    assert result["error_code"] == "python_executable_unusable"


def test_configuration_errors_are_distinct_from_interpreter_errors(tmp_path, monkeypatch):
    import codex_hook_trust_fingerprint as trust
    missing = trust.verify_codex_hook_trust(codex_home=tmp_path, hook_sources=[])
    assert missing["error_code"] == "configuration_missing"
    config = tmp_path / "config.toml"
    config.write_text("[invalid TOML")
    invalid = trust.verify_codex_hook_trust(codex_home=tmp_path, hook_sources=[])
    assert invalid["error_code"] == "configuration_parse_failed"
    monkeypatch.setattr(trust.sys, "version_info", (3, 9, 6))
    old_interpreter = trust.verify_codex_hook_trust(codex_home=tmp_path, hook_sources=[])
    assert old_interpreter["error_code"] == "python_version_too_old"


def test_configuration_read_error_is_preserved(tmp_path, monkeypatch):
    import codex_hook_trust_fingerprint as trust
    def unreadable_config(*_args, **_kwargs):
        raise PermissionError("夹具模拟读取权限不足")
    monkeypatch.setattr(Path, "read_text", unreadable_config)
    result = trust.verify_codex_hook_trust(codex_home=tmp_path, hook_sources=[])
    assert result["error_code"] == "configuration_read_failed"


def test_bootstrap_module_runs_with_system_39_when_available():
    system_python = Path("/usr/bin/python3")
    if not system_python.exists():
        pytest.skip("此平台没有 /usr/bin/python3；其他选择器测试覆盖相同协议")
    completed = subprocess.run([str(system_python), "-B", str(ROOT / "scripts/rvf_tool_runtime_python_environment.py")], env={**os.environ, runtime.PYTHON_EXECUTABLE_ENVIRONMENT_VARIABLE: sys.executable}, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["python_executable_absolute_path"] == sys.executable
