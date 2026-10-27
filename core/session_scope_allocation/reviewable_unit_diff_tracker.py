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
from typing import Any, Callable, Iterable, Sequence

# 本模块迁入 core/ 后既被当作 ``core.session_scope_allocation.reviewable_unit_diff_tracker``
# import、也作为 CLI(``__main__``) 与 ``spec_from_file_location`` 直接加载。后两种上下文里
# 含 ``core/`` 的 PYROOT 尚不在 ``sys.path``，且 ``_rvf_pyroot.py`` 不在本目录（它在
# ``scripts/``），无法沿用 scripts 侧「同目录 ``import _rvf_pyroot``」自举。故内联哨兵自举：
# 自底向上找同时含 ``.rvf-pyroot`` 哨兵与 ``core/`` 的最近祖先（与 ``_rvf_pyroot`` 同口径、
# depth-robust），把它插入 ``sys.path``，使下面的 ``core.*`` import 在 repo / 部署 payload /
# worktree / 测试四种上下文走同一条码路。被当作 ``core.*`` import 时 PYROOT 已在 path，幂等无副作用。
_pyroot_search_start = Path(__file__).resolve()
for _pyroot_candidate in (_pyroot_search_start, *_pyroot_search_start.parents):
    if (_pyroot_candidate / ".rvf-pyroot").is_file() and (_pyroot_candidate / "core").is_dir():
        if str(_pyroot_candidate) not in sys.path:
            sys.path.insert(0, str(_pyroot_candidate))
        break
