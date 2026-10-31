"""复用已核验观察，按项保存 RVF 决策事实；不替 Agent 作风险判断。"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
import uuid
from pathlib import Path, PurePosixPath

from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from .persistent_artifacts import exclusive_lock, git, git_text, read_json, repository_identity, write_json
from .task_snapshot import capture_worktree_tree, normalized_scope_paths


RECOVERY_CONDITION = '来源或内容观察变化，或下一次真实用户消息时有界重验；重复 Stop 不重试'
GROUP_LABELS = {'implementation_behavior': '实现／行为配置', 'tests_fixtures': '测试与 fixtures',
                'documentation': '文档', 'other': '其他'}
DECISION_FACT_IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def decision_state_path(identity: dict, task_id: str | None) -> Path:
    key = hashlib.sha256((identity['git_directory'] + '\0' + (task_id or '')).encode()).hexdigest()
    return resolved_rvf_user_local_data_paths().runtime_state_root / 'task-review-decisions' / f'{key}.json'


def _file_purpose(path: str) -> str:
    relative = PurePosixPath(path.lower())
    parts, name = relative.parts, relative.name
    if (set(parts) & {'skills', 'hooks', 'prompts', 'agents', 'commands', '.codex', '.claude', '.kimi'}
            or name in {'agents.md', 'claude.md', 'skill.md'}):
        return 'implementation_behavior'
    if (set(parts) & {'tests', 'test', 'fixtures', '__tests__', '__fixtures__'}
            or name.startswith('test_') or '.test.' in name or '.spec.' in name):
        return 'tests_fixtures'
    if relative.suffix in {'.md', '.mdx', '.rst'} or name.startswith(('readme', 'changelog', 'license')):
        return 'documentation'
    if (relative.suffix in {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.sh', '.bash', '.zsh',
                           '.rs', '.go', '.c', '.h', '.cpp', '.java', '.kt', '.swift', '.rb', '.php',
                           '.html', '.css', '.scss', '.vue', '.svelte', '.sql', '.json', '.yaml', '.yml',
                           '.toml', '.ini', '.conf', '.cfg', '.xml', '.env'}
            or name in {'makefile', 'dockerfile', '.gitignore', '.gitattributes'}):
        return 'implementation_behavior'
    return 'other'


def _parse_numstat(output: bytes) -> list[dict]:
    tokens = output.decode('utf-8', errors='surrogateescape').split('\0')
    files, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.strip('\n'):
            continue
        token = token.lstrip('\n')
        additions, deletions, path = token.split('\t', 2)
        previous_path = None
        if not path:
            previous_path, path = tokens[index:index + 2]
            index += 2
        binary = additions == '-' and deletions == '-'
        files.append({'path': path, 'previous_path': previous_path, 'binary': binary,
                      'additions': 0 if binary else int(additions), 'deletions': 0 if binary else int(deletions)})
    return files


def _totals(files: list[dict]) -> dict:
    return {'file_count': len(files), 'additions': sum(item['additions'] for item in files),
            'deletions': sum(item['deletions'] for item in files),
            'binary_file_count': sum(item['binary'] for item in files)}


def _inside_scope(path: str, scope_paths: list[str]) -> bool:
    return any(parent == '.' or path == parent or path.startswith(parent.rstrip('/') + '/') for parent in scope_paths)


def _workspace_statistics(run_git, head: str, tree: str, scopes: list[str], explicit_scope: bool) -> dict:
    if not head or not tree:
        raise ValueError('尚无完整 HEAD 与工作树内容观察')
    files = _parse_numstat(run_git('diff', '--numstat', '-z', '--find-renames', '--no-ext-diff',
                                  '--no-textconv', head, tree, '--'))
    for item in files:
        category = _file_purpose(item['path'])
        if item['previous_path'] and _file_purpose(item['previous_path']) == 'implementation_behavior':
            category = 'implementation_behavior'
        item['purpose_hint'] = category
        item['in_task_scope'] = (_inside_scope(item['path'], scopes)
            or bool(item['previous_path'] and _inside_scope(item['previous_path'], scopes))) if explicit_scope else None
    return {'basis': 'HEAD 到完整工作树的净内容差异；暂存与未暂存合并计一次，包含未跟踪文件',
            'classification_notice': '分组只提示用途，Agent 仍须核对语义；不推断 session/hunk 所有权',
            'total': _totals(files), 'files': files,
            'groups': {group: _totals([item for item in files if item['purpose_hint'] == group]) for group in GROUP_LABELS},
            'task_scope': _totals([item for item in files if item['in_task_scope']]) if explicit_scope else None,
            'background_wip': _totals([item for item in files if not item['in_task_scope']]) if explicit_scope else None,
            'background_partition_status': 'verified_paths' if explicit_scope else 'unknown_without_explicit_scope'}


def _valid_commit_statistics(item: object, sha: str, *, require_merge_flag: bool = True) -> bool:
    return (isinstance(item, dict) and item.get('commitSha') == sha
        and all(type(item.get(key)) is int and item[key] >= 0 for key in ('additions', 'deletions'))
        and (not require_merge_flag or type(item.get('isMergeCommit')) is bool))


def _history_statistics(run_git, evidence: dict, sources: dict, prior: dict) -> dict:
    commits = evidence.get('history_task_owned_commit_shas', evidence.get('task_owned_commit_shas', []))
    if evidence.get('status') != 'verified' or evidence.get('committed_work_status') == 'unknown':
        raise ValueError('任务绑定、固定起点或提交归属尚未证实')
    if any(not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', sha) for sha in commits):
        raise ValueError('任务提交 SHA 损坏')
    history, snapshot = sources.get('retained_committed_history') or {}, sources.get('workspace_snapshot') or {}
    statistics = sources.get('commit_statistics_by_sha') or {}
    summary, retained = history.get('summary') or {}, history.get('commits')
    non_merge_owned = {sha for sha in commits if not
                       (_valid_commit_statistics(statistics.get(sha), sha) and statistics[sha]['isMergeCommit'])}
    if (summary.get('status') == 'complete' and isinstance(retained, list)
            and snapshot.get('headCommit') == evidence.get('observed_head')
            and sources.get('observed_base_ref_tip') and snapshot.get('baseRefTipCommit') == sources['observed_base_ref_tip']
            and all(isinstance(item, dict) and re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', str(item.get('commitSha', '')))
                    and _valid_commit_statistics(item, item['commitSha'], require_merge_flag=False) for item in retained)
            and len({item['commitSha'] for item in retained}) == len(retained)
            and non_merge_owned <= {item['commitSha'] for item in retained}
            and summary.get('commitCount') == len(retained)
            and summary.get('additions') == sum(item['additions'] for item in retained)
            and summary.get('deletions') == sum(item['deletions'] for item in retained)):
        return {'basis': 'task_card_retained_committed_history', 'commit_count': len(retained),
                'additions': summary['additions'], 'deletions': summary['deletions'],
                'commits': [{**item, 'isMergeCommit': False} for item in retained],
                'history_version': summary.get('historyVersion')}
    known = {item['commitSha']: item for item in prior.get('commits', [])
             if isinstance(item, dict) and _valid_commit_statistics(item, item.get('commitSha'))}
    known.update({sha: item for sha, item in statistics.items() if _valid_commit_statistics(item, sha)})
    missing = [sha for sha in commits if sha not in known]
    for offset in range(0, len(missing), 64):
        output = run_git('show', '--no-walk=unsorted',
            '--format=%x00RVF_DECISION_COMMIT_STATISTICS%x00%H%x00%P%x00', '--numstat', '-z',
            '--find-renames', '--no-ext-diff', '--no-textconv', *missing[offset:offset + 64], '--')
        for chunk in output.split(b'\0RVF_DECISION_COMMIT_STATISTICS\0')[1:]:
            sha_bytes, parents, numstat = chunk.split(b'\0', 2)
            sha = sha_bytes.decode('ascii')
            files = _parse_numstat(numstat.lstrip(b'\n'))
            known[sha] = {'commitSha': sha, 'isMergeCommit': len(parents.split()) > 1,
                          'additions': sum(item['additions'] for item in files),
                          'deletions': sum(item['deletions'] for item in files)}
    if any(sha not in known for sha in commits):
        raise ValueError('精确任务提交统计不完整，不能把缺失材料计为零')
    selected = [known[sha] for sha in commits if not known[sha]['isMergeCommit']]
    return {'basis': 'fixed_task_window_commit_sum', 'commit_count': len(selected),
            'additions': sum(item['additions'] for item in selected),
            'deletions': sum(item['deletions'] for item in selected), 'commits': selected,
            'scope_notice': '原固定起点内已证实的任务提交逐个累计；不是当前净 diff，也不补猜缺失的更早 incarnation'}


def _load_cached_packet(directory: Path) -> dict:
    try:
        pointer = read_json(directory / 'decision-facts.json')
        artifact = Path(pointer['artifact_path'])
        if artifact.parent.resolve() != (directory / 'observations').resolve():
            return {}
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != pointer['sha256']:
            return {}
        packet = read_json(artifact)
        return packet if packet.get('schema_version') == 1 and isinstance(packet.get('facts'), dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def collect_task_review_decision_facts(repo: Path, *, task_id: str | None,
        starting_point_evidence: dict | None = None, snapshot_record: dict | None = None,
        observed_worktree_tree: str | None = None, real_prompt_identity: str | None = None) -> dict:
    started = time.monotonic()
    evidence, snapshot = starting_point_evidence or {}, snapshot_record or {}
    supplied_identity = snapshot or evidence
    identity = ({key: supplied_identity[key] for key in ('repo', 'git_directory', 'git_common_directory')}
                if all(supplied_identity.get(key) for key in ('repo', 'git_directory', 'git_common_directory'))
                else repository_identity(repo))
    if Path(identity['repo']).resolve() != repo.resolve():
        raise ValueError('共用事实与请求工作树身份不同')
    directory = decision_state_path(identity, task_id).with_suffix('')
    sources = evidence.get('decision_fact_sources') or {}
    head = snapshot.get('original_head') or evidence.get('observed_head')
    tree = observed_worktree_tree or evidence.get('observed_worktree_tree')
    measurement = {'statistics_git_command_count': 0, 'additional_worktree_capture_count': 0}
    def run_git(*arguments):
        measurement['statistics_git_command_count'] += 1
        return git(repo, *arguments)
    with exclusive_lock(directory / 'decision-facts.lock'):
        observation_error = None
        try:
            head = head or git_text(repo, 'rev-parse', 'HEAD')
            if not tree:
                measurement['additional_worktree_capture_count'] += 1
                tree = capture_worktree_tree(repo, ['.'], directory, head=head)
        except (OSError, ValueError, RuntimeError) as error:
            observation_error = str(error)
        scopes = normalized_scope_paths(snapshot.get('scope_paths') or evidence.get('scope_paths') or ['.'])
        explicit_scope = bool(snapshot or evidence.get('scope_is_explicit'))
        binding_matches = (evidence.get('status') == 'verified' and evidence.get('binding', {}).get('task_id') == task_id
            and all(evidence.get('binding', {}).get(key) == value for key, value in identity.items()))
        binding = evidence.get('binding', {}) if binding_matches else {}
        base = snapshot.get('base_commit') or evidence.get('base_commit')
        observation = {**identity, 'task_id': task_id, 'binding': binding, 'head': head, 'worktree_tree': tree,
            'base_commit': base, 'scope_paths': scopes, 'explicit_scope': explicit_scope,
            'candidate_commit': snapshot.get('candidate_commit'), 'workspace_observation_error': observation_error,
            'collector_implementation_sha256': DECISION_FACT_IMPLEMENTATION_SHA256,
            'starting_point_source_observation_id': evidence.get('source_observation_sha256')}
        prior = _load_cached_packet(directory)
        facts, reuse = {}, []
        def fact(name, inputs, source, valid_for, compute):
            observation_id = _digest([DECISION_FACT_IMPLEMENTATION_SHA256, inputs])
            previous = prior.get('facts', {}).get(name, {})
            if (previous.get('observation_id') == observation_id and previous.get('status') in {'verified', 'unknown'}
                    and (previous['status'] == 'verified' or not real_prompt_identity
                         or previous.get('last_real_prompt_identity') == real_prompt_identity)):
                facts[name] = previous
                reuse.append(True)
                return
            item = {'observation_id': observation_id, 'source': source, 'valid_for': valid_for,
                    'last_real_prompt_identity': real_prompt_identity}
            try:
                value = compute()
                # 单项的异常路径编码或损坏字段不能遮蔽其他事实。
                json.dumps(value, ensure_ascii=False).encode('utf-8')
                item.update(status='verified', value=value)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
                item.update(status='unknown', diagnostic=str(error), recovery_condition=RECOVERY_CONDITION)
            facts[name] = item
            reuse.append(False)
        def verified_binding():
            if not binding_matches:
                raise ValueError('尚无与当前 task/worktree 一致的已核验身份')
            return binding
        fact('task_identity', [binding_matches, binding, task_id, identity],
             evidence.get('recovery_record_path') or 'unavailable_task_binding', identity, verified_binding)
        def fixed_start():
            if not snapshot:
                verified_binding()
            if not base:
                raise ValueError('尚无可靠固定基线')
            return {'base_commit': base, 'original_start_commit': evidence.get('original_start_commit'),
                    'scope_paths': scopes, 'task_owned_commit_shas': evidence.get('task_owned_commit_shas', [])}
        fact('fixed_start_and_scope', [binding_matches, base, scopes, evidence.get('task_owned_commit_shas')],
             {'task_starting_point_file': evidence.get('recovery_record_path'),
              'explicit_snapshot_base_commit': snapshot.get('base_commit')}, {'head': head, 'scope_paths': scopes}, fixed_start)
        fact('workspace_change_statistics', [head, tree, scopes, explicit_scope, observation_error],
             {'head': head, 'full_worktree_tree': tree}, {'head': head, 'worktree_tree': tree, 'scope_paths': scopes},
             lambda: _workspace_statistics(run_git, head, tree, scopes, explicit_scope))
        def history_statistics():
            verified_binding()
            return _history_statistics(run_git, evidence, sources,
                prior.get('facts', {}).get('task_commit_history_statistics', {}).get('value', {}))
        fact('task_commit_history_statistics', [binding_matches, binding, evidence.get('observed_head'),
             evidence.get('original_start_commit'), evidence.get('committed_work_status'),
             evidence.get('history_task_owned_commit_shas', evidence.get('task_owned_commit_shas')),
             {key: value for key, value in sources.items() if key not in {'registered_runs', 'task_card_title'}}],
             sources.get('provenance_file') or evidence.get('recovery_record_path') or 'unavailable_task_commit_history',
             {'head': evidence.get('observed_head'), 'task_binding': binding, 'dirty_excluded': True}, history_statistics)
        def existing_review_evidence():
            verified_binding()
            if (head != evidence.get('observed_head') or tree != evidence.get('observed_worktree_tree')
                    or evidence.get('completion_evidence_status') == 'unknown'):
                raise ValueError('完成覆盖未知或当前候选内容已变化；保留原始材料，不推断通过')
            return {'completion_evidence_status': evidence.get('completion_evidence_status'),
                    'current_scope_reviewed': evidence.get('current_scope_reviewed', False),
                    'registered_runs': sources.get('registered_runs', {}),
                    'completed_run_association_path': evidence.get('completed_run_association_path')}
        fact('existing_review_evidence', [binding_matches, head, tree, evidence.get('observed_head'),
             evidence.get('observed_worktree_tree'), evidence.get('completion_evidence_status'),
             evidence.get('current_scope_reviewed'), sources.get('registered_runs')],
             {'resolution_file': evidence.get('recovery_record_path'), 'exact_runs': sources.get('registered_runs', {})},
             {'head': head, 'worktree_tree': tree, 'scope_paths': scopes}, existing_review_evidence)
        baseline_arguments = (f'--base-from-kanban-task {shlex.quote(task_id)}'
                              if binding_matches and (not snapshot or base == evidence.get('base_commit')) else
                              f'--base-ref {shlex.quote(base)}' if base else '--base-ref <固定基线>')
        guidance = {'new_run_command': f'rvf review-task-snapshot --repo {shlex.quote(str(repo))} {baseline_arguments} '
            + ' '.join(f'--scope {shlex.quote(path)}' for path in scopes) + ' --requirements-file <持久需求文件>',
            'resume_commands': [f'rvf resume-task-review --run-dir {shlex.quote(path)}'
                for path, run in sources.get('registered_runs', {}).items()
                if binding_matches and isinstance(run['status'], str) and run['status'] not in {'completed', 'unknown'}
                and run['execution_revocation_status'] == 'not_observed'],
            'reuse_rule': '优先读取精确材料；仅在内容变化、来源失效或语义判断需要时补查，不重复查询全量历史或 CLI 帮助'}
        fact('command_guidance', guidance, '当前插件 CLI 契约', {'task_id': task_id, 'scope_paths': scopes}, lambda: guidance)
        observation_id = _digest(observation)
        if prior.get('observation_id') == observation_id and all(reuse):
            return {**prior, 'measurement': {**measurement, 'cache_reused': True,
                                           'collection_elapsed_ms_before_persistence': (time.monotonic() - started) * 1000}}
        artifact = directory / 'observations' / f'{observation_id}-{uuid.uuid4().hex}.json'
        packet = {'schema_version': 1, 'observation_id': observation_id, 'observation': observation,
                  'observed_at': time.time(), 'artifact_path': str(artifact), 'facts': facts,
                  'measurement': {**measurement, 'cache_reused': False,
                                  'collection_elapsed_ms_before_persistence': (time.monotonic() - started) * 1000}}
        packet['summary'] = render_task_review_decision_summary(packet)
        write_json(artifact, packet)
        write_json(directory / 'decision-facts.json', {'artifact_path': str(artifact),
            'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()})
        return packet


def render_task_review_decision_summary(packet: dict) -> str:
    facts, observation = packet['facts'], packet['observation']
    lines = [f"RVF 共用事实：任务 {observation.get('task_id') or 'unknown'}；HEAD {(observation.get('head') or 'unknown')[:12]}；"
             f"固定基线 {(observation.get('base_commit') or 'unknown')[:12]}。"]
    history = facts['task_commit_history_statistics']
    if history['status'] == 'verified':
        value = history['value']
        basis = 'Task Card 保留提交' if value['basis'] == 'task_card_retained_committed_history' else '固定任务窗口逐提交'
        lines.append(f"历史累计（{basis}，不含 Dirty）：{value['commit_count']} 提交，+{value['additions']}/-{value['deletions']}。")
    else:
        lines.append('历史累计：unknown，详单保留补证条件。')
    dirty = facts['workspace_change_statistics']
    if dirty['status'] == 'verified':
        value = dirty['value']
        def counts(total):
            return f"{total['file_count']} 文件 +{total['additions']}/-{total['deletions']}，二进制 {total['binary_file_count']}"
        lines.append('当前 Dirty（HEAD → 完整工作树净内容，含未跟踪）：' + counts(value['total']) + '。')
        lines.append('；'.join(f"{label}：{counts(value['groups'][group])}" for group, label in GROUP_LABELS.items()) + '。')
        lines.append(('任务路径内：' + counts(value['task_scope']) + '；背景 WIP：' + counts(value['background_wip']) + '。')
                     if value['background_wip'] is not None else '背景 WIP：unknown，未声明任务路径，需核对归属。')
    else:
        lines.append('当前 Dirty：unknown，其他已证实事实仍有效。')
    review = facts['existing_review_evidence']
    lines.append(('精确评审：当前范围已覆盖。' if review['value']['current_scope_reviewed'] else
                  f"精确评审：{len(review['value']['registered_runs'])} 个 run，当前范围尚未证实覆盖。")
                 if review['status'] == 'verified' else '精确评审覆盖：unknown，不能解释为通过或不存在。')
    lines.append(f"持久详单：{packet['artifact_path']}。分组是用途提示，仍须判断实际行为与风险。")
    lines.append('启动提示：' + facts['command_guidance']['value']['new_run_command'])
    lines.extend(facts['command_guidance']['value']['resume_commands'][:1])
    return '\n'.join(lines)
