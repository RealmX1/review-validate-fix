#!/usr/bin/env python3
"""Agentic-initiation 决策 token 落盘存储（RVF overhaul S1 基建）。

背景：RVF「hook 发起 → agent 发起」overhaul 把发起决策权交给主 agent。Stop hook 降级为
「决策点信使」：它**铸造**一个决策 token 并把 token 注入给主 agent；主 agent 显式
``start`` 或 ``decline``。本模块是该 token 的**唯一落盘真相源**，被两类调用方共享：

  * Stop hook 侧：``mint_decision_token`` / ``mark_injected`` / ``is_fingerprint_in_declined_cooldown``；
  * rvf-agent CLI 侧（S3）：``acknowledge_decision_token`` / ``mark_dispatched`` /
    ``mark_declined`` / ``record_start_failure``（start --retry 回退）。

设计要点（实施计划 §四 S1 / §二 R24·R25）：

  * 状态机 ``minted → injected → acknowledged → dispatched | declined``，旁路 ``expired | lost``；
    ``acknowledged`` 为**非终态**（允许 scope 子命令与 start --retry，dispatched 前可从失败回退回
    acknowledged）；``dispatched`` / ``declined`` 为终态。非法迁移一律显式抛
    ``DecisionTokenStateError``。
  * 去重键 ``(worktree, session_id, candidate_fingerprint)``：同键存在活跃（非终态、且 TTL 未过期或
    run 在途可复活）token 时**拒绝重铸并返回既有 token**——这是注入幂等的硬保证（R25）。
    ``candidate_fingerprint`` 对本模块是**不透明字符串**（由 S2 摘要生成器计算，本模块不实现指纹算法）。
  * declined 指纹冷却表：decline 时把 ``(worktree, candidate_fingerprint)`` 写入冷却表，摘要生成器
    在再次注入前查表，指纹未变则不再注入（防 decline→Stop→再注入的 nag 循环）。
  * 铸造侧频率熔断（R24 裁决 b）：同 session、3600 秒滚动窗口内第 ≥3 次**铸造请求**即拒铸，写
    circuit-breaker-tripped ledger，并回结构化结果供 hook 出 systemMessage 告警。纯计数，零语义判断。
  * TTL 复活（参照 rvf_prep_file 的 FU-1 方案 B）：token 已过 TTL、但其锚定 run_dir 仍在磁盘（run
    在途）→ 就地续期为原状态而非判定 expired，避免半途丢弃在途审查。

所有落盘根目录既可用显式参数注入、也可用环境变量覆盖，测试须用 tmp 隔离，绝不写真实 ``~/.rvf``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
DEFAULT_DECISION_TOKEN_TTL_SECONDS = 900  # 15 分钟：足够主 agent 在同一 turn 内简析后 start/decline
DEFAULT_TOKEN_ALLOCATION_ATTEMPTS = 8
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")

# 铸造侧频率熔断参数（R24 裁决 b）：同 session 3600 秒窗口内第 ≥3 次铸造请求拒铸。
DEFAULT_MINT_CIRCUIT_BREAKER_WINDOW_SECONDS = 3600
DEFAULT_MINT_CIRCUIT_BREAKER_MAX_REQUESTS = 3

# 落盘根：显式参数 > 专用环境变量 > ``$RVF_HOME`` 或 ``~/.rvf`` 下的固定子目录。
ENV_RVF_HOME = "RVF_HOME"
ENV_DECISION_TOKEN_ROOT = "RVF_AGENTIC_INITIATION_DECISION_TOKEN_ROOT"
ENV_DECLINED_FINGERPRINT_ROOT = "RVF_AGENTIC_INITIATION_DECLINED_FINGERPRINT_ROOT"
ENV_CIRCUIT_BREAKER_ROOT = "RVF_AGENTIC_INITIATION_CIRCUIT_BREAKER_ROOT"

DECISION_TOKEN_DIRNAME = "agentic-initiation-decision-tokens"
DECLINED_FINGERPRINT_DIRNAME = "agentic-initiation-declined-fingerprints"
CIRCUIT_BREAKER_DIRNAME = "agentic-initiation-circuit-breaker-ledger"

# 去重键各字段的分隔符（ASCII Unit Separator，正常字段值不含）。
_KEY_FIELD_SEPARATOR = "\x1e"


# ---------------------------------------------------------------------------
# 生命周期状态机（决策状态，独立于 TTL）
# ---------------------------------------------------------------------------

STATE_MINTED = "minted"
STATE_INJECTED = "injected"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_DISPATCHED = "dispatched"
STATE_DECLINED = "declined"
STATE_EXPIRED = "expired"
STATE_LOST = "lost"

# 非终态且仍在决策进行中的活跃状态（去重、复活只认这三种）。
ACTIVE_NONTERMINAL_STATES = frozenset({STATE_MINTED, STATE_INJECTED, STATE_ACKNOWLEDGED})
# 决策已定的终态（dispatched=已派发，declined=已谢绝）。
TERMINAL_STATES = frozenset({STATE_DISPATCHED, STATE_DECLINED})
# 旁路终态（TTL 死亡 / 投递回执丢失）。
BYPASS_STATES = frozenset({STATE_EXPIRED, STATE_LOST})

# 合法迁移邻接表：key=当前状态，value=允许迁入的目标状态集合。
# acknowledged 自环 = start --retry 幂等重入（回退失败后再 start）。TTL 复活不是一次迁移
# （状态不变、只续期），故不出现在本表中，由 revive_decision_token 单独处理。
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_MINTED: frozenset({STATE_INJECTED, STATE_ACKNOWLEDGED, STATE_DECLINED, STATE_EXPIRED, STATE_LOST}),
    STATE_INJECTED: frozenset({STATE_ACKNOWLEDGED, STATE_DECLINED, STATE_EXPIRED, STATE_LOST}),
    STATE_ACKNOWLEDGED: frozenset({STATE_ACKNOWLEDGED, STATE_DISPATCHED, STATE_DECLINED, STATE_EXPIRED, STATE_LOST}),
    STATE_DISPATCHED: frozenset(),
    STATE_DECLINED: frozenset(),
    STATE_EXPIRED: frozenset(),
    STATE_LOST: frozenset(),
}

# 每个目标状态对应的落盘时间戳字段名（进入该状态时打戳）。
_STATE_TIMESTAMP_FIELD: dict[str, str] = {
    STATE_INJECTED: "injected_at",
    STATE_ACKNOWLEDGED: "acknowledged_at",
    STATE_DISPATCHED: "dispatched_at",
    STATE_DECLINED: "declined_at",
    STATE_EXPIRED: "expired_at",
    STATE_LOST: "lost_at",
}

# mint 结果的四种结局。
MINT_OUTCOME_MINTED = "minted"
MINT_OUTCOME_DEDUPLICATED = "deduplicated"
MINT_OUTCOME_DECLINED_COOLDOWN = "declined_cooldown"
MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class DecisionTokenError(ValueError):
    """决策 token 存储的基类异常。"""


class DecisionTokenStateError(DecisionTokenError):
    """非法状态迁移（如从终态再迁移、或跳过前置状态）。"""


class DecisionTokenExpiredError(DecisionTokenError):
    """token 已过 TTL 且不可复活（无在途 run 锚点），无法继续推进状态。"""


class DecisionTokenNotFoundError(DecisionTokenError):
    """token 文件缺失或损坏，无法读取。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionTokenRecord:
    """一枚落盘决策 token 的内存视图（token 值 + 文件路径 + 完整 payload）。"""

    token: str
    path: Path
    payload: dict[str, Any]

    @property
    def state(self) -> str:
        return str(self.payload.get("state"))


