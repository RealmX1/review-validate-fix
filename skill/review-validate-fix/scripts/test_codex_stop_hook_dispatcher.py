#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("codex_stop_hook_dispatcher.py")


def load_dispatcher_module():
    spec = importlib.util.spec_from_file_location("rvf_stop_hook_dispatcher_for_tests", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(cmd: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], cwd=path)
    return path


def write_fake_dev_scripts(repo: Path, marker: Path, *, fail_sync: bool = False) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    sync_body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker / 'sync-ran')!r}).write_text('sync\\n', encoding='utf-8')\n"
    )
    if fail_sync:
        sync_body += "sys.exit(7)\n"
    (scripts / "sync_plugin_payload.py").write_text(sync_body, encoding="utf-8")
    (scripts / "install_to_codex.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        f"pathlib.Path({str(marker / 'install-ran')!r}).write_text('install\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )


def write_env_check_dev_scripts(repo: Path, marker: Path) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "if any(key.startswith('CODEX_RVF_') for key in os.environ):\n"
        "    sys.exit(9)\n"
        f"pathlib.Path({str(marker)!r}).write_text('clean\\n', encoding='utf-8')\n"
    )
    (scripts / "sync_plugin_payload.py").write_text(body, encoding="utf-8")
    (scripts / "install_to_codex.py").write_text(body, encoding="utf-8")


def write_fake_installed_hook(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "raw = sys.stdin.read()\n"
        f"pathlib.Path({str(marker / 'hook-input.json')!r}).write_text(raw, encoding='utf-8')\n"
        "print(json.dumps({'continue': True, 'systemMessage': 'real hook ran'}))\n",
        encoding="utf-8",
    )


def invoke(
    event: dict[str, object],
    *,
    dev_repo: Path | None,
    hook: Path,
    state: Path,
    extra_env: dict[str, str] | None = None,
) -> str:
    env = os.environ.copy()
    for key in (
        "CODEX_RVF_DEV_REPO",
        "CODEX_RVF_INSTALLED_STOP_HOOK",
        "CODEX_RVF_DEV_SYNC_STATE_DIR",
        "CODEX_RVF_DEV_SYNC",
    ):
        env.pop(key, None)
    if dev_repo is not None:
        env["CODEX_RVF_DEV_REPO"] = str(dev_repo)
    env["CODEX_RVF_INSTALLED_STOP_HOOK"] = str(hook)
    env["CODEX_RVF_DEV_SYNC_STATE_DIR"] = str(state)
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_dev_repo_main_session_syncs_before_running_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_non_matching_repo_runs_installed_hook_without_sync(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    other = init_repo(tmp_path / "other")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    stdout = invoke(
        {"cwd": str(other), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert (marker / "hook-input.json").exists()


def test_subagent_stop_runs_installed_hook_without_sync(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = tmp_path / "subagent.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stdout = invoke(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()


def test_sync_failure_skips_installed_hook_to_avoid_stale_fork(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker, fail_sync=True)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    payload = json.loads(stdout)
    assert "dev sync" in payload["systemMessage"]
    assert "失败" in payload["systemMessage"]
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    assert list(state.glob("*.rvf-dev-sync.json"))


def test_coerce_text_handles_timeout_bytes() -> None:
    module = load_dispatcher_module()
    assert module.coerce_text(b"\xffstdout") == "�stdout"
    assert module.coerce_text(None) == ""


def test_sync_subprocesses_do_not_inherit_rvf_runtime_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "clean-env"
    hook_marker = tmp_path / "hook"
    hook_marker.mkdir()
    write_env_check_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "codex_stop_review_validate_fix.py"
    write_fake_installed_hook(hook, hook_marker)

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"CODEX_RVF_FORK_MODE": "dry-run", "CODEX_RVF_STATE_DIR": "/tmp/rvf-test"},
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert marker.exists()


def main() -> int:
    tests = [
        test_dev_repo_main_session_syncs_before_running_installed_hook,
        test_non_matching_repo_runs_installed_hook_without_sync,
        test_subagent_stop_runs_installed_hook_without_sync,
        test_sync_failure_skips_installed_hook_to_avoid_stale_fork,
        test_coerce_text_handles_timeout_bytes,
        test_sync_subprocesses_do_not_inherit_rvf_runtime_env,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for test in tests:
            if test is test_coerce_text_handles_timeout_bytes:
                test()
            else:
                test(root / test.__name__)
    print("codex stop hook dispatcher tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
