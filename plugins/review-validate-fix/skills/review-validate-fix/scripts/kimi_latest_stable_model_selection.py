"""通过 Kimi 官方 OAuth 刷新接口为一次评审选择稳定标准/Pro 模型。"""
from __future__ import annotations

import http.client
import json
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


KIMI_MANAGED_PROVIDER_ID = "managed:kimi-code"
KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_DIRECTORY_NAME = ".kanban-kimi-model-catalog-refresh.lock"
KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_STALE_SECONDS = 30
KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_HEARTBEAT_SECONDS = 15


class KimiModelCatalogHomeRefreshLock:
    """用空目录、mtime 心跳与目录 fd 身份校验兼容跨进程共享 home 锁协议。"""

    def __init__(self, home: Path, remaining_seconds, on_compromised):
        self.path = home / KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_DIRECTORY_NAME
        self.remaining_seconds = remaining_seconds
        self.on_compromised = on_compromised
        self.directory_descriptor = None
        self.owned_directory_snapshot = None
        self.heartbeat_thread = None
        self.stop_heartbeat = threading.Event()
        self.compromised = threading.Event()
        self.ownership_guard = threading.RLock()

    @staticmethod
    def _directory_snapshot(directory_stat):
        return directory_stat.st_dev, directory_stat.st_ino, directory_stat.st_mtime_ns

    def _path_matches_owned_directory(self):
        try:
            return self._directory_snapshot(self.path.stat(follow_symlinks=False)) == self.owned_directory_snapshot
        except FileNotFoundError:
            return False

    def _mark_compromised(self):
        with self.ownership_guard:
            if not self.compromised.is_set():
                self.compromised.set()
                self.on_compromised()

    def assert_held(self):
        with self.ownership_guard:
            if self.owned_directory_snapshot is not None:
                if not self.compromised.is_set() and not self._path_matches_owned_directory():
                    self._mark_compromised()
                if self.compromised.is_set():
                    raise ValueError("Kimi 模型目录刷新互斥锁已失效")

    def _refresh_heartbeat(self):
        while not self.stop_heartbeat.wait(KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_HEARTBEAT_SECONDS):
            try:
                with self.ownership_guard:
                    self.assert_held()
                    # fd 始终指向旧 inode，检查后发生替换也不会刷新新持有者。
                    os.utime(self.directory_descriptor, None)
                    self.owned_directory_snapshot = self._directory_snapshot(os.fstat(self.directory_descriptor))
                    self.assert_held()
            except (OSError, ValueError):
                self._mark_compromised()
                return

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            self.remaining_seconds()
            try:
                self.path.mkdir(mode=0o700)
            except FileExistsError:
                try:
                    observed = self.path.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(observed.st_mode):
                        raise ValueError("Kimi 模型目录刷新锁不是目录")
                    if time.time() - observed.st_mtime > KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_STALE_SECONDS:
                        # 打开的目录 fd 固定旧身份；mtime 已变化或新 holder 已替换时不删除。
                        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            observed_snapshot = self._directory_snapshot(observed)
                            if self._directory_snapshot(os.fstat(descriptor)) == observed_snapshot and self._directory_snapshot(
                                self.path.stat(follow_symlinks=False)
                            ) == observed_snapshot:
                                self.path.rmdir()
                        finally:
                            os.close(descriptor)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(min(0.05, self.remaining_seconds()))
                continue
            self.owned_directory_snapshot = self._directory_snapshot(self.path.stat(follow_symlinks=False))
            self.directory_descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if self._directory_snapshot(os.fstat(self.directory_descriptor)) != self.owned_directory_snapshot:
                self._mark_compromised()
            self.assert_held()
            heartbeat_thread = threading.Thread(
                target=self._refresh_heartbeat, name="rvf-kimi-model-catalog-home-lock-heartbeat",
            )
            heartbeat_thread.start()
            self.heartbeat_thread = heartbeat_thread
            self.remaining_seconds()
            return

    def release(self):
        self.stop_heartbeat.set()
        if self.heartbeat_thread is not None:
            self.heartbeat_thread.join()
        with self.ownership_guard:
            if self.owned_directory_snapshot is not None:
                try:
                    self.assert_held()
                    self.path.rmdir()
                finally:
                    if self.directory_descriptor is not None:
                        os.close(self.directory_descriptor)
                        self.directory_descriptor = None
                    self.owned_directory_snapshot = None

    def __enter__(self):
        try:
            self.acquire()
            return self
        except BaseException:
            self.release()
            raise

    def __exit__(self, *_):
        self.release()


