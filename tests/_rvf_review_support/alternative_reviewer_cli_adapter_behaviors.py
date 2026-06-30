#!/usr/bin/env python3
"""alternative reviewer CLI 适配器行为（codex/cursor/claude 流式、空闲、路由、预检） 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_alternative_reviewer_prompt_uses_session_env_refs',
    'test_alternative_reviewer_infers_scope_contract_from_inputs_layout',
    'test_alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract',
    'test_alternative_reviewer_pre_run_health_refreshes_before_reviewer',
    'test_alternative_reviewer_pre_run_health_failure_skips_reviewer',
    'test_alternative_reviewer_pre_run_health_timeout_skips_reviewer',
    'test_alternative_reviewer_idle_timeout_flag',
    'test_alternative_reviewer_activity_probe_keeps_silent_reviewer_alive',
    'test_alternative_reviewer_requires_review_result_artifact',
    'test_alternative_reviewer_records_request_as_pending_state',
    'test_alternative_reviewer_activity_probe_failure_threshold_times_out',
    'test_alternative_reviewer_timeout_kills_child_process_group',
    'test_alternative_reviewer_activity_refreshes_idle_timeout',
    'test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout',
    'test_alternative_reviewer_repeated_run_keeps_prior_artifacts',
    'test_alternative_reviewer_long_command_wait_uses_check_interval',
    'test_alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state',
    'test_alternative_reviewer_claude_stream_json_extracts_result',
    'test_alternative_reviewer_codex_json_extracts_agent_message',
    'test_alternative_reviewer_codex_json_extracts_item_completed_agent_message',
    'test_alternative_reviewer_codex_json_reports_backend_challenge_html',
    'test_alternative_reviewer_codex_exec_json_command_is_patched',
    'test_alternative_reviewer_codex_exec_after_global_options_is_patched',
    'test_alternative_reviewer_codex_hooks_disable_is_not_duplicated',
    'test_alternative_reviewer_sets_codex_stop_hook_suppress_env',
    'test_alternative_reviewer_legacy_claude_config_gets_stream_json',
    'test_alternative_reviewer_respects_explicit_claude_text_output',
    'test_alternative_reviewer_non_claude_stream_json_command_is_not_patched',
    'test_alternative_reviewer_cursor_stream_json_extracts_result',
    'test_alternative_reviewer_cursor_stream_monitor_detects_tool_layer_failure',
    'test_alternative_reviewer_cursor_tool_layer_failure_fast_aborts',
    'test_alternative_reviewer_cursor_command_not_claude_patched',
    'test_alternative_reviewer_cursor_autodetects_stream_json',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_alternative_reviewer_prompt_uses_session_env_refs(tmp_path: Path) -> None:
    module = load_alternative_reviewer_module()
    repo = init_repo(tmp_path / "repo")
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text("# Review Prompt\n\nBody\n", encoding="utf-8")
    context = tmp_path / "very" / "long" / "artifacts" / "scope-of-work.md"
    context.parent.mkdir(parents=True)
    context.write_text("scope\n", encoding="utf-8")
    packet = tmp_path / "very" / "long" / "artifacts" / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    scope_contract = packet.parent / "inputs" / "scope.contract.json"
    scope_contract.parent.mkdir()
    scope_contract.write_text('{"scope_hash":"abc"}\n', encoding="utf-8")

    result_path = tmp_path / "run" / "artifacts" / "reviewers" / "test" / "review-result.json"
    prompt = module.build_prompt(prompt_file, context, packet, repo, scope_contract, result_path)

    assert "$RVF_SCOPE_CONTRACT" in prompt
    assert "$RVF_SCOPE_OF_WORK" in prompt
    assert "$RVF_REVIEW_PACKET" in prompt
    assert "$RVF_COMMAND_LOCK" in prompt
    assert "$RVF_WRITE_REVIEW_RESULT" in prompt
    assert "$RVF_CHECK_REVIEW_RESULT" in prompt
    assert "$RVF_REVIEW_RESULT" in prompt
    assert "$RVF_REPO" in prompt
    assert "`primary_units` takes precedence over session manifest paths" in prompt
    assert "not as the final scope contract" in prompt
    assert str(scope_contract) not in prompt
    assert str(context) not in prompt
    assert str(result_path) not in prompt
    assert str(module.COMMAND_LOCK) not in prompt


def test_alternative_reviewer_infers_scope_contract_from_inputs_layout(tmp_path: Path) -> None:
    module = load_alternative_reviewer_module()
    inputs = tmp_path / "run" / "artifacts" / "inputs"
    inputs.mkdir(parents=True)
    packet = inputs / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    scope_contract = inputs / "scope.contract.json"
    scope_contract.write_text('{"scope_hash":"abc"}\n', encoding="utf-8")

    assert module.infer_scope_contract(packet) == scope_contract.resolve()


def test_alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "scope-of-work.md"
    context.write_text("scope\n", encoding="utf-8")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    scope_contract = tmp_path / "scope.contract.json"
    scope_contract.write_text('{"scope_hash":"abc"}\n', encoding="utf-8")
    reviewer_code = (
        "import os, sys; "
        "sys.stdin.read(); "
        f"expected = {str(context.resolve())!r}; "
        f"expected_scope = {str(scope_contract.resolve())!r}; "
        "assert os.environ['RVF_SCOPE_OF_WORK'] == expected; "
        "assert os.environ['RVF_SESSION_CONTEXT'] == expected; "
        "assert os.environ['RVF_SCOPE_CONTRACT'] == expected_scope; "
        "assert os.environ['RVF_REVIEW_RESULT']; "
        "import subprocess; "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
        "print('artifact written')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )

    completed = run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--review-packet",
            str(packet),
            "--scope-contract",
            str(scope_contract),
        ]
    )

    assert completed.stdout.strip() == "artifact written"


def test_alternative_reviewer_pre_run_health_refreshes_before_reviewer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    stale_run_dir = tmp_path / "stale-run"
    stale_run_dir.mkdir()
    token = tmp_path / "health-token"
    order = tmp_path / "order.txt"
    health_code = (
        "from pathlib import Path; "
        f"Path({str(token)!r}).write_text('ready', encoding='utf-8'); "
        f"Path({str(order)!r}).write_text('health\\n', encoding='utf-8'); "
        "print('HEALTH_OK')"
    )
    reviewer_code = (
        "import os, subprocess, sys; "
        "from pathlib import Path; "
        "sys.stdin.read(); "
        f"assert Path({str(token)!r}).read_text(encoding='utf-8') == 'ready'; "
        f"Path({str(order)!r}).write_text(Path({str(order)!r}).read_text(encoding='utf-8') + 'reviewer\\n', encoding='utf-8'); "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
        "print('artifact written')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        health_command=[sys.executable, "-c", health_code],
        pre_run_health=True,
    )
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(stale_run_dir)
    env["RVF_REVIEW_RESULT"] = str(stale_run_dir / "wrong" / "review-result.json")

    completed = run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env=env,
    )

    assert completed.stdout.strip() == "artifact written"
    assert order.read_text(encoding="utf-8") == "health\nreviewer\n"
    assert not (stale_run_dir / "wrong" / "review-result.json").exists()


def test_alternative_reviewer_pre_run_health_failure_skips_reviewer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    marker = tmp_path / "reviewer-ran"
    reviewer_code = (
        "from pathlib import Path; "
        "import sys; "
        "sys.stdin.read(); "
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        health_command=[sys.executable, "-c", "import sys; print('login failed'); sys.exit(7)"],
        pre_run_health=True,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "login failed" in completed.stderr
    assert not marker.exists()


def test_alternative_reviewer_pre_run_health_timeout_skips_reviewer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    marker = tmp_path / "reviewer-ran"
    reviewer_code = (
        "from pathlib import Path; "
        "import sys; "
        "sys.stdin.read(); "
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        health_command=[sys.executable, "-c", "import time; time.sleep(2)"],
        pre_run_health=True,
    )
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["health_timeout_seconds"] = 0.1
    config.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "health command timed out after" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert not marker.exists()


def test_alternative_reviewer_idle_timeout_flag(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(1.0)",
        ],
        idle_timeout_seconds=0.2,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr


def test_alternative_reviewer_activity_probe_keeps_silent_reviewer_alive(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import time; time.sleep(0.6); " + clean_review_result_python(stdout="NO_ISSUES"),
        ],
        idle_timeout_seconds=0.25,
        activity_check_interval_seconds=0.05,
        activity_probe_command=[
            sys.executable,
            "-c",
            "import os; print('PROBE ' + os.environ.get('RVF_REVIEWER_PID', ''))",
        ],
        activity_probe_timeout_seconds=0.5,
        activity_probe_failure_threshold=2,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-id",
            "probe-success-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["activity_probe_configured"] is True
    assert summary["activity_probe_history"]
    assert any(
        item["status"] == "completed" and item["stdout"].startswith("PROBE")
        for item in summary["activity_probe_history"]
    )
    normalized = Path(summary["paths"]["normalized"]).read_text(encoding="utf-8")
    assert normalized.strip() == "NO_ISSUES"
    assert "PROBE" not in normalized
    assert summary["review_result_valid"] is True
    result_summary = json.loads(Path(summary["paths"]["review_result_summary"]).read_text(encoding="utf-8"))
    assert result_summary["kind"] == "no_issues"


def test_alternative_reviewer_requires_review_result_artifact(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); print('NO_ISSUES')",
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-id",
            "missing-result-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "missing review result artifact" in completed.stderr
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["reason_code"] == "reviewer_result_invalid"
    assert summary["review_result_valid"] is False


def test_alternative_reviewer_records_request_as_pending_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    reviewer_code = (
        "import os, subprocess, sys; "
        "sys.stdin.read(); "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'lock-request', '--out', os.environ['RVF_REVIEW_RESULT'], "
        "'--name', 'pytest', '--command', 'python3 -m pytest', "
        "'--reason', 'needs serialized test cache'], check=True); "
        "print('request written')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-id",
            "request-pending-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "request written"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "pending"
    assert summary["reason_code"] == "reviewer_request_pending"
    assert summary["returncode"] == 0
    assert summary["review_result_valid"] is True
    assert summary["review_result_kind"] == "request"
    assert summary["review_result_complete"] is False
    assert summary["review_request_pending"] is True
    assert summary["review_result_summary"]["request_types"] == ["lock_request"]
    events = read_jsonl(run_dir / "events.jsonl")
    assert any(
        event["event"] == "request_pending"
        and event["reason_code"] == "reviewer_request_pending"
        and event["review_result_kind"] == "request"
        for event in events
    )


def test_alternative_reviewer_activity_probe_failure_threshold_times_out(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(1.0)",
        ],
        idle_timeout_seconds=0.08,
        activity_check_interval_seconds=0.03,
        activity_probe_command=[
            sys.executable,
            "-c",
            "import sys; print('inactive'); sys.exit(2)",
        ],
        activity_probe_timeout_seconds=0.5,
        activity_probe_failure_threshold=2,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-id",
            "probe-failure-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["reason_code"] == "reviewer_timeout"
    assert summary["timeout_reason"] == "no_observable_activity_probe_failed"
    assert summary["pid"] is not None
    assert summary["terminated_signal"] == "SIGKILL"
    assert len(summary["activity_probe_history"]) == 2
    assert all(item["returncode"] == 2 for item in summary["activity_probe_history"])
    normalized = Path(summary["paths"]["normalized"]).read_text(encoding="utf-8")
    assert normalized.strip() == "RVF_EXTERNAL_REVIEWER_TIMEOUT"


def test_alternative_reviewer_timeout_kills_child_process_group(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    marker = tmp_path / "child-survived.txt"
    child_code = (
        "import pathlib, time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        "sys.stdin.read(); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10.0)"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", parent_code],
        idle_timeout_seconds=0.5,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr
    # Wait past the (now 1s) child sleep so a *surviving* child would have
    # written the marker; the process-group kill at the ~0.5s idle timeout
    # happens regardless of the child's sleep length, so the proof and its
    # margin are unchanged.
    time.sleep(1.3)
    assert not marker.exists()


def test_alternative_reviewer_activity_refreshes_idle_timeout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, subprocess, sys, time; sys.stdin.read(); "
                "[print(f'tick-{i}', flush=True) or time.sleep(0.08) for i in range(4)]; "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
                "print('NO_ISSUES', flush=True)"
            ),
        ],
        idle_timeout_seconds=2.0,
        activity_check_interval_seconds=0.05,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "NO_ISSUES" in completed.stdout
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" not in completed.stderr


def test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, os, subprocess, sys, time; sys.stdin.read(); "
                "print(json.dumps({'type':'assistant','message':{'content':["
                "{'type':'tool_use','id':'toolu_1','name':'Bash','input':{'command':'sleep 1'}}"
                "]}}), flush=True); "
                "time.sleep(1.5); "
                "print(json.dumps({'type':'user','message':{'content':["
                "{'type':'tool_result','tool_use_id':'toolu_1','content':''}"
                "]}}), flush=True); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=1.0,
        activity_check_interval_seconds=0.03,
        output_format="claude_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES"
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" not in completed.stderr


def test_alternative_reviewer_repeated_run_keeps_prior_artifacts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            clean_review_result_python(stdout="NO_ISSUES"),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )
    command = [
        sys.executable,
        str(RUN_ALTERNATIVE_REVIEWER),
        "--config",
        str(config),
        "--repo",
        str(repo),
        "--review-packet",
        str(packet),
        "--rvf-run-id",
        "repeat-artifact-test",
        "--rvf-run-dir",
        str(run_dir),
    ]

    first = run(command)
    second = run(command)

    assert first.stdout.strip() == "NO_ISSUES"
    assert second.stdout.strip() == "NO_ISSUES"
    artifacts = run_dir / "artifacts" / "reviewers" / "test"
    for name in [
        "reviewer.prompt.txt",
        "reviewer.prompt.2.txt",
        "reviewer.stdout.txt",
        "reviewer.stdout.2.txt",
        "reviewer.stderr.txt",
        "reviewer.stderr.2.txt",
        "reviewer.normalized.txt",
        "reviewer.normalized.2.txt",
        "reviewer.summary.json",
        "reviewer.summary.2.json",
    ]:
        assert (artifacts / name).exists()
    assert not (run_dir / "artifacts" / "reviewer.prompt.txt").exists()


def test_alternative_reviewer_long_command_wait_uses_check_interval() -> None:
    module = load_alternative_reviewer_module()
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=2.0,
        max_runtime_remaining_seconds=None,
        waiting_on_long_command=False,
    ) == 2.0
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=0.0,
        max_runtime_remaining_seconds=None,
        waiting_on_long_command=True,
    ) == 5.0
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=0.0,
        max_runtime_remaining_seconds=2.0,
        waiting_on_long_command=True,
    ) == 2.0
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=0.0,
        max_runtime_remaining_seconds=None,
        waiting_on_long_command=False,
    ) == 0.01


def test_alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state() -> None:
    module = load_alternative_reviewer_module()
    monitor = module.ClaudeStreamActivityMonitor()
    tool_use_event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "sleep 1"},
                    }
                ]
            },
        }
    )
    split_at = len(tool_use_event) // 2

    monitor.ingest(tool_use_event[:split_at])
    assert monitor.waiting_on_long_command is False
    monitor.ingest(tool_use_event[split_at:] + "\n")
    assert monitor.waiting_on_long_command is True
    monitor.ingest(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "",
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    assert monitor.waiting_on_long_command is False

    monitor.ingest(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "sleep 1"},
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    assert monitor.waiting_on_long_command is True
    monitor.ingest(json.dumps({"type": "result", "result": "NO_ISSUES"}) + "\n")
    assert monitor.waiting_on_long_command is False


def test_alternative_reviewer_claude_stream_json_extracts_result(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, subprocess, sys, time, json; sys.stdin.read(); "
                "print(json.dumps({'type':'system','subtype':'init'}), flush=True); "
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'working'}]}}), flush=True); "
                "time.sleep(0.08); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
                "print(json.dumps({'type':'result','subtype':'success','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_codex_json_extracts_agent_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, os, subprocess, sys; sys.stdin.read(); "
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'working'}}), flush=True); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_codex_json_extracts_item_completed_agent_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, os, subprocess, sys; sys.stdin.read(); "
                "print('non-json warning line', flush=True); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'NO_ISSUES'}}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_codex_json_reports_backend_challenge_html(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    html = (
        "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
        "<body><script src=\"/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1\"></script>"
        "Cloudflare challenge</body></html>"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            f"import sys; sys.stdin.read(); print({html!r})",
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "RVF_CODEX_BACKEND_CHALLENGE" in completed.stdout
    assert "RVF_CODEX_BACKEND_CHALLENGE" in completed.stderr
    reviewer_dir = run_dir / "artifacts" / "reviewers" / "test"
    normalized = next(reviewer_dir.glob("reviewer.normalized*.txt")).read_text(encoding="utf-8")
    raw_stdout = next(reviewer_dir.glob("reviewer.stdout*.txt")).read_text(encoding="utf-8")
    summary = json.loads(next(reviewer_dir.glob("reviewer.summary*.json")).read_text(encoding="utf-8"))
    assert normalized.startswith("RVF_CODEX_BACKEND_CHALLENGE")
    assert "challenge-platform" in raw_stdout
    assert summary["output_error_reason"] == "codex_backend_challenge"


def test_alternative_reviewer_codex_exec_json_command_is_patched(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    shim = tmp_path / "codex"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["codex", "exec"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == [
        "--disable",
        "hooks",
        "exec",
        "--json",
        "--add-dir",
        str(run_dir.resolve()),
        "-",
    ]


def test_alternative_reviewer_codex_exec_after_global_options_is_patched(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    shim = tmp_path / "codex"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["codex", "--ask-for-approval", "never", "exec", "--sandbox", "workspace-write"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == [
        "--ask-for-approval",
        "never",
        "--disable",
        "hooks",
        "exec",
        "--sandbox",
        "workspace-write",
        "--json",
        "--add-dir",
        str(run_dir.resolve()),
        "-",
    ]


def test_alternative_reviewer_codex_hooks_disable_is_not_duplicated() -> None:
    module = load_alternative_reviewer_module()
    command = ["codex", "--disable", "hooks", "exec", "--json", "-"]
    assert module.ensure_codex_hooks_disabled_command(command) == command
    assert module.ensure_codex_hooks_disabled_command(
        ["codex", "--disable=hooks", "exec", "--json", "-"]
    ) == ["codex", "--disable=hooks", "exec", "--json", "-"]


def test_alternative_reviewer_sets_codex_stop_hook_suppress_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "codex"
    sink = tmp_path / "env.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps({"
                "'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK'), "
                "'thread': os.environ.get('CODEX_THREAD_ID')"
                "}))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["codex", "exec"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env.get('PATH', '')}"
    env["CODEX_THREAD_ID"] = "parent-thread-id-for-regression-test"
    env.pop("CODEX_RVF_SUPPRESS_STOP_HOOK", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    payload = json.loads(sink.read_text(encoding="utf-8"))
    assert payload == {
        "suppress": "1",
        "thread": "parent-thread-id-for-regression-test",
    }


def test_alternative_reviewer_legacy_claude_config_gets_stream_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "claude"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["claude", "-p"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--include-hook-events" in argv
    assert "--include-partial-messages" in argv
    assert "--verbose" in argv
    assert "--disable-slash-commands" in argv


def test_alternative_reviewer_respects_explicit_claude_text_output(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "claude"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["claude", "-p", "--output-format", "text"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format", "text"]


def test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "stream_wrapper"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-u", str(shim), "--native-stream"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    assert json.loads(sink.read_text(encoding="utf-8")) == ["--native-stream"]


def test_alternative_reviewer_cursor_stream_json_extracts_result(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, subprocess, sys, time, json; sys.stdin.read(); "
                "print(json.dumps({'type':'system','subtype':'init','model':'Composer 2.5'}), flush=True); "
                "print(json.dumps({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'working'}]}}), flush=True); "
                "time.sleep(0.08); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="cursor_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_cursor_stream_monitor_detects_tool_layer_failure() -> None:
    module = load_alternative_reviewer_module()
    spawn_error = _cursor_tool_call_line(
        "shellToolCall", {"spawnError": {"command": "", "workingDirectory": "", "error": "returned no exit status"}}
    )

    # 阈值前不触发；达到阈值（且零成功）触发；split 字节边界仍能识别
    monitor = module.CursorStreamActivityMonitor(tool_failure_threshold=3)
    monitor.ingest(spawn_error + "\n")
    monitor.ingest(spawn_error + "\n")
    assert monitor.tool_layer_unavailable is False
    half = len(spawn_error) // 2
    monitor.ingest(spawn_error[:half])
    assert monitor.tool_layer_unavailable is False
    monitor.ingest(spawn_error[half:] + "\n")
    assert monitor.tool_layer_unavailable is True
    # cursor monitor 刻意不抑制 idle 超时
    assert monitor.waiting_on_long_command is False

    # 出现过一次成功 => 工具层是好的，后续失败也不判不可用
    mixed = module.CursorStreamActivityMonitor(tool_failure_threshold=2)
    mixed.ingest(_cursor_tool_call_line("readToolCall", {"success": {"content": "x"}}) + "\n")
    for _ in range(5):
        mixed.ingest(spawn_error + "\n")
    assert mixed.tool_successes == 1
    assert mixed.tool_runtime_failures == 5
    assert mixed.tool_layer_unavailable is False


def test_alternative_reviewer_cursor_tool_layer_failure_fast_aborts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    spawn_error_event = {
        "type": "tool_call",
        "subtype": "completed",
        "tool_call": {
            "shellToolCall": {"result": {"spawnError": {"command": "", "workingDirectory": "", "error": "returned no exit status"}}}
        },
    }
    # 假 reviewer：发 init/叙述，再连发 3 条 spawnError（工具层坏），然后长睡。
    # idle_timeout 设很大（30s）：若 fast-abort 失效，测试会因等到 idle 而超慢/原因不符而失败。
    fake = (
        "import sys, time, json; sys.stdin.read(); "
        "print(json.dumps({'type':'system','subtype':'init','model':'Composer 2.5'}), flush=True); "
        "print(json.dumps({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'probing tools'}]}}), flush=True); "
        f"ev = {json.dumps(spawn_error_event)}; "
        "[ (print(json.dumps(ev), flush=True), time.sleep(0.05)) for _ in range(3) ]; "
        "time.sleep(30)"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-u", "-c", fake],
        idle_timeout_seconds=30.0,
        activity_check_interval_seconds=0.05,
        tool_failure_threshold=3,
        output_format="cursor_stream_json",
    )

    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-id",
            "cursor-tool-layer-fast-abort",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 124, completed.stderr
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr
    # 关键：远早于 idle_timeout(30s) 就因工具层判定而终止
    assert elapsed < 15.0, f"fast-abort 应远早于 idle_timeout，实际 {elapsed:.1f}s"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["timeout_reason"] == "cursor_tool_layer_unavailable", summary
    assert summary["terminated_signal"] == "SIGKILL"


def test_alternative_reviewer_cursor_command_not_claude_patched(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "cursor-agent"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print(json.dumps({'type':'result','subtype':'success','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["cursor-agent", "-p"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="cursor_stream_json",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    # ensure_cursor_stream_json_command 只补 print + stream-json，绝不注入 claude 专属 flag。
    assert argv == ["-p", "--output-format", "stream-json"], argv
    for claude_only_flag in (
        "--include-hook-events",
        "--include-partial-messages",
        "--verbose",
        "--disable-slash-commands",
    ):
        assert claude_only_flag not in argv, claude_only_flag


def test_alternative_reviewer_cursor_autodetects_stream_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "cursor-agent"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print(json.dumps({'type':'system','subtype':'init'}), flush=True)",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True)",
                "print(json.dumps({'type':'result','subtype':'success','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    # 故意不在 config 中写 output_format：依赖 is_cursor_print_command 自动判定为 cursor_stream_json。
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["cursor-agent", "-p"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    # 若 autodetect 误判为 text，stdout 会含 init JSON 行；等于 NO_ISSUES 证明走了 result 提取。
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format", "stream-json"], argv

