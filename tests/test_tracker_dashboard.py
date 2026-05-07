#!/usr/bin/env python3
"""Tests for scripts/tracker_dashboard.py."""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
    )


def _init_repo_with_dirty_file(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")
    (path / "file.txt").write_text("init\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    (path / "file.txt").write_text("init\nchanged-line\n")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, str, dict[str, str]]:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, body, dict(resp.headers)


def test_collect_snapshot_empty_repo(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    repo = _init_repo_with_dirty_file(tmp_path / "repo")
    log_root = tmp_path / "log_root"
    snap = dashboard.collect_snapshot(repo, log_root_override=log_root, events_limit=10)
    assert snap["repo"]["repo_path"] == str(repo.resolve())
    assert snap["repo"]["db_exists"] is False
    assert snap["units"] == []
    assert snap["leases"] == []
    assert snap["events"] == []
    assert snap["counters"]["units_total"] == 0


def test_collect_snapshot_after_allocate_release(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    diff_tracker = _load("diff_tracker")
    repo = _init_repo_with_dirty_file(tmp_path / "repo")
    log_root = tmp_path / "log_root"
    alloc = diff_tracker.allocate_review_scope(
        repo=repo,
        session_id="sess-A",
        run_id="run-1",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
    )
    assert alloc["status"] == "allocated"
    diff_tracker.lease_release(
        repo=repo,
        lease_id=alloc["lease_id"],
        reason="completed",
        log_root_override=log_root,
    )
    snap = dashboard.collect_snapshot(repo, log_root_override=log_root, events_limit=20)
    assert snap["repo"]["db_exists"] is True
    assert snap["counters"]["units_total"] >= 1
    assert snap["counters"]["tombstone_state"]["active"] >= 1
    assert snap["counters"]["leases_total"] >= 1
    assert snap["counters"]["sessions_total"] >= 1
    assert any(ev["event"] == "allocate_review_scope" for ev in snap["events"])
    assert any(
        ev["event"] in {"lease_released", "review_scope_completed"}
        for ev in snap["events"]
    )


def test_render_shell_embeds_repo_metadata(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    html = dashboard.render_shell("my-repo-key", "/abs/path/to/repo", poll_seconds=3)
    assert "my-repo-key" in html
    assert "/abs/path/to/repo" in html
    assert "POLL_MS = 3000" in html
    assert "freeze-btn" in html
    assert "download-btn" in html
    assert "/api/snapshot" in html


def test_cli_snapshot_json_writes_file(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    repo = _init_repo_with_dirty_file(tmp_path / "repo")
    log_root = tmp_path / "log_root"
    out = tmp_path / "snap.json"
    rc = dashboard._main([
        "--repo", str(repo),
        "--snapshot-json", str(out),
        "--log-root", str(log_root),
        "--limit-events", "5",
    ])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["repo"]["repo_path"] == str(repo.resolve())


def test_cli_requires_repo_or_snapshot(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    rc = dashboard._main([])
    assert rc == 2


def test_cli_snapshot_json_requires_repo(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    out = tmp_path / "snap.json"
    rc = dashboard._main(["--snapshot-json", str(out)])
    assert rc == 2


def test_cli_from_snapshot_missing_file(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    rc = dashboard._main(["--from-snapshot", str(tmp_path / "nope.json")])
    assert rc == 2


def test_server_live_serves_shell_and_api(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    diff_tracker = _load("diff_tracker")
    repo = _init_repo_with_dirty_file(tmp_path / "repo")
    log_root = tmp_path / "log_root"
    alloc = diff_tracker.allocate_review_scope(
        repo=repo,
        session_id="sess-server",
        run_id="run-1",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
    )
    assert alloc["status"] == "allocated"
    port = _free_port()
    loader = dashboard._live_loader(
        repo,
        log_root_override=log_root,
        events_limit=20,
        include_tombstones=False,
    )
    server = dashboard.serve(
        snapshot_loader=loader,
        repo_label=str(repo),
        repo_key_label="test-key",
        host="127.0.0.1",
        port=port,
        poll_seconds=2,
        is_frozen=False,
        open_browser=False,
    )
    try:
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        status, body, headers = _http_get(f"http://127.0.0.1:{port}/")
        assert status == 200
        assert "<!DOCTYPE html>" in body
        assert "test-key" in body
        assert headers.get("Content-Type", "").startswith("text/html")
        status, body, headers = _http_get(f"http://127.0.0.1:{port}/api/snapshot")
        assert status == 200
        snap = json.loads(body)
        assert snap["_dashboard_mode"] == "live"
        assert snap["counters"]["units_total"] >= 1
        status, body, _ = _http_get(f"http://127.0.0.1:{port}/api/missing", timeout=2.0) if False else (404, "", {})
    finally:
        server.shutdown()
        server.server_close()


def test_server_frozen_serves_snapshot_file(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    diff_tracker = _load("diff_tracker")
    repo = _init_repo_with_dirty_file(tmp_path / "repo")
    log_root = tmp_path / "log_root"
    alloc = diff_tracker.allocate_review_scope(
        repo=repo,
        session_id="sess-frozen",
        run_id="run-1",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
    )
    assert alloc["status"] == "allocated"
    snap_file = tmp_path / "snap.json"
    rc = dashboard._main([
        "--repo", str(repo),
        "--snapshot-json", str(snap_file),
        "--log-root", str(log_root),
    ])
    assert rc == 0
    saved = json.loads(snap_file.read_text(encoding="utf-8"))
    saved_repo_key = saved["repo"]["repo_key"]
    port = _free_port()
    loader = dashboard._frozen_loader(snap_file)
    server = dashboard.serve(
        snapshot_loader=loader,
        repo_label=str(snap_file),
        repo_key_label=saved_repo_key,
        host="127.0.0.1",
        port=port,
        poll_seconds=2,
        is_frozen=True,
        open_browser=False,
    )
    try:
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        status, body, _ = _http_get(f"http://127.0.0.1:{port}/api/snapshot")
        assert status == 200
        served = json.loads(body)
        assert served["_dashboard_mode"] == "frozen"
        assert served["repo"]["repo_key"] == saved_repo_key
        assert served["counters"]["units_total"] == saved["counters"]["units_total"]
    finally:
        server.shutdown()
        server.server_close()


def test_server_returns_404_for_unknown_path(tmp_path: Path) -> None:
    dashboard = _load("tracker_dashboard")
    repo = _init_repo_with_dirty_file(tmp_path / "repo")
    log_root = tmp_path / "log_root"
    port = _free_port()
    loader = dashboard._live_loader(
        repo,
        log_root_override=log_root,
        events_limit=5,
        include_tombstones=False,
    )
    server = dashboard.serve(
        snapshot_loader=loader,
        repo_label=str(repo),
        repo_key_label="test-key",
        host="127.0.0.1",
        port=port,
        poll_seconds=2,
        is_frozen=False,
        open_browser=False,
    )
    try:
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2.0)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("expected 404")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    import tempfile
    cases = [
        test_collect_snapshot_empty_repo,
        test_collect_snapshot_after_allocate_release,
        test_render_shell_embeds_repo_metadata,
        test_cli_snapshot_json_writes_file,
        test_cli_requires_repo_or_snapshot,
        test_cli_snapshot_json_requires_repo,
        test_cli_from_snapshot_missing_file,
        test_server_live_serves_shell_and_api,
        test_server_frozen_serves_snapshot_file,
        test_server_returns_404_for_unknown_path,
    ]
    with tempfile.TemporaryDirectory(prefix="rvf-dashboard-tests-") as tmp:
        root = Path(tmp)
        for case in cases:
            case_root = root / case.__name__
            case_root.mkdir(parents=True, exist_ok=True)
            case(case_root)
    print("tracker_dashboard tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
