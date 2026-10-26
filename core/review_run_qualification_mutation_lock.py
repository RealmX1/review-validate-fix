"""canonical 资格写入与消费者 CAS 共用的进程锁；同线程嵌套调用可重入。"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import functools
import inspect
import os
from pathlib import Path
import sys
import threading
import time


QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH = Path("artifacts/elevated-issue-delegation/mutation.lock")
_registry_mutex = threading.RLock()
_thread_locks: dict[str, threading.RLock] = {}
_thread_held_locks = threading.local()
_open_lock_descriptors: set[int] = set()


class ReviewRunQualificationMutationLockBusy(ValueError):
    status = "waiting_for_machine_evidence"
    reason_code = "review_run_qualification_mutation_lock_busy"


def _reset_lock_state_after_fork() -> None:
    global _registry_mutex, _thread_locks, _thread_held_locks, _open_lock_descriptors
    for descriptor in _open_lock_descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass
    _registry_mutex = threading.RLock()
    _thread_locks = {}
    _thread_held_locks = threading.local()
    _open_lock_descriptors = set()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_lock_state_after_fork)


@contextmanager
def review_run_qualification_mutation_lock(run_dir: Path | None, *, timeout_seconds: float = 30):
    if run_dir is None:
        yield
        return
    run_directory = Path(run_dir).expanduser().resolve()
    lock_path = run_directory / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH
    for directory in (run_directory / "artifacts", lock_path.parent):
        if directory.is_symlink():
            raise ValueError("资格锁目录不可使用软链接")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = str(lock_path)
    with _registry_mutex:
        thread_lock = _thread_locks.setdefault(lock_key, threading.RLock())
    if not thread_lock.acquire(timeout=timeout_seconds):
        raise ReviewRunQualificationMutationLockBusy("资格写入正由同进程其他线程占用；原持有者释放后重试")
    try:
        held_locks = getattr(_thread_held_locks, "paths", set())
        if lock_key in held_locks:
            yield
            return
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with _registry_mutex:
            _open_lock_descriptors.add(descriptor)
        try:
            deadline = time.monotonic() + timeout_seconds
            waiting_notice_emitted = False
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting_notice_emitted:
                        try:
                            print(f"等待 RVF run 资格锁：{lock_path}；持有者释放后自动接续。", file=sys.stderr, flush=True)
                        except (OSError, ValueError):
                            pass
                        waiting_notice_emitted = True
                    if time.monotonic() >= deadline:
                        raise ReviewRunQualificationMutationLockBusy("消费者 CAS 或 canonical writer 持有资格锁；未越过锁，释放后重试")
                    time.sleep(0.02)
            _thread_held_locks.paths = held_locks | {lock_key}
            try:
                yield
            finally:
                _thread_held_locks.paths = held_locks
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            with _registry_mutex:
                _open_lock_descriptors.discard(descriptor)
            os.close(descriptor)
    finally:
        thread_lock.release()


def run_directory_for_qualification_artifact(path: Path) -> Path | None:
    artifact_path = Path(path).expanduser().absolute()
    if artifact_path.name == "summary.json" and artifact_path.parent.is_dir():
        return artifact_path.parent
    for parent in artifact_path.parents:
        if parent.name == "artifacts" and (parent.parent / "summary.json").is_file():
            return parent.parent
    return None


def review_run_qualification_mutation_lock_availability(run_dir: Path) -> str:
    lock_path = Path(run_dir).expanduser().resolve() / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH
    if str(lock_path) in getattr(_thread_held_locks, "paths", set()):
        return "held_by_current_thread"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "available_not_created"
    except OSError:
        return "unavailable"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return "available"
    finally:
        os.close(descriptor)


def guard_review_run_qualification_mutation(run_directory_argument):
    def decorate(function):
        signature = inspect.signature(function)
        @functools.wraps(function)
        def guarded(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            run_dir = (run_directory_argument(arguments) if callable(run_directory_argument)
                       else arguments.get(run_directory_argument))
            with review_run_qualification_mutation_lock(run_dir):
                return function(*args, **kwargs)
        return guarded
    return decorate
