"""复用 dd4fa 的 CLI 投递与结构化输出适配；不携带旧 run/tracker 编排。"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from .kimi_latest_stable_model_selection import resolve_latest_kimi_managed_stable_model
REVIEWER_PROMPT_FILE_POINTER_INSTRUCTION_TEMPLATE = '请先完整读取 {prompt_file_path} 中的评审指令，再执行该文件要求。'
EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG = 'RVF_EXTERNAL_REVIEWER_USAGE_LIMIT'
CODEX_BACKEND_CHALLENGE_FLAG = 'RVF_CODEX_BACKEND_CHALLENGE'
USAGE_LIMIT_SIGNATURES: tuple[str, ...] = ('usage limit', 'rate limit', 'quota exceeded', 'insufficient_quota', 'out of credits', 'plan limit', 'too many requests', 'resource_exhausted', 'rate_limit_exceeded')
USAGE_LIMIT_429_SIGNATURES: tuple[str, ...] = ('http 429', 'status 429', '429 too many requests')
PROMPT_DELIVERY_STDIN = 'stdin'
PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER = 'argv_prompt_flag_with_prompt_file_pointer'
PROMPT_DELIVERY_ARGV_POSITIONAL_PROMPT_FILE_REFERENCE = 'argv_positional_prompt_file_reference'
OUTPUT_FORMAT_TEXT = 'text'
OUTPUT_FORMAT_CLAUDE_STREAM_JSON = 'claude_stream_json'
OUTPUT_FORMAT_CODEX_JSON = 'codex_json'
OUTPUT_FORMAT_CURSOR_STREAM_JSON = 'cursor_stream_json'
OUTPUT_FORMAT_KIMI_STREAM_JSON = 'kimi_stream_json'
OUTPUT_FORMAT_OH_MY_PI_JSON = 'oh_my_pi_json'
USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_RELATIVE_DIRECTORY = Path('.rvf') / 'alternative-reviewer-config-overrides'
USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_SCHEMA_VERSION = 1
USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_ALLOWED_FIELDS = {'env_set', 'health_command', 'health_timeout_seconds', 'pre_run_health'}
STRUCTURED_REVIEWER_OUTPUT_PROTOCOL_CONTROL_FIELD_NAMES = {'event', 'kind', 'name', 'role', 'status', 'subtype', 'type'}

def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding='utf-8') as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError('config root must be a JSON object')
    return config

def environment_variable_mapping(config: dict[str, Any], key: str) -> dict[str, str]:
    configured_environment_variables = config.get(key, {})
    if not isinstance(configured_environment_variables, dict):
        raise ValueError(f'{key} must be a string-to-string object')
    validated_environment_variables: dict[str, str] = {}
    for variable_name, variable_value in configured_environment_variables.items():
        if not isinstance(variable_name, str) or not variable_name or '=' in variable_name or ('\x00' in variable_name):
            raise ValueError(f'{key} variable names must be non-empty environment variable names')
        if not isinstance(variable_value, str) or '\x00' in variable_value:
            raise ValueError(f'{key} values must be strings without NUL characters')
        validated_environment_variables[variable_name] = variable_value
    return validated_environment_variables

def redact_configured_environment_variable_values(text: str, configured_environment_variables: dict[str, str]) -> str:
    redacted_text = text
    variables_by_descending_value_length = sorted(configured_environment_variables.items(), key=lambda item: len(item[1]), reverse=True)
    for variable_name, variable_value in variables_by_descending_value_length:
        if not variable_value:
            continue
        redacted_text = redacted_text.replace(variable_value, f'<redacted-env-value:{variable_name}>')
    return redacted_text

def redact_configured_environment_variable_values_in_json_value(json_value: Any, configured_environment_variables: dict[str, str], *, containing_field_name: str | None=None) -> Any:
    if isinstance(json_value, str):
        if containing_field_name in STRUCTURED_REVIEWER_OUTPUT_PROTOCOL_CONTROL_FIELD_NAMES:
            return json_value
        return redact_configured_environment_variable_values(json_value, configured_environment_variables)
    if isinstance(json_value, list):
        return [redact_configured_environment_variable_values_in_json_value(item, configured_environment_variables, containing_field_name=containing_field_name) for item in json_value]
    if isinstance(json_value, dict):
        return {field_name: redact_configured_environment_variable_values_in_json_value(field_value, configured_environment_variables, containing_field_name=str(field_name)) for field_name, field_value in json_value.items()}
    return json_value

def redact_configured_environment_variable_values_in_json_lines(text: str, configured_environment_variables: dict[str, str]) -> str:
    if not configured_environment_variables:
        return text
    redacted_lines: list[str] = []
    for line in text.splitlines():
        try:
            parsed_line = json.loads(line)
        except json.JSONDecodeError:
            redacted_lines.append(redact_configured_environment_variable_values(line, configured_environment_variables))
            continue
        redacted_lines.append(json.dumps(redact_configured_environment_variable_values_in_json_value(parsed_line, configured_environment_variables), ensure_ascii=False))
    redacted_text = '\n'.join(redacted_lines)
    if text.endswith(('\n', '\r')):
        redacted_text += '\n'
    return redacted_text

def user_local_alternative_reviewer_config_override_path(reviewer_label: str, *, user_home: Path | None=None) -> Path:
    reviewer_identifier = reviewer_id_from_label(reviewer_label)
    return (user_home if user_home is not None else Path.home()) / USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_RELATIVE_DIRECTORY / f'{reviewer_identifier}.json'

def load_effective_alternative_reviewer_config(bundled_config_path: Path, *, user_home: Path | None=None) -> tuple[dict[str, Any], dict[str, Any]]:
    bundled_config = load_config(bundled_config_path)
    reviewer_label = bundled_config.get('label')
    if not isinstance(reviewer_label, str) or not reviewer_label.startswith('alternative-reviewer:'):
        return (bundled_config, {'applied': False, 'fields': [], 'env_set_names': []})
    override_path = user_local_alternative_reviewer_config_override_path(reviewer_label, user_home=user_home)
    if not override_path.exists():
        return (bundled_config, {'applied': False, 'fields': [], 'env_set_names': []})
    user_local_override = load_config(override_path)
    if type(user_local_override.get('schema_version')) is not int or user_local_override.get('schema_version') != USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_SCHEMA_VERSION:
        raise ValueError('user-local alternative reviewer override schema_version must be 1')
    if user_local_override.get('reviewer_label') != reviewer_label:
        raise ValueError('user-local alternative reviewer override reviewer_label must match bundled config label')
    metadata_fields = {'schema_version', 'reviewer_label'}
    unsupported_fields = sorted(set(user_local_override) - metadata_fields - USER_LOCAL_ALTERNATIVE_REVIEWER_CONFIG_OVERRIDE_ALLOWED_FIELDS)
    if unsupported_fields:
        raise ValueError('user-local alternative reviewer override contains unsupported fields: ' + ', '.join(unsupported_fields))
    applied_fields = sorted(set(user_local_override) - metadata_fields)
    if not applied_fields:
        raise ValueError('user-local alternative reviewer override must configure at least one supported field')
    override_env_set = environment_variable_mapping(user_local_override, 'env_set')
    effective_config = dict(bundled_config)
    for field_name in applied_fields:
        effective_config[field_name] = user_local_override[field_name]
    return (effective_config, {'applied': True, 'fields': applied_fields, 'env_set_names': sorted(override_env_set)})

def is_claude_print_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == 'claude' and any((item in {'-p', '--print'} for item in command))

def is_cursor_print_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == 'cursor-agent' and any((item in {'-p', '--print'} for item in command))

def is_kimi_prompt_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == 'kimi'

def is_oh_my_pi_print_command(command: list[str]) -> bool:
    return bool(command) and Path(command[0]).name == 'omp' and any((item in {'-p', '--print', '--mode'} or item.startswith('--mode=') for item in command))

def codex_subcommand_index(command: list[str]) -> int | None:
    if not command or Path(command[0]).name != 'codex':
        return None
    options_with_values = {'-a', '--add-dir', '--ask-for-approval', '-c', '-C', '--cd', '--config', '--disable', '--enable', '-i', '--image', '--local-provider', '-m', '--model', '-p', '--profile', '--remote', '--remote-auth-token-env', '-s', '--sandbox'}
    index = 1
    while index < len(command):
        item = command[index]
        if item in {'exec', 'e'}:
            return index
        if item == '--':
            return None
        if item in options_with_values:
            index += 2
            continue
        if item.startswith('--') and '=' in item:
            index += 1
            continue
        if item.startswith('-'):
            index += 1
            continue
        return None
    return None

def is_codex_exec_command(command: list[str]) -> bool:
    return codex_subcommand_index(command) is not None

def ensure_claude_stream_json_command(command: list[str]) -> list[str]:
    patched = list(command)
    equals_index = next((index for index, item in enumerate(patched) if item.startswith('--output-format=')), None)
    if equals_index is not None:
        patched[equals_index] = '--output-format=stream-json'
    elif '--output-format' in patched:
        index = patched.index('--output-format')
        if index + 1 < len(patched):
            patched[index + 1] = 'stream-json'
        else:
            patched.append('stream-json')
    else:
        patched.extend(['--output-format', 'stream-json'])
    if '--include-hook-events' not in patched:
        patched.append('--include-hook-events')
    if '--include-partial-messages' not in patched:
        patched.append('--include-partial-messages')
    if '--verbose' not in patched:
        patched.append('--verbose')
    if '--disable-slash-commands' not in patched:
        patched.append('--disable-slash-commands')
    return patched

def ensure_cursor_stream_json_command(command: list[str]) -> list[str]:
    patched = list(command)
    if not any((item in {'-p', '--print'} for item in patched)):
        patched.append('-p')
    equals_index = next((index for index, item in enumerate(patched) if item.startswith('--output-format=')), None)
    if equals_index is not None:
        patched[equals_index] = '--output-format=stream-json'
    elif '--output-format' in patched:
        index = patched.index('--output-format')
        if index + 1 < len(patched):
            patched[index + 1] = 'stream-json'
        else:
            patched.append('stream-json')
    else:
        patched.extend(['--output-format', 'stream-json'])
    return patched

def ensure_kimi_stream_json_command(command: list[str]) -> list[str]:
    """保证 `--output-format stream-json`。

    ⚠️ 绝不注入 `-y/--yolo` 或 `--auto`：kimi 的 prompt 模式**隐含** forceAuto，
    显式加上反而与 `-p` 互斥报错。也绝不注入 `-p`——它的值是运行期才生成的指针文本，
    由 `finalize_reviewer_command_for_prompt_delivery` 追加。
    """
    patched = list(command)
    equals_index = next((index for index, item in enumerate(patched) if item.startswith('--output-format=')), None)
    if equals_index is not None:
        patched[equals_index] = '--output-format=stream-json'
    elif '--output-format' in patched:
        index = patched.index('--output-format')
        if index + 1 < len(patched):
            patched[index + 1] = 'stream-json'
        else:
            patched.append('stream-json')
    else:
        patched.extend(['--output-format', 'stream-json'])
    return patched

def ensure_oh_my_pi_json_command(command: list[str]) -> list[str]:
    """保证 print 模式 + `--mode json`，并保住「评审腿不得递归触发 RVF」这条硬约束。

    `--no-extensions` 是 omp 侧等价于 codex `--disable hooks` 的东西：omp 的 Stop 语义完全
    活在进程内的 TS extension 里，reviewer 子进程一旦加载它们，就会在评审腿里再起一轮 RVF。
    `RVF_SUPPRESS_STOP_HOOK=1` 是第二道保险，不是它的替代品。
    """
    patched = list(command)
    if not any((item in {'-p', '--print'} for item in patched)):
        patched.append('-p')
    equals_index = next((index for index, item in enumerate(patched) if item.startswith('--mode=')), None)
    if equals_index is not None:
        patched[equals_index] = '--mode=json'
    elif '--mode' in patched:
        index = patched.index('--mode')
        if index + 1 < len(patched):
            patched[index + 1] = 'json'
        else:
            patched.append('json')
    else:
        patched.extend(['--mode', 'json'])
    if '--no-extensions' not in patched:
        patched.append('--no-extensions')
    return patched

def codex_hooks_disabled(command: list[str]) -> bool:
    for index, item in enumerate(command):
        if item == '--disable' and index + 1 < len(command) and (command[index + 1] == 'hooks'):
            return True
        if item == '--disable=hooks':
            return True
        if item == '-c' and index + 1 < len(command) and (command[index + 1] == 'features.hooks=false'):
            return True
        if item == '-c=features.hooks=false':
            return True
    return False

def ensure_codex_hooks_disabled_command(command: list[str]) -> list[str]:
    exec_index = codex_subcommand_index(command)
    if exec_index is None or codex_hooks_disabled(command):
        return list(command)
    patched = list(command)
    patched[exec_index:exec_index] = ['--disable', 'hooks']
    return patched

def ensure_codex_json_command(command: list[str]) -> list[str]:
    patched = list(command)
    if '--json' not in patched:
        patched.append('--json')
    if '-' not in patched:
        patched.append('-')
    return patched

def ensure_run_directory_grant_argument(command: list[str], directory: Path, *, grant_flag: str, scan_from_index: int=1, insert_before_token: str | None=None) -> list[str]:
    """幂等地保证 argv 里带上「把 RVF run directory 授权成子进程可写目录」的参数。

    这不是 codex 专属需求：reviewer 要写的 `$RVF_REVIEW_RESULT` 落在 run directory 里、
    位于目标仓库之外，因此凡是带工作目录沙箱的 reviewer CLI 都必须显式授权它。
    codex / kimi / oh-my-pi 三家的 flag 名恰好都是 `--add-dir`，但位置约束不同：
    codex 必须插在 `exec` 子命令之后、stdin 占位符 `-` 之前，故位置约束由调用方
    通过 scan_from_index / insert_before_token 表达，而不是写死在本函数里。
    """
    directory_arg = str(directory)
    patched = list(command)
    equals_prefix = f'{grant_flag}='
    index = max(scan_from_index, 0)
    while index < len(patched):
        item = patched[index]
        if item == grant_flag and index + 1 < len(patched):
            if Path(patched[index + 1]).expanduser() == directory:
                return patched
            index += 2
            continue
        if item.startswith(equals_prefix):
            if Path(item.split('=', 1)[1]).expanduser() == directory:
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
    return ensure_run_directory_grant_argument(command, directory, grant_flag='--add-dir', scan_from_index=exec_index + 1, insert_before_token='-')

def build_reviewer_prompt_file_pointer_instruction(prompt_file_path: Path) -> str:
    """生成投给 argv 的短指针指令（绝对路径逐字写入，避免 reviewer 读不到 env 时无路可走）。"""
    return REVIEWER_PROMPT_FILE_POINTER_INSTRUCTION_TEMPLATE.format(prompt_file_path=prompt_file_path)

def finalize_reviewer_command_for_prompt_delivery(command: list[str], *, prompt_delivery: str, prompt_flag: str | None, prompt_file_path: Path, prompt_text: str) -> tuple[list[str], str]:
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
        return (list(command), prompt_text)
    if prompt_delivery == PROMPT_DELIVERY_ARGV_PROMPT_FLAG_WITH_PROMPT_FILE_POINTER:
        if not prompt_flag:
            raise ValueError(f'prompt_delivery={prompt_delivery} requires a non-empty prompt_flag')
        pointer_instruction = build_reviewer_prompt_file_pointer_instruction(prompt_file_path)
        return ([*command, prompt_flag, pointer_instruction], '')
    if prompt_delivery == PROMPT_DELIVERY_ARGV_POSITIONAL_PROMPT_FILE_REFERENCE:
        return ([*command, f'@{prompt_file_path}'], '')
    raise ValueError(f'unsupported prompt_delivery: {prompt_delivery}')

def normalise_reviewer_command_for_output_format(argv: list[str], output_format: str) -> list[str]:
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

def resolve_reviewer_model_commands(config: dict[str, Any], command: list[str], health_command: list[str] | None, *, env: dict[str, str], cwd: Path, timeout: int) -> tuple[list[str], list[str] | None]:
    source = config.get('reviewer_model_selection_source')
    if source != 'kimi_latest_managed_stable_model':
        return resolve_oh_my_pi_default_model_commands(config, command, health_command, env=env, cwd=cwd, timeout=timeout)
    if not is_kimi_prompt_command(command) or not health_command or (not is_kimi_prompt_command(health_command)):
        raise ValueError('Kimi 动态选模要求正式评审和健康检查都使用 kimi')

    def extract_native_resume_selector(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
        selector_tokens: list[str] = []
        canonical_selector: list[str] = []
        without_resume = [argv[0]]
        index = 1
        while index < len(argv):
            item = argv[index]
            if item in {'-c', '--continue'}:
                selector_tokens.append(item)
                canonical_selector.append('--continue')
            elif item in {'-S', '--session'}:
                selector_tokens.append(item)
                canonical_selector.append('--session')
                if index + 1 < len(argv) and (not argv[index + 1].startswith('-')):
                    index += 1
                    selector_tokens.append(argv[index])
                    canonical_selector.append(argv[index])
            elif item.startswith('--session=') or (item.startswith('-S') and len(item) > 2):
                selector_tokens.append(item)
                canonical_selector.extend(['--session', item.split('=', 1)[1] if item.startswith('--session=') else item[2:]])
            else:
                without_resume.append(item)
            index += 1
        return (selector_tokens, canonical_selector, without_resume)
    resume_tokens, resume_selector, _ = extract_native_resume_selector(command)
    _, health_resume_selector, health_without_resume = extract_native_resume_selector(health_command)
    if health_resume_selector and health_resume_selector != resume_selector:
        raise ValueError('Kimi 正式评审与健康检查的原生恢复会话不一致')
    if resume_tokens:
        health_command = [*health_without_resume, *resume_tokens]

    def extract_explicit_model_selector(argv: list[str]) -> tuple[str | None, list[str]]:
        selectors = []
        without_model = [argv[0]]
        index = 1
        while index < len(argv):
            item = argv[index]
            if item in {'-m', '--model'}:
                if index + 1 >= len(argv) or not argv[index + 1] or argv[index + 1].startswith('-'):
                    raise ValueError('Kimi 显式模型参数缺少模型值')
                selectors.append(argv[index + 1])
                index += 2
                continue
            elif item.startswith('--model=') or (item.startswith('-m') and len(item) > 2):
                selectors.append(item.split('=', 1)[1] if item.startswith('--model=') else item[2:])
            else:
                without_model.append(item)
            index += 1
        if len(selectors) > 1 or selectors == ['']:
            raise ValueError('Kimi 显式模型参数为空或重复')
        return (selectors[0] if selectors else None, without_model)
    selected_model, command_without_model = extract_explicit_model_selector(command)
    health_model, health_without_model = extract_explicit_model_selector(health_command)
    if selected_model is None and health_model is not None:
        raise ValueError('Kimi 健康检查不能单独覆盖正式评审的模型')
    if selected_model is not None and health_model not in {None, selected_model}:
        raise ValueError('Kimi 正式评审与健康检查的显式模型不一致')
    if selected_model is None:
        if resume_tokens:
            return (command, health_command)
        selected_model = resolve_latest_kimi_managed_stable_model(command[0], env=env, timeout=min(timeout, 10))
    return ([command[0], '--model', selected_model, *command_without_model[1:]], [health_command[0], '--model', selected_model, *health_without_model[1:]])

class ReviewerStructuredOutputError(ValueError):
    """结构化 stdout 里读出的、足以否定整条评审腿的 typed 错误。

    不止 codex 会有：kimi 的重试耗尽与 oh-my-pi 的 `stopReason:"error"` 都必须走这里。
    尤其 omp——它在 `--mode json` 下**报错也退 0**，若不 typed 识别，只认非零退出码的
    D2 文本兜底永远不会触发（这是「omp 比 kimi 省事」那个判断反了的根因）。
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code

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
        if payload.get('type') == 'result' and isinstance(payload.get('result'), str):
            result = payload['result']
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
        text = item.get('text')
        if isinstance(text, str):
            parts.append(text)
            continue
        for key in ('output_text', 'input_text'):
            value = item.get(key)
            if isinstance(value, str):
                parts.append(value)
                break
    return parts

