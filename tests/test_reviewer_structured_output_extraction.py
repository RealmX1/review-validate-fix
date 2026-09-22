#!/usr/bin/env python3
"""kimi / oh-my-pi 结构化 stdout 的提取与活性判据（S-A A-2）。

两家的错误面与既有三家都不同，且**两处判断在设计阶段是反的**，本文件逐条把它们钉住：

* **SX-1（kimi 没有 result 信封）** —— kimi 的 stream-json 里**不存在** ``type == "result"``
  终止事件，既有 `extract_result_event_text` 会整份回退成 raw stdout，把整条 JSONL 当成
  「评审正文」落进 ``reviewer.normalized.txt``。正确做法是取最后一条 ``role == "assistant"``
  且 content 非空的行。
* **SX-2（重试 ≠ 失败）** —— ``turn.step.retrying`` 按定义是**重试**事件，该 turn 仍可能成功。
  在「发生了一次 429」上就判死，会让这条腿连同它随后可能写出的合法 review-result 一起作废
  （`_leg_is_effective` 要求 returncode == 0，`check_review_result_artifact` 也只在
  returncode == 0 时才跑）。权威信号是 kimi 自报的 ``failed_attempt >= max_attempts``。
* **SX-3（omp 报错也退 0）** —— omp 把 ``errorMessage → stderr → exit(1)`` 整段包在
  ``if (mode === "text")`` 里，而评审腿用的是 ``--mode json`` ⇒ 只认非零退出码的文本兜底
  **永远不会触发**。额度错误必须在提取器里 typed 识别并 raise。
* **SX-4（omp 的 isError 不是 cursor spawnError 的对偶）** —— 普通非零 shell exit 与工具参数
  校验错误都会置起它。拿它累计判「工具层不可用」，会把三条正常失败的 shell 命令误报成
  runtime 坏掉，杀掉一条健康的评审腿。omp monitor **刻意不做**这件事。
* **SX-5（新格式必须进脱敏集合）** —— 结构化格式的 raw stdout 落工件前要按 JSON 行脱敏，
  漏登记则 ``env_set`` 的秘密值原样进 JSONL。这是本文件历史上最容易漏的一处。
* **SX-6（评审腿不得递归触发 RVF）** —— omp 的 Stop 语义活在进程内 TS extension 里，
  规整必须保证 ``--no-extensions``（等价 codex 的 ``--disable hooks``）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import run_alternative_reviewer as runner  # noqa: E402


def _jsonl(*records: dict) -> str:
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"


# --------------------------------------------------------------------------------------
# SX-1：kimi 提取器
# --------------------------------------------------------------------------------------


def test_kimi_extractor_returns_last_assistant_content_not_the_whole_stream() -> None:
    output = _jsonl(
        {"role": "meta", "type": "turn.start"},
        {"role": "assistant", "content": "先想一想", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "diff --git a/x b/x"},
        {"role": "assistant", "content": "kind: no_issues —— 已写入 review-result"},
        {"role": "meta", "type": "turn.end"},
    )
    extracted = runner.extract_kimi_stream_json_result(output)
    assert extracted == "kind: no_issues —— 已写入 review-result"
    assert "tool_call_id" not in extracted
    assert "turn.start" not in extracted


def test_kimi_extractor_surfaces_hook_blocked_assistant_line() -> None:
    output = _jsonl(
        {"role": "assistant", "content": "<Event> hook blocked\n\nStop hook 拒绝了本轮"},
    )
    assert "hook blocked" in runner.extract_kimi_stream_json_result(output)


def test_kimi_extractor_falls_back_to_raw_output_when_no_assistant_line() -> None:
    output = "not json at all\n"
    assert runner.extract_kimi_stream_json_result(output) == "not json at all"


def test_kimi_extractor_ignores_empty_assistant_content() -> None:
    output = _jsonl(
        {"role": "assistant", "content": "真正的结论"},
        {"role": "assistant", "content": "   "},
    )
    assert runner.extract_kimi_stream_json_result(output) == "真正的结论"


# --------------------------------------------------------------------------------------
# SX-2：重试 ≠ 失败
# --------------------------------------------------------------------------------------


def test_kimi_recoverable_retry_does_not_kill_the_leg() -> None:
    """429 但重试**未**耗尽 ⇒ 绝不 raise，否则可恢复重试被变成硬失败腿。"""
    output = _jsonl(
        {
            "role": "meta",
            "type": "turn.step.retrying",
            "status_code": 429,
            "error_message": "rate limit exceeded, retrying",
            "failed_attempt": 1,
            "max_attempts": 5,
        },
        {"role": "assistant", "content": "重试之后还是把评审做完了"},
    )
    assert runner.extract_kimi_stream_json_result(output) == "重试之后还是把评审做完了"


def test_kimi_exhausted_retries_raise_usage_limit() -> None:
    output = _jsonl(
        {
            "role": "meta",
            "type": "turn.step.retrying",
            "status_code": 429,
            "error_message": "usage limit reached for your plan",
            "failed_attempt": 5,
            "max_attempts": 5,
        },
    )
    with pytest.raises(runner.ReviewerStructuredOutputError) as excinfo:
        runner.extract_kimi_stream_json_result(output)
    assert excinfo.value.reason_code == "usage_limit_exhausted"
    assert runner.EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG in str(excinfo.value)


def test_kimi_non_usage_limit_exhaustion_is_not_reported_as_usage_limit() -> None:
    output = _jsonl(
        {
            "role": "meta",
            "type": "turn.step.retrying",
            "status_code": 500,
            "error_message": "internal server error",
            "failed_attempt": 5,
            "max_attempts": 5,
        },
        {"role": "assistant", "content": "部分结果"},
    )
    assert runner.extract_kimi_stream_json_result(output) == "部分结果"


def test_kimi_retry_event_without_attempt_counters_is_not_treated_as_exhausted() -> None:
    assert not runner.kimi_retry_event_exhausted_provider_usage(
        {"type": "turn.step.retrying", "status_code": 429, "error_message": "quota"}
    )


# --------------------------------------------------------------------------------------
# SX-3：omp 提取器
# --------------------------------------------------------------------------------------


def test_oh_my_pi_extractor_returns_last_assistant_text() -> None:
    output = _jsonl(
        {"type": "session_init", "id": "s1"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "第一轮"}]},
        },
        {"type": "tool_execution_end", "isError": False},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "kind: issues —— 报告已写入"}],
            },
        },
        {"type": "turn_end", "message": {"role": "assistant", "content": []}},
    )
    assert runner.extract_oh_my_pi_json_result(output) == "kind: issues —— 报告已写入"


def test_oh_my_pi_extractor_falls_back_to_agent_end_messages() -> None:
    output = _jsonl(
        {"type": "session_init"},
        {
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": [{"type": "text", "text": "兜底正文"}]}],
        },
    )
    assert runner.extract_oh_my_pi_json_result(output) == "兜底正文"


def test_oh_my_pi_usage_limit_raises_even_though_the_process_would_exit_zero() -> None:
    """SX-3 的核心：omp 用 --mode json 时报错也退 0，非零退出码兜底永远不触发。"""
    output = _jsonl(
        {"type": "session_init"},
        {
            "type": "turn_end",
            "stopReason": "error",
            "errorMessage": "You have reached your usage limit for this plan.",
        },
    )
    with pytest.raises(runner.ReviewerStructuredOutputError) as excinfo:
        runner.extract_oh_my_pi_json_result(output)
    assert excinfo.value.reason_code == "usage_limit_exhausted"


def test_oh_my_pi_non_usage_error_does_not_raise() -> None:
    """非额度类错误不在提取器判死：交由 review-result 校验去定性，与 codex 先例一致。"""
    output = _jsonl(
        {"type": "session_init"},
        {"type": "turn_end", "stopReason": "error", "errorMessage": "tool crashed"},
    )
    extracted = runner.extract_oh_my_pi_json_result(output)
    # 不 raise 是本条的全部要求；没有 assistant 正文时按契约回退 raw stdout。
    assert "tool crashed" in extracted


def test_oh_my_pi_error_text_only_reads_records_whose_stop_reason_is_error() -> None:
    assert runner.oh_my_pi_record_error_text({"type": "turn_end"}) is None
    assert (
        runner.oh_my_pi_record_error_text(
            {"type": "turn_end", "stopReason": "stop", "errorMessage": "ignored"}
        )
        is None
    )
    assert (
        runner.oh_my_pi_record_error_text(
            {"type": "message_end", "message": {"stopReason": "error", "errorMessage": "boom"}}
        )
        == "boom"
    )


# --------------------------------------------------------------------------------------
# 收尾判据
# --------------------------------------------------------------------------------------


def test_terminated_cleanly_tracks_kimi_resume_hint() -> None:
    with_hint = _jsonl({"role": "meta", "session": {"resume_hint": "kimi -S session_x"}})
    without_hint = _jsonl({"role": "assistant", "content": "结论"})
    fmt = runner.OUTPUT_FORMAT_KIMI_STREAM_JSON
    assert runner.structured_output_terminated_cleanly(with_hint, fmt) is True
    assert runner.structured_output_terminated_cleanly(without_hint, fmt) is False


def test_terminated_cleanly_tracks_oh_my_pi_terminal_event() -> None:
    fmt = runner.OUTPUT_FORMAT_OH_MY_PI_JSON
    assert runner.structured_output_terminated_cleanly(_jsonl({"type": "turn_end"}), fmt) is True
    assert (
        runner.structured_output_terminated_cleanly(_jsonl({"type": "message_start"}), fmt) is False
    )


def test_terminated_cleanly_is_not_applicable_to_the_older_formats() -> None:
    for fmt in (
        runner.OUTPUT_FORMAT_TEXT,
        runner.OUTPUT_FORMAT_CLAUDE_STREAM_JSON,
        runner.OUTPUT_FORMAT_CODEX_JSON,
        runner.OUTPUT_FORMAT_CURSOR_STREAM_JSON,
    ):
        assert runner.structured_output_terminated_cleanly("whatever", fmt) is None


# --------------------------------------------------------------------------------------
# SX-2 / SX-4：两个 monitor
# --------------------------------------------------------------------------------------


def test_kimi_monitor_stays_quiet_on_a_recoverable_retry() -> None:
    monitor = runner.KimiStreamActivityMonitor()
    monitor.ingest(
        _jsonl(
            {
                "role": "meta",
                "type": "turn.step.retrying",
                "status_code": 429,
                "failed_attempt": 1,
                "max_attempts": 5,
            }
        )
    )
    assert monitor.provider_usage_limit_signalled is False
    assert monitor.early_fail_over_reason_code is None


def test_kimi_monitor_trips_only_when_retries_are_exhausted() -> None:
    monitor = runner.KimiStreamActivityMonitor()
    monitor.ingest(
        _jsonl(
            {
                "role": "meta",
                "type": "turn.step.retrying",
                "status_code": 429,
                "failed_attempt": 5,
                "max_attempts": 5,
            }
        )
    )
    assert monitor.provider_usage_limit_signalled is True
    assert monitor.early_fail_over_reason_code == "kimi_provider_usage_limit_exhausted"


def test_kimi_monitor_never_suppresses_idle_timeout_or_claims_tool_layer_failure() -> None:
    monitor = runner.KimiStreamActivityMonitor()
    monitor.ingest(_jsonl({"role": "tool", "tool_call_id": "t1", "content": "anything"}))
    assert monitor.waiting_on_long_command is False
    assert monitor.tool_layer_unavailable is False


def test_oh_my_pi_monitor_does_not_accumulate_is_error() -> None:
    """SX-4：三条普通失败的 shell 命令**不得**被判成工具层坏掉。"""
    monitor = runner.OhMyPiJsonStreamActivityMonitor()
    monitor.ingest(
        _jsonl(
            *[{"type": "tool_execution_end", "isError": True, "name": "bash"} for _ in range(5)]
        )
    )
    assert monitor.tool_layer_unavailable is False
    assert monitor.early_fail_over_reason_code is None


def test_oh_my_pi_monitor_trips_on_terminal_usage_limit() -> None:
    monitor = runner.OhMyPiJsonStreamActivityMonitor()
    monitor.ingest(
        _jsonl(
            {
                "type": "turn_end",
                "stopReason": "error",
                "errorMessage": "usage limit reached for alpha plan",
            }
        )
    )
    assert monitor.early_fail_over_reason_code == "oh_my_pi_provider_usage_limit_exhausted"


def test_cursor_fail_over_reason_code_is_unchanged() -> None:
    """既有 reason 字面量是 dispatch 侧的识别键，泛化 fail-over 分支不得改动它。"""
    monitor = runner.CursorStreamActivityMonitor(tool_failure_threshold=1)
    assert monitor.early_fail_over_reason_code is None
    monitor.tool_runtime_failures = 1
    assert monitor.early_fail_over_reason_code == "cursor_tool_layer_unavailable"


def test_every_monitor_exposes_the_same_three_members() -> None:
    monitors = [
        runner.ClaudeStreamActivityMonitor(),
        runner.CursorStreamActivityMonitor(tool_failure_threshold=3),
        runner.KimiStreamActivityMonitor(),
        runner.OhMyPiJsonStreamActivityMonitor(),
    ]
    for monitor in monitors:
        assert isinstance(monitor.waiting_on_long_command, bool)
        assert isinstance(monitor.tool_layer_unavailable, bool)
        assert monitor.early_fail_over_reason_code is None or isinstance(
            monitor.early_fail_over_reason_code, str
        )
        monitor.ingest("{}\n")


# --------------------------------------------------------------------------------------
# SX-5 / SX-6：格式登记与命令规整
# --------------------------------------------------------------------------------------


def test_new_formats_are_registered_everywhere_they_must_be() -> None:
    for fmt in (runner.OUTPUT_FORMAT_KIMI_STREAM_JSON, runner.OUTPUT_FORMAT_OH_MY_PI_JSON):
        assert fmt in runner.SUPPORTED_OUTPUT_FORMATS
        # 漏这一处 ⇒ env_set 的秘密值不脱敏就落进 reviewer.stdout.txt。
        assert fmt in runner.STRUCTURED_JSON_LINES_OUTPUT_FORMATS
        assert fmt in runner.STRUCTURED_OUTPUT_RESULT_EXTRACTORS


def test_new_format_raw_stdout_is_redacted_line_by_line() -> None:
    secret = "sk-super-secret-value"
    raw = _jsonl({"role": "assistant", "content": f"token={secret}"})
    redacted = runner.redact_configured_environment_variable_values_in_json_lines(
        raw, {"SOME_TOKEN": secret}
    )
    assert secret not in redacted


def test_kimi_command_normalisation_never_adds_the_mutually_exclusive_auto_flags() -> None:
    patched = runner.ensure_kimi_stream_json_command(["kimi"])
    assert patched == ["kimi", "--output-format", "stream-json"]
    assert "--yolo" not in patched and "-y" not in patched and "--auto" not in patched
    # 幂等，且 text 会被强制改成 stream-json。
    assert runner.ensure_kimi_stream_json_command(patched) == patched
    assert runner.ensure_kimi_stream_json_command(["kimi", "--output-format", "text"]) == [
        "kimi",
        "--output-format",
        "stream-json",
    ]
    assert runner.ensure_kimi_stream_json_command(["kimi", "--output-format=text"]) == [
        "kimi",
        "--output-format=stream-json",
    ]


def test_oh_my_pi_command_normalisation_forces_print_json_and_no_extensions() -> None:
    patched = runner.ensure_oh_my_pi_json_command(["omp"])
    assert "-p" in patched
    assert patched[patched.index("--mode") + 1] == "json"
    # SX-6：评审腿不得加载 omp 的 TS extension，否则会在腿里再起一轮 RVF。
    assert "--no-extensions" in patched
    assert runner.ensure_oh_my_pi_json_command(patched) == patched
    assert "--mode=json" in runner.ensure_oh_my_pi_json_command(["omp", "-p", "--mode=text"])


def test_command_recognition_does_not_depend_on_the_runtime_prompt_flag() -> None:
    # kimi 的 -p 由 runner 在运行期追加，识别不能依赖它。
    assert runner.is_kimi_prompt_command(["kimi", "--output-format", "stream-json"])
    assert not runner.is_kimi_prompt_command(["claude", "-p"])
    assert runner.is_oh_my_pi_print_command(["omp", "-p", "--mode", "json"])
    assert runner.is_oh_my_pi_print_command(["omp", "--mode=json"])
    assert not runner.is_oh_my_pi_print_command(["omp"])
    assert not runner.is_oh_my_pi_print_command(["cursor-agent", "-p"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
