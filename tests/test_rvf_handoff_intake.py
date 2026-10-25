#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
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
VALIDATION_RESULT_CACHE = SCRIPT.parent / "validation_result_cache.py"


def clean_test_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    # 同时剥掉 RVF_* / CODEX_RVF_*：被测脚本不得读到活 run 的环境。validation_result_cache 曾回退读
    # RVF_RUN_DIR、把测试记录写进活 run 的 validation index；源头已删，这里保留为纵深防御。
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("GIT_", "RVF_", "CODEX_RVF_"))
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    environment.update(extra or {})
    return environment


def run(cmd: list[str], cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=clean_test_environment(),
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

def create_valid_reviewed_candidate_evidence_fixture(
    tmp_path: Path,
    repo: Path,
    *,
    cache_key_character: str,
    command_digest_character: str,
    environment_digest_character: str,
) -> tuple[Path, dict[str, object], Path]:
    git_dir = Path(run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo).stdout.strip())
    common_dir = Path(
        run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo,
        ).stdout.strip()
    ).resolve()
    manifest = json.loads(
        run(
            [sys.executable, str(VALIDATION_RESULT_CACHE), "manifest", "--repo", str(repo)]
        ).stdout
    )
    cache_key = cache_key_character * 64
    validation_process = subprocess.run(
        [
            sys.executable,
            str(VALIDATION_RESULT_CACHE),
            "run",
            "--repo",
            str(repo),
            "--name",
            f"targeted-{cache_key_character}",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
        env=clean_test_environment(
            {"RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "rvf-user-data")}
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    record = json.loads(validation_process.stdout)
    record_path = Path(record["cache_record_path"])
    record_digest = Path(f"{record_path}.sha256").read_text(
        encoding="ascii"
    ).strip()
    command_digest = record["command_sha256"]
    environment_digest = record["environment_fingerprint_sha256"]

    head_oid = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run_id = f"rvf-handoff-intake-{cache_key_character}"
    canonical_scope = {
        "version": 3,
        "review_kind": "implementation",
        "base_ref": "main",
        "remote_name": "origin",
    }
    scope_digest = hashlib.sha256(
        json.dumps(
            canonical_scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    contract = {
        **canonical_scope,
        "run_id": run_id,
        "scope_hash": scope_digest,
        "canonical_scope": canonical_scope,
    }
    contract_path = tmp_path / f"scope-{cache_key_character}.contract.json"
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    contract_digest = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence_path = git_dir / "rvf/evidence/reviewed-task-candidate-evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence: dict[str, object] = {
        "schema": "rvf-reviewed-task-candidate-evidence",
        "schema_version": 1,
        "subject": {
            "worktree_realpath": str(repo.resolve()),
            "git_common_dir_realpath": str(common_dir),
            "head_oid": head_oid,
            "candidate_tree_oid": run(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=repo
            ).stdout.strip(),
            "comparison_base_tip": head_oid,
            "reviewed_base_tip": head_oid,
            "base_ref": "main",
            "remote_name": "origin",
            **manifest,
        },
        "review": {
            "run_id": run_id,
            "review_kind": "implementation",
            "conclusion": "no_issues",
            "protocol_valid": True,
            "canonical_issue_count": 0,
            "completed_at": "2026-07-21T00:00:00Z",
        },
        "scope": {
            "contract_path": str(contract_path),
            "contract_sha256": contract_digest,
            "scope_digest": scope_digest,
        },
        "validation_records": [
            {
                "name": record["name"],
                "status": "passed",
                "exit_code": 0,
                "command_sha256": command_digest,
                "environment_fingerprint_sha256": environment_digest,
                "cache_record_path": str(record_path),
                "cache_record_sha256": record_digest,
                "requires_base_worktree_context": False,
                "validation_context_kind": record["validation_context_kind"],
                "validation_context_worktree_realpath_sha256": record[
                    "validation_context_worktree_realpath_sha256"
                ],
                "candidate_content_manifest_sha256": manifest[
                    "candidate_content_manifest_sha256"
                ],
            }
        ],
        "receipts": {
            "bootstrap_path": None,
            "bootstrap_sha256": None,
            "parent_evidence_path": None,
            "parent_evidence_sha256": None,
            "integration_candidate_path": None,
            "integration_candidate_sha256": None,
        },
        "created_at": "2026-07-21T00:00:00Z",
    }
    encoded = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    evidence_path.write_bytes(encoded)
    Path(f"{evidence_path}.sha256").write_text(
        hashlib.sha256(encoded).hexdigest() + "\n", encoding="ascii"
    )
    return evidence_path, evidence, record_path


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


def test_handoff_intake_uses_candidate_evidence_before_nested_markdown_validation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    evidence_path, evidence, record_path = create_valid_reviewed_candidate_evidence_fixture(
        tmp_path,
        repo,
        cache_key_character="1",
        command_digest_character="2",
        environment_digest_character="3",
    )
    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        f"- run id: `{evidence['review']['run_id']}`\n"
        f"- scope contract: `{evidence['scope']['contract_path']}`\n\n"
        "## 验证\n\n### Scoped verification (in-contract)\n\n"
        "- `pytest tests/test_example.py::test_case[param-id]` -> passed\n",
        encoding="utf-8",
    )
    payload = json.loads(
        run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)]).stdout
    )
    assert payload["validation_evidence_source"] == "candidate_evidence"
    assert payload["validation_records"] == evidence["validation_records"]
    assert payload["validation_commands"] == []
    assert payload["candidate_evidence_path"] == str(evidence_path)

    record_path.write_text("tampered\n", encoding="utf-8")
    tampered_payload = json.loads(
        run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)]).stdout
    )
    assert tampered_payload["candidate_evidence_status"] == "invalid"
    assert tampered_payload["validation_evidence_source"] == "invalid_candidate_evidence"
    assert tampered_payload["validation_records"] == []
    assert tampered_payload["validation_commands"] == [
        "pytest tests/test_example.py::test_case[param-id]"
    ]


