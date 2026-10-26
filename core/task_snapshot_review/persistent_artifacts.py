"""原子保存进度与原始材料；锁随进程退出释放，不使用 lease。"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending-write")
    try:
        with os.fdopen(os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging_path, path)
    finally:
        staging_path.unlink(missing_ok=True)


def write_json(path: Path, content: object) -> None:
    write_bytes(path, (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path: Path) -> dict:
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError(f"需要 JSON object：{path}")
    return content


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = True, inherited_descriptor: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with (os.fdopen(inherited_descriptor,'a+b') if inherited_descriptor is not None else path.open("a+b")) as handle:
        if inherited_descriptor is None:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise RuntimeError(f"另一个进程正在处理：{path}") from None
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def git(repo: Path, *arguments: str, input_bytes: bytes | None = None, environment: dict | None = None, pass_fds: tuple[int, ...] = ()) -> bytes:
    effective_environment = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        effective_environment.pop(key, None)
    effective_environment.update(environment or {})
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "--literal-pathspecs", "-C", str(repo), *arguments],
        input=input_bytes, capture_output=True, env=effective_environment, check=False,pass_fds=pass_fds,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode(errors="replace").strip())
    return completed.stdout


def git_text(repo: Path, *arguments: str, **kwargs) -> str:
    return git(repo, *arguments, **kwargs).decode().strip()


def repository_identity(repo: Path) -> dict:
    root = Path(git_text(repo, "rev-parse", "--show-toplevel")).resolve()
    return {
        "repo": str(root),
        "git_directory": str(Path(git_text(root, "rev-parse", "--absolute-git-dir")).resolve()),
        "git_common_directory": str(Path(git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()),
    }


def verify_repository_identity(record: dict) -> Path:
    repo = Path(record["repo"])
    if repository_identity(repo) != {key: record[key] for key in ("repo", "git_directory", "git_common_directory")}:
        raise ValueError("工作树身份已经改变，保留原 run；应在原工作树恢复后接续")
    return repo
