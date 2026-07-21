from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_RESULT_CACHE = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
    / "validation_result_cache.py"
)


def clean_test_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    environment.update(extra or {})
    return environment


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=clean_test_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def initialized_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "RVF Validation Test")
    git(path, "config", "user.email", "rvf-validation@example.test")
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", "initial")
    return path


def cache_command(
    repo: Path,
    user_data_root: Path,
    counter_path: Path,
    *,
    extra_command_argument: str = "first",
    environment_value: str = "alpha",
) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    environment = clean_test_environment(
        {
            "RVF_USER_LOCAL_DATA_ROOT": str(user_data_root),
            "RVF_TEST_VALIDATION_FLAVOR": environment_value,
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATION_RESULT_CACHE),
            "run",
            "--repo",
            str(repo),
            "--name",
            "contract-suite",
            "--environment-key",
            "RVF_TEST_VALIDATION_FLAVOR",
            "--",
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys; "
                "p=Path(sys.argv[1]); p.write_text((p.read_text() if p.exists() else '')+'run\\n'); "
                "print(sys.argv[2])"
            ),
            str(counter_path),
            extra_command_argument,
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(completed.stdout) if completed.stdout.strip().startswith("{") else None
    return completed, payload


def test_content_manifest_is_stable_across_commit_with_delete_and_rename(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "RVF Validation Test")
    git(repo, "config", "user.email", "rvf-validation@example.test")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "run.sh").chmod(0o755)
    (repo / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    (repo / "renamed-old.txt").write_text("rename me\n", encoding="utf-8")
    (repo / "link-to-keep").symlink_to("keep.txt")
    git(repo, "add", ".gitignore", "keep.txt", "run.sh", "deleted.txt", "renamed-old.txt", "link-to-keep")
    git(repo, "commit", "-q", "-m", "manifest fixture")
    git(repo, "rm", "-q", "deleted.txt")
    git(repo, "mv", "renamed-old.txt", "renamed-new.txt")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    before = subprocess.run(
        [sys.executable, str(VALIDATION_RESULT_CACHE), "manifest", "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    before_payload = json.loads(before.stdout)
    assert before_payload["candidate_content_manifest_algorithm"] == "git-ls-files-content-manifest-v1"
    assert before_payload["candidate_content_manifest_sha256"] == (
        "dd775ca24e4d996204c928574bb3251f7a83baefde8cc6fac44a145955de1bcc"
    )

    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "commit effective filesystem")
    after = subprocess.run(
        [sys.executable, str(VALIDATION_RESULT_CACHE), "manifest", "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(after.stdout) == before_payload


def test_content_manifest_encodes_gitlink_oid_without_reading_submodule_worktree(
    tmp_path: Path,
) -> None:
    repo = initialized_repository(tmp_path / "repo")
    git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{'1' * 40},vendor/dependency",
    )
    first = subprocess.run(
        [sys.executable, str(VALIDATION_RESULT_CACHE), "manifest", "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    first_payload = json.loads(first.stdout)
    assert first_payload["candidate_content_manifest_sha256"] == (
        "58c02eba6750be257982e8b4bb6b74814bfd02bf6f143800f8f53b278e494bea"
    )
    git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{'2' * 40},vendor/dependency",
    )
    second = subprocess.run(
        [sys.executable, str(VALIDATION_RESULT_CACHE), "manifest", "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(second.stdout) != first_payload


def test_cache_hits_and_invalidates_on_content_command_and_environment(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    counter = tmp_path / "counter.txt"
    user_data = tmp_path / "user-data"

    first, first_payload = cache_command(repo, user_data, counter)
    second, second_payload = cache_command(repo, user_data, counter)
    assert first.returncode == second.returncode == 0
    assert first_payload is not None and first_payload["cache_status"] == "executed"
    assert second_payload is not None and second_payload["cache_status"] == "hit"
    assert counter.read_text(encoding="utf-8") == "run\n"

    changed_command, changed_command_payload = cache_command(
        repo, user_data, counter, extra_command_argument="second"
    )
    assert changed_command.returncode == 0
    assert changed_command_payload is not None and changed_command_payload["cache_status"] == "executed"
    changed_environment, changed_environment_payload = cache_command(
        repo, user_data, counter, extra_command_argument="second", environment_value="beta"
    )
    assert changed_environment.returncode == 0
    assert changed_environment_payload is not None and changed_environment_payload["cache_status"] == "executed"
    (repo / "nested").mkdir()
    (repo / "nested" / "new.txt").write_text("content change\n", encoding="utf-8")
    changed_content, changed_content_payload = cache_command(
        repo, user_data, counter, extra_command_argument="second", environment_value="beta"
    )
    assert changed_content.returncode == 0
    assert changed_content_payload is not None and changed_content_payload["cache_status"] == "executed"
    assert counter.read_text(encoding="utf-8") == "run\n" * 4


def test_cache_is_shared_across_linked_worktrees_with_identical_content(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    counter = tmp_path / "counter.txt"
    user_data = tmp_path / "user-data"

    first, first_payload = cache_command(repo, user_data, counter)
    second, second_payload = cache_command(linked, user_data, counter)
    assert first.returncode == second.returncode == 0
    assert first_payload is not None and first_payload["cache_status"] == "executed"
    assert second_payload is not None and second_payload["cache_status"] == "hit"
    assert counter.read_text(encoding="utf-8") == "run\n"


def test_base_worktree_context_cache_identity_is_not_shared_across_worktrees(
    tmp_path: Path,
) -> None:
    repo = initialized_repository(tmp_path / "repo")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    user_data = tmp_path / "user-data"

    def run_base_context(worktree: Path) -> dict[str, object]:
        completed = subprocess.run(
            [
                sys.executable,
                str(VALIDATION_RESULT_CACHE),
                "run",
                "--repo",
                str(worktree),
                "--name",
                "base-context-smoke",
                "--requires-base-worktree-context",
                "--validation-context-kind",
                "base_worktree",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(0)",
            ],
            env=clean_test_environment(
                {"RVF_USER_LOCAL_DATA_ROOT": str(user_data)}
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)

    first = run_base_context(repo)
    second = run_base_context(linked)
    assert first["cache_status"] == second["cache_status"] == "executed"
    assert first["cache_record_path"] != second["cache_record_path"]
    assert first["validation_context_worktree_realpath_sha256"] != second[
        "validation_context_worktree_realpath_sha256"
    ]


def test_cache_concurrent_callers_execute_suite_once(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    counter = tmp_path / "counter.txt"
    user_data = tmp_path / "user-data"
    environment = clean_test_environment({"RVF_USER_LOCAL_DATA_ROOT": str(user_data)})
    command = [
        sys.executable,
        str(VALIDATION_RESULT_CACHE),
        "run",
        "--repo",
        str(repo),
        "--name",
        "concurrent-suite",
        "--",
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import sys,time; p=Path(sys.argv[1]); time.sleep(.2); "
            "p.write_text((p.read_text() if p.exists() else '')+'run\\n')"
        ),
        str(counter),
    ]
    first = subprocess.Popen(command, cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(command, cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first_stdout, first_stderr = first.communicate(timeout=10)
    second_stdout, second_stderr = second.communicate(timeout=10)
    assert first.returncode == second.returncode == 0, (first_stderr, second_stderr)
    assert {json.loads(first_stdout)["cache_status"], json.loads(second_stdout)["cache_status"]} == {
        "executed",
        "hit",
    }
    assert counter.read_text(encoding="utf-8") == "run\n"


def test_corrupt_cache_record_fails_closed_without_rerun(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    counter = tmp_path / "counter.txt"
    user_data = tmp_path / "user-data"
    first, first_payload = cache_command(repo, user_data, counter)
    assert first.returncode == 0 and first_payload is not None
    record_path = Path(str(first_payload["cache_record_path"]))
    record_path.write_text("not json\n", encoding="utf-8")

    second, second_payload = cache_command(repo, user_data, counter)
    assert second.returncode == 76
    assert second_payload is not None
    assert second_payload["cache_status"] == "corrupt_fail_closed"
    assert counter.read_text(encoding="utf-8") == "run\n"


def test_symlink_replacement_of_cache_record_fails_closed(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    counter = tmp_path / "counter.txt"
    user_data = tmp_path / "user-data"
    first, first_payload = cache_command(repo, user_data, counter)
    assert first.returncode == 0 and first_payload is not None
    record_path = Path(str(first_payload["cache_record_path"]))
    real_record_path = record_path.with_name("relocated-record.json")
    record_path.rename(real_record_path)
    record_path.symlink_to(real_record_path)

    second, second_payload = cache_command(repo, user_data, counter)
    assert second.returncode == 76
    assert second_payload is not None
    assert second_payload["cache_status"] == "corrupt_fail_closed"
    assert counter.read_text(encoding="utf-8") == "run\n"


def test_cache_rejects_secret_like_environment_keys(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATION_RESULT_CACHE),
            "run",
            "--repo",
            str(repo),
            "--name",
            "unsafe-env-suite",
            "--environment-key",
            "ANTHROPIC_API_KEY",
            "--",
            sys.executable,
            "-c",
            "print('must not run')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "secret-like environment key" in completed.stderr


def test_relative_working_directory_and_poisoned_git_environment_are_isolated(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    (repo / "subdirectory").mkdir()
    sacrificial = initialized_repository(tmp_path / "sacrificial")
    user_data = tmp_path / "user-data"
    observed_repository_root = tmp_path / "observed-repository-root.txt"
    secret_output_marker = "validation-secret-output-must-not-persist"
    sacrificial_snapshot = {
        "head": git(sacrificial, "rev-parse", "HEAD"),
        "status": git(sacrificial, "status", "--porcelain=v2", "--untracked-files=all"),
        "config": (sacrificial / ".git/config").read_bytes(),
        "index": (sacrificial / ".git/index").read_bytes(),
    }
    environment = clean_test_environment(
        {
            "RVF_USER_LOCAL_DATA_ROOT": str(user_data),
            "GIT_DIR": str(sacrificial / ".git"),
            "GIT_WORK_TREE": str(sacrificial),
            "GIT_COMMON_DIR": str(sacrificial / ".git"),
            "GIT_INDEX_FILE": str(sacrificial / ".git/index"),
            "GIT_OBJECT_DIRECTORY": str(sacrificial / ".git/objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(sacrificial / ".git/objects"),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATION_RESULT_CACHE),
            "run",
            "--repo",
            str(repo),
            "--name",
            "poisoned-git-suite",
            "--working-directory",
            "subdirectory",
            "--",
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import subprocess,sys; "
                "root=subprocess.run(['git','rev-parse','--show-toplevel'],check=True,"
                "capture_output=True,text=True).stdout.strip(); "
                "Path(sys.argv[1]).write_text(root); print(sys.argv[2])"
            ),
            str(observed_repository_root),
            secret_output_marker,
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["command_metadata"]["working_directory_relative_to_worktree"] == "subdirectory"
    assert payload["environment"]["python_version"]
    assert observed_repository_root.read_text(encoding="utf-8") == str(repo)
    assert payload["stdout_byte_count"] == len(secret_output_marker) + 1
    assert "stdout_log_path" not in payload
    cache_entry_directory = Path(payload["cache_record_path"]).parent
    assert all(secret_output_marker.encode() not in path.read_bytes() for path in cache_entry_directory.iterdir())
    assert git(sacrificial, "rev-parse", "HEAD") == sacrificial_snapshot["head"]
    assert git(sacrificial, "status", "--porcelain=v2", "--untracked-files=all") == sacrificial_snapshot["status"]
    assert (sacrificial / ".git/config").read_bytes() == sacrificial_snapshot["config"]
    assert (sacrificial / ".git/index").read_bytes() == sacrificial_snapshot["index"]


def test_failed_result_is_cached_with_original_exit_code_and_base_context_identity(tmp_path: Path) -> None:
    repo = initialized_repository(tmp_path / "repo")
    user_data = tmp_path / "user-data"
    environment = clean_test_environment({"RVF_USER_LOCAL_DATA_ROOT": str(user_data)})
    command = [
        sys.executable,
        str(VALIDATION_RESULT_CACHE),
        "run",
        "--repo",
        str(repo),
        "--name",
        "expected-failure-suite",
        "--requires-base-worktree-context",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(7)",
    ]
    first = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    second = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert first.returncode == second.returncode == 7
    assert json.loads(first.stdout)["cache_status"] == "executed"
    second_payload = json.loads(second.stdout)
    assert second_payload["cache_status"] == "hit"
    assert second_payload["requires_base_worktree_context"] is True