def test_handoff_intake_refuses_stale_candidate_evidence_and_preserves_legacy_commands(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    create_valid_reviewed_candidate_evidence_fixture(
        tmp_path,
        repo,
        cache_key_character="4",
        command_digest_character="5",
        environment_digest_character="6",
    )
    (repo / "other.txt").write_text("candidate drift\n", encoding="utf-8")
    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        "## 验证\n\n### Scoped verification (in-contract)\n\n- `npm test` -> passed\n",
        encoding="utf-8",
    )
    payload = json.loads(
        run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)]).stdout
    )
    assert payload["candidate_evidence_status"] == "stale"
    assert payload["validation_evidence_source"] == "stale_candidate_evidence"
    assert payload["validation_records"] == []
    assert payload["validation_commands"] == ["npm test"]


def test_handoff_intake_does_not_reuse_same_content_evidence_from_another_run(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    create_valid_reviewed_candidate_evidence_fixture(
        tmp_path,
        repo,
        cache_key_character="a",
        command_digest_character="b",
        environment_digest_character="c",
    )
    run_dir = tmp_path / "rvf-20260721T120000Z-new-run"
    handoff = run_dir / "artifacts/handoff.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text(
        "RVF run rvf-20260721T120000Z-new-run\n\n"
        "## 验证\n\n- `npm test` -> passed\n",
        encoding="utf-8",
    )
    payload = json.loads(
        run(
            [sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)]
        ).stdout
    )
    assert payload["candidate_evidence_status"] == "foreign_run"
    assert payload["validation_evidence_source"] == "foreign_run_candidate_evidence"
    assert payload["validation_records"] == []
    assert payload["validation_commands"] == ["npm test"]


def test_handoff_intake_requires_run_and_scope_binding_before_reusing_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    _evidence_path, evidence, _record_path = create_valid_reviewed_candidate_evidence_fixture(
        tmp_path, repo, cache_key_character="d", command_digest_character="e", environment_digest_character="f",
    )
    handoff = tmp_path / "handoff.md"
    validation = "## 验证\n\n- `npm test` -> passed\n"
    for identity, expected_status in (
        ("", "unbound_run"),
        (f"- run id: `{evidence['review']['run_id']}`\n", "unbound_scope"),
    ):
        handoff.write_text(identity + validation, encoding="utf-8")
        payload = json.loads(run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)]).stdout)
        assert payload["candidate_evidence_status"] == expected_status
        assert payload["validation_records"] == []
        assert payload["validation_commands"] == ["npm test"]
        assert payload["candidate_evidence_identity_recovery_condition"]


