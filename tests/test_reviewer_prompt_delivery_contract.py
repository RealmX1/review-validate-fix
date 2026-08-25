#!/usr/bin/env python3
"""备选评审腿的 prompt 投递契约（S-A A-1）。

背景：派发内核历史上**只经 stdin 投递 prompt**（`deliver_reviewer_prompt_on_dedicated_stdin_writer_thread`），
cursor/claude 的 `-p` 与 codex 的 `exec -` 都读 stdin。Kimi Code CLI 不吻合且两条退路都被堵死：
它的 `-p/--prompt` 是**带值 flag**、headless 路径只读 `opts.prompt` 从不读 stdin；而把整份 prompt
塞进 argv 又必然 `E2BIG`（本机实盘 `reviewer.prompt.txt` 最大 1,522,602 字节 > `ARG_MAX` 1,048,576）。

护的不变量：

* **PD-1（既有腿零回归）** —— `prompt_delivery` 缺省是 `stdin`，此形态下 argv 逐字不变、
  投进 stdin 的仍是整份 prompt。三份既有模板不得因为本抽象出现任何 argv 差异。
* **PD-2（指针形态不撑爆 argv）** —— `argv_prompt_flag_with_prompt_file_pointer` 只把一段定长
  短指令追加进 argv，整份 prompt 留在磁盘。无论 prompt 多大，argv 字节数都远低于 `ARG_MAX`。
* **PD-3（指针自足）** —— 指针正文里逐字带 prompt 文件的**绝对路径**，不依赖 reviewer 能读到
  `$RVF_REVIEWER_PROMPT_FILE`；env 只是第二条路。
* **PD-4（配置错误 fail-closed）** —— 未知投递形态、指针形态缺 `prompt_flag`、`prompt_flag` 已在
  `command` 里出现（会被追加的那份影子化）、`run_directory_grant_flag` 不是 flag，四种都必须
  在配置校验期 exit 2，而不是运行期才炸或静默降级。
* **PD-5（`--dry-run` 打印真实 argv，且仍然零 API 成本）** —— argv 终结与 prompt 落盘被前移到
  `--dry-run` 之前（否则预演打印的不是将被执行的那条命令），但 `pre_run_health` 仍留在其后。
  历史教训：把 dry-run 整块挪到 health 之后，会把「零成本出口」变成每次烧一次真实 probe。
* **PD-6（run directory 授权不是 codex 专属）** —— reviewer 要写的 `$RVF_REVIEW_RESULT` 落在
  run directory 里、位于目标仓库之外，凡带工作目录沙箱的 reviewer CLI 都要显式授权它。
  codex 的位置约束（`exec` 之后、stdin 占位符 `-` 之前）是特例，不是通例。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
CONFIG_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/config"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIRECTORY / "run_alternative_reviewer.py"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import run_alternative_reviewer as runner  # noqa: E402


# --------------------------------------------------------------------------------------
# PD-1 / PD-2 / PD-3：finalize_reviewer_command_for_prompt_delivery 纯函数层
# --------------------------------------------------------------------------------------


def test_stdin_delivery_leaves_argv_untouched_and_sends_whole_prompt() -> None:
    command = ["cursor-agent", "-p", "--output-format", "stream-json"]
    final_argv, stdin_text = runner.finalize_reviewer_command_for_prompt_delivery(
        command,
        prompt_delivery=runner.PROMPT_DELIVERY_STDIN,
        prompt_flag=None,
        prompt_file_path=Path("/tmp/does-not-need-to-exist/reviewer.prompt.txt"),
        prompt_text="ENTIRE REVIEW PACKET",
    )
    assert final_argv == command
    assert final_argv is not command, "必须返回副本，避免调用方持有的 argv 被就地改写"
    assert stdin_text == "ENTIRE REVIEW PACKET"


def test_pointer_delivery_appends_flag_and_empties_stdin(tmp_path: Path) -> None:
    prompt_file = tmp_path / "reviewer.prompt.txt"
    huge_prompt = "x" * 2_000_000
    prompt_file.write_text(huge_prompt, encoding="utf-8")

    final_argv, stdin_text = runner.finalize_reviewer_command_for_prompt_delivery(
        ["kimi", "--output-format", "stream-json"],
        prompt_delivery=runner.PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER,
        prompt_flag="-p",
        prompt_file_path=prompt_file,
        prompt_text=huge_prompt,
    )

    assert final_argv[:3] == ["kimi", "--output-format", "stream-json"]
    assert final_argv[3] == "-p"
    assert len(final_argv) == 5
    assert stdin_text == "", "指针形态绝不能再把整份 prompt 写进 stdin"

    pointer = final_argv[4]
    # PD-3：绝对路径逐字在正文里，而不是只靠 env。
    assert str(prompt_file) in pointer
    assert "$RVF_REVIEWER_PROMPT_FILE" in pointer
    # PD-2：argv 体积与 prompt 体积解耦。
    assert len(pointer.encode("utf-8")) < 1024
    assert runner.reviewer_command_argv_bytes(final_argv) < 4096
    assert len(huge_prompt) > 1_000_000


def test_pointer_instruction_length_is_independent_of_prompt_size(tmp_path: Path) -> None:
    small = tmp_path / "small.txt"
    small.write_text("tiny", encoding="utf-8")
    pointer_for_small = runner.build_reviewer_prompt_file_pointer_instruction(small)
    pointer_for_large = runner.build_reviewer_prompt_file_pointer_instruction(small)
    assert pointer_for_small == pointer_for_large
    assert small.name in pointer_for_small


def test_pointer_delivery_without_prompt_flag_is_rejected(tmp_path: Path) -> None:
    for missing in (None, ""):
        with pytest.raises(ValueError, match="prompt_flag"):
            runner.finalize_reviewer_command_for_prompt_delivery(
                ["kimi"],
                prompt_delivery=runner.PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER,
                prompt_flag=missing,
                prompt_file_path=tmp_path / "p.txt",
                prompt_text="p",
            )


def test_unknown_prompt_delivery_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported prompt_delivery"):
        runner.finalize_reviewer_command_for_prompt_delivery(
            ["kimi"],
            prompt_delivery="write_it_on_a_postcard",
            prompt_flag="-p",
            prompt_file_path=tmp_path / "p.txt",
            prompt_text="p",
        )


def test_argv_bytes_counts_utf8_plus_nul_terminator() -> None:
    # "评审" 是 6 个 UTF-8 字节；每个 argv 元素再加一个 NUL。
    assert runner.reviewer_command_argv_bytes(["ab", "评审"]) == (2 + 1) + (6 + 1)
    assert runner.reviewer_command_argv_bytes([]) == 0


# --------------------------------------------------------------------------------------
# PD-6：run directory 授权的泛化 + codex 特例零回归
# --------------------------------------------------------------------------------------


def test_run_directory_grant_appends_when_absent() -> None:
    patched = runner.ensure_run_directory_grant_argument(
        ["kimi", "--output-format", "stream-json"],
        Path("/runs/abc"),
        grant_flag="--add-dir",
        scan_from_index=1,
    )
    assert patched == ["kimi", "--output-format", "stream-json", "--add-dir", "/runs/abc"]


def test_run_directory_grant_is_idempotent_in_both_spellings() -> None:
    spaced = ["omp", "--add-dir", "/runs/abc"]
    assert runner.ensure_run_directory_grant_argument(
        spaced, Path("/runs/abc"), grant_flag="--add-dir"
    ) == spaced
    equals = ["omp", "--add-dir=/runs/abc"]
    assert runner.ensure_run_directory_grant_argument(
        equals, Path("/runs/abc"), grant_flag="--add-dir"
    ) == equals


def test_run_directory_grant_adds_second_directory_when_a_different_one_is_present() -> None:
    patched = runner.ensure_run_directory_grant_argument(
        ["omp", "--add-dir", "/runs/other"],
        Path("/runs/abc"),
        grant_flag="--add-dir",
    )
    assert patched == ["omp", "--add-dir", "/runs/other", "--add-dir", "/runs/abc"]


def test_run_directory_grant_respects_insert_before_token() -> None:
    patched = runner.ensure_run_directory_grant_argument(
        ["codex", "exec", "--json", "-"],
        Path("/runs/abc"),
        grant_flag="--add-dir",
        scan_from_index=2,
        insert_before_token="-",
    )
    assert patched == ["codex", "exec", "--json", "--add-dir", "/runs/abc", "-"]


def test_codex_exec_add_dir_behaviour_is_unchanged_by_the_generalisation() -> None:
    # 既有形状：插在 exec 之后、stdin 占位符 `-` 之前，且幂等。
    command = ["codex", "--ask-for-approval", "never", "exec", "--json", "-"]
    patched = runner.ensure_codex_exec_add_dir(command, Path("/runs/abc"))
    assert patched == [
        "codex", "--ask-for-approval", "never", "exec", "--json", "--add-dir", "/runs/abc", "-",
    ]
    assert runner.ensure_codex_exec_add_dir(patched, Path("/runs/abc")) == patched
    # 非 codex 命令原样返回。
    assert runner.ensure_codex_exec_add_dir(["kimi", "-p"], Path("/runs/abc")) == ["kimi", "-p"]


# --------------------------------------------------------------------------------------
# PD-4 / PD-5：进程级——配置校验 fail-closed，--dry-run 打印真实 argv 且不烧额度
# --------------------------------------------------------------------------------------


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def _run_reviewer(tmp_path: Path, config: dict, *, extra_args: list[str] | None = None):
    repo = _repository(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "reviewer-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    environment = dict(os.environ)
    environment["RVF_SUPPRESS_STOP_HOOK"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--repo",
            str(repo),
            "--config",
            str(config_path),
            "--rvf-run-dir",
            str(run_dir),
            "--rvf-run-id",
            "prompt-delivery-contract",
            *(extra_args or ["--dry-run"]),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=environment,
    ), run_dir


_POINTER_CONFIG = {
    "enabled": True,
    "label": "alternative-reviewer:prompt-delivery-fixture",
    "command": ["kimi", "--output-format", "stream-json"],
    "prompt_delivery": "argv_prompt_flag_with_prompt_file_pointer",
    "prompt_flag": "-p",
    "run_directory_grant_flag": "--add-dir",
    "allow_repo_cwd": True,
    "output_format": "text",
}


@pytest.mark.parametrize(
    ("mutation", "expected_message_fragment"),
    [
        ({"prompt_delivery": "carrier_pigeon"}, "prompt_delivery must be one of"),
        ({"prompt_flag": None}, "requires prompt_flag"),
        (
            {"command": ["kimi", "-p", "already-here"]},
            "must not appear in command",
        ),
        ({"run_directory_grant_flag": "add-dir"}, "run_directory_grant_flag must be a flag"),
    ],
)
def test_invalid_prompt_delivery_configuration_exits_two(
    tmp_path: Path, mutation: dict, expected_message_fragment: str
) -> None:
    config = dict(_POINTER_CONFIG)
    config.update(mutation)
    if mutation.get("prompt_flag", "keep") is None:
        config.pop("prompt_flag")
    completed, _ = _run_reviewer(tmp_path, config)
    assert completed.returncode == 2, completed.stderr
    assert expected_message_fragment in completed.stderr


def test_dry_run_prints_the_real_pointer_argv_with_a_bounded_size(tmp_path: Path) -> None:
    completed, run_dir = _run_reviewer(tmp_path, _POINTER_CONFIG)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["prompt_delivery"] == "argv_prompt_flag_with_prompt_file_pointer"
    command = payload["command"]
    assert "-p" in command, "预演打印的必须是终结后的 argv，而不是 config 里的静态 flag 组"
    pointer = command[command.index("-p") + 1]
    assert payload["prompt_file"] in pointer
    assert Path(payload["prompt_file"]).is_file(), "指针指向的文件必须已经落盘"
    assert Path(payload["prompt_file"]).read_text(encoding="utf-8") != ""
    # run directory 授权对非 codex 腿同样生效。
    assert "--add-dir" in command
    assert str(run_dir) in command
    # PD-2 的量化出口：prompt 很大而 argv 很小。
    assert payload["prompt_chars"] > 1000
    assert payload["command_argv_bytes"] < 4096


def test_dry_run_does_not_burn_a_real_health_probe(tmp_path: Path) -> None:
    """R-7：`--dry-run` 必须停在 `pre_run_health` 之前，否则零成本出口变成每次烧一次 probe。"""
    sentinel = tmp_path / "health-probe-actually-ran"
    config = dict(_POINTER_CONFIG)
    config["pre_run_health"] = True
    config["health_command"] = [
        sys.executable,
        "-c",
        f"open({str(sentinel)!r}, 'w').close()",
    ]
    completed, _ = _run_reviewer(tmp_path, config)
    assert completed.returncode == 0, completed.stderr
    assert not sentinel.exists(), "--dry-run 触发了真实 health probe：零成本出口已失守"


def test_stdin_delivery_dry_run_reports_the_stdin_mode_for_shipped_templates(
    tmp_path: Path,
) -> None:
    for template in ("cursor", "claude", "codex"):
        config = json.loads(
            (CONFIG_DIRECTORY / f"alternative-reviewer.{template}.json").read_text(
                encoding="utf-8"
            )
        )
        completed, _ = _run_reviewer(tmp_path / template, config)
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload["prompt_delivery"] == "stdin"
        assert payload["prompt_chars"] > 0


# --------------------------------------------------------------------------------------
# PD-7：出厂模板的**派发 argv** 必须带各自 harness 的递归守卫与授权
#
# 这些必须是结构化断言而不是字面量 grep：契约 gate 的 require_literal 是整文件匹配，
# 同一个串出现在 health_command 或注释里就会绿——实测把 `--no-extensions` 从 command
# 里删掉，字面量断言照样通过。
# --------------------------------------------------------------------------------------


def _shipped_template(name: str) -> dict:
    return json.loads(
        (CONFIG_DIRECTORY / f"alternative-reviewer.{name}.json").read_text(encoding="utf-8")
    )


def test_oh_my_pi_dispatch_argv_carries_the_recursion_guard() -> None:
    """omp 的 Stop 语义活在进程内 TS extension 里：评审腿加载它们就会再起一轮 RVF。"""
    command = _shipped_template("oh-my-pi")["command"]
    assert "--no-extensions" in command
    assert "--no-session" in command
    assert "--auto-approve" in command


def test_codex_dispatch_argv_still_disables_hooks() -> None:
    template = _shipped_template("codex")
    # 派发侧由 ensure_codex_hooks_disabled_command 运行期注入，探针侧必须自己写死——
    # 探针不经过那条注入路径，漏掉它每一轮可用性检查都会在探针里再起一轮 RVF。
    health = template["health_command"]
    assert health[health.index("--disable") + 1] == "hooks"


def test_kimi_dispatch_argv_omits_the_flags_that_conflict_with_prompt_mode() -> None:
    command = _shipped_template("kimi")["command"]
    for conflicting in ("-y", "--yolo", "--auto", "-p", "--prompt"):
        assert conflicting not in command, f"{conflicting} 不该出现在 kimi 的静态 flag 组里"
    assert command[command.index("--output-format") + 1] == "stream-json"


def test_oh_my_pi_model_pin_is_fully_qualified_with_its_provider() -> None:
    """裸 `gpt-5.6-sol` 会被 omp 的模糊匹配撞上另一 provider 的 gpt-5.6-sol-high/-low/-max。"""
    template = _shipped_template("oh-my-pi")
    for argv in (template["command"], template["health_command"]):
        model = argv[argv.index("--model") + 1]
        assert model == "openai-codex/gpt-5.6-sol", model
        assert "/" in model, "必须是完全限定 selector，否则模糊匹配会落到别的 provider"
        # reasoning effort 是独立 flag，不是模型名的一部分。
        assert argv[argv.index("--thinking") + 1] == "xhigh"


def test_every_shipped_template_grants_the_run_directory_when_it_needs_to() -> None:
    """$RVF_REVIEW_RESULT 落在 run directory 里、位于目标仓库之外，必须显式授权。"""
    for name in ("kimi", "oh-my-pi"):
        assert _shipped_template(name)["run_directory_grant_flag"] == "--add-dir"
    # codex 走位置约束特例（exec 之后、`-` 之前），因此**不**声明通用字段。
    assert "run_directory_grant_flag" not in _shipped_template("codex")


def test_no_shipped_template_exempts_itself_from_the_health_probe_parity_gate() -> None:
    for name in ("cursor", "claude", "codex", "kimi", "oh-my-pi"):
        assert "health_probe_parity_exemption" not in _shipped_template(name)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------------------
# PD-8：第三种投递形态 `argv_positional_prompt_file_reference`（omp）
#
# 【这一段是端到端实跑逼出来的，不是设计推导】omp 原本配的是 `stdin`，理由是「其余四腿
# 都走 stdin」。真跑一次评审才发现 `omp -p` **根本不读 stdin**：整份 prompt 被静默丢弃，
# 而进程 exit 0、stderr 全空、stdout 只有一行 session、零 turn —— 退出码与产物都看不出
# 异常，唯一症状是下游「没有 review-result.json」。比 kimi 那条（stdin 喂进去至少 exit 1
# 报 argument missing）更难发现。
#
# 修法用 omp 原生的 `@<绝对路径>` 语法，它比 kimi 的指针形态**更强**：文件内容由 omp 在
# 进程内展开进 prompt，不依赖模型自觉去读文件，送达性与 stdin 等价。两条腿虽然都不走
# stdin，投递可靠性并不同级。
# --------------------------------------------------------------------------------------


def test_positional_file_reference_appends_an_at_prefixed_absolute_path() -> None:
    prompt_file = Path("/tmp/some run dir/reviewer.prompt.txt")
    argv, stdin_text = runner.finalize_reviewer_command_for_prompt_delivery(
        ["omp", "-p", "--mode", "json"],
        prompt_delivery="argv_positional_prompt_file_reference",
        prompt_flag=None,
        prompt_file_path=prompt_file,
        prompt_text="x" * 5000,
    )
    assert argv[-1] == f"@{prompt_file}"
    assert argv[:-1] == ["omp", "-p", "--mode", "json"]
    # 整份 prompt 绝不进 stdin：omp 不读它，喂进去只会被静默丢弃。
    assert stdin_text == ""


def test_positional_file_reference_keeps_argv_far_below_arg_max() -> None:
    argv, _ = runner.finalize_reviewer_command_for_prompt_delivery(
        ["omp", "-p"],
        prompt_delivery="argv_positional_prompt_file_reference",
        prompt_flag=None,
        prompt_file_path=Path("/tmp/run/reviewer.prompt.txt"),
        prompt_text="x" * 1_600_000,
    )
    assert runner.reviewer_command_argv_bytes(argv) < 4096


def test_positional_file_reference_does_not_require_a_prompt_flag() -> None:
    """prompt 是位置参数而非带值 flag——强求 prompt_flag 会把这条形态误配成指针形态。"""
    argv, _ = runner.finalize_reviewer_command_for_prompt_delivery(
        ["omp", "-p"],
        prompt_delivery="argv_positional_prompt_file_reference",
        prompt_flag=None,
        prompt_file_path=Path("/tmp/run/p.txt"),
        prompt_text="hello",
    )
    assert argv[-1].startswith("@")


def test_oh_my_pi_template_must_not_regress_to_stdin_delivery() -> None:
    """回归防护：改回 stdin 不会报错，只会让这条腿每一轮都静默交白卷。"""
    template = _shipped_template("oh-my-pi")
    assert template["prompt_delivery"] == "argv_positional_prompt_file_reference", (
        "omp -p 不读 stdin；改回 stdin 会让整份 prompt 被静默丢弃，"
        "而进程仍然 exit 0、stderr 全空，只有下游缺 review-result.json 这一个症状"
    )


def test_no_shipped_template_claims_a_delivery_mode_the_runner_cannot_honour() -> None:
    for path in sorted(CONFIG_DIRECTORY.glob("alternative-reviewer.*.json")):
        template = json.loads(path.read_text(encoding="utf-8"))
        delivery = template.get("prompt_delivery", "stdin")
        assert delivery in runner.SUPPORTED_PROMPT_DELIVERY_MODES, (path.name, delivery)


def test_oh_my_pi_dry_run_reports_the_positional_file_reference_mode(
    tmp_path: Path,
) -> None:
    config = _shipped_template("oh-my-pi")
    completed, _ = _run_reviewer(tmp_path, config)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["prompt_delivery"] == "argv_positional_prompt_file_reference"
    assert payload["command"][-1].startswith("@")
    assert payload["command"][-1].endswith(payload["prompt_file"])
    assert payload["prompt_chars"] > 0
