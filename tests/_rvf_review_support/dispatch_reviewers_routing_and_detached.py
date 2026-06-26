#!/usr/bin/env python3
"""dispatch reviewers 选路与 detached 派发 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_dispatch_reviewers_detached_launch_wiring',
    'test_dispatch_reviewers_detached_exports_codex_rvf_log_root',
    'test_dispatch_reviewers_wait_status_branches',
    'test_dispatch_reviewers_routing_matrix',
    'test_dispatch_reviewers_same_harness_double_instance_distinct_ids',
    'test_dispatch_reviewers_plan_artifact_schema',
    'test_dispatch_reviewers_executes_two_external',
    'test_dispatch_reviewers_execute_backfills_review_env',
    'test_dispatch_reviewers_reroutes_on_usage_limit',
    'test_dispatch_reviewers_reroute_id_collision',
    'test_dispatch_reviewers_probe_excludes_cooldown',
    'test_dispatch_reviewers_failclose_when_main_exhausted',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_dispatch_reviewers_detached_launch_wiring(root: Path) -> None:
    """--execute --detached：self-fork dispatch_reviewers.py --execute 进 tmux，立即返回 status 路径。"""
    import contextlib
    import io

    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_tmux = write_fake_tmux_script(root / "tmux.py")
    calls = root / "calls.jsonl"
    run_dir = root / "runs" / "rvf-disp-unit"
    reviewers_dir = run_dir / "artifacts" / "reviewers"
    reviewers_dir.mkdir(parents=True, exist_ok=True)

    class _Ledger:
        run_id = "rvf-disp-unit"

        def __init__(self, rd: Path) -> None:
            self.run_dir = rd

        def env(self) -> dict:
            return {"CODEX_RVF_RUN_ID": self.run_id}

        def event(self, **_kw) -> None:
            pass

    args = argparse.Namespace(
        registry="reg.json",
        probe_mode="preflight",
        probe_timeout=60.0,
        main_harness="auto",
        transcript=None,
        main_harness_file=None,
        assume_available=None,
        require_external=False,
        total_timeout=2700.0,
    )
    saved = {
        k: os.environ.get(k)
        for k in ("CODEX_RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")
    }
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = d.launch_detached_dispatch(
                args,
                _Ledger(run_dir),
                reviewers_dir,
                repo="/repo",
                review_packet="/pkt",
                session_context="/sow",
                scope_contract="/sc",
            )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    out = buf.getvalue()
    assert rc == 0, out
    assert "RVF_DISPATCH_STATUS=" in out and "RVF_DISPATCH_LAUNCH=launched" in out
    status = json.loads(
        (reviewers_dir / ".dispatch-thread.status.json").read_text(encoding="utf-8")
    )
    assert status["launch_status"] == "launched"
    assert status["tmux_session"] == "rvf-dispatch-rvf-disp-unit"
    child = status["command"]
    assert "--execute" in child and "--detached" not in child
    assert "--rvf-run-id" in child and "rvf-disp-unit" in child
    assert "--rvf-run-dir" in child and "--repo" in child and "/repo" in child
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 1
    assert recorded[0]["argv"][:4] == [
        "new-session",
        "-d",
        "-s",
        "rvf-dispatch-rvf-disp-unit",
    ]


def test_dispatch_reviewers_detached_exports_codex_rvf_log_root(root: Path) -> None:
    """FU-3：detached 派发把 ``ledger.env()``（含 CODEX_RVF_LOG_ROOT）显式写进 tmux 内层
    wrapper shell 的 ``export X=Y;`` 行——reviewer 子进程不再依赖 tmux server 的 env 继承，
    其 diff-tracker DB 与 prepare 写 lease 的库一致，消除 lease_not_found。"""
    import contextlib
    import io

    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_tmux = write_fake_tmux_script(root / "tmux.py")
    calls = root / "calls.jsonl"
    run_dir = root / "runs" / "rvf-disp-unit"
    reviewers_dir = run_dir / "artifacts" / "reviewers"
    reviewers_dir.mkdir(parents=True, exist_ok=True)
    log_root = root / "rvf-log-root"

    class _Ledger:
        run_id = "rvf-disp-unit"

        def __init__(self, rd: Path) -> None:
            self.run_dir = rd

        def env(self) -> dict:
            # 模拟 RunLedger.env()：含决定 diff-tracker DB 落点的 CODEX_RVF_LOG_ROOT。
            return {
                "CODEX_RVF_RUN_ID": self.run_id,
                "CODEX_RVF_LOG_ROOT": str(log_root),
                "CODEX_RVF_RUN_DIR": str(self.run_dir),
            }

        def event(self, **_kw) -> None:
            pass

    args = argparse.Namespace(
        registry="reg.json",
        probe_mode="preflight",
        probe_timeout=60.0,
        main_harness="auto",
        transcript=None,
        main_harness_file=None,
        assume_available=None,
        require_external=False,
        total_timeout=2700.0,
    )
    saved = {
        k: os.environ.get(k)
        for k in ("CODEX_RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")
    }
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = d.launch_detached_dispatch(
                args,
                _Ledger(run_dir),
                reviewers_dir,
                repo="/repo",
                review_packet="/pkt",
                session_context="/sow",
                scope_contract="/sc",
            )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert rc == 0, buf.getvalue()
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 1
    # tmux new-session -d -s <name> <shell>：内层 wrapper shell 是最后一个参数。
    shell_command = recorded[0]["argv"][-1]
    # 关键断言：CODEX_RVF_LOG_ROOT 必须以正确的值显式 export 进内层 shell。
    assert (
        f"export CODEX_RVF_LOG_ROOT={shlex.quote(str(log_root))};" in shell_command
    ), shell_command
    assert f"export CODEX_RVF_RUN_DIR={shlex.quote(str(run_dir))};" in shell_command


def test_dispatch_reviewers_wait_status_branches(root: Path) -> None:
    """waiter 终态判定：running / done(finished_at) / done(launch_failed) / 缺文件→running。"""
    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    status_path = root / "s.json"

    status_path.write_text(
        json.dumps({"launch_status": "launched", "finished_at": None, "returncode": None}),
        encoding="utf-8",
    )
    running = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert running["state"] == "running", running

    status_path.write_text(
        json.dumps({"launch_status": "launched", "finished_at": "t1", "returncode": 0}),
        encoding="utf-8",
    )
    done = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert done["state"] == "done" and done["returncode"] == 0, done

    status_path.write_text(
        json.dumps({"launch_status": "launch_failed", "finished_at": None, "error": "boom"}),
        encoding="utf-8",
    )
    failed = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert failed["state"] == "done" and failed["launch_status"] == "launch_failed", failed

    missing = d.wait_for_dispatch_status(root / "missing.json", max_wait=0.0)
    assert missing["state"] == "running", missing


def test_dispatch_reviewers_routing_matrix(root: Path) -> None:
    """路由矩阵 R0–R4：测试用例名 ↔ 规则 id。

    | 场景 | M | A | rule | slots |
    |------|---|---|------|-------|
    | R0 main=claude | claude_code | cursor,claude_code,codex | R0 | cursor,codex |
    | R0 main=codex  | codex       | cursor,claude_code,codex | R0 | cursor,claude_code |
    | R0 main=cursor(override) | cursor | cursor,claude_code,codex | R0 | claude_code,codex |
    | R1 cursor+codex | claude_code | cursor,codex | R1 | cursor,codex |
    | R1 no-cursor (R4) | claude_code | claude_code,codex | R1 | claude_code,codex + cursor_unavailable |
    | R2 only==M | codex | codex | R2 | codex-cli-a,codex-cli-b |
    | R2 only!=M | claude_code | codex | R2 | codex-cli-a,codex-cli-b + mismatch |
    | R3 zero | claude_code | (none) | R3 | needs_last_resort_fallback |
    """
    d = load_dispatch_reviewers_module()
    reg = _dispatch_registry()

    def harnesses(plan):
        return [r["harness_id"] for r in plan["reviewers"]]

    def warns(plan):
        return {w["code"] for w in plan["warnings"]}

    # R0 — 默认两路非主，cursor 必选一腿
    p = d.route("claude_code", ["cursor", "claude_code", "codex"], reg)
    assert p["routing_rule"] == "R0", p
    assert harnesses(p) == ["cursor", "codex"], p
    assert all(r["dispatch_mode"] == "external_cli" for r in p["reviewers"]), p
    assert p["status"] == "planned" and p["needs_last_resort_fallback"] is False

    p = d.route("codex", ["cursor", "claude_code", "codex"], reg)
    assert p["routing_rule"] == "R0" and harnesses(p) == ["cursor", "claude_code"], p

    # R0 主=cursor（仅显式覆盖可达）→ 两路非主，cursor 不占 slot
    p = d.route("cursor", ["cursor", "claude_code", "codex"], reg)
    assert p["routing_rule"] == "R0" and set(harnesses(p)) == {"claude_code", "codex"}, p

    # R1 — 恰两路 external（含 cursor）
    p = d.route("claude_code", ["cursor", "codex"], reg)
    assert p["routing_rule"] == "R1" and set(harnesses(p)) == {"cursor", "codex"}, p

    # R1 + R4 — cursor 不可用：仍两路 external（主以 external 跑），记 cursor_unavailable
    p = d.route("claude_code", ["claude_code", "codex"], reg)
    assert p["routing_rule"] == "R1" and set(harnesses(p)) == {"claude_code", "codex"}, p
    assert "cursor_unavailable" in warns(p), p
    assert all(r["dispatch_mode"] == "external_cli" for r in p["reviewers"]), p

    # R2 — 同 harness 双 external，only==M（info）
    p = d.route("codex", ["codex"], reg)
    assert p["routing_rule"] == "R2", p
    assert [r["reviewer_id"] for r in p["reviewers"]] == ["codex-cli-a", "codex-cli-b"], p
    assert "only_main_harness_available" in warns(p), p
    assert "cursor_unavailable" not in warns(p), p  # R2 不叠加 R4

    # R2 — only!=M：必须 mismatch warning
    p = d.route("claude_code", ["codex"], reg)
    assert p["routing_rule"] == "R2" and "available_reviewer_harness_mismatch" in warns(p), p

    # R3 — 零可用：默认 needs_last_resort_fallback，不 fail
    p = d.route("claude_code", [], reg)
    assert p["routing_rule"] == "R3" and p["needs_last_resort_fallback"] is True, p
    assert p["status"] == "planned" and p["reviewers"] == [], p

    # R3 — require_external：fail-close
    p = d.route("claude_code", [], reg, require_external_only=True)
    assert p["status"] == "failed" and p.get("reason") == "no_reviewer_harness_available", p


def test_dispatch_reviewers_same_harness_double_instance_distinct_ids(root: Path) -> None:
    d = load_dispatch_reviewers_module()
    reg = _dispatch_registry()
    p = d.route("cursor", ["cursor"], reg)
    ids = [r["reviewer_id"] for r in p["reviewers"]]
    labels = [r["label"] for r in p["reviewers"]]
    assert len(set(ids)) == 2, ids
    assert ids == ["cursor-cli-a", "cursor-cli-b"], ids
    assert labels == [
        "alternative-reviewer:cursor-cli#a",
        "alternative-reviewer:cursor-cli#b",
    ], labels


def test_dispatch_reviewers_plan_artifact_schema(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    reg_path = root / "registry.json"
    reg_path.write_text(json.dumps(_dispatch_registry()), encoding="utf-8")
    run_dir = root / "run"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "dispatch_reviewers.py"),
            "--registry",
            str(reg_path),
            "--assume-available",
            "cursor,codex",
            "--main-harness",
            "codex",
            "--rvf-run-dir",
            str(run_dir),
            "--plan-only",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    plan_path = run_dir / "artifacts" / "reviewers" / "reviewer-plan.json"
    assert plan_path.exists(), completed.stdout
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for key in (
        "schema_version",
        "main_harness",
        "available_harnesses",
        "routing_rule",
        "reviewers",
        "warnings",
        "needs_last_resort_fallback",
        "fallbacks",
        "status",
    ):
        assert key in plan, (key, plan)
    assert plan["fallbacks"] == [], plan
    assert plan["status"] == "planned" and plan["main_harness"] == "codex"
    assert plan["routing_rule"] == "R1" and len(plan["reviewers"]) == 2
    for r in plan["reviewers"]:
        for key in ("slot", "harness_id", "dispatch_mode", "label", "config_path", "reviewer_id"):
            assert key in r, (key, r)
        assert r["dispatch_mode"] == "external_cli", r


def test_dispatch_reviewers_executes_two_external(root: Path) -> None:
    """fake CLI shim 并行双 external：两路 review-result.json 路径存在、reviewer_id 唯一不撞目录。"""
    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_cmd = [
        "bash",
        "-c",
        'cat >/dev/null; python3 "$RVF_WRITE_REVIEW_RESULT" no-issues '
        '--out "$RVF_REVIEW_RESULT" --audit-summary "fake $RVF_REVIEWER_ID reviewed scope"',
    ]
    reg = {"schema_version": 1, "harnesses": {}}
    for hid in ("alpha", "beta"):
        config_path = root / f"alt-{hid}.json"
        config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "label": f"alternative-reviewer:{hid}-cli",
                    "command": fake_cmd,
                    "allow_repo_cwd": True,
                    "pre_run_health": False,
                    "output_format": "text",
                }
            ),
            encoding="utf-8",
        )
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(config_path),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": 100 if hid == "alpha" else 50,
        }
    plan = d.route("codex", ["alpha", "beta"], reg)
    assert plan["routing_rule"] == "R1" and len(plan["reviewers"]) == 2
    run_dir = root / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = root / "packet.md"
    packet.write_text("## Review Packet\n\nintegration test\n", encoding="utf-8")
    plan = d.execute_plan(
        plan,
        repo=None,
        review_packet=str(packet),
        session_context=None,
        scope_contract=None,
        run_id="dispatch-exec-test",
        run_dir=str(run_dir),
        artifacts_dir=artifacts_dir,
    )
    assert plan["status"] == "completed", plan
    reviewer_dirs = sorted(p.name for p in (artifacts_dir / "reviewers").iterdir() if p.is_dir())
    assert reviewer_dirs == ["alpha-cli", "beta-cli"], reviewer_dirs
    seen_paths = set()
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        result_path = Path(r["review_result_path"])
        assert result_path.exists(), r
        seen_paths.add(str(result_path))
    assert len(seen_paths) == 2, seen_paths


def test_dispatch_reviewers_execute_backfills_review_env(root: Path) -> None:
    """回归（RVF-001）：`source review-env.sh; dispatch_reviewers.py --execute` 不带显式
    --repo/--review-packet/--session-context 时，应从 review-env.sh 导出的
    RVF_REPO / RVF_REVIEW_PACKET / RVF_SCOPE_OF_WORK 回填，否则子进程 reviewer 因缺参失败。"""
    root.mkdir(parents=True, exist_ok=True)
    fake_cmd = [
        "bash",
        "-c",
        'cat >/dev/null; python3 "$RVF_WRITE_REVIEW_RESULT" no-issues '
        '--out "$RVF_REVIEW_RESULT" --audit-summary "env-backfill ok"',
    ]
    reg = {"schema_version": 1, "harnesses": {}}
    for hid in ("alpha", "beta"):
        config_path = root / f"alt-{hid}.json"
        config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "label": f"alternative-reviewer:{hid}-cli",
                    "command": fake_cmd,
                    "allow_repo_cwd": True,
                    "pre_run_health": False,
                    "output_format": "text",
                }
            ),
            encoding="utf-8",
        )
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(config_path),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": 100 if hid == "alpha" else 50,
        }
    reg_path = root / "registry.json"
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    packet = root / "packet.md"
    packet.write_text("## Review Packet\n\nenv backfill\n", encoding="utf-8")
    sow = root / "scope-of-work.md"
    sow.write_text("## scope\n", encoding="utf-8")
    run_dir = root / "run"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_RVF_")}
    # emulate `source review-env.sh`: packet/scope only via env, NOT CLI args.
    # (RVF_REPO omitted — review-packet alone is sufficient for the reviewer kernel;
    # setting it to a non-git tmp dir would trip check_repo. The env-backfill code path
    # is identical for repo/packet/scope.)
    env["RVF_REVIEW_PACKET"] = str(packet)
    env["RVF_SCOPE_OF_WORK"] = str(sow)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "dispatch_reviewers.py"),
            "--registry",
            str(reg_path),
            "--main-harness",
            "codex",
            "--assume-available",
            "alpha,beta",
            "--rvf-run-id",
            "env-backfill",
            "--rvf-run-dir",
            str(run_dir),
            "--execute",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    plan = json.loads(
        (run_dir / "artifacts" / "reviewers" / "reviewer-plan.json").read_text(encoding="utf-8")
    )
    assert plan["status"] == "completed", plan
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        assert Path(r["review_result_path"]).exists(), r


def test_dispatch_reviewers_reroutes_on_usage_limit(tmp_path: Path) -> None:
    """alpha 撞额度(125) → 轮内 reroute 到 gamma：status completed、fallbacks 记录、cooldown 落盘。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("usage", 100), "beta": ("clean", 90), "gamma": ("clean", 80)}
    )
    plan = d.route("codex", ["alpha", "beta", "gamma"], reg)
    assert plan["routing_rule"] == "R0", plan
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="reroute-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta", "gamma"],
        )
    assert plan["status"] == "completed", plan
    assert "alpha" in plan["cooldown_recorded"], plan
    assert (cooldown_root / "harness-alpha.json").exists(), list(cooldown_root.glob("*"))
    assert len(plan["fallbacks"]) == 1, plan["fallbacks"]
    fb = plan["fallbacks"][0]
    assert fb["from"] == "alpha" and fb["to"] == "gamma", fb
    assert len(plan["reviewers"]) == 2
    assert {r["harness_id"] for r in plan["reviewers"]} == {"beta", "gamma"}
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        assert Path(r["review_result_path"]).exists(), r


