"""外部角色只校验实际派发的命令，保留选模、恢复及结束证据边界。"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from core.task_snapshot_review import harness_cli_adapters as cli
from core.task_snapshot_review.kimi_latest_stable_model_selection import select_latest_kimi_managed_stable_model
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.role_process_execution import launch_role


@pytest.mark.parametrize("role", ["reviewer", "fixer"])
def test_unused_health_command_does_not_prevent_actual_role_dispatch(tmp_path, role):
    repository = tmp_path / "candidate-repository"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "RVF test")
    git(repository, "config", "user.email", "rvf-test@example.invalid")
    (repository / "candidate.py").write_text("value = 1\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "test: 初始化派发候选")
    (repository / "background.txt").write_text("背景 WIP\n")
    attempt = tmp_path / "external-role-attempt"
    attempt.mkdir()
    invocation_marker_file = attempt / "actually-dispatched.json"
    executable = tmp_path / "kimi"
    payload = ({"status": "completed", "audit_summary": "已读取候选", "findings": []}
               if role == "reviewer" else {"status": "completed", "dispositions": []})
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,pathlib,sys\n"
        "if '--version' in sys.argv: print('isolated-test-cli'); sys.exit(0)\n"
        "assert '-p' in sys.argv and '--model' in sys.argv\n"
        f"assert {str(attempt / 'prompt.md')!r} in sys.argv[-1]\n"
        f"pathlib.Path({str(invocation_marker_file)!r}).write_text(json.dumps(sys.argv))\n"
        f"print(json.dumps({{'role':'assistant','content':{json.dumps(payload)!r}}}))\n"
        "print(json.dumps({'role':'meta','type':'session.resume_hint','content':'isolated-test-session'}))\n"
    )
    executable.chmod(0o755)
    run = attempt / "execution-lifecycle"
    write_json(run / "run.json", {"status": "review_pending"})
    write_json(attempt / "request.json", {
        "run_directory": str(run), "role": role, "repository": str(repository),
        "workspace": str(attempt / "workspace"), "candidate_commit": git_text(repository, "rev-parse", "HEAD"),
        "input_file": str(attempt / "request.json"), "result_file": str(attempt / "role-output.json"),
        "findings": [], "config": {
            "command": [str(executable), "--model", "isolated-fixed-model"],
            "health_command": [sys.executable, "-c", "raise SystemExit('不应执行的探针')"],
            "pre_run_health": False, "reviewer_model_selection_source": "kimi_latest_managed_stable_model",
            "prompt_delivery": "argv_prompt_flag_with_prompt_file_pointer", "prompt_flag": "-p",
            "output_format": "kimi_stream_json", "timeout_seconds": 20,
        },
    })
    (attempt / "prompt.md").write_text("读取固定候选并提交有效角色结果。\n")
    process = launch_role(attempt)
    assert process.wait(timeout=30) == 0
    outcome = read_json(attempt / "outcome.json")
    assert outcome["status"] == "completed", outcome
    assert outcome["returncode"] == 0
    assert invocation_marker_file.exists()
    assert read_json(attempt / "result.json") == payload
    assert git(repository, "status", "--porcelain") == b"?? background.txt\n"


@pytest.mark.parametrize("selector", [
    ["--model", "fixed-model"], ["-m", "fixed-model"], ["--model=fixed-model"], ["-mfixed-model"],
])
def test_explicit_kimi_model_does_not_refresh_catalog(monkeypatch, tmp_path, selector):
    def unexpected_refresh(*args, **kwargs):
        pytest.fail("固定模型不应刷新目录")
    monkeypatch.setattr(cli, "resolve_latest_kimi_managed_stable_model", unexpected_refresh)
    assert cli.resolve_reviewer_model_command(
        {"reviewer_model_selection_source": "kimi_latest_managed_stable_model"},
        ["kimi", *selector, "--output-format", "stream-json"], env={}, cwd=tmp_path, timeout=3,
    ) == ["kimi", "--model", "fixed-model", "--output-format", "stream-json"]


@pytest.mark.parametrize("selector", [["--model"], ["-m", "--continue"], ["--model="], ["-mone", "--model=two"]])
def test_invalid_kimi_model_selectors_remain_configuration_errors(tmp_path, selector):
    with pytest.raises(ValueError, match="模型"):
        cli.resolve_reviewer_model_command(
            {"reviewer_model_selection_source": "kimi_latest_managed_stable_model"},
            ["kimi", *selector], env={}, cwd=tmp_path, timeout=3,
        )


@pytest.mark.parametrize("selector", [["-c"], ["--continue"], ["-S", "session-id"], ["--session=session-id"], ["-Ssession-id"]])
def test_native_kimi_resume_retains_session_model_without_catalog_refresh(monkeypatch, tmp_path, selector):
    def unexpected_refresh(*args, **kwargs):
        pytest.fail("恢复会话应保留原生模型")
    monkeypatch.setattr(cli, "resolve_latest_kimi_managed_stable_model", unexpected_refresh)
    command = ["kimi", *selector]
    assert cli.resolve_reviewer_model_command(
        {"reviewer_model_selection_source": "kimi_latest_managed_stable_model"},
        command, env={}, cwd=tmp_path, timeout=3,
    ) == command


def test_new_kimi_role_refreshes_stable_model_and_propagates_catalog_failure(monkeypatch, tmp_path):
    catalog_refresh_calls = []
    def refresh(executable, *, env, timeout):
        catalog_refresh_calls.append((executable, env, timeout))
        return "kimi-code/k3.1-pro"
    monkeypatch.setattr(cli, "resolve_latest_kimi_managed_stable_model", refresh)
    config = {"reviewer_model_selection_source": "kimi_latest_managed_stable_model"}
    assert cli.resolve_reviewer_model_command(config, ["kimi"], env={"HOME": "isolated"}, cwd=tmp_path, timeout=30) == [
        "kimi", "--model", "kimi-code/k3.1-pro"]
    assert catalog_refresh_calls == [("kimi", {"HOME": "isolated"}, 10)]
    def failed_refresh(*args, **kwargs):
        raise ValueError("Kimi 官方 OAuth 模型目录刷新失败")
    monkeypatch.setattr(cli, "resolve_latest_kimi_managed_stable_model", failed_refresh)
    with pytest.raises(ValueError, match="OAuth"):
        cli.resolve_reviewer_model_command(config, ["kimi"], env={}, cwd=tmp_path, timeout=3)
    with pytest.raises(ValueError, match="kimi"):
        cli.resolve_reviewer_model_command(config, ["python3"], env={}, cwd=tmp_path, timeout=3)


def test_kimi_catalog_selects_latest_stable_pro_without_highspeed_or_preview():
    def model(alias, display_name="稳定模型"):
        return {"provider": "managed:kimi-code", "model": alias, "display_name": display_name, "max_context_size": 1048576}
    assert select_latest_kimi_managed_stable_model([
        model("kimi-code/k3"), model("kimi-code/k3.1"), model("kimi-code/k3.1-pro"),
        model("kimi-code/k3.1-pro-highspeed"), model("kimi-code/k4", "Preview"),
    ]) == "kimi-code/k3.1-pro"


def test_omp_shared_model_resolution_only_requires_dispatched_command(monkeypatch, tmp_path):
    def query(command, **kwargs):
        assert command == ["omp", "config", "get", "modelRoles", "--json"]
        return subprocess.CompletedProcess(command, 0, json.dumps({"key": "modelRoles", "value": {"default": "openai-codex/gpt-6:high"}}))
    monkeypatch.setattr(subprocess, "run", query)
    config = {"reviewer_model_selection_source": "oh_my_pi_default_model_role"}
    assert cli.resolve_reviewer_model_command(config, ["omp", "-p"], env={}, cwd=tmp_path, timeout=3) == [
        "omp", "--model", "openai-codex/gpt-6:high", "-p"]
    with pytest.raises(ValueError, match="固定"):
        cli.resolve_reviewer_model_command(config, ["omp", "-p", "--model=fixed"], env={}, cwd=tmp_path, timeout=3)


@pytest.mark.parametrize("field", ["health_command", "pre_run_health", "health_timeout_seconds"])
def test_user_override_rejects_removed_non_executed_health_options(tmp_path, field):
    bundled = tmp_path / "bundled-kimi.json"
    write_json(bundled, {"label": "alternative-reviewer:kimi-code", "command": ["kimi"]})
    override = cli.user_local_alternative_reviewer_config_override_path("alternative-reviewer:kimi-code", user_home=tmp_path)
    write_json(override, {"schema_version": 1, "reviewer_label": "alternative-reviewer:kimi-code", field: False})
    with pytest.raises(ValueError, match="unsupported fields"):
        cli.load_effective_alternative_reviewer_config(bundled, user_home=tmp_path)


def test_user_environment_override_still_reaches_effective_configuration(tmp_path):
    bundled = tmp_path / "bundled-kimi.json"
    write_json(bundled, {"label": "alternative-reviewer:kimi-code", "command": ["kimi"]})
    override = cli.user_local_alternative_reviewer_config_override_path("alternative-reviewer:kimi-code", user_home=tmp_path)
    write_json(override, {"schema_version": 1, "reviewer_label": "alternative-reviewer:kimi-code", "env_set": {"ISOLATED_TEST_SETTING": "enabled"}})
    config, receipt = cli.load_effective_alternative_reviewer_config(bundled, user_home=tmp_path)
    assert config["env_set"] == {"ISOLATED_TEST_SETTING": "enabled"}
    assert receipt == {"applied": True, "fields": ["env_set"], "env_set_names": ["ISOLATED_TEST_SETTING"]}