@dataclass(frozen=True)
class DecisionTokenLookup:
    """``read_decision_token`` 的读取结果。

    ``status`` 反映读取/TTL 结局：``valid`` / ``expired`` / ``missing`` / ``invalid_token`` /
    ``invalid_json`` / ``unreadable`` / ``invalid_payload`` / ``schema_mismatch`` /
    ``token_mismatch``。``state`` 是 payload 中记录的生命周期状态（与 TTL 正交）。
    ``revivable`` 表示「TTL 已过期但状态仍活跃且 run_dir 在途」——即可用 ``revive_decision_token`` 续期。
    """

    status: str
    token: str
    path: Path
    payload: dict[str, Any] | None = None
    state: str | None = None
    revivable: bool = False
    error: str | None = None


@dataclass(frozen=True)
class CircuitBreakerStatus:
    """铸造频率熔断的结构化结果，供 hook 生成 systemMessage 告警。"""

    tripped: bool
    request_count: int
    window_seconds: int
    max_requests: int
    session_id: str
    ledger_path: Path | None = None


@dataclass(frozen=True)
class MintResult:
    """``mint_decision_token`` 的结构化返回。

    ``outcome`` ∈ {``minted``, ``deduplicated``, ``declined_cooldown``,
    ``circuit_breaker_tripped``}。仅 ``minted`` / ``deduplicated`` 携带可用 token/record；
    ``declined_cooldown`` / ``circuit_breaker_tripped`` 表示拒铸（``record`` 为 None），
    调用方据 ``message`` 出用户告警。
    """

    outcome: str
    token: str | None = None
    record: DecisionTokenRecord | None = None
    circuit_breaker: CircuitBreakerStatus | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# 时间戳工具（与 rvf_prep_file 保持一致的 UTC ISO-8601「Z」格式）
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """把 datetime 归一化为 UTC ISO-8601、尾随 ``Z`` 的字符串。"""
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond:
        return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """解析 ISO-8601（含尾随 ``Z``）时间戳为带 UTC 时区的 datetime。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_now(now: datetime | None) -> datetime:
    return (now or utc_now()).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# token 值工具
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """生成一枚 16 位十六进制随机 token。"""
    return secrets.token_hex(8)


def validate_token(token: str) -> str:
    """校验并归一化 token（16 位小写十六进制），非法则抛 ``DecisionTokenError``。"""
    normalized = token.strip().lower()
    if not TOKEN_RE.fullmatch(normalized):
        raise DecisionTokenError(f"非法决策 token: {token!r}")
    return normalized


# ---------------------------------------------------------------------------
# 落盘根目录解析
# ---------------------------------------------------------------------------

def _rvf_home() -> Path:
    """RVF 状态根目录：``$RVF_HOME`` 或 ``~/.rvf``。"""
    configured = os.environ.get(ENV_RVF_HOME)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rvf"


def _resolve_root(explicit: str | Path | None, env_name: str, dirname: str) -> Path:
    """通用根目录解析：显式参数 > 专用环境变量 > ``$RVF_HOME``/``~/.rvf`` 下的固定子目录。"""
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured).expanduser()
    return _rvf_home() / dirname


def decision_token_root(root: str | Path | None = None) -> Path:
    """决策 token 落盘根目录。"""
    return _resolve_root(root, ENV_DECISION_TOKEN_ROOT, DECISION_TOKEN_DIRNAME)


def declined_fingerprint_root(root: str | Path | None = None) -> Path:
    """declined 指纹冷却表根目录。"""
    return _resolve_root(root, ENV_DECLINED_FINGERPRINT_ROOT, DECLINED_FINGERPRINT_DIRNAME)


def circuit_breaker_root(root: str | Path | None = None) -> Path:
    """铸造频率熔断 ledger 根目录。"""
    return _resolve_root(root, ENV_CIRCUIT_BREAKER_ROOT, CIRCUIT_BREAKER_DIRNAME)


def decision_token_path(token: str, root: str | Path | None = None) -> Path:
    """给定 token 的落盘文件路径。"""
    return decision_token_root(root) / f"{validate_token(token)}.json"


# ---------------------------------------------------------------------------
# 原子落盘工具（O_EXCL 创建 / replace 覆盖 / append），复用 rvf_prep_file 家族的语义
# ---------------------------------------------------------------------------

def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def _write_json_tmp(path: Path, payload: dict[str, Any]) -> Path:
    _ensure_parent_dir(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        return tmp
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    """O_EXCL 语义原子创建：目标已存在则抛 ``FileExistsError``（作为去重竞态互斥用）。"""
    tmp = _write_json_tmp(path, payload)
    try:
        os.link(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    tmp.unlink()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """replace 语义原子覆盖：目标存在则整份替换。"""
    tmp = _write_json_tmp(path, payload)
    try:
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """向 jsonl ledger 追加一行（0o600 文件）。"""
    _ensure_parent_dir(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 jsonl ledger 全部有效行（坏行跳过，best-effort）。"""
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