def test_dispatch_reviewers_reroute_id_collision(tmp_path: Path) -> None:
    """两腿均撞额度、只剩一个 eligible harness → 两替换 leg id 不碰撞(-fb1/-fb2)、两份 artifact 都在。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("usage", 100), "beta": ("usage", 90), "gamma": ("clean", 80)}
    )
    plan = d.route("codex", ["alpha", "beta", "gamma"], reg)
    assert plan["routing_rule"] == "R0"
    assert {r["harness_id"] for r in plan["reviewers"]} == {"alpha", "beta"}, plan["reviewers"]
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="collision-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta", "gamma"],
        )
    assert plan["status"] == "completed", plan
    assert {"alpha", "beta"} <= set(plan["cooldown_recorded"]), plan
    assert len(plan["fallbacks"]) == 2, plan["fallbacks"]
    ids = sorted(r["reviewer_id"] for r in plan["reviewers"])
    assert ids == ["gamma-cli-fb1", "gamma-cli-fb2"], ids
    seen = set()
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        assert Path(r["review_result_path"]).exists(), r
        seen.add(r["review_result_path"])
    assert len(seen) == 2, seen


def test_dispatch_reviewers_probe_excludes_cooldown(tmp_path: Path) -> None:
    """tmp root 预置 alpha 冷却 → 真实 probe 排除 alpha → route 落他者 + harness_limit_cooldown_active 警告。"""
    cd = load_harness_limit_cooldown_module()
    load_dispatch_reviewers_module()  # 确保 SCRIPT_DIR 在 sys.path
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg_path = tmp_path / "registry.json"
    reg = {"schema_version": 1, "harnesses": {}}
    for hid, prio in (("alpha", 100), ("beta", 50)):
        cfg = tmp_path / f"alt-{hid}.json"
        write_alternative_reviewer_config(
            cfg,
            [sys.executable, "-c", "pass"],
            idle_timeout_seconds=5.0,
            activity_check_interval_seconds=0.05,
            output_format="text",
            health_command=[sys.executable, "-c", ""],
            pre_run_health=True,
        )
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(cfg),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": prio,
        }
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    with _with_cooldown_env(cooldown_root):
        # env 已指向 cooldown_root（无 SUBDIR）；用 env 路径 record，确保子进程读同一处。
        cd.record("alpha", reason="usage_limit_exhausted")
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "dispatch_reviewers.py"),
                "--registry",
                str(reg_path),
                "--main-harness",
                "codex",
                "--probe-mode",
                "preflight",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            env=dict(os.environ),
            check=False,
        )
    assert "harness_limit_cooldown_active" in completed.stderr, completed.stderr
    plan = json.loads(completed.stdout)
    assert "alpha" not in plan["available_harnesses"], plan
    assert "beta" in plan["available_harnesses"], plan


def test_dispatch_reviewers_failclose_when_main_exhausted(tmp_path: Path) -> None:
    """external 补不上 + 主 harness 耗尽 → status=failed + main_harness_usage_limit_exhausted，不置伪 R3。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("usage", 100), "beta": ("clean", 90)}
    )
    plan = d.route("alpha", ["alpha", "beta"], reg)
    assert plan["routing_rule"] == "R1", plan
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="failclose-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="alpha",
            available=["alpha", "beta"],
        )
    assert plan["status"] == "failed", plan
    assert plan.get("reason") == "main_harness_usage_limit_exhausted", plan
    assert plan["needs_last_resort_fallback"] is False, plan
    assert any(w["code"] == "main_harness_usage_limit_exhausted" for w in plan["warnings"]), plan["warnings"]
    assert "alpha" in plan["cooldown_recorded"], plan

