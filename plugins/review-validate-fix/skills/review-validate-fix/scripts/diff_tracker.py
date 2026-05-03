#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import (
    _append_jsonl,
    _atomic_write_text,
    log_root,
    safe_token,
    utc_now,
)


SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
RANGE_TOLERANCE = 5

DISABLE_ENV = "CODEX_RVF_TRACKER_DISABLE"


def _disabled() -> bool:
    # Only explicit truthy values disable the tracker. Previously this used a
    # blacklist (`value not in {"", "0", "false", "False"}`) which silently
    # disabled the tracker for any other non-empty string — including
    # `no` / `off` / `False` / `NO`, the exact opposite of user intent.
    value = os.environ.get(DISABLE_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HunkAnchor:
    header: str
    context_hash: str
    old_range: tuple[int, int]
    new_range: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "header": self.header,
            "context_hash": self.context_hash,
            "old_range": list(self.old_range),
            "new_range": list(self.new_range),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "HunkAnchor | None":
        if not isinstance(payload, dict):
            return None
        header = payload.get("header")
        context_hash = payload.get("context_hash")
        old_range = payload.get("old_range")
        new_range = payload.get("new_range")
        if not isinstance(header, str) or not isinstance(context_hash, str):
            return None
        if not isinstance(old_range, list) or len(old_range) != 2:
            return None
        if not isinstance(new_range, list) or len(new_range) != 2:
            return None
        try:
            return cls(
                header=header,
                context_hash=context_hash,
                old_range=(int(old_range[0]), int(old_range[1])),
                new_range=(int(new_range[0]), int(new_range[1])),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class OwnedUnit:
    path: str
    unit: str  # "hunk" | "path"
    hunk_anchor: HunkAnchor | None = None


@dataclass
class RegisterResult:
    status: str
    repo_key: str
    tracker_dir: str | None
    claim_ids: list[str] = field(default_factory=list)
    dropped_stale_claim_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "repo_key": self.repo_key,
            "tracker_dir": self.tracker_dir,
            "claim_ids": list(self.claim_ids),
            "dropped_stale_claim_ids": list(self.dropped_stale_claim_ids),
        }


@dataclass
class Conflict:
    path: str
    unit: str
    hunk_header: str | None
    other_session_id: str
    other_run_id: str | None
    other_branch: str | None
    other_worktree: str | None
    other_claim_id: str
    last_seen_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "unit": self.unit,
            "hunk_header": self.hunk_header,
            "other_session_id": self.other_session_id,
            "other_run_id": self.other_run_id,
            "other_branch": self.other_branch,
            "other_worktree": self.other_worktree,
            "other_claim_id": self.other_claim_id,
            "last_seen_at": self.last_seen_at,
        }


def _run_git(repo: Path, args: list[str], *, text: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        stdout = completed.stdout if text else completed.stdout.decode("utf-8", "replace")
        raise RuntimeError(stderr.strip() or stdout.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def git_common_dir(repo: Path) -> Path | None:
    try:
        raw = _run_git(repo, ["rev-parse", "--git-common-dir"]).strip()
    except RuntimeError:
        return None
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (repo / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def is_bare_repo(repo: Path) -> bool:
    try:
        raw = _run_git(repo, ["rev-parse", "--is-bare-repository"]).strip()
    except RuntimeError:
        return False
    return raw == "true"


def repo_key(git_common_dir_path: Path) -> str:
    abspath = str(git_common_dir_path.resolve())
    digest = hashlib.sha1(abspath.encode("utf-8")).hexdigest()[:12]
    name_source = git_common_dir_path.parent.name or git_common_dir_path.name or "repo"
    return f"{safe_token(name_source)}-{digest}"


def tracker_dir(log_root_dir: Path, key: str) -> Path:
    return log_root_dir / "tracker" / key


def _new_claim_id() -> str:
    return f"clm-{secrets.token_hex(8)}"


def _hunk_header_parts(line: str) -> tuple[tuple[int, int], tuple[int, int], str] | None:
    match = HUNK_HEADER_RE.match(line)
    if match is None:
        return None
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    suffix = (match.group(5) or "").rstrip()
    return (old_start, old_count), (new_start, new_count), suffix


def _context_hash(lines: list[str]) -> str:
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


def _normalize_header(line: str) -> str:
    return line.rstrip()


def derive_hunk_anchors(repo: Path, path: str) -> list[HunkAnchor]:
    # Use -U3 so we actually capture unchanged context lines around each hunk.
    # With -U0 git emits zero context lines, which makes context_hash collapse
    # to sha1("")[:16] for every hunk and breaks the fuzzy matcher in
    # `_hunk_anchors_match` (it would degrade to "ranges within ±5 lines"
    # regardless of surrounding code, folding distinct hunks together and
    # producing spurious cross-session conflicts).
    try:
        diff = _run_git(repo, ["diff", "-U3", "--no-color", "HEAD", "--", path])
    except RuntimeError:
        return []
    anchors: list[HunkAnchor] = []
    pending_header: tuple[tuple[int, int], tuple[int, int], str] | None = None
    pending_header_line: str = ""
    context_lines: list[str] = []
    in_hunk = False
    for raw_line in diff.splitlines():
        if raw_line.startswith("@@"):
            if pending_header is not None:
                old_range, new_range, _ = pending_header
                anchors.append(
                    HunkAnchor(
                        header=_normalize_header(pending_header_line),
                        context_hash=_context_hash(context_lines),
                        old_range=old_range,
                        new_range=new_range,
                    )
                )
            parsed = _hunk_header_parts(raw_line)
            if parsed is None:
                pending_header = None
                pending_header_line = ""
                context_lines = []
                in_hunk = False
                continue
            pending_header = parsed
            pending_header_line = raw_line
            context_lines = []
            in_hunk = True
            continue
        if not in_hunk or pending_header is None:
            continue
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+") or raw_line.startswith("-"):
            continue
        if len(context_lines) < 3:
            context_lines.append(raw_line)
    if pending_header is not None:
        old_range, new_range, _ = pending_header
        anchors.append(
            HunkAnchor(
                header=_normalize_header(pending_header_line),
                context_hash=_context_hash(context_lines),
                old_range=old_range,
                new_range=new_range,
            )
        )
    return anchors


def _load_state(state_path: Path) -> dict[str, Any]:
    try:
        text = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "claims": [], "tombstones": []}
    except OSError:
        return {"schema_version": SCHEMA_VERSION, "claims": [], "tombstones": []}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"schema_version": SCHEMA_VERSION, "claims": [], "tombstones": []}
    if not isinstance(payload, dict):
        return {"schema_version": SCHEMA_VERSION, "claims": [], "tombstones": []}
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("claims", [])
    payload.setdefault("tombstones", [])
    if not isinstance(payload["claims"], list):
        payload["claims"] = []
    if not isinstance(payload["tombstones"], list):
        payload["tombstones"] = []
    return payload


def _write_state(state_path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = utc_now()
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(state_path, text)


def _ensure_meta(meta_path: Path, repo: Path, common_dir: Path, key: str) -> None:
    if meta_path.exists():
        return
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo.resolve()),
        "git_common_dir": str(common_dir.resolve()),
        "repo_key": key,
        "created_at": utc_now(),
    }
    _atomic_write_text(meta_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + timeout
        acquired = False
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_SECONDS)
        if not acquired:
            raise TimeoutError(f"timed out acquiring tracker lock: {lock_path}")
        try:
            yield handle
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


def _hunk_anchors_match(left: HunkAnchor, right: HunkAnchor, *, strict: bool = False) -> bool:
    if left.header == right.header:
        return True
    if strict:
        return False
    if left.context_hash and left.context_hash == right.context_hash:
        if abs(left.old_range[0] - right.old_range[0]) <= RANGE_TOLERANCE:
            return True
    return False


def _claim_overlaps(left: dict[str, Any], right_path: str, right_unit: str, right_anchor: HunkAnchor | None) -> bool:
    if left.get("path") != right_path:
        return False
    left_unit = left.get("unit")
    if left_unit == "path" or right_unit == "path":
        return True
    left_anchor = HunkAnchor.from_dict(left.get("hunk_anchor"))
    if left_anchor is None or right_anchor is None:
        return True
    return _hunk_anchors_match(left_anchor, right_anchor)


