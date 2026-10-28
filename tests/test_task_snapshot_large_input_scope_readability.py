"""正式路径中的超大文本和产物保留完整冻结内容，不夹带范围外历史。"""
from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path
import sys

from test_task_snapshot_review_workflow import canary, repository
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review


def test_large_mixed_scope_is_fully_readable_by_each_reviewer_after_worktree_drift(repository, tmp_path):
    scope = [
        "src/multiple-hunks.txt", "src/long-single-line.txt", "src/windows-lines.txt",
        "src/generator.py", "generated/payload.bin", "generated/payload.gz",
        "src/rename-before.txt", "src/rename-after.txt", "src/deleted.txt", "src/reference-link",
    ]
    (repository / "src").mkdir()
    (repository / "generated").mkdir()
    (repository / ".gitattributes").write_text("src/windows-lines.txt text eol=lf\n")
    lines = [f"unchanged line {index}\n" for index in range(240)]
    (repository / scope[0]).write_text("".join(lines))
    (repository / scope[1]).write_bytes(b"L" * 2_100_000 + b" old\n")
    (repository / scope[2]).write_bytes(b"first\nsecond\n")
    (repository / scope[3]).write_text("payload = b'old'\n")
    (repository / scope[4]).write_bytes(b"old")
    (repository / scope[5]).write_bytes(gzip.compress(b"old", mtime=0))
    (repository / scope[6]).write_text("renamed content\n")
    (repository / scope[8]).write_text("deleted content\n")
    (repository / scope[9]).symlink_to("windows-lines.txt")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "test: 固定边界输入基线")
    baseline = git_text(repository, "rev-parse", "HEAD")

    lines[2] = "--counter;\n"
    (repository / scope[0]).write_text("".join(lines))
    (repository / "outside-history.txt").write_text("EXCLUDED_HISTORY_ONLY_SENTINEL\n")
    git(repository, "add", scope[0], "outside-history.txt")
    git(repository, "commit", "-qm", "test: 已提交任务修改与范围外历史并存")
    lines[-3] = "++counter;\n"
    payload = bytes(range(256)) * 8192
    expected = {
        scope[0]: "".join(lines).encode(),
        scope[1]: b"L" * 2_100_000 + b" new\n",
        scope[2]: b"first\nmodified\n",
        scope[3]: b"payload = bytes(range(256)) * 8192\n",
        scope[4]: payload,
        scope[5]: gzip.compress(payload, mtime=0),
        scope[7]: b"renamed content\n",
        scope[9]: b"multiple-hunks.txt",
    }
    for path, content in expected.items():
        target = repository / path
        if target.is_symlink():
            target.unlink()
        if path == scope[9]:
            target.symlink_to(os.fsdecode(content))
        else:
            target.write_bytes(content if path != scope[2] else content.replace(b"\n", b"\r\n"))
    for path in (scope[6], scope[8]):
        (repository / path).unlink()
    (repository / "background.txt").write_text("staged background\n")
    git(repository, "add", "background.txt")
    (repository / "background.txt").write_text("unstaged background\n")
    before = canary(repository)
    requirements = "检查完整显式范围，包括源码和生成物；按需读取固定 Git 对象。".encode()
    run = tmp_path / "large-input-fixed-task-review"

    record = create_snapshot(repository, base_ref=baseline, scope_paths=scope,
                             requirements=requirements, run_directory=run)

    assert canary(repository) == before
    patch = (run / "task-changes.patch").read_bytes()
    assert b"EXCLUDED_HISTORY_ONLY_SENTINEL" not in patch
    assert b"outside-history.txt" not in patch
    assert b"--counter;" in patch and b"++counter;" in patch
    assert record["scope_paths"] == sorted(scope)
    assert set(record["changed_paths"]) <= set(scope)
    assert set(scope) - {scope[6]} <= set(record["changed_paths"])
    assert git(repository, "show", f"{baseline}:{scope[6]}") == expected[scope[7]]
    assert (run / "requirements.md").read_bytes() == requirements
    assert b"L" * 1000 not in (run / "review-input.md").read_bytes()
    for path, content in expected.items():
        assert git(repository, "show", f"{record['candidate_commit']}:{path}") == content
    assert git_text(repository, "ls-tree", record["candidate_commit"], scope[9]).startswith("120000")
    for path in (scope[6], scope[8]):
        assert not git(repository, "ls-tree", record["candidate_commit"], "--", path)
    (repository / scope[1]).write_text("冻结后工作区已改变\n")
    after_drift = canary(repository)

    expected_file = tmp_path / "expected-fixed-path-byte-identities.json"
    write_json(expected_file, {path: hashlib.sha256(content).hexdigest() for path, content in expected.items()})
    verifier = tmp_path / "review-each-full-scoped-file.py"
    verifier.write_text(
        "import hashlib,json,os,pathlib,sys\n"
        "expected=json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "for path,digest in expected.items():\n"
        " p=pathlib.Path(path)\n"
        " content=os.fsencode(os.readlink(p)) if p.is_symlink() else p.read_bytes()\n"
        " assert hashlib.sha256(content).hexdigest()==digest,path\n"
        "assert not pathlib.Path('src/deleted.txt').exists()\n"
        "assert not pathlib.Path('src/rename-before.txt').exists()\n"
        "print(json.dumps({'status':'completed','audit_summary':'已读取全部显式路径，包括超长单行、规范化文本、二进制和压缩产物','findings':[]}))\n"
    )
    config = {"command": [sys.executable, str(verifier), str(expected_file)], "output_format": "text"}

    result = resume_review(run, reviewer_configs=[config, config])

    assert result["status"] == "ready_for_validation"
    for leg in ("reviewer-1", "reviewer-2"):
        attempt = run / leg / "attempt-001"
        assert read_json(attempt / "outcome.json")["returncode"] == 0
        assert read_json(attempt / "result.json")["status"] == "completed"
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 2
    assert canary(repository) == after_drift
    assert resume_review(run, validation_commands=[[sys.executable, "-c", "pass"]])["status"] == "candidate_changed"
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 2
    assert git(repository, "show", f"{record['candidate_commit']}:{scope[1]}") == expected[scope[1]]
