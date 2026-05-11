"""Bootstrap-size confirmation gate shared by Stop hook and UserPromptSubmit hook."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIRM_DIRNAME = "dispatch-confirmations"
DEFAULT_THRESHOLD_PATHS = 10
DEFAULT_THRESHOLD_BYTES = 1 * 1024 * 1024
DEFAULT_TTL_SECONDS = 300
PATHS_ENV = "CODEX_RVF_BOOTSTRAP_CONFIRM_THRESHOLD_PATHS"
BYTES_ENV = "CODEX_RVF_BOOTSTRAP_CONFIRM_THRESHOLD_BYTES"
TTL_ENV = "CODEX_RVF_BOOTSTRAP_CONFIRM_TTL_SECONDS"
YES_LITERALS = frozenset({"yes", "Yes", "YES"})
YES_PATTERN = re.compile(r"^(yes|Yes|YES)\s*$")


@dataclass(frozen=True)
class Thresholds:
    paths: int
    bytes: int

    @property
    def paths_disabled(self) -> bool:
        return self.paths <= 0

    @property
    def bytes_disabled(self) -> bool:
        return self.bytes <= 0


@dataclass
class Decision:
    needs_confirmation: bool
    oversize_paths: list[str] = field(default_factory=list)
    unattributed_path_count: int = 0
    unattributed_bytes: int = 0
    total_path_count: int = 0
    total_bytes: int = 0
    bootstrap_kind: str = "session-owned-only"
    reason: str = ""
    exempt: bool = False
    exempt_reason: str | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "needs_confirmation": self.needs_confirmation,
            "oversize_paths": list(self.oversize_paths),
            "unattributed_path_count": self.unattributed_path_count,
            "unattributed_bytes": self.unattributed_bytes,
            "total_path_count": self.total_path_count,
            "total_bytes": self.total_bytes,
            "bootstrap_kind": self.bootstrap_kind,
            "reason": self.reason,
            "exempt": self.exempt,
            "exempt_reason": self.exempt_reason,
        }


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value


def thresholds_from_env() -> Thresholds:
    return Thresholds(
        paths=_parse_int_env(PATHS_ENV, DEFAULT_THRESHOLD_PATHS),
        bytes=_parse_int_env(BYTES_ENV, DEFAULT_THRESHOLD_BYTES),
    )


def ttl_seconds_from_env() -> int:
    value = _parse_int_env(TTL_ENV, DEFAULT_TTL_SECONDS)
    return value if value > 0 else DEFAULT_TTL_SECONDS


def compute_decision(
    payload: dict[str, Any] | None,
    *,
    thresholds: Thresholds | None = None,
    exempt_kanban_followup: bool = False,
) -> Decision:
    thresholds = thresholds or thresholds_from_env()
    payload = payload or {}
    unattributed = [
        path for path in payload.get("unattributed_dirty_paths") or []
        if isinstance(path, str) and path.strip()
    ]
    bootstrap_kind = str(payload.get("bootstrap_kind") or "session-owned-only")
    unattributed_path_count = int(payload.get("unattributed_path_count") or len(unattributed))
    unattributed_bytes = int(payload.get("unattributed_bytes") or 0)
    total_path_count = len(payload.get("owned_dirty_paths") or [])
    total_bytes = int(payload.get("total_bootstrap_bytes") or 0)

    if exempt_kanban_followup:
        return Decision(
            needs_confirmation=False,
            oversize_paths=unattributed[: thresholds.paths or DEFAULT_THRESHOLD_PATHS],
            unattributed_path_count=unattributed_path_count,
            unattributed_bytes=unattributed_bytes,
            total_path_count=total_path_count,
            total_bytes=total_bytes,
            bootstrap_kind=bootstrap_kind,
            reason="exempt_cline_kanban_followup",
            exempt=True,
            exempt_reason="cline-kanban-followup context: non-interactive, fail-open",
        )

    if bootstrap_kind != "full-dirty" or not unattributed:
        return Decision(
            needs_confirmation=False,
            unattributed_path_count=unattributed_path_count,
            unattributed_bytes=unattributed_bytes,
            total_path_count=total_path_count,
            total_bytes=total_bytes,
            bootstrap_kind=bootstrap_kind,
            reason="no_unattributed_dirty",
        )

    path_trigger = (not thresholds.paths_disabled) and unattributed_path_count >= thresholds.paths
    byte_trigger = (not thresholds.bytes_disabled) and unattributed_bytes >= thresholds.bytes

    if not path_trigger and not byte_trigger:
        return Decision(
            needs_confirmation=False,
            unattributed_path_count=unattributed_path_count,
            unattributed_bytes=unattributed_bytes,
            total_path_count=total_path_count,
            total_bytes=total_bytes,
            bootstrap_kind=bootstrap_kind,
            reason="below_threshold",
        )

    triggers: list[str] = []
    if path_trigger:
        triggers.append(f"paths>= {thresholds.paths}")
    if byte_trigger:
        triggers.append(f"bytes>= {thresholds.bytes}")
    return Decision(
        needs_confirmation=True,
        oversize_paths=sorted(unattributed)[: max(thresholds.paths, DEFAULT_THRESHOLD_PATHS) or DEFAULT_THRESHOLD_PATHS],
        unattributed_path_count=unattributed_path_count,
        unattributed_bytes=unattributed_bytes,
        total_path_count=total_path_count,
        total_bytes=total_bytes,
        bootstrap_kind=bootstrap_kind,
        reason="; ".join(triggers),
    )


def marker_dir(state_root: Path) -> Path:
    return Path(state_root) / CONFIRM_DIRNAME


def safe_session_key(session_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id)
    return cleaned or "unknown-session"


def marker_path(state_root: Path, session_id: str) -> Path:
    return marker_dir(state_root) / f"{safe_session_key(session_id)}.json"


def write_marker(
    state_root: Path,
    *,
    session_id: str,
    token: str,
    decision: Decision,
    dispatch_context: dict[str, Any],
    ttl_seconds: int | None = None,
) -> Path:
    ttl = ttl_seconds if (ttl_seconds and ttl_seconds > 0) else ttl_seconds_from_env()
    now = datetime.now(timezone.utc)
    path = marker_path(state_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "token": token,
        "ttl_seconds": ttl,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": now.timestamp() + ttl,
        "decision": decision.to_summary(),
        "dispatch_context": dispatch_context,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_marker(state_root: Path, session_id: str) -> dict[str, Any] | None:
    path = marker_path(state_root, session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def delete_marker(state_root: Path, session_id: str) -> bool:
    path = marker_path(state_root, session_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def marker_is_expired(payload: dict[str, Any], *, now: float | None = None) -> bool:
    expires = payload.get("expires_at")
    if not isinstance(expires, (int, float)):
        return False
    current = now if now is not None else datetime.now(timezone.utc).timestamp()
    return current > float(expires)


def sweep_expired(state_root: Path, *, now: float | None = None) -> list[str]:
    base = marker_dir(state_root)
    if not base.exists():
        return []
    removed: list[str] = []
    current = now if now is not None else datetime.now(timezone.utc).timestamp()
    for path in base.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if marker_is_expired(payload, now=current):
            try:
                path.unlink()
                removed.append(path.stem)
            except OSError:
                continue
    return removed


def is_yes_literal(text: str | None) -> bool:
    if not isinstance(text, str):
        return False
    return bool(YES_PATTERN.match(text))


def format_system_message(decision: Decision, *, marker_path: Path) -> str:
    lines = [
        "review-validate-fix: bootstrap dirty 量超阈，已暂停 dispatch 等待你确认。",
        "",
        f"- bootstrap_kind: {decision.bootstrap_kind}",
        f"- 未归属 dirty paths: {decision.unattributed_path_count}",
        f"- 未归属 dirty bytes: {decision.unattributed_bytes}",
        f"- 总 dirty paths: {decision.total_path_count}",
        f"- 触发原因: {decision.reason}",
        "",
        "前若干超阈路径:",
    ]
    if decision.oversize_paths:
        for path in decision.oversize_paths[:10]:
            lines.append(f"  - {path}")
        remaining = max(0, decision.unattributed_path_count - len(decision.oversize_paths[:10]))
        if remaining:
            lines.append(f"  - ...还有 {remaining} 个未显示")
    else:
        lines.append("  - (无)")
    lines.extend(
        [
            "",
            "回复 `yes` / `Yes` / `YES`（严格字面，无前后内容）以继续此次 dispatch；",
            "回复其他任意内容视为取消，本次 dispatch 将被丢弃。",
            f"marker: {marker_path}",
        ]
    )
    return "\n".join(lines)
