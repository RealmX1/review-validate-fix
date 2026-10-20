#!/usr/bin/env python3
"""session 归属的分层补判：命令文本 / 子代理写工具 / 时间窗 / HEAD 截断。

背景见 08ac4 调查报告 §5.5：Bash / 子代理 / mv·cp 写的文件、以及 fast-forward 到 base 后被提交时刻截断的
写入，原先一律落进 protected/background、不进正式审查。这里钉住四件事：

1. 命令文本解析只认**真实写入目标**（重定向、heredoc 内联脚本、sed -i、cp/mv 目标…），只读命令不算；
2. 各宿主的工具调用时间窗（含后台任务的完成通知、子代理 transcript、Codex 信封、Kimi wire）；
3. 时间窗判定的四种结局：本会话独占 → 登记；与别的会话重叠 / 只落在批量触碰命令期间 → 有争议；
   只落在别的会话期间 → 其他会话写的；谁都对不上 → 无主；
4. HEAD 截断：worktree 被快进到 base 后不再截掉更早的未提交写入；Claude Bash 里的 ``git commit`` 参与截断。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.session_scope_allocation import session_change_manifest  # noqa: E402
from core.session_scope_allocation.agent_sessions_that_worked_in_same_worktree_discovery import (  # noqa: E402
    discover_agent_sessions_that_worked_in_same_worktree,
)
from core.session_scope_allocation.agent_transcript_tool_call_time_windows_extraction import (  # noqa: E402
    extract_claude_code_session_tool_call_time_windows,
    extract_codex_rollout_tool_call_time_windows,
    extract_kimi_session_tool_call_time_windows,
)
from core.session_scope_allocation.layered_session_write_attribution_for_unattributed_dirty_paths import (  # noqa: E402
    EVIDENCE_FILE_WRITE_TOOL_CALL_BY_SUBAGENT,
    EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION,
    EVIDENCE_SHELL_COMMAND_NAMED_WRITE_TARGET,
    UNCOVERED_REASON_CONTESTED,
    UNCOVERED_REASON_NO_AGENT_SESSION_TOOL_CALL_MATCHES,
    UNCOVERED_REASON_TIME_WINDOW_SUGGESTS_THIS_SESSION_OUTSIDE_SINGLE_TASK_WORKTREE,
    UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION,
)
from core.session_scope_allocation.shell_command_named_write_target_paths_extraction import (  # noqa: E402
    analyze_shell_command_named_write_targets,
    resolve_shell_command_named_write_targets_to_dirty_paths,
    shell_command_runs_git_commit_inside_repository,
)


# --------------------------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------------------------


def _run_git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env={**os.environ, **(env or {})}
    )
    return completed.stdout


def _init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True)
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "rvf@example.test")
    _run_git(repo, "config", "user.name", "RVF Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    # 基线提交放在过去：transcript 里的调用都晚于它，不会被 HEAD 提交时刻截断。
    old_date = "2020-01-01T00:00:00+00:00"
    _run_git(repo, "commit", "-q", "-m", "base", env={"GIT_AUTHOR_DATE": old_date, "GIT_COMMITTER_DATE": old_date})
    return repo.resolve()


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    return path


class ClaudeTranscriptBuilder:
    """造 Claude Code transcript：assistant 记录里的 tool_use + user 记录里的 tool_result，带 cwd 与 sessionId。"""

    def __init__(self, session_id: str, cwd: Path) -> None:
        self.session_id = session_id
        self.cwd = str(cwd)
        self.records: list[dict[str, Any]] = []
        self._counter = 0

    def _base(self, record_type: str, at: float) -> dict[str, Any]:
        return {"type": record_type, "timestamp": _iso(at), "sessionId": self.session_id, "cwd": self.cwd}

    def user_prompt(self, at: float, text: str = "do the work") -> None:
        self.records.append({**self._base("user", at), "message": {"role": "user", "content": text}})

    def tool_call(
        self,
        name: str,
        tool_input: dict[str, Any],
        *,
        started_at: float,
        ended_at: float | None,
        is_error: bool = False,
        result_text: str = "ok",
        tool_use_id: str | None = None,
    ) -> str:
        self._counter += 1
        tool_use_id = tool_use_id or f"toolu_{self.session_id[:6]}_{self._counter:04d}"
        self.records.append(
            {
                **self._base("assistant", started_at),
                "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}]},
            }
        )
        if ended_at is not None:
            self.records.append(
                {
                    **self._base("user", ended_at),
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error, "content": result_text}],
                    },
                }
            )
        return tool_use_id

    def task_notification(self, tool_use_id: str, at: float) -> None:
        self.records.append(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": _iso(at),
                "sessionId": self.session_id,
                "content": f"<task-notification>\n<task-id>x</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n<status>completed</status>\n</task-notification>",
            }
        )

    def write(self, path: Path) -> Path:
        return _write_jsonl(path, self.records)


@pytest.fixture()
def isolated_agent_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """把三个宿主的会话目录都指到临时目录，避免读到本机真实会话。"""
    homes = {
        "claude_config": tmp_path / "claude-config",
        "codex_home": tmp_path / "codex-home",
        "kimi_home": tmp_path / "kimi-home",
    }
    for directory in homes.values():
        directory.mkdir()
    (homes["claude_config"] / "projects").mkdir()
    (homes["codex_home"] / "sessions").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(homes["claude_config"]))
    monkeypatch.setenv("CODEX_HOME", str(homes["codex_home"]))
    monkeypatch.setenv("KIMI_CODE_HOME", str(homes["kimi_home"]))
    monkeypatch.delenv("CODEX_RVF_COMMITTED_ROUND_BASE_REF", raising=False)
    return homes


ENVIRONMENT_VARIABLE_PREFIXES_INHERITED_FROM_RVF_OR_KANBAN_INVOKING_SHELL = ("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")


@pytest.fixture(autouse=True)
def rvf_state_roots_isolated_from_invoking_shell_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request
) -> dict[str, Path]:
    """清掉从调用方 shell 继承来的全部 RVF / Kanban 环境变量，再把 RVF 的各个状态根钉进 tmp。

    在 source 过 ``review-env.sh`` 的 reviewer / fixer shell 里跑本文件时，``RunLedger`` 会回落读
    ``RVF_RUN_ID`` / ``RVF_RUN_DIR``：端到端用例的 prepare 曾因此把活跃 run 当成自己的 run，
    整组冻结工件（scope.contract.json / session-manifest.json / tracker-scope.json / review-packet …）
    被临时仓库的内容覆盖。做法同 ``tests/test_stop_review_validate_fix.py`` 的 ``invoke()``：
    先按前缀整体清空，再显式钉根——只清不钉会回落到真实 ``~/.rvf``。"""
    for environment_variable_name in tuple(os.environ):
        if environment_variable_name.startswith(ENVIRONMENT_VARIABLE_PREFIXES_INHERITED_FROM_RVF_OR_KANBAN_INVOKING_SHELL):
            monkeypatch.delenv(environment_variable_name, raising=False)
    state_root = tmp_path / "rvf-state-isolated-from-invoking-shell"
    pinned_roots = {
        "state_root": state_root,
        "rvf_home": state_root / "rvf-home",
        "rvf_user_local_data_root": state_root / "user-local-data",
        "prepare_root": state_root / "prep-root",
        "kanban_followup_lock_root": state_root / "kanban-followup-in-progress",
    }
    monkeypatch.setenv("CODEX_RVF_STATE_DIR", str(pinned_roots["state_root"]))
    monkeypatch.setenv("RVF_HOME", str(pinned_roots["rvf_home"]))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(pinned_roots["rvf_user_local_data_root"]))
    monkeypatch.setenv("CODEX_RVF_PREP_ROOT", str(pinned_roots["prepare_root"]))
    monkeypatch.setenv("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", str(pinned_roots["kanban_followup_lock_root"]))
    if request.node.name.startswith("test_end_to_end_"):
        # 此簇使用 Claude 合成归属历史，只验证归属/lease/覆盖链；不冒充已验证的宿主分界。
        # 独立消息、Git 快照和直接入口 gate 由专用集成套件验证，Claude 原生 rewind 仍未取证。
        from _rvf_test_support.loader import load_script_module
        cli = load_script_module("rvf_agent_decision_cli")
        prepare = load_script_module("prepare_review_run")
        monkeypatch.setattr(cli, "require_review_start_boundary", lambda **kw: {"test_scope":"layered-attribution"})
        monkeypatch.setattr(cli, "require_prepared_run_review_start_boundary", lambda **kw: {"test_scope":"layered-attribution"})
        monkeypatch.setattr(prepare, "require_review_start_boundary_for_prepare", lambda **kw: {"test_scope":"layered-attribution"})
        for module in (cli, prepare):
            monkeypatch.setattr(module, "bind_review_start_boundary_to_one_run", lambda receipt, run_id, run_dir: (run_id, Path(run_dir) if run_dir is not None else None))
    return pinned_roots


def _claude_project_transcript_path(homes: dict[str, Path], cwd: Path, session_id: str) -> Path:
    slug = "".join(character if character.isalnum() else "-" for character in str(cwd))
    return homes["claude_config"] / "projects" / slug / f"{session_id}.jsonl"


def _build_manifest(repo: Path, transcript: Path, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("enable_layered_session_write_attribution", True)
    return session_change_manifest.build_manifest(repo, transcript, tracker_enabled=False, **kwargs)


def _init_kanban_single_task_worktree_repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / ".cline" / "worktrees" / "abcde" / "repo")


# --------------------------------------------------------------------------------------------
# 1. 命令文本
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "dirty_paths", "expected_paths"),
    [
        ("cat > notes/a.md <<'EOF'\n> not a redirect\nEOF", {"notes/a.md", "other.md"}, {"notes/a.md"}),
        (
            "python3 - <<'PY'\nfrom pathlib import Path\nPath('docs/report.md').write_text('mentions other.md')\nPY",
            {"docs/report.md", "other.md"},
            {"docs/report.md"},
        ),
        ("sed -i '' 's/a/b/' src/module.py && rg foo src", {"src/module.py", "src/untouched.py"}, {"src/module.py"}),
        ("cp /tmp/draft.html reports/final.html", {"reports/final.html"}, {"reports/final.html"}),
        ("mv scripts/old_tool.py tools/", {"scripts/old_tool.py", "tools/old_tool.py"}, {"scripts/old_tool.py", "tools/old_tool.py"}),
        ("rm -r build/", {"build/a.txt", "build/sub/b.txt", "keep.txt"}, {"build/a.txt", "build/sub/b.txt"}),
        ("cd sub && echo hi > out.txt", {"sub/out.txt", "out.txt"}, {"sub/out.txt"}),
        ("git mv a.txt b.txt", {"a.txt", "b.txt"}, {"a.txt", "b.txt"}),
        ("rg -n foo src; cat README.md; git diff HEAD", {"src/a.py", "README.md"}, set()),
        ("echo x > /dev/null 2>&1", {"x"}, set()),
        # sort -o / uniq 与 xxd 的第二个位置参数是输出文件
        ("sort -u -o data/sorted.txt data/in.txt", {"data/sorted.txt", "data/in.txt"}, {"data/sorted.txt"}),
        ("sort --output=data/sorted.txt data/in.txt", {"data/sorted.txt", "data/in.txt"}, {"data/sorted.txt"}),
        ("uniq -f 2 data/in.txt data/deduped.txt", {"data/deduped.txt", "data/in.txt"}, {"data/deduped.txt"}),
        ("xxd -r dump.hex firmware.bin", {"firmware.bin", "dump.hex"}, {"firmware.bin"}),
        ("sort -u data/in.txt | uniq -c; xxd -l 64 firmware.bin", {"data/in.txt", "firmware.bin"}, set()),
    ],
)
def test_shell_command_named_write_targets_resolve_only_real_write_targets(
    tmp_path: Path, command: str, dirty_paths: set[str], expected_paths: set[str]
) -> None:
    repo = tmp_path.resolve()
    analysis = analyze_shell_command_named_write_targets(command, str(repo))
    matches = resolve_shell_command_named_write_targets_to_dirty_paths(analysis, repository_root=repo, dirty_paths=dirty_paths)
    assert {match.repository_relative_path for match in matches} == expected_paths


def test_shell_command_bulk_work_tree_rewrites_are_flagged(tmp_path: Path) -> None:
    for command in ("git checkout main", "git stash pop", "npx prettier --write .", "git merge feature"):
        assert analyze_shell_command_named_write_targets(command, str(tmp_path)).rewrites_work_tree_in_bulk_or_unresolvably, command
    assert not analyze_shell_command_named_write_targets("pytest -q tests", str(tmp_path)).rewrites_work_tree_in_bulk_or_unresolvably


def test_shell_command_git_commit_detection_follows_cd_and_git_dash_c(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    elsewhere = (tmp_path / "elsewhere").resolve()

    def runs_commit(command: str) -> bool:
        return shell_command_runs_git_commit_inside_repository(
            analyze_shell_command_named_write_targets(command, str(repo)), repository_root=repo
        )

    assert runs_commit("git add -A && git commit -m 'x'")
    assert runs_commit("rvf land commit --message-file - <<'M'\nfix: x\nM")
    assert not runs_commit(f"cd {elsewhere} && git commit -m x")
    assert not runs_commit(f"git -C {elsewhere} commit -m x")
    assert not runs_commit("git log --oneline -1")


@pytest.mark.parametrize(
    ("command", "expected_read_only"),
    [
        ("sed -n '1,40p' a.py", True),
        ("sed 's/foo/bar/g' a.py | head -5", True),
        ("sed -n -e '/^def /p' -e '$=' a.py", True),
        ("sed ':a;N;$!ba;s/\\n/ /g' a.txt", True),
        ("git diff HEAD --stat && git log -p -1 && git show HEAD:a.py", True),
        # 解释器 / 脚本执行一律不透明
        ("awk 'BEGIN { print \"x\" > \"out.txt\" }'", False),
        ("source build.sh", False),
        (". ./gen.sh", False),
        # sed 的 w / W 写命令、s 命令的 w 标志、脚本来自文件
        ("sed -n 'w out.txt' a.txt", False),
        ("sed -n '/x/W out.txt' a.txt", False),
        ("sed 's/a/b/w changed.txt' a.txt", False),
        ("sed -n -e '1p' -e '/x/w out.txt' a.txt", False),
        ("sed -f edits.sed a.txt", False),
        # git diff / log / show 的 --output 写文件
        ("git diff --output=patch.diff", False),
        ("git log -p --output out.log", False),
        ("git show --output=x.txt HEAD", False),
        # sort / uniq / xxd 按参数判定：只往 stdout 输出时仍只读
        ("sort -u a.txt | uniq -c", True),
        ("sort -t, -k 2,2 -rn a.txt && uniq -f 1 a.txt -", True),
        ("xxd -c 16 -s 32 a.bin | head", True),
        ("sort -o out.txt in.txt", False),
        ("sort -uo out.txt in.txt", False),
        ("sort --output=out.txt in.txt", False),
        ("uniq in.txt out.txt", False),
        ("xxd -r dump.hex out.bin", False),
        ("xxd -rp dump.hex", False),
        ("xxd in.bin dump.hex", False),
    ],
)
def test_shell_command_read_only_classification_rejects_commands_that_can_write_files(
    tmp_path: Path, command: str, expected_read_only: bool
) -> None:
    assert analyze_shell_command_named_write_targets(command, str(tmp_path)).has_no_possible_write_effect is expected_read_only


def test_shell_command_git_commit_dry_run_and_help_are_not_commits(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()

    def runs_commit(command: str) -> bool:
        return shell_command_runs_git_commit_inside_repository(
            analyze_shell_command_named_write_targets(command, str(repo)), repository_root=repo
        )

    for command in ("git commit --dry-run -m x", "git commit -h", "git commit --help", "git add -A && git commit --short"):
        assert not runs_commit(command), command
    assert runs_commit("git commit -am 'fix dry-run handling'")

    # manifest 的 HEAD 截断点识别同样不把成功的 dry-run 当提交
    builder = ClaudeTranscriptBuilder("dry-run-committer", repo)
    builder.tool_call("Bash", {"command": "git commit --dry-run -m x"}, started_at=100.0, ended_at=101.0)
    builder.tool_call("Bash", {"command": "git commit -m x"}, started_at=110.0, ended_at=111.0)
    records = [{**record, "_line_number": line_number} for line_number, record in enumerate(builder.records, start=1)]
    assert session_change_manifest.successful_repository_git_commit_line_numbers_from_shell_records(repo, records) == [3]


def test_shell_command_unique_basename_fallback_only_when_working_directory_is_unknown(tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    dirty_paths = {"source/generated_report.md"}

    def matched(command: str, working_directory: str | None) -> list[tuple[str, str]]:
        analysis = analyze_shell_command_named_write_targets(command, working_directory)
        return [(match.repository_relative_path, match.match_kind)
                for match in resolve_shell_command_named_write_targets_to_dirty_paths(
                    analysis, repository_root=repo, dirty_paths=dirty_paths)]

    # 工作目录已知：确切路径 build/generated_report.md 不在脏集合里就不匹配，不改认别处的同名文件
    assert matched("echo generated > build/generated_report.md", str(repo)) == []
    assert matched("python3 - <<'PY'\nfrom pathlib import Path\nPath('build/generated_report.md').write_text('x')\nPY",
                   str(repo)) == []
    # 工作目录未知（初始 cwd 缺失或 cd 到未解析变量）时才允许唯一同名兜底
    assert matched("echo generated > build/generated_report.md", None) == [("source/generated_report.md", "unique_basename")]
    assert matched('cd "$OUT" && echo generated > generated_report.md', str(repo)) == [
        ("source/generated_report.md", "unique_basename")]


# --------------------------------------------------------------------------------------------
# 2. 各宿主的工具调用时间窗
# --------------------------------------------------------------------------------------------


def test_claude_time_windows_pair_results_extend_background_tasks_and_read_subagents(tmp_path: Path) -> None:
    builder = ClaudeTranscriptBuilder("claude-main-session", tmp_path)
    builder.user_prompt(1000.0)
    builder.tool_call("Read", {"file_path": "a.txt"}, started_at=1001.0, ended_at=1001.5)
    background = builder.tool_call(
        "Bash", {"command": "pytest -q", "run_in_background": True}, started_at=1002.0, ended_at=1002.1,
        result_text="Command running in background with ID: b1",
    )
    agent_call = builder.tool_call("Agent", {"prompt": "x"}, started_at=1003.0, ended_at=1050.0)
    builder.task_notification(background, 1040.0)
    transcript = builder.write(tmp_path / "claude-main-session.jsonl")
    subagent = ClaudeTranscriptBuilder("claude-main-session", tmp_path)
    subagent.tool_call("Edit", {"file_path": str(tmp_path / "sub.py")}, started_at=1010.0, ended_at=1011.0)
    subagent.write(tmp_path / "claude-main-session" / "subagents" / "agent-abc.jsonl")
    (tmp_path / "claude-main-session" / "subagents" / "agent-abc.meta.json").write_text(
        json.dumps({"toolUseId": agent_call}), encoding="utf-8"
    )

    collected = extract_claude_code_session_tool_call_time_windows(transcript)
    by_tool = {window.tool_name: window for window in collected.windows}
    assert collected.session_id == "claude-main-session"
    assert by_tool["Read"].tool_call_cannot_write_repository_files
    assert by_tool["Bash"].tool_call_ran_in_background
    assert by_tool["Bash"].ended_at_epoch_ms == 1040_000  # 结束时刻取完成通知，而不是立即返回的 tool_result
    assert by_tool["Agent"].tool_call_is_subagent_container_with_own_transcript
    assert by_tool["Edit"].tool_call_source == "subagent:abc"
    assert by_tool["Edit"].file_write_paths == (str(tmp_path / "sub.py"),)
    assert collected.subagent_transcript_paths_read == [str(tmp_path / "claude-main-session" / "subagents" / "agent-abc.jsonl")]


def test_codex_time_windows_use_item_envelope_and_legacy_process_session_chain(tmp_path: Path) -> None:
    rollout = _write_jsonl(
        tmp_path / "rollout-2026-10-05T00-00-00-11111111-1111-1111-1111-111111111111.jsonl",
        [
            {"timestamp": "2026-10-05T00:00:00.000Z", "type": "session_meta", "payload": {"id": "codex-s", "cwd": str(tmp_path)}},
            {
                "timestamp": "2026-10-05T00:01:10.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "CommandExecution", "id": "exec-1", "command": ["/bin/zsh", "-lc", "npm test"], "cwd": f"file://{tmp_path}", "exit_code": 0},
                    "started_at_ms": 1791158400000,
                    "completed_at_ms": 1791158470000,
                },
            },
            {
                "timestamp": "2026-10-05T00:02:00.000Z",
                "type": "response_item",
                "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": json.dumps({"cmd": "make build"})},
            },
            {
                "timestamp": "2026-10-05T00:02:10.000Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "c1", "output": "Process running with session ID 7"},
            },
            {
                "timestamp": "2026-10-05T00:03:00.000Z",
                "type": "response_item",
                "payload": {"type": "function_call", "name": "write_stdin", "call_id": "c2", "arguments": json.dumps({"session_id": 7})},
            },
            {
                "timestamp": "2026-10-05T00:04:00.000Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "c2", "output": "Process exited with code 0"},
            },
        ],
    )
    collected = extract_codex_rollout_tool_call_time_windows(rollout, include_subagent_rollouts=False)
    by_command = {window.shell_command_text: window for window in collected.windows}
    assert collected.session_id == "codex-s"
    assert by_command["npm test"].started_at_epoch_ms == 1791158400000
    assert by_command["npm test"].ended_at_epoch_ms == 1791158470000
    assert by_command["make build"].ended_at_epoch_ms == int(datetime(2026, 10, 5, 0, 4, tzinfo=timezone.utc).timestamp() * 1000)


def _codex_exec_custom_tool_call_records(call_id: str, javascript_text: str, *, called_at: str, output_at: str | None) -> list[dict[str, Any]]:
    """新 Codex（gpt-5.6-sol 世代）的 exec 单元：custom_tool_call name=exec（input 是 JS）+ custom_tool_call_output。"""
    records: list[dict[str, Any]] = [
        {
            "timestamp": called_at,
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": javascript_text},
        }
    ]
    if output_at is not None:
        records.append(
            {"timestamp": output_at, "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": "ok"}}
        )
    return records


def _codex_patch_apply_end_record(call_id: str, changes: dict[str, Any], *, at: str, success: bool = True) -> dict[str, Any]:
    return {
        "timestamp": at,
        "type": "event_msg",
        "payload": {"type": "patch_apply_end", "call_id": call_id, "turn_id": "turn-1", "success": success, "changes": changes, "status": "completed"},
    }


def _codex_exec_javascript_calling_exec_command(command: str, workdir: str) -> str:
    return f"const r = await tools.exec_command({json.dumps({'cmd': command, 'workdir': workdir})}); text(r.output);\n"


def test_codex_time_windows_cover_exec_custom_tool_call_with_patch_apply_end_and_embedded_exec_command_without_item_envelope(
    tmp_path: Path,
) -> None:
    """RVF-004：没有 item_completed 信封、只有「custom_tool_call(name=exec) + event_msg.patch_apply_end」的 rollout
    也要抽出写入窗口（成功判据与 synthesize_apply_patch_text_from_patch_apply_end 一致），exec 里内嵌的
    tools.exec_command 抽成 shell 窗口；有 item 的 rollout 不重复计（FileChange 与 patch_apply_end 按 call_id 去重）。"""

    def at(second: int) -> str:
        return f"2026-10-05T00:00:{second:02d}.000Z"

    def epoch_ms(second: int) -> int:
        return int(datetime(2026, 10, 5, 0, 0, second, tzinfo=timezone.utc).timestamp() * 1000)

    updated, renamed_source, renamed_target = (str(tmp_path / name) for name in ("a.py", "old.py", "new.py"))
    heredoc_command = "cat > notes.md <<'EOF'\nwritten by exec\nEOF"
    exec_only_rollout = _write_jsonl(
        tmp_path / "rollout-2026-10-05T00-00-00-33333333-3333-3333-3333-333333333333.jsonl",
        [
            {"timestamp": at(0), "type": "session_meta", "payload": {"id": "codex-exec-only", "cwd": str(tmp_path)}},
            *_codex_exec_custom_tool_call_records(
                "call_patch", f'const r = await tools.apply_patch("*** Begin Patch\\n*** Update File: {updated}\\n...");', called_at=at(10), output_at=None
            ),
            _codex_patch_apply_end_record(
                "exec-p1",
                {
                    updated: {"type": "update", "unified_diff": "@@ -1 +1 @@\n-a\n+b\n"},
                    renamed_source: {"type": "update", "move_path": renamed_target, "unified_diff": "@@ -1 +1 @@\n-c\n+d\n"},
                },
                at=at(11),
            ),
            {"timestamp": at(12), "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call_patch", "output": "ok"}},
            _codex_patch_apply_end_record("exec-p2", {str(tmp_path / "failed.py"): {"type": "add", "content": "x\n"}}, at=at(13), success=False),
            *_codex_exec_custom_tool_call_records(
                "call_shell", _codex_exec_javascript_calling_exec_command(heredoc_command, str(tmp_path / "sub")), called_at=at(20), output_at=at(25)
            ),
        ],
    )

    collected = extract_codex_rollout_tool_call_time_windows(exec_only_rollout, include_subagent_rollouts=False)

    assert [(window.tool_name, window.call_id) for window in collected.windows] == [("apply_patch", "exec-p1"), ("exec_command", "call_shell")]
    patch_window, shell_window = collected.windows
    assert patch_window.file_write_paths == (updated, renamed_source, renamed_target)
    assert (patch_window.started_at_epoch_ms, patch_window.ended_at_epoch_ms) == (epoch_ms(10), epoch_ms(12))  # exec 调用 → 输出
    assert patch_window.tool_call_failed is False
    assert shell_window.shell_command_text == heredoc_command
    assert shell_window.shell_working_directory == str(tmp_path / "sub")
    assert (shell_window.started_at_epoch_ms, shell_window.ended_at_epoch_ms) == (epoch_ms(20), epoch_ms(25))

    rollout_with_item_envelope = _write_jsonl(
        tmp_path / "rollout-2026-10-05T00-00-00-44444444-4444-4444-4444-444444444444.jsonl",
        [
            {"timestamp": at(0), "type": "session_meta", "payload": {"id": "codex-with-items", "cwd": str(tmp_path)}},
            *_codex_exec_custom_tool_call_records(
                "call_shell", _codex_exec_javascript_calling_exec_command("npm test", str(tmp_path)), called_at=at(10), output_at=at(15)
            ),
            {
                "timestamp": at(14),
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "FileChange", "id": "exec-p1", "changes": {updated: {"type": "update"}}},
                    "started_at_ms": epoch_ms(13),
                    "completed_at_ms": epoch_ms(14),
                },
            },
            _codex_patch_apply_end_record("exec-p1", {updated: {"type": "update", "unified_diff": "@@ -1 +1 @@\n-a\n+b\n"}}, at=at(14)),
        ],
    )

    collected_with_items = extract_codex_rollout_tool_call_time_windows(rollout_with_item_envelope, include_subagent_rollouts=False)

    assert [(window.tool_name, window.call_id) for window in collected_with_items.windows] == [("apply_patch", "exec-p1")]


def test_kimi_time_windows_pair_tool_call_ids(tmp_path: Path) -> None:
    session_directory = tmp_path / "sessions" / "wd_x_1" / "session_k"
    (session_directory / "state.json").parent.mkdir(parents=True)
    (session_directory / "state.json").write_text(json.dumps({"id": "session_k", "cwd": str(tmp_path)}), encoding="utf-8")
    wire = _write_jsonl(
        session_directory / "agents" / "main" / "wire.jsonl",
        [
            {"type": "context.append_loop_event", "time": 1000, "event": {"type": "tool.call", "toolCallId": "t1", "name": "Write", "args": {"path": "a.py"}}},
            {"type": "context.append_loop_event", "time": 1500, "event": {"type": "tool.result", "toolCallId": "t1"}},
            {"type": "context.append_loop_event", "time": 2000, "event": {"type": "tool.call", "toolCallId": "t2", "name": "Bash", "args": {"command": "ls"}}},
            {"type": "context.append_loop_event", "time": 2600, "event": {"type": "tool.result", "toolCallId": "t2"}},
        ],
    )
    collected = extract_kimi_session_tool_call_time_windows(wire)
    assert collected.session_id == "session_k"
    assert [(window.tool_name, window.started_at_epoch_ms, window.ended_at_epoch_ms) for window in collected.windows] == [
        ("Write", 1000, 1500),
        ("Bash", 2000, 2600),
    ]
    assert collected.windows[0].file_write_paths == ("a.py",)


def test_same_worktree_session_discovery_finds_each_host_and_excludes_self(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    worktree = (tmp_path / ".cline" / "worktrees" / "abcde" / "repo")
    worktree.mkdir(parents=True)
    worktree = worktree.resolve()
    other_worktree = (tmp_path / "other").resolve()
    other_worktree.mkdir()
    own = ClaudeTranscriptBuilder("own", worktree)
    own.user_prompt(time.time())
    own_path = own.write(_claude_project_transcript_path(isolated_agent_homes, worktree, "own"))
    peer = ClaudeTranscriptBuilder("peer", worktree / "sub")
    peer.user_prompt(time.time())
    peer.write(_claude_project_transcript_path(isolated_agent_homes, worktree / "sub", "peer"))
    stranger = ClaudeTranscriptBuilder("stranger", other_worktree)
    stranger.user_prompt(time.time())
    stranger.write(_claude_project_transcript_path(isolated_agent_homes, other_worktree, "stranger"))
    _write_jsonl(
        isolated_agent_homes["codex_home"] / "sessions" / "2026" / "10" / "05" / "rollout-x-22222222-2222-2222-2222-222222222222.jsonl",
        [{"type": "session_meta", "payload": {"id": "codex-peer", "cwd": str(worktree)}}],
    )
    kimi_session = isolated_agent_homes["kimi_home"] / "sessions" / "wd_repo_1" / "session_kp"
    _write_jsonl(kimi_session / "agents" / "main" / "wire.jsonl", [{"type": "metadata"}])
    (isolated_agent_homes["kimi_home"] / "session_index.jsonl").write_text(
        json.dumps({"sessionId": "session_kp", "sessionDir": str(kimi_session), "workDir": str(worktree)}) + "\n", encoding="utf-8"
    )

    result = discover_agent_sessions_that_worked_in_same_worktree(
        worktree, activity_lower_bound_epoch_ms=int((time.time() - 3600) * 1000), excluded_transcript_paths=[own_path]
    )
    found = {(session.host_kind, session.session_id) for session in result.sessions}
    assert found == {("claude_code", "peer"), ("codex", "codex-peer"), ("kimi", "session_kp")}
    assert all(session.is_same_kanban_task_as_target_worktree for session in result.sessions)


# --------------------------------------------------------------------------------------------
# 3. build_manifest 分层补判
# --------------------------------------------------------------------------------------------


def test_build_manifest_attributes_claude_bash_heredoc_write_by_command_text(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "notes.md").write_text("written by bash\n", encoding="utf-8")
    (repo / "background.txt").write_text("someone else\n", encoding="utf-8")
    now = time.time()
    builder = ClaudeTranscriptBuilder("bash-writer", repo)
    builder.user_prompt(now - 3600)
    builder.tool_call("Bash", {"command": "cat > notes.md <<'EOF'\nwritten by bash\nEOF"}, started_at=now - 3500, ended_at=now - 3499)
    transcript = builder.write(tmp_path / "bash-writer.jsonl")

    manifest = _build_manifest(repo, transcript)

    assert "notes.md" in manifest["owned_dirty_paths"]
    assert "background.txt" in manifest["unattributed_dirty_paths"]
    evidence = {item["path"]: item for item in manifest["attribution_evidence"]}
    assert evidence["notes.md"]["evidence"] == EVIDENCE_SHELL_COMMAND_NAMED_WRITE_TARGET
    assert evidence["notes.md"]["confidence"] == "medium"


def test_build_manifest_attributes_claude_subagent_edit(tmp_path: Path, isolated_agent_homes: dict[str, Path]) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("base\nsubagent\n", encoding="utf-8")
    now = time.time()
    builder = ClaudeTranscriptBuilder("parent", repo)
    builder.user_prompt(now - 100)
    agent_call = builder.tool_call("Agent", {"prompt": "edit"}, started_at=now - 90, ended_at=now - 10)
    transcript = builder.write(tmp_path / "parent.jsonl")
    subagent = ClaudeTranscriptBuilder("parent", repo)
    subagent.tool_call("Edit", {"file_path": str(repo / "tracked.txt")}, started_at=now - 80, ended_at=now - 79)
    subagent.write(tmp_path / "parent" / "subagents" / "agent-s1.jsonl")
    (tmp_path / "parent" / "subagents" / "agent-s1.meta.json").write_text(json.dumps({"toolUseId": agent_call}), encoding="utf-8")

    manifest = _build_manifest(repo, transcript)

    assert manifest["owned_dirty_paths"] == ["tracked.txt"]
    assert manifest["attribution_evidence"][0]["evidence"] == EVIDENCE_FILE_WRITE_TOOL_CALL_BY_SUBAGENT
    assert manifest["attribution_evidence"][0]["tool_call_source"] == "subagent:s1"


def test_build_manifest_attributes_codex_subagent_exec_patch_apply_end_and_embedded_exec_command_writes(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    """RVF-004：Codex 子代理 rollout 只有「custom_tool_call(name=exec) + event_msg.patch_apply_end」（没有 item_completed
    信封）时，补丁写的文件归为 file_write_tool_call_by_subagent，exec 里内嵌 exec_command 写的文件按命令文本归属。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("base\nsubagent\n", encoding="utf-8")
    (repo / "notes.md").write_text("written by exec\n", encoding="utf-8")
    (repo / "background.txt").write_text("someone else\n", encoding="utf-8")
    now = time.time()
    subagent_thread_id = "55555555-5555-5555-5555-555555555555"
    day_directory = isolated_agent_homes["codex_home"] / "sessions" / "2026" / "10" / "05"
    parent_rollout = _write_jsonl(
        day_directory / "rollout-2026-10-05T00-00-00-66666666-6666-6666-6666-666666666666.jsonl",
        [
            {"timestamp": _iso(now - 100), "type": "session_meta", "payload": {"id": "codex-parent", "cwd": str(repo)}},
            {"timestamp": _iso(now - 95), "type": "event_msg", "payload": {"type": "collab_agent_spawn_end", "new_thread_id": subagent_thread_id}},
        ],
    )
    _write_jsonl(
        day_directory / f"rollout-2026-10-05T00-00-05-{subagent_thread_id}.jsonl",
        [
            {"timestamp": _iso(now - 90), "type": "session_meta", "payload": {"id": subagent_thread_id, "cwd": str(repo)}},
            *_codex_exec_custom_tool_call_records(
                "call_patch",
                f'const r = await tools.apply_patch("*** Begin Patch\\n*** Update File: {repo / "tracked.txt"}\\n...");',
                called_at=_iso(now - 80),
                output_at=None,  # 输出在 patch_apply_end 之后，下面单独写
            ),
            _codex_patch_apply_end_record(
                "exec-patch-1", {str(repo / "tracked.txt"): {"type": "update", "unified_diff": "@@ -1 +1,2 @@\n base\n+subagent\n"}}, at=_iso(now - 79)
            ),
            {"timestamp": _iso(now - 78), "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call_patch", "output": "ok"}},
            *_codex_exec_custom_tool_call_records(
                "call_shell",
                _codex_exec_javascript_calling_exec_command("cat > notes.md <<'EOF'\nwritten by exec\nEOF", str(repo)),
                called_at=_iso(now - 70),
                output_at=_iso(now - 69),
            ),
        ],
    )

    manifest = _build_manifest(repo, parent_rollout)

    assert manifest["owned_dirty_paths"] == ["notes.md", "tracked.txt"]
    assert "background.txt" in manifest["unattributed_dirty_paths"]
    evidence = {item["path"]: item for item in manifest["attribution_evidence"]}
    assert evidence["tracked.txt"]["evidence"] == EVIDENCE_FILE_WRITE_TOOL_CALL_BY_SUBAGENT
    assert evidence["tracked.txt"]["tool_call_source"] == f"subagent:{subagent_thread_id}"
    assert evidence["notes.md"]["evidence"] == EVIDENCE_SHELL_COMMAND_NAMED_WRITE_TARGET
    assert evidence["notes.md"]["tool_call_source"] == f"subagent:{subagent_thread_id}"


