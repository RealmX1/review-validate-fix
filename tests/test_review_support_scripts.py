#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import concurrent.futures
import functools
import json
import importlib.util
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from _rvf_test_support.registry_completeness_guard import (
    assert_every_defined_test_is_registered,
    registered_names_from_case_tuples,
)


# 注册表完整性守卫的豁免名单（唯一合法出口）：仅放确属暂时隔离、暂时跑不过的测试，
# 每项须附 `# quarantined: <原因+issue>`。当前为空 = 严格模式：任何 def test_* 漏登记即红。
INTENTIONALLY_UNREGISTERED: frozenset[str] = frozenset()


ROOT = Path(__file__).resolve().parents[1]
CHECK_SKILL_CONTRACTS = ROOT / "scripts" / "check_skill_contracts.sh"
CHECK_PLUGIN_CONTRACTS = ROOT / "scripts" / "check_plugin_contracts.py"
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)
BUILD_PACKET = SCRIPT_DIR / "build_review_packet.py"
CHECK_REVIEW_OUTPUT = SCRIPT_DIR / "check_review_output.py"
WRITE_REVIEW_RESULT = SCRIPT_DIR / "write_review_result.py"
CHECK_REVIEW_RESULT = SCRIPT_DIR / "check_review_result.py"
COMMAND_LOCK = SCRIPT_DIR / "command_lock.py"
PREPARE_REVIEW_RUN = SCRIPT_DIR / "prepare_review_run.py"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIR / "run_alternative_reviewer.py"
CURSOR_STREAM_TOOL_LAYER_HEALTH = SCRIPT_DIR / "cursor_stream_tool_layer_health.py"
VERIFY_CURSOR_TOOL_LAYER = SCRIPT_DIR / "verify_cursor_tool_layer.py"
CANCEL_RVF_RUN = SCRIPT_DIR / "cancel_rvf_run.py"
CLINE_KANBAN_CLIENT = SCRIPT_DIR / "cline_kanban_client.py"
APPLY_WORKTREE_BOOTSTRAP = SCRIPT_DIR / "apply_worktree_bootstrap.py"
SESSION_MANIFEST = SCRIPT_DIR / "session_manifest.py"
DIAGNOSE_STOP_HOOK_SCOPE = SCRIPT_DIR / "diagnose_stop_hook_scope.py"
RVF_LOGGING = SCRIPT_DIR / "rvf_logging.py"
RVF_HANDOFF = SCRIPT_DIR / "rvf_handoff.py"
RVF_PREP_FILE = SCRIPT_DIR / "rvf_prep_file.py"
RVF_USER_PROMPT_SUBMIT = SCRIPT_DIR / "rvf_user_prompt_submit.py"
KANBAN_FOLLOWUP_LOCK = SCRIPT_DIR / "kanban_followup_lock.py"

for _name in tuple(os.environ):
    if _name.startswith("CODEX_RVF_"):
        os.environ.pop(_name, None)


