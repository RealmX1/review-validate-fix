#!/usr/bin/env python3
"""只读回放固定历史转录，并在隔离仓库量测新增采集成本；不调用模型。"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from adapters.cline_kanban.task_review_starting_point import resolve_task_review_starting_point
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_review_decision_facts import collect_task_review_decision_facts


def command_replay_disposition(command: str) -> str:
    if 'write_text' in command or ".open('a')" in command:
        return '保留：仍需判断或保存语义结论；其中机器状态可复用'
    if 'diff --check' in command or 'manage-base-ref-scoped-sync-lock.sh' in command:
        return '保留：校验或本地 base 锁状态超出本采集器范围'
    if 'task rvf-history' in command or 'final-rvf-history-query.json' in command:
        return '可替换事实查询：读取本任务精确注册表及覆盖事实，unknown 仍保持 unknown'
    if 'history' in command and ('rg ' in command or 'command -v rvf' in command):
        return '可省去入口发现：摘要直接提供当前命令与精确材料路径'
    return '部分复用：HEAD、工作树与精确 run 由摘要提供；授权、语义及本地 base 整合材料仍须核对'


def replay_samples(manifest: dict) -> dict:
    samples = []
    for sample in manifest['samples']:
        source = Path(sample['transcript_path'])
        selected = source.read_bytes().splitlines(keepends=True)[sample['start'] - 1:sample['end']]
        records = [json.loads(line) for line in selected]
        first, last = records[0], records[-1]
        elapsed = (datetime.fromisoformat(last['timestamp'].replace('Z', '+00:00'))
                   - datetime.fromisoformat(first['timestamp'].replace('Z', '+00:00'))).total_seconds()
        assert abs(elapsed - sample['seconds']) < 0.002
        calls, commands = [], []
        for offset, record in enumerate(records, sample['start']):
            payload = record.get('payload', {})
            if record['type'] == 'response_item' and payload.get('type') in {'function_call', 'custom_tool_call'}:
                calls.append({'line': offset, 'name': payload.get('name')})
            item = payload.get('item') or {}
            if record['type'] == 'event_msg' and payload.get('type') == 'item_completed' and item.get('type') == 'CommandExecution':
                command = ' '.join(item['command'])
                commands.append({'line': offset, 'command': command,
                                 'disposition': command_replay_disposition(command)})
        assert len(calls) == sample['calls'] and len(commands) == sample['commands']
        samples.append({**sample, 'raw_span_sha256': hashlib.sha256(b''.join(selected)).hexdigest(),
                        'tool_calls': calls, 'command_replay': commands})
    return {'kind': '只读转录动作回放，不执行历史命令，不重启历史任务，不量测新模型决策',
            'samples': samples, 'summary': {'sample_count': len(samples),
                'historical_decision_wall_seconds_sum': sum(sample['seconds'] for sample in samples),
                'historical_decision_wall_seconds_median': statistics.median(sample['seconds'] for sample in samples),
                'historical_tool_call_count': sum(sample['calls'] for sample in samples),
                'historical_native_command_count': sum(sample['commands'] for sample in samples),
                'replaceable_native_fact_query_count': sum(command['disposition'].startswith('可替换')
                    for sample in samples for command in sample['command_replay']),
                'avoidable_native_entry_discovery_count': sum(command['disposition'].startswith('可省去')
                    for sample in samples for command in sample['command_replay']),
                'post_implementation_agent_tool_call_count': None,
                'post_implementation_agent_decision_wall_seconds': None},
            'limitation': '可替换项是新契约覆盖的查询动作，不等于实测少调用次数或节约墙钟；授权、风险判断和整合核验仍保留'}


def measure_isolated_collection(*, with_task_card_statistics: bool) -> list[dict]:
    previous_environment = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory(prefix='rvf-decision-facts-measurement-') as fixture_directory:
            fixture = Path(fixture_directory).resolve()
            for key, value in {'HOME': fixture / 'isolated-user-home', 'CODEX_HOME': fixture / 'isolated-codex-home',
                    'CODEX_SQLITE_HOME': fixture / 'isolated-codex-sqlite',
                    'RVF_USER_LOCAL_DATA_ROOT': fixture / 'isolated-rvf-state',
                    'RVF_CLINE_KANBAN_STATE_DIRECTORY': fixture / 'kanban-state'}.items():
                os.environ[key] = str(value)
            for key in ('KANBAN_TASK_ID', 'KANBAN_PROJECT_PATH', 'CLINE_KANBAN_TASK_ID', 'CLINE_KANBAN_PROJECT_PATH'):
                os.environ.pop(key, None)
            main = fixture / 'repository'
            main.mkdir()
            git(main, 'init', '-q', '-b', 'main')
            git(main, 'config', 'user.name', 'RVF measurement')
            git(main, 'config', 'user.email', 'measurement@example.invalid')
            (main / 'implementation.py').write_text('value = 1\n')
            git(main, 'add', '.')
            git(main, 'commit', '-qm', 'test: 隔离测量起点')
            initial = git_text(main, 'rev-parse', 'HEAD')
            repo = fixture / 'task-worktree'
            git(main, 'worktree', 'add', '-q', '--detach', str(repo), initial)
            (repo / 'implementation.py').write_text('value = 2\n')
            git(repo, 'commit', '-qam', 'feat: 测量任务行为')
            head = git_text(repo, 'rev-parse', 'HEAD')
            workspace = fixture / 'kanban-state/workspaces/isolated-measurement'
            write_json(workspace / 'board.json', {'columns': [{'cards': [
                {'id': 'measurement-task', 'createdAt': 1, 'baseRef': 'main'}]}]})
            write_json(workspace / 'sessions.json', {'measurement-task': {
                'taskId': 'measurement-task', 'workspacePath': str(repo)}})
            entry = {'taskId': 'measurement-task', 'baseRef': 'main', 'incarnations': [{
                'incarnationId': initial + ':2', 'initialBaseCommit': initial, 'startedAt': 2,
                'captureSource': 'worktree_creation', 'observedTaskCommits': [{'commitSha': head, 'stablePatchId': None}]}]}
            if with_task_card_statistics:
                entry['lastWorkspaceGitSnapshot'] = {'path': str(repo), 'headCommit': head, 'baseRefTipCommit': initial}
                entry['retainedCommittedLineChangeHistory'] = {'summary': {'status': 'complete', 'commitCount': 1,
                    'additions': 1, 'deletions': 1, 'historyVersion': 'measurement'},
                    'commits': [{'commitSha': head, 'additions': 1, 'deletions': 1}]}
            write_json(workspace / 'task-commit-integration-provenance.json', {'schemaVersion': 1, 'tasks': {'measurement-task': entry}})
            measurements = []
            for stage in ('cold', 'warm', 'warm', 'dirty_changed', 'warm'):
                if stage == 'dirty_changed':
                    (repo / 'implementation.py').write_text('value = 3\n')
                    (repo / 'tests').mkdir()
                    (repo / 'tests/test_feature.py').write_text('assert True\n')
                started = time.perf_counter()
                evidence = resolve_task_review_starting_point(repo, 'measurement-task')
                resolved = time.perf_counter()
                packet = collect_task_review_decision_facts(repo, task_id='measurement-task', starting_point_evidence=evidence)
                finished = time.perf_counter()
                assert all(packet['facts'][name]['status'] == 'verified' for name in
                    ('task_identity', 'workspace_change_statistics', 'task_commit_history_statistics'))
                assert packet['measurement']['additional_worktree_capture_count'] == 0
                measurements.append({'stage': stage, 'task_card_statistics_available': with_task_card_statistics,
                    'collector_wall_ms_including_persistence': (finished - resolved) * 1000,
                    'existing_starting_point_resolver_wall_ms': (resolved - started) * 1000,
                    'new_base_ref_tip_probe_command_count': int(with_task_card_statistics),
                    **packet['measurement']})
            return measurements
    finally:
        os.environ.clear()
        os.environ.update(previous_environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=Path, default=Path(__file__).with_name('recent-skip-decision-samples.json'))
    parser.add_argument('--output-file', type=Path, required=True)
    arguments = parser.parse_args()
    replay = replay_samples(read_json(arguments.samples))
    measurements = measure_isolated_collection(with_task_card_statistics=True) + measure_isolated_collection(with_task_card_statistics=False)
    write_json(arguments.output_file, {'historical_replay': replay, 'isolated_collection_measurements': measurements})
    print(json.dumps({'historical_replay_summary': replay['summary'], 'measurements': measurements}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