def _session_that_ran_script_around_now(repo: Path, session_id: str, *, started_at: float, ended_at: float) -> ClaudeTranscriptBuilder:
    builder = ClaudeTranscriptBuilder(session_id, repo)
    builder.user_prompt(started_at - 60)
    builder.tool_call("Bash", {"command": "python3 tools/generate_report.py"}, started_at=started_at, ended_at=ended_at)
    return builder


def test_build_manifest_outside_single_task_worktree_time_window_only_hints(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    """非 Kanban 单任务 worktree：改动时刻只落在本会话调用期间也**不**自动归属，只打「可能是你写的」标签。"""
    repo = _init_repo(tmp_path / "repo")
    started_at = time.time() - 1
    (repo / "generated_report.md").write_text("generated\n", encoding="utf-8")
    ended_at = time.time() + 1
    transcript = _session_that_ran_script_around_now(repo, "own", started_at=started_at, ended_at=ended_at).write(
        _claude_project_transcript_path(isolated_agent_homes, repo, "own")
    )

    manifest = _build_manifest(repo, transcript)

    assert manifest["owned_dirty_paths"] == []
    hinted = manifest["uncovered_dirty_paths_by_reason"][UNCOVERED_REASON_TIME_WINDOW_SUGGESTS_THIS_SESSION_OUTSIDE_SINGLE_TASK_WORKTREE]
    assert hinted[0]["path"] == "generated_report.md"
    assert hinted[0]["possibly_written_by_this_session_command_excerpt"] == "python3 tools/generate_report.py"


def test_build_manifest_single_task_worktree_default_attributes_unowned_change(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    """Kanban 单任务 worktree：没有任何证据表明别的会话写了它 ⇒ 默认归本会话（弱证据），含 IDE 手改。"""
    repo = _init_kanban_single_task_worktree_repo(tmp_path)
    started_at = time.time() - 1
    (repo / "generated_report.md").write_text("generated\n", encoding="utf-8")
    ended_at = time.time() + 1
    transcript = _session_that_ran_script_around_now(repo, "own", started_at=started_at, ended_at=ended_at).write(
        _claude_project_transcript_path(isolated_agent_homes, repo, "own")
    )
    time.sleep(1.6 + 1.0)
    (repo / "hand_edit_in_ide.txt").write_text("ide\n", encoding="utf-8")

    manifest = _build_manifest(repo, transcript)

    assert manifest["owned_dirty_paths"] == ["generated_report.md", "hand_edit_in_ide.txt"]
    evidence = {item["path"]: item for item in manifest["attribution_evidence"]}
    assert {item["evidence"] for item in evidence.values()} == {
        EVIDENCE_KANBAN_SINGLE_TASK_WORKTREE_UNOWNED_DIRTY_CHANGE_DEFAULT_ATTRIBUTION
    }
    assert {item["confidence"] for item in evidence.values()} == {"weak"}
    assert evidence["generated_report.md"]["detail"]["possibly_written_by_this_session_command_excerpt"] == (
        "python3 tools/generate_report.py"
    )
    assert "possibly_written_by_this_session_command_excerpt" not in evidence["hand_edit_in_ide.txt"]["detail"]


def test_build_manifest_single_task_worktree_does_not_claim_change_made_during_other_session_write(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    """同一 worktree 另一个会话在改动时刻有可能写入的调用 ⇒ 不默认认领，判「其他会话写的」。"""
    repo = _init_kanban_single_task_worktree_repo(tmp_path)
    now = time.time()
    own = ClaudeTranscriptBuilder("own", repo)
    own.user_prompt(now - 7200)
    own.tool_call("Bash", {"command": "pytest -q"}, started_at=now - 7100, ended_at=now - 7090)
    transcript = own.write(_claude_project_transcript_path(isolated_agent_homes, repo, "own"))
    started_at = time.time() - 1
    (repo / "peer_output.txt").write_text("peer\n", encoding="utf-8")
    ended_at = time.time() + 1
    _session_that_ran_script_around_now(repo, "peer", started_at=started_at, ended_at=ended_at).write(
        _claude_project_transcript_path(isolated_agent_homes, repo, "peer")
    )

    manifest = _build_manifest(repo, transcript)

    assert manifest["owned_dirty_paths"] == []
    other = manifest["uncovered_dirty_paths_by_reason"][UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION]
    assert other[0]["path"] == "peer_output.txt"
    assert other[0]["other_sessions"][0]["session_id"] == "peer"
    assert other[0]["other_sessions"][0]["is_same_kanban_task"] is True


def test_build_manifest_single_task_worktree_read_only_reviewer_session_is_not_a_concurrent_writer(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    """同 worktree 的 RVF reviewer 只跑只读命令（rg / git diff）：不算并发写入者，照样默认认领。"""
    repo = _init_kanban_single_task_worktree_repo(tmp_path)
    started_at = time.time() - 1
    (repo / "generated_report.md").write_text("generated\n", encoding="utf-8")
    ended_at = time.time() + 1
    transcript = _session_that_ran_script_around_now(repo, "own", started_at=started_at, ended_at=ended_at).write(
        _claude_project_transcript_path(isolated_agent_homes, repo, "own")
    )
    reviewer = ClaudeTranscriptBuilder("reviewer", repo)
    reviewer.user_prompt(started_at - 60)
    reviewer.tool_call("Bash", {"command": "rg -n foo . && git diff HEAD | head -50"}, started_at=started_at, ended_at=ended_at)
    reviewer.write(_claude_project_transcript_path(isolated_agent_homes, repo, "reviewer"))

    manifest = _build_manifest(repo, transcript)

    assert manifest["owned_dirty_paths"] == ["generated_report.md"]


def test_layered_attribution_skips_paths_held_by_other_sessions_with_strong_evidence(tmp_path: Path) -> None:
    from core.session_scope_allocation.layered_session_write_attribution_for_unattributed_dirty_paths import (
        attribute_unattributed_dirty_paths_with_layered_session_write_evidence,
    )

    repo = _init_kanban_single_task_worktree_repo(tmp_path)
    (repo / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    builder = ClaudeTranscriptBuilder("own", repo)
    builder.user_prompt(time.time() - 10)
    transcript = builder.write(tmp_path / "own.jsonl")

    result = attribute_unattributed_dirty_paths_with_layered_session_write_evidence(
        repository_root=repo,
        main_transcript_path=transcript,
        session_id="own",
        unattributed_dirty_paths={"theirs.txt", "mine.txt"},
        main_transcript_cutoff_line_number=None,
        attribution_lower_bound_epoch_ms=None,
        paths_held_by_other_sessions_with_strong_evidence={"theirs.txt"},
        enable_time_window_layer=False,
    )

    assert result.attributed_paths() == {"mine.txt"}
    assert result.uncovered_dirty_paths_by_reason[UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION][0]["path"] == "theirs.txt"


def test_build_manifest_time_window_marks_overlap_with_other_session_as_contested(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    repo = _init_kanban_single_task_worktree_repo(tmp_path)
    started_at = time.time() - 1
    (repo / "generated_report.md").write_text("generated\n", encoding="utf-8")
    ended_at = time.time() + 1
    transcript = _session_that_ran_script_around_now(repo, "own", started_at=started_at, ended_at=ended_at).write(
        _claude_project_transcript_path(isolated_agent_homes, repo, "own")
    )
    _session_that_ran_script_around_now(repo, "peer", started_at=started_at, ended_at=ended_at).write(
        _claude_project_transcript_path(isolated_agent_homes, repo, "peer")
    )

    manifest = _build_manifest(repo, transcript)

    assert manifest["owned_dirty_paths"] == []
    contested = manifest["uncovered_dirty_paths_by_reason"][UNCOVERED_REASON_CONTESTED]
    assert contested[0]["path"] == "generated_report.md"
    assert contested[0]["other_sessions"][0]["session_id"] == "peer"


def test_build_manifest_time_window_reports_other_session_and_unmatched_changes(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    now = time.time()
    own = ClaudeTranscriptBuilder("own", repo)
    own.user_prompt(now - 7200)
    own.tool_call("Bash", {"command": "pytest -q"}, started_at=now - 7100, ended_at=now - 7090)
    transcript = own.write(_claude_project_transcript_path(isolated_agent_homes, repo, "own"))
    started_at = time.time() - 1
    (repo / "peer_output.txt").write_text("peer\n", encoding="utf-8")
    ended_at = time.time() + 1
    _session_that_ran_script_around_now(repo, "peer", started_at=started_at, ended_at=ended_at).write(
        _claude_project_transcript_path(isolated_agent_homes, repo, "peer")
    )
    # 谁的调用期间都对不上的改动（例如 IDE 手改）：放在两个会话的窗口之后。
    time.sleep(1.6 + 1.0)
    (repo / "hand_edit.txt").write_text("ide\n", encoding="utf-8")

    manifest = _build_manifest(repo, transcript)

    by_reason = manifest["uncovered_dirty_paths_by_reason"]
    assert [entry["path"] for entry in by_reason[UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION]] == ["peer_output.txt"]
    assert by_reason[UNCOVERED_REASON_WRITTEN_BY_OTHER_AGENT_SESSION][0]["other_sessions"][0]["is_same_kanban_task"] is False
    assert [entry["path"] for entry in by_reason[UNCOVERED_REASON_NO_AGENT_SESSION_TOOL_CALL_MATCHES]] == ["hand_edit.txt"]
    assert manifest["owned_dirty_paths"] == []


def test_build_manifest_layered_attribution_is_off_unless_explicitly_enabled(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    """遗留 Stop 路由调 build_manifest 不开分层补判（复核 M14）：Bash 写的文件照旧不归属。"""
    repo = _init_kanban_single_task_worktree_repo(tmp_path)
    (repo / "notes.md").write_text("written by bash\n", encoding="utf-8")
    now = time.time()
    builder = ClaudeTranscriptBuilder("own", repo)
    builder.user_prompt(now - 100)
    builder.tool_call("Bash", {"command": "echo hi > notes.md"}, started_at=now - 90, ended_at=now - 89)
    transcript = builder.write(tmp_path / "own.jsonl")

    manifest = _build_manifest(repo, transcript, enable_layered_session_write_attribution=False)

    assert manifest["owned_dirty_paths"] == []
    assert manifest["layered_session_write_attribution"]["status"] == "disabled_by_caller"


def test_build_manifest_serena_write_tool_and_failed_edit(tmp_path: Path, isolated_agent_homes: dict[str, Path]) -> None:
    """Serena 写工具按 relative_path 计入第 1 层（复核 M5）；报错的 Edit 不算写过。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "failed.txt").write_text("changed elsewhere\n", encoding="utf-8")
    now = time.time()
    builder = ClaudeTranscriptBuilder("own", repo)
    builder.user_prompt(now - 100)
    agent_call = builder.tool_call("Agent", {"prompt": "x"}, started_at=now - 95, ended_at=now - 10)
    transcript = builder.write(tmp_path / "own.jsonl")
    subagent = ClaudeTranscriptBuilder("own", repo)
    subagent.tool_call(
        "mcp__serena__replace_symbol_body", {"relative_path": "src/module.py", "name_path": "x"}, started_at=now - 90, ended_at=now - 89
    )
    subagent.tool_call("Edit", {"file_path": str(repo / "failed.txt")}, started_at=now - 80, ended_at=now - 79, is_error=True)
    subagent.write(tmp_path / "own" / "subagents" / "agent-s1.jsonl")
    (tmp_path / "own" / "subagents" / "agent-s1.meta.json").write_text(json.dumps({"toolUseId": agent_call}), encoding="utf-8")

    manifest = _build_manifest(repo, transcript, enable_file_change_time_window_attribution=False)

    assert manifest["owned_dirty_paths"] == ["src/module.py"]
    assert manifest["attribution_evidence"][0]["detail"]["tool_name"] == "mcp__serena__replace_symbol_body"


def test_shell_command_read_only_classification(tmp_path: Path) -> None:
    def read_only(command: str) -> bool:
        return analyze_shell_command_named_write_targets(command, str(tmp_path)).has_no_possible_write_effect

    assert read_only("rg -n foo src | head -20 && git diff HEAD --stat; git log --oneline -5")
    assert read_only("sed -n '1,40p' a.py && cat b.py && ls -la && rvf wait")
    assert read_only("git -C /tmp/x status --short && git stash list")
    assert not read_only("python3 tools/generate_report.py")
    assert not read_only("sed -i '' 's/a/b/' a.py")
    assert not read_only("echo hi > out.txt")
    assert not read_only("git stash")
    assert not read_only("find . -name '*.pyc' -delete")


def test_shell_command_git_restore_is_not_authorship_and_inline_mention_is_not_a_write(tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    for command in (
        "git restore src/a.py",
        "git checkout -- src/a.py",
        "python3 - <<'PY'\nimport json\ndata = json.load(open('src/a.py'))\nopen(out_path, 'w').write('x')\nPY",
    ):
        analysis = analyze_shell_command_named_write_targets(command, str(repo))
        matches = resolve_shell_command_named_write_targets_to_dirty_paths(analysis, repository_root=repo, dirty_paths={"src/a.py"})
        assert matches == [], command


def test_agent_explicit_claims_are_bound_to_claimed_file_content(tmp_path: Path) -> None:
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    repo = _init_repo(tmp_path / "repo")
    log_root = tmp_path / "rvf-log-root"
    (repo / "claimed.txt").write_text("v1\n", encoding="utf-8")
    reviewable_unit_diff_tracker.record_session_path_claims_by_agent_explicit_declaration(
        repo=repo, session_id="S", paths=["claimed.txt"], reason="written by python script", log_root_override=log_root
    )
    assert reviewable_unit_diff_tracker.list_still_valid_session_path_claims_by_agent_explicit_declaration(
        repo=repo, session_id="S", log_root_override=log_root
    ) == {"claimed.txt"}
    (repo / "claimed.txt").write_text("v2 by someone else\n", encoding="utf-8")
    assert reviewable_unit_diff_tracker.list_still_valid_session_path_claims_by_agent_explicit_declaration(
        repo=repo, session_id="S", log_root_override=log_root
    ) == set()


def test_orphan_cleanup_does_not_supersede_unit_still_held_by_other_session(tmp_path: Path) -> None:
    from core.session_scope_allocation import reviewable_unit_diff_tracker
    from core.session_scope_allocation.reviewable_unit_diff_tracker import OwnedUnit

    repo = _init_repo(tmp_path / "repo")
    log_root = tmp_path / "rvf-log-root"
    (repo / "tracked.txt").write_text("base\nshared\n", encoding="utf-8")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    shared_unit = OwnedUnit(path="tracked.txt", unit="path", hunk_anchor=None)
    for session_id in ("A", "B"):
        reviewable_unit_diff_tracker.register_claims(
            repo=repo, session_id=session_id, run_id=None, worktree=repo, branch=None,
            owned_paths=["tracked.txt"], apply_patch_paths=set(), exec_only_paths={"tracked.txt"},
            owned_units_override=[(shared_unit, "shell_command_named_write_target")], log_root_override=log_root,
        )
    # A 不再声明 tracked.txt（只声明 other.txt）：B 仍持有，单元不得被全局 superseded。
    reviewable_unit_diff_tracker.register_claims(
        repo=repo, session_id="A", run_id=None, worktree=repo, branch=None,
        owned_paths=["other.txt"], apply_patch_paths=set(), exec_only_paths={"other.txt"},
        owned_units_override=[(OwnedUnit(path="other.txt", unit="path", hunk_anchor=None), "shell_command_named_write_target")],
        log_root_override=log_root,
    )
    db = next(log_root.rglob("tracker.sqlite3"))
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT u.observed_state, u.is_tombstoned, su.session_id FROM units u JOIN session_units su USING(unit_id) WHERE u.path='tracked.txt'"
        ).fetchall()
    assert rows and all(state != "superseded" and tombstoned == 0 for state, tombstoned, _ in rows)
    assert {session for _, _, session in rows} == {"B"}


# --------------------------------------------------------------------------------------------
# 端到端：rvf start（真实 prepare + tracker）→ 覆盖闸 → scope claim → dispatch
# --------------------------------------------------------------------------------------------


def test_end_to_end_start_reports_coverage_claim_moves_path_into_formal_review_and_dispatch_passes(
    tmp_path: Path,
    isolated_agent_homes: dict[str, Path],
    rvf_state_roots_isolated_from_invoking_shell_environment: dict[str, Path],
) -> None:
    """Edit 写的、Bash heredoc 写的文件自动进正式审查；谁都对不上的 IDE 手改留在未覆盖清单；dispatch 被覆盖闸拦下；
    ``scope claim`` 之后它进 fix_allowlist / primary_units、移出 protected、lease 重分配、packet 重渲；dispatch 放行。
    全部落盘隔离在 tmp（RVF_HOME / state / prep / lock / tracker），不碰真实 ~/.rvf，也不碰调用方 shell 的活跃 run
    （见 autouse 夹具 ``rvf_state_roots_isolated_from_invoking_shell_environment``）。"""
    from _rvf_test_support.loader import load_script_module

    kanban_followup_lock_root = rvf_state_roots_isolated_from_invoking_shell_environment["kanban_followup_lock_root"]

    repo = _init_repo(tmp_path / "repo")
    (repo / "edited.py").write_text("x = 1\n", encoding="utf-8")
    _run_git(repo, "add", "edited.py")
    old_date = "2020-01-02T00:00:00+00:00"
    _run_git(repo, "commit", "-q", "-m", "add edited.py", env={"GIT_AUTHOR_DATE": old_date, "GIT_COMMITTER_DATE": old_date})
    now = time.time()
    builder = ClaudeTranscriptBuilder("e2e-claim-session", repo)
    builder.user_prompt(now - 300)
    builder.tool_call("Edit", {"file_path": str(repo / "edited.py")}, started_at=now - 290, ended_at=now - 289)
    builder.tool_call("Bash", {"command": "cat > notes.md <<'EOF'\nnotes\nEOF"}, started_at=now - 280, ended_at=now - 279)
    transcript = builder.write(_claude_project_transcript_path(isolated_agent_homes, repo, "e2e-claim-session"))
    (repo / "edited.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    (repo / "hand_edit.txt").write_text("edited in an IDE\n", encoding="utf-8")

    cli = load_script_module("rvf_agent_decision_cli")
    dts = load_script_module("rvf_decision_token_store")
    roots = cli.AgentDecisionRoots(
        token_root=tmp_path / "decision-tokens",
        cooldown_root=tmp_path / "declined-fingerprints",
        scope_adjustment_root=tmp_path / "scope-adjustments",
        telemetry_root=tmp_path / "decision-telemetry",
        scope_issue_ledger_root=tmp_path / "scope-issue-ledger",
        declined_reason_ledger_root=tmp_path / "declined-reasons",
    )
    candidates = ["edited.py", "hand_edit.txt", "notes.md"]
    token = dts.mint_decision_token(
        worktree=str(repo), session_id="e2e-claim-session", candidate_fingerprint="fp-e2e",
        candidate_summary={"candidates": [{"path": path, "included_in_candidate_set": True} for path in candidates]},
        root=roots.token_root, cooldown_root=roots.cooldown_root,
        circuit_breaker_root_dir=tmp_path / "circuit-breaker",
    ).token

    start = cli.run_start(
        token=token, reason="e2e", repo=str(repo), session_id="e2e-claim-session", transcript_path=str(transcript),
        roots=roots, lock_root=kanban_followup_lock_root,
        provider_health_runner=lambda requirement, timeout: {"status": "ok", "provider": "stub"},
    )
    assert start.status == "ok", start.message
    assert start.detail["tracker_scope_allocation"]["status"] == cli.AGENTIC_INITIATION_TRACKER_SCOPE_ALLOCATED, start.detail
    coverage = start.detail["review_scope_coverage"]
    assert coverage["formal_review_covered_candidate_path_count"] == 2, coverage
    assert coverage["uncovered_candidate_paths_without_agent_decision"] == ["hand_edit.txt"], coverage
    assert "正式审查覆盖 2/3 个候选路径" in start.message
    artifacts = start.detail["prepare_artifacts"]
    contract_path = Path(artifacts["scope_contract"])
    contract_before = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract_before["scope_mode"] == "session-owned"
    assert "hand_edit.txt" in contract_before["protected_files"]

    Path(artifacts["scope_of_work"]).write_text("# Scope of Work\n\ne2e：edited.py、notes.md、hand_edit.txt。\n", encoding="utf-8")
    assert cli.run_scope_rerender_scope_of_work(token=token, roots=roots).status == "ok"

    def fake_reviewer_dispatcher(**kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "returncode": 0, "plan_path": None}

    blocked = cli.run_dispatch(token=token, strictness="light", roots=roots, dry_run=True, reviewer_dispatcher=fake_reviewer_dispatcher)
    assert blocked.exit_code == cli.EXIT_UNCOVERED_CANDIDATE_PATHS_NEED_AGENT_DECISION, blocked.message

    claim = cli.run_scope_claim(token=token, paths=["hand_edit.txt"], reason="本会话在 IDE 里改的", roots=roots)
    assert claim.status == "ok", claim.message
    assert claim.detail["claim"]["run_has_tracker_lease_after_claim"] is True, claim.detail
    contract_after = json.loads(contract_path.read_text(encoding="utf-8"))
    assert "hand_edit.txt" in contract_after["fix_allowlist"]
    assert "hand_edit.txt" in contract_after["primary_files"]
    assert "hand_edit.txt" not in contract_after["protected_files"]
    assert not set(contract_after["primary_files"]) & set(contract_after["protected_files"])
    assert set(contract_before["primary_units"]) < set(contract_after["primary_units"])
    assert contract_after["tracker_lease_id"] != contract_before["tracker_lease_id"]
    assert contract_after["scope_hash"] != contract_before["scope_hash"]
    assert claim.detail["claim"]["packet_rerender"]["status"] == "rerendered"
    assert "正式审查覆盖 3/3 个候选路径" in claim.message

    dispatched = cli.run_dispatch(token=token, strictness="light", roots=roots, dry_run=True, reviewer_dispatcher=fake_reviewer_dispatcher)
    assert dispatched.exit_code == cli.EXIT_OK, dispatched.message


def test_end_to_end_prepare_does_not_write_active_rvf_run_inherited_from_invoking_shell(tmp_path: Path) -> None:
    """回归（RVF-001）：模拟 source 过 ``review-env.sh`` 的 reviewer / fixer shell——子进程带着指向诱饵目录的
    ``RVF_RUN_DIR`` / ``RVF_RUN_ID`` 及各 RVF 状态根跑端到端用例，断言诱饵目录根本没被创建。

    必须走子进程：进程内 ``setenv`` 发生在 autouse 夹具清理之后，那是用例自己设的 run，生产代码照写不误，
    验证不到「外部 shell 带进来的变量被夹具挡掉」这件事。"""
    decoy_root = tmp_path / "decoy-active-rvf-run-of-invoking-shell"
    decoy_environment = {
        "RVF_RUN_DIR": str(decoy_root / "active-run"),
        "RVF_RUN_ID": "decoy-active-run",
        "RVF_HOME": str(decoy_root / "rvf-home"),
        "RVF_USER_LOCAL_DATA_ROOT": str(decoy_root / "user-local-data"),
        "CODEX_RVF_STATE_DIR": str(decoy_root / "state"),
        "CODEX_RVF_LOG_ROOT": str(decoy_root / "log-root"),
        "CODEX_RVF_PREP_ROOT": str(decoy_root / "prep-root"),
        "RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(decoy_root / "kanban-followup-in-progress"),
    }
    end_to_end_test_node_id = (
        f"{Path(__file__).resolve()}::"
        f"{test_end_to_end_start_reports_coverage_claim_moves_path_into_formal_review_and_dispatch_passes.__name__}"
    )
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--basetemp={tmp_path / 'inner-pytest-basetemp'}", end_to_end_test_node_id,
        ],
        cwd=REPO_ROOT,
        env={**os.environ, **decoy_environment},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout, completed.stdout
    assert not decoy_root.exists(), sorted(str(path.relative_to(decoy_root)) for path in decoy_root.rglob("*"))


def _fake_reviewer_dispatcher_that_never_runs_a_reviewer(**kwargs: Any) -> dict[str, Any]:
    return {"status": "ok", "returncode": 0, "plan_path": None}


def _start_agentic_initiation_run_leaving_ide_hand_edit_uncovered(
    tmp_path: Path, isolated_agent_homes: dict[str, Path], kanban_followup_lock_root: Path
) -> dict[str, Any]:
    """与上面端到端用例同一套隔离落盘（状态根由 autouse 夹具钉进 tmp）：``rvf start`` 后 edited.py / notes.md
    进正式审查（有 tracker lease），IDE 手改的 hand_edit.txt 未覆盖、在 protected 里；scope-of-work 已写实、可直接 dispatch。"""
    from _rvf_test_support.loader import load_script_module

    repo = _init_repo(tmp_path / "repo")
    (repo / "edited.py").write_text("x = 1\n", encoding="utf-8")
    _run_git(repo, "add", "edited.py")
    old_date = "2020-01-02T00:00:00+00:00"
    _run_git(repo, "commit", "-q", "-m", "add edited.py", env={"GIT_AUTHOR_DATE": old_date, "GIT_COMMITTER_DATE": old_date})
    now = time.time()
    session_id = "e2e-claim-lease-session"
    builder = ClaudeTranscriptBuilder(session_id, repo)
    builder.user_prompt(now - 300)
    builder.tool_call("Edit", {"file_path": str(repo / "edited.py")}, started_at=now - 290, ended_at=now - 289)
    builder.tool_call("Bash", {"command": "cat > notes.md <<'EOF'\nnotes\nEOF"}, started_at=now - 280, ended_at=now - 279)
    transcript = builder.write(_claude_project_transcript_path(isolated_agent_homes, repo, session_id))
    (repo / "edited.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    (repo / "hand_edit.txt").write_text("edited in an IDE\n", encoding="utf-8")

    cli = load_script_module("rvf_agent_decision_cli")
    dts = load_script_module("rvf_decision_token_store")
    roots = cli.AgentDecisionRoots(
        token_root=tmp_path / "decision-tokens",
        cooldown_root=tmp_path / "declined-fingerprints",
        scope_adjustment_root=tmp_path / "scope-adjustments",
        telemetry_root=tmp_path / "decision-telemetry",
        scope_issue_ledger_root=tmp_path / "scope-issue-ledger",
        declined_reason_ledger_root=tmp_path / "declined-reasons",
    )
    candidates = ["edited.py", "hand_edit.txt", "notes.md"]
    token = dts.mint_decision_token(
        worktree=str(repo), session_id=session_id, candidate_fingerprint="fp-e2e-lease",
        candidate_summary={"candidates": [{"path": path, "included_in_candidate_set": True} for path in candidates]},
        root=roots.token_root, cooldown_root=roots.cooldown_root,
        circuit_breaker_root_dir=tmp_path / "circuit-breaker",
    ).token
    start = cli.run_start(
        token=token, reason="e2e", repo=str(repo), session_id=session_id, transcript_path=str(transcript),
        roots=roots, lock_root=kanban_followup_lock_root,
        provider_health_runner=lambda requirement, timeout: {"status": "ok", "provider": "stub"},
    )
    assert start.status == "ok", start.message
    assert start.detail["tracker_scope_allocation"]["status"] == cli.AGENTIC_INITIATION_TRACKER_SCOPE_ALLOCATED, start.detail
    artifacts = start.detail["prepare_artifacts"]
    contract_path = Path(artifacts["scope_contract"])
    contract_before = json.loads(contract_path.read_text(encoding="utf-8"))
    assert isinstance(contract_before["tracker_lease_id"], str) and contract_before["primary_units"]
    assert "hand_edit.txt" in contract_before["protected_files"]
    Path(artifacts["scope_of_work"]).write_text("# Scope of Work\n\ne2e：edited.py、notes.md、hand_edit.txt。\n", encoding="utf-8")
    assert cli.run_scope_rerender_scope_of_work(token=token, roots=roots).status == "ok"
    return {
        "cli": cli, "roots": roots, "token": token, "repo": repo, "transcript": transcript,
        "contract_path": contract_path, "contract_before": contract_before,
    }


def test_end_to_end_claim_of_path_held_by_other_session_active_lease_stays_uncovered_and_dispatch_still_blocked(
    tmp_path: Path,
    isolated_agent_homes: dict[str, Path],
    rvf_state_roots_isolated_from_invoking_shell_environment: dict[str, Path],
) -> None:
    """认领路径在别的会话的在途 lease 里、进不了新的 tracker scope：不得并进 fix_allowlist / primary_files 冒充覆盖
    （reviewer 按 primary_units 审，它实际不在必审范围里），结果单列、回执说明，dispatch 覆盖闸照拦。"""
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    run = _start_agentic_initiation_run_leaving_ide_hand_edit_uncovered(
        tmp_path, isolated_agent_homes, rvf_state_roots_isolated_from_invoking_shell_environment["kanban_followup_lock_root"]
    )
    cli, roots, token, repo = run["cli"], run["roots"], run["token"], run["repo"]
    reviewable_unit_diff_tracker.register_claims(
        repo=repo, session_id="peer-session", run_id="peer-run", worktree=repo, branch=None,
        owned_paths=["hand_edit.txt"], apply_patch_paths=set(), exec_only_paths={"hand_edit.txt"},
    )
    peer_allocation = reviewable_unit_diff_tracker.allocate_review_scope(
        repo=repo, session_id="peer-session", run_id="peer-run", reviewer_id="peer-reviewer",
        dry_run=False, auto_claim_observed=False,
    )
    assert peer_allocation["status"] == "allocated", peer_allocation

    claim = cli.run_scope_claim(
        token=token, paths=["hand_edit.txt"], reason="本会话在 IDE 里改的", takeover_reason="确认是本会话的工作", roots=roots
    )

    assert claim.status == "ok", claim.message
    claim_result = claim.detail["claim"]
    assert claim_result["tracker_lease_state_after_claim"] == (
        cli.review_scope_coverage_and_claim.TRACKER_LEASE_STATE_AFTER_CLAIM_PRIOR_LEASE_RELEASED_AND_NEW_LEASE_ALLOCATED
    ), claim_result
    assert claim_result["run_has_tracker_lease_after_claim"] is True
    assert claim_result["claimed_paths_admitted_into_formal_review_scope"] == []
    assert claim_result["claimed_paths_not_admitted_into_formal_review_scope"] == ["hand_edit.txt"]
    contract_after = json.loads(run["contract_path"].read_text(encoding="utf-8"))
    assert "hand_edit.txt" not in contract_after["fix_allowlist"]
    assert "hand_edit.txt" not in contract_after["primary_files"]
    assert "hand_edit.txt" in contract_after["protected_files"]
    assert not set(contract_after["primary_files"]) & set(contract_after["protected_files"])
    assert "0 个路径已认领进正式审查" in claim.message
    assert "没能进新的 tracker scope" in claim.message and "hand_edit.txt" in claim.message
    assert "正式审查覆盖 2/3 个候选路径" in claim.message

    blocked = cli.run_dispatch(
        token=token, strictness="light", roots=roots, dry_run=True,
        reviewer_dispatcher=_fake_reviewer_dispatcher_that_never_runs_a_reviewer,
    )
    assert blocked.exit_code == cli.EXIT_UNCOVERED_CANDIDATE_PATHS_NEED_AGENT_DECISION, blocked.message


def test_end_to_end_claim_reallocation_failure_clears_dead_lease_fields_from_scope_contract(
    tmp_path: Path,
    isolated_agent_homes: dict[str, Path],
    rvf_state_roots_isolated_from_invoking_shell_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 lease 已释放、重新分配没成：contract 不得再引用死 lease（finalize 会拿它收尾）——lease 字段清成 None，
    退回按文件定范围、认领路径照常进 fix_allowlist，结果与回执都明说「本轮已无 lease」。"""
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    run = _start_agentic_initiation_run_leaving_ide_hand_edit_uncovered(
        tmp_path, isolated_agent_homes, rvf_state_roots_isolated_from_invoking_shell_environment["kanban_followup_lock_root"]
    )
    cli, roots, token = run["cli"], run["roots"], run["token"]
    contract_before = run["contract_before"]

    def allocate_review_scope_that_reports_nothing_allocated(**kwargs: Any) -> dict[str, Any]:
        return {"status": "empty", "lease_id": None}

    monkeypatch.setattr(reviewable_unit_diff_tracker, "allocate_review_scope", allocate_review_scope_that_reports_nothing_allocated)

    claim = cli.run_scope_claim(token=token, paths=["hand_edit.txt"], reason="本会话在 IDE 里改的", roots=roots)

    assert claim.status == "ok", claim.message
    claim_result = claim.detail["claim"]
    assert claim_result["tracker_lease_state_after_claim"] == (
        cli.review_scope_coverage_and_claim.TRACKER_LEASE_STATE_AFTER_CLAIM_PRIOR_LEASE_RELEASED_AND_REALLOCATION_FAILED_RUN_HAS_NO_LEASE
    ), claim_result
    assert claim_result["tracker_reallocation"]["released_lease_id"] == contract_before["tracker_lease_id"]
    assert claim_result["run_has_tracker_lease_after_claim"] is False
    assert claim_result["tracker_lease_id_after_claim"] is None
    for contract_copy_path in {run["contract_path"], run["contract_path"].parent / "inputs" / "scope.contract.json"}:
        if not contract_copy_path.is_file():
            continue
        contract_after = json.loads(contract_copy_path.read_text(encoding="utf-8"))
        assert contract_after["tracker_lease_id"] is None, contract_copy_path
        assert contract_after["tracker_scope_hash"] is None, contract_copy_path
        assert contract_after["primary_units"] is None, contract_copy_path
        assert "hand_edit.txt" in contract_after["fix_allowlist"]
        assert not set(contract_after["primary_files"]) & set(contract_after["protected_files"])
    assert "本轮已无 tracker lease" in claim.message
    assert "正式审查覆盖 3/3 个候选路径" in claim.message


def test_end_to_end_claim_without_transcript_keeps_prior_lease_and_reports_claimed_path_not_admitted(
    tmp_path: Path,
    isolated_agent_homes: dict[str, Path],
    rvf_state_roots_isolated_from_invoking_shell_environment: dict[str, Path],
) -> None:
    """transcript 缺失、跳过重分配：旧 lease 没释放、contract 的 lease 字段原样保留；认领路径进不了 lease，
    不冒充覆盖，结果区分「旧 lease 仍在」与「真的没有 lease」。"""
    run = _start_agentic_initiation_run_leaving_ide_hand_edit_uncovered(
        tmp_path, isolated_agent_homes, rvf_state_roots_isolated_from_invoking_shell_environment["kanban_followup_lock_root"]
    )
    cli, roots, token = run["cli"], run["roots"], run["token"]
    contract_before = run["contract_before"]
    run["transcript"].rename(run["transcript"].with_name("transcript-moved-away.jsonl"))

    claim = cli.run_scope_claim(token=token, paths=["hand_edit.txt"], reason="本会话在 IDE 里改的", roots=roots)

    assert claim.status == "ok", claim.message
    claim_result = claim.detail["claim"]
    assert claim_result["tracker_lease_state_after_claim"] == (
        cli.review_scope_coverage_and_claim.TRACKER_LEASE_STATE_AFTER_CLAIM_REALLOCATION_SKIPPED_PRIOR_LEASE_KEPT
    ), claim_result
    assert claim_result["tracker_reallocation"]["skip_reason"] == "session_transcript_missing"
    assert claim_result["run_has_tracker_lease_after_claim"] is True
    assert claim_result["tracker_lease_id_after_claim"] == contract_before["tracker_lease_id"]
    assert claim_result["claimed_paths_not_admitted_into_formal_review_scope"] == ["hand_edit.txt"]
    contract_after = json.loads(run["contract_path"].read_text(encoding="utf-8"))
    for lease_field in ("tracker_lease_id", "tracker_scope_hash", "primary_units"):
        assert contract_after[lease_field] == contract_before[lease_field], lease_field
    assert "hand_edit.txt" not in contract_after["fix_allowlist"]
    assert "hand_edit.txt" in contract_after["protected_files"]
    assert "旧 lease 仍在" in claim.message
    assert "本轮没有 tracker lease" not in claim.message

    blocked = cli.run_dispatch(
        token=token, strictness="light", roots=roots, dry_run=True,
        reviewer_dispatcher=_fake_reviewer_dispatcher_that_never_runs_a_reviewer,
    )
    assert blocked.exit_code == cli.EXIT_UNCOVERED_CANDIDATE_PATHS_NEED_AGENT_DECISION, blocked.message


# --------------------------------------------------------------------------------------------
# 4. HEAD 截断
# --------------------------------------------------------------------------------------------


def test_build_manifest_keeps_earlier_edit_after_worktree_fast_forwarded_to_base(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    """worktree 被快进到 base 上别的任务的提交后，本会话更早的未提交 Edit 不能再被截掉（4d6fd366 形态）。"""
    repo = _init_repo(tmp_path / "repo")
    _run_git(repo, "checkout", "-q", "-b", "task/ff")
    edit_at = time.time() - 600
    builder = ClaudeTranscriptBuilder("ff-session", repo)
    builder.user_prompt(edit_at - 10)
    builder.tool_call("Edit", {"file_path": str(repo / "tracked.txt")}, started_at=edit_at, ended_at=edit_at + 1)
    transcript = builder.write(tmp_path / "ff-session.jsonl")
    # base 上别的任务在本会话编辑之后提交，task 分支随后被快进过去（autostash 保住未提交改动）。
    _run_git(repo, "checkout", "-q", "main")
    (repo / "other_task.txt").write_text("other\n", encoding="utf-8")
    _run_git(repo, "add", "other_task.txt")
    _run_git(repo, "commit", "-q", "-m", "other task lands on main")
    _run_git(repo, "checkout", "-q", "task/ff")
    _run_git(repo, "merge", "-q", "--ff-only", "main")
    (repo / "tracked.txt").write_text("base\nsession edit\n", encoding="utf-8")

    manifest = _build_manifest(repo, transcript)

    assert manifest["ownership_baseline"]["mode"] == "no_session_branch_commit_after_excluding_base_reachable_commits"
    assert manifest["owned_dirty_paths"] == ["tracked.txt"]


def test_build_manifest_claude_bash_git_commit_becomes_line_cutoff(
    tmp_path: Path, isolated_agent_homes: dict[str, Path]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    now = time.time()
    builder = ClaudeTranscriptBuilder("committer", repo)
    builder.user_prompt(now - 100)
    builder.tool_call("Edit", {"file_path": str(repo / "tracked.txt")}, started_at=now - 90, ended_at=now - 89)
    builder.tool_call("Bash", {"command": "git add -A && git commit -m 'land'"}, started_at=now - 80, ended_at=now - 79)
    builder.tool_call("Write", {"file_path": str(repo / "after_commit.txt")}, started_at=now - 70, ended_at=now - 69)
    transcript = builder.write(tmp_path / "committer.jsonl")
    (repo / "tracked.txt").write_text("someone else changed it after the commit\n", encoding="utf-8")
    (repo / "after_commit.txt").write_text("mine\n", encoding="utf-8")

    manifest = _build_manifest(repo, transcript, enable_file_change_time_window_attribution=False)

    assert manifest["ownership_baseline"]["mode"] == "line_cutoff"
    assert manifest["owned_dirty_paths"] == ["after_commit.txt"]
    assert "tracked.txt" in manifest["unattributed_dirty_paths"]


def test_build_manifest_failed_git_commit_does_not_cut_off(tmp_path: Path, isolated_agent_homes: dict[str, Path]) -> None:
    repo = _init_repo(tmp_path / "repo")
    now = time.time()
    builder = ClaudeTranscriptBuilder("failed-committer", repo)
    builder.user_prompt(now - 100)
    builder.tool_call("Edit", {"file_path": str(repo / "tracked.txt")}, started_at=now - 90, ended_at=now - 89)
    builder.tool_call("Bash", {"command": "git commit -m 'x'"}, started_at=now - 80, ended_at=now - 79, is_error=True)
    transcript = builder.write(tmp_path / "failed-committer.jsonl")
    (repo / "tracked.txt").write_text("base\nedit\n", encoding="utf-8")

    manifest = _build_manifest(repo, transcript, enable_file_change_time_window_attribution=False)

    assert manifest["ownership_baseline"]["mode"] != "line_cutoff"
    assert manifest["owned_dirty_paths"] == ["tracked.txt"]
