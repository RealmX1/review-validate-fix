#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cursor_stream_tool_layer_health import classify_cursor_tool_call_outcome
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import start_run  # noqa: E402
from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: E402


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "config" / "alternative-reviewer.cursor.json"
DEFAULT_PROMPT = SKILL_DIR / "prompts" / "reviewer.md"
COMMAND_LOCK = SKILL_DIR / "scripts" / "command_lock.py"
WRITE_REVIEW_RESULT = SKILL_DIR / "scripts" / "write_review_result.py"
CHECK_REVIEW_RESULT = SKILL_DIR / "scripts" / "check_review_result.py"
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_ACTIVITY_CHECK_INTERVAL_SECONDS = 300.0
DEFAULT_ACTIVITY_PROBE_TIMEOUT_SECONDS = 10.0
DEFAULT_ACTIVITY_PROBE_FAILURE_THRESHOLD = 3
# cursor-agent stream-json 工具层失败「快速失败」阈值：在 cursor 自身 tool-runtime
# 连续报 spawnError / Aborted（工具执行器坏了，而非命令本身退出非零）累计达到该次数、
# 且尚无任何一次成功工具调用时，判定工具层不可用并提前 fail-over，不再白烧 idle_timeout。
# 详见 CursorStreamActivityMonitor。背景：2026-06-24 一次 cursor 自更新后的首次运行
# transient 把整条工具层打断（10/10 工具调用失败），旧逻辑直到 ~83s 才放弃。
DEFAULT_CURSOR_TOOL_FAILURE_THRESHOLD = 3
DEFAULT_MAX_RUNTIME_SECONDS: float | None = None
DEFAULT_LEASE_HEARTBEAT_SECONDS = 60.0
LEASE_HEARTBEAT_ENV = "RVF_LEASE_HEARTBEAT_SECONDS"
EXTERNAL_REVIEWER_TIMEOUT_FLAG = "RVF_EXTERNAL_REVIEWER_TIMEOUT"
EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE = 124
# reviewer 背后的 agentic/coding 订阅额度耗尽（usage limit / quota exhausted）专用退出码 + flag。
# 对齐 timeout 的 124+flag 双条件先例：dispatch_reviewers 以「125 AND (flag in stderr OR
# summary.output_error_reason==usage_limit_exhausted)」双条件识别，避免裸退出码误判。
EXTERNAL_REVIEWER_USAGE_LIMIT_EXIT_CODE = 125
EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG = "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT"
CODEX_BACKEND_CHALLENGE_FLAG = "RVF_CODEX_BACKEND_CHALLENGE"
# 额度/配额耗尽签名（D4 收紧）：在「无合法 review-result 的失败输出」上做大小写无关子串匹配。
# 裸 ``429`` 不计（commit hash / 字节数会误命中），需 http/status 前缀或与 too many requests 共现。
USAGE_LIMIT_SIGNATURES: tuple[str, ...] = (
    "usage limit",
    "rate limit",
    "quota exceeded",
    "insufficient_quota",
    "out of credits",
    "plan limit",
    "too many requests",
    "resource_exhausted",
    "rate_limit_exceeded",
)
USAGE_LIMIT_429_SIGNATURES: tuple[str, ...] = (
    "http 429",
    "status 429",
    "429 too many requests",
)
PROMPT_DELIVERY_STDIN = "stdin"
PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER = (
    "argv_prompt_flag_with_prompt_file_pointer"
)
PROMPT_DELIVERY_ARGV_POSITIONAL_PROMPT_FILE_REFERENCE = (
    "argv_positional_prompt_file_reference"
)
SUPPORTED_PROMPT_DELIVERY_MODES = {
    PROMPT_DELIVERY_STDIN,
    PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER,
    PROMPT_DELIVERY_ARGV_POSITIONAL_PROMPT_FILE_REFERENCE,
}
REVIEWER_PROMPT_FILE_ENV = "RVF_REVIEWER_PROMPT_FILE"
OUTPUT_FORMAT_TEXT = "text"
OUTPUT_FORMAT_CLAUDE_STREAM_JSON = "claude_stream_json"
OUTPUT_FORMAT_CODEX_JSON = "codex_json"
OUTPUT_FORMAT_CURSOR_STREAM_JSON = "cursor_stream_json"
OUTPUT_FORMAT_KIMI_STREAM_JSON = "kimi_stream_json"
OUTPUT_FORMAT_OH_MY_PI_JSON = "oh_my_pi_json"
SUPPRESS_STOP_HOOK_ENV = "RVF_SUPPRESS_STOP_HOOK"
SUPPORTED_OUTPUT_FORMATS = {
    OUTPUT_FORMAT_TEXT,
    OUTPUT_FORMAT_CLAUDE_STREAM_JSON,
    OUTPUT_FORMAT_CODEX_JSON,
    OUTPUT_FORMAT_CURSOR_STREAM_JSON,
    OUTPUT_FORMAT_KIMI_STREAM_JSON,
    OUTPUT_FORMAT_OH_MY_PI_JSON,
}
# 逐行 JSON 的格式集合：raw stdout 落工件前要按 JSON 行做脱敏，否则 `env_set` 的秘密值不脱敏。
# 新增结构化格式**必须**同时进这个集合——这是本文件历史上最容易漏的一处。
STRUCTURED_JSON_LINES_OUTPUT_FORMATS = {
    OUTPUT_FORMAT_CLAUDE_STREAM_JSON,
    OUTPUT_FORMAT_CODEX_JSON,
    OUTPUT_FORMAT_CURSOR_STREAM_JSON,
    OUTPUT_FORMAT_KIMI_STREAM_JSON,
    OUTPUT_FORMAT_OH_MY_PI_JSON,
}
CHILD_RVF_ENV_KEYS = {
    "RVF_RUN_DIR",
    "RVF_ARTIFACTS_DIR",
    "RVF_INPUTS_DIR",
    "RVF_REPO",
    "RVF_SCOPE_CONTRACT",
    "RVF_SCOPE_OF_WORK",
    "RVF_SESSION_CONTEXT",
    "RVF_SESSION_MANIFEST",
    "RVF_REVIEW_PACKET",
    "RVF_REVIEW_PACKET_METADATA",
    "RVF_REVIEW_RESULT",
    "RVF_REVIEWER_ID",
    REVIEWER_PROMPT_FILE_ENV,
}
USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_RELATIVE_DIRECTORY = (
    Path(".rvf") / "alternative-reviewer-config-overrides"
)
USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_SCHEMA_VERSION = 1
USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_ALLOWED_FIELDS = {
    "env_set",
    "health_command",
    "health_timeout_seconds",
    "pre_run_health",
}
STRUCTURED_REVIEWER_OUTPUT_PROTOCOL_CONTROL_FIELD_NAMES = {
    "event",
    "kind",
    "name",
    "role",
    "status",
    "subtype",
    "type",
}
_ACTIVE_REVIEWER_PROCESS_LOCK = threading.Lock()
_ACTIVE_REVIEWER_PROCESS: subprocess.Popen[str] | None = None


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("config root must be a JSON object")
    return config


def environment_variable_mapping(config: dict[str, Any], key: str) -> dict[str, str]:
    configured_environment_variables = config.get(key, {})
    if not isinstance(configured_environment_variables, dict):
        raise ValueError(f"{key} must be a string-to-string object")
    validated_environment_variables: dict[str, str] = {}
    for variable_name, variable_value in configured_environment_variables.items():
        if (
            not isinstance(variable_name, str)
            or not variable_name
            or "=" in variable_name
            or "\x00" in variable_name
        ):
            raise ValueError(f"{key} variable names must be non-empty environment variable names")
        if not isinstance(variable_value, str) or "\x00" in variable_value:
            raise ValueError(f"{key} values must be strings without NUL characters")
        validated_environment_variables[variable_name] = variable_value
    return validated_environment_variables


