from __future__ import annotations

import importlib.util
import json
import os
import shutil
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


def test_validate_source_reports_dirty_local_main_without_blocking(
    tmp_path: Path,
) -> None:
    """local main 的未提交改动是合法状态：只上报，不再是失败原因。

    payload 来自 deploy 源 worktree 的 HEAD checkout，dirty 既进不了发布件，也不需要被
    stash 挪走 —— 挪走才是危险的，那会把正在 local main 上工作的人/agent 的文件抽掉。
    """
    runner = _load_runner()
    local_main_worktree, task_worktree = _repository_with_main_and_task_worktrees(
        tmp_path
    )
    (local_main_worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (local_main_worktree / "untracked-wip.txt").write_text("wip\n", encoding="utf-8")

    source = runner.validate_local_main_deployment_source(
        repository_hint=task_worktree,
        runner_path=local_main_worktree / "scripts/rvf_local_deploy.py",
        expected_local_main_commit="HEAD",
    )

    assert source.dirty_paths == ("tracked.txt", "untracked-wip.txt")
    assert source.runner_matches_local_main_head is True


def test_validate_source_reports_runner_diverged_from_local_main_head(
    tmp_path: Path,
) -> None:
    """runner 自己是未提交版本时必须如实上报。

    dirty 放行之后，「本次执行的编排逻辑」与「被部署的编排逻辑」可能不是同一份；
    这不是失败，但不暴露就会被误读成部署件里已经带上了这次改动。
    """
    runner = _load_runner()
    local_main_worktree, task_worktree = _repository_with_main_and_task_worktrees(
        tmp_path
    )
    (local_main_worktree / "scripts/rvf_local_deploy.py").write_text(
        "# local main runner with uncommitted change\n",
        encoding="utf-8",
    )

    source = runner.validate_local_main_deployment_source(
        repository_hint=task_worktree,
        runner_path=local_main_worktree / "scripts/rvf_local_deploy.py",
        expected_local_main_commit="HEAD",
    )

    assert source.runner_matches_local_main_head is False
    assert source.dirty_paths == ("scripts/rvf_local_deploy.py",)


def test_validate_source_pins_runner_baseline_to_frozen_deployment_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 基线必须绑定「本次冻结的部署 commit」，不得跟着 local main 的新 HEAD 漂。

    并行 agent 可能在 HEAD 被冻结之后向 local main 追加提交。若比较目标是活的 `HEAD`，
    该字段回答的就不再是「本次执行的 runner 是不是本次被部署的那一版」——磁盘上的 runner
    与新 HEAD 相符会被误报成 true，尽管发布件用的是更早的那个 commit。
    """
    runner = _load_runner()
    local_main_worktree, task_worktree = _repository_with_main_and_task_worktrees(
        tmp_path
    )
    frozen_deployment_commit = _git(local_main_worktree, "rev-parse", "HEAD")
    real_status_paths = runner._status_paths
    concurrent_commit_landed = False

    def _land_concurrent_local_main_commit_then_report_status(
        repository: Path,
    ) -> list[str]:
        # `_status_paths` 落在「HEAD 已冻结」与「runner 基线比较」之间，是模拟并行 agent
        # 抢先提交的天然注入点。
        nonlocal concurrent_commit_landed
        if (
            not concurrent_commit_landed
            and Path(repository).resolve() == local_main_worktree.resolve()
        ):
            concurrent_commit_landed = True
            (local_main_worktree / "scripts/rvf_local_deploy.py").write_text(
                "# runner committed by another agent after the freeze\n",
                encoding="utf-8",
            )
            _git(local_main_worktree, "add", ".")
            _git(
                local_main_worktree,
                "commit",
                "-m",
                "test: concurrent local main commit after freeze",
            )
        return real_status_paths(repository)

    monkeypatch.setattr(
        runner,
        "_status_paths",
        _land_concurrent_local_main_commit_then_report_status,
    )

    source = runner.validate_local_main_deployment_source(
        repository_hint=task_worktree,
        runner_path=local_main_worktree / "scripts/rvf_local_deploy.py",
        expected_local_main_commit=None,
    )

    assert concurrent_commit_landed is True
    assert source.commit == frozen_deployment_commit
    assert _git(local_main_worktree, "rev-parse", "HEAD") != frozen_deployment_commit
    # 磁盘上的 runner 此刻等于新 HEAD 的版本，却不等于被部署的 frozen commit 版本。
    assert source.runner_matches_local_main_head is False


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


def _seed_succeeding_official_cli_deploy_fixture(local_main_worktree: Path) -> Path:
    """写入「全部成功」的假 contract / installer / verifier 三件套，返回 local main runner 路径。

    假 installer 与真安装器同构地把 official installation receipt 写到
    `RVF_USER_LOCAL_DATA_ROOT` 下 —— commit-delta 的 OLD 正是从这份 receipt 读的。
    """
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
    \"plugin_selector\": \"review-validate-fix@review-validate-fix-local-main\",
    \"receipt_path\": str(receipt),
}
receipt.write_text(json.dumps(payload) + \"\\n\", encoding=\"utf-8\")
print(json.dumps(payload))
"""
        + _SYNTHETIC_CODEX_PLUGIN_REGISTRATION_SOURCE,
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
    return local_main_runner


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
    local_main_runner = _seed_succeeding_official_cli_deploy_fixture(local_main_worktree)
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
    assert result["tool_runtime_python"]["python_version"] == sys.version.split()[0]
    assert result["tool_runtime_python"]["required_module_preflight_results"] == {
        "tomllib": "available", "pytest": "available",
    }
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["tool_runtime_python"] == result["tool_runtime_python"]
    assert all(command[0] == result["tool_runtime_python"]["python_executable_absolute_path"] for command in result["commands"])


def _seed_local_main_with_rollback_recording_installer(
    tmp_path: Path,
    *,
    verifier_source: str,
    installer_registers_canonical_plugin_in_codex_config: bool = True,
) -> tuple[Path, Path]:
    """建一个 stub installer 会把「回滚跑过了」写成磁盘证据的 local main fixture。

    回滚是否发生只能靠磁盘证据判定——它是子进程里的一条 CLI 调用，进程返回值里看不出来。
    `installer_registers_canonical_plugin_in_codex_config=False` 用来制造「核不到唯一性」
    （config.toml 根本不存在）那一路。
    """
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
    installer_source = """\
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
    "plugin_selector": "review-validate-fix@review-validate-fix-local-main",
    "receipt_path": str(receipt),
}
receipt.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
print(json.dumps(payload))
"""
    if installer_registers_canonical_plugin_in_codex_config:
        installer_source += _SYNTHETIC_CODEX_PLUGIN_REGISTRATION_SOURCE
    (scripts_root / "install_to_codex.py").write_text(
        installer_source,
        encoding="utf-8",
    )
    verifier_script = (
        local_main_worktree
        / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
    )
    verifier_script.parent.mkdir(parents=True)
    verifier_script.write_text(verifier_source, encoding="utf-8")
    _git(local_main_worktree, "add", ".")
    _git(local_main_worktree, "commit", "-m", "test: deploy failure-handling fixture")
    return local_main_worktree, local_main_runner


def test_run_local_deploy_rolls_back_first_install_when_verification_fails(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    local_main_worktree, local_main_runner = (
        _seed_local_main_with_rollback_recording_installer(
            tmp_path,
            verifier_source=(
                "import sys\n"
                "print('synthetic verifier failure', file=sys.stderr)\n"
                "raise SystemExit(9)\n"
            ),
        )
    )
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
    # 失败/回滚结果一个 forward 列表都不给：没装成的东西不能被读成「已投入使用」。
    assert "source_commit_delta" not in raised.value.result


def test_registration_uniqueness_verification_failure_refuses_certification_without_rollback(
    tmp_path: Path,
) -> None:
    """verifier 判「存在第二份 RVF 注册」时**不得回滚**，只拒绝认证。

    回滚会移除刚装好的 canonical，把机器留在「只剩残留那份」的更差状态——正是这条门禁要消灭
    的局面。verifier 用专属退出码把这一类与其余验证失败区分开；把它并进通用失败分支，写在下游
    的「刻意不回滚」分支就永远轮不到执行。
    """
    runner = _load_runner()
    local_main_worktree, local_main_runner = (
        _seed_local_main_with_rollback_recording_installer(
            tmp_path,
            verifier_source=(
                "import sys\n"
                "print('Codex 存在多份 RVF 插件注册: [...]', file=sys.stderr)\n"
                f"raise SystemExit({runner.OFFICIAL_CLI_VERIFIER_REGISTRATION_UNIQUENESS_EXIT_CODE})\n"
            ),
        )
    )
    home_directory = tmp_path / "home"

    with pytest.raises(runner.DeployError) as raised:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=home_directory,
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    assert not (home_directory / "rollback-ran").exists()
    result = raised.value.result
    assert result["phase"] == "official_cli_plugin_registration_uniqueness"
    assert result["rollback_status"] == "not_required"
    assert "rollback" not in result
    assert "存在多份 RVF 插件注册" in result["error"]
    assert result["remediation"] == runner.REGISTRATION_UNIQUENESS_REMEDIATION
    # 刚装好的 canonical 必须原地不动：这才是「部署做完但拒绝认证」而不是「部署被撤销」。
    installation_receipt = (
        home_directory
        / ".rvf/user-local-data/deployment-receipts/latest-official-cli-installation.json"
    )
    assert json.loads(installation_receipt.read_text(encoding="utf-8"))["status"] == (
        "installed"
    )
    # runner 自己的退出码 3 早已是「已有 deploy 正在运行」，不得被 verifier 的退出码顶掉。
    assert raised.value.exit_code == 2


def test_deploy_refuses_certification_when_registration_uniqueness_is_unavailable(
    tmp_path: Path,
) -> None:
    """「核不到」不是「核过了、没问题」：唯一性核对不可用同样拒绝认证，且同样不回滚。"""
    runner = _load_runner()
    local_main_worktree, local_main_runner = (
        _seed_local_main_with_rollback_recording_installer(
            tmp_path,
            verifier_source="import json\nprint(json.dumps({'status': 'ok'}))\n",
            installer_registers_canonical_plugin_in_codex_config=False,
        )
    )
    home_directory = tmp_path / "home"

    with pytest.raises(runner.DeployError) as raised:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=home_directory,
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    assert not (home_directory / "rollback-ran").exists()
    result = raised.value.result
    assert result["status"] == "failed"
    assert result["phase"] == "codex_registration_uniqueness"
    assert result["rollback_status"] == "not_required"
    assert result["codex_registration_uniqueness"]["status"] == "unavailable"


def test_contract_failure_receipt_records_host_load_and_failed_contract_step_labels(
    tmp_path: Path,
) -> None:
    """契约检查失败时 receipt 必须留下定性所需的现场，而不是只留一个 phase 名。

    这两项都是 2026-07-25 那次 `plugin_contract_check` 失败复盘时缺失的信息：
    当时既不知道具体哪个 shard 红了，也只能靠事后 `uptime` 猜机器当时有多忙。
    """
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
    contract_script = scripts_root / "check_plugin_contracts.py"
    contract_script.write_text(
        """\
import sys
print("RVF_CONTRACT_PARALLEL_EXECUTION jobs=4 cpu=10 load1m=57", file=sys.stderr)
print("验证失败: tests: review_support_scripts shard 1/4", file=sys.stderr)
print("验证失败: tests: stop_hook shard 4/4", file=sys.stderr)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    installer_script = scripts_root / "install_to_codex.py"
    installer_script.write_text(
        "raise SystemExit('installer must not run after contract failure')\n",
        encoding="utf-8",
    )
    verifier_script = (
        local_main_worktree
        / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
    )
    verifier_script.parent.mkdir(parents=True)
    verifier_script.write_text("raise SystemExit('verifier must not run')\n", encoding="utf-8")
    _git(local_main_worktree, "add", ".")
    _git(local_main_worktree, "commit", "-m", "test: contract failure observability fixture")

    with pytest.raises(runner.DeployError) as raised:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=tmp_path / "home",
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    result = raised.value.result
    assert result["phase"] == "plugin_contract_check"
    assert result["failed_contract_step_labels"] == [
        "tests: review_support_scripts shard 1/4",
        "tests: stop_hook shard 4/4",
    ]
    assert result["contract_effective_parallel_jobs"] == 4
    assert result["host_cpu_count"] == os.cpu_count()
    assert isinstance(result["host_load_average_1m"], float)


CANONICAL_PLUGIN_SELECTOR = "review-validate-fix@review-validate-fix-local-main"

_SYNTHETIC_CODEX_PLUGIN_REGISTRATION_SOURCE = '''
# 复刻 `codex plugin add` 的可见后果：把 canonical selector 写进 ~/.codex/config.toml。
# runner 的收尾门禁要的是「canonical 确实在册」的正向证明——stub 安装器不写这一步，
# 部署就该被拒绝认证（这正是 fixture 必须跟着真实安装器走的原因，不是可省的装饰）。
config_path = pathlib.Path(os.environ["HOME"]) / ".codex/config.toml"
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(
    '[plugins."SELECTOR_PLACEHOLDER"]\\nenabled = true\\n', encoding="utf-8"
)
'''.replace("SELECTOR_PLACEHOLDER", CANONICAL_PLUGIN_SELECTOR)


_DEPLOY_SOURCE_OBSERVING_INSTALLER_SOURCE = """\
import json
import os
import pathlib
import subprocess

source_tree = pathlib.Path.cwd()
user_local_data_root = pathlib.Path(os.environ["RVF_USER_LOCAL_DATA_ROOT"])
commit = subprocess.run(
    ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
).stdout.strip()
observation = {
    "source_tree": str(source_tree),
    "tracked_text": (source_tree / "tracked.txt").read_text(encoding="utf-8"),
    "untracked_marker_present": (source_tree / "untracked-wip.txt").exists(),
    "status_output": subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout,
}
observation_path = user_local_data_root / "installer-observation.json"
observation_path.parent.mkdir(parents=True, exist_ok=True)
observation_path.write_text(json.dumps(observation), encoding="utf-8")

receipt = user_local_data_root / "deployment-receipts/latest-official-cli-installation.json"
receipt.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "status": "installed",
    "local_main_worktree": str(source_tree),
    "local_main_commit": commit,
    "generated_plugin_version": "0.1.0-dev.1+g" + commit[:12],
    "plugin_selector": "review-validate-fix@review-validate-fix-local-main",
    "receipt_path": str(receipt),
}
receipt.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
print(json.dumps(payload))
""" + _SYNTHETIC_CODEX_PLUGIN_REGISTRATION_SOURCE


def _seed_local_main_with_deploy_entrypoints(tmp_path: Path) -> tuple[Path, Path]:
    """建一个可完整跑通 deploy 的 local main fixture（contract/installer/verifier 均为 stub）。"""
    local_main_worktree = tmp_path / "review-validate-fix-local-main"
    local_main_worktree.mkdir()
    _git(local_main_worktree, "init", "-b", "main")
    _git(local_main_worktree, "config", "user.name", "RVF Test")
    _git(local_main_worktree, "config", "user.email", "rvf@example.test")
    (local_main_worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
    scripts_root = local_main_worktree / "scripts"
    scripts_root.mkdir()
    local_main_runner = scripts_root / "rvf_local_deploy.py"
    local_main_runner.write_text("# local main runner\n", encoding="utf-8")
    (scripts_root / "check_plugin_contracts.py").write_text(
        "print('contracts OK')\n",
        encoding="utf-8",
    )
    (scripts_root / "install_to_codex.py").write_text(
        _DEPLOY_SOURCE_OBSERVING_INSTALLER_SOURCE,
        encoding="utf-8",
    )
    verifier_script = (
        local_main_worktree
        / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
    )
    verifier_script.parent.mkdir(parents=True)
    verifier_script.write_text(
        "import json\nprint(json.dumps({'status': 'ok'}))\n",
        encoding="utf-8",
    )
    _git(local_main_worktree, "add", ".")
    _git(local_main_worktree, "commit", "-m", "test: seed deploy source fixture")
    return local_main_worktree, local_main_runner


def test_dirty_local_main_deploys_from_head_without_touching_worktree(
    tmp_path: Path,
) -> None:
    """带 WIP 的 local main 能部署，且它的工作树一个字节都没被动过。

    这是本方案取代自动 stash 的核心断言：stash 方案在部署窗口内会把开发者/其它 agent 的
    未提交文件从磁盘上抽走；这里则是 local main 全程只读 ref。
    """
    runner = _load_runner()
    local_main_worktree, local_main_runner = _seed_local_main_with_deploy_entrypoints(
        tmp_path
    )
    (local_main_worktree / "tracked.txt").write_text("dirty wip\n", encoding="utf-8")
    (local_main_worktree / "untracked-wip.txt").write_text("wip\n", encoding="utf-8")
    status_before = _git(
        local_main_worktree, "status", "--porcelain=v1", "--untracked-files=all"
    )
    home_directory = tmp_path / "home"

    result = runner.run_local_deploy(
        repository_hint=local_main_worktree,
        home=home_directory,
        expected_local_main_commit="HEAD",
        runner_path=local_main_runner,
    )

    assert result["status"] == "ok"
    assert result["local_main_dirty_paths"] == ["tracked.txt", "untracked-wip.txt"]
    assert result["runner_matches_local_main_head"] is True
    assert result["deployment_source_worktree"] == str(
        runner.deployment_source_worktree_path(home_directory)
    )
    assert result["deployment_source_worktree_created"] is True
    assert result["deployment_source_worktree_synced_to_commit"] == result[
        "local_main_commit"
    ]

    observation = json.loads(
        (
            home_directory / ".rvf/user-local-data/installer-observation.json"
        ).read_text(encoding="utf-8")
    )
    assert observation["source_tree"] == str(
        runner.deployment_source_worktree_path(home_directory)
    )
    assert observation["tracked_text"] == "base\n"
    assert observation["untracked_marker_present"] is False
    assert observation["status_output"] == ""

    assert (local_main_worktree / "tracked.txt").read_text(
        encoding="utf-8"
    ) == "dirty wip\n"
    assert (local_main_worktree / "untracked-wip.txt").read_text(
        encoding="utf-8"
    ) == "wip\n"
    assert (
        _git(local_main_worktree, "status", "--porcelain=v1", "--untracked-files=all")
        == status_before
    )
    assert _git(local_main_worktree, "stash", "list") == ""


def test_deployment_source_worktree_is_reused_and_scrubbed_between_deploys(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    local_main_worktree, local_main_runner = _seed_local_main_with_deploy_entrypoints(
        tmp_path
    )
    home_directory = tmp_path / "home"

    first = runner.run_local_deploy(
        repository_hint=local_main_worktree,
        home=home_directory,
        expected_local_main_commit="HEAD",
        runner_path=local_main_runner,
    )
    assert first["deployment_source_worktree_created"] is True

    deployment_source_worktree = runner.deployment_source_worktree_path(home_directory)
    residue = deployment_source_worktree / "stale-build-residue.txt"
    residue.write_text("residue\n", encoding="utf-8")
    (deployment_source_worktree / "tracked.txt").write_text(
        "clobbered\n", encoding="utf-8"
    )

    second = runner.run_local_deploy(
        repository_hint=local_main_worktree,
        home=home_directory,
        expected_local_main_commit="HEAD",
        runner_path=local_main_runner,
    )

    assert second["deployment_source_worktree_created"] is False
    assert not residue.exists()
    assert (deployment_source_worktree / "tracked.txt").read_text(
        encoding="utf-8"
    ) == "base\n"
    worktree_paths = [
        record["worktree"]
        for record in runner._git_worktree_records(local_main_worktree)
    ]
    assert worktree_paths.count(str(deployment_source_worktree)) == 1


def test_deployment_source_worktree_fails_closed_on_foreign_worktree_residue(
    tmp_path: Path,
) -> None:
    """落点上若坐着别的仓库的 worktree，必须识别出来并拒绝部署，而不是删掉重建。

    识别外来树只是第一层；第二层是识别之后做什么 —— 删除不可恢复，而 basename 对得上
    并不能证明这棵树归 RVF，所以只能把处置权交回给人。
    """
    runner = _load_runner()
    local_main_worktree, local_main_runner = _seed_local_main_with_deploy_entrypoints(
        tmp_path
    )
    home_directory = tmp_path / "home"
    deployment_source_worktree = runner.deployment_source_worktree_path(home_directory)

    foreign_repository = tmp_path / "foreign-repository"
    foreign_repository.mkdir()
    _git(foreign_repository, "init", "-b", "main")
    _git(foreign_repository, "config", "user.name", "Foreign Test")
    _git(foreign_repository, "config", "user.email", "foreign@example.test")
    (foreign_repository / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _git(foreign_repository, "add", ".")
    _git(foreign_repository, "commit", "-m", "test: foreign seed")
    deployment_source_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(
        foreign_repository,
        "worktree",
        "add",
        "--detach",
        str(deployment_source_worktree),
        "HEAD",
    )
    assert (deployment_source_worktree / "foreign.txt").is_file()

    with pytest.raises(runner.DeployError) as failure:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=home_directory,
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    # 外来树被识别出来了，而且是被「认出并拒绝」而不是「认出并清掉」。
    assert "无法证明它归 RVF 所有" in str(failure.value)
    assert "另一个 git 仓库的 worktree" in str(failure.value)
    assert failure.value.result["status"] == "failed"
    assert (
        failure.value.result["phase"] == "deployment_source_worktree_preparation"
    )

    # 外来仓库的内容与注册都必须原封不动，本仓也不得在落点上建出任何东西。
    assert (deployment_source_worktree / "foreign.txt").read_text(
        encoding="utf-8"
    ) == "foreign\n"
    assert str(deployment_source_worktree) in _git(
        foreign_repository, "worktree", "list"
    )
    assert not (deployment_source_worktree / "tracked.txt").exists()
    assert str(deployment_source_worktree) not in _git(
        local_main_worktree, "worktree", "list"
    )


def test_deployment_source_worktree_fails_closed_on_branch_attached_own_worktree(
    tmp_path: Path,
) -> None:
    """落点是本仓 worktree 但挂着分支时，里面装的是开发者的活，必须拒绝而不是清空重建。

    这是最危险的一支：`git worktree remove --force` 对本仓的树一定会成功，未提交改动
    连同分支 checkout 一起消失，且部署还会报告「成功」。
    """
    runner = _load_runner()
    local_main_worktree, local_main_runner = _seed_local_main_with_deploy_entrypoints(
        tmp_path
    )
    home_directory = tmp_path / "home"
    deployment_source_worktree = runner.deployment_source_worktree_path(home_directory)
    deployment_source_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(
        local_main_worktree,
        "worktree",
        "add",
        "-b",
        "developer-wip",
        str(deployment_source_worktree),
    )
    precious_work = deployment_source_worktree / "precious-uncommitted-work.txt"
    precious_work.write_text("irreplaceable\n", encoding="utf-8")

    with pytest.raises(runner.DeployError) as failure:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=home_directory,
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    assert "无法证明它归 RVF 所有" in str(failure.value)
    assert "挂在分支 refs/heads/developer-wip 上" in str(failure.value)
    assert precious_work.read_text(encoding="utf-8") == "irreplaceable\n"
    assert "developer-wip" in _git(local_main_worktree, "worktree", "list")


def test_deployment_source_worktree_is_created_into_vacant_landing_site_with_stale_registration(
    tmp_path: Path,
) -> None:
    """fail-closed 不能把正常的首建/重建回路一并堵死。

    空目录没有任何可丢失的东西，因此仍必须能建起来；指向它的僵尸注册要被 prune 掉，
    否则 `worktree add` 会被拒。
    """
    runner = _load_runner()
    local_main_worktree, local_main_runner = _seed_local_main_with_deploy_entrypoints(
        tmp_path
    )
    home_directory = tmp_path / "home"
    deployment_source_worktree = runner.deployment_source_worktree_path(home_directory)
    deployment_source_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(
        local_main_worktree,
        "worktree",
        "add",
        "--detach",
        str(deployment_source_worktree),
        "HEAD",
    )
    # 留下「注册还在、树已被清空」的僵尸状态。
    shutil.rmtree(deployment_source_worktree)
    deployment_source_worktree.mkdir()

    result = runner.run_local_deploy(
        repository_hint=local_main_worktree,
        home=home_directory,
        expected_local_main_commit="HEAD",
        runner_path=local_main_runner,
    )

    assert result["status"] == "ok"
    assert result["deployment_source_worktree_created"] is True
    assert (deployment_source_worktree / "tracked.txt").read_text(
        encoding="utf-8"
    ) == "base\n"
    worktree_paths = [
        record["worktree"]
        for record in runner._git_worktree_records(local_main_worktree)
    ]
    assert worktree_paths.count(str(deployment_source_worktree)) == 1


def test_deployment_source_worktree_fails_closed_on_own_detached_worktree_without_ownership_evidence(
    tmp_path: Path,
) -> None:
    """「同仓 + detached」这组形状不构成归属证明，不得据此就地 reset --hard + clean -xdff。

    这个形状任何人都手工造得出来（`git worktree add --detach` 一条命令），而复用分支紧接着
    就是无条件清空。只有 RVF 亲手建树时写下的归属证据才能授权销毁；没有证据就必须走
    fail-closed，把处置权交回给人。
    """
    runner = _load_runner()
    local_main_worktree, local_main_runner = _seed_local_main_with_deploy_entrypoints(
        tmp_path
    )
    home_directory = tmp_path / "home"
    deployment_source_worktree = runner.deployment_source_worktree_path(home_directory)
    deployment_source_worktree.parent.mkdir(parents=True, exist_ok=True)
    # 手工建一棵形状与 RVF 自有树完全一致的树：同仓、detached、basename 也对得上。
    _git(
        local_main_worktree,
        "worktree",
        "add",
        "--detach",
        str(deployment_source_worktree),
        "HEAD",
    )
    precious_tracked_edit = deployment_source_worktree / "tracked.txt"
    precious_tracked_edit.write_text("uncommitted human work\n", encoding="utf-8")
    precious_untracked_file = deployment_source_worktree / "precious-untracked.txt"
    precious_untracked_file.write_text("irreplaceable\n", encoding="utf-8")

    with pytest.raises(runner.DeployError) as failure:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=home_directory,
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    assert "无法证明它归 RVF 所有" in str(failure.value)
    assert "没有 RVF 归属证据" in str(failure.value)
    assert failure.value.result["phase"] == "deployment_source_worktree_preparation"
    # 未提交的 tracked 改动与 untracked 文件都必须原封不动。
    assert precious_tracked_edit.read_text(encoding="utf-8") == "uncommitted human work\n"
    assert precious_untracked_file.read_text(encoding="utf-8") == "irreplaceable\n"


def test_deployment_source_worktree_fails_closed_on_symlink_landing_site(
    tmp_path: Path,
) -> None:
    """符号链接落点在任何分支上都不得进入销毁回路。

    `Path.is_dir()` / `exists()` 都跟随符号链接，因此一条指向他人 worktree 的链接会让
    「可复用」判定看到一棵完全合规的树，随后 reset --hard + clean -xdff 落在**链接目标**上。
    真实内容在别处，删除同样不可逆。
    """
    runner = _load_runner()
    local_main_worktree, local_main_runner = _seed_local_main_with_deploy_entrypoints(
        tmp_path
    )
    home_directory = tmp_path / "home"
    deployment_source_worktree = runner.deployment_source_worktree_path(home_directory)
    deployment_source_worktree.parent.mkdir(parents=True, exist_ok=True)

    symlink_target_worktree = tmp_path / "developer-own-detached-worktree"
    _git(
        local_main_worktree,
        "worktree",
        "add",
        "--detach",
        str(symlink_target_worktree),
        "HEAD",
    )
    precious_untracked_file = symlink_target_worktree / "precious-untracked.txt"
    precious_untracked_file.write_text("irreplaceable\n", encoding="utf-8")
    deployment_source_worktree.symlink_to(symlink_target_worktree)

    with pytest.raises(runner.DeployError) as failure:
        runner.run_local_deploy(
            repository_hint=local_main_worktree,
            home=home_directory,
            expected_local_main_commit="HEAD",
            runner_path=local_main_runner,
        )

    assert "无法证明它归 RVF 所有" in str(failure.value)
    assert "符号链接" in str(failure.value)
    # 链接目标里的内容必须完好，链接本身也不该被动过。
    assert precious_untracked_file.read_text(encoding="utf-8") == "irreplaceable\n"
    assert deployment_source_worktree.is_symlink()


def test_deployment_source_worktree_fails_closed_when_head_does_not_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    local_main_worktree, _ = _repository_with_main_and_task_worktrees(tmp_path)
    local_main_commit = _git(local_main_worktree, "rev-parse", "HEAD")
    deployment_source_worktree = runner.deployment_source_worktree_path(
        tmp_path / "home"
    )
    real_git = runner._git

    def _git_with_drifted_deploy_head(repo: Path, *args: str) -> str:
        if list(args) == ["rev-parse", "HEAD"] and Path(repo) == deployment_source_worktree:
            return "0" * 40
        return real_git(repo, *args)

    monkeypatch.setattr(runner, "_git", _git_with_drifted_deploy_head)

    with pytest.raises(runner.DeployError, match="未收敛到 local main commit"):
        runner._ensure_deployment_source_worktree(
            local_main_worktree=local_main_worktree,
            deployment_source_worktree=deployment_source_worktree,
            commit=local_main_commit,
        )


def test_deployment_source_worktree_fails_closed_when_residual_paths_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    local_main_worktree, _ = _repository_with_main_and_task_worktrees(tmp_path)
    local_main_commit = _git(local_main_worktree, "rev-parse", "HEAD")
    deployment_source_worktree = runner.deployment_source_worktree_path(
        tmp_path / "home"
    )

    monkeypatch.setattr(
        runner,
        "_status_paths",
        lambda repo: (
            ["leftover-build-artifact.txt"]
            if Path(repo) == deployment_source_worktree
            else []
        ),
    )

    with pytest.raises(runner.DeployError, match="同步后仍不干净"):
        runner._ensure_deployment_source_worktree(
            local_main_worktree=local_main_worktree,
            deployment_source_worktree=deployment_source_worktree,
            commit=local_main_commit,
        )


def test_parse_contract_check_failure_observations_ignores_unrelated_output() -> None:
    runner = _load_runner()
    assert runner.parse_contract_check_failure_observations(
        "plugin 契约检查失败\nsome unrelated stderr noise\n"
    ) == {}


def _commit_source_change(repository: Path, filename: str, subject: str) -> str:
    (repository / filename).write_text(f"{subject}\n", encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "-m", subject)
    return _git(repository, "rev-parse", "HEAD")


def _linear_source_history(tmp_path: Path) -> tuple[Path, str, str]:
    """建一段线性源码史，返回 (repo, 较早的 commit, HEAD)。"""
    repository = tmp_path / "source-history"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "RVF Test")
    _git(repository, "config", "user.email", "rvf@example.test")
    earlier_commit = _commit_source_change(repository, "a.txt", "feat: 第一段工作")
    _commit_source_change(repository, "b.txt", "feat: 第二段工作")
    head_commit = _commit_source_change(repository, "c.txt", "fix: 第三段工作")
    return repository, earlier_commit, head_commit


def test_source_commit_delta_reports_no_baseline_when_no_prior_installation_receipt(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    repository, _, head_commit = _linear_source_history(tmp_path)

    delta = runner.describe_source_commit_delta_since_last_installation(
        repository=repository,
        previously_installed_commit=None,
        newly_installed_commit=head_commit,
    )

    assert delta["kind"] == "no_baseline"
    assert delta["old_commit"] is None
    assert delta["new_commit"] == head_commit
    assert [commit["sha"] for commit in delta["forward_commits"]] == [head_commit]
    assert delta["reverse_commits"] == []
    assert "没有可用的上一次安装基线" in delta["boundary_explanation"]


def test_source_commit_delta_lists_forward_commits_since_previously_installed_commit(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    repository, earlier_commit, head_commit = _linear_source_history(tmp_path)

    delta = runner.describe_source_commit_delta_since_last_installation(
        repository=repository,
        previously_installed_commit=earlier_commit,
        newly_installed_commit=head_commit,
    )

    assert delta["kind"] == "forward"
    assert delta["old_commit"] == earlier_commit
    assert delta["reverse_commits"] == []
    # git log 是从新到旧，且区间不含 OLD 自己。
    assert [commit["subject"] for commit in delta["forward_commits"]] == [
        "fix: 第三段工作",
        "feat: 第二段工作",
    ]
    assert delta["forward_commits"][0]["sha"] == head_commit


def test_source_commit_delta_reports_unchanged_when_previously_installed_commit_matches(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    repository, _, head_commit = _linear_source_history(tmp_path)

    delta = runner.describe_source_commit_delta_since_last_installation(
        repository=repository,
        previously_installed_commit=head_commit,
        newly_installed_commit=head_commit,
    )

    assert delta["kind"] == "unchanged"
    assert delta["forward_commits"] == []
    assert delta["reverse_commits"] == []


def test_source_commit_delta_treats_abbreviated_previous_commit_as_unchanged(
    tmp_path: Path,
) -> None:
    """OLD 是 NEW 的缩写或大小写异体时，两端解析到同一个 commit 就必须归为 unchanged。

    回归 RVF-001：字符串相等判定会漏掉这些异体，落进 forward 分支产出空的
    forward_commits 与「前进了 0 个源 commit」这种自相矛盾的措辞。
    """
    runner = _load_runner()
    repository, _, head_commit = _linear_source_history(tmp_path)

    for previously_installed_commit in (
        head_commit[:7],
        head_commit[:12],
        head_commit.upper(),
    ):
        delta = runner.describe_source_commit_delta_since_last_installation(
            repository=repository,
            previously_installed_commit=previously_installed_commit,
            newly_installed_commit=head_commit,
        )

        assert delta["kind"] == "unchanged", previously_installed_commit
        assert delta["forward_commits"] == []
        assert delta["reverse_commits"] == []
        assert "没有变化" in delta["boundary_explanation"]
        assert "前进了" not in delta["boundary_explanation"]


def test_source_commit_delta_reports_rollback_or_diverged_with_both_directions(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    repository, shared_ancestor, previously_installed_commit = _linear_source_history(
        tmp_path
    )
    _git(repository, "checkout", "-b", "other", shared_ancestor)
    newly_installed_commit = _commit_source_change(
        repository, "d.txt", "feat: 另一条线上的工作"
    )

    delta = runner.describe_source_commit_delta_since_last_installation(
        repository=repository,
        previously_installed_commit=previously_installed_commit,
        newly_installed_commit=newly_installed_commit,
    )

    assert delta["kind"] == "rollback_or_diverged"
    assert [commit["subject"] for commit in delta["forward_commits"]] == [
        "feat: 另一条线上的工作"
    ]
    assert [commit["subject"] for commit in delta["reverse_commits"]] == [
        "fix: 第三段工作",
        "feat: 第二段工作",
    ]
    assert "回退或分叉" in delta["boundary_explanation"]


def test_source_commit_delta_reports_unknown_revision_when_previous_commit_absent_from_repository(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    repository, _, head_commit = _linear_source_history(tmp_path)

    delta = runner.describe_source_commit_delta_since_last_installation(
        repository=repository,
        previously_installed_commit="0" * 40,
        newly_installed_commit=head_commit,
    )

    assert delta["kind"] == "unknown_revision"
    assert delta["old_commit"] == "0" * 40
    assert delta["new_commit"] == head_commit
    assert delta["forward_commits"] == []
    assert delta["reverse_commits"] == []


def test_source_commit_delta_degrades_instead_of_raising_on_undecodable_commit_subject(
    tmp_path: Path,
) -> None:
    """非 UTF-8 的 commit subject 不得让一次已收敛的部署以异常收场。

    commit log 是**事后统计**：它跑在 contract check + installer + verifier 全绿、official
    installation receipt 已收敛之后，且在 `run_local_deploy` 的 `except DeployError` 回滚块
    之外。任何从这里逃逸的异常都会绕过 completed result receipt 的写入，把一次实际上装好并
    验证通过的部署伪装成失败。
    """
    runner = _load_runner()
    repository = tmp_path / "latin1-source-history"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "RVF Test")
    _git(repository, "config", "user.email", "rvf@example.test")
    # 只用 Latin-1 能表示的非 ASCII 字符，git 才会把它们真的转成非 UTF-8 字节；
    # CJK 之类无法转换的字符会让 git 原样吐 UTF-8，测试就退化成假绿。
    earlier_commit = _commit_source_change(repository, "a.txt", "feat: cafe base")
    head_commit = _commit_source_change(repository, "b.txt", "fix: café naïve tweak")
    # 提交信息本身按 UTF-8 存盘，但仓库要求 git 以 ISO-8859-1 输出 log。
    _git(repository, "config", "i18n.logOutputEncoding", "ISO-8859-1")

    # 先证明输入确实无法严格解码，否则下面的断言证明不了任何事。
    raw_log = subprocess.run(
        ["git", "-C", str(repository), "log", "--format=%s"],
        check=True,
        capture_output=True,
    ).stdout
    with pytest.raises(UnicodeDecodeError):
        raw_log.decode("utf-8")

    delta = runner.describe_source_commit_delta_since_last_installation(
        repository=repository,
        previously_installed_commit=earlier_commit,
        newly_installed_commit=head_commit,
    )

    # 没抛异常，而且区间没有因为解码退化被截断或吞掉。
    assert delta["kind"] == "forward"
    assert [commit["sha"] for commit in delta["forward_commits"]] == [head_commit]
    assert delta["reverse_commits"] == []
    # 坏字节被顶替成 U+FFFD，而不是把整条记录丢掉。
    degraded_subject = delta["forward_commits"][0]["subject"]
    assert degraded_subject.startswith("fix: caf")
    assert "\ufffd" in degraded_subject
    # 降级后的文本必须还能进 result receipt 的 JSON：surrogateescape 会在这里炸。
    json.dumps(delta)


def test_source_commit_delta_human_output_neutralizes_terminal_control_sequences(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """人读输出里的外部可控串不得把 ESC/OSC 原样送进终端。

    两个来源都不受本进程控制：commit `subject` 由任何能往本仓写提交的人构造，`old_commit`
    来自磁盘上的 official installation receipt。原样打印可以清屏盖掉前面的真实结论、伪造
    一行看着像真结论的输出——而人读输出正是开发者用来判断「到底装了什么」的那一路。

    同时钉住两条反向约束：
    1. 转义只在打印侧，结构化 delta（`--json` 回执与 receipt 的来源）必须仍是原始值；
    2. 容错解码留下的 `\ufffd` 是普通可打印字符，不得被中和函数当成控制序列吃掉——
       「坏字节」这件事本身要如实呈现给开发者。
    """
    runner = _load_runner()
    repository = tmp_path / "source-history-with-control-sequences"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "RVF Test")
    _git(repository, "config", "user.email", "rvf@example.test")
    baseline_commit = _commit_source_change(repository, "a.txt", "feat: 基线")
    hostile_subject = "feat: 正常标题\x1b[2J\x1b[H伪造的结论行\x07 caf\ufffd"
    head_commit = _commit_source_change(repository, "b.txt", hostile_subject)

    forward_delta = runner.describe_source_commit_delta_since_last_installation(
        repository=repository,
        previously_installed_commit=baseline_commit,
        newly_installed_commit=head_commit,
    )
    assert forward_delta["kind"] == "forward"
    # 结构化值保持原样：JSON 回执不因人读路径的转义而失真。
    assert forward_delta["forward_commits"][0]["subject"] == hostile_subject

    runner._print_source_commit_delta(forward_delta)
    forward_output = capsys.readouterr().out
    assert "\x1b" not in forward_output
    assert "\x07" not in forward_output
    assert "\\x1b[2J\\x1b[H伪造的结论行\\x07" in forward_output
    assert "feat: 正常标题" in forward_output
    # U+FFFD 与控制字符中和正交：`�` 必须原样留在人读输出里。
    assert "caf\ufffd" in forward_output

    hostile_previously_installed_commit = "0" * 20 + "\x1b]0;伪造的终端标题\x07"
    unknown_revision_delta = (
        runner.describe_source_commit_delta_since_last_installation(
            repository=repository,
            previously_installed_commit=hostile_previously_installed_commit,
            newly_installed_commit=head_commit,
        )
    )
    assert unknown_revision_delta["kind"] == "unknown_revision"
    assert unknown_revision_delta["old_commit"] == hostile_previously_installed_commit

    runner._print_source_commit_delta(unknown_revision_delta)
    unknown_revision_output = capsys.readouterr().out
    # OLD 在两处进人读路径：boundary_explanation 的内插值，以及「⚠️ 上次:」那一行。
    assert "\x1b" not in unknown_revision_output
    assert "\x07" not in unknown_revision_output
    assert unknown_revision_output.count("\\x1b]0;伪造的终端标题\\x07") == 2


def test_run_local_deploy_success_result_carries_source_commit_delta_from_installation_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功部署必须带上 commit 级 delta，且 OLD 来自 official receipt 而不是当前 HEAD。

    这同时是**反 HEAD 冒充 OLD** 的回归测试：一旦有人把 OLD 换成 `rev-parse HEAD`，
    `old_commit` 会等于 `new_commit`、`kind` 塌成 `unchanged`，这条立刻红。
    """
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
    local_main_runner = _seed_succeeding_official_cli_deploy_fixture(local_main_worktree)
    _git(local_main_worktree, "add", ".")
    _git(local_main_worktree, "commit", "-m", "test: seed deploy fixture")
    previously_installed_commit = _git(local_main_worktree, "rev-parse", "HEAD")
    head_commit = _commit_source_change(
        local_main_worktree, "shipped.txt", "feat: 本次要投入使用的工作"
    )

    # 预置「上一次安装」的 official receipt —— OLD 的唯一事实源。
    installation_receipt = (
        configured_user_local_data_root
        / "deployment-receipts/latest-official-cli-installation.json"
    )
    installation_receipt.parent.mkdir(parents=True, exist_ok=True)
    installation_receipt.write_text(
        json.dumps(
            {
                "status": "installed",
                "local_main_commit": previously_installed_commit,
                "generated_plugin_version": "0.1.0-dev.1+g"
                + previously_installed_commit[:12],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.run_local_deploy(
        repository_hint=local_main_worktree,
        home=tmp_path / "home",
        expected_local_main_commit="HEAD",
        runner_path=local_main_runner,
    )

    assert result["status"] == "ok"
    delta = result["source_commit_delta"]
    assert delta["kind"] == "forward"
    assert delta["old_commit"] == previously_installed_commit
    assert delta["new_commit"] == head_commit
    assert [commit["sha"] for commit in delta["forward_commits"]] == [head_commit]
    assert [commit["subject"] for commit in delta["forward_commits"]] == [
        "feat: 本次要投入使用的工作"
    ]
    assert delta["reverse_commits"] == []
