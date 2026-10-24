"""为既有 prepare/dispatch 回归构造真实 Git 快照和合成原生 turn，不提供生产 bypass。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def fixed_git_coordinates_for_review_start_boundary_unit_test(repo, *, test_scope):
    """为仅替代宿主 gate 的单元测试保留真实 Git 坐标。"""
    from core.session_scope_allocation.committed_round_detection import _resolve_committed_round_base_ref
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    def commit(revision):
        if not revision:
            return None
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "--verify", f"{revision}^{{commit}}"],
            cwd=repo, env=environment, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    base_ref = _resolve_committed_round_base_ref(Path(repo))
    head = commit("HEAD")
    checkpoint = None
    if head is not None:
        with tempfile.TemporaryDirectory(prefix="rvf-unit-test-candidate-index-") as directory:
            snapshot_environment = dict(environment, GIT_INDEX_FILE=str(Path(directory) / "index"))
            def snapshot_git(*arguments):
                return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=repo,
                                      env=snapshot_environment, check=True, capture_output=True, text=True).stdout.strip()
            snapshot_git("read-tree", head)
            snapshot_git("add", "--all")
            tree = snapshot_git("write-tree")
            checkpoint = snapshot_git("commit-tree", tree, "-p", head, "-m", "test: 冻结独立单元测试候选")
    return {"test_scope": test_scope, "worktree_head_commit_sha": head, "checkpoint_commit": checkpoint,
            "comparison_base_ref": base_ref, "comparison_base_ref_tip": commit(base_ref),
            "local_review_base_ref": base_ref, "local_review_base_tip": commit(base_ref),
            "automatic_closeout_eligibility": {"status": "unavailable", "reason_code": "isolated_unit_test_host"},
            "comparison_base_tip": None}


def environment_with_synthetic_independent_review_start_boundary(*, repo: Path, environment=None, run_id=None, run_dir=None, session_id="synthetic-native-session", preserve_runtime_state_root=False, existing_token=None, native_transcript_path=None, accept_decision_before_prepare=True):
    import rvf_decision_token_store as tokens
    import stop_decision_candidate_summary as summaries
    import review_start_boundary_gate as gate
    from core.review_start_boundary.review_start_boundary_verification import CHECKPOINT_BINDING_PREFIX, CHECKPOINT_BINDING_FIELDS, candidate_filesystem_state_sha256

    repo = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo, check=True, capture_output=True, text=True,
        env={key:value for key,value in os.environ.items() if not key.startswith("GIT_")}).stdout.strip())
    state = repo.parent / f"isolated-review-start-boundary-{uuid.uuid4().hex}"
    state.mkdir()
    env = dict(environment or os.environ)
    for key in tuple(env):
        if key.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")) or key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env.pop(key)
    # 保留被测试的 run/packet/registry 环境，只替换会泄漏到真实会话的身份和状态根。
    for key, value in (environment or {}).items():
        if key.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")) and key not in ("RVF_HOME", "RVF_SESSION_ID", "RVF_REVIEW_START_BOUNDARY_RECEIPT", "RVF_RUN_ID", "RVF_RUN_DIR"):
            env[key] = value
    env.update(RVF_HOME=str(state / "rvf-home"), RVF_USER_LOCAL_DATA_ROOT=str(state / "user-local-data"),
               CODEX_RVF_LOG_ROOT=str((state / "run-logs").resolve()), CODEX_THREAD_ID=session_id)
    if preserve_runtime_state_root and environment:
        for key in ("RVF_HOME", "RVF_USER_LOCAL_DATA_ROOT", "CODEX_RVF_STATE_DIR", "RVF_KANBAN_FOLLOWUP_LOCK_ROOT"):
            if environment.get(key):
                env[key] = environment[key]
        env.pop("CODEX_RVF_LOG_ROOT", None)
    configured_log_root = (environment or {}).get("CODEX_RVF_LOG_ROOT") or (environment or {}).get("CODEX_RVF_STATE_DIR")
    if configured_log_root and Path(configured_log_root).resolve().is_relative_to(repo.parent.resolve()):
        env["CODEX_RVF_LOG_ROOT"] = str(Path(configured_log_root).resolve())
    if run_id:
        env["RVF_RUN_ID"] = run_id
    if run_dir:
        env["RVF_RUN_DIR"] = str(run_dir)

    def git(*arguments, index=None):
        git_env = {key: value for key, value in env.items() if not key.startswith("GIT_")}
        git_env.update(GIT_AUTHOR_NAME="隔离分界测试", GIT_AUTHOR_EMAIL="boundary@example.invalid", GIT_COMMITTER_NAME="隔离分界测试", GIT_COMMITTER_EMAIL="boundary@example.invalid")
        if index:
            git_env["GIT_INDEX_FILE"] = str(index)
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=repo, env=git_env, check=True, capture_output=True, text=True).stdout.strip()

    transcript = Path(native_transcript_path) if native_transcript_path else state / "synthetic-native-transcript.jsonl"
    event = {"session_id": session_id, "cwd": str(repo), "transcript_path": str(transcript)}
    with patch.dict(os.environ, env, clear=True):
        summary = summaries.build_candidate_summary(event=event, cwd=str(repo), repo=str(repo), ledger=None)
        token = existing_token or tokens.mint_decision_token(worktree=str(repo), session_id=event["session_id"], candidate_summary=summary, candidate_fingerprint=summary["candidate_fingerprint"]).token
        if existing_token:
            summary = tokens.read_decision_token(existing_token).payload["candidate_summary"]
        text = tokens.render_decision_point_out_of_band_boundary_marker(token=token)
        prior_records = [json.loads(line) for line in transcript.read_text().splitlines() if line.strip()] if transcript.exists() else []
        for record in prior_records:
            if record.get("type") == "session_meta":
                record["payload"].update(cli_version="0.163.0", originator="codex-tui", source="cli")
        transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in prior_records + [
            {"type":"session_meta", "payload":{"id":event["session_id"], "cli_version":"0.163.0", "originator":"codex-tui", "source":"cli"}},
            {"type":"event_msg", "payload":{"type":"task_started", "turn_id":"synthetic-independent-turn", "turn_attribution":{"turn_trigger":"user"}}},
            {"type":"event_msg", "timestamp":datetime.now(timezone.utc).isoformat(), "payload":{"type":"item_completed", "turn_id":"synthetic-independent-turn", "item":{"type":"UserMessage", "id":"synthetic-native-message", "content":[{"type":"text", "text":text}]}}},
        ]) + "\n")
        index = state / "isolated-checkpoint-index"
        git("read-tree", "HEAD", index=index)
        git("add", "-A", index=index)
        tree = git("write-tree", index=index)
        base_ref = summary.get("committed_round_base_ref")
        receipt = dict(schema="rvf-independent-user-turn-boundary/v1", decision_token=token, kanban_task_id=summary.get("kanban_task_id"), idempotency_key=f"rvf-decision-point-{token}", session_id=event["session_id"], worktree=str(repo),
            transcript_path=str(transcript), native_user_message_id="synthetic-native-message", native_turn_id="synthetic-independent-turn",
            boundary_message_sha256=hashlib.sha256(text.encode()).hexdigest(), source="rvf_automatic_decision_message",
            candidate_fingerprint=summary["candidate_fingerprint"], worktree_head_commit_sha=git("rev-parse", "HEAD"),
            comparison_base_tip=summary.get("committed_round_baseline"), comparison_base_ref=base_ref,
            comparison_base_ref_tip=git("rev-parse", base_ref) if base_ref else None, checkpoint_tree=tree)
        receipt["candidate_filesystem_state_sha256"] = candidate_filesystem_state_sha256(str(repo), summary["candidates"])
        receipt["checkpoint_commit"] = git("commit-tree", tree, "-p", "HEAD", "-m", CHECKPOINT_BINDING_PREFIX + json.dumps({key:receipt.get(key) for key in CHECKPOINT_BINDING_FIELDS}, ensure_ascii=False))
        path = gate.review_start_boundary_receipt_path(token)
        # 复用隔离运行根时可创建第二份真实测试分界，用于验证不同 token 的排他归属。
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt))
        if accept_decision_before_prepare:
            tokens.acknowledge_decision_token(token)
        env["RVF_REVIEW_START_BOUNDARY_RECEIPT"] = str(path)
        contract = Path(env.get("RVF_SCOPE_CONTRACT") or state / "synthetic-scope.contract.json")
        existing = json.loads(contract.read_text()) if contract.exists() else {}
        existing.update(review_start_boundary_receipt=receipt, run_id=run_id or env.get("RVF_RUN_ID") or f"rvf-review-start-{token}")
        contract.write_text(json.dumps(existing))
        env["RVF_SCOPE_CONTRACT"] = str(contract)
        env = configure_synthetic_live_boundary_host_observation(receipt_path=path, repo=repo, environment=env)
    return env


def run_subprocess_with_independent_review_start_boundary(command, *args, **kwargs):
    """既有 CLI 契约测试显式构造新增前置条件；其它命令原样执行。"""
    if isinstance(command, (list, tuple)) and len(command) > 1:
        script = Path(command[1]).name
        if script == "prepare_review_run.py" or (script == "dispatch_reviewers.py" and "--execute" in command):
            env = kwargs.get("env") or os.environ.copy()
            def argument(name, fallback=None):
                return command[command.index(name) + 1] if name in command else fallback
            repo = argument("--repo", env.get("RVF_REPO"))
            if repo:
                kwargs["env"] = environment_with_synthetic_independent_review_start_boundary(repo=Path(repo), environment=env,
                    run_id=argument("--rvf-run-id", env.get("RVF_RUN_ID")), run_dir=argument("--rvf-run-dir", env.get("RVF_RUN_DIR")))
    return subprocess.run(command, *args, **kwargs)


def configure_synthetic_live_boundary_host_observation(*, receipt_path: Path, repo: Path, environment):
    """仅供测试的宿主接口替身：回显 nonce，并从合成宿主分支快照返回当前消息集合。"""
    import shlex
    import sys
    import rvf_decision_token_store as tokens

    receipt = json.loads(receipt_path.read_text())
    script = receipt_path.with_suffix(".synthetic-live-host.py")
    script.write_text('''import json, sys
from pathlib import Path
receipt_path = Path(sys.argv[1])
arguments = sys.argv[2:]
def argument(name):
    return arguments[arguments.index(name)+1]
receipt = json.loads(receipt_path.read_text())
active_file = receipt_path.with_suffix(".native-active-branch.json")
if active_file.exists():
    active = json.loads(active_file.read_text())
else:
    active_ids = []
    current_turn = None
    for line in Path(receipt["transcript_path"]).read_text().splitlines():
        entry = json.loads(line)
        payload = entry.get("payload") or {}
        if entry.get("type") != "event_msg":
            continue
        if payload.get("type") == "task_started":
            current_turn = payload.get("turn_id")
        if payload.get("type") in ("thread_rolled_back", "thread_reverted", "conversation_reverted"):
            active_ids = []
        item = payload.get("item") or {}
        if payload.get("type") == "item_completed" and item.get("type") == "UserMessage":
            active_ids.append(item.get("id"))
    active = {"native_active_branch_user_message_ids":active_ids, "native_active_branch_user_turn_id":current_turn}
observation = {"request_nonce":argument("--review-start-boundary-observation-nonce"),
    "kanban_task_id":argument("--task-id"), "idempotency_key":argument("--idempotency-key"),
    "session_id":receipt["session_id"], **active}
print(json.dumps({"independent_user_turn_boundary_receipt":receipt,
    "review_start_boundary_active_branch_observation":observation}))
''')
    task_id = receipt.get("kanban_task_id") or "synthetic-native-boundary-host-task"
    key = receipt.get("idempotency_key") or f"rvf-decision-point-{receipt['decision_token']}"
    tokens.record_out_of_band_delivery_status(receipt["decision_token"], status="delivered_and_submit_confirmed",
        detail={"kanban_task_id":task_id, "kanban_project_path":str(repo), "idempotency_key":key})
    env = dict(environment)
    env["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = shlex.join([sys.executable, str(script), str(receipt_path)])
    return env
