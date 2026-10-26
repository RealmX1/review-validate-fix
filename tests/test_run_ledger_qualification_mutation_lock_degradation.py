"""资格锁真实竞争时，附属日志降级不得改变命令或 hook 的结果。"""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.run_ledger import run_ledger
from core.review_run_qualification_mutation_lock import (
    QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH,
    review_run_qualification_mutation_lock,
)


SCRIPT_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
)


@contextmanager
def external_qualification_lock_owner(run_directory: Path):
    lock_path = run_directory / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import fcntl,json,os,sys; "
        "handle=open(sys.argv[1],'a'); "
        "fcntl.flock(handle,fcntl.LOCK_EX); "
        "print(json.dumps({'locked_inode':os.fstat(handle.fileno()).st_ino}),flush=True); "
        "sys.stdin.readline()"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([owner.stdout], [], [], 10)[0], "独立进程未能取得资格锁"
        ownership = json.loads(owner.stdout.readline())
        assert ownership["locked_inode"] == lock_path.stat().st_ino
        yield lock_path
        assert owner.poll() is None, "执行期间资格锁持有者必须存活"
        assert ownership["locked_inode"] == lock_path.stat().st_ino
    finally:
        try:
            _, stderr = owner.communicate(input="\n", timeout=5)
        except subprocess.TimeoutExpired:
            owner.kill()
            _, stderr = owner.communicate(timeout=5)
        assert owner.returncode == 0, stderr


def persistent_log_file_contents(log_root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(log_root)): path.read_bytes()
        for path in log_root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> run_ledger.RunLedger:
    log_root = tmp_path / "isolated-run-ledger-log-root"
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    monkeypatch.setattr(
        run_ledger,
        "review_run_qualification_mutation_lock",
        partial(review_run_qualification_mutation_lock, timeout_seconds=0.03),
    )
    return run_ledger.RunLedger(
        component="command-lock",
        run_id="rvf-qualification-lock-degradation-test",
        run_dir=log_root / "runs/rvf-qualification-lock-degradation-test",
    )


@pytest.mark.parametrize("operation", ["artifact", "summary"])
@pytest.mark.parametrize("existing_content", [False, True])
def test_busy_qualification_lock_degrades_without_mutation_then_recovers(
    isolated_ledger: run_ledger.RunLedger,
    operation: str,
    existing_content: bool,
    capfd: pytest.CaptureFixture[str],
) -> None:
    ledger = isolated_ledger
    if existing_content:
        ledger.artifact("qualification-controlled.json", {"state": "原有产物"})
        ledger.summary(status="started", reason_code="existing_summary")
    with external_qualification_lock_owner(ledger.run_dir) as lock_path:
        before = persistent_log_file_contents(ledger.root)
        if operation == "artifact":
            result = ledger.artifact("qualification-controlled.json", {"state": "更新产物"})
            assert result is None
        else:
            result = ledger.summary(status="completed", reason_code="command_completed", returncode=0)
            assert result["status"] == "completed"
            assert result["reason_code"] == "command_completed"
            assert result["returncode"] == 0
            assert result["log_unavailable"] is True
            assert result["diagnostics"] is ledger.diagnostics
            assert ledger.last_summary is result
        assert persistent_log_file_contents(ledger.root) == before
        assert ledger.available is False
        assert ledger.diagnostics[-1]["kind"] == "log_unavailable"
        assert ledger.diagnostics[-1]["operation"].startswith(operation)
        assert "ReviewRunQualificationMutationLockBusy" in ledger.diagnostics[-1]["error"]
        assert "未越过锁，释放后重试" in ledger.diagnostics[-1]["error"]
        waiting_notice = capfd.readouterr().err
        assert "等待 RVF run 资格锁" in waiting_notice
        assert str(lock_path) in waiting_notice
    if operation == "artifact":
        path = ledger.artifact("qualification-controlled.json", {"state": "已恢复"})
        assert json.loads(Path(path).read_text())["state"] == "已恢复"
    else:
        ledger.summary(status="completed", reason_code="qualification_lock_recovered")
        assert json.loads(ledger.summary_path.read_text())["reason_code"] == "qualification_lock_recovered"


