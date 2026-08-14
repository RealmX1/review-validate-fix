#!/usr/bin/env python3
"""每个 RVF dispatch token 的**持久终态**：`consumed`（agent 收到了）或 `withdrawn`（已撤回）。

## 为什么需要它

RVF 把一条 follow-up 交给 cline-kanban 之后，投递可能**迟到很久**（实测 49 分钟）。于是有一个
必然的竞态：RVF 这边已经判定「上一条丢了」并重投/放弃，而旧的那条随后才落地。落地时它是
**完全可执行**的——注入正文自包含（第一行就是 ``$review-validate-fix``），agent 会照跑一次
无锁、无 lease、无 scope 契约的 RVF，还可能与新一轮并发撞车。

历史设计打算靠「撤回时把 prep 文件删掉」来止血，但那**结构上不可判定**：撤回删 prep 与 UPS
正常消费 prep，留在磁盘上的都是「文件缺失」，agent 无从区分。所以这里写的是**正向 tombstone**
——一条明确说「这个 token 已被撤回」的记录，而不是一个缺失。

## CAS：谁先谁赢

``claim_dispatch_token_terminal_state`` 先把**完整内容**写进同目录的临时文件，再用 ``os.link``
把它挂到最终路径上（内核级互斥：目标已存在即 ``EEXIST``）：

- UPS 在消费 token **之前**抢 ``consumed``：抢到即这条投递赢了，之后任何超时逻辑都不得再把它
  当「未投递」撤回（应改走 active-run cancel）；
- 超时侧抢 ``withdrawn``：抢到才执行物理取消 + 清理；抢不到且既有态是 ``consumed``，说明投递
  其实已经落地，**不能**撤回。

⚠️ **为什么不是「``O_EXCL`` 建空文件再写内容」**：那两步之间有一个**半写窗口**——并发方在窗口
里读到的是空文件，``_read_payload`` 返回 None，于是判成「不可信记录」走 unlink 重建分支，把别人
正在写的那条抢走。结果是两个**状态相反**的 claimant 都拿到 ``won=True``，先到先得当场失效。
「先写满、再原子挂名」把「文件出现」与「内容完整」合并成一个事件，该窗口结构上不存在。

⚠️ **``won=True`` 只表示「本次调用写下了这个终态」**。同一个 state 被再抢一次**不自动算赢**：
两个并发的撤回者若都拿到 ``won=True``，就会各自跑一遍 message-cancel / 放 lease / 清锁，慢的那个
可能在第一个已放行新一轮之后误删**新一轮**的锁与 pending。只有能**证明是同一逻辑操作的重入**
（同一个显式 ``claim_id``，或没有 ``claim_id`` 时的同一个进程身份）才幂等地算赢，且此时
``reentrant=True``、并未真的重写文件。

终态写一次即定，与 cline-kanban 侧「终态写一次即定」的保证同构，两边的竞态因此都是确定性的。

## TTL

默认 6 小时，覆盖「最长可信迟到窗口」。计划原文一度写「2 分钟」——那远远不够：实测已迟到
49 分钟，2 分钟的 tombstone 等于没有。TTL 过期后 tombstone 失效（视为无记录），由 O_EXCL 抢占
覆盖；同时每次写入顺带清一次过期文件，避免目录无界增长。

## 落盘位置

``$RVF_HOME``（默认 ``~/.rvf``）下的 ``rvf-withdrawn-dispatch-tokens/``——与
``kanban-followup-in-progress`` / ``kanban-followup-dispatched`` 并列，同属 ``RVF_HOME`` 家族，
**不在状态根（log root）迁移范围内**。

刻意**不**跟随 prep 文件根：prep 是有意短命的（TTL 15min、会被 sweep，默认还落在
``$XDG_RUNTIME_DIR`` / ``/tmp``），而 tombstone 必须**活得比它久**——「prep 已经不在了」正是
撤回与正常消费共有的表征，本模块的存在意义就是给出一条比它更长命的正向记录。

⚠️ **测试须经 ``RVF_HOME``（或本模块专用 env）隔离，绝不写真实 ``~/.rvf``**：Stop hook 与 UPS
两侧都不显式传 root。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SUBDIR_NAME = "rvf-withdrawn-dispatch-tokens"
TOMBSTONE_VERSION = 1
ROOT_ENV = "RVF_WITHDRAWN_DISPATCH_TOMBSTONE_ROOT"
#: ``RVF_HOME`` 家族的公共根 env（决策 token store / 注入 ledger / kanban-followup 锁同源）。
#: 认它是**测试隔离的关键**：两侧调用点都不显式传 root，各测试靠这一个 env 一次性隔离整族。
RVF_HOME_ENV = "RVF_HOME"

#: agent 已经收到了这条投递（UPS 在消费前抢下的态）。
STATE_CONSUMED = "consumed"
#: 这条投递已被 RVF 主动撤回，迟到落地时必须被中和。
STATE_WITHDRAWN = "withdrawn"
KNOWN_STATES = frozenset({STATE_CONSUMED, STATE_WITHDRAWN})

#: 「最长可信迟到窗口」。实测已迟到 49 分钟，故以小时计而不是分钟计。
DEFAULT_TTL_SECONDS = 6 * 60 * 60
TTL_ENV = "RVF_WITHDRAWN_DISPATCH_TOMBSTONE_TTL_SECONDS"

#: 每次写入顺带清理的过期文件数上界（避免在 Stop 的秒级预算里扫一个巨大目录）。
LAZY_SWEEP_MAX_FILES = 64

_TOKEN_RE = re.compile(r"^[0-9A-Fa-f]{6,64}$")

#: 进程内稳定、跨进程互不相同的随机量。**不能只用 PID**：PID 会被复用，而 tombstone 的 TTL 有
#: 6 小时，足够一个新进程拿到旧进程的 PID，从而把两个毫不相干的撤回者误判成「同一逻辑操作的
#: 重入」。与当前 PID 组合后，fork 出来的子进程也能与父进程区分开。
_PROCESS_CLAIM_NONCE = uuid.uuid4().hex


def _process_claim_identity() -> str:
    """本进程的 claim 身份（调用方没给 ``claim_id`` 时的默认重入证明）。"""
    return f"pid-{os.getpid()}-{_PROCESS_CLAIM_NONCE}"


@dataclass(frozen=True)
class DispatchTokenTerminalStateClaim:
    """``claim_dispatch_token_terminal_state`` 的结果。

    ``won`` = **本次调用**写下了终态（唯一例外见 ``reentrant``）；``existing_state`` = 输给了谁
    （``won`` 为 True 时为 None）。
    ``applicable`` = False 表示这道机制在当前条件下用不了（token 形状不合法、文件系统报错），
    此时调用方应**按无 tombstone 处理**——它是止血机制，不是安全边界，绝不能把主流程堵死。
    ``reentrant`` = 这是同一逻辑操作对**自己已写下的**终态的重入（幂等算赢，但并没有真的重写）。
    """

    won: bool
    applicable: bool
    existing_state: str | None
    existing_payload: dict[str, Any] | None
    path: Path | None
    reentrant: bool = False


def root(override: Path | str | None = None) -> Path:
    """显式参数 > 专用 env > ``$RVF_HOME`` > ``~/.rvf``，其下固定子目录。

    第三档不可省：Stop hook 与 UPS 都不显式传 root，而两侧的测试 harness 已经在用 ``RVF_HOME``
    隔离同族的决策 token store / 注入 ledger。少认这一档，本模块就会在测试里写进真实 ``~/.rvf``
    （live infra）——本仓有过同类前科。
    """
    if override is not None:
        return Path(override).expanduser() / SUBDIR_NAME
    raw = os.environ.get(ROOT_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser()
    home = os.environ.get(RVF_HOME_ENV)
    if home and home.strip():
        return Path(home).expanduser() / SUBDIR_NAME
    return Path.home() / ".rvf" / SUBDIR_NAME


def ttl_seconds() -> float:
    raw = os.environ.get(TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TTL_SECONDS)
    return max(0.0, value)


def _normalized_token(token: Any) -> str | None:
    if not isinstance(token, str):
        return None
    text = token.strip()
    if not text or not _TOKEN_RE.match(text):
        return None
    return text.lower()


def tombstone_path(token: str, *, root_override: Path | str | None = None) -> Path | None:
    normalized = _normalized_token(token)
    if normalized is None:
        return None
    return root(root_override) / f"token-{normalized}.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _expired(payload: dict[str, Any] | None, *, now_ts: float | None = None) -> bool:
    if not isinstance(payload, dict):
        return True
    expires = _parse_iso_ts(payload.get("expires_at"))
    if expires is None:
        return True
    current = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    return current > expires


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _lazy_sweep_expired(base: Path) -> None:
    """顺带清掉已过期的 tombstone（有界、吞错）。目录无界增长不是安全问题，但也不该放着不管。"""
    try:
        paths = sorted(base.glob("token-*.json"))[:LAZY_SWEEP_MAX_FILES]
    except OSError:
        return
    for path in paths:
        payload = _read_payload(path)
        if payload is not None and not _expired(payload):
            continue
        try:
            path.unlink()
        except OSError:
            continue


#: ``_install_tombstone_exclusively`` 的三种结局。
_INSTALL_WON = "won"
_INSTALL_ALREADY_EXISTS = "already_exists"
_INSTALL_UNUSABLE = "unusable"


def _install_tombstone_exclusively(path: Path, encoded: str) -> str:
    """把**完整内容**排他地安装到 ``path``：先写满临时文件，再 ``os.link`` 原子挂名。

    关键在于最终路径上**从不出现半成品**：``link`` 要么让一条内容完整的记录整体出现，要么因目标
    已存在而失败。旧写法（``O_EXCL`` 建空文件 → 再写内容）在两步之间留下一个窗口，并发方在窗口里
    读到空文件会判成「不可信记录」并把它 unlink 重建，于是两个状态相反的 claimant 同时算赢。
    """
    tmp_path = path.parent / f".claim-tmp-{path.stem}-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return _INSTALL_UNUSABLE
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
        except OSError:
            return _INSTALL_UNUSABLE
        try:
            os.link(str(tmp_path), str(path))
        except FileExistsError:
            return _INSTALL_ALREADY_EXISTS
        except OSError:
            # 硬链接不可用（异种文件系统等）：如实降级成「机制此刻用不了」，而不是退回那个有
            # 半写窗口的写法——止血机制宁可缺席，也不能给出错误的仲裁结果。
            return _INSTALL_UNUSABLE
        return _INSTALL_WON
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _is_same_logical_claimant(existing: dict[str, Any], *, claim_id: str | None) -> bool:
    """既有记录是不是**本次调用所属的那个逻辑操作**自己写下的？

    只有这一种情况才允许同态重复 claim 幂等地算赢。两个并发的撤回者不满足它——那正是要挡的。
    """
    recorded_claim_id = existing.get("claim_id")
    if claim_id is not None:
        return isinstance(recorded_claim_id, str) and recorded_claim_id == claim_id
    if recorded_claim_id is not None:
        # 既有记录属于一个具名逻辑操作，而本次调用没自称是它 ⇒ 不是同一件事。
        return False
    identity = existing.get("claimed_by_process_identity")
    return isinstance(identity, str) and identity == _process_claim_identity()


def claim_dispatch_token_terminal_state(
    token: Any,
    *,
    state: str,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    claim_id: str | None = None,
    root_override: Path | str | None = None,
) -> DispatchTokenTerminalStateClaim:
    """给这个 dispatch token 抢一个终态（先到先得，且**内容完整地**原子落盘）。

    ``won=True`` 表示**本次调用**写下了这个终态。唯一例外是**同一逻辑操作的重入**：既有记录是
    同一个 state，且能证明就是自己写的（同一个显式 ``claim_id``；没给 ``claim_id`` 时则是同一个
    进程身份），此时幂等算赢并带 ``reentrant=True``。两个并发的撤回者互不满足这个条件，因此不会
    双双开始清理。
    已过期的既有 tombstone 视为不存在，可被覆盖。
    """
    if state not in KNOWN_STATES:
        raise ValueError(f"unknown dispatch token terminal state: {state!r}")
    path = tombstone_path(token, root_override=root_override)
    if path is None:
        return DispatchTokenTerminalStateClaim(
            won=False, applicable=False, existing_state=None, existing_payload=None, path=None
        )
    ttl = ttl_seconds()
    payload = {
        "tombstone_version": TOMBSTONE_VERSION,
        "state": state,
        "token": _normalized_token(token),
        "reason": reason,
        "claimed_at": _iso_now(),
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=ttl)
        ).isoformat().replace("+00:00", "Z"),
        "ttl_seconds": ttl,
        "claimed_by_pid": os.getpid(),
        # 重入证明的载体：``claim_id`` 是调用方显式声明的逻辑操作身份，进程身份是它缺省时的兜底。
        "claim_id": claim_id,
        "claimed_by_process_identity": _process_claim_identity(),
        "details": dict(details) if details else None,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return DispatchTokenTerminalStateClaim(
            won=False, applicable=False, existing_state=None, existing_payload=None, path=path
        )
    for _ in range(2):
        outcome = _install_tombstone_exclusively(path, encoded)
        if outcome == _INSTALL_WON:
            _lazy_sweep_expired(path.parent)
            return DispatchTokenTerminalStateClaim(
                won=True, applicable=True, existing_state=None, existing_payload=None, path=path
            )
        if outcome == _INSTALL_UNUSABLE:
            return DispatchTokenTerminalStateClaim(
                won=False, applicable=False, existing_state=None, existing_payload=None, path=path
            )
        # 已经有人挂上了名字。因为安装是原子的，这里读到的要么是一条完整记录，要么是外力造成的
        # 残骸——不会再是「某个 claimant 正写到一半」。
        existing = _read_payload(path)
        if existing is not None and not _expired(existing):
            existing_state = existing.get("state")
            if existing_state == state and _is_same_logical_claimant(existing, claim_id=claim_id):
                # 同一逻辑操作的重入：幂等地算赢，别把自己判负；但没有重写文件。
                return DispatchTokenTerminalStateClaim(
                    won=True,
                    applicable=True,
                    existing_state=None,
                    existing_payload=None,
                    path=path,
                    reentrant=True,
                )
            return DispatchTokenTerminalStateClaim(
                won=False,
                applicable=True,
                existing_state=(
                    existing_state if isinstance(existing_state, str) else None
                ),
                existing_payload=existing,
                path=path,
            )
        # 过期 / 内容不可信 → 允许覆盖，再抢一次。
        try:
            path.unlink()
        except FileNotFoundError:
            # 已经被别人（或 lazy sweep）清掉了 —— 直接重试抢占即可。
            continue
        except OSError:
            return DispatchTokenTerminalStateClaim(
                won=False,
                applicable=False,
                existing_state=None,
                existing_payload=existing,
                path=path,
            )
        continue
    return DispatchTokenTerminalStateClaim(
        won=False, applicable=False, existing_state=None, existing_payload=None, path=path
    )


def read_active_dispatch_token_terminal_state(
    token: Any,
    *,
    root_override: Path | str | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """读这个 token 的**未过期**终态记录；没有 / 已过期 / 不可读一律 None。"""
    path = tombstone_path(token, root_override=root_override)
    if path is None:
        return None
    payload = _read_payload(path)
    if payload is None or _expired(payload, now_ts=now_ts):
        return None
    if payload.get("state") not in KNOWN_STATES:
        return None
    payload.setdefault("_tombstone_path", str(path))
    return payload


def dispatch_token_was_withdrawn(
    token: Any,
    *,
    root_override: Path | str | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """这条投递是否已被撤回（是则返回 tombstone payload，否则 None）。"""
    payload = read_active_dispatch_token_terminal_state(
        token, root_override=root_override, now_ts=now_ts
    )
    if payload is not None and payload.get("state") == STATE_WITHDRAWN:
        return payload
    return None


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "KNOWN_STATES",
    "ROOT_ENV",
    "STATE_CONSUMED",
    "STATE_WITHDRAWN",
    "SUBDIR_NAME",
    "TTL_ENV",
    "DispatchTokenTerminalStateClaim",
    "claim_dispatch_token_terminal_state",
    "dispatch_token_was_withdrawn",
    "read_active_dispatch_token_terminal_state",
    "root",
    "tombstone_path",
    "ttl_seconds",
]
