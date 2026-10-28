"""Kanban 证据只选择固定起点与发现任务提交，不授予 session/hunk 所有权。"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path

from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from core.task_snapshot_review.persistent_artifacts import (
    exclusive_lock, git, git_text, read_json, repository_identity, write_bytes, write_json,
)
from core.task_snapshot_review.task_snapshot import normalized_scope_paths


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    try:
        git(repo, 'merge-base', '--is-ancestor', ancestor, descendant)
        return True
    except RuntimeError:
        return False


def _commit(repo: Path, reference: str) -> str:
    return git_text(repo, 'rev-parse', '--verify', '--end-of-options', f'{reference}^{{commit}}')


def _reflog(repo: Path) -> list[tuple[str, str, str, int]]:
    path = Path(git_text(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'logs/HEAD'))
    if not path.exists() or path.stat().st_size > 16 * 1024 * 1024:
        return []
    entries = []
    for line in path.read_text().splitlines():
        header, separator, message = line.partition('\t')
        fields = header.split()
        if (len(fields) < 5 or not fields[-2].isdigit() or (not separator and fields[0].strip('0'))
                or any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) for value in fields[:2])):
            return []
        entries.append((fields[0], fields[1], message, int(fields[-2]) * 1000))
    return entries


def _source_observations(task_id: str) -> list[dict]:
    root = Path(os.environ.get('RVF_CLINE_KANBAN_STATE_DIRECTORY') or Path.home() / '.cline/kanban')
    observations = []
    for directory in sorted((root / 'workspaces').glob('*')):
        values, errors = {}, {}
        for name in ('board.json', 'sessions.json', 'task-commit-integration-provenance.json'):
            try:
                values[name] = read_json(directory / name)
            except (OSError, ValueError) as error:
                errors[name] = str(error)
        board = values.get('board.json', {})
        cards = [card for column in board.get('columns', []) for card in column.get('cards', [])
                 if isinstance(card, dict) and card.get('id') == task_id]
        session = values.get('sessions.json', {}).get(task_id)
        provenance = values.get('task-commit-integration-provenance.json', {})
        entry = provenance.get('tasks', {}).get(task_id)
        if cards or session or entry:
            observations.append({'directory': str(directory.resolve()), 'cards': cards, 'session': session,
                                 'entry': entry, 'provenance_schema': provenance.get('schemaVersion'), 'errors': errors})
    return observations


def _directory_identity(path: Path) -> list:
    status = path.stat()
    # st_dev 会在 macOS 重启后漂移；使用目录实体和创建时间检测同路径替换。
    return [status.st_ino, getattr(status, 'st_birthtime_ns', getattr(status, 'st_birthtime', None))]


def _task_binding(repo: Path, task_id: str, identity: dict, sources: list[dict], entries: list[tuple]) -> tuple[dict, dict]:
    bound_task = os.environ.get('KANBAN_TASK_ID') or os.environ.get('CLINE_KANBAN_TASK_ID')
    if bound_task and bound_task != task_id:
        raise ValueError('请求 task 与当前 Kanban 任务身份不同')
    if len(sources) != 1 or len(sources[0]['cards']) != 1:
        raise ValueError('Kanban task 的持久卡片/工作区来源缺失或不唯一')
    source = sources[0]
    card, session = source['cards'][0], source['session']
    if (not isinstance(card.get('createdAt'), (int, float)) or isinstance(card['createdAt'], bool)
            or not math.isfinite(card['createdAt'])):
        raise ValueError('Kanban task 缺少创建身份')
    if not isinstance(session, dict) or session.get('taskId') != task_id or Path(session.get('workspacePath', '')).resolve() != repo:
        raise ValueError('Kanban task 的真实 worktree 绑定不匹配')
    project = os.environ.get('KANBAN_PROJECT_PATH') or os.environ.get('CLINE_KANBAN_PROJECT_PATH')
    if project and repository_identity(Path(project))['git_common_directory'] != identity['git_common_directory']:
        raise ValueError('Kanban project 的 Git common-dir 不匹配')
    base_ref = card.get('baseRef') or 'main'
    if not isinstance(base_ref, str):
        raise ValueError('Kanban task 的 base ref 无效')
    provenance = source['entry'] or {}
    if provenance and (source['provenance_schema'] != 1 or provenance.get('taskId') != task_id or provenance.get('baseRef') != base_ref):
        raise ValueError('Kanban provenance 的 task/base/schema 不匹配')
    incarnations = provenance.get('incarnations', [])
    incarnation = incarnations[-1] if incarnations else {}
    if incarnation:
        initial, started = incarnation.get('initialBaseCommit'), incarnation.get('startedAt')
        if (not isinstance(started, (int, float)) or isinstance(started, bool) or not math.isfinite(started)
                or started < card['createdAt'] or incarnation.get('incarnationId') != f'{initial}:{started}'
                or any(item.get('startedAt', 0) > started for item in incarnations)):
            raise ValueError('Kanban provenance incarnation 无效或次序不明')
        snapshot = provenance.get('lastWorkspaceGitSnapshot')
        if snapshot and Path(snapshot.get('path', '')).resolve() != repo:
            raise ValueError('Kanban provenance 指向另一个 worktree')
    creation = entries[0][1] if entries and not entries[0][0].strip('0') else None
    # 原生 Git 时间精度为秒；只在确定早于当前卡片时拒绝创建记录。
    if creation and entries[0][3] + 999 < card['createdAt']:
        raise ValueError('Git worktree 创建记录早于当前 Kanban 卡片')
    if creation and incarnation.get('captureSource') == 'worktree_creation' and incarnation.get('initialBaseCommit') != creation:
        raise ValueError('Kanban 创建锚点与当前 worktree 的 Git 创建记录冲突')
    binding = {**identity, 'task_id': task_id, 'task_created_at': card['createdAt'], 'base_ref': base_ref,
               'kanban_workspace_directory': source['directory'],
               'git_common_directory_identity': _directory_identity(Path(identity['git_common_directory'])),
               'worktree_directory_identity': _directory_identity(repo),
               'git_directory_identity': _directory_identity(Path(identity['git_directory'])),
               'creation_commit': incarnation.get('initialBaseCommit') or creation,
               'provenance_incarnation_id': incarnation.get('incarnationId')}
    return binding, incarnation


def _same_binding(first: dict, second: dict) -> bool:
    optional = {'provenance_incarnation_id', 'creation_commit'}
    keys = set(first) | set(second)
    return all(first.get(key) == second.get(key) if key not in optional or (first.get(key) and second.get(key)) else True for key in keys)


def _checkpoint_start(repo: Path, task_id: str, entries: list[tuple], binding: dict, incarnation: dict) -> str | None:
    # task 级 turn/1 不能证明另一个 incarnation；backfill 也不是创建事实。
    if not entries or entries[0][0].strip('0') or (incarnation and incarnation.get('captureSource') != 'worktree_creation'):
        return None
    encoded = base64.urlsafe_b64encode(task_id.encode()).decode().rstrip('=')
    try:
        parents = git_text(repo, 'rev-list', '--parents', '-n', '1', f'refs/kanban/checkpoints/{encoded}/turn/1').split()
        created = int(git_text(repo, 'show', '-s', '--format=%ct', parents[0])) * 1000
        return parents[1] if (len(parents) == 2 and parents[1] == entries[0][1]
                              and created + 999 >= max(binding['task_created_at'], entries[0][3])) else None
    except RuntimeError:
        return None


def _patch_identity(repo: Path, commit: str) -> str | None:
    # 与 Kanban 的 git show <sha> | git patch-id --stable 使用同一种 diff 表达。
    patch = git(repo, 'show', '--no-ext-diff', commit)
    output = git_text(repo, 'patch-id', '--stable', input_bytes=patch)
    return output.split()[0] if output else None


def _owned_commits(repo: Path, baseline: str, head: str, incarnation: dict, entries: list[tuple]) -> list[str]:
    owned = set()
    pending = []
    identities = incarnation.get('observedTaskCommits', [])
    if not isinstance(identities, list):
        raise ValueError('Kanban 提交身份列表损坏')
    for item in identities:
        if not isinstance(item, dict) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(item.get('commitSha', ''))):
            raise ValueError('Kanban 提交 SHA 身份损坏')
        original = item['commitSha']
        if _is_ancestor(repo, original, baseline):
            continue
        if _is_ancestor(repo, original, head):
            owned.add(original)
        else:
            pending.append(item)
    if pending:
        window = git_text(repo, 'rev-list', '--no-merges', f'{baseline}..{head}').splitlines()
        by_patch = {}
        for commit in window:
            patch_id = _patch_identity(repo, commit)
            if patch_id:
                by_patch.setdefault(patch_id, []).append(commit)
        consumed = set(owned)
        for item in pending:
            patch_id = item.get('stablePatchId')
            if not patch_id:
                try:
                    patch_id = _patch_identity(repo, item['commitSha'])
                except RuntimeError:
                    pass
            matches = by_patch.get(patch_id, [])
            if len(matches) != 1 or matches[0] in consumed:
                raise ValueError('历史改写缺少唯一的一对一 patch-id 映射')
            consumed.add(matches[0]); owned.add(matches[0])
    for _, following, message, _ in entries:
        if re.match(r'^commit(?: \([^)]*\))?: ', message) and _is_ancestor(repo, following, head) and not _is_ancestor(repo, following, baseline):
            owned.add(following)
    return sorted(owned)


def _only_base_fast_forwards(entries: list[tuple], baseline: str, head: str) -> bool:
    if not entries or entries[0][0].strip('0') or entries[0][1] != baseline:
        return False
    current = baseline
    for previous, following, message, _ in entries[1:]:
        if previous != current or not re.fullmatch(r'(?:merge [^\s:]+|pull(?: [^:]*)?): Fast-forward', message):
            return False
        current = following
    return current == head


def _storage_paths(identity: dict, task_id: str) -> tuple[Path, Path, Path]:
    key = _digest({'git_directory': identity['git_directory'], 'task_id': task_id})
    directory = resolved_rvf_user_local_data_paths().runtime_state_root / 'kanban-task-review-starting-points' / key
    return directory / 'starting-point.json', directory / 'source-resolution.json', directory / 'resolution.lock'


def _read_optional(path: Path) -> dict | None:
    return read_json(path) if path.exists() else None


def _real_prompt_identity(event: dict) -> str | None:
    if not isinstance(event.get('prompt'), str) or not event['prompt'].strip():
        return None
    identity = {key: event.get(key) for key in ('prompt', 'turn_id', 'prompt_id', 'event_id', 'session_id')}
    if not any(event.get(key) for key in ('turn_id', 'prompt_id', 'event_id')) and event.get('transcript_path'):
        # 相同文本的两条真实消息是两次机会；assistant 输出和重复 hook 不是新机会。
        path = Path(event['transcript_path']).expanduser()
        try:
            with path.open('rb') as handle:
                while True:
                    offset = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get('payload')
                    message = record.get('message')
                    if (record.get('type') == 'user' or record.get('role') == 'user'
                            or (isinstance(message, dict) and message.get('role') == 'user')
                            or (isinstance(payload, dict) and (payload.get('role') == 'user'
                                                              or payload.get('type') == 'user_message'))):
                        identity['native_user_message'] = [str(path.resolve()), offset, hashlib.sha256(line).hexdigest()]
        except OSError:
            # 原生记录暂时不可读时仍可使用宿主给出的身份；诊断不成为用户上下文。
            pass
    return _digest(identity)


def _latest_verified_resolution(resolution_path: Path, binding: dict, prior: dict | None = None) -> tuple[dict, Path] | None:
    candidates = [(prior, resolution_path)] if prior else []
    for path in (resolution_path.parent / 'resolution-attempts').glob('*.json'):
        try:
            candidates.append((read_json(path), path))
        except (OSError, ValueError):
            continue
    candidates = [(record, path) for record, path in candidates
                  if record.get('status') == 'verified' and _same_binding(record.get('binding', {}), binding)]
    return max(candidates, key=lambda item: item[0].get('resolution_attempt_count', 0), default=None)


def _historical_starting_point(starting_path: Path, binding: dict, incarnations: list[dict]) -> tuple[dict, Path] | None:
    candidates = []
    order = {item.get('incarnationId'): index for index, item in enumerate(incarnations)}
    for path in starting_path.parent.parent.glob('*/starting-point.json'):
        if path == starting_path:
            continue
        try:
            record = read_json(path)
        except (OSError, ValueError):
            continue
        previous = record.get('binding', {})
        if all(previous.get(key) == binding.get(key) for key in (
                'task_id', 'task_created_at', 'base_ref', 'git_common_directory', 'git_common_directory_identity',
                'kanban_workspace_directory')):
            candidates.append((record, path))
    if not candidates:
        return None
    if len({record.get('base_commit') for record, _ in candidates}) != 1:
        raise ValueError('同一卡片存在不同的历史固定起点，尚不能证明唯一的连续窗口')
    return max(candidates, key=lambda item: order.get(item[0]['binding'].get('provenance_incarnation_id'), -1))


def _rebind_starting_point(repo: Path, starting: dict, original_path: Path, starting_path: Path,
                          binding: dict, incarnation: dict, incarnations: list[dict], entries: list[tuple],
                          scope_paths: list[str] | None, prior: dict) -> tuple[dict, dict]:
    previous = starting['binding']
    stable_keys = ('task_id', 'base_ref', 'git_common_directory', 'git_common_directory_identity', 'kanban_workspace_directory')
    recreated_directory_keys = ('worktree_directory_identity', 'git_directory_identity')
    if (any(previous.get(key) != binding.get(key) for key in stable_keys)
            or not any(previous.get(key) != binding.get(key) for key in recreated_directory_keys)
            or incarnation.get('captureSource') != 'worktree_creation' or not entries or entries[0][0].strip('0')
            or entries[0][1] != incarnation.get('initialBaseCommit')
            or previous.get('provenance_incarnation_id') == incarnation.get('incarnationId')):
        raise ValueError('保存的实现起点属于另一次 task/worktree incarnation，尚缺少新的创建与连续性证据')
    creation = _commit(repo, incarnation['initialBaseCommit'])
    if not _is_ancestor(repo, creation, 'HEAD'):
        raise ValueError('新的 worktree 创建点尚不能从当前 HEAD 到达')
    evidence = {'previous_binding': previous, 'current_binding': binding, 'creation_commit': creation,
                'provenance_incarnation_id': incarnation['incarnationId']}
    if previous['task_created_at'] == binding['task_created_at']:
        old_index = next((index for index, item in enumerate(incarnations)
                          if item.get('incarnationId') == previous.get('provenance_incarnation_id')), None)
        if old_index is None or old_index >= len(incarnations) - 1:
            raise ValueError('同卡片重建缺少先前 incarnation 的连续来源')
        old_incarnation = incarnations[old_index]
        verified = _latest_verified_resolution(original_path.with_name('source-resolution.json'), previous,
                                               prior if original_path == starting_path else None)
        if (not verified or verified[0].get('original_start_commit') != starting['base_commit']
                or old_incarnation.get('initialBaseCommit') != previous.get('creation_commit')
                or old_incarnation.get('startedAt', 0) < binding['task_created_at']
                or not _is_ancestor(repo, starting['base_commit'], creation)
                or not _is_ancestor(repo, verified[0]['observed_head'], creation)):
            raise ValueError('同卡片重建尚不能证明原固定起点及先前任务内容完整延续')
        identities = list(starting.get('retained_task_commit_identities', [])) + old_incarnation.get('observedTaskCommits', [])
        identities += [{'commitSha': commit, 'stablePatchId': None} for commit in verified[0]['task_owned_commit_shas']]
        identities = list({item['commitSha']: item for item in identities}.values())
        retained = _owned_commits(repo, starting['base_commit'], creation, {'observedTaskCommits': identities}, [])
        recovered = {**starting, 'binding': binding, 'retained_task_commit_identities': [
            {'commitSha': commit, 'stablePatchId': _patch_identity(repo, commit)} for commit in retained]}
        # 原 run 仍留在旧 marker 中；新的工作树不能直接继承其执行身份。
        recovered.pop('fixed_run_directory', None)
        evidence['verified_resolution'] = str(verified[1])
        evidence['previous_observed_head'] = verified[0]['observed_head']
    else:
        if previous['task_created_at'] >= binding['task_created_at']:
            raise ValueError('当前卡片创建身份未能证明是后续独立任务')
        recovered = {'binding': binding, 'base_commit': creation, 'scope_paths': scope_paths, 'source': 'kanban_worktree_creation'}
    original_bytes = original_path.read_bytes()
    preserved = starting_path.with_name(f'starting-point.prior-binding-{hashlib.sha256(original_bytes).hexdigest()}.json')
    write_bytes(preserved, original_bytes)
    recovered['rebinding_evidence'] = evidence
    write_json(starting_path, recovered)
    return recovered, {'reason': '新的 worktree 创建来源已补齐，并已核对任务身份及历史连续性',
                       'preserved_original': str(preserved), 'equivalent_evidence': evidence}


def _owned_changed_paths(repo: Path, owned: list[str], head: str, changed: list[str]) -> list[str]:
    owned_paths = set()
    for commit in owned:
        paths = {path for path in git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z', commit).decode().split('\0') if path}
        owned_paths.update(paths)
        # 只用于发现路径；保留该路径完整净 diff，不投影 session/hunk。
        fields = git(repo, 'diff', '--name-status', '--find-renames', '-z', commit, head).decode().split('\0')
        index = 0
        while index < len(fields) and fields[index]:
            status, path = fields[index:index + 2]
            index += 2
            if status.startswith(('R', 'C')):
                destination = fields[index]
                index += 1
                if status.startswith('R') and path in paths:
                    owned_paths.add(destination)
    return sorted(set(changed) & owned_paths)


def _scope_is_covered(scopes: list[str], covered: list[str]) -> bool:
    return all(any(parent == '.' or path == parent or path.startswith(parent.rstrip('/') + '/') for parent in covered) for path in scopes)


def _completed_run_baseline(repo: Path, run_directory: Path, binding: dict, scopes: list[str], original: str) -> tuple[str, str]:
    record = read_json(run_directory / 'run.json')
    evidence = record.get('starting_point_evidence', {})
    if (not _same_binding(evidence.get('binding', {}), binding) or record.get('status') != 'completed'
            or evidence.get('original_start_commit') != original or evidence.get('base_commit') != record.get('base_commit')):
        raise ValueError('增量起点必须来自同任务、同工作树的已完成新版 run')
    validation = read_json(run_directory / 'validation.json')
    commit = _commit(repo, record['commit'])
    if (validation.get('status') != 'passed' or validation.get('tested_tree') != git_text(repo, 'rev-parse', f'{commit}^{{tree}}')
            or not _is_ancestor(repo, commit, 'HEAD')):
        raise ValueError('增量 run 缺少当前可核对的完整树验证和已发生提交')
    covered = record.get('effective_scope_paths', record['scope_paths'])
    if not _scope_is_covered(scopes, covered):
        return original, 'expanded_scope_original_start'
    return commit, 'explicit_completed_run_increment'


def resolve_task_review_starting_point(repo: Path, task_id: str, *, scope_paths: list[str] | None = None,
                                      event: dict | None = None, incremental_after_run: Path | None = None) -> dict:
    """同一来源观察仅解析一次；真实 prompt 和来源变化允许有界重验。"""
    identity = repository_identity(repo)
    repo = Path(identity['repo'])
    starting_path, resolution_path, lock = _storage_paths(identity, task_id)
    event = event or {}
    prompt_identity = _real_prompt_identity(event)
    with exclusive_lock(lock):
        try:
            prior = _read_optional(resolution_path) or {}
        except (OSError, ValueError):
            prior = {}
        result = {'status': 'unknown', **identity, 'task_id': task_id, 'task_owned_commit_shas': [],
                  'committed_work_status': 'unknown', 'recovery_record_path': str(resolution_path),
                  'recovery_condition': 'Kanban 绑定、起点、checkpoint、Git 历史或精确 run 证据变化，或下一次真实 prompt 时有界重验'}
        observation = {}
        try:
            sources = _source_observations(task_id)
            entries = _reflog(repo)
            head = _commit(repo, 'HEAD')
            # 缓存不能替代当前 project/common-dir 与目录实体身份门禁。
            binding, incarnation = _task_binding(repo, task_id, identity, sources, entries)
            result['binding'] = binding
            try:
                starting = _read_optional(starting_path)
            except (ValueError, OSError) as error:
                verified = _latest_verified_resolution(resolution_path, binding, prior)
                if not verified or not _is_ancestor(repo, verified[0]['original_start_commit'], head):
                    raise ValueError('起点文件损坏，尚无可独立核对的原起点记录') from error
                saved, evidence_path = verified
                original_bytes = starting_path.read_bytes()
                preserved = starting_path.with_name(f'starting-point.corrupt-{hashlib.sha256(original_bytes).hexdigest()}.json')
                write_bytes(preserved, original_bytes)
                starting = {'binding': binding, 'base_commit': saved['original_start_commit'],
                            'scope_paths': saved.get('recorded_scope_paths'), 'source': 'recovered_persisted_implementation_start'}
                for key in ('fixed_run_directory', 'retained_task_commit_identities'):
                    if key in saved:
                        starting[key] = saved[key]
                write_json(starting_path, starting)
                result['recovery_actions'] = [{'reason': str(error), 'preserved_original': str(preserved),
                                               'equivalent_evidence': str(evidence_path)}]
            original_path = starting_path
            incarnations = (sources[0]['entry'] or {}).get('incarnations', [])
            if not starting:
                historical = _historical_starting_point(starting_path, binding, incarnations)
                if historical:
                    starting, original_path = historical
            if starting and not _same_binding(starting['binding'], binding):
                starting, action = _rebind_starting_point(repo, starting, original_path, starting_path, binding,
                                                         incarnation, incarnations, entries, scope_paths, prior)
                result.setdefault('recovery_actions', []).append(action)
            checkpoint = _checkpoint_start(repo, task_id, entries, binding, incarnation)
            run_observation = None
            run_path = incremental_after_run or (Path(starting['fixed_run_directory']) if starting and starting.get('fixed_run_directory') else None)
            if run_path:
                run_observation = {'run': _read_optional(run_path / 'run.json'), 'validation': _read_optional(run_path / 'validation.json')}
            observation = {'sources': sources, 'head': head, 'reflog': entries, 'starting': starting,
                           'checkpoint': checkpoint, 'scope_paths': scope_paths, 'run': run_observation, 'binding': binding,
                           'incremental_after_run': str(incremental_after_run.resolve()) if incremental_after_run else None,
                           'bound_task_id': os.environ.get('KANBAN_TASK_ID') or os.environ.get('CLINE_KANBAN_TASK_ID'),
                           'project': os.environ.get('KANBAN_PROJECT_PATH') or os.environ.get('CLINE_KANBAN_PROJECT_PATH'),
                           'directory_identity': [_directory_identity(repo), _directory_identity(Path(identity['git_directory']))]}
            digest = _digest(observation)
            same_prompt = not prompt_identity or prompt_identity == prior.get('last_real_prompt_identity')
            if prior.get('source_observation_sha256') == digest and ((prior.get('status') == 'verified' and prior.get('committed_work_status') != 'unknown') or same_prompt):
                return prior
            if starting:
                baseline = starting['base_commit']
                source = starting['source']
            else:
                baseline = incarnation.get('initialBaseCommit') if incarnation.get('captureSource') == 'worktree_creation' else checkpoint
                source = 'kanban_worktree_creation' if incarnation.get('captureSource') == 'worktree_creation' else 'kanban_checkpoint_and_git_creation'
                if not baseline:
                    raise ValueError('缺少可靠实现起点；不使用 live main、merge-base 或当前 HEAD 猜测')
            baseline = _commit(repo, baseline)
            if not _is_ancestor(repo, baseline, head):
                raise ValueError('保存的固定起点不可从当前 HEAD 到达；需要可核对的历史/起点证据')
            scopes = normalized_scope_paths(scope_paths or (starting or {}).get('scope_paths') or ['.'])
            original = baseline
            if incremental_after_run:
                baseline, source = _completed_run_baseline(repo, incremental_after_run.resolve(), binding, scopes, original)
            elif run_observation:
                record = run_observation['run']
                evidence = (record or {}).get('starting_point_evidence', {})
                if record and (evidence.get('original_start_commit') != original
                               or evidence.get('base_commit') != record.get('base_commit')
                               or not _same_binding(evidence.get('binding', {}), binding)):
                    raise ValueError('保存的精确 run 与固定起点或任务身份不匹配')
            discovery_incarnation = {**incarnation, 'observedTaskCommits': list(incarnation.get('observedTaskCommits', []))
                                     + (starting or {}).get('retained_task_commit_identities', [])}
            discovery_incarnation['observedTaskCommits'] = list({item['commitSha']: item for item in discovery_incarnation['observedTaskCommits']}.values())
            owned = _owned_commits(repo, baseline, head, discovery_incarnation, entries)
            changed = [path for path in git(repo, 'diff', '--name-only', '-z', baseline, head, '--', *scopes).decode().split('\0') if path]
            owned_changed = _owned_changed_paths(repo, owned, head, changed)
            work_status = 'present' if owned_changed else ('absent' if not changed or head == baseline or _only_base_fast_forwards(entries, baseline, head) else 'unknown')
            reviewed = False
            if run_path and run_observation and (run_observation['run'] or {}).get('status') == 'completed':
                try:
                    # 提醒比较实际净变化；自动默认的全库范围不要求窄范围 run 评审未变化路径。
                    coverage_paths = [path for path in git(repo, 'diff', '--no-renames', '--name-only', '-z',
                                                          baseline, head, '--', *scopes).decode().split('\0') if path]
                    completed_base, completed_source = _completed_run_baseline(repo, run_path, binding, coverage_paths, original)
                    reviewed = completed_source == 'explicit_completed_run_increment' and not git(repo, 'diff', '--name-only', completed_base, head, '--', *scopes)
                except (OSError, ValueError, RuntimeError, KeyError):
                    # 起点仍有独立证明；损坏的完成记录不能获得已审资格。
                    pass
            if not starting:
                starting = {'binding': binding, 'base_commit': original, 'scope_paths': scope_paths, 'source': source}
                write_json(starting_path, starting)
                observation['starting'] = starting
            result.update(status='verified', binding=binding, base_ref=binding['base_ref'], base_commit=baseline,
                          original_start_commit=original, observed_head=head, source=source, scope_paths=scopes,
                          scope_is_explicit=bool(scope_paths or starting.get('scope_paths')), changed_paths=changed,
                          recorded_scope_paths=starting.get('scope_paths'), current_scope_reviewed=reviewed,
                          task_owned_commit_shas=owned, owned_changed_paths=owned_changed, committed_work_status=work_status)
            if starting.get('fixed_run_directory'):
                result['fixed_run_directory'] = starting['fixed_run_directory']
            if starting.get('retained_task_commit_identities'):
                result['retained_task_commit_identities'] = starting['retained_task_commit_identities']
            if incremental_after_run:
                result['incremental_after_run_directory'] = str(incremental_after_run.resolve())
            if work_status == 'unknown':
                result['diagnostic'] = '起点已证实，仍缺少当前窗口的任务提交归属证据；不能解释为没有候选'
            if work_status != 'unknown':
                result.pop('recovery_condition', None)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
            result['diagnostic'] = str(error)
            if not observation:
                observation = {'sources': locals().get('sources'), 'starting_bytes': starting_path.read_bytes().hex() if starting_path.exists() else None,
                               'head': locals().get('head'), 'diagnostic': str(error)}
        result.update(source_observation_sha256=_digest(observation),
                      resolution_attempt_count=prior.get('resolution_attempt_count', 0) + 1,
                      last_real_prompt_identity=prompt_identity or prior.get('last_real_prompt_identity'))
        # 无效 JSON 等失败观察也去重；原起点文件不会被 unknown 覆盖。
        if prior.get('source_observation_sha256') == result['source_observation_sha256'] and (not prompt_identity or prompt_identity == prior.get('last_real_prompt_identity')):
            return prior
        write_json(resolution_path.parent / 'resolution-attempts' / f"{result['resolution_attempt_count']}-{result['source_observation_sha256']}.json", result)
        write_json(resolution_path, result)
        return result


def record_task_review_start(repo: Path, task_id: str, *, base_ref: str, scope_paths: list[str]) -> dict:
    """仅由明确记录命令设定一段实现的起点；prompt 和 Stop 不推进它。"""
    identity = repository_identity(repo)
    repo = Path(identity['repo'])
    binding, _ = _task_binding(repo, task_id, identity, _source_observations(task_id), _reflog(repo))
    baseline = _commit(repo, base_ref)
    if not _is_ancestor(repo, baseline, 'HEAD'):
        raise ValueError('明确实现起点必须是当前 HEAD 可达的提交')
    starting_path, _, lock = _storage_paths(identity, task_id)
    with exclusive_lock(lock):
        write_json(starting_path, {'binding': binding, 'base_commit': baseline, 'scope_paths': normalized_scope_paths(scope_paths),
                                   'source': 'explicit_implementation_start'})
    return resolve_task_review_starting_point(repo, task_id)


def attach_task_review_run(repo: Path, task_id: str, run_directory: Path) -> None:
    """保存精确 run 指针；默认起点保持不变，增量选择不改变后续默认。"""
    starting_path, _, lock = _storage_paths(repository_identity(repo), task_id)
    with exclusive_lock(lock):
        starting = read_json(starting_path)
        record = read_json(run_directory / 'run.json')
        evidence = record['starting_point_evidence']
        current_binding, _ = _task_binding(Path(repository_identity(repo)['repo']), task_id, repository_identity(repo),
                                          _source_observations(task_id), _reflog(repo))
        if not _same_binding(starting['binding'], evidence['binding']) or not _same_binding(current_binding, evidence['binding']):
            raise ValueError('run 创建期间 Kanban 实现起点身份变化')
        if (evidence.get('original_start_commit') != starting['base_commit']
                or evidence.get('base_commit') != record['base_commit']):
            raise ValueError('run 不属于保存的原固定起点')
        if record['base_commit'] != starting['base_commit']:
            previous_run = evidence.get('incremental_after_run_directory')
            if not previous_run:
                raise ValueError('增量 run 缺少明确选择的已完成前序 run')
            baseline, source = _completed_run_baseline(repo, Path(previous_run), current_binding, record['scope_paths'], starting['base_commit'])
            if source != 'explicit_completed_run_increment' or baseline != record['base_commit']:
                raise ValueError('增量 run 的精确前序完成证据不匹配')
        starting['fixed_run_directory'] = str(run_directory.resolve())
        write_json(starting_path, starting)
