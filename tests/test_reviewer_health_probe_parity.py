#!/usr/bin/env python3
"""健康探针必须与派发命令走同一条后端路径（S-A A-3 / 计划 Q3 选项 A）。

这条不变量是用一次真实故障买来的。2026-08-11 的 run ``rvf-20260811T170157Z``：cursor 登录
已过期，两条命令给出相反结论——``cursor-agent status`` 退 0（正文里写着
``unable to fetch user details``），``cursor-agent -p ...`` 退 1 ``Authentication required``。
probe 只看前者，把 cursor 判为可用并选进两腿；那一腿派出去立刻失败、不产出 review-result，
**本轮实际只剩单路评审结论，而 RVF 全程认为自己做了双评审。**

在此之前这条纪律只以 cursor 模板里的一段注释存在，claude 与 codex 两份模板则原样带着
``claude auth status`` / ``codex login status`` —— 同一个失效形状，只是还没实盘发作。

护的不变量：

* **HP-1（同一可执行名 ⇒ 必须同一组 flag）** —— 旁路子命令（``auth status`` / ``login status``
  / ``doctor config``）一律 exit 2。少一个 flag 也算换了一道门：cursor 曾试过省掉
  ``--force --trust --sandbox disabled``，实测直接撞上 Workspace Trust gate，把**健康的**
  cursor 判成不可用。
* **HP-2（探针必须真的带 prompt）** —— 一条只有 flag、不发起任何调用的 "探针" 必须被拒。
  历史上这条差点被自己的实现漏掉：末尾 token 一旦被误当成位置参数 prompt，而同源规整又把
  被"借走"的 flag 值补了回来，两边多重集会恰好相等 ⇒ 静默通过。
* **HP-3（prompt 必须走位置参数）** —— ``run_health`` 调 ``subprocess.run`` 时既不传 ``input=``
  也不设 ``stdin=``。裸 ``-p``（claude）与 ``exec -``（codex）都是读 stdin 的形态，照搬会让探针
  阻塞到 ``health_timeout_seconds`` 超时，把健康的 harness 判成不可用。
* **HP-4（运行期注入不参与比对）** —— run-directory 授权与 prompt 终结是派发独有的运行期注入，
  拿它们比对只会得到「修对了也红」。gate 必须跑在这两步之前。
* **HP-5（外部探针不归本 gate 管，但必须可见）** —— 运维显式把探针指向自建网关（不同可执行名）
  是一次显式决策，不是"没人审视过的默认值"这个失效模式；放行，但在 ledger 里记明关系。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/review-validate-fix/skills/review-validate-fix"
SCRIPT_DIRECTORY = SKILL / "scripts"
CONFIG_DIRECTORY = SKILL / "config"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIRECTORY / "run_alternative_reviewer.py"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import run_alternative_reviewer as runner  # noqa: E402

SHIPPED_TEMPLATES = ("cursor", "claude", "codex", "kimi", "oh-my-pi")


def _gate(command, health_command, *, prompt_flag=None, run_directory_grant_flag=None):
    return runner.assert_health_command_shares_dispatch_flag_set(
        command,
        health_command,
        prompt_flag=prompt_flag,
        run_directory_grant_flag=run_directory_grant_flag,
    )


# --------------------------------------------------------------------------------------
# HP-1 / HP-2 / HP-3：纯函数层
# --------------------------------------------------------------------------------------


def test_matching_flag_sets_pass() -> None:
    command = ["cursor-agent", "-p", "--output-format", "stream-json", "--trust"]
    health = [*command, "Reply with exactly: OK"]
    assert _gate(command, health) == runner.HEALTH_PROBE_SHARES_DISPATCH_ENTRYPOINT


def test_bypass_subcommand_is_rejected() -> None:
    for command, health in (
        (["cursor-agent", "-p", "--output-format", "stream-json"], ["cursor-agent", "status"]),
        (["claude", "-p", "--output-format", "stream-json"], ["claude", "auth", "status"]),
        (["codex", "exec", "--json", "-"], ["codex", "login", "status"]),
        (["kimi", "--output-format", "stream-json"], ["kimi", "doctor", "config"]),
    ):
        with pytest.raises(ValueError, match="没有走同一条后端路径"):
            _gate(command, health)


def test_a_single_missing_flag_is_rejected() -> None:
    command = ["cursor-agent", "-p", "--output-format", "stream-json", "--force", "--trust"]
    health = ["cursor-agent", "-p", "--output-format", "stream-json", "--force", "Reply: OK"]
    with pytest.raises(ValueError, match="--trust"):
        _gate(command, health)


def test_a_changed_flag_value_is_rejected() -> None:
    command = ["omp", "-p", "--thinking", "xhigh"]
    health = ["omp", "-p", "--thinking", "low", "Reply: OK"]
    with pytest.raises(ValueError, match="没有走同一条后端路径"):
        _gate(command, health)


def test_probe_without_any_prompt_is_rejected() -> None:
    """HP-2：末尾 token 是派发签名里已有的值时，绝不能被当成 prompt。"""
    command = ["kimi", "--output-format", "stream-json"]
    health = ["kimi", "--output-format", "stream-json"]
    with pytest.raises(ValueError, match="缺少 prompt"):
        _gate(command, health, prompt_flag="-p")


def test_stdin_placeholder_is_not_a_prompt() -> None:
    """HP-3：`-` 是 stdin 占位符，而 run_health 不喂 stdin。"""
    command = ["codex", "exec", "--json", "-"]
    health = ["codex", "exec", "--json", "-"]
    with pytest.raises(ValueError, match="缺少 prompt"):
        _gate(command, health)


def test_prompt_carried_by_prompt_flag_is_accepted() -> None:
    command = ["kimi", "--output-format", "stream-json"]
    health = ["kimi", "--output-format", "stream-json", "-p", "Reply with exactly: OK"]
    assert _gate(command, health, prompt_flag="-p") == (
        runner.HEALTH_PROBE_SHARES_DISPATCH_ENTRYPOINT
    )


# --------------------------------------------------------------------------------------
# HP-4 / HP-5
# --------------------------------------------------------------------------------------


def test_runtime_injected_run_directory_grant_does_not_make_a_correct_pair_red() -> None:
    """HP-4：run-dir 授权只出现在派发侧，拿它比对就是「修对了也红」。"""
    command = ["omp", "-p", "--mode", "json", "--add-dir", "/runs/abc"]
    health = ["omp", "-p", "--mode", "json", "Reply: OK"]
    assert _gate(command, health, run_directory_grant_flag="--add-dir") == (
        runner.HEALTH_PROBE_SHARES_DISPATCH_ENTRYPOINT
    )
    equals_form = ["omp", "-p", "--mode", "json", "--add-dir=/runs/abc"]
    assert _gate(equals_form, health, run_directory_grant_flag="--add-dir") == (
        runner.HEALTH_PROBE_SHARES_DISPATCH_ENTRYPOINT
    )


def test_flag_order_does_not_matter() -> None:
    command = ["omp", "-p", "--mode", "json", "--no-session"]
    health = ["omp", "--no-session", "--mode", "json", "-p", "Reply: OK"]
    assert _gate(command, health) == runner.HEALTH_PROBE_SHARES_DISPATCH_ENTRYPOINT


def test_external_probe_executable_is_reported_not_rejected() -> None:
    """HP-5：运维显式指向自建网关的探针放行，但关系必须被回报出来。"""
    command = ["claude", "-p", "--output-format", "stream-json"]
    health = ["/usr/bin/curl", "--fail", "--silent", "http://127.0.0.1:8080/health"]
    assert _gate(command, health) == runner.HEALTH_PROBE_USES_EXTERNAL_EXECUTABLE


def test_empty_commands_are_rejected() -> None:
    with pytest.raises(ValueError):
        _gate([], ["claude", "-p", "x"])
    with pytest.raises(ValueError):
        _gate(["claude", "-p"], [])


# --------------------------------------------------------------------------------------
# 同源规整：探针与派发共用同一条规整函数
# --------------------------------------------------------------------------------------


def test_health_command_is_normalised_from_the_same_source_as_dispatch() -> None:
    """运维不需要在两处各抄一遍自动补齐的 flag —— 抄漏一个就换一道门。"""
    health = runner.normalise_health_command_for_output_format(
        ["claude", "-p", "--output-format", "stream-json", "Reply: OK"],
        runner.OUTPUT_FORMAT_CLAUDE_STREAM_JSON,
        prompt_flag=None,
    )
    assert "--verbose" in health and "--disable-slash-commands" in health
    assert health[-1] == "Reply: OK", "位置参数 prompt 必须仍在末尾，不能被补齐的 flag 挤到中间"
    assert "-" not in health, "探针不留 stdin 占位符：run_health 不喂 stdin"


# --------------------------------------------------------------------------------------
# 进程级：五份出厂模板必须全部通过，且每一条负向变异必须 exit 2
# --------------------------------------------------------------------------------------


def _check(config_payload: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    config_path = tmp_path / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    isolated_home = tmp_path / "home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    if config_payload.get("reviewer_model_selection_source"):
        executable = tmp_path / "bin" / "omp"
        executable.parent.mkdir()
        executable.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            "assert sys.argv[1:] == ['config', 'get', 'modelRoles', '--json'], 'check 不应调用模型'\n"
            "print(json.dumps({'key': 'modelRoles', 'value': {'default': 'openai-codex/gpt-6.1-sol:xhigh'}}))\n"
        )
        executable.chmod(0o755)
        config_payload["command"][0] = str(executable)
        config_payload["health_command"][0] = str(executable)
        config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    environment = dict(os.environ)
    # user-local override 按 Path.home() 解析：不隔离就测不到出厂模板本身。
    environment["HOME"] = str(isolated_home)
    environment["RVF_SUPPRESS_STOP_HOOK"] = "1"
    return subprocess.run(
        [sys.executable, str(RUN_ALTERNATIVE_REVIEWER), "--config", str(config_path), "--check"],
        capture_output=True,
        text=True,
        env=environment,
    )


def _template(name: str) -> dict:
    return json.loads(
        (CONFIG_DIRECTORY / f"alternative-reviewer.{name}.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("name", SHIPPED_TEMPLATES)
def test_every_shipped_template_passes_the_parity_gate(name: str, tmp_path: Path) -> None:
    completed = _check(_template(name), tmp_path / name)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("name", SHIPPED_TEMPLATES)
def test_every_shipped_template_declares_a_health_command(name: str) -> None:
    template = _template(name)
    assert template.get("pre_run_health") is True
    assert template.get("health_command"), "缺 health_command 等于没有可用性判据"
    # 出厂模板一律自证：探针与派发同一个可执行名。
    assert Path(template["health_command"][0]).name == Path(template["command"][0]).name


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("cursor", lambda h: h.remove("--trust")),
        ("claude", lambda h: h.__setitem__(slice(0, len(h)), ["claude", "auth", "status"])),
        ("codex", lambda h: h.__setitem__(slice(0, len(h)), ["codex", "login", "status"])),
        ("kimi", lambda h: h.__setitem__(slice(0, len(h)), ["kimi", "doctor", "config"])),
        ("oh-my-pi", lambda h: h.__setitem__(h.index("--thinking") + 1, "low")),
        ("codex", lambda h: h.__setitem__(-1, "-")),
    ],
)
def test_each_divergence_makes_the_template_exit_two(
    name: str, mutation, tmp_path: Path
) -> None:
    template = _template(name)
    mutation(template["health_command"])
    completed = _check(template, tmp_path / f"{name}-mutated")
    assert completed.returncode == 2, (
        f"{name} 的健康探针已经与派发命令分道扬镳，却仍被判为配置有效：{completed.stdout}"
    )
    assert "health_command" in completed.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
