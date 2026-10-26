"""spawn 守卫的行为 gate + 三处副本的结构性同步 gate。

守卫存在的理由见
``core/hook_command_spawn_guard_against_missing_target_script.py`` 的模块 docstring：
``python3 <缺失脚本>`` 以**退出码 2 + 非空 stderr** 结束，而 Codex 与 Claude Code 都把
Stop 事件的这个组合硬编码成「把 stderr 当续跑指令喂给模型并继续」⇒ 无上界续跑。

本文件的 gate 分三层：
1. 危险组合的**前提**本身（``python3 <缺失脚本>`` 确实 exit 2 + stderr）；
2. 守卫消灭该组合的三条路径——目标缺失、目标不可读、检查通过后目标消失（TOCTOU）；
3. 命令串三处副本（bundled ``hooks.json`` / ``verify_local_deploy.py`` / installer）不漂移。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.hook_command_spawn_guard_against_missing_target_script import (  # noqa: E402
    AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO,
    HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME,
    HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME,
    _BREADCRUMB_DIRECTORY_SHELL_VARIABLE,
    hook_command_guarded_against_missing_target_script,
)

BUNDLED_HOOKS_JSON = ROOT / "plugins/review-validate-fix/hooks/hooks.json"
VERIFY_LOCAL_DEPLOY = (
    ROOT
    / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
)
INSTALLER = ROOT / "scripts/install_to_codex.py"

BUNDLED_HOOK_EVENT_SCRIPTS = {
    "Stop": "stop.py",
    "UserPromptSubmit": "user_prompt_submit.py",
}


def _run_guarded_command(
    command: str,
    *,
    rvf_user_local_data_root: Path,
    stdin_text: str = "{}\n",
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(rvf_user_local_data_root.parent),
        "RVF_USER_LOCAL_DATA_ROOT": str(rvf_user_local_data_root),
    }
    environment.update(extra_environment or {})
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _commands_for_event(payload: dict[str, object], event_name: str) -> list[str]:
    hooks = payload.get("hooks")
    assert isinstance(hooks, dict)
    groups = hooks.get(event_name)
    assert isinstance(groups, list)
    commands: list[str] = []
    for group in groups:
        assert isinstance(group, dict)
        entries = group.get("hooks")
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            command = entry.get("command")
            assert isinstance(command, str)
            commands.append(command)
    return commands


def test_missing_target_script_exits_zero_without_stdout_or_stderr(tmp_path: Path) -> None:
    """本 gate 是整条修复的核心：目标缺失时**绝不能**留下 exit 2 + stderr 的组合。"""
    rvf_root = tmp_path / "home/.rvf/user-local-data"
    rvf_root.mkdir(parents=True)
    missing_script = tmp_path / "gone/hooks/stop.py"
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(missing_script)),
        hook_event_name="Stop",
    )

    completed = _run_guarded_command(command, rvf_user_local_data_root=rvf_root)

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_missing_target_script_leaves_developer_only_breadcrumb(tmp_path: Path) -> None:
    rvf_root = tmp_path / "home/.rvf/user-local-data"
    rvf_root.mkdir(parents=True)
    missing_script = tmp_path / "gone/hooks/stop.py"
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(missing_script)),
        hook_event_name="Stop",
    )

    _run_guarded_command(command, rvf_user_local_data_root=rvf_root)

    breadcrumb = rvf_root / HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME
    assert breadcrumb.is_file()
    line = breadcrumb.read_text(encoding="utf-8").strip()
    assert "\tStop\t" in line
    assert str(missing_script) in line


def test_breadcrumb_falls_back_to_home_when_user_local_data_root_is_unset(
    tmp_path: Path,
) -> None:
    """``RVF_USER_LOCAL_DATA_ROOT`` 未注入时（bundled hooks.json 就是这种情形）仍要留痕。"""
    home = tmp_path / "home"
    home.mkdir()
    missing_script = tmp_path / "gone/hooks/post_tool_use.py"
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(missing_script)),
        hook_event_name="PostToolUse",
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", command],
        input="{}\n",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    breadcrumb = (
        home / ".rvf/user-local-data" / HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME
    )
    assert breadcrumb.is_file()
    assert "\tPostToolUse\t" in breadcrumb.read_text(encoding="utf-8")


def test_present_target_script_receives_stdin_and_environment_prefix(
    tmp_path: Path,
) -> None:
    rvf_root = tmp_path / "home/.rvf/user-local-data"
    rvf_root.mkdir(parents=True)
    script = tmp_path / "plugin/hooks/stop.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, os, sys\n"
        "sys.stdout.write(json.dumps({\n"
        "    'stdin': sys.stdin.read().strip(),\n"
        "    'injected': os.environ.get('CODEX_RVF_MODE'),\n"
        "}))\n",
        encoding="utf-8",
    )
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(script)),
        hook_event_name="Stop",
        environment_assignment_prefix="CODEX_RVF_MODE=fork",
    )

    completed = _run_guarded_command(
        command,
        rvf_user_local_data_root=rvf_root,
        stdin_text='{"session_id":"abc"}',
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "stdin": '{"session_id":"abc"}',
        "injected": "fork",
    }
    assert not (rvf_root / HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME).exists()


def test_deliberate_non_zero_exit_from_present_script_is_propagated(
    tmp_path: Path,
) -> None:
    """脚本自己的非零退出码必须原样透传，否则会掩盖真实故障。

    刻意用 **3** 而不是 2：2 是守卫按契约归一化掉的那一格
    （见 ``test_exit_status_two_is_deliberately_normalized_to_zero``），
    拿它当「透传」样本会把两条契约搅在一起。

    stderr 断言刻意用**包含式**：helper 用精简环境启动解释器，某些 macOS 环境会先往
    stderr 打 ``confstr`` / 临时目录一类警告，逐字相等会变成对宿主环境的隐式假设 ⇒ 假红。
    这条 gate 的意图只是「刻意写出的诊断信息没有被吞掉」。
    """
    rvf_root = tmp_path / "home/.rvf/user-local-data"
    rvf_root.mkdir(parents=True)
    script = tmp_path / "plugin/hooks/stop.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nsys.stderr.write('deliberate')\nsys.exit(3)\n", encoding="utf-8")
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(script)),
        hook_event_name="Stop",
    )

    completed = _run_guarded_command(command, rvf_user_local_data_root=rvf_root)

    assert completed.returncode == 3
    assert "deliberate" in completed.stderr


def test_exit_status_two_is_deliberately_normalized_to_zero(tmp_path: Path) -> None:
    """**契约**：退出码 2 被刻意归一化成 0，即使它来自目标脚本自己。

    这不是疏漏。RVF 的 hook 入口（三个 shim + ``stop_hook_router.py`` +
    ``rvf_user_prompt_submit.py``）每一条终止路径都是 ``return 0``，所以命令串里出现
    退出码 2 只可能来自 spawn 失败；而「2 + 非空 stderr」是唯一会无上界驱动 agent 的
    组合。用归一化换掉「先检查再执行」的原子性假设，是零信号损失的交易。
    """
    assert AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO == 2
    rvf_root = tmp_path / "home/.rvf/user-local-data"
    rvf_root.mkdir(parents=True)
    script = tmp_path / "plugin/hooks/stop.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\nsys.stderr.write('would-drive-the-agent')\nsys.exit(2)\n",
        encoding="utf-8",
    )
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(script)),
        hook_event_name="Stop",
    )

    completed = _run_guarded_command(command, rvf_user_local_data_root=rvf_root)

    assert completed.returncode == 0
    assert "would-drive-the-agent" in completed.stderr


def test_bare_interpreter_on_missing_script_still_produces_the_agent_driving_combination(
    tmp_path: Path,
) -> None:
    """钉住守卫存在的**前提**：不加守卫时 ``python3 <缺失脚本>`` 就是 2 + 非空 stderr。

    前提若被解释器版本改掉（例如改用别的退出码），守卫的形态要跟着重新论证，
    而不是继续照抄一个已经失效的常量。
    """
    missing_script = tmp_path / "gone/hooks/stop.py"

    completed = subprocess.run(
        [sys.executable, str(missing_script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO
    assert completed.stderr.strip() != ""


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root 无视权限位，构造不出「存在但不可读」",
)
def test_unreadable_target_script_never_leaves_the_agent_driving_combination(
    tmp_path: Path,
) -> None:
    """残留窗口 ①：``[ -f ]`` 为真但解释器 open 拿到 ``[Errno 13]``。"""
    rvf_root = tmp_path / "home/.rvf/user-local-data"
    rvf_root.mkdir(parents=True)
    script = tmp_path / "plugin/hooks/stop.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    script.chmod(0o000)
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(script)),
        hook_event_name="Stop",
    )

    try:
        completed = _run_guarded_command(command, rvf_user_local_data_root=rvf_root)
    finally:
        script.chmod(0o644)

    assert completed.returncode != AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO
    assert completed.returncode == 0
    assert "Permission denied" in completed.stderr


def test_target_deleted_between_existence_check_and_interpreter_open_is_neutralized(
    tmp_path: Path,
) -> None:
    """残留窗口 ②：TOCTOU。

    用一个抢在 PATH 前面的 ``python3`` 垫片把竞态变成**确定性**的：垫片先删掉目标脚本，
    再 exec 真解释器。于是 ``[ -f ]`` 已经通过、而解释器 open 时目标已消失——与「部署
    恰好在这几毫秒里换掉版本目录」逐字同构，且不依赖任何 sleep/重试。
    """
    rvf_root = tmp_path / "home/.rvf/user-local-data"
    rvf_root.mkdir(parents=True)
    script = tmp_path / "plugin/hooks/stop.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    shim_directory = tmp_path / "interpreter-shim"
    shim_directory.mkdir()
    shim = shim_directory / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        "# 在解释器真正 open 之前删掉目标，制造 [ -f ] 与 open 之间的差异。\n"
        'rm -f "$1"\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(script)),
        hook_event_name="Stop",
    )

    completed = _run_guarded_command(
        command,
        rvf_user_local_data_root=rvf_root,
        extra_environment={"PATH": f"{shim_directory}:/usr/bin:/bin"},
    )

    assert not script.exists(), "垫片没有真的制造出竞态"
    assert completed.returncode != AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO
    assert completed.returncode == 0
    assert "No such file or directory" in completed.stderr


def test_explicit_breadcrumb_directory_literal_wins_over_ambient_environment(
    tmp_path: Path,
) -> None:
    """RVF-004：面包屑根显式传入时，不得再受宿主 ambient 环境变量摆布。

    installer 烘焙的自定义根只作用于 ``python3`` 这一条命令；面包屑分支读 ambient
    变量，于是自定义部署的**唯一**诊断会掉进默认 ``$HOME/.rvf/user-local-data``。
    """
    resolved_root = tmp_path / "custom-user-local-data"
    ambient_root = tmp_path / "home/.rvf/user-local-data"
    ambient_root.mkdir(parents=True)
    missing_script = tmp_path / "gone/hooks/stop.py"
    command = hook_command_guarded_against_missing_target_script(
        target_script_shell_literal=shlex.quote(str(missing_script)),
        hook_event_name="Stop",
        hook_target_missing_breadcrumb_directory_shell_literal=shlex.quote(
            str(resolved_root)
        ),
    )

    completed = _run_guarded_command(command, rvf_user_local_data_root=ambient_root)

    assert completed.returncode == 0
    assert completed.stderr == ""
    breadcrumb = resolved_root / HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME
    assert breadcrumb.is_file()
    assert str(missing_script) in breadcrumb.read_text(encoding="utf-8")
    assert not (ambient_root / HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME).exists()


def test_guard_rejects_empty_inputs() -> None:
    for kwargs in (
        {"target_script_shell_literal": "", "hook_event_name": "Stop"},
        {"target_script_shell_literal": "'/x.py'", "hook_event_name": ""},
    ):
        try:
            hook_command_guarded_against_missing_target_script(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝空输入: {kwargs}")


def test_bundled_hooks_json_commands_are_generated_by_the_single_guard_source() -> None:
    """bundled ``hooks.json`` 是静态文件，只能靠 gate 钉住它与唯一真相源不漂移。"""
    payload = json.loads(BUNDLED_HOOKS_JSON.read_text(encoding="utf-8"))
    for event_name, script_name in BUNDLED_HOOK_EVENT_SCRIPTS.items():
        expected = hook_command_guarded_against_missing_target_script(
            target_script_shell_literal='"${CLAUDE_PLUGIN_ROOT}/hooks/%s"' % script_name,
            hook_event_name=event_name,
        )
        assert _commands_for_event(payload, event_name) == [expected], (
            f"{event_name} 的 bundled 命令串与守卫真相源不一致"
        )


def test_verify_local_deploy_keeps_its_shell_variable_name_copy_in_sync() -> None:
    source = VERIFY_LOCAL_DEPLOY.read_text(encoding="utf-8")
    assert (
        f'HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME = "{HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME}"'
        in source
    ), "verify_local_deploy.py 的 shell 变量名副本与 core 真相源漂移了"


def _bake_codex_hooks_json(
    tmp_path: Path,
    monkeypatch,
    *,
    user_local_data_root: Path | None = None,
) -> tuple[dict[str, object], object]:
    spec = importlib.util.spec_from_file_location(
        "installer_for_hook_command_spawn_guard_tests", INSTALLER
    )
    assert spec and spec.loader
    installer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = installer
    spec.loader.exec_module(installer)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    user_paths = installer.resolved_rvf_user_local_data_paths(
        environment=(
            {"RVF_USER_LOCAL_DATA_ROOT": str(user_local_data_root)}
            if user_local_data_root is not None
            else {}
        ),
        home_directory=tmp_path,
    )
    plugin_skill_dir = (
        tmp_path / "active/plugins/review-validate-fix/skills/review-validate-fix"
    )
    installer.configure_stop_hook(
        plugin_skill_dir, user_local_data_paths=user_paths
    )
    installer.configure_user_prompt_submit_hook(
        plugin_skill_dir, user_local_data_paths=user_paths
    )
    payload = json.loads((tmp_path / ".codex/hooks.json").read_text(encoding="utf-8"))
    return payload, user_paths


def _baked_command(payload: dict[str, object], event_name: str, script_name: str) -> str:
    commands = [
        command
        for command in _commands_for_event(payload, event_name)
        if script_name in command
    ]
    assert len(commands) == 1
    return commands[0]


def test_installer_bakes_guarded_commands_into_codex_hooks_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload, _ = _bake_codex_hooks_json(tmp_path, monkeypatch)

    for event_name, script_name in (
        ("Stop", "task_snapshot_review_hook.py"),
        ("UserPromptSubmit", "task_snapshot_review_hook.py"),
    ):
        command = _baked_command(payload, event_name, script_name)
        assert command.startswith(f"{HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME}=")
        assert f'if [ -f "${HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME}" ]; then' in command
        assert command.endswith("exit 0; fi")
        assert HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME in command


def test_installer_bakes_the_resolved_breadcrumb_root_instead_of_the_ambient_variable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """RVF-004 的 installer 侧：烘焙已解析的根，别把面包屑留给宿主 ambient 环境。"""
    user_local_data_root = tmp_path / "custom/user-local-data"
    payload, user_paths = _bake_codex_hooks_json(
        tmp_path, monkeypatch, user_local_data_root=user_local_data_root
    )
    assert user_paths.user_local_data_root == user_local_data_root

    for event_name, script_name in (
        ("Stop", "task_snapshot_review_hook.py"),
        ("UserPromptSubmit", "task_snapshot_review_hook.py"),
    ):
        command = _baked_command(payload, event_name, script_name)
        assert (
            f"{_BREADCRUMB_DIRECTORY_SHELL_VARIABLE}="
            f"{shlex.quote(str(user_local_data_root))};" in command
        )
        assert (
            f'{_BREADCRUMB_DIRECTORY_SHELL_VARIABLE}="$RVF_USER_LOCAL_DATA_ROOT"'
            not in command
        )


def test_installer_baked_command_leaves_the_breadcrumb_under_the_resolved_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """端到端：真跑一遍烘焙出来的命令串，面包屑必须落在自定义根、而非 ambient 根。

    ``plugin_skill_dir`` 在 tmp 下从未创建 ⇒ 目标脚本天然缺失，正是要诊断的那条路径。
    """
    user_local_data_root = tmp_path / "custom/user-local-data"
    ambient_root = tmp_path / "ambient/user-local-data"
    ambient_root.mkdir(parents=True)
    payload, _ = _bake_codex_hooks_json(
        tmp_path, monkeypatch, user_local_data_root=user_local_data_root
    )
    command = _baked_command(payload, "Stop", "task_snapshot_review_hook.py")

    completed = _run_guarded_command(command, rvf_user_local_data_root=ambient_root)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert (
        user_local_data_root / HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME
    ).is_file()
    assert not (ambient_root / HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME).exists()