def redact_configured_environment_variable_values(
    text: str,
    configured_environment_variables: dict[str, str],
) -> str:
    redacted_text = text
    variables_by_descending_value_length = sorted(
        configured_environment_variables.items(),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    for variable_name, variable_value in variables_by_descending_value_length:
        if not variable_value:
            continue
        redacted_text = redacted_text.replace(
            variable_value,
            f"<redacted-env-value:{variable_name}>",
        )
    return redacted_text


def redact_configured_environment_variable_values_in_json_value(
    json_value: Any,
    configured_environment_variables: dict[str, str],
    *,
    containing_field_name: str | None = None,
) -> Any:
    if isinstance(json_value, str):
        if containing_field_name in STRUCTURED_REVIEWER_OUTPUT_PROTOCOL_CONTROL_FIELD_NAMES:
            return json_value
        return redact_configured_environment_variable_values(
            json_value,
            configured_environment_variables,
        )
    if isinstance(json_value, list):
        return [
            redact_configured_environment_variable_values_in_json_value(
                item,
                configured_environment_variables,
                containing_field_name=containing_field_name,
            )
            for item in json_value
        ]
    if isinstance(json_value, dict):
        return {
            field_name: redact_configured_environment_variable_values_in_json_value(
                field_value,
                configured_environment_variables,
                containing_field_name=str(field_name),
            )
            for field_name, field_value in json_value.items()
        }
    return json_value


def redact_configured_environment_variable_values_in_json_lines(
    text: str,
    configured_environment_variables: dict[str, str],
) -> str:
    if not configured_environment_variables:
        return text
    redacted_lines: list[str] = []
    for line in text.splitlines():
        try:
            parsed_line = json.loads(line)
        except json.JSONDecodeError:
            redacted_lines.append(
                redact_configured_environment_variable_values(
                    line,
                    configured_environment_variables,
                )
            )
            continue
        redacted_lines.append(
            json.dumps(
                redact_configured_environment_variable_values_in_json_value(
                    parsed_line,
                    configured_environment_variables,
                ),
                ensure_ascii=False,
            )
        )
    redacted_text = "\n".join(redacted_lines)
    if text.endswith(("\n", "\r")):
        redacted_text += "\n"
    return redacted_text


def user_local_alternative_reviewer_config_override_path(
    reviewer_label: str,
    *,
    user_home: Path | None = None,
) -> Path:
    reviewer_identifier = reviewer_id_from_label(reviewer_label)
    return (
        (user_home if user_home is not None else Path.home())
        / USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_RELATIVE_DIRECTORY
        / f"{reviewer_identifier}.json"
    )


def load_effective_alternative_reviewer_config(
    bundled_config_path: Path,
    *,
    user_home: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundled_config = load_config(bundled_config_path)
    reviewer_label = bundled_config.get("label")
    if not isinstance(reviewer_label, str) or not reviewer_label.startswith(
        "alternative-reviewer:"
    ):
        return bundled_config, {
            "applied": False,
            "fields": [],
            "env_set_names": [],
        }

    override_path = user_local_alternative_reviewer_config_override_path(
        reviewer_label,
        user_home=user_home,
    )
    if not override_path.exists():
        return bundled_config, {
            "applied": False,
            "fields": [],
            "env_set_names": [],
        }

    user_local_override = load_config(override_path)
    if type(user_local_override.get("schema_version")) is not int or user_local_override.get(
        "schema_version"
    ) != USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_SCHEMA_VERSION:
        raise ValueError(
            "user-local alternative reviewer override schema_version must be 1"
        )
    if user_local_override.get("reviewer_label") != reviewer_label:
        raise ValueError(
            "user-local alternative reviewer override reviewer_label must match bundled config label"
        )
    metadata_fields = {"schema_version", "reviewer_label"}
    unsupported_fields = sorted(
        set(user_local_override)
        - metadata_fields
        - USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_ALLOWED_FIELDS
    )
    if unsupported_fields:
        raise ValueError(
            "user-local alternative reviewer override contains unsupported fields: "
            + ", ".join(unsupported_fields)
        )
    applied_fields = sorted(set(user_local_override) - metadata_fields)
    if not applied_fields:
        raise ValueError(
            "user-local alternative reviewer override must configure at least one supported field"
        )
    override_env_set = environment_variable_mapping(user_local_override, "env_set")
    effective_config = dict(bundled_config)
    for field_name in applied_fields:
        effective_config[field_name] = user_local_override[field_name]
    return effective_config, {
        "applied": True,
        "fields": applied_fields,
        "env_set_names": sorted(override_env_set),
    }


def string_list(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a string array")
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def optional_string_list(config: dict[str, Any], key: str) -> list[str] | None:
    if key not in config:
        return None
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a string array or null")
    if not value:
        raise ValueError(f"{key} must not be empty when configured")
    return value


def positive_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive number")
    return parsed


def positive_int(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def optional_positive_float(config: dict[str, Any], key: str, default: float | None) -> float | None:
    if key not in config:
        return default
    value = config.get(key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive number or null") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive number or null")
    return parsed


def check_command(command: list[str]) -> str | None:
    return shutil.which(command[0])


def is_claude_print_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == "claude" and any(
        item in {"-p", "--print"} for item in command
    )


def is_cursor_print_command(command: list[str]) -> bool:
    # cursor-agent 的 headless 评审模式：`cursor-agent -p ...`。
    # 只匹配真实二进制名 cursor-agent，避免误判其它名为 agent 的命令。
    return bool(command) and Path(command[0]).name == "cursor-agent" and any(
        item in {"-p", "--print"} for item in command
    )


def is_kimi_prompt_command(command: list[str]) -> bool:
    # kimi 的 `-p/--prompt` 是**带值 flag**，且其值（prompt 指针）由 runner 在运行期追加，
    # 所以这里刻意不看 `-p`——只认二进制名，否则 config 里的静态 flag 组永远匹配不上。
    return bool(command) and Path(command[0]).name == "kimi"


def is_oh_my_pi_print_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == "omp" and any(
        item in {"-p", "--print", "--mode"} or item.startswith("--mode=")
        for item in command
    )


def codex_subcommand_index(command: list[str]) -> int | None:
    if not command or Path(command[0]).name != "codex":
        return None

    options_with_values = {
        "-a",
        "--add-dir",
        "--ask-for-approval",
        "-c",
        "-C",
        "--cd",
        "--config",
        "--disable",
        "--enable",
        "-i",
        "--image",
        "--local-provider",
        "-m",
        "--model",
        "-p",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "-s",
        "--sandbox",
    }
    index = 1
    while index < len(command):
        item = command[index]
        if item in {"exec", "e"}:
            return index
        if item == "--":
            return None
        if item in options_with_values:
            index += 2
            continue
        if item.startswith("--") and "=" in item:
            index += 1
            continue
        if item.startswith("-"):
            index += 1
            continue
        return None
    return None


def is_codex_exec_command(command: list[str]) -> bool:
    return codex_subcommand_index(command) is not None


def output_format_argument_value(command: list[str]) -> str | None:
    for index, item in enumerate(command):
        if item.startswith("--output-format="):
            return item.split("=", 1)[1]
        if item == "--output-format" and index + 1 < len(command):
            return command[index + 1]
    return None


def ensure_claude_stream_json_command(command: list[str]) -> list[str]:
    patched = list(command)
    equals_index = next(
        (
            index
            for index, item in enumerate(patched)
            if item.startswith("--output-format=")
        ),
        None,
    )
    if equals_index is not None:
        patched[equals_index] = "--output-format=stream-json"
    elif "--output-format" in patched:
        index = patched.index("--output-format")
        if index + 1 < len(patched):
            patched[index + 1] = "stream-json"
        else:
            patched.append("stream-json")
    else:
        patched.extend(["--output-format", "stream-json"])
    if "--include-hook-events" not in patched:
        patched.append("--include-hook-events")
    if "--include-partial-messages" not in patched:
        patched.append("--include-partial-messages")
    if "--verbose" not in patched:
        patched.append("--verbose")
    if "--disable-slash-commands" not in patched:
        patched.append("--disable-slash-commands")
    return patched


def ensure_cursor_stream_json_command(command: list[str]) -> list[str]:
    # 仅保证 print 模式 + stream-json 输出，使 bare cursor 配置也能被正常解析。
    # 绝不注入 claude 专属 flag（--include-hook-events / --include-partial-messages /
    # --verbose / --disable-slash-commands）——cursor-agent 不认识它们。
    patched = list(command)
    if not any(item in {"-p", "--print"} for item in patched):
        patched.append("-p")
    equals_index = next(
        (
            index
            for index, item in enumerate(patched)
            if item.startswith("--output-format=")
        ),
        None,
    )
    if equals_index is not None:
        patched[equals_index] = "--output-format=stream-json"
    elif "--output-format" in patched:
        index = patched.index("--output-format")
        if index + 1 < len(patched):
            patched[index + 1] = "stream-json"
        else:
            patched.append("stream-json")
    else:
        patched.extend(["--output-format", "stream-json"])
    return patched


def ensure_kimi_stream_json_command(command: list[str]) -> list[str]:
    """保证 `--output-format stream-json`。

    ⚠️ 绝不注入 `-y/--yolo` 或 `--auto`：kimi 的 prompt 模式**隐含** forceAuto，
    显式加上反而与 `-p` 互斥报错。也绝不注入 `-p`——它的值是运行期才生成的指针文本，
    由 `finalize_reviewer_command_for_prompt_delivery` 追加。
    """
    patched = list(command)
    equals_index = next(
        (index for index, item in enumerate(patched) if item.startswith("--output-format=")),
        None,
    )
    if equals_index is not None:
        patched[equals_index] = "--output-format=stream-json"
    elif "--output-format" in patched:
        index = patched.index("--output-format")
        if index + 1 < len(patched):
            patched[index + 1] = "stream-json"
        else:
            patched.append("stream-json")
    else:
        patched.extend(["--output-format", "stream-json"])
    return patched


def ensure_oh_my_pi_json_command(command: list[str]) -> list[str]:
    """保证 print 模式 + `--mode json`，并保住「评审腿不得递归触发 RVF」这条硬约束。

    `--no-extensions` 是 omp 侧等价于 codex `--disable hooks` 的东西：omp 的 Stop 语义完全
    活在进程内的 TS extension 里，reviewer 子进程一旦加载它们，就会在评审腿里再起一轮 RVF。
    `RVF_SUPPRESS_STOP_HOOK=1` 是第二道保险，不是它的替代品。
    """
    patched = list(command)
    if not any(item in {"-p", "--print"} for item in patched):
        patched.append("-p")
    equals_index = next(
        (index for index, item in enumerate(patched) if item.startswith("--mode=")),
        None,
    )
    if equals_index is not None:
        patched[equals_index] = "--mode=json"
    elif "--mode" in patched:
        index = patched.index("--mode")
        if index + 1 < len(patched):
            patched[index + 1] = "json"
        else:
            patched.append("json")
    else:
        patched.extend(["--mode", "json"])
    if "--no-extensions" not in patched:
        patched.append("--no-extensions")
    return patched


def codex_json_enabled(command: list[str]) -> bool:
    return "--json" in command


def codex_hooks_disabled(command: list[str]) -> bool:
    for index, item in enumerate(command):
        if item == "--disable" and index + 1 < len(command) and command[index + 1] == "hooks":
            return True
        if item == "--disable=hooks":
            return True
        if item == "-c" and index + 1 < len(command) and command[index + 1] == "features.hooks=false":
            return True
        if item == "-c=features.hooks=false":
            return True
    return False


def ensure_codex_hooks_disabled_command(command: list[str]) -> list[str]:
    exec_index = codex_subcommand_index(command)
    if exec_index is None or codex_hooks_disabled(command):
        return list(command)
    patched = list(command)
    patched[exec_index:exec_index] = ["--disable", "hooks"]
    return patched


def ensure_codex_json_command(command: list[str]) -> list[str]:
    patched = list(command)
    if "--json" not in patched:
        patched.append("--json")
    if "-" not in patched:
        patched.append("-")
    return patched


def ensure_run_directory_grant_argument(
    command: list[str],
    directory: Path,
    *,
    grant_flag: str,
    scan_from_index: int = 1,
    insert_before_token: str | None = None,
) -> list[str]:
    """幂等地保证 argv 里带上「把 RVF run directory 授权成子进程可写目录」的参数。

    这不是 codex 专属需求：reviewer 要写的 `$RVF_REVIEW_RESULT` 落在 run directory 里、
    位于目标仓库之外，因此凡是带工作目录沙箱的 reviewer CLI 都必须显式授权它。
    codex / kimi / oh-my-pi 三家的 flag 名恰好都是 `--add-dir`，但位置约束不同：
    codex 必须插在 `exec` 子命令之后、stdin 占位符 `-` 之前，故位置约束由调用方
    通过 scan_from_index / insert_before_token 表达，而不是写死在本函数里。
    """
    directory_arg = str(directory)
    patched = list(command)
    equals_prefix = f"{grant_flag}="
    index = max(scan_from_index, 0)
    while index < len(patched):
        item = patched[index]
        if item == grant_flag and index + 1 < len(patched):
            if Path(patched[index + 1]).expanduser() == directory:
                return patched
            index += 2
            continue
        if item.startswith(equals_prefix):
            if Path(item.split("=", 1)[1]).expanduser() == directory:
                return patched
            index += 1
            continue
        index += 1

    insert_at = len(patched)
    if insert_before_token is not None:
        for index in range(max(scan_from_index, 0), len(patched)):
            if patched[index] == insert_before_token:
                insert_at = index
                break
    patched[insert_at:insert_at] = [grant_flag, directory_arg]
    return patched


def ensure_codex_exec_add_dir(command: list[str], directory: Path) -> list[str]:
    exec_index = codex_subcommand_index(command)
    if exec_index is None:
        return command
    return ensure_run_directory_grant_argument(
        command,
        directory,
        grant_flag="--add-dir",
        scan_from_index=exec_index + 1,
        insert_before_token="-",
    )


REVIEWER_PROMPT_FILE_POINTER_INSTRUCTION_TEMPLATE = (
    "Your review instructions and review packet are too large to pass on the command line, "
    "so they were written to this file:\n"
    "{prompt_file_path}\n"
    "That absolute path is also exported as $RVF_REVIEWER_PROMPT_FILE. Read the whole file "
    "first, then do exactly what it says. Do not begin reviewing, and do not answer, before "
    "you have read it. This message is only a pointer, never the review instructions."
)


def build_reviewer_prompt_file_pointer_instruction(prompt_file_path: Path) -> str:
    """生成投给 argv 的短指针指令（绝对路径逐字写入，避免 reviewer 读不到 env 时无路可走）。"""
    return REVIEWER_PROMPT_FILE_POINTER_INSTRUCTION_TEMPLATE.format(
        prompt_file_path=prompt_file_path
    )


def finalize_reviewer_command_for_prompt_delivery(
    command: list[str],
    *,
    prompt_delivery: str,
    prompt_flag: str | None,
    prompt_file_path: Path,
    prompt_text: str,
) -> tuple[list[str], str]:
    """按 harness 的 prompt 投递形态终结 argv，并给出真正要写进 stdin 的文本。

    返回 `(final_argv, stdin_text)`。三种形态：

    - `stdin`：整份 prompt 走 stdin，逐字保持既有行为（cursor / claude / codex）。
    - `argv_prompt_flag_with_prompt_file_pointer`：整份 prompt 留在磁盘，argv 只带一段
      固定短指令。kimi 的 `-p` 是带值 flag 且从不读 stdin，而真实评审 packet 远超
      ARG_MAX，两头堵死才走到这一步。**代价是投递不保证送达**——模型得先自己去读那个
      文件，读失败与「拒绝评审」在产物上不可区分。
    - `argv_positional_prompt_file_reference`：argv 追加 `@<绝对路径>`，由 harness 自己
      把文件内容展开进 prompt（omp 的原生语法）。比上一条强：展开发生在 harness 进程内，
      不依赖模型自觉去读文件，送达性与 stdin 等价，同时 argv 只有一个短路径。

    omp 走第三条而不是 stdin，是被实测逼出来的：`omp -p` **不读 stdin**，喂进去的整份
    prompt 会被静默丢弃——进程 exit 0、stderr 全空、stdout 只有一行 session、零 turn。
    比 kimi 那条（至少 exit 1 报 argument missing）更难发现，端到端跑一次才暴露。
    """
    if prompt_delivery == PROMPT_DELIVERY_STDIN:
        return list(command), prompt_text
    if prompt_delivery == PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER:
        if not prompt_flag:
            raise ValueError(
                f"prompt_delivery={prompt_delivery} requires a non-empty prompt_flag"
            )
        pointer_instruction = build_reviewer_prompt_file_pointer_instruction(prompt_file_path)
        return [*command, prompt_flag, pointer_instruction], ""
    if prompt_delivery == PROMPT_DELIVERY_ARGV_POSITIONAL_PROMPT_FILE_REFERENCE:
        return [*command, f"@{prompt_file_path}"], ""
    raise ValueError(f"unsupported prompt_delivery: {prompt_delivery}")


def reviewer_command_argv_bytes(command: list[str]) -> int:
    """argv 在 execve 侧占用的字节数近似（每个参数含结尾 NUL），用于对照 ARG_MAX。"""
    return sum(len(item.encode("utf-8")) + 1 for item in command)


HEALTH_PROBE_SHARES_DISPATCH_ENTRYPOINT = "shared_dispatch_entrypoint"
HEALTH_PROBE_PARITY_EXEMPTION_CONFIG_FIELD = "health_probe_parity_exemption"
# 唯一合法的豁免取值，且**只**给测试夹具用：夹具的 "reviewer" 是 `python3 -c "<脚本>"`，
# 两边 `-c` 的载荷本来就必须不同（健康脚本验环境、评审脚本写工件），根本不存在可比对的
# CLI 入口。刻意做成「一个自证其为夹具的字面量」而不是布尔开关：布尔 opt-out 正是
# 「配错即静默退化」的形状，而这个名字一旦出现在出厂模板里，contract gate 会当场拒绝。
HEALTH_PROBE_PARITY_EXEMPTION_INLINE_SCRIPT_FIXTURE = (
    "inline_script_fixture_has_no_shared_cli_entrypoint"
)
HEALTH_PROBE_PARITY_EXEMPTED = "exempted_inline_script_fixture"
HEALTH_PROBE_USES_EXTERNAL_EXECUTABLE = "external_probe_executable"


def reviewer_dispatch_flag_multiset(
    argv: list[str],
    *,
    prompt_flag: str | None,
    run_directory_grant_flag: str | None,
) -> list[str]:
    """一条 reviewer 命令「走的是哪条后端路径」的签名。

    刻意剥掉三类与后端路径无关的东西：可执行名（由调用方单独比对）、prompt 承载 token
    （`-` 占位符或 ``prompt_flag`` 及其值）、运行期注入的 run-directory 授权。
    结果排序后比较——这里要钉的是「flag 集合相同」，不是「顺序相同」。
    """
    tokens: list[str] = []
    index = 1
    while index < len(argv):
        item = argv[index]
        if item == "-":
            index += 1
            continue
        if run_directory_grant_flag and item == run_directory_grant_flag:
            index += 2
            continue
        if run_directory_grant_flag and item.startswith(f"{run_directory_grant_flag}="):
            index += 1
            continue
        if prompt_flag and item == prompt_flag:
            index += 2
            continue
        tokens.append(item)
        index += 1
    return sorted(tokens)


def split_health_command_positional_prompt(
    health_command: list[str],
    *,
    prompt_flag: str | None,
    dispatch_signature: list[str] | None = None,
) -> tuple[list[str], str | None]:
    """把 health 命令拆成「flag 部分」与「末尾位置参数 prompt」。

    位置参数是必须的形态：`run_health` 调 `subprocess.run` 时**既不传 input= 也不设 stdin=**，
    照搬 claude 的裸 `-p` 或 codex 的 `exec -` 会让探针阻塞在 stdin 上、一路挂到超时。

    ⚠️ 末尾 token 只有在它**不是派发签名里已有的 token** 时才算 prompt。少了这条，
    ``["kimi","--output-format","stream-json"]``（一条根本没有 prompt 的探针）会把
    ``stream-json`` 当成 prompt，而同源规整又会把 ``--output-format stream-json`` 补回来，
    两边多重集恰好相等 ⇒ **一条不发起任何调用的探针静默通过**。这正是本 gate 要防的形状。
    """
    if prompt_flag and prompt_flag in health_command:
        return list(health_command), None
    if len(health_command) >= 2 and not health_command[-1].startswith("-"):
        candidate = health_command[-1]
        if dispatch_signature is None or candidate not in dispatch_signature:
            return list(health_command[:-1]), candidate
    return list(health_command), None


def assert_health_command_shares_dispatch_flag_set(
    command: list[str],
    health_command: list[str],
    *,
    prompt_flag: str | None,
    run_directory_grant_flag: str | None,
) -> str:
    """把 cursor 那条用真实故障买来的注释纪律变成可执行不变量。

    2026-08-11 的 run rvf-20260811T170157Z：cursor 登录已过期，`cursor-agent status` 退 0
    而 `cursor-agent -p ...` 退 1。probe 只看前者，于是把 cursor 判为可用、选进两腿；那一腿
    派出去立刻失败、不产出 review-result，本轮实际只剩单路评审结论。**旁路的 auth 查询命令
    不是真实可用性的充分判据——只有走评审那条命令路径的探针才是。**

    判据刻意只钉住教训本身：**同一个可执行名**时，两边的 flag 多重集必须完全相等。
    可执行名不同（例如运维显式把探针指向自建网关的 curl 健康检查）不属于本 gate 管辖——
    那是一次显式的运维决策，不是「没人审视过的默认值」这个失效模式；但它必须被记进 ledger
    而不是无声通过。
    """
    if not command or not health_command:
        raise ValueError("command 与 health_command 都必须非空才能比对健康探针路径")
    if Path(health_command[0]).name != Path(command[0]).name:
        return HEALTH_PROBE_USES_EXTERNAL_EXECUTABLE

    dispatch_signature = reviewer_dispatch_flag_multiset(
        command,
        prompt_flag=prompt_flag,
        run_directory_grant_flag=run_directory_grant_flag,
    )
    health_flags, positional_prompt = split_health_command_positional_prompt(
        health_command,
        prompt_flag=prompt_flag,
        dispatch_signature=dispatch_signature,
    )
    health_signature = reviewer_dispatch_flag_multiset(
        health_flags,
        prompt_flag=prompt_flag,
        run_directory_grant_flag=run_directory_grant_flag,
    )
    if dispatch_signature != health_signature:
        only_in_dispatch = sorted(set(dispatch_signature) - set(health_signature))
        only_in_health = sorted(set(health_signature) - set(dispatch_signature))
        raise ValueError(
            "health_command 与 command 没有走同一条后端路径，健康探针因此不能证明评审腿可用"
            f"（只在 command 里: {only_in_dispatch or '无'}；只在 health_command 里: "
            f"{only_in_health or '无'}）。"
            "健康探针必须与派发命令共用同一组 flag，只把 prompt 换成一句最小指令，"
            "且 prompt 要走位置参数——run_health 不喂 stdin。"
        )
    if positional_prompt is None and not (prompt_flag and prompt_flag in health_command):
        raise ValueError(
            "health_command 缺少 prompt：它必须真的发起一次最小评审调用，"
            "而不是一条旁路查询命令"
        )
    return HEALTH_PROBE_SHARES_DISPATCH_ENTRYPOINT


def normalise_reviewer_command_for_output_format(
    argv: list[str], output_format: str
) -> list[str]:
    """按 output_format 把一条 reviewer 命令规整成「真正会被执行的那一条」。

    派发命令与健康探针共用本函数——这样「探针和评审走同一条路径」是**构造出来的**，
    而不是靠运维记得在两处各抄一遍 flag（抄漏一个就换一道门，正是 cursor 那次故障的形状）。
    """
    if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON and is_claude_print_command(argv):
        return ensure_claude_stream_json_command(argv)
    if output_format == OUTPUT_FORMAT_CURSOR_STREAM_JSON and is_cursor_print_command(argv):
        return ensure_cursor_stream_json_command(argv)
    if output_format == OUTPUT_FORMAT_CODEX_JSON and is_codex_exec_command(argv):
        return ensure_codex_json_command(ensure_codex_hooks_disabled_command(argv))
    if output_format == OUTPUT_FORMAT_KIMI_STREAM_JSON and is_kimi_prompt_command(argv):
        return ensure_kimi_stream_json_command(argv)
    if output_format == OUTPUT_FORMAT_OH_MY_PI_JSON and is_oh_my_pi_print_command(argv):
        return ensure_oh_my_pi_json_command(argv)
    return list(argv)


def normalise_health_command_for_output_format(
    health_command: list[str],
    output_format: str,
    *,
    prompt_flag: str | None,
    dispatch_signature: list[str] | None = None,
) -> list[str]:
    """健康探针的规整：与派发同源，但 prompt 走位置参数、且不留 stdin 占位符 `-`。"""
    flags, positional_prompt = split_health_command_positional_prompt(
        health_command,
        prompt_flag=prompt_flag,
        dispatch_signature=dispatch_signature,
    )
    normalised = [
        item
        for item in normalise_reviewer_command_for_output_format(flags, output_format)
        if item != "-"
    ]
    if positional_prompt is not None:
        normalised.append(positional_prompt)
    return normalised


def check_repo(repo: Path) -> None:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(f"not a git repo: {repo}")


def safe_artifact_token(value: str) -> str:
    token = "".join(char if char.isalnum() or char in "._-" else "-" for char in value.strip())
    return token.strip("-")[:80] or "reviewer"


def reviewer_id_from_label(label: str) -> str:
    if label.startswith("alternative-reviewer:"):
        label = label.split(":", 1)[1]
    return safe_artifact_token(label)


def unique_child_path(directory: Path, name: str) -> Path:
    path = directory / safe_artifact_token(name)
    if not path.exists():
        return path
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    for index in range(2, 10000):
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}.{os.urandom(4).hex()}{suffix}"


def write_child_artifact(
    directory: Path,
    name: str,
    content_or_bytes: bytes | str | dict[str, Any] | list[Any],
    *,
    unique: bool = False,
) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = unique_child_path(directory, name) if unique else directory / safe_artifact_token(name)
    if isinstance(content_or_bytes, bytes):
        path.write_bytes(content_or_bytes)
    elif isinstance(content_or_bytes, str):
        path.write_text(content_or_bytes, encoding="utf-8")
    else:
        path.write_text(json.dumps(content_or_bytes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def infer_scope_contract(review_packet: Path | None) -> Path | None:
    if review_packet is not None:
        candidates = [
            review_packet.parent / "scope.contract.json",
            review_packet.parent / "inputs" / "scope.contract.json",
            review_packet.parent.parent / "inputs" / "scope.contract.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
    env_value = os.environ.get("RVF_SCOPE_CONTRACT")
    if env_value and env_value.strip():
        candidate = Path(env_value).expanduser().resolve()
        if candidate.exists():
            return candidate
    return None


def load_scope_contract(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def tracker_scope_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    session_manifest_path = contract.get("session_manifest_path")
    if not isinstance(session_manifest_path, str) or not session_manifest_path:
        return {}
    try:
        manifest = json.loads(Path(session_manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    tracker = manifest.get("tracker")
    if not isinstance(tracker, dict):
        return {}
    scope = tracker.get("tracker_scope")
    return scope if isinstance(scope, dict) else {}


def lease_heartbeat_seconds() -> float:
    raw = os.environ.get(LEASE_HEARTBEAT_ENV, "").strip()
    if not raw:
        return DEFAULT_LEASE_HEARTBEAT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LEASE_HEARTBEAT_SECONDS
    return value if value > 0 else DEFAULT_LEASE_HEARTBEAT_SECONDS


class TrackerLeaseRuntime:
    def __init__(
        self,
        *,
        repo: Path | None,
        scope_contract: dict[str, Any],
        reviewer_id: str,
        run_id: str,
    ) -> None:
        self.repo = repo
        self.scope_contract = scope_contract
        self.reviewer_id = reviewer_id
        self.run_id = run_id
        self.lease_id: str | None = None
        self._owns_lease = False
        self._participant_joined = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_handlers: dict[int, Any] = {}

    def acquire(self) -> None:
        if self.repo is None:
            return
        unit_ids = self.scope_contract.get("primary_units")
        if not isinstance(unit_ids, list) or not all(isinstance(item, str) and item for item in unit_ids):
            return
        if not unit_ids:
            return
        existing = self.scope_contract.get("tracker_lease_id")
        if isinstance(existing, str) and existing:
            self.lease_id = existing
            result = reviewable_unit_diff_tracker.lease_participant_join(
                repo=self.repo,
                lease_id=existing,
                reviewer_id=self.reviewer_id,
                run_id=self.run_id,
                owns_lease=False,
                log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
                if os.environ.get("CODEX_RVF_LOG_ROOT")
                else None,
            )
            if not result.get("joined"):
                raise RuntimeError(f"tracker lease participant join failed: {result.get('reason')}")
            self._participant_joined = True
            return
        tracker_scope = tracker_scope_from_contract(self.scope_contract)
        session_id = tracker_scope.get("source_session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = os.environ.get("CODEX_SESSION_ID") or "alternative-reviewer"
        result = reviewable_unit_diff_tracker.lease_acquire(
            repo=self.repo,
            session_id=session_id,
            run_id=str(self.scope_contract.get("run_id") or self.run_id),
            reviewer_id=self.reviewer_id,
            unit_ids=list(dict.fromkeys(unit_ids)),
            log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
            if os.environ.get("CODEX_RVF_LOG_ROOT")
            else None,
        )
        if not result.get("acquired"):
            raise RuntimeError(f"tracker lease acquire failed: {result.get('reason')}")
        lease_id = result.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id:
            raise RuntimeError("tracker lease acquire returned no lease_id")
        self.lease_id = lease_id
        self._owns_lease = True
        joined = reviewable_unit_diff_tracker.lease_participant_join(
            repo=self.repo,
            lease_id=lease_id,
            reviewer_id=self.reviewer_id,
            run_id=self.run_id,
            owns_lease=True,
            log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
            if os.environ.get("CODEX_RVF_LOG_ROOT")
            else None,
        )
        if not joined.get("joined"):
            raise RuntimeError(f"tracker lease participant join failed: {joined.get('reason')}")
        self._participant_joined = True

    def start(self) -> None:
        if self.repo is None or self.lease_id is None:
            return
        self._install_signal_handlers()
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._restore_signal_handlers()

    def release(self, reason: str) -> None:
        if self.repo is None or self.lease_id is None:
            return
        log_root_override = (
            Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
            if os.environ.get("CODEX_RVF_LOG_ROOT")
            else None
        )
        active_participant_count = 0
        if self._participant_joined:
            result = reviewable_unit_diff_tracker.lease_participant_finish(
                repo=self.repo,
                lease_id=self.lease_id,
                reviewer_id=self.reviewer_id,
                run_id=self.run_id,
                reason=reason,
                log_root_override=log_root_override,
            )
            active_participant_count = int(result.get("active_participant_count") or 0)
            owning_participant_count = int(result.get("owning_participant_count") or 0)
        else:
            owning_participant_count = 1 if self._owns_lease else 0
        if self._owns_lease and owning_participant_count > 0 and active_participant_count == 0:
            reviewable_unit_diff_tracker.lease_release(
                repo=self.repo,
                lease_id=self.lease_id,
                reason=reason,
                log_root_override=log_root_override,
            )
        self.lease_id = None
        self._owns_lease = False
        self._participant_joined = False

    def _heartbeat_loop(self) -> None:
        assert self.repo is not None
        assert self.lease_id is not None
        interval = lease_heartbeat_seconds()
        while not self._stop.wait(interval):
            reviewable_unit_diff_tracker.lease_participant_refresh(
                repo=self.repo,
                lease_id=self.lease_id,
                reviewer_id=self.reviewer_id,
                run_id=self.run_id,
                log_root_override=Path(os.environ["CODEX_RVF_LOG_ROOT"]).expanduser().resolve()
                if os.environ.get("CODEX_RVF_LOG_ROOT")
                else None,
            )

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._old_handlers[sig] = signal.getsignal(sig)

            def _handler(signum: int, frame: object, *, _sig: int = sig) -> None:
                terminate_active_reviewer_process()
                self.stop()
                self.release(f"signal:{signal_name(signum)}")
                old = self._old_handlers.get(_sig)
                if callable(old):
                    old(signum, frame)
                raise SystemExit(128 + signum)

            signal.signal(sig, _handler)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._old_handlers.items():
            signal.signal(sig, handler)
        self._old_handlers.clear()


def build_prompt(
    prompt_file: Path,
    session_context: Path | None,
    review_packet: Path | None,
    repo: Path | None,
    scope_contract: Path | None = None,
    result_path: Path | None = None,
) -> str:
    parts = []
    env_lines = [
        "## Review session environment",
        "- The reviewer process receives short-lived `RVF_*` environment variables for this review session.",
        "- Use these variables in commands and notes instead of expanding absolute artifact paths.",
    ]
    if repo is not None:
        env_lines.append("- `RVF_REPO`: target repository. Use it as the repo path if `pwd` is not already the repo.")
    if scope_contract is not None:
        env_lines.append(
            "- `RVF_SCOPE_CONTRACT`: scope contract. Read it before reviewing and obey its scope boundaries; "
            "`primary_units` takes precedence over session manifest paths when present."
        )
    if session_context is not None:
        env_lines.extend(
            [
                "- `RVF_SCOPE_OF_WORK`: main-agent scope anchor. Read it before analyzing code.",
                "- `RVF_SESSION_CONTEXT`: alias for `RVF_SCOPE_OF_WORK`.",
            ]
        )
    if review_packet is not None:
        env_lines.append("- `RVF_REVIEW_PACKET`: self-contained review packet and fallback context.")
    if scope_contract is not None or session_context is not None or review_packet is not None:
        read_targets = []
        if scope_contract is not None:
            read_targets.append('"$RVF_SCOPE_CONTRACT"')
        if session_context is not None:
            read_targets.append('"$RVF_SCOPE_OF_WORK"')
        if review_packet is not None:
            read_targets.append('"$RVF_REVIEW_PACKET"')
        env_lines.append(f"- Read entry files with: `sed -n '1,220p' {' '.join(read_targets)}`")
    if repo is not None:
        env_lines.extend(
            [
                "- `RVF_COMMAND_LOCK`: repo-scoped command lock wrapper.",
                "- `RVF_WRITE_REVIEW_RESULT`: script for writing the canonical review result artifact.",
                "- `RVF_CHECK_REVIEW_RESULT`: script for validating the canonical artifact before final response.",
                '- Example: `python3 "$RVF_COMMAND_LOCK" --repo "$RVF_REPO" --name <stable-lock-name> -- <command ...>`',
                '- Result example: `python3 "$RVF_WRITE_REVIEW_RESULT" no-issues --out "$RVF_REVIEW_RESULT" --audit-summary "<what you actually audited and ruled out>"`',
            ]
        )
    if result_path is not None:
        env_lines.extend(
            [
                "- `RVF_REVIEW_RESULT`: canonical reviewer result artifact. This protocol output is under the RVF run directory and is not a repo source edit.",
                '- Validate before your final message with: `python3 "$RVF_CHECK_REVIEW_RESULT" "$RVF_REVIEW_RESULT"`',
            ]
        )
    if session_context is not None:
        env_lines.append(
            "- Do not use the entire git diff as full review scope unless the main agent explicitly requested full diff review."
        )
    if scope_contract is not None:
        env_lines.append(
            "- Treat the session manifest as ownership evidence and tracker audit context, not as the final scope contract."
        )
    if len(env_lines) > 3:
        parts.append("\n".join(env_lines))
    parts.append(prompt_file.read_text(encoding="utf-8").strip())
    if review_packet is not None:
        parts.append(review_packet.read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)


def scrub_env(env_unset: list[str]) -> dict[str, str]:
    env = os.environ.copy()
    for name in CHILD_RVF_ENV_KEYS:
        env.pop(name, None)
    for name in env_unset:
        env.pop(name, None)
    env["RVF_SKILL_DIR"] = str(SKILL_DIR)
    env["RVF_COMMAND_LOCK"] = str(COMMAND_LOCK)
    env["RVF_WRITE_REVIEW_RESULT"] = str(WRITE_REVIEW_RESULT)
    env["RVF_CHECK_REVIEW_RESULT"] = str(CHECK_REVIEW_RESULT)
    return env


def build_alternative_reviewer_subprocess_environment(
    env_unset: list[str],
    env_set: dict[str, str],
) -> dict[str, str]:
    reviewer_subprocess_environment = scrub_env(env_unset)
    reviewer_subprocess_environment.update(env_set)
    reviewer_subprocess_environment["RVF_SKILL_DIR"] = str(SKILL_DIR)
    reviewer_subprocess_environment["RVF_COMMAND_LOCK"] = str(COMMAND_LOCK)
    reviewer_subprocess_environment["RVF_WRITE_REVIEW_RESULT"] = str(WRITE_REVIEW_RESULT)
    reviewer_subprocess_environment["RVF_CHECK_REVIEW_RESULT"] = str(CHECK_REVIEW_RESULT)
    return reviewer_subprocess_environment


def check_review_result_artifact(path: Path, scope_contract: Path | None) -> tuple[dict[str, Any], str]:
    command = [sys.executable, str(CHECK_REVIEW_RESULT), str(path), "--json"]
    if scope_contract is not None:
        command.extend(["--scope-contract", str(scope_contract)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "kind": "invalid",
            "issue_count": 0,
            "request_count": 0,
            "request_types": [],
            "errors": [stdout or completed.stderr.strip() or "check_review_result.py returned invalid output"],
        }
    return payload, completed.stderr.strip()


def run_health(
    command: list[str],
    env: dict[str, str],
    timeout: int,
    *,
    emit_output: bool = True,
    cwd: Path | None = None,
) -> int:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return fail(f"health command timed out after {timeout}s")
    if completed.returncode != 0:
        return fail(completed.stderr.strip() or completed.stdout.strip() or "health command failed")
    if emit_output:
        print(completed.stdout.strip())
    return 0


def resolve_oh_my_pi_default_model_commands(
    config: dict[str, Any],
    command: list[str],
    health_command: list[str] | None,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int,
) -> tuple[list[str], list[str] | None]:
    source = config.get("reviewer_model_selection_source")
    if source is None:
        return command, health_command
    if source != "oh_my_pi_default_model_role":
        raise ValueError("未知 reviewer_model_selection_source")
    if not is_oh_my_pi_print_command(command) or not health_command or not is_oh_my_pi_print_command(health_command):
        raise ValueError("OMP 默认选模要求正式评审和健康检查都使用 omp")
    if any(item == "--model" or item.startswith("--model=") for item in command + health_command):
        raise ValueError("动态默认选模不能同时配置固定 --model")
    try:
        completed = subprocess.run(
            [command[0], "config", "get", "modelRoles", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            # 配置查询不发模型请求，为外层 probe 的真实健康检查保留预算。
            timeout=min(timeout, 5),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"无法查询 OMP 默认模型: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise ValueError(f"OMP 默认模型查询失败: exit {completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("OMP 默认模型查询未返回有效 JSON") from exc
    roles = payload.get("value") if isinstance(payload, dict) and payload.get("key") == "modelRoles" else None
    selector = roles.get("default") if isinstance(roles, dict) else None
    # 完整 selector 固定 provider，保持注册表中的 Codex 订阅额度池身份真实。
    if not isinstance(selector, str) or not re.fullmatch(
        r"openai-codex/[A-Za-z0-9][A-Za-z0-9._-]*(?::(?:off|minimal|low|medium|high|xhigh|max|ultra|inherit))?",
        selector,
    ):
        raise ValueError("OMP 默认模型必须明确指定 openai-codex 的单一模型；该评审腿不可用")
    return (
        [command[0], "--model", selector, *command[1:]],
        [health_command[0], "--model", selector, *health_command[1:]],
    )


def _payload_length(payload: bytes | str | None) -> int:
    if payload is None:
        return 0
    return len(payload)


def _payload_delta(payload: bytes | str | None, start: int) -> str:
    if payload is None:
        return ""
    delta = payload[start:]
    if isinstance(delta, bytes):
        return delta.decode("utf-8", errors="replace")
    return delta


class ClaudeStreamActivityMonitor:
    def __init__(self) -> None:
        self.active_bash_tool_ids: set[str] = set()
        self.active_anonymous_bash_tools = 0
        self._pending_line = ""

    @property
    def waiting_on_long_command(self) -> bool:
        return bool(self.active_bash_tool_ids or self.active_anonymous_bash_tools > 0)

    @property
    def tool_layer_unavailable(self) -> bool:
        return False

    @property
    def early_fail_over_reason_code(self) -> str | None:
        return None

    def ingest(self, output: str) -> None:
        self._pending_line += output
        lines = self._pending_line.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending_line = lines.pop()
        else:
            self._pending_line = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            self._ingest_line(line)

    def _ingest_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "result":
            self.active_bash_tool_ids.clear()
            self.active_anonymous_bash_tools = 0
            return
        message = payload.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "tool_use" and item.get("name") == "Bash":
                tool_id = item.get("id")
                if isinstance(tool_id, str) and tool_id:
                    self.active_bash_tool_ids.add(tool_id)
                else:
                    self.active_anonymous_bash_tools += 1
            elif item_type == "tool_result":
                tool_id = item.get("tool_use_id")
                if isinstance(tool_id, str) and tool_id:
                    self.active_bash_tool_ids.discard(tool_id)
                elif self.active_anonymous_bash_tools > 0:
                    self.active_anonymous_bash_tools -= 1


class CursorStreamActivityMonitor:
    """监控 cursor-agent stream-json，在工具执行层整体坏掉时支持「快速失败」。

    与 :class:`ClaudeStreamActivityMonitor` 不同，本 monitor **不**抑制 idle 超时
    （``waiting_on_long_command`` 恒为 ``False``，保留既有 idle-timeout 安全网完整不变），
    只做一件事：累计 cursor 自身 tool-runtime 失败次数，并在「失败达阈值且从未有过一次
    成功工具调用」时把 :attr:`tool_layer_unavailable` 置真，让调度循环提前 fail-over。
    """

    def __init__(self, *, tool_failure_threshold: int) -> None:
        self._pending_line = ""
        self._tool_failure_threshold = max(1, int(tool_failure_threshold))
        self.tool_runtime_failures = 0
        self.tool_successes = 0

    @property
    def waiting_on_long_command(self) -> bool:
        # 刻意不抑制 idle 超时：cursor 一直靠 stdout 增长刷新 liveness，且「started 但永不
        # completed」的悬挂工具调用若抑制 idle 反而会让既有兜底失效（cursor 无 max_runtime）。
        return False

    @property
    def tool_layer_unavailable(self) -> bool:
        return (
            self.tool_runtime_failures >= self._tool_failure_threshold
            and self.tool_successes == 0
        )

    @property
    def early_fail_over_reason_code(self) -> str | None:
        if self.tool_layer_unavailable:
            return "cursor_tool_layer_unavailable"
        return None

    def ingest(self, output: str) -> None:
        self._pending_line += output
        lines = self._pending_line.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending_line = lines.pop()
        else:
            self._pending_line = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            self._ingest_line(line)

    def _ingest_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "tool_call" or payload.get("subtype") != "completed":
            return
        outcome = classify_cursor_tool_call_outcome(payload.get("tool_call"))
        if outcome == "runtime_failure":
            self.tool_runtime_failures += 1
        elif outcome == "ok":
            self.tool_successes += 1


class KimiStreamActivityMonitor:
    """kimi stream-json 的极小 monitor —— 职责与 cursor 的那个**不同**，不是它的翻版。

    kimi 的 ``{"role":"tool", ...}`` 行**没有任何成功/失败标记**（writer 只写 content），
    因此**做不了工具层 fail-over**。这是取证后的结论，不是待办：写成 TODO 会诱使后人拿
    猜测填坑。它只干两件真事：

    1. **重试耗尽**才置 :attr:`provider_usage_limit_signalled`。刻意**不**在「发生了一次
       429 重试」时就置位——``turn.step.retrying`` 按定义是重试事件，那一轮仍可能成功，
       在这里提前 fail-over 等于把可恢复重试变成硬失败腿。权威信号是 kimi 自己给的
       ``failed_attempt >= max_attempts``。
    2. 记录 ``session.resume_hint`` 是否出现。它只在成功路径写出（失败先 throw），
       因此**缺席**是「异常终止」的可靠判据；但它**不是**判决点——review-result 才是。
    """

    def __init__(self) -> None:
        self._pending_line = ""
        self.provider_usage_limit_signalled = False
        self.resume_hint_observed = False

    @property
    def waiting_on_long_command(self) -> bool:
        # 重试期间 stdout 一直在长，idle-timeout 本就不会误杀；这里没有需要抑制的长静默命令。
        return False

    @property
    def tool_layer_unavailable(self) -> bool:
        return False

    @property
    def structured_output_terminated_cleanly(self) -> bool:
        return self.resume_hint_observed

    @property
    def early_fail_over_reason_code(self) -> str | None:
        if self.provider_usage_limit_signalled:
            return "kimi_provider_usage_limit_exhausted"
        return None

    def ingest(self, output: str) -> None:
        self._pending_line += output
        lines = self._pending_line.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending_line = lines.pop()
        else:
            self._pending_line = ""
        for raw_line in lines:
            line = raw_line.strip()
            if line:
                self._ingest_line(line)

    def _ingest_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if "session" in payload and isinstance(payload.get("session"), dict):
            if payload["session"].get("resume_hint"):
                self.resume_hint_observed = True
        if payload.get("resume_hint"):
            self.resume_hint_observed = True
        if payload.get("role") != "meta":
            return
        if kimi_retry_event_exhausted_provider_usage(payload):
            self.provider_usage_limit_signalled = True


class OhMyPiJsonStreamActivityMonitor:
    """oh-my-pi 事件流的极小 monitor。

    ⚠️ **刻意不累计 ``tool_execution_end.isError``**：它并不是 cursor ``spawnError`` 的对偶。
    omp 会在**普通非零 shell exit** 与**工具参数校验错误**上同样置起它，因此「连续三次
    isError 判工具层不可用」会把三条正常失败的 shell 命令误报成 runtime 坏掉，进而杀掉一条
    健康的评审腿。这同样是取证后的结论，不是待办。

    它只干一件真事：在终局记录里读出 ``stopReason == "error"`` 且错误正文命中额度签名时置
    :attr:`provider_usage_limit_signalled`。这条之所以必要，是因为 omp 在 ``--mode json``
    下**报错也退 0** ⇒ 只认非零退出码的文本兜底永远不会触发。
    """

    def __init__(self) -> None:
        self._pending_line = ""
        self.provider_usage_limit_signalled = False
        self.terminal_turn_observed = False

    @property
    def waiting_on_long_command(self) -> bool:
        return False

    @property
    def tool_layer_unavailable(self) -> bool:
        return False

    @property
    def structured_output_terminated_cleanly(self) -> bool:
        return self.terminal_turn_observed

    @property
    def early_fail_over_reason_code(self) -> str | None:
        if self.provider_usage_limit_signalled:
            return "oh_my_pi_provider_usage_limit_exhausted"
        return None

    def ingest(self, output: str) -> None:
        self._pending_line += output
        lines = self._pending_line.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending_line = lines.pop()
        else:
            self._pending_line = ""
        for raw_line in lines:
            line = raw_line.strip()
            if line:
                self._ingest_line(line)

    def _ingest_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") in {"message_end", "turn_end", "agent_end"}:
            self.terminal_turn_observed = True
            error_text = oh_my_pi_record_error_text(payload)
            if error_text and looks_like_usage_limit(error_text):
                self.provider_usage_limit_signalled = True


class ReviewerRunResult:
    def __init__(
        self,
        *,
        args: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
        pid: int | None,
        signal: int | None = None,
        terminated_signal: int | None = None,
        timeout_reason: str | None = None,
        probe_history: list[dict[str, Any]] | None = None,
    ) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid
        self.signal = signal
        self.terminated_signal = terminated_signal
        self.timeout_reason = timeout_reason
        self.probe_history = probe_history or []


class ReviewerStructuredOutputError(ValueError):
    """结构化 stdout 里读出的、足以否定整条评审腿的 typed 错误。

    不止 codex 会有：kimi 的重试耗尽与 oh-my-pi 的 `stopReason:"error"` 都必须走这里。
    尤其 omp——它在 `--mode json` 下**报错也退 0**，若不 typed 识别，只认非零退出码的
    D2 文本兜底永远不会触发（这是「omp 比 kimi 省事」那个判断反了的根因）。
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def signal_name(signal_number: int | None) -> str | None:
    if signal_number is None:
        return None
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return str(signal_number)


def subprocess_signal(returncode: int | None) -> int | None:
    if returncode is not None and returncode < 0:
        return -returncode
    return None


def _timeout_payload_delta(payload: bytes | str | None, start: int) -> str:
    return _payload_delta(payload, start)


def run_activity_probe(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    started_at: float,
) -> dict[str, Any]:
    probe_started_at = time.monotonic()
    record: dict[str, Any] = {
        "command": command,
        "started_after_seconds": round(probe_started_at - started_at, 3),
        "timeout_seconds": timeout_seconds,
        "status": "running",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        record.update(
            {
                "status": "completed" if completed.returncode == 0 else "failed",
                "returncode": completed.returncode,
                "signal": signal_name(subprocess_signal(completed.returncode)),
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            }
        )
    except subprocess.TimeoutExpired as exc:
        record.update(
            {
                "status": "timeout",
                "returncode": None,
                "signal": None,
                "stdout": _timeout_payload_delta(exc.stdout, 0),
                "stderr": _timeout_payload_delta(exc.stderr, 0),
            }
        )
    except Exception as exc:
        record.update(
            {
                "status": "error",
                "returncode": None,
                "signal": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
            }
        )
    ended_at = time.monotonic()
    record["ended_after_seconds"] = round(ended_at - started_at, 3)
    record["duration_seconds"] = round(ended_at - probe_started_at, 3)
    return record


def next_wait_seconds(
    *,
    activity_check_interval_seconds: float,
    remaining_idle_seconds: float,
    max_runtime_remaining_seconds: float | None,
    waiting_on_long_command: bool,
    probe_retry_remaining_seconds: float | None = None,
) -> float:
    wait_candidates = [activity_check_interval_seconds]
    if not waiting_on_long_command:
        wait_candidates.append(
            probe_retry_remaining_seconds
            if probe_retry_remaining_seconds is not None
            else remaining_idle_seconds
        )
    if max_runtime_remaining_seconds is not None:
        wait_candidates.append(max(0.0, max_runtime_remaining_seconds))
    return max(0.01, min(wait_candidates))


def set_active_reviewer_process(process: subprocess.Popen[str]) -> None:
    global _ACTIVE_REVIEWER_PROCESS
    with _ACTIVE_REVIEWER_PROCESS_LOCK:
        _ACTIVE_REVIEWER_PROCESS = process


def clear_active_reviewer_process(process: subprocess.Popen[str]) -> None:
    global _ACTIVE_REVIEWER_PROCESS
    with _ACTIVE_REVIEWER_PROCESS_LOCK:
        if _ACTIVE_REVIEWER_PROCESS is process:
            _ACTIVE_REVIEWER_PROCESS = None


def terminate_active_reviewer_process() -> int | None:
    with _ACTIVE_REVIEWER_PROCESS_LOCK:
        process = _ACTIVE_REVIEWER_PROCESS
    if process is None or process.poll() is not None:
        return None
    signal_number = terminate_process_group(process)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    return signal_number


def deliver_reviewer_prompt_on_dedicated_stdin_writer_thread(
    process: subprocess.Popen[str], prompt_text: str
) -> threading.Thread:
    """在独立线程里把 prompt 写进 reviewer 的 stdin 并关闭它。

    绝不能把 prompt 交给下面 watchdog 循环里的 `communicate(input=..., timeout=...)`：那次调用
    一旦超时，CPython 会把 `_communication_started` 置位；之后循环只能传 `input=None`
    （传别的会 ValueError），而此时 `_communicate` 既不再把 stdin 注册为可写、也不会关闭
    stdin —— 没写完的 prompt 被永久丢弃，reviewer 阻塞在 `stdin.read()` 上一个字节都不产出，
    最终被空闲 watchdog 判成 `no_observable_activity` 超时。macOS 每轮 selector 只写
    `select.PIPE_BUF`（512 字节），几十 KB 的 review packet 在高负载机器上极易写不完，
    表现就是「reviewer 无缘无故超时」的负载敏感故障。

    stdin 的所有权整体移交给本线程（`process.stdin` 置 None），因此 `communicate()` 不会
    与它抢关闭。线程是 daemon：即便 reviewer 从不读取 stdin，主流程也不会被它拖住。
    """
    reviewer_stdin_stream = process.stdin
    process.stdin = None

    def write_prompt_then_close_stdin() -> None:
        try:
            if reviewer_stdin_stream is None:
                return
            if prompt_text:
                reviewer_stdin_stream.write(prompt_text)
                reviewer_stdin_stream.flush()
        except (OSError, ValueError):
            # reviewer 提前退出（BrokenPipeError）或自行关掉 stdin：prompt 投递失败本身不是
            # 判决点，交由下面的 returncode 与超时路径去定性。
            pass
        finally:
            if reviewer_stdin_stream is not None:
                try:
                    reviewer_stdin_stream.close()
                except OSError:
                    pass

    writer_thread = threading.Thread(
        target=write_prompt_then_close_stdin,
        name="rvf-reviewer-prompt-stdin-writer",
        daemon=True,
    )
    try:
        writer_thread.start()
    except BaseException:
        # start() 失败（线程资源耗尽 -> RuntimeError）时没有任何线程会跑 write_prompt_then_close_stdin，
        # 而 stdin 的所有权已经从 Popen 摘走（process.stdin = None），于是既不会被线程关闭、也不会被
        # communicate() 关闭：fd 泄漏且 reviewer 永远等不到 EOF。这里补上关闭再向上抛，交由调用方的
        # 清理路径终止子进程并清除 active 注册。
        if reviewer_stdin_stream is not None:
            try:
                reviewer_stdin_stream.close()
            except OSError:
                pass
        raise
    return writer_thread


def run_with_activity_timeout(
    command: list[str],
    *,
    input_text: str,
    cwd: Path,
    reviewer_subprocess_environment: dict[str, str],
    activity_probe_subprocess_environment: dict[str, str],
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None,
    activity_probe_timeout_seconds: float,
    activity_probe_failure_threshold: int,
    max_runtime_seconds: float | None,
    output_format: str,
    tool_failure_threshold: int = DEFAULT_CURSOR_TOOL_FAILURE_THRESHOLD,
) -> ReviewerRunResult:
    """运行外部 reviewer，并按 stdout/stderr 可观测活动刷新空闲超时。

    这里刻意不把未来的 reviewer-owned subagent 能力写进 prompt：后续如果允许
    review agent 自己再派生一层子代理，应在调度层显式建模后再开放。
    """

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True,
        env=reviewer_subprocess_environment,
        start_new_session=True,
    )
    set_active_reviewer_process(process)
    started_at = time.monotonic()
    last_liveness_at = started_at
    last_stdout_len = 0
    last_stderr_len = 0
    next_probe_at = started_at
    consecutive_probe_failures = 0
    probe_history: list[dict[str, Any]] = []
    # 两种 stream monitor 各司其职：
    #  - Claude：解析 tool_use 块，在「长静默命令」期间抑制 idle 超时（waiting_on_long_command）。
    #  - cursor：解析 tool_call(completed) 事件，统计 cursor 自身工具执行层失败；不抑制 idle，
    #    只在工具层整体坏掉时（tool_layer_unavailable）让下方循环提前 fail-over。
    #    背景见 CursorStreamActivityMonitor 与 DEFAULT_CURSOR_TOOL_FAILURE_THRESHOLD。
    #  - kimi：只认「额度类失败且重试已耗尽」，不做工具层 fail-over（kimi 的 tool 行没有成败标记）。
    #  - oh-my-pi：只认终局 stopReason:"error" 的额度签名，同样不做工具层 fail-over
    #    （它的 isError 会在普通非零 shell exit 上置起，不是 runtime 故障信号）。
    stream_monitor: (
        ClaudeStreamActivityMonitor
        | CursorStreamActivityMonitor
        | KimiStreamActivityMonitor
        | OhMyPiJsonStreamActivityMonitor
        | None
    )
    if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON:
        stream_monitor = ClaudeStreamActivityMonitor()
    elif output_format == OUTPUT_FORMAT_CURSOR_STREAM_JSON:
        stream_monitor = CursorStreamActivityMonitor(
            tool_failure_threshold=tool_failure_threshold
        )
    elif output_format == OUTPUT_FORMAT_KIMI_STREAM_JSON:
        stream_monitor = KimiStreamActivityMonitor()
    elif output_format == OUTPUT_FORMAT_OH_MY_PI_JSON:
        stream_monitor = OhMyPiJsonStreamActivityMonitor()
    else:
        stream_monitor = None

    try:
        # 必须在 try 内投递：writer thread 的 start() 可能因线程资源耗尽抛 RuntimeError，
        # 只有落在这里，下面的 except/finally 才能终止已经 Popen 的 reviewer 并清掉 active 注册。
        deliver_reviewer_prompt_on_dedicated_stdin_writer_thread(process, input_text)
        while True:
            now = time.monotonic()
            if max_runtime_seconds is not None and now - started_at >= max_runtime_seconds:
                return timeout_completed(
                    process,
                    command,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_check_interval_seconds=activity_check_interval_seconds,
                    activity_probe_command=activity_probe_command,
                    activity_probe_timeout_seconds=activity_probe_timeout_seconds,
                    activity_probe_failure_threshold=activity_probe_failure_threshold,
                    max_runtime_seconds=max_runtime_seconds,
                    reason="max_runtime_exceeded",
                    probe_history=probe_history,
                )
            idle_for = now - last_liveness_at
            remaining_idle = max(0.0, idle_timeout_seconds - idle_for)
            max_runtime_remaining = (
                max_runtime_seconds - (now - started_at)
                if max_runtime_seconds is not None
                else None
            )
            probe_retry_remaining = None
            if (
                activity_probe_command is not None
                and remaining_idle <= 0.0
                and next_probe_at > now
            ):
                probe_retry_remaining = max(0.0, next_probe_at - now)
            wait_for = next_wait_seconds(
                activity_check_interval_seconds=activity_check_interval_seconds,
                remaining_idle_seconds=remaining_idle,
                max_runtime_remaining_seconds=max_runtime_remaining,
                waiting_on_long_command=(
                    stream_monitor.waiting_on_long_command
                    if stream_monitor is not None
                    else False
                ),
                probe_retry_remaining_seconds=probe_retry_remaining,
            )

            try:
                stdout, stderr = process.communicate(timeout=wait_for)
                return ReviewerRunResult(
                    args=command,
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    pid=process.pid,
                    signal=subprocess_signal(process.returncode),
                    probe_history=probe_history,
                )
            except subprocess.TimeoutExpired as exc:
                now = time.monotonic()
                if max_runtime_seconds is not None and now - started_at >= max_runtime_seconds:
                    return timeout_completed(
                        process,
                        command,
                        idle_timeout_seconds=idle_timeout_seconds,
                        activity_check_interval_seconds=activity_check_interval_seconds,
                        activity_probe_command=activity_probe_command,
                        activity_probe_timeout_seconds=activity_probe_timeout_seconds,
                        activity_probe_failure_threshold=activity_probe_failure_threshold,
                        max_runtime_seconds=max_runtime_seconds,
                        reason="max_runtime_exceeded",
                        probe_history=probe_history,
                    )
                stdout_len = _payload_length(exc.stdout)
                stderr_len = _payload_length(exc.stderr)
                if stdout_len > last_stdout_len or stderr_len > last_stderr_len:
                    if stream_monitor is not None and stdout_len > last_stdout_len:
                        stream_monitor.ingest(_payload_delta(exc.stdout, last_stdout_len))
                    last_liveness_at = now
                    last_stdout_len = stdout_len
                    last_stderr_len = stderr_len
                    consecutive_probe_failures = 0
                    # 快速失败：monitor 判定「继续等下去也不会有合法评审」时立即终止并 fail-over，
                    # 不再等 idle_timeout 白烧（cursor 工具层整体坏掉实测可省 ~83s；kimi/omp 则是
                    # 额度耗尽——重试期间 stdout 一直在长，idle-timeout 永远不会触发）。
                    early_fail_over_reason_code = (
                        stream_monitor.early_fail_over_reason_code
                        if stream_monitor is not None
                        else None
                    )
                    if early_fail_over_reason_code is not None:
                        return timeout_completed(
                            process,
                            command,
                            idle_timeout_seconds=idle_timeout_seconds,
                            activity_check_interval_seconds=activity_check_interval_seconds,
                            activity_probe_command=activity_probe_command,
                            activity_probe_timeout_seconds=activity_probe_timeout_seconds,
                            activity_probe_failure_threshold=activity_probe_failure_threshold,
                            max_runtime_seconds=max_runtime_seconds,
                            reason=early_fail_over_reason_code,
                            probe_history=probe_history,
                        )
                    continue

                if now - last_liveness_at < idle_timeout_seconds:
                    continue
                if stream_monitor is not None and stream_monitor.waiting_on_long_command:
                    continue
                if activity_probe_command is not None:
                    if now < next_probe_at:
                        continue
                    effective_activity_probe_subprocess_environment = (
                        activity_probe_subprocess_environment.copy()
                    )
                    effective_activity_probe_subprocess_environment["RVF_REVIEWER_PID"] = str(
                        process.pid
                    )
                    probe = run_activity_probe(
                        activity_probe_command,
                        cwd=cwd,
                        env=effective_activity_probe_subprocess_environment,
                        timeout_seconds=activity_probe_timeout_seconds,
                        started_at=started_at,
                    )
                    probe_history.append(probe)
                    next_probe_at = time.monotonic() + activity_check_interval_seconds
                    if probe.get("status") == "completed" and probe.get("returncode") == 0:
                        last_liveness_at = time.monotonic()
                        consecutive_probe_failures = 0
                        continue
                    consecutive_probe_failures += 1
                    if consecutive_probe_failures < activity_probe_failure_threshold:
                        continue

                return timeout_completed(
                    process,
                    command,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_check_interval_seconds=activity_check_interval_seconds,
                    activity_probe_command=activity_probe_command,
                    activity_probe_timeout_seconds=activity_probe_timeout_seconds,
                    activity_probe_failure_threshold=activity_probe_failure_threshold,
                    max_runtime_seconds=max_runtime_seconds,
                    reason=(
                        "no_observable_activity_probe_failed"
                        if activity_probe_command is not None
                        else "no_observable_activity"
                    ),
                    probe_history=probe_history,
                )
    except BaseException:
        if process.poll() is None:
            terminate_process_group(process)
        raise
    finally:
        clear_active_reviewer_process(process)


def timeout_completed(
    process: subprocess.Popen[str],
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None,
    activity_probe_timeout_seconds: float,
    activity_probe_failure_threshold: int,
    max_runtime_seconds: float | None,
    reason: str,
    probe_history: list[dict[str, Any]],
) -> ReviewerRunResult:
    terminated_signal = terminate_process_group(process)
    stdout, stderr = process.communicate()
    timeout_parts = [
        EXTERNAL_REVIEWER_TIMEOUT_FLAG,
        f"idle_timeout_seconds={idle_timeout_seconds:g}",
        f"activity_check_interval_seconds={activity_check_interval_seconds:g}",
    ]
    if activity_probe_command is not None:
        timeout_parts.extend(
            [
                f"activity_probe_timeout_seconds={activity_probe_timeout_seconds:g}",
                f"activity_probe_failure_threshold={activity_probe_failure_threshold}",
                f"activity_probe_failures={len([item for item in probe_history if item.get('status') != 'completed' or item.get('returncode') != 0])}",
            ]
        )
    if max_runtime_seconds is not None:
        timeout_parts.append(f"max_runtime_seconds={max_runtime_seconds:g}")
    timeout_parts.append(f"reason={reason}")
    timeout_line = " ".join(timeout_parts)
    stderr = (stderr or "").rstrip()
    stderr = f"{stderr}\n{timeout_line}" if stderr else timeout_line
    return ReviewerRunResult(
        args=command,
        returncode=EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE,
        stdout=stdout,
        stderr=stderr,
        pid=process.pid,
        signal=subprocess_signal(process.returncode),
        terminated_signal=terminated_signal,
        timeout_reason=reason,
        probe_history=probe_history,
    )


def terminate_process_group(process: subprocess.Popen[str]) -> int:
    try:
        signal_number = signal.SIGKILL
        os.killpg(os.getpgid(process.pid), signal_number)
        return signal_number
    except Exception:
        process.kill()
        return signal.SIGKILL


def extract_result_event_text(output: str) -> str:
    """从 stream-json stdout 中提取最终 `{"type":"result","result":"…"}` 事件文本。

    Claude Code 与 cursor-agent 的 stream-json 终止事件形状一致
    （均为 type=="result" 且 result 为字符串），故两种 output_format 共用本提取逻辑。
    找不到 result 事件时回退到原始 stdout。
    """

    result: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "result" and isinstance(payload.get("result"), str):
            result = payload["result"]
    if result is not None:
        return result.strip()
    return output.strip()


def extract_claude_stream_result(output: str) -> str:
    """从 Claude Code stream-json stdout 中提取最终 result 文本。"""

    return extract_result_event_text(output)


def text_parts_from_content(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        for key in ("output_text", "input_text"):
            value = item.get(key)
            if isinstance(value, str):
                parts.append(value)
                break
    return parts


def message_text_from_payload(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content_text = "\n".join(text_parts_from_content(message.get("content"))).strip()
        if content_text:
            return content_text

    if payload.get("type") == "message":
        content_text = "\n".join(text_parts_from_content(payload.get("content"))).strip()
        if content_text:
            return content_text

    for key in ("text", "result", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def looks_like_codex_backend_challenge(output: str) -> bool:
    text = output.lstrip()[:8192].lower()
    if not text:
        return False
    htmlish = text.startswith("<!doctype html") or text.startswith("<html") or "<html" in text[:512]
    if not htmlish:
        return False
    challenge_markers = (
        "cloudflare",
        "cf-chl",
        "challenge-platform",
        "just a moment",
        "turnstile",
        "checking your browser",
    )
    return any(marker in text for marker in challenge_markers)


def looks_like_usage_limit(text: str, *, signatures: tuple[str, ...] | None = None) -> str | None:
    """识别 reviewer 失败输出里的「额度/配额耗尽」签名，命中返回该短语、否则 None。

    只应对**无合法 review-result 的失败输出**调用（绑 raw stderr/stdout，绝不绑 normalize 后的
    模型评审正文）；调用点见 main() 的 D2 守卫与 ``extract_codex_json_result`` 的 typed error
    事件分支。``signatures`` 可由 config 覆盖/扩充内置集。
    """
    if not text:
        return None
    lowered = text.lower()
    sigs = signatures if signatures is not None else USAGE_LIMIT_SIGNATURES
    for sig in sigs:
        if sig in lowered:
            return sig
    for sig in USAGE_LIMIT_429_SIGNATURES:
        if sig in lowered:
            return sig
    if "429" in lowered and "too many requests" in lowered:
        return "429 too many requests"
    return None


def _codex_error_text(record: dict[str, Any]) -> str | None:
    """从 codex JSONL ``error`` / ``turn.failed`` / ``thread.error`` 事件里抽出人类可读错误文本。

    兼容多种嵌套形态（顶层 ``message`` / ``error`` 字符串、``error.message``、``turn.error`` …）；
    全部抽不到时兜底序列化整条记录（bounded），保证签名扫描总有输入。
    """
    candidates: list[Any] = [record.get(key) for key in ("message", "reason", "detail")]
    err = record.get("error")
    if isinstance(err, str):
        candidates.append(err)
    elif isinstance(err, dict):
        candidates.extend(err.get(key) for key in ("message", "reason", "detail", "type", "code"))
    turn = record.get("turn")
    if isinstance(turn, dict):
        terr = turn.get("error")
        if isinstance(terr, str):
            candidates.append(terr)
        elif isinstance(terr, dict):
            candidates.extend(terr.get(key) for key in ("message", "reason", "detail"))
    parts = [c for c in candidates if isinstance(c, str) and c.strip()]
    if parts:
        return " ".join(parts)
    via_payload = message_text_from_payload(record)
    if via_payload:
        return via_payload
    try:
        return json.dumps(record, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return None


def extract_codex_json_result(output: str) -> str:
    """从 `codex exec --json` JSONL stdout 中提取最后一条 assistant 文本。"""

    result: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        record_type = record.get("type")
        payload = record.get("payload")

        # D1：codex 真实额度错误走 error / turn.failed / thread.error 事件（顶层或 event_msg /
        # item.completed 嵌套），而非 assistant 文本。命中额度签名**立即 raise**，优先于任何已累积的
        # assistant 文本——失败的 turn 不构成合法评审；非额度类错误不在此 raise，沿用既有收尾逻辑。
        error_record: dict[str, Any] | None = None
        if record_type in {"error", "turn.failed", "thread.error"}:
            error_record = record
        elif (
            record_type == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") in {"error", "turn.failed", "thread.error"}
        ):
            error_record = payload
        elif (
            record_type == "item.completed"
            and isinstance(record.get("item"), dict)
            and record["item"].get("type") in {"error", "turn.failed", "thread.error"}
        ):
            error_record = record["item"]
        if error_record is not None:
            error_text = _codex_error_text(error_record)
            if error_text and looks_like_usage_limit(error_text):
                raise ReviewerStructuredOutputError(
                    "usage_limit_exhausted",
                    f"{EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG} {error_text.strip()[:2000]}",
                )
            continue

        if record_type in {"agent_message", "assistant_message"}:
            text = message_text_from_payload(record)
            if text:
                result = text
        elif record_type == "event_msg" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type in {"agent_message", "assistant_message"}:
                text = message_text_from_payload(payload)
                if text:
                    result = text
        elif record_type == "response_item" and isinstance(payload, dict):
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = message_text_from_payload(payload)
                if text:
                    result = text
        elif record_type == "item.completed" and isinstance(record.get("item"), dict):
            item = record["item"]
            item_type = item.get("type")
            if item_type in {"agent_message", "assistant_message"}:
                text = message_text_from_payload(item)
                if text:
                    result = text
            elif item_type == "message" and item.get("role") == "assistant":
                text = message_text_from_payload(item)
                if text:
                    result = text
        elif record_type in {"result", "task_complete"}:
            text = message_text_from_payload(record)
            if text:
                result = text

    if result is not None:
        return result.strip()
    if looks_like_codex_backend_challenge(output):
        raise ReviewerStructuredOutputError(
            "codex_backend_challenge",
            (
                f"{CODEX_BACKEND_CHALLENGE_FLAG} Codex CLI returned ChatGPT/Codex "
                "backend challenge HTML instead of valid `codex exec --json` output."
            ),
        )
    return output.strip()


def kimi_retry_event_exhausted_provider_usage(record: dict[str, Any]) -> bool:
    """kimi 的 ``turn.step.retrying`` 是否代表「额度类失败且重试已耗尽」。

    两个条件都要满足才算数：① 错误是额度/429 类；② kimi 自己报告 ``failed_attempt``
    已达 ``max_attempts``。只满足①的是**可恢复重试**，判死会把整条腿连同它随后可能写出的
    合法 review-result 一起丢掉（`_leg_is_effective` 要求 returncode == 0，
    `check_review_result_artifact` 也只在 returncode == 0 时才跑）。
    """
    if record.get("type") != "turn.step.retrying":
        return False
    status_code = record.get("status_code", record.get("statusCode"))
    error_message = record.get("error_message") or record.get("errorMessage") or ""
    if not isinstance(error_message, str):
        error_message = ""
    usage_flavoured = status_code == 429 or bool(looks_like_usage_limit(error_message))
    if not usage_flavoured:
        return False
    failed_attempt = record.get("failed_attempt", record.get("failedAttempt"))
    max_attempts = record.get("max_attempts", record.get("maxAttempts"))
    if not isinstance(failed_attempt, int) or not isinstance(max_attempts, int):
        return False
    return failed_attempt >= max_attempts


def extract_kimi_stream_json_result(output: str) -> str:
    """从 kimi ``--output-format stream-json`` 的 JSONL 里提取最后一条 assistant 正文。

    kimi **没有** ``type == "result"`` 这种终止信封，所以既有的 `extract_result_event_text`
    会整份回退成 raw stdout（把整条 JSONL 当成「评审正文」落进工件）。这里改成取最后一条
    ``role == "assistant"`` 且 content 为非空字符串的行。
    """
    result: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("role") == "meta":
            if kimi_retry_event_exhausted_provider_usage(record):
                error_message = (
                    record.get("error_message") or record.get("errorMessage") or "provider usage limit"
                )
                raise ReviewerStructuredOutputError(
                    "usage_limit_exhausted",
                    f"{EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG} {str(error_message).strip()[:2000]}",
                )
            continue
        if record.get("role") != "assistant":
            continue
        content = record.get("content")
        if isinstance(content, str) and content.strip():
            result = content
        elif isinstance(content, list):
            parts = text_parts_from_content(content)
            if parts:
                result = "\n".join(parts)
    if result is not None:
        return result.strip()
    return output.strip()


def oh_my_pi_record_error_text(record: dict[str, Any]) -> str | None:
    """从 omp 的终局记录里取错误正文（``stopReason == "error"`` 才算）。"""
    candidates: list[dict[str, Any]] = [record]
    message = record.get("message")
    if isinstance(message, dict):
        candidates.append(message)
    for candidate in candidates:
        stop_reason = candidate.get("stopReason", candidate.get("stop_reason"))
        if stop_reason != "error":
            continue
        error_message = candidate.get("errorMessage") or candidate.get("error_message")
        if isinstance(error_message, str) and error_message.strip():
            return error_message
        return "oh-my-pi reported stopReason: error without an errorMessage"
    return None


def extract_oh_my_pi_json_result(output: str) -> str:
    """从 omp ``--mode json`` 的事件流里提取最后一条 assistant 正文。

    ⚠️ omp 在 ``--mode json`` 下**报错也退 0**（把 ``errorMessage → stderr → exit(1)``
    那整段包在 ``if (mode === "text")`` 里），所以只认非零退出码的文本兜底**永远不会触发**。
    额度类错误必须在这里 typed 识别并 raise，否则该腿会被当成「跑完了但没写结果」。
    """
    result: str | None = None
    fallback_from_agent_end: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record_type = record.get("type")
        if record_type in {"message_end", "turn_end", "agent_end"}:
            error_text = oh_my_pi_record_error_text(record)
            if error_text and looks_like_usage_limit(error_text):
                raise ReviewerStructuredOutputError(
                    "usage_limit_exhausted",
                    f"{EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG} {error_text.strip()[:2000]}",
                )
        if record_type in {"message_end", "turn_end"}:
            message = record.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                parts = text_parts_from_content(message.get("content"))
                if parts:
                    result = "\n".join(parts)
        elif record_type == "agent_end":
            messages = record.get("messages")
            if isinstance(messages, list) and messages:
                last = messages[-1]
                if isinstance(last, dict):
                    parts = text_parts_from_content(last.get("content"))
                    if parts:
                        fallback_from_agent_end = "\n".join(parts)
    if result is not None:
        return result.strip()
    if fallback_from_agent_end is not None:
        return fallback_from_agent_end.strip()
    return output.strip()


# 会抛 typed 错误的结构化提取器：这些格式的 stdout 里可能藏着足以否定整条腿的错误事件，
# 必须统一走同一条 raise → reason_code → 125 的路径。
STRUCTURED_OUTPUT_RESULT_EXTRACTORS = {
    OUTPUT_FORMAT_CODEX_JSON: extract_codex_json_result,
    OUTPUT_FORMAT_KIMI_STREAM_JSON: extract_kimi_stream_json_result,
    OUTPUT_FORMAT_OH_MY_PI_JSON: extract_oh_my_pi_json_result,
}


def structured_output_terminated_cleanly(output: str, output_format: str) -> bool | None:
    """结构化 stdout 是否带着「正常走完一个 turn」的终止证据。返回 None 表示该格式无此概念。

    ⚠️ 它**不是**判决点——review-result 工件才是。它只进 `reviewer.summary.json` 供取证：
    kimi 的 ``session.resume_hint`` 只在成功路径写出（失败先 throw），omp 则看有没有终局事件。
    """
    if output_format == OUTPUT_FORMAT_KIMI_STREAM_JSON:
        terminal_marker_present = False
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            session = record.get("session")
            if isinstance(session, dict) and session.get("resume_hint"):
                terminal_marker_present = True
            elif record.get("resume_hint"):
                terminal_marker_present = True
        return terminal_marker_present
    if output_format == OUTPUT_FORMAT_OH_MY_PI_JSON:
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") in {"turn_end", "agent_end"}:
                return True
        return False
    return None


def normalize_review_output(output: str) -> str:
    return output.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run configured review-validate-fix alternative reviewer.")
    parser.add_argument("--repo", help="Target git repository.")
    parser.add_argument("--session-context", help="File containing the main-agent scope-of-work / Session context block.")
    parser.add_argument("--review-packet", help="Self-contained packet generated by build_review_packet.py, including Session Context.")
    parser.add_argument("--scope-contract", help="scope.contract.json path for this review run.")
    parser.add_argument("--prompt-file", default=str(DEFAULT_PROMPT), help="Review prompt file.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Alternative reviewer config JSON.")
    parser.add_argument("--output", help="Optional file to write reviewer stdout.")
    parser.add_argument("--check", action="store_true", help="Validate config and command availability only.")
    parser.add_argument("--preflight", action="store_true", help="Validate config, command availability, and configured health command when present.")
    parser.add_argument("--health", action="store_true", help="Run the configured health command.")
    parser.add_argument("--print-label", action="store_true", help="Print configured source label.")
    parser.add_argument("--dry-run", action="store_true", help="Print command and prompt length without invoking reviewer.")
    parser.add_argument("--rvf-run-id", help="Use an existing RVF run id instead of creating a new one.")
    parser.add_argument("--rvf-run-dir", help="Use this RVF run directory instead of resolving state/runs/<run_id>.")
    parser.add_argument("--reviewer-id", help="Override the reviewer artifact id (defaults to the config label). Used by dispatch_reviewers.py to run two same-harness instances without colliding on artifacts/reviewers/<id>/.")
    args = parser.parse_args()
    ledger = start_run(
        "reviewer",
        repo=args.repo,
        cwd=args.repo,
        run_id=args.rvf_run_id,
        run_dir=Path(args.rvf_run_dir).expanduser().resolve() if args.rvf_run_dir else None,
    )
    ledger.event(
        phase="review",
        event="started",
        status="started",
        reason_code="reviewer_started",
        repo=args.repo,
        cwd=args.repo,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        ledger.event(
            phase="review",
            event="config_missing",
            status="failed",
            reason_code="reviewer_config_missing",
            paths={"config": str(config_path)},
            error=f"missing config: {config_path}",
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_config_missing",
            message=f"缺少 alternative reviewer 配置: {config_path}",
            paths={"config": str(config_path)},
        )
        return fail(f"缺少 alternative reviewer 配置: {config_path}", 2)

    try:
        config, user_local_config_override_summary = load_effective_alternative_reviewer_config(
            config_path
        )
        if config.get("enabled") is not True:
            ledger.event(
                phase="review",
                event="config_disabled",
                status="failed",
                reason_code="reviewer_config_disabled",
                paths={"config": str(config_path)},
            )
            ledger.summary(
                status="failed",
                reason_code="reviewer_config_disabled",
                message="alternative reviewer 未启用",
                paths={"config": str(config_path)},
            )
            return fail("alternative reviewer 未启用", 2)
        label = config.get("label")
        if not isinstance(label, str) or not label.startswith("alternative-reviewer:"):
            raise ValueError("label must start with alternative-reviewer:")
        command = string_list(config, "command")
        allow_repo_cwd = config.get("allow_repo_cwd", False)
        if not isinstance(allow_repo_cwd, bool):
            raise ValueError("allow_repo_cwd must be a boolean")
        env_unset = config.get("env_unset", [])
        if not isinstance(env_unset, list) or not all(isinstance(item, str) for item in env_unset):
            raise ValueError("env_unset must be a string array")
        env_set = environment_variable_mapping(config, "env_set")
        if "idle_timeout_seconds" in config:
            idle_timeout = positive_float(config, "idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
        elif "timeout_seconds" in config:
            idle_timeout = positive_float(config, "timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
        else:
            idle_timeout = DEFAULT_IDLE_TIMEOUT_SECONDS
        activity_check_interval = positive_float(
            config,
            "activity_check_interval_seconds",
            DEFAULT_ACTIVITY_CHECK_INTERVAL_SECONDS,
        )
        activity_probe_command = optional_string_list(config, "activity_probe_command")
        activity_probe_timeout = positive_float(
            config,
            "activity_probe_timeout_seconds",
            DEFAULT_ACTIVITY_PROBE_TIMEOUT_SECONDS,
        )
        activity_probe_failure_threshold = positive_int(
            config,
            "activity_probe_failure_threshold",
            DEFAULT_ACTIVITY_PROBE_FAILURE_THRESHOLD,
        )
        tool_failure_threshold = positive_int(
            config,
            "tool_failure_threshold",
            DEFAULT_CURSOR_TOOL_FAILURE_THRESHOLD,
        )
        max_runtime = optional_positive_float(
            config,
            "max_runtime_seconds",
            DEFAULT_MAX_RUNTIME_SECONDS,
        )
        health_timeout = int(positive_float(config, "health_timeout_seconds", 30.0))
        health_command = optional_string_list(config, "health_command")
        pre_run_health = config.get("pre_run_health", False)
        if not isinstance(pre_run_health, bool):
            raise ValueError("pre_run_health must be a boolean")
        if pre_run_health and health_command is None:
            raise ValueError("pre_run_health requires health_command")
        if args.repo and allow_repo_cwd:
            model_selection_cwd = Path(args.repo).expanduser().resolve()
        elif args.review_packet:
            model_selection_cwd = Path(args.review_packet).expanduser().resolve().parent
        elif args.preflight or args.health or args.check or args.print_label:
            model_selection_cwd = Path.cwd()
        else:
            model_selection_cwd = SKILL_DIR
        if not args.print_label:
            command, health_command = resolve_oh_my_pi_default_model_commands(
                config,
                command,
                health_command,
                env=build_alternative_reviewer_subprocess_environment(env_unset, env_set),
                cwd=model_selection_cwd,
                timeout=health_timeout,
            )
        health_command_cwd = (
            model_selection_cwd if config.get("reviewer_model_selection_source") else None
        )
        health_command_ledger_metadata = (
            {"command": health_command}
            if user_local_config_override_summary.get("applied") is not True
            else {}
        )
        prompt_delivery = config.get("prompt_delivery", PROMPT_DELIVERY_STDIN)
        if not isinstance(prompt_delivery, str):
            raise ValueError("prompt_delivery must be a string")
        if prompt_delivery not in SUPPORTED_PROMPT_DELIVERY_MODES:
            raise ValueError(
                "prompt_delivery must be one of: "
                f"{', '.join(sorted(SUPPORTED_PROMPT_DELIVERY_MODES))}"
            )
        prompt_flag = config.get("prompt_flag")
        if prompt_flag is not None and not (isinstance(prompt_flag, str) and prompt_flag):
            raise ValueError("prompt_flag must be a non-empty string when present")
        if prompt_delivery == PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER:
            if prompt_flag is None:
                raise ValueError(
                    "prompt_delivery="
                    f"{PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER}"
                    " requires prompt_flag"
                )
            if prompt_flag in command or any(
                item.startswith(f"{prompt_flag}=") for item in command
            ):
                raise ValueError(
                    f"prompt_flag {prompt_flag!r} must not appear in command: the prompt pointer "
                    "is appended at run time and a second occurrence would shadow it"
                )
        run_directory_grant_flag = config.get("run_directory_grant_flag")
        if run_directory_grant_flag is not None and not (
            isinstance(run_directory_grant_flag, str)
            and run_directory_grant_flag.startswith("-")
        ):
            raise ValueError(
                "run_directory_grant_flag must be a flag string starting with '-' when present"
            )
        output_format = config.get("output_format")
        if output_format is None and is_claude_print_command(command):
            cli_output_format = output_format_argument_value(command)
            if cli_output_format in {None, "stream-json"}:
                output_format = OUTPUT_FORMAT_CLAUDE_STREAM_JSON
                command = ensure_claude_stream_json_command(command)
            else:
                output_format = OUTPUT_FORMAT_TEXT
        elif output_format is None and is_cursor_print_command(command):
            # output_format_argument_value 只解析通用 --output-format flag，对 cursor 同样适用。
            cli_output_format = output_format_argument_value(command)
            if cli_output_format in {None, "stream-json"}:
                output_format = OUTPUT_FORMAT_CURSOR_STREAM_JSON
                command = ensure_cursor_stream_json_command(command)
            else:
                output_format = OUTPUT_FORMAT_TEXT
        elif output_format is None and is_codex_exec_command(command):
            output_format = OUTPUT_FORMAT_CODEX_JSON if codex_json_enabled(command) else OUTPUT_FORMAT_TEXT
        elif output_format is None and is_kimi_prompt_command(command):
            cli_output_format = output_format_argument_value(command)
            if cli_output_format in {None, "stream-json"}:
                output_format = OUTPUT_FORMAT_KIMI_STREAM_JSON
                command = ensure_kimi_stream_json_command(command)
            else:
                output_format = OUTPUT_FORMAT_TEXT
        elif output_format is None and is_oh_my_pi_print_command(command):
            output_format = OUTPUT_FORMAT_OH_MY_PI_JSON
            command = ensure_oh_my_pi_json_command(command)
        elif output_format is None:
            output_format = OUTPUT_FORMAT_TEXT
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of: {', '.join(sorted(SUPPORTED_OUTPUT_FORMATS))}"
            )
        command = normalise_reviewer_command_for_output_format(command, output_format)
        # 健康探针与派发命令必须走同一条后端路径（cursor rvf-20260811T170157Z 的教训）。
        # gate 在 run-directory 授权与 prompt 终结**之前**跑：那两步是运行期注入，
        # 两边不对称是设计使然，拿它们去比对只会得到「修对了也红」。
        health_probe_parity_exemption = config.get(HEALTH_PROBE_PARITY_EXEMPTION_CONFIG_FIELD)
        if health_probe_parity_exemption is not None and (
            health_probe_parity_exemption != HEALTH_PROBE_PARITY_EXEMPTION_INLINE_SCRIPT_FIXTURE
        ):
            raise ValueError(
                f"{HEALTH_PROBE_PARITY_EXEMPTION_CONFIG_FIELD} 只接受字面量 "
                f"{HEALTH_PROBE_PARITY_EXEMPTION_INLINE_SCRIPT_FIXTURE!r}"
            )
        health_probe_entrypoint_relationship: str | None = None
        if health_command is not None and health_probe_parity_exemption is not None:
            health_command = normalise_health_command_for_output_format(
                health_command,
                output_format,
                prompt_flag=prompt_flag,
                dispatch_signature=reviewer_dispatch_flag_multiset(
                    command,
                    prompt_flag=prompt_flag,
                    run_directory_grant_flag=run_directory_grant_flag,
                ),
            )
            health_probe_entrypoint_relationship = HEALTH_PROBE_PARITY_EXEMPTED
            health_command_ledger_metadata = (
                {
                    "command": health_command,
                    "health_probe_entrypoint_relationship": health_probe_entrypoint_relationship,
                }
                if user_local_config_override_summary.get("applied") is not True
                else {
                    "health_probe_entrypoint_relationship": health_probe_entrypoint_relationship
                }
            )
        elif health_command is not None:
            health_command = normalise_health_command_for_output_format(
                health_command,
                output_format,
                prompt_flag=prompt_flag,
                dispatch_signature=reviewer_dispatch_flag_multiset(
                    command,
                    prompt_flag=prompt_flag,
                    run_directory_grant_flag=run_directory_grant_flag,
                ),
            )
            health_probe_entrypoint_relationship = (
                assert_health_command_shares_dispatch_flag_set(
                    command,
                    health_command,
                    prompt_flag=prompt_flag,
                    run_directory_grant_flag=run_directory_grant_flag,
                )
            )
            health_command_ledger_metadata = (
                {
                    "command": health_command,
                    "health_probe_entrypoint_relationship": health_probe_entrypoint_relationship,
                }
                if user_local_config_override_summary.get("applied") is not True
                else {
                    "health_probe_entrypoint_relationship": health_probe_entrypoint_relationship
                }
            )
        if is_codex_exec_command(command):
            # codex 的位置约束（exec 之后、stdin 占位符 `-` 之前）优先于配置声明的通用形态。
            command = ensure_codex_exec_add_dir(command, ledger.run_dir)
        elif run_directory_grant_flag is not None:
            command = ensure_run_directory_grant_argument(
                command,
                ledger.run_dir,
                grant_flag=run_directory_grant_flag,
            )
    except Exception as exc:
        ledger.event(
            phase="review",
            event="config_invalid",
            status="failed",
            reason_code="reviewer_config_invalid",
            error=f"{type(exc).__name__}: {exc}",
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_config_invalid",
            message=f"alternative reviewer 配置无效: {exc}",
        )
        return fail(f"alternative reviewer 配置无效: {exc}", 2)

    if args.print_label:
        ledger.event(
            phase="review",
            event="print_label",
            status="completed",
            reason_code="print_label",
        )
        ledger.summary(status="completed", reason_code="print_label", message=label)
        print(label)
        return 0

    command_path = check_command(command)
    if command_path is None:
        ledger.event(
            phase="review",
            event="command_missing",
            status="failed",
            reason_code="reviewer_command_missing",
            command=command,
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_command_missing",
            message=f"找不到 alternative reviewer 命令: {command[0]}",
            command=command,
        )
        return fail(f"找不到 alternative reviewer 命令: {command[0]}", 2)

    reviewer_subprocess_environment = build_alternative_reviewer_subprocess_environment(
        env_unset,
        env_set,
    )
    reviewer_support_subprocess_environment = scrub_env(env_unset)
    for subprocess_environment in (
        reviewer_subprocess_environment,
        reviewer_support_subprocess_environment,
    ):
        subprocess_environment.update(ledger.env())
        subprocess_environment[SUPPRESS_STOP_HOOK_ENV] = "1"
        subprocess_environment["RVF_RUN_DIR"] = str(ledger.run_dir)
        subprocess_environment["RVF_ARTIFACTS_DIR"] = str(ledger.artifacts_dir)

    if args.check:
        ledger.event(
            phase="review",
            event="check_completed",
            status="completed",
            reason_code="reviewer_check_completed",
            command_path=command_path,
        )
        ledger.summary(
            status="completed",
            reason_code="reviewer_check_completed",
            message=f"OK {label} {command_path}",
            command=command,
            command_path=command_path,
            user_local_config_override=user_local_config_override_summary,
        )
        print(f"OK {label} {command_path}")
        return 0

    if args.preflight:
        print(f"OK {label} {command_path}")
        if health_command is None:
            ledger.event(
                phase="review",
                event="preflight_completed",
                status="completed",
                reason_code="reviewer_preflight_completed",
                command_path=command_path,
                health_configured=False,
            )
            ledger.summary(
                status="completed",
                reason_code="reviewer_preflight_completed",
                message="health command not configured",
                command=command,
                command_path=command_path,
                health_configured=False,
                user_local_config_override=user_local_config_override_summary,
            )
            print("health command not configured")
            return 0
        returncode = run_health(
            health_command,
            reviewer_support_subprocess_environment,
            health_timeout,
            cwd=health_command_cwd,
        )
        ledger.event(
            phase="review",
            event="health_completed",
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            returncode=returncode,
            **health_command_ledger_metadata,
        )
        ledger.summary(
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            message="alternative reviewer health command completed",
            returncode=returncode,
            user_local_config_override=user_local_config_override_summary,
            **health_command_ledger_metadata,
        )
        return returncode

    if args.health:
        if health_command is None:
            return fail("health command not configured", 2)
        returncode = run_health(
            health_command,
            reviewer_support_subprocess_environment,
            health_timeout,
            cwd=health_command_cwd,
        )
        ledger.event(
            phase="review",
            event="health_completed",
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            returncode=returncode,
            **health_command_ledger_metadata,
        )
        ledger.summary(
            status="completed" if returncode == 0 else "failed",
            reason_code="reviewer_health_completed" if returncode == 0 else "reviewer_health_failed",
            message="alternative reviewer health command completed",
            returncode=returncode,
            user_local_config_override=user_local_config_override_summary,
            **health_command_ledger_metadata,
        )
        return returncode

    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    session_context = Path(args.session_context).expanduser().resolve() if args.session_context else None
    review_packet = Path(args.review_packet).expanduser().resolve() if args.review_packet else None
    scope_contract = Path(args.scope_contract).expanduser().resolve() if args.scope_contract else None
    reviewer_id = safe_artifact_token(args.reviewer_id) if args.reviewer_id else reviewer_id_from_label(label)
    reviewer_dir = ledger.artifacts_dir / "reviewers" / reviewer_id
    review_result_path = reviewer_dir / "review-result.json"

    try:
        if repo is None and review_packet is None:
            raise ValueError("缺少 --repo 或 --review-packet")
        if repo is not None:
            check_repo(repo)
        if session_context is not None and not session_context.exists():
            raise ValueError(f"session context file not found: {session_context}")
        if review_packet is not None and not review_packet.exists():
            raise ValueError(f"review packet file not found: {review_packet}")
        if scope_contract is None:
            scope_contract = infer_scope_contract(review_packet)
        if scope_contract is not None and not scope_contract.exists():
            raise ValueError(f"scope contract file not found: {scope_contract}")
        scope_contract_payload = load_scope_contract(scope_contract)
        prompt = build_prompt(prompt_file, session_context, review_packet, repo, scope_contract, review_result_path)
    except Exception as exc:
        ledger.event(
            phase="review",
            event="input_invalid",
            status="failed",
            reason_code="reviewer_input_invalid",
            error=f"{type(exc).__name__}: {exc}",
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_input_invalid",
            message=str(exc),
        )
        return fail(str(exc), 2)

    if repo is not None:
        reviewer_subprocess_environment["RVF_REPO"] = str(repo)
    if session_context is not None:
        reviewer_subprocess_environment["RVF_SCOPE_OF_WORK"] = str(session_context)
        reviewer_subprocess_environment["RVF_SESSION_CONTEXT"] = str(session_context)
    if review_packet is not None:
        reviewer_subprocess_environment["RVF_REVIEW_PACKET"] = str(review_packet)
    if scope_contract is not None:
        reviewer_subprocess_environment["RVF_SCOPE_CONTRACT"] = str(scope_contract)
    reviewer_subprocess_environment["RVF_REVIEWER_ID"] = reviewer_id
    reviewer_subprocess_environment["RVF_REVIEW_RESULT"] = str(review_result_path)

    # prompt artifact 与 argv 终结必须先于 --dry-run：指针形态的 argv 里逐字带着 prompt 文件的
    # 绝对路径，只有先落盘、先终结，--dry-run 打印出来的 command 才是真实将被执行的那一条。
    # pre_run_health 仍留在 --dry-run 之后，保住「--dry-run 零 API 成本」这条出口。
    cwd = repo if repo is not None and allow_repo_cwd else (review_packet.parent if review_packet is not None else SKILL_DIR)
    try:
        prompt_path = write_child_artifact(reviewer_dir, "reviewer.prompt.txt", prompt, unique=True)
    except OSError as exc:
        ledger.event(
            phase="review",
            event="artifact_write_failed",
            status="failed",
            reason_code="reviewer_artifact_write_failed",
            error=f"{type(exc).__name__}: {exc}",
            paths={"reviewer_dir": str(reviewer_dir)},
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_artifact_write_failed",
            message=f"failed to write reviewer artifact: {exc}",
            paths={"reviewer_dir": str(reviewer_dir)},
        )
        return fail(f"failed to write reviewer artifact: {exc}", 2)

    reviewer_subprocess_environment[REVIEWER_PROMPT_FILE_ENV] = prompt_path
    try:
        command, reviewer_stdin_text = finalize_reviewer_command_for_prompt_delivery(
            command,
            prompt_delivery=prompt_delivery,
            prompt_flag=prompt_flag,
            prompt_file_path=Path(prompt_path),
            prompt_text=prompt,
        )
    except ValueError as exc:
        ledger.event(
            phase="review",
            event="config_invalid",
            status="failed",
            reason_code="reviewer_config_invalid",
            error=f"{type(exc).__name__}: {exc}",
        )
        ledger.summary(
            status="failed",
            reason_code="reviewer_config_invalid",
            message=f"alternative reviewer 配置无效: {exc}",
        )
        return fail(f"alternative reviewer 配置无效: {exc}", 2)

    if args.dry_run:
        payload = {
            "label": label,
            "command": command,
            "cwd": str(cwd),
            "prompt_chars": len(prompt),
            "prompt_delivery": prompt_delivery,
            "prompt_file": prompt_path,
            "command_argv_bytes": reviewer_command_argv_bytes(command),
            "activity_probe_configured": activity_probe_command is not None,
            "scope_contract": str(scope_contract) if scope_contract is not None else None,
            "review_result": str(review_result_path),
            "user_local_config_override": user_local_config_override_summary,
        }
        ledger.event(
            phase="review",
            event="dry_run",
            status="completed",
            reason_code="reviewer_dry_run",
            cwd=str(cwd),
            prompt_chars=len(prompt),
            prompt_delivery=prompt_delivery,
            command_argv_bytes=payload["command_argv_bytes"],
        )
        ledger.summary(
            status="completed",
            reason_code="reviewer_dry_run",
            message="alternative reviewer dry run",
            **payload,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if pre_run_health and health_command is not None:
        returncode = run_health(
            health_command,
            reviewer_support_subprocess_environment,
            health_timeout,
            emit_output=False,
            cwd=health_command_cwd,
        )
        ledger.event(
            phase="review",
            event="pre_run_health_completed",
            status="completed" if returncode == 0 else "failed",
            reason_code=(
                "reviewer_pre_run_health_completed"
                if returncode == 0
                else "reviewer_pre_run_health_failed"
            ),
            returncode=returncode,
            **health_command_ledger_metadata,
        )
        if returncode != 0:
            ledger.summary(
                status="failed",
                reason_code="reviewer_pre_run_health_failed",
                message="alternative reviewer pre-run health command failed",
                returncode=returncode,
                user_local_config_override=user_local_config_override_summary,
                **health_command_ledger_metadata,
            )
            return returncode

    lease_runtime = TrackerLeaseRuntime(
        repo=repo,
        scope_contract=scope_contract_payload,
        reviewer_id=reviewer_id,
        run_id=ledger.run_id,
    )
    lease_release_reason = "failed"
    try:
        lease_runtime.acquire()
        lease_runtime.start()
        try:
            completed = run_with_activity_timeout(
                command,
                input_text=reviewer_stdin_text,
                cwd=cwd,
                reviewer_subprocess_environment=reviewer_subprocess_environment,
                activity_probe_subprocess_environment=reviewer_support_subprocess_environment,
                idle_timeout_seconds=idle_timeout,
                activity_check_interval_seconds=activity_check_interval,
                activity_probe_command=activity_probe_command,
                activity_probe_timeout_seconds=activity_probe_timeout,
                activity_probe_failure_threshold=activity_probe_failure_threshold,
                max_runtime_seconds=max_runtime,
                output_format=output_format,
                tool_failure_threshold=tool_failure_threshold,
            )
        except Exception as exc:
            completed = ReviewerRunResult(
                args=command,
                returncode=1,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                pid=None,
            )
        lease_release_reason = (
            "failed"
            if completed.returncode != 0
            or (
                output_format == OUTPUT_FORMAT_CODEX_JSON
                and looks_like_codex_backend_challenge(completed.stdout or "")
            )
            else "completed"
        )
    finally:
        lease_runtime.stop()
        lease_runtime.release(lease_release_reason)

    raw_stdout = completed.stdout or ""
    raw_stderr = completed.stderr or ""
    stdout = raw_stdout.strip()
    output_error_reason: str | None = None
    output_error_message: str | None = None
    # 子进程真实退出码（D2 守卫）：先于任何翻码记录。文本兜底额度扫描只在它非零时触发，
    # 由此排除「returncode 0 → review-result 校验失败被翻成 1」这条会让模型评审正文进入扫描的路径。
    subprocess_returncode = completed.returncode
    if output_format == OUTPUT_FORMAT_CLAUDE_STREAM_JSON:
        stdout = extract_claude_stream_result(stdout)
    elif output_format == OUTPUT_FORMAT_CURSOR_STREAM_JSON:
        stdout = extract_result_event_text(stdout)
    elif output_format in STRUCTURED_OUTPUT_RESULT_EXTRACTORS:
        try:
            stdout = STRUCTURED_OUTPUT_RESULT_EXTRACTORS[output_format](stdout)
        except ReviewerStructuredOutputError as exc:
            output_error_reason = exc.reason_code
            output_error_message = str(exc)
            stdout = str(exc)
            if completed.returncode == 0:
                completed.returncode = 1
            raw_stderr = f"{raw_stderr.rstrip()}\n{exc}".strip()
    stdout = redact_configured_environment_variable_values(
        normalize_review_output(stdout),
        env_set,
    )
    stderr = redact_configured_environment_variable_values(
        raw_stderr.strip(),
        env_set,
    )
    timed_out = (
        completed.returncode == EXTERNAL_REVIEWER_TIMEOUT_EXIT_CODE
        and EXTERNAL_REVIEWER_TIMEOUT_FLAG in stderr
    )
    if timed_out:
        stdout = EXTERNAL_REVIEWER_TIMEOUT_FLAG

    # D2/D3/D4：文本兜底额度检测。仅在「子进程真实非零退出、尚未识别错误原因、非 timeout」时，
    # 从 raw stderr（优先）/ raw stdout 扫额度签名——绝不扫 normalize 后的模型评审正文。
    # codex_json 的 typed error 事件已在 extract_codex_json_result 中先行命中（output_error_reason
    # 已置位则跳过此处），此兜底主要覆盖 text/claude/cursor 等格式 CLI 自身崩溃时 stderr 带额度文案的情形。
    if output_error_reason is None and not timed_out and subprocess_returncode != 0:
        hit = looks_like_usage_limit(raw_stderr)
        scanned = raw_stderr
        if hit is None:
            hit = looks_like_usage_limit(raw_stdout)
            scanned = raw_stdout
        if hit is not None:
            output_error_reason = "usage_limit_exhausted"
            output_error_message = redact_configured_environment_variable_values(
                scanned.strip()[:2000],
                env_set,
            )

    # 命中额度耗尽：统一规整为专用退出码 125 + flag（对齐 timeout 124+flag 双条件先例），
    # 供 dispatch_reviewers 以「125 AND (flag in stderr OR summary.output_error_reason)」识别并回退。
    if output_error_reason == "usage_limit_exhausted":
        completed.returncode = EXTERNAL_REVIEWER_USAGE_LIMIT_EXIT_CODE
        if EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG not in raw_stderr:
            raw_stderr = f"{raw_stderr.rstrip()}\n{EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG}".strip()
        stderr = redact_configured_environment_variable_values(
            raw_stderr.strip(),
            env_set,
        )
        if output_error_message is None:
            output_error_message = stderr[:2000]
    if output_format in STRUCTURED_JSON_LINES_OUTPUT_FORMATS:
        redacted_raw_stdout = redact_configured_environment_variable_values_in_json_lines(
            raw_stdout,
            env_set,
        )
    else:
        redacted_raw_stdout = redact_configured_environment_variable_values(
            raw_stdout,
            env_set,
        )
    redacted_raw_stderr = redact_configured_environment_variable_values(raw_stderr, env_set)
    normalized_path = write_child_artifact(reviewer_dir, "reviewer.normalized.txt", stdout + ("\n" if stdout else ""), unique=True)
    stdout_path = write_child_artifact(reviewer_dir, "reviewer.stdout.txt", redacted_raw_stdout, unique=True)
    stderr_path = write_child_artifact(reviewer_dir, "reviewer.stderr.txt", redacted_raw_stderr, unique=True)
    review_result_summary: dict[str, Any] | None = None
    review_result_check_stderr = ""
    review_result_summary_path: str | None = None
    if completed.returncode == 0:
        review_result_summary, review_result_check_stderr = check_review_result_artifact(
            review_result_path,
            scope_contract,
        )
        review_result_summary_path = write_child_artifact(
            reviewer_dir,
            "review-result.summary.json",
            review_result_summary,
            unique=True,
        )
        if not review_result_summary.get("valid"):
            completed.returncode = 1
            errors = review_result_summary.get("errors")
            error_text = "; ".join(str(item) for item in errors) if isinstance(errors, list) else "invalid review result artifact"
            stderr = f"{stderr}\n{error_text}".strip()
    review_result_kind = review_result_summary.get("kind") if review_result_summary else None
    review_result_complete = review_result_kind in {"no_issues", "issues"}
    review_request_pending = review_result_kind == "request"
    probe_history_path = write_child_artifact(
        reviewer_dir,
        "reviewer.activity_probe_history.json",
        completed.probe_history,
        unique=True,
    )
    reviewer_summary_path = write_child_artifact(
        reviewer_dir,
        "reviewer.summary.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "reviewer_id": reviewer_id,
            "command": command,
            "cwd": str(cwd),
            "output_format": output_format,
            "structured_output_terminated_cleanly": structured_output_terminated_cleanly(
                raw_stdout, output_format
            ),
            "scope_contract": str(scope_contract) if scope_contract is not None else None,
            "review_packet": str(review_packet) if review_packet is not None else None,
            "session_context": str(session_context) if session_context is not None else None,
            "paths": {
                "prompt": prompt_path,
                "stdout": stdout_path,
                "stderr": stderr_path,
                "normalized": normalized_path,
                "review_result": str(review_result_path),
                "review_result_summary": review_result_summary_path,
                "activity_probe_history": probe_history_path,
            },
            "review_result_summary": review_result_summary,
            "review_result_complete": review_result_complete,
            "review_request_pending": review_request_pending,
            "output_error_reason": output_error_reason,
            "output_error_message": output_error_message,
            "user_local_config_override": user_local_config_override_summary,
        },
        unique=True,
    )
    if args.output:
        output_text = f"{EXTERNAL_REVIEWER_TIMEOUT_FLAG}\n" if timed_out else stdout + ("\n" if stdout else "")
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        print(stdout)

    paths = {
        "prompt": prompt_path,
        "stdout": stdout_path,
        "stderr": stderr_path,
        "normalized": normalized_path,
        "review_result": str(review_result_path),
        "review_result_summary": review_result_summary_path,
        "activity_probe_history": probe_history_path,
        "reviewer_summary": reviewer_summary_path,
        "reviewer_dir": str(reviewer_dir),
        "review_packet": str(review_packet) if review_packet is not None else None,
        "session_context": str(session_context) if session_context is not None else None,
        "scope_contract": str(scope_contract) if scope_contract is not None else None,
    }
    status = "completed" if completed.returncode == 0 else "failed"
    reason_code = "reviewer_completed"
    event_name = "completed"
    message = "alternative reviewer completed"
    if timed_out:
        reason_code = "reviewer_timeout"
        event_name = "failed"
        message = "alternative reviewer failed"
    elif review_result_summary is not None and not review_result_summary.get("valid"):
        reason_code = "reviewer_result_invalid"
        event_name = "failed"
        message = "alternative reviewer failed"
    elif review_request_pending:
        status = "pending"
        reason_code = "reviewer_request_pending"
        event_name = "request_pending"
        message = "alternative reviewer recorded a request"
    elif output_error_reason == "usage_limit_exhausted":
        reason_code = "reviewer_usage_limit_exhausted"
        event_name = "failed"
        message = "alternative reviewer failed: provider usage/quota limit exhausted"
    elif output_error_reason == "codex_backend_challenge":
        reason_code = "reviewer_codex_backend_challenge"
        event_name = "failed"
        message = "alternative reviewer failed before producing valid Codex JSON output"
    elif completed.returncode != 0:
        reason_code = "reviewer_failed"
        event_name = "failed"
        message = "alternative reviewer failed"
    ledger.event(
        phase="review",
        event=event_name,
        status=status,
        reason_code=reason_code,
        repo=str(repo) if repo is not None else None,
        cwd=str(cwd),
        paths={key: value for key, value in paths.items() if value},
        returncode=completed.returncode,
        pid=completed.pid,
        signal=signal_name(completed.signal),
        signal_number=completed.signal,
        terminated_signal=signal_name(completed.terminated_signal),
        terminated_signal_number=completed.terminated_signal,
        timed_out=timed_out,
        timeout_reason=completed.timeout_reason,
        review_result_valid=bool(review_result_summary and review_result_summary.get("valid")),
        review_result_kind=review_result_kind,
        review_result_complete=review_result_complete,
        review_request_pending=review_request_pending,
        review_result_check_stderr=review_result_check_stderr,
        output_error_reason=output_error_reason,
        output_error_message=output_error_message,
        activity_probe_configured=activity_probe_command is not None,
        activity_probe_failure_threshold=activity_probe_failure_threshold,
        activity_probe_history=completed.probe_history,
        output_format=output_format,
        reviewer_id=reviewer_id,
        user_local_config_override=user_local_config_override_summary,
    )
    ledger.summary(
        status=status,
        reason_code=reason_code,
        message=message,
        repo=str(repo) if repo is not None else None,
        cwd=str(cwd),
        paths={key: value for key, value in paths.items() if value},
        returncode=completed.returncode,
        pid=completed.pid,
        signal=signal_name(completed.signal),
        signal_number=completed.signal,
        terminated_signal=signal_name(completed.terminated_signal),
        terminated_signal_number=completed.terminated_signal,
        timed_out=timed_out,
        timeout_reason=completed.timeout_reason,
        review_result_valid=bool(review_result_summary and review_result_summary.get("valid")),
        review_result_kind=review_result_kind,
        review_result_complete=review_result_complete,
        review_request_pending=review_request_pending,
        review_result_summary=review_result_summary,
        review_result_check_stderr=review_result_check_stderr,
        output_error_reason=output_error_reason,
        output_error_message=output_error_message,
        activity_probe_configured=activity_probe_command is not None,
        activity_probe_command=activity_probe_command,
        activity_probe_timeout_seconds=activity_probe_timeout,
        activity_probe_failure_threshold=activity_probe_failure_threshold,
        activity_probe_history=completed.probe_history,
        output_format=output_format,
        label=label,
        reviewer_id=reviewer_id,
        user_local_config_override=user_local_config_override_summary,
    )

    if completed.returncode != 0:
        if stderr:
            print(stderr, file=sys.stderr)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
