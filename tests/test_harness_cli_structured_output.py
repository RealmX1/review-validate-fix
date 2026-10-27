"""保留跨 CLI 结构化输出与投递的已验证行为。"""
import json
import pytest
from core.task_snapshot_review import harness_cli_adapters as runner


def test_every_shipped_configuration_loads_through_the_real_user_configuration_path():
    from pathlib import Path
    from core.task_snapshot_review.workflow import load_reviewer_configuration
    directory=Path(__file__).resolve().parents[1]/'plugins/review-validate-fix/skills/review-validate-fix/config'
    configurations=[load_reviewer_configuration(path) for path in directory.glob('alternative-reviewer.*.json')]
    assert len(configurations)==5
    assert all(config['command'] and config['label'].startswith('alternative-reviewer:') for config in configurations)


@pytest.mark.parametrize('delivery,command,flag',[
    ('stdin',['codex','exec','-'],None),
    ('argv_prompt_flag_with_prompt_file_pointer',['kimi'],'-p'),
    ('argv_positional_prompt_file_reference',['omp','-p'],None),
])
def test_large_role_inputs_use_the_shipped_delivery_modes_without_large_argv(tmp_path,delivery,command,flag):
    path=tmp_path/'role instructions.md'
    argv,stdin=runner.finalize_reviewer_command_for_prompt_delivery(command,prompt_delivery=delivery,prompt_flag=flag,prompt_file_path=path,prompt_text='x'*2_000_000)
    assert sum(len(value.encode()) for value in argv)<4096
    if delivery=='stdin':
        assert len(stdin)==2_000_000
    else:
        assert not stdin
        assert str(path) in argv[-1]

def _jsonl(*records: dict) -> str:
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"

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

def test_terminated_cleanly_supports_native_kimi_resume_event() -> None:
    output = _jsonl({"type": "session.resume_hint", "role": "meta", "content": "kimi -r session"})
    assert runner.structured_output_terminated_cleanly(output, runner.OUTPUT_FORMAT_KIMI_STREAM_JSON) is True


def test_terminated_cleanly_requires_success_for_codex_claude_and_cursor() -> None:
    codex_success = _jsonl({"type": "item.completed", "item": {}}, {"type": "turn.completed"})
    assert runner.structured_output_terminated_cleanly(codex_success, runner.OUTPUT_FORMAT_CODEX_JSON) is True
    assert runner.structured_output_terminated_cleanly(_jsonl({"type": "turn.failed"}), runner.OUTPUT_FORMAT_CODEX_JSON) is False
    for fmt in (runner.OUTPUT_FORMAT_CLAUDE_STREAM_JSON, runner.OUTPUT_FORMAT_CURSOR_STREAM_JSON):
        success = _jsonl({"type": "result", "subtype": "success", "is_error": False})
        failure = _jsonl({"type": "result", "subtype": "error", "is_error": True})
        assert runner.structured_output_terminated_cleanly(success, fmt) is True
        assert runner.structured_output_terminated_cleanly(failure, fmt) is False
    assert runner.structured_output_terminated_cleanly("whatever", runner.OUTPUT_FORMAT_TEXT) is None


def test_terminated_cleanly_rejects_omp_provider_error() -> None:
    output = _jsonl({"type": "turn_end", "message": {"stopReason": "error", "errorMessage": "usage limit exhausted"}})
    assert runner.structured_output_terminated_cleanly(output, runner.OUTPUT_FORMAT_OH_MY_PI_JSON) is False

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
