from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/rvf_local_deploy.py"
SKILL = ROOT / "plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True
    ).strip()


def _fake_canonical_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "canonical"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "rvf@example.test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "RVF Test"], check=True)

    _write(
        repo / "scripts/check_plugin_contracts.py",
        """
        import os
        import sys
        from pathlib import Path

        sys.stdin.read()
        with Path(os.environ["RVF_LOCAL_DEPLOY_TEST_TRACE"]).open("a", encoding="utf-8") as handle:
            handle.write("contract\\n")
        """,
    )
    _write(
        repo / "scripts/install_to_codex.py",
        """
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path

        assert "--configure-stop-hook" in sys.argv
        assert "--configure-user-prompt-submit-hook" in sys.argv
        assert "--skip-claude-plugin" in sys.argv
        trace = Path(os.environ["RVF_LOCAL_DEPLOY_TEST_TRACE"])
        with trace.open("a", encoding="utf-8") as handle:
            handle.write("install\\n")
        repo = Path.cwd()
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        home = Path(os.environ["HOME"])
        roots = (
            home / "plugins/review-validate-fix",
            home / ".codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0",
        )
        payload = {
            "source": {"head": head},
            "options": {
                "configure_stop_hook": True,
                "configure_user_prompt_submit_hook": True,
            },
        }
        for root in roots:
            path = root / "skills/review-validate-fix/state/deployments/latest-deployment.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        print("fake installer OK")
        """,
    )
    _write(
        repo
        / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py",
        """
        import json
        import os
        from pathlib import Path

        with Path(os.environ["RVF_LOCAL_DEPLOY_TEST_TRACE"]).open("a", encoding="utf-8") as handle:
            handle.write("verify\\n")
        print(json.dumps({"status": "ok", "deployment_delta": {"forward_commits": ["new"]}}))
        """,
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo


def _installed_log(home: Path, head: str) -> None:
    path = (
        home
        / "plugins/review-validate-fix"
        / "skills/review-validate-fix/state/deployments/latest-deployment.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": {"head": head}}), encoding="utf-8")


def _run(
    repo: Path,
    home: Path,
    trace: Path,
    *,
    keep_stdin_open: bool = False,
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(RUNNER),
        "--repo",
        str(repo),
        "--home",
        str(home),
        "--skip-claude-plugin",
        "--json",
    ]
    run_kwargs = {
        "cwd": ROOT,
        "env": {**os.environ, "RVF_LOCAL_DEPLOY_TEST_TRACE": str(trace)},
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": 10,
    }
    if not keep_stdin_open:
        return subprocess.run(argv, **run_kwargs)
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "rb") as read_handle:
            return subprocess.run(argv, stdin=read_handle, **run_kwargs)
    finally:
        os.close(write_fd)


def test_stale_installed_skill_delegates_to_canonical_runner(tmp_path: Path) -> None:
    repo = _fake_canonical_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "trace.txt"
    _installed_log(home, "old-installed-head")

    completed = _run(repo, home, trace, keep_stdin_open=True)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    assert result["canonical_head"] == _git(repo, "rev-parse", "HEAD")
    assert result["installed_before"]["plugin"] == "old-installed-head"
    assert result["installed_after"]["plugin"] == result["canonical_head"]
    assert result["stale_before"] is True
    assert trace.read_text(encoding="utf-8").splitlines() == [
        "contract",
        "install",
        "verify",
    ]


def test_dirty_canonical_repo_fails_before_deploy(tmp_path: Path) -> None:
    repo = _fake_canonical_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "trace.txt"
    with (repo / "scripts/install_to_codex.py").open("a", encoding="utf-8") as handle:
        handle.write("# dirty\n")

    completed = _run(repo, home, trace)

    assert completed.returncode == 2
    assert "canonical deploy scope 含未提交改动" in completed.stderr
    assert not trace.exists()


def test_missing_canonical_repo_fails_before_creating_lock(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "trace.txt"

    completed = _run(tmp_path / "missing", home, trace)

    assert completed.returncode == 2
    assert "canonical repo 不存在" in completed.stderr
    assert not (home / ".rvf/local-deploy.lock").exists()
    assert not trace.exists()


def test_concurrent_deploy_writer_is_rejected(tmp_path: Path) -> None:
    repo = _fake_canonical_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "trace.txt"
    lock_path = home / ".rvf/local-deploy.lock"
    lock_path.parent.mkdir(parents=True)

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = _run(repo, home, trace)

    assert completed.returncode == 3
    assert "已有 RVF local deploy 正在运行" in completed.stderr
    assert not trace.exists()


def test_skill_is_a_stable_canonical_trampoline() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert (
        "python3 /Users/bominzhang/Documents/GitHub/review-validate-fix/"
        "scripts/rvf_local_deploy.py --json"
    ) in text
    assert "scripts/install_to_codex.py" not in text
    assert "verify_local_deploy.py" not in text
