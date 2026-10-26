"""核验 Kanban 创建锚点及提交归属，保存可跨重启接续的补证结果。"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from core.session_scope_allocation.committed_candidate_scope_evidence import (
    COMMITTED_CANDIDATE_SCOPE_EVIDENCE_SCHEMA, commit_is_ancestor, git_read,
    scope_evidence_digest, validate_committed_candidate_scope_evidence,
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("证据必须为 JSON 对象")
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".scope-evidence-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        Path(name).unlink(missing_ok=True)


def _stable_patch_id(repo: Path, commit: str) -> str | None:
    patch = git_read(repo, "show", "--pretty=format:", "--no-ext-diff", "--no-textconv", commit)
    result = git_read(repo, "patch-id", "--stable", input_text=patch + "\n")
    return result.split()[0] if result else None


def _map_owned_commit_identities(
    repo: Path, initial: str, identities: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """SHA 优先；历史改写只接受两侧都唯一的 patch-id，不用路径猜归属。"""
    reachable = set(git_read(repo, "rev-list", "--no-merges", f"{initial}..HEAD").splitlines())
    owned = set()
    unresolved = []
    patches: dict[str, list[str]] | None = None
    identity_patch_counts: dict[str, int] = {}
    for identity in identities:
        patch_id = identity.get("stablePatchId")
        if patch_id:
            identity_patch_counts[patch_id] = identity_patch_counts.get(patch_id, 0) + 1
    for identity in identities:
        sha = identity.get("commitSha")
        if sha in reachable:
            owned.add(sha)
            continue
        # 起点之前的旧提交不因保存于 provenance 而重新进入候选。
        if isinstance(sha, str) and commit_is_ancestor(repo, sha, initial):
            continue
        patch_id = identity.get("stablePatchId")
        if patch_id and identity_patch_counts[patch_id] == 1:
            if patches is None:
                patches = {}
                patch_history = git_read(repo, "log", "--no-merges", "--no-ext-diff", "--no-textconv",
                                         "--format=commit %H", "-p", f"{initial}..HEAD")
                for line in git_read(repo, "patch-id", "--stable", input_text=patch_history + "\n").splitlines():
                    candidate_patch, candidate = line.split()
                    patches.setdefault(candidate_patch, []).append(candidate)
            matches = patches.get(patch_id, [])
            if len(matches) == 1 and matches[0] not in owned:
                owned.add(matches[0])
                continue
        unresolved.append(str(sha))
    return sorted(owned), unresolved


def _checkpoint_initial_commit(repo: Path, task_id: str) -> str | None:
    encoded = base64.urlsafe_b64encode(task_id.encode()).decode().rstrip("=")
    try:
        checkpoint = git_read(repo, "rev-parse", "--verify", f"refs/kanban/checkpoints/{encoded}/turn/1")
        parents = git_read(repo, "rev-list", "--parents", "-n", "1", checkpoint).split()
        if len(parents) == 2 and commit_is_ancestor(repo, parents[1], "HEAD"):
            return parents[1]
    except ValueError:
        pass
    return None


def _tracker_proven_commit_shas(repo: Path, initial: str, session_id: str | None) -> list[str]:
    """仅用本会话强归属与逐单元内容相等补证；不把相同路径当作提交所有权。"""
    if not session_id:
        return []
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker

    common = tracker.git_common_dir(repo)
    if common is None:
        return []
    database = tracker.tracker_dir(tracker.log_root(), tracker.repo_key(common)) / tracker.SQLITE_FILENAME
    if not database.exists():
        return []
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT u.unit_id FROM units u JOIN session_units s ON s.unit_id=u.unit_id "
                "WHERE s.session_id=? AND s.assignment_kind IN ('owned','takeover') "
                "AND s.evidence IN ('apply_patch','claude_write','exec_command','agent_explicit_declaration') "
                "AND u.worktree_key=? AND u.is_tombstoned=0",
                (session_id, tracker._worktree_key(str(repo.resolve()))),
            ).fetchall()
    except sqlite3.Error:
        return []
    owned_units = {row[0] for row in rows}
    if not owned_units:
        return []
    proven = []
    for commit in git_read(repo, "rev-list", "--no-merges", f"{initial}..HEAD").splitlines():
        parent = git_read(repo, "rev-parse", commit + "^")
        unit_ids = set()
        for path in git_read(repo, "diff", "--name-only", "--no-renames", "-z", parent, commit).split("\0"):
            if path:
                observation = tracker._classify_committed_path(repo, path, parent, head_ref=commit)
                if observation is None:
                    unit_ids.clear()
                    break
                unit_ids.update(spec.unit_id for spec in tracker._specs_from_observation(observation, path))
        if unit_ids and unit_ids <= owned_units:
            proven.append(commit)
    return proven


def _native_session_proven_commit_shas(
    repo: Path, initial: str, session_id: str | None, transcript_path: str | None,
) -> list[str]:
    """只接受同一原生会话、同仓库 git commit 工具调用的成功回执。"""
    if not transcript_path or not session_id:
        return []
    from core.session_scope_allocation.session_change_manifest import (
        parse_jsonl, session_id_from_records,
    )
    from core.session_scope_allocation.shell_command_named_write_target_paths_extraction import (
        analyze_shell_command_named_write_targets, shell_command_runs_git_commit_inside_repository,
    )
    path = Path(transcript_path)
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        return []
    records = parse_jsonl(path)
    if session_id_from_records(records) != session_id:
        return []
    pending: set[str] = set()
    proven = set()
    for record in records:
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "function_call" and payload.get("name") in ("exec_command", "shell_command"):
            try:
                arguments = json.loads(payload.get("arguments", "{}"))
            except (ValueError, TypeError):
                continue
            command = arguments.get("cmd", arguments.get("command", ""))
            directory = arguments.get("workdir", arguments.get("cwd"))
            # 缺 cwd 或带二次目录跳转的命令不能证明执行归属。
            if (isinstance(command, str) and directory and Path(directory).resolve() == repo.resolve()
                    and shell_command_runs_git_commit_inside_repository(
                        analyze_shell_command_named_write_targets(command, directory), repository_root=repo,
                    )):
                pending.add(str(payload.get("call_id")))
        elif payload.get("type") == "function_call_output" and str(payload.get("call_id")) in pending:
            output = payload.get("output", "")
            if not isinstance(output, str):
                continue
            for abbreviated in re.findall(r"^\[[^\]\n]*?\s([0-9a-f]{7,64})\]", output, re.MULTILINE):
                try:
                    commit = git_read(repo, "rev-parse", "--verify", abbreviated + "^{commit}")
                    if commit_is_ancestor(repo, commit, "HEAD") and not commit_is_ancestor(repo, commit, initial):
                        proven.add(commit)
                except ValueError:
                    continue
    return sorted(proven)


def resolve_kanban_committed_candidate_scope_evidence(
    repo: Path, task_id: str, session_id: str | None, *, event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    repo = repo.resolve()
    event = event or {}
    common = git_read(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    try:
        head = git_read(repo, "rev-parse", "--verify", "HEAD")
    except ValueError:
        # unborn 分支没有已提交层；不能让这一层的缺席阻断独立 dirty 候选。
        branch = git_read(repo, "symbolic-ref", "--quiet", "HEAD")
        if git_read(repo, "for-each-ref", "--format=%(refname)", branch):
            raise
        return {
            "schema": COMMITTED_CANDIDATE_SCOPE_EVIDENCE_SCHEMA, "status": "unborn_head",
            "task_id": task_id, "worktree": str(repo), "git_common_directory": common,
            "head_commit": None, "unborn_head_ref": branch, "task_owned_commit_shas": [],
        }
    evidence: dict[str, Any] = {
        "schema": COMMITTED_CANDIDATE_SCOPE_EVIDENCE_SCHEMA, "status": "unknown",
        "task_id": task_id, "worktree": str(repo), "git_common_directory": common,
        "head_commit": head, "task_owned_commit_shas": [], "recovery_condition":
        "Kanban 来源、checkpoint、tracker 或原生会话证据变化后，或下一次真实 prompt 时重新核验",
        "recovery_attempt_limit_per_observation": 1,
    }
    state_root = Path(os.environ.get("CODEX_RVF_CLINE_KANBAN_STATE_DIR") or Path.home() / ".cline/kanban")
    task_state_path = Path(git_read(repo, "rev-parse", "--absolute-git-dir")) / "rvf-committed-candidate-evidence" / (scope_evidence_digest({"task": task_id, "session": session_id}) + ".json")
    sources = []
    matches = []
    for directory in sorted((state_root / "workspaces").glob("*")):
        sessions_path = directory / "sessions.json"
        try:
            sessions = _read_object(sessions_path)
        except (OSError, ValueError):
            continue
        session = sessions.get(task_id)
        if isinstance(session, dict) and session.get("taskId") == task_id:
            matches.append((directory, session))
    source_paths = [directory / name for directory, _ in matches
                    for name in ("sessions.json", "task-commit-integration-provenance.json", "board.json")]
    transcript_path = event.get("transcript_path")
    if isinstance(transcript_path, str):
        source_paths.append(Path(transcript_path))
    # tracker 只读补证的输入变化也能解除上次 unknown，不依赖进程内缓存。
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    database = tracker.tracker_dir(tracker.log_root(), tracker.repo_key(Path(common))) / tracker.SQLITE_FILENAME
    source_paths.extend([database, Path(str(database) + "-wal")])
    for source in source_paths:
        try:
            state = source.stat()
            sources.append([str(source), state.st_mtime_ns, state.st_size])
        except OSError:
            sources.append([str(source), None, None])
    checkpoint = _checkpoint_initial_commit(repo, task_id)
    from core.session_scope_allocation.round_baseline_marker import read_round_baseline_marker
    prompt_observation = read_round_baseline_marker(task_id=task_id, session_id=session_id, repo=repo) or {}
    observation = scope_evidence_digest({"sources": sources, "head": head, "checkpoint": checkpoint,
                                         "real_prompt_captured_at": prompt_observation.get("captured_at"),
                                         "prompt": [event.get(key) for key in ("prompt", "turn_id", "prompt_id")]
                                         if event.get("hook_event_name") == "UserPromptSubmit" else None})
    try:
        prior = _read_object(task_state_path)
        if prior.get("source_observation_digest") == observation:
            validate_committed_candidate_scope_evidence(repo, prior, task_id=task_id)
            return prior
    except (OSError, ValueError):
        pass
    evidence["source_observation_digest"] = observation
    try:
        if len(matches) != 1:
            raise ValueError("Kanban task 的持久 workspace 绑定缺失或不唯一")
        directory, session = matches[0]
        if Path(str(session.get("workspacePath") or "")).resolve() != repo:
            raise ValueError("Kanban task 的持久 worktree 绑定不匹配")
        project = os.environ.get("KANBAN_PROJECT_PATH") or os.environ.get("CLINE_KANBAN_PROJECT_PATH")
        if project and git_read(Path(project), "rev-parse", "--path-format=absolute", "--git-common-dir") != common:
            raise ValueError("Kanban project 的 Git common-dir 不匹配")
        board = _read_object(directory / "board.json")
        cards = [card for column in board.get("columns", []) for card in column.get("cards", [])
                 if card.get("id") == task_id]
        if len(cards) != 1:
            raise ValueError("Kanban task 卡片不存在或不唯一")
        base_ref = cards[0].get("baseRef") or "main"
        entry: dict[str, Any] = {}
        try:
            provenance = _read_object(directory / "task-commit-integration-provenance.json")
            if provenance.get("schemaVersion") != 1:
                raise ValueError("Kanban provenance schema 不匹配")
            entry = provenance.get("tasks", {}).get(task_id, {})
        except (OSError, ValueError):
            evidence["provenance_unavailable"] = True
        incarnations = entry.get("incarnations", [])
        incarnation = incarnations[-1] if incarnations else {}
        initial = incarnation.get("initialBaseCommit")
        identities = incarnation.get("observedTaskCommits", [])
        if incarnation:
            if (not isinstance(incarnation.get("startedAt"), int)
                    or not isinstance(incarnation.get("observedTaskCommits"), list)
                    or any(not isinstance(identity, dict)
                           or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(identity.get("commitSha", "")))
                           or (identity.get("stablePatchId") is not None
                               and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(identity["stablePatchId"])))
                           for identity in identities)):
                raise ValueError("Kanban provenance 的 incarnation 或提交身份字段不完整")
            if (entry.get("taskId") != task_id or entry.get("baseRef") != base_ref
                    or incarnation.get("incarnationId") != f"{initial}:{incarnation.get('startedAt')}"
                    or any(item.get("startedAt", 0) > incarnation.get("startedAt", 0) for item in incarnations)):
                raise ValueError("Kanban provenance 的 task/base/incarnation 不匹配")
            snapshot = entry.get("lastWorkspaceGitSnapshot") or {}
            if snapshot and Path(str(snapshot.get("path") or "")).resolve() != repo:
                raise ValueError("Kanban provenance 指向另一个 worktree")
            if incarnation.get("captureSource") != "worktree_creation":
                if len(incarnations) > 1 and checkpoint != initial:
                    raise ValueError("首轮 checkpoint 不能证明当前 incarnation 的起点")
                initial = checkpoint
        else:
            initial = checkpoint
        if not initial or not commit_is_ancestor(repo, initial, head):
            raise ValueError("缺少当前 incarnation 的可达创建锚点；不使用 live merge-base 代替")
        evidence.update(initial_base_commit=initial, base_ref=base_ref,
                        base_tip_commit=git_read(repo, "rev-parse", "--verify", f"refs/heads/{base_ref.removeprefix('refs/heads/')}^{{commit}}"),
                        incarnation_id=incarnation.get("incarnationId") or f"checkpoint:{initial}",
                        stable_start_source="kanban_provenance" if incarnation.get("captureSource") == "worktree_creation" else "kanban_first_turn_checkpoint")
        owned, unresolved = _map_owned_commit_identities(repo, initial, identities)
        # 缺失或改写身份时按固定次序补证。完整 provenance 无须扫描 transcript。
        if not incarnation or unresolved:
            recovered = _tracker_proven_commit_shas(repo, initial, session_id)
            recovered.extend(_native_session_proven_commit_shas(repo, initial, session_id, transcript_path))
            owned = sorted(set(owned) | set(recovered))
            unresolved = [sha for sha in unresolved if sha not in recovered]
        evidence["task_owned_commit_shas"] = owned
        if unresolved:
            raise ValueError("部分历史提交无法唯一映射：" + ",".join(unresolved))
        # 缺 provenance 时 checkpoint 只证明起点；没有自有提交证明不能断言已无候选。
        if not incarnation and initial != head and not owned:
            raise ValueError("已恢复创建锚点，仍缺少 tracker 或原生会话的提交归属证据")
        evidence["status"] = "verified"
        validate_committed_candidate_scope_evidence(repo, evidence, task_id=task_id)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired) as exc:
        evidence["status"] = "unknown"
        evidence["diagnostic"] = str(exc)
    try:
        _atomic_write(task_state_path, evidence)
    except OSError:
        evidence["recovery_record_write_failed"] = True
    return evidence