def test_handoff_intake_treats_incomplete_candidate_evidence_as_invalid_not_stale(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    evidence_path, evidence, _record_path = create_valid_reviewed_candidate_evidence_fixture(
        tmp_path,
        repo,
        cache_key_character="7",
        command_digest_character="8",
        environment_digest_character="9",
    )
    review = evidence["review"]
    assert isinstance(review, dict)
    review.pop("review_kind")
    encoded = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    evidence_path.write_bytes(encoded)
    Path(f"{evidence_path}.sha256").write_text(
        hashlib.sha256(encoded).hexdigest() + "\n", encoding="ascii"
    )
    handoff = tmp_path / "handoff.md"
    handoff.write_text("## 验证\n\n- `npm test` -> passed\n", encoding="utf-8")

    payload = json.loads(
        run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)]).stdout
    )
    assert payload["candidate_evidence_status"] == "invalid"
    assert payload["validation_evidence_source"] == "invalid_candidate_evidence"
    assert payload["validation_records"] == []
    assert payload["validation_commands"] == ["npm test"]


def test_candidate_evidence_fixture_does_not_write_into_ambient_rvf_run_directory(
    tmp_path: Path,
) -> None:
    # 回归：validation_result_cache 的 --rvf-run-dir 曾缺省回退到 RVF_RUN_DIR；fixture 不传该参数，
    # 在活 run shell 里跑测试会把 targeted-* 记录写进活 run。源头与 clean_test_environment 两层都挡。
    ambient_run_directory = tmp_path / "ambient-rvf-run-directory-must-stay-untouched"
    ambient_run_directory.mkdir()
    repo = init_repo(tmp_path / "repo")
    previous_rvf_run_directory = os.environ.get("RVF_RUN_DIR")
    os.environ["RVF_RUN_DIR"] = str(ambient_run_directory)
    try:
        create_valid_reviewed_candidate_evidence_fixture(
            tmp_path,
            repo,
            cache_key_character="e",
            command_digest_character="f",
            environment_digest_character="0",
        )
    finally:
        if previous_rvf_run_directory is None:
            os.environ.pop("RVF_RUN_DIR", None)
        else:
            os.environ["RVF_RUN_DIR"] = previous_rvf_run_directory
    artifacts = ambient_run_directory / "artifacts"
    assert not (artifacts / "validation-records.json").exists()
    assert not (artifacts / "handoff-validation-commands.json").exists()
    assert not artifacts.exists()


def test_handoff_intake_ignores_poisoned_git_repository_location_environment(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    sacrificial = init_repo(tmp_path / "sacrificial")
    sacrificial_head = run(["git", "rev-parse", "HEAD"], cwd=sacrificial).stdout.strip()
    sacrificial_index = (sacrificial / ".git/index").read_bytes()
    handoff = tmp_path / "handoff.md"
    handoff.write_text("## 验证\n\n- `npm test` -> passed\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)],
        env=clean_test_environment(
            {
                "GIT_DIR": str(sacrificial / ".git"),
                "GIT_WORK_TREE": str(sacrificial),
                "GIT_COMMON_DIR": str(sacrificial / ".git"),
                "GIT_INDEX_FILE": str(sacrificial / ".git/index"),
                "GIT_OBJECT_DIRECTORY": str(sacrificial / ".git/objects"),
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["current_repo_snapshot"]["repo"] == str(repo.resolve())
    assert run(["git", "rev-parse", "HEAD"], cwd=sacrificial).stdout.strip() == sacrificial_head
    assert (sacrificial / ".git/index").read_bytes() == sacrificial_index


def test_handoff_intake_legacy_validation_collects_nested_subsections_and_bracketed_ids(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        "## 验证\n\n### Scoped verification (in-contract)\n\n"
        "- `pytest tests/test_example.py::test_case[param-id]` -> passed\n\n"
        "### Full-suite results (unrelated)\n\n- `npm test` -> passed\n",
        encoding="utf-8",
    )
    payload = json.loads(
        run([sys.executable, str(SCRIPT), "--handoff", str(handoff), "--repo", str(repo)]).stdout
    )
    assert payload["validation_evidence_source"] == "legacy_handoff_markdown"
    assert payload["validation_records"] == []
    assert payload["validation_commands"] == [
        "pytest tests/test_example.py::test_case[param-id]",
        "npm test",
    ]

if __name__ == "__main__":
    # **不要再手写清单。** 这里曾经列举 7 个函数，而文件里有 14 个——另外 7 个（含当时唯一覆盖
    # 门禁的三个）在 `python3 tests/test_rvf_handoff_intake.py` 下**静默不跑**，handoff 里自称的
    # 那条命令对它们零覆盖。改成从模块 globals 取全集，新增测试自动进入自跑入口。
    _cases = sorted(
        (name, value)
        for name, value in list(globals().items())
        if name.startswith("test_") and callable(value)
    )
    for _name, _case in _cases:
        _case(Path(tempfile.mkdtemp(prefix="rvf-handoff-intake-test-")))
    print(f"rvf handoff intake tests OK ({len(_cases)} cases)")
