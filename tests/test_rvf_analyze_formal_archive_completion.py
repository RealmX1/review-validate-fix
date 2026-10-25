"""后台复盘以正式产物为完成依据，失败不能污染业务工作区。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from _rvf_test_support.loader import load_script_module


SCRIPTS_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
)
EXECUTION_SCRIPT = SCRIPTS_DIRECTORY / "rvf_analyze_formal_archive_verified_execution.py"


def seed_formal_analysis(run_directory: Path, *, completed: bool = False) -> Path:
    analysis_directory = run_directory / "artifacts/analysis"
    analysis_directory.mkdir(parents=True)
    (analysis_directory / "summary.md").write_text(
        "# 已完成复盘\n\n没有需要归因的修复。\n" if completed
        else "# 复盘骨架\n<!-- TODO(rvf-analyze): 补全 -->\n"
    )
    (analysis_directory / "causality.json").write_text(json.dumps({
        "schema_version": 2, "run_id": run_directory.name,
        "issues": [{"issue_id": "RVF-001", "candidate_patch_call_ids": []}],
        "patches": [],
    }))
    (analysis_directory / ".analyze-thread.status.json").write_text(json.dumps({
        "run_dir": str(run_directory), "started_at": "原启动时间",
        "returncode": None,
    }))
    return analysis_directory


def run_verified_execution(run_directory: Path, command: list[str], repository: Path):
    guardian_root = run_directory.parent / "isolated-guardian-state"
    guardian_root.mkdir(exist_ok=True)
    guardian = load_script_module("agentic_initiation_guardian_dispatch")
    counter_path = guardian_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME
    if not counter_path.exists():
        counter_path.write_text(json.dumps({
            "analyze_completion_count": 0, "dispatched_milestones": [1, 2, 4, 8, 16],
            "guardian_retired": True,
        }))
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_"))
    }
    environment.update({
        "RVF_HOME": str(run_directory.parent / "isolated-rvf-state"),
        "RVF_AGENTIC_INITIATION_GUARDIAN_ROOT": str(guardian_root),
        "RVF_ANALYZE_THREAD": "1", "RVF_SUPPRESS_STOP_HOOK": "1",
    })
    result = subprocess.run(
        [sys.executable, str(EXECUTION_SCRIPT), "--run-dir", str(run_directory), "--", *command],
        cwd=repository, env=environment, input="冻结的分析输入", capture_output=True, text=True,
    )
    return result, json.loads(counter_path.read_text())["analyze_completion_count"]


def test_codex_command_grants_only_the_formal_analysis_directory(tmp_path):
    load_script_module("rvf_analyze_thread")
    from adapters.codex.subagent import build_analyze_command

    directory = tmp_path / "含空格的正式 analysis"
    command = build_analyze_command(codex_bin="codex", formal_analysis_directory=directory)
    assert command.argv[command.argv.index("--add-dir") + 1] == str(directory.resolve())
    assert command.argv.count("--add-dir") == 1
    assert "workspace-write" in command.argv
    assert "danger-full-access" not in command.argv
    assert command.argv[-1] == "-"


@pytest.mark.parametrize("invalid_output", [
    "scaffold", "missing-summary", "empty-summary", "malformed-json", "wrong-run",
    "wrong-schema", "unknown-call-id", "invalid-issues", "invalid-patches",
])
def test_zero_exit_with_invalid_formal_outputs_is_incomplete(tmp_path, invalid_output):
    run_directory = tmp_path / "archive outside repository" / "rvf-invalid"
    analysis_directory = seed_formal_analysis(run_directory, completed=True)
    summary = analysis_directory / "summary.md"
    causality = analysis_directory / "causality.json"
    payload = json.loads(causality.read_text())
    if invalid_output == "scaffold":
        summary.write_text("<!-- TODO(rvf-analyze): 未补全 -->")
    elif invalid_output == "missing-summary":
        summary.unlink()
    elif invalid_output == "empty-summary":
        summary.write_text(" \n")
    elif invalid_output == "malformed-json":
        causality.write_text("{")
    else:
        if invalid_output == "wrong-run":
            payload["run_id"] = "another-run"
        elif invalid_output == "wrong-schema":
            payload["schema_version"] = 999
        elif invalid_output == "unknown-call-id":
            payload["issues"][0]["candidate_patch_call_ids"] = ["invented"]
        elif invalid_output == "invalid-issues":
            payload["issues"] = None
        elif invalid_output == "invalid-patches":
            payload["patches"] = None
        causality.write_text(json.dumps(payload))
    repository = tmp_path / "business-repository"
    repository.mkdir()
    result, completion_count = run_verified_execution(
        run_directory, [sys.executable, "-c", "import sys; assert sys.stdin.read() == '冻结的分析输入'"], repository,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    status = json.loads((analysis_directory / ".analyze-thread.status.json").read_text())
    assert status["agent_process_returncode"] == 0
    assert status["formal_analysis_completion_status"] == "incomplete"
    assert status["formal_analysis_validation_errors"]
    assert status["started_at"] == "原启动时间"
    assert completion_count == 0
    assert list(repository.iterdir()) == []


def test_completed_empty_attribution_can_preserve_causality_bytes(tmp_path):
    run_directory = tmp_path / "archive" / "rvf-completed"
    analysis_directory = seed_formal_analysis(run_directory)
    before = (analysis_directory / "causality.json").read_bytes()
    repository = tmp_path / "business-repository"
    repository.mkdir()
    command = [sys.executable, "-c", (
        "from pathlib import Path; "
        f"Path({str(analysis_directory / 'summary.md')!r}).write_text('# 已完成复盘\\n没有修复 patch。\\n')"
    )]
    result, completion_count = run_verified_execution(run_directory, command, repository)
    assert result.returncode == 0, result.stdout + result.stderr
    status = json.loads((analysis_directory / ".analyze-thread.status.json").read_text())
    assert status["formal_analysis_completion_status"] == "completed"
    assert status["formal_analysis_validation_errors"] == []
    assert status["formal_analysis_output_sha256"]["summary.md"]
    assert (analysis_directory / "causality.json").read_bytes() == before
    assert completion_count == 1
    assert list(repository.iterdir()) == []


@pytest.mark.parametrize("command", [
    [sys.executable, "-c", "raise SystemExit(7)"], ["/nonexistent-rvf-analysis-agent"],
])
def test_agent_failure_never_counts_completed_outputs(tmp_path, command):
    run_directory = tmp_path / "archive" / "rvf-agent-failure"
    analysis_directory = seed_formal_analysis(run_directory, completed=True)
    result, completion_count = run_verified_execution(run_directory, command, tmp_path)
    assert result.returncode != 0
    status = json.loads((analysis_directory / ".analyze-thread.status.json").read_text())
    assert status["formal_analysis_completion_status"] == "failed"
    assert completion_count == 0


def test_analysis_launcher_exports_current_identity_and_uses_verified_execution(tmp_path, monkeypatch):
    module = load_script_module("rvf_analyze_thread")
    run_directory = tmp_path / "archive" / "rvf-launch"
    analysis_directory = seed_formal_analysis(run_directory)
    captured = {}

    def capture_launch(**kwargs):
        captured.update(kwargs)
        return {"launch_status": "launched", "returncode": 0, "error": None, "tmux_command": []}

    class Ledger:
        def env(self):
            return {"RVF_RUN_ID": run_directory.name, "RVF_RUN_DIR": str(run_directory)}

    monkeypatch.setattr(module, "launch_detached", capture_launch)
    monkeypatch.setenv("KANBAN_TASK_ID", "current-task")
    monkeypatch.setenv("CODEX_THREAD_ID", "current-session")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    result = module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis={
        "run_dir": str(run_directory), "summary_md_path": str(analysis_directory / "summary.md"),
        "causality_json_path": str(analysis_directory / "causality.json"),
    })
    assert result["launch_status"] == "launched"
    assert captured["exports"]["RVF_RUN_DIR"] == str(run_directory)
    assert captured["exports"]["KANBAN_TASK_ID"] == "current-task"
    assert captured["exports"]["CODEX_THREAD_ID"] == "current-session"
    assert captured["exports"]["CLAUDE_CODE_SESSION_ID"] == ""
    assert captured["exports"]["RVF_ANALYZE_THREAD"] == "1"
    assert str(EXECUTION_SCRIPT) in captured["argv"]
    assert not captured.get("post_exit_argv")


def test_invalid_output_path_does_not_grant_writes_or_remove_existing_lock(tmp_path, monkeypatch):
    module = load_script_module("rvf_analyze_thread")
    run_directory = tmp_path / "archive" / "rvf-path-check"
    analysis_directory = seed_formal_analysis(run_directory)
    lock = analysis_directory / ".analyze-thread.lock"
    lock.write_text("另一个线程持有")
    monkeypatch.setattr(module, "launch_detached", lambda **kwargs: pytest.fail("不应启动线程"))
    result = module.launch_detached_analyze_thread(event={}, ledger=None, analysis={
        "run_dir": str(run_directory), "summary_md_path": str(analysis_directory / "summary.md"),
        "causality_json_path": str(tmp_path / "outside-causality.json"),
    })
    assert result["launch_status"] == "launch_failed"
    assert lock.read_text() == "另一个线程持有"


@pytest.mark.parametrize("linked_name", ["analysis", "summary.md", "causality.json", ".analyze-thread.status.json"])
def test_formal_outputs_cannot_escape_through_symlinks(tmp_path, linked_name):
    module = load_script_module("rvf_analyze_formal_archive_verified_execution")
    run_directory = tmp_path / "archive" / "rvf-symlink"
    analysis_directory = seed_formal_analysis(run_directory, completed=True)
    if linked_name == "analysis":
        outside = tmp_path / "outside-analysis"
        analysis_directory.rename(outside)
        analysis_directory.symlink_to(outside, target_is_directory=True)
    else:
        output = analysis_directory / linked_name
        outside = tmp_path / linked_name
        output.rename(outside)
        output.symlink_to(outside)
    result = module.validate_formal_analysis_outputs(run_directory)
    assert result["valid"] is False
    assert "符号链接" in result["errors"][0]


@pytest.mark.parametrize("invent_patch", [False, True])
def test_candidate_attribution_may_only_reference_preexisting_patch_facts(tmp_path, invent_patch):
    run_directory = tmp_path / "archive" / "rvf-attribution"
    analysis_directory = seed_formal_analysis(run_directory, completed=True)
    causality_path = analysis_directory / "causality.json"
    causality = json.loads(causality_path.read_text())
    if not invent_patch:
        causality["patches"] = [{"call_id": "recorded-patch"}]
        causality_path.write_text(json.dumps(causality))
    command = [sys.executable, "-c", (
        "from pathlib import Path; import json; "
        f"p=Path({str(causality_path)!r}); c=json.loads(p.read_text()); "
        "c['patches']=[{'call_id':'recorded-patch'}]; "
        "c['issues'][0]['candidate_patch_call_ids']=['recorded-patch']; "
        "p.write_text(json.dumps(c))"
    )]
    result, count = run_verified_execution(run_directory, command, tmp_path)
    assert (result.returncode == 0) is (not invent_patch)
    assert count == (0 if invent_patch else 1)


@pytest.mark.parametrize("fact_mutation", ["rewrite-issue-id", "invent-patch"])
@pytest.mark.parametrize("first_agent_returncode", [0, 7])
def test_failed_retry_preserves_original_facts_until_legal_recovery(
    tmp_path, fact_mutation, first_agent_returncode,
):
    run_directory = tmp_path / "archive" / "rvf-immutable-retry"
    analysis_directory = seed_formal_analysis(run_directory, completed=True)
    causality_path = analysis_directory / "causality.json"
    original_causality = json.loads(causality_path.read_text())
    original_causality["patches"] = [{"call_id": "recorded-patch"}]
    causality_path.write_text(json.dumps(original_causality))
    mutation = (
        "c['issues'][0]['issue_id']='rewritten-issue'"
        if fact_mutation == "rewrite-issue-id" else
        "c['patches'].append({'call_id':'invented-patch'})"
    )
    command = [sys.executable, "-c", (
        "from pathlib import Path; import json; "
        f"p=Path({str(causality_path)!r}); c=json.loads(p.read_text()); "
        f"{mutation}; p.write_text(json.dumps(c)); "
        f"raise SystemExit({first_agent_returncode})"
    )]
    first_result, count = run_verified_execution(run_directory, command, tmp_path)
    assert first_result.returncode != 0
    assert count == 0
    status_path = analysis_directory / ".analyze-thread.status.json"
    first_status = json.loads(status_path.read_text())
    assert first_status["formal_analysis_completion_status"] == (
        "failed" if first_agent_returncode else "incomplete"
    )

    retry_result, count = run_verified_execution(
        run_directory, [sys.executable, "-c", "pass"], tmp_path,
    )
    assert retry_result.returncode != 0, retry_result.stdout + retry_result.stderr
    assert count == 0
    retry_status = json.loads(status_path.read_text())
    assert retry_status["formal_analysis_completion_status"] == "incomplete"
    assert any("已记录事实" in error for error in retry_status["formal_analysis_validation_errors"])

    original_causality["issues"][0]["candidate_patch_call_ids"] = ["recorded-patch"]
    recovery_command = [sys.executable, "-c", (
        "from pathlib import Path; "
        f"Path({str(causality_path)!r}).write_text({json.dumps(original_causality)!r})"
    )]
    recovered_result, count = run_verified_execution(run_directory, recovery_command, tmp_path)
    assert recovered_result.returncode == 0, recovered_result.stdout + recovered_result.stderr
    assert count == 1
    assert json.loads(status_path.read_text())["formal_analysis_completion_status"] == "completed"


@pytest.mark.parametrize("snapshot_damage", [
    "missing", "malformed-json", "changed-facts", "symlink", "unreadable-directory",
])
def test_original_snapshot_damage_cannot_adopt_current_outputs(tmp_path, snapshot_damage):
    module = load_script_module("rvf_analyze_formal_archive_verified_execution")
    run_directory = tmp_path / "archive" / "rvf-snapshot-damage"
    analysis_directory = seed_formal_analysis(run_directory, completed=True)
    result, count = run_verified_execution(
        run_directory, [sys.executable, "-c", "raise SystemExit(7)"], tmp_path,
    )
    assert result.returncode == 7
    assert count == 0
    snapshot_path = analysis_directory.parent / module.ORIGINAL_CAUSALITY_SNAPSHOT_FILENAME
    if snapshot_damage == "missing":
        snapshot_path.unlink()
    elif snapshot_damage == "malformed-json":
        snapshot_path.write_text("{")
    elif snapshot_damage == "changed-facts":
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["recorded_causality"]["issues"][0]["issue_id"] = "rewritten-issue"
        snapshot_path.write_text(json.dumps(snapshot))
    elif snapshot_damage == "symlink":
        outside = tmp_path / "outside-original-causality.json"
        snapshot_path.rename(outside)
        snapshot_path.symlink_to(outside)
    else:
        snapshot_path.unlink()
        snapshot_path.mkdir()
    agent_started_marker = analysis_directory / "unexpected-agent-started"
    retry_result, count = run_verified_execution(run_directory, [sys.executable, "-c", (
        "from pathlib import Path; "
        f"Path({str(agent_started_marker)!r}).write_text('不应启动')"
    )], tmp_path)
    assert retry_result.returncode != 0
    assert count == 0
    assert not agent_started_marker.exists()


def test_initial_unreadable_facts_cannot_become_an_agent_generated_baseline(tmp_path):
    run_directory = tmp_path / "archive" / "rvf-invalid-original-facts"
    analysis_directory = seed_formal_analysis(run_directory, completed=True)
    causality_path = analysis_directory / "causality.json"
    original_bytes = causality_path.read_bytes()
    causality_path.write_text("{")
    repair_command = [sys.executable, "-c", (
        "from pathlib import Path; "
        f"Path({str(causality_path)!r}).write_bytes({original_bytes!r})"
    )]
    for command in (repair_command, [sys.executable, "-c", "pass"]):
        result, count = run_verified_execution(run_directory, command, tmp_path)
        assert result.returncode != 0
        assert count == 0


def test_detached_shell_retries_incomplete_run_and_preserves_completed_run(tmp_path, monkeypatch):
    module = load_script_module("rvf_analyze_thread")
    guardian = load_script_module("agentic_initiation_guardian_dispatch")
    run_directory = tmp_path / "archive with spaces" / "rvf-detached-retry"
    analysis_directory = seed_formal_analysis(run_directory)
    guardian_root = tmp_path / "guardian-state"
    guardian_root.mkdir()
    counter_path = guardian_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME
    counter_path.write_text(json.dumps({
        "analyze_completion_count": 0, "dispatched_milestones": [1, 2, 4, 8, 16],
        "guardian_retired": True,
    }))
    monkeypatch.setenv("RVF_AGENTIC_INITIATION_GUARDIAN_ROOT", str(guardian_root))
    monkeypatch.setenv("KANBAN_TASK_ID", "current-task")
    monkeypatch.setenv("CODEX_THREAD_ID", "current-session")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    fake_agent = tmp_path / "fake-codex"
    fake_agent.write_text("#!" + sys.executable + "\n" + '''
import json, os, pathlib, sys
assert os.environ['RVF_RUN_ID'] == 'rvf-detached-retry'
assert os.environ['KANBAN_TASK_ID'] == 'current-task'
assert os.environ['CODEX_THREAD_ID'] == 'current-session'
assert os.environ['CLAUDE_CODE_SESSION_ID'] == ''
assert os.environ['RVF_ANALYZE_THREAD'] == '1'
assert '不要重新执行 rvf_analyze.py' in sys.stdin.read()
directory=pathlib.Path(sys.argv[sys.argv.index('--add-dir')+1])
assert directory == pathlib.Path(os.environ['RVF_RUN_DIR'])/'artifacts/analysis'
if os.environ['FAKE_ANALYSIS_FILL'] != '0':
    (directory/'summary.md').write_text('# 已完成复盘\\n没有修复 patch。\\n')
causality_path=directory/'causality.json'
causality=json.loads(causality_path.read_text())
causality['issues'][0]['issue_id']=(
    'RVF-001' if os.environ['FAKE_ANALYSIS_FILL'] == '2' else 'rewritten-issue'
)
causality_path.write_text(json.dumps(causality))
''')
    fake_agent.chmod(0o755)
    monkeypatch.setattr(module, "codex_bin", lambda: str(fake_agent))
    fake_tmux = tmp_path / "fake-tmux"
    fake_tmux.write_text("#!" + sys.executable + "\n" + '''
import os, subprocess, sys
if sys.argv[1] == 'has-session':
    raise SystemExit(1)
environment=dict(os.environ)
environment.update(RVF_RUN_ID='stale-run', RVF_RUN_DIR='/stale-run',
    KANBAN_TASK_ID='stale-task', CODEX_THREAD_ID='stale-session',
    CLAUDE_CODE_SESSION_ID='stale-claude-session')
raise SystemExit(subprocess.run(['/bin/sh','-c',sys.argv[-1]],env=environment).returncode)
''')
    fake_tmux.chmod(0o755)
    monkeypatch.setenv("RVF_TMUX_BIN", str(fake_tmux))

    class Ledger:
        def env(self):
            return {"RVF_RUN_ID": run_directory.name, "RVF_RUN_DIR": str(run_directory)}

    analysis = {"run_dir": str(run_directory),
                "summary_md_path": str(analysis_directory / "summary.md"),
                "causality_json_path": str(analysis_directory / "causality.json")}
    status_path = analysis_directory / ".analyze-thread.status.json"
    monkeypatch.setenv("FAKE_ANALYSIS_FILL", "0")
    assert module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)["launch_status"] == "launched"
    incomplete = json.loads(status_path.read_text())
    assert incomplete["returncode"] != 0
    assert incomplete["agent_process_returncode"] == 0
    assert incomplete["formal_analysis_completion_status"] == "incomplete"
    assert json.loads(counter_path.read_text())["analyze_completion_count"] == 0
    monkeypatch.setenv("FAKE_ANALYSIS_FILL", "1")
    # 新执行版本是可检测的恢复证据；单改产物不重置持久尝试预算。
    fake_agent.write_text(fake_agent.read_text() + "\n# 修复候选版本一\n")
    assert module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)["launch_status"] == "launched"
    illegal_retry = json.loads(status_path.read_text())
    assert illegal_retry["returncode"] != 0
    assert illegal_retry["formal_analysis_completion_status"] == "incomplete"
    assert json.loads(counter_path.read_text())["analyze_completion_count"] == 0
    execution = load_script_module("rvf_analyze_formal_archive_verified_execution")
    snapshot_path = analysis_directory.parent / execution.ORIGINAL_CAUSALITY_SNAPSHOT_FILENAME
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_path.unlink()
    assert module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)["launch_status"] == "launched"
    assert json.loads(status_path.read_text())["returncode"] != 0
    assert not snapshot_path.exists()
    assert json.loads(counter_path.read_text())["analyze_completion_count"] == 0
    snapshot_path.write_bytes(snapshot_bytes)
    monkeypatch.setenv("FAKE_ANALYSIS_FILL", "2")
    fake_agent.write_text(fake_agent.read_text() + "\n# 修复候选版本二\n")
    assert module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)["launch_status"] == "launched"
    completed = json.loads(status_path.read_text())
    assert completed["returncode"] == 0
    assert completed["formal_analysis_completion_status"] == "completed"
    assert completed["finished_at"]
    assert json.loads(counter_path.read_text())["analyze_completion_count"] == 1
    assert module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)["launch_status"] == "already_running"
    assert json.loads(counter_path.read_text())["analyze_completion_count"] == 1


def test_guardian_dispatch_grants_its_own_report_directory(tmp_path, monkeypatch):
    guardian = load_script_module("agentic_initiation_guardian_dispatch")
    detached = load_script_module("rvf_detached_thread")
    captured = {}

    def capture_launch(**kwargs):
        captured.update(kwargs)
        return {"launch_status": "launched", "error": None}

    monkeypatch.setattr(detached, "launch_detached", capture_launch)
    guardian._default_guardian_analysis_dispatcher(
        milestone=1, guardian_root=tmp_path, metrics_snapshot={},
    )
    command = captured["argv"]
    assert command[command.index("--add-dir") + 1] == str(tmp_path / "guardian-analysis-1")
    assert str(EXECUTION_SCRIPT) not in command


def test_tmux_launch_failure_records_formal_failure(tmp_path, monkeypatch):
    module = load_script_module("rvf_analyze_thread")
    run_directory = tmp_path / "archive" / "rvf-launch-failed"
    analysis_directory = seed_formal_analysis(run_directory)
    monkeypatch.setenv("RVF_TMUX_BIN", "/nonexistent-rvf-tmux")

    class Ledger:
        def env(self):
            return {}

    result = module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis={
        "run_dir": str(run_directory), "summary_md_path": str(analysis_directory / "summary.md"),
        "causality_json_path": str(analysis_directory / "causality.json"),
    })
    assert result["launch_status"] == "launch_failed"
    status = json.loads((analysis_directory / ".analyze-thread.status.json").read_text())
    assert status["formal_analysis_completion_status"] == "failed"
    assert status["agent_process_returncode"] is None
    assert not (analysis_directory / ".analyze-thread.lock").exists()


@pytest.mark.parametrize("obsolete_failure_stage", ["after-lock-release", "before-lock-acquisition"])
def test_obsolete_launch_failure_preserves_completed_retry(
    tmp_path, monkeypatch, obsolete_failure_stage,
):
    module = load_script_module("rvf_analyze_thread")
    guardian = load_script_module("agentic_initiation_guardian_dispatch")
    run_directory = tmp_path / "archive" / "rvf-obsolete-launch-failure"
    analysis_directory = seed_formal_analysis(run_directory)
    status_path = analysis_directory / ".analyze-thread.status.json"
    lock_path = analysis_directory / ".analyze-thread.lock"
    guardian_root = tmp_path / "guardian-state"
    guardian_root.mkdir()
    counter_path = guardian_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME
    counter_path.write_text(json.dumps({
        "analyze_completion_count": 0, "dispatched_milestones": [1, 2, 4, 8, 16],
        "guardian_retired": True,
    }))
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-state"))
    monkeypatch.setenv("RVF_AGENTIC_INITIATION_GUARDIAN_ROOT", str(guardian_root))
    fake_agent = tmp_path / "fake-codex"
    fake_agent.write_text("#!" + sys.executable + "\n" + '''
import pathlib, sys
assert '不要重新执行 rvf_analyze.py' in sys.stdin.read()
directory=pathlib.Path(sys.argv[sys.argv.index('--add-dir')+1])
(directory/'summary.md').write_text('# 已完成复盘\\n没有修复 patch。\\n')
''')
    fake_agent.chmod(0o755)
    monkeypatch.setattr(module, "codex_bin", lambda: str(fake_agent))
    fake_tmux = tmp_path / "fake-tmux"
    fake_tmux.write_text("#!" + sys.executable + "\n" + '''
import os, subprocess, sys
if sys.argv[1] == 'has-session':
    raise SystemExit(1)
if os.environ.get('FAKE_TMUX_OBSOLETE_LAUNCH') == '1':
    print('隔离的旧 tmux 启动失败', file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(subprocess.run(['/bin/sh','-c',sys.argv[-1]]).returncode)
''')
    fake_tmux.chmod(0o755)
    monkeypatch.setenv("RVF_TMUX_BIN", str(fake_tmux))
    original_launch = module.launch_detached
    original_builder = module.build_analyze_command
    obsolete_failure_ready = threading.Event()
    release_obsolete_call = threading.Event()
    obsolete_thread_identity = None

    def launch_with_paused_failure(**kwargs):
        obsolete_call = threading.get_ident() == obsolete_thread_identity
        if obsolete_call:
            kwargs["launch_env"] = {**kwargs["launch_env"], "FAKE_TMUX_OBSOLETE_LAUNCH": "1"}
        result = original_launch(**kwargs)
        if obsolete_call:
            assert result["launch_status"] == "launch_failed"
            failure_status = json.loads(status_path.read_text())
            assert failure_status["formal_analysis_completion_status"] == "failed"
            assert "隔离的旧" in failure_status["error"]
            obsolete_failure_ready.set()
            assert release_obsolete_call.wait(10), "新重试未按期完成"
        return result

    def build_with_paused_failure(*args, **kwargs):
        if (
            obsolete_failure_stage == "before-lock-acquisition"
            and threading.get_ident() == obsolete_thread_identity
        ):
            obsolete_failure_ready.set()
            assert release_obsolete_call.wait(10), "新重试未按期完成"
            raise ValueError("隔离的旧分析命令构建失败")
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(module, "launch_detached", launch_with_paused_failure)
    monkeypatch.setattr(module, "build_analyze_command", build_with_paused_failure)

    class Ledger:
        def env(self):
            return {"RVF_RUN_ID": run_directory.name, "RVF_RUN_DIR": str(run_directory)}

    analysis = {"run_dir": str(run_directory),
                "summary_md_path": str(analysis_directory / "summary.md"),
                "causality_json_path": str(analysis_directory / "causality.json")}

    def launch_obsolete_call():
        nonlocal obsolete_thread_identity
        obsolete_thread_identity = threading.get_ident()
        return module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)

    with ThreadPoolExecutor(max_workers=1) as executor:
        obsolete_future = executor.submit(launch_obsolete_call)
        try:
            assert obsolete_failure_ready.wait(10), "旧失败未到达确定性屏障"
            assert not lock_path.exists()
            retry = module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)
            assert retry["launch_status"] == "launched"
            completed_status = status_path.read_bytes()
            completed = json.loads(completed_status)
            assert completed["formal_analysis_completion_status"] == "completed"
            assert completed["returncode"] == 0
            assert completed["formal_analysis_output_sha256"]
            assert json.loads(counter_path.read_text())["analyze_completion_count"] == 1
        finally:
            release_obsolete_call.set()
        obsolete_result = obsolete_future.result(timeout=10)
    assert obsolete_result["launch_status"] == "launch_failed"
    assert "隔离的旧" in obsolete_result["error"]
    after_obsolete_failure = status_path.read_bytes()
    third_launch = module.launch_detached_analyze_thread(event={}, ledger=Ledger(), analysis=analysis)
    completion_count = json.loads(counter_path.read_text())["analyze_completion_count"]
    assert after_obsolete_failure == completed_status, {
        "third_launch_status": third_launch["launch_status"],
        "analyze_completion_count": completion_count,
    }
    assert third_launch["launch_status"] == "already_running"
    assert completion_count == 1


@pytest.mark.parametrize("reserved_field", [
    "launch_status", "returncode", "error", "lock_path", "status_path",
])
def test_launch_failure_extensions_cannot_override_owned_state(tmp_path, reserved_field):
    module = load_script_module("rvf_detached_thread")
    status_path = tmp_path / "completed.status.json"
    completed_status = json.dumps({"returncode": 0, "formal_analysis_completion_status": "completed"})
    status_path.write_text(completed_status)
    lock_path = tmp_path / "another-launch.lock"
    lock_path.write_text("另一个调用持有的锁")
    result = module.launch_detached(
        session_name="rvf-reserved-failure-fields",
        argv=[sys.executable, "-c", "pass"],
        log_path=tmp_path / "launch.log",
        status_path=status_path,
        lock_path=lock_path,
        status_payload={"returncode": None},
        launch_failure_status_fields={reserved_field: "不允许覆盖"},
    )
    assert result["launch_status"] == "launch_failed"
    assert reserved_field in result["error"]
    assert status_path.read_text() == completed_status
    assert lock_path.read_text() == "另一个调用持有的锁"