from core.run_ledger.run_ledger import (  # noqa: E402
    _append_jsonl,
    _atomic_write_text,
    log_root,
    safe_token,
    utc_now,
)
from core.session_scope_allocation.frozen_review_unit_evidence import (  # noqa: E402
    freeze_observed_unit_evidence, tracked_hunk_content_identity,
    read_worktree_path_git_blob_input, write_worktree_path_git_normalized_blob,
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
BUSY_TIMEOUT_ENV = "RVF_TRACKER_BUSY_TIMEOUT_MS"

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
RANGE_TOLERANCE = 5

DISABLE_ENV = "RVF_TRACKER_DISABLE"

# Per-commit opt-out trailer (pillar ③ compatible). A round commit whose message
# carries this git trailer (`RVF-Skip-Review:` with an optional reason value, or a
# bare `RVF-Skip-Review` line) is excluded from the committed-round path set, so
# its changes are never auto-routed to review. This is the explicit "don't review
# this commit" escape hatch that complements the default-on attribution of
# `_list_committed_round_changed_paths`.
RVF_SKIP_REVIEW_COMMIT_TRAILER = "RVF-Skip-Review"

# Slice 3 reason-code rename (with one-release alias). The new names belong to
# the `allocate_review_scope` path; the legacy names stay live in the
# `RVF_TRACKER_DISABLE=1` fallback so disable-mode users see no behavior
# change.
REASON_NO_UNASSIGNED_REVIEW_SCOPE = "no_unassigned_review_scope"
REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE = "unassigned_review_scope_available"
LEGACY_REASON_NO_SESSION_OWNED_DIRTY = "no_session_owned_dirty"
LEGACY_REASON_SESSION_OWNED_DIRTY = "session_owned_dirty"

DEFAULT_LEASE_TTL_SECONDS = 600
LEASE_TTL_ENV = "RVF_TRACKER_LEASE_TTL_SECONDS"
MANUAL_RUN_TTL_ENV = "RVF_MANUAL_RUN_TTL_SECONDS"
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


def _canonical_hash_tracked_hunk(path: str, change_type: str, hunk_lines: list[str]) -> str:
    return tracked_hunk_content_identity(path, change_type, hunk_lines)


def _canonical_hash_added(path: str, content_sha: str) -> str:
    """Whole-file identity for a newly added file, keyed by (path, content_sha).

    The hash bytes deliberately keep the legacy ``b"untracked\0..."`` domain
    separator so an *untracked* observation and a *tracked* add (staged or
    committed) of the same new file converge on one unit_id. Keeping the bytes
    unchanged also means existing untracked unit_ids do not churn; only the
    tracked-add side now folds onto this identity. That convergence is what
    makes a new file reviewed-while-dirty dedup (stay ``reviewed``) once it is
    staged/committed, instead of re-dispatching as a fresh ``available`` unit.
    """
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
    postimage_hash: str | None                 # Git 规范化 blob 内容的 SHA-256
    old_path: str | None
    hunks: tuple[_ObservedHunk, ...] = ()     # only for tracked_hunk
    observed_worktree_content_sha256: str | None = None
    observed_worktree_path_is_symbolic_link: bool | None = None
    git_normalized_postimage_blob: str | None = None


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
    """对 Git blob 内容求摘要，与 dirty 侧经过 clean 规范化的内容身份一致。
    路径不存在时返回 None；工作区原始字节摘要独立用于漂移核验。"""
    try:
        out = _run_git_bytes(repo, ["cat-file", "-p", f"{ref}:{path}"])
    except RuntimeError:
        return None
    return hashlib.sha256(out).hexdigest()


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
        # 文件头只出现在首个 @@ 之前；进入 hunk 后的 +++/--- 都是合法正文。
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

    abs_file = repo / path

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

    try:
        is_symbolic_link = abs_file.is_symlink()
        raw_content = read_worktree_path_git_blob_input(abs_file)
        postimage_blob = write_worktree_path_git_normalized_blob(repo, path, raw_content,
            is_symbolic_link=is_symbolic_link)
        postimage_content_sha256 = hashlib.sha256(_run_git_bytes(
            repo, ["cat-file", "blob", postimage_blob])).hexdigest()
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        return None
    observed_worktree_identity_fields = {
        "observed_worktree_content_sha256": hashlib.sha256(raw_content).hexdigest(),
        "observed_worktree_path_is_symbolic_link": is_symbolic_link,
        "git_normalized_postimage_blob": postimage_blob,
    }

    # Untracked.
    if code_x == "?" and code_y == "?":
        return _PathObservation(
            path=path,
            kind="untracked_file",
            change_type="add",
            preimage_blob=None,
            postimage_hash=postimage_content_sha256,
            old_path=None,
            **observed_worktree_identity_fields,
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
            return _PathObservation(
                path=path,
                kind="binary_file",
                change_type="modify" if preimage_blob else "add",
                preimage_blob=preimage_blob,
                postimage_hash=postimage_content_sha256,
                old_path=None,
                **observed_worktree_identity_fields,
            )

    hunks = _parse_tracked_hunks(repo, path)
    if hunks:
        return _PathObservation(
            path=path,
            kind="tracked_hunk",
            change_type="modify" if preimage_blob else "add",
            preimage_blob=preimage_blob,
            postimage_hash=postimage_content_sha256,
            old_path=None,
            **observed_worktree_identity_fields,
            hunks=tuple(hunks),
        )

    if preimage_blob is None and abs_file.exists():
        # Empty (hunkless) staged add: an empty new file produces no diff hunk,
        # so without this it would fall through to path_only and mint a unit_id
        # that does NOT match the untracked observation of the same empty file.
        # Emit a content-keyed add (empty hunks) so `_specs_from_observation`'s
        # add branch folds it onto `_canonical_hash_added` and empty new files
        # dedup across dirty(untracked)↔staged↔committed like any other add.
        return _PathObservation(
            path=path,
            kind="tracked_hunk",
            change_type="add",
            preimage_blob=None,
            postimage_hash=postimage_content_sha256,
            old_path=None,
            **observed_worktree_identity_fields,
            hunks=(),
        )

    return None


def _added_file_unit_spec(path: str, postimage_hash: str | None) -> _UnitSpec:
    """Single whole-file spec for an added file observed as a *tracked* hunk
    (staged or committed add). Keyed by content via `_canonical_hash_added` so it
    shares the unit_id of the same file's untracked observation — without this, a
    new file picks up a fresh ``hunk``-body unit_id the moment it becomes tracked
    and a reviewed-while-dirty new file gets re-dispatched after commit/stage.

    ``kind='tracked_hunk'`` is accurate for this observation (the file *is*
    tracked at this point) and stays within the units.kind CHECK enum; the
    untracked observation keeps ``kind='untracked_file'``. The two share a
    unit_id and `_upsert_unit` merges them by primary key, so the differing kind
    is cosmetic while review_state (never rewritten on conflict) is preserved."""
    return _UnitSpec(
        unit_id=_canonical_hash_added(path, postimage_hash or ""),
        path=path,
        old_path=None,
        kind="tracked_hunk",
        change_type="add",
        preimage_blob=None,
        postimage_hash=postimage_hash,
        hunk_header=None,
    )


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
        if observation.change_type == "add":
            # A tracked add (staged/committed new file) is whole-file identity,
            # not per-hunk: fold it onto the same content-keyed unit_id as the
            # untracked observation so a reviewed-while-dirty new file dedups.
            return [_added_file_unit_spec(path, observation.postimage_hash)]
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
                unit_id=_canonical_hash_added(path, observation.postimage_hash or ""),
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

    if status_code == "A" and preimage_blob is None:
        # Empty (hunkless) committed add — mirror `_classify_path`: an empty new
        # file committed within the round has no diff hunk, so emit a
        # content-keyed add (empty hunks) instead of falling through to None /
        # path_only. This keeps its unit_id equal to the dirty/untracked
        # observation so a reviewed empty new file stays deduped after commit.
        return _PathObservation(
            path=path,
            kind="tracked_hunk",
            change_type="add",
            preimage_blob=None,
            postimage_hash=_blob_content_sha_at_ref(repo, "HEAD", path),
            old_path=None,
            hunks=(),
        )

    return None


def _commit_message_has_skip_review_trailer(message: str) -> bool:
    """True when a commit message opts out via the ``RVF-Skip-Review`` trailer.

    Accepts both the canonical git-trailer form (``RVF-Skip-Review: <reason>``,
    value optional) and a bare standalone ``RVF-Skip-Review`` line. Matching is
    case-insensitive on the trailer key and lenient about placement (any line
    in the message), so an agent need not perfectly format a trailer block to
    opt a commit out of review."""
    token = RVF_SKIP_REVIEW_COMMIT_TRAILER.lower()
    for raw_line in message.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == token:
            return True
        key = line.split(":", 1)[0].strip().lower()
        if key == token:
            return True
    return False


def _base_ref_exclude_revs(repo: Path, base_ref: str | None) -> list[str]:
    """``["^<base-sha>"]`` to subtract base-branch commits that a rebase / cherry
    replayed onto this round's first-parent line, or ``[]`` when no *safe*
    exclusion applies.

    The committed-round walks use ``--first-parent --no-merges``, which only drops
    base work imported through a *merge* second parent (``git merge`` / ``git
    pull``). A ``git rebase`` / ``git pull --rebase`` / ``base-branch-sync`` rebase
    replays the base commits as ordinary first-parent commits, so they survive the
    walk and get mis-attributed as this session's committed-but-unreviewed work.
    Appending ``^<base_ref>`` removes every commit reachable from the live base
    branch — exactly the replayed base work — while leaving the session's own
    commits (not on base) in scope.

    Safe-exclude preconditions (any miss → ``[]``, i.e. today's no-exclusion
    behaviour, so this can never *over*-exclude):

    - ``base_ref`` resolves to a real commit (unknown / ambiguous ref degrades
      silently rather than breaking the walk);
    - the current checkout's branch ref is NOT the base branch ref. Only when the
      session is committing *on* the base branch itself (``symbolic-ref HEAD`` equals
      ``<base_ref>``'s full ref, e.g. both ``refs/heads/main``) would ``^base_ref``
      subtract the session's OWN commits → self-disable there (plain repo, agent on
      ``main``). A *fast-forward* to the base tip (detached HEAD, or a non-base
      feature branch quick-forwarded onto base — ``git merge --ff-only`` /
      base-branch-sync non-divergent handback) leaves ``HEAD == base tip`` but NOT
      on the base branch ref; ff can only succeed when the branch carries no own
      commits, so ``baseline..HEAD`` is then pure base work and ``^base_ref`` safely
      drops it. (The earlier ``merge-base --is-ancestor HEAD base`` guard treated
      every ``HEAD == base tip`` alike and mis-self-disabled the ff-to-base case,
      leaking base commits into committed-round.)

    Resolving to a SHA pins the exclusion against ref churn and keeps all three
    committed-round walks (skip-sha, count, allowlist) consistent within a call."""
    if not base_ref:
        return []
    try:
        sha = _run_git(repo, ["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"]).strip()
    except RuntimeError:
        return []
    if not sha:
        return []
    # Self-disable ONLY when the current checkout's branch ref IS the base branch
    # ref — i.e. the session is committing on the base branch itself, where
    # ``^base_ref`` would subtract its OWN commits. A detached HEAD (``symbolic-ref``
    # fails) or any non-base branch — including one fast-forwarded onto the base tip
    # — is safe to exclude: ff implies the branch carries no own commits, so
    # ``baseline..HEAD`` minus base is empty / own-only, never the base's work.
    try:
        head_ref = _run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip()
    except RuntimeError:
        head_ref = ""  # detached HEAD → not on the base branch → apply exclusion
    if head_ref:
        try:
            base_branch_ref = _run_git(
                repo, ["rev-parse", "--symbolic-full-name", base_ref]
            ).strip()
        except RuntimeError:
            base_branch_ref = ""
        if base_branch_ref and head_ref == base_branch_ref:
            return []
    return [f"^{sha}"]


def _list_round_skip_review_commit_shas(
    repo: Path, baseline: str, base_ref: str | None = None
) -> set[str]:
    """SHAs of this round's first-parent, non-merge commits that carry the
    ``RVF-Skip-Review`` opt-out trailer (see ``RVF_SKIP_REVIEW_COMMIT_TRAILER``).

    Walks the same first-parent/no-merges window the path collector uses (incl. the
    same ``^<base_ref>`` exclusion of rebase-replayed base commits), so an excluded
    commit's changes drop out of every committed-round consumer at once."""
    base_excludes = _base_ref_exclude_revs(repo, base_ref)
    try:
        # %H<US>%B<RS> — unit-sep between sha and body, record-sep between commits.
        out = _run_git(
            repo,
            ["log", "--first-parent", "--no-merges", "--format=%H%x1f%B%x1e",
             f"{baseline}..HEAD", *base_excludes],
        )
    except RuntimeError:
        return set()
    skip: set[str] = set()
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record or "\x1f" not in record:
            continue
        sha, _sep, body = record.partition("\x1f")
        sha = sha.strip()
        if sha and _commit_message_has_skip_review_trailer(body):
            skip.add(sha)
    return skip


def _list_committed_round_changed_paths(
    repo: Path, baseline: str, base_ref: str | None = None
) -> list[str]:
    """Paths whose content differs between the `baseline` tree and `HEAD`,
    restricted to the work introduced by *this round's own* first-parent,
    non-merge, non-base commits, minus any commit that opted out via the
    ``RVF-Skip-Review`` trailer.

    Pillar ③ (exclude background WIP / base-branch sync): the ``baseline..HEAD``
    range already floors at this round's start, and when the round contains a
    merge (e.g. the agent ran `git merge main` / `git pull`) the first-parent +
    no-merges commit walk drops the merge commit and the base commits it imports
    via its second parent. But a *rebase* (`git rebase` / `git pull --rebase` /
    `base-branch-sync` diverged handback) replays the base commits as ordinary
    first-parent commits, so ``--first-parent --no-merges`` alone keeps them and
    over-attributes base work as this session's committed-but-unreviewed changes.
    When `base_ref` (the live base branch, e.g. `main`) is supplied,
    ``_base_ref_exclude_revs`` adds ``^<base-sha>`` to drop every base-reachable
    commit from the window — covering the rebase path that merge exclusion misses,
    and harmlessly redundant on the merge path. It self-disables (no exclusion)
    when HEAD is on/behind base, so committing directly on the base branch is
    unaffected.

    A first-parent commit may additionally opt out of review via the
    ``RVF-Skip-Review`` trailer; such commits' exclusive changes are removed here
    (a path also touched by a non-skip round commit stays, since it remains in
    that commit's name-only block). This function is the cheap structural
    pre-filter; `build_manifest` then attributes the survivors."""
    try:
        net = _run_git(repo, ["diff", "--no-renames", "--name-only", "-z", f"{baseline}..HEAD"])
    except RuntimeError:
        return []
    net_paths = sorted({item for item in net.split("\0") if item})
    if not net_paths:
        return []
    skip_shas = _list_round_skip_review_commit_shas(repo, baseline, base_ref)
    base_excludes = _base_ref_exclude_revs(repo, base_ref)
    # Fast path: the net (tree) diff already reflects only reviewable first-parent
    # own work when the round has no merges, no opt-out commits, AND no
    # base-reachable commit replayed onto the first-parent line. Any of those means
    # a path in the net diff might belong to non-session work, so fall to the
    # allowlist pass (the net tree diff cannot itself honour `^base_ref`, so base
    # exclusion is realised by intersecting against the base-excluded walk below).
    try:
        total = _run_git(repo, ["rev-list", "--count", f"{baseline}..HEAD"]).strip()
        first_parent = _run_git(
            repo, ["rev-list", "--count", "--first-parent", "--no-merges", f"{baseline}..HEAD"]
        ).strip()
        no_merges = total == first_parent
        base_in_window = bool(base_excludes) and (
            _run_git(
                repo, ["rev-list", "--count", f"{baseline}..HEAD", *base_excludes]
            ).strip()
            != total
        )
    except RuntimeError:
        # Counts unavailable: only safe to fast-return when nothing must be
        # filtered out; otherwise fall through to the per-commit allowlist.
        if not skip_shas and not base_excludes:
            return net_paths
        no_merges = False
        base_in_window = bool(base_excludes)
    if no_merges and not skip_shas and not base_in_window:
        return net_paths
    # Merges, opt-out commits, and/or rebase-replayed base commits present: keep
    # only paths touched by a first-parent, non-merge, non-base, non-skip commit.
    # `^<base-sha>` drops base-reachable commits; the `%x1e%H` record marker lets
    # us attribute each name-only block to its commit so skip commits drop out.
    try:
        log_out = _run_git(
            repo,
            ["log", "--first-parent", "--no-merges", "--no-renames", "--name-only",
             "--format=%x1e%H", f"{baseline}..HEAD", *base_excludes],
        )
    except RuntimeError:
        return net_paths
    allowed: set[str] = set()
    for block in log_out.split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        lines = block.split("\n")
        sha = lines[0].strip()
        if sha in skip_shas:
            continue
        for line in lines[1:]:
            path = line.strip()
            if path:
                allowed.add(path)
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
        if observation.change_type == "add":
            # Mirror `_specs_from_observation`: a tracked add is whole-file
            # identity. register_claims keys session_units ownership on this
            # unit_id, so it MUST equal the observation walk's id for the same
            # staged add — otherwise the owned (hunk-body) unit and the observed
            # (content-keyed) unit diverge and the dirty review mis-binds.
            return [_added_file_unit_spec(owned_unit.path, observation.postimage_hash)]
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
                unit_id=_canonical_hash_added(observation.path, observation.postimage_hash or ""),
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
  -- 「agent 谢绝这一轮 RVF 时显式声明本 diff 不需审核」的可审计侧车。豁免本身**不是**靠这三
  -- 列生效的：生效靠把 review_state 置成 'reviewed'（从而自动继承 scope 分配 / committed-round
  -- 检测 / highwater / dedup 等全部既有过滤）。这三列只回答「这条 reviewed 究竟是真审过、还是
  -- 被声明不需审」。
  --
  -- ⚠️ 三列刻意**不进** _upsert_unit 的 ON CONFLICT SET 列表——与 review_state 同等待遇。
  -- 反例就在隔壁：is_tombstoned/tombstoned_at/tombstone_reason 在 ON CONFLICT 里被无条件重置，
  -- 而每次 Stop 都会跑一次 tracker refresh，豁免若挂在 tombstone 一侧会被静默抹掉。
  review_not_required_declared_at         TEXT,
  review_not_required_declared_reason     TEXT,
  review_not_required_declared_by_run_id  TEXT,
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
  state         TEXT NOT NULL CHECK (state IN ('open','fixed','false_positive','elevated','failed','superseded','submitted_to_kanban_backlog')),
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
  status                 TEXT NOT NULL CHECK (status IN ('prepared','started','fixed','false_positive','elevated','failed','applied','merge_conflict','validated_and_submitted_to_kanban_backlog')),
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
        conn.execute("""CREATE TABLE IF NOT EXISTS unit_frozen_review_evidence (
            unit_id TEXT PRIMARY KEY REFERENCES units(unit_id) ON DELETE CASCADE,
            frozen_evidence_json TEXT NOT NULL
        )""")
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
        _ensure_unit_review_not_required_waiver_schema(conn)
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


def _ensure_unit_review_not_required_waiver_schema(conn: sqlite3.Connection) -> None:
    """给既有库补「声明不需审核」的可审计侧车列（纯 ADD COLUMN，无需 rebuild）。

    只加列不改 CHECK 约束：豁免的生效路径是 ``review_state='reviewed'``（既有合法取值），
    这三列纯审计。列名一律全称，别缩成 ``waived_at`` 之类脱离语境读不出含义的名字。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)").fetchall()}
    if "review_not_required_declared_at" not in columns:
        conn.execute("ALTER TABLE units ADD COLUMN review_not_required_declared_at TEXT")
    if "review_not_required_declared_reason" not in columns:
        conn.execute("ALTER TABLE units ADD COLUMN review_not_required_declared_reason TEXT")
    if "review_not_required_declared_by_run_id" not in columns:
        conn.execute("ALTER TABLE units ADD COLUMN review_not_required_declared_by_run_id TEXT")


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
          state         TEXT NOT NULL CHECK (state IN ('open','fixed','false_positive','elevated','failed','superseded','submitted_to_kanban_backlog')),
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
          status                 TEXT NOT NULL CHECK (status IN ('prepared','started','fixed','false_positive','elevated','failed','applied','merge_conflict','validated_and_submitted_to_kanban_backlog')),
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
    _rebuild_rvf_causality_tables_admitting_kanban_backlog_states(conn)


RVF_ISSUE_STATE_SUBMITTED_TO_KANBAN_BACKLOG = "submitted_to_kanban_backlog"
RVF_ATTEMPT_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG = "validated_and_submitted_to_kanban_backlog"


def _rebuild_rvf_causality_tables_admitting_kanban_backlog_states(conn: sqlite3.Connection) -> bool:
    """给既有库的 ``rvf_issues.state`` / ``rvf_fix_attempts.status`` CHECK 约束补上 Kanban backlog 两个取值。

    范围外问题「只验证、不修、提交 backlog」要落成机器事实：issue 终态 ``submitted_to_kanban_backlog``、
    attempt 终态 ``validated_and_submitted_to_kanban_backlog``。SQLite 改 CHECK 只能重建表；按建表 SQL
    里有没有新取值自愈判断（与 ``_rebuild_units_without_legacy_tombstoned_review_state`` 同一模式），
    重建期间关外键，否则 DROP 会级联删掉 patch 事件与因果链接。"""
    issues_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='rvf_issues'"
    ).fetchone()
    attempts_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='rvf_fix_attempts'"
    ).fetchone()
    issues_sql = issues_sql_row["sql"] if issues_sql_row else ""
    attempts_sql = attempts_sql_row["sql"] if attempts_sql_row else ""
    issues_need_rebuild = bool(issues_sql) and f"'{RVF_ISSUE_STATE_SUBMITTED_TO_KANBAN_BACKLOG}'" not in issues_sql
    attempts_need_rebuild = bool(attempts_sql) and (
        f"'{RVF_ATTEMPT_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG}'" not in attempts_sql
    )
    if not issues_need_rebuild and not attempts_need_rebuild:
        return False
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        script = ["BEGIN IMMEDIATE;"]
        if issues_need_rebuild:
            script.append(
                """
                CREATE TABLE rvf_issues_admitting_kanban_backlog_state (
                  issue_key     TEXT PRIMARY KEY,
                  repo_key      TEXT NOT NULL,
                  run_id        TEXT NOT NULL,
                  issue_id      TEXT NOT NULL,
                  payload       TEXT NOT NULL,
                  source_refs   TEXT NOT NULL,
                  artifact_path TEXT,
                  state         TEXT NOT NULL CHECK (state IN ('open','fixed','false_positive','elevated','failed','superseded','submitted_to_kanban_backlog')),
                  created_at    TEXT NOT NULL,
                  updated_at    TEXT NOT NULL,
                  UNIQUE(run_id, issue_id)
                );
                INSERT INTO rvf_issues_admitting_kanban_backlog_state
                  SELECT issue_key, repo_key, run_id, issue_id, payload, source_refs,
                         artifact_path, state, created_at, updated_at
                  FROM rvf_issues;
                DROP TABLE rvf_issues;
                ALTER TABLE rvf_issues_admitting_kanban_backlog_state RENAME TO rvf_issues;
                CREATE INDEX IF NOT EXISTS idx_rvf_issues_run ON rvf_issues(run_id);
                """
            )
        if attempts_need_rebuild:
            script.append(
                """
                CREATE TABLE rvf_fix_attempts_admitting_kanban_backlog_status (
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
                  status                 TEXT NOT NULL CHECK (status IN ('prepared','started','fixed','false_positive','elevated','failed','applied','merge_conflict','validated_and_submitted_to_kanban_backlog')),
                  result_payload         TEXT NOT NULL DEFAULT '{}',
                  created_at             TEXT NOT NULL,
                  updated_at             TEXT NOT NULL,
                  started_at             TEXT,
                  stopped_at             TEXT,
                  applied_at             TEXT,
                  FOREIGN KEY(issue_key) REFERENCES rvf_issues(issue_key) ON DELETE CASCADE
                );
                INSERT INTO rvf_fix_attempts_admitting_kanban_backlog_status
                  SELECT attempt_id, issue_key, repo_key, run_id, issue_id, worktree_path,
                         base_head, baseline_overlay_path, baseline_commit, fix_patch_path,
                         status, result_payload, created_at, updated_at, started_at, stopped_at, applied_at
                  FROM rvf_fix_attempts;
                DROP TABLE rvf_fix_attempts;
                ALTER TABLE rvf_fix_attempts_admitting_kanban_backlog_status RENAME TO rvf_fix_attempts;
                CREATE INDEX IF NOT EXISTS idx_rvf_fix_attempts_run ON rvf_fix_attempts(run_id);
                CREATE INDEX IF NOT EXISTS idx_rvf_fix_attempts_issue ON rvf_fix_attempts(issue_key);
                """
            )
        script.append("COMMIT;")
        conn.executescript("\n".join(script))
    finally:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"tracker rvf causality migration left foreign key violations: {violations!r}")
    return True


def _migrate_schema_v3_to_v4(conn: sqlite3.Connection) -> None:
    _ensure_manual_rvf_runs_schema(conn)
    _ensure_lease_watermark_schema(conn)
    _ensure_lease_participants_schema(conn)
    _ensure_rvf_causality_schema(conn)
    conn.execute("PRAGMA user_version = 4")


def _migrate_schema_v4_to_v5(conn: sqlite3.Connection) -> None:
    _ensure_unit_tombstone_schema(conn)
    _ensure_unit_review_not_required_waiver_schema(conn)
    _ensure_manual_rvf_runs_schema(conn)
    _ensure_lease_watermark_schema(conn)
    _ensure_lease_participants_schema(conn)
    _ensure_rvf_causality_schema(conn)
    conn.execute("PRAGMA user_version = 5")


def _migrate_schema_v5_to_v6(conn: sqlite3.Connection) -> None:
    _ensure_unit_tombstone_schema(conn)
    _ensure_unit_review_not_required_waiver_schema(conn)
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
                still_held_by_other_session = conn.execute(
                    """
                    SELECT 1 FROM session_units
                     WHERE unit_id=? AND session_id<>? AND assignment_kind IN ('owned','takeover')
                     LIMIT 1
                    """,
                    (orphan_unit_id, session_id),
                ).fetchone()
                if still_held_by_other_session is not None:
                    # 本会话不再声明，但别的会话仍持有：只撤本会话这一行，不把单元全局 superseded / tombstone
                    # （分层补判的弱证据会随改动时刻漂移，不能因此波及别的会话；08ac4 独立复核 M10）。
                    _emit_event(
                        events_path,
                        {
                            "event": "claim_dropped",
                            "unit_id": orphan_unit_id,
                            "session_id": session_id,
                            "run_id": run_id,
                            "reason": "session_no_longer_owns_but_other_session_still_holds_unit",
                        },
                    )
                    continue
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
    exclude_run_id: str | None = None,
) -> dict[str, Any]:
    """本会话已交给 review 的 transcript 行号上界（各 lease 的 ``transcript_max_line_number`` 取最大）。

    ``exclude_run_id``：同一轮 run 在分配 lease 之后再重建 session-manifest（prepare、``rvf scope claim``）时，
    不能被**本轮自己**的 lease 截断——否则本轮已归属的调用全落到水位线下，``register_claims`` 的孤儿清理会把
    它们从 session_units 删掉（08ac4 端到端测试实测）。"""
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
               AND run_id <> ?
            """,
            (session_id, exclude_run_id or ""),
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


SESSION_PATH_CLAIMS_BY_AGENT_EXPLICIT_DECLARATION_TABLE = "session_path_claims_by_agent_explicit_declaration"
# 认领时文件已被删除：内容哈希记成这个哨兵，删除状态不变就一直有效。
CLAIMED_WORKTREE_FILE_ABSENT_CONTENT_SHA256 = "worktree_file_absent_at_claim_time"


def _ensure_session_path_claims_by_agent_explicit_declaration_schema(conn: sqlite3.Connection) -> None:
    """agent 用 ``rvf scope claim`` 显式认领的路径。

    归属的分层补判（命令文本 / 时间窗）判不准时，最后由 agent 自己认领。``register_claims`` 每次都会清掉
    本会话不再声明的单元（孤儿清理），所以认领必须单独落表，由 ``build_manifest`` 每次重新注入。
    认领绑定当时的文件内容：内容变了就不再生效，免得审过、提交之后别的会话再改同一个文件时，
    又被算到本会话头上。"""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SESSION_PATH_CLAIMS_BY_AGENT_EXPLICIT_DECLARATION_TABLE} (
          session_id                            TEXT NOT NULL,
          path                                  TEXT NOT NULL,
          claimed_worktree_file_content_sha256  TEXT NOT NULL,
          reason                                TEXT NOT NULL,
          decision_token                        TEXT,
          run_id                                TEXT,
          claimed_at                            TEXT NOT NULL,
          PRIMARY KEY (session_id, path)
        )
        """
    )


def worktree_file_content_sha256_for_agent_explicit_claim(repo: Path, path: str) -> str:
    candidate = repo / path
    try:
        if candidate.is_symlink():
            return hashlib.sha256(("symlink:" + os.readlink(candidate)).encode("utf-8")).hexdigest()
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except FileNotFoundError:
        return CLAIMED_WORKTREE_FILE_ABSENT_CONTENT_SHA256
    except (IsADirectoryError, OSError):
        return CLAIMED_WORKTREE_FILE_ABSENT_CONTENT_SHA256


def record_session_path_claims_by_agent_explicit_declaration(
    *,
    repo: Path,
    session_id: str,
    paths: Iterable[str],
    reason: str,
    decision_token: str | None = None,
    run_id: str | None = None,
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    repo_resolved = repo.resolve()
    if _disabled():
        return {"status": "disabled", "recorded_paths": []}
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None or is_bare_repo(repo_resolved):
        return {"status": "unsupported_repo", "recorded_paths": []}
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    directory = tracker_dir(base, key)
    directory.mkdir(parents=True, exist_ok=True)
    now = _tracker_now_iso()
    recorded: list[dict[str, str]] = []
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(directory / SQLITE_FILENAME)
        with _begin_immediate(conn):
            _ensure_session_path_claims_by_agent_explicit_declaration_schema(conn)
            for path in sorted({str(path) for path in paths if str(path).strip()}):
                content_sha256 = worktree_file_content_sha256_for_agent_explicit_claim(repo_resolved, path)
                conn.execute(
                    f"""
                    INSERT INTO {SESSION_PATH_CLAIMS_BY_AGENT_EXPLICIT_DECLARATION_TABLE}
                      (session_id, path, claimed_worktree_file_content_sha256, reason, decision_token, run_id, claimed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, path) DO UPDATE SET
                      claimed_worktree_file_content_sha256=excluded.claimed_worktree_file_content_sha256,
                      reason=excluded.reason,
                      decision_token=excluded.decision_token,
                      run_id=excluded.run_id,
                      claimed_at=excluded.claimed_at
                    """,
                    (session_id, path, content_sha256, reason, decision_token, run_id, now),
                )
                recorded.append({"path": path, "claimed_worktree_file_content_sha256": content_sha256})
        _emit_event(
            directory / EVENTS_FILENAME,
            {
                "event": "session_path_claimed_by_agent_explicit_declaration",
                "session_id": session_id,
                "run_id": run_id,
                "paths": [item["path"] for item in recorded],
                "reason": reason,
            },
        )
    finally:
        if conn is not None:
            conn.close()
    return {"status": "ok", "recorded_paths": recorded, "tracker_dir": str(directory)}


def list_still_valid_session_path_claims_by_agent_explicit_declaration(
    *,
    repo: Path,
    session_id: str,
    log_root_override: Path | None = None,
) -> set[str]:
    """本会话认领过、且文件内容与认领时一致的路径。读不到 / 表不存在 → 空集。"""
    repo_resolved = repo.resolve()
    if _disabled() or not session_id:
        return set()
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return set()
    base = log_root_override if log_root_override is not None else log_root()
    db_path = tracker_dir(base, repo_key(common_dir)) / SQLITE_FILENAME
    if not db_path.exists():
        return set()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT path, claimed_worktree_file_content_sha256
              FROM {SESSION_PATH_CLAIMS_BY_AGENT_EXPLICIT_DECLARATION_TABLE}
             WHERE session_id=?
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        if conn is not None:
            conn.close()
    return {
        row["path"]
        for row in rows
        if worktree_file_content_sha256_for_agent_explicit_claim(repo_resolved, row["path"])
        == row["claimed_worktree_file_content_sha256"]
    }


def dirty_paths_held_by_other_sessions_with_non_weak_evidence(
    *,
    repo: Path,
    session_id: str | None,
    weak_evidence_labels: Iterable[str],
    log_root_override: Path | None = None,
) -> set[str]:
    """本 worktree 里被**别的**会话以非弱证据（写工具、命令文本、认领…）持有的脏路径。只读；读不到 → 空集。

    session 归属分层补判的第 4 层（Kanban 单任务 worktree 默认认领）据此跳过「明摆着是别人写的」路径。"""
    repo_resolved = repo.resolve()
    if _disabled():
        return set()
    common_dir = git_common_dir(repo_resolved)
    if common_dir is None:
        return set()
    base = log_root_override if log_root_override is not None else log_root()
    db_path = tracker_dir(base, repo_key(common_dir)) / SQLITE_FILENAME
    if not db_path.exists():
        return set()
    weak = sorted({str(label) for label in weak_evidence_labels})
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT DISTINCT u.path AS path
              FROM units u
              JOIN session_units su ON su.unit_id = u.unit_id
             WHERE u.worktree_key = ?
               AND u.is_tombstoned = 0
               AND u.observed_state = 'dirty'
               AND su.assignment_kind IN ('owned', 'takeover')
               AND su.session_id <> ?
               AND COALESCE(su.evidence, '') NOT IN ({",".join("?" for _ in weak) or "''"})
            """,
            (_worktree_key(str(repo_resolved)), session_id or "", *weak),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        if conn is not None:
            conn.close()
    return {row["path"] for row in rows}


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


def read_tracker_lease_reconciliation_evidence_for_exact_run(
    *,
    repo: str | Path,
    run_id: str,
    expected_lease_id: str | None = None,
    log_root_override: Path | None = None,
) -> dict[str, Any]:
    """只读核验精确 run 的 lease 归属和终态，供清理前补证；读失败保持 unknown。

    原 run artifact 丢失时可从原生账本恢复 lease 身份；artifact 若指向别的 run 则拒绝释放。
    当前工作树必须持有该 lease 的全部 unit，不能因同仓／同名 run 就释放另一 worktree 的 lease。
    """
    connection: sqlite3.Connection | None = None
    try:
        repo_resolved = Path(repo).expanduser().resolve()
        common_dir = git_common_dir(repo_resolved)
        if common_dir is None:
            return {"status": "unknown", "reason": "tracker_repository_identity_unavailable"}
        base = log_root_override if log_root_override is not None else log_root()
        db_path = tracker_dir(base, repo_key(common_dir)) / SQLITE_FILENAME
        try:
            db_path.stat()
        except FileNotFoundError:
            if expected_lease_id:
                return {"status": "unknown", "reason": "expected_tracker_lease_database_missing"}
            return {"status": "verified", "reason": "no_tracker_database", "leases": []}
        connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if expected_lease_id:
            expected = connection.execute("SELECT run_id FROM leases WHERE lease_id=?", (expected_lease_id,)).fetchone()
            if expected is None or expected["run_id"] != run_id:
                return {"status": "unknown", "reason": "tracker_lease_exact_run_identity_mismatch",
                        "lease_id": expected_lease_id, "run_id": run_id}
        rows = connection.execute("SELECT lease_id, run_id, state FROM leases WHERE run_id=?", (run_id,)).fetchall()
        for row in rows:
            worktrees = connection.execute(
                "SELECT DISTINCT u.worktree_key FROM lease_units lu JOIN units u ON u.unit_id=lu.unit_id WHERE lu.lease_id=?",
                (row["lease_id"],),
            ).fetchall()
            if any(item["worktree_key"] != _worktree_key(str(repo_resolved)) for item in worktrees):
                return {"status": "unknown", "reason": "tracker_lease_worktree_identity_mismatch",
                        "lease_id": row["lease_id"], "run_id": run_id}
        return {"status": "verified", "reason": "native_tracker_exact_run_checked",
                "run_id": run_id, "leases": [dict(row) for row in rows]}
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return {"status": "unknown", "reason": "native_tracker_exact_run_query_failed",
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if connection is not None:
            connection.close()


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
    mark_reviewed: bool = True,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Mark a finished review scope as reviewed.

    This is intentionally stronger than generic lease release: completed RVF
    scopes must leave durable `reviewed` unit state, even when a long-running
    reviewer let its lease expire and stale-sweep already removed `lease_units`.

    ``mark_reviewed`` (default True) gates the unit ``review_state='reviewed'``
    flip. A no-op completion — a run that released its lease without actually
    dispatching any reviewer (no review artifacts on disk) — must pass
    ``mark_reviewed=False`` so it still releases the lease/participants but does
    NOT silently mark unreviewed units reviewed. Flipping on a no-op completion
    would turn an over-dispatch into a fresh missed-review (see
    `rvf_run_finalize._release_tracker_lease`).
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
                if reviewable_unit_ids:
                    reviewable_placeholders = ",".join("?" for _ in reviewable_unit_ids)
                    if mark_reviewed:
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
                        # No-op completion: this run released its lease without
                        # actually reviewing. Revert any units it had assigned
                        # back to 'available' (mirror lease_release's failed path)
                        # so genuinely-unreviewed work stays reviewable instead of
                        # being stranded 'assigned' or silently marked 'reviewed'.
                        # Emptying reviewable_unit_ids below keeps the downstream
                        # edit_claims update and the returned counts honest.
                        conn.execute(
                            f"""
                            UPDATE units
                               SET review_state='available'
                             WHERE review_state='assigned'
                               AND is_tombstoned=0
                               AND unit_id IN ({reviewable_placeholders})
                            """,
                            tuple(reviewable_unit_ids),
                        )
                        reviewable_unit_ids = []
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


def declare_units_review_not_required(
    *,
    repo: str | Path,
    reason: str,
    unit_ids: Sequence[str] | None = None,
    lease_id: str | None = None,
    run_id: str | None = None,
    declared_by_run_id: str | None = None,
    log_root_override: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """把一批 reviewable unit 声明为「不需审核」，使后续 RVF 不再把它们纳入 scope。

    用途：agent **谢绝**一轮自动 RVF 时可选地带上豁免（默认不带）。不带豁免的谢绝只是「这一
    轮不审」，下次 Stop 仍会重新提议；带豁免则该批 diff 从此不再进入审核范围。

    **粒度是 diff hunk 级、不是 file 级**——这是 tracker 的原生粒度：``unit_id`` 由
    ``canonical_patch_hash`` 等**内容**派生，所以豁免天然只绑定「这一段 diff 的这个内容」。
    该 hunk 内容一旦被改动，就会产生一个**新的 unit_id**，不继承豁免、重新进入审核范围。
    file 级豁免做不到这点（会把日后对该文件的一切新编辑一并吞掉）。

    实现刻意挂在 ``review_state='reviewed'`` 一侧，而**不是**复用 tombstone：
    ``_upsert_unit`` 的 ON CONFLICT 子句会无条件重置 ``is_tombstoned/tombstoned_at/
    tombstone_reason``，而每次 Stop hook 都跑一次 tracker refresh —— 豁免若挂在 tombstone 上
    会被静默抹掉。``review_state`` 及本函数写的三列侧车都不在那份 SET 列表里，故跨再观察持久。
    置成 ``reviewed`` 还顺带**自动继承**既有全部过滤（scope 分配 / committed-round 检测 /
    highwater / dedup 都已按 reviewed 排除），无需逐处改。

    目标 unit 三选一（按此优先序解析，至少给一个）：``unit_ids`` 显式列表 / ``lease_id``
    （该 lease 圈定的全部 unit）/ ``run_id``（该 run 的 ``session_units``）。
    tombstoned unit 一律跳过（已消失的 diff 无所谓豁免）。
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
            requested_unit_ids: list[str] = []
            resolved_from: str
            if unit_ids:
                requested_unit_ids = sorted({uid for uid in unit_ids if isinstance(uid, str) and uid})
                resolved_from = "unit_ids"
            elif lease_id:
                requested_unit_ids = sorted(_unit_ids_for_lease_in_txn(conn, lease_id))
                resolved_from = "lease_id"
            elif run_id:
                rows = conn.execute(
                    "SELECT DISTINCT unit_id FROM session_units WHERE run_id=?",
                    (run_id,),
                ).fetchall()
                requested_unit_ids = sorted(row["unit_id"] for row in rows)
                resolved_from = "run_id"
            else:
                return {
                    "status": "noop",
                    "reason": "no_target_units_specified",
                    "declared_unit_ids": [],
                    "declared_unit_count": 0,
                    "repo_key": key,
                    "tracker_dir": str(directory),
                }

            declared_unit_ids: list[str] = []
            if requested_unit_ids:
                placeholders = ",".join("?" for _ in requested_unit_ids)
                present_rows = conn.execute(
                    f"""
                    SELECT unit_id FROM units
                     WHERE is_tombstoned=0
                       AND unit_id IN ({placeholders})
                    """,
                    tuple(requested_unit_ids),
                ).fetchall()
                declared_unit_ids = sorted(row["unit_id"] for row in present_rows)
            if declared_unit_ids:
                placeholders = ",".join("?" for _ in declared_unit_ids)
                conn.execute(
                    f"""
                    UPDATE units
                       SET review_state='reviewed',
                           review_not_required_declared_at=?,
                           review_not_required_declared_reason=?,
                           review_not_required_declared_by_run_id=?
                     WHERE is_tombstoned=0
                       AND unit_id IN ({placeholders})
                    """,
                    (now_iso, reason, declared_by_run_id or run_id, *declared_unit_ids),
                )
                # 维持 complete_review_scope 的不变量（claim 为 reviewed 当且仅当其全部 unit
                # 都 reviewed 且在场）：豁免同样让 claim 满足该条件，故一并推进，免得留下永远
                # pending 的孤儿 claim。
                conn.execute(
                    f"""
                    UPDATE edit_claims
                       SET status='reviewed',
                           reviewed_at=?,
                           last_seen_at=?
                     WHERE status='pending'
                       AND claim_id IN (
                           SELECT DISTINCT claim_id
                             FROM edit_claim_units
                            WHERE unit_id IN ({placeholders})
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
                    (now_iso, now_iso, *declared_unit_ids),
                )
        _ensure_meta(directory, repo_resolved, common_dir, key)
        _emit_event(
            events_path,
            {
                "event": "units_declared_review_not_required",
                "rvf_state_phase": "review",
                "run_id": declared_by_run_id or run_id,
                "lease_id": lease_id,
                "reason_code": reason,
                "resolved_from": resolved_from,
                "requested_unit_count": len(requested_unit_ids),
                "declared_unit_count": len(declared_unit_ids),
                "declared_unit_ids": declared_unit_ids,
            },
        )
        return {
            "status": "declared" if declared_unit_ids else "noop",
            "reason": reason,
            "resolved_from": resolved_from,
            "requested_unit_ids": requested_unit_ids,
            "declared_unit_ids": declared_unit_ids,
            "declared_unit_count": len(declared_unit_ids),
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
                   -- 被显式声明「不需审核」的 unit 不参与失败再入的重开：它的 reviewed 不是
                   -- 「审过了」而是「用户/agent 决定不审」，重开会把那个决定悄悄撤销。
                   AND u.review_not_required_declared_at IS NULL
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
    committed_base_ref: str | None = None,
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
    try:
        observed_head_commit = _run_git(repo, ["rev-parse", "--verify", "HEAD"]).strip()
    except RuntimeError:
        # 尚无首次提交时仍保留既有候选枚举；正式冻结前不能宣称已绑定原始 HEAD。
        observed_head_commit = ""

    def record_observation(observation: _PathObservation, specs: list[_UnitSpec], source: str) -> None:
        baseline_commit = _run_git(repo, ["rev-parse", committed_baseline]).strip() if source == "committed" else observed_head_commit
        for evidence in freeze_observed_unit_evidence(
            repo, observation, specs, source=source, baseline_commit=baseline_commit,
            target_commit=observed_head_commit,
        ):
            conn.execute("""INSERT INTO unit_frozen_review_evidence VALUES (?, ?)
                ON CONFLICT(unit_id) DO UPDATE SET frozen_evidence_json=excluded.frozen_evidence_json""",
                (evidence["unit_id"], json.dumps(evidence, ensure_ascii=False)))

    if committed_baseline:
        for path in _list_committed_round_changed_paths(repo, committed_baseline, committed_base_ref):
            observation = _classify_committed_path(repo, path, committed_baseline)
            if observation is None:
                continue
            specs = _specs_from_observation(observation, path)
            for spec in specs:
                _upsert_unit(
                    conn, spec, branch_key, worktree_key, now_iso, observed_state="committed"
                )
                committed_observed_unit_ids.add(spec.unit_id)
            record_observation(observation, specs, "committed")

    dirty_paths = _list_dirty_paths(repo)
    observed_unit_ids: set[str] = set()
    for path in dirty_paths:
        observation = _classify_path(repo, path)
        specs = _specs_from_observation(observation, path)
        for spec in specs:
            _upsert_unit(conn, spec, branch_key, worktree_key, now_iso)
            observed_unit_ids.add(spec.unit_id)
        if observation is not None:
            record_observation(observation, specs, "dirty")

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


def _union_prior_scope_unit_ids_in_txn(
    conn: sqlite3.Connection,
    *,
    candidates: list[str],
    prior_scope_unit_ids: Sequence[str] | None,
) -> tuple[list[str], list[str]]:
    """把上一轮 scope 里**仍可审**的 unit 并回候选集，返回 ``(合并后候选, 本次并回的)``。

    过滤条件与 ``_collect_candidate_unit_ids_in_txn`` 完全一致（``review_state='available'``、
    未 tombstone、``observed_state IN ('dirty','committed')``），只是不受 session 归属约束
    ——重新分配时 session id 常常已经换了，正是那条约束把上一轮的 committed-round scope
    整个漏掉的。已被真正审掉（``reviewed``）的不会并回。

    ⚠️ 第二个返回值是**排他检查之前**的清单：active lease 的反连接由调用方的 Step 5 统一做
    （并回的 unit 必须与本轮新观察到的走同一道检查）。因此调用方在对外报告「承接自上一轮」
    之前，必须先把它与 Step 5 的幸存集合求交，否则会报出 reviewer 根本没拿到的 unit。
    """
    if not prior_scope_unit_ids:
        return candidates, []
    requested = sorted({uid for uid in prior_scope_unit_ids if isinstance(uid, str) and uid})
    already = set(candidates)
    missing = [uid for uid in requested if uid not in already]
    if not missing:
        return candidates, []
    placeholders = ",".join("?" for _ in missing)
    rows = conn.execute(
        f"""
        SELECT unit_id FROM units
         WHERE review_state='available'
           AND is_tombstoned=0
           AND observed_state IN ('dirty','committed')
           AND unit_id IN ({placeholders})
         ORDER BY path, hunk_header IS NULL, hunk_header, unit_id
        """,
        tuple(missing),
    ).fetchall()
    unioned = [row["unit_id"] for row in rows]
    return candidates + unioned, unioned


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
        SELECT u.unit_id, u.path, u.hunk_header, e.frozen_evidence_json
          FROM units u LEFT JOIN unit_frozen_review_evidence e ON e.unit_id=u.unit_id
         WHERE u.unit_id IN ({placeholders})
         ORDER BY u.path, u.hunk_header IS NULL, u.hunk_header, u.unit_id
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
                "frozen_evidence": json.loads(row["frozen_evidence_json"]) if row["frozen_evidence_json"] else None,
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


def select_committed_paths_reviewed_by_session(
    *,
    repo: Path,
    session_id: str | None,
    candidate_paths: Iterable[str],
    log_root_override: Path | None = None,
) -> set[str]:
    """# OBSERVATION-WINDOW(R9) —— review_state「已审/未审」真相源过滤器（R9 真相源迁移落点）。

    给定一组「本轮已提交」候选路径，返回其中**已被本 session 审过**的路径子集，供 detection /
    候选摘要从 committed 候选里剔除。判据（per-unit ``review_state`` 为「已审」裁决真相源，owner
    归属经 ``sessions`` / ``session_units`` 表）：

      * 该路径在本 worktree 存在 ``observed_state='committed'`` 且未 tombstone 的单元；
      * 这些 committed 单元 **全部** ``review_state='reviewed'``；
      * 且其中 **至少一个** 归属本 session（``session_units`` ``assignment_kind IN
        ('owned','takeover')``，与候选摘要 ``_read_tracker_ownership`` 同口径）。

    任一不满足（无登记单元 / 存在 ``available``|``assigned`` 未审单元 / 仅归属他 session）→ **不
    剔除**（保守偏「重审」方向，与 finalize 高水位『纯 no-op 不推进』同向；宁可多审一次也不漏审）。
    只看 committed 维度：同路径若仍有未提交 dirty 改动，那份未审工作由调用方经 dirty 候选保留，
    不被本过滤器误剔。

    ## R9 真相源迁移的角色分工（方案 A）与 marker 卸载前置依赖

    committed-round 的 **git 窗口下界种子** 仍由 SHA marker 家族（review 高水位 / round-baseline，
    见 ``committed_round_detection._resolve_committed_round_baseline``）提供——本过滤器只把「已审」
    **裁决权** 交给 ``review_state``，不接管窗口种子。这样保住 orphan 高水位窗口检测（ac042be /
    a296266 教训：孤儿提交靠高水位 SHA 才落在 ``high-water..HEAD`` 窗口内）。

    **R9 终局物理删除 SHA marker 家族前，须先把 ``reviewed_head`` 持久化进 tracker（方案 B 写侧），
    届时 marker 才能卸下「窗口种子」角色。** 在那之前 marker 家族不可删（R9 登记簿：『SHA marker 家族
    reroute 不删码；观察期后用户确认才物理删除』）——本函数头的 ``# OBSERVATION-WINDOW(R9)`` 标注
    与本段即把该前置依赖固化在代码里，避免观察期结束时误删。

    best-effort：tracker 禁用 / DB 不存在 / 任何 sqlite 异常 → 返回空集（不剔除，退化为今日
    SHA-only 窗口行为，绝不误剔除未审工作）。
    """
    paths = [p for p in candidate_paths if p]
    if not paths or not session_id:
        return set()
    if _disabled():
        return set()
    common_dir = git_common_dir(repo)
    if common_dir is None:
        return set()
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    db_path = tracker_dir(base, key) / SQLITE_FILENAME
    if not db_path.exists():
        return set()
    worktree = _worktree_key(str(repo.resolve()))
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in paths)
        rows = conn.execute(
            f"""
            SELECT u.path AS path,
                   u.review_state AS review_state,
                   CASE WHEN EXISTS (
                       SELECT 1
                         FROM session_units su
                        WHERE su.unit_id = u.unit_id
                          AND su.session_id = ?
                          AND su.assignment_kind IN ('owned', 'takeover')
                   ) THEN 1 ELSE 0 END AS owned_by_session
              FROM units u
             WHERE u.worktree_key = ?
               AND u.is_tombstoned = 0
               AND u.observed_state = 'committed'
               AND u.path IN ({placeholders})
            """,
            (session_id, worktree, *paths),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        if conn is not None:
            conn.close()

    # 按路径聚合 per-unit review_state 与本 session 归属：全 reviewed 且本 session 拥有才算「已审」。
    per_path: dict[str, dict[str, bool]] = {}
    for row in rows:
        agg = per_path.setdefault(
            row["path"],
            {"has_committed_unit": False, "all_reviewed": True, "owned_by_session": False},
        )
        agg["has_committed_unit"] = True
        if row["review_state"] != "reviewed":
            agg["all_reviewed"] = False
        if row["owned_by_session"]:
            agg["owned_by_session"] = True
    return {
        path
        for path, agg in per_path.items()
        if agg["has_committed_unit"] and agg["all_reviewed"] and agg["owned_by_session"]
    }


# ``DirtyPathReviewedChangeObservation.unit_kind`` 的哨兵值：该路径在 tracker 里压根没有「本
# session 名下的 dirty+reviewed 单元」，因此**刻意跳过了昂贵的工作树 git 观测**（见
# ``select_dirty_path_reviewed_change_by_session`` 的「先按 DB 收窄」一节）。语义上等价于「一行
# 已审都扣不掉」，与真做了观测却查不到已审单元的路径落在同一个未审桶里；``unit_kind`` 用独立取值
# 而不是伪造 ``path_only``，是为了让诊断读者一眼分清「观测过、没已审」与「按 DB 判定无需观测」。
DIRTY_REVIEWED_OBSERVATION_UNIT_KIND_SKIPPED_NO_REVIEWED_UNITS_IN_TRACKER = (
    "skipped_no_reviewed_units_in_tracker"
)


@dataclass(frozen=True)
class DirtyPathReviewedChangeObservation:
    """单个候选路径上「已被本 session 审过」的那部分**未提交**改动的只读观测。

    ``reviewed_added_lines`` / ``reviewed_deleted_lines`` 是已审 hunk 体内的裸增删行文本（已去掉
    前导 ``+``/``-``），供调用方对全量 diff 做**多重集减法**估算「本轮尚未审的规模」。刻意返回行
    文本而非行数：行数减法无法分辨「同一批行」与「恰好同样多的新行」，多重集减法减不掉就自然计入
    未审，天生偏保守。

    ``unit_kind`` / ``observed_unit_count`` 是纯诊断字段。当 ``unit_kind`` 是
    ``DIRTY_REVIEWED_OBSERVATION_UNIT_KIND_SKIPPED_NO_REVIEWED_UNITS_IN_TRACKER`` 时，本路径没有
    做工作树 git 观测（DB 已证明这里没有可扣的已审单元），``observed_unit_count`` 恒为 0，不代表
    「工作树上没有改动」。
    """

    path: str
    unit_kind: str
    observed_unit_count: int
    reviewed_unit_count: int
    whole_path_identity_reviewed: bool
    reviewed_added_lines: tuple[str, ...]
    reviewed_deleted_lines: tuple[str, ...]
    last_observed_at: str | None


def _reviewed_lines_from_hunk_body(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    """把一个 hunk 体（context + ``+``/``-`` 行，无 ``@@`` 头）拆成裸增行 / 裸删行。

    与 ``stop_decision_candidate_summary._parse_unified_diff_added_deleted`` 的分类口径对齐：
    ``+++``/``---`` 文件头在 hunk 体里不会出现，``\\ No newline`` 以反斜杠开头故不会被误判。
    """
    added: list[str] = []
    deleted: list[str] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            deleted.append(line[1:])
    return added, deleted


def select_dirty_path_reviewed_change_by_session(
    *,
    repo: Path,
    session_id: str | None,
    candidate_paths: Iterable[str],
    log_root_override: Path | None = None,
) -> dict[str, DirtyPathReviewedChangeObservation]:
    """dirty 侧的「已审」只读观测器 —— ``select_committed_paths_reviewed_by_session`` 的对偶。

    committed 侧那位回答「这条路径能不能整条从候选里剔掉」；本函数**不做任何剔除裁决**，只回答
    「这条路径上哪些行是上一轮 review 已经审过的」，供 Stop 决策点横幅把规模数字拆成「已审 / 未审」
    两桶。候选集永远不因本函数的返回值缩小——dirty 侧没有 commit 那种不可变锚点，剔除就是凭空开
    漏审面。

    ## 身份对齐：内容寻址，而不是 hunk header 匹配

    用 ``_classify_path`` + ``_specs_from_observation``（**与 tracker 写侧同一对函数**）从当前工
    作树重新推导 unit_id，再拿 unit_id 对上 ``review_state``。这样：

      * 行号漂移不影响判定——``_canonical_hash_tracked_hunk`` 只哈希 (path, change_type, hunk 体)，
        刻意不含 ``@@`` 头；
      * 工作树在上一轮 review 之后又改过 → 新内容推导出的是**新** unit_id，tracker 里查不到 →
        自动落入「未审」。查不到即未审，是这套设计的 fail-safe 方向，不是缺陷。

    判据（与 committed 侧同口径）：``observed_state='dirty'``、未 tombstone、``review_state
    ='reviewed'``、且经 ``session_units``（``assignment_kind IN ('owned','takeover')``）归属本
    session。任一不满足 → 该 unit 计未审。

    ## 顺序：先按 DB 收窄，再做 git 观测（纯性能，语义不变）

    ``_classify_path`` 每条路径要起多个 git 子进程，而本观测在**每一次 Stop** 的轻量候选摘要里都跑。
    所以先用一条只读 SQL 问出「哪些候选路径名下真有本 session 的 dirty+reviewed 单元」，只对这个通常
    小得多的子集做工作树观测。因 unit_id 由 ``_canonical_hash_*`` 把 ``path`` 一并哈希（单射），按
    path 先筛与按 unit_id 后筛结果完全一致；被跳过的路径仍会带一条 ``reviewed_unit_count=0`` 的观测
    出现在返回值里（``unit_kind`` 为
    ``DIRTY_REVIEWED_OBSERVATION_UNIT_KIND_SKIPPED_NO_REVIEWED_UNITS_IN_TRACKER``），落进与「观测过但
    查不到已审单元」相同的未审桶。**候选集本身永远不因本函数收窄**。

    best-effort：tracker 禁用 / DB 不存在 / 任何 sqlite 或 git 异常 → 返回空 dict（调用方退化为
    「全量都算未审」，即今日行为）。只读，不 refresh、不写。
    """
    paths = [p for p in candidate_paths if p]
    if not paths or not session_id:
        return {}
    if _disabled():
        return {}
    common_dir = git_common_dir(repo)
    if common_dir is None:
        return {}
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    db_path = tracker_dir(base, key) / SQLITE_FILENAME
    if not db_path.exists():
        return {}

    repo_resolved = repo.resolve()
    worktree = _worktree_key(str(repo_resolved))

    # ① 先按 DB 把「需要做 git 观测的路径」收窄到真有已审单元的那些。
    #
    # 这一步纯属性能，不改语义：每一个 unit_id 都由 ``_canonical_hash_*`` 把 ``path`` 连同内容一起
    # 哈希（六个函数各带独立域标签），所以「unit_id → path」是单射；一条候选路径 P 推导出的 unit_id
    # 不可能命中另一条路径 Q 名下的行（除非 sha256 碰撞）。因此「P 名下没有 dirty+reviewed+owned
    # 行」⟺「P 的任何 spec 都不会出现在 ② 的结果里」，先按 path 过滤与后按 unit_id 过滤等价。
    #
    # 值得这么做的原因：``_classify_path`` 每条路径要起 ``git status`` / ``git diff --numstat`` /
    # ``git diff -U3`` 等多个子进程，而本观测在**每一次 Stop** 的轻量候选摘要里都会跑（
    # ``stop_decision_candidate_summary`` 的模块铁律是「只用 git + tracker sqlite，绝不重量级解析」）。
    # 绝大多数 Stop 下「本 session 名下已审的 dirty 单元」远少于候选集，收窄后开销与已审路径数成正比，
    # 而不再与候选集大小成正比。
    reviewed_owned_by_path: dict[str, dict[str, str | None]] = {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        # SQLite 的 bound-parameter 上限在旧版本是 999；分批而不是一次性展开，避免大候选集触顶。
        chunk_size = 400
        for start in range(0, len(paths), chunk_size):
            chunk = paths[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT u.path AS path,
                       u.unit_id AS unit_id,
                       u.last_observed_at AS last_observed_at
                  FROM units u
                 WHERE u.worktree_key = ?
                   AND u.is_tombstoned = 0
                   AND u.observed_state = 'dirty'
                   AND u.review_state = 'reviewed'
                   AND u.path IN ({placeholders})
                   AND EXISTS (
                       SELECT 1
                         FROM session_units su
                        WHERE su.unit_id = u.unit_id
                          AND su.session_id = ?
                          AND su.assignment_kind IN ('owned', 'takeover')
                   )
                """,
                (worktree, *chunk, session_id),
            ).fetchall()
            for row in rows:
                reviewed_owned_by_path.setdefault(row["path"], {})[row["unit_id"]] = row[
                    "last_observed_at"
                ]
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            conn.close()

    # ② 只对收窄后的路径从当前工作树重新观测，推导出与写侧同源的 unit_id。
    # 被跳过的路径**不从结果里消失**：它们照旧带一条 reviewed_unit_count=0 的观测返回，让调用方
    # 落进和「观测过但查不到已审单元」完全相同的未审桶（扣不掉即未审）。
    observed_paths = list(dict.fromkeys(path for path in paths if path in reviewed_owned_by_path))
    observations: dict[str, _PathObservation | None] = {}
    specs_by_path: dict[str, list[_UnitSpec]] = {}
    try:
        for path in observed_paths:
            observation = _classify_path(repo_resolved, path)
            observations[path] = observation
            specs_by_path[path] = _specs_from_observation(observation, path)
    except Exception:  # pragma: no cover - 观测永不阻断调用方
        return {}

    # ③ 把「已审」折回每条路径的行文本。
    result: dict[str, DirtyPathReviewedChangeObservation] = {}
    for path in paths:
        specs = specs_by_path.get(path) or []
        if not specs:
            # DB 已经证明这条路径没有可扣的已审单元 —— 不付 git 观测的代价，直接记为全未审。
            result[path] = DirtyPathReviewedChangeObservation(
                path=path,
                unit_kind=DIRTY_REVIEWED_OBSERVATION_UNIT_KIND_SKIPPED_NO_REVIEWED_UNITS_IN_TRACKER,
                observed_unit_count=0,
                reviewed_unit_count=0,
                whole_path_identity_reviewed=False,
                reviewed_added_lines=(),
                reviewed_deleted_lines=(),
                last_observed_at=None,
            )
            continue
        reviewed_owned = reviewed_owned_by_path.get(path) or {}
        observation = observations.get(path)
        reviewed_added: list[str] = []
        reviewed_deleted: list[str] = []
        whole_path_reviewed = False
        reviewed_count = 0
        last_observed_at: str | None = None
        for unit_id in (spec.unit_id for spec in specs):
            if unit_id in reviewed_owned:
                reviewed_count += 1
                observed_at = reviewed_owned[unit_id]
                if observed_at and (last_observed_at is None or observed_at > last_observed_at):
                    last_observed_at = observed_at
        is_per_hunk = (
            observation is not None
            and observation.kind == "tracked_hunk"
            and observation.change_type != "add"
            and bool(observation.hunks)
        )
        if is_per_hunk:
            assert observation is not None  # narrowed by is_per_hunk
            for hunk in observation.hunks:
                if hunk.canonical_hash not in reviewed_owned:
                    continue
                hunk_added, hunk_deleted = _reviewed_lines_from_hunk_body(hunk.lines)
                reviewed_added.extend(hunk_added)
                reviewed_deleted.extend(hunk_deleted)
        else:
            # 整路径身份单元（untracked / 追踪态新增 / 删除 / 二进制 / path_only）：没有 hunk 锚点，
            # 只能整条判定，不做行级折算。
            whole_path_reviewed = reviewed_count > 0
        result[path] = DirtyPathReviewedChangeObservation(
            path=path,
            unit_kind=(observation.kind if observation is not None else "path_only"),
            observed_unit_count=len(specs),
            reviewed_unit_count=reviewed_count,
            whole_path_identity_reviewed=whole_path_reviewed,
            reviewed_added_lines=tuple(reviewed_added),
            reviewed_deleted_lines=tuple(reviewed_deleted),
            last_observed_at=last_observed_at,
        )
    return result


def summarize_session_unit_review_states(
    *,
    repo: Path,
    session_id: str | None,
    tracker_dir_override: Path | None = None,
    log_root_override: Path | None = None,
) -> dict[str, int]:
    """本 session 名下可审单元按 ``review_state`` 的计数：
    ``{available, assigned, reviewed, total, path_only_reviewed}``。

    存在的理由很具体：``allocate_review_scope`` 报 ``status='empty'`` 时，调用方分不清成因。
    ``_collect_candidate_unit_ids_in_txn`` 只收 ``review_state='available'``，被在途 lease 持有的
    单元是 ``assigned``，**在候选查询阶段就被滤掉了**，根本走不到 Step 5 的 anti-join —— 所以
    ``leased_excluded_count`` 在正常流程下恒为 0，拿它当「是不是并发」的判据永远为假。判断成因必须
    直接看 review_state 分布：

      * ``assigned > 0``      → 另一条 lease 正持有这批单元（并发），多审一遍是安全的；
      * ``reviewed > 0`` 且 ``available == 0`` → 确实全审过了；
      * 全 0                  → 本 session 名下压根没有登记单元（tracker 刚建 / 归属播种失败），
                                此时**不能**声称「已全部审过」，调用方应保守回退。

    ``path_only_reviewed`` 是给上一条「确实全审过了」加的**可信度旁证**，不属于 review_state 分布：
    ``path_only`` 是「看不见改了什么」时的兜底身份（``_canonical_hash_path_only`` 只哈希路径，不含
    mode、不含 change 方向），所以它的 ``reviewed`` 标记只能证明「这个路径被审过一次」，**证明不了
    「当前这次改动被审过」**。典型反例是 chmod 往返：``100644→100755`` 审掉并提交后，相反方向的
    ``100755→100644`` 会推导出同一个 unit_id、命中同一行 reviewed，于是新的可执行位回退被当成旧工作。
    调用方据此判「全已审」而跳过 review 时，必须先看这个计数是不是 0。

    WHERE 条件与 ``_collect_candidate_unit_ids_in_txn`` 逐条同源（含 pending edit_claim 那一支），
    只是把「取 available」换成「按 (review_state, kind) 分组计数」。best-effort：任何异常 → 全 0。
    """
    empty = {"available": 0, "assigned": 0, "reviewed": 0, "total": 0, "path_only_reviewed": 0}
    if not session_id:
        return dict(empty)
    if _disabled():
        return dict(empty)
    if tracker_dir_override is not None:
        db_path = Path(tracker_dir_override) / SQLITE_FILENAME
    else:
        common_dir = git_common_dir(repo)
        if common_dir is None:
            return dict(empty)
        base = log_root_override if log_root_override is not None else log_root()
        db_path = tracker_dir(base, repo_key(common_dir)) / SQLITE_FILENAME
    if not db_path.exists():
        return dict(empty)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT u.review_state AS review_state, u.kind AS kind, COUNT(*) AS n
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
               AND u.is_tombstoned=0
               AND u.observed_state IN ('dirty','committed')
             GROUP BY u.review_state, u.kind
            """,
            (session_id, session_id),
        ).fetchall()
    except sqlite3.Error:
        return dict(empty)
    finally:
        if conn is not None:
            conn.close()
    counts = dict(empty)
    for row in rows:
        state = row["review_state"]
        n = int(row["n"] or 0)
        if state in ("available", "assigned", "reviewed"):
            counts[state] += n
            if state == "reviewed" and row["kind"] == "path_only":
                counts["path_only_reviewed"] += n
    counts["total"] = counts["available"] + counts["assigned"] + counts["reviewed"]
    return counts


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
    committed_base_ref: str | None = None,
    union_with_prior_scope_unit_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Producer half of the global reviewed-diff tracker.

    On `status='allocated'` writes `tracker-scope.json` to `output_scope_path`
    when supplied; the JSON shape passes `prepare_review_run.load_tracker_scope`
    unchanged. On `status='empty'` writes nothing — the dispatcher / Stop hook
    converts that into the `no_unassigned_review_scope` skip payload.

    `dry_run=True` runs the same observation + candidate query but stops short
    of inserting a lease — used by the dispatcher to decide whether the
    installed Stop hook should fire.

    ``union_with_prior_scope_unit_ids`` 是**lease 过期后重新分配**的并集入口（「并集全审」）。
    一轮 review 的 lease 过期后，若只按当时工作树重新分配，原本圈定的那批改动会被整个踢出
    packet——实测一次 committed-round 派发就是这样在中断+rewind 后被换成了纯 dirty 口径，最终
    审的 100% 是另一批东西。传入上一轮 scope 的 ``unit_ids`` 后，其中**仍可审**（present、
    未 tombstone、未被别的 active lease 持有、``review_state='available'``）的部分会并回本轮
    候选集，使新 scope ⊇ 原 scope ∩ 仍存在者。已被真正审掉（``reviewed``）的不会复活。

    ⚠️ 并集必须**连口径一起**继承：若原 scope 含 committed-round 的 unit，重新分配时还要把原来的
    ``committed_baseline`` / ``committed_base_ref`` 一并传回，否则观察阶段会判定它们「已不在场」
    而 superseded，并集查询自然就找不到了。CLI 的 ``--union-with-prior-scope`` 会从上一轮 scope
    artifact 里自动继承这两个字段（它们由本函数写进 artifact），模块调用方需自行传。
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
                committed_base_ref=committed_base_ref,
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
            # Step 4b（并集全审）：把上一轮 scope 里仍可审的 unit 并回候选集。必须排在 Step 5
            # 的 active-lease 反连接**之前**，让并回来的 unit 与本轮候选走同一道排他检查。
            (
                candidates,
                unioned_prior_scope_unit_ids_before_active_lease_exclusion,
            ) = _union_prior_scope_unit_ids_in_txn(
                conn,
                candidates=candidates,
                prior_scope_unit_ids=union_with_prior_scope_unit_ids,
            )
            candidate_unit_count = len(candidates)

            # Step 5: exclude active-leased units (anti-join).
            surviving, leased_excluded_count = _exclude_active_leased_in_txn(conn, candidates, now_iso)
            # Step 5b: narrow the reported union list to what actually entered this
            # round's scope. Step 4b deliberately runs *before* the anti-join so the
            # unioned units face the same exclusive check as freshly observed ones —
            # which means a prior unit still held by another active lease is dropped
            # right here. Reporting the pre-anti-join list would (a) tell the packet
            # and the audit event that units are being carried over when the reviewer
            # never received them, and (b) let the event's
            # `newly_observed_unit_count` go negative once the anti-join removes more
            # unioned units than this round observed fresh.
            surviving_unit_id_set = set(surviving)
            unioned_prior_scope_unit_ids = [
                unit_id
                for unit_id in unioned_prior_scope_unit_ids_before_active_lease_exclusion
                if unit_id in surviving_unit_id_set
            ]
            unioned_prior_scope_unit_id_set = set(unioned_prior_scope_unit_ids)

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
                    # 本轮的 committed-round 口径。写进 scope artifact 让**下一次重新分配**能
                    # 原样继承——只按当时工作树重分配正是「口径被悄悄换掉」的成因。
                    "committed_baseline": committed_baseline,
                    "committed_base_ref": committed_base_ref,
                    # 并集全审：这批 unit 来自上一轮 scope，不是本轮新观察到的。reviewer 与
                    # handoff 据此区分「已定稿的那一段」与「刚写的那一段」。
                    "unioned_prior_scope_unit_ids": unioned_prior_scope_unit_ids,
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
                    "unioned_prior_scope_unit_count": len(unioned_prior_scope_unit_ids),
                    "reason_code": REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
                    "reason_code_legacy_alias": LEGACY_REASON_SESSION_OWNED_DIRTY,
                },
            )
            if unioned_prior_scope_unit_ids:
                _emit_event(
                    events_path,
                    {
                        "event": "scope_reallocated_after_lease_expiry",
                        "rvf_state_phase": "review",
                        "session_id": session_id,
                        "run_id": run_id,
                        "lease_id": committed_payload["lease_id"],
                        "scope_hash": committed_payload["scope_hash"],
                        "unioned_prior_scope_unit_ids": unioned_prior_scope_unit_ids,
                        "unioned_prior_scope_unit_count": len(unioned_prior_scope_unit_ids),
                        # Counted as a set difference rather than `len(scope) -
                        # len(unioned)`: the subtraction is only non-negative while the
                        # two lists stay in the containment relation Step 5b
                        # establishes, and a silently negative audit number is worse
                        # than a slightly slower count.
                        "newly_observed_unit_count": sum(
                            1
                            for unit_id in committed_payload["unit_ids"]
                            if unit_id not in unioned_prior_scope_unit_id_set
                        ),
                        "committed_baseline": committed_baseline,
                        "reason_code": "scope_reallocated_after_lease_expiry",
                    },
                )
            return {
                "status": "allocated",
                "unioned_prior_scope_unit_ids": unioned_prior_scope_unit_ids,
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
    RVF_ATTEMPT_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG,
}
RVF_ISSUE_STATES = {
    "open",
    "fixed",
    "false_positive",
    "elevated",
    "failed",
    "superseded",
    RVF_ISSUE_STATE_SUBMITTED_TO_KANBAN_BACKLOG,
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
    """``source_refs=None`` 表示「不改来源」：已有记录保留原 ``source_refs`` 列，新记录写 ``[]``。

    来源只由合并处置派生（``rvf_fix_issue.upsert_canonical_issue``）；只同步状态的调用方（fix attempt
    stop / apply 读的是已剥掉来源字段的镜像）必须传 None，否则会把来源清空。"""
    if state not in RVF_ISSUE_STATES:
        raise ValueError("invalid RVF issue state")
    _repo, key, common_dir, directory, db_path, events_path = _rvf_store(repo, log_root_override)
    now_iso = now or utc_now()
    issue_key = _rvf_issue_key(run_id, issue_id)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_conn(db_path)
        with _begin_immediate(conn):
            existing = conn.execute(
                "SELECT created_at, source_refs FROM rvf_issues WHERE issue_key=?",
                (issue_key,),
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now_iso
            if source_refs is not None:
                stored_source_refs = _json_dumps(source_refs)
            elif existing is not None:
                stored_source_refs = existing["source_refs"]
            else:
                stored_source_refs = _json_dumps([])
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
                    stored_source_refs,
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
            if status in {
                "fixed", "false_positive", "elevated", "failed",
                RVF_ATTEMPT_STATUS_VALIDATED_AND_SUBMITTED_TO_KANBAN_BACKLOG,
            } and stopped_at is None:
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
        "--union-with-prior-scope",
        default=None,
        help=(
            "上一轮 tracker-scope.json 的路径（lease 过期后重新分配时使用）。并集全审："
            "其中仍可审的 unit 会并回本轮 scope，避免原本圈定的那批改动被整个换掉；"
            "该文件里的 committed_baseline / committed_base_ref 也会在未显式指定时被继承。"
        ),
    )
    alloc.add_argument(
        "--committed-baseline",
        default=None,
        help="committed-round 观察基线（<baseline>..HEAD）。省略时可由 --union-with-prior-scope 继承。",
    )
    alloc.add_argument(
        "--committed-base-ref",
        default=None,
        help="committed-round 的 base ref（排除可达 base 的提交）。省略时可由 --union-with-prior-scope 继承。",
    )
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
    prior_scope_unit_ids: list[str] | None = None
    committed_baseline = args.committed_baseline
    committed_base_ref = args.committed_base_ref
    if args.union_with_prior_scope:
        prior_path = Path(args.union_with_prior_scope).expanduser()
        try:
            prior_payload = json.loads(prior_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": f"--union-with-prior-scope 不可读: {prior_path}: {exc}",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        if not isinstance(prior_payload, dict):
            print(
                json.dumps(
                    {"status": "error", "error": "--union-with-prior-scope 必须是 JSON 对象"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        raw_unit_ids = prior_payload.get("unit_ids")
        prior_scope_unit_ids = (
            [uid for uid in raw_unit_ids if isinstance(uid, str) and uid]
            if isinstance(raw_unit_ids, list)
            else []
        )
        # 口径继承：显式参数优先，缺省时沿用上一轮的 committed-round 基线——只按当时工作树
        # 重分配正是「committed-round 被悄悄换成纯 dirty」的成因。
        if not committed_baseline and isinstance(prior_payload.get("committed_baseline"), str):
            committed_baseline = prior_payload["committed_baseline"]
        if not committed_base_ref and isinstance(prior_payload.get("committed_base_ref"), str):
            committed_base_ref = prior_payload["committed_base_ref"]
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
        committed_baseline=committed_baseline,
        committed_base_ref=committed_base_ref,
        union_with_prior_scope_unit_ids=prior_scope_unit_ids,
    )
    if args.print_result:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
