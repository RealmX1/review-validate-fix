#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from _rvf_test_support.repo import templated_repo


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
    / "rvf_handoff_intake.py"
)


def run(cmd: list[str], cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed


@templated_repo
def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=path)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=path)
    run(["git", "config", "user.name", "RVF Test"], cwd=path)
    (path / "scoped.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "scoped.txt"], cwd=path)
    run(["git", "commit", "-q", "-m", "base"], cwd=path)
    (path / "scoped.txt").write_text("base\nchange\n", encoding="utf-8")
    (path / "other.txt").write_text("other\n", encoding="utf-8")
    return path


def init_clean_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=path)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=path)
    run(["git", "config", "user.name", "RVF Test"], cwd=path)
    return path


def test_handoff_intake_summarizes_scope_status_and_artifacts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts" / "inputs"
    artifacts.mkdir(parents=True)
    (artifacts / "session-manifest.json").write_text(
        json.dumps({"owned_dirty_paths": ["scoped.txt"]}),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "reason_code": "handoff",
                "repo": str(repo),
            }
        ),
        encoding="utf-8",
    )
    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        "\n".join(
            [
                "## Origin",
                "- RVF run id: `rvf-20260506T000000Z-test`",
                "",
                "## 状态",
                f"- run dir: {run_dir}",
                f"- 目标仓库: {repo}",
                "",
                "## Scope",
                "- Session-owned files reviewed:",
                "  - `scoped.txt`",
                "",
                "## Handoff intake hints",
                "- reviewed scope paths:",
                "  - scoped.txt",
                "- protected / background / cross-session paths:",
                "  - other.txt",
                "- accepted changes:",
                "  - scoped.txt",
                "- rejected / not accepted changes:",
                "  - shared lease hunks in tests/test_review_support_scripts.py",
                "- main-session validation commands:",
                "  - python3 tests/test_rvf_handoff_intake.py",
                "",
                "## Review And Fix",
                "- Left untouched unrelated dirty paths.",
                "",
                "## Validation",
                "- `python3 -m py_compile scoped.py` -> passed.",
            ]
        ),
        encoding="utf-8",
    )

    completed = run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)])
    payload = json.loads(completed.stdout)

    assert payload["run_id"] == "rvf-20260506T000000Z-test"
    assert payload["run_dir"] == str(run_dir)
    assert payload["reviewed_scope_paths"] == ["scoped.txt"]
    assert payload["artifact_paths"]["session_manifest"] == str(artifacts / "session-manifest.json")
    assert payload["scoped_status_in_current_repo"] == [{"path": "scoped.txt", "status": " M"}]
    assert payload["unrelated_dirty_paths_in_current_repo"] == [{"status": "??", "path": "other.txt", "old_path": ""}]
    assert payload["target_repo_same_git_common_dir_as_current"] is True
    assert payload["validation_commands"] == ["python3 -m py_compile scoped.py"]
    assert "- Left untouched unrelated dirty paths." in payload["conflict_hints"]
    assert payload["intake_hints"]["protected_paths"] == ["other.txt"]
    assert payload["intake_hints"]["accepted_changes"] == ["scoped.txt"]
    assert payload["intake_hints"]["rejected_changes"] == [
        "shared lease hunks in tests/test_review_support_scripts.py"
    ]
    assert payload["intake_hints"]["main_session_validation_commands"] == [
        "python3 tests/test_rvf_handoff_intake.py"
    ]


def test_handoff_intake_matches_git_porcelain_z_paths(tmp_path: Path) -> None:
    repo = init_clean_repo(tmp_path / "repo")
    quoted_path = '路径 "quoted".txt'
    (repo / quoted_path).write_text("base\n", encoding="utf-8")
    run(["git", "add", quoted_path], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / quoted_path).write_text("base\nchange\n", encoding="utf-8")

    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        "\n".join(
            [
                "## 状态",
                f"- target repo: {repo}",
                "",
                "## Handoff intake hints",
                "- reviewed scope paths:",
                f"  - {quoted_path}",
            ]
        ),
        encoding="utf-8",
    )

    completed = run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)])
    payload = json.loads(completed.stdout)

    assert payload["scoped_status_in_current_repo"] == [{"path": quoted_path, "status": " M"}]
    assert payload["unrelated_dirty_paths_in_current_repo"] == []
    assert payload["current_repo_snapshot"]["dirty_paths"] == [
        {"status": " M", "path": quoted_path, "old_path": ""}
    ]


