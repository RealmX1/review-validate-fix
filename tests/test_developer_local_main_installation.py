from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/rvf_local_deploy.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "rvf_local_deploy_for_developer_installation_tests",
        RUNNER,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository_with_main_and_task_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    local_main_worktree = tmp_path / "review-validate-fix-local-main"
    local_main_worktree.mkdir()
    _git(local_main_worktree, "init", "-b", "main")
    _git(local_main_worktree, "config", "user.name", "RVF Test")
    _git(local_main_worktree, "config", "user.email", "rvf@example.test")
    (local_main_worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
    (local_main_worktree / "scripts").mkdir()
    local_main_runner = local_main_worktree / "scripts/rvf_local_deploy.py"
    local_main_runner.write_text("# local main runner\n", encoding="utf-8")
    _git(local_main_worktree, "add", ".")
    _git(local_main_worktree, "commit", "-m", "test: seed")

    task_worktree = tmp_path / "review-validate-fix-task"
    _git(
        local_main_worktree,
        "worktree",
        "add",
        "-b",
        "codex/test-task",
        str(task_worktree),
    )
    return local_main_worktree, task_worktree


def test_validate_source_discovers_unique_clean_local_main_from_task_worktree(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    local_main_worktree, task_worktree = _repository_with_main_and_task_worktrees(
        tmp_path
    )
    local_main_commit = _git(local_main_worktree, "rev-parse", "HEAD")

    source = runner.validate_local_main_deployment_source(
        repository_hint=task_worktree,
        runner_path=local_main_worktree / "scripts/rvf_local_deploy.py",
        expected_local_main_commit="HEAD",
    )

    assert source.worktree_path == local_main_worktree.resolve()
    assert source.commit == local_main_commit


def test_validate_source_rejects_runner_loaded_from_task_worktree(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    _, task_worktree = _repository_with_main_and_task_worktrees(tmp_path)

    with pytest.raises(runner.DeployError, match="runner.*local main worktree"):
        runner.validate_local_main_deployment_source(
            repository_hint=task_worktree,
            runner_path=task_worktree / "scripts/rvf_local_deploy.py",
            expected_local_main_commit="HEAD",
        )


def test_validate_source_rejects_dirty_local_main(tmp_path: Path) -> None:
    runner = _load_runner()
    local_main_worktree, task_worktree = _repository_with_main_and_task_worktrees(
        tmp_path
    )
    (local_main_worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(runner.DeployError, match="local main worktree.*未提交改动"):
        runner.validate_local_main_deployment_source(
            repository_hint=task_worktree,
            runner_path=local_main_worktree / "scripts/rvf_local_deploy.py",
            expected_local_main_commit="HEAD",
        )


def test_validate_source_rejects_expected_commit_mismatch(tmp_path: Path) -> None:
    runner = _load_runner()
    local_main_worktree, task_worktree = _repository_with_main_and_task_worktrees(
        tmp_path
    )
    (task_worktree / "task-only.txt").write_text("task\n", encoding="utf-8")
    _git(task_worktree, "add", ".")
    _git(task_worktree, "commit", "-m", "test: task commit")

    with pytest.raises(runner.DeployError, match="expected local main commit"):
        runner.validate_local_main_deployment_source(
            repository_hint=task_worktree,
            runner_path=local_main_worktree / "scripts/rvf_local_deploy.py",
            expected_local_main_commit="codex/test-task",
        )


def test_public_runner_cli_has_no_repo_or_dirty_bypass() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--expected-local-main-commit" in completed.stdout
    assert "--json" in completed.stdout
    assert "--repo" not in completed.stdout
    assert "--allow-dirty" not in completed.stdout


def test_run_local_deploy_records_local_main_and_official_cli_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    configured_user_local_data_root = tmp_path / "configured-user-local-data"
    monkeypatch.setenv(
        "RVF_USER_LOCAL_DATA_ROOT",
        str(configured_user_local_data_root),
    )
    local_main_worktree = tmp_path / "review-validate-fix-local-main"
    local_main_worktree.mkdir()
    _git(local_main_worktree, "init", "-b", "main")
    _git(local_main_worktree, "config", "user.name", "RVF Test")
    _git(local_main_worktree, "config", "user.email", "rvf@example.test")
    scripts_root = local_main_worktree / "scripts"
    scripts_root.mkdir()
    local_main_runner = scripts_root / "rvf_local_deploy.py"
    local_main_runner.write_text("# local main runner\n", encoding="utf-8")
    contract_script = scripts_root / "check_plugin_contracts.py"
    contract_script.write_text(
        """\
import os
assert "RVF_USER_LOCAL_DATA_ROOT" not in os.environ
print("contracts OK")
""",
        encoding="utf-8",
    )
    installer_script = scripts_root / "install_to_codex.py"
    installer_script.write_text(
        """\
import json
import os
import pathlib
import subprocess
user_local_data_root = pathlib.Path(os.environ[\"RVF_USER_LOCAL_DATA_ROOT\"])
commit = subprocess.run([\"git\", \"rev-parse\", \"HEAD\"], check=True, capture_output=True, text=True).stdout.strip()
receipt = user_local_data_root / \"deployment-receipts/latest-official-cli-installation.json\"
receipt.parent.mkdir(parents=True, exist_ok=True)
payload = {
    \"status\": \"installed\",
    \"local_main_worktree\": str(pathlib.Path.cwd()),
    \"local_main_commit\": commit,
    \"generated_plugin_version\": \"0.1.0-dev.1+g\" + commit[:12],
    \"receipt_path\": str(receipt),
}
receipt.write_text(json.dumps(payload) + \"\\n\", encoding=\"utf-8\")
print(json.dumps(payload))
""",
        encoding="utf-8",
    )
    verifier_script = (
        local_main_worktree
        / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
    )
    verifier_script.parent.mkdir(parents=True)
    verifier_script.write_text(
        "import json\nprint(json.dumps({'status': 'ok', 'cache_payloads_match_active_release': True}))\n",
        encoding="utf-8",
    )
    _git(local_main_worktree, "add", ".")
    _git(local_main_worktree, "commit", "-m", "test: seed deploy fixture")
    home_directory = tmp_path / "home"

    result = runner.run_local_deploy(
        repository_hint=local_main_worktree,
        home=home_directory,
        expected_local_main_commit="HEAD",
        runner_path=local_main_runner,
    )

    assert result["status"] == "ok"
    assert result["local_main_worktree"] == str(local_main_worktree.resolve())
    assert result["local_main_commit"] == _git(
        local_main_worktree, "rev-parse", "HEAD"
    )
    assert result["verification"]["cache_payloads_match_active_release"] is True


def test_run_local_deploy_rolls_back_first_install_when_verification_fails(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    local_main_worktree = tmp_path / "review-validate-fix-local-main"
    local_main_worktree.mkdir()
    _git(local_main_worktree, "init", "-b", "main")
    _git(local_main_worktree, "config", "user.name", "RVF Test")
    _git(local_main_worktree, "config", "user.email", "rvf@example.test")
    scripts_root = local_main_worktree / "scripts"
    scripts_root.mkdir()
    local_main_runner = scripts_root / "rvf_local_deploy.py"
    local_main_runner.write_text("# local main runner\n", encoding="utf-8")
    (scripts_root / "check_plugin_contracts.py").write_text(
        "print('contracts OK')\n",
        encoding="utf-8",
    )
    (scripts_root / "install_to_codex.py").write_text(
        """\
import json
import os
import pathlib
import subprocess
import sys
home = pathlib.Path(os.environ["HOME"])
if "--rollback-remove-current-installation" in sys.argv:
    (home / "rollback-ran").write_text("removed_partial_first_install\\n", encoding="utf-8")
    print(json.dumps({"status": "rolled_back", "rollback_status": "removed_partial_first_install"}))
    raise SystemExit(0)
commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
receipt = home / ".rvf/user-local-data/deployment-receipts/latest-official-cli-installation.json"
receipt.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "status": "installed",
    "local_main_worktree": str(pathlib.Path.cwd()),
    "local_main_commit": commit,
    "generated_plugin_version": "0.1.0-dev.1+g" + commit[:12],
    "receipt_path": str(receipt),
}
receipt.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
print(json.dumps(payload))
""",
        encoding="utf-8",
    )
    verifier_script = (
        local_main_worktree
        / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
    )
    verifier_script.parent.mkdir(parents=True)
    verifier_script.write_text(
        "import sys\nprint('synthetic verifier failure', file=sys.stderr)\nraise SystemExit(9)\n",
        encoding="utf-8",
    )
    _git(local_main_worktree, "add", ".")
    _git(local_main_worktree, "commit", "-m", "test: verification rollback fixture")
    home_directory = tmp_path / "home"

    with pytest.raises(runner.DeployError) as raised:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=home_directory,
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    assert (home_directory / "rollback-ran").read_text(encoding="utf-8").strip() == (
        "removed_partial_first_install"
    )
    assert raised.value.result["rollback_status"] == "removed_partial_first_install"