def load_alternative_reviewer_module():
    spec = importlib.util.spec_from_file_location("rvf_run_alternative_reviewer", RUN_ALTERNATIVE_REVIEWER)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load run_alternative_reviewer module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cursor_tool_layer_health_module():
    spec = importlib.util.spec_from_file_location(
        "rvf_cursor_stream_tool_layer_health", CURSOR_STREAM_TOOL_LAYER_HEALTH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cursor_stream_tool_layer_health module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cancel_rvf_run_module():
    spec = importlib.util.spec_from_file_location("rvf_cancel_rvf_run", CANCEL_RVF_RUN)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cancel_rvf_run module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rvf_logging_module():
    spec = importlib.util.spec_from_file_location("rvf_logging", RVF_LOGGING)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_logging module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rvf_prep_file_module():
    spec = importlib.util.spec_from_file_location("rvf_prep_file", RVF_PREP_FILE)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_prep_file module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_rvf_user_prompt_submit_module():
    # Ensure rvf_prep_file and other dependencies are importable from SCRIPT_DIR
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    load_rvf_prep_file_module()
    spec = importlib.util.spec_from_file_location(
        "rvf_user_prompt_submit", RVF_USER_PROMPT_SUBMIT
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_user_prompt_submit module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_kanban_followup_lock_module():
    # kanban_followup_lock 依赖 ``from rvf_logging import safe_token``，需 SCRIPT_DIR 在 path 上。
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "kanban_followup_lock", KANBAN_FOLLOWUP_LOCK
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load kanban_followup_lock module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_check_plugin_contracts_module():
    spec = importlib.util.spec_from_file_location(
        "rvf_check_plugin_contracts",
        CHECK_PLUGIN_CONTRACTS,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load check_plugin_contracts module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cline_kanban_client_module():
    spec = importlib.util.spec_from_file_location("rvf_cline_kanban_client", CLINE_KANBAN_CLIENT)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cline_kanban_client module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rvf_analyze_thread_module():
    # rvf_analyze_thread top-level imports rvf_logging / trajectory_distill，且
    # launch_detached_analyze_thread 会 lazy import rvf_analyze_advisory，全部需
    # 要 SCRIPT_DIR 在 sys.path 上才能解析；以真实模块名注册进 sys.modules，让
    # advisory 的 `from rvf_analyze_thread import ...` 命中同一实例。
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "rvf_analyze_thread", SCRIPT_DIR / "rvf_analyze_thread.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_analyze_thread module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_fake_tmux_script(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "calls = os.environ.get('FAKE_TMUX_CALLS')\n"
        "if calls:\n"
        "    with open(calls, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "stderr = os.environ.get('FAKE_TMUX_STDERR')\n"
        "if stderr:\n"
        "    sys.stderr.write(stderr)\n"
        "raise SystemExit(int(os.environ.get('FAKE_TMUX_RETURNCODE', '0')))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def write_subcommand_aware_tmux_script(path: Path) -> Path:
    """fake tmux：记录调用，并按子命令分别返回退出码——

    - ``has-session`` → ``FAKE_TMUX_HAS_SESSION_RETURNCODE``（默认 0，即「存活」）
    - 其余（``new-session`` 等）→ ``FAKE_TMUX_RETURNCODE``（默认 0）

    供 FU-2 staleness 测试构造「has-session 报死 + new-session 成功」等组合
    （uniform 的 write_fake_tmux_script 无法区分子命令）。
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "calls = os.environ.get('FAKE_TMUX_CALLS')\n"
        "if calls:\n"
        "    with open(calls, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps({'argv': argv}) + '\\n')\n"
        "sub = argv[0] if argv else ''\n"
        "if sub == 'has-session':\n"
        "    raise SystemExit(int(os.environ.get('FAKE_TMUX_HAS_SESSION_RETURNCODE', '0')))\n"
        "raise SystemExit(int(os.environ.get('FAKE_TMUX_RETURNCODE', '0')))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class _AnalyzeLedgerStub:
    run_id = "rvf-unit"

    def env(self) -> dict[str, str]:
        return {}


def _seed_analysis_payload(run_dir: Path) -> dict[str, str]:
    analysis_dir = run_dir / "artifacts" / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    summary = analysis_dir / "summary.md"
    causality = analysis_dir / "causality.json"
    summary.write_text("# scaffold\n<!-- TODO(rvf-analyze): fill -->\n", encoding="utf-8")
    causality.write_text(json.dumps({"issues": []}) + "\n", encoding="utf-8")
    return {
        "run_dir": str(run_dir),
        "summary_md_path": str(summary),
        "causality_json_path": str(causality),
    }


def test_rvf_analyze_thread_builds_claude_command(_root: Path | None = None) -> None:
    module = load_rvf_analyze_thread_module()
    argv, uses_stdin = module.build_analyze_command(module.HOST_CLAUDE)
    assert uses_stdin is True
    assert Path(argv[0]).name == "claude"
    assert "-p" in argv
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    assert "--output-format" in argv and "stream-json" in argv
    # analyze agent 必须能解析 $rvf-analyze slash command 并 Edit 文件。
    assert "--disable-slash-commands" not in argv


def test_rvf_analyze_thread_builds_codex_command(_root: Path | None = None) -> None:
    module = load_rvf_analyze_thread_module()
    argv, uses_stdin = module.build_analyze_command(module.HOST_CODEX)
    assert uses_stdin is True
    assert Path(argv[0]).name == "codex"
    assert "exec" in argv
    assert argv[-1] == "-"
    assert "--sandbox" in argv and "workspace-write" in argv
    # 未知 host 兜底到 codex 向量。
    fallback_argv, _ = module.build_analyze_command("totally-unknown-host")
    assert fallback_argv == argv


def test_rvf_analyze_thread_select_host(root: Path) -> None:
    module = load_rvf_analyze_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    claude_t = root / "claude.jsonl"
    claude_t.write_text(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n", encoding="utf-8")
    codex_t = root / "codex.jsonl"
    codex_t.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n", encoding="utf-8")

    assert module.select_host({"transcript_path": str(claude_t)}) == module.HOST_CLAUDE
    assert module.select_host({"transcript_path": str(codex_t)}) == module.HOST_CODEX
    # transcript 缺失 → 兜底 codex。
    assert module.select_host({}) == module.HOST_CODEX
    assert module.select_host({"transcript_path": str(root / "missing.jsonl")}) == module.HOST_CODEX


def test_rvf_analyze_thread_status_file_schema(root: Path) -> None:
    module = load_rvf_analyze_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / "runs" / "rvf-unit"
    analysis = _seed_analysis_payload(run_dir)
    analysis_dir = Path(analysis["summary_md_path"]).parent
    fake_tmux = write_fake_tmux_script(root / "fake_tmux.py")
    tmux_calls = root / "tmux-calls.jsonl"

    saved = {k: os.environ.get(k) for k in ("CODEX_RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")}
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(tmux_calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    try:
        result = module.launch_detached_analyze_thread(
            event={},
            ledger=_AnalyzeLedgerStub(),
            analysis=analysis,
            finalize_record=None,
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert result["launch_status"] == "launched"
    assert result["tmux_session"] == "rvf-analyze-rvf-unit"

    status = json.loads((analysis_dir / ".analyze-thread.status.json").read_text(encoding="utf-8"))
    expected_keys = {
        "schema_version",
        "run_dir",
        "run_name",
        "host",
        "tmux_session",
        "command",
        "pid",
        "started_at",
        "armed_at",
        "returncode",
        "finished_at",
        "launch_status",
        "error",
    }
    assert expected_keys <= set(status)
    assert status["schema_version"] == 1
    assert status["launch_status"] == "launched"
    # 锁与冻结 prompt 落盘；fake tmux 恰好被调一次。
    assert (analysis_dir / ".analyze-thread.lock").exists()
    assert (analysis_dir / ".analyze-thread.prompt.md").exists()
    assert len(tmux_calls.read_text(encoding="utf-8").splitlines()) == 1
    # 切忌触碰 summary/causality（COMPLETE 判定靠它们 mtime）。
    assert "TODO(rvf-analyze)" in Path(analysis["summary_md_path"]).read_text(encoding="utf-8")


def test_rvf_analyze_thread_lock_blocks_second_launch(root: Path) -> None:
    module = load_rvf_analyze_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / "runs" / "rvf-unit"
    analysis = _seed_analysis_payload(run_dir)
    fake_tmux = write_fake_tmux_script(root / "fake_tmux.py")
    tmux_calls = root / "tmux-calls.jsonl"

    saved = {k: os.environ.get(k) for k in ("CODEX_RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")}
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(tmux_calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    try:
        first = module.launch_detached_analyze_thread(
            event={}, ledger=_AnalyzeLedgerStub(), analysis=analysis, finalize_record=None
        )
        second = module.launch_detached_analyze_thread(
            event={}, ledger=_AnalyzeLedgerStub(), analysis=analysis, finalize_record=None
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert first["launch_status"] == "launched"
    # 每 run O_EXCL 锁：第二次命中 already_running，不再启动 tmux。
    # 第二次会先 has-session 探活（session 存活 → already_running），故按 new-session 计数。
    recorded = [json.loads(line)["argv"] for line in tmux_calls.read_text(encoding="utf-8").splitlines()]
    assert second["launch_status"] == "already_running"
    assert len([a for a in recorded if a[:1] == ["new-session"]]) == 1, recorded


def load_check_review_output_module():
    spec = importlib.util.spec_from_file_location(
        "rvf_check_review_output", CHECK_REVIEW_OUTPUT
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load check_review_output module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(
    cmd: list[str],
    cwd: Path | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_rvf_prep_file_round_trip_and_sweep(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    root = tmp_path / "prep-root"
    now = prep.parse_timestamp("2026-05-07T00:00:00Z")

    written = prep.write_prep_file(
        {
            "origin_session_id": "session-a",
            "origin_repo": str(tmp_path / "repo"),
            "target_flow": "flow-2-branch",
            "rvf_run": {
                "run_id": "rvf-20260507T000000Z-test",
                "tracker_scope_hash": "sha256:abc",
            },
        },
        root=root,
        token="0123456789abcdef",
        now=now,
        ttl_seconds=300,
    )

    assert written.token == "0123456789abcdef"
    assert written.path == root / "0123456789abcdef.json"
    assert written.payload["schema_version"] == 1
    assert written.payload["created_at"] == "2026-05-07T00:00:00Z"
    assert written.payload["expires_at"] == "2026-05-07T00:05:00Z"
    assert written.path.stat().st_mode & 0o777 == 0o600
    assert written.path.parent.stat().st_mode & 0o777 == 0o700

    try:
        prep.write_prep_file(
            {"origin_session_id": "session-collision", "origin_repo": str(tmp_path)},
            root=root,
            token="0123456789abcdef",
            now=prep.parse_timestamp("2026-05-07T00:00:10Z"),
            ttl_seconds=300,
        )
    except prep.PrepFileError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected explicit token collision to fail")
    assert json.loads(written.path.read_text(encoding="utf-8"))["origin_session_id"] == "session-a"

    stale = prep.write_prep_file(
        {"origin_session_id": "old-session", "origin_repo": str(tmp_path)},
        root=root,
        token="bbbbbbbbbbbbbbbb",
        now=prep.parse_timestamp("2026-05-07T00:00:00Z"),
        ttl_seconds=1,
    )
    reused = prep.write_prep_file(
        {"origin_session_id": "new-session", "origin_repo": str(tmp_path)},
        root=root,
        token="bbbbbbbbbbbbbbbb",
        now=prep.parse_timestamp("2026-05-07T00:00:02Z"),
        ttl_seconds=1000,
    )
    assert reused.path == stale.path
    assert reused.payload["origin_session_id"] == "new-session"

    existing_generated = prep.write_prep_file(
        {"origin_session_id": "generated-existing", "origin_repo": str(tmp_path)},
        root=root,
        token="ffffffffffffffff",
        now=now,
        ttl_seconds=1000,
    )
    original_generate_token = prep.generate_token
    generated_tokens = iter(["ffffffffffffffff", "eeeeeeeeeeeeeeee"])
    try:
        prep.generate_token = lambda: next(generated_tokens)
        generated = prep.write_prep_file(
            {"origin_session_id": "generated-retry", "origin_repo": str(tmp_path)},
            root=root,
            now=now,
            ttl_seconds=1000,
        )
    finally:
        prep.generate_token = original_generate_token
    assert existing_generated.path.exists()
    assert generated.token == "eeeeeeeeeeeeeeee"
    assert generated.payload["origin_session_id"] == "generated-retry"

    valid = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:01:00Z"),
    )
    assert valid.status == "valid"
    assert valid.payload["origin_session_id"] == "session-a"

    updated = prep.update_prep_file(
        written,
        {
            "target_worktree": str(tmp_path / "task-worktree"),
            "target_kanban_task_id": "task-123",
        },
    )
    assert updated.path == written.path
    assert updated.payload["created_at"] == "2026-05-07T00:00:00Z"
    assert updated.payload["expires_at"] == "2026-05-07T00:05:00Z"
    assert updated.payload["target_worktree"] == str(tmp_path / "task-worktree")
    valid_updated = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:01:00Z"),
    )
    assert valid_updated.status == "valid"
    assert valid_updated.payload["target_kanban_task_id"] == "task-123"

    for protected_key in ("created_at", "expires_at", "token", "schema_version"):
        try:
            prep.update_prep_file(updated, {protected_key: "bad"})
        except prep.PrepFileError as exc:
            assert protected_key in str(exc)
        else:
            raise AssertionError(f"expected PrepFileError for protected field {protected_key}")

    expired = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:06:00Z"),
    )
    assert expired.status == "expired"

    removed = prep.sweep_stale(
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:06:00Z"),
    )
    assert removed == [written.path]
    assert not written.path.exists()


def test_rvf_prep_file_revive_expired_restamps_ttl(tmp_path: Path) -> None:
    """FU-1：写 prep → now 越过 expiry（read 为 expired 但带回 payload）→ revive 续期 →
    再 read 为 valid，且非时间戳字段原样保留；缺 payload 的 lookup 不可 revive。"""
    prep = load_rvf_prep_file_module()
    root = tmp_path / "prep-root"
    t0 = prep.parse_timestamp("2026-05-07T00:00:00Z")
    written = prep.write_prep_file(
        {
            "origin_session_id": "session-revive",
            "rvf_run": {"run_id": "rvf-revive", "run_dir": str(tmp_path / "run")},
        },
        root=root,
        token="0123456789abcdef",
        now=t0,
        ttl_seconds=300,
    )
    after = prep.parse_timestamp("2026-05-07T00:10:00Z")  # 越过 00:05:00 expiry
    expired = prep.read_prep_file(written.token, root=root, now=after)
    assert expired.status == "expired"
    assert expired.payload is not None  # read 对 expired 仍带回 payload

    revived = prep.revive_prep_file(expired, root=root, now=after, ttl_seconds=300)
    assert revived.path == written.path  # 就地重写同一 token 文件
    assert revived.payload["created_at"] == "2026-05-07T00:10:00Z"
    assert revived.payload["expires_at"] == "2026-05-07T00:15:00Z"
    assert revived.payload["origin_session_id"] == "session-revive"  # 非时间戳字段保留
    assert revived.payload["rvf_run"]["run_id"] == "rvf-revive"

    again = prep.read_prep_file(
        written.token, root=root, now=prep.parse_timestamp("2026-05-07T00:12:00Z")
    )
    assert again.status == "valid", again  # 续期后回到 valid

    missing = prep.read_prep_file("aaaaaaaaaaaaaaaa", root=root, now=after)
    assert missing.status == "missing" and missing.payload is None
    try:
        prep.revive_prep_file(missing, root=root, now=after)
    except prep.PrepFileError:
        pass
    else:
        raise AssertionError("expected revive of payload-less lookup to fail")


def test_kanban_followup_pending_marker_round_trip(tmp_path: Path) -> None:
    """dispatched-unconfirmed(pending) marker：write/read/clear(token 防误清)/stale/与 in-progress 物理隔离。

    pending 记录「dispatch 已发、尚未确认落地」；UPS arm 落地时按 token 清，超时则下次 Stop 据其重投。
    """
    k = load_kanban_followup_lock_module()
    root = tmp_path / "lock-root"
    path = k.write_pending_marker(
        task_id="bf042",
        session_id="s1",
        run_id="run-x",
        run_dir=str(tmp_path / "run-x"),
        repo="/repo",
        cwd="/repo",
        token="deadbeefdeadbeef",
        delivery_channel="terminal",
        message_id="terminal:bf042:run-x",
        root=root,
    )
    assert path is not None and path.exists()
    # 与 in-progress marker 物理隔离：pending 落在独立子目录，不会同名互相覆盖。
    assert path.parent.name == k.PENDING_SUBDIR_NAME
    marker = k.read_pending_marker(task_id="bf042", root=root)
    assert marker is not None
    assert marker["state"] == k.PENDING_STATE == "dispatched_unconfirmed"
    assert marker["token"] == "deadbeefdeadbeef"
    assert marker["delivery_channel"] == "terminal"
    assert k.pending_status(marker) == k.STATUS_ACTIVE
    # token 防误清：一条迟到的旧投递确认（不同 token）不得清掉这把新 pending。
    assert k.clear_pending_marker(task_id="bf042", token="other", root=root) == []
    assert k.read_pending_marker(task_id="bf042", root=root) is not None
    # 正确 token 清掉。
    removed = k.clear_pending_marker(task_id="bf042", token="deadbeefdeadbeef", root=root)
    assert removed == [str(path)]
    assert not path.exists()
    assert k.read_pending_marker(task_id="bf042", root=root) is None
    # stale 判定：TTL=0 → 立即过期。
    prev_ttl = os.environ.get(k.PENDING_TTL_ENV)
    os.environ[k.PENDING_TTL_ENV] = "0"
    try:
        k.write_pending_marker(
            task_id="t2",
            session_id=None,
            run_id="r",
            run_dir="d",
            repo=None,
            cwd=None,
            token="t",
            delivery_channel="terminal",
            root=root,
        )
        stale = k.read_pending_marker(task_id="t2", root=root)
        assert k.pending_status(stale) == k.STATUS_STALE
    finally:
        if prev_ttl is None:
            os.environ.pop(k.PENDING_TTL_ENV, None)
        else:
            os.environ[k.PENDING_TTL_ENV] = prev_ttl


def test_kanban_followup_iter_pending_and_stamp_notified(tmp_path: Path) -> None:
    """S0/S0b：pending 新快照字段 + iter_pending_markers 跨 task 枚举 + stamp_pending_notified 保 token。

    stamp 必须 read-merge-write 且**原样保留 token**，否则会破坏 clear_pending_marker 的 token
    防误清 guard（一条迟到的旧投递确认会因 token 不再匹配而无法清掉 marker → 永久误锁）。
    """
    k = load_kanban_followup_lock_module()
    root = tmp_path / "lock-root"
    pa = k.write_pending_marker(
        task_id="taskA", session_id="sA", run_id="rA", run_dir=str(tmp_path / "rA"),
        repo="/repo", cwd="/repo", token="aaaaaaaaaaaaaaaa", delivery_channel="terminal",
        kanban_project_path="/repo", kanban_task_title="标题A",
        kanban_task_title_source="cline_kanban_task_env",
        origin_transcript_path=str(tmp_path / "tA.jsonl"), root=root,
    )
    pb = k.write_pending_marker(
        task_id="taskB", session_id="sB", run_id="rB", run_dir=str(tmp_path / "rB"),
        repo="/repo2", cwd="/repo2", token="bbbbbbbbbbbbbbbb", delivery_channel="terminal",
        kanban_project_path="/repo2", kanban_task_title="标题B",
        origin_transcript_path=None, root=root,
    )
    assert pa and pb
    # S0 快照字段持久化。
    ma = k.read_pending_marker(task_id="taskA", root=root)
    assert ma["kanban_project_path"] == "/repo"
    assert ma["kanban_task_title"] == "标题A"
    assert ma["kanban_task_title_source"] == "cline_kanban_task_env"
    assert ma["origin_transcript_path"] == str(tmp_path / "tA.jsonl")
    # iter 枚举两个 task（顺序无关），每个 payload 注入 _marker_path。
    markers = k.iter_pending_markers(root=root)
    assert {m.get("kanban_task_id") for m in markers} == {"taskA", "taskB"}
    assert all(m.get("_marker_path") for m in markers)
    # stamp 盖 last_notified_at 且保留 token。
    assert k.stamp_pending_notified(task_id="taskA", token="aaaaaaaaaaaaaaaa", root=root) is True
    after = k.read_pending_marker(task_id="taskA", root=root)
    assert after.get("last_notified_at")
    assert after.get("token") == "aaaaaaaaaaaaaaaa"
    assert int(after.get("notify_count")) == 1
    # 错误 token 不盖戳（notify_count 不变）。
    assert k.stamp_pending_notified(task_id="taskA", token="ffffffffffffffff", root=root) is False
    assert int(k.read_pending_marker(task_id="taskA", root=root).get("notify_count")) == 1
    # 关键回归：stamp 之后用原 token 仍能清掉（token guard 未被破坏）。
    removed = k.clear_pending_marker(task_id="taskA", token="aaaaaaaaaaaaaaaa", root=root)
    assert removed == [str(pa)]
    assert k.read_pending_marker(task_id="taskA", root=root) is None
    # 缺失 marker → stamp 返回 False、不抛。
    assert k.stamp_pending_notified(task_id="taskA", token="aaaaaaaaaaaaaaaa", root=root) is False
    # iter 在空/不存在目录上返回 []（不抛）。
    assert k.iter_pending_markers(root=tmp_path / "nonexistent") == []


def test_notify_kanban_followup_stranded(tmp_path: Path) -> None:
    """S1a/S1b 共用 OS 通知：命令含 -open <taskUrl> + task 标题 + 按 task 合并 group；
    缺 notifier / 非 darwin 返回显式 reason 且不抛。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = _load_rvf_handoff_module()
    notifier_log = tmp_path / "notify.log"
    notifier = _write_fake_notifier(tmp_path / "fake_notifier.py", notifier_log)
    saved_bin = os.environ.get(mod.TERMINAL_NOTIFIER_BIN_ENV)
    os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = str(notifier)
    try:
        result = mod.notify_kanban_followup_stranded(
            task_id="4fc83",
            task_title="修复登录",
            task_url="http://127.0.0.1:3484/repo?task=4fc83",
            reason="stranded-escalated",
        )
        assert result["notified"] is True
        calls = [
            json.loads(line)
            for line in notifier_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(calls) == 1
        argv = calls[0]
        assert argv[argv.index("-open") + 1] == "http://127.0.0.1:3484/repo?task=4fc83"
        msg = argv[argv.index("-message") + 1]
        assert "4fc83" in msg and "修复登录" in msg
        group = argv[argv.index("-group") + 1]
        assert group.startswith("rvf-followup-")
    finally:
        if saved_bin is None:
            os.environ.pop(mod.TERMINAL_NOTIFIER_BIN_ENV, None)
        else:
            os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = saved_bin
    # 缺 notifier：非 darwin → unsupported-platform；darwin → terminal-notifier-missing；均不抛。
    saved_which = mod.shutil.which
    saved_bin2 = os.environ.pop(mod.TERMINAL_NOTIFIER_BIN_ENV, None)
    try:
        mod.shutil.which = lambda name: None
        r2 = mod.notify_kanban_followup_stranded(
            task_id="t", task_title=None, task_url=None, reason="dispatched-unconfirmed"
        )
        assert r2["notified"] is False
        assert r2["reason"] in {"terminal-notifier-missing", "unsupported-platform"}
    finally:
        mod.shutil.which = saved_which
        if saved_bin2 is not None:
            os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = saved_bin2


def test_parse_manual_scope_directive_variants() -> None:
    """`scope:` 指令解析：空白/逗号分隔、去引号、大小写不敏感、行内/跨行、无 scope。"""
    submit = load_rvf_user_prompt_submit_module()
    parse = submit.parse_manual_scope_directive
    assert parse("/review-validate-fix scope: src/a.py src/b.py") == ["src/a.py", "src/b.py"]
    assert parse("/review-validate-fix scope: src/a.py, src/b.py") == ["src/a.py", "src/b.py"]
    assert parse('/review-validate-fix scope: "src/a.py"') == ["src/a.py"]
    assert parse("/review-validate-fix SCOPE: src/a.py") == ["src/a.py"]
    assert parse("/review-validate-fix\nscope: src/a.py") == ["src/a.py"]
    assert parse("/review-validate-fix please review my work") == []
    # `telescope:` 之类的子串不得被当成指令（要求行首或空白前缀）。
    assert parse("just talking about telescope: lens here") == []
    assert parse("") == []
    assert parse(None) == []


def test_claude_plugin_hooks_declare_user_prompt_submit() -> None:
    """Option C: the Claude plugin's own hooks/hooks.json must declare both
    Stop and UserPromptSubmit (no installer / settings.json mutation needed),
    and the UserPromptSubmit shim must exist and compile."""
    import py_compile

    hooks_json = ROOT / "plugins" / "review-validate-fix" / "hooks" / "hooks.json"
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert "Stop" in hooks, "Stop hook regression"

    ups_groups = hooks.get("UserPromptSubmit")
    assert isinstance(ups_groups, list) and ups_groups, "UserPromptSubmit not declared"
    commands = [
        entry.get("command")
        for group in ups_groups
        for entry in group.get("hooks", [])
    ]
    assert any(
        isinstance(cmd, str)
        and "${CLAUDE_PLUGIN_ROOT}" in cmd
        and "hooks/user_prompt_submit.py" in cmd
        for cmd in commands
    ), f"UserPromptSubmit command not wired to the shim: {commands}"

    stop_groups = hooks.get("Stop")
    assert isinstance(stop_groups, list) and stop_groups, "Stop not declared"
    stop_commands = [
        entry.get("command")
        for group in stop_groups
        for entry in group.get("hooks", [])
    ]
    assert any(
        isinstance(cmd, str)
        and "${CLAUDE_PLUGIN_ROOT}" in cmd
        and "hooks/stop.py" in cmd
        for cmd in stop_commands
    ), f"Stop command not wired to the shim: {stop_commands}"

    # 两入口 shim + S3 单一契约 sibling 均须存在且可编译。
    hooks_dir = ROOT / "plugins" / "review-validate-fix" / "hooks"
    for name in ("user_prompt_submit.py", "stop.py", "_claude_hook_entry.py"):
        shim = hooks_dir / name
        assert shim.is_file(), f"hooks/{name} missing"
        py_compile.compile(str(shim), doraise=True)


def _load_claude_hook_entry_module():
    """以模块方式加载 hooks/_claude_hook_entry.py（两 shim 共享的单一契约），
    对 ``is_foreign_invocation`` / ``run_claude_hook`` 做单元测试。该模块
    stdlib-only、无 sibling 依赖，故 ``spec_from_file_location`` 直接可加载。"""
    import importlib.util

    entry_path = (
        ROOT / "plugins" / "review-validate-fix" / "hooks" / "_claude_hook_entry.py"
    )
    spec = importlib.util.spec_from_file_location("rvf_claude_hook_entry", entry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_claude_hook_entry_detects_foreign_invocation() -> None:
    """``is_foreign_invocation`` 守卫（S3 单源化：原 stop.py / user_prompt_submit.py
    两份逐字复制的 ``_is_codex_invocation`` 收敛为本契约一处）：Codex 转写路径
    返回 True；Claude 转写路径 / 缺路径 / 非 str 值时返回 False（保守，未知按
    Claude 跑）。"""
    entry = _load_claude_hook_entry_module()

    assert entry.is_foreign_invocation(
        {"transcript_path": "/Users/me/.codex/sessions/2026/05/21/rollout-XYZ.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"conversation_path": "/Users/me/.codex/sessions/anywhere/file.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"session_path": "/Users/me/.codex/sessions/2026/05/file.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"session_file": "/Users/me/.codex/sessions/some/leaf.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"transcript_path": "/Users/me/.claude/projects/-encoded/session.jsonl"}
    ) is False
    # 缺所有路径键 → False（保守，按 Claude 跑）
    assert entry.is_foreign_invocation({}) is False
    # 路径键值非 str → False
    assert entry.is_foreign_invocation({"transcript_path": None}) is False
    assert entry.is_foreign_invocation({"transcript_path": 12345}) is False


def test_claude_hook_entry_dispatches_claude_invocation() -> None:
    """G 正向：Claude 调用（无 Codex 证据）经 ``run_claude_hook`` 恰好调起核心
    一次并 normalize event；foreign（Codex）调用则**不**调起核心——「每 host
    恰好处理一次」的两半。用替身替换 entry 模块的 subprocess 拦截，不触真
    subprocess、不真起核心。"""
    import io

    entry = _load_claude_hook_entry_module()

    class _Completed:
        stdout = '{"continue": true}'
        stderr = ""
        returncode = 0

    def _run_once(event: dict) -> list[dict]:
        calls: list[dict] = []

        class _FakeSubprocess:
            @staticmethod
            def run(argv, **kwargs):
                calls.append({"argv": argv, "input": kwargs.get("input")})
                return _Completed()

        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        saved_subprocess = entry.subprocess
        sys.stdin = io.StringIO(json.dumps(event))
        sys.stdout = io.StringIO()
        entry.subprocess = _FakeSubprocess
        try:
            rc = entry.run_claude_hook(
                event_name="Stop",
                core_script=(
                    "skills",
                    "review-validate-fix",
                    "scripts",
                    "codex_stop_review_validate_fix.py",
                ),
                timeout_env="CLAUDE_RVF_STOP_HOOK_TIMEOUT",
                default_timeout="115",
                silent_success=False,
            )
        finally:
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout
            entry.subprocess = saved_subprocess
        assert rc == 0
        return calls

    # Claude 调用：核心被调起恰好一次，且 forwarded event 已 normalize。
    calls = _run_once({"session_id": "claude-abc"})
    assert len(calls) == 1, "Claude invocation must dispatch the core exactly once"
    assert calls[0]["argv"][0] == sys.executable
    assert calls[0]["argv"][1].endswith("codex_stop_review_validate_fix.py")
    forwarded = json.loads(calls[0]["input"])
    assert forwarded["hook_event_name"] == "Stop"
    assert forwarded["source"] == {"provider": "claude-code", "plugin": "review-validate-fix"}

    # Foreign（Codex）调用：守卫 no-op，核心**不**被调起。
    foreign_calls = _run_once(
        {"transcript_path": "/Users/me/.codex/sessions/2026/05/22/rollout-fake.jsonl"}
    )
    assert foreign_calls == [], "foreign (Codex) invocation must not dispatch the core"


def test_claude_plugin_shim_codex_invocation_noop(tmp_path: Path) -> None:
    """端到端：用 Codex 转写路径的 event 调 shim，应静默退出（stdout 空，
    退出码 0，且不写任何 prep file）。"""
    shim_path = ROOT / "plugins" / "review-validate-fix" / "hooks" / "user_prompt_submit.py"
    prep_root = tmp_path / "prep-root"
    prep_root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CODEX_RVF_PREP_ROOT": str(prep_root),
        "CODEX_RVF_LOG_ROOT": str(tmp_path / "log-root"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
    }
    event = {
        "prompt": "/review-validate-fix",
        "session_id": "codex-session-abc",
        "cwd": str(tmp_path),
        "transcript_path": "/Users/bominzhang/.codex/sessions/2026/05/21/rollout-fake.jsonl",
    }
    completed = subprocess.run(
        [sys.executable, str(shim_path)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "", f"expected silent no-op, got stdout={completed.stdout!r}"
    # 无 prep file 写入（如果 core 跑了就会写）
    assert not any(prep_root.iterdir()), "prep root should be empty after Codex no-op"


def test_claude_plugin_stop_shim_codex_invocation_noop(tmp_path: Path) -> None:
    """端到端：用 Codex 转写路径的 event 调 Stop shim，应静默退出（stdout
    空，退出码 0，且不调起 RVF 核心）。如果核心被调起，CODEX_RVF_LOG_ROOT
    指向的 log 目录会有 run dir 写入；空目录证明守卫生效。"""
    shim_path = ROOT / "plugins" / "review-validate-fix" / "hooks" / "stop.py"
    log_root = tmp_path / "log-root"
    log_root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CODEX_RVF_LOG_ROOT": str(log_root),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
    }
    event = {
        "session_id": "codex-session-stop-abc",
        "cwd": str(tmp_path),
        "transcript_path": "/Users/bominzhang/.codex/sessions/2026/05/22/rollout-fake.jsonl",
    }
    completed = subprocess.run(
        [sys.executable, str(shim_path)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "", f"expected silent no-op, got stdout={completed.stdout!r}"
    # 守卫生效则核心未被调起，log_root 下不会创建任何 run 子目录
    assert not any(log_root.iterdir()), "log root should be empty after Codex no-op"


def test_prepare_run_from_prep_file_timeout_returns_immediately(tmp_path: Path) -> None:
    """Verify TimeoutError unblocks the hook even when the worker is still running.

    Regression: previous implementation used ``with ThreadPoolExecutor(...)``,
    whose ``__exit__`` defaults to ``wait=True``, so a timeout exception still
    blocked until the worker finished. The fix manages the executor manually
    and calls ``shutdown(wait=False, cancel_futures=True)`` on timeout.
    """

    prep = load_rvf_prep_file_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)

    now = prep.parse_timestamp("2026-05-07T00:00:00Z")
    record = prep.write_prep_file(
        {
            "origin_session_id": "session-a",
            "origin_repo": str(repo),
            "origin_cwd": str(repo),
            "target_worktree": str(repo),
            "target_flow": "flow-manual",
            "rvf_run": {
                "run_id": "rvf-timeout-test",
                "run_dir": str(tmp_path / "rvf-state" / "runs" / "rvf-timeout-test"),
            },
        },
        root=root,
        token="dddddddddddddddd",
        now=now,
        ttl_seconds=3600,
    )

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import importlib

    prepare_module = importlib.import_module("prepare_review_run")
    original_prepare_run = prepare_module.prepare_run

    def slow_prepare_run(**_: object) -> dict[str, object]:
        # Sleep well past our 1-second timeout to prove that the wrapper does
        # not block on shutdown.
        time.sleep(5.0)
        return {"run_id": "should-never-finish"}

    prepare_module.prepare_run = slow_prepare_run
    try:
        start = time.monotonic()
        state = prepare_module.prepare_run_from_prep_file(
            record,
            timeout_seconds=1.0,
            base_dir=tmp_path / "base",
        )
        elapsed = time.monotonic() - start
    finally:
        prepare_module.prepare_run = original_prepare_run

    # Wall-clock must reflect the 1s timeout, not the 5s worker sleep. Allow
    # generous slack for slow CI but still well below the worker's 5s sleep.
    assert elapsed < 3.0, f"prepare_run_from_prep_file blocked for {elapsed:.2f}s"
    assert state["status"] == "timeout", state
    assert "1s timeout" in state["error"]

    # Prep file on disk must reflect the timeout state.
    stored = json.loads((root / "dddddddddddddddd.json").read_text(encoding="utf-8"))
    assert stored["rvf_run"]["shared_workflow_state"]["status"] == "timeout"


def _load_rvf_handoff_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import rvf_handoff

    return rvf_handoff


def _write_fake_notifier(path: Path, log: Path, *, fail: bool = False) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(log)!r}).open('a', encoding='utf-8')."
        "write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
        + ("sys.exit(5)\n" if fail else ""),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_rvf_handoff_cli_notify(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    notifier_log = tmp_path / "notify.log"
    notifier = _write_fake_notifier(tmp_path / "fake_notifier.py", notifier_log)
    env = {**os.environ, "CODEX_RVF_TERMINAL_NOTIFIER_BIN": str(notifier)}

    completed = run(
        [
            sys.executable,
            str(RVF_HANDOFF),
            "notify",
            str(handoff),
            "--task-url",
            "http://127.0.0.1:3484/repo?task=t1",
            "--summary",
            "两个 reviewer 通过",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)

    assert payload["valid"] is True
    assert payload["notified"] is True
    assert payload["handoff_path"] == str(handoff.resolve())
    calls = [
        json.loads(line)
        for line in notifier_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1
    assert calls[0][calls[0].index("-message") + 1] == "两个 reviewer 通过"
    assert calls[0][calls[0].index("-open") + 1] == "http://127.0.0.1:3484/repo?task=t1"


def test_rvf_handoff_marker_tail_and_summary() -> None:
    mod = _load_rvf_handoff_module()
    text = "前置废话\nRVF_HANDOFF_FILE: /tmp/h.md\n两个 reviewer 通过；1 项已修复。"
    tail = mod.handoff_marker_tail(text)
    assert tail == "RVF_HANDOFF_FILE: /tmp/h.md\n两个 reviewer 通过；1 项已修复。"
    assert mod._notification_summary(tail) == "两个 reviewer 通过；1 项已修复。"
    # 多个 marker → 取最后一个起的尾段。
    multi = "RVF_HANDOFF_FILE: /a.md\n旧\nRVF_HANDOFF_FILE: /b.md\n新结论"
    assert mod.handoff_marker_tail(multi) == "RVF_HANDOFF_FILE: /b.md\n新结论"
    assert mod.handoff_marker_tail("没有 marker") is None


def test_rvf_handoff_resolve_kanban_task_url(tmp_path: Path) -> None:
    mod = _load_rvf_handoff_module()
    home = tmp_path / "home"
    index = home / ".cline" / "kanban" / "workspaces" / "index.json"
    index.parent.mkdir(parents=True)
    repo = "/Users/x/Documents/GitHub/review-validate-fix"
    index.write_text(
        json.dumps({"repoPathToId": {repo: "review-validate-fix"}}),
        encoding="utf-8",
    )
    saved_home = os.environ.get("HOME")
    saved_port = os.environ.pop("KANBAN_RUNTIME_PORT", None)
    os.environ["HOME"] = str(home)
    try:
        # index 命中。
        assert mod.workspace_id_for_repo(repo) == "review-validate-fix"
        assert (
            mod.resolve_kanban_task_url(repo, "task-1")
            == "http://127.0.0.1:3484/review-validate-fix?task=task-1"
        )
        # index 缺失 → kebab basename 兜底。
        assert mod.workspace_id_for_repo("/Users/x/Some-Other Repo") == "some-other-repo"
        # 缺 project_path / task_id → None。
        assert mod.resolve_kanban_task_url(None, "t") is None
        assert mod.resolve_kanban_task_url(repo, None) is None
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        if saved_port is not None:
            os.environ["KANBAN_RUNTIME_PORT"] = saved_port


def test_rvf_handoff_notify_requires_terminal_notifier(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = _load_rvf_handoff_module()
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    saved_which = mod.shutil.which
    saved_bin = os.environ.pop(mod.TERMINAL_NOTIFIER_BIN_ENV, None)
    try:
        mod.shutil.which = lambda name: None
        result = mod.notify_handoff_ready(
            handoff_path=handoff, summary_text=None, task_url=None, group_ref="r"
        )
        assert result["notified"] is False
        # darwin 上缺二进制 → 显式 missing；非 darwin 且无 override → unsupported-platform。
        if sys.platform == "darwin":
            assert result["reason"] == "terminal-notifier-missing"
        else:
            assert result["reason"] == "unsupported-platform"
    finally:
        mod.shutil.which = saved_which
        if saved_bin is not None:
            os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = saved_bin


def test_rvf_handoff_maybe_trigger_kanban_notification(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = _load_rvf_handoff_module()
    ctx = {"kanban_task_id": "task-9"}
    saved = os.environ.pop(mod.KANBAN_NOTIFY_CMD_ENV, None)
    try:
        # 未配置 → 守护性 no-op。
        assert mod.maybe_trigger_kanban_notification(
            task_url="u", copy_text="c", kanban_context=ctx, project_path=None
        ) == {"triggered": False, "reason": "kanban-notify-not-configured"}
        # 配置后 → 运行命令，把 context 经 stdin(JSON) + env 传入。
        out = tmp_path / "trigger.json"
        recorder = tmp_path / "recorder.py"
        recorder.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            f"pathlib.Path({str(out)!r}).write_text("
            "sys.stdin.read() + '|' + os.environ.get('RVF_KANBAN_TASK_ID', ''), encoding='utf-8')\n",
            encoding="utf-8",
        )
        recorder.chmod(0o755)
        os.environ[mod.KANBAN_NOTIFY_CMD_ENV] = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(recorder))}"
        )
        result = mod.maybe_trigger_kanban_notification(
            task_url="http://x/repo?task=task-9",
            copy_text="尾段",
            kanban_context=ctx,
            project_path=None,
        )
        assert result["triggered"] is True
        body, task_id = out.read_text(encoding="utf-8").split("|")
        parsed = json.loads(body)
        assert parsed["task_id"] == "task-9"
        assert parsed["copy_text"] == "尾段"
        assert task_id == "task-9"
    finally:
        if saved is None:
            os.environ.pop(mod.KANBAN_NOTIFY_CMD_ENV, None)
        else:
            os.environ[mod.KANBAN_NOTIFY_CMD_ENV] = saved


@functools.lru_cache(maxsize=1)
def _repo_template() -> Path:
    """Build the canonical repo once per process; callers copy it.

    A plain `git init` repo stores no absolute worktree path in
    `.git/config`, so a filesystem copy reproduces byte-identical repo
    state (history, baked identity, dirty `tracked.txt`/untracked
    `new.txt`) without re-spawning git per test.
    """
    base = Path(tempfile.mkdtemp(prefix="rvf-repo-template-"))
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    template = base / "repo"
    template.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=template)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=template)
    run(["git", "config", "user.name", "RVF Test"], cwd=template)
    (template / "tracked.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=template)
    run(["git", "commit", "-q", "-m", "base"], cwd=template)
    (template / "tracked.txt").write_text("base\nchange\n", encoding="utf-8")
    (template / "new.txt").write_text("new\n", encoding="utf-8")
    return template


def init_repo(path: Path) -> Path:
    # `copytree` runs `os.makedirs(path)` (creates parents, errors if
    # `path` exists) — same contract as the former `path.mkdir(parents=True)`.
    shutil.copytree(_repo_template(), path, symlinks=True)
    return path


def write_alternative_reviewer_config(
    path: Path,
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None = None,
    activity_probe_timeout_seconds: float | None = None,
    activity_probe_failure_threshold: int | None = None,
    tool_failure_threshold: int | None = None,
    max_runtime_seconds: float | None = None,
    output_format: str | None = "text",
    health_command: list[str] | None = None,
    pre_run_health: bool | None = None,
) -> Path:
    payload = {
        "enabled": True,
        "label": "alternative-reviewer:test",
        "command": command,
        "allow_repo_cwd": True,
        "idle_timeout_seconds": idle_timeout_seconds,
        "activity_check_interval_seconds": activity_check_interval_seconds,
        "env_unset": [],
    }
    if activity_probe_command is not None:
        payload["activity_probe_command"] = activity_probe_command
    if activity_probe_timeout_seconds is not None:
        payload["activity_probe_timeout_seconds"] = activity_probe_timeout_seconds
    if activity_probe_failure_threshold is not None:
        payload["activity_probe_failure_threshold"] = activity_probe_failure_threshold
    if tool_failure_threshold is not None:
        payload["tool_failure_threshold"] = tool_failure_threshold
    if max_runtime_seconds is not None:
        payload["max_runtime_seconds"] = max_runtime_seconds
    if output_format is not None:
        payload["output_format"] = output_format
    if health_command is not None:
        payload["health_command"] = health_command
    if pre_run_health is not None:
        payload["pre_run_health"] = pre_run_health
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def clean_review_result_python(*, stdout: str = "artifact written") -> str:
    return (
        "import os, subprocess, sys; "
        "sys.stdin.read(); "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
        f"print({stdout!r})"
    )


def write_codex_transcript(path: Path, repo: Path) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        "*** Update File: tracked.txt\n"
        "@@\n"
        " base\n"
        "+change\n"
        "*** Add File: owned-new.txt\n"
        "+owned\n"
        "*** Delete File: removed.txt\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2999-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-tracking-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
        {
            "timestamp": "2999-04-27T00:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "printf generated > generated.txt", "workdir": str(repo)}),
                "call_id": "call_exec",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def test_check_review_output_lock_request() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="RVF_LOCK_REQUEST name=npm-test command=npm test reason=shared-cache\n",
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "lock_request"
    assert payload["lock_request_count"] == 1

    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name=n command=x reason=y\nNO_ISSUES\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0

    malformed_lock = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST please-lock-npm-test\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_lock.returncode != 0

    empty_lock_field = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name= command=npm test reason=shared-cache\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty_lock_field.returncode != 0


def test_check_review_output_protocol_extension_requests() -> None:
    standard = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "RVF_STANDARD_REQUEST domain=security reason=auth-boundary scope=src/auth.py\n"
        ),
    )
    standard_payload = json.loads(standard.stdout)
    assert standard_payload["valid"] is True
    assert standard_payload["kind"] == "request"
    assert standard_payload["request_count"] == 1
    assert standard_payload["request_types"] == ["standard_request"]

    mixed_requests = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "RVF_MEASUREMENT_REQUEST metric=p95 command=pytest reason=needs-baseline\n"
            "RVF_CONTEXT_REQUEST need=test-result reason=compare-existing-output\n"
            "RVF_SUBTASK_REQUEST type=security_check scope=src/auth.py reason=auth-change\n"
        ),
    )
    mixed_payload = json.loads(mixed_requests.stdout)
    assert mixed_payload["valid"] is True
    assert mixed_payload["kind"] == "request"
    assert mixed_payload["request_count"] == 3
    assert mixed_payload["request_types"] == [
        "context_request",
        "measurement_request",
        "subtask_request",
    ]

    malformed = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_MEASUREMENT_REQUEST metric=p95 reason=missing-command\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode != 0

    empty_field = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_STANDARD_REQUEST domain=security reason= scope=src/auth.py\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty_field.returncode != 0

    invalid_standard_domain = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_STANDARD_REQUEST domain=privacy reason=auth-boundary scope=src/auth.py\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_standard_domain.returncode != 0

    invalid_subtask_type = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_SUBTASK_REQUEST type=delete_repo scope=src/auth.py reason=auth-change\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_subtask_type.returncode != 0

    invalid_context_need = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_CONTEXT_REQUEST need=secret reason=compare-existing-output\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_context_need.returncode != 0

    mixed_with_completion = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input=(
            "RVF_STANDARD_REQUEST domain=performance reason=needs-budget scope=src/app.ts\n"
            "NO_ISSUES\n"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert mixed_with_completion.returncode != 0


def test_review_result_artifact_no_issues_and_issues(tmp_path: Path) -> None:
    clean = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")

    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "no-issues",
            "--out",
            str(clean),
            "--audit-summary",
            "审了 src/foo.ts 与 Dockerfile 改动及其边界，未发现 correctness 问题。",
        ],
        env=env,
    )
    clean_check = run([sys.executable, str(CHECK_REVIEW_RESULT), str(clean), "--json"])
    clean_payload = json.loads(clean_check.stdout)
    assert clean_payload["valid"] is True
    assert clean_payload["kind"] == "no_issues"
    assert clean_payload["audit_summary"].strip()
    assert json.loads(clean.read_text(encoding="utf-8"))["audit_summary"].strip()

    issues = tmp_path / "run" / "artifacts" / "reviewers" / "b" / "review-result.json"
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(issues),
            "--path",
            "src/foo.ts",
            "--line",
            "42",
            "--message",
            "空输入时会跳过必要校验。",
            "--kind",
            "REAL",
            "--severity",
            "high",
        ],
        env=env,
    )
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(issues),
            "--path",
            "Dockerfile",
            "--line",
            "3",
            "--message",
            "构建参数没有传入默认值。",
            "--kind",
            "REAL",
            "--severity",
            "medium",
        ],
        env=env,
    )
    issue_check = run([sys.executable, str(CHECK_REVIEW_RESULT), str(issues), "--json"])
    issue_payload = json.loads(issue_check.stdout)
    assert issue_payload["valid"] is True
    assert issue_payload["kind"] == "issues"
    assert issue_payload["issue_count"] == 2
    assert issue_payload["issues"][0]["path"] == "src/foo.ts"
    assert issue_payload["issues"][0]["kind"] == "REAL"
    assert issue_payload["issues"][0]["severity"] == "high"
    assert issue_payload["issues"][1]["kind"] == "REAL"
    assert issue_payload["issues"][1]["severity"] == "medium"


def test_no_issues_requires_audit_summary(tmp_path: Path) -> None:
    """no_issues 必须带非空 audit_summary：写入层强制必填，校验层拒绝缺失/空白，正常路径会被 surface。"""
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"

    # 1) 写入层：缺 --audit-summary 必须失败
    missing = subprocess.run(
        [sys.executable, str(WRITE_REVIEW_RESULT), "no-issues", "--out", str(out)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert missing.returncode != 0
    assert "audit-summary" in (missing.stderr + missing.stdout)

    # 2) 写入层：空白 audit_summary 必须失败
    blank = subprocess.run(
        [sys.executable, str(WRITE_REVIEW_RESULT), "no-issues", "--out", str(out),
         "--audit-summary", "   "],
        env=env, capture_output=True, text=True, check=False,
    )
    assert blank.returncode != 0

    # 3) 校验层：手写一个缺 audit_summary 的 no_issues artifact 必须被判违规
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({
            "schema_version": 1, "kind": "no_issues",
            "created_at": "2026-05-30T00:00:00+00:00",
            "issues": [], "requests": [],
        }) + "\n", encoding="utf-8",
    )
    rejected = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(out), "--json"],
        capture_output=True, text=True, check=False,
    )
    payload = json.loads(rejected.stdout)
    assert payload["valid"] is False
    assert any("audit_summary" in e for e in payload["errors"])

    # 4) 正常路径：带 audit_summary 写入并通过校验，且被 surface 出来
    run(
        [sys.executable, str(WRITE_REVIEW_RESULT), "no-issues", "--out", str(out),
         "--audit-summary", "逐函数核对 docstring 与边界，确认无 correctness 回归。"],
        env=env,
    )
    ok = json.loads(run([sys.executable, str(CHECK_REVIEW_RESULT), str(out), "--json"]).stdout)
    assert ok["valid"] is True
    assert ok["kind"] == "no_issues"
    assert "docstring" in ok["audit_summary"]