def _digest(*parts: str) -> str:
    """把若干字段拼接后取 sha256 十六进制摘要（用作安全文件名/去重键哈希）。"""
    joined = _KEY_FIELD_SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# run_dir 复活锚点（参照 rvf_user_prompt_submit._run_artifacts_present / FU-1 方案 B）
# ---------------------------------------------------------------------------

def _run_anchor_present(payload: dict[str, Any] | None) -> bool:
    """best-effort：token payload 锚定的 run_dir 是否仍在磁盘（run 在途）。

    只复活 run_dir 仍存在的过期 token，不复活已被清理 / 从未开工的过期 token。任何结构异常或
    OSError → False（保守不复活）。
    """
    if not isinstance(payload, dict):
        return False
    run_dir = payload.get("run_anchor_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return False
    try:
        return Path(run_dir).is_dir()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def read_decision_token(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenLookup:
    """读取一枚决策 token，返回其读取/TTL 结局与生命周期状态。

    对已过 TTL 的文件仍带回 payload 与 ``state``（供复活/旁路 mark 使用），并计算
    ``revivable``（TTL 过期 + 状态活跃 + run_dir 在途）。
    """
    try:
        normalized_token = validate_token(token)
    except DecisionTokenError as exc:
        return DecisionTokenLookup(status="invalid_token", token=token, path=decision_token_root(root), error=str(exc))

    path = decision_token_path(normalized_token, root)
    if not path.is_file():
        return DecisionTokenLookup(status="missing", token=normalized_token, path=path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return DecisionTokenLookup(status="invalid_json", token=normalized_token, path=path, error=str(exc))
    except OSError as exc:
        return DecisionTokenLookup(status="unreadable", token=normalized_token, path=path, error=str(exc))
    if not isinstance(payload, dict):
        return DecisionTokenLookup(status="invalid_payload", token=normalized_token, path=path, error="token 文件根须为对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        return DecisionTokenLookup(status="schema_mismatch", token=normalized_token, path=path, payload=payload)
    if payload.get("token") != normalized_token:
        return DecisionTokenLookup(status="token_mismatch", token=normalized_token, path=path, payload=payload)

    state = payload.get("state")
    expires_raw = payload.get("expires_at")
    if not isinstance(expires_raw, str):
        return DecisionTokenLookup(status="invalid_payload", token=normalized_token, path=path, payload=payload, error="缺 expires_at")
    try:
        expires_at = parse_timestamp(expires_raw)
    except ValueError as exc:
        return DecisionTokenLookup(status="invalid_payload", token=normalized_token, path=path, payload=payload, error=str(exc))

    if expires_at <= _coerce_now(now):
        revivable = state in ACTIVE_NONTERMINAL_STATES and _run_anchor_present(payload)
        return DecisionTokenLookup(
            status="expired", token=normalized_token, path=path, payload=payload, state=state, revivable=revivable
        )
    return DecisionTokenLookup(
        status="valid", token=normalized_token, path=path, payload=payload, state=state, revivable=False
    )


def _record_from_lookup(lookup: DecisionTokenLookup) -> DecisionTokenRecord:
    assert lookup.payload is not None
    return DecisionTokenRecord(token=lookup.token, path=lookup.path, payload=lookup.payload)


def _read_record_any(token: str, *, root: str | Path | None, now: datetime | None) -> DecisionTokenRecord:
    """读取 token 记录（含 TTL 已过期者），仅在缺失/损坏时抛 ``DecisionTokenNotFoundError``。

    供旁路终态标注（mark_expired / mark_lost）使用——它们本就作用于 TTL 死亡的 token。
    """
    lookup = read_decision_token(token, root=root, now=now)
    if lookup.status in ("valid", "expired") and lookup.payload is not None:
        return _record_from_lookup(lookup)
    raise DecisionTokenNotFoundError(f"无法读取决策 token {lookup.token}：status={lookup.status}")


def _load_active_record(
    token: str,
    *,
    root: str | Path | None,
    now: datetime | None,
    ttl_seconds: int,
) -> DecisionTokenRecord:
    """读取一枚仍可推进的 token：TTL 有效直接返回；TTL 过期但可复活则先复活再返回；否则抛
    ``DecisionTokenExpiredError`` / ``DecisionTokenNotFoundError``。
    """
    lookup = read_decision_token(token, root=root, now=now)
    if lookup.status == "valid":
        return _record_from_lookup(lookup)
    if lookup.status == "expired":
        if lookup.revivable:
            return revive_decision_token(lookup, root=root, now=now, ttl_seconds=ttl_seconds)
        raise DecisionTokenExpiredError(f"决策 token {lookup.token} 已过 TTL 且无在途 run 锚点，无法复活")
    raise DecisionTokenNotFoundError(f"无法读取决策 token {lookup.token}：status={lookup.status}")


# ---------------------------------------------------------------------------
# 状态迁移引擎
# ---------------------------------------------------------------------------

def _apply_lifecycle_transition(
    record: DecisionTokenRecord,
    target_state: str,
    *,
    now: datetime,
    extra_updates: dict[str, Any] | None = None,
) -> DecisionTokenRecord:
    """校验并落盘一次生命周期状态迁移；非法迁移抛 ``DecisionTokenStateError``。

    合法性以 ``LEGAL_TRANSITIONS`` 邻接表为准。落盘时打对应状态时间戳、追加一条 history。
    """
    current = str(record.payload.get("state"))
    allowed = LEGAL_TRANSITIONS.get(current, frozenset())
    if target_state not in allowed:
        raise DecisionTokenStateError(
            f"非法状态迁移: {current} → {target_state}（token={record.token}；"
            f"当前状态允许迁往 {sorted(allowed) or '无（终态）'}）"
        )
    payload = dict(record.payload)
    payload["state"] = target_state
    stamp_field = _STATE_TIMESTAMP_FIELD.get(target_state)
    if stamp_field:
        payload[stamp_field] = format_timestamp(now)
    history = list(payload.get("history") or [])
    history.append({"from": current, "to": target_state, "at": format_timestamp(now)})
    payload["history"] = history
    if extra_updates:
        payload.update(extra_updates)
    _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


# ---------------------------------------------------------------------------
# 去重索引（key = (worktree, session_id, candidate_fingerprint)）
# ---------------------------------------------------------------------------

def _dedup_index_root(root: str | Path | None) -> Path:
    return decision_token_root(root) / "dedup-index"


def _dedup_index_path(worktree: str, session_id: str, candidate_fingerprint: str, root: str | Path | None) -> Path:
    key_hash = _digest(worktree, session_id, candidate_fingerprint)
    return _dedup_index_root(root) / f"{key_hash}.json"


def _read_dedup_index(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _dedup_active_lookup(
    index_payload: dict[str, Any],
    *,
    root: str | Path | None,
    now: datetime | None,
    ttl_seconds: int,
) -> DecisionTokenRecord | None:
    """给定去重索引指向的 token，若其仍活跃（非终态、TTL 有效或可复活）则返回可用 record，
    否则返回 None（索引已陈旧）。可复活者就地复活后返回。
    """
    token = index_payload.get("token")
    if not isinstance(token, str):
        return None
    lookup = read_decision_token(token, root=root, now=now)
    if lookup.state not in ACTIVE_NONTERMINAL_STATES:
        return None
    if lookup.status == "valid":
        return _record_from_lookup(lookup)
    if lookup.status == "expired" and lookup.revivable:
        return revive_decision_token(lookup, root=root, now=now, ttl_seconds=ttl_seconds)
    return None


# ---------------------------------------------------------------------------
# 铸造侧频率熔断（R24 裁决 b）：纯计数
# ---------------------------------------------------------------------------

def _mint_request_ledger_path(session_id: str, root: str | Path | None) -> Path:
    return circuit_breaker_root(root) / "mint-requests" / f"{_digest(session_id)}.jsonl"


def _tripped_ledger_path(session_id: str, root: str | Path | None) -> Path:
    return circuit_breaker_root(root) / "circuit-breaker-tripped" / f"{_digest(session_id)}.jsonl"


def count_recent_mint_requests(
    session_id: str,
    *,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_MINT_CIRCUIT_BREAKER_WINDOW_SECONDS,
    root: str | Path | None = None,
) -> int:
    """统计给定 session 在 ``[now - window, now]`` 滚动窗口内已记录的铸造请求条数（纯计数）。"""
    ledger = _mint_request_ledger_path(session_id, root)
    horizon = _coerce_now(now) - timedelta(seconds=window_seconds)
    count = 0
    for record in _read_jsonl(ledger):
        raw = record.get("at")
        if not isinstance(raw, str):
            continue
        try:
            at = parse_timestamp(raw)
        except ValueError:
            continue
        if at >= horizon:
            count += 1
    return count


def _register_mint_request_and_check_breaker(
    *,
    session_id: str,
    worktree: str,
    candidate_fingerprint: str,
    now: datetime,
    window_seconds: int,
    max_requests: int,
    root: str | Path | None,
) -> CircuitBreakerStatus:
    """记录本次铸造请求并判断是否熔断。

    先向 mint-request ledger 追加本次请求（含当前请求本身参与计数），再统计窗口内条数；
    达到 ``max_requests`` 即熔断，向 circuit-breaker-tripped ledger 追加一条并返回 tripped 状态。
    """
    ledger = _mint_request_ledger_path(session_id, root)
    _append_jsonl(
        ledger,
        {
            "at": format_timestamp(now),
            "worktree": worktree,
            "candidate_fingerprint": candidate_fingerprint,
        },
    )
    count = count_recent_mint_requests(session_id, now=now, window_seconds=window_seconds, root=root)
    if count >= max_requests:
        tripped_ledger = _tripped_ledger_path(session_id, root)
        _append_jsonl(
            tripped_ledger,
            {
                "at": format_timestamp(now),
                "session_id": session_id,
                "worktree": worktree,
                "candidate_fingerprint": candidate_fingerprint,
                "request_count": count,
                "window_seconds": window_seconds,
                "max_requests": max_requests,
            },
        )
        return CircuitBreakerStatus(
            tripped=True,
            request_count=count,
            window_seconds=window_seconds,
            max_requests=max_requests,
            session_id=session_id,
            ledger_path=tripped_ledger,
        )
    return CircuitBreakerStatus(
        tripped=False,
        request_count=count,
        window_seconds=window_seconds,
        max_requests=max_requests,
        session_id=session_id,
        ledger_path=None,
    )


# ---------------------------------------------------------------------------
# declined 指纹冷却表
# ---------------------------------------------------------------------------

def _declined_fingerprint_path(worktree: str, candidate_fingerprint: str, root: str | Path | None) -> Path:
    key_hash = _digest(worktree, candidate_fingerprint)
    return declined_fingerprint_root(root) / f"{key_hash}.json"


def record_declined_fingerprint(
    *,
    worktree: str,
    candidate_fingerprint: str,
    reason: str | None = None,
    token: str | None = None,
    now: datetime | None = None,
    cooldown_root: str | Path | None = None,
) -> Path:
    """把 ``(worktree, candidate_fingerprint)`` 写入 declined 冷却表（decline 时调用，幂等覆盖）。

    冷却语义为「指纹未变即持续冷却」：无 TTL，直到候选文件集变化导致指纹变化才不再命中。
    """
    path = _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root)
    _atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "worktree": worktree,
            "candidate_fingerprint": candidate_fingerprint,
            "declined_at": format_timestamp(_coerce_now(now)),
            "reason": reason,
            "token": token,
        },
    )
    return path


def is_fingerprint_in_declined_cooldown(
    *,
    worktree: str,
    candidate_fingerprint: str,
    cooldown_root: str | Path | None = None,
) -> bool:
    """查询 ``(worktree, candidate_fingerprint)`` 是否处于 declined 冷却中（供摘要生成器「指纹未变不再注入」）。"""
    return _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root).is_file()


def read_declined_fingerprint(
    *,
    worktree: str,
    candidate_fingerprint: str,
    cooldown_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """读取 declined 冷却记录的完整 payload（不存在或损坏返回 None）。"""
    path = _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root)
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def clear_declined_fingerprint(
    *,
    worktree: str,
    candidate_fingerprint: str,
    cooldown_root: str | Path | None = None,
) -> bool:
    """从冷却表移除某指纹（返回是否真的删除了文件）。"""
    path = _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 铸造（Stop hook 侧唯一铸造入口）
# ---------------------------------------------------------------------------

def mint_decision_token(
    *,
    worktree: str,
    session_id: str,
    candidate_fingerprint: str,
    candidate_summary: dict[str, Any] | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
    now: datetime | None = None,
    root: str | Path | None = None,
    cooldown_root: str | Path | None = None,
    circuit_breaker_root_dir: str | Path | None = None,
    circuit_breaker_window_seconds: int = DEFAULT_MINT_CIRCUIT_BREAKER_WINDOW_SECONDS,
    circuit_breaker_max_requests: int = DEFAULT_MINT_CIRCUIT_BREAKER_MAX_REQUESTS,
) -> MintResult:
    """铸造一枚决策 token（Stop hook 侧唯一入口），依次经三道闸：

    1. **去重（注入幂等硬保证，R25）**：``(worktree, session_id, candidate_fingerprint)`` 同键存在活跃
       token 时不重铸、直接返回既有 token（``outcome=deduplicated``；过期可复活者就地复活）。
    2. **declined 冷却**：指纹处于 declined 冷却中则拒铸（``outcome=declined_cooldown``），防 nag 循环。
    3. **铸造频率熔断（R24 裁决 b）**：同 session 窗口内第 ≥K 次铸造请求即拒铸
       （``outcome=circuit_breaker_tripped``），写 ledger 并回告警文案。纯计数，零语义判断。

    三闸皆过则新铸 token（``outcome=minted``），落盘 + 建/覆盖去重索引。``candidate_fingerprint`` 对
    本模块不透明。
    """
    if ttl_seconds <= 0:
        raise DecisionTokenError("ttl_seconds 必须为正")
    now_dt = _coerce_now(now)

    # 闸一：去重。存在活跃同键 token → 幂等返回。
    index_path = _dedup_index_path(worktree, session_id, candidate_fingerprint, root)
    existing_index = _read_dedup_index(index_path)
    index_is_stale = False
    if existing_index is not None:
        active = _dedup_active_lookup(existing_index, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        if active is not None:
            return MintResult(outcome=MINT_OUTCOME_DEDUPLICATED, token=active.token, record=active)
        index_is_stale = True  # 索引指向的 token 已终态/真过期，可覆盖

    # 闸二：declined 冷却（指纹未变不再注入）。
    if is_fingerprint_in_declined_cooldown(
        worktree=worktree, candidate_fingerprint=candidate_fingerprint, cooldown_root=cooldown_root
    ):
        return MintResult(
            outcome=MINT_OUTCOME_DECLINED_COOLDOWN,
            message="候选指纹处于 declined 冷却中，跳过铸造（指纹未变不再注入）。",
        )

    # 闸三：铸造频率熔断（纯计数）。
    breaker = _register_mint_request_and_check_breaker(
        session_id=session_id,
        worktree=worktree,
        candidate_fingerprint=candidate_fingerprint,
        now=now_dt,
        window_seconds=circuit_breaker_window_seconds,
        max_requests=circuit_breaker_max_requests,
        root=circuit_breaker_root_dir,
    )
    if breaker.tripped:
        return MintResult(
            outcome=MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED,
            circuit_breaker=breaker,
            message=(
                f"铸造频率熔断：session {session_id} 在 {breaker.window_seconds}s 窗口内已第 "
                f"{breaker.request_count} 次请求铸造决策 token（阈值 {breaker.max_requests}）。"
                "疑似递归发起，已拒铸——请检查是否在对自己的 RVF 结果再次发起 RVF。"
            ),
        )

    # 三闸皆过：新铸 token。
    record = _create_token_record(
        worktree=worktree,
        session_id=session_id,
        candidate_fingerprint=candidate_fingerprint,
        candidate_summary=candidate_summary,
        ttl_seconds=ttl_seconds,
        now=now_dt,
        root=root,
    )

    # 建/覆盖去重索引。首次创建用 O_EXCL 防并发双铸竞态：若竞态者已抢先建索引且其 token 活跃，
    # 丢弃本次刚铸 token、返回竞态者的 token（幂等）。
    index_payload = {
        "schema_version": SCHEMA_VERSION,
        "token": record.token,
        "worktree": worktree,
        "session_id": session_id,
        "candidate_fingerprint": candidate_fingerprint,
        "created_at": format_timestamp(now_dt),
    }
    if index_is_stale:
        _atomic_write_json(index_path, index_payload)
        return MintResult(outcome=MINT_OUTCOME_MINTED, token=record.token, record=record)
    try:
        _atomic_create_json(index_path, index_payload)
    except FileExistsError:
        winner = _read_dedup_index(index_path)
        winner_active = (
            _dedup_active_lookup(winner, root=root, now=now_dt, ttl_seconds=ttl_seconds)
            if winner is not None
            else None
        )
        if winner_active is not None:
            _discard_token_record(record)
            return MintResult(outcome=MINT_OUTCOME_DEDUPLICATED, token=winner_active.token, record=winner_active)
        _atomic_write_json(index_path, index_payload)
    return MintResult(outcome=MINT_OUTCOME_MINTED, token=record.token, record=record)


def _create_token_record(
    *,
    worktree: str,
    session_id: str,
    candidate_fingerprint: str,
    candidate_summary: dict[str, Any] | None,
    ttl_seconds: int,
    now: datetime,
    root: str | Path | None,
) -> DecisionTokenRecord:
    """分配唯一 token 并原子落盘初始（``minted``）payload。"""
    expires_at = now + timedelta(seconds=ttl_seconds)
    for _attempt in range(DEFAULT_TOKEN_ALLOCATION_ATTEMPTS):
        token = generate_token()
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "token": token,
            "state": STATE_MINTED,
            "worktree": worktree,
            "session_id": session_id,
            "candidate_fingerprint": candidate_fingerprint,
            "candidate_summary": candidate_summary,
            "created_at": format_timestamp(now),
            "minted_at": format_timestamp(now),
            "expires_at": format_timestamp(expires_at),
            "run_anchor_dir": None,
            "start_attempt_count": 0,
            "revived_count": 0,
            "history": [{"from": None, "to": STATE_MINTED, "at": format_timestamp(now)}],
        }
        path = decision_token_path(token, root)
        try:
            _atomic_create_json(path, payload)
            return DecisionTokenRecord(token=token, path=path, payload=payload)
        except FileExistsError:
            continue
    raise DecisionTokenError(f"连续 {DEFAULT_TOKEN_ALLOCATION_ATTEMPTS} 次都未能分配唯一决策 token")


def _discard_token_record(record: DecisionTokenRecord) -> None:
    """丢弃一枚刚铸但因竞态需作废的 token 文件（best-effort）。"""
    try:
        record.path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 状态推进接口（Stop hook + rvf-agent CLI 共用）
# ---------------------------------------------------------------------------

def mark_injected(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """``minted → injected``（Stop hook 在**确认投递注入后**调用）。已是 injected 则幂等返回。

    遵循 87e9338「确认落地才 arm」教训：仅在真正投递注入后推进，不做乐观预 arm。
    """
    now_dt = _coerce_now(now)
    record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
    if record.state == STATE_INJECTED:
        return record
    return _apply_lifecycle_transition(record, STATE_INJECTED, now=now_dt)


def acknowledge_decision_token(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """``minted|injected|acknowledged → acknowledged``（rvf-agent ``start``；acknowledged 为非终态）。

    每次调用递增 ``start_attempt_count``（首次记 ``acknowledged_at``），支撑 ``start --retry``：
    重量级准备失败后回退到 acknowledged、再次 start 即再走一轮 acknowledge。
    """
    now_dt = _coerce_now(now)
    record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
    if record.state not in (STATE_MINTED, STATE_INJECTED, STATE_ACKNOWLEDGED):
        raise DecisionTokenStateError(
            f"非法状态迁移: {record.state} → {STATE_ACKNOWLEDGED}（token={record.token}；已达终态不可再 start）"
        )
    extra: dict[str, Any] = {"start_attempt_count": int(record.payload.get("start_attempt_count") or 0) + 1}
    if "acknowledged_at" not in record.payload:
        extra["acknowledged_at"] = format_timestamp(now_dt)
    return _apply_lifecycle_transition(record, STATE_ACKNOWLEDGED, now=now_dt, extra_updates=extra)


def record_start_failure(
    token: str,
    *,
    reason: str,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """记录 ``start`` 重量级准备失败并**回退保持在 acknowledged**（供 ``start --retry``）。

    仅在 ``acknowledged`` 状态合法：dispatched/declined（终态）或 minted/injected（尚未 start）均拒绝，
    以坐实「dispatched 前才可从失败回退到 acknowledged」的边界。状态不变，仅落盘失败诊断。
    """
    now_dt = _coerce_now(now)
    record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
    if record.state != STATE_ACKNOWLEDGED:
        raise DecisionTokenStateError(
            f"只能在 acknowledged 状态记录 start 失败回退，当前={record.state}（token={record.token}）"
        )
    payload = dict(record.payload)
    payload["last_start_failure"] = {"reason": reason, "at": format_timestamp(now_dt)}
    _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


def attach_run_anchor(
    token: str,
    *,
    run_dir: str,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """把在途 ``run_dir`` 锚点写入 token（``start``/prepare 建好 run 后由 CLI 调用），供 TTL 复活判据。"""
    now_dt = _coerce_now(now)
    record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
    if record.state not in ACTIVE_NONTERMINAL_STATES:
        raise DecisionTokenStateError(
            f"只能在活跃非终态挂载 run 锚点，当前={record.state}（token={record.token}）"
        )
    payload = dict(record.payload)
    payload["run_anchor_dir"] = run_dir
    _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


def mark_dispatched(
    token: str,
    *,
    strictness: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """``acknowledged → dispatched``（rvf-agent ``dispatch``，终态）。minted/injected 直接 dispatch 为非法。"""
    now_dt = _coerce_now(now)
    record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
    extra: dict[str, Any] = {}
    if strictness is not None:
        extra["dispatched_strictness"] = strictness
    return _apply_lifecycle_transition(record, STATE_DISPATCHED, now=now_dt, extra_updates=extra)


def mark_declined(
    token: str,
    *,
    reason: str,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
    cooldown_root: str | Path | None = None,
) -> DecisionTokenRecord:
    """``minted|injected|acknowledged → declined``（rvf-agent ``decline``，终态）。

    同时把 ``(worktree, candidate_fingerprint)`` 写入 declined 冷却表——指纹未变即不再注入。
    """
    now_dt = _coerce_now(now)
    record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
    updated = _apply_lifecycle_transition(record, STATE_DECLINED, now=now_dt, extra_updates={"decline_reason": reason})
    record_declined_fingerprint(
        worktree=str(updated.payload.get("worktree")),
        candidate_fingerprint=str(updated.payload.get("candidate_fingerprint")),
        reason=reason,
        token=updated.token,
        now=now_dt,
        cooldown_root=cooldown_root,
    )
    return updated


def mark_lost(
    token: str,
    *,
    reason: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenRecord:
    """旁路终态 ``… → lost``（S4 投递回执 TTL 超时无任一信号；作用于可能已 TTL 过期的 token）。"""
    now_dt = _coerce_now(now)
    record = _read_record_any(token, root=root, now=now_dt)
    extra: dict[str, Any] = {}
    if reason is not None:
        extra["lost_reason"] = reason
    return _apply_lifecycle_transition(record, STATE_LOST, now=now_dt, extra_updates=extra)


def mark_expired(
    token: str,
    *,
    reason: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenRecord:
    """旁路终态 ``… → expired``（显式把一枚 TTL 死亡且不复活的 token 终局化；作用于已过期 token）。"""
    now_dt = _coerce_now(now)
    record = _read_record_any(token, root=root, now=now_dt)
    extra: dict[str, Any] = {}
    if reason is not None:
        extra["expired_reason"] = reason
    return _apply_lifecycle_transition(record, STATE_EXPIRED, now=now_dt, extra_updates=extra)


def revive_decision_token(
    lookup_or_token: DecisionTokenLookup | str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """把一枚**已过 TTL 但 run 在途**的 token 就地续期为原状态（参照 rvf_prep_file 的 FU-1 方案 B）。

    仅当读取状态为 ``expired``、生命周期仍处活跃非终态、且锚定 run_dir 仍在磁盘时才复活：重写
    ``created_at`` / ``expires_at``、递增 ``revived_count``，**保持 state 不变**。其余情形抛
    ``DecisionTokenError`` / ``DecisionTokenExpiredError``。
    """
    if ttl_seconds <= 0:
        raise DecisionTokenError("ttl_seconds 必须为正")
    now_dt = _coerce_now(now)
    lookup = (
        read_decision_token(lookup_or_token, root=root, now=now_dt)
        if isinstance(lookup_or_token, str)
        else lookup_or_token
    )
    if lookup.status != "expired" or lookup.payload is None:
        raise DecisionTokenError(f"仅可复活 expired 状态的 token，当前 status={lookup.status}")
    if lookup.state not in ACTIVE_NONTERMINAL_STATES:
        raise DecisionTokenError(f"仅可复活活跃非终态 token，当前 state={lookup.state}")
    if not _run_anchor_present(lookup.payload):
        raise DecisionTokenExpiredError("token 已过 TTL 且无在途 run 锚点，不复活")
    payload = dict(lookup.payload)
    payload["created_at"] = format_timestamp(now_dt)
    payload["expires_at"] = format_timestamp(now_dt + timedelta(seconds=ttl_seconds))
    payload["revived_count"] = int(payload.get("revived_count") or 0) + 1
    _atomic_write_json(lookup.path, payload)
    return DecisionTokenRecord(token=lookup.token, path=lookup.path, payload=payload)


# ---------------------------------------------------------------------------
# 清扫（best-effort：删除已终态或真过期不复活的 token 与其陈旧去重索引）
# ---------------------------------------------------------------------------

def sweep_stale_decision_tokens(
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """清扫已终态 / 真过期不复活的 token 文件，返回被删路径列表（陈旧去重索引由下轮 mint 覆盖）。"""
    base = decision_token_root(root)
    if not base.is_dir():
        return []
    removed: list[Path] = []
    now_dt = _coerce_now(now)
    for path in sorted(base.glob("*.json")):
        lookup = read_decision_token(path.stem, root=base, now=now_dt)
        if lookup.status == "valid" and lookup.state in ACTIVE_NONTERMINAL_STATES:
            continue
        if lookup.status == "expired" and lookup.revivable:
            continue
        if lookup.status in ("invalid_token",):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed
