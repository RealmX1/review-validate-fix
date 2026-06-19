#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import (
    _append_jsonl,
    _atomic_write_text,
    log_root,
    safe_token,
    utc_now,
)


SCHEMA_VERSION = 6
SQLITE_FILENAME = "tracker.sqlite3"
EVENTS_FILENAME = "events.jsonl"
META_FILENAME = "meta.json"
LEGACY_DIRNAME = "_legacy"
EVENTS_SCHEMA = "diff-tracker.v2"

# Phase 1 layout (kept around so we can find legacy state and migrate it).
LEGACY_TRACKER_SUBDIR = "tracker"

DEFAULT_BUSY_TIMEOUT_MS = 5000
BUSY_TIMEOUT_ENV = "CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS"

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
RANGE_TOLERANCE = 5

DISABLE_ENV = "CODEX_RVF_TRACKER_DISABLE"

# Slice 3 reason-code rename (with one-release alias). The new names belong to
# the `allocate_review_scope` path; the legacy names stay live in the
# `CODEX_RVF_TRACKER_DISABLE=1` fallback so disable-mode users see no behavior
# change.
REASON_NO_UNASSIGNED_REVIEW_SCOPE = "no_unassigned_review_scope"
REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE = "unassigned_review_scope_available"
LEGACY_REASON_NO_SESSION_OWNED_DIRTY = "no_session_owned_dirty"
LEGACY_REASON_SESSION_OWNED_DIRTY = "session_owned_dirty"

DEFAULT_LEASE_TTL_SECONDS = 600
LEASE_TTL_ENV = "CODEX_RVF_TRACKER_LEASE_TTL_SECONDS"
MANUAL_RUN_TTL_ENV = "CODEX_RVF_MANUAL_RUN_TTL_SECONDS"
REASON_MANUAL_SCOPE_ALREADY_COMPLETED = "manual_scope_already_completed"
REASON_MANUAL_TAKEOVER_COMPLETED = "manual_takeover_completed"


def _disabled() -> bool:
    # Only explicit truthy values disable the tracker. Previously this used a
    # blacklist (`value not in {"", "0", "false", "False"}`) which silently
    # disabled the tracker for any other non-empty string — including
    # `no` / `off` / `False` / `NO`, the exact opposite of user intent.
    value = os.environ.get(DISABLE_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _busy_timeout_ms() -> int:
    raw = os.environ.get(BUSY_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_BUSY_TIMEOUT_MS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BUSY_TIMEOUT_MS
    return max(0, value)


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


@dataclass(frozen=True)
class _UnitSpec:
    """One row destined for the `units` table, derived from observation."""
    unit_id: str
    path: str
    old_path: str | None
    kind: str
    change_type: str
    preimage_blob: str | None
    postimage_hash: str | None
    hunk_header: str | None


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


def _run_git_bytes(repo: Path, args: list[str]) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode("utf-8", "replace").strip()
            or f"git {' '.join(args)} failed"
        )
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
    return log_root_dir / "diff-tracker" / "repos" / key


def _legacy_tracker_dir(log_root_dir: Path, key: str) -> Path:
    return log_root_dir / LEGACY_TRACKER_SUBDIR / key


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


def derive_hunk_anchors(repo: Path, path: str, base_ref: str = "HEAD") -> list[HunkAnchor]:
    """Phase 1 anchor derivation, kept verbatim so manifest payloads (which
    serialize HunkAnchor) remain stable across versions. Used to map an OwnedUnit
    back onto a freshly observed hunk under Phase 2.

    `base_ref` is the git diff spec to compare against. It defaults to ``HEAD``
    (worktree-vs-HEAD, the original behaviour). Callers observing committed
    round work pass a two-dot range spec like ``"<baseline>..HEAD"`` so anchors
    are derived from the net committed diff instead of the dirty worktree."""
    try:
        diff = _run_git(repo, ["diff", "-U3", "--no-color", base_ref, "--", path])
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


def _hunk_anchors_match(left: HunkAnchor, right: HunkAnchor, *, strict: bool = False) -> bool:
    if left.header == right.header:
        return True
    if strict:
        return False
    if left.context_hash and left.context_hash == right.context_hash:
        if abs(left.old_range[0] - right.old_range[0]) <= RANGE_TOLERANCE:
            return True
    return False


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


# -------------------------- canonical hash helpers --------------------------

def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_payload_for_hunk(hunk_lines: list[str]) -> bytes:
    """Pure hunk content (context + +/- lines), stripped of @@ header. Joining
    with a newline keeps line-internal whitespace intact while staying
    independent of which specific @@ -A,B +C,D @@ git emitted."""
    return ("\n".join(hunk_lines)).encode("utf-8")


def _canonical_hash_tracked_hunk(path: str, change_type: str, hunk_lines: list[str]) -> str:
    parts = [b"hunk", path.encode("utf-8"), change_type.encode("utf-8"), _canonical_payload_for_hunk(hunk_lines)]
    return _sha256_hex(b"\0".join(parts))


def _canonical_hash_untracked(path: str, content_sha: str) -> str:
    return _sha256_hex(b"\0".join([b"untracked", path.encode("utf-8"), content_sha.encode("utf-8")]))


def _canonical_hash_deleted(path: str, preimage_blob: str) -> str:
    return _sha256_hex(b"\0".join([b"delete", path.encode("utf-8"), preimage_blob.encode("utf-8")]))


def _canonical_hash_binary(path: str, preimage_blob: str, postimage_sha: str) -> str:
    return _sha256_hex(
        b"\0".join(
            [b"binary", path.encode("utf-8"), preimage_blob.encode("utf-8"), postimage_sha.encode("utf-8")]
        )
    )


def _canonical_hash_path_only(path: str) -> str:
    return _sha256_hex(b"\0".join([b"path_only", path.encode("utf-8")]))


def _legacy_fallback_hash(path: str, header: str | None, claim_id: str) -> str:
    """Used when migrating Phase 1 claims whose hunk has since vanished from the
    worktree (committed / reverted). Tied to the original claim_id so re-running
    migration produces the same unit_id and the row stays idempotent."""
    return _sha256_hex(
        b"\0".join(
            [
                b"legacy",
                path.encode("utf-8"),
                (header or "").encode("utf-8"),
                claim_id.encode("utf-8"),
            ]
        )
    )


# -------------------------- observation derivation --------------------------

@dataclass(frozen=True)
class _ObservedHunk:
    anchor: HunkAnchor
    lines: tuple[str, ...]      # context + +/- lines, in diff order, no @@ header
    canonical_hash: str


@dataclass(frozen=True)
class _PathObservation:
    """Everything we observed for a single path on the worktree side. May
    contain multiple tracked_hunk rows when the diff has multiple hunks."""
    path: str
    kind: str                                 # see units.kind enum
    change_type: str                          # add/modify/delete
    preimage_blob: str | None
    postimage_hash: str | None
    old_path: str | None
    hunks: tuple[_ObservedHunk, ...] = ()     # only for tracked_hunk


def _resolve_blob_at_ref(repo: Path, ref: str, path: str) -> str | None:
    """Return the git blob oid for `path` at `ref` (e.g. ``HEAD`` or a commit
    sha), or None when the path does not exist there. Used for preimage/postimage
    identity on both the dirty side (ref=HEAD) and the committed-round side
    (ref=baseline for preimage, ref=HEAD for postimage)."""
    try:
        sha = _run_git(repo, ["rev-parse", f"{ref}:{path}"]).strip()
    except RuntimeError:
        return None
    return sha or None


def _resolve_blob_at_head(repo: Path, path: str) -> str | None:
    return _resolve_blob_at_ref(repo, "HEAD", path)


def _blob_content_sha_at_ref(repo: Path, ref: str, path: str) -> str | None:
    """sha256 of the bytes of `path` at `ref`, matching `_file_content_sha`'s
    digest so a binary/postimage observed first on the dirty side (worktree
    bytes) and later on the committed side (HEAD blob bytes) hash identically
    and dedup by unit_id. Returns None when the path is absent at `ref`."""
    try:
        out = _run_git_bytes(repo, ["cat-file", "-p", f"{ref}:{path}"])
    except RuntimeError:
        return None
    return hashlib.sha256(out).hexdigest()


def _file_content_sha(file_path: Path) -> str | None:
    try:
        with file_path.open("rb") as handle:
            digest = hashlib.sha256()
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def _is_binary_blob(repo: Path, sha: str) -> bool:
    try:
        # 8KB is enough to make git's binary heuristic reliable.
        out = _run_git_bytes(repo, ["cat-file", "-p", sha])
    except RuntimeError:
        return False
    return b"\0" in out[:8192]


def _parse_tracked_hunks(repo: Path, path: str, base_ref: str = "HEAD") -> list[_ObservedHunk]:
    """Parse `git diff -U3 <base_ref> -- path` into per-hunk observations whose
    canonical_hash is line-shift-stable.

    `base_ref` defaults to ``HEAD`` (worktree-vs-HEAD dirty diff). The committed
    classifier passes a two-dot range spec (``"<baseline>..HEAD"``) so the net
    committed-round hunk bodies are parsed; because the canonical hash hashes
    only `(path, change_type, hunk body)` — never the @@ header or the ref — a
    hunk observed dirty and the same hunk observed committed produce the same
    unit_id, which is what makes reviewed-then-committed dedup free."""
    try:
        diff = _run_git(repo, ["diff", "-U3", "--no-color", base_ref, "--", path])
    except RuntimeError:
        return []
    if not diff:
        return []
    hunks: list[_ObservedHunk] = []
    pending_header: tuple[tuple[int, int], tuple[int, int], str] | None = None
    pending_header_line: str = ""
    context_lines: list[str] = []      # for HunkAnchor.context_hash (first 3 only)
    body_lines: list[str] = []         # full hunk body (context + +/-)
    in_hunk = False

    def _flush() -> None:
        if pending_header is None:
            return
        old_range, new_range, _ = pending_header
        anchor = HunkAnchor(
            header=_normalize_header(pending_header_line),
            context_hash=_context_hash(context_lines),
            old_range=old_range,
            new_range=new_range,
        )
        canonical = _canonical_hash_tracked_hunk(path, "modify", body_lines)
        hunks.append(_ObservedHunk(anchor=anchor, lines=tuple(body_lines), canonical_hash=canonical))

    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git") or raw_line.startswith("index "):
            continue
        if raw_line.startswith("@@"):
            _flush()
            parsed = _hunk_header_parts(raw_line)
            if parsed is None:
                pending_header = None
                pending_header_line = ""
                context_lines = []
                body_lines = []
                in_hunk = False
                continue
            pending_header = parsed
            pending_header_line = raw_line
            context_lines = []
            body_lines = []
            in_hunk = True
            continue
        if not in_hunk or pending_header is None:
            continue
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        body_lines.append(raw_line)
        if not (raw_line.startswith("+") or raw_line.startswith("-")):
            if len(context_lines) < 3:
                context_lines.append(raw_line)
    _flush()
    return hunks


def _classify_path(repo: Path, path: str) -> _PathObservation | None:
    """Classify the worktree state of `path` into one of the unit kinds.
    Returns None when there's no observable change at all (caller falls back
    to path_only)."""
    try:
        status_out = _run_git(repo, ["status", "--porcelain=v1", "-z", "--", path])
    except RuntimeError:
        status_out = ""
    entry = ""
    if status_out:
        # `-z` separates entries with NUL; we only inspect the first record
        # because callers always pass a single `path` pathspec.
        entry = status_out.split("\0", 1)[0]

    code_x = entry[0] if len(entry) >= 1 else " "
    code_y = entry[1] if len(entry) >= 2 else " "

    abs_file = (repo / path).resolve() if not Path(path).is_absolute() else Path(path)

    # Untracked.
    if code_x == "?" and code_y == "?":
        content_sha = _file_content_sha(abs_file)
        if content_sha is None:
            return None
        return _PathObservation(
            path=path,
            kind="untracked_file",
            change_type="add",
            preimage_blob=None,
            postimage_hash=content_sha,
            old_path=None,
        )

    # Deletion (in worktree or staged).
    if code_x == "D" or code_y == "D":
        preimage = _resolve_blob_at_head(repo, path)
        if preimage is None:
            return None
        return _PathObservation(
            path=path,
            kind="deleted_file",
            change_type="delete",
            preimage_blob=preimage,
            postimage_hash=None,
            old_path=None,
        )

    # Rename detection deliberately omitted: callers always pass a single
    # `path` pathspec to `git status -z -- <path>`, which surfaces the
    # add/delete halves of a rename separately rather than an `R` row. The
    # surviving halves depend on whether the rename was staged:
    #   * worktree-only rename (`mv`): new path shows `??` → `untracked_file`,
    #     old path shows ` D` → `deleted_file`.
    #   * staged rename (`git mv`): new path shows `A ` and falls through to
    #     the `tracked_hunk` branch below (preimage_blob is None at HEAD, so
    #     change_type='add'); old path shows `D ` → `deleted_file`.
    # A future slice may re-add first-class rename units behind a schema bump
    # and `-M` diff.

    # Untracked file with a non-?? status row shouldn't happen — fall through
    # to tracked_hunk parsing for any remaining modify/add cases.

    preimage_blob = _resolve_blob_at_head(repo, path)
    # Detect binary modification: ask git diff --numstat — `-` `-` means binary.
    try:
        numstat = _run_git(repo, ["diff", "--numstat", "HEAD", "--", path]).strip()
    except RuntimeError:
        numstat = ""
    if numstat:
        cols = numstat.split("\t", 2)
        if len(cols) >= 2 and cols[0] == "-" and cols[1] == "-":
            postimage_sha = _file_content_sha(abs_file) or ""
            if preimage_blob is None and postimage_sha == "":
                return None
            return _PathObservation(
                path=path,
                kind="binary_file",
                change_type="modify" if preimage_blob else "add",
                preimage_blob=preimage_blob,
                postimage_hash=postimage_sha or None,
                old_path=None,
            )

    hunks = _parse_tracked_hunks(repo, path)
    if hunks:
        return _PathObservation(
            path=path,
            kind="tracked_hunk",
            change_type="modify" if preimage_blob else "add",
            preimage_blob=preimage_blob,
            postimage_hash=_file_content_sha(abs_file),
            old_path=None,
            hunks=tuple(hunks),
        )

    return None


def _specs_from_observation(observation: _PathObservation | None, path: str) -> list[_UnitSpec]:
    """Turn a `_PathObservation` into the `_UnitSpec` rows the observation walk
    upserts. Shared by the dirty walk and the committed-round walk so both
    derive identical unit_ids for identical change content. A None observation
    (no observable change) falls back to a single `path_only` spec, matching the
    dirty walk's historical behaviour for exec-only evidence."""
    if observation is None:
        return [
            _UnitSpec(
                unit_id=_canonical_hash_path_only(path),
                path=path,
                old_path=None,
                kind="path_only",
                change_type="modify",
                preimage_blob=None,
                postimage_hash=None,
                hunk_header=None,
            )
        ]
    if observation.kind == "tracked_hunk":
        return [
            _UnitSpec(
                unit_id=hunk.canonical_hash,
                path=path,
                old_path=None,
                kind="tracked_hunk",
                change_type=observation.change_type,
                preimage_blob=observation.preimage_blob,
                postimage_hash=observation.postimage_hash,
                hunk_header=hunk.anchor.header,
            )
            for hunk in observation.hunks
        ]
    if observation.kind == "untracked_file":
        return [
            _UnitSpec(
                unit_id=_canonical_hash_untracked(path, observation.postimage_hash or ""),
                path=path,
                old_path=None,
                kind="untracked_file",
                change_type="add",
                preimage_blob=None,
                postimage_hash=observation.postimage_hash,
                hunk_header=None,
            )
        ]
    if observation.kind == "deleted_file":
        return [
            _UnitSpec(
                unit_id=_canonical_hash_deleted(path, observation.preimage_blob or ""),
                path=path,
                old_path=None,
                kind="deleted_file",
                change_type="delete",
                preimage_blob=observation.preimage_blob,
                postimage_hash=None,
                hunk_header=None,
            )
        ]
    if observation.kind == "binary_file":
        return [
            _UnitSpec(
                unit_id=_canonical_hash_binary(
                    path,
                    observation.preimage_blob or "",
                    observation.postimage_hash or "",
                ),
                path=path,
                old_path=None,
                kind="binary_file",
                change_type=observation.change_type,
                preimage_blob=observation.preimage_blob,
                postimage_hash=observation.postimage_hash,
                hunk_header=None,
            )
        ]
    return [
        _UnitSpec(
            unit_id=_canonical_hash_path_only(path),
            path=path,
            old_path=None,
            kind="path_only",
            change_type="modify",
            preimage_blob=None,
            postimage_hash=None,
            hunk_header=None,
        )
    ]


def _classify_committed_path(repo: Path, path: str, baseline: str) -> _PathObservation | None:
    """Committed-round sibling of `_classify_path`. Where `_classify_path`
    reads worktree state (`git status`, the on-disk file) to classify a *dirty*
    change, this classifies the *net committed* change for `path` across the
    range ``baseline..HEAD``. State therefore comes from the two-tree diff, not
    the worktree:

    * change_type/kind from `git diff --name-status`/`--numstat baseline..HEAD`,
    * preimage from the blob at `baseline`, postimage from the blob at `HEAD`,
    * hunks from `_parse_tracked_hunks(base_ref="baseline..HEAD")`.

    Returns None when there is no net committed change for the path (e.g. it was
    committed and then reverted within the round, leaving the trees equal)."""
    spec_diff = f"{baseline}..HEAD"
    try:
        name_status = _run_git(
            repo, ["diff", "--no-renames", "--name-status", "-z", spec_diff, "--", path]
        )
    except RuntimeError:
        return None
    records = [item for item in name_status.split("\0") if item]
    status_code = records[0][0] if records and records[0] else ""

    preimage_blob = _resolve_blob_at_ref(repo, baseline, path)

    if status_code == "D":
        if preimage_blob is None:
            return None
        return _PathObservation(
            path=path,
            kind="deleted_file",
            change_type="delete",
            preimage_blob=preimage_blob,
            postimage_hash=None,
            old_path=None,
        )

    # Binary modification: `git diff --numstat` reports `-` `-` for both columns.
    try:
        numstat = _run_git(repo, ["diff", "--numstat", spec_diff, "--", path]).strip()
    except RuntimeError:
        numstat = ""
    if numstat:
        cols = numstat.split("\t", 2)
        if len(cols) >= 2 and cols[0] == "-" and cols[1] == "-":
            postimage_sha = _blob_content_sha_at_ref(repo, "HEAD", path) or ""
            if preimage_blob is None and postimage_sha == "":
                return None
            return _PathObservation(
                path=path,
                kind="binary_file",
                change_type="modify" if preimage_blob else "add",
                preimage_blob=preimage_blob,
                postimage_hash=postimage_sha or None,
                old_path=None,
            )

    hunks = _parse_tracked_hunks(repo, path, base_ref=spec_diff)
    if hunks:
        return _PathObservation(
            path=path,
            kind="tracked_hunk",
            # A committed add (absent at baseline) is still a tracked file at
            # HEAD, so it surfaces as tracked_hunk/change_type='add' rather than
            # an untracked_file — git diff renders it as one all-`+` hunk.
            change_type="modify" if preimage_blob else "add",
            preimage_blob=preimage_blob,
            postimage_hash=_blob_content_sha_at_ref(repo, "HEAD", path),
            old_path=None,
            hunks=tuple(hunks),
        )

    return None


def _list_committed_round_changed_paths(repo: Path, baseline: str) -> list[str]:
    """Paths whose content differs between the `baseline` tree and `HEAD`,
    restricted to the work introduced by *this round's own* first-parent,
    non-merge commits.

    Pillar ③ (exclude background WIP / base-branch sync): the ``baseline..HEAD``
    range already floors at this round's start, and when the round contains a
    merge (e.g. the agent ran `git merge main` / `git pull`) the first-parent +
    no-merges commit walk drops the merge commit and the base commits it imports
    via its second parent. The authoritative gate remains transcript attribution
    in `build_manifest` (`owned_paths ∩` this set); this function is the cheap
    structural pre-filter, not the scope decision itself."""
    try:
        net = _run_git(repo, ["diff", "--no-renames", "--name-only", "-z", f"{baseline}..HEAD"])
    except RuntimeError:
        return []
    net_paths = sorted({item for item in net.split("\0") if item})
    if not net_paths:
        return []
    # Fast path: when the round contains no merges, the net diff already
    # reflects only first-parent work — no allowlist filtering needed.
    try:
        total = _run_git(repo, ["rev-list", "--count", f"{baseline}..HEAD"]).strip()
        first_parent = _run_git(
            repo, ["rev-list", "--count", "--first-parent", "--no-merges", f"{baseline}..HEAD"]
        ).strip()
    except RuntimeError:
        return net_paths
    if total == first_parent:
        return net_paths
    # Merges present: keep only paths touched by first-parent non-merge commits.
    try:
        log_out = _run_git(
            repo,
            ["log", "--first-parent", "--no-merges", "--no-renames", "--name-only", "--format=", f"{baseline}..HEAD"],
        )
    except RuntimeError:
        return net_paths
    allowed = {line.strip() for line in log_out.splitlines() if line.strip()}
    return [path for path in net_paths if path in allowed]


def _unit_specs_for_owned(
    repo: Path,
    owned_unit: OwnedUnit,
    committed_baseline: str | None = None,
) -> list[_UnitSpec]:
    if committed_baseline:
        # Committed-round ownership: classify against the net committed diff so
        # the owned unit_ids match what the committed observation walk records.
        # The file is clean at HEAD, so the dirty classifier below would
        # degrade to path_only and mint a non-matching unit_id; instead we emit
        # one spec per committed hunk via the shared `_specs_from_observation`
        # (committed owned hints are always path-level, so hunk anchors are
        # moot here).
        return _specs_from_observation(
            _classify_committed_path(repo, owned_unit.path, committed_baseline),
            owned_unit.path,
        )
    observation = _classify_path(repo, owned_unit.path)
    if observation is None:
        return [
            _UnitSpec(
                unit_id=_canonical_hash_path_only(owned_unit.path),
                path=owned_unit.path,
                old_path=None,
                kind="path_only",
                change_type="modify",
                preimage_blob=None,
                postimage_hash=None,
                hunk_header=None,
            )
        ]
    if observation.kind == "tracked_hunk":
        if owned_unit.unit == "hunk" and owned_unit.hunk_anchor is not None:
            for hunk in observation.hunks:
                if _hunk_anchors_match(owned_unit.hunk_anchor, hunk.anchor):
                    return [
                        _UnitSpec(
                            unit_id=hunk.canonical_hash,
                            path=owned_unit.path,
                            old_path=None,
                            kind="tracked_hunk",
                            change_type=observation.change_type,
                            preimage_blob=observation.preimage_blob,
                            postimage_hash=observation.postimage_hash,
                            hunk_header=hunk.anchor.header,
                        )
                    ]
            # No hunk matches the anchor — owned hunk was rewritten or reverted
            # since the manifest snapshot. Fall back to path_only so the row
            # still represents *some* claim but doesn't pretend the original
            # hunk is still there.
            return [
                _UnitSpec(
                    unit_id=_canonical_hash_path_only(owned_unit.path),
                    path=owned_unit.path,
                    old_path=None,
                    kind="path_only",
                    change_type="modify",
                    preimage_blob=None,
                    postimage_hash=None,
                    hunk_header=None,
                )
            ]
        # path-level OwnedUnit on a file with multiple hunks: emit one row per hunk.
        return [
            _UnitSpec(
                unit_id=hunk.canonical_hash,
                path=owned_unit.path,
                old_path=None,
                kind="tracked_hunk",
                change_type=observation.change_type,
                preimage_blob=observation.preimage_blob,
                postimage_hash=observation.postimage_hash,
                hunk_header=hunk.anchor.header,
            )
            for hunk in observation.hunks
        ]

    if observation.kind == "untracked_file":
        return [
            _UnitSpec(
                unit_id=_canonical_hash_untracked(observation.path, observation.postimage_hash or ""),
                path=observation.path,
                old_path=None,
                kind="untracked_file",
                change_type="add",
                preimage_blob=None,
                postimage_hash=observation.postimage_hash,
                hunk_header=None,
            )
        ]
    if observation.kind == "deleted_file":
        return [
            _UnitSpec(
                unit_id=_canonical_hash_deleted(observation.path, observation.preimage_blob or ""),
                path=observation.path,
                old_path=None,
                kind="deleted_file",
                change_type="delete",
                preimage_blob=observation.preimage_blob,
                postimage_hash=None,
                hunk_header=None,
            )
        ]
    if observation.kind == "binary_file":
        return [
            _UnitSpec(
                unit_id=_canonical_hash_binary(
                    observation.path,
                    observation.preimage_blob or "",
                    observation.postimage_hash or "",
                ),
                path=observation.path,
                old_path=None,
                kind="binary_file",
                change_type=observation.change_type,
                preimage_blob=observation.preimage_blob,
                postimage_hash=observation.postimage_hash,
                hunk_header=None,
            )
        ]
    # Should be unreachable; keep as a safety net.
    return [
        _UnitSpec(
            unit_id=_canonical_hash_path_only(owned_unit.path),
            path=owned_unit.path,
            old_path=None,
            kind="path_only",
            change_type="modify",
            preimage_blob=None,
            postimage_hash=None,
            hunk_header=None,
        )
    ]


def unit_ids_for_owned_unit(repo: Path, owned_unit: OwnedUnit) -> list[str]:
    """Return current tracker unit ids represented by an OwnedUnit hint."""
    return [spec.unit_id for spec in _unit_specs_for_owned(repo.resolve(), owned_unit)]


# -------------------------- SQLite plumbing --------------------------

DDL = r"""
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
  branch_key     TEXT PRIMARY KEY,
  refname        TEXT NOT NULL,
  base_ref       TEXT,
  state          TEXT NOT NULL CHECK (state IN ('active','deleted')),
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
  worktree_key   TEXT PRIMARY KEY,
  worktree_path  TEXT NOT NULL,
  branch_key     TEXT,
  head_oid       TEXT,
  state          TEXT NOT NULL CHECK (state IN ('active','deleted')),
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  FOREIGN KEY (branch_key) REFERENCES branches(branch_key)
);

-- Slice 2-A note: `path_only` is the 5th legal kind, used as a fallback when
-- worktree state has no observable diff (e.g. exec-only chmod). path_only
-- conflicts with any other kind on the same path.
-- `renamed_file` / `change_type='rename'` are reserved for a future slice;
-- single-path pathspec status output never produces them today. Renames land
-- as two rows whose shape depends on staging:
--   * worktree-only rename (`mv`):  `untracked_file` (new) + `deleted_file` (old)
--   * staged rename (`git mv`):     `tracked_hunk` add (new) + `deleted_file` (old)
-- `old_path` stays nullable for that future slice.
CREATE TABLE IF NOT EXISTS units (
  unit_id              TEXT PRIMARY KEY,
  branch_key           TEXT,
  worktree_key         TEXT NOT NULL,
  path                 TEXT NOT NULL,
  old_path             TEXT,
  kind                 TEXT NOT NULL CHECK (kind IN
                         ('tracked_hunk','untracked_file','deleted_file','binary_file','path_only')),
  change_type          TEXT NOT NULL CHECK (change_type IN ('add','modify','delete')),
  preimage_blob        TEXT,
  postimage_hash       TEXT,
  hunk_header          TEXT,
  canonical_patch_hash TEXT NOT NULL,
  first_observed_at    TEXT NOT NULL,
  last_observed_at     TEXT NOT NULL,
  observed_state       TEXT NOT NULL CHECK (observed_state IN ('dirty','committed','superseded')),
  review_state         TEXT NOT NULL CHECK (review_state IN ('available','assigned','reviewed')),
  is_tombstoned        INTEGER NOT NULL DEFAULT 0 CHECK (is_tombstoned IN (0,1)),
  tombstoned_at        TEXT,
  tombstone_reason     TEXT,
  FOREIGN KEY (branch_key)   REFERENCES branches(branch_key)   ON DELETE SET NULL,
  FOREIGN KEY (worktree_key) REFERENCES worktrees(worktree_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_units_branch       ON units(branch_key, observed_state);
CREATE INDEX IF NOT EXISTS idx_units_worktree     ON units(worktree_key, observed_state);
CREATE INDEX IF NOT EXISTS idx_units_path         ON units(path);
CREATE INDEX IF NOT EXISTS idx_units_review_state ON units(review_state);
CREATE INDEX IF NOT EXISTS idx_units_tombstone    ON units(is_tombstoned, review_state);
CREATE INDEX IF NOT EXISTS idx_units_canonical    ON units(canonical_patch_hash);

CREATE TABLE IF NOT EXISTS sessions (
  session_id            TEXT PRIMARY KEY,
  current_worktree_key  TEXT NOT NULL,
  parent_session_id     TEXT,
  channel               TEXT,
  disabled              INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL,
  last_seen_at          TEXT NOT NULL,
  FOREIGN KEY (current_worktree_key) REFERENCES worktrees(worktree_key)
);
CREATE INDEX IF NOT EXISTS idx_sessions_parent    ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen_at);

CREATE TABLE IF NOT EXISTS session_units (
  session_id      TEXT NOT NULL,
  unit_id         TEXT NOT NULL,
  assignment_kind TEXT NOT NULL CHECK (assignment_kind IN ('owned','takeover','transferred')),
  assigned_at     TEXT NOT NULL,
  run_id          TEXT,
  branch          TEXT,
  worktree        TEXT,
  evidence        TEXT,
  last_seen_at    TEXT,
  PRIMARY KEY (session_id, unit_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
  FOREIGN KEY (unit_id)    REFERENCES units(unit_id)        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_session_units_unit ON session_units(unit_id);
CREATE INDEX IF NOT EXISTS idx_session_units_kind ON session_units(assignment_kind);

CREATE TABLE IF NOT EXISTS edit_claims (
  claim_id                 TEXT PRIMARY KEY,
  session_id               TEXT NOT NULL,
  run_id                   TEXT,
  tool_name                TEXT NOT NULL,
  call_id                  TEXT,
  transcript_line_number   INTEGER,
  path                     TEXT NOT NULL,
  hunk_index               INTEGER,
  operation                TEXT,
  status                   TEXT NOT NULL CHECK (status IN ('pending','reviewed','superseded')),
  latest_user_line_number  INTEGER,
  latest_user_message      TEXT,
  created_at               TEXT NOT NULL,
  last_seen_at             TEXT NOT NULL,
  reviewed_at              TEXT,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edit_claims_session_status ON edit_claims(session_id, status);
CREATE INDEX IF NOT EXISTS idx_edit_claims_path ON edit_claims(path);
CREATE INDEX IF NOT EXISTS idx_edit_claims_line ON edit_claims(transcript_line_number);

CREATE TABLE IF NOT EXISTS edit_claim_units (
  claim_id TEXT NOT NULL,
  unit_id  TEXT NOT NULL,
  PRIMARY KEY (claim_id, unit_id),
  FOREIGN KEY (claim_id) REFERENCES edit_claims(claim_id) ON DELETE CASCADE,
  FOREIGN KEY (unit_id)  REFERENCES units(unit_id)        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edit_claim_units_unit ON edit_claim_units(unit_id);

CREATE TABLE IF NOT EXISTS manual_rvf_runs (
  session_id   TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  scope_hash   TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  PRIMARY KEY (session_id, run_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS leases (
  lease_id          TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  run_id            TEXT NOT NULL,
  reviewer_id       TEXT NOT NULL,
  holder_kind       TEXT NOT NULL CHECK (holder_kind IN ('reviewer','validate-fix','manual')),
  scope_hash        TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN
                       ('active','paused','completed','stale-released','failed-released')),
  ttl_seconds       INTEGER NOT NULL,
  transcript_max_line_number INTEGER,
  created_at        TEXT NOT NULL,
  last_activity_at  TEXT NOT NULL,
  expires_at        TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_leases_state    ON leases(state, expires_at);
CREATE INDEX IF NOT EXISTS idx_leases_reviewer ON leases(reviewer_id, state);

CREATE TABLE IF NOT EXISTS lease_units (
  lease_id TEXT NOT NULL,
  unit_id  TEXT NOT NULL,
  PRIMARY KEY (lease_id, unit_id),
  FOREIGN KEY (lease_id) REFERENCES leases(lease_id) ON DELETE CASCADE,
  FOREIGN KEY (unit_id)  REFERENCES units(unit_id)   ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_lease_units_unit ON lease_units(unit_id);

CREATE TABLE IF NOT EXISTS lease_participants (
  lease_id         TEXT NOT NULL,
  reviewer_id      TEXT NOT NULL,
  run_id           TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('active','completed','failed')),
  joined_at        TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  finished_at      TEXT,
  release_reason   TEXT,
  owns_lease       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (lease_id, reviewer_id, run_id),
  FOREIGN KEY (lease_id) REFERENCES leases(lease_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_lease_participants_state ON lease_participants(lease_id, state);

CREATE TABLE IF NOT EXISTS tombstones (
  tombstone_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL CHECK (kind IN ('unit','lease','session','branch','worktree')),
  ref_id        TEXT NOT NULL,
  reason        TEXT NOT NULL,
  payload       TEXT NOT NULL,
  retired_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tombstones_expires ON tombstones(expires_at);

CREATE TABLE IF NOT EXISTS rvf_issues (
  issue_key     TEXT PRIMARY KEY,
  repo_key      TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  issue_id      TEXT NOT NULL,
  payload       TEXT NOT NULL,
  source_refs   TEXT NOT NULL,
  artifact_path TEXT,
  state         TEXT NOT NULL CHECK (state IN ('open','fixed','false_positive','elevated','failed','superseded')),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(run_id, issue_id)
);
CREATE INDEX IF NOT EXISTS idx_rvf_issues_run ON rvf_issues(run_id);

CREATE TABLE IF NOT EXISTS rvf_fix_attempts (
  attempt_id             TEXT PRIMARY KEY,
  issue_key              TEXT NOT NULL,
  repo_key               TEXT NOT NULL,
  run_id                 TEXT NOT NULL,
  issue_id               TEXT NOT NULL,
  worktree_path          TEXT NOT NULL,
  base_head              TEXT,
  baseline_overlay_path  TEXT,
  baseline_commit        TEXT,
  fix_patch_path         TEXT,
  status                 TEXT NOT NULL CHECK (status IN ('prepared','started','fixed','false_positive','elevated','failed','applied','merge_conflict')),
  result_payload         TEXT NOT NULL DEFAULT '{}',
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL,
  started_at             TEXT,
  stopped_at             TEXT,
  applied_at             TEXT,
  FOREIGN KEY(issue_key) REFERENCES rvf_issues(issue_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rvf_fix_attempts_run ON rvf_fix_attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_rvf_fix_attempts_issue ON rvf_fix_attempts(issue_key);

CREATE TABLE IF NOT EXISTS rvf_fix_patch_events (
  patch_event_id TEXT PRIMARY KEY,
  attempt_id     TEXT NOT NULL,
  issue_key      TEXT NOT NULL,
  repo_key       TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  issue_id       TEXT NOT NULL,
  path           TEXT NOT NULL,
  op             TEXT NOT NULL,
  call_id        TEXT,
  trajectory_ref TEXT,
  diff_ref       TEXT,
  created_at     TEXT NOT NULL,
  FOREIGN KEY(attempt_id) REFERENCES rvf_fix_attempts(attempt_id) ON DELETE CASCADE,
  FOREIGN KEY(issue_key) REFERENCES rvf_issues(issue_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rvf_fix_patch_events_attempt ON rvf_fix_patch_events(attempt_id);
CREATE INDEX IF NOT EXISTS idx_rvf_fix_patch_events_run ON rvf_fix_patch_events(run_id);

CREATE TABLE IF NOT EXISTS rvf_issue_patch_links (
  issue_key      TEXT NOT NULL,
  attempt_id     TEXT NOT NULL,
  patch_event_id TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY(issue_key, attempt_id, patch_event_id),
  FOREIGN KEY(issue_key) REFERENCES rvf_issues(issue_key) ON DELETE CASCADE,
  FOREIGN KEY(attempt_id) REFERENCES rvf_fix_attempts(attempt_id) ON DELETE CASCADE,
  FOREIGN KEY(patch_event_id) REFERENCES rvf_fix_patch_events(patch_event_id) ON DELETE CASCADE
);
"""


def _open_conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = _busy_timeout_ms()
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=timeout_ms / 1000.0)
    # Wrap every step after `connect` in try/except so any failure (busy
    # timeout exhaustion on `PRAGMA journal_mode = WAL`, a misbehaving
    # `PRAGMA foreign_keys`, schema-version mismatch in `_ensure_schema`)
    # closes the freshly-opened connection before propagating. Callers'
    # `try: conn = _open_conn(...) ... finally: conn.close()` guards leak
    # the underlying connection if `_open_conn` itself raises, because
    # `conn` stays unbound at the call site.
    try:
        conn.row_factory = sqlite3.Row
        # Set busy_timeout *first* so every subsequent PRAGMA / DDL waits on a
        # contended lock instead of failing immediately. The connect-time
        # `timeout=` argument seeds the same value, but issuing the PRAGMA
        # makes the contract explicit and survives any future driver tweak.
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        # `PRAGMA journal_mode = WAL` takes an EXCLUSIVE lock to flip the
        # journal header, so two concurrent first-writers on a fresh DB used
        # to race here and the loser surfaced as `lock_timeout` even with a
        # 30s busy_timeout (the connect-time timeout was bypassed by the
        # immediate-fail EXCLUSIVE acquisition path on some sqlite builds).
        # Read the mode first and only attempt to switch when needed; once
        # the file is in WAL, every later opener no-ops here. For the very
        # first opener we still need the write, so wrap it in a busy-aware
        # retry loop bounded by `busy_timeout`.
        cur = conn.execute("PRAGMA journal_mode")
        current_mode = (cur.fetchone() or [""])[0]
        if isinstance(current_mode, str) and current_mode.lower() != "wal":
            deadline = time.monotonic() + max(timeout_ms, 1) / 1000.0
            backoff = 0.01
            while True:
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if not _is_lock_busy(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 0.2)
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_schema(conn)
    except BaseException:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version")
    row = cur.fetchone()
    version = row[0] if row else 0
    if version == 0:
        conn.executescript(DDL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    if version == 2:
        _migrate_schema_v2_to_v3(conn)
        _migrate_schema_v3_to_v4(conn)
        _migrate_schema_v4_to_v5(conn)
        _migrate_schema_v5_to_v6(conn)
        return
    if version == 3:
        _migrate_schema_v3_to_v4(conn)
        _migrate_schema_v4_to_v5(conn)
        _migrate_schema_v5_to_v6(conn)
        return
    if version == 4:
        _migrate_schema_v4_to_v5(conn)
        _migrate_schema_v5_to_v6(conn)
        return
    if version == 5:
        _migrate_schema_v5_to_v6(conn)
        return
    if version == SCHEMA_VERSION:
        _ensure_unit_tombstone_schema(conn)
        _ensure_manual_rvf_runs_schema(conn)
        _ensure_lease_watermark_schema(conn)
        _ensure_lease_participants_schema(conn)
        _ensure_rvf_causality_schema(conn)
        _ensure_edit_claim_schema(conn)
        return
    raise RuntimeError(f"unknown tracker schema version: {version}")


def _ensure_manual_rvf_runs_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_rvf_runs (
          session_id   TEXT NOT NULL,
          run_id       TEXT NOT NULL,
          scope_hash   TEXT NOT NULL,
          completed_at TEXT NOT NULL,
          PRIMARY KEY (session_id, run_id),
          FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
        """
    )


def _ensure_lease_watermark_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(leases)").fetchall()}
    if "transcript_max_line_number" not in columns:
        conn.execute("ALTER TABLE leases ADD COLUMN transcript_max_line_number INTEGER")


def _ensure_lease_participants_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lease_participants (
          lease_id         TEXT NOT NULL,
          reviewer_id      TEXT NOT NULL,
          run_id           TEXT NOT NULL,
          state            TEXT NOT NULL CHECK (state IN ('active','completed','failed')),
          joined_at        TEXT NOT NULL,
          last_activity_at TEXT NOT NULL,
          finished_at      TEXT,
          release_reason   TEXT,
          owns_lease       INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (lease_id, reviewer_id, run_id),
          FOREIGN KEY (lease_id) REFERENCES leases(lease_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lease_participants_state
          ON lease_participants(lease_id, state)
        """
    )


def _ensure_edit_claim_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS edit_claims (
          claim_id                 TEXT PRIMARY KEY,
          session_id               TEXT NOT NULL,
          run_id                   TEXT,
          tool_name                TEXT NOT NULL,
          call_id                  TEXT,
          transcript_line_number   INTEGER,
          path                     TEXT NOT NULL,
          hunk_index               INTEGER,
          operation                TEXT,
          status                   TEXT NOT NULL CHECK (status IN ('pending','reviewed','superseded')),
          latest_user_line_number  INTEGER,
          latest_user_message      TEXT,
          created_at               TEXT NOT NULL,
          last_seen_at             TEXT NOT NULL,
          reviewed_at              TEXT,
          FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_edit_claims_session_status ON edit_claims(session_id, status);
        CREATE INDEX IF NOT EXISTS idx_edit_claims_path ON edit_claims(path);
        CREATE INDEX IF NOT EXISTS idx_edit_claims_line ON edit_claims(transcript_line_number);

        CREATE TABLE IF NOT EXISTS edit_claim_units (
          claim_id TEXT NOT NULL,
          unit_id  TEXT NOT NULL,
          PRIMARY KEY (claim_id, unit_id),
          FOREIGN KEY (claim_id) REFERENCES edit_claims(claim_id) ON DELETE CASCADE,
          FOREIGN KEY (unit_id)  REFERENCES units(unit_id)        ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_edit_claim_units_unit ON edit_claim_units(unit_id);
        """
    )


def _ensure_unit_tombstone_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)").fetchall()}
    if "is_tombstoned" not in columns:
        conn.execute(
            "ALTER TABLE units ADD COLUMN is_tombstoned INTEGER NOT NULL DEFAULT 0 CHECK (is_tombstoned IN (0,1))"
        )
    if "tombstoned_at" not in columns:
        conn.execute("ALTER TABLE units ADD COLUMN tombstoned_at TEXT")
    if "tombstone_reason" not in columns:
        conn.execute("ALTER TABLE units ADD COLUMN tombstone_reason TEXT")
    if _rebuild_units_without_legacy_tombstoned_review_state(conn):
        return
    conn.execute(
        """
        UPDATE units
           SET is_tombstoned=1,
               tombstone_reason=COALESCE(tombstone_reason, 'legacy_review_state_tombstoned')
         WHERE review_state='tombstoned'
        """
    )
    conn.execute(
        "UPDATE units SET review_state='reviewed' WHERE review_state='tombstoned'"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_units_tombstone
          ON units(is_tombstoned, review_state)
        """
    )


def _rebuild_units_without_legacy_tombstoned_review_state(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='units'"
    ).fetchone()
    table_sql = row["sql"] if row else ""
    if "'tombstoned'" not in table_sql:
        return False

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE units_v5 (
              unit_id              TEXT PRIMARY KEY,
              branch_key           TEXT,
              worktree_key         TEXT NOT NULL,
              path                 TEXT NOT NULL,
              old_path             TEXT,
              kind                 TEXT NOT NULL CHECK (kind IN
                                     ('tracked_hunk','untracked_file','deleted_file','binary_file','path_only')),
              change_type          TEXT NOT NULL CHECK (change_type IN ('add','modify','delete')),
              preimage_blob        TEXT,
              postimage_hash       TEXT,
              hunk_header          TEXT,
              canonical_patch_hash TEXT NOT NULL,
              first_observed_at    TEXT NOT NULL,
              last_observed_at     TEXT NOT NULL,
              observed_state       TEXT NOT NULL CHECK (observed_state IN ('dirty','committed','superseded')),
              review_state         TEXT NOT NULL CHECK (review_state IN ('available','assigned','reviewed')),
              is_tombstoned        INTEGER NOT NULL DEFAULT 0 CHECK (is_tombstoned IN (0,1)),
              tombstoned_at        TEXT,
              tombstone_reason     TEXT,
              FOREIGN KEY (branch_key)   REFERENCES branches(branch_key)   ON DELETE SET NULL,
              FOREIGN KEY (worktree_key) REFERENCES worktrees(worktree_key) ON DELETE CASCADE
            );

            INSERT INTO units_v5(
                unit_id, branch_key, worktree_key, path, old_path, kind, change_type,
                preimage_blob, postimage_hash, hunk_header, canonical_patch_hash,
                first_observed_at, last_observed_at, observed_state, review_state,
                is_tombstoned, tombstoned_at, tombstone_reason
            )
            SELECT
                unit_id, branch_key, worktree_key, path, old_path, kind, change_type,
                preimage_blob, postimage_hash, hunk_header, canonical_patch_hash,
                first_observed_at, last_observed_at, observed_state,
                CASE WHEN review_state='tombstoned' THEN 'reviewed' ELSE review_state END,
                CASE WHEN review_state='tombstoned' THEN 1 ELSE COALESCE(is_tombstoned, 0) END,
                tombstoned_at,
                CASE
                  WHEN review_state='tombstoned'
                    THEN COALESCE(tombstone_reason, 'legacy_review_state_tombstoned')
                  ELSE tombstone_reason
                END
              FROM units;

            DROP TABLE units;
            ALTER TABLE units_v5 RENAME TO units;
            CREATE INDEX IF NOT EXISTS idx_units_branch       ON units(branch_key, observed_state);
            CREATE INDEX IF NOT EXISTS idx_units_worktree     ON units(worktree_key, observed_state);
            CREATE INDEX IF NOT EXISTS idx_units_path         ON units(path);
            CREATE INDEX IF NOT EXISTS idx_units_review_state ON units(review_state);
            CREATE INDEX IF NOT EXISTS idx_units_tombstone    ON units(is_tombstoned, review_state);
            CREATE INDEX IF NOT EXISTS idx_units_canonical    ON units(canonical_patch_hash);
            """
        )
    finally:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"tracker schema migration left foreign key violations: {violations!r}")
    return True


def _migrate_schema_v2_to_v3(conn: sqlite3.Connection) -> None:
    _ensure_manual_rvf_runs_schema(conn)
    _ensure_lease_watermark_schema(conn)
    _ensure_lease_participants_schema(conn)
    conn.execute("PRAGMA user_version = 3")


def _ensure_rvf_causality_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rvf_issues (
          issue_key     TEXT PRIMARY KEY,
          repo_key      TEXT NOT NULL,
          run_id        TEXT NOT NULL,
          issue_id      TEXT NOT NULL,
          payload       TEXT NOT NULL,
          source_refs   TEXT NOT NULL,
          artifact_path TEXT,
          state         TEXT NOT NULL CHECK (state IN ('open','fixed','false_positive','elevated','failed','superseded')),
          created_at    TEXT NOT NULL,
          updated_at    TEXT NOT NULL,
          UNIQUE(run_id, issue_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rvf_issues_run ON rvf_issues(run_id);

        CREATE TABLE IF NOT EXISTS rvf_fix_attempts (
          attempt_id             TEXT PRIMARY KEY,
          issue_key              TEXT NOT NULL,
          repo_key               TEXT NOT NULL,
          run_id                 TEXT NOT NULL,
          issue_id               TEXT NOT NULL,
          worktree_path          TEXT NOT NULL,
          base_head              TEXT,
          baseline_overlay_path  TEXT,
          baseline_commit        TEXT,
          fix_patch_path         TEXT,
          status                 TEXT NOT NULL CHECK (status IN ('prepared','started','fixed','false_positive','elevated','failed','applied','merge_conflict')),
          result_payload         TEXT NOT NULL DEFAULT '{}',
          created_at             TEXT NOT NULL,
          updated_at             TEXT NOT NULL,
          started_at             TEXT,
          stopped_at             TEXT,
          applied_at             TEXT,
          FOREIGN KEY(issue_key) REFERENCES rvf_issues(issue_key) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_rvf_fix_attempts_run ON rvf_fix_attempts(run_id);
        CREATE INDEX IF NOT EXISTS idx_rvf_fix_attempts_issue ON rvf_fix_attempts(issue_key);

        CREATE TABLE IF NOT EXISTS rvf_fix_patch_events (
          patch_event_id TEXT PRIMARY KEY,
          attempt_id     TEXT NOT NULL,
          issue_key      TEXT NOT NULL,
          repo_key       TEXT NOT NULL,
          run_id         TEXT NOT NULL,
          issue_id       TEXT NOT NULL,
          path           TEXT NOT NULL,
          op             TEXT NOT NULL,
          call_id        TEXT,
          trajectory_ref TEXT,
          diff_ref       TEXT,
          created_at     TEXT NOT NULL,
          FOREIGN KEY(attempt_id) REFERENCES rvf_fix_attempts(attempt_id) ON DELETE CASCADE,
          FOREIGN KEY(issue_key) REFERENCES rvf_issues(issue_key) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_rvf_fix_patch_events_attempt ON rvf_fix_patch_events(attempt_id);
        CREATE INDEX IF NOT EXISTS idx_rvf_fix_patch_events_run ON rvf_fix_patch_events(run_id);

        CREATE TABLE IF NOT EXISTS rvf_issue_patch_links (
          issue_key      TEXT NOT NULL,
          attempt_id     TEXT NOT NULL,
          patch_event_id TEXT NOT NULL,
          created_at     TEXT NOT NULL,
          PRIMARY KEY(issue_key, attempt_id, patch_event_id),
          FOREIGN KEY(issue_key) REFERENCES rvf_issues(issue_key) ON DELETE CASCADE,
          FOREIGN KEY(attempt_id) REFERENCES rvf_fix_attempts(attempt_id) ON DELETE CASCADE,
          FOREIGN KEY(patch_event_id) REFERENCES rvf_fix_patch_events(patch_event_id) ON DELETE CASCADE
        );
        """
    )


def _migrate_schema_v3_to_v4(conn: sqlite3.Connection) -> None:
    _ensure_manual_rvf_runs_schema(conn)
    _ensure_lease_watermark_schema(conn)
    _ensure_lease_participants_schema(conn)
    _ensure_rvf_causality_schema(conn)
    conn.execute("PRAGMA user_version = 4")


def _migrate_schema_v4_to_v5(conn: sqlite3.Connection) -> None:
    _ensure_unit_tombstone_schema(conn)
    _ensure_manual_rvf_runs_schema(conn)
    _ensure_lease_watermark_schema(conn)
    _ensure_lease_participants_schema(conn)
    _ensure_rvf_causality_schema(conn)
    conn.execute("PRAGMA user_version = 5")


def _migrate_schema_v5_to_v6(conn: sqlite3.Connection) -> None:
    _ensure_unit_tombstone_schema(conn)
    _ensure_manual_rvf_runs_schema(conn)
    _ensure_lease_watermark_schema(conn)
    _ensure_lease_participants_schema(conn)
    _ensure_rvf_causality_schema(conn)
    _ensure_edit_claim_schema(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextlib.contextmanager
def _begin_immediate(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def _is_lock_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _ensure_meta(directory: Path, repo: Path, common_dir: Path, key: str) -> None:
    meta_path = directory / META_FILENAME
    if meta_path.exists():
        return
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo.resolve()),
        "git_common_dir": str(common_dir.resolve()),
        "repo_key": key,
        "created_at": utc_now(),
    }
    _atomic_write_text(
        meta_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _emit_event(events_path: Path, payload: dict[str, Any]) -> None:
    record = {"timestamp": utc_now(), "schema": EVENTS_SCHEMA, **payload}
    try:
        _append_jsonl(events_path, record)
    except OSError:
        pass


# -------------------------- upserts --------------------------

def _branch_key(refname: str | None) -> str | None:
    if refname is None:
        return None
    return hashlib.sha1(refname.encode("utf-8")).hexdigest()[:12]


def _worktree_key(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def _upsert_branch(conn: sqlite3.Connection, refname: str | None, now: str) -> str | None:
    if not refname:
        return None
    key = _branch_key(refname)
    conn.execute(
        """
        INSERT INTO branches(branch_key, refname, base_ref, state, first_seen_at, last_seen_at)
        VALUES (?, ?, NULL, 'active', ?, ?)
        ON CONFLICT(branch_key) DO UPDATE SET
            refname=excluded.refname,
            state='active',
            last_seen_at=excluded.last_seen_at
        """,
        (key, refname, now, now),
    )
    return key


def _upsert_worktree(
    conn: sqlite3.Connection,
    path: str,
    branch_key: str | None,
    head_oid: str | None,
    now: str,
) -> str:
    key = _worktree_key(path)
    conn.execute(
        """
        INSERT INTO worktrees(worktree_key, worktree_path, branch_key, head_oid, state, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?)
        ON CONFLICT(worktree_key) DO UPDATE SET
            worktree_path=excluded.worktree_path,
            branch_key=excluded.branch_key,
            head_oid=excluded.head_oid,
            state='active',
            last_seen_at=excluded.last_seen_at
        """,
        (key, path, branch_key, head_oid, now, now),
    )
    return key


def _upsert_session(
    conn: sqlite3.Connection,
    session_id: str,
    worktree_key: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions(session_id, current_worktree_key, parent_session_id, channel, disabled, created_at, last_seen_at)
        VALUES (?, ?, NULL, NULL, 0, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            current_worktree_key=excluded.current_worktree_key,
            last_seen_at=excluded.last_seen_at
        """,
        (session_id, worktree_key, now, now),
    )


def _upsert_unit(
    conn: sqlite3.Connection,
    spec: _UnitSpec,
    branch_key: str | None,
    worktree_key: str,
    now: str,
    observed_state: str = "dirty",
) -> None:
    """Upsert a unit row. `observed_state` defaults to ``'dirty'`` (the original
    behaviour); the committed-round observation source passes ``'committed'``.

    The conflict clause sets ``observed_state=excluded.observed_state`` (the
    value we tried to insert) rather than a hardcoded ``'dirty'``: this lets the
    committed walk record ``'committed'`` while the dirty walk — which runs last
    in `_observe_and_upsert_units_in_txn` — still wins for a path that is both
    committed and re-dirtied. Crucially `review_state` is NEVER written here, so
    a unit that was already ``reviewed`` stays reviewed when re-observed as
    committed: that is what keeps reviewed-then-committed work out of the
    candidate pool without any extra dedup logic."""
    conn.execute(
        """
        INSERT INTO units(
            unit_id, branch_key, worktree_key, path, old_path, kind, change_type,
            preimage_blob, postimage_hash, hunk_header, canonical_patch_hash,
            first_observed_at, last_observed_at, observed_state, review_state
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available')
        ON CONFLICT(unit_id) DO UPDATE SET
            branch_key=COALESCE(excluded.branch_key, units.branch_key),
            worktree_key=excluded.worktree_key,
            path=excluded.path,
            old_path=excluded.old_path,
            kind=excluded.kind,
            change_type=excluded.change_type,
            preimage_blob=COALESCE(excluded.preimage_blob, units.preimage_blob),
            postimage_hash=COALESCE(excluded.postimage_hash, units.postimage_hash),
            hunk_header=COALESCE(excluded.hunk_header, units.hunk_header),
            last_observed_at=excluded.last_observed_at,
            observed_state=excluded.observed_state,
            is_tombstoned=0,
            tombstoned_at=NULL,
            tombstone_reason=NULL
        """,
        (
            spec.unit_id,
            branch_key,
            worktree_key,
            spec.path,
            spec.old_path,
            spec.kind,
            spec.change_type,
            spec.preimage_blob,
            spec.postimage_hash,
            spec.hunk_header,
            spec.unit_id,
            now,
            now,
            observed_state,
        ),
    )


def _mark_unit_tombstoned_in_txn(
    conn: sqlite3.Connection,
    *,
    unit_id: str,
    reason: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        UPDATE units
           SET is_tombstoned=1,
               tombstoned_at=?,
               tombstone_reason=?
         WHERE unit_id=?
        """,
        (now_iso, reason, unit_id),
    )


def _upsert_session_unit(
    conn: sqlite3.Connection,
    session_id: str,
    unit_id: str,
    *,
    run_id: str | None,
    branch: str | None,
    worktree: str | None,
    evidence: str | None,
    now: str,
) -> bool:
    """Returns True if this is a new (session_id, unit_id) row."""
    cur = conn.execute(
        "SELECT 1 FROM session_units WHERE session_id=? AND unit_id=?",
        (session_id, unit_id),
    )
    existed = cur.fetchone() is not None
    conn.execute(
        """
        INSERT INTO session_units(session_id, unit_id, assignment_kind, assigned_at, run_id, branch, worktree, evidence, last_seen_at)
        VALUES (?, ?, 'owned', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, unit_id) DO UPDATE SET
            run_id=COALESCE(excluded.run_id, session_units.run_id),
            branch=COALESCE(excluded.branch, session_units.branch),
            worktree=COALESCE(excluded.worktree, session_units.worktree),
            evidence=COALESCE(excluded.evidence, session_units.evidence),
            last_seen_at=excluded.last_seen_at
        """,
        (session_id, unit_id, now, run_id, branch, worktree, evidence, now),
    )
    return not existed


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _unit_ids_from_claim_payload(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _upsert_edit_claims_in_txn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    run_id: str | None,
    edit_claims: list[dict[str, Any]],
    now: str,
) -> int:
    inserted_or_updated = 0
    for claim in edit_claims:
        if not isinstance(claim, dict):
            continue
        claim_id = _string_or_none(claim.get("claim_id"))
        path = _string_or_none(claim.get("path"))
        tool_name = _string_or_none(claim.get("tool_name")) or "unknown"
        if claim_id is None or path is None:
            continue
        call_id = _string_or_none(claim.get("call_id"))
        operation = _string_or_none(claim.get("operation"))
        transcript_line_number = _int_or_none(claim.get("transcript_line_number"))
        hunk_index = _int_or_none(claim.get("hunk_index"))
        latest_user_line_number = _int_or_none(claim.get("latest_user_line_number"))
        latest_user_message = _string_or_none(claim.get("latest_user_message"))
        claim_run_id = _string_or_none(claim.get("run_id")) or run_id

        conn.execute(
            """
            INSERT INTO edit_claims(
                claim_id, session_id, run_id, tool_name, call_id,
                transcript_line_number, path, hunk_index, operation, status,
                latest_user_line_number, latest_user_message,
                created_at, last_seen_at, reviewed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, NULL)
            ON CONFLICT(claim_id) DO UPDATE SET
                session_id=excluded.session_id,
                run_id=COALESCE(excluded.run_id, edit_claims.run_id),
                tool_name=excluded.tool_name,
                call_id=COALESCE(excluded.call_id, edit_claims.call_id),
                transcript_line_number=COALESCE(excluded.transcript_line_number, edit_claims.transcript_line_number),
                path=excluded.path,
                hunk_index=COALESCE(excluded.hunk_index, edit_claims.hunk_index),
                operation=COALESCE(excluded.operation, edit_claims.operation),
                status=CASE
                    WHEN edit_claims.status='reviewed' THEN 'reviewed'
                    ELSE 'pending'
                END,
                latest_user_line_number=COALESCE(excluded.latest_user_line_number, edit_claims.latest_user_line_number),
                latest_user_message=COALESCE(excluded.latest_user_message, edit_claims.latest_user_message),
                last_seen_at=excluded.last_seen_at
            """,
            (
                claim_id,
                session_id,
                claim_run_id,
                tool_name,
                call_id,
                transcript_line_number,
                path,
                hunk_index,
                operation,
                latest_user_line_number,
                latest_user_message,
                now,
                now,
            ),
        )
        conn.execute("DELETE FROM edit_claim_units WHERE claim_id=?", (claim_id,))
        for unit_id in _unit_ids_from_claim_payload(claim.get("mapped_unit_ids")):
            conn.execute(
                """
                INSERT OR IGNORE INTO edit_claim_units(claim_id, unit_id)
                VALUES (?, ?)
                """,
                (claim_id, unit_id),
            )
        inserted_or_updated += 1
    return inserted_or_updated


# -------------------------- Phase 1 → SQLite migration --------------------------

def _migrate_phase1_if_needed(
    *,
    repo: Path,
    common_dir: Path,
    key: str,
    log_root_dir: Path,
    new_dir: Path,
    conn: sqlite3.Connection,
    events_path: Path,
) -> "Callable[[], None] | None":
    """Lazy idempotent migration of Phase 1 JSON state into the SQLite store.

    DB writes happen inside the caller's transaction (this function MUST be
    invoked under `_begin_immediate(conn)`). The legacy filesystem archival
    step (`shutil.move` of state.json/events.jsonl/...) is *deferred* and
    returned as a `post_commit` callable; the caller MUST invoke it after
    the surrounding transaction commits. This split is what makes the
    migration crash-safe: if the process dies between the COMMIT and the
    archive, the next opener sees `migrated_from` already in meta and the
    legacy files still live on disk — the early-exit short-circuit below
    has been narrowed accordingly so we drive the archive to completion.

    Returns None when nothing to do; otherwise returns the deferred archival
    callable. The caller may invoke it unconditionally (it's a no-op when
    the work is already done) or skip it when the transaction was rolled
    back (in which case the next opener will retry the whole thing).
    """
    legacy_dir = _legacy_tracker_dir(log_root_dir, key)
    legacy_state = legacy_dir / "state.json"
    legacy_archive = legacy_dir / LEGACY_DIRNAME
    legacy_archive_state = legacy_archive / "state.json"

    # Recovery path: the legacy live file may already be archived (a previous
    # call's deferred archive step ran). If the DB also has `migrated_from`
    # there's nothing to do. If the DB *doesn't* have `migrated_from` but
    # an archived copy survives, treat the archive as the source of truth
    # and re-import — this guards against the historical bug where the
    # archive moved inside a transaction that later rolled back.
    if not legacy_state.exists():
        if legacy_archive_state.exists():
            cur = conn.execute("SELECT 1 FROM meta WHERE key='migrated_from'")
            if cur.fetchone() is not None:
                return None
            # Re-import from the archive.
            legacy_state = legacy_archive_state
        else:
            return None

    legacy_events = legacy_dir / "events.jsonl"
    legacy_meta = legacy_dir / "meta.json"
    legacy_lock = legacy_dir / "state.lock"

    # Was this Phase 1 → Phase 2 transition already archived on disk? We
    # still allow falling through when the DB never recorded the import,
    # so we re-import from the archive on the recovery path.
    already_archived = legacy_archive.exists()
    # When the recovery branch above re-pointed legacy_state at the archive
    # itself, there is nothing to move; the archive *is* the source of truth.
    reading_from_archive = legacy_state == legacy_archive_state
    if reading_from_archive:
        already_archived = True
        # Recovery: the live events.jsonl was moved into _legacy/ by a prior
        # _post_commit before the crash, so reading from `legacy_dir/events.jsonl`
        # would find an empty file. Point at the archived copy so the replay
        # loop in _post_commit appends phase1 history into the new events.jsonl
        # — keeping the recovery branch aligned with the normal migration path
        # on events.jsonl side-effects (DB state was already aligned).
        legacy_events = legacy_archive / "events.jsonl"

    legacy_payload = _read_legacy_state(legacy_state)
    legacy_event_lines = _read_legacy_events(legacy_events) if legacy_events.exists() else []

    cur = conn.execute("SELECT 1 FROM meta WHERE key='migrated_from'")
    already_imported = cur.fetchone() is not None

    if already_archived and already_imported:
        return None

    now = utc_now()
    _emit_event(
        events_path,
        {
            "event": "migration_started",
            "from": "json-v1",
            "legacy_dir": str(legacy_dir),
            "claim_count": len(legacy_payload.get("claims", []) or []),
        },
    )

    repo_resolved = repo.resolve()

    # Build lookup of observed canonical_hash per (path, anchor.header) so we
    # can preserve unit identity wherever the worktree still exhibits the hunk.
    observation_index: dict[tuple[str, str], _ObservedHunk] = {}
    observed_paths: dict[str, _PathObservation | None] = {}
    for claim in legacy_payload.get("claims", []) or []:
        if not isinstance(claim, dict):
            continue
        path = claim.get("path")
        if not isinstance(path, str) or path in observed_paths:
            continue
        observed_paths[path] = _classify_path(repo_resolved, path)
    for path, observation in observed_paths.items():
        if observation is None or observation.kind != "tracked_hunk":
            continue
        for hunk in observation.hunks:
            observation_index[(path, hunk.anchor.header)] = hunk

    if not already_imported:
        branch = _current_branch(repo_resolved)
        branch_key = _upsert_branch(conn, branch, now) if branch else None
        worktree_path = str(repo_resolved)
        worktree_key = _upsert_worktree(conn, worktree_path, branch_key, None, now)

        for claim in legacy_payload.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            path = claim.get("path")
            if not isinstance(path, str):
                continue
            session_id = str(claim.get("session_id") or "")
            if not session_id:
                continue
            claim_id = str(claim.get("claim_id") or "")
            assigned_at = str(claim.get("claimed_at") or now)
            anchor_payload = claim.get("hunk_anchor")
            anchor_header = None
            if isinstance(anchor_payload, dict):
                anchor_header = anchor_payload.get("header") if isinstance(anchor_payload.get("header"), str) else None

            unit_kind = claim.get("unit") or "path"
            spec: _UnitSpec
            observed_state = "dirty"
            if unit_kind == "hunk" and anchor_header is not None and (path, anchor_header) in observation_index:
                hunk = observation_index[(path, anchor_header)]
                spec = _UnitSpec(
                    unit_id=hunk.canonical_hash,
                    path=path,
                    old_path=None,
                    kind="tracked_hunk",
                    change_type="modify",
                    preimage_blob=None,
                    postimage_hash=None,
                    hunk_header=hunk.anchor.header,
                )
            else:
                # Either path-level claim or hunk no longer present. Mint a
                # legacy-fallback unit_id so re-running migration is stable.
                unit_id = _legacy_fallback_hash(path, anchor_header, claim_id)
                spec = _UnitSpec(
                    unit_id=unit_id,
                    path=path,
                    old_path=None,
                    kind="path_only",
                    change_type="modify",
                    preimage_blob=None,
                    postimage_hash=None,
                    hunk_header=anchor_header,
                )
                observed_state = "superseded"

            _upsert_unit(conn, spec, branch_key, worktree_key, now)
            if observed_state != "dirty":
                conn.execute(
                    "UPDATE units SET observed_state=? WHERE unit_id=?",
                    (observed_state, spec.unit_id),
                )
            _upsert_session(conn, session_id, worktree_key, now)
            _upsert_session_unit(
                conn,
                session_id,
                spec.unit_id,
                run_id=str(claim.get("run_id")) if claim.get("run_id") is not None else None,
                branch=str(claim.get("branch")) if claim.get("branch") is not None else None,
                worktree=str(claim.get("worktree")) if claim.get("worktree") is not None else None,
                evidence=str(claim.get("evidence")) if claim.get("evidence") is not None else None,
                now=assigned_at,
            )
            # Preserve original assignment timestamp (overrides upsert default).
            conn.execute(
                "UPDATE session_units SET assigned_at=? WHERE session_id=? AND unit_id=?",
                (assigned_at, session_id, spec.unit_id),
            )

        for tomb in legacy_payload.get("tombstones", []) or []:
            if not isinstance(tomb, dict):
                continue
            ref = str(tomb.get("claim_id") or "")
            payload_text = json.dumps(tomb, ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                INSERT INTO tombstones(kind, ref_id, reason, payload, retired_at, expires_at)
                VALUES ('unit', ?, ?, ?, ?, ?)
                """,
                (
                    ref,
                    str(tomb.get("reason") or "phase1_migration_archive"),
                    payload_text,
                    str(tomb.get("dropped_at") or now),
                    str(tomb.get("dropped_at") or now),
                ),
            )

        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("migrated_from", "json-v1"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("migrated_at", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("legacy_archive", str(legacy_archive)),
        )

    # All filesystem mutations (archive move + meta.json stamp + replay of
    # legacy events into the new events.jsonl) are deferred to the
    # post-commit callable below. Doing them here would risk losing the
    # legacy state.json if the surrounding transaction rolled back after
    # `shutil.move` already ran — the original Phase-1 → Phase-2 bug.
    claim_count = len(legacy_payload.get("claims", []) or [])
    captured_repo = repo_resolved

    def _post_commit() -> None:
        if not already_archived:
            try:
                legacy_archive.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
            for src in (legacy_state, legacy_events, legacy_meta, legacy_lock):
                if not src.exists():
                    continue
                dst = legacy_archive / src.name
                if dst.exists():
                    # Recovery path may find both src and dst; treat dst as
                    # authoritative and just unlink the live copy.
                    try:
                        src.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                try:
                    shutil.move(str(src), str(dst))
                except OSError:
                    try:
                        shutil.copy2(str(src), str(dst))
                        src.unlink(missing_ok=True)
                    except OSError:
                        pass

        # Append legacy events to the new events.jsonl so `tail -f` still
        # surfaces them after migration. Idempotency: only do this when we
        # actually carried legacy events on this call.
        for line in legacy_event_lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload.setdefault("schema", "diff-tracker.v1")
            payload.setdefault("event", payload.get("kind", "legacy"))
            try:
                with events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({**payload, "imported_from": "phase1"}, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError:
                break

        _ensure_meta(new_dir, captured_repo, common_dir, key)
        # Stamp meta.json with migration metadata.
        try:
            meta_payload = json.loads((new_dir / META_FILENAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta_payload = {}
        if not isinstance(meta_payload, dict):
            meta_payload = {}
        meta_payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "migrated_from": "json-v1",
                "migrated_at": now,
                "legacy_archive": str(legacy_archive),
            }
        )
        try:
            _atomic_write_text(
                new_dir / META_FILENAME,
                json.dumps(meta_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        except OSError:
            pass

        _emit_event(
            events_path,
            {
                "event": "migration_completed",
                "from": "json-v1",
                "imported_claim_count": claim_count,
            },
        )

    return _post_commit


def _read_legacy_state(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"claims": [], "tombstones": []}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"claims": [], "tombstones": []}
    if not isinstance(payload, dict):
        return {"claims": [], "tombstones": []}
    if not isinstance(payload.get("claims"), list):
        payload["claims"] = []
    if not isinstance(payload.get("tombstones"), list):
        payload["tombstones"] = []
    return payload


def _read_legacy_events(path: Path) -> list[str]:
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


# -------------------------- public API --------------------------

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
    owned_units_override: list[tuple[OwnedUnit, str]] | None = None,
    log_root_override: Path | None = None,
    committed_paths: set[str] | None = None,
    committed_baseline: str | None = None,
) -> RegisterResult:
    # `committed_paths` are owned paths whose round work lives in committed
    # history (clean at HEAD); for them ownership is classified against
    # `committed_baseline` (range `<baseline>..HEAD`) instead of the worktree,
    # and the unit row is recorded as observed_state='committed' so it matches
    # the committed observation walk. Empty/None => identical to prior behaviour.
    committed_path_set = {p for p in (committed_paths or set()) if isinstance(p, str)}
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
    db_path = directory / SQLITE_FILENAME
    events_path = directory / EVENTS_FILENAME

    paths_list = sorted({path for path in owned_paths if isinstance(path, str) and path.strip()})
    if not paths_list:
        # Empty owned_paths is a no-op for the tracker; explicit drops belong to
        # a release_claims API. Falling through would orphan every existing
        # claim for this session.
        _ensure_meta(directory, repo_resolved, common_dir, key)
        return RegisterResult(status="no_paths", repo_key=key, tracker_dir=str(directory))

    if owned_units_override is None:
        units = _build_owned_units(
            repo_resolved,
            owned_paths=paths_list,
            apply_patch_paths={path for path in apply_patch_paths if isinstance(path, str)},
            exec_only_paths={path for path in exec_only_paths if isinstance(path, str)},
        )
    else:
        units = [
            (owned_unit, evidence)
            for owned_unit, evidence in owned_units_override
            if isinstance(owned_unit, OwnedUnit) and isinstance(evidence, str)
        ]

    branch_value = branch if branch is not None else _current_branch(repo_resolved)
    worktree_value = str(worktree.resolve()) if worktree is not None else str(repo_resolved)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        migration_finalize: Callable[[], None] | None = None
        with _begin_immediate(conn):
            migration_finalize = _migrate_phase1_if_needed(
                repo=repo_resolved,
                common_dir=common_dir,
                key=key,
                log_root_dir=base,
                new_dir=directory,
                conn=conn,
                events_path=events_path,
            )

            now = _tracker_now_iso()
            branch_key = _upsert_branch(conn, branch_value, now) if branch_value else None
            worktree_key = _upsert_worktree(conn, worktree_value, branch_key, None, now)
            _upsert_session(conn, session_id, worktree_key, now)

            new_claim_ids: list[str] = []
            existing_unit_ids: set[str] = {
                row["unit_id"]
                for row in conn.execute(
                    "SELECT unit_id FROM session_units WHERE session_id=? AND assignment_kind='owned'",
                    (session_id,),
                )
            }
            current_unit_ids: set[str] = set()

            for owned_unit, evidence in units:
                is_committed = bool(committed_baseline) and owned_unit.path in committed_path_set
                specs = _unit_specs_for_owned(
                    repo_resolved,
                    owned_unit,
                    committed_baseline=committed_baseline if is_committed else None,
                )
                spec_observed_state = "committed" if is_committed else "dirty"
                for spec in specs:
                    _upsert_unit(
                        conn, spec, branch_key, worktree_key, now, observed_state=spec_observed_state
                    )
                    is_new = _upsert_session_unit(
                        conn,
                        session_id,
                        spec.unit_id,
                        run_id=run_id,
                        branch=branch_value,
                        worktree=worktree_value,
                        evidence=evidence,
                        now=now,
                    )
                    new_claim_ids.append(spec.unit_id)
                    current_unit_ids.add(spec.unit_id)
                    if is_new:
                        _emit_event(
                            events_path,
                            {
                                "event": "claim_added",
                                "unit_id": spec.unit_id,
                                "session_id": session_id,
                                "run_id": run_id,
                                "path": spec.path,
                                "kind": spec.kind,
                                "evidence": evidence,
                            },
                        )

            dropped_stale: list[str] = []
            for orphan_unit_id in existing_unit_ids - current_unit_ids:
                conn.execute(
                    "DELETE FROM session_units WHERE session_id=? AND unit_id=?",
                    (session_id, orphan_unit_id),
                )
                conn.execute(
                    "UPDATE units SET observed_state='superseded' WHERE unit_id=?",
                    (orphan_unit_id,),
                )
                _mark_unit_tombstoned_in_txn(
                    conn,
                    unit_id=orphan_unit_id,
                    reason="session_no_longer_owns",
                    now_iso=now,
                )
                conn.execute(
                    """
                    INSERT INTO tombstones(kind, ref_id, reason, payload, retired_at, expires_at)
                    VALUES ('unit', ?, 'session_no_longer_owns', ?, ?, ?)
                    """,
                    (
                        orphan_unit_id,
                        json.dumps(
                            {"unit_id": orphan_unit_id, "session_id": session_id, "run_id": run_id},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
                dropped_stale.append(orphan_unit_id)
                _emit_event(
                    events_path,
                    {
                        "event": "claim_dropped",
                        "unit_id": orphan_unit_id,
                        "session_id": session_id,
                        "run_id": run_id,
                        "reason": "session_no_longer_owns",
                    },
                )

        # Run the deferred legacy archival now that the txn has committed.
        # If this raises (or the process dies mid-way), the next opener will
        # rerun it from the recovery branch in `_migrate_phase1_if_needed`.
        if migration_finalize is not None:
            try:
                migration_finalize()
            except Exception:
                pass
        _ensure_meta(directory, repo_resolved, common_dir, key)
        return RegisterResult(
            status="ok",
            repo_key=key,
            tracker_dir=str(directory),
            claim_ids=new_claim_ids,
            dropped_stale_claim_ids=dropped_stale,
        )
    except sqlite3.OperationalError as exc:
        if _is_lock_busy(exc):
            _emit_event(
                events_path,
                {
                    "event": "lock_timeout",
                    "session_id": session_id,
                    "run_id": run_id,
                    "owned_path_count": len(paths_list),
                },
            )
            return RegisterResult(status="lock_timeout", repo_key=key, tracker_dir=str(directory))
        _emit_event(
            events_path,
            {
                "event": "register_failed",
                "session_id": session_id,
                "run_id": run_id,
                "owned_path_count": len(paths_list),
                "error": repr(exc),
            },
        )
        return RegisterResult(status="error", repo_key=key, tracker_dir=str(directory))
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        _emit_event(
            events_path,
            {
                "event": "register_failed",
                "session_id": session_id,
                "run_id": run_id,
                "owned_path_count": len(paths_list),
                "error": repr(exc),
            },
        )
        return RegisterResult(status="error", repo_key=key, tracker_dir=str(directory))
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def register_edit_claims(
    *,
    repo: Path,
    session_id: str,
    run_id: str | None,
    edit_claims: list[dict[str, Any]],
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    repo_resolved = repo.resolve()
    if _disabled():
        return {"status": "disabled"}
    if is_bare_repo(repo_resolved):
        return {"status": "unsupported_repo"}
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return {"status": "unsupported_repo"}
    if not edit_claims:
        return {"status": "no_claims", "registered_count": 0}

    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / SQLITE_FILENAME
    events_path = directory / EVENTS_FILENAME

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            now = _tracker_now_iso()
            branch_value = _current_branch(repo_resolved)
            branch_key = _upsert_branch(conn, branch_value, now) if branch_value else None
            worktree_key = _upsert_worktree(conn, str(repo_resolved), branch_key, None, now)
            _upsert_session(conn, session_id, worktree_key, now)
            registered_count = _upsert_edit_claims_in_txn(
                conn,
                session_id=session_id,
                run_id=run_id,
                edit_claims=edit_claims,
                now=now,
            )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "edit_claims_registered",
                "session_id": session_id,
                "run_id": run_id,
                "registered_count": registered_count,
            },
        )
        return {
            "status": "ok",
            "registered_count": registered_count,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    except sqlite3.OperationalError as exc:
        if _is_lock_busy(exc):
            _emit_event(
                events_path,
                {
                    "event": "edit_claims_register_lock_timeout",
                    "session_id": session_id,
                    "run_id": run_id,
                    "claim_count": len(edit_claims),
                },
            )
            return {"status": "lock_timeout", "repo_key": key, "tracker_dir": str(directory)}
        _emit_event(
            events_path,
            {
                "event": "edit_claims_register_failed",
                "session_id": session_id,
                "run_id": run_id,
                "claim_count": len(edit_claims),
                "error": repr(exc),
            },
        )
        return {"status": "error", "repo_key": key, "tracker_dir": str(directory)}
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        _emit_event(
            events_path,
            {
                "event": "edit_claims_register_failed",
                "session_id": session_id,
                "run_id": run_id,
                "claim_count": len(edit_claims),
                "error": repr(exc),
            },
        )
        return {"status": "error", "repo_key": key, "tracker_dir": str(directory)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def latest_transcript_watermark(
    *,
    repo: Path,
    session_id: str,
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    repo_resolved = repo.resolve()
    if _disabled():
        return {"status": "disabled", "transcript_max_line_number": None}
    if not session_id:
        return {"status": "missing_session_id", "transcript_max_line_number": None}
    if is_bare_repo(repo_resolved):
        return {"status": "unsupported_repo", "transcript_max_line_number": None}
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return {"status": "unsupported_repo", "transcript_max_line_number": None}

    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    db_path = directory / SQLITE_FILENAME
    if not db_path.exists():
        return {
            "status": "no_tracker_db",
            "repo_key": key,
            "tracker_dir": str(directory),
            "session_id": session_id,
            "transcript_max_line_number": None,
        }

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        row = conn.execute(
            """
            SELECT MAX(transcript_max_line_number) AS transcript_max_line_number
              FROM leases
             WHERE session_id=?
               AND state IN ('active','paused','completed')
               AND transcript_max_line_number IS NOT NULL
            """,
            (session_id,),
        ).fetchone()
        value = row["transcript_max_line_number"] if row is not None else None
        return {
            "status": "ok",
            "repo_key": key,
            "tracker_dir": str(directory),
            "session_id": session_id,
            "transcript_max_line_number": int(value) if value is not None else None,
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    db_path = directory / SQLITE_FILENAME
    events_path = directory / EVENTS_FILENAME

    legacy_state = _legacy_tracker_dir(base, key) / "state.json"
    if not db_path.exists() and not legacy_state.exists():
        return []

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        # Folding migration into a read path is intentional: list_conflicts is
        # the most likely caller in cross-session smoke tests, and we want
        # legacy state surfaced even if no register happens first.
        migration_finalize: Callable[[], None] | None = None
        with _begin_immediate(conn):
            migration_finalize = _migrate_phase1_if_needed(
                repo=repo_resolved,
                common_dir=common_dir,
                key=key,
                log_root_dir=base,
                new_dir=directory,
                conn=conn,
                events_path=events_path,
            )
        if migration_finalize is not None:
            try:
                migration_finalize()
            except Exception:
                pass

        conflicts: list[Conflict] = []
        seen: set[tuple[str, str, str]] = set()
        for owned in owned_units:
            specs = _unit_specs_for_owned(repo_resolved, owned)
            for spec in specs:
                # Match on canonical hash (covers exact unit overlap).
                rows = conn.execute(
                    """
                    SELECT u.path, u.kind, u.hunk_header, u.canonical_patch_hash, u.last_observed_at,
                           su.session_id, su.run_id, su.branch, su.worktree
                    FROM session_units su
                    JOIN units u ON u.unit_id = su.unit_id
                    WHERE u.canonical_patch_hash = ?
                      AND su.session_id != ?
                    """,
                    (spec.unit_id, current_session_id),
                ).fetchall()
                # path_only ↔ same-path-other-kind cross-match.
                cross_rows = conn.execute(
                    """
                    SELECT u.path, u.kind, u.hunk_header, u.canonical_patch_hash, u.last_observed_at,
                           su.session_id, su.run_id, su.branch, su.worktree
                    FROM session_units su
                    JOIN units u ON u.unit_id = su.unit_id
                    WHERE u.path = ?
                      AND u.unit_id != ?
                      AND (u.kind = 'path_only' OR ? = 'path_only')
                      AND su.session_id != ?
                    """,
                    (spec.path, spec.unit_id, spec.kind, current_session_id),
                ).fetchall()
                for row in list(rows) + list(cross_rows):
                    other_session_id = str(row["session_id"] or "")
                    other_unit_id = str(row["canonical_patch_hash"] or "")
                    dedupe = (other_session_id, spec.path, other_unit_id)
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    other_kind = str(row["kind"] or "path_only")
                    conflicts.append(
                        Conflict(
                            path=spec.path,
                            unit="hunk" if other_kind == "tracked_hunk" else ("path" if other_kind == "path_only" else other_kind),
                            hunk_header=row["hunk_header"],
                            other_session_id=other_session_id,
                            other_run_id=row["run_id"],
                            other_branch=row["branch"],
                            other_worktree=row["worktree"],
                            other_claim_id=other_unit_id,
                            last_seen_at=row["last_observed_at"],
                        )
                    )
        return conflicts
    except sqlite3.OperationalError as exc:
        if _is_lock_busy(exc):
            return []
        return []
    except (OSError, sqlite3.Error, RuntimeError):
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def heartbeat(
    repo: Path,
    *,
    session_id: str,
    run_id: str | None,
    lease_id: str | None = None,
    ttl_seconds: int | None = None,
    rvf_state_phase: str = "review",
    rvf_backend: str | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
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
    db_path = directory / SQLITE_FILENAME
    events_path = directory / EVENTS_FILENAME

    legacy_state = _legacy_tracker_dir(base, key) / "state.json"
    if not db_path.exists() and not legacy_state.exists():
        return {"status": "ok", "repo_key": key, "tracker_dir": str(directory), "updated_claim_count": 0}

    conn: sqlite3.Connection | None = None
    updated = 0
    lease_refreshed = False
    lease_refresh_reason: str | None = None
    lease_expires_at: str | None = None
    try:
        conn = _open_conn(db_path)
        migration_finalize: Callable[[], None] | None = None
        with _begin_immediate(conn):
            migration_finalize = _migrate_phase1_if_needed(
                repo=repo_resolved,
                common_dir=common_dir,
                key=key,
                log_root_dir=base,
                new_dir=directory,
                conn=conn,
                events_path=events_path,
            )
            now = _tracker_now_iso(now)
            cur = conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                (now, session_id),
            )
            session_touched = cur.rowcount or 0
            cur = conn.execute(
                """
                UPDATE units SET last_observed_at=?
                WHERE unit_id IN (
                    SELECT unit_id FROM session_units WHERE session_id=?
                )
                """,
                (now, session_id),
            )
            updated = cur.rowcount or 0
            if run_id is not None and session_touched:
                conn.execute(
                    "UPDATE session_units SET run_id=?, last_seen_at=? WHERE session_id=?",
                    (run_id, now, session_id),
                )
            if lease_id:
                ttl = _lease_ttl_seconds(ttl_seconds)
                lease_expires_at = _datetime_to_iso(
                    (_iso_to_datetime(now) or datetime.now(timezone.utc)) + timedelta(seconds=ttl)
                )
                row = conn.execute(
                    "SELECT state, expires_at FROM leases WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
                if row is None or row["state"] != "active":
                    lease_refresh_reason = "lease_not_found"
                    lease_expires_at = None
                elif row["expires_at"] <= now:
                    lease_refresh_reason = "lease_expired_before_refresh"
                    lease_expires_at = row["expires_at"]
                else:
                    conn.execute(
                        """
                        UPDATE leases
                           SET last_activity_at=?, expires_at=?, ttl_seconds=?
                         WHERE lease_id=?
                        """,
                        (now, lease_expires_at, ttl, lease_id),
                    )
                    lease_refreshed = True
                    lease_refresh_reason = "lease_refreshed"
        if migration_finalize is not None:
            try:
                migration_finalize()
            except Exception:
                pass
        _emit_event(
            events_path,
            {
                "event": "heartbeat",
                "rvf_state_phase": rvf_state_phase,
                "rvf_backend": rvf_backend,
                "session_id": session_id,
                "run_id": run_id,
                "updated_unit_count": updated,
                "tracker_lease_id": lease_id,
                "lease_refreshed": lease_refreshed,
                "lease_refresh_reason": lease_refresh_reason,
                "lease_expires_at": lease_expires_at,
            },
        )
        return {
            "status": "ok",
            "repo_key": key,
            "tracker_dir": str(directory),
            "updated_claim_count": updated,
            "lease_refreshed": lease_refreshed,
            "lease_refresh_reason": lease_refresh_reason,
            "lease_expires_at": lease_expires_at,
        }
    except sqlite3.OperationalError as exc:
        if _is_lock_busy(exc):
            _emit_event(
                events_path,
                {
                    "event": "lock_timeout",
                    "session_id": session_id,
                    "run_id": run_id,
                    "phase": "heartbeat",
                },
            )
            return {"status": "lock_timeout", "repo_key": key, "tracker_dir": str(directory)}
        _emit_event(
            events_path,
            {
                "event": "heartbeat_failed",
                "session_id": session_id,
                "run_id": run_id,
                "error": repr(exc),
            },
        )
        return {"status": "error", "repo_key": key, "tracker_dir": str(directory)}
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        _emit_event(
            events_path,
            {
                "event": "heartbeat_failed",
                "session_id": session_id,
                "run_id": run_id,
                "error": repr(exc),
            },
        )
        return {"status": "error", "repo_key": key, "tracker_dir": str(directory)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _lease_repo_paths(
    repo: str | Path,
    log_root_override: Path | None,
) -> tuple[Path, str, Path, Path, Path, Path]:
    repo_resolved = Path(repo).expanduser().resolve()
    if is_bare_repo(repo_resolved):
        raise ValueError(f"bare repositories are not supported: {repo_resolved}")
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        raise ValueError(f"not a git repository: {repo_resolved}")
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    directory.mkdir(parents=True, exist_ok=True)
    return (
        repo_resolved,
        key,
        directory,
        directory / SQLITE_FILENAME,
        directory / EVENTS_FILENAME,
        common_dir,
    )


def _lease_active_unit_conflicts_in_txn(
    conn: sqlite3.Connection,
    unit_ids: list[str],
    now_iso: str,
) -> list[str]:
    if not unit_ids:
        return []
    placeholders = ",".join("?" for _ in unit_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT lu.unit_id
          FROM lease_units lu
          JOIN leases l ON l.lease_id = lu.lease_id
         WHERE l.state='active'
           AND l.expires_at > ?
           AND lu.unit_id IN ({placeholders})
        """,
        (now_iso, *unit_ids),
    ).fetchall()
    return sorted(row["unit_id"] for row in rows)


def _lease_existing_assigned_units_in_txn(
    conn: sqlite3.Connection,
    unit_ids: list[str],
) -> list[str]:
    if not unit_ids:
        return []
    placeholders = ",".join("?" for _ in unit_ids)
    rows = conn.execute(
        f"""
        SELECT unit_id
          FROM units
         WHERE review_state='assigned'
           AND is_tombstoned=0
           AND unit_id IN ({placeholders})
        """,
        tuple(unit_ids),
    ).fetchall()
    return sorted(row["unit_id"] for row in rows)


def _lease_tombstoned_units_in_txn(
    conn: sqlite3.Connection,
    unit_ids: list[str],
) -> list[str]:
    if not unit_ids:
        return []
    placeholders = ",".join("?" for _ in unit_ids)
    rows = conn.execute(
        f"""
        SELECT unit_id
          FROM units
         WHERE is_tombstoned=1
           AND unit_id IN ({placeholders})
        """,
        tuple(unit_ids),
    ).fetchall()
    return sorted(row["unit_id"] for row in rows)


def lease_acquire(
    *,
    repo: str | Path,
    session_id: str,
    run_id: str,
    reviewer_id: str,
    unit_ids: list[str],
    holder_kind: str = "reviewer",
    lease_ttl_seconds: int | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if not unit_ids:
        raise ValueError("unit_ids must not be empty")
    if holder_kind not in {"reviewer", "validate-fix", "manual"}:
        raise ValueError("holder_kind must be reviewer, validate-fix, or manual")
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    ttl_seconds = _lease_ttl_seconds(lease_ttl_seconds)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            stale_freed = _prune_stale_leases_in_txn(conn, now_iso)
            active_conflicts = _lease_active_unit_conflicts_in_txn(conn, unit_ids, now_iso)
            if active_conflicts:
                result = {
                    "status": "conflict",
                    "acquired": False,
                    "reason": "lease_unit_already_assigned",
                    "lease_id": None,
                    "unit_ids": unit_ids,
                    "conflicting_unit_ids": active_conflicts,
                    "stale_freed": stale_freed,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
                return result
            assigned_conflicts = _lease_existing_assigned_units_in_txn(conn, unit_ids)
            if assigned_conflicts:
                return {
                    "status": "conflict",
                    "acquired": False,
                    "reason": "lease_already_held_by_other",
                    "lease_id": None,
                    "unit_ids": unit_ids,
                    "conflicting_unit_ids": assigned_conflicts,
                    "stale_freed": stale_freed,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            tombstoned_conflicts = _lease_tombstoned_units_in_txn(conn, unit_ids)
            if tombstoned_conflicts:
                return {
                    "status": "conflict",
                    "acquired": False,
                    "reason": "lease_unit_tombstoned",
                    "lease_id": None,
                    "unit_ids": unit_ids,
                    "conflicting_unit_ids": tombstoned_conflicts,
                    "stale_freed": stale_freed,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            branch_value = _current_branch(repo_resolved)
            branch_key = _upsert_branch(conn, branch_value, now_iso) if branch_value else None
            worktree_key = _upsert_worktree(conn, str(repo_resolved), branch_key, None, now_iso)
            _upsert_session(conn, session_id, worktree_key, now_iso)
            scope_hash = _compute_scope_hash(unit_ids)
            lease_id = _new_lease_id(now_iso)
            _create_lease_in_txn(
                conn,
                lease_id=lease_id,
                session_id=session_id,
                run_id=run_id,
                reviewer_id=reviewer_id,
                holder_kind=holder_kind,
                scope_hash=scope_hash,
                unit_ids=unit_ids,
                ttl_seconds=ttl_seconds,
                now_iso=now_iso,
            )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "lease_acquired",
                "rvf_state_phase": "review",
                "session_id": session_id,
                "run_id": run_id,
                "reviewer_id": reviewer_id,
                "holder_kind": holder_kind,
                "lease_id": lease_id,
                "scope_hash": scope_hash,
                "unit_count": len(unit_ids),
                "stale_freed": stale_freed,
                "reason_code": "lease_acquired",
            },
        )
        return {
            "status": "acquired",
            "acquired": True,
            "reason": "lease_acquired",
            "lease_id": lease_id,
            "scope_hash": scope_hash,
            "unit_ids": unit_ids,
            "ttl_seconds": ttl_seconds,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def lease_refresh(
    *,
    repo: str | Path,
    lease_id: str,
    ttl_seconds: int | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    ttl = _lease_ttl_seconds(ttl_seconds)
    expires_at = _datetime_to_iso((_iso_to_datetime(now_iso) or datetime.now(timezone.utc)) + timedelta(seconds=ttl))
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            row = conn.execute(
                "SELECT lease_id, state, expires_at FROM leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if row is None or row["state"] != "active":
                return {
                    "status": "missing",
                    "refreshed": False,
                    "reason": "lease_not_found",
                    "lease_id": lease_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            if row["expires_at"] <= now_iso:
                return {
                    "status": "expired",
                    "refreshed": False,
                    "reason": "lease_expired_before_refresh",
                    "lease_id": lease_id,
                    "expires_at": row["expires_at"],
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            conn.execute(
                """
                UPDATE leases
                   SET last_activity_at=?, expires_at=?, ttl_seconds=?
                 WHERE lease_id=?
                """,
                (now_iso, expires_at, ttl, lease_id),
            )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "lease_refreshed",
                "rvf_state_phase": "review",
                "lease_id": lease_id,
                "expires_at": expires_at,
                "reason_code": "lease_refreshed",
            },
        )
        return {
            "status": "refreshed",
            "refreshed": True,
            "reason": "lease_refreshed",
            "lease_id": lease_id,
            "expires_at": expires_at,
            "ttl_seconds": ttl,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def _participant_state_for_reason(reason: str) -> str:
    return "completed" if reason == "completed" else "failed"


def _normalize_unit_id_list(unit_ids: Iterable[Any] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in unit_ids or []:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
    return normalized


def _unit_ids_for_lease_in_txn(conn: sqlite3.Connection, lease_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT unit_id FROM lease_units WHERE lease_id=? ORDER BY unit_id",
        (lease_id,),
    ).fetchall()
    return [row["unit_id"] for row in rows]


def _existing_unit_ids_in_txn(conn: sqlite3.Connection, unit_ids: list[str]) -> list[str]:
    if not unit_ids:
        return []
    placeholders = ",".join("?" for _ in unit_ids)
    rows = conn.execute(
        f"SELECT unit_id FROM units WHERE unit_id IN ({placeholders}) ORDER BY unit_id",
        tuple(unit_ids),
    ).fetchall()
    return [row["unit_id"] for row in rows]


def _active_participant_count_in_txn(conn: sqlite3.Connection, lease_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM lease_participants WHERE lease_id=? AND state='active'",
        (lease_id,),
    ).fetchone()
    return int(row["count"] or 0) if row is not None else 0


def _owning_participant_count_in_txn(conn: sqlite3.Connection, lease_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM lease_participants WHERE lease_id=? AND owns_lease=1",
        (lease_id,),
    ).fetchone()
    return int(row["count"] or 0) if row is not None else 0


def lease_participant_join(
    *,
    repo: str | Path,
    lease_id: str,
    reviewer_id: str,
    run_id: str,
    owns_lease: bool = False,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            row = conn.execute(
                "SELECT lease_id, state, expires_at FROM leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if row is None or row["state"] != "active":
                return {
                    "status": "missing",
                    "joined": False,
                    "reason": "lease_not_found",
                    "lease_id": lease_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            if row["expires_at"] <= now_iso:
                return {
                    "status": "expired",
                    "joined": False,
                    "reason": "lease_expired_before_join",
                    "lease_id": lease_id,
                    "expires_at": row["expires_at"],
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            conn.execute(
                """
                INSERT INTO lease_participants(
                    lease_id, reviewer_id, run_id, state, joined_at,
                    last_activity_at, finished_at, release_reason, owns_lease
                )
                VALUES (?, ?, ?, 'active', ?, ?, NULL, NULL, ?)
                ON CONFLICT(lease_id, reviewer_id, run_id) DO UPDATE SET
                    state='active',
                    last_activity_at=excluded.last_activity_at,
                    finished_at=NULL,
                    release_reason=NULL,
                    owns_lease=MAX(lease_participants.owns_lease, excluded.owns_lease)
                """,
                (lease_id, reviewer_id, run_id, now_iso, now_iso, 1 if owns_lease else 0),
            )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "lease_participant_joined",
                "rvf_state_phase": "review",
                "lease_id": lease_id,
                "reviewer_id": reviewer_id,
                "run_id": run_id,
                "owns_lease": owns_lease,
                "reason_code": "lease_participant_joined",
            },
        )
        return {
            "status": "joined",
            "joined": True,
            "reason": "lease_participant_joined",
            "lease_id": lease_id,
            "reviewer_id": reviewer_id,
            "run_id": run_id,
            "owns_lease": owns_lease,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def lease_participant_refresh(
    *,
    repo: str | Path,
    lease_id: str,
    reviewer_id: str,
    run_id: str,
    ttl_seconds: int | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    ttl = _lease_ttl_seconds(ttl_seconds)
    expires_at = _datetime_to_iso((_iso_to_datetime(now_iso) or datetime.now(timezone.utc)) + timedelta(seconds=ttl))
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            row = conn.execute(
                "SELECT lease_id, state, expires_at FROM leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if row is None or row["state"] != "active":
                return {
                    "status": "missing",
                    "refreshed": False,
                    "reason": "lease_not_found",
                    "lease_id": lease_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            if row["expires_at"] <= now_iso:
                return {
                    "status": "expired",
                    "refreshed": False,
                    "reason": "lease_expired_before_refresh",
                    "lease_id": lease_id,
                    "expires_at": row["expires_at"],
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            cur = conn.execute(
                """
                UPDATE lease_participants
                   SET last_activity_at=?
                 WHERE lease_id=?
                   AND reviewer_id=?
                   AND run_id=?
                   AND state='active'
                """,
                (now_iso, lease_id, reviewer_id, run_id),
            )
            if not (cur.rowcount or 0):
                return {
                    "status": "missing",
                    "refreshed": False,
                    "reason": "lease_participant_not_found",
                    "lease_id": lease_id,
                    "reviewer_id": reviewer_id,
                    "run_id": run_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            conn.execute(
                """
                UPDATE leases
                   SET last_activity_at=?, expires_at=?, ttl_seconds=?
                 WHERE lease_id=?
                """,
                (now_iso, expires_at, ttl, lease_id),
            )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "lease_participant_refreshed",
                "rvf_state_phase": "review",
                "lease_id": lease_id,
                "reviewer_id": reviewer_id,
                "run_id": run_id,
                "expires_at": expires_at,
                "reason_code": "lease_participant_refreshed",
            },
        )
        return {
            "status": "refreshed",
            "refreshed": True,
            "reason": "lease_participant_refreshed",
            "lease_id": lease_id,
            "reviewer_id": reviewer_id,
            "run_id": run_id,
            "expires_at": expires_at,
            "ttl_seconds": ttl,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def lease_participant_finish(
    *,
    repo: str | Path,
    lease_id: str,
    reviewer_id: str,
    run_id: str,
    reason: str = "completed",
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    participant_state = _participant_state_for_reason(reason)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            row = conn.execute(
                """
                SELECT state, owns_lease
                  FROM lease_participants
                 WHERE lease_id=? AND reviewer_id=? AND run_id=?
                """,
                (lease_id, reviewer_id, run_id),
            ).fetchone()
            if row is None:
                return {
                    "status": "missing",
                    "finished": False,
                    "reason": "lease_participant_not_found",
                    "lease_id": lease_id,
                    "reviewer_id": reviewer_id,
                    "run_id": run_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            if row["state"] not in {"completed", "failed"}:
                conn.execute(
                    """
                    UPDATE lease_participants
                       SET state=?, last_activity_at=?, finished_at=?, release_reason=?
                     WHERE lease_id=? AND reviewer_id=? AND run_id=?
                    """,
                    (participant_state, now_iso, now_iso, reason, lease_id, reviewer_id, run_id),
                )
            active_count = _active_participant_count_in_txn(conn, lease_id)
            owning_count = _owning_participant_count_in_txn(conn, lease_id)
            owns_lease_value = bool(row["owns_lease"])
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "lease_participant_finished",
                "rvf_state_phase": "review",
                "lease_id": lease_id,
                "reviewer_id": reviewer_id,
                "run_id": run_id,
                "participant_state": participant_state,
                "release_reason": reason,
                "active_participant_count": active_count,
                "owning_participant_count": owning_count,
                "owns_lease": owns_lease_value,
                "reason_code": "lease_participant_finished",
            },
        )
        return {
            "status": "finished",
            "finished": True,
            "reason": "lease_participant_finished",
            "lease_id": lease_id,
            "reviewer_id": reviewer_id,
            "run_id": run_id,
            "participant_state": participant_state,
            "active_participant_count": active_count,
            "owning_participant_count": owning_count,
            "owns_lease": owns_lease_value,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def lease_release(
    *,
    repo: str | Path,
    lease_id: str,
    reason: str = "completed",
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if reason == "completed":
        return complete_review_scope(
            repo=repo,
            lease_id=lease_id,
            reason=reason,
            log_root_override=log_root_override,
            now=now,
        )

    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            row = conn.execute(
                "SELECT lease_id, state FROM leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if row is None or row["state"] in {"completed", "stale-released", "failed-released"}:
                return {
                    "status": "missing",
                    "released": False,
                    "reason": "lease_not_found",
                    "lease_id": lease_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }
            unit_rows = conn.execute(
                "SELECT unit_id FROM lease_units WHERE lease_id=?",
                (lease_id,),
            ).fetchall()
            unit_ids = [row["unit_id"] for row in unit_rows]
            if unit_ids:
                placeholders = ",".join("?" for _ in unit_ids)
                conn.execute(
                    f"""
                    UPDATE units
                       SET review_state='available'
                     WHERE review_state='assigned'
                       AND is_tombstoned=0
                       AND unit_id IN ({placeholders})
                    """,
                    tuple(unit_ids),
                )
            conn.execute("DELETE FROM lease_units WHERE lease_id=?", (lease_id,))
            release_state = "completed" if reason == "completed" else "failed-released"
            participant_state = _participant_state_for_reason(reason)
            conn.execute(
                """
                UPDATE lease_participants
                   SET state=?, last_activity_at=?, finished_at=?, release_reason=?
                 WHERE lease_id=?
                   AND state='active'
                """,
                (participant_state, now_iso, now_iso, reason, lease_id),
            )
            conn.execute(
                """
                UPDATE leases
                   SET state=?, last_activity_at=?
                 WHERE lease_id=?
                """,
                (release_state, now_iso, lease_id),
            )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "lease_released",
                "rvf_state_phase": "review",
                "lease_id": lease_id,
                "release_reason": reason,
                "released_unit_count": len(unit_ids),
                "reason_code": "lease_released",
            },
        )
        return {
            "status": "released",
            "released": True,
            "reason": "lease_released",
            "lease_id": lease_id,
            "release_state": release_state,
            "unit_ids": unit_ids,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def complete_review_scope(
    *,
    repo: str | Path,
    lease_id: str,
    unit_ids: Iterable[Any] | None = None,
    scope_hash: str | None = None,
    run_id: str | None = None,
    reason: str = "completed",
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Mark a finished review scope as reviewed.

    This is intentionally stronger than generic lease release: completed RVF
    scopes must leave durable `reviewed` unit state, even when a long-running
    reviewer let its lease expire and stale-sweep already removed `lease_units`.
    """
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    contract_unit_ids = _normalize_unit_id_list(unit_ids)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            row = conn.execute(
                "SELECT lease_id, state, scope_hash, run_id FROM leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if row is None and not contract_unit_ids:
                return {
                    "status": "missing",
                    "released": False,
                    "reason": "lease_not_found",
                    "lease_id": lease_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }

            prior_state = row["state"] if row is not None else "missing"
            if prior_state in {"failed-released"}:
                return {
                    "status": "missing",
                    "released": False,
                    "reason": "lease_failed_released",
                    "lease_id": lease_id,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }

            lease_unit_ids = _unit_ids_for_lease_in_txn(conn, lease_id) if row is not None else []
            selected_unit_ids = _normalize_unit_id_list([*contract_unit_ids, *lease_unit_ids])
            existing_unit_ids = _existing_unit_ids_in_txn(conn, selected_unit_ids)
            effective_scope_hash = scope_hash or (row["scope_hash"] if row is not None else None)
            effective_run_id = run_id or (row["run_id"] if row is not None else None)
            superseded_active_lease_ids: list[str] = []
            blocked_active_lease_ids: list[str] = []
            blocked_unit_ids: set[str] = set()
            if existing_unit_ids:
                placeholders = ",".join("?" for _ in existing_unit_ids)
                overlap_rows = conn.execute(
                    f"""
                    SELECT DISTINCT l.lease_id, l.scope_hash, lu.unit_id
                      FROM lease_units lu
                      JOIN leases l ON l.lease_id = lu.lease_id
                     WHERE l.state='active'
                       AND l.lease_id<>?
                       AND lu.unit_id IN ({placeholders})
                    """,
                    (lease_id, *existing_unit_ids),
                ).fetchall()
                for overlap in overlap_rows:
                    overlap_lease_id = overlap["lease_id"]
                    if effective_scope_hash and overlap["scope_hash"] == effective_scope_hash:
                        if overlap_lease_id not in superseded_active_lease_ids:
                            superseded_active_lease_ids.append(overlap_lease_id)
                    else:
                        if overlap_lease_id not in blocked_active_lease_ids:
                            blocked_active_lease_ids.append(overlap_lease_id)
                        blocked_unit_ids.add(overlap["unit_id"])
                if superseded_active_lease_ids:
                    lease_placeholders = ",".join("?" for _ in superseded_active_lease_ids)
                    conn.execute(
                        f"""
                        DELETE FROM lease_units
                         WHERE lease_id IN ({lease_placeholders})
                           AND unit_id IN ({placeholders})
                        """,
                        tuple(superseded_active_lease_ids) + tuple(existing_unit_ids),
                    )
                    emptied_rows = conn.execute(
                        f"""
                        SELECT l.lease_id
                          FROM leases l
                         WHERE l.state='active'
                           AND l.lease_id IN ({lease_placeholders})
                           AND NOT EXISTS (
                               SELECT 1 FROM lease_units lu WHERE lu.lease_id=l.lease_id
                           )
                        """,
                        tuple(superseded_active_lease_ids),
                    ).fetchall()
                    emptied_lease_ids = [emptied["lease_id"] for emptied in emptied_rows]
                    if emptied_lease_ids:
                        emptied_placeholders = ",".join("?" for _ in emptied_lease_ids)
                        conn.execute(
                            f"""
                            UPDATE leases
                               SET state='completed',
                                   last_activity_at=?
                             WHERE lease_id IN ({emptied_placeholders})
                            """,
                            (now_iso, *emptied_lease_ids),
                        )
                        conn.execute(
                            f"""
                            UPDATE lease_participants
                               SET state='completed',
                                   last_activity_at=?,
                                   finished_at=?,
                                   release_reason='completed-by-overlapping-scope'
                             WHERE state='active'
                               AND lease_id IN ({emptied_placeholders})
                            """,
                            (now_iso, now_iso, *emptied_lease_ids),
                        )
                reviewable_unit_ids = [unit_id for unit_id in existing_unit_ids if unit_id not in blocked_unit_ids]
                reviewable_placeholders = ",".join("?" for _ in reviewable_unit_ids)
                if reviewable_unit_ids:
                    conn.execute(
                        f"""
                        UPDATE units
                           SET review_state='reviewed'
                         WHERE review_state IN ('available','assigned','reviewed')
                           AND is_tombstoned=0
                           AND unit_id IN ({reviewable_placeholders})
                        """,
                        tuple(reviewable_unit_ids),
                    )
                else:
                    reviewable_unit_ids = []
            else:
                reviewable_unit_ids = []

            if row is not None:
                conn.execute("DELETE FROM lease_units WHERE lease_id=?", (lease_id,))
                conn.execute(
                    """
                    UPDATE lease_participants
                       SET state='completed',
                           last_activity_at=?,
                           finished_at=?,
                           release_reason=?
                     WHERE lease_id=?
                       AND state='active'
                    """,
                    (now_iso, now_iso, reason, lease_id),
                )
                conn.execute(
                    """
                    UPDATE leases
                       SET state='completed',
                           last_activity_at=?
                     WHERE lease_id=?
                    """,
                    (now_iso, lease_id),
                )
            reviewed_edit_claim_count = 0
            if reviewable_unit_ids:
                claim_placeholders = ",".join("?" for _ in reviewable_unit_ids)
                cur = conn.execute(
                    f"""
                    UPDATE edit_claims
                       SET status='reviewed',
                           reviewed_at=?,
                           last_seen_at=?
                     WHERE status='pending'
                       AND claim_id IN (
                           SELECT DISTINCT claim_id
                             FROM edit_claim_units
                            WHERE unit_id IN ({claim_placeholders})
                       )
                       AND NOT EXISTS (
                           SELECT 1
                             FROM edit_claim_units ecu_all
                             LEFT JOIN units u ON u.unit_id = ecu_all.unit_id
                            WHERE ecu_all.claim_id = edit_claims.claim_id
                              AND (
                                  u.unit_id IS NULL
                                  OR u.review_state <> 'reviewed'
                                  OR u.is_tombstoned <> 0
                              )
                       )
                    """,
                    (now_iso, now_iso, *reviewable_unit_ids),
                )
                reviewed_edit_claim_count = cur.rowcount or 0

        _ensure_meta(directory, repo_resolved, common_dir, key)
        if prior_state == "completed":
            completed_reason = "lease_already_completed"
        elif prior_state == "stale-released":
            completed_reason = "lease_completed_after_stale"
        elif prior_state == "missing":
            completed_reason = "lease_completed_from_contract"
        else:
            completed_reason = "lease_completed"
        _emit_event(
            events_path,
            {
                "event": "review_scope_completed",
                "rvf_state_phase": "review",
                "lease_id": lease_id,
                "run_id": effective_run_id,
                "scope_hash": effective_scope_hash,
                "previous_lease_state": prior_state,
                "completed_unit_count": len(reviewable_unit_ids),
                "reviewed_edit_claim_count": reviewed_edit_claim_count,
                "superseded_active_lease_ids": superseded_active_lease_ids,
                "blocked_active_lease_ids": blocked_active_lease_ids,
                "reason_code": completed_reason,
            },
        )
        return {
            "status": "released",
            "released": True,
            "reason": completed_reason,
            "lease_id": lease_id,
            "release_state": "completed",
            "unit_ids": reviewable_unit_ids,
            "released_unit_count": len(reviewable_unit_ids),
            "reviewed_edit_claim_count": reviewed_edit_claim_count,
            "scope_hash": effective_scope_hash,
            "run_id": effective_run_id,
            "previous_lease_state": prior_state,
            "superseded_active_lease_ids": superseded_active_lease_ids,
            "blocked_active_lease_ids": blocked_active_lease_ids,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def invalidate_reviewed_units_for_run(
    *,
    repo: str | Path,
    run_id: str,
    reason: str = "failed_impl_reentry",
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Reopen (`reviewed` → `available`) the still-present units a given RVF run
    reviewed, so a follow-up review re-covers the *whole* implementation — not
    just the redo's delta.

    Run-scoped on purpose: only units whose `session_units.run_id` equals
    ``run_id`` are touched. Units the redo *changed* already became fresh
    ``available`` units via content-hash identity (the old unit went
    ``superseded``); this reopens the ones the redo left **untouched** but the
    failed implementation run had marked ``reviewed``. The union of the two is
    the full "first implementation ∪ fix" scope.

    Deliberately narrow — never broadcasts beyond the target run:
    - other runs' ``reviewed`` units stay reviewed (no worktree/session sweep);
    - ``superseded`` / tombstoned units are excluded (``is_tombstoned=0`` and
      ``observed_state IN ('dirty','committed')``);
    - units currently held by a still-active lease are skipped (never yanked out
      from under a running reviewer), reported under
      ``skipped_active_lease_unit_ids``.

    Idempotent: a second call finds nothing left in ``reviewed`` for the run and
    returns ``status='noop'``.
    """
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            candidate_rows = conn.execute(
                """
                SELECT DISTINCT u.unit_id
                  FROM session_units su
                  JOIN units u ON u.unit_id = su.unit_id
                 WHERE su.run_id = ?
                   AND u.review_state = 'reviewed'
                   AND u.is_tombstoned = 0
                   AND u.observed_state IN ('dirty','committed')
                """,
                (run_id,),
            ).fetchall()
            candidate_unit_ids = sorted(row["unit_id"] for row in candidate_rows)
            reopened_unit_ids: list[str] = []
            skipped_active_lease_unit_ids: list[str] = []
            if candidate_unit_ids:
                held = set(
                    _lease_active_unit_conflicts_in_txn(conn, candidate_unit_ids, now_iso)
                )
                skipped_active_lease_unit_ids = sorted(held)
                reopened_unit_ids = [uid for uid in candidate_unit_ids if uid not in held]
                if reopened_unit_ids:
                    placeholders = ",".join("?" for _ in reopened_unit_ids)
                    conn.execute(
                        f"""
                        UPDATE units
                           SET review_state='available'
                         WHERE review_state='reviewed'
                           AND is_tombstoned=0
                           AND unit_id IN ({placeholders})
                        """,
                        tuple(reopened_unit_ids),
                    )
                    # Maintain complete_review_scope's invariant (a claim is
                    # `reviewed` iff all its units are reviewed+present): any
                    # edit_claim that touches a reopened unit is no longer fully
                    # reviewed, so revert it to `pending`.
                    conn.execute(
                        f"""
                        UPDATE edit_claims
                           SET status='pending',
                               reviewed_at=NULL,
                               last_seen_at=?
                         WHERE status='reviewed'
                           AND claim_id IN (
                               SELECT DISTINCT claim_id
                                 FROM edit_claim_units
                                WHERE unit_id IN ({placeholders})
                           )
                        """,
                        (now_iso, *reopened_unit_ids),
                    )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "review_scope_reopened_for_run",
                "rvf_state_phase": "review",
                "run_id": run_id,
                "reason_code": reason,
                "candidate_unit_count": len(candidate_unit_ids),
                "reopened_unit_count": len(reopened_unit_ids),
                "skipped_active_lease_unit_ids": skipped_active_lease_unit_ids,
            },
        )
        return {
            "status": "reopened" if reopened_unit_ids else "noop",
            "run_id": run_id,
            "reason": reason,
            "reopened_unit_ids": reopened_unit_ids,
            "reopened_unit_count": len(reopened_unit_ids),
            "candidate_unit_count": len(candidate_unit_ids),
            "skipped_active_lease_unit_ids": skipped_active_lease_unit_ids,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def latest_reviewed_run_for_worktree(
    *,
    repo: str | Path,
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    """Resolve the most recent RVF run that left still-present ``reviewed`` units
    in *this* worktree.

    Used by ``rvf_rescope.py`` to pick ``target_run_id`` when the user did not
    paste an RVF handoff. Worktree-scoped (``units.worktree_key`` of the current
    repo path) so a sibling worktree's runs never leak in; ordered by the most
    recent ``session_units.assigned_at`` for the run. Returns ``run_id=None`` when
    no reviewed run is found.
    """
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    worktree = _worktree_key(str(repo_resolved))
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        row = conn.execute(
            """
            SELECT su.run_id AS run_id,
                   MAX(su.assigned_at) AS recent_at,
                   COUNT(*) AS reviewed_unit_count
              FROM session_units su
              JOIN units u ON u.unit_id = su.unit_id
             WHERE u.worktree_key = ?
               AND su.run_id IS NOT NULL
               AND u.review_state = 'reviewed'
               AND u.is_tombstoned = 0
               AND u.observed_state IN ('dirty','committed')
             GROUP BY su.run_id
             ORDER BY recent_at DESC
             LIMIT 1
            """,
            (worktree,),
        ).fetchone()
        if row is None or not row["run_id"]:
            return {
                "status": "not_found",
                "run_id": None,
                "worktree_key": worktree,
                "repo_key": key,
                "tracker_dir": str(directory),
            }
        return {
            "status": "found",
            "run_id": row["run_id"],
            "recent_at": row["recent_at"],
            "reviewed_unit_count": row["reviewed_unit_count"],
            "worktree_key": worktree,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def sweep_stale(
    *,
    repo: str | Path,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    repo_resolved, key, directory, db_path, events_path, common_dir = _lease_repo_paths(
        repo,
        log_root_override,
    )
    now_iso = _tracker_now_iso(now)
    conn: sqlite3.Connection | None = None
    released: list[dict[str, Any]] = []
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            rows = conn.execute(
                """
                SELECT lease_id, session_id, run_id, reviewer_id, expires_at
                  FROM leases
                 WHERE state='active'
                   AND expires_at <= ?
                 ORDER BY expires_at, lease_id
                """,
                (now_iso,),
            ).fetchall()
            _emit_event(
                events_path,
                {
                    "event": "lease_sweep_started",
                    "rvf_state_phase": "review",
                    "checked": len(rows),
                    "reason_code": "lease_sweep_started",
                },
            )
            for row in rows:
                unit_rows = conn.execute(
                    "SELECT unit_id FROM lease_units WHERE lease_id=?",
                    (row["lease_id"],),
                ).fetchall()
                unit_ids = [unit_row["unit_id"] for unit_row in unit_rows]
                released.append(
                    {
                        "lease_id": row["lease_id"],
                        "session_id": row["session_id"],
                        "run_id": row["run_id"],
                        "reviewer_id": row["reviewer_id"],
                        "expires_at": row["expires_at"],
                        "unit_ids": unit_ids,
                    }
                )
            _prune_stale_leases_in_txn(conn, now_iso)
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "lease_sweep_completed",
                "rvf_state_phase": "review",
                "released": len(released),
                "lease_ids": [item["lease_id"] for item in released],
                "reason_code": "sweep_completed",
            },
        )
        return released
    finally:
        if conn is not None:
            conn.close()


def lease_holder_for_unit(
    *,
    repo: str | Path,
    unit_id: str,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    _, _, directory, db_path, _, _ = _lease_repo_paths(repo, log_root_override)
    now_iso = _tracker_now_iso(now)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        row = conn.execute(
            """
            SELECT l.lease_id, l.session_id, l.run_id, l.reviewer_id, l.holder_kind,
                   l.scope_hash, l.state, l.expires_at
              FROM lease_units lu
              JOIN leases l ON l.lease_id = lu.lease_id
             WHERE lu.unit_id=?
               AND l.state='active'
               AND l.expires_at > ?
             ORDER BY l.created_at DESC
             LIMIT 1
            """,
            (unit_id, now_iso),
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()} | {"tracker_dir": str(directory)}
    finally:
        if conn is not None:
            conn.close()


def units_for_path(
    *,
    repo: str | Path,
    path: str,
    log_root_override: Path | None = None,
) -> list[str]:
    _, _, _, db_path, _, _ = _lease_repo_paths(repo, log_root_override)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        rows = conn.execute(
            """
            SELECT unit_id
              FROM units
             WHERE path=?
             ORDER BY unit_id
            """,
            (path,),
        ).fetchall()
        return [row["unit_id"] for row in rows]
    finally:
        if conn is not None:
            conn.close()


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


# -------------------------- Slice 3: allocate-review-scope --------------------------

def _compute_scope_hash(unit_ids: Iterable[str]) -> str:
    """sha256 of newline-joined sorted unit_ids. Stable across allocator runs
    over the same dirty paths and consumed verbatim by Slice 2-B
    `prepare_review_run.load_tracker_scope`."""
    payload = "\n".join(sorted(unit_ids)).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _new_lease_id(now_iso: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z]", "-", now_iso)
    return f"lse-{compact}-{secrets.token_hex(4)}"


def _lease_ttl_seconds(override: int | None) -> int:
    if override is not None and override > 0:
        return int(override)
    raw = os.environ.get(LEASE_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_LEASE_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LEASE_TTL_SECONDS
    return value if value > 0 else DEFAULT_LEASE_TTL_SECONDS


def _iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # `utc_now()` emits `...Z`; normalize to `+00:00` for fromisoformat.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _datetime_to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tracker_now_iso(value: str | None = None) -> str:
    parsed = _iso_to_datetime(value or utc_now())
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    return _datetime_to_iso(parsed)


def _list_dirty_paths(repo: Path) -> list[str]:
    """Return the sorted unique set of paths reported by `git status -z -uall`.
    Renames surface as two halves (old + new) — both are emitted so the
    observation step can classify each side independently. The pathspec
    branch used by `_classify_path` then deduplicates per path.

    `-uall` is required so untracked directories expand to file paths. A
    directory-level `?? path/` row cannot be mapped back to edit-claim units for
    files created under that directory, and would make live untracked units look
    superseded during allocator observation.
    """
    try:
        raw = _run_git(repo, ["status", "--porcelain=v1", "-z", "-uall"])
    except RuntimeError:
        return []
    if not raw:
        return []
    paths: set[str] = set()
    entries = raw.split("\0")
    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        if not entry:
            idx += 1
            continue
        if len(entry) < 3:
            idx += 1
            continue
        code_x = entry[0]
        code_y = entry[1]
        body = entry[3:]
        # Renames: `R ` / `RM` / etc carry `<new>\0<old>` across two NUL
        # records. Push both halves into the dirty path set so each side gets
        # classified independently.
        if code_x == "R" or code_y == "R":
            new_path = body
            old_path = entries[idx + 1] if idx + 1 < len(entries) else ""
            if new_path:
                paths.add(new_path)
            if old_path:
                paths.add(old_path)
            idx += 2
            continue
        if body:
            paths.add(body)
        idx += 1
    return sorted(paths)


def _prune_stale_leases_in_txn(conn: sqlite3.Connection, now_iso: str) -> int:
    """Mark every active lease whose `expires_at <= now` as `stale-released`.
    Returns the number of leases freed."""
    stale_rows = conn.execute(
        """
        SELECT lease_id
          FROM leases
         WHERE state='active'
           AND expires_at <= ?
        """,
        (now_iso,),
    ).fetchall()
    stale_lease_ids = [row["lease_id"] for row in stale_rows]
    if not stale_lease_ids:
        return 0
    placeholders = ",".join("?" for _ in stale_lease_ids)
    conn.execute(
        """
        UPDATE leases
           SET state='stale-released'
         WHERE lease_id IN ({placeholders})
        """.format(placeholders=placeholders),
        tuple(stale_lease_ids),
    )
    conn.execute(
        """
        UPDATE lease_participants
           SET state='failed',
               last_activity_at=?,
               finished_at=?,
               release_reason='stale-released'
         WHERE state='active'
           AND lease_id IN ({placeholders})
        """.format(placeholders=placeholders),
        (now_iso, now_iso, *stale_lease_ids),
    )
    unit_rows = conn.execute(
        """
        SELECT DISTINCT unit_id
          FROM lease_units
         WHERE lease_id IN ({placeholders})
        """.format(placeholders=placeholders),
        tuple(stale_lease_ids),
    ).fetchall()
    unit_ids = [row["unit_id"] for row in unit_rows]
    if unit_ids:
        unit_placeholders = ",".join("?" for _ in unit_ids)
        # Only units not held by a still-active lease can re-enter the pool.
        conn.execute(
            """
            UPDATE units
               SET review_state='available'
             WHERE review_state='assigned'
               AND is_tombstoned=0
               AND unit_id IN ({unit_placeholders})
               AND unit_id NOT IN (
                   SELECT lu.unit_id
                     FROM lease_units lu
                     JOIN leases l ON l.lease_id = lu.lease_id
                    WHERE l.state='active'
                      AND lu.unit_id IN ({unit_placeholders})
               )
            """.format(unit_placeholders=unit_placeholders),
            tuple(unit_ids) + tuple(unit_ids),
        )
    conn.execute(
        """
        DELETE FROM lease_units
         WHERE lease_id IN ({placeholders})
        """.format(placeholders=placeholders),
        tuple(stale_lease_ids),
    )
    return len(stale_lease_ids)


def _supersede_absent_units_in_txn(
    conn: sqlite3.Connection,
    *,
    worktree_key: str,
    live_unit_ids: set[str],
    now_iso: str,
) -> None:
    """Single supersession chokepoint shared by the dirty walk and the
    committed-round walk. A unit is superseded only when it is observed in
    NEITHER the dirty set NOR the committed-round set this pass, and is not
    already `reviewed`.

    The `review_state IN ('available','assigned')` filter is the load-bearing
    invariant: a `reviewed` unit is never touched here, so reviewed-then-
    committed work neither gets superseded nor resurrected — reopening reviewed
    work stays the exclusive job of the explicit `rvf-reopen` marker
    (`invalidate_reviewed_units_for_run`), keeping the two paths orthogonal.

    Selecting both observed_states (not just 'dirty') means a committed unit
    that has dropped out of `baseline..HEAD` (rebased/reverted away) is also
    swept. When `live_unit_ids` carries only dirty ids — i.e. no committed
    baseline this pass — there are no committed rows to consider, so behaviour
    is identical to the historical dirty-only sweep."""
    rows = conn.execute(
        """
        SELECT unit_id
          FROM units
         WHERE worktree_key=?
           AND observed_state IN ('dirty','committed')
           AND review_state IN ('available','assigned')
           AND is_tombstoned=0
        """,
        (worktree_key,),
    ).fetchall()
    for row in rows:
        unit_id = row["unit_id"]
        if unit_id in live_unit_ids:
            continue
        conn.execute(
            "UPDATE units SET observed_state='superseded', last_observed_at=? WHERE unit_id=?",
            (now_iso, unit_id),
        )


def _observe_and_upsert_units_in_txn(
    conn: sqlite3.Connection,
    *,
    repo: Path,
    branch_value: str | None,
    worktree_value: str,
    now_iso: str,
    committed_baseline: str | None = None,
) -> dict[str, Any]:
    """Walk the worktree (and, when `committed_baseline` is set, the net
    committed-round diff), upsert units for every observed change, and mark
    units that no longer have an observable change as `superseded`. Returns a
    dict with `branch_key`, `worktree_key`, the set of currently-observed dirty
    `observed_unit_ids`, and `committed_observed_unit_ids`.

    Ordering: the committed walk runs FIRST and the dirty walk SECOND, so a path
    that was committed and then re-dirtied within the round ends in the `dirty`
    observed_state (live state wins) via `_upsert_unit`'s
    `observed_state=excluded.observed_state` conflict clause. When
    `committed_baseline` is None the committed walk is skipped entirely and the
    function reduces to its historical dirty-only behaviour."""
    branch_key = _upsert_branch(conn, branch_value, now_iso) if branch_value else None
    worktree_key = _upsert_worktree(conn, worktree_value, branch_key, None, now_iso)

    committed_observed_unit_ids: set[str] = set()
    if committed_baseline:
        for path in _list_committed_round_changed_paths(repo, committed_baseline):
            observation = _classify_committed_path(repo, path, committed_baseline)
            if observation is None:
                continue
            for spec in _specs_from_observation(observation, path):
                _upsert_unit(
                    conn, spec, branch_key, worktree_key, now_iso, observed_state="committed"
                )
                committed_observed_unit_ids.add(spec.unit_id)

    dirty_paths = _list_dirty_paths(repo)
    observed_unit_ids: set[str] = set()
    for path in dirty_paths:
        for spec in _specs_from_observation(_classify_path(repo, path), path):
            _upsert_unit(conn, spec, branch_key, worktree_key, now_iso)
            observed_unit_ids.add(spec.unit_id)

    # Mark units whose change no longer appears in either observed set (file got
    # committed past the baseline / reverted) as `superseded` so they don't show
    # up as candidates. Reviewed units are left untouched by the helper.
    _supersede_absent_units_in_txn(
        conn,
        worktree_key=worktree_key,
        live_unit_ids=observed_unit_ids | committed_observed_unit_ids,
        now_iso=now_iso,
    )

    return {
        "branch_key": branch_key,
        "worktree_key": worktree_key,
        "observed_unit_ids": observed_unit_ids,
        "committed_observed_unit_ids": committed_observed_unit_ids,
    }


def _takeover_transfer_in_txn(
    conn: sqlite3.Connection,
    *,
    parent_session_id: str,
    current_session_id: str,
    now_iso: str,
) -> list[str]:
    """Transfer the parent's owned-and-unleased units to the current session.
    Parent rows flip `assignment_kind='owned' -> 'transferred'`; the current
    session gets matching `assignment_kind='takeover'` rows. Returns the list
    of transferred unit_ids."""
    rows = conn.execute(
        """
        SELECT su.unit_id, su.run_id, su.branch, su.worktree, su.evidence
          FROM session_units su
          JOIN units u ON u.unit_id = su.unit_id
         WHERE su.session_id=?
           AND su.assignment_kind='owned'
           AND u.review_state IN ('available','assigned')
           AND u.is_tombstoned=0
           AND u.unit_id NOT IN (
               SELECT lu.unit_id
                 FROM lease_units lu
                 JOIN leases l ON l.lease_id = lu.lease_id
                WHERE l.state='active'
                  AND l.expires_at > ?
           )
        """,
        (parent_session_id, now_iso),
    ).fetchall()
    transferred: list[str] = []
    for row in rows:
        unit_id = row["unit_id"]
        conn.execute(
            "UPDATE session_units SET assignment_kind='transferred', last_seen_at=? WHERE session_id=? AND unit_id=?",
            (now_iso, parent_session_id, unit_id),
        )
        conn.execute(
            """
            INSERT INTO session_units(session_id, unit_id, assignment_kind, assigned_at, run_id, branch, worktree, evidence, last_seen_at)
            VALUES (?, ?, 'takeover', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, unit_id) DO UPDATE SET
                assignment_kind='takeover',
                last_seen_at=excluded.last_seen_at
            """,
            (
                current_session_id,
                unit_id,
                now_iso,
                row["run_id"],
                row["branch"],
                row["worktree"],
                row["evidence"],
                now_iso,
            ),
        )
        transferred.append(unit_id)
    return transferred


def _preview_takeover_candidate_unit_ids_in_txn(
    conn: sqlite3.Connection,
    *,
    parent_session_id: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT su.unit_id
          FROM session_units su
          JOIN units u ON u.unit_id = su.unit_id
         WHERE su.session_id=?
           AND su.assignment_kind='owned'
           AND u.review_state='available'
           AND u.is_tombstoned=0
           AND u.observed_state IN ('dirty','committed')
         ORDER BY u.path, u.hunk_header IS NULL, u.hunk_header, u.unit_id
        """,
        (parent_session_id,),
    ).fetchall()
    return [row["unit_id"] for row in rows]


def _resolve_session_assignment_in_txn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    parent_session_id: str | None,
    worktree_key: str,
    now_iso: str,
) -> dict[str, Any]:
    """Detect first-stop forks (no row yet for `session_id`) and run the
    takeover transfer before the upsert seeds `last_seen_at`. Then upsert the
    session row. Returns `{takeover_from: <parent or None>, transferred_unit_ids: [...]}`."""
    cur = conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,))
    is_first_stop = cur.fetchone() is None
    transferred: list[str] = []
    takeover_from: str | None = None
    # Insert the child session row BEFORE the takeover transfer so the
    # `session_units(child).session_id` FK is satisfied. Detection has
    # already been captured in `is_first_stop` so this re-ordering is safe.
    _upsert_session(conn, session_id, worktree_key, now_iso)
    if is_first_stop and parent_session_id and parent_session_id != session_id:
        # Multi-parent forks join with `;` but Slice 3 only supports one parent
        # per stop; multi-parent is left for Slice 5 manual takeover CLI.
        transferred = _takeover_transfer_in_txn(
            conn,
            parent_session_id=parent_session_id,
            current_session_id=session_id,
            now_iso=now_iso,
        )
        if transferred:
            takeover_from = parent_session_id
    if parent_session_id and parent_session_id != session_id:
        # Record the parent linkage even when nothing was transferred so a
        # later allocator pass sees the lineage.
        conn.execute(
            "UPDATE sessions SET parent_session_id=COALESCE(parent_session_id, ?) WHERE session_id=?",
            (parent_session_id, session_id),
        )
    return {"takeover_from": takeover_from, "transferred_unit_ids": transferred}


def _collect_candidate_unit_ids_in_txn(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT u.unit_id, u.path, u.hunk_header
          FROM units u
         WHERE (
               EXISTS (
                   SELECT 1
                     FROM session_units su
                    WHERE su.unit_id = u.unit_id
                      AND su.session_id=?
                      AND su.assignment_kind IN ('owned','takeover')
               )
               OR EXISTS (
                   SELECT 1
                     FROM edit_claim_units ecu
                     JOIN edit_claims ec ON ec.claim_id = ecu.claim_id
                    WHERE ecu.unit_id = u.unit_id
                      AND ec.session_id=?
                      AND ec.status='pending'
               )
           )
           AND u.review_state='available'
           AND u.is_tombstoned=0
           AND u.observed_state IN ('dirty','committed')
         ORDER BY u.path, u.hunk_header IS NULL, u.hunk_header, u.unit_id
        """,
        (session_id, session_id),
    ).fetchall()
    return [row["unit_id"] for row in rows]


def _exclude_active_leased_in_txn(
    conn: sqlite3.Connection,
    candidate_unit_ids: list[str],
    now_iso: str,
) -> tuple[list[str], int]:
    if not candidate_unit_ids:
        return ([], 0)
    placeholders = ",".join("?" for _ in candidate_unit_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT lu.unit_id
          FROM lease_units lu
          JOIN leases l ON l.lease_id = lu.lease_id
         WHERE l.state='active'
           AND l.expires_at > ?
           AND lu.unit_id IN ({placeholders})
        """,
        (now_iso, *candidate_unit_ids),
    ).fetchall()
    leased = {row["unit_id"] for row in rows}
    excluded = sum(1 for uid in candidate_unit_ids if uid in leased)
    surviving = [uid for uid in candidate_unit_ids if uid not in leased]
    return (surviving, excluded)


def _create_lease_in_txn(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    session_id: str,
    run_id: str,
    reviewer_id: str,
    holder_kind: str,
    scope_hash: str,
    unit_ids: list[str],
    ttl_seconds: int,
    now_iso: str,
    transcript_max_line_number: int | None = None,
) -> None:
    expires_dt = (_iso_to_datetime(now_iso) or datetime.now(timezone.utc)) + timedelta(seconds=ttl_seconds)
    expires_at = _datetime_to_iso(expires_dt)
    conn.execute(
        """
        INSERT INTO leases(
            lease_id, session_id, run_id, reviewer_id, holder_kind, scope_hash,
            state, ttl_seconds, transcript_max_line_number, created_at, last_activity_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (
            lease_id,
            session_id,
            run_id,
            reviewer_id,
            holder_kind,
            scope_hash,
            ttl_seconds,
            transcript_max_line_number,
            now_iso,
            now_iso,
            expires_at,
        ),
    )
    for unit_id in unit_ids:
        conn.execute(
            "INSERT INTO lease_units(lease_id, unit_id) VALUES (?, ?)",
            (lease_id, unit_id),
        )
    placeholders = ",".join("?" for _ in unit_ids)
    conn.execute(
        f"UPDATE units SET review_state='assigned' WHERE is_tombstoned=0 AND unit_id IN ({placeholders})",
        tuple(unit_ids),
    )


def _collect_paths_and_hunks_in_txn(
    conn: sqlite3.Connection,
    unit_ids: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    if not unit_ids:
        return ([], [])
    placeholders = ",".join("?" for _ in unit_ids)
    rows = conn.execute(
        f"""
        SELECT unit_id, path, hunk_header
          FROM units
         WHERE unit_id IN ({placeholders})
         ORDER BY path, hunk_header IS NULL, hunk_header, unit_id
        """,
        tuple(unit_ids),
    ).fetchall()
    paths_set: set[str] = set()
    hunks: list[dict[str, Any]] = []
    for row in rows:
        path = row["path"]
        if isinstance(path, str):
            paths_set.add(path)
        hunks.append(
            {
                "unit_id": row["unit_id"],
                "path": path,
                "hunk_header": row["hunk_header"],
            }
        )
    return (sorted(paths_set), hunks)


def _empty_allocate_result(
    *,
    status: str,
    reason: str | None,
    reason_legacy_alias: str | None,
    repo_key_value: str,
    tracker_dir_value: str | None,
    candidate_unit_count: int = 0,
    leased_excluded_count: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "acquired": False,
        "would_acquire": False,
        "reason": reason,
        "reason_legacy_alias": reason_legacy_alias,
        "scope": None,
        "scope_path": None,
        "lease_id": None,
        "scope_hash": None,
        "candidate_unit_count": candidate_unit_count,
        "leased_excluded_count": leased_excluded_count,
        "repo_key": repo_key_value,
        "tracker_dir": tracker_dir_value,
    }


def allocate_review_scope(
    *,
    repo: Path,
    session_id: str,
    run_id: str,
    reviewer_id: str | None = None,
    output_scope_path: Path | None = None,
    parent_session_id: str | None = None,
    holder_kind: str = "reviewer",
    lease_ttl_seconds: int | None = None,
    dry_run: bool = False,
    log_root_override: Path | None = None,
    now: str | None = None,
    auto_claim_observed: bool = True,
    transcript_max_line_number: int | None = None,
    committed_baseline: str | None = None,
) -> dict[str, Any]:
    """Producer half of the global reviewed-diff tracker.

    On `status='allocated'` writes `tracker-scope.json` to `output_scope_path`
    when supplied; the JSON shape passes `prepare_review_run.load_tracker_scope`
    unchanged. On `status='empty'` writes nothing — the dispatcher / Stop hook
    converts that into the `no_unassigned_review_scope` skip payload.

    `dry_run=True` runs the same observation + candidate query but stops short
    of inserting a lease — used by the dispatcher to decide whether the
    installed Stop hook should fire.
    """
    repo_resolved = repo.resolve()
    if _disabled():
        return _empty_allocate_result(
            status="disabled",
            reason=None,
            reason_legacy_alias=None,
            repo_key_value="",
            tracker_dir_value=None,
        )
    if is_bare_repo(repo_resolved):
        return _empty_allocate_result(
            status="unsupported_repo",
            reason=None,
            reason_legacy_alias=None,
            repo_key_value="",
            tracker_dir_value=None,
        )
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return _empty_allocate_result(
            status="unsupported_repo",
            reason=None,
            reason_legacy_alias=None,
            repo_key_value="",
            tracker_dir_value=None,
        )
    if not dry_run and not reviewer_id:
        raise ValueError("reviewer_id is required when dry_run=False")

    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / SQLITE_FILENAME
    events_path = directory / EVENTS_FILENAME

    branch_value = _current_branch(repo_resolved)
    worktree_value = str(repo_resolved)
    now_iso = _tracker_now_iso(now)
    ttl_seconds = _lease_ttl_seconds(lease_ttl_seconds)
    holder_kind_value = holder_kind if holder_kind in {"reviewer", "validate-fix", "manual"} else "reviewer"

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        # Slice 3 step 1-7 all live inside one BEGIN IMMEDIATE; step 8 (file
        # writes + events.jsonl append) happens after COMMIT so a rollback
        # leaves no half-written tracker-scope.json behind.
        committed_payload: dict[str, Any] | None = None
        result_status: str
        candidate_unit_count = 0
        leased_excluded_count = 0
        with _begin_immediate(conn):
            migration_finalize: Callable[[], None] | None = None
            migration_finalize = _migrate_phase1_if_needed(
                repo=repo_resolved,
                common_dir=common_dir,
                key=key,
                log_root_dir=base,
                new_dir=directory,
                conn=conn,
                events_path=events_path,
            )

            # Step 1: prune stale leases.
            stale_freed = _prune_stale_leases_in_txn(conn, now_iso)

            # Step 2: observe worktree (and committed round, when a baseline is
            # supplied) → upsert units.
            observation = _observe_and_upsert_units_in_txn(
                conn,
                repo=repo_resolved,
                branch_value=branch_value,
                worktree_value=worktree_value,
                now_iso=now_iso,
                committed_baseline=committed_baseline,
            )
            worktree_key = observation["worktree_key"]

            # Step 3: resolve session assignment (fork takeover before upsert).
            session_resolution = _resolve_session_assignment_in_txn(
                conn,
                session_id=session_id,
                parent_session_id=parent_session_id,
                worktree_key=worktree_key,
                now_iso=now_iso,
            )
            takeover_from = session_resolution.get("takeover_from")

            # Step 3b: claim observed units when this is a fresh session with
            # no prior `register_claims` attribution AND no parent fork. This
            # is the manual-CLI escape hatch (D8 commentary): the producer
            # operating without transcript-derived ownership has to fall back
            # to "review whatever's dirty in this worktree". The auto Stop-hook
            # path passes `auto_claim_observed=False` because
            # `refresh_global_diff_tracker` already pre-populated
            # `session_units` via `build_manifest` → `register_claims`, so any
            # auto-claim here would broaden scope past the transcript intent.
            if auto_claim_observed and not session_resolution.get("transferred_unit_ids"):
                existing = conn.execute(
                    "SELECT 1 FROM session_units WHERE session_id=? LIMIT 1",
                    (session_id,),
                ).fetchone()
                if existing is None:
                    for unit_id in observation["observed_unit_ids"]:
                        _upsert_session_unit(
                            conn,
                            session_id,
                            unit_id,
                            run_id=run_id,
                            branch=branch_value,
                            worktree=worktree_value,
                            evidence="allocator",
                            now=now_iso,
                        )

            # Step 4: collect candidate unit_ids.
            candidates = _collect_candidate_unit_ids_in_txn(conn, session_id)
            candidate_unit_count = len(candidates)

            # Step 5: exclude active-leased units (anti-join).
            surviving, leased_excluded_count = _exclude_active_leased_in_txn(conn, candidates, now_iso)

            if not surviving:
                # Step 6 (empty): no lease, no scope file. Build the empty
                # result eagerly so we can emit the events.jsonl marker after
                # COMMIT for grep reliability (D2).
                committed_payload = {
                    "status": "empty",
                    "scope": None,
                    "lease_id": None,
                    "scope_hash": None,
                    "unit_ids": [],
                    "paths": [],
                    "hunks": [],
                    "stale_freed": stale_freed,
                    "takeover_from": takeover_from,
                    "transferred_unit_ids": session_resolution.get("transferred_unit_ids", []),
                }
                result_status = "empty"
            elif dry_run:
                # Step 6 (dry_run): no DB writes, but still report the would-be
                # scope hash so callers can dedupe.
                scope_hash = _compute_scope_hash(surviving)
                paths_list, hunks_list = _collect_paths_and_hunks_in_txn(conn, surviving)
                committed_payload = {
                    "status": "dry_run",
                    "scope": None,
                    "lease_id": None,
                    "scope_hash": scope_hash,
                    "unit_ids": surviving,
                    "paths": paths_list,
                    "hunks": hunks_list,
                    "stale_freed": stale_freed,
                    "takeover_from": takeover_from,
                    "transferred_unit_ids": session_resolution.get("transferred_unit_ids", []),
                }
                result_status = "dry_run"
            else:
                # Steps 6-7 (allocate): hash, lease, mark assigned, collect paths.
                scope_hash = _compute_scope_hash(surviving)
                lease_id = _new_lease_id(now_iso)
                _create_lease_in_txn(
                    conn,
                    lease_id=lease_id,
                    session_id=session_id,
                    run_id=run_id,
                    reviewer_id=reviewer_id or "",
                    holder_kind=holder_kind_value,
                    scope_hash=scope_hash,
                    unit_ids=surviving,
                    ttl_seconds=ttl_seconds,
                    now_iso=now_iso,
                    transcript_max_line_number=transcript_max_line_number,
                )
                paths_list, hunks_list = _collect_paths_and_hunks_in_txn(conn, surviving)
                scope_payload: dict[str, Any] = {
                    "unit_ids": surviving,
                    "lease_id": lease_id,
                    "lease_ttl_seconds": ttl_seconds,
                    "transcript_max_line_number": transcript_max_line_number,
                    "scope_hash": scope_hash,
                    "paths": paths_list,
                    "hunks": hunks_list,
                    "source_session_id": session_id,
                    "takeover_from_session_id": takeover_from,
                }
                committed_payload = {
                    "status": "allocated",
                    "scope": scope_payload,
                    "lease_id": lease_id,
                    "scope_hash": scope_hash,
                    "unit_ids": surviving,
                    "paths": paths_list,
                    "hunks": hunks_list,
                    "stale_freed": stale_freed,
                    "takeover_from": takeover_from,
                    "transferred_unit_ids": session_resolution.get("transferred_unit_ids", []),
                }
                result_status = "allocated"

        # Step 8 (post-COMMIT): write tracker-scope.json + events.jsonl event.
        if migration_finalize is not None:
            try:
                migration_finalize()
            except Exception:
                pass
        _ensure_meta(directory, repo_resolved, common_dir, key)

        assert committed_payload is not None
        scope_path_str: str | None = None
        if result_status == "allocated":
            scope = committed_payload["scope"]
            if output_scope_path is not None:
                output_scope_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(
                    output_scope_path,
                    json.dumps(scope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
                scope_path_str = str(output_scope_path)
            _emit_event(
                events_path,
                {
                    "event": "allocate_review_scope",
                    "rvf_state_phase": "review",
                    "session_id": session_id,
                    "run_id": run_id,
                    "reviewer_id": reviewer_id,
                    "holder_kind": holder_kind_value,
                    "lease_id": committed_payload["lease_id"],
                    "scope_hash": committed_payload["scope_hash"],
                    "unit_count": len(committed_payload["unit_ids"]),
                    "paths": committed_payload["paths"],
                    "leased_excluded_count": leased_excluded_count,
                    "stale_freed": committed_payload["stale_freed"],
                    "takeover_from_session_id": committed_payload["takeover_from"],
                    "transcript_max_line_number": transcript_max_line_number,
                    "reason_code": REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
                    "reason_code_legacy_alias": LEGACY_REASON_SESSION_OWNED_DIRTY,
                },
            )
            return {
                "status": "allocated",
                "acquired": True,
                "would_acquire": True,
                "reason": REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
                "reason_legacy_alias": LEGACY_REASON_SESSION_OWNED_DIRTY,
                "scope": scope,
                "scope_path": scope_path_str,
                "lease_id": committed_payload["lease_id"],
                "scope_hash": committed_payload["scope_hash"],
                "candidate_unit_count": candidate_unit_count,
                "leased_excluded_count": leased_excluded_count,
                "transcript_max_line_number": transcript_max_line_number,
                "repo_key": key,
                "tracker_dir": str(directory),
            }
        if result_status == "dry_run":
            _emit_event(
                events_path,
                {
                    "event": "allocate_review_scope_dry_run",
                    "rvf_state_phase": "review",
                    "session_id": session_id,
                    "run_id": run_id,
                    "scope_hash": committed_payload["scope_hash"],
                    "unit_count": len(committed_payload["unit_ids"]),
                    "paths": committed_payload["paths"],
                    "leased_excluded_count": leased_excluded_count,
                    "transcript_max_line_number": transcript_max_line_number,
                    "reason_code": REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
                    "reason_code_legacy_alias": LEGACY_REASON_SESSION_OWNED_DIRTY,
                },
            )
            return {
                "status": "dry_run",
                "acquired": False,
                "would_acquire": True,
                "reason": REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
                "reason_legacy_alias": LEGACY_REASON_SESSION_OWNED_DIRTY,
                "scope": None,
                "scope_path": None,
                "lease_id": None,
                "scope_hash": committed_payload["scope_hash"],
                "unit_ids": committed_payload["unit_ids"],
                "paths": committed_payload["paths"],
                "candidate_unit_count": candidate_unit_count,
                "leased_excluded_count": leased_excluded_count,
                "repo_key": key,
                "tracker_dir": str(directory),
            }
        # status == "empty"
        _emit_event(
            events_path,
            {
                "event": "allocate_review_scope_empty",
                "rvf_state_phase": "review",
                "session_id": session_id,
                "run_id": run_id,
                "reviewer_id": reviewer_id,
                "holder_kind": holder_kind_value,
                "candidate_unit_count": candidate_unit_count,
                "leased_excluded_count": leased_excluded_count,
                "stale_freed": committed_payload["stale_freed"],
                "takeover_from_session_id": committed_payload["takeover_from"],
                "reason_code": REASON_NO_UNASSIGNED_REVIEW_SCOPE,
                "reason_code_legacy_alias": LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
            },
        )
        return _empty_allocate_result(
            status="empty",
            reason=REASON_NO_UNASSIGNED_REVIEW_SCOPE,
            reason_legacy_alias=LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
            repo_key_value=key,
            tracker_dir_value=str(directory),
            candidate_unit_count=candidate_unit_count,
            leased_excluded_count=leased_excluded_count,
        )
    except sqlite3.OperationalError as exc:
        if _is_lock_busy(exc):
            _emit_event(
                events_path,
                {
                    "event": "lock_timeout",
                    "phase": "allocate_review_scope",
                    "session_id": session_id,
                    "run_id": run_id,
                },
            )
            return _empty_allocate_result(
                status="lock_timeout",
                reason=None,
                reason_legacy_alias=None,
                repo_key_value=key,
                tracker_dir_value=str(directory),
            )
        _emit_event(
            events_path,
            {
                "event": "allocate_review_scope_failed",
                "session_id": session_id,
                "run_id": run_id,
                "error": repr(exc),
            },
        )
        return _empty_allocate_result(
            status="error",
            reason=None,
            reason_legacy_alias=None,
            repo_key_value=key,
            tracker_dir_value=str(directory),
        )
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        _emit_event(
            events_path,
            {
                "event": "allocate_review_scope_failed",
                "session_id": session_id,
                "run_id": run_id,
                "error": repr(exc),
            },
        )
        return _empty_allocate_result(
            status="error",
            reason=None,
            reason_legacy_alias=None,
            repo_key_value=key,
            tracker_dir_value=str(directory),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _manual_tracker_store(repo_resolved: Path, log_root_override: Path | None) -> tuple[str, Path, Path, Path, Path]:
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        raise RuntimeError(f"unsupported repo: {repo_resolved}")
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    directory.mkdir(parents=True, exist_ok=True)
    return key, common_dir, directory, directory / SQLITE_FILENAME, directory / EVENTS_FILENAME


def _manual_run_ttl_seconds(override: int | None) -> int | None:
    raw = os.environ.get(MANUAL_RUN_TTL_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return override
        return value if value > 0 else None
    if override is not None and override > 0:
        return int(override)
    return None


def record_manual_rvf_run(
    *,
    repo: str | Path,
    session_id: str,
    run_id: str,
    scope_hash: str,
    completed_at: str | None = None,
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    repo_resolved = Path(repo).expanduser().resolve()
    key, common_dir, directory, db_path, events_path = _manual_tracker_store(repo_resolved, log_root_override)
    now_iso = completed_at or utc_now()
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            branch_key = _upsert_branch(conn, _current_branch(repo_resolved), now_iso)
            worktree_key = _upsert_worktree(conn, str(repo_resolved), branch_key, None, now_iso)
            _upsert_session(conn, session_id, worktree_key, now_iso)
            conn.execute(
                """
                INSERT INTO manual_rvf_runs(session_id, run_id, scope_hash, completed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, run_id) DO UPDATE SET
                    scope_hash=excluded.scope_hash,
                    completed_at=excluded.completed_at
                """,
                (session_id, run_id, scope_hash, now_iso),
            )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        payload = {
            "status": "recorded",
            "session_id": session_id,
            "run_id": run_id,
            "scope_hash": scope_hash,
            "completed_at": now_iso,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
        _emit_event(events_path, {"event": "manual_rvf_run_recorded", **payload})
        return payload
    finally:
        if conn is not None:
            conn.close()


def find_manual_rvf_run_for_scope_hash(
    *,
    repo: str | Path,
    scope_hash: str,
    ttl_seconds: int | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    repo_resolved = Path(repo).expanduser().resolve()
    key, common_dir, directory, db_path, events_path = _manual_tracker_store(repo_resolved, log_root_override)
    ttl_value = _manual_run_ttl_seconds(ttl_seconds)
    now_iso = _tracker_now_iso(now)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            row = conn.execute(
                """
                SELECT session_id, run_id, completed_at
                  FROM manual_rvf_runs
                 WHERE scope_hash=?
                 ORDER BY completed_at DESC
                 LIMIT 1
                """,
                (scope_hash,),
            ).fetchone()
        _ensure_meta(directory, repo_resolved, common_dir, key)
        if row is None:
            return None
        completed_at = row["completed_at"]
        if ttl_value is not None:
            completed_dt = _iso_to_datetime(completed_at)
            now_dt = _iso_to_datetime(now_iso)
            if completed_dt is None or now_dt is None:
                return None
            if (now_dt - completed_dt).total_seconds() > ttl_value:
                return None
        payload = {
            "session_id": row["session_id"],
            "run_id": row["run_id"],
            "completed_at": completed_at,
        }
        _emit_event(
            events_path,
            {
                "event": "manual_scope_hash_match",
                "scope_hash": scope_hash,
                **payload,
            },
        )
        return payload
    finally:
        if conn is not None:
            conn.close()


def _manual_suppression_scope_probe(
    *,
    repo: str | Path,
    session_id: str,
    parent_session_id: str | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    repo_resolved = Path(repo).expanduser().resolve()
    key, _common_dir, directory, db_path, _events_path = _manual_tracker_store(repo_resolved, log_root_override)
    now_iso = _tracker_now_iso(now)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            candidates = _collect_candidate_unit_ids_in_txn(conn, session_id)
            if parent_session_id and parent_session_id != session_id:
                for unit_id in _preview_takeover_candidate_unit_ids_in_txn(
                    conn,
                    parent_session_id=parent_session_id,
                ):
                    if unit_id not in candidates:
                        candidates.append(unit_id)
            candidate_unit_count = len(candidates)
            surviving, leased_excluded_count = _exclude_active_leased_in_txn(conn, candidates, now_iso)
            paths_list, hunks_list = _collect_paths_and_hunks_in_txn(conn, surviving)
        return {
            "status": "dry_run" if surviving else "empty",
            "would_acquire": bool(surviving),
            "scope_hash": _compute_scope_hash(surviving) if surviving else None,
            "candidate_unit_count": candidate_unit_count,
            "leased_excluded_count": leased_excluded_count,
            "unit_ids": surviving,
            "paths": paths_list,
            "hunks": hunks_list,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


def manual_takeover(
    *,
    repo: str | Path,
    parent_session_id: str,
    current_session_id: str,
    run_id: str,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    repo_resolved = Path(repo).expanduser().resolve()
    key, common_dir, directory, db_path, events_path = _manual_tracker_store(repo_resolved, log_root_override)
    now_iso = _tracker_now_iso(now)
    transferred: list[str] = []
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            branch_key = _upsert_branch(conn, _current_branch(repo_resolved), now_iso)
            worktree_key = _upsert_worktree(conn, str(repo_resolved), branch_key, None, now_iso)
            parents = [item.strip() for item in parent_session_id.split(";") if item.strip()]
            missing_parents = [
                parent
                for parent in parents
                if conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (parent,)).fetchone() is None
            ]
            if missing_parents:
                raise RuntimeError(f"manual takeover parent session not found: {', '.join(missing_parents)}")
            _upsert_session(conn, current_session_id, worktree_key, now_iso)
            for parent in parents:
                transferred.extend(
                    _takeover_transfer_in_txn(
                        conn,
                        parent_session_id=parent,
                        current_session_id=current_session_id,
                        now_iso=now_iso,
                    )
                )
            if parents:
                conn.execute(
                    "UPDATE sessions SET parent_session_id=COALESCE(parent_session_id, ?) WHERE session_id=?",
                    (parent_session_id, current_session_id),
                )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        transferred_unique = list(dict.fromkeys(transferred))
        payload = {
            "status": "completed",
            "reason": REASON_MANUAL_TAKEOVER_COMPLETED,
            "parent_session_id": parent_session_id,
            "current_session_id": current_session_id,
            "run_id": run_id,
            "transferred_unit_ids": transferred_unique,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
        _emit_event(events_path, {"event": "manual_takeover_completed", **payload})
        return payload
    finally:
        if conn is not None:
            conn.close()


# -------------------------- RVF causality ledger --------------------------

RVF_ATTEMPT_STATUSES = {
    "prepared",
    "started",
    "fixed",
    "false_positive",
    "elevated",
    "failed",
    "applied",
    "merge_conflict",
}


def _rvf_issue_key(run_id: str, issue_id: str) -> str:
    return f"{safe_token(run_id)}:{safe_token(issue_id)}"


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: Any, fallback: Any) -> Any:
    if not isinstance(raw, str) or not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _rvf_store(repo: str | Path, log_root_override: Path | None) -> tuple[Path, str, Path, Path, Path, Path]:
    repo_resolved = Path(repo).expanduser().resolve()
    key, common_dir, directory, db_path, events_path = _manual_tracker_store(repo_resolved, log_root_override)
    return repo_resolved, key, common_dir, directory, db_path, events_path


def rvf_issue_upsert(
    *,
    repo: str | Path,
    run_id: str,
    issue_id: str,
    payload: dict[str, Any],
    artifact_path: str | Path | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    state: str = "open",
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if state not in {"open", "fixed", "false_positive", "elevated", "failed", "superseded"}:
        raise ValueError("invalid RVF issue state")
    _repo, key, common_dir, directory, db_path, events_path = _rvf_store(repo, log_root_override)
    now_iso = now or utc_now()
    issue_key = _rvf_issue_key(run_id, issue_id)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            existing = conn.execute(
                "SELECT created_at FROM rvf_issues WHERE issue_key=?",
                (issue_key,),
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now_iso
            conn.execute(
                """
                INSERT INTO rvf_issues(
                  issue_key, repo_key, run_id, issue_id, payload, source_refs,
                  artifact_path, state, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                  payload=excluded.payload,
                  source_refs=excluded.source_refs,
                  artifact_path=excluded.artifact_path,
                  state=excluded.state,
                  updated_at=excluded.updated_at
                """,
                (
                    issue_key,
                    key,
                    run_id,
                    issue_id,
                    _json_dumps(payload),
                    _json_dumps(source_refs or []),
                    str(artifact_path) if artifact_path is not None else None,
                    state,
                    created_at,
                    now_iso,
                ),
            )
        _ensure_meta(directory, _repo, common_dir, key)
        result = {
            "status": "upserted",
            "issue_key": issue_key,
            "issue_id": issue_id,
            "run_id": run_id,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
        _emit_event(events_path, {"event": "rvf_issue_upserted", **result})
        return result
    finally:
        if conn is not None:
            conn.close()


def rvf_attempt_upsert(
    *,
    repo: str | Path,
    run_id: str,
    issue_id: str,
    attempt_id: str,
    worktree_path: str | Path,
    base_head: str | None = None,
    baseline_overlay_path: str | Path | None = None,
    baseline_commit: str | None = None,
    fix_patch_path: str | Path | None = None,
    status: str = "prepared",
    result_payload: dict[str, Any] | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if status not in RVF_ATTEMPT_STATUSES:
        raise ValueError("invalid RVF attempt status")
    _repo, key, common_dir, directory, db_path, events_path = _rvf_store(repo, log_root_override)
    now_iso = now or utc_now()
    issue_key = _rvf_issue_key(run_id, issue_id)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            if conn.execute("SELECT 1 FROM rvf_issues WHERE issue_key=?", (issue_key,)).fetchone() is None:
                raise ValueError(f"RVF issue does not exist: {issue_id}")
            existing = conn.execute(
                "SELECT created_at, started_at, stopped_at, applied_at FROM rvf_fix_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now_iso
            started_at = existing["started_at"] if existing is not None else None
            stopped_at = existing["stopped_at"] if existing is not None else None
            applied_at = existing["applied_at"] if existing is not None else None
            if status == "started" and started_at is None:
                started_at = now_iso
            if status in {"fixed", "false_positive", "elevated", "failed"} and stopped_at is None:
                stopped_at = now_iso
            if status == "applied" and applied_at is None:
                applied_at = now_iso
            conn.execute(
                """
                INSERT INTO rvf_fix_attempts(
                  attempt_id, issue_key, repo_key, run_id, issue_id, worktree_path,
                  base_head, baseline_overlay_path, baseline_commit, fix_patch_path,
                  status, result_payload, created_at, updated_at, started_at, stopped_at, applied_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                  worktree_path=excluded.worktree_path,
                  base_head=COALESCE(excluded.base_head, rvf_fix_attempts.base_head),
                  baseline_overlay_path=COALESCE(excluded.baseline_overlay_path, rvf_fix_attempts.baseline_overlay_path),
                  baseline_commit=COALESCE(excluded.baseline_commit, rvf_fix_attempts.baseline_commit),
                  fix_patch_path=COALESCE(excluded.fix_patch_path, rvf_fix_attempts.fix_patch_path),
                  status=excluded.status,
                  result_payload=excluded.result_payload,
                  updated_at=excluded.updated_at,
                  started_at=COALESCE(rvf_fix_attempts.started_at, excluded.started_at),
                  stopped_at=COALESCE(rvf_fix_attempts.stopped_at, excluded.stopped_at),
                  applied_at=COALESCE(rvf_fix_attempts.applied_at, excluded.applied_at)
                """,
                (
                    attempt_id,
                    issue_key,
                    key,
                    run_id,
                    issue_id,
                    str(worktree_path),
                    base_head,
                    str(baseline_overlay_path) if baseline_overlay_path is not None else None,
                    baseline_commit,
                    str(fix_patch_path) if fix_patch_path is not None else None,
                    status,
                    _json_dumps(result_payload or {}),
                    created_at,
                    now_iso,
                    started_at,
                    stopped_at,
                    applied_at,
                ),
            )
        _ensure_meta(directory, _repo, common_dir, key)
        result = {
            "status": status,
            "attempt_id": attempt_id,
            "issue_key": issue_key,
            "issue_id": issue_id,
            "run_id": run_id,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
        _emit_event(events_path, {"event": "rvf_fix_attempt_upserted", **result})
        return result
    finally:
        if conn is not None:
            conn.close()


def rvf_attempt_get(
    *,
    repo: str | Path,
    attempt_id: str,
    log_root_override: Path | None = None,
) -> dict[str, Any] | None:
    _repo, _key, _common_dir, directory, db_path, _events_path = _rvf_store(repo, log_root_override)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        row = conn.execute(
            "SELECT * FROM rvf_fix_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        payload = {key: row[key] for key in row.keys()}
        payload["result_payload"] = _json_loads(payload.get("result_payload"), {})
        payload["tracker_dir"] = str(directory)
        return payload
    finally:
        if conn is not None:
            conn.close()


def rvf_patch_events_replace(
    *,
    repo: str | Path,
    attempt_id: str,
    events: list[dict[str, Any]],
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    _repo, key, common_dir, directory, db_path, events_path = _rvf_store(repo, log_root_override)
    now_iso = now or utc_now()
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            attempt = conn.execute(
                "SELECT issue_key, run_id, issue_id FROM rvf_fix_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise ValueError(f"RVF attempt does not exist: {attempt_id}")
            issue_key = attempt["issue_key"]
            run_id = attempt["run_id"]
            issue_id = attempt["issue_id"]
            conn.execute("DELETE FROM rvf_issue_patch_links WHERE attempt_id=?", (attempt_id,))
            conn.execute("DELETE FROM rvf_fix_patch_events WHERE attempt_id=?", (attempt_id,))
            written = 0
            for index, event in enumerate(events):
                path = str(event.get("path") or "").strip()
                if not path:
                    continue
                patch_event_id = str(event.get("patch_event_id") or f"{attempt_id}:{index}:{safe_token(path)}")
                conn.execute(
                    """
                    INSERT INTO rvf_fix_patch_events(
                      patch_event_id, attempt_id, issue_key, repo_key, run_id, issue_id,
                      path, op, call_id, trajectory_ref, diff_ref, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        patch_event_id,
                        attempt_id,
                        issue_key,
                        key,
                        run_id,
                        issue_id,
                        path,
                        str(event.get("op") or "modified"),
                        event.get("call_id") if isinstance(event.get("call_id"), str) else None,
                        _json_dumps(event.get("trajectory_ref")) if event.get("trajectory_ref") is not None else None,
                        _json_dumps(event.get("diff_ref")) if event.get("diff_ref") is not None else None,
                        now_iso,
                    ),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO rvf_issue_patch_links(issue_key, attempt_id, patch_event_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (issue_key, attempt_id, patch_event_id, now_iso),
                )
                written += 1
        _ensure_meta(directory, _repo, common_dir, key)
        result = {
            "status": "recorded",
            "attempt_id": attempt_id,
            "patch_event_count": written,
            "repo_key": key,
            "tracker_dir": str(directory),
        }
        _emit_event(events_path, {"event": "rvf_fix_patch_events_recorded", **result})
        return result
    finally:
        if conn is not None:
            conn.close()


def rvf_causality_for_run(
    *,
    repo: str | Path,
    run_id: str,
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    _repo, _key, _common_dir, directory, db_path, _events_path = _rvf_store(repo, log_root_override)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        issue_rows = conn.execute(
            "SELECT * FROM rvf_issues WHERE run_id=? ORDER BY issue_id",
            (run_id,),
        ).fetchall()
        attempt_rows = conn.execute(
            "SELECT * FROM rvf_fix_attempts WHERE run_id=? ORDER BY created_at, attempt_id",
            (run_id,),
        ).fetchall()
        event_rows = conn.execute(
            "SELECT * FROM rvf_fix_patch_events WHERE run_id=? ORDER BY created_at, patch_event_id",
            (run_id,),
        ).fetchall()
        events_by_issue: dict[str, list[sqlite3.Row]] = {}
        for row in event_rows:
            events_by_issue.setdefault(row["issue_key"], []).append(row)
        attempts = []
        for row in attempt_rows:
            payload = {key: row[key] for key in row.keys()}
            payload["result_payload"] = _json_loads(payload.get("result_payload"), {})
            attempts.append(payload)
        patch_events = []
        for row in event_rows:
            payload = {key: row[key] for key in row.keys()}
            payload["trajectory_ref"] = _json_loads(payload.get("trajectory_ref"), None)
            payload["diff_ref"] = _json_loads(payload.get("diff_ref"), None)
            patch_events.append(payload)
        issues = []
        for row in issue_rows:
            payload = _json_loads(row["payload"], {})
            if not isinstance(payload, dict):
                payload = {}
            call_ids = [
                event["call_id"]
                for event in events_by_issue.get(row["issue_key"], [])
                if isinstance(event["call_id"], str) and event["call_id"]
            ]
            fix_patch_paths = [
                attempt["fix_patch_path"]
                for attempt in attempts
                if attempt["issue_key"] == row["issue_key"] and attempt.get("fix_patch_path")
            ]
            issue = {
                **payload,
                "issue_id": row["issue_id"],
                "run_id": row["run_id"],
                "state": row["state"],
                "candidate_patch_call_ids": list(dict.fromkeys(call_ids)),
                "fix_patch_paths": list(dict.fromkeys(fix_patch_paths)),
                "source_refs": _json_loads(row["source_refs"], []),
                "artifact_path": row["artifact_path"],
            }
            issues.append(issue)
        return {
            "status": "found" if issue_rows else "missing",
            "run_id": run_id,
            "issues": issues,
            "fix_attempts": attempts,
            "patch_events": patch_events,
            "tracker_dir": str(directory),
        }
    finally:
        if conn is not None:
            conn.close()


# -------------------------- CLI dispatcher --------------------------

def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diff_tracker",
        description="Global reviewed-diff tracker producer (Slice 3 of Phase 2).",
    )
    subparsers = parser.add_subparsers(dest="subcommand")
    # Slice 3 only registers `allocate-review-scope`. Slice 4/5 will add their
    # own subparsers; we deliberately skip `required=True` so future subcommand
    # registration stays additive.
    alloc = subparsers.add_parser(
        "allocate-review-scope",
        help="Allocate a reviewer lease over the current session's unleased units.",
    )
    alloc.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    alloc.add_argument("--session-id", required=True)
    alloc.add_argument("--run-id", required=True)
    alloc.add_argument("--reviewer-id", default=None)
    alloc.add_argument("--parent-session-id", default=None)
    alloc.add_argument(
        "--holder-kind",
        choices=("reviewer", "validate-fix", "manual"),
        default="reviewer",
    )
    alloc.add_argument(
        "--output-scope",
        default=None,
        help="Path to write tracker-scope.json on status=allocated.",
    )
    alloc.add_argument("--lease-ttl-seconds", type=int, default=None)
    alloc.add_argument("--dry-run", action="store_true")
    alloc.add_argument("--print-result", action="store_true")
    alloc.add_argument(
        "--log-root",
        default=None,
        help="Override CODEX_RVF_LOG_ROOT for this invocation (test hook).",
    )
    lease_acq = subparsers.add_parser(
        "lease-acquire",
        help="Acquire a public reviewer lease over explicit tracker unit ids.",
    )
    lease_acq.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    lease_acq.add_argument("--session-id", required=True)
    lease_acq.add_argument("--run-id", required=True)
    lease_acq.add_argument("--reviewer-id", required=True)
    lease_acq.add_argument("--unit-ids", nargs="+", required=True)
    lease_acq.add_argument(
        "--holder-kind",
        choices=("reviewer", "validate-fix", "manual"),
        default="reviewer",
    )
    lease_acq.add_argument("--lease-ttl-seconds", type=int, default=None)
    lease_acq.add_argument("--print-result", action="store_true")
    lease_acq.add_argument("--log-root", default=None, help="Override CODEX_RVF_LOG_ROOT.")

    lease_rel = subparsers.add_parser(
        "lease-release",
        help="Release a tracker lease; completed leases mark units reviewed.",
    )
    lease_rel.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    lease_rel.add_argument("--lease-id", required=True)
    lease_rel.add_argument("--reason", default="completed")
    lease_rel.add_argument("--print-result", action="store_true")
    lease_rel.add_argument("--log-root", default=None, help="Override CODEX_RVF_LOG_ROOT.")

    lease_sweep = subparsers.add_parser(
        "lease-sweep",
        help="Release stale active leases whose TTL has expired.",
    )
    lease_sweep.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    lease_sweep.add_argument("--print-result", action="store_true")
    lease_sweep.add_argument("--log-root", default=None, help="Override CODEX_RVF_LOG_ROOT.")

    record = subparsers.add_parser(
        "record-manual-run",
        help="Record a completed manual RVF run for a scope_hash.",
    )
    record.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    record.add_argument("--session-id", required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--scope-hash", required=True)
    record.add_argument("--completed-at", default=None)
    record.add_argument("--print-result", action="store_true")
    record.add_argument(
        "--log-root",
        default=None,
        help="Override CODEX_RVF_LOG_ROOT for this invocation (test hook).",
    )
    takeover = subparsers.add_parser(
        "manual-takeover",
        help="Transfer a parent session's unleased units to the current session.",
    )
    takeover.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    takeover.add_argument("--parent-session-id", required=True)
    takeover.add_argument("--current-session-id", required=True)
    takeover.add_argument("--run-id", required=True)
    takeover.add_argument("--print-result", action="store_true")
    takeover.add_argument(
        "--log-root",
        default=None,
        help="Override CODEX_RVF_LOG_ROOT for this invocation (test hook).",
    )
    reopen = subparsers.add_parser(
        "reopen-run-scope",
        help="Reopen (reviewed→available) the still-present units a given run reviewed.",
    )
    reopen.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    reopen.add_argument("--run-id", required=True, help="target_run_id to reopen.")
    reopen.add_argument("--reason", default="failed_impl_reentry")
    reopen.add_argument("--print-result", action="store_true")
    reopen.add_argument(
        "--log-root",
        default=None,
        help="Override CODEX_RVF_LOG_ROOT for this invocation (test hook).",
    )
    latest_run = subparsers.add_parser(
        "latest-reviewed-run",
        help="Print the most recent RVF run that left reviewed units in this worktree.",
    )
    latest_run.add_argument("--repo", required=True, help="Path to the target repo / worktree.")
    latest_run.add_argument("--print-result", action="store_true")
    latest_run.add_argument(
        "--log-root",
        default=None,
        help="Override CODEX_RVF_LOG_ROOT for this invocation (test hook).",
    )
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.subcommand is None:
        parser.print_help()
        return 2
    log_root_override = Path(args.log_root).expanduser().resolve() if getattr(args, "log_root", None) else None
    if args.subcommand == "lease-acquire":
        result = lease_acquire(
            repo=Path(args.repo).expanduser().resolve(),
            session_id=args.session_id,
            run_id=args.run_id,
            reviewer_id=args.reviewer_id,
            unit_ids=list(args.unit_ids),
            holder_kind=args.holder_kind,
            lease_ttl_seconds=args.lease_ttl_seconds,
            log_root_override=log_root_override,
        )
        if args.print_result:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.subcommand == "lease-release":
        result = lease_release(
            repo=Path(args.repo).expanduser().resolve(),
            lease_id=args.lease_id,
            reason=args.reason,
            log_root_override=log_root_override,
        )
        if args.print_result:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.subcommand == "lease-sweep":
        result = sweep_stale(
            repo=Path(args.repo).expanduser().resolve(),
            log_root_override=log_root_override,
        )
        if args.print_result:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.subcommand == "record-manual-run":
        result = record_manual_rvf_run(
            repo=Path(args.repo).expanduser().resolve(),
            session_id=args.session_id,
            run_id=args.run_id,
            scope_hash=args.scope_hash,
            completed_at=args.completed_at,
            log_root_override=log_root_override,
        )
        if args.print_result:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.subcommand == "manual-takeover":
        result = manual_takeover(
            repo=Path(args.repo).expanduser().resolve(),
            parent_session_id=args.parent_session_id,
            current_session_id=args.current_session_id,
            run_id=args.run_id,
            log_root_override=log_root_override,
        )
        if args.print_result:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.subcommand == "reopen-run-scope":
        result = invalidate_reviewed_units_for_run(
            repo=Path(args.repo).expanduser().resolve(),
            run_id=args.run_id,
            reason=args.reason,
            log_root_override=log_root_override,
        )
        if args.print_result:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.subcommand == "latest-reviewed-run":
        result = latest_reviewed_run_for_worktree(
            repo=Path(args.repo).expanduser().resolve(),
            log_root_override=log_root_override,
        )
        if args.print_result:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.subcommand != "allocate-review-scope":
        parser.print_help()
        return 2
    if not args.dry_run and not args.reviewer_id:
        print(
            json.dumps(
                {"status": "error", "error": "--reviewer-id is required unless --dry-run is set"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    output_scope_path = Path(args.output_scope).expanduser().resolve() if args.output_scope else None
    repo_path = Path(args.repo).expanduser().resolve()
    result = allocate_review_scope(
        repo=repo_path,
        session_id=args.session_id,
        run_id=args.run_id,
        reviewer_id=args.reviewer_id,
        output_scope_path=output_scope_path,
        parent_session_id=args.parent_session_id,
        holder_kind=args.holder_kind,
        lease_ttl_seconds=args.lease_ttl_seconds,
        dry_run=bool(args.dry_run),
        log_root_override=log_root_override,
    )
    if args.print_result:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