def select_latest_kimi_managed_stable_model(items: list[Any]) -> str:
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Kimi 官方模型目录包含非法条目")
        if item.get("provider") != KIMI_MANAGED_PROVIDER_ID:
            continue
        alias = item.get("model")
        if not isinstance(alias, str):
            raise ValueError("Kimi 官方模型目录缺少模型 alias")
        match = re.fullmatch(r"kimi-code/k(\d+(?:\.\d+)*)(-pro)?(?:-\d+k)?", alias, re.IGNORECASE)
        if not match or re.search(r"highspeed|preview", str(item.get("display_name", "")), re.IGNORECASE):
            continue
        context_size = item.get("max_context_size")
        if type(context_size) is not int or context_size <= 0:
            raise ValueError("Kimi 稳定模型的上下文窗口非法")
        version = tuple(int(segment) for segment in match[1].split("."))
        while len(version) > 1 and version[-1] == 0:
            version = version[:-1]
        candidates.append((version, bool(match[2]), context_size, alias))
    if not candidates:
        raise ValueError("Kimi 官方目录没有合格的稳定标准/Pro 模型")
    return max(candidates)[-1]


def resolve_latest_kimi_managed_stable_model(
    executable: str, *, env: dict[str, str], timeout: float,
) -> str:
    deadline = time.monotonic() + timeout
    home_refresh_lock = None

    def remaining_seconds() -> float:
        if home_refresh_lock is not None:
            home_refresh_lock.assert_held()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Kimi 官方模型目录刷新超时")
        return remaining

    kimi_home = Path(env.get("KIMI_CODE_HOME") or Path(env.get("HOME") or Path.home()) / ".kimi-code")
    process = None
    active_request_socket = None
    output_drain_thread = None
    stop_output_drain = threading.Event()
    def interrupt_active_request() -> None:
        request_socket = active_request_socket
        if request_socket is not None:
            try:
                request_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def abort_compromised_refresh() -> None:
        interrupt_active_request()
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    home_refresh_lock = KimiModelCatalogHomeRefreshLock(kimi_home, remaining_seconds, abort_compromised_refresh)
    # 外层锁覆盖服务与临时目录的清理；查询不加载评审仓项目配置或创建会话工件。
    with home_refresh_lock, tempfile.TemporaryDirectory(prefix="rvf-kimi-model-catalog-") as working_directory:
        try:
            remaining_seconds()
            process = subprocess.Popen(
                [executable, "web", "--host", "127.0.0.1", "--port", "0", "--no-open"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                cwd=working_directory, start_new_session=True,
            )
            base_url = None
            with selectors.DefaultSelector() as reader:
                reader.register(process.stdout, selectors.EVENT_READ)
                startup_output = b""
                while base_url is None:
                    if not reader.select(min(0.2, remaining_seconds())):
                        if process.poll() is not None:
                            raise ValueError("Kimi 本地模型服务提前退出")
                        continue
                    # 必须读原始字节；readline 的预读缓冲与 fd readiness 无法配合。
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        raise ValueError("Kimi 本地模型服务提前退出")
                    startup_output = (startup_output + chunk)[-65536:]
                    match = re.search(rb"http://127\.0\.0\.1:(\d+)(?:/|\s)", startup_output)
                    if match and 0 < int(match[1]) < 65536:
                        base_url = "http://127.0.0.1:" + match[1].decode("ascii")

            def discard_service_output() -> None:
                # HTTP 请求期间持续排空；按固定大小丢弃，避免日志阻塞服务或暴露凭证。
                with selectors.DefaultSelector() as output_reader:
                    output_reader.register(process.stdout, selectors.EVENT_READ)
                    while not stop_output_drain.is_set():
                        if output_reader.select(0.1) and not os.read(process.stdout.fileno(), 8192):
                            break

            output_drain_thread = threading.Thread(
                target=discard_service_output, name="rvf-kimi-model-catalog-output-drain",
            )
            output_drain_thread.start()

            token = (kimi_home / "server.token").read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError("Kimi 本地模型服务未提供认证 token")
            def request_catalog(path: str, *, method: str = "GET") -> dict[str, Any]:
                nonlocal active_request_socket
                # 直接连接 loopback，不经过代理，也不跟随重定向携带认证信息。
                connection = http.client.HTTPConnection(
                    "127.0.0.1", int(base_url.rsplit(":", 1)[1]), timeout=remaining_seconds(),
                )
                request_deadline_timer = None
                try:
                    connection.connect()
                    active_request_socket = connection.sock
                    # 单次 socket timeout 无法限制持续 trickle；shutdown 会唤醒缓冲读取。
                    request_deadline_timer = threading.Timer(remaining_seconds(), interrupt_active_request)
                    request_deadline_timer.start()
                    connection.request(
                        method, path, body=b"{}" if method == "POST" else None,
                        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                    )
                    with connection.getresponse() as response:
                        if not 200 <= response.status < 300:
                            raise ValueError("Kimi 本地模型接口返回失败状态")
                        payload = json.load(response)
                    remaining_seconds()
                except Exception as exc:
                    remaining_seconds()
                    # 异常正文可能包含凭证或服务输出，只记录异常类型。
                    raise ValueError(f"Kimi 本地模型接口请求失败: {type(exc).__name__}") from None
                finally:
                    if request_deadline_timer is not None:
                        request_deadline_timer.cancel()
                        request_deadline_timer.join()
                    connection.close()
                    active_request_socket = None
                if (
                    not isinstance(payload, dict) or type(payload.get("code")) is not int
                    or payload["code"] != 0 or not isinstance(payload.get("data"), dict)
                ):
                    raise ValueError("Kimi 本地模型接口返回失败或非法信封")
                return payload["data"]

            report = request_catalog("/api/v1/providers:refresh_oauth", method="POST")
            changed, unchanged, failed = (report.get(key) for key in ("changed", "unchanged", "failed"))
            if not all(isinstance(value, list) for value in (changed, unchanged, failed)) or failed:
                raise ValueError("Kimi 官方 OAuth 模型目录刷新失败")
            if any(
                not isinstance(item, str) and not (
                    isinstance(item, dict) and isinstance(item.get("provider_id"), str)
                ) for item in changed
            ) or not all(isinstance(item, str) for item in unchanged):
                raise ValueError("Kimi 官方 OAuth 模型目录刷新报告非法")
            refreshed = [item if isinstance(item, str) else item["provider_id"] for item in changed] + unchanged
            if KIMI_MANAGED_PROVIDER_ID not in refreshed:
                raise ValueError("Kimi 官方 OAuth provider 未成功刷新")
            items = request_catalog("/api/v1/models").get("items")
            if not isinstance(items, list):
                raise ValueError("Kimi 官方模型目录不是列表")
            remaining_seconds()
            return select_latest_kimi_managed_stable_model(items)
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            raise ValueError(f"无法刷新 Kimi 官方模型目录: {type(exc).__name__}") from None
        finally:
            if process is not None:
                # 不先回收父进程 PID，保留本次进程组身份；父进程退出也必须升级整组清理。
                for group_signal in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, group_signal)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # macOS 对已无可发信号成员的进程组可能返回 EPERM。
                        if group_signal == signal.SIGKILL and process.poll() is None:
                            process.kill()
                    if group_signal == signal.SIGTERM and output_drain_thread is not None:
                        # EOF 允许正常服务立即结束；忽略 SIGTERM 的成员最多获 0.1 秒宽限。
                        output_drain_thread.join(timeout=0.1)
                process.wait(timeout=2)
                if process.stdout is not None:
                    stop_output_drain.set()
                    if output_drain_thread is not None:
                        output_drain_thread.join()
                    process.stdout.close()