def _build_owned_units(
    repo: Path,
    *,
    owned_paths: Iterable[str],
    apply_patch_paths: set[str],
    exec_only_paths: set[str],
) -> list[tuple[OwnedUnit, str]]:
    units: list[tuple[OwnedUnit, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in owned_paths:
        if path in apply_patch_paths:
            evidence = "apply_patch"
            anchors = derive_hunk_anchors(repo, path)
            if anchors:
                for anchor in anchors:
                    key = (path, "hunk", anchor.header)
                    if key in seen:
                        continue
                    seen.add(key)
                    units.append((OwnedUnit(path=path, unit="hunk", hunk_anchor=anchor), evidence))
                continue
        elif path in exec_only_paths:
            evidence = "exec_command"
        else:
            evidence = "git_diff"
        key = (path, "path", "")
        if key in seen:
            continue
        seen.add(key)
        units.append((OwnedUnit(path=path, unit="path", hunk_anchor=None), evidence))
    return units


def _current_branch(repo: Path) -> str | None:
    try:
        raw = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    except RuntimeError:
        return None
    if not raw or raw == "HEAD":
        return None
    return raw


def register_claims(
    *,
    repo: Path,
    session_id: str,
    run_id: str | None,
    worktree: Path | None,
    branch: str | None,
    owned_paths: Iterable[str],
    apply_patch_paths: set[str],
    exec_only_paths: set[str],
    log_root_override: Path | None = None,
) -> RegisterResult:
    repo_resolved = repo.resolve()

    if _disabled():
        return RegisterResult(status="disabled", repo_key="", tracker_dir=None)

    if is_bare_repo(repo_resolved):
        return RegisterResult(status="unsupported_repo", repo_key="", tracker_dir=None)

    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return RegisterResult(status="unsupported_repo", repo_key="", tracker_dir=None)

    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "state.json"
    events_path = directory / "events.jsonl"
    meta_path = directory / "meta.json"
    lock_path = directory / "state.lock"

    paths_list = sorted({path for path in owned_paths if isinstance(path, str) and path.strip()})
    if not paths_list:
        # An empty owned_paths list is a no-op for the tracker. Falling through
        # would cause register_claims to drop *all* of this session's existing
        # claims (orphan path → tombstone). Active drops should go through an
        # explicit release_claims API instead, so callers can't lose state by
        # accident.
        return RegisterResult(status="no_paths", repo_key=key, tracker_dir=str(directory))
    units = _build_owned_units(
        repo_resolved,
        owned_paths=paths_list,
        apply_patch_paths={path for path in apply_patch_paths if isinstance(path, str)},
        exec_only_paths={path for path in exec_only_paths if isinstance(path, str)},
    )

    branch_value = branch if branch is not None else _current_branch(repo_resolved)
    worktree_value = str(worktree.resolve()) if worktree is not None else str(repo_resolved)
    now = utc_now()

    try:
        with _exclusive_lock(lock_path):
            _ensure_meta(meta_path, repo_resolved, common_dir, key)
            state = _load_state(state_path)
            existing_claims: list[dict[str, Any]] = list(state.get("claims", []))

            keep_claims: list[dict[str, Any]] = []
            session_existing: list[dict[str, Any]] = []
            for claim in existing_claims:
                if claim.get("session_id") == session_id:
                    session_existing.append(claim)
                else:
                    keep_claims.append(claim)

            new_claims: list[dict[str, Any]] = []
            new_claim_ids: list[str] = []

            for unit, evidence in units:
                hunk_anchor_dict = unit.hunk_anchor.to_dict() if unit.hunk_anchor is not None else None
                matched: dict[str, Any] | None = None
                for prior in session_existing:
                    if (
                        prior.get("path") == unit.path
                        and prior.get("unit") == unit.unit
                    ):
                        if unit.unit == "hunk":
                            prior_anchor = HunkAnchor.from_dict(prior.get("hunk_anchor"))
                            if (
                                prior_anchor is not None
                                and unit.hunk_anchor is not None
                                and _hunk_anchors_match(prior_anchor, unit.hunk_anchor)
                            ):
                                matched = prior
                                break
                        else:
                            matched = prior
                            break
                if matched is not None:
                    matched["last_seen_at"] = now
                    matched["worktree"] = worktree_value
                    matched["branch"] = branch_value
                    if run_id is not None:
                        matched["run_id"] = run_id
                    new_claims.append(matched)
                    new_claim_ids.append(str(matched.get("claim_id")))
                    session_existing.remove(matched)
                else:
                    claim_id = _new_claim_id()
                    record = {
                        "claim_id": claim_id,
                        "session_id": session_id,
                        "run_id": run_id,
                        "worktree": worktree_value,
                        "branch": branch_value,
                        "path": unit.path,
                        "unit": unit.unit,
                        "hunk_anchor": hunk_anchor_dict,
                        "evidence": evidence,
                        "claimed_at": now,
                        "last_seen_at": now,
                        "lease": None,
                    }
                    new_claims.append(record)
                    new_claim_ids.append(claim_id)
                    _append_jsonl(
                        events_path,
                        {
                            "timestamp": now,
                            "event": "claim_added",
                            "claim_id": claim_id,
                            "session_id": session_id,
                            "run_id": run_id,
                            "path": unit.path,
                            "unit": unit.unit,
                            "evidence": evidence,
                        },
                    )

            dropped_stale: list[str] = []
            tombstones = list(state.get("tombstones", []))
            for orphan in session_existing:
                claim_id = str(orphan.get("claim_id") or "")
                dropped_stale.append(claim_id)
                tombstones.append(
                    {
                        "claim_id": claim_id,
                        "session_id": orphan.get("session_id"),
                        "path": orphan.get("path"),
                        "unit": orphan.get("unit"),
                        "dropped_at": now,
                        "reason": "session_no_longer_owns",
                    }
                )
                _append_jsonl(
                    events_path,
                    {
                        "timestamp": now,
                        "event": "claim_dropped",
                        "claim_id": claim_id,
                        "session_id": orphan.get("session_id"),
                        "run_id": orphan.get("run_id"),
                        "path": orphan.get("path"),
                        "reason": "session_no_longer_owns",
                    },
                )

            state["claims"] = keep_claims + new_claims
            state["tombstones"] = tombstones[-200:]
            state["repo"] = str(repo_resolved)
            state["git_common_dir"] = str(common_dir.resolve())
            state["schema_version"] = SCHEMA_VERSION
            _write_state(state_path, state)

            return RegisterResult(
                status="ok",
                repo_key=key,
                tracker_dir=str(directory),
                claim_ids=new_claim_ids,
                dropped_stale_claim_ids=dropped_stale,
            )
    except TimeoutError:
        try:
            _append_jsonl(
                events_path,
                {
                    "timestamp": now,
                    "event": "lock_timeout",
                    "session_id": session_id,
                    "run_id": run_id,
                    "owned_path_count": len(paths_list),
                },
            )
        except OSError:
            pass
        return RegisterResult(status="lock_timeout", repo_key=key, tracker_dir=str(directory))
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        # Known I/O / parse / git failure modes degrade non-fatally but record
        # an event for triage. Real programming bugs (e.g. AttributeError,
        # TypeError) intentionally propagate so they're not hidden.
        try:
            _append_jsonl(
                events_path,
                {
                    "timestamp": now,
                    "event": "register_failed",
                    "session_id": session_id,
                    "run_id": run_id,
                    "owned_path_count": len(paths_list),
                    "error": repr(exc),
                },
            )
        except OSError:
            pass
        return RegisterResult(status="error", repo_key=key, tracker_dir=str(directory))


def list_conflicts(
    repo: Path,
    *,
    current_session_id: str,
    owned_units: list[OwnedUnit],
    log_root_override: Path | None = None,
) -> list[Conflict]:
    repo_resolved = repo.resolve()
    if _disabled():
        return []
    if is_bare_repo(repo_resolved):
        return []
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return []
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    state_path = directory / "state.json"
    if not state_path.exists():
        return []
    try:
        with _exclusive_lock(directory / "state.lock"):
            state = _load_state(state_path)
    except TimeoutError:
        return []
    claims = state.get("claims") or []
    if not isinstance(claims, list):
        return []
    conflicts: list[Conflict] = []
    seen: set[tuple[str, str, str]] = set()
    for unit in owned_units:
        path = unit.path
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            if claim.get("session_id") == current_session_id:
                continue
            if not _claim_overlaps(claim, path, unit.unit, unit.hunk_anchor):
                continue
            anchor_dict = claim.get("hunk_anchor") if isinstance(claim.get("hunk_anchor"), dict) else None
            anchor_header = anchor_dict.get("header") if anchor_dict else None
            claim_id = str(claim.get("claim_id") or "")
            dedupe = (path, anchor_header or "", claim_id)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            conflicts.append(
                Conflict(
                    path=path,
                    unit=str(claim.get("unit") or "path"),
                    hunk_header=anchor_header,
                    other_session_id=str(claim.get("session_id") or ""),
                    other_run_id=str(claim.get("run_id")) if claim.get("run_id") is not None else None,
                    other_branch=str(claim.get("branch")) if claim.get("branch") is not None else None,
                    other_worktree=str(claim.get("worktree")) if claim.get("worktree") is not None else None,
                    other_claim_id=claim_id,
                    last_seen_at=str(claim.get("last_seen_at")) if claim.get("last_seen_at") is not None else None,
                )
            )
    return conflicts


def heartbeat(
    repo: Path,
    *,
    session_id: str,
    run_id: str | None,
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    if _disabled():
        return {"status": "disabled"}
    repo_resolved = repo.resolve()
    if is_bare_repo(repo_resolved):
        return {"status": "unsupported_repo"}
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return {"status": "unsupported_repo"}
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    state_path = directory / "state.json"
    if not state_path.exists():
        return {"status": "ok", "repo_key": key, "tracker_dir": str(directory), "updated_claim_count": 0}
    now = utc_now()
    events_path = directory / "events.jsonl"
    updated = 0
    try:
        with _exclusive_lock(directory / "state.lock"):
            state = _load_state(state_path)
            for claim in state.get("claims", []) or []:
                if not isinstance(claim, dict):
                    continue
                if claim.get("session_id") != session_id:
                    continue
                claim["last_seen_at"] = now
                if run_id is not None:
                    claim["run_id"] = run_id
                updated += 1
            if updated:
                _write_state(state_path, state)
    except TimeoutError:
        return {"status": "lock_timeout", "repo_key": key, "tracker_dir": str(directory)}
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        # Mirror register_claims: record known-failure modes and degrade
        # non-fatally; let true programming bugs propagate.
        try:
            _append_jsonl(
                events_path,
                {
                    "timestamp": now,
                    "event": "heartbeat_failed",
                    "session_id": session_id,
                    "run_id": run_id,
                    "error": repr(exc),
                },
            )
        except OSError:
            pass
        return {"status": "error", "repo_key": key, "tracker_dir": str(directory)}
    return {"status": "ok", "repo_key": key, "tracker_dir": str(directory), "updated_claim_count": updated}


def lease_acquire(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise NotImplementedError("lease_acquire is reserved for Phase 2 of the global reviewed-diff tracker")


def lease_refresh(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise NotImplementedError("lease_refresh is reserved for Phase 2 of the global reviewed-diff tracker")


def lease_release(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise NotImplementedError("lease_release is reserved for Phase 2 of the global reviewed-diff tracker")


def sweep_stale(*_args: Any, **_kwargs: Any) -> list[Any]:
    return []


def owned_units_from_manifest(manifest: dict[str, Any]) -> list[OwnedUnit]:
    units: list[OwnedUnit] = []
    seen: set[tuple[str, str, str]] = set()
    tracker = manifest.get("tracker") if isinstance(manifest.get("tracker"), dict) else None
    if tracker is not None:
        recorded_units = tracker.get("owned_units")
        if isinstance(recorded_units, list):
            for entry in recorded_units:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path")
                unit_kind = entry.get("unit")
                if not isinstance(path, str) or unit_kind not in {"hunk", "path"}:
                    continue
                anchor = HunkAnchor.from_dict(entry.get("hunk_anchor")) if unit_kind == "hunk" else None
                key = (path, unit_kind, anchor.header if anchor is not None else "")
                if key in seen:
                    continue
                seen.add(key)
                units.append(OwnedUnit(path=path, unit=unit_kind, hunk_anchor=anchor))
            if units:
                return units
    owned_paths = manifest.get("owned_paths") if isinstance(manifest.get("owned_paths"), list) else []
    for path in owned_paths:
        if not isinstance(path, str):
            continue
        key = (path, "path", "")
        if key in seen:
            continue
        seen.add(key)
        units.append(OwnedUnit(path=path, unit="path", hunk_anchor=None))
    return units