def test_review_result_artifact_requests_and_scope_exclusions(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "lock-request",
            "--out",
            str(result),
            "--name",
            "npm-test",
            "--command",
            "npm test",
            "--reason",
            "测试命令会争用共享缓存。",
        ],
        env=env,
    )
    payload = json.loads(run([sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"]).stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "request"
    assert payload["request_types"] == ["lock_request"]

    excluded = tmp_path / "run" / "artifacts" / "reviewers" / "b" / "review-result.json"
    contract = tmp_path / "scope.contract.json"
    contract.write_text('{"canonical_scope":{"excluded_path_prefixes":["vendor"]}}\n', encoding="utf-8")
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(excluded),
            "--path",
            "vendor/generated.py",
            "--line",
            "1",
            "--message",
            "不应报告 excluded path。",
            "--kind",
            "REAL",
            "--severity",
            "medium",
        ],
        env=env,
    )
    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(excluded), "--scope-contract", str(contract), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0
    assert "excluded" in invalid.stdout


def test_review_result_artifact_rejects_malformed_and_mixed_state(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")

    missing = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "missing review result artifact" in missing.stdout

    bad_path = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(result),
            "--path",
            "../escape.py",
            "--line",
            "1",
            "--message",
            "bad",
            "--kind",
            "REAL",
            "--severity",
            "high",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_path.returncode != 0

    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "no_issues",
                "issues": [
                    {
                        "path": "src/a.py",
                        "line": 1,
                        "message": "mixed",
                        "kind": "REAL",
                        "severity": "high",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    mixed = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert mixed.returncode != 0
    assert "no_issues result must not include issues" in mixed.stdout

    outside = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "no-issues",
            "--out",
            str(tmp_path / "outside.json"),
            "--audit-summary",
            "审了改动，无问题。",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert outside.returncode != 0
    assert "RVF_RUN_DIR" in outside.stderr


def test_issue_requires_kind(tmp_path: Path) -> None:
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    result = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(out),
            "--path",
            "src/foo.py",
            "--line",
            "1",
            "--message",
            "missing kind argument",
            "--severity",
            "high",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--kind" in result.stderr


def test_issue_requires_severity(tmp_path: Path) -> None:
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    result = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(out),
            "--path",
            "src/foo.py",
            "--line",
            "1",
            "--message",
            "missing severity argument",
            "--kind",
            "REAL",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--severity" in result.stderr


def test_check_rejects_issue_without_kind(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "issues",
                "issues": [
                    {
                        "path": "src/foo.py",
                        "line": 1,
                        "message": "missing kind",
                        "severity": "high",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    check = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "kind" in check.stdout


def test_check_rejects_invalid_severity(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "issues",
                "issues": [
                    {
                        "path": "src/foo.py",
                        "line": 1,
                        "message": "bad severity value",
                        "kind": "REAL",
                        "severity": "wat",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    check = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "severity" in check.stdout


def test_check_skill_contracts_requires_validate_fix_request_literals() -> None:
    script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    for literal in (
        "require_literal \"prompts/validate-fix.md\" 'RVF_STANDARD_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_MEASUREMENT_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_SUBTASK_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_CONTEXT_REQUEST'",
    ):
        assert literal in script


def test_contract_check_entrypoints_default_quiet_with_verbose_flag() -> None:
    skill_script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    plugin_script = CHECK_PLUGIN_CONTRACTS.read_text(encoding="utf-8")
    for literal in (
        "verbose=0",
        "-v|--verbose)",
        "run_step()",
        "run_parallel_test_steps()",
        "RVF_CONTRACT_PARALLEL_TESTS",
        "RVF_CONTRACT_PARALLEL_JOBS",
        "RVF_CONTRACT_REVIEW_SUPPORT_SHARDS",
        "RVF_CONTRACT_STOP_HOOK_SHARDS",
        "RVF_CONTRACT_DISPATCHER_SHARDS",
        "command_status",
        "验证失败:",
        'return "$command_status"',
    ):
        assert literal in skill_script
    for literal in (
        'parser.add_argument("-v", "--verbose"',
        'command.append("--verbose")',
        "capture_output=True",
        "plugin 契约检查通过",
    ):
        assert literal in plugin_script

    function_start = skill_script.index("timestamp_ms() {")
    function_end = skill_script.index("\nhash_file() {")
    probe = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "verbose=0\n"
        f"{skill_script[function_start:function_end]}\n"
        f"run_step failing {shlex.quote(sys.executable)} -c "
        "'import sys; print(\"boom\"); sys.exit(7)'\n"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        probe_path = Path(tmp_dir) / "probe.sh"
        timing_path = Path(tmp_dir) / "timing.jsonl"
        probe_path.write_text(probe, encoding="utf-8")
        env = os.environ.copy()
        env["RVF_CONTRACT_TIMING_JSONL"] = str(timing_path)
        env["RVF_CONTRACT_TIMING_SCRIPT"] = str(CHECK_SKILL_CONTRACTS)
        completed = subprocess.run(
            ["bash", str(probe_path)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert not timing_path.exists()
    assert completed.returncode == 7
    assert "验证失败: failing" in completed.stderr
    assert "boom" in completed.stderr


def test_contract_check_parallel_test_steps_record_parallel_timing() -> None:
    skill_script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    function_start = skill_script.index("timestamp_ms() {")
    function_end = skill_script.index("\nhash_file() {")
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        tests_dir = root / "tests"
        tests_dir.mkdir()
        for name in (
            "test_install_to_codex.py",
            "test_rvf_handoff_intake.py",
            "test_review_reopen_marker.py",
            "test_review_support_scripts.py",
            "test_codex_stop_hook_dispatcher.py",
            "test_codex_stop_review_validate_fix.py",
        ):
            (tests_dir / name).write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(0.05)\n"
                "print('ok')\n",
                encoding="utf-8",
            )
        timing_path = root / "timing.jsonl"
        probe = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "verbose=0\n"
            f"tests_dir={shlex.quote(str(tests_dir))}\n"
            f"export RVF_CONTRACT_TIMING_JSONL={shlex.quote(str(timing_path))}\n"
            "export RVF_CONTRACT_TIMING_SCRIPT=\"$0\"\n"
            "export RVF_CONTRACT_PARALLEL_TESTS=1\n"
            "export RVF_CONTRACT_PARALLEL_JOBS=4\n"
            "export RVF_CONTRACT_REVIEW_SUPPORT_SHARDS=4\n"
            "export RVF_CONTRACT_STOP_HOOK_SHARDS=4\n"
            "export RVF_CONTRACT_DISPATCHER_SHARDS=2\n"
            f"{skill_script[function_start:function_end]}\n"
            "run_parallel_test_steps\n"
        )
        probe_path = root / "probe.sh"
        probe_path.write_text(probe, encoding="utf-8")
        completed = subprocess.run(
            ["bash", str(probe_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        records = [json.loads(line) for line in timing_path.read_text(encoding="utf-8").splitlines()]

    assert completed.returncode == 0
    assert [record["execution_mode"] for record in records] == ["parallel"] * 13
    assert {record["label"] for record in records} == {
        "tests: install_to_codex",
        "tests: rvf_handoff_intake",
        "tests: review_reopen_marker",
        "tests: review_support_scripts shard 1/4",
        "tests: review_support_scripts shard 2/4",
        "tests: review_support_scripts shard 3/4",
        "tests: review_support_scripts shard 4/4",
        "tests: codex_stop_hook_dispatcher shard 1/2",
        "tests: codex_stop_hook_dispatcher shard 2/2",
        "tests: codex_stop_review_validate_fix shard 1/4",
        "tests: codex_stop_review_validate_fix shard 2/4",
        "tests: codex_stop_review_validate_fix shard 3/4",
        "tests: codex_stop_review_validate_fix shard 4/4",
    }


def test_contract_check_timing_report_accounts_internal_steps() -> None:
    module = load_check_plugin_contracts_module()

    report = module.build_timing_report(
        started_at="2026-05-01T00:00:00Z",
        ended_at="2026-05-01T00:00:01Z",
        duration_ms=1000,
        returncode=0,
        command=["bash", "scripts/check_skill_contracts.sh"],
        top_level_steps=[
            {
                "label": "preflight",
                "source": "check_plugin_contracts.py",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 50,
            },
            {
                "label": "contract shell script",
                "source": "check_plugin_contracts.py",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 900,
            },
        ],
        shell_steps=[
            {
                "label": "python compile",
                "source": "check_skill_contracts.sh",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 200,
            },
            {
                "label": "tests: codex_stop_review_validate_fix",
                "source": "check_skill_contracts.sh",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 600,
            },
        ],
    )

    assert report["slowest_step"]["label"] == "tests: codex_stop_review_validate_fix"
    assert report["slowest_step"]["percentage_of_total"] == 60.0
    assert report["slowest_step"]["percentage_of_wall_time"] == 60.0
    assert report["slowest_step"]["percentage_of_measured_work"] == 63.16
    assert report["measured_work_duration_ms"] == 950
    assert any(step["label"] == "shell script overhead" for step in report["steps"])
    assert report["groups"][0]["name"] == "tests"
    assert report["groups"][0]["duration_ms"] == 600


def test_run_ledger_summary_preserves_contract_timing_fields(tmp_path: Path) -> None:
    module = load_rvf_logging_module()
    ledger = module.RunLedger(
        component="dispatcher",
        run_id="rvf-contract-timing-preserve",
        run_dir=tmp_path / "run",
    )

    ledger.summary(
        status="synced",
        reason_code="synced",
        dev_sync_steps=[{"name": "contract-check"}],
        contract_check_timing_report_path="/tmp/contract-check.timing.json",
        contract_check_timing={
            "path": "/tmp/contract-check.timing.json",
            "slowest_step": {"label": "tests: codex_stop_review_validate_fix"},
        },
    )
    later = ledger.summary(
        status="session-hook-control",
        reason_code="session_hook_gate_disabled",
    )

    assert later["dev_sync_steps"] == [{"name": "contract-check"}]
    assert later["contract_check_timing_report_path"] == "/tmp/contract-check.timing.json"
    assert later["contract_check_timing"]["slowest_step"]["label"] == (
        "tests: codex_stop_review_validate_fix"
    )


def test_rvf_logging_non_canonical_skill_dirs_default_to_installed_plugin_state(
    tmp_path: Path,
) -> None:
    module = load_rvf_logging_module()
    installed_skill = tmp_path / "home" / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
    installed_skill.mkdir(parents=True)
    (installed_skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    cline_skill = (
        tmp_path
        / "home"
        / ".cline"
        / "worktrees"
        / "9336c"
        / "review-validate-fix"
        / "plugins"
        / "review-validate-fix"
        / "skills"
        / "review-validate-fix"
    )
    dev_skill = tmp_path / "dev" / "skills" / "review-validate-fix"

    original = os.environ.get("CODEX_RVF_INSTALLED_SKILL_DIR")
    os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = str(installed_skill)
    try:
        assert module.default_log_root_for_skill_dir(cline_skill) == installed_skill / "state"
        assert module.default_log_root_for_skill_dir(dev_skill) == installed_skill / "state"
        assert (
            module.default_log_root_for_skill_dir(installed_skill) == installed_skill / "state"
        )
    finally:
        if original is None:
            os.environ.pop("CODEX_RVF_INSTALLED_SKILL_DIR", None)
        else:
            os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = original


def test_rvf_logging_falls_back_to_skill_dir_state_when_install_missing(
    tmp_path: Path,
) -> None:
    module = load_rvf_logging_module()
    installed_skill = tmp_path / "home" / "plugins" / "missing" / "skills" / "review-validate-fix"
    dev_skill = tmp_path / "dev" / "skills" / "review-validate-fix"

    original = os.environ.get("CODEX_RVF_INSTALLED_SKILL_DIR")
    os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = str(installed_skill)
    try:
        assert module.default_log_root_for_skill_dir(dev_skill) == dev_skill / "state"
    finally:
        if original is None:
            os.environ.pop("CODEX_RVF_INSTALLED_SKILL_DIR", None)
        else:
            os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = original


def test_check_review_output_accepts_wrapped_issue_continuation() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "1. apps/theseus-mcp/src/tool_registry.ts:1306 task 级上下文先截断 reviewRuns。\n"
            "`query_checkpoint_context` 随后用截断后的 run 集合过滤 signals，可能漏掉同 task 的较早 run。\n"
        ),
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "issues"
    assert payload["issue_count"] == 1
    assert payload["continuation_line_count"] == 1

    cro = load_check_review_output_module()

    accept_cases = [
        ("extensionless_numbered", "1. Dockerfile:3 合法 issue 可以引用没有扩展名的文件。\n"),
        ("spaced_path", "1. slide-versions/claude cowork 1/deck.txt:2 含空格路径仍是合法 path:line。\n"),
        ("spaced_root_component", "1. my dir/file.py:2 根目录组件含空格仍是合法 path:line。\n"),
        ("colon_path", "1. foo:bar.py:2 路径名含冒号时应使用最后的 :line 作为行号。\n"),
        ("unicode_root_path", "1. 设计 文档.md:3 非 ASCII 根路径也应支持。\n"),
        ("repeated_path_line", "1. a.py:1 causes b.py:2 to fail when both paths are involved.\n"),
    ]
    for case_id, text in accept_cases:
        out = cro.classify(text)
        assert out["valid"] is True, f"[{case_id}] expected valid, got {out}"
        assert out["issue_count"] == 1, f"[{case_id}] expected 1 issue, got {out}"

    reject_cases = [
        ("unnumbered_missing_line", "1. apps/foo.ts 这条缺少行号\n续行不能补足 path:line\n"),
        ("misplaced_path_line", "1. 这里先写说明，再引用 plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n"),
        ("english_misplaced_path_line", "1. explanation before plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n"),
        ("prose_see_path_line", "1. See plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n"),
        ("prose_in_path_line", "1. in plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n"),
        ("prose_because_path_line", "1. Because a.py:1 misplaced path\n"),
        ("chinese_because_path_line", "1. 因为 a.py:1 misplaced path\n"),
        ("chinese_file_path_line", "1. 文件 a.py:1 misplaced path\n"),
        ("prose_note_colon_path_line", "1. Note: a.py:1 misplaced path\n"),
        ("prose_warning_path_line", "1. warning a.py:1 misplaced path\n"),
        (
            "invalid_extensionless",
            "1. plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 valid issue\n"
            "Dockerfile:2 missing numbered prefix\n"
            "Makefile:10 missing numbered prefix\n",
        ),
        ("unnumbered_issue", "1. a.py:1 第一条问题\nb.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_no_extension_issue", "1. a.py:1 第一条问题\nMakefile:2 第二条问题但缺少编号\n"),
        ("malformed_numbered_issue", "1. a.py:1 第一条问题\n2) b.py:2 第二条编号格式错误\n"),
        ("malformed_numbered_continuation", "1. a.py:1 第一条问题\n2) 第二条编号格式错误\n"),
        ("chinese_no_issue_continuation", "1. a.py:1 第一条问题\n没有问题\n"),
        ("fix_summary_continuation", "1. a.py:1 第一条问题\n修复说明：已修改文件\n"),
        ("handoff_completion_continuation", "1. a.py:1 第一条问题\nRVF_HANDOFF_FILE: /tmp/rvf-handoff.md\n"),
        ("handoff_reviewers_summary_continuation", "1. a.py:1 第一条问题\nReviewers：NO_ISSUES\n"),
        ("handoff_validate_fixers_summary_continuation", "1. a.py:1 第一条问题\nValidate/fixers：REAL fixed\n"),
        ("unnumbered_spaced_issue", "1. a.py:1 第一条问题\nmy file.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_spaced_dir_issue", "1. a.py:1 第一条问题\nmy dir/file.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_colon_issue", "1. a.py:1 第一条问题\nfoo:bar.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_unicode_issue", "1. a.py:1 第一条问题\n设计 文档.md:3 第二条问题但缺少编号\n"),
    ]
    for case_id, text in reject_cases:
        out = cro.classify(text)
        assert out["valid"] is False, f"[{case_id}] expected rejection, got {out}"


def test_complete_review_scope_waits_for_all_edit_claim_units(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    run(["git", "add", "a.txt", "b.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("a\nclaimed-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\nclaimed-b\n", encoding="utf-8")
    log_root = tmp_path / "logs"
    registered = module.register_claims(
        repo=repo,
        session_id="S",
        run_id="run-1",
        worktree=repo,
        branch=None,
        owned_paths=["a.txt", "b.txt"],
        apply_patch_paths={"a.txt", "b.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert registered.status == "ok"
    unit_a = module.unit_ids_for_owned_unit(repo, module.OwnedUnit(path="a.txt", unit="path", hunk_anchor=None))[0]
    unit_b = module.unit_ids_for_owned_unit(repo, module.OwnedUnit(path="b.txt", unit="path", hunk_anchor=None))[0]
    edit_registered = module.register_edit_claims(
        repo=repo,
        session_id="S",
        run_id="run-1",
        edit_claims=[
            {
                "claim_id": "edit-two-units",
                "tool_name": "apply_patch",
                "call_id": "patch-1",
                "transcript_line_number": 10,
                "path": "a.txt",
                "hunk_index": 1,
                "operation": "update",
                "mapped_unit_ids": [unit_a, unit_b],
                "latest_user_line_number": 2,
                "latest_user_message": "change both files",
            }
        ],
        log_root_override=log_root,
    )
    assert edit_registered["status"] == "ok"

    first = module.complete_review_scope(
        repo=repo,
        lease_id="missing-lease-a",
        unit_ids=[unit_a],
        scope_hash="sha256:first",
        run_id="review-a",
        log_root_override=log_root,
    )
    assert first["status"] == "released"
    assert first["reviewed_edit_claim_count"] == 0
    tracker_dir = Path(first["tracker_dir"])
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        assert conn.execute("SELECT status FROM edit_claims WHERE claim_id='edit-two-units'").fetchone()[0] == "pending"

    second = module.complete_review_scope(
        repo=repo,
        lease_id="missing-lease-b",
        unit_ids=[unit_b],
        scope_hash="sha256:second",
        run_id="review-b",
        log_root_override=log_root,
    )
    assert second["status"] == "released"
    assert second["reviewed_edit_claim_count"] == 1
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        assert conn.execute("SELECT status FROM edit_claims WHERE claim_id='edit-two-units'").fetchone()[0] == "reviewed"


def test_diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    transcript = tmp_path / "claude-session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-06T06:00:00.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": str(repo / "tracked.txt")},
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "stop-event.json").write_text(
        json.dumps({"cwd": str(repo), "transcript_path": str(transcript)}, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifacts / "session-manifest.json").write_text(
        json.dumps(
            {
                "owned_paths": [],
                "owned_dirty_paths": [],
                "unattributed_dirty_paths": ["tracked.txt"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary = run_dir / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-06T06:00:01Z",
                "status": "skipped",
                "reason_code": "no_session_owned_dirty",
                "repo": str(repo),
                "run_dir": str(run_dir),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    old_scripts = tmp_path / "old-scripts"
    old_scripts.mkdir()
    (old_scripts / "session_manifest.py").write_text(
        "def build_manifest():\n"
        "    return {'owned_dirty_paths': []}\n",
        encoding="utf-8",
    )
    (old_scripts / "codex_stop_review_validate_fix.py").write_text(
        "def legacy_session_scope_gate_payload():\n"
        "    pass\n",
        encoding="utf-8",
    )

    completed = run(
        [
            sys.executable,
            str(DIAGNOSE_STOP_HOOK_SCOPE),
            "--summary",
            str(summary),
            "--runtime-scripts-dir",
            str(old_scripts),
            "--format",
            "json",
        ]
    )
    payload = json.loads(completed.stdout)
    codes = {item["code"] for item in payload["diagnoses"]}

    assert payload["transcript_probe"]["claude_dirty_write_paths"] == ["tracked.txt"]
    assert "stop_hook_skipped_no_session_owned_dirty" in codes
    assert "run_manifest_missing_tracker_field" in codes
    assert "claude_writes_not_attributed" in codes
    assert "runtime_session_manifest_differs_from_reference" in codes
    assert "runtime_session_manifest_lacks_tracker_field" in codes


def test_command_lock_writes_lifecycle_events(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    run_id = "test-command-lock-lifecycle"
    env = os.environ.copy()
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    env["CODEX_RVF_RUN_ID"] = run_id
    env.pop("CODEX_RVF_RUN_DIR", None)

    locked = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "lifecycle-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ],
        env=env,
    )

    assert "locked" in locked.stdout
    events = read_jsonl(state / "runs" / run_id / "events.jsonl")
    event_names = [event["event"] for event in events]
    assert event_names == ["lock_wait_started", "lock_acquired", "lock_released"]
    assert {event["component"] for event in events} == {"command-lock"}
    assert all(event["phase"] == "review" for event in events)
    assert events[1]["lock_name"] == "lifecycle-test"
    assert events[2]["returncode"] == 0

    summary = json.loads((state / "runs" / run_id / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["reason_code"] == "lock_released"
    assert summary["lock_name"] == "lifecycle-test"


def test_command_lock_respects_env_run_dir(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    run_dir = tmp_path / "custom-run-dir"
    env = os.environ.copy()
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    env["CODEX_RVF_RUN_ID"] = "test-command-lock-custom-dir"
    env["CODEX_RVF_RUN_DIR"] = str(run_dir)

    run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "custom-dir-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ],
        env=env,
    )

    assert (run_dir / "events.jsonl").exists()
    assert not (state / "runs" / "test-command-lock-custom-dir" / "events.jsonl").exists()
    events = read_jsonl(run_dir / "events.jsonl")
    assert [event["event"] for event in events] == ["lock_wait_started", "lock_acquired", "lock_released"]


def test_command_lock_logs_timeout_with_holder_metadata(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    lock_dir = tmp_path / "locks"
    holder_env = os.environ.copy()
    holder_env["CODEX_RVF_LOG_ROOT"] = str(state)
    holder_env["CODEX_RVF_RUN_ID"] = "test-command-lock-holder"
    holder_env.pop("CODEX_RVF_RUN_DIR", None)
    contender_env = os.environ.copy()
    contender_env["CODEX_RVF_LOG_ROOT"] = str(state)
    contender_env["CODEX_RVF_RUN_ID"] = "test-command-lock-contender"
    contender_env.pop("CODEX_RVF_RUN_DIR", None)

    lock_path_result = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contended-test",
            "--lock-dir",
            str(lock_dir),
            "--print-path",
        ],
    )
    metadata_path = Path(lock_path_result.stdout.strip()).with_suffix(".json")

    holder = subprocess.Popen(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contended-test",
            "--lock-dir",
            str(lock_dir),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(3)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_env,
    )
    try:
        deadline = time.monotonic() + 5
        while not metadata_path.exists():
            if holder.poll() is not None:
                stdout, stderr = holder.communicate()
                raise AssertionError(stderr.strip() or stdout.strip() or "holder exited before acquiring lock")
            if time.monotonic() >= deadline:
                raise AssertionError("holder did not acquire lock")
            time.sleep(0.01)

        contender = subprocess.run(
            [
                sys.executable,
                str(COMMAND_LOCK),
                "--repo",
                str(repo),
                "--name",
                "contended-test",
                "--lock-dir",
                str(lock_dir),
                "--timeout",
                "0.3",
                "--poll-interval",
                "0.05",
                "--",
                sys.executable,
                "-c",
                "print('should-not-run')",
            ],
            capture_output=True,
            text=True,
            env=contender_env,
            check=False,
        )
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder.communicate(timeout=5)

    assert contender.returncode == 75
    assert "current holder metadata" in contender.stderr
    events = read_jsonl(state / "runs" / "test-command-lock-contender" / "events.jsonl")
    event_names = [event["event"] for event in events]
    assert event_names == ["lock_wait_started", "lock_timeout"]
    timeout_event = events[-1]
    assert timeout_event["reason_code"] == "lock_timeout"
    assert timeout_event["lock_name"] == "contended-test"
    assert "holder_metadata" in timeout_event
    assert "contended-test" in str(timeout_event["holder_metadata"])


def _cursor_tool_call_line(tool_key: str, result: dict) -> str:
    return json.dumps(
        {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {tool_key: {"result": result}},
        }
    )


def test_cursor_stream_tool_layer_health_classifier_and_summary() -> None:
    health = load_cursor_tool_layer_health_module()
    classify = health.classify_cursor_tool_call_outcome

    # spawnError（shell 执行器坏了）=> runtime_failure
    assert (
        classify({"shellToolCall": {"result": {"spawnError": {"error": "returned no exit status"}}}})
        == "runtime_failure"
    )
    # read/glob 的 Aborted（两种 error 嵌套形态）=> runtime_failure
    assert classify({"readToolCall": {"result": {"error": {"errorMessage": "Aborted"}}}}) == "runtime_failure"
    assert classify({"globToolCall": {"result": {"error": {"error": "Aborted"}}}}) == "runtime_failure"
    # 关键反例：普通工具错误（文件不存在）证明工具层在工作 => ok，绝不能误判成 runtime_failure
    assert (
        classify({"readToolCall": {"result": {"error": {"errorMessage": "ENOENT: no such file"}}}})
        == "ok"
    )
    # 成功 => ok；非工具结果 => None
    assert classify({"shellToolCall": {"result": {"success": "out", "isBackground": False}}}) == "ok"
    assert classify({"toolCallId": "x"}) is None
    assert classify("not-a-dict") is None

    broken_stream = "\n".join(
        _cursor_tool_call_line("shellToolCall", {"spawnError": {"error": "returned no exit status"}})
        for _ in range(4)
    )
    broken = health.summarize_cursor_stream_tool_layer(broken_stream)
    assert broken == {
        "tool_runtime_failures": 4,
        "tool_successes": 0,
        "completed_tool_calls": 4,
        "healthy": False,
    }

    healthy_stream = "\n".join(
        [
            _cursor_tool_call_line("shellToolCall", {"success": "ok", "isBackground": False}),
            _cursor_tool_call_line("readToolCall", {"success": {"content": "x"}}),
        ]
    )
    assert health.summarize_cursor_stream_tool_layer(healthy_stream)["healthy"] is True


def test_apply_worktree_bootstrap_replays_tracked_and_untracked(tmp_path: Path) -> None:
    source = init_repo(tmp_path / "source")
    clone = tmp_path / "clone"
    run(["git", "clone", "-q", str(source), str(clone)], cwd=tmp_path)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (source / "owned.txt").write_text("owned\n", encoding="utf-8")
    base_ref = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    patch = tmp_path / "bootstrap.patch"
    patch.write_text(subprocess.run(["git", "diff", "--binary", "HEAD", "--", "tracked.txt"], cwd=source, check=True, capture_output=True, text=True).stdout, encoding="utf-8")
    files = tmp_path / "files"
    files.mkdir()
    stored = files / "owned.txt"
    stored.write_text("owned\n", encoding="utf-8")
    metadata = tmp_path / "bootstrap.json"
    metadata.write_text(json.dumps({"base_ref": base_ref, "patch_file": str(patch), "files_dir": str(files), "untracked_files": [{"path": "owned.txt", "stored_path": str(stored)}]}), encoding="utf-8")
    completed = run([sys.executable, str(APPLY_WORKTREE_BOOTSTRAP), "--metadata", str(metadata), "--repo", str(clone)])
    assert json.loads(completed.stdout)["ok"] is True
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "changed\n"
    assert (clone / "owned.txt").read_text(encoding="utf-8") == "owned\n"


def test_apply_worktree_bootstrap_rejects_mismatched_base_ref(tmp_path: Path) -> None:
    source = init_repo(tmp_path / "source")
    clone = tmp_path / "clone"
    run(["git", "clone", "-q", str(source), str(clone)], cwd=tmp_path)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    base_ref = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    patch = tmp_path / "bootstrap.patch"
    patch.write_text(subprocess.run(["git", "diff", "--binary", "HEAD", "--", "tracked.txt"], cwd=source, check=True, capture_output=True, text=True).stdout, encoding="utf-8")
    metadata = tmp_path / "bootstrap.json"
    metadata.write_text(json.dumps({"base_ref": base_ref, "patch_file": str(patch), "untracked_files": []}), encoding="utf-8")

    (clone / "other.txt").write_text("other\n", encoding="utf-8")
    run(["git", "add", "other.txt"], cwd=clone)
    run(["git", "commit", "-q", "-m", "advance clone"], cwd=clone)
    completed = subprocess.run(
        [sys.executable, str(APPLY_WORKTREE_BOOTSTRAP), "--metadata", str(metadata), "--repo", str(clone)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "base_ref mismatch" in completed.stdout
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "base\n"


def test_run_ledger_summary_preserves_cline_kanban_fields(tmp_path: Path) -> None:
    module = load_rvf_logging_module()
    run_dir = tmp_path / "run"
    ledger = module.RunLedger(component="stop-hook", repo=tmp_path, cwd=tmp_path, run_id="run-1", run_dir=run_dir)
    ledger.summary(
        status="cline-kanban-started",
        reason_code="cline_kanban_task_started",
        cline_kanban_task_id="task-1",
        cline_kanban_base_ref="HEAD",
        worktree_bootstrap_path="/tmp/bootstrap.json",
    )
    later = ledger.summary(status="completed", reason_code="prepare_completed", message="later phase")
    assert later["cline_kanban_task_id"] == "task-1"
    assert later["cline_kanban_base_ref"] == "HEAD"
    assert later["worktree_bootstrap_path"] == "/tmp/bootstrap.json"


def test_run_ledger_summary_preserves_rvf_state_fields(tmp_path: Path) -> None:
    module = load_rvf_logging_module()
    run_dir = tmp_path / "run"
    ledger = module.RunLedger(component="stop-hook", repo=tmp_path, cwd=tmp_path, run_id="run-1", run_dir=run_dir)
    ledger.summary(
        status="cline-kanban-started",
        reason_code="cline_kanban_task_started",
        **module.rvf_state_fields(
            phase="prepare",
            backend="kanban-task",
            backend_raw="cline-kanban",
            scope_contract_path="/tmp/scope.contract.json",
            review_packet_path="/tmp/review-packet.md",
        ),
    )
    later = ledger.summary(status="completed", reason_code="prepare_completed", message="later phase")
    assert later["rvf_backend"] == "kanban-task"
    assert later["rvf_backend_raw"] == "cline-kanban"
    assert later["rvf_state_phase"] == "prepare"
    assert later["rvf_scope_contract_path"] == "/tmp/scope.contract.json"
    assert later["rvf_review_packet_path"] == "/tmp/review-packet.md"
    assert later["rvf_state"]["phases"] == list(module.RVF_STATE_PHASES)


def test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / "runs" / "rvf-user-cancel"
    run_dir.mkdir(parents=True)
    repo = init_repo(tmp_path / "repo")
    fake_task = tmp_path / "fake_task.py"
    calls = tmp_path / "trash-calls.jsonl"
    fake_task.write_text(
        "import json, os, sys\n"
        "with open(os.environ['KANBAN_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'task_id': sys.argv[-1], 'status': 'trashed'}))\n",
        encoding="utf-8",
    )
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run_id": "rvf-user-cancel",
                "status": "cline-kanban-started",
                "repo": str(repo),
                "cwd": str(repo),
                "run_dir": str(run_dir),
                "cline_kanban_task_id": "task-1",
                "runner_pid": 999999,
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["KANBAN_CALLS"] = str(calls)
    completed = run(
        [
            sys.executable,
            str(CANCEL_RVF_RUN),
            "--summary",
            str(summary_path),
            "--force-after",
            "0",
            "--task-cmd",
            f"{sys.executable} {fake_task}",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "cancelled"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "cline-kanban-rvf-cancelled"
    assert summary["reason_code"] == "user_cancelled"
    events = read_jsonl(run_dir / "events.jsonl")
    assert any(event["event"] == "run_cancel_requested" for event in events)
    assert any(event["event"] == "run_cancelled" for event in events)
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert recorded == [["trash", "--project-path", str(repo.resolve()), "--task-id", "task-1"]]


def test_cancel_rvf_run_ignores_stale_runner_pid_without_matching_command() -> None:
    module = load_cancel_rvf_run_module()
    original_ps = module.ps_processes
    try:
        module.ps_processes = lambda: [
            (4242, "/usr/bin/python unrelated_server.py"),
            (4343, "/usr/local/bin/codex exec --output-last-message /tmp/rvf-live/final.md -"),
        ]
        candidates = module.discover_run_processes(
            "rvf-live",
            {"runner_pid": 4242},
        )
    finally:
        module.ps_processes = original_ps

    assert 4242 not in candidates
    assert candidates == {4343: "/usr/local/bin/codex exec --output-last-message /tmp/rvf-live/final.md -"}


DIFF_TRACKER = SCRIPT_DIR / "diff_tracker.py"


def load_diff_tracker_module():
    if "rvf_diff_tracker" in sys.modules:
        return sys.modules["rvf_diff_tracker"]
    spec = importlib.util.spec_from_file_location("rvf_diff_tracker", DIFF_TRACKER)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load diff_tracker module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rvf_diff_tracker"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("rvf_diff_tracker", None)
        raise
    return module


def _write_session_transcript(path: Path, repo: Path, *, session_id: str, target_path: str, line: str) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        f"*** Update File: {target_path}\n"
        "@@\n"
        "-base\n"
        f"+{line}\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2026-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(repo)},
        },
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def test_canonical_patch_hash_stable_across_reruns(tmp: Path) -> None:
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-stable",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    second = module.register_claims(
        repo=repo,
        session_id="session-stable",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.claim_ids == second.claim_ids
    assert second.dropped_stale_claim_ids == []
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"
    conn = _sqlite.connect(str(db_path))
    try:
        units = conn.execute("SELECT unit_id FROM units").fetchall()
    finally:
        conn.close()
    assert len(units) == 1


def test_canonical_patch_hash_stable_under_line_shift(tmp: Path) -> None:
    """Inserting blank lines above an unrelated hunk shifts only `@@ -A,B +C,D @@`
    line numbers; the hunk's content payload is untouched, so its
    canonical_patch_hash (== unit_id) must stay byte-identical."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    baseline = "".join(f"line-{i}\n" for i in range(1, 21))
    target = repo / "shift.txt"
    target.write_text(baseline, encoding="utf-8")
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)

    # First edit: change line-15 only — produces a single hunk near EOF.
    first_lines = baseline.splitlines(keepends=True)
    first_lines[14] = "LINE-15\n"
    target.write_text("".join(first_lines), encoding="utf-8")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-shift",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["shift.txt"],
        apply_patch_paths={"shift.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok", first.to_dict()
    assert len(first.claim_ids) == 1
    first_unit_id = first.claim_ids[0]
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"

    # Second edit: re-write file inserting 5 blank lines at top, keeping the
    # same line-15 → LINE-15 edit (which now sits at line 20). The hunk content
    # the diff emits is identical (same context lines, same +/- pair); only
    # the @@ header numbers change.
    shifted = ["\n"] * 5 + first_lines
    target.write_text("".join(shifted), encoding="utf-8")
    # Recommit baseline so HEAD also has the prefix blanks — otherwise the diff
    # would include the blank-line insertions and the hunk content would
    # legitimately differ.
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "shift baseline"], cwd=repo)
    # Restore the same edit on the new baseline.
    new_baseline = "".join(["\n"] * 5 + baseline.splitlines(keepends=True))
    target.write_text(new_baseline, encoding="utf-8")
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "restore base after shift"], cwd=repo)
    edited2 = new_baseline.splitlines(keepends=True)
    edited2[19] = "LINE-15\n"  # 14 (original index) + 5 (prefix blanks) = 19
    target.write_text("".join(edited2), encoding="utf-8")

    second = module.register_claims(
        repo=repo,
        session_id="session-shift",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["shift.txt"],
        apply_patch_paths={"shift.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok", second.to_dict()
    assert second.claim_ids == [first_unit_id], (first_unit_id, second.claim_ids)
    assert second.dropped_stale_claim_ids == []
    conn = _sqlite.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT unit_id, observed_state FROM units WHERE path='shift.txt'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, rows
    assert rows[0][0] == first_unit_id
    assert rows[0][1] == "dirty"


def test_canonical_patch_hash_changes_on_content_edit(tmp: Path) -> None:
    """Editing the hunk content (not just shifting line numbers) must produce
    a new unit_id and demote the old unit to observed_state='superseded'."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    baseline = "alpha\nbeta\ngamma\n"
    target = repo / "edit.txt"
    target.write_text(baseline, encoding="utf-8")
    run(["git", "add", "edit.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    target.write_text("alpha\nBETA-v1\ngamma\n", encoding="utf-8")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-edit",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["edit.txt"],
        apply_patch_paths={"edit.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok"
    assert len(first.claim_ids) == 1
    old_unit_id = first.claim_ids[0]
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"

    # Now genuinely change the hunk content — different replacement line.
    target.write_text("alpha\nBETA-v2\ngamma\n", encoding="utf-8")
    second = module.register_claims(
        repo=repo,
        session_id="session-edit",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["edit.txt"],
        apply_patch_paths={"edit.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok"
    assert len(second.claim_ids) == 1
    new_unit_id = second.claim_ids[0]
    assert new_unit_id != old_unit_id
    # The old session_units row must be gone (replaced by the new unit_id),
    # and dropped_stale_claim_ids surfaces the old unit_id.
    assert old_unit_id in second.dropped_stale_claim_ids
    conn = _sqlite.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT unit_id, observed_state, review_state, is_tombstoned, tombstone_reason
              FROM units
             WHERE path='edit.txt'
             ORDER BY first_observed_at
            """
        ).fetchall()
        session_units = conn.execute(
            "SELECT unit_id FROM session_units WHERE session_id='session-edit'"
        ).fetchall()
        try:
            conn.execute("UPDATE units SET review_state='tombstoned' WHERE unit_id=?", (new_unit_id,))
        except _sqlite.IntegrityError:
            rejected_review_tombstone = True
        else:
            rejected_review_tombstone = False
    finally:
        conn.close()
    by_unit = {row[0]: row[1:] for row in rows}
    assert by_unit.get(old_unit_id) == (
        "superseded",
        "available",
        1,
        "session_no_longer_owns",
    ), by_unit
    assert by_unit.get(new_unit_id) == ("dirty", "available", 0, None), by_unit
    assert rejected_review_tombstone is True
    assert {row[0] for row in session_units} == {new_unit_id}


def test_migration_phase1_json_to_sqlite_idempotent(tmp: Path) -> None:
    """Hand-write a Phase 1 state.json + events.jsonl + meta.json under the
    legacy `<log_root>/tracker/<key>/` path. First register_claims call must
    create sqlite, archive legacy files into `_legacy/`, and stamp meta.json
    with `migrated_from`. Second call must not re-import or re-archive."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    common_dir = module.git_common_dir(repo.resolve())
    assert common_dir is not None
    repo_key = module.repo_key(common_dir)
    legacy_dir = log_root / "tracker" / repo_key
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_state = {
        "schema_version": 1,
        "claims": [
            {
                "claim_id": "clm-legacy-001",
                "session_id": "session-legacy",
                "run_id": "run-legacy",
                "worktree": str(repo.resolve()),
                "branch": "main",
                "path": "tracked.txt",
                "unit": "hunk",
                "hunk_anchor": {
                    "header": "@@ -1 +1,2 @@",
                    "context_hash": "deadbeefdeadbeef",
                    "old_range": [1, 1],
                    "new_range": [1, 2],
                },
                "evidence": "apply_patch",
                "claimed_at": "2026-04-01T00:00:00Z",
                "last_seen_at": "2026-04-01T00:00:00Z",
                "lease": None,
            },
        ],
        "tombstones": [
            {
                "claim_id": "clm-tombstoned",
                "session_id": "session-legacy",
                "path": "removed.txt",
                "unit": "path",
                "dropped_at": "2026-04-01T00:00:00Z",
                "reason": "session_no_longer_owns",
            },
        ],
    }
    (legacy_dir / "state.json").write_text(
        json.dumps(legacy_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (legacy_dir / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-04-01T00:00:00Z", "event": "claim_added"}) + "\n",
        encoding="utf-8",
    )
    (legacy_dir / "meta.json").write_text(
        json.dumps({"schema_version": 1, "repo_key": repo_key}, ensure_ascii=False),
        encoding="utf-8",
    )

    first = module.register_claims(
        repo=repo,
        session_id="session-after-migration",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok", first.to_dict()
    new_dir = log_root / "diff-tracker" / "repos" / repo_key
    db_path = new_dir / "tracker.sqlite3"
    assert db_path.is_file()
    archive = legacy_dir / "_legacy"
    assert (archive / "state.json").is_file()
    assert (archive / "events.jsonl").is_file()
    assert not (legacy_dir / "state.json").exists()
    meta_payload = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta_payload.get("migrated_from") == "json-v1"
    assert meta_payload.get("schema_version") == module.SCHEMA_VERSION
    archive_state = json.loads((archive / "state.json").read_text(encoding="utf-8"))
    assert archive_state["claims"][0]["claim_id"] == "clm-legacy-001"

    conn = _sqlite.connect(str(db_path))
    try:
        units_before = conn.execute("SELECT unit_id, path FROM units ORDER BY unit_id").fetchall()
        sessions_before = conn.execute("SELECT session_id FROM sessions").fetchall()
        tombstones_before = conn.execute("SELECT ref_id FROM tombstones").fetchall()
    finally:
        conn.close()
    assert {row[0] for row in sessions_before} == {"session-legacy", "session-after-migration"}
    assert any(row[0] == "clm-tombstoned" for row in tombstones_before)
    archive_state_mtime = (archive / "state.json").stat().st_mtime

    events_first = read_jsonl(new_dir / "events.jsonl")
    migration_started_first = sum(1 for e in events_first if e.get("event") == "migration_started")
    assert migration_started_first == 1, events_first

    second = module.register_claims(
        repo=repo,
        session_id="session-after-migration",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok"
    assert (archive / "state.json").stat().st_mtime == archive_state_mtime
    conn = _sqlite.connect(str(db_path))
    try:
        units_after = conn.execute("SELECT unit_id, path FROM units ORDER BY unit_id").fetchall()
    finally:
        conn.close()
    assert units_after == units_before
    events_second = read_jsonl(new_dir / "events.jsonl")
    migration_started_second = sum(1 for e in events_second if e.get("event") == "migration_started")
    assert migration_started_second == 1, events_second


def test_migration_phase1_recovers_when_archive_predates_db_marker(tmp: Path) -> None:
    """Simulate the historical crash window: legacy state.json was already moved
    into `_legacy/` but the SQLite transaction that recorded `migrated_from`
    was rolled back (process death between archive and COMMIT). The next
    register_claims call MUST re-import claims from the archived state.json
    rather than treat the missing live state.json as "nothing to migrate"
    and silently lose every Phase-1 claim."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    common_dir = module.git_common_dir(repo.resolve())
    assert common_dir is not None
    repo_key = module.repo_key(common_dir)

    # Stage the post-crash on-disk shape directly: archive dir holds the
    # legacy state.json, but live legacy_dir/state.json is gone and the
    # SQLite db has no `migrated_from` row.
    legacy_dir = log_root / "tracker" / repo_key
    archive = legacy_dir / "_legacy"
    archive.mkdir(parents=True, exist_ok=True)
    legacy_state = {
        "schema_version": 1,
        "claims": [
            {
                "claim_id": "clm-recover-001",
                "session_id": "session-recover",
                "run_id": "run-recover",
                "worktree": str(repo.resolve()),
                "branch": "main",
                "path": "tracked.txt",
                "unit": "path",
                "evidence": "apply_patch",
                "claimed_at": "2026-04-02T00:00:00Z",
                "last_seen_at": "2026-04-02T00:00:00Z",
                "lease": None,
            },
        ],
        "tombstones": [],
    }
    (archive / "state.json").write_text(
        json.dumps(legacy_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # The archived events.jsonl is the only surviving copy after the crash
    # window: a prior _post_commit moved the live copy into _legacy/ before
    # the SQLite COMMIT rolled back. Recovery must replay these into the new
    # events.jsonl, matching the normal-migration path's side effects.
    (archive / "events.jsonl").write_text(
        json.dumps(
            {"timestamp": "2026-04-02T00:00:00Z", "event": "claim_added", "claim_id": "clm-recover-001"}
        )
        + "\n",
        encoding="utf-8",
    )
    # No live state.json — represents the "archived but not committed" gap.

    result = module.register_claims(
        repo=repo,
        session_id="session-after-crash",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert result.status == "ok", result.to_dict()

    new_dir = log_root / "diff-tracker" / "repos" / repo_key
    db_path = new_dir / "tracker.sqlite3"
    assert db_path.is_file()
    conn = _sqlite.connect(str(db_path))
    try:
        sessions = {row[0] for row in conn.execute("SELECT session_id FROM sessions").fetchall()}
        migrated_from = conn.execute(
            "SELECT value FROM meta WHERE key='migrated_from'"
        ).fetchone()
    finally:
        conn.close()
    # Both the recovered legacy session AND the new one must be present.
    assert sessions == {"session-recover", "session-after-crash"}, sessions
    assert migrated_from is not None and migrated_from[0] == "json-v1"
    # Archive remains intact; live legacy state.json must NOT have been
    # re-created (would re-trigger migration on every call).
    assert (archive / "state.json").is_file()
    assert not (legacy_dir / "state.json").exists()
    # Recovery must replay the archived phase1 events into the new events.jsonl
    # so the recovery branch is observationally aligned with the normal
    # migration path's events.jsonl side effects.
    new_events = read_jsonl(new_dir / "events.jsonl")
    replayed = [e for e in new_events if e.get("imported_from") == "phase1"]
    assert any(
        e.get("event") == "claim_added" and e.get("claim_id") == "clm-recover-001"
        for e in replayed
    ), new_events


def _slice_2b_repo_with_two_dirty(tmp: Path) -> Path:
    """init_repo + a second tracked file so two paths are dirty."""
    repo = init_repo(tmp / "repo")
    (repo / "tracked2.txt").write_text("base2\n", encoding="utf-8")
    run(["git", "add", "tracked2.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add tracked2"], cwd=repo)
    (repo / "tracked2.txt").write_text("base2\nchange2\n", encoding="utf-8")
    return repo


def _slice_2b_tracker_scope_payload(
    *,
    paths: list[str] | None = None,
    unit_ids: list[str] | None = None,
    lease_id: str = "lse-2b-test",
    scope_hash: str = "sha256:" + "b" * 64,
    hunks: object = "default",
    source_session_id: str | None = "sess-2b",
    takeover_from_session_id: str | None = None,
    extras: dict[str, object] | None = None,
) -> dict[str, object]:
    if unit_ids is None:
        unit_ids = ["a" * 64, "c" * 64]
    if paths is None:
        paths = ["tracked.txt"]
    if hunks == "default":
        hunks = [
            {"unit_id": unit_ids[0], "path": paths[0] if paths else "tracked.txt", "hunk_header": "@@ -1 +1,2 @@"}
        ]
    payload: dict[str, object] = {
        "unit_ids": unit_ids,
        "lease_id": lease_id,
        "scope_hash": scope_hash,
        "paths": paths,
        "hunks": hunks,
        "source_session_id": source_session_id,
        "takeover_from_session_id": takeover_from_session_id,
    }
    if extras:
        payload.update(extras)
    return payload


def _slice_2b_write_scope_file(tmp: Path, payload: dict[str, object]) -> Path:
    path = tmp / "tracker-scope.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _slice_2b_prepare(
    *,
    tmp: Path,
    repo: Path,
    tracker_scope_path: Path | None,
    log_root: Path,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path | None]:
    """Run prepare_review_run.py with a transcript, optionally with --tracker-scope.

    Returns (completed_process, run_dir_or_None). When tracker_scope rejection
    happens (non-zero exit), run_dir is None.
    """
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n- intent: 2-B test\n- work: edit tracked.txt\n",
        encoding="utf-8",
    )
    output_json = tmp / "run.json"
    base_dir = tmp / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(PREPARE_REVIEW_RUN),
        "--repo",
        str(repo),
        "--session-context",
        str(context),
        "--transcript",
        str(transcript),
        "--output-json",
        str(output_json),
        "--base-dir",
        str(base_dir),
    ]
    if tracker_scope_path is not None:
        cmd.extend(["--tracker-scope", str(tracker_scope_path)])
    if extra_args:
        cmd.extend(extra_args)
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    completed = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if completed.returncode != 0:
        return completed, None
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    return completed, Path(payload["artifacts_dir"])


def test_tracker_scope_payload_splices_into_manifest(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    artifact_manifest = json.loads((artifacts_dir / "session-manifest.json").read_text(encoding="utf-8"))
    input_manifest = json.loads((artifacts_dir / "inputs" / "session-manifest.json").read_text(encoding="utf-8"))
    for manifest_payload in (artifact_manifest, input_manifest):
        tracker_block = manifest_payload.get("tracker")
        assert isinstance(tracker_block, dict)
        scope = tracker_block.get("tracker_scope")
        assert isinstance(scope, dict)
        assert scope["unit_ids"] == payload["unit_ids"]
        assert scope["lease_id"] == payload["lease_id"]
        assert scope["scope_hash"] == payload["scope_hash"]
        assert scope["paths"] == payload["paths"]
        assert scope["hunks"] == payload["hunks"]
        assert scope["source_session_id"] == payload["source_session_id"]
        assert scope["takeover_from_session_id"] == payload["takeover_from_session_id"]
    # tracker_scope_file (artifact-root entry) must point to artifact_dir copy,
    # not the inputs/ duplicate; otherwise downstream consumers cannot retrieve
    # the artifact-root tracker-scope.json.
    run_payload = json.loads((tmp / "run.json").read_text(encoding="utf-8"))
    assert run_payload["tracker_scope_file"] is not None
    assert run_payload["input_tracker_scope_file"] is not None
    assert run_payload["tracker_scope_file"] != run_payload["input_tracker_scope_file"]
    assert run_payload["tracker_scope_file"] == str((artifacts_dir / "tracker-scope.json").resolve())
    assert run_payload["input_tracker_scope_file"] == str(
        (artifacts_dir / "inputs" / "tracker-scope.json").resolve()
    )


def test_tracker_scope_unlocks_scope_contract_v2_fields(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(unit_ids=["e" * 64, "d" * 64])
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 2
    assert contract["primary_units"] == sorted({"e" * 64, "d" * 64})
    assert contract["tracker_lease_id"] == payload["lease_id"]
    assert contract["tracker_scope_hash"] == payload["scope_hash"]
    canonical = contract["canonical_scope"]
    assert "primary_units" not in canonical
    assert "tracker_lease_id" not in canonical
    assert "tracker_scope_hash" not in canonical


def test_scope_contract_v2_emitted_without_tracker_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=None, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 2
    assert contract["primary_units"] is None
    assert contract["tracker_lease_id"] is None
    assert contract["tracker_scope_hash"] is None
    canonical = contract["canonical_scope"]
    assert canonical["version"] == 2
    assert set(canonical.keys()) == {
        "version",
        "repo",
        "scope_mode",
        "primary_files",
        "background_files",
        "protected_files",
        "canonical_issues",
        "fix_allowlist",
        "excluded_path_prefixes",
    }


def test_packet_emits_tracker_scope_section_when_present(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    assert "## Tracker Scope" in packet_text
    assert "## Allocated Git Diff" in packet_text
    assert "## Full Git Diff HEAD (Evidence Only)" in packet_text
    assert "## Session-Owned Git Diff" not in packet_text
    assert payload["lease_id"] in packet_text
    assert payload["scope_hash"] in packet_text


def test_packet_omits_tracker_scope_section_when_absent(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=None, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    assert "## Tracker Scope" not in packet_text
    assert "## Allocated Git Diff" not in packet_text
    assert "## Session-Owned Git Diff" in packet_text


def test_packet_metadata_carries_tracker_scope_keys(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    metadata = json.loads((artifacts_dir / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert metadata["tracker_scope_present"] is True
    assert metadata["tracker_scope_unit_count"] == len(payload["unit_ids"])
    assert metadata["tracker_scope_lease_id"] == payload["lease_id"]
    assert metadata["tracker_scope_hash"] == payload["scope_hash"]
    assert metadata["tracker_scope_paths"] == payload["paths"]
    assert metadata["tracker_scope_source_session_id"] == payload["source_session_id"]
    assert metadata["tracker_scope_takeover_from_session_id"] == payload["takeover_from_session_id"]

    completed_b, artifacts_b = _slice_2b_prepare(
        tmp=tmp / "no-scope",
        repo=init_repo(tmp / "no-scope" / "repo"),
        tracker_scope_path=None,
        log_root=tmp / "no-scope" / "logs",
    )
    assert completed_b.returncode == 0, completed_b.stderr
    assert artifacts_b is not None
    metadata_b = json.loads((artifacts_b / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert metadata_b["tracker_scope_present"] is False
    assert metadata_b["tracker_scope_unit_count"] == 0
    assert metadata_b["tracker_scope_lease_id"] is None
    assert metadata_b["tracker_scope_hash"] is None
    assert metadata_b["tracker_scope_paths"] == []
    assert metadata_b["tracker_scope_source_session_id"] is None
    assert metadata_b["tracker_scope_takeover_from_session_id"] is None


def test_tracker_scope_payload_rejects_invalid_payloads(tmp: Path) -> None:
    cases = [
        ("missing_unit_ids", {"unit_ids": None}, "tracker_scope.unit_ids"),
        ("empty_unit_ids", {"unit_ids": []}, "tracker_scope.unit_ids"),
        ("non_string_unit_id", {"unit_ids": [123, "a" * 64]}, "tracker_scope.unit_ids"),
        ("non_hex_unit_id", {"unit_ids": ["zzzz" * 16]}, "tracker_scope.unit_ids"),
        ("missing_lease", {"lease_id": ""}, "tracker_scope.lease_id"),
        ("missing_scope_hash", {"scope_hash": ""}, "tracker_scope.scope_hash"),
        ("malformed_scope_hash", {"scope_hash": "sha256:short"}, "tracker_scope.scope_hash"),
        ("non_list_paths", {"paths": "tracked.txt"}, "tracker_scope.paths"),
        ("non_string_path", {"paths": [123]}, "tracker_scope.paths"),
        ("non_list_hunks", {"hunks": "x"}, "tracker_scope.hunks"),
    ]
    log_root = tmp / "logs"
    for index, (label, override, expected_substr) in enumerate(cases):
        sub_tmp = tmp / f"case-{index}-{label}"
        sub_tmp.mkdir(parents=True, exist_ok=True)
        repo = init_repo(sub_tmp / "repo")
        payload = _slice_2b_tracker_scope_payload()
        for key, value in override.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        scope_path = _slice_2b_write_scope_file(sub_tmp, payload)
        completed, artifacts_dir = _slice_2b_prepare(
            tmp=sub_tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
        )
        assert completed.returncode != 0, f"case {label} should have failed, got: {completed.stdout}"
        assert artifacts_dir is None
        assert expected_substr in completed.stderr, (
            f"case {label}: stderr did not mention {expected_substr!r}: {completed.stderr!r}"
        )


def test_tracker_scope_requires_session_manifest_or_transcript(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    context = tmp / "context.md"
    context.write_text("## Session context\n- intent: D4 test\n", encoding="utf-8")
    base_dir = tmp / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    log_root = tmp / "logs"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--tracker-scope",
            str(scope_path),
            "--base-dir",
            str(base_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode != 0
    assert "--tracker-scope requires --session-manifest or --transcript" in completed.stderr


def test_tracker_scope_tolerates_unknown_keys(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(extras={"future_field": "preserved"})
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    manifest = json.loads((artifacts_dir / "session-manifest.json").read_text(encoding="utf-8"))
    spliced = manifest["tracker"]["tracker_scope"]
    assert spliced.get("future_field") == "preserved"


def test_review_env_exports_rvf_tracker_scope_path(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    env_text = (artifacts_dir / "review-env.sh").read_text(encoding="utf-8")
    assert "RVF_TRACKER_SCOPE" in env_text
    assert "tracker-scope.json" in env_text
    assert (artifacts_dir / "inputs" / "tracker-scope.json").exists()

    repo_b = init_repo(tmp / "no-scope" / "repo")
    log_root_b = tmp / "no-scope" / "logs"
    completed_b, artifacts_b = _slice_2b_prepare(
        tmp=tmp / "no-scope", repo=repo_b, tracker_scope_path=None, log_root=log_root_b
    )
    assert completed_b.returncode == 0, completed_b.stderr
    assert artifacts_b is not None
    env_text_b = (artifacts_b / "review-env.sh").read_text(encoding="utf-8")
    assert "RVF_TRACKER_SCOPE" not in env_text_b
    assert not (artifacts_b / "inputs" / "tracker-scope.json").exists()


def test_allocated_git_diff_uses_tracker_scope_paths(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(paths=["tracked.txt"])
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    allocated_idx = packet_text.index("## Allocated Git Diff")
    full_idx = packet_text.index("## Full Git Diff HEAD (Evidence Only)")
    assert allocated_idx < full_idx
    allocated_block = packet_text[allocated_idx:full_idx]
    full_block = packet_text[full_idx:]
    assert "diff --git a/tracked.txt b/tracked.txt" in allocated_block
    assert "diff --git a/tracked2.txt b/tracked2.txt" not in allocated_block
    assert "diff --git a/tracked.txt b/tracked.txt" in full_block
    assert "diff --git a/tracked2.txt b/tracked2.txt" in full_block


def test_existing_cross_session_conflicts_path_unchanged_with_tracker_scope(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    module.register_claims(
        repo=repo,
        session_id="other-session",
        run_id="run-other",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    metadata = json.loads((artifacts_dir / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert "## Tracker Scope" in packet_text
    assert "## Cross-Session Conflicts" in packet_text
    assert "other-session" in packet_text
    assert metadata["cross_session_conflicts"]
    assert metadata["tracker_scope_present"] is True


# ---------------------------------------------------------------------------
# Committed-round detection (RVF Stop hook auto-includes work the agent
# committed mid-round). Exercises diff_tracker committed observation/dedup,
# session_manifest committed attribution, and the round-baseline marker.
# ---------------------------------------------------------------------------

def _round_baseline_committed_modules():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import diff_tracker as _dt  # noqa: PLC0415
    import session_manifest as _sm  # noqa: PLC0415
    import round_baseline_marker as _rbm  # noqa: PLC0415

    return _dt, _sm, _rbm


def _committed_round_repo(tmp: Path) -> tuple[Path, str]:
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    baseline = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    return repo, baseline


def _committed_round_transcript(tmp: Path, repo: Path, *, session_id: str, path: str, old: str, new: str) -> Path:
    transcript = tmp / "session.jsonl"
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        f"-{old}\n"
        f"+{new}\n"
        "*** End Patch\n"
    )
    records = [
        {"timestamp": "2026-04-27T00:00:00.000Z", "type": "session_meta", "payload": {"id": session_id, "cwd": str(repo)}},
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "c1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return transcript


def _units_observed_state_by_path(tracker_dir: str, path: str) -> dict[str, str]:
    import sqlite3 as _sqlite  # noqa: PLC0415

    conn = _sqlite.connect(str(Path(tracker_dir) / "tracker.sqlite3"))
    try:
        rows = conn.execute(
            "SELECT unit_id, observed_state FROM units WHERE path=?", (path,)
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def _units_full_by_path(tracker_dir: str, path: str) -> dict[str, tuple[str, str]]:
    import sqlite3 as _sqlite  # noqa: PLC0415

    conn = _sqlite.connect(str(Path(tracker_dir) / "tracker.sqlite3"))
    try:
        rows = conn.execute(
            "SELECT unit_id, observed_state, review_state FROM units WHERE path=?", (path,)
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: (row[1], row[2]) for row in rows}


def _session_units_evidence_by_path(tracker_dir: str, path: str) -> set[str]:
    import sqlite3 as _sqlite  # noqa: PLC0415

    conn = _sqlite.connect(str(Path(tracker_dir) / "tracker.sqlite3"))
    try:
        rows = conn.execute(
            "SELECT su.evidence FROM session_units su "
            "JOIN units u ON u.unit_id = su.unit_id WHERE u.path = ?",
            (path,),
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def test_committed_unit_dedup_reviewed_not_resurrected(tmp: Path) -> None:
    """A reviewed dirty unit, once committed, must NOT re-enter the candidate
    pool: same unit_id, review_state stays 'reviewed' (§4)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    first = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert first["status"] == "allocated", first
    assert first["candidate_unit_count"] >= 1, first
    dt.complete_review_scope(
        repo=repo, lease_id=first["lease_id"], scope_hash=first["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    reviewed_before = _units_full_by_path(first["tracker_dir"], "f.txt")
    reviewed_ids = [uid for uid, (_obs, rev) in reviewed_before.items() if rev == "reviewed"]
    assert reviewed_ids, reviewed_before
    # Commit the reviewed work; re-register + re-allocate with the baseline.
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit reviewed"], cwd=repo)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r2", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths=set(), exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
        committed_paths={"f.txt"}, committed_baseline=baseline,
    )
    second = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    assert second["status"] == "empty", second
    # The reviewed units are now observed 'committed' yet stay 'reviewed'.
    after = _units_full_by_path(first["tracker_dir"], "f.txt")
    for uid in reviewed_ids:
        assert after[uid][0] == "committed", (uid, after[uid])
        assert after[uid][1] == "reviewed", (uid, after[uid])


def test_added_file_unit_identity_stable_dirty_staged_committed(tmp: Path) -> None:
    """A NEW file must mint ONE content-keyed unit_id across all three observed
    forms — untracked (dirty), staged, and committed — so a new file reviewed
    while dirty stays deduped once it becomes tracked. Before the add-identity
    unification the tracked (staged/committed) forms minted a fresh hunk-body
    unit_id and diverged from the untracked id, which is what re-dispatched
    reviewed-then-committed new files (the rename add-half over-dispatch)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    (repo / "newfile.txt").write_text("hello\nworld\n", encoding="utf-8")

    dirty_obs = dt._classify_path(repo, "newfile.txt")
    assert dirty_obs is not None and dirty_obs.kind == "untracked_file", dirty_obs
    dirty_ids = sorted(s.unit_id for s in dt._specs_from_observation(dirty_obs, "newfile.txt"))
    assert len(dirty_ids) == 1, dirty_ids

    run(["git", "add", "newfile.txt"], cwd=repo)
    staged_obs = dt._classify_path(repo, "newfile.txt")
    assert staged_obs is not None and staged_obs.kind == "tracked_hunk", staged_obs
    assert staged_obs.change_type == "add", staged_obs
    staged_ids = sorted(s.unit_id for s in dt._specs_from_observation(staged_obs, "newfile.txt"))

    run(["git", "commit", "-q", "-m", "add newfile"], cwd=repo)
    assert run(["git", "status", "--porcelain"], cwd=repo).stdout.strip() == ""
    committed_obs = dt._classify_committed_path(repo, "newfile.txt", baseline)
    assert committed_obs is not None and committed_obs.change_type == "add", committed_obs
    committed_ids = sorted(s.unit_id for s in dt._specs_from_observation(committed_obs, "newfile.txt"))

    assert dirty_ids == staged_ids == committed_ids, (dirty_ids, staged_ids, committed_ids)


def test_empty_added_file_unit_identity_stable(tmp: Path) -> None:
    """Edge of the add-identity unification (RVF reviewer cursor-cli finding):
    an EMPTY new file produces no diff hunk, so without the hunkless-add handling
    the committed/staged observation falls back to path_only and mints a unit_id
    that diverges from the untracked observation — re-dispatching a reviewed
    empty new file after commit. Assert all three forms share one unit_id."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    (repo / "empty.txt").write_text("", encoding="utf-8")

    dirty_obs = dt._classify_path(repo, "empty.txt")
    assert dirty_obs is not None and dirty_obs.kind == "untracked_file", dirty_obs
    dirty_ids = sorted(s.unit_id for s in dt._specs_from_observation(dirty_obs, "empty.txt"))

    run(["git", "add", "empty.txt"], cwd=repo)
    staged_obs = dt._classify_path(repo, "empty.txt")
    assert staged_obs is not None and staged_obs.change_type == "add", staged_obs
    staged_ids = sorted(s.unit_id for s in dt._specs_from_observation(staged_obs, "empty.txt"))

    run(["git", "commit", "-q", "-m", "add empty file"], cwd=repo)
    committed_obs = dt._classify_committed_path(repo, "empty.txt", baseline)
    assert committed_obs is not None and committed_obs.change_type == "add", committed_obs
    committed_ids = sorted(s.unit_id for s in dt._specs_from_observation(committed_obs, "empty.txt"))

    assert dirty_ids == staged_ids == committed_ids, (dirty_ids, staged_ids, committed_ids)
    assert len(dirty_ids) == 1, dirty_ids


def test_committed_added_file_dedup_reviewed_not_redispatched(tmp: Path) -> None:
    """The reported over-dispatch dual, at the allocate level: a NEW file (models
    a rename's add half / a new file authored + reviewed while dirty) is reviewed
    clean, then committed; the follow-up allocate with the round baseline must
    find NO candidate (status 'empty') — not re-dispatch it. Pre-fix the committed
    add minted a fresh unit_id, so it re-entered the candidate pool as available
    and the follow-up over-dispatched a no-op review round."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "newfile.txt").write_text("brand new content\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="newfile.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["newfile.txt"], apply_patch_paths={"newfile.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    first = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert first["status"] == "allocated", first
    assert first["candidate_unit_count"] >= 1, first
    dt.complete_review_scope(
        repo=repo, lease_id=first["lease_id"], scope_hash=first["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    reviewed_before = _units_full_by_path(first["tracker_dir"], "newfile.txt")
    reviewed_ids = [uid for uid, (_obs, rev) in reviewed_before.items() if rev == "reviewed"]
    assert reviewed_ids, reviewed_before

    run(["git", "add", "newfile.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit reviewed new file"], cwd=repo)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r2", worktree=repo, branch=None,
        owned_paths=["newfile.txt"], apply_patch_paths=set(), exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
        committed_paths={"newfile.txt"}, committed_baseline=baseline,
    )
    second = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    assert second["status"] == "empty", second
    after = _units_full_by_path(first["tracker_dir"], "newfile.txt")
    for uid in reviewed_ids:
        assert after[uid][0] == "committed", (uid, after[uid])
        assert after[uid][1] == "reviewed", (uid, after[uid])


def test_complete_review_scope_mark_reviewed_gate(tmp: Path) -> None:
    """`complete_review_scope(mark_reviewed=False)` must release the lease but NOT
    flip any unit to 'reviewed' (the no-op-completion guard); the default
    (mark_reviewed=True) still flips. Without the gate, a no-op completion
    silently marks unreviewed work reviewed → reverse missed-review."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert alloc["status"] == "allocated", alloc
    released = dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        mark_reviewed=False, log_root_override=logs,
    )
    assert released["released"] is True, released
    assert released["released_unit_count"] == 0, released
    after_noop = _units_full_by_path(alloc["tracker_dir"], "f.txt")
    assert after_noop, after_noop
    # No-op completion reverts assigned units to 'available' (re-reviewable), not 'reviewed'.
    assert all(rev == "available" for (_obs, rev) in after_noop.values()), after_noop

    # Nothing was marked reviewed, so the same work is still allocatable.
    re_alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1b", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert re_alloc["status"] == "allocated", re_alloc
    dt.complete_review_scope(
        repo=repo, lease_id=re_alloc["lease_id"], scope_hash=re_alloc["scope_hash"], run_id="r1b",
        log_root_override=logs,
    )
    after_real = _units_full_by_path(alloc["tracker_dir"], "f.txt")
    assert any(rev == "reviewed" for (_obs, rev) in after_real.values()), after_real


def test_release_tracker_lease_noop_completion_does_not_mark_reviewed(tmp: Path) -> None:
    """finalize-level wiring of the no-op guard: `_release_tracker_lease` derives
    `mark_reviewed` from reviewer-artifact presence. A run_dir with NO
    reviewers/*/review-result.json (a no-op follow-up) must release the lease with
    did_review=False and leave units unreviewed; a run_dir WITH a reviewer
    artifact marks them reviewed."""
    import importlib  # noqa: PLC0415

    dt, _sm, _rbm = _round_baseline_committed_modules()
    if "rvf_run_finalize" in sys.modules:
        fin = sys.modules["rvf_run_finalize"]
    else:
        fin = importlib.import_module("rvf_run_finalize")
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)

    def _write_contract(run_dir: Path, lease_id: str, scope_hash: str, run_id: str, units: list[str]) -> None:
        inputs = run_dir / "artifacts" / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        (inputs / "scope.contract.json").write_text(
            json.dumps({
                "tracker_lease_id": lease_id,
                "tracker_scope_hash": scope_hash,
                "run_id": run_id,
                "primary_units": units,
            }),
            encoding="utf-8",
        )

    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    try:
        dt.register_claims(
            repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
            owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
            owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
        )
        alloc1 = dt.allocate_review_scope(
            repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
            log_root_override=logs, auto_claim_observed=False,
        )
        assert alloc1["status"] == "allocated", alloc1
        unit_ids = list(_units_full_by_path(alloc1["tracker_dir"], "f.txt").keys())

        # No-op follow-up: run_dir without any reviewer artifact.
        noop_run = tmp / "run-noop"
        _write_contract(noop_run, alloc1["lease_id"], alloc1["scope_hash"], "r1", unit_ids)
        noop_result = fin._release_tracker_lease(noop_run, repo, decision_kind="handoff")
        assert noop_result is not None, "expected a release result"
        assert noop_result["did_review"] is False, noop_result
        assert noop_result["released"] is True, noop_result
        after_noop = _units_full_by_path(alloc1["tracker_dir"], "f.txt")
        # No-op finalize reverts assigned units to 'available', never 'reviewed'.
        assert all(rev == "available" for (_obs, rev) in after_noop.values()), after_noop

        # Genuine review: run_dir with a reviewer artifact present.
        alloc2 = dt.allocate_review_scope(
            repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
            log_root_override=logs, auto_claim_observed=False,
        )
        assert alloc2["status"] == "allocated", alloc2
        real_run = tmp / "run-real"
        reviewer_dir = real_run / "artifacts" / "reviewers" / "rev2"
        reviewer_dir.mkdir(parents=True, exist_ok=True)
        (reviewer_dir / "review-result.json").write_text("{}", encoding="utf-8")
        _write_contract(real_run, alloc2["lease_id"], alloc2["scope_hash"], "r2", unit_ids)
        real_result = fin._release_tracker_lease(real_run, repo, decision_kind="handoff")
        assert real_result is not None and real_result["did_review"] is True, real_result
        after_real = _units_full_by_path(alloc1["tracker_dir"], "f.txt")
        assert any(rev == "reviewed" for (_obs, rev) in after_real.values()), after_real
    finally:
        if original_log_root is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = original_log_root


def test_seal_round_baseline_to_head_advances_marker(tmp: Path) -> None:
    """rvf-land 封窗: `seal_round_baseline_to_head` advances the round-baseline
    marker from an older baseline to the current HEAD, so the next Stop's
    committed-round window (baseline..HEAD) is empty for the just-landed commit."""
    dt, _sm, rbm = _round_baseline_committed_modules()
    import seal_round_baseline_to_head as seal  # noqa: PLC0415
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_task = os.environ.get("KANBAN_TASK_ID")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    os.environ["KANBAN_TASK_ID"] = "seal-task-1"
    try:
        # Marker initially pinned at the pre-work baseline.
        rbm.write_round_baseline_marker(
            task_id="seal-task-1", session_id=None, baseline_head=baseline, repo=str(repo),
        )
        (repo / "f.txt").write_text("base\nlanded\n", encoding="utf-8")
        run(["git", "add", "f.txt"], cwd=repo)
        run(["git", "commit", "-q", "-m", "land reviewed work"], cwd=repo)
        new_head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        assert new_head != baseline

        result = seal.seal_round_baseline_to_head(str(repo))
        assert result["sealed"] is True, result
        assert result["baseline_head"] == new_head, result
        sealed_head = rbm.resolve_round_baseline_head(task_id="seal-task-1", session_id=None)
        assert sealed_head == new_head, (sealed_head, new_head)
        # Window is now empty for the landed commit.
        assert dt._list_committed_round_changed_paths(repo, sealed_head) == []
    finally:
        for name, val in (("CODEX_RVF_LOG_ROOT", original_log_root), ("KANBAN_TASK_ID", original_task)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val


def test_review_highwater_marker_round_trip(tmp: Path) -> None:
    """review_highwater_marker: task-first/session-fallback keying, overwrite
    advances, missing/empty/no-key → None, clear removes. Mirrors
    round_baseline_marker's IO contract so finalize/seal writes land exactly where
    committed-round reads (same (task,session) under the same log_root())."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import review_highwater_marker as hwm  # noqa: PLC0415
    root = tmp / "state"
    head_a = "a" * 40
    head_b = "b" * 40
    hwm.write_review_highwater(task_id="t1", session_id="s1", reviewed_head=head_a, repo="/r", root=root)
    assert hwm.resolve_review_highwater_head(task_id="t1", session_id=None, root=root) == head_a
    # Overwrite advances (high-water only moves forward by caller contract).
    hwm.write_review_highwater(task_id="t1", session_id="s1", reviewed_head=head_b, repo="/r", root=root)
    assert hwm.resolve_review_highwater_head(task_id="t1", session_id=None, root=root) == head_b
    # Session-only keying when no task_id.
    hwm.write_review_highwater(task_id=None, session_id="s9", reviewed_head=head_a, repo="/r", root=root)
    assert hwm.resolve_review_highwater_head(task_id=None, session_id="s9", root=root) == head_a
    # Missing / empty head / no key → None (no write).
    assert hwm.resolve_review_highwater_head(task_id="nope", session_id=None, root=root) is None
    assert hwm.write_review_highwater(task_id=None, session_id=None, reviewed_head=head_a, repo=None, root=root) is None
    assert hwm.write_review_highwater(task_id="t2", session_id=None, reviewed_head="  ", repo=None, root=root) is None
    removed = hwm.clear_review_highwater(task_id="t1", session_id="s1", root=root)
    assert removed, removed
    assert hwm.resolve_review_highwater_head(task_id="t1", session_id=None, root=root) is None


def test_seal_advances_review_highwater(tmp: Path) -> None:
    """rvf-land 封窗 must advance BOTH round-baseline AND the last-reviewed high-water
    to HEAD. committed-round prefers the high-water; if seal advanced only
    round-baseline, the next Stop would re-window the just-landed commit
    (over-dispatch) because the high-water still pointed at the prior review's HEAD."""
    _dt, _sm, _rbm = _round_baseline_committed_modules()
    import seal_round_baseline_to_head as seal  # noqa: PLC0415
    import review_highwater_marker as hwm  # noqa: PLC0415
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_task = os.environ.get("KANBAN_TASK_ID")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    os.environ["KANBAN_TASK_ID"] = "seal-hw-task"
    try:
        (repo / "f.txt").write_text("base\nlanded\n", encoding="utf-8")
        run(["git", "add", "f.txt"], cwd=repo)
        run(["git", "commit", "-q", "-m", "land reviewed work"], cwd=repo)
        new_head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        result = seal.seal_round_baseline_to_head(str(repo))
        assert result["sealed"] is True, result
        assert result.get("highwater_marker_path"), result
        assert hwm.resolve_review_highwater_head(task_id="seal-hw-task", session_id=None) == new_head
    finally:
        for name, val in (("CODEX_RVF_LOG_ROOT", original_log_root), ("KANBAN_TASK_ID", original_task)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val


def test_advance_review_highwater_only_on_did_review(tmp: Path) -> None:
    """finalize 推进高水位的语义：``did_review=True`` → 写高水位到被审 HEAD；
    ``did_review=False``（纯 no-op 完成）→ 不写。偏向「重审」安全方向，杜绝把未审已提交
    工作静默标已审。键走 task（cline-kanban 主路径）。"""
    import importlib  # noqa: PLC0415

    if "rvf_run_finalize" in sys.modules:
        fin = sys.modules["rvf_run_finalize"]
    else:
        fin = importlib.import_module("rvf_run_finalize")
    import review_highwater_marker as hwm  # noqa: PLC0415
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_task = os.environ.get("KANBAN_TASK_ID")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    os.environ["KANBAN_TASK_ID"] = "hw-fin-task"
    try:
        run_dir = tmp / "run"
        # No-op completion (no reviewer artifacts) → high-water NOT advanced.
        rec_noop = {
            "tracker_lease_release": {"did_review": False},
            "workspace_diff": {"head_after": head},
        }
        out_noop = fin._advance_review_highwater(run_dir, repo, {}, {}, rec_noop)
        assert out_noop["status"] == "skipped", out_noop
        assert hwm.resolve_review_highwater_head(task_id="hw-fin-task", session_id=None) is None

        # Genuine review completion → high-water advanced to reviewed HEAD.
        rec_real = {
            "tracker_lease_release": {"did_review": True},
            "workspace_diff": {"head_after": head},
        }
        out_real = fin._advance_review_highwater(run_dir, repo, {}, {}, rec_real)
        assert out_real["status"] == "advanced", out_real
        assert out_real["reviewed_head"] == head, out_real
        assert hwm.resolve_review_highwater_head(task_id="hw-fin-task", session_id=None) == head
    finally:
        for name, val in (("CODEX_RVF_LOG_ROOT", original_log_root), ("KANBAN_TASK_ID", original_task)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val


def test_review_highwater_write_is_monotonic_with_repo(tmp: Path) -> None:
    """高水位只前移不回退（实际强制，非仅注释）：传入 repo 时，把高水位写成现值的严格祖先
    会被拒绝（保留更高水位）；前移正常推进；repo=None 退化盲写。修 task-keyed 跨 worktree
    落后上下文后完成 review 盲覆盖回退高水位 → committed-round 窗口偏宽重派已审工作 的缝
    （对抗式 review 发现，over-dispatch-regression lens）。"""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import review_highwater_marker as hwm  # noqa: PLC0415
    repo, _baseline = _committed_round_repo(tmp)
    root = tmp / "state"
    (repo / "f.txt").write_text("base\nc1\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo); run(["git", "commit", "-q", "-m", "c1"], cwd=repo)
    c1 = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    (repo / "f.txt").write_text("base\nc1\nc2\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo); run(["git", "commit", "-q", "-m", "c2"], cwd=repo)
    c2 = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    # Advance to c2 (ahead).
    hwm.write_review_highwater(task_id="mono", session_id=None, reviewed_head=c2, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, root=root) == c2
    # Regress to c1 (strict ancestor of c2) WITH repo → refused, stays c2.
    hwm.write_review_highwater(task_id="mono", session_id=None, reviewed_head=c1, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, root=root) == c2, "regress must be refused"
    # Forward to c3 (descendant) → advances.
    (repo / "f.txt").write_text("base\nc1\nc2\nc3\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo); run(["git", "commit", "-q", "-m", "c3"], cwd=repo)
    c3 = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    hwm.write_review_highwater(task_id="mono", session_id=None, reviewed_head=c3, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, root=root) == c3
    # repo=None → degrade to blind write (best-effort), regress allowed.
    hwm.write_review_highwater(task_id="mono", session_id=None, reviewed_head=c1, repo=None, root=root)
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, root=root) == c1


def test_committed_observation_excludes_base_branch_sync_merge(tmp: Path) -> None:
    """Files brought in only via a base-branch-sync merge (second parent) are
    excluded from committed-round paths; first-parent agent work is kept (§3)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    # main advances with a base-only file.
    run(["git", "checkout", "-q", "main"], cwd=repo)
    (repo / "base_only.txt").write_text("from base\n", encoding="utf-8")
    run(["git", "add", "base_only.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base advance"], cwd=repo)
    # feature: agent's own first-parent commit, then merge main in.
    run(["git", "checkout", "-q", "feature"], cwd=repo)
    (repo / "feature.txt").write_text("agent work\n", encoding="utf-8")
    run(["git", "add", "feature.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "feature work"], cwd=repo)
    run(["git", "merge", "-q", "--no-edit", "main"], cwd=repo)
    paths = dt._list_committed_round_changed_paths(repo, baseline)
    assert "feature.txt" in paths, paths
    assert "base_only.txt" not in paths, paths


def test_build_manifest_includes_committed_round_owned_paths(tmp: Path) -> None:
    """An apply_patch-attributed file committed clean within the round still
    lands in owned_committed_round_paths and registers tracker ownership (§5)."""
    dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    transcript = _committed_round_transcript(tmp, repo, session_id="s1", path="f.txt", old="base", new="changed")
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "agent commit"], cwd=repo)
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=baseline)
    assert manifest["owned_committed_round_paths"] == ["f.txt"], manifest["owned_committed_round_paths"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["tracker"]["status"] == "ok"
    # Zero-diff guarantee: no baseline => no committed scope.
    manifest_none = sm.build_manifest(repo, transcript, tracker_run_id="r2", tracker_log_root=tmp / "logs2", committed_baseline=None)
    assert manifest_none["owned_committed_round_paths"] == []


def test_build_manifest_attributes_committed_round_without_transcript_evidence(tmp: Path) -> None:
    """Second-gate fix: a path committed in-round with NO parent-session
    transcript tool evidence (sub-agent / headless runner / Kanban commit) is
    still attributed (evidence='committed_round_git') and registers session_units,
    so `_collect_candidate_unit_ids_in_txn` (EXISTS session_units) can see it.
    Reproduces the `no_session_owned_dirty` leak that survived 218c5ed."""
    dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    # Transcript with session_meta only — no apply_patch / write / exec record,
    # i.e. the parent session never touched f.txt via a tool.
    transcript = tmp / "session.jsonl"
    records = [
        {"timestamp": "2026-06-24T00:00:00.000Z", "type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    # A "sub-agent" commits work to f.txt directly; HEAD is clean.
    (repo / "f.txt").write_text("base\nsubagent\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "subagent work"], cwd=repo)
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=baseline)
    assert manifest["owned_committed_round_paths"] == ["f.txt"], manifest["owned_committed_round_paths"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["tracker"]["status"] == "ok", manifest["tracker"]
    evidence = _session_units_evidence_by_path(manifest["tracker"]["tracker_dir"], "f.txt")
    assert evidence == {"committed_round_git"}, evidence


def test_committed_round_changed_paths_excludes_skip_review_trailer(tmp: Path) -> None:
    """Per-commit opt-out: a round commit carrying the `RVF-Skip-Review` trailer
    drops its EXCLUSIVE paths from the committed-round set, while a path also
    touched by a normal round commit stays (it remains in that commit's
    name-only block), and normal-commit paths are kept."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    # Normal round commit: normal.txt (exclusive) + shared.txt.
    (repo / "normal.txt").write_text("normal\n", encoding="utf-8")
    (repo / "shared.txt").write_text("v1\n", encoding="utf-8")
    run(["git", "add", "normal.txt", "shared.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "normal round work"], cwd=repo)
    # Opt-out round commit: skip_only.txt (exclusive) + shared.txt again.
    (repo / "skip_only.txt").write_text("skip\n", encoding="utf-8")
    (repo / "shared.txt").write_text("v2\n", encoding="utf-8")
    run(["git", "add", "skip_only.txt", "shared.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "headless cleanup", "-m", "RVF-Skip-Review: noise commit"], cwd=repo)
    skip_shas = dt._list_round_skip_review_commit_shas(repo, baseline)
    assert len(skip_shas) == 1, skip_shas
    paths = dt._list_committed_round_changed_paths(repo, baseline)
    assert "normal.txt" in paths, paths
    assert "shared.txt" in paths, paths  # kept: also in the non-skip commit's block
    assert "skip_only.txt" not in paths, paths  # excluded: only the skip commit touched it
    # Bare-token form (no colon / value) also opts a commit out.
    (repo / "bare_skip.txt").write_text("bare\n", encoding="utf-8")
    run(["git", "add", "bare_skip.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "bare opt out", "-m", "RVF-Skip-Review"], cwd=repo)
    assert len(dt._list_round_skip_review_commit_shas(repo, baseline)) == 2
    assert "bare_skip.txt" not in dt._list_committed_round_changed_paths(repo, baseline)


def test_committed_then_dirty_same_path_resolves_to_dirty(tmp: Path) -> None:
    """A path both committed in-round and further dirtied: the live worktree
    change is observed 'dirty' (committed walk runs first, dirty walk wins; §5)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nX\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "committed X"], cwd=repo)
    (repo / "f.txt").write_text("base\nX\nY\n", encoding="utf-8")  # further dirty edit
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    result = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    states = set(_units_observed_state_by_path(result["tracker_dir"], "f.txt").values())
    assert "dirty" in states, states  # the live worktree edit is captured


def test_committed_unit_gone_after_reset_is_superseded(tmp: Path) -> None:
    """A committed unit that drops out of baseline..HEAD (commit reset away) is
    swept to 'superseded' by the unified supersession chokepoint."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "work"], cwd=repo)
    dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    tracker_dir = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1b", reviewer_id="rev1b",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
        dry_run=True,
    )["tracker_dir"]
    states_before = _units_observed_state_by_path(tracker_dir, "f.txt")
    committed_ids = [uid for uid, st in states_before.items() if st == "committed"]
    assert committed_ids, states_before
    # Drop the commit; the committed unit leaves baseline..HEAD.
    run(["git", "reset", "-q", "--hard", baseline], cwd=repo)
    dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    states_after = _units_observed_state_by_path(tracker_dir, "f.txt")
    assert all(states_after.get(uid) == "superseded" for uid in committed_ids), states_after


def test_round_baseline_marker_round_trip(tmp: Path) -> None:
    """write/read/dual-key/overwrite/status for the round-baseline marker."""
    _dt, _sm, rbm = _round_baseline_committed_modules()
    root = tmp / "state"
    head_a = "a" * 40
    head_b = "b" * 40
    # task-keyed write, then overwrite (multi-prompt semantics: last wins).
    rbm.write_round_baseline_marker(task_id="t1", session_id="s1", baseline_head=head_a, repo="/r", root=root)
    assert rbm.read_round_baseline_marker(task_id="t1", session_id=None, root=root)["baseline_head"] == head_a
    rbm.write_round_baseline_marker(task_id="t1", session_id="s1", baseline_head=head_b, repo="/r", root=root)
    marker = rbm.read_round_baseline_marker(task_id="t1", session_id=None, root=root)
    assert marker["baseline_head"] == head_b
    # session fallback when no task id.
    rbm.write_round_baseline_marker(task_id=None, session_id="s9", baseline_head=head_a, repo="/r", root=root)
    assert rbm.read_round_baseline_marker(task_id=None, session_id="s9", root=root)["baseline_head"] == head_a
    # status + resolve helper.
    assert rbm.round_baseline_status(marker) == rbm.STATUS_ACTIVE
    assert rbm.resolve_round_baseline_head(task_id="t1", session_id=None, root=root) == head_b
    assert rbm.round_baseline_status(marker, now_ts=4102444800.0) == rbm.STATUS_STALE  # year 2100
    assert rbm.round_baseline_status({"baseline_head": ""}) == rbm.STATUS_INVALID
    assert rbm.round_baseline_status(None) == rbm.STATUS_INVALID
    # No keys / empty head => no write.
    assert rbm.write_round_baseline_marker(task_id=None, session_id=None, baseline_head=head_a, repo=None, root=root) is None
    assert rbm.write_round_baseline_marker(task_id="t2", session_id=None, baseline_head="", repo=None, root=root) is None


def test_allocate_review_scope_includes_committed_round(tmp: Path) -> None:
    """End-to-end: round-baseline marker present + in-round commit ⇒ the
    committed unit reaches allocator scope through resolve→build_manifest→allocate."""
    dt, sm, rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    state_root = tmp / "state"
    transcript = _committed_round_transcript(tmp, repo, session_id="s1", path="f.txt", old="base", new="changed")
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "agent commit"], cwd=repo)
    # Marker written at the prior prompt (baseline = pre-commit HEAD).
    rbm.write_round_baseline_marker(task_id=None, session_id="s1", baseline_head=baseline, repo=str(repo), root=state_root)
    resolved = rbm.resolve_round_baseline_head(task_id=None, session_id="s1", root=state_root)
    assert resolved == baseline
    sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=resolved)
    result = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=resolved,
    )
    assert result["status"] == "allocated", result
    assert result["candidate_unit_count"] >= 1, result


def load_dispatch_reviewers_module():
    # dispatch_reviewers imports rvf_logging / run_alternative_reviewer / trajectory_distill
    # from SCRIPT_DIR, so SCRIPT_DIR must be importable.
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "dispatch_reviewers", SCRIPT_DIR / "dispatch_reviewers.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load dispatch_reviewers module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_harness_limit_cooldown_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "harness_limit_cooldown", SCRIPT_DIR / "harness_limit_cooldown.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load harness_limit_cooldown module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _usage_limit_text_reviewer_config(path: Path, *, stderr_text: str) -> Path:
    """文本格式 reviewer 配置：shim 往 stderr 吐额度签名并非零退出、不写 review-result。"""
    return write_alternative_reviewer_config(
        path,
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys; sys.stdin.read(); "
                f"sys.stderr.write({stderr_text!r}); sys.stderr.flush(); sys.exit(1)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="text",
    )


def _clean_text_reviewer_config(path: Path, *, label: str) -> Path:
    """文本格式 reviewer 配置：shim 写合法 no-issues review-result 并 rc0（reroute 备援腿用）。"""
    cfg = write_alternative_reviewer_config(
        path,
        [
            sys.executable,
            "-u",
            "-c",
            clean_review_result_python(stdout="ok"),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    payload["label"] = label
    cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg


def _dispatch_registry() -> dict:
    """In-memory registry mirroring config/reviewer-registry.json (real harness ids)."""
    return {
        "schema_version": 1,
        "harnesses": {
            "cursor": {
                "harness_id": "cursor",
                "label_prefix": "alternative-reviewer:cursor-cli",
                "config_path": "config/alternative-reviewer.cursor.json",
                "dispatch_mode": "external_cli",
                "enabled": True,
                "priority_default": 100,
            },
            "claude_code": {
                "harness_id": "claude_code",
                "label_prefix": "alternative-reviewer:claude-code",
                "config_path": "config/alternative-reviewer.claude.json",
                "dispatch_mode": "external_cli",
                "enabled": True,
                "priority_default": 50,
            },
            "codex": {
                "harness_id": "codex",
                "label_prefix": "alternative-reviewer:codex-cli",
                "config_path": "config/alternative-reviewer.codex.json",
                "dispatch_mode": "external_cli",
                "enabled": True,
                "priority_default": 50,
            },
        },
    }


def load_rvf_detached_thread_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "rvf_detached_thread", SCRIPT_DIR / "rvf_detached_thread.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_detached_thread module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_realexec_tmux_script(path: Path) -> Path:
    """fake tmux：同步执行 wrapper shell（区别于 write_fake_tmux_script 只记录调用）。

    真实 ``tmux new-session -d`` detach 所有 fd 后台运行；测试为确定性改为同步跑完
    wrapper（被包命令 + ``--finalize-status`` 回调），故 launch_detached 返回时
    status.json 已落终态，便于断言两阶段写入。
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "shell = sys.argv[-1]\n"  # tmux new-session -d -s <name> <shell>
        "raise SystemExit(subprocess.run(['/bin/sh', '-c', shell]).returncode)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _detached_status_payload() -> dict:
    return {
        "schema_version": 1,
        "started_at": "t0",
        "returncode": None,
        "finished_at": None,
        "launch_status": "launched",
        "error": None,
    }


def _seed_detached_stale_lock(root: Path, *, returncode: object) -> tuple[Path, Path]:
    """预置一个「持锁线程已退出」的现场：写锁文件 + 带指定 returncode 的 status.json。

    ``returncode=None`` 表示未干净完成（launched 后没回写）；``returncode=0`` 表示干净完成。
    """
    lock_path = root / "s.lock"
    status_path = root / "s.status.json"
    lock_path.write_text("rvf-detached-unit\nt0\n", encoding="utf-8")
    payload: dict[str, object] = {"launch_status": "launched", "finished_at": None}
    if returncode is not None:
        payload["returncode"] = returncode
        payload["finished_at"] = "t1"
    else:
        payload["returncode"] = None
    status_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return lock_path, status_path


def _launch_detached_with_staleness_env(
    module,
    root: Path,
    *,
    lock_path: Path,
    status_path: Path,
    has_session_rc: str,
    new_session_rc: str = "0",
) -> tuple[dict, list[list]]:
    """用 subcommand-aware fake tmux 跑一次 launch_detached，返回 (result, 记录的 tmux argv)。"""
    fake_tmux = write_subcommand_aware_tmux_script(root / "tmux.py")
    calls = root / "calls.jsonl"
    saved = {
        k: os.environ.get(k)
        for k in (
            "CODEX_RVF_TMUX_BIN",
            "FAKE_TMUX_CALLS",
            "FAKE_TMUX_RETURNCODE",
            "FAKE_TMUX_HAS_SESSION_RETURNCODE",
        )
    }
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = new_session_rc
    os.environ["FAKE_TMUX_HAS_SESSION_RETURNCODE"] = has_session_rc
    try:
        result = module.launch_detached(
            session_name="rvf-detached-unit",
            argv=["echo", "hi"],
            log_path=root / "s.log",
            status_path=status_path,
            lock_path=lock_path,
            status_payload=_detached_status_payload(),
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    recorded = (
        [json.loads(line)["argv"] for line in calls.read_text(encoding="utf-8").splitlines()]
        if calls.exists()
        else []
    )
    return result, recorded


def _run_alternative_reviewer_summary(run_dir: Path, reviewer_id: str = "test") -> dict:
    reviewer_dir = run_dir / "artifacts" / "reviewers" / reviewer_id
    return json.loads(
        next(reviewer_dir.glob("reviewer.summary*.json")).read_text(encoding="utf-8")
    )


def test_run_alternative_reviewer_usage_limit_codex_json_error(tmp_path: Path) -> None:
    """codex_json 吐 turn.failed + 真实额度文案 → 退出码 125 + summary usage_limit_exhausted。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, sys; sys.stdin.read(); "
                "print(json.dumps({'type':'turn.failed','error':{'message':"
                "'You have hit your usage limit. Please try again in 4h.'}}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 125, (completed.returncode, completed.stderr)
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" in completed.stderr, completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary["output_error_reason"] == "usage_limit_exhausted", summary
    assert "usage limit" in (summary.get("output_error_message") or "").lower(), summary


def test_run_alternative_reviewer_usage_limit_stderr_text(tmp_path: Path) -> None:
    """text reviewer：stderr 吐额度签名 + 非零退出 + 无 review-result → 退出码 125。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = _usage_limit_text_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        stderr_text="Error: rate limit exceeded — HTTP 429 Too Many Requests\n",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 125, (completed.returncode, completed.stderr)
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" in completed.stderr, completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary["output_error_reason"] == "usage_limit_exhausted", summary


def test_run_alternative_reviewer_usage_limit_no_false_positive_success(tmp_path: Path) -> None:
    """成功评审正文含 'rate limit' → 退出码 0、非额度（rc0 路径不扫正文）。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            clean_review_result_python(
                stdout="review complete: consider adding rate limit handling for HTTP 429"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" not in (completed.stderr or ""), completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary.get("output_error_reason") is None, summary


def test_run_alternative_reviewer_usage_limit_no_false_positive_invalid_result(tmp_path: Path) -> None:
    """rc0 子进程 + 无合法 review-result + 正文含 429/rate limit → reviewer_result_invalid，非 usage_limit、非 125（D2 回归）。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, sys; sys.stdin.read(); "
                "open(os.environ['RVF_REVIEW_RESULT'], 'w', encoding='utf-8')"
                ".write('not-valid-json — discussing http 429 rate limit'); "
                "print('model body mentions rate limit and 429')"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, (completed.returncode, completed.stderr)
    assert completed.returncode != 125
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" not in (completed.stderr or ""), completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary.get("output_error_reason") is None, summary


def test_harness_limit_cooldown_record_active_sweep(tmp_path: Path) -> None:
    """record→active True；过期→lazy sweep 后 False；默认 TTL 3600；parse_reset_hint '4h'→14400。"""
    from datetime import datetime, timedelta, timezone

    cd = load_harness_limit_cooldown_module()
    root = tmp_path / "cooldown"
    assert cd.default_ttl_seconds() == 3600.0
    assert cd.parse_reset_hint("please try again in 4h") == 14400.0
    assert cd.parse_reset_hint("retry in 30 minutes") == 1800.0
    assert cd.parse_reset_hint("Retry-After: 120") == 120.0
    assert cd.parse_reset_hint("no reset hint here") is None

    cd.record("codex", reason="usage_limit_exhausted", error_message="hit usage limit", root=root)
    assert cd.active("codex", root=root) is True
    assert cd.active("claude_code", root=root) is False
    assert set(cd.active_harnesses(root=root)) == {"codex"}

    # 写一条过期 marker → active() 的 lazy sweep 应清掉它。
    past = (
        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    marker_path = cd._marker_path("codex", root)
    marker_path.write_text(
        json.dumps(
            {"marker_version": 1, "harness_id": "codex", "recorded_at": past, "expires_at": past}
        ),
        encoding="utf-8",
    )
    assert cd.active("codex", root=root) is False
    assert not marker_path.exists()


def _make_usage_or_clean_registry(tmp_path: Path, specs: dict[str, tuple[str, int]]) -> dict:
    """specs: {harness_id: (kind, priority)}，kind ∈ {'usage','clean'}。返回内存 registry。"""
    reg: dict = {"schema_version": 1, "harnesses": {}}
    for hid, (kind, prio) in specs.items():
        cfg_path = tmp_path / f"alt-{hid}.json"
        if kind == "usage":
            _usage_limit_text_reviewer_config(
                cfg_path, stderr_text=f"fatal: usage limit reached for {hid} plan\n"
            )
        else:
            _clean_text_reviewer_config(cfg_path, label=f"alternative-reviewer:{hid}-cli")
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(cfg_path),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": prio,
        }
    return reg


def _with_cooldown_env(cooldown_root: Path):
    """上下文：把 RVF_HARNESS_LIMIT_COOLDOWN_ROOT 指到 tmp，退出时还原（防污染真实 ~/.rvf）。"""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        key = "RVF_HARNESS_LIMIT_COOLDOWN_ROOT"
        prev = os.environ.get(key)
        os.environ[key] = str(cooldown_root)
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    return _ctx()


def review_support_test_cases(root: Path) -> list[tuple[str, object]]:
    return [
        (
            "dispatch_reviewers_routing_matrix",
            lambda: test_dispatch_reviewers_routing_matrix(root / "dispatch-routing"),
        ),
        (
            "dispatch_reviewers_same_harness_double_instance_distinct_ids",
            lambda: test_dispatch_reviewers_same_harness_double_instance_distinct_ids(
                root / "dispatch-double-instance"
            ),
        ),
        (
            "dispatch_reviewers_plan_artifact_schema",
            lambda: test_dispatch_reviewers_plan_artifact_schema(root / "dispatch-plan-schema"),
        ),
        (
            "dispatch_reviewers_executes_two_external",
            lambda: test_dispatch_reviewers_executes_two_external(root / "dispatch-execute"),
        ),
        (
            "dispatch_reviewers_execute_backfills_review_env",
            lambda: test_dispatch_reviewers_execute_backfills_review_env(root / "dispatch-env-backfill"),
        ),
        (
            "run_alternative_reviewer_usage_limit_codex_json_error",
            lambda: test_run_alternative_reviewer_usage_limit_codex_json_error(
                root / "usage-limit-codex-json"
            ),
        ),
        (
            "run_alternative_reviewer_usage_limit_stderr_text",
            lambda: test_run_alternative_reviewer_usage_limit_stderr_text(
                root / "usage-limit-stderr-text"
            ),
        ),
        (
            "run_alternative_reviewer_usage_limit_no_false_positive_success",
            lambda: test_run_alternative_reviewer_usage_limit_no_false_positive_success(
                root / "usage-limit-fp-success"
            ),
        ),
        (
            "run_alternative_reviewer_usage_limit_no_false_positive_invalid_result",
            lambda: test_run_alternative_reviewer_usage_limit_no_false_positive_invalid_result(
                root / "usage-limit-fp-invalid"
            ),
        ),
        (
            "harness_limit_cooldown_record_active_sweep",
            lambda: test_harness_limit_cooldown_record_active_sweep(root / "cooldown-unit"),
        ),
        (
            "dispatch_reviewers_reroutes_on_usage_limit",
            lambda: test_dispatch_reviewers_reroutes_on_usage_limit(root / "dispatch-reroute"),
        ),
        (
            "dispatch_reviewers_reroute_id_collision",
            lambda: test_dispatch_reviewers_reroute_id_collision(root / "dispatch-reroute-collision"),
        ),
        (
            "dispatch_reviewers_probe_excludes_cooldown",
            lambda: test_dispatch_reviewers_probe_excludes_cooldown(root / "dispatch-probe-cooldown"),
        ),
        (
            "dispatch_reviewers_failclose_when_main_exhausted",
            lambda: test_dispatch_reviewers_failclose_when_main_exhausted(
                root / "dispatch-failclose-main"
            ),
        ),
        (
            "diff_tracker_observes_committed_round_units",
            lambda: test_diff_tracker_observes_committed_round_units(root / "committed-observe"),
        ),
        (
            "committed_unit_dedup_reviewed_not_resurrected",
            lambda: test_committed_unit_dedup_reviewed_not_resurrected(root / "committed-dedup"),
        ),
        (
            "added_file_unit_identity_stable_dirty_staged_committed",
            lambda: test_added_file_unit_identity_stable_dirty_staged_committed(root / "added-identity"),
        ),
        (
            "empty_added_file_unit_identity_stable",
            lambda: test_empty_added_file_unit_identity_stable(root / "empty-added-identity"),
        ),
        (
            "committed_added_file_dedup_reviewed_not_redispatched",
            lambda: test_committed_added_file_dedup_reviewed_not_redispatched(root / "added-dedup"),
        ),
        (
            "complete_review_scope_mark_reviewed_gate",
            lambda: test_complete_review_scope_mark_reviewed_gate(root / "mark-reviewed-gate"),
        ),
        (
            "release_tracker_lease_noop_completion_does_not_mark_reviewed",
            lambda: test_release_tracker_lease_noop_completion_does_not_mark_reviewed(root / "noop-finalize"),
        ),
        (
            "seal_round_baseline_to_head_advances_marker",
            lambda: test_seal_round_baseline_to_head_advances_marker(root / "seal-marker"),
        ),
        (
            "review_highwater_marker_round_trip",
            lambda: test_review_highwater_marker_round_trip(root / "highwater-marker"),
        ),
        (
            "seal_advances_review_highwater",
            lambda: test_seal_advances_review_highwater(root / "seal-highwater"),
        ),
        (
            "advance_review_highwater_only_on_did_review",
            lambda: test_advance_review_highwater_only_on_did_review(root / "finalize-highwater"),
        ),
        (
            "review_highwater_write_is_monotonic_with_repo",
            lambda: test_review_highwater_write_is_monotonic_with_repo(root / "highwater-monotonic"),
        ),
        (
            "committed_observation_excludes_base_branch_sync_merge",
            lambda: test_committed_observation_excludes_base_branch_sync_merge(root / "committed-merge"),
        ),
        (
            "build_manifest_includes_committed_round_owned_paths",
            lambda: test_build_manifest_includes_committed_round_owned_paths(root / "committed-manifest"),
        ),
        (
            "build_manifest_attributes_committed_round_without_transcript_evidence",
            lambda: test_build_manifest_attributes_committed_round_without_transcript_evidence(
                root / "committed-no-evidence"
            ),
        ),
        (
            "committed_round_changed_paths_excludes_skip_review_trailer",
            lambda: test_committed_round_changed_paths_excludes_skip_review_trailer(
                root / "committed-skip-trailer"
            ),
        ),
        (
            "committed_then_dirty_same_path_resolves_to_dirty",
            lambda: test_committed_then_dirty_same_path_resolves_to_dirty(root / "committed-then-dirty"),
        ),
        (
            "committed_unit_gone_after_reset_is_superseded",
            lambda: test_committed_unit_gone_after_reset_is_superseded(root / "committed-superseded"),
        ),
        (
            "round_baseline_marker_round_trip",
            lambda: test_round_baseline_marker_round_trip(root / "round-baseline-marker"),
        ),
        (
            "rvf_user_prompt_submit_captures_round_baseline",
            lambda: test_rvf_user_prompt_submit_captures_round_baseline(root / "round-baseline-capture"),
        ),
        (
            "allocate_review_scope_includes_committed_round",
            lambda: test_allocate_review_scope_includes_committed_round(root / "committed-allocate-e2e"),
        ),
        (
            "rvf_handoff_cli_notify",
            lambda: test_rvf_handoff_cli_notify(root / "handoff-notify"),
        ),
        (
            "rvf_handoff_marker_tail_and_summary",
            lambda: test_rvf_handoff_marker_tail_and_summary(),
        ),
        (
            "rvf_handoff_resolve_kanban_task_url",
            lambda: test_rvf_handoff_resolve_kanban_task_url(root / "handoff-url"),
        ),
        (
            "rvf_handoff_notify_requires_terminal_notifier",
            lambda: test_rvf_handoff_notify_requires_terminal_notifier(root / "handoff-missing"),
        ),
        (
            "rvf_handoff_maybe_trigger_kanban_notification",
            lambda: test_rvf_handoff_maybe_trigger_kanban_notification(root / "handoff-kanban-trigger"),
        ),
        ("rvf_prep_file_round_trip_and_sweep", lambda: test_rvf_prep_file_round_trip_and_sweep(root / "prep-file")),
        (
            "rvf_prep_file_revive_expired_restamps_ttl",
            lambda: test_rvf_prep_file_revive_expired_restamps_ttl(root / "prep-file-revive"),
        ),
        (
            "rvf_user_prompt_submit_dispatches_shared_workflow",
            lambda: test_rvf_user_prompt_submit_dispatches_shared_workflow(root / "prompt-submit-token"),
        ),
        (
            "rvf_user_prompt_submit_revives_expired_prep_when_run_artifacts_exist",
            lambda: test_rvf_user_prompt_submit_revives_expired_prep_when_run_artifacts_exist(
                root / "prompt-submit-revive"
            ),
        ),
        (
            "rvf_user_prompt_submit_reports_no_prep_when_expired_and_run_dir_missing",
            lambda: test_rvf_user_prompt_submit_reports_no_prep_when_expired_and_run_dir_missing(
                root / "prompt-submit-revive-missing"
            ),
        ),
        (
            "rvf_user_prompt_submit_marker_without_token",
            lambda: test_rvf_user_prompt_submit_marker_without_token(root / "prompt-submit-marker"),
        ),
        (
            "rvf_user_prompt_submit_arms_kanban_followup_lock_on_delivery",
            lambda: test_rvf_user_prompt_submit_arms_kanban_followup_lock_on_delivery(
                root / "prompt-submit-followup-arm"
            ),
        ),
        (
            "kanban_followup_pending_marker_round_trip",
            lambda: test_kanban_followup_pending_marker_round_trip(
                root / "kanban-followup-pending"
            ),
        ),
        (
            "kanban_followup_iter_pending_and_stamp_notified",
            lambda: test_kanban_followup_iter_pending_and_stamp_notified(
                root / "kanban-followup-iter-stamp"
            ),
        ),
        (
            "notify_kanban_followup_stranded",
            lambda: test_notify_kanban_followup_stranded(root / "kanban-followup-notify"),
        ),
        (
            "rvf_user_prompt_submit_clears_pending_on_delivery",
            lambda: test_rvf_user_prompt_submit_clears_pending_on_delivery(
                root / "prompt-submit-followup-pending-clear"
            ),
        ),
        (
            "rvf_user_prompt_submit_structured_manual_detection_catches_namespaced",
            lambda: test_rvf_user_prompt_submit_structured_manual_detection_catches_namespaced(
                root / "prompt-submit-structured-manual"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_path_creates_prep_and_runs_prepare",
            lambda: test_rvf_user_prompt_submit_manual_path_creates_prep_and_runs_prepare(root / "prompt-submit-manual"),
        ),
        (
            "parse_manual_scope_directive_variants",
            lambda: test_parse_manual_scope_directive_variants(),
        ),
        (
            "rvf_user_prompt_submit_manual_scope_directive_passes_primary_files",
            lambda: test_rvf_user_prompt_submit_manual_scope_directive_passes_primary_files(
                root / "prompt-submit-manual-scope"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger",
            lambda: test_rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger(root / "prompt-submit-substring"),
        ),
        (
            "rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger",
            lambda: test_rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger(
                root / "prompt-submit-handoff-literal"
            ),
        ),
        (
            "rvf_user_prompt_submit_namespaced_subskill_does_not_falsely_trigger",
            lambda: test_rvf_user_prompt_submit_namespaced_subskill_does_not_falsely_trigger(
                root / "prompt-submit-namespaced-subskill"
            ),
        ),
        (
            "rvf_user_prompt_submit_failed_prepare_records_state_without_blocking",
            lambda: test_rvf_user_prompt_submit_failed_prepare_records_state_without_blocking(root / "prompt-submit-failed"),
        ),
        (
            "claude_plugin_hooks_declare_user_prompt_submit",
            lambda: test_claude_plugin_hooks_declare_user_prompt_submit(),
        ),
        (
            "claude_hook_entry_detects_foreign_invocation",
            lambda: test_claude_hook_entry_detects_foreign_invocation(),
        ),
        (
            "claude_hook_entry_dispatches_claude_invocation",
            lambda: test_claude_hook_entry_dispatches_claude_invocation(),
        ),
        (
            "claude_plugin_shim_codex_invocation_noop",
            lambda: test_claude_plugin_shim_codex_invocation_noop(root / "shim-codex-noop"),
        ),
        (
            "claude_plugin_stop_shim_codex_invocation_noop",
            lambda: test_claude_plugin_stop_shim_codex_invocation_noop(root / "stop-shim-codex-noop"),
        ),
        (
            "rvf_user_prompt_submit_backfills_child_session",
            lambda: test_rvf_user_prompt_submit_backfills_child_session(root / "prompt-submit-child"),
        ),
        (
            "prepare_run_from_prep_file_timeout_returns_immediately",
            lambda: test_prepare_run_from_prep_file_timeout_returns_immediately(root / "prepare-run-timeout"),
        ),
        (
            "rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode",
            lambda: test_rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode(root / "prompt-submit-silent"),
        ),
        (
            "rvf_user_prompt_submit_dispatch_no_prep_emits_user_visible_systemMessage",
            lambda: test_rvf_user_prompt_submit_dispatch_no_prep_emits_user_visible_systemMessage(
                root / "prompt-submit-no-prep"
            ),
        ),
        (
            "rvf_user_prompt_submit_render_hook_payload_merges_channels",
            lambda: test_rvf_user_prompt_submit_render_hook_payload_merges_channels(root / "prompt-submit-render"),
        ),
        ("check_review_output_lock_request", lambda: test_check_review_output_lock_request()),
        (
            "check_review_output_protocol_extension_requests",
            lambda: test_check_review_output_protocol_extension_requests(),
        ),
        (
            "review_result_artifact_no_issues_and_issues",
            lambda: test_review_result_artifact_no_issues_and_issues(root / "review-result-basic"),
        ),
        (
            "no_issues_requires_audit_summary",
            lambda: test_no_issues_requires_audit_summary(root / "no-issues-audit-summary"),
        ),
        (
            "review_result_artifact_requests_and_scope_exclusions",
            lambda: test_review_result_artifact_requests_and_scope_exclusions(root / "review-result-request"),
        ),
        (
            "review_result_artifact_rejects_malformed_and_mixed_state",
            lambda: test_review_result_artifact_rejects_malformed_and_mixed_state(root / "review-result-invalid"),
        ),
        (
            "issue_requires_kind",
            lambda: test_issue_requires_kind(root / "issue-requires-kind"),
        ),
        (
            "issue_requires_severity",
            lambda: test_issue_requires_severity(root / "issue-requires-severity"),
        ),
        (
            "check_rejects_issue_without_kind",
            lambda: test_check_rejects_issue_without_kind(root / "check-rejects-no-kind"),
        ),
        (
            "check_rejects_invalid_severity",
            lambda: test_check_rejects_invalid_severity(root / "check-rejects-bad-severity"),
        ),
        (
            "check_skill_contracts_requires_validate_fix_request_literals",
            lambda: test_check_skill_contracts_requires_validate_fix_request_literals(),
        ),
        (
            "contract_check_entrypoints_default_quiet_with_verbose_flag",
            lambda: test_contract_check_entrypoints_default_quiet_with_verbose_flag(),
        ),
        (
            "contract_check_parallel_test_steps_record_parallel_timing",
            lambda: test_contract_check_parallel_test_steps_record_parallel_timing(),
        ),
        (
            "contract_check_timing_report_accounts_internal_steps",
            lambda: test_contract_check_timing_report_accounts_internal_steps(),
        ),
        (
            "run_ledger_summary_preserves_contract_timing_fields",
            lambda: test_run_ledger_summary_preserves_contract_timing_fields(
                root / "summary-preserve-contract-timing"
            ),
        ),
        (
            "rvf_logging_non_canonical_skill_dirs_default_to_installed_plugin_state",
            lambda: test_rvf_logging_non_canonical_skill_dirs_default_to_installed_plugin_state(
                root / "non-canonical-log-root"
            ),
        ),
        (
            "rvf_logging_falls_back_to_skill_dir_state_when_install_missing",
            lambda: test_rvf_logging_falls_back_to_skill_dir_state_when_install_missing(
                root / "skill-dir-fallback-state"
            ),
        ),
        (
            "check_review_output_accepts_wrapped_issue_continuation",
            lambda: test_check_review_output_accepts_wrapped_issue_continuation(),
        ),
        ("build_packet_metadata_and_scope", lambda: test_build_packet_metadata_and_scope(root / "packet")),
        (
            "build_packet_allows_clean_repo_with_manual_scope",
            lambda: test_build_packet_allows_clean_repo_with_manual_scope(root / "packet-clean-manual-scope"),
        ),
        (
            "session_manifest_extracts_apply_patch_and_command_candidates",
            lambda: test_session_manifest_extracts_apply_patch_and_command_candidates(root / "session-manifest"),
        ),
        (
            "session_manifest_does_not_claim_post_commit_same_path_background_dirty",
            lambda: test_session_manifest_does_not_claim_post_commit_same_path_background_dirty(
                root / "session-manifest-post-commit"
            ),
        ),
        (
            "session_manifest_claims_apply_patch_after_commit_cutoff",
            lambda: test_session_manifest_claims_apply_patch_after_commit_cutoff(
                root / "session-manifest-commit-cutoff"
            ),
        ),
        (
            "session_manifest_only_claims_matching_apply_patch_hunk",
            lambda: test_session_manifest_only_claims_matching_apply_patch_hunk(
                root / "session-manifest-live-hunk"
            ),
        ),
        (
            "session_manifest_records_edit_claim_user_context",
            lambda: test_session_manifest_records_edit_claim_user_context(
                root / "session-manifest-edit-claim-user-context"
            ),
        ),
        (
            "session_manifest_records_codex_message_user_context",
            lambda: test_session_manifest_records_codex_message_user_context(
                root / "session-manifest-codex-message-user-context"
            ),
        ),
        (
            "complete_review_scope_waits_for_all_edit_claim_units",
            lambda: test_complete_review_scope_waits_for_all_edit_claim_units(
                root / "complete-review-scope-edit-claim-units"
            ),
        ),
        (
            "session_manifest_suppresses_unresolved_without_tracker_watermark",
            lambda: test_session_manifest_suppresses_unresolved_without_tracker_watermark(
                root / "session-manifest-unresolved-no-watermark"
            ),
        ),
        (
            "session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark",
            lambda: test_session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark(
                root / "session-manifest-unresolved-with-watermark"
            ),
        ),
        (
            "session_manifest_uses_tracker_transcript_watermark",
            lambda: test_session_manifest_uses_tracker_transcript_watermark(
                root / "session-manifest-tracker-watermark"
            ),
        ),
        (
            "session_manifest_legacy_timestampless_transcript_fallback_warns",
            lambda: test_session_manifest_legacy_timestampless_transcript_fallback_warns(
                root / "session-manifest-legacy-fallback"
            ),
        ),
        (
            "session_manifest_resolves_exec_paths_from_command_workdir",
            lambda: test_session_manifest_resolves_exec_paths_from_command_workdir(
                root / "session-manifest-workdir"
            ),
        ),
        (
            "session_manifest_claims_claude_write_tool_paths",
            lambda: test_session_manifest_claims_claude_write_tool_paths(
                root / "session-manifest-claude-write"
            ),
        ),
        (
            "diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap",
            lambda: test_diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap(
                root / "diagnose-stop-hook-scope"
            ),
        ),
        (
            "build_packet_uses_session_manifest_as_scope_anchor",
            lambda: test_build_packet_uses_session_manifest_as_scope_anchor(root / "packet-manifest"),
        ),
        (
            "build_packet_filters_session_owned_diff_to_tracker_hunk",
            lambda: test_build_packet_filters_session_owned_diff_to_tracker_hunk(
                root / "packet-tracker-hunk"
            ),
        ),
        (
            "build_packet_rejects_session_manifest_for_different_repo",
            lambda: test_build_packet_rejects_session_manifest_for_different_repo(
                root / "packet-manifest-repo"
            ),
        ),
        (
            "build_packet_rejects_empty_session_owned_scope",
            lambda: test_build_packet_rejects_empty_session_owned_scope(root / "packet-manifest-empty"),
        ),
        (
            "build_packet_requires_session_context",
            lambda: test_build_packet_requires_session_context(root / "packet-requires-context"),
        ),
        (
            "build_packet_honors_review_validate_fix_ignore",
            lambda: test_build_packet_honors_review_validate_fix_ignore(root / "packet-ignore"),
        ),
        (
            "build_packet_treats_ignore_prefixes_as_literal_pathspecs",
            lambda: test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(root / "packet-literal-ignore"),
        ),
        ("prepare_review_run_and_command_lock", lambda: test_prepare_review_run_and_command_lock(root / "prepare")),
        (
            "alternative_reviewer_prompt_uses_session_env_refs",
            lambda: test_alternative_reviewer_prompt_uses_session_env_refs(root / "alternative-prompt-env"),
        ),
        (
            "alternative_reviewer_infers_scope_contract_from_inputs_layout",
            lambda: test_alternative_reviewer_infers_scope_contract_from_inputs_layout(
                root / "alternative-inputs-scope"
            ),
        ),
        (
            "alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract",
            lambda: test_alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract(
                root / "alternative-session-alias"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_refreshes_before_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_refreshes_before_reviewer(
                root / "alternative-pre-run-health"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_failure_skips_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_failure_skips_reviewer(
                root / "alternative-pre-run-health-failure"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_timeout_skips_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_timeout_skips_reviewer(
                root / "alternative-pre-run-health-timeout"
            ),
        ),
        (
            "prepare_review_run_manual_all_uncommitted_allows_dirty_paths",
            lambda: test_prepare_review_run_manual_all_uncommitted_allows_dirty_paths(root / "prepare-manual-all"),
        ),
        (
            "command_lock_writes_lifecycle_events",
            lambda: test_command_lock_writes_lifecycle_events(root / "command-lock-lifecycle"),
        ),
        (
            "command_lock_respects_env_run_dir",
            lambda: test_command_lock_respects_env_run_dir(root / "command-lock-env-run-dir"),
        ),
        (
            "command_lock_logs_timeout_with_holder_metadata",
            lambda: test_command_lock_logs_timeout_with_holder_metadata(root / "command-lock-timeout"),
        ),
        (
            "prepare_review_run_can_build_session_manifest_from_transcript",
            lambda: test_prepare_review_run_can_build_session_manifest_from_transcript(root / "prepare-transcript"),
        ),
        (
            "prepare_review_run_requires_session_context",
            lambda: test_prepare_review_run_requires_session_context(root / "prepare-requires-context"),
        ),
        (
            "alternative_reviewer_idle_timeout_flag",
            lambda: test_alternative_reviewer_idle_timeout_flag(root / "alternative-timeout"),
        ),
        (
            "alternative_reviewer_activity_probe_keeps_silent_reviewer_alive",
            lambda: test_alternative_reviewer_activity_probe_keeps_silent_reviewer_alive(
                root / "alternative-probe-success"
            ),
        ),
        (
            "alternative_reviewer_requires_review_result_artifact",
            lambda: test_alternative_reviewer_requires_review_result_artifact(
                root / "alternative-missing-result"
            ),
        ),
        (
            "alternative_reviewer_records_request_as_pending_state",
            lambda: test_alternative_reviewer_records_request_as_pending_state(
                root / "alternative-request-pending"
            ),
        ),
        (
            "alternative_reviewer_activity_probe_failure_threshold_times_out",
            lambda: test_alternative_reviewer_activity_probe_failure_threshold_times_out(
                root / "alternative-probe-failure"
            ),
        ),
        (
            "alternative_reviewer_timeout_kills_child_process_group",
            lambda: test_alternative_reviewer_timeout_kills_child_process_group(root / "alternative-timeout-child"),
        ),
        (
            "alternative_reviewer_activity_refreshes_idle_timeout",
            lambda: test_alternative_reviewer_activity_refreshes_idle_timeout(root / "alternative-activity"),
        ),
        (
            "alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout",
            lambda: test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout(
                root / "alternative-bash-tool"
            ),
        ),
        (
            "alternative_reviewer_repeated_run_keeps_prior_artifacts",
            lambda: test_alternative_reviewer_repeated_run_keeps_prior_artifacts(root / "alternative-repeat-artifacts"),
        ),
        (
            "alternative_reviewer_long_command_wait_uses_check_interval",
            lambda: test_alternative_reviewer_long_command_wait_uses_check_interval(),
        ),
        (
            "alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state",
            lambda: test_alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state(),
        ),
        (
            "alternative_reviewer_claude_stream_json_extracts_result",
            lambda: test_alternative_reviewer_claude_stream_json_extracts_result(root / "alternative-stream-json"),
        ),
        (
            "alternative_reviewer_cursor_stream_json_extracts_result",
            lambda: test_alternative_reviewer_cursor_stream_json_extracts_result(root / "alternative-cursor-stream-json"),
        ),
        (
            "cursor_stream_tool_layer_health_classifier_and_summary",
            lambda: test_cursor_stream_tool_layer_health_classifier_and_summary(),
        ),
        (
            "alternative_reviewer_cursor_stream_monitor_detects_tool_layer_failure",
            lambda: test_alternative_reviewer_cursor_stream_monitor_detects_tool_layer_failure(),
        ),
        (
            "alternative_reviewer_cursor_tool_layer_failure_fast_aborts",
            lambda: test_alternative_reviewer_cursor_tool_layer_failure_fast_aborts(root / "cursor-tool-layer-fast-abort"),
        ),
        (
            "alternative_reviewer_cursor_command_not_claude_patched",
            lambda: test_alternative_reviewer_cursor_command_not_claude_patched(root / "alternative-cursor-command"),
        ),
        (
            "alternative_reviewer_cursor_autodetects_stream_json",
            lambda: test_alternative_reviewer_cursor_autodetects_stream_json(root / "alternative-cursor-autodetect"),
        ),
        (
            "alternative_reviewer_codex_json_extracts_agent_message",
            lambda: test_alternative_reviewer_codex_json_extracts_agent_message(root / "alternative-codex-json"),
        ),
        (
            "alternative_reviewer_codex_json_extracts_item_completed_agent_message",
            lambda: test_alternative_reviewer_codex_json_extracts_item_completed_agent_message(
                root / "alternative-codex-json-item-completed"
            ),
        ),
        (
            "alternative_reviewer_codex_json_reports_backend_challenge_html",
            lambda: test_alternative_reviewer_codex_json_reports_backend_challenge_html(
                root / "alternative-codex-challenge-html"
            ),
        ),
        (
            "alternative_reviewer_codex_exec_json_command_is_patched",
            lambda: test_alternative_reviewer_codex_exec_json_command_is_patched(root / "alternative-codex-command"),
        ),
        (
            "alternative_reviewer_codex_exec_after_global_options_is_patched",
            lambda: test_alternative_reviewer_codex_exec_after_global_options_is_patched(
                root / "alternative-codex-global-options"
            ),
        ),
        (
            "alternative_reviewer_codex_hooks_disable_is_not_duplicated",
            lambda: test_alternative_reviewer_codex_hooks_disable_is_not_duplicated(),
        ),
        (
            "alternative_reviewer_sets_codex_stop_hook_suppress_env",
            lambda: test_alternative_reviewer_sets_codex_stop_hook_suppress_env(root / "alternative-codex-suppress"),
        ),
        (
            "alternative_reviewer_legacy_claude_config_gets_stream_json",
            lambda: test_alternative_reviewer_legacy_claude_config_gets_stream_json(root / "alternative-legacy-config"),
        ),
        (
            "alternative_reviewer_respects_explicit_claude_text_output",
            lambda: test_alternative_reviewer_respects_explicit_claude_text_output(root / "alternative-text-config"),
        ),
        (
            "alternative_reviewer_non_claude_stream_json_command_is_not_patched",
            lambda: test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(
                root / "alternative-wrapper"
            ),
        ),
        ("cline_kanban_client_detects_runtime_port", lambda: test_cline_kanban_client_detects_runtime_port()),
        (
            "cline_kanban_client_rejects_ambiguous_runtime_ports",
            lambda: test_cline_kanban_client_rejects_ambiguous_runtime_ports(),
        ),
        (
            "cline_kanban_client_reports_missing_stable_binary",
            test_cline_kanban_client_reports_missing_stable_binary,
        ),
        (
            "cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd",
            lambda: test_cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd(
                root / "cline-kanban-cline-tmux-listener"
            ),
        ),
        (
            "cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane",
            test_cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane,
        ),
        (
            "cline_kanban_client_rejects_listener_without_cline_tmux_session",
            lambda: test_cline_kanban_client_rejects_listener_without_cline_tmux_session(
                root / "cline-kanban-non-cline-tmux-listener"
            ),
        ),
        (
            "cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener",
            lambda: test_cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener(
                root / "cline-kanban-workspace-payload"
            ),
        ),
        (
            "cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener",
            lambda: test_cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener(
                root / "cline-kanban-workspace-payload-no-tmux"
            ),
        ),
        (
            "cline_kanban_client_does_not_start_when_listener_exists_but_list_fails",
            lambda: test_cline_kanban_client_does_not_start_when_listener_exists_but_list_fails(
                root / "cline-kanban-existing-listener-list-fails"
            ),
        ),
        (
            "cline_kanban_client_create_and_start_task",
            lambda: test_cline_kanban_client_create_and_start_task(root / "cline-kanban-client"),
        ),
        (
            "cline_kanban_client_rejects_main_worktree_mode",
            lambda: test_cline_kanban_client_rejects_main_worktree_mode(
                root / "cline-kanban-main-worktree-mode"
            ),
        ),
        (
            "cline_kanban_client_start_task_uses_session_cwd_workspace",
            lambda: test_cline_kanban_client_start_task_uses_session_cwd_workspace(
                root / "cline-kanban-session-cwd"
            ),
        ),
        (
            "cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path",
            lambda: test_cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path(
                root / "cline-kanban-task-workspace"
            ),
        ),
        (
            "cline_kanban_client_branch_mode_rejects_parent_project_workspace",
            lambda: test_cline_kanban_client_branch_mode_rejects_parent_project_workspace(
                root / "cline-kanban-parent-workspace"
            ),
        ),
        (
            "cline_kanban_client_message_accepts_response_without_task_id",
            lambda: test_cline_kanban_client_message_accepts_response_without_task_id(root / "cline-kanban-message"),
        ),
        (
            "prepare_review_run_writes_worktree_bootstrap",
            lambda: test_prepare_review_run_writes_worktree_bootstrap(root / "worktree-bootstrap"),
        ),
        (
            "prepare_review_run_worktree_bootstrap_respects_review_validate_fix_ignore",
            lambda: test_prepare_review_run_worktree_bootstrap_respects_review_validate_fix_ignore(
                root / "worktree-bootstrap-ignore"
            ),
        ),
        (
            "prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide",
            lambda: test_prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide(
                root / "worktree-bootstrap-name-collision"
            ),
        ),
        (
            "prepare_review_run_scope_file_matches_metadata_through_symlink_state",
            lambda: test_prepare_review_run_scope_file_matches_metadata_through_symlink_state(
                root / "prepare-symlink-state"
            ),
        ),
        (
            "apply_worktree_bootstrap_replays_tracked_and_untracked",
            lambda: test_apply_worktree_bootstrap_replays_tracked_and_untracked(root / "apply-bootstrap"),
        ),
        (
            "apply_worktree_bootstrap_rejects_mismatched_base_ref",
            lambda: test_apply_worktree_bootstrap_rejects_mismatched_base_ref(root / "apply-bootstrap-base-ref"),
        ),
        (
            "run_ledger_summary_preserves_cline_kanban_fields",
            lambda: test_run_ledger_summary_preserves_cline_kanban_fields(root / "summary-preserve-cline"),
        ),
        (
            "run_ledger_summary_preserves_rvf_state_fields",
            lambda: test_run_ledger_summary_preserves_rvf_state_fields(root / "summary-preserve-rvf-state"),
        ),
        (
            "cancel_rvf_run_marks_cancelled_and_trashes_cline_task",
            lambda: test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task(root / "cancel-rvf-run"),
        ),
        (
            "cancel_rvf_run_ignores_stale_runner_pid_without_matching_command",
            lambda: test_cancel_rvf_run_ignores_stale_runner_pid_without_matching_command(),
        ),
        (
            "diff_tracker_register_creates_sqlite_and_events",
            lambda: test_diff_tracker_register_creates_sqlite_and_events(root / "diff-tracker-register"),
        ),
        (
            "diff_tracker_register_concurrent_writers",
            lambda: test_diff_tracker_register_concurrent_writers(root / "diff-tracker-concurrent"),
        ),
        (
            "canonical_patch_hash_stable_across_reruns",
            lambda: test_canonical_patch_hash_stable_across_reruns(root / "diff-tracker-stable"),
        ),
        (
            "canonical_patch_hash_stable_under_line_shift",
            lambda: test_canonical_patch_hash_stable_under_line_shift(root / "diff-tracker-line-shift"),
        ),
        (
            "canonical_patch_hash_changes_on_content_edit",
            lambda: test_canonical_patch_hash_changes_on_content_edit(root / "diff-tracker-content-edit"),
        ),
        (
            "migration_phase1_json_to_sqlite_idempotent",
            lambda: test_migration_phase1_json_to_sqlite_idempotent(root / "diff-tracker-migration"),
        ),
        (
            "migration_phase1_recovers_when_archive_predates_db_marker",
            lambda: test_migration_phase1_recovers_when_archive_predates_db_marker(
                root / "diff-tracker-migration-recover"
            ),
        ),
        (
            "diff_tracker_hunk_anchor_distinguishes_close_hunks",
            lambda: test_diff_tracker_hunk_anchor_distinguishes_close_hunks(
                root / "diff-tracker-close-hunks"
            ),
        ),
        (
            "diff_tracker_register_empty_owned_paths_preserves_session_claim",
            lambda: test_diff_tracker_register_empty_owned_paths_preserves_session_claim(
                root / "diff-tracker-empty-paths"
            ),
        ),
        (
            "diff_tracker_list_conflicts_reports_other_session_overlap",
            lambda: test_diff_tracker_list_conflicts_reports_other_session_overlap(root / "diff-tracker-conflicts"),
        ),
        (
            "diff_tracker_path_claim_conflicts_with_hunk_claim",
            lambda: test_diff_tracker_path_claim_conflicts_with_hunk_claim(root / "diff-tracker-path-vs-hunk"),
        ),
        (
            "session_manifest_writes_tracker_claim",
            lambda: test_session_manifest_writes_tracker_claim(root / "session-manifest-tracker"),
        ),
        (
            "build_packet_emits_cross_session_conflict_section",
            lambda: test_build_packet_emits_cross_session_conflict_section(root / "packet-cross-session"),
        ),
        (
            "build_packet_omits_cross_session_section_when_clean",
            lambda: test_build_packet_omits_cross_session_section_when_clean(root / "packet-cross-session-clean"),
        ),
        (
            "diff_tracker_disable_env_short_circuits",
            lambda: test_diff_tracker_disable_env_short_circuits(root / "diff-tracker-disabled"),
        ),
        (
            "diff_tracker_lock_timeout_degrades_gracefully",
            lambda: test_diff_tracker_lock_timeout_degrades_gracefully(root / "diff-tracker-lock-timeout"),
        ),
        (
            "tracker_scope_payload_splices_into_manifest",
            lambda: test_tracker_scope_payload_splices_into_manifest(root / "tracker-scope-splice"),
        ),
        (
            "tracker_scope_unlocks_scope_contract_v2_fields",
            lambda: test_tracker_scope_unlocks_scope_contract_v2_fields(root / "tracker-scope-contract-v2"),
        ),
        (
            "scope_contract_v2_emitted_without_tracker_scope",
            lambda: test_scope_contract_v2_emitted_without_tracker_scope(root / "tracker-scope-contract-v2-bare"),
        ),
        (
            "packet_emits_tracker_scope_section_when_present",
            lambda: test_packet_emits_tracker_scope_section_when_present(root / "tracker-scope-packet-present"),
        ),
        (
            "packet_omits_tracker_scope_section_when_absent",
            lambda: test_packet_omits_tracker_scope_section_when_absent(root / "tracker-scope-packet-absent"),
        ),
        (
            "packet_metadata_carries_tracker_scope_keys",
            lambda: test_packet_metadata_carries_tracker_scope_keys(root / "tracker-scope-metadata"),
        ),
        (
            "tracker_scope_payload_rejects_invalid_payloads",
            lambda: test_tracker_scope_payload_rejects_invalid_payloads(root / "tracker-scope-rejection"),
        ),
        (
            "tracker_scope_requires_session_manifest_or_transcript",
            lambda: test_tracker_scope_requires_session_manifest_or_transcript(root / "tracker-scope-d4"),
        ),
        (
            "tracker_scope_tolerates_unknown_keys",
            lambda: test_tracker_scope_tolerates_unknown_keys(root / "tracker-scope-unknown-keys"),
        ),
        (
            "review_env_exports_rvf_tracker_scope_path",
            lambda: test_review_env_exports_rvf_tracker_scope_path(root / "tracker-scope-env"),
        ),
        (
            "allocated_git_diff_uses_tracker_scope_paths",
            lambda: test_allocated_git_diff_uses_tracker_scope_paths(root / "tracker-scope-diff-filter"),
        ),
        (
            "existing_cross_session_conflicts_path_unchanged_with_tracker_scope",
            lambda: test_existing_cross_session_conflicts_path_unchanged_with_tracker_scope(root / "tracker-scope-cross-session"),
        ),
        # Slice 3 allocator T1-T15.
        (
            "allocate_review_scope_emits_valid_tracker_scope_json",
            lambda: test_allocate_review_scope_emits_valid_tracker_scope_json(root / "alloc-T1"),
        ),
        (
            "allocate_review_scope_empty_returns_no_unassigned_review_scope",
            lambda: test_allocate_review_scope_empty_returns_no_unassigned_review_scope(root / "alloc-T2"),
        ),
        (
            "allocate_review_scope_preserves_untracked_file_under_new_directory",
            lambda: test_allocate_review_scope_preserves_untracked_file_under_new_directory(root / "alloc-T2b"),
        ),
        (
            "allocate_review_scope_excludes_active_leased_units",
            lambda: test_allocate_review_scope_excludes_active_leased_units(root / "alloc-T3"),
        ),
        (
            "allocate_review_scope_inserts_lease_and_marks_units_assigned",
            lambda: test_allocate_review_scope_inserts_lease_and_marks_units_assigned(root / "alloc-T4"),
        ),
        (
            "allocate_review_scope_prunes_stale_leases_first",
            lambda: test_allocate_review_scope_prunes_stale_leases_first(root / "alloc-T5"),
        ),
        (
            "allocate_review_scope_concurrent_writers_serialize",
            lambda: test_allocate_review_scope_concurrent_writers_serialize(root / "alloc-T6"),
        ),
        (
            "fork_first_stop_takeover_transfers_unleased_units",
            lambda: test_fork_first_stop_takeover_transfers_unleased_units(root / "alloc-T7"),
        ),
        (
            "fork_takeover_skips_actively_leased_units",
            lambda: test_fork_takeover_skips_actively_leased_units(root / "alloc-T8"),
        ),
        (
            "scope_hash_is_sha256_of_sorted_unit_ids",
            lambda: test_scope_hash_is_sha256_of_sorted_unit_ids(root / "alloc-T9"),
        ),
        (
            "allocator_event_appended_to_events_jsonl",
            lambda: test_allocator_event_appended_to_events_jsonl(root / "alloc-T10"),
        ),
        (
            "manual_rvf_run_inserts_row_and_emits_event",
            lambda: test_manual_rvf_run_inserts_row_and_emits_event(root / "manual-run-T1"),
        ),
        (
            "manual_rvf_run_upserts_on_pk_conflict",
            lambda: test_manual_rvf_run_upserts_on_pk_conflict(root / "manual-run-T2"),
        ),
        (
            "manual_rvf_run_find_returns_latest_completed_at",
            lambda: test_manual_rvf_run_find_returns_latest_completed_at(root / "manual-run-T4"),
        ),
        (
            "manual_rvf_run_find_respects_ttl",
            lambda: test_manual_rvf_run_find_respects_ttl(root / "manual-run-T5"),
        ),
        (
            "manual_rvf_run_ensures_table_for_existing_v2_db",
            lambda: test_manual_rvf_run_ensures_table_for_existing_v2_db(root / "manual-run-T6"),
        ),
        (
            "manual_takeover_transfers_unleased_units",
            lambda: test_manual_takeover_transfers_unleased_units(root / "manual-takeover-T1"),
        ),
        (
            "manual_takeover_skips_actively_leased_units",
            lambda: test_manual_takeover_skips_actively_leased_units(root / "manual-takeover-T2"),
        ),
        (
            "manual_takeover_rejects_missing_parent_session",
            lambda: test_manual_takeover_rejects_missing_parent_session(root / "manual-takeover-T3"),
        ),
        (
            "manual_takeover_cli_records_takeover",
            lambda: test_manual_takeover_cli_records_takeover(root / "manual-takeover-cli"),
        ),
        (
            "record_manual_run_cli_writes_row",
            lambda: test_record_manual_run_cli_writes_row(root / "manual-record-cli"),
        ),
        (
            "allocate_review_scope_disable_env_short_circuits",
            lambda: test_allocate_review_scope_disable_env_short_circuits(root / "alloc-T11"),
        ),
        (
            "allocate_review_scope_busy_timeout_degrades",
            lambda: test_allocate_review_scope_busy_timeout_degrades(root / "alloc-T12"),
        ),
        (
            "allocate_review_scope_writes_paths_and_hunks",
            lambda: test_allocate_review_scope_writes_paths_and_hunks(root / "alloc-T13"),
        ),
        (
            "allocate_review_scope_dry_run_does_not_create_lease",
            lambda: test_allocate_review_scope_dry_run_does_not_create_lease(root / "alloc-T14"),
        ),
        (
            "allocate_review_scope_output_consumed_by_prepare_run",
            lambda: test_allocate_review_scope_output_consumed_by_prepare_run(root / "alloc-T15"),
        ),
        # Slice 4 lease lifecycle.
        (
            "tracker_schema_v2_migrates_lease_participants_table",
            lambda: test_tracker_schema_v2_migrates_lease_participants_table(root / "lease-T0"),
        ),
        (
            "tracker_schema_v4_rebuilds_legacy_tombstoned_review_state",
            lambda: test_tracker_schema_v4_rebuilds_legacy_tombstoned_review_state(root / "lease-T0b"),
        ),
        (
            "lease_acquire_creates_lease_and_assigns_units",
            lambda: test_lease_acquire_creates_lease_and_assigns_units(root / "lease-T1"),
        ),
        (
            "lease_acquire_rejects_when_any_unit_already_leased",
            lambda: test_lease_acquire_rejects_when_any_unit_already_leased(root / "lease-T2"),
        ),
        (
            "lease_acquire_rejects_tombstoned_unit",
            lambda: test_lease_acquire_rejects_tombstoned_unit(root / "lease-T2b"),
        ),
        (
            "lease_acquire_prunes_stale_leases_first",
            lambda: test_lease_acquire_prunes_stale_leases_first(root / "lease-T3"),
        ),
        (
            "stale_prune_does_not_release_unit_reacquired_by_fresh_lease",
            lambda: test_stale_prune_does_not_release_unit_reacquired_by_fresh_lease(root / "lease-T3b"),
        ),
        (
            "lease_refresh_extends_expires_at",
            lambda: test_lease_refresh_extends_expires_at(root / "lease-T4"),
        ),
        (
            "lease_refresh_returns_expired_when_past_ttl",
            lambda: test_lease_refresh_returns_expired_when_past_ttl(root / "lease-T5"),
        ),
        (
            "heartbeat_refreshes_tracker_lease_and_records_backend",
            lambda: test_heartbeat_refreshes_tracker_lease_and_records_backend(root / "lease-T5b"),
        ),
        (
            "heartbeat_treats_same_second_expiry_as_expired",
            lambda: test_heartbeat_treats_same_second_expiry_as_expired(root / "lease-T5c"),
        ),
        (
            "lease_release_completed_marks_units_reviewed",
            lambda: test_lease_release_completed_marks_units_reviewed(root / "lease-T6"),
        ),
        (
            "lease_release_idempotent",
            lambda: test_lease_release_idempotent(root / "lease-T7"),
        ),
        (
            "complete_review_scope_unions_contract_and_lease_units",
            lambda: test_complete_review_scope_unions_contract_and_lease_units(root / "lease-T7a"),
        ),
        (
            "complete_review_scope_keeps_partial_edit_claim_pending",
            lambda: test_complete_review_scope_keeps_partial_edit_claim_pending(root / "lease-T7a-partial-claim"),
        ),
        (
            "complete_review_scope_does_not_complete_failed_released_lease",
            lambda: test_complete_review_scope_does_not_complete_failed_released_lease(root / "lease-T7c"),
        ),
        (
            "complete_review_scope_supersedes_overlapping_active_lease",
            lambda: test_complete_review_scope_supersedes_overlapping_active_lease(root / "lease-T7d"),
        ),
        (
            "complete_review_scope_keeps_different_scope_active_lease",
            lambda: test_complete_review_scope_keeps_different_scope_active_lease(root / "lease-T7e"),
        ),
        (
            "lease_participants_finish_does_not_release_shared_lease",
            lambda: test_lease_participants_finish_does_not_release_shared_lease(root / "lease-T7b"),
        ),
        (
            "sweep_stale_releases_expired_active_leases",
            lambda: test_sweep_stale_releases_expired_active_leases(root / "lease-T8"),
        ),
        (
            "sweep_stale_releases_same_second_expired_lease",
            lambda: test_sweep_stale_releases_same_second_expired_lease(root / "lease-T8b"),
        ),
        (
            "sweep_stale_no_op_when_all_active_leases_fresh",
            lambda: test_sweep_stale_no_op_when_all_active_leases_fresh(root / "lease-T9"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_normal_exit",
            lambda: test_run_alternative_reviewer_releases_lease_on_normal_exit(root / "lease-T10"),
        ),
        (
            "run_alternative_reviewer_shared_lease_does_not_release_on_exit",
            lambda: test_run_alternative_reviewer_shared_lease_does_not_release_on_exit(root / "lease-T10b"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_codex_backend_challenge",
            lambda: test_run_alternative_reviewer_releases_lease_on_codex_backend_challenge(root / "lease-T11"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_timeout",
            lambda: test_run_alternative_reviewer_releases_lease_on_timeout(root / "lease-T12"),
        ),
        (
            "run_alternative_reviewer_sigterm_kills_child_before_release",
            lambda: test_run_alternative_reviewer_sigterm_kills_child_before_release(root / "lease-T12b"),
        ),
        (
            "lease_acquire_concurrent_writers_serialize",
            lambda: test_lease_acquire_concurrent_writers_serialize(root / "lease-T13"),
        ),
        (
            "invalidate_reviewed_units_for_run_reopens_and_is_idempotent",
            lambda: test_invalidate_reviewed_units_for_run_reopens_and_is_idempotent(root / "reopen-T1"),
        ),
        (
            "invalidate_reviewed_units_for_run_is_run_scoped",
            lambda: test_invalidate_reviewed_units_for_run_is_run_scoped(root / "reopen-T2"),
        ),
        (
            "invalidate_reviewed_units_for_run_excludes_tombstoned",
            lambda: test_invalidate_reviewed_units_for_run_excludes_tombstoned(root / "reopen-T3"),
        ),
        (
            "latest_reviewed_run_for_worktree_resolution",
            lambda: test_latest_reviewed_run_for_worktree_resolution(root / "reopen-T4"),
        ),
        (
            "rvf_analyze_thread_builds_claude_command",
            lambda: test_rvf_analyze_thread_builds_claude_command(),
        ),
        (
            "rvf_analyze_thread_builds_codex_command",
            lambda: test_rvf_analyze_thread_builds_codex_command(),
        ),
        (
            "rvf_analyze_thread_select_host",
            lambda: test_rvf_analyze_thread_select_host(root / "analyze-thread-host"),
        ),
        (
            "rvf_analyze_thread_status_file_schema",
            lambda: test_rvf_analyze_thread_status_file_schema(root / "analyze-thread-status"),
        ),
        (
            "rvf_analyze_thread_lock_blocks_second_launch",
            lambda: test_rvf_analyze_thread_lock_blocks_second_launch(root / "analyze-thread-lock"),
        ),
        (
            "rvf_detached_thread_status_two_phase",
            lambda: test_rvf_detached_thread_status_two_phase(root / "detached-two-phase"),
        ),
        (
            "rvf_detached_thread_lock_idempotent",
            lambda: test_rvf_detached_thread_lock_idempotent(root / "detached-lock"),
        ),
        (
            "rvf_detached_thread_launch_failed_releases_lock",
            lambda: test_rvf_detached_thread_launch_failed_releases_lock(root / "detached-failed"),
        ),
        (
            "rvf_detached_thread_reclaims_stale_lock_when_session_dead",
            lambda: test_rvf_detached_thread_reclaims_stale_lock_when_session_dead(
                root / "detached-stale-reclaim"
            ),
        ),
        (
            "rvf_detached_thread_keeps_lock_when_session_alive",
            lambda: test_rvf_detached_thread_keeps_lock_when_session_alive(
                root / "detached-stale-alive"
            ),
        ),
        (
            "rvf_detached_thread_keeps_lock_on_clean_finish",
            lambda: test_rvf_detached_thread_keeps_lock_on_clean_finish(
                root / "detached-stale-clean"
            ),
        ),
        (
            "rvf_detached_thread_keeps_lock_when_tmux_probe_fails",
            lambda: test_rvf_detached_thread_keeps_lock_when_tmux_probe_fails(
                root / "detached-stale-probe-fail"
            ),
        ),
        (
            "rvf_detached_thread_run_with_timeout",
            lambda: test_rvf_detached_thread_run_with_timeout(),
        ),
        (
            "rvf_detached_thread_finalize_status_cli",
            lambda: test_rvf_detached_thread_finalize_status_cli(root / "detached-finalize"),
        ),
        (
            "dispatch_reviewers_detached_launch_wiring",
            lambda: test_dispatch_reviewers_detached_launch_wiring(root / "dispatch-detached"),
        ),
        (
            "dispatch_reviewers_detached_exports_codex_rvf_log_root",
            lambda: test_dispatch_reviewers_detached_exports_codex_rvf_log_root(
                root / "dispatch-detached-exports"
            ),
        ),
        (
            "dispatch_reviewers_wait_status_branches",
            lambda: test_dispatch_reviewers_wait_status_branches(root / "dispatch-wait"),
        ),
    ]


def selected_test_cases(
    cases: list[tuple[str, object]],
    *,
    shard_count: int,
    shard_index: int,
) -> list[tuple[str, object]]:
    if shard_count <= 1:
        return cases
    return [case for index, case in enumerate(cases) if index % shard_count == shard_index]


def _timing_sink() -> Path | None:
    raw = os.environ.get("RVF_TEST_TIMING_JSONL")
    return Path(raw) if raw else None


def _record_timing(sink: Path, name: str, duration_ms: int, status: str) -> None:
    record = {"name": name, "duration_ms": duration_ms, "status": status}
    sink.parent.mkdir(parents=True, exist_ok=True)
    with sink.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        )


def _run_case_by_name(
    root_str: str, name: str
) -> tuple[str, bool, str | None, int]:
    """Worker: rebuild the registry (lambdas aren't picklable) and run
    one case by name in an isolated process."""
    cases = dict(review_support_test_cases(Path(root_str)))
    started = time.perf_counter()
    try:
        cases[name]()
        ok, tb = True, None
    except BaseException:
        ok, tb = False, traceback.format_exc()
    duration_ms = int((time.perf_counter() - started) * 1000)
    return name, ok, tb, duration_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel worker processes (default 1 = serial; the "
        "contract orchestrator never sets this). Best-effort dev "
        "accelerator: keep jobs <= cpu/2 — workers also spawn "
        "subprocess children, and a few reviewer tests use a fixed "
        "idle timeout that can starve under oversubscription.",
    )
    args = parser.parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must be in [0, shard-count)")
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    if args.jobs > 1:
        _cpu = os.cpu_count() or 2
        if args.jobs * 2 > _cpu:
            print(
                f"warning: --jobs {args.jobs} oversubscribes ~{_cpu} cores "
                f"(workers spawn subprocess children); timeout-coupled "
                f"reviewer tests may flake. Recommended: --jobs <= {_cpu // 2}.",
                file=sys.stderr,
            )

    # 分片之前无条件跑一次完整性守卫：注册表 = 唯一索引，任何 def test_* 漏登记即红。
    # 用任意 root 构造未分片的全量注册表即可（lambda 不被调用，root 值无关）。
    assert_every_defined_test_is_registered(
        globals(),
        registered_names_from_case_tuples(review_support_test_cases(Path("/"))),
        source_path=__file__,
        intentionally_unregistered=INTENTIONALLY_UNREGISTERED,
    )

    suffix = (
        f" shard {args.shard_index + 1}/{args.shard_count}"
        if args.shard_count > 1
        else ""
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        cases = selected_test_cases(
            review_support_test_cases(root),
            shard_count=args.shard_count,
            shard_index=args.shard_index,
        )
        timing_sink = _timing_sink()

        if args.jobs <= 1:
            for name, test_case in cases:
                if timing_sink is None:
                    test_case()
                    continue
                started = time.perf_counter()
                status = "completed"
                try:
                    test_case()
                except BaseException:
                    status = "failed"
                    raise
                finally:
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    _record_timing(timing_sink, name, duration_ms, status)
        else:
            results: dict[str, tuple[bool, str | None, int]] = {}
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=args.jobs
            ) as pool:
                futures = [
                    pool.submit(_run_case_by_name, str(root), name)
                    for name, _ in cases
                ]
                for fut in concurrent.futures.as_completed(futures):
                    name, ok, tb, duration_ms = fut.result()
                    results[name] = (ok, tb, duration_ms)
            # Report/record in original registry order so failures and
            # the timing JSONL are deterministic regardless of completion
            # order.
            if timing_sink is not None:
                for name, _ in cases:
                    ok, _tb, duration_ms = results[name]
                    _record_timing(
                        timing_sink,
                        name,
                        duration_ms,
                        "completed" if ok else "failed",
                    )
            for name, _ in cases:
                ok, tb, _duration_ms = results[name]
                if not ok:
                    raise AssertionError(
                        f"test case {name!r} failed:\n{tb}"
                    )
    print(f"review support script tests OK{suffix}")
    return 0



# Bounded split: the Slice-3 allocator/lease/tracker tests live in a
# submodule; inject the shared deps then bind the 54 test names so the
# (unchanged) registry lambdas resolve them. Registry order / shard
# identity are preserved.
from _rvf_review_support import allocator_lease_tracker as _alt
_alt.inject(
    run=run,
    read_jsonl=read_jsonl,
    init_repo=init_repo,
    write_alternative_reviewer_config=write_alternative_reviewer_config,
    clean_review_result_python=clean_review_result_python,
    load_diff_tracker_module=load_diff_tracker_module,
    _slice_2b_repo_with_two_dirty=_slice_2b_repo_with_two_dirty,
    _slice_2b_prepare=_slice_2b_prepare,
    SCRIPT_DIR=SCRIPT_DIR,
    DIFF_TRACKER=DIFF_TRACKER,
    PREPARE_REVIEW_RUN=PREPARE_REVIEW_RUN,
    RUN_ALTERNATIVE_REVIEWER=RUN_ALTERNATIVE_REVIEWER,
)
globals().update({_n: getattr(_alt, _n) for _n in _alt.__all__})


# 有界拆分：rvf_user_prompt_submit hook 测试簇移入子模块；inject 共享依赖后重绑 18 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import rvf_user_prompt_submit_hook as _ups
_ups.inject(
    run=run,
    read_jsonl=read_jsonl,
    load_rvf_user_prompt_submit_module=load_rvf_user_prompt_submit_module,
    load_rvf_prep_file_module=load_rvf_prep_file_module,
    load_kanban_followup_lock_module=load_kanban_followup_lock_module,
    _committed_round_repo=_committed_round_repo,
    _round_baseline_committed_modules=_round_baseline_committed_modules,
    RVF_USER_PROMPT_SUBMIT=RVF_USER_PROMPT_SUBMIT,
    SCRIPT_DIR=SCRIPT_DIR,
)
globals().update({_n: getattr(_ups, _n) for _n in _ups.__all__})


# 有界拆分：alternative reviewer CLI 适配器行为（codex/cursor/claude 流式、空闲、路由、预检） 测试簇移入子模块；inject 共享依赖后重绑 33 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import alternative_reviewer_cli_adapter_behaviors as _altrev
_altrev.inject(
    RUN_ALTERNATIVE_REVIEWER=RUN_ALTERNATIVE_REVIEWER,
    clean_review_result_python=clean_review_result_python,
    init_repo=init_repo,
    load_alternative_reviewer_module=load_alternative_reviewer_module,
    read_jsonl=read_jsonl,
    run=run,
    write_alternative_reviewer_config=write_alternative_reviewer_config,
    _cursor_tool_call_line=_cursor_tool_call_line,
)
globals().update({_n: getattr(_altrev, _n) for _n in _altrev.__all__})


# 有界拆分：cline-kanban client 与通知 测试簇移入子模块；inject 共享依赖后重绑 15 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import cline_kanban_client_and_notify as _clkb
_clkb.inject(
    run=run,
    init_repo=init_repo,
    load_cline_kanban_client_module=load_cline_kanban_client_module,
    CLINE_KANBAN_CLIENT=CLINE_KANBAN_CLIENT,
)
globals().update({_n: getattr(_clkb, _n) for _n in _clkb.__all__})


# 有界拆分：session manifest 记录与 ownership claims 测试簇移入子模块；inject 共享依赖后重绑 13 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import session_manifest_records_and_claims as _sessman
_sessman.inject(
    run=run,
    init_repo=init_repo,
    write_codex_transcript=write_codex_transcript,
    load_diff_tracker_module=load_diff_tracker_module,
    SESSION_MANIFEST=SESSION_MANIFEST,
)
globals().update({_n: getattr(_sessman, _n) for _n in _sessman.__all__})


# 有界拆分：dispatch reviewers 选路与 detached 派发 测试簇移入子模块；inject 共享依赖后重绑 12 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import dispatch_reviewers_routing_and_detached as _disprev
_disprev.inject(
    load_dispatch_reviewers_module=load_dispatch_reviewers_module,
    load_harness_limit_cooldown_module=load_harness_limit_cooldown_module,
    write_fake_tmux_script=write_fake_tmux_script,
    write_alternative_reviewer_config=write_alternative_reviewer_config,
    _dispatch_registry=_dispatch_registry,
    _make_usage_or_clean_registry=_make_usage_or_clean_registry,
    _with_cooldown_env=_with_cooldown_env,
    init_repo=init_repo,
    SCRIPT_DIR=SCRIPT_DIR,
)
globals().update({_n: getattr(_disprev, _n) for _n in _disprev.__all__})


# 有界拆分：review packet 组装与省略规则 测试簇移入子模块；inject 共享依赖后重绑 11 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import build_packet_assembly_and_omissions as _bldpkt
_bldpkt.inject(
    init_repo=init_repo,
    run=run,
    write_codex_transcript=write_codex_transcript,
    load_diff_tracker_module=load_diff_tracker_module,
    BUILD_PACKET=BUILD_PACKET,
    SESSION_MANIFEST=SESSION_MANIFEST,
)
globals().update({_n: getattr(_bldpkt, _n) for _n in _bldpkt.__all__})


# 有界拆分：rvf detached 线程生命周期 测试簇移入子模块；inject 共享依赖后重绑 9 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import rvf_detached_thread_lifecycle as _detthread
_detthread.inject(
    load_rvf_detached_thread_module=load_rvf_detached_thread_module,
    write_realexec_tmux_script=write_realexec_tmux_script,
    write_fake_tmux_script=write_fake_tmux_script,
    _detached_status_payload=_detached_status_payload,
    _seed_detached_stale_lock=_seed_detached_stale_lock,
    _launch_detached_with_staleness_env=_launch_detached_with_staleness_env,
    SCRIPT_DIR=SCRIPT_DIR,
)
globals().update({_n: getattr(_detthread, _n) for _n in _detthread.__all__})


# 有界拆分：diff tracker 注册与 scope 测试簇移入子模块；inject 共享依赖后重绑 9 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import diff_tracker_register_and_scope as _difftrk
_difftrk.inject(
    run=run,
    read_jsonl=read_jsonl,
    init_repo=init_repo,
    load_diff_tracker_module=load_diff_tracker_module,
    _round_baseline_committed_modules=_round_baseline_committed_modules,
    _committed_round_repo=_committed_round_repo,
    SCRIPT_DIR=SCRIPT_DIR,
)
globals().update({_n: getattr(_difftrk, _n) for _n in _difftrk.__all__})


# 有界拆分：prepare review run 与 worktree bootstrap 测试簇移入子模块；inject 共享依赖后重绑 8 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import prepare_review_run_bootstrap as _preprev
_preprev.inject(
    run=run,
    init_repo=init_repo,
    write_codex_transcript=write_codex_transcript,
    PREPARE_REVIEW_RUN=PREPARE_REVIEW_RUN,
    COMMAND_LOCK=COMMAND_LOCK,
    APPLY_WORKTREE_BOOTSTRAP=APPLY_WORKTREE_BOOTSTRAP,
)
globals().update({_n: getattr(_preprev, _n) for _n in _preprev.__all__})



if __name__ == "__main__":
    raise SystemExit(main())
