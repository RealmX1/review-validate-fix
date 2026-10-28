"""只在可信原生错误或非零退出的 stderr 中分类故障。"""
from core.task_snapshot_review.role_failure_recovery import classify_role_failure


def test_model_body_quota_words_are_not_availability_facts():
    body = '{"status":"completed","audit_summary":"检查 quota exceeded 的错误分支","findings":[]}'
    assert classify_role_failure(body, "", 0, "text") is None
    assert classify_role_failure(body, "普通错误", 7, "text")["category"] == "transient"


def test_native_authentication_error_is_classified_even_when_cli_exits_zero():
    error = '{"type":"result","is_error":true,"error":"authentication_failed","result":"Invalid API key"}'
    result = classify_role_failure(error, "", 0, "claude_stream_json")
    assert result["category"] == "authentication"
    assert result["retry_not_before_seconds"] == 300


def test_native_expired_oauth_session_waits_for_credentials_instead_of_transient_retry():
    error = '{"type":"result","is_error":true,"result":"Failed to authenticate: OAuth session expired and could not be refreshed"}'
    result = classify_role_failure(error, "", 0, "claude_stream_json")
    assert result["category"] == "authentication"
    assert result["maximum_same_configuration_retries"] == 1
    assert result["retry_not_before_seconds"] == 300


def test_stderr_quota_failure_waits_for_bounded_recheck():
    assert classify_role_failure("", "quota exceeded", 1, "text")["category"] == "usage_limit"


def test_invalid_configuration_does_not_retry_unchanged_configuration():
    assert classify_role_failure("", "unknown option --broken", 2, "text")["category"] == "configuration"


def test_native_usage_code_wins_over_ordinary_result_words():
    assert classify_role_failure('{"type":"turn.failed","error":{"code":429,"message":"try later"}}', "", 0, "codex_json")["category"] == "usage_limit"


def test_supported_kimi_and_omp_terminal_errors_enter_usage_cooldown():
    for output, format_name in [
        ('{"role":"meta","type":"turn.step.retrying","status_code":429,"error_message":"quota exceeded","failed_attempt":3,"max_attempts":3}', "kimi_stream_json"),
        ('{"type":"message_end","message":{"stopReason":"error","errorMessage":"quota exceeded"}}', "oh_my_pi_json"),
    ]:
        failure = classify_role_failure(output, "", 0, format_name)
        assert failure["category"] == "usage_limit"
        assert failure["retry_not_before_seconds"] == 300