def test_handoff_intake_treats_rename_old_and_new_paths_as_scoped(tmp_path: Path) -> None:
    repo = init_clean_repo(tmp_path / "repo")
    (repo / "old.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "old.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    run(["git", "mv", "old.txt", "new.txt"], cwd=repo)

    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        "\n".join(
            [
                "## 状态",
                f"- target repo: {repo}",
                "",
                "## Scope",
                "- Session-owned files reviewed:",
                "  - `old.txt`",
                "  - `new.txt`",
            ]
        ),
        encoding="utf-8",
    )

    completed = run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)])
    payload = json.loads(completed.stdout)

    assert payload["scoped_status_in_current_repo"] == [
        {"path": "old.txt", "status": "R "},
        {"path": "new.txt", "status": "R "},
    ]
    assert payload["unrelated_dirty_paths_in_current_repo"] == []
    assert payload["current_repo_snapshot"]["dirty_paths"] == [
        {"status": "R ", "path": "new.txt", "old_path": "old.txt"}
    ]


def test_handoff_intake_maps_current_handoff_artifact_fields(tmp_path: Path) -> None:
    repo = init_clean_repo(tmp_path / "repo")
    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    inputs = artifacts / "inputs"
    inputs.mkdir(parents=True)
    origin = artifacts / "origin.json"
    scope_of_work = inputs / "scope-of-work.md"
    session_manifest = inputs / "session-manifest.json"
    review_packet = inputs / "review-packet.md"
    scope_contract = artifacts / "scope.contract.json"
    transcript = tmp_path / "transcript.jsonl"
    for path in (origin, scope_of_work, review_packet, scope_contract, transcript):
        path.write_text("{}\n", encoding="utf-8")
    session_manifest.write_text(json.dumps({"owned_dirty_paths": []}), encoding="utf-8")

    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        "\n".join(
            [
                "## Origin",
                "- original Codex URL: https://chatgpt.example/c/123",
                f"- original transcript: {transcript}",
                f"- origin metadata: {origin}",
                "",
                "## Review scope",
                f"- scope-of-work: {scope_of_work}",
                f"- session manifest: {session_manifest}",
                f"- review packet: {review_packet}",
                f"- scope contract: {scope_contract}",
                "",
                "## Handoff intake hints",
                f"- RVF worktree / target repo: {repo}",
            ]
        ),
        encoding="utf-8",
    )

    completed = run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)])
    payload = json.loads(completed.stdout)

    assert payload["run_dir"] == str(run_dir)
    assert payload["target_repo"] == str(repo)
    assert payload["origin"] == {
        "codex_url": "https://chatgpt.example/c/123",
        "transcript_path": str(transcript),
        "origin_metadata_path": str(origin),
    }
    assert payload["artifact_paths"]["scope_of_work"] == str(scope_of_work)
    assert payload["artifact_paths"]["session_manifest"] == str(session_manifest)
    assert payload["artifact_paths"]["review_packet"] == str(review_packet)
    assert payload["artifact_paths"]["scope_contract"] == str(scope_contract)


def test_handoff_intake_parses_template_origin_metadata_and_path_run_dir(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_dir = tmp_path / "state" / "runs" / "rvf-20260506T000000Z-origin"
    artifacts = run_dir / "artifacts"
    (artifacts / "inputs").mkdir(parents=True)
    origin = artifacts / "origin.json"
    origin.write_text("{}", encoding="utf-8")
    handoff = artifacts / "handoff.md"
    handoff.write_text(
        "\n".join(
            [
                "## Origin",
                "- original Codex URL: codex://local/abc",
                "- original transcript: /tmp/transcript.jsonl",
                f"- origin metadata: {origin}",
            ]
        ),
        encoding="utf-8",
    )

    completed = run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)])
    payload = json.loads(completed.stdout)

    assert payload["run_id"] == "rvf-20260506T000000Z-origin"
    assert payload["run_dir"] == str(run_dir)
    assert payload["origin"]["origin_metadata_path"] == str(origin)

    fallback_handoff = artifacts / "handoff-without-origin.md"
    fallback_handoff.write_text("## Origin\n- original Codex conversation: test\n", encoding="utf-8")
    completed = run([sys.executable, str(SCRIPT), "--handoff", str(fallback_handoff), "--repo", str(repo)])
    payload = json.loads(completed.stdout)
    assert payload["run_dir"] == str(run_dir)


if __name__ == "__main__":
    test_handoff_intake_summarizes_scope_status_and_artifacts(
        Path(tempfile.mkdtemp(prefix="rvf-handoff-intake-test-"))
    )
    test_handoff_intake_matches_git_porcelain_z_paths(
        Path(tempfile.mkdtemp(prefix="rvf-handoff-intake-test-"))
    )
    test_handoff_intake_treats_rename_old_and_new_paths_as_scoped(
        Path(tempfile.mkdtemp(prefix="rvf-handoff-intake-test-"))
    )
    test_handoff_intake_maps_current_handoff_artifact_fields(
        Path(tempfile.mkdtemp(prefix="rvf-handoff-intake-test-"))
    )
    test_handoff_intake_parses_template_origin_metadata_and_path_run_dir(
        Path(tempfile.mkdtemp(prefix="rvf-handoff-intake-test-"))
    )
    print("rvf handoff intake tests OK")
