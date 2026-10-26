"""用隔离原生子进程验证分析重试，不调用模型或生产控制面。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from _rvf_test_support.loader import load_script_module


@pytest.fixture(autouse=True)
def isolated_analysis_environment(tmp_path, monkeypatch):
    """只读取自产登录文件；成功计数也不得触发真实 guardian 调度。"""
    guardian = load_script_module("agentic_initiation_guardian_dispatch")
    isolated_paths = {
        "HOME": tmp_path / "isolated-user-home",
        "CODEX_HOME": tmp_path / "isolated-codex-home",
        "RVF_USER_LOCAL_DATA_ROOT": tmp_path / "isolated-user-local-root",
        "RVF_AGENTIC_INITIATION_GUARDIAN_ROOT": tmp_path / "isolated-retired-guardian-root",
    }
    for name, path in isolated_paths.items():
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    guardian_root = isolated_paths["RVF_AGENTIC_INITIATION_GUARDIAN_ROOT"]
    (guardian_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME).write_text(json.dumps({
        "analyze_completion_count": 0, "dispatched_milestones": [1, 2, 4, 8, 16],
        "guardian_retired": True,
    }))
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_"))}
    environment.update({name: str(path) for name, path in isolated_paths.items()})
    return environment


@pytest.mark.parametrize("eventually_succeeds", [True, False])
def test_background_analysis_retries_frozen_prompt_and_never_reexecutes_verified_completion(
    tmp_path, eventually_succeeds, isolated_analysis_environment,
):
    wrapper = load_script_module("rvf_analyze_formal_archive_verified_execution")
    run_directory = tmp_path / "review-run"
    analysis = run_directory / "artifacts/analysis"
    analysis.mkdir(parents=True)
    (analysis / "summary.md").write_text("<!-- TODO(rvf-analyze): 待补全 -->")
    (analysis / "causality.json").write_text(json.dumps({
        "schema_version": 2, "run_id": run_directory.name, "issues": [], "patches": [],
    }))
    (analysis / ".analyze-thread.status.json").write_text(json.dumps({
        "run_dir": str(run_directory), "started_at": "2026-10-25T12:00:00Z",
        "automatic_analysis_attempt_limit": 3,
    }))
    (analysis / ".analyze-thread.prompt.md").write_text("冻结的分析指令")
    agent = tmp_path / "analysis-agent-fixture.py"
    agent.write_text(
        "from pathlib import Path\nimport sys\n"
        f"root=Path({str(analysis)!r})\n"
        "assert sys.stdin.read() == '冻结的分析指令'\n"
        "counter=root/'attempt-count.txt'\n"
        "count=int(counter.read_text())+1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        f"if count < 3 or {not eventually_succeeds!r}: raise SystemExit(1)\n"
        "(root/'summary.md').write_text('已核验的完整复盘。')\n"
    )
    environment = isolated_analysis_environment
    command = [sys.executable, wrapper.__file__, "--run-dir", str(run_directory), "--", sys.executable, str(agent)]
    result = subprocess.run(command, input="冻结的分析指令", env=environment, capture_output=True, text=True)
    assert (result.returncode == 0) == eventually_succeeds, result.stdout + result.stderr
    assert (analysis / "attempt-count.txt").read_text() == "3"
    replay = subprocess.run(command, input="冻结的分析指令", env=environment, capture_output=True, text=True)
    assert (replay.returncode == 0) == eventually_succeeds, replay.stdout + replay.stderr
    assert (analysis / "attempt-count.txt").read_text() == "3"
    budget = json.loads((run_directory / "artifacts/analysis-execution-attempt-budget.json").read_text())
    assert budget["attempts_started"] == 3
    assert len(budget["attempt_history"]) == 3
    if not eventually_succeeds:
        agent.write_text(agent.read_text().replace("or True", "or False") + "\n# 修复执行版本后形成新的证据代次。\n")
        recovery = subprocess.run(command, input="冻结的分析指令", env=environment, capture_output=True, text=True)
        assert recovery.returncode == 0, recovery.stdout + recovery.stderr
        assert (analysis / "attempt-count.txt").read_text() == "4"


def seed_failing_analysis_execution(tmp_path, environment, *, direct_executable=False):
    wrapper = load_script_module("rvf_analyze_formal_archive_verified_execution")
    run_directory = tmp_path / "isolated-failing-review-run"
    analysis_directory = run_directory / "artifacts/analysis"
    analysis_directory.mkdir(parents=True)
    (analysis_directory / "summary.md").write_text("TODO(rvf-analyze): 自产失败占位符")
    (analysis_directory / "causality.json").write_text(json.dumps({
        "schema_version": 2, "run_id": run_directory.name, "issues": [], "patches": [],
    }))
    (analysis_directory / ".analyze-thread.status.json").write_text(json.dumps({
        "run_dir": str(run_directory), "automatic_analysis_attempt_limit": 3,
    }))
    (analysis_directory / ".analyze-thread.prompt.md").write_text("自产冻结分析指令")
    counter_path = analysis_directory / "synthetic-analysis-process-count.txt"
    agent_path = tmp_path / "synthetic-failing-analysis-agent.py"
    agent_path.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
        "assert sys.stdin.read() == '自产冻结分析指令'\n"
        f"counter=Path({str(counter_path)!r})\n"
        "counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))\n"
        "raise SystemExit(17)\n"
    )
    if direct_executable:
        agent_path.chmod(0o755)
    auth_path = Path(environment["CODEX_HOME"]) / "auth.json"
    auth_path.write_text(json.dumps({"token": "synthetic-rvf006-credential-v1"}))
    agent_argv = [str(agent_path)] if direct_executable else [sys.executable, str(agent_path)]
    return {
        "wrapper": wrapper, "run_directory": run_directory, "agent_path": agent_path,
        "auth_path": auth_path, "counter_path": counter_path, "agent_argv": agent_argv,
        "environment": environment,
    }


def execute_failing_analysis_fixture(fixture):
    result = subprocess.run(
        [sys.executable, fixture["wrapper"].__file__, "--run-dir", str(fixture["run_directory"]),
         "--", *fixture["agent_argv"]],
        input="自产冻结分析指令", env=fixture["environment"], capture_output=True, text=True,
    )
    budget = json.loads((fixture["run_directory"] / "artifacts"
                         / fixture["wrapper"].ANALYSIS_ATTEMPT_BUDGET_FILENAME).read_text())
    assert "synthetic-rvf006-credential" not in json.dumps(budget)
    process_count = int(fixture["counter_path"].read_text()) if fixture["counter_path"].exists() else 0
    return result, budget, process_count


@pytest.mark.parametrize("evidence_file", ["auth_path", "agent_path"])
@pytest.mark.parametrize("equivalent_mutation", [
    "touch", "identical-rewrite", "atomic-identical-replacement", "irrelevant-permission-bit",
])
def test_unchanged_analysis_evidence_never_renews_exhausted_budget(
    tmp_path, isolated_analysis_environment, evidence_file, equivalent_mutation,
):
    fixture = seed_failing_analysis_execution(tmp_path, isolated_analysis_environment)
    initial_result, initial_budget, initial_count = execute_failing_analysis_fixture(fixture)
    assert initial_result.returncode == 17 and initial_count == 3
    evidence_path = fixture[evidence_file]
    original_stat = evidence_path.stat()
    if equivalent_mutation == "identical-rewrite":
        evidence_path.write_bytes(evidence_path.read_bytes())
    elif equivalent_mutation == "atomic-identical-replacement":
        replacement = tmp_path / "same-content-evidence-replacement"
        replacement.write_bytes(evidence_path.read_bytes())
        replacement.chmod(original_stat.st_mode & 0o777)
        replacement.replace(evidence_path)
    elif equivalent_mutation == "irrelevant-permission-bit":
        # Python 读取的脚本和登录文件不依赖自身的执行位。
        evidence_path.chmod((original_stat.st_mode & 0o777) | 0o100)
    if equivalent_mutation != "irrelevant-permission-bit":
        os.utime(evidence_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 10_000_000))
    replay, replay_budget, replay_count = execute_failing_analysis_fixture(fixture)
    assert replay.returncode == 1, replay.stdout + replay.stderr
    assert replay_count == 3
    assert replay_budget == initial_budget


@pytest.mark.parametrize("evidence_file", ["auth_path", "agent_path"])
@pytest.mark.parametrize("preserve_original_mtime", [False, True])
def test_real_analysis_evidence_changes_recover_only_three_attempts_and_preserve_failures(
    tmp_path, isolated_analysis_environment, evidence_file, preserve_original_mtime,
):
    fixture = seed_failing_analysis_execution(tmp_path, isolated_analysis_environment)
    _, initial_budget, initial_count = execute_failing_analysis_fixture(fixture)
    assert initial_count == 3
    evidence_path = fixture[evidence_file]
    original_stat = evidence_path.stat()
    old_content = evidence_path.read_bytes()
    evidence_path.write_bytes(old_content.replace(b"credential-v1", b"credential-v2")
                              if evidence_file == "auth_path"
                              else old_content.replace(b"SystemExit(17)", b"SystemExit(19)"))
    assert evidence_path.stat().st_size == original_stat.st_size
    os.utime(evidence_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns
             if preserve_original_mtime else original_stat.st_mtime_ns + 10_000_000))
    recovered, recovery_budget, recovery_count = execute_failing_analysis_fixture(fixture)
    assert recovered.returncode == (17 if evidence_file == "auth_path" else 19)
    assert recovery_count == 6
    assert recovery_budget["evidence_generation_sha256"] != initial_budget["evidence_generation_sha256"]
    assert recovery_budget["attempts_started"] == 3
    assert len(recovery_budget["attempt_history"]) == 6
    assert recovery_budget["attempt_history"][:3] == initial_budget["attempt_history"]
    replay, replay_budget, replay_count = execute_failing_analysis_fixture(fixture)
    assert replay.returncode == 1 and replay_count == 6
    assert replay_budget == recovery_budget


def test_credential_readability_change_allows_bounded_recovery(tmp_path, isolated_analysis_environment):
    fixture = seed_failing_analysis_execution(tmp_path, isolated_analysis_environment)
    _, initial_budget, _ = execute_failing_analysis_fixture(fixture)
    fixture["auth_path"].chmod(0)
    if os.access(fixture["auth_path"], os.R_OK):
        pytest.skip("当前用户可绕过文件读取权限，无法用 chmod 复现不可读状态")
    try:
        recovered, recovery_budget, recovery_count = execute_failing_analysis_fixture(fixture)
        assert recovered.returncode == 17 and recovery_count == 6
        assert recovery_budget["attempt_history"][:3] == initial_budget["attempt_history"]
        replay, replay_budget, replay_count = execute_failing_analysis_fixture(fixture)
        assert replay.returncode == 1 and replay_count == 6
        assert replay_budget == recovery_budget
    finally:
        fixture["auth_path"].chmod(0o600)


def test_execution_entry_permission_repair_allows_bounded_recovery(tmp_path, isolated_analysis_environment):
    fixture = seed_failing_analysis_execution(tmp_path, isolated_analysis_environment, direct_executable=True)
    fixture["agent_path"].chmod(0o644)
    denied, initial_budget, initial_count = execute_failing_analysis_fixture(fixture)
    assert denied.returncode == 127 and initial_count == 0
    assert len(initial_budget["attempt_history"]) == 3
    fixture["agent_path"].chmod(0o755)
    recovered, recovery_budget, recovery_count = execute_failing_analysis_fixture(fixture)
    assert recovered.returncode == 17 and recovery_count == 3
    assert len(recovery_budget["attempt_history"]) == 6
    assert recovery_budget["attempt_history"][:3] == initial_budget["attempt_history"]
    replay, replay_budget, replay_count = execute_failing_analysis_fixture(fixture)
    assert replay.returncode == 1 and replay_count == 3
    assert replay_budget == recovery_budget


@pytest.mark.parametrize("changed_path", ["credential-configuration-root", "entry-symlink-target"])
def test_real_login_or_entry_path_change_allows_bounded_recovery(
    tmp_path, isolated_analysis_environment, changed_path,
):
    fixture = seed_failing_analysis_execution(tmp_path, isolated_analysis_environment)
    if changed_path == "entry-symlink-target":
        entry_link = tmp_path / "selected-analysis-entry.py"
        entry_link.symlink_to(fixture["agent_path"])
        fixture["agent_argv"][1] = str(entry_link)
    _, initial_budget, _ = execute_failing_analysis_fixture(fixture)
    if changed_path == "credential-configuration-root":
        new_codex_home = tmp_path / "new-selected-codex-configuration"
        new_codex_home.mkdir()
        (new_codex_home / "auth.json").write_bytes(fixture["auth_path"].read_bytes())
        fixture["environment"]["CODEX_HOME"] = str(new_codex_home)
    else:
        new_entry = tmp_path / "new-selected-analysis-agent.py"
        new_entry.write_bytes(fixture["agent_path"].read_bytes())
        entry_link.unlink()
        entry_link.symlink_to(new_entry)
    recovered, recovery_budget, recovery_count = execute_failing_analysis_fixture(fixture)
    assert recovered.returncode == 17 and recovery_count == 6
    assert recovery_budget["attempt_history"][:3] == initial_budget["attempt_history"]
    replay, replay_budget, replay_count = execute_failing_analysis_fixture(fixture)
    assert replay.returncode == 1 and replay_count == 6
    assert replay_budget == recovery_budget


def test_login_evidence_distinguishes_missing_directory_and_file(tmp_path, isolated_analysis_environment):
    wrapper = load_script_module("rvf_analyze_formal_archive_verified_execution")
    auth_path = Path(isolated_analysis_environment["CODEX_HOME"]) / "auth.json"
    generations = [wrapper.automatic_analysis_evidence_generation(tmp_path, [sys.executable])]
    auth_path.mkdir()
    generations.append(wrapper.automatic_analysis_evidence_generation(tmp_path, [sys.executable]))
    auth_path.rmdir()
    auth_path.write_text('{"token":"synthetic-credential"}')
    generations.append(wrapper.automatic_analysis_evidence_generation(tmp_path, [sys.executable]))
    assert len(set(generations)) == 3