def test_hook_log_degradation_remains_developer_only(isolated_ledger: run_ledger.RunLedger) -> None:
    ledger = isolated_ledger
    with external_qualification_lock_owner(ledger.run_dir):
        before = persistent_log_file_contents(ledger.root)
        payload = ledger.hook_payload(status="completed", reason_code="nothing_to_review")
        assert persistent_log_file_contents(ledger.root) == before
    assert set(payload) == {"continue", "systemMessage"}
    assert payload["continue"] is True
    assert "log_unavailable=true" in payload["systemMessage"]
    assert ledger.diagnostics[-1]["operation"] == "summary"


@pytest.mark.parametrize("command_returncode", [0, 7])
def test_command_lock_preserves_real_command_result_when_final_summary_cannot_lock(
    tmp_path: Path,
    isolated_ledger: run_ledger.RunLedger,
    command_returncode: int,
) -> None:
    ledger = isolated_ledger
    repo = tmp_path / "isolated-command-working-directory"
    repo.mkdir()
    executed_marker = tmp_path / "real-command-executed.txt"
    bootstrap = (
        f"import sys; sys.path.insert(0,{str(SCRIPT_DIRECTORY)!r}); "
        "import _rvf_pyroot; from functools import partial; "
        "from core.run_ledger import run_ledger; "
        "from core.review_run_qualification_mutation_lock import review_run_qualification_mutation_lock; "
        "run_ledger.review_run_qualification_mutation_lock="
        "partial(review_run_qualification_mutation_lock,timeout_seconds=0.03); "
        "import command_lock; raise SystemExit(command_lock.main())"
    )
    command = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text('真实命令已执行\\n',encoding='utf-8'); "
        "raise SystemExit(int(sys.argv[2]))"
    )
    with external_qualification_lock_owner(ledger.run_dir):
        completed = subprocess.run(
            [
                sys.executable, "-c", bootstrap,
                "--repo", str(repo), "--name", "qualification-log-degradation",
                "--lock-dir", str(tmp_path / "isolated-command-locks"),
                "--rvf-run-id", ledger.run_id, "--rvf-run-dir", str(ledger.run_dir),
                "--", sys.executable, "-c", command, str(executed_marker), str(command_returncode),
            ],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == command_returncode, completed.stdout + completed.stderr
        assert executed_marker.read_text() == "真实命令已执行\n"
        assert not ledger.summary_path.exists()
        assert not (ledger.root / "latest.json").exists()
        assert "等待 RVF run 资格锁" in completed.stderr
        assert "Traceback" not in completed.stderr
    events = [json.loads(line) for line in ledger.events_path.read_text().splitlines()]
    assert [entry["event"] for entry in events] == ["lock_wait_started", "lock_acquired", "lock_released"]
    assert events[-1]["returncode"] == command_returncode


@pytest.mark.parametrize("operation", ["artifact", "summary"])
def test_non_busy_qualification_lock_validation_errors_are_not_swallowed(
    tmp_path: Path, isolated_ledger: run_ledger.RunLedger, operation: str
) -> None:
    ledger = isolated_ledger
    symlink_destination = tmp_path / "symlink-lock-directory-target"
    symlink_destination.mkdir()
    (ledger.run_dir / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH).parent.symlink_to(
        symlink_destination, target_is_directory=True
    )
    with pytest.raises(ValueError, match="资格锁目录不可使用软链接"):
        if operation == "artifact":
            ledger.artifact("qualification-controlled.json", {})
        else:
            ledger.summary(status="completed", reason_code="command_completed")
    assert ledger.diagnostics == []
    assert list(symlink_destination.iterdir()) == []