def message_text_from_payload(payload: dict[str, Any]) -> str | None:
    message = payload.get('message')
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content_text = '\n'.join(text_parts_from_content(message.get('content'))).strip()
        if content_text:
            return content_text
    if payload.get('type') == 'message':
        content_text = '\n'.join(text_parts_from_content(payload.get('content'))).strip()
        if content_text:
            return content_text
    for key in ('text', 'result', 'output'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

def looks_like_codex_backend_challenge(output: str) -> bool:
    text = output.lstrip()[:8192].lower()
    if not text:
        return False
    htmlish = text.startswith('<!doctype html') or text.startswith('<html') or '<html' in text[:512]
    if not htmlish:
        return False
    challenge_markers = ('cloudflare', 'cf-chl', 'challenge-platform', 'just a moment', 'turnstile', 'checking your browser')
    return any((marker in text for marker in challenge_markers))

def looks_like_usage_limit(text: str, *, signatures: tuple[str, ...] | None=None) -> str | None:
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
    if '429' in lowered and 'too many requests' in lowered:
        return '429 too many requests'
    return None

def _codex_error_text(record: dict[str, Any]) -> str | None:
    """从 codex JSONL ``error`` / ``turn.failed`` / ``thread.error`` 事件里抽出人类可读错误文本。

    兼容多种嵌套形态（顶层 ``message`` / ``error`` 字符串、``error.message``、``turn.error`` …）；
    全部抽不到时兜底序列化整条记录（bounded），保证签名扫描总有输入。
    """
    candidates: list[Any] = [record.get(key) for key in ('message', 'reason', 'detail')]
    err = record.get('error')
    if isinstance(err, str):
        candidates.append(err)
    elif isinstance(err, dict):
        candidates.extend((err.get(key) for key in ('message', 'reason', 'detail', 'type', 'code')))
    turn = record.get('turn')
    if isinstance(turn, dict):
        terr = turn.get('error')
        if isinstance(terr, str):
            candidates.append(terr)
        elif isinstance(terr, dict):
            candidates.extend((terr.get(key) for key in ('message', 'reason', 'detail')))
    parts = [c for c in candidates if isinstance(c, str) and c.strip()]
    if parts:
        return ' '.join(parts)
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
        record_type = record.get('type')
        payload = record.get('payload')
        error_record: dict[str, Any] | None = None
        if record_type in {'error', 'turn.failed', 'thread.error'}:
            error_record = record
        elif record_type == 'event_msg' and isinstance(payload, dict) and (payload.get('type') in {'error', 'turn.failed', 'thread.error'}):
            error_record = payload
        elif record_type == 'item.completed' and isinstance(record.get('item'), dict) and (record['item'].get('type') in {'error', 'turn.failed', 'thread.error'}):
            error_record = record['item']
        if error_record is not None:
            error_text = _codex_error_text(error_record)
            if error_text and looks_like_usage_limit(error_text):
                raise ReviewerStructuredOutputError('usage_limit_exhausted', f'{EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG} {error_text.strip()[:2000]}')
            continue
        if record_type in {'agent_message', 'assistant_message'}:
            text = message_text_from_payload(record)
            if text:
                result = text
        elif record_type == 'event_msg' and isinstance(payload, dict):
            payload_type = payload.get('type')
            if payload_type in {'agent_message', 'assistant_message'}:
                text = message_text_from_payload(payload)
                if text:
                    result = text
        elif record_type == 'response_item' and isinstance(payload, dict):
            if payload.get('type') == 'message' and payload.get('role') == 'assistant':
                text = message_text_from_payload(payload)
                if text:
                    result = text
        elif record_type == 'item.completed' and isinstance(record.get('item'), dict):
            item = record['item']
            item_type = item.get('type')
            if item_type in {'agent_message', 'assistant_message'}:
                text = message_text_from_payload(item)
                if text:
                    result = text
            elif item_type == 'message' and item.get('role') == 'assistant':
                text = message_text_from_payload(item)
                if text:
                    result = text
        elif record_type in {'result', 'task_complete'}:
            text = message_text_from_payload(record)
            if text:
                result = text
    if result is not None:
        return result.strip()
    if looks_like_codex_backend_challenge(output):
        raise ReviewerStructuredOutputError('codex_backend_challenge', f'{CODEX_BACKEND_CHALLENGE_FLAG} Codex CLI returned ChatGPT/Codex backend challenge HTML instead of valid `codex exec --json` output.')
    return output.strip()

def kimi_retry_event_exhausted_provider_usage(record: dict[str, Any]) -> bool:
    """kimi 的 ``turn.step.retrying`` 是否代表「额度类失败且重试已耗尽」。

    两个条件都要满足才算数：① 错误是额度/429 类；② kimi 自己报告 ``failed_attempt``
    已达 ``max_attempts``。只满足①的是**可恢复重试**，判死会把整条腿连同它随后可能写出的
    合法 review-result 一起丢掉（`_leg_is_effective` 要求 returncode == 0，
    `check_review_result_artifact` 也只在 returncode == 0 时才跑）。
    """
    if record.get('type') != 'turn.step.retrying':
        return False
    status_code = record.get('status_code', record.get('statusCode'))
    error_message = record.get('error_message') or record.get('errorMessage') or ''
    if not isinstance(error_message, str):
        error_message = ''
    usage_flavoured = status_code == 429 or bool(looks_like_usage_limit(error_message))
    if not usage_flavoured:
        return False
    failed_attempt = record.get('failed_attempt', record.get('failedAttempt'))
    max_attempts = record.get('max_attempts', record.get('maxAttempts'))
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
        if record.get('role') == 'meta':
            if kimi_retry_event_exhausted_provider_usage(record):
                error_message = record.get('error_message') or record.get('errorMessage') or 'provider usage limit'
                raise ReviewerStructuredOutputError('usage_limit_exhausted', f'{EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG} {str(error_message).strip()[:2000]}')
            continue
        if record.get('role') != 'assistant':
            continue
        content = record.get('content')
        if isinstance(content, str) and content.strip():
            result = content
        elif isinstance(content, list):
            parts = text_parts_from_content(content)
            if parts:
                result = '\n'.join(parts)
    if result is not None:
        return result.strip()
    return output.strip()

def oh_my_pi_record_error_text(record: dict[str, Any]) -> str | None:
    """从 omp 的终局记录里取错误正文（``stopReason == "error"`` 才算）。"""
    candidates: list[dict[str, Any]] = [record]
    message = record.get('message')
    if isinstance(message, dict):
        candidates.append(message)
    for candidate in candidates:
        stop_reason = candidate.get('stopReason', candidate.get('stop_reason'))
        if stop_reason != 'error':
            continue
        error_message = candidate.get('errorMessage') or candidate.get('error_message')
        if isinstance(error_message, str) and error_message.strip():
            return error_message
        return 'oh-my-pi reported stopReason: error without an errorMessage'
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
        record_type = record.get('type')
        if record_type in {'message_end', 'turn_end', 'agent_end'}:
            error_text = oh_my_pi_record_error_text(record)
            if error_text and looks_like_usage_limit(error_text):
                raise ReviewerStructuredOutputError('usage_limit_exhausted', f'{EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG} {error_text.strip()[:2000]}')
        if record_type in {'message_end', 'turn_end'}:
            message = record.get('message')
            if isinstance(message, dict) and message.get('role') == 'assistant':
                parts = text_parts_from_content(message.get('content'))
                if parts:
                    result = '\n'.join(parts)
        elif record_type == 'agent_end':
            messages = record.get('messages')
            if isinstance(messages, list) and messages:
                last = messages[-1]
                if isinstance(last, dict):
                    parts = text_parts_from_content(last.get('content'))
                    if parts:
                        fallback_from_agent_end = '\n'.join(parts)
    if result is not None:
        return result.strip()
    if fallback_from_agent_end is not None:
        return fallback_from_agent_end.strip()
    return output.strip()

def structured_output_terminated_cleanly(output: str, output_format: str) -> bool | None:
    """结构化 stdout 是否带着正常终止证据；None 表示纯文本无此概念。"""
    records = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    if output_format == OUTPUT_FORMAT_KIMI_STREAM_JSON:
        for record in records:
            session = record.get('session')
            if record.get('type') == 'session.resume_hint' or record.get('resume_hint'):
                return True
            if isinstance(session, dict) and session.get('resume_hint'):
                return True
        return False
    if output_format == OUTPUT_FORMAT_OH_MY_PI_JSON:
        terminated = False
        for record in records:
            if record.get('type') not in {'turn_end', 'agent_end'}:
                continue
            if oh_my_pi_record_error_text(record):
                return False
            terminated = True
        return terminated
    if output_format == OUTPUT_FORMAT_CODEX_JSON:
        terminated = None
        for record in records:
            record_type = record.get('type')
            payload = record.get('payload')
            if record_type == 'event_msg' and isinstance(payload, dict):
                record_type = payload.get('type')
            if record_type == 'turn.completed':
                terminated = True
            elif record_type in {'error', 'turn.failed', 'thread.error'}:
                terminated = False
        return terminated if terminated is not None else False
    if output_format in {OUTPUT_FORMAT_CLAUDE_STREAM_JSON, OUTPUT_FORMAT_CURSOR_STREAM_JSON}:
        terminated = False
        for record in records:
            if record.get('type') == 'result':
                terminated = record.get('subtype') == 'success' and record.get('is_error') is False
        return terminated
    return None

def resolve_oh_my_pi_default_model_commands(config: dict[str, Any], command: list[str], health_command: list[str] | None, *, env: dict[str, str], cwd: Path, timeout: int) -> tuple[list[str], list[str] | None]:
    source = config.get('reviewer_model_selection_source')
    if source is None:
        return (command, health_command)
    if source != 'oh_my_pi_default_model_role':
        raise ValueError('未知 reviewer_model_selection_source')
    if not is_oh_my_pi_print_command(command) or not health_command or (not is_oh_my_pi_print_command(health_command)):
        raise ValueError('OMP 默认选模要求正式评审和健康检查都使用 omp')
    if any((item == '--model' or item.startswith('--model=') for item in command + health_command)):
        raise ValueError('动态默认选模不能同时配置固定 --model')
    try:
        completed = subprocess.run([command[0], 'config', 'get', 'modelRoles', '--json'], check=False, capture_output=True, text=True, env=env, cwd=cwd, timeout=min(timeout, 5))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'无法查询 OMP 默认模型: {type(exc).__name__}') from exc
    if completed.returncode != 0:
        raise ValueError(f'OMP 默认模型查询失败: exit {completed.returncode}')
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('OMP 默认模型查询未返回有效 JSON') from exc
    roles = payload.get('value') if isinstance(payload, dict) and payload.get('key') == 'modelRoles' else None
    selector = roles.get('default') if isinstance(roles, dict) else None
    if not isinstance(selector, str) or not re.fullmatch('openai-codex/[A-Za-z0-9][A-Za-z0-9._-]*(?::(?:off|minimal|low|medium|high|xhigh|max|ultra|inherit))?', selector):
        raise ValueError('OMP 默认模型必须明确指定 openai-codex 的单一模型；该评审腿不可用')
    return ([command[0], '--model', selector, *command[1:]], [health_command[0], '--model', selector, *health_command[1:]])

def safe_artifact_token(value: str) -> str:
    token = "".join(char if char.isalnum() or char in "._-" else "-" for char in value.strip())
    return token.strip("-")[:80] or "reviewer"

def reviewer_id_from_label(label: str) -> str:
    if label.startswith("alternative-reviewer:"):
        label = label.split(":", 1)[1]
    return safe_artifact_token(label)
