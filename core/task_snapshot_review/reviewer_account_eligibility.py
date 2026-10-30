"""共享 reviewer 资格证据；状态查询不发送推理请求，正式调用承担最终认证核验。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from . import harness_cli_adapters as cli
from .persistent_artifacts import exclusive_lock, read_json, write_bytes, write_json
from .execution_lifecycle import ExecutionRevoked, ensure_execution_not_revoked, register_owned_process, finish_owned_process_registration

FORMAL_INVOCATION_SUCCESS_VALIDITY_SECONDS = 3600
NON_INFERENCE_OBSERVATION_VALIDITY_SECONDS = 30
ACCOUNT_STATUS_MAXIMUM_AGE_SECONDS = 60
AVAILABILITY_RECOVERY_COOLDOWN_SECONDS = 300
MAXIMUM_UNCHANGED_FAILURE_ATTEMPTS = 2
ACCOUNT_FAILURE_CATEGORIES = {'authentication', 'account_banned'}
PERSISTENT_FAILURE_CATEGORIES = ACCOUNT_FAILURE_CATEGORIES | {'usage_limit'}


class AvailabilityObservationInProgress(RuntimeError):
    pass


@contextmanager
def _bounded_availability_lock(path: Path, deadline_at: float | None):
    with ExitStack() as acquired:
        for _ in range(500):
            if deadline_at is not None and time.time() >= deadline_at:
                break
            try:
                handle = acquired.enter_context(exclusive_lock(path, blocking=False))
            except RuntimeError:
                time.sleep(0.01)
            else:
                yield handle
                return
    raise AvailabilityObservationInProgress('账户资格记录仍由其他进程持有')


def _sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reviewer_execution_environment(config: dict) -> dict:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('RVF_', 'CODEX_RVF_', 'CLAUDE_RVF_', 'KANBAN_', 'CLINE_KANBAN_'))
                   and key not in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')}
    secret_names = config.get('credential_environment_variable_names', [])
    secrets = {name: os.environ[name] for name in secret_names if name in os.environ}
    if config.get('configuration_file_path') and secret_names:
        current, _ = cli.load_effective_alternative_reviewer_config(Path(config['configuration_file_path']))
        secrets.update({name: current.get('env_set', {})[name] for name in secret_names if name in current.get('env_set', {})})
    for key in config.get('env_unset', []):
        environment.pop(key, None)
    environment.update(secrets)
    environment.update(config.get('env_set', {}))
    environment['RVF_SUPPRESS_STOP_HOOK'] = '1'
    return environment


def reviewer_credential_evidence_paths(config: dict, environment: dict) -> list[str]:
    if 'credential_evidence_paths' in config:
        return config['credential_evidence_paths']
    home = Path(environment.get('HOME') or Path.home())
    executable_name = Path(config['command'][0]).name
    paths = {
        'codex': [Path(environment.get('CODEX_HOME') or home / '.codex') / 'auth.json',
                  Path(environment.get('CODEX_HOME') or home / '.codex') / 'config.toml'],
        'claude': [Path(environment.get('CLAUDE_SECURESTORAGE_CONFIG_DIR') or environment.get('CLAUDE_CONFIG_DIR') or home / '.claude') / '.credentials.json',
                   Path(environment.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'settings.json'],
    }
    return [str(path) for path in paths.get(executable_name, [])]


def _claude_secure_storage_fingerprint(config: dict, environment: dict) -> str | None:
    if sys.platform != 'darwin' or config.get('credential_identity_source') != 'claude_secure_storage':
        return None
    directory = environment.get('CLAUDE_SECURESTORAGE_CONFIG_DIR', environment.get('CLAUDE_CONFIG_DIR', ''))
    suffix = '-' + hashlib.sha256(unicodedata.normalize('NFC', directory).encode()).hexdigest()[:8] if directory else ''
    account = environment.get('USER') or pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r'[a-zA-Z0-9._-]+', account):
        account = 'claude-code-user'
    try:
        # 与原生 CLI 的生产 secure-storage 服务绑定；只持久化摘要，绝不输出凭据正文。
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', account, '-w', '-s',
                                 'Claude Code-credentials' + suffix], env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=2)
        return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else f'keychain_exit_{result.returncode}'
    except (OSError, subprocess.SubprocessError) as error:
        return type(error).__name__


def _without_native_session_selectors(command: list[str]) -> list[str]:
    remaining = [command[0]]
    executable_name = Path(command[0]).name
    codex_exec_index = cli.codex_subcommand_index(command)
    codex_positional_argument_seen = False
    codex_session_id_pending = False
    value_selectors = {'--session', '--session-id', '--resume'}
    if executable_name == 'kimi':
        value_selectors.add('-S')
    if executable_name in {'claude', 'omp'}:
        value_selectors.add('-r')
    if executable_name == 'omp':
        value_selectors.add('--fork')
    index = 1
    while index < len(command):
        item = command[index]
        if item == '--':
            remaining.extend(command[index:index + 1] + command[index + 2:]
                             if codex_session_id_pending else command[index:])
            break
        if executable_name == 'codex' and item in cli.CODEX_GLOBAL_AND_EXEC_OPTIONS_WITH_ARGUMENT_VALUES:
            remaining.extend(command[index:index + 2])
            index += 2
            continue
        if codex_session_id_pending and not item.startswith('-'):
            codex_session_id_pending = False
            index += 1
            continue
        codex_session_subcommand = False
        if (codex_exec_index is not None and index > codex_exec_index
                and not item.startswith('-') and not codex_positional_argument_seen):
            codex_session_subcommand = item in {'resume', 'fork'}
            codex_positional_argument_seen = True
        if codex_session_subcommand:
            codex_session_id_pending = True
            index += 1
            continue
        if item in value_selectors:
            index += 1
            if index < len(command) and not command[index].startswith('-'):
                index += 1
            continue
        if (any(item.startswith(selector + '=') for selector in value_selectors if selector.startswith('--'))
                or executable_name == 'kimi' and item.startswith('-S') and len(item) > 2
                or executable_name == 'claude' and item.startswith('-r') and len(item) > 2
                or item in {'--continue', '--fork-session', '--last'}
                or executable_name in {'kimi', 'claude', 'omp'} and item == '-c'):
            index += 1
            continue
        remaining.append(item)
        index += 1
    return remaining


def _account_identity_command(command: list[str]) -> list[str]:
    command = _without_native_session_selectors(command)
    remaining = []
    index = 0
    while index < len(command):
        if command[index] == '--model' or command[index] == '-m' and Path(command[0]).name in {'codex', 'kimi'}:
            index += 2
            continue
        if not command[index].startswith('--model='):
            remaining.append(command[index])
        index += 1
    return remaining


def _identity(config: dict, environment: dict, resolved_command: list[str] | None) -> dict:
    credential_observations = {}
    for name in reviewer_credential_evidence_paths(config, environment):
        path = Path(name).expanduser()
        try:
            credential_observations[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            credential_observations[str(path)] = type(error).__name__
    relevant_environment = {name: value for name, value in environment.items()
        if name.startswith(('ANTHROPIC_', 'OPENAI_', 'KIMI_', 'CURSOR_', 'PI_', 'OMP_'))
        or name in {'HOME', 'PATH', 'XDG_CONFIG_HOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'CODEX_HOME',
                    'CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN',
                    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                    'AWS_PROFILE', 'AWS_REGION', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                    'GOOGLE_APPLICATION_CREDENTIALS', 'ANTHROPIC_VERTEX_PROJECT_ID'}
        or name in config.get('credential_environment_variable_names', []) or name in config.get('env_set', {})}
    executable = shutil.which(config['command'][0], path=environment.get('PATH'))
    try:
        executable_sha256 = hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None
    except OSError:
        executable_sha256 = None
    secure_storage_sha256 = _claude_secure_storage_fingerprint(config, environment)
    configured_credential_names = {*config.get('credential_environment_variable_names', []), *config.get('env_set', {})}
    credential_identity_observable = (any(re.fullmatch(r'[0-9a-f]{64}', value)
        and Path(name).name not in {'config.toml', 'settings.json'} for name, value in credential_observations.items())
        or bool(secure_storage_sha256 and re.fullmatch(r'[0-9a-f]{64}', secure_storage_sha256))
        or any(name.endswith(('_KEY', '_TOKEN')) and environment.get(name) for name in configured_credential_names)
        or all(config.get(field) for field in ('account_identity', 'credential_source_identity', 'route_identity')))
    account_identity = {
        'command': _account_identity_command(config['command']), 'executable': executable,
        'executable_sha256': executable_sha256, 'credential_observations': credential_observations,
        'claude_secure_storage_sha256': secure_storage_sha256,
        'execution_environment': relevant_environment,
        **{key: config.get(key) for key in ('account_identity', 'credential_source_identity', 'route_identity')},
    }
    account_sha256 = _sha256(account_identity)
    configuration = {key: value for key, value in config.items() if key not in {
        'env_set', 'credential_environment_variable_names', 'credential_environment_sha256',
        'credential_evidence_sha256', 'configuration_file_path'}}
    configuration['command'] = resolved_command or config['command']
    root = resolved_rvf_user_local_data_paths().runtime_state_root / 'reviewer-account-availability'
    configuration_paths = [str(resolved_rvf_user_local_data_paths().reviewer_registry_path)]
    if config.get('configuration_file_path'):
        configuration_paths.append(config['configuration_file_path'])
    if config.get('label', '').startswith('alternative-reviewer:'):
        configuration_paths.append(str(cli.user_local_alternative_reviewer_config_override_path(config['label'])))
    return {'identity_sha256': account_sha256,
            'configuration_scope_sha256': _sha256([account_sha256, configuration]),
            'model_scope_sha256': _sha256([account_sha256, _without_native_session_selectors(resolved_command or config['command'])]),
            'declared_model_or_quota_pool_scope_sha256s': [_sha256([account_sha256, field, config[field]]) for field in (
                'model_scope_identity', 'underlying_provider_account_quota_pool_identity') if config.get(field)],
            'evidence_path': str(root / f'{account_sha256}.json'),
            'credential_evidence_paths': sorted(credential_observations),
            'configuration_evidence_paths': configuration_paths,
            'credential_identity_observable': bool(credential_identity_observable),
            **({'account_status_evidence_path': str(Path(config['account_status_evidence_path']).expanduser())}
               if config.get('account_status_evidence_path') else {})}


def _availability_record(path: Path) -> dict:
    if not path.exists():
        return {'negative_evidence': {}, 'formal_success_by_configuration': {}, 'events': []}
    try:
        record = read_json(path)
        if not all(isinstance(record.get(key), expected) for key, expected in (
                ('negative_evidence', dict), ('formal_success_by_configuration', dict), ('events', list))):
            raise ValueError('资格记录结构无效')
        for failure in record['negative_evidence'].values():
            if (not isinstance(failure, dict) or failure.get('category') not in PERSISTENT_FAILURE_CATEGORIES
                    or type(failure.get('failure_count')) is not int or failure['failure_count'] < 1
                    or any(type(failure.get(key)) not in (int, float) or not math.isfinite(failure[key])
                           for key in ('observed_at', 'retry_not_before'))):
                raise ValueError('负面资格记录结构无效')
        for success in record['formal_success_by_configuration'].values():
            if not isinstance(success, dict) or type(success.get('observed_at')) not in (int, float):
                raise ValueError('正式调用证据结构无效')
        if 'non_inference_observation' in record:
            observation = record['non_inference_observation']
            if (not isinstance(observation, dict) or observation.get('status') not in ('unknown', 'available', 'unavailable')
                    or type(observation.get('next_check_at')) not in (int, float)):
                raise ValueError('非推理状态记录结构无效')
        return record
    except (ValueError, UnicodeError, TypeError):
        original = path.read_bytes()
        preserved = path.with_name(f'{path.stem}.unreadable-{hashlib.sha256(original).hexdigest()}.json')
        if not preserved.exists():
            write_bytes(preserved, original)
        return {'negative_evidence': {}, 'formal_success_by_configuration': {}, 'events': [],
                'preserved_unreadable_evidence': str(preserved)}


def _non_inference_command(config: dict, environment: dict) -> tuple[list[str] | None, str]:
    command = config.get('health_command')
    if config.get('pre_run_health', True) is False:
        return None, 'non_inference_check_disabled'
    if not command:
        return None, 'no_verified_non_inference_check'
    if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
        return None, 'inference_or_unverified_health_command_skipped'
    name = Path(command[0]).name
    same_cli = (shutil.which(command[0], path=environment.get('PATH'))
                == shutil.which(config['command'][0], path=environment.get('PATH')))
    if name == 'claude' and same_cli and command[1:] == ['auth', 'status', '--json']:
        return command, 'local_authentication_status'
    if name == 'codex' and same_cli and command[1:] == ['login', 'status']:
        return command, 'local_authentication_status'
    if name == 'curl':
        urls = []
        for item in command[1:]:
            if item.startswith(('http://', 'https://')):
                urls.append(item)
            elif item not in {'-f', '--fail', '-s', '--silent', '-S', '--show-error', '-fsS', '-sSf', '-sf', '-sS'}:
                break
        else:
            if len(urls) == 1:
                parsed = urlsplit(urls[0])
                if parsed.path == '/health' and not parsed.query and not parsed.username and not parsed.password:
                    # 不读取 curlrc、不跟随重定向，强制 GET、总时限及响应大小上限。
                    return [command[0], '-q', '--silent', '--max-time', '5', '--max-filesize', '65536',
                            '--request', 'GET', '--write-out', '\n%{http_code}', urls[0]], 'route_health_http_get'
    return None, 'inference_or_unverified_health_command_skipped'


def _query_non_inference_status(config: dict, environment: dict, cwd: Path | None,
                                timeout: float, run_directory: Path | None) -> dict:
    command, source = _non_inference_command(config, environment)
    if command is None:
        return {'status': 'unknown', 'reason': source}
    process = None
    registered = False
    try:
        if run_directory is not None:
            ensure_execution_not_revoked(run_directory)
        process = subprocess.Popen(command, env=environment, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        if run_directory is not None:
            register_owned_process(run_directory, process.pid, 'reviewer_non_inference_status', owns_process_group=True)
            registered = True
        # communicate 同时排空两个通道；状态查询没有输入或模型 prompt。
        stdout, stderr = process.communicate(timeout=timeout)
        if run_directory is not None:
            ensure_execution_not_revoked(run_directory)
        if source == 'route_health_http_get':
            _, _, status_code = stdout.decode(errors='replace').rpartition('\n')
            if status_code == '503':
                return {'status': 'unavailable', 'reason': 'route_health_503', 'category': 'route_unavailable'}
            if process.returncode != 0 or not status_code.isdigit() or not 200 <= int(status_code) < 300:
                return {'status': 'unknown', 'reason': 'route_status_interface_error'}
            return {'status': 'unknown', 'reason': 'route_reachable_account_not_verified'}
        if Path(command[0]).name == 'claude':
            try:
                payload = json.loads(stdout)
            except ValueError:
                payload = {}
            if isinstance(payload, dict) and payload.get('loggedIn') is False:
                return {'status': 'unavailable', 'reason': 'local_cli_not_logged_in', 'category': 'authentication'}
            if isinstance(payload, dict) and payload.get('loggedIn') is True and process.returncode == 0:
                return {'status': 'unknown', 'reason': 'local_login_does_not_verify_remote_authentication'}
        elif process.returncode != 0 and b'not logged in' in (stdout + stderr).lower():
            return {'status': 'unavailable', 'reason': 'local_cli_not_logged_in', 'category': 'authentication'}
        return {'status': 'unknown', 'reason': 'authentication_status_not_verified'}
    except ExecutionRevoked:
        raise
    except (OSError, subprocess.SubprocessError):
        return {'status': 'unknown', 'reason': 'non_inference_status_interface_error'}
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.communicate()
            if registered:
                finish_owned_process_registration(run_directory, process.pid, 'reviewer_non_inference_status')


def _cached_account_status(config: dict, now: float) -> dict:
    path = config.get('account_status_evidence_path')
    if not path:
        return {'status': 'unknown', 'reason': 'no_cached_account_status'}
    try:
        payload = read_json(Path(path).expanduser())
        for field in ('account_identity', 'credential_source_identity', 'route_identity'):
            if not config.get(field) or payload.get(field) != config[field]:
                return {'status': 'unknown', 'reason': 'cached_account_identity_mismatch'}
        if (config.get('model_scope_identity') or payload.get('model_scope_identity')) and payload.get('model_scope_identity') != config.get('model_scope_identity'):
            return {'status': 'unknown', 'reason': 'cached_model_scope_mismatch'}
        observed_at = payload.get('observed_at')
        valid_until = payload.get('valid_until')
        fresh = (type(observed_at) in (int, float) and 0 <= now - observed_at <= ACCOUNT_STATUS_MAXIMUM_AGE_SECONDS
                 and payload.get('stale') is False and (valid_until is None or type(valid_until) in (int, float) and now < valid_until))
        quota = payload.get('quota') if isinstance(payload.get('quota'), dict) else {}
        if any(key in quota and (type(quota[key]) not in (int, float) or not math.isfinite(quota[key]))
               for key in ('remaining', 'reset_at', 'observed_at')):
            quota = {'stale': True}
        result = {'status': 'unknown', 'reason': 'cached_account_status_stale' if not fresh else 'cached_account_status',
                  'stale': not fresh, 'observed_at': observed_at,
                  'quota': {key: quota[key] for key in ('remaining', 'reset_at', 'observed_at', 'stale') if key in quota}}
        if not fresh:
            return result
        status = payload.get('status')
        if isinstance(status, str) and status in PERSISTENT_FAILURE_CATEGORIES:
            return {**result, 'status': 'unavailable', 'category': status}
        if status == 'available':
            quota_observed_at = quota.get('observed_at', observed_at)
            reset_at = quota.get('reset_at')
            quota_fresh = (quota.get('stale', False) is False and type(quota_observed_at) in (int, float)
                           and 0 <= now - quota_observed_at <= ACCOUNT_STATUS_MAXIMUM_AGE_SECONDS
                           and (reset_at is None or reset_at > now))
            if quota:
                result['quota']['stale'] = not quota_fresh
            if (quota_fresh and type(quota.get('remaining')) in (int, float) and quota['remaining'] <= 0
                    and (reset_at is None or isinstance(reset_at, (int, float)) and reset_at > now)):
                return {**result, 'status': 'unavailable', 'category': 'usage_limit', 'reason': 'fresh_quota_exhausted'}
            return {**result, 'status': 'available', 'reason': 'fresh_bound_account_status',
                    'quota_available': quota_fresh and type(quota.get('remaining')) in (int, float) and quota['remaining'] > 0}
        return result
    except (OSError, ValueError, UnicodeError, TypeError):
        return {'status': 'unknown', 'reason': 'cached_account_status_unavailable'}


def _applicable_reviewer_account_failures(record: dict, identity: dict) -> list[tuple[str, dict]]:
    model_scopes = {identity['model_scope_sha256']}
    pool_scopes = set(identity['declared_model_or_quota_pool_scope_sha256s'])
    applicable = {}
    # 声明只能扩展已有故障的约束，不能让同一实际模型脱离原故障与恢复预算。
    while True:
        previous_count = len(applicable)
        for scope, failure in record['negative_evidence'].items():
            if scope == 'account':
                applicable[scope] = failure
            elif (model_scopes.intersection(failure.get('model_scope_sha256s', [scope]))
                    or pool_scopes.intersection(failure.get('declared_model_or_quota_pool_scope_sha256s', []))):
                applicable[scope] = failure
                model_scopes.update(failure.get('model_scope_sha256s', [scope]))
                pool_scopes.update(failure.get('declared_model_or_quota_pool_scope_sha256s', []))
        if len(applicable) == previous_count:
            break
    for scope in applicable:
        _invalidate_formal_success_evidence(record, scope)
    return list(applicable.items())


def _invalidate_formal_success_evidence(record: dict, failure_scope: str) -> None:
    failure = record['negative_evidence'][failure_scope]
    record['formal_success_by_configuration'] = {
        key: value for key, value in record['formal_success_by_configuration'].items()
        if failure_scope != 'account'
        and value.get('model_scope_sha256') not in failure.get('model_scope_sha256s', [failure_scope])
        and not set(value.get('declared_model_or_quota_pool_scope_sha256s', [])).intersection(
            failure.get('declared_model_or_quota_pool_scope_sha256s', []))}


def reviewer_eligibility(config: dict, *, environment: dict | None = None, cwd: Path | None = None,
                         deadline_at: float | None = None, run_directory: Path | None = None,
                         reserve_recovery_for: str | None = None, resolved_command: list[str] | None = None,
                         locked_pre_invocation_attempt_directory: Path | None = None) -> dict:
    environment = reviewer_execution_environment(config) if environment is None else environment
    identity = _identity(config, environment, resolved_command)
    path = Path(identity['evidence_path'])
    now = time.time()
    result = {**identity, 'status': 'unknown', 'can_dispatch': True, 'reason': 'no_formal_success_evidence'}
    if not config.get('enabled', True):
        return {**result, 'status': 'unavailable', 'can_dispatch': False, 'reason': 'configuration_disabled'}
    if deadline_at is not None and deadline_at <= now:
        return {**result, 'status': 'unavailable', 'can_dispatch': False, 'reason': 'availability_observation_deadline_exhausted'}
    try:
        with _bounded_availability_lock(path.with_suffix('.lock'), deadline_at):
            record = _availability_record(path)
            before = json.dumps(record, sort_keys=True)
            check_budget = None
            status_check_configuration_sha256 = _sha256([config.get('health_command'), config.get('pre_run_health')])
            if run_directory is not None and deadline_at is not None:
                budgets = record.setdefault('non_inference_check_budgets_by_run', {})
                check_budget = budgets.setdefault(_sha256([str(run_directory), deadline_at, status_check_configuration_sha256]), {
                    'attempt_count': 0, 'maximum_attempts': math.ceil(max(0, deadline_at - now) / NON_INFERENCE_OBSERVATION_VALIDITY_SECONDS) + 1,
                    'deadline_at': deadline_at})
            observation = record.get('non_inference_observation', {})
            observation_expired = (now >= observation.get('next_check_at', 0)
                                   or observation.get('configuration_sha256') != status_check_configuration_sha256)
            status_check_available = check_budget is None or check_budget['attempt_count'] < check_budget['maximum_attempts']
            if observation_expired and status_check_available:
                timeout = min(5, max(0.01, float(config.get('health_timeout_seconds', 5))),
                              max(0.01, deadline_at - now) if deadline_at is not None else 5)
                checked = _query_non_inference_status(config, environment, cwd, timeout, run_directory)
                if check_budget is not None:
                    check_budget['attempt_count'] += 1
                same_unavailable = checked['status'] == 'unavailable' and checked.get('reason') == observation.get('reason')
                unchanged_checks = observation.get('unchanged_unavailable_check_count', 0) + 1 if same_unavailable else int(checked['status'] == 'unavailable')
                observation = {**checked, 'observed_at': now, 'unchanged_unavailable_check_count': unchanged_checks,
                    'configuration_sha256': status_check_configuration_sha256,
                    'next_check_at': now + (AVAILABILITY_RECOVERY_COOLDOWN_SECONDS if checked['status'] == 'unavailable'
                                            else NON_INFERENCE_OBSERVATION_VALIDITY_SECONDS)}
                record['non_inference_observation'] = observation
            elif observation_expired:
                observation = {**observation, 'status': 'unknown', 'stale': True,
                               'reason': 'non_inference_status_budget_exhausted'}
                result.update(can_dispatch=False, reason=observation['reason'],
                              recovery={'trigger': 'credentials_or_bound_account_status_changed',
                                        'action': 'resume_same_run', 'maximum_unchanged_retries': 0})
            result['non_inference_observation'] = observation
            account_status = _cached_account_status(config, now)
            result['account_status_observation'] = account_status
            if account_status['status'] == 'unavailable':
                category = account_status['category']
                quota_failures = [(key, failure) for key, failure in _applicable_reviewer_account_failures(record, identity) if key != 'account']
                scope = 'account' if category in ACCOUNT_FAILURE_CATEGORIES else (
                    quota_failures[0][0] if quota_failures else identity['model_scope_sha256'])
                fingerprint = _sha256([category, account_status.get('quota')])
                fingerprints = record.setdefault('cached_account_failure_fingerprints_by_scope', {})
                previous = record['negative_evidence'].get(scope, {})
                if (fingerprints.get(scope) != fingerprint or category == 'usage_limit'
                        and identity['model_scope_sha256'] not in previous.get('model_scope_sha256s', [])):
                    failure = {**previous, 'category': 'account_banned' if previous.get('category') == 'account_banned' else category,
                        'observed_at': max(account_status['observed_at'], previous.get('observed_at', 0)),
                        'failure_count': max(1, previous.get('failure_count', 0)),
                        'evidence_path': result['account_status_evidence_path'],
                        'retry_not_before': max(now + AVAILABILITY_RECOVERY_COOLDOWN_SECONDS,
                            account_status.get('quota', {}).get('reset_at') or 0)}
                    if category == 'usage_limit':
                        reported_models = set(previous.get('model_scope_sha256s', [scope] if previous else []))
                        reported_pools = set(previous.get('declared_model_or_quota_pool_scope_sha256s', []))
                        if identity['model_scope_sha256'] not in reported_models:
                            reported_pools.update(identity['declared_model_or_quota_pool_scope_sha256s'])
                        reported_models.add(identity['model_scope_sha256'])
                        failure.update(model_scope_sha256s=sorted(reported_models),
                                       declared_model_or_quota_pool_scope_sha256s=sorted(reported_pools))
                    record['negative_evidence'][scope] = failure
                    _applicable_reviewer_account_failures(record, identity)
                    _invalidate_formal_success_evidence(record, scope)
                    record['events'].append({'event': 'cached_account_status_failed', 'scope': scope, **failure})
                    fingerprints[scope] = fingerprint
                elif account_status['observed_at'] > previous.get('observed_at', 0):
                    previous['observed_at'] = account_status['observed_at']
                    record['events'].append({'event': 'cached_account_status_reconfirmed', 'scope': scope,
                                             'observed_at': previous['observed_at']})
            active_failures = _applicable_reviewer_account_failures(record, identity)
            recovery_candidates = []
            for key, failure in active_failures:
                quota_recovery_sample_covers_failure_scope = (not config.get('model_scope_identity')
                    or set(failure.get('model_scope_sha256s', [key])) == {identity['model_scope_sha256']})
                if (account_status['status'] == 'available'
                        and account_status.get('observed_at', 0) > failure['observed_at']
                        and (failure['category'] != 'usage_limit' or account_status.get('quota_available')
                             and account_status.get('quota', {}).get('observed_at', 0) > failure['observed_at']
                             and quota_recovery_sample_covers_failure_scope)):
                    record['events'].append({'event': 'account_status_recovered', 'observed_at': account_status['observed_at'], 'scope': key})
                    del record['negative_evidence'][key]
                    record.get('cached_account_failure_fingerprints_by_scope', {}).pop(key, None)
                else:
                    # 调用方持有原尝试的执行锁；启动证据存在后仍禁止重放该认领。
                    resuming_own_claim = (locked_pre_invocation_attempt_directory is not None
                        and failure.get('recovery_attempt_claim') == str(locked_pre_invocation_attempt_directory)
                        and not (locked_pre_invocation_attempt_directory / 'model-invocation.json').exists())
                    retry_available = (now >= failure['retry_not_before']
                        and failure['category'] != 'account_banned'
                        and failure['failure_count'] < MAXIMUM_UNCHANGED_FAILURE_ATTEMPTS
                        and (not failure.get('recovery_attempt_claim') or resuming_own_claim))
                    if retry_available:
                        recovery_candidates.append(failure)
                    if not retry_available:
                        result.update(status='unavailable', can_dispatch=False, reason='persistent_' + failure['category'])
                    else:
                        result['reason'] = 'bounded_formal_recovery_available'
                    result['recovery'] = {'trigger': 'credentials_or_bound_account_status_changed', 'action': 'resume_same_run',
                        'maximum_unchanged_retries': 1 if failure['category'] != 'account_banned' and (retry_available or not failure.get('recovery_attempt_claim')
                            and failure['failure_count'] < MAXIMUM_UNCHANGED_FAILURE_ATTEMPTS) else 0,
                        **({'retry_not_before_epoch_ms': failure['retry_not_before'] * 1000}
                           if failure['category'] != 'account_banned' and not failure.get('recovery_attempt_claim') and failure['failure_count'] < MAXIMUM_UNCHANGED_FAILURE_ATTEMPTS else {})}
            if observation['status'] == 'unavailable' or account_status['status'] == 'unavailable':
                unavailable = account_status if account_status['status'] == 'unavailable' else observation
                result.update(status='unavailable', can_dispatch=False, reason=unavailable['reason'])
                status_recheck_available = check_budget is None or check_budget['attempt_count'] < check_budget['maximum_attempts']
                result['recovery'] = {'trigger': 'credentials_or_bound_account_status_changed', 'action': 'resume_same_run',
                    'maximum_unchanged_retries': int(status_recheck_available),
                    **({'retry_not_before_epoch_ms': observation['next_check_at'] * 1000}
                       if status_recheck_available else {})}
            if (not result['can_dispatch'] and config.get('credential_identity_source') == 'claude_secure_storage'
                    and run_directory is not None and deadline_at is not None
                    and check_budget['attempt_count'] < check_budget['maximum_attempts']):
                # Keychain 没有可由所属执行读取的文本凭据文件，按原时限有界复核摘要。
                next_check_at = record.get('credential_identity_recheck_not_before', 0)
                if next_check_at <= now:
                    next_check_at = now + AVAILABILITY_RECOVERY_COOLDOWN_SECONDS
                    record['credential_identity_recheck_not_before'] = next_check_at
                recovery = result.setdefault('recovery', {'trigger': 'credentials_or_bound_account_status_changed', 'action': 'resume_same_run'})
                recovery['maximum_unchanged_retries'] = 1
                recovery['retry_not_before_epoch_ms'] = min(next_check_at, deadline_at,
                    recovery.get('retry_not_before_epoch_ms', float('inf')) / 1000) * 1000
            if result['can_dispatch'] and reserve_recovery_for:
                for failure in recovery_candidates:
                    if not failure.get('recovery_attempt_claim'):
                        failure['recovery_attempt_claim'] = reserve_recovery_for
                        failure['recovery_attempt_claimed_at'] = now
            success = record['formal_success_by_configuration'].get(identity['configuration_scope_sha256'])
            if (result['can_dispatch'] and identity['credential_identity_observable']
                    and not _applicable_reviewer_account_failures(record, identity) and success
                    and 0 <= now - success['observed_at'] < FORMAL_INVOCATION_SUCCESS_VALIDITY_SECONDS):
                result.update(status='available', reason='recent_formal_invocation_success', formal_success=success)
            if json.dumps(record, sort_keys=True) != before or not path.exists():
                write_json(path, record)
            return result
    except AvailabilityObservationInProgress:
        return {**result, 'can_dispatch': False, 'reason': 'account_availability_observation_in_progress',
                'recovery': {'trigger': 'account_availability_lock_released', 'action': 'resume_same_run',
                             'maximum_unchanged_retries': 1, 'retry_not_before_epoch_ms': (now + 5) * 1000}}
    except ExecutionRevoked:
        raise
    except OSError as error:
        return {**result, 'reason': 'account_availability_storage_unknown', 'storage_error': type(error).__name__}


def record_formal_invocation_result(eligibility: dict, *, started_at: float, success: bool = False,
                                    failure_category: str | None = None, evidence_path: str) -> None:
    if not success and failure_category not in PERSISTENT_FAILURE_CATEGORIES:
        return
    path = Path(eligibility['evidence_path'])
    with exclusive_lock(path.with_suffix('.lock')):
        record = _availability_record(path)
        invocation_identity = _sha256([evidence_path, started_at, failure_category if not success else 'success'])
        processed = record.setdefault('processed_invocation_evidence', [])
        if invocation_identity in processed:
            return
        now = time.time()
        applicable = _applicable_reviewer_account_failures(record, eligibility)
        if failure_category in PERSISTENT_FAILURE_CATEGORIES:
            prior_failures = [(key, failure) for key, failure in applicable
                              if (key == 'account') == (failure_category in ACCOUNT_FAILURE_CATEGORIES)]
            scope = 'account' if failure_category in ACCOUNT_FAILURE_CATEGORIES else (
                prior_failures[0][0] if prior_failures else eligibility['model_scope_sha256'])
            previous = max((failure for _, failure in prior_failures), key=lambda failure: failure['failure_count'], default={})
            failure = {'category': 'account_banned' if previous.get('category') == 'account_banned' else failure_category,
                       'observed_at': now, 'evidence_path': evidence_path,
                       'retry_not_before': now + AVAILABILITY_RECOVERY_COOLDOWN_SECONDS,
                       'failure_count': previous.get('failure_count', 0) + 1}
            if failure_category == 'usage_limit':
                reported_models = {model for key, prior in prior_failures for model in prior.get('model_scope_sha256s', [key])}
                reported_pools = {pool for _, prior in prior_failures for pool in prior.get('declared_model_or_quota_pool_scope_sha256s', [])}
                reported_pools.update(eligibility['declared_model_or_quota_pool_scope_sha256s'])
                reported_models.add(eligibility['model_scope_sha256'])
                failure.update(model_scope_sha256s=sorted(reported_models),
                               declared_model_or_quota_pool_scope_sha256s=sorted(reported_pools))
                for key, _ in prior_failures:
                    record['negative_evidence'].pop(key, None)
            record['negative_evidence'][scope] = failure
            _invalidate_formal_success_evidence(record, scope)
            record['events'].append({'event': 'formal_invocation_failed', 'scope': scope, **failure})
        elif success:
            if all(started_at > failure['observed_at'] for _, failure in applicable):
                for scope, _ in applicable:
                    record['negative_evidence'].pop(scope, None)
                    record.get('cached_account_failure_fingerprints_by_scope', {}).pop(scope, None)
                record['formal_success_by_configuration'][eligibility['configuration_scope_sha256']] = {
                    'observed_at': now, 'valid_until': now + FORMAL_INVOCATION_SUCCESS_VALIDITY_SECONDS,
                    'model_scope_sha256': eligibility['model_scope_sha256'],
                    'declared_model_or_quota_pool_scope_sha256s': eligibility['declared_model_or_quota_pool_scope_sha256s'],
                    'evidence_path': evidence_path}
                record['events'].append({'event': 'formal_invocation_succeeded', 'observed_at': now, 'evidence_path': evidence_path})
        processed.append(invocation_identity)
        write_json(path, record)
