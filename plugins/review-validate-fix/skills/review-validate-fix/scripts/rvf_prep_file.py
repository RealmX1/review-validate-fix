#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 300
DEFAULT_TOKEN_WRITE_ATTEMPTS = 8
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
ENV_PREP_ROOT = "CODEX_RVF_PREP_ROOT"
PROTECTED_UPDATE_FIELDS = frozenset({"schema_version", "token", "created_at", "expires_at"})


class PrepFileError(ValueError):
    pass


@dataclass(frozen=True)
class PrepFileRecord:
    token: str
    path: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class PrepFileLookup:
    status: str
    token: str
    path: Path
    payload: dict[str, Any] | None = None
    error: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond:
        return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prep_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    configured = os.environ.get(ENV_PREP_ROOT)
    if configured:
        return Path(configured).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir).expanduser() / "rvf-prep"
    return Path("/tmp") / "rvf-prep"


def generate_token() -> str:
    return secrets.token_hex(8)


def validate_token(token: str) -> str:
    normalized = token.strip().lower()
    if not TOKEN_RE.fullmatch(normalized):
        raise PrepFileError(f"invalid RVF dispatch token: {token!r}")
    return normalized


def prep_file_path(token: str, root: str | Path | None = None) -> Path:
    return prep_root(root) / f"{validate_token(token)}.json"


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
    tmp = _write_json_tmp(path, payload)
    try:
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_prep_file(
    payload: dict[str, Any],
    *,
    root: str | Path | None = None,
    token: str | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> PrepFileRecord:
    if not isinstance(payload, dict):
        raise PrepFileError("prep payload must be a JSON object")
    if ttl_seconds <= 0:
        raise PrepFileError("prep file ttl_seconds must be positive")

    explicit_token = token is not None
    max_attempts = 1 if explicit_token else DEFAULT_TOKEN_WRITE_ATTEMPTS
    created_at = (now or utc_now()).astimezone(timezone.utc)
    last_collision: FileExistsError | None = None
    for _attempt in range(max_attempts):
        normalized_token = validate_token(token or generate_token())
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        normalized_payload = dict(payload)
        normalized_payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "token": normalized_token,
                "created_at": format_timestamp(created_at),
                "expires_at": format_timestamp(expires_at),
            }
        )
        path = prep_file_path(normalized_token, root)
        try:
            _atomic_create_json(path, normalized_payload)
            return PrepFileRecord(token=normalized_token, path=path, payload=normalized_payload)
        except FileExistsError as exc:
            last_collision = exc
            lookup = read_prep_file(normalized_token, root=root, now=created_at)
            if lookup.status != "valid":
                try:
                    path.unlink()
                except OSError:
                    pass
                try:
                    _atomic_create_json(path, normalized_payload)
                    return PrepFileRecord(token=normalized_token, path=path, payload=normalized_payload)
                except FileExistsError as retry_exc:
                    last_collision = retry_exc
            if explicit_token:
                raise PrepFileError(f"prep file token already exists: {normalized_token}") from exc
            continue
    raise PrepFileError(
        f"failed to allocate a unique RVF dispatch token after {max_attempts} attempts"
    ) from last_collision


def update_prep_file(
    record: PrepFileRecord,
    updates: dict[str, Any],
) -> PrepFileRecord:
    if not isinstance(updates, dict):
        raise PrepFileError("prep file updates must be a JSON object")
    protected = sorted(PROTECTED_UPDATE_FIELDS.intersection(updates))
    if protected:
        raise PrepFileError(f"prep file updates cannot override protected fields: {', '.join(protected)}")
    payload = dict(record.payload)
    payload.update(updates)
    payload["schema_version"] = SCHEMA_VERSION
    payload["token"] = validate_token(record.token)
    if not isinstance(payload.get("created_at"), str):
        raise PrepFileError("existing prep payload is missing created_at")
    if not isinstance(payload.get("expires_at"), str):
        raise PrepFileError("existing prep payload is missing expires_at")
    _atomic_write_json(record.path, payload)
    return PrepFileRecord(token=record.token, path=record.path, payload=payload)


def _invalid(token: str, path: Path, status: str, error: str | None = None) -> PrepFileLookup:
    return PrepFileLookup(status=status, token=token, path=path, error=error)


def read_prep_file(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> PrepFileLookup:
    try:
        normalized_token = validate_token(token)
    except PrepFileError as exc:
        return PrepFileLookup(status="invalid_token", token=token, path=prep_root(root), error=str(exc))

    path = prep_file_path(normalized_token, root)
    if not path.is_file():
        return _invalid(normalized_token, path, "missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _invalid(normalized_token, path, "invalid_json", str(exc))
    except OSError as exc:
        return _invalid(normalized_token, path, "unreadable", str(exc))
    if not isinstance(payload, dict):
        return _invalid(normalized_token, path, "invalid_payload", "prep file root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        return _invalid(normalized_token, path, "schema_mismatch")
    if payload.get("token") != normalized_token:
        return _invalid(normalized_token, path, "token_mismatch")
    expires_raw = payload.get("expires_at")
    if not isinstance(expires_raw, str):
        return _invalid(normalized_token, path, "invalid_payload", "missing expires_at")
    try:
        expires_at = parse_timestamp(expires_raw)
    except ValueError as exc:
        return _invalid(normalized_token, path, "invalid_payload", str(exc))
    if expires_at <= (now or utc_now()).astimezone(timezone.utc):
        return PrepFileLookup(status="expired", token=normalized_token, path=path, payload=payload)
    return PrepFileLookup(status="valid", token=normalized_token, path=path, payload=payload)


def sweep_stale(
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> list[Path]:
    base = prep_root(root)
    if not base.is_dir():
        return []
    removed: list[Path] = []
    for path in sorted(base.glob("*.json")):
        token = path.stem
        lookup = read_prep_file(token, root=base, now=now)
        if lookup.status == "valid":
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def append_diagnostic(
    *,
    root: str | Path | None,
    token: str,
    record: dict[str, Any],
) -> Path:
    normalized_token = validate_token(token)
    diagnostics_dir = prep_root(root) / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    diagnostics_dir.chmod(0o700)
    path = diagnostics_dir / f"{normalized_token}.jsonl"
    diagnostic = dict(record)
    diagnostic.setdefault("timestamp", format_timestamp(utc_now()))
    diagnostic["token"] = normalized_token
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return path
