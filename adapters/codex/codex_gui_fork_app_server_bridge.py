#!/usr/bin/env python3
"""Codex GUI-fork / app-server / bridge 子系统（adapters/codex 的 fork 执行维）。

S9a：从共享审查引擎（``codex_stop_review_validate_fix.py``）抽出的 **Codex 专属**
fork 执行缝——通过 Codex Desktop 的 app-server control socket（含 RVF 自管的
bridge app-server）创建并可见化 GUI fork 线程、查询父线程名、做 socket 选择/探活/
重启与可见性诊断。

这是 host-specific 实现：只依赖 stdlib 与本文件自带的几个 1-3 行纯工具副本
（``is_truthy`` / ``is_falsey`` / ``codex_bin``，与引擎同源——本仓既有惯例即
「每个脚本自含这些 trivial helper」，见 ``codex_stop_hook_dispatcher.is_truthy`` /
``rvf_analyze_thread.codex_bin``），**刻意不反向 import 共享审查引擎**，以保持
``adapters → engine`` 零依赖边、无循环。引擎侧的中性 fork 路由骨架
（``run_codex_fork``）通过 import 本模块的少数公开入口
（``run_app_server_fork`` / ``app_server_fork_requests`` /
``parent_thread_name_from_app_server`` / ``can_connect_app_server_socket`` /
``select_existing_app_server_socket_for_metadata`` / ``path_is_relative_to``）来委派
codex 后端执行。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# ── 自带的 trivial 工具副本（与引擎同源；本仓既有惯例 = 每个脚本自含，避免
#    adapters → engine import 造成循环）。 ──
def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def is_falsey(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"0", "false", "no", "n", "off", "skip", "disabled"}


def codex_bin() -> str:
    return os.environ.get("CODEX_RVF_CODEX_BIN", "codex")


# ── app-server / bridge 常量（从引擎上移，codex Desktop 控制面专属）。 ──
DEFAULT_APP_SERVER_CONTROL_SOCKET = (
    Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"
)
DEFAULT_BRIDGE_SOCKET = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.sock"
DEFAULT_BRIDGE_LOG = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.log"
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS = 8.0
DEFAULT_OPEN_GUI_FORK_ATTEMPTS = 3
DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS = 5
DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY = "auto"
APP_SERVER_CLIENT_INFO = {
    "name": "review-validate-fix-stop-hook",
    "title": "review-validate-fix Stop hook",
    "version": "0.1.0",
}


# ── 错误类型 ──
class AppServerError(RuntimeError):
    pass


class AppServerSocketSelectionError(AppServerError):
    def __init__(self, message: str, socket_selection: dict[str, Any]) -> None:
        super().__init__(message)
        self.socket_selection = socket_selection


# ── app-server / bridge / GUI-fork 函数（从引擎 4740-5823 原样抽出）。 ──
def app_server_fork_requests(
    *,
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
) -> list[dict[str, Any]]:
    fork_params: dict[str, Any] = {
        "threadId": parent_thread_id,
        "cwd": cwd,
        "excludeTurns": True,
        "persistExtendedHistory": True,
    }
    if parent_thread_path is not None:
        fork_params["path"] = str(parent_thread_path)
    if model:
        fork_params["model"] = model

    turn_params: dict[str, Any] = {
        "threadId": "<fork_thread_id>",
        "input": [{"type": "text", "text": prompt, "text_elements": []}],
        "cwd": cwd,
        "summary": "auto",
        "personality": None,
        "outputSchema": None,
    }
    if model:
        turn_params["model"] = model
    if reasoning_effort:
        turn_params["effort"] = reasoning_effort

    return [
        {"method": "thread/fork", "params": fork_params},
        {"method": "turn/start", "params": turn_params},
    ]


class AppServerWebSocket:
    def __init__(self, socket_path: Path, timeout: float = 15) -> None:
        self.socket_path = socket_path
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(str(socket_path))
        self.recv_buffer = b""
        self.perform_handshake()
        self.next_id = 1
        self.notifications: list[dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass

    def perform_handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self.socket.sendall(request)

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise AppServerError("app-server websocket handshake closed")
            response += chunk
            if len(response) > 16384:
                raise AppServerError("app-server websocket handshake response too large")

        header_bytes, self.recv_buffer = response.split(b"\r\n\r\n", 1)
        header_text = header_bytes.decode("iso-8859-1")
        lines = header_text.split("\r\n")
        status_line = lines[0] if lines else ""
        if not status_line.startswith("HTTP/1.1 101") and not status_line.startswith(
            "HTTP/1.0 101"
        ):
            raise AppServerError(f"app-server websocket handshake failed: {status_line}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            raise AppServerError("app-server websocket handshake accept mismatch")

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        mask = os.urandom(4)
        if len(data) < 126:
            header = bytes([0x81, 0x80 | len(data)])
        elif len(data) < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", len(data))
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", len(data))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.socket.sendall(header + mask + masked)

    def recv_exact(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        if self.recv_buffer:
            chunk = self.recv_buffer[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
            self.recv_buffer = self.recv_buffer[len(chunk) :]
        while remaining > 0:
            chunk = self.socket.recv(remaining)
            if not chunk:
                raise AppServerError("app-server websocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv_json(self) -> dict[str, Any]:
        first, second = self.recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.recv_exact(8))[0]

        mask = self.recv_exact(4) if second & 0x80 else None
        payload = self.recv_exact(length)
        if mask is not None:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

        if opcode == 0x8:
            raise AppServerError("app-server websocket closed")
        if opcode == 0x9:
            self.send_pong(payload)
            return self.recv_json()
        if opcode != 0x1:
            raise AppServerError(f"unsupported websocket opcode {opcode}")
        return json.loads(payload.decode("utf-8"))

    def send_pong(self, payload: bytes) -> None:
        if len(payload) >= 126:
            return
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)

    def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_json(payload)
        while True:
            response = self.recv_json()
            if response.get("id") != request_id:
                self.notifications.append(response)
                continue
            error = response.get("error")
            if error:
                raise AppServerError(json.dumps(error, ensure_ascii=False))
            result = response.get("result")
            return result if isinstance(result, dict) else {}


def can_connect_app_server_socket(socket_path: Path) -> bool:
    return bool(probe_app_server_socket(socket_path).get("protocol_ok"))


def app_server_probe_ready(probe: dict[str, Any]) -> bool:
    if "protocol_ok" in probe:
        return bool(probe.get("protocol_ok"))
    return bool(probe.get("connect_ok"))


def probe_app_server_socket(socket_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(socket_path),
        "exists": socket_path.exists(),
        "parent_exists": socket_path.parent.exists(),
        "is_socket": False,
        "connect_ok": False,
        "protocol_ok": False,
        "reason": None,
    }
    try:
        if socket_path.exists():
            result["is_socket"] = socket_path.is_socket()
    except OSError as exc:
        result.update(
            {
                "reason": "stat-error",
                "error": f"{type(exc).__name__}: {exc}",
                "errno": getattr(exc, "errno", None),
            }
        )
        return result

    if not result["exists"]:
        result["reason"] = "missing"
        return result
    if not result["is_socket"]:
        result["reason"] = "not-a-socket"
        return result

    try:
        probe = AppServerWebSocket(socket_path, timeout=0.5)
        result["connect_ok"] = True
        result["protocol_ok"] = True
        result["reason"] = "websocket-ok"
        return result
    except AppServerError as exc:
        result.update(
            {
                "connect_ok": True,
                "reason": "websocket-failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return result
    except OSError as exc:
        result.update(
            {
                "reason": "connect-failed",
                "error": f"{type(exc).__name__}: {exc}",
                "errno": getattr(exc, "errno", None),
            }
        )
        return result
    finally:
        try:
            probe.close()
        except UnboundLocalError:
            pass


def bridge_socket_path() -> Path:
    env_value = os.environ.get("CODEX_RVF_BRIDGE_SOCKET")
    if env_value and env_value.strip():
        return Path(env_value).expanduser().resolve()
    return DEFAULT_BRIDGE_SOCKET.resolve()


def bridge_log_path() -> Path:
    env_value = os.environ.get("CODEX_RVF_BRIDGE_LOG")
    if env_value and env_value.strip():
        return Path(env_value).expanduser().resolve()
    return DEFAULT_BRIDGE_LOG.resolve()


def select_app_server_socket() -> tuple[Path, str, dict[str, Any]]:
    explicit = os.environ.get("CODEX_RVF_APP_SERVER_SOCKET")
    if explicit and explicit.strip():
        socket_path = Path(explicit).expanduser().resolve()
        return socket_path, "explicit", {"explicit": probe_app_server_socket(socket_path)}

    desktop_probe = probe_app_server_socket(DEFAULT_APP_SERVER_CONTROL_SOCKET)
    if app_server_probe_ready(desktop_probe):
        return DEFAULT_APP_SERVER_CONTROL_SOCKET, "desktop-control", {
            "desktop_control": desktop_probe,
        }

    bridge_policy = bridge_gui_unverified_policy()
    if bridge_policy not in {"auto", "bridge"}:
        socket_selection = {
            "desktop_control": desktop_probe,
            "bridge": probe_app_server_socket(bridge_socket_path()),
            "bridge_policy": bridge_policy,
        }
        raise AppServerSocketSelectionError(
            "desktop-control unavailable; bridge fallback disabled by "
            f"CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY={bridge_policy}",
            socket_selection,
        )

    bridge_probe = probe_app_server_socket(bridge_socket_path())
    if bridge_policy == "auto" and app_server_probe_ready(bridge_probe):
        return bridge_socket_path(), "bridge", {
            "desktop_control": desktop_probe,
            "bridge": bridge_probe,
            "bridge_policy": bridge_policy,
            "bridge_decision": "existing-bridge-connect-ok",
        }

    try:
        socket_path = ensure_bridge_app_server()
    except Exception as exc:
        socket_selection = {
            "desktop_control": desktop_probe,
            "bridge": probe_app_server_socket(bridge_socket_path()),
            "bridge_policy": bridge_policy,
        }
        raise AppServerSocketSelectionError(
            f"desktop-control unavailable and bridge fallback failed: {exc}",
            socket_selection,
        ) from exc
    return socket_path, "bridge", {
        "desktop_control": desktop_probe,
        "bridge": probe_app_server_socket(socket_path),
        "bridge_policy": bridge_policy,
    }


def select_existing_app_server_socket_for_metadata() -> tuple[Path, str, dict[str, Any]]:
    explicit = os.environ.get("CODEX_RVF_APP_SERVER_SOCKET")
    if explicit and explicit.strip():
        socket_path = Path(explicit).expanduser().resolve()
        probe = probe_app_server_socket(socket_path)
        if app_server_probe_ready(probe):
            return socket_path, "explicit", {"explicit": probe}
        raise AppServerSocketSelectionError(
            "explicit app-server socket unavailable for metadata lookup",
            {"explicit": probe},
        )

    desktop_probe = probe_app_server_socket(DEFAULT_APP_SERVER_CONTROL_SOCKET)
    if app_server_probe_ready(desktop_probe):
        return DEFAULT_APP_SERVER_CONTROL_SOCKET, "desktop-control", {
            "desktop_control": desktop_probe,
        }

    bridge_path = bridge_socket_path()
    bridge_probe = probe_app_server_socket(bridge_path)
    if app_server_probe_ready(bridge_probe):
        return bridge_path, "bridge", {
            "desktop_control": desktop_probe,
            "bridge": bridge_probe,
        }

    raise AppServerSocketSelectionError(
        "no existing app-server socket available for metadata lookup",
        {"desktop_control": desktop_probe, "bridge": bridge_probe},
    )


def bridge_gui_unverified_policy() -> str:
    if is_truthy(os.environ.get("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER")):
        return "bridge"
    raw = os.environ.get(
        "CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY",
        DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY,
    )
    value = raw.strip().lower() if raw else DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY
    if value in {"auto", "detect", "fallback"}:
        return "auto"
    if value in {"bridge", "allow", "allowed", "fork", "app-server", "appserver"}:
        return "bridge"
    if value in {"manual", "prepare", "prepared", "log-only"}:
        return "manual"
    if value in {"fail", "error"}:
        return "fail"
    return "report"


def ensure_bridge_app_server(restart_existing: bool = False) -> Path:
    socket_path = bridge_socket_path()
    if (
        not restart_existing
        and socket_path.exists()
        and can_connect_app_server_socket(socket_path)
    ):
        return socket_path

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if restart_existing:
        stop_existing_bridge_app_servers(socket_path)
    if socket_path.exists():
        socket_path.unlink()

    log_path = bridge_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            [
                codex_bin(),
                "app-server",
                "--listen",
                f"unix://{socket_path}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if socket_path.exists() and can_connect_app_server_socket(socket_path):
            return socket_path
        time.sleep(0.1)
    raise AppServerError(f"app-server bridge socket did not become ready: {socket_path}")


def bridge_app_server_listener_pids(socket_path: Path) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-U"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode not in {0, 1}:
        return []

    pids: list[int] = []
    socket_text = str(socket_path)
    for line in result.stdout.splitlines():
        if socket_text not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid in pids:
            continue
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        command_text = command.stdout.strip()
        if (
            command.returncode == 0
            and "codex app-server" in command_text
            and f"unix://{socket_text}" in command_text
        ):
            pids.append(pid)
    return pids


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_existing_bridge_app_servers(socket_path: Path) -> dict[str, Any]:
    pids = [pid for pid in bridge_app_server_listener_pids(socket_path) if pid != os.getpid()]
    stopped: list[int] = []
    failed: list[dict[str, Any]] = []
    force_killed: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, 15)
            stopped.append(pid)
        except ProcessLookupError:
            stopped.append(pid)
        except OSError as exc:
            failed.append({"pid": pid, "error": str(exc)})

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        alive = [pid for pid in stopped if process_is_running(pid)]
        if not alive:
            break
        time.sleep(0.1)

    still_running = [pid for pid in stopped if process_is_running(pid)]
    if still_running and not is_falsey(
        os.environ.get("CODEX_RVF_BRIDGE_FORCE_KILL_ON_RESTART", "1")
    ):
        for pid in still_running:
            try:
                os.kill(pid, 9)
                force_killed.append(pid)
            except ProcessLookupError:
                force_killed.append(pid)
            except OSError as exc:
                failed.append({"pid": pid, "signal": 9, "error": str(exc)})
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            alive = [pid for pid in force_killed if process_is_running(pid)]
            if not alive:
                break
            time.sleep(0.1)
        still_running = [pid for pid in stopped if process_is_running(pid)]
    return {
        "pids": pids,
        "stopped": stopped,
        "force_killed": force_killed,
        "failed": failed,
        "still_running": still_running,
    }


def bridge_retry_after_app_server_error(error: Exception) -> bool:
    if is_falsey(os.environ.get("CODEX_RVF_BRIDGE_RETRY_ON_APP_SERVER_ERROR")):
        return False
    text = str(error).lower()
    return (
        "failed to load configuration" in text
        or "operation not permitted" in text
        or "os error 1" in text
    )


def maybe_open_fork_in_codex(fork_thread_id: str) -> bool:
    if os.environ.get("CODEX_RVF_OPEN_GUI_FORK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    if sys.platform != "darwin":
        return False
    url = f"codex://local/{fork_thread_id}"
    try:
        subprocess.Popen(
            ["open", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def open_gui_fork_unavailable_reason() -> str | None:
    if os.environ.get("CODEX_RVF_OPEN_GUI_FORK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return "disabled"
    if sys.platform != "darwin":
        return "unsupported-platform"
    return None


def open_gui_fork_attempts() -> int:
    raw = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    if raw is None or not raw.strip():
        return DEFAULT_OPEN_GUI_FORK_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_OPEN_GUI_FORK_ATTEMPTS


def open_gui_fork_retry_delay_seconds() -> float:
    raw = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS


def open_fork_in_codex_with_retries(fork_thread_id: str) -> dict[str, Any]:
    max_attempts = open_gui_fork_attempts()
    retry_delay = open_gui_fork_retry_delay_seconds()
    attempts: list[dict[str, Any]] = []
    started = time.monotonic()
    unavailable_reason = open_gui_fork_unavailable_reason()
    if unavailable_reason is not None:
        opened = maybe_open_fork_in_codex(fork_thread_id)
        attempts.append(
            {
                "attempt": 1,
                "opened": opened,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        return {
            "opened": opened,
            "attempts": attempts,
            "retry_delay_seconds": retry_delay,
            "skipped_retries_reason": unavailable_reason,
        }
    for attempt in range(1, max_attempts + 1):
        opened = maybe_open_fork_in_codex(fork_thread_id)
        attempts.append(
            {
                "attempt": attempt,
                "opened": opened,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        if opened:
            break
        if attempt < max_attempts:
            time.sleep(retry_delay)
    return {
        "opened": any(item["opened"] for item in attempts),
        "attempts": attempts,
        "retry_delay_seconds": retry_delay,
    }


def fork_visibility_timeout_seconds() -> float:
    raw = os.environ.get("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def notification_thread_path(
    notifications: list[dict[str, Any]],
    thread_id: str,
) -> str | None:
    for notification in reversed(notifications):
        if notification.get("method") != "thread/started":
            continue
        params = notification.get("params")
        thread = params.get("thread") if isinstance(params, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            continue
        path = thread.get("path")
        if isinstance(path, str) and path:
            return path
    return None


def fork_session_visibility(
    thread_id: str,
    hinted_path: str | None,
) -> dict[str, Any]:
    active_paths: list[str] = []
    hinted = Path(hinted_path).expanduser() if hinted_path else None
    hinted_exists = False
    if hinted is not None:
        try:
            hinted_exists = hinted.exists()
        except OSError:
            hinted_exists = False
        if hinted_exists and path_is_relative_to(hinted, DEFAULT_CODEX_SESSIONS_DIR):
            active_paths.append(str(hinted))

    if not active_paths and DEFAULT_CODEX_SESSIONS_DIR.exists():
        active_paths.extend(
            str(path)
            for path in DEFAULT_CODEX_SESSIONS_DIR.rglob(f"*{thread_id}*.jsonl")
        )

    location = "active" if active_paths else "missing"

    return {
        "thread_id": thread_id,
        "hinted_path": str(hinted) if hinted is not None else None,
        "hinted_exists": hinted_exists,
        "location": location,
        "active_paths": active_paths,
    }


def wait_for_fork_session_visibility(
    thread_id: str,
    hinted_path: str | None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    timeout = fork_visibility_timeout_seconds() if timeout_seconds is None else timeout_seconds
    deadline = time.monotonic() + timeout
    checks = 0
    while True:
        checks += 1
        visibility = fork_session_visibility(thread_id, hinted_path)
        visibility["checks"] = checks
        visibility["timeout_seconds"] = timeout
        if visibility["location"] != "missing" or time.monotonic() >= deadline:
            return visibility
        time.sleep(0.1)


def compact_app_server_thread(thread: dict[str, Any]) -> dict[str, Any]:
    status = thread.get("status")
    return {
        "id": thread.get("id"),
        "name": thread.get("name"),
        "path": thread.get("path"),
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt"),
        "status": status if isinstance(status, dict) else None,
    }


def initialize_app_server_client(client: Any) -> None:
    client.request(
        "initialize",
        {
            "clientInfo": APP_SERVER_CLIENT_INFO,
            "capabilities": {
                "experimentalApi": True,
                "optOutNotificationMethods": [],
            },
        },
    )
    send_json = getattr(client, "send_json", None)
    if callable(send_json):
        send_json({"method": "initialized"})


def request_app_server_diagnostic(
    client: AppServerWebSocket,
    method: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        return {"ok": True, "result": client.request(method, params)}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def app_server_thread_visibility_diagnostics(
    client: AppServerWebSocket,
    thread_id: str,
    cwd: str | None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"thread_id": thread_id}

    read_probe = request_app_server_diagnostic(
        client,
        "thread/read",
        {"threadId": thread_id, "includeTurns": False},
    )
    if read_probe.get("ok"):
        result = read_probe.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        read_probe = {
            "ok": True,
            "contains_thread": isinstance(thread, dict) and thread.get("id") == thread_id,
            "thread": compact_app_server_thread(thread) if isinstance(thread, dict) else None,
        }
    diagnostics["thread_read"] = read_probe

    list_params: dict[str, Any] = {
        "limit": 50,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "archived": False,
        "useStateDbOnly": False,
    }
    if cwd:
        list_params["cwd"] = cwd
    list_probe = request_app_server_diagnostic(client, "thread/list", list_params)
    if list_probe.get("ok"):
        result = list_probe.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        threads = data if isinstance(data, list) else []
        matches = [
            compact_app_server_thread(thread)
            for thread in threads
            if isinstance(thread, dict) and thread.get("id") == thread_id
        ]
        list_probe = {
            "ok": True,
            "params": list_params,
            "contains_thread": bool(matches),
            "matches": matches,
            "returned": len(threads),
            "nextCursor": result.get("nextCursor") if isinstance(result, dict) else None,
        }
    diagnostics["thread_list"] = list_probe

    loaded_probe = request_app_server_diagnostic(
        client,
        "thread/loaded/list",
        {"limit": 200},
    )
    if loaded_probe.get("ok"):
        result = loaded_probe.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        loaded_ids = (
            [item for item in data if isinstance(item, str)]
            if isinstance(data, list)
            else []
        )
        loaded_probe = {
            "ok": True,
            "contains_thread": thread_id in loaded_ids,
            "returned": len(loaded_ids),
            "nextCursor": result.get("nextCursor") if isinstance(result, dict) else None,
        }
    diagnostics["thread_loaded_list"] = loaded_probe

    return diagnostics


def app_server_thread_name_from_result(result: dict[str, Any], thread_id: str) -> str | None:
    thread = result.get("thread")
    if isinstance(thread, dict) and thread.get("id") == thread_id:
        name = thread.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def app_server_thread_metadata_from_result(result: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    thread = result.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return None
    name = thread.get("name")
    return {"thread_found": True, "name": name.strip() if isinstance(name, str) and name.strip() else None}


def parent_thread_name_from_app_server(
    thread_id: str | None,
    cwd: str | None,
) -> dict[str, Any]:
    if not thread_id:
        return {"name": None, "thread_found": False, "source": "missing-thread-id"}
    try:
        socket_path, socket_source, socket_selection = select_existing_app_server_socket_for_metadata()
    except Exception as exc:
        return {
            "name": None,
            "thread_found": False,
            "source": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        client = AppServerWebSocket(socket_path)
    except Exception as exc:
        return {
            "name": None,
            "thread_found": False,
            "source": socket_source,
            "socket_path": str(socket_path),
            "socket_selection": socket_selection,
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        initialize_app_server_client(client)
        read_error = None
        try:
            read_result = client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
            read_metadata = app_server_thread_metadata_from_result(read_result, thread_id)
            name = read_metadata.get("name") if read_metadata else None
        except Exception as exc:
            read_metadata = None
            name = None
            read_error = f"{type(exc).__name__}: {exc}"
        if name:
            return {
                "name": name,
                "thread_found": True,
                "source": socket_source,
                "method": "thread/read",
                "socket_path": str(socket_path),
                "socket_selection": socket_selection,
            }
        if read_metadata is not None:
            return {
                "name": None,
                "thread_found": True,
                "source": socket_source,
                "method": "thread/read",
                "socket_path": str(socket_path),
                "socket_selection": socket_selection,
                "reason": "thread-unnamed",
            }

        list_params: dict[str, Any] = {
            "limit": 50,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "archived": False,
            "useStateDbOnly": False,
        }
        if cwd:
            list_params["cwd"] = cwd
        list_result = client.request("thread/list", list_params)
        data = list_result.get("data")
        threads = data if isinstance(data, list) else []
        for thread in threads:
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                continue
            name_value = thread.get("name")
            name = name_value.strip() if isinstance(name_value, str) else ""
            lookup = {
                "name": name or None,
                "thread_found": True,
                "source": socket_source,
                "method": "thread/list",
                "socket_path": str(socket_path),
                "socket_selection": socket_selection,
            }
            if not name:
                lookup["reason"] = "thread-unnamed"
            return lookup
        return {
            "name": None,
            "thread_found": False,
            "source": socket_source,
            "method": "thread/list",
            "socket_path": str(socket_path),
            "socket_selection": socket_selection,
            "reason": "thread-not-found",
            "thread_read_error": read_error,
        }
    except Exception as exc:
        return {
            "name": None,
            "thread_found": False,
            "source": socket_source,
            "socket_path": str(socket_path),
            "socket_selection": socket_selection,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        client.close()


def run_app_server_fork_with_socket(
    *,
    socket_path: Path,
    socket_source: str,
    socket_selection: dict[str, Any],
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    bridge_retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = AppServerWebSocket(socket_path)
    try:
        initialize_app_server_client(client)
        requests = app_server_fork_requests(
            parent_thread_id=parent_thread_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        fork_result = client.request("thread/fork", requests[0]["params"])
        fork_thread = fork_result.get("thread")
        if not isinstance(fork_thread, dict) or not isinstance(fork_thread.get("id"), str):
            raise AppServerError("thread/fork did not return a fork thread id")
        fork_thread_id = fork_thread["id"]
        fork_thread_path = (
            fork_thread.get("path") if isinstance(fork_thread.get("path"), str) else None
        )
        turn_params = dict(requests[1]["params"])
        turn_params["threadId"] = fork_thread_id
        turn_result = client.request("turn/start", turn_params)
        turn = turn_result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        session_hint = fork_thread_path or notification_thread_path(
            client.notifications,
            fork_thread_id,
        )
        session_visibility = wait_for_fork_session_visibility(fork_thread_id, session_hint)
        app_server_visibility = app_server_thread_visibility_diagnostics(
            client,
            fork_thread_id,
            cwd,
        )
        open_result = open_fork_in_codex_with_retries(fork_thread_id)
        session_location = session_visibility.get("location")
        gui_visibility = "unverified-bridge-only"
        if socket_source == "desktop-control":
            gui_visibility = (
                "verified"
                if session_location == "active"
                else f"unverified-session-{session_location or 'unknown'}"
            )
        result = {
            "status": "app-server-started",
            "socket_path": str(socket_path),
            "socket_source": socket_source,
            "socket_selection": socket_selection,
            "fork_thread_id": fork_thread_id,
            "fork_thread_path": fork_thread_path,
            "turn_id": turn_id,
            "session_visibility": session_visibility,
            "app_server_visibility": app_server_visibility,
            "gui_visibility": gui_visibility,
            "opened_gui_deeplink": open_result["opened"],
            "open_gui_deeplink": open_result,
            "notifications": client.notifications[-20:],
        }
        if bridge_retry is not None:
            result["bridge_retry"] = bridge_retry
        return result
    finally:
        client.close()


def run_app_server_fork(
    *,
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    log_path: Path,
) -> dict[str, Any]:
    socket_path, socket_source, socket_selection = select_app_server_socket()
    try:
        return run_app_server_fork_with_socket(
            socket_path=socket_path,
            socket_source=socket_source,
            socket_selection=socket_selection,
            parent_thread_id=parent_thread_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    except AppServerError as first_error:
        if socket_source != "bridge" or not bridge_retry_after_app_server_error(first_error):
            raise

        retry_socket = ensure_bridge_app_server(restart_existing=True)
        retry_selection = {
            "desktop_control": socket_selection.get("desktop_control"),
            "bridge": probe_app_server_socket(retry_socket),
            "bridge_policy": socket_selection.get("bridge_policy", "auto"),
            "bridge_decision": "restarted-after-app-server-error",
        }
        retry = {
            "reason": "app-server-error",
            "first_error": f"{type(first_error).__name__}: {first_error}",
            "first_socket_path": str(socket_path),
            "first_socket_selection": socket_selection,
            "restarted_socket_path": str(retry_socket),
        }
        return run_app_server_fork_with_socket(
            socket_path=retry_socket,
            socket_source="bridge",
            socket_selection=retry_selection,
            parent_thread_id=parent_thread_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            bridge_retry=retry,
        )
