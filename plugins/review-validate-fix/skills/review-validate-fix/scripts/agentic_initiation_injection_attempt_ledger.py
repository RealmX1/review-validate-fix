#!/usr/bin/env python3
"""Agentic-initiation 决策点「注入尝试」持久化 ledger（RVF overhaul S4 / 计划 §三⑥·§五.3）。

RVF overhaul 把 Stop hook 降级为决策点信使：每当 Stop hook 走到「候选摘要非空」这一步，它要么
铸 token + 注入决策提示，要么因去重/declined 冷却/铸造熔断/已有活跃 token 而**静默停**。无论哪
一种结局，本 ledger 都要记一条——它是 canary 分母的持久化真相源：

  * **负路径不变量**（计划 §五.3，评审共识）：``候选非空 ⇒ 本 ledger 必有一条记录``。若候选非空
    却既没注入、也没在本 ledger 留痕，就是「静默吞掉一次可审改动」的发起层静默故障——测试即红。
  * **S8 守护 canary 对账**：守护 agent 用 ``Stop 事件数 vs 注入尝试数 vs RVF 完成数`` 三者对账，
    发现「发起层疑似静默死亡」。因此本 ledger 必须**跨 run 持久化**（非 per-run RunLedger 事件），
    默认落在 ``$RVF_HOME``/``~/.rvf`` 下，与决策 token store 同一状态根家族。

落盘根：显式参数 > 专用环境变量 ``RVF_AGENTIC_INITIATION_INJECTION_ATTEMPT_LEDGER_ROOT`` >
``$RVF_HOME``/``~/.rvf`` 下固定子目录。**测试须用 tmp + HOME/RVF_HOME 隔离，绝不写真实
``~/.rvf``**（Stop hook 侧默认不显式传 root，故测试靠 ``RVF_HOME`` 环境变量隔离）。

一条记录一行 JSONL，按 ``worktree`` 分片文件（避免所有 worktree 抢同一文件的写竞争）。
记录字段（过度指定命名，语义自解释）：``recorded_at`` / ``worktree`` / ``session_id`` /
``candidate_fingerprint`` / ``candidate_path_count`` / ``has_committed_but_unreviewed`` /
``mint_outcome``（injected|deduplicated_pending|declined_cooldown|declined_session_scope_snooze|
circuit_breaker_tripped 等）/
``decision_token``（铸成才有）/ ``injected``（本次是否真的发出了注入 block）/ ``note``。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets  # noqa: F401 — 预留：未来分片文件名可能引入随机后缀避免热点
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

ENV_RVF_HOME = "RVF_HOME"
ENV_INJECTION_ATTEMPT_LEDGER_ROOT = "RVF_AGENTIC_INITIATION_INJECTION_ATTEMPT_LEDGER_ROOT"
INJECTION_ATTEMPT_LEDGER_DIRNAME = "agentic-initiation-injection-attempt-ledger"

# mint_outcome 取值（与 rvf_decision_token_store.MINT_OUTCOME_* 对齐 + S4 特有的静默结局）。
INJECTION_OUTCOME_INJECTED = "injected"  # 新铸 token 并发出注入 block
INJECTION_OUTCOME_DEDUPLICATED_PENDING = "deduplicated_pending"  # 同键活跃 token 已在等待，不重注入
INJECTION_OUTCOME_DECLINED_COOLDOWN = "declined_cooldown"  # 指纹处于 declined 冷却，静默停
# 本会话谢绝过这片工作范围、尚未出现工作边界（闸 2b），静默停
INJECTION_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE = "declined_session_scope_snooze"
# agent 在本会话把审查延后到某个条件（延后型谢绝），此后还没有新回合 / 提交（闸 2c），静默停
INJECTION_OUTCOME_DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY = (
    "deferred_until_condition_decline_awaiting_turn_boundary"
)
INJECTION_OUTCOME_CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"  # 铸造频率熔断，静默停
INJECTION_OUTCOME_MINT_ANOMALY = "mint_anomaly"  # 铸造抛异常（best-effort 兜底记录）
# 横幅已交给 cline-kanban 终端投递（带外），**这一刻 agent 还什么都没拿到**。
#
# 为什么必须单列一个取值：这条路上 ``injected`` 只能记 ``False``——记 True 就是谎报，canary 的
# 分子会被一批「投出去了但可能根本没落地」的记录污染，而那正是 canary 要发现的故障本身。
# 「agent 到底接到没有」的答案在 token 状态里（UPS 观测到 needle 才推进 ``injected``），两边靠
# ``decision_token`` 关联即可对账；刻意**不**在 UPS 侧再补一条记录，否则「候选非空的 Stop 次数」
# 这个分母会被搅浑。
INJECTION_OUTCOME_DELIVERED_OUT_OF_BAND = "delivered_out_of_band"

# 字段分隔符（ASCII Unit Separator，正常字段值不含）。
_KEY_FIELD_SEPARATOR = "\x1e"


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    if now.microsecond:
        return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _rvf_home() -> Path:
    configured = os.environ.get(ENV_RVF_HOME)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rvf"


def injection_attempt_ledger_root(root: str | Path | None = None) -> Path:
    """注入尝试 ledger 落盘根：显式参数 > 专用 env > ``$RVF_HOME``/``~/.rvf`` 下固定子目录。"""
    if root is not None:
        return Path(root).expanduser()
    configured = os.environ.get(ENV_INJECTION_ATTEMPT_LEDGER_ROOT)
    if configured:
        return Path(configured).expanduser()
    return _rvf_home() / INJECTION_ATTEMPT_LEDGER_DIRNAME


def _digest(*parts: str) -> str:
    joined = _KEY_FIELD_SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _ledger_path_for_worktree(worktree: str, root: str | Path | None) -> Path:
    return injection_attempt_ledger_root(root) / f"{_digest(worktree)}.jsonl"


def record_injection_attempt(
    *,
    worktree: str,
    session_id: str | None,
    candidate_fingerprint: str,
    candidate_path_count: int,
    has_committed_but_unreviewed: bool,
    mint_outcome: str,
    injected: bool,
    decision_token: str | None = None,
    note: str | None = None,
    root: str | Path | None = None,
) -> Path | None:
    """向注入尝试 ledger 追加一条记录（每当 Stop hook 走到「候选摘要非空」这一步都要调）。

    **best-effort**：任何 OSError 都吞掉并返回 None——发起层的可观测性记录**永不**阻断 Stop hook
    本身的主流程结果（宁可丢一条 canary 记录，也不让 ledger I/O 把整个 Stop hook 拖红）。
    """
    record = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": _utc_now_iso(),
        "worktree": worktree,
        "session_id": session_id,
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_path_count": candidate_path_count,
        "has_committed_but_unreviewed": has_committed_but_unreviewed,
        "mint_outcome": mint_outcome,
        "decision_token": decision_token,
        "injected": injected,
        "note": note,
    }
    path = _ledger_path_for_worktree(worktree, root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(path, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                fd = -1
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError:
        return None
    return path


def read_injection_attempts(
    *,
    worktree: str,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """读取某 worktree 的全部注入尝试记录（坏行跳过，best-effort）。供负路径不变量测试与 S8
    守护 canary 对账读取。"""
    path = _ledger_path_for_worktree(worktree, root)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


__all__ = [
    "SCHEMA_VERSION",
    "ENV_INJECTION_ATTEMPT_LEDGER_ROOT",
    "INJECTION_ATTEMPT_LEDGER_DIRNAME",
    "INJECTION_OUTCOME_INJECTED",
    "INJECTION_OUTCOME_DEDUPLICATED_PENDING",
    "INJECTION_OUTCOME_DECLINED_COOLDOWN",
    "INJECTION_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE",
    "INJECTION_OUTCOME_DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY",
    "INJECTION_OUTCOME_CIRCUIT_BREAKER_TRIPPED",
    "INJECTION_OUTCOME_MINT_ANOMALY",
    "INJECTION_OUTCOME_DELIVERED_OUT_OF_BAND",
    "injection_attempt_ledger_root",
    "record_injection_attempt",
    "read_injection_attempts",
]


def _main(argv: list[str] | None = None) -> int:
    """诊断 CLI：打印某 worktree 的注入尝试记录（best-effort，供人工/守护对账）。"""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True)
    args = parser.parse_args(argv)
    for record in read_injection_attempts(worktree=args.worktree):
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
