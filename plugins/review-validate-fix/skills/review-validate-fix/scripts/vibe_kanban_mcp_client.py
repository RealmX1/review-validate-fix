#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse


DEFAULT_MCP_CMD = "npx -y vibe-kanban@0.1.44 --mcp"
DEFAULT_START_CMD = "npx -y vibe-kanban@0.1.44"
DEFAULT_START_TIMEOUT_SECONDS = 90.0
DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_TMUX_SESSION = "rvf-vibe-kanban"
DEFAULT_CREATE_TOOL = "create_issue"
DEFAULT_UPDATE_TOOL = "update_issue"
DEFAULT_LOCAL_WORKSPACE_STATUS_PREFIXES = {
    "queued": "RVF queued",
    "running": "RVF running",
    "completed": "RVF completed",
    "failed": "RVF failed",
    "cancelled": "RVF cancelled",
}
LOCAL_NO_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_STATUS_ALIASES = {
    "queued": "To do",
    "running": "In progress",
    "completed": "Done",
    "failed": "Cancelled",
    "cancelled": "Cancelled",
}


class McpError(RuntimeError):
    pass


def _read_message(stream: Any, *, timeout_seconds: float | None = None) -> dict[str, Any]:
    deadline = None
    if timeout_seconds is not None and timeout_seconds > 0:
        deadline = time.monotonic() + timeout_seconds
    line = _readline(stream, deadline=deadline)
    if not line:
        raise EOFError("MCP server closed stdout")
    if line.lower().startswith(b"content-length:"):
        return _read_content_length_message(stream, line, deadline=deadline)
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise McpError(f"MCP response was not JSON-lines: {line!r}") from exc


def _read_content_length_message(stream: Any, first_line: bytes, *, deadline: float | None) -> dict[str, Any]:
    content_length: int | None = _content_length_from_header(first_line)
    while True:
        line = _readline(stream, deadline=deadline)
        if not line:
            raise EOFError("MCP server closed stdout")
        if line in {b"\r\n", b"\n"}:
            break
        length = _content_length_from_header(line)
        if length is not None:
            content_length = length
    if content_length is None:
        raise McpError("MCP response missing Content-Length header")
    body = _read_exact(stream, content_length, deadline=deadline)
    if len(body) != content_length:
        raise EOFError("MCP server closed stdout mid-message")
    return json.loads(body.decode("utf-8"))


def _readline(stream: Any, *, deadline: float | None) -> bytes:
    if deadline is None:
        return stream.readline()
    chunks: list[bytes] = []
    while True:
        chunk = _read_available(stream, 1, deadline=deadline)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        if chunk == b"\n":
            return b"".join(chunks)


def _read_exact(stream: Any, size: int, *, deadline: float | None) -> bytes:
    if deadline is None:
        return stream.read(size)
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = _read_available(stream, remaining, deadline=deadline)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_available(stream: Any, size: int, *, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out waiting for MCP stdout")
    ready, _, _ = select.select([stream], [], [], remaining)
    if not ready:
        raise TimeoutError("timed out waiting for MCP stdout")
    return os.read(stream.fileno(), size)


def _content_length_from_header(line: bytes) -> int | None:
    name, _, value = line.decode("ascii", errors="replace").partition(":")
    if name.lower() != "content-length":
        return None
    return int(value.strip())


def _write_message(stream: Any, payload: dict[str, Any]) -> None:
    body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    stream.write(body)
    stream.flush()


def _extract_text_content(result: dict[str, Any]) -> str | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts) if parts else None


def _parse_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        text = _extract_text_content(result)
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    _raise_tool_error(parsed)
                    return parsed
            except json.JSONDecodeError:
                return {"text": text}
        _raise_tool_error(result)
        return result
    return {"result": result}


def _raise_tool_error(payload: dict[str, Any]) -> None:
    if payload.get("success") is not False:
        return
    error = str(payload.get("error") or payload.get("message") or "Vibe-Kanban tool failed")
    details = payload.get("details")
    if details:
        error = f"{error}: {details}"
    raise McpError(error)


def _find_tool(tools: list[dict[str, Any]], candidates: list[str]) -> dict[str, Any]:
    available = [tool.get("name") for tool in tools if isinstance(tool.get("name"), str)]
    for candidate in candidates:
        if candidate in available:
            for tool in tools:
                if tool.get("name") == candidate:
                    return tool
    raise McpError(f"none of the requested tools are available: {candidates}; available={available}")


def _schema_properties(tool: dict[str, Any]) -> set[str] | None:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    return {key for key in properties if isinstance(key, str)}


def _tool_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    properties = _schema_properties(tool)
    if not properties:
        return arguments
    filtered = {key: value for key, value in arguments.items() if key in properties}
    return filtered or arguments


def _payload_list(payload: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        nested = _payload_list(data, keys)
        if nested:
            return nested
    return []


def _normalize_id(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, int):
            return str(value)
    for key in ("project", "repo", "issue", "task", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            nested = _normalize_id(value, keys)
            if nested:
                return nested
    return None


def _repo_name(repo: Path) -> str:
    return repo.resolve().name.lower()


def _path_matches(value: Any, repo: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return Path(value).expanduser().resolve() == repo.resolve()
    except OSError:
        return False


def _name_matches(value: Any, repo: Path) -> bool:
    return isinstance(value, str) and value.strip().lower() == _repo_name(repo)


def _repo_record_matches(record: dict[str, Any], repo: Path) -> bool:
    for key in (
        "path",
        "repo_path",
        "local_path",
        "working_dir",
        "default_working_dir",
        "git_repo_path",
        "repository_path",
    ):
        if _path_matches(record.get(key), repo):
            return True
    for key in ("name", "repo_name", "display_name"):
        if _name_matches(record.get(key), repo):
            return True
    return False


def _project_record_matches(record: dict[str, Any], repo: Path) -> bool:
    for key in ("name", "display_name", "repo_name", "slug"):
        if _name_matches(record.get(key), repo):
            return True
    for key in ("path", "repo_path", "local_path", "working_dir", "default_working_dir"):
        if _path_matches(record.get(key), repo):
            return True
    return False


def _project_id_from_record(record: dict[str, Any]) -> str | None:
    return _normalize_id(record, ("project_id", "projectId", "id", "remote_project_id", "remoteProjectId"))


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _start_log_path() -> Path:
    root = Path(os.environ.get("CODEX_RVF_VK_LOG_DIR", Path.home() / ".codex" / "rvf"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "vibe-kanban.log"


def _normalize_backend_url(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return f"http://127.0.0.1:{text}"
    if re.fullmatch(r"(?:127\.0\.0\.1|localhost):\d+", text):
        return f"http://{text}"
    if text.startswith(("http://", "https://")):
        return text.rstrip("/")
    return None


def _append_unique(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def _port_file_candidates() -> list[Path]:
    roots: list[Path] = []
    for value in (os.environ.get("TMPDIR"), tempfile.gettempdir()):
        if value:
            root = Path(value).expanduser()
            if root not in roots:
                roots.append(root)
    return [root / "vibe-kanban" / "vibe-kanban.port" for root in roots]


def _backend_urls_from_port_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    urls: list[str] = []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key in ("backend_port", "backendPort", "main_port", "mainPort", "port"):
            value = payload.get(key)
            if isinstance(value, int):
                _append_unique(urls, _normalize_backend_url(str(value)))
            elif isinstance(value, str):
                _append_unique(urls, _normalize_backend_url(value))
        for key in ("backend_url", "backendUrl", "url"):
            value = payload.get(key)
            if isinstance(value, str):
                _append_unique(urls, _normalize_backend_url(value))
    for value in re.findall(r"\b\d{4,5}\b", text):
        _append_unique(urls, _normalize_backend_url(value))
    return [url for url in urls if _backend_url_is_reachable(url)]


def _backend_url_is_reachable(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.25):
            return True
    except OSError:
        return False


def _parse_lsof_listen_ports(output: str) -> list[int]:
    ports: list[int] = []
    command = ""
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        marker = raw_line[0]
        value = raw_line[1:]
        if marker == "p":
            command = ""
        elif marker == "c":
            command = value.lower()
        elif marker == "n":
            if "vibe-kanb" not in command or "mcp" in command:
                continue
            match = re.search(r"(?:127\.0\.0\.1|localhost|\[::1\]|\*):(\d+)", value)
            if not match:
                continue
            port = int(match.group(1))
            if port not in ports:
                ports.append(port)
    return ports


def _backend_urls_from_lsof() -> list[str]:
    if shutil.which("lsof") is None:
        return []
    completed = subprocess.run(
        ["lsof", "-Pan", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        return []
    urls: list[str] = []
    for port in _parse_lsof_listen_ports(completed.stdout):
        _append_unique(urls, _normalize_backend_url(str(port)))
    return urls


def discover_backend_urls() -> list[str]:
    urls: list[str] = []
    for env_name in ("CODEX_RVF_VK_BACKEND_URL", "VIBE_BACKEND_URL"):
        _append_unique(urls, _normalize_backend_url(os.environ.get(env_name, "")))
    for path in _port_file_candidates():
        for url in _backend_urls_from_port_file(path):
            _append_unique(urls, url)
    for url in _backend_urls_from_lsof():
        _append_unique(urls, url)
    return urls


def _candidate_backend_urls(backend_url: str | None = None) -> list[str | None]:
    candidates: list[str | None] = []
    normalized = _normalize_backend_url(backend_url or "")
    if normalized:
        candidates.append(normalized)
    else:
        for url in discover_backend_urls():
            if url not in candidates:
                candidates.append(url)
        candidates.append(None)
    return candidates


def normalize_issue_status(status: str) -> str:
    text = status.strip()
    if not text:
        return text
    env_key = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
    override = os.environ.get(f"CODEX_RVF_VK_STATUS_{env_key}", "").strip()
    if override:
        return override
    return DEFAULT_STATUS_ALIASES.get(text.lower(), text)


def _local_no_proxy_env(env: dict[str, str]) -> None:
    for key in ("NO_PROXY", "no_proxy"):
        existing = [item.strip() for item in env.get(key, "").split(",") if item.strip()]
        for host in LOCAL_NO_PROXY_HOSTS:
            if host not in existing:
                existing.append(host)
        env[key] = ",".join(existing)


class McpClient:
    def __init__(
        self,
        command: str,
        *,
        backend_url: str | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self.command = command
        self.backend_url = _normalize_backend_url(backend_url or "")
        if request_timeout_seconds is None:
            request_timeout_seconds = float(
                os.environ.get("CODEX_RVF_VK_MCP_REQUEST_TIMEOUT", DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS)
            )
        self.request_timeout_seconds = request_timeout_seconds
        self.process: subprocess.Popen[bytes] | None = None
        self.next_id = 1

    def __enter__(self) -> "McpClient":
        args = shlex.split(self.command)
        if not args:
            raise McpError("empty MCP command")
        env = os.environ.copy()
        _local_no_proxy_env(env)
        if self.backend_url:
            env["VIBE_BACKEND_URL"] = self.backend_url
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "review-validate-fix-vibe-kanban",
                    "version": "0.1.0",
                },
            },
        )
        self.notify("notifications/initialized", {})
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except Exception:
            self.process.kill()

    def _stdio(self) -> tuple[Any, Any]:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise McpError("MCP process is not running")
        return self.process.stdin, self.process.stdout

    def _stderr_tail(self) -> str:
        if self.process is None or self.process.stderr is None:
            return ""
        if self.process.poll() is None:
            return ""
        try:
            data = self.process.stderr.read()
        except Exception:
            return ""
        return data.decode("utf-8", errors="replace").strip()

    def _terminate_after_timeout(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=1)
        except Exception:
            self.process.kill()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        stdin, _ = self._stdio()
        _write_message(stdin, {"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        stdin, stdout = self._stdio()
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        _write_message(stdin, payload)
        request_deadline = None
        if self.request_timeout_seconds is not None and self.request_timeout_seconds > 0:
            request_deadline = time.monotonic() + self.request_timeout_seconds
        while True:
            try:
                read_timeout = None
                if request_deadline is not None:
                    read_timeout = request_deadline - time.monotonic()
                    if read_timeout <= 0:
                        raise TimeoutError("timed out waiting for MCP stdout")
                response = _read_message(stdout, timeout_seconds=read_timeout)
            except TimeoutError as exc:
                self._terminate_after_timeout()
                raise McpError(
                    f"MCP {method} timed out after {self.request_timeout_seconds:g}s waiting for stdout"
                ) from exc
            except EOFError as exc:
                stderr = self._stderr_tail()
                detail = f"; stderr={stderr}" if stderr else ""
                raise McpError(f"MCP server exited before responding to {method}{detail}") from exc
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise McpError(f"MCP {method} failed: {response['error']}")
            return response.get("result")

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpError(f"invalid tools/list response: {result!r}")
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        return _parse_result(result)


def _summarize_backend(backend_url: str | None) -> str:
    return backend_url or "default-port-file"


def _api_url(backend_url: str, path: str) -> str:
    normalized = _normalize_backend_url(backend_url)
    if not normalized:
        raise McpError(f"invalid Vibe-Kanban backend URL: {backend_url!r}")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{normalized}{suffix}"


def _is_local_backend_url(backend_url: str) -> bool:
    hostname = urlparse(backend_url).hostname
    return hostname in LOCAL_NO_PROXY_HOSTS


def _open_api_request(request: urllib_request.Request, *, backend_url: str) -> Any:
    if _is_local_backend_url(backend_url):
        opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
        return opener.open(request, timeout=10)
    return urllib_request.urlopen(request, timeout=10)


def _api_request(
    backend_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib_request.Request(
        _api_url(backend_url, path),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with _open_api_request(request, backend_url=backend_url) as response:
            raw = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise McpError(f"Vibe-Kanban API {method} {path} failed with HTTP {exc.code}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise McpError(f"Vibe-Kanban API {method} {path} failed: {exc.reason}") from exc

    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise McpError(f"Vibe-Kanban API {method} {path} returned non-JSON: {raw!r}") from exc
    if isinstance(parsed, dict):
        if parsed.get("success") is False:
            message = parsed.get("message") or parsed.get("error_data") or "Vibe-Kanban API request failed"
            raise McpError(str(message))
        return parsed
    raise McpError(f"Vibe-Kanban API {method} {path} returned invalid JSON payload: {parsed!r}")


def _api_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _workspace_id_from_record(record: dict[str, Any]) -> str | None:
    return _normalize_id(record, ("workspace_id", "workspaceId", "id"))


def format_workspace_name(status: str, title: str) -> str:
    normalized_status = status.strip().lower()
    prefix = DEFAULT_LOCAL_WORKSPACE_STATUS_PREFIXES.get(normalized_status, f"RVF {normalized_status or 'status'}")
    base = title.strip() or "RVF run"
    return f"{prefix}: {base}"


def try_list_tools(mcp_cmd: str, *, backend_url: str | None = None) -> list[dict[str, Any]]:
    with McpClient(mcp_cmd, backend_url=backend_url) as client:
        return client.list_tools()


def probe_mcp(mcp_cmd: str, *, backend_url: str | None = None) -> dict[str, Any]:
    with McpClient(mcp_cmd, backend_url=backend_url) as client:
        tools = client.list_tools()
        tool_names = [str(tool.get("name")) for tool in tools if isinstance(tool.get("name"), str)]
        if "list_repos" in tool_names:
            tool = _find_tool(tools, ["list_repos"])
            client.call_tool(str(tool["name"]), _tool_arguments(tool, {}))
        return {
            "backend_url": backend_url,
            "tools": tool_names,
        }


def probe_mcp_candidates(mcp_cmd: str, *, backend_url: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    for candidate in _candidate_backend_urls(backend_url):
        try:
            return probe_mcp(mcp_cmd, backend_url=candidate)
        except Exception as exc:
            errors.append(f"{_summarize_backend(candidate)}={type(exc).__name__}: {exc}")
    raise McpError("; ".join(errors) or "no Vibe-Kanban MCP backend candidates were available")


def _with_mcp_client(
    mcp_cmd: str,
    *,
    backend_url: str | None,
    callback: Any,
) -> Any:
    errors: list[str] = []
    for candidate in _candidate_backend_urls(backend_url):
        try:
            with McpClient(mcp_cmd, backend_url=candidate) as client:
                return callback(client, candidate)
        except Exception as exc:
            errors.append(f"{_summarize_backend(candidate)}={type(exc).__name__}: {exc}")
    raise McpError("; ".join(errors) or "no Vibe-Kanban MCP backend candidates were available")


def start_vibe_kanban_app(
    *,
    start_cmd: str,
    repo: Path | None,
    tmux_session: str,
    log_path: Path | None = None,
) -> dict[str, Any]:
    args = shlex.split(start_cmd)
    if not args:
        raise McpError("empty Vibe-Kanban start command")
    cwd = repo if repo is not None else Path.home()
    log_path = log_path or _start_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tmux = shutil.which("tmux")
    if tmux:
        has_session = subprocess.run(
            [tmux, "has-session", "-t", tmux_session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if has_session.returncode == 0:
            return {
                "started": False,
                "launcher": "tmux",
                "tmux_session": tmux_session,
                "log_path": str(log_path),
            }
        shell_command = (
            f"cd {shlex.quote(str(cwd))} && "
            f"exec {start_cmd} >> {shlex.quote(str(log_path))} 2>&1"
        )
        subprocess.run(
            [tmux, "new-session", "-d", "-s", tmux_session, "sh", "-lc", shell_command],
            check=True,
        )
        return {
            "started": True,
            "launcher": "tmux",
            "tmux_session": tmux_session,
            "log_path": str(log_path),
        }

    stdout = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {
        "started": True,
        "launcher": "popen",
        "pid": process.pid,
        "log_path": str(log_path),
    }


def ensure_vibe_kanban_mcp(
    *,
    mcp_cmd: str,
    backend_url: str | None = None,
    start_if_needed: bool,
    start_cmd: str,
    repo: Path | None,
    tmux_session: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        probe = probe_mcp_candidates(mcp_cmd, backend_url=backend_url)
        return {"available": True, "started": False, **probe}
    except Exception as initial_exc:
        if not start_if_needed:
            raise
        lock_path = Path(tempfile.gettempdir()) / "rvf-vibe-kanban-bootstrap.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        launcher_payload: dict[str, Any] | None = None
        with lock_path.open("w", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                probe = probe_mcp_candidates(mcp_cmd, backend_url=backend_url)
                return {
                    "available": True,
                    "started": False,
                    **probe,
                    "initial_error": f"{type(initial_exc).__name__}: {initial_exc}",
                }
            except Exception:
                launcher_payload = start_vibe_kanban_app(
                    start_cmd=start_cmd,
                    repo=repo,
                    tmux_session=tmux_session,
                )

        deadline = time.monotonic() + timeout_seconds
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                probe = probe_mcp_candidates(mcp_cmd, backend_url=backend_url)
                return {
                    "available": True,
                    "started": bool(launcher_payload and launcher_payload.get("started")),
                    "launcher": launcher_payload,
                    **probe,
                    "initial_error": f"{type(initial_exc).__name__}: {initial_exc}",
                }
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(1.0)
        raise McpError(
            "Vibe-Kanban MCP did not become available after start; "
            f"initial={type(initial_exc).__name__}: {initial_exc}; last={last_error}; "
            f"launcher={launcher_payload}"
        )


def ensure_local_backend(
    *,
    mcp_cmd: str,
    backend_url: str | None,
    start_if_needed: bool,
    start_cmd: str,
    repo: Path | None,
    tmux_session: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    normalized = _normalize_backend_url(backend_url or "")
    if normalized and _backend_url_is_reachable(normalized):
        return {
            "available": True,
            "started": False,
            "backend_url": normalized,
            "source": "explicit_backend_url",
        }

    bootstrap = ensure_vibe_kanban_mcp(
        mcp_cmd=mcp_cmd,
        backend_url=backend_url,
        start_if_needed=start_if_needed,
        start_cmd=start_cmd,
        repo=repo,
        tmux_session=tmux_session,
        timeout_seconds=timeout_seconds,
    )
    resolved = bootstrap.get("backend_url")
    if isinstance(resolved, str) and _backend_url_is_reachable(resolved):
        return {**bootstrap, "backend_url": resolved, "source": "mcp_bootstrap"}
    for candidate in discover_backend_urls():
        if _backend_url_is_reachable(candidate):
            return {**bootstrap, "backend_url": candidate, "source": "discovered_backend_url"}
    if not start_if_needed:
        raise McpError("Vibe-Kanban local backend was not reachable after bootstrap")

    launcher_payload = start_vibe_kanban_app(
        start_cmd=start_cmd,
        repo=repo,
        tmux_session=tmux_session,
    )
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            probe = probe_mcp_candidates(mcp_cmd, backend_url=backend_url)
        except Exception as exc:
            probe = {}
            last_error = f"{type(exc).__name__}: {exc}"
        resolved = probe.get("backend_url")
        if isinstance(resolved, str) and _backend_url_is_reachable(resolved):
            return {
                **bootstrap,
                **probe,
                "backend_url": resolved,
                "source": "started_mcp_bootstrap",
                "started": bool(bootstrap.get("started")) or bool(launcher_payload.get("started")),
                "launcher": launcher_payload,
            }
        for candidate in discover_backend_urls():
            if _backend_url_is_reachable(candidate):
                return {
                    **bootstrap,
                    **probe,
                    "backend_url": candidate,
                    "source": "started_discovered_backend_url",
                    "started": bool(bootstrap.get("started")) or bool(launcher_payload.get("started")),
                    "launcher": launcher_payload,
                }
        time.sleep(1.0)
    raise McpError(
        "Vibe-Kanban local backend was not reachable after local app start; "
        f"last={last_error}; launcher={launcher_payload}"
    )


def upsert_workspace_notes(
    *,
    backend_url: str,
    workspace_id: str,
    description: str,
) -> dict[str, Any] | None:
    if not description:
        return None
    payload = {
        "payload": {
            "type": "WORKSPACE_NOTES",
            "data": {
                "content": description,
            },
        }
    }
    response = _api_request(
        backend_url,
        "PUT",
        f"/api/scratch/WORKSPACE_NOTES/{workspace_id}",
        payload,
    )
    return _api_data(response)


def create_local_workspace(
    *,
    mcp_cmd: str,
    backend_url: str | None = None,
    start_if_needed: bool,
    start_cmd: str,
    repo: Path | None,
    tmux_session: str,
    timeout_seconds: float,
    title: str,
    description: str,
    status: str = "queued",
) -> dict[str, Any]:
    bootstrap = ensure_local_backend(
        mcp_cmd=mcp_cmd,
        backend_url=backend_url,
        start_if_needed=start_if_needed,
        start_cmd=start_cmd,
        repo=repo,
        tmux_session=tmux_session,
        timeout_seconds=timeout_seconds,
    )
    resolved_backend_url = bootstrap.get("backend_url")
    if not isinstance(resolved_backend_url, str):
        raise McpError(f"Vibe-Kanban local backend bootstrap did not include backend_url: {bootstrap!r}")

    response = _api_request(
        resolved_backend_url,
        "POST",
        "/api/workspaces",
        {"name": format_workspace_name(status, title)},
    )
    workspace = _api_data(response)
    workspace_id = _workspace_id_from_record(workspace)
    if not workspace_id:
        raise McpError(f"Vibe-Kanban workspace creation did not include workspace id: {response!r}")

    notes: dict[str, Any] | None = None
    notes_error: str | None = None
    try:
        notes = upsert_workspace_notes(
            backend_url=resolved_backend_url,
            workspace_id=workspace_id,
            description=description,
        )
    except Exception as exc:
        notes_error = f"{type(exc).__name__}: {exc}"

    return {
        "workspace_id": workspace_id,
        "backend_url": resolved_backend_url,
        "workspace": workspace,
        "notes": notes,
        "notes_error": notes_error,
        "bootstrap": bootstrap,
        "tool": "local_api_create_workspace",
    }


def update_local_workspace(
    *,
    backend_url: str,
    workspace_id: str,
    title: str | None,
    description: str,
    status: str,
) -> dict[str, Any]:
    name = format_workspace_name(status, title or f"workspace {workspace_id}")
    response = _api_request(
        backend_url,
        "PUT",
        f"/api/workspaces/{workspace_id}",
        {"name": name},
    )
    workspace = _api_data(response)
    notes: dict[str, Any] | None = None
    notes_error: str | None = None
    try:
        notes = upsert_workspace_notes(
            backend_url=backend_url,
            workspace_id=workspace_id,
            description=description,
        )
    except Exception as exc:
        notes_error = f"{type(exc).__name__}: {exc}"
    return {
        "workspace_id": workspace_id,
        "backend_url": backend_url,
        "workspace": workspace,
        "notes": notes,
        "notes_error": notes_error,
        "tool": "local_api_update_workspace",
    }


def normalize_issue_id(payload: dict[str, Any]) -> str | None:
    for key in ("issue_id", "issueId", "id", "task_id", "taskId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, int):
            return str(value)
    for key in ("issue", "task", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            nested = normalize_issue_id(value)
            if nested:
                return nested
    return None


def _organization_id_from_record(record: dict[str, Any]) -> str | None:
    return _normalize_id(record, ("organization_id", "organizationId", "id"))


def list_organizations(*, mcp_cmd: str, backend_url: str | None = None) -> list[dict[str, Any]]:
    def _call(client: McpClient, _candidate: str | None) -> list[dict[str, Any]]:
        tool = _find_tool(client.list_tools(), ["list_organizations"])
        payload = client.call_tool(str(tool["name"]), _tool_arguments(tool, {}))
        return _payload_list(payload, ("organizations",))

    return _with_mcp_client(mcp_cmd, backend_url=backend_url, callback=_call)


def list_projects(*, mcp_cmd: str, backend_url: str | None = None) -> list[dict[str, Any]]:
    def _call(client: McpClient, candidate: str | None) -> list[dict[str, Any]]:
        tool = _find_tool(client.list_tools(), ["list_projects"])
        properties = _schema_properties(tool) or set()
        required = tool.get("inputSchema", {}).get("required") if isinstance(tool.get("inputSchema"), dict) else []
        needs_org = "organization_id" in properties and isinstance(required, list) and "organization_id" in required
        payloads: list[dict[str, Any]] = []
        if needs_org:
            for organization in list_organizations(mcp_cmd=mcp_cmd, backend_url=candidate):
                organization_id = _organization_id_from_record(organization)
                if not organization_id:
                    continue
                payload = client.call_tool(str(tool["name"]), _tool_arguments(tool, {"organization_id": organization_id}))
                payloads.extend(_payload_list(payload, ("projects",)))
            return payloads
        payload = client.call_tool(str(tool["name"]), _tool_arguments(tool, {}))
        return _payload_list(payload, ("projects",))

    return _with_mcp_client(mcp_cmd, backend_url=backend_url, callback=_call)


def list_repos(*, mcp_cmd: str, backend_url: str | None = None) -> list[dict[str, Any]]:
    def _call(client: McpClient, _candidate: str | None) -> list[dict[str, Any]]:
        tool = _find_tool(client.list_tools(), ["list_repos"])
        payload = client.call_tool(str(tool["name"]), _tool_arguments(tool, {}))
        return _payload_list(payload, ("repos", "repositories"))

    return _with_mcp_client(mcp_cmd, backend_url=backend_url, callback=_call)


def create_project_if_tool_exists(
    *,
    mcp_cmd: str,
    repo: Path,
    backend_url: str | None = None,
) -> dict[str, Any] | None:
    tool_candidates = [
        os.environ.get("CODEX_RVF_VK_CREATE_PROJECT_TOOL", "").strip(),
        "create_project",
        "create_remote_project",
        "create_project_from_repo",
    ]
    def _call(client: McpClient, _candidate: str | None) -> dict[str, Any] | None:
        tools = client.list_tools()
        try:
            tool = _find_tool(tools, [item for item in tool_candidates if item])
        except McpError:
            return None
        arguments = {
            "name": repo.name,
            "repo_path": str(repo),
            "path": str(repo),
            "local_path": str(repo),
            "working_dir": str(repo),
        }
        tool_name = str(tool["name"])
        payload = client.call_tool(tool_name, _tool_arguments(tool, arguments))
        project_id = _project_id_from_record(payload)
        if project_id:
            payload["project_id"] = project_id
        payload["tool"] = tool_name
        return payload

    return _with_mcp_client(mcp_cmd, backend_url=backend_url, callback=_call)


def resolve_project(
    *,
    mcp_cmd: str,
    repo: Path,
    backend_url: str | None = None,
    start_if_needed: bool,
    create_if_missing: bool,
    start_cmd: str,
    tmux_session: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    bootstrap = ensure_vibe_kanban_mcp(
        mcp_cmd=mcp_cmd,
        backend_url=backend_url,
        start_if_needed=start_if_needed,
        start_cmd=start_cmd,
        repo=repo,
        tmux_session=tmux_session,
        timeout_seconds=timeout_seconds,
    )
    resolved_backend_url = bootstrap.get("backend_url") if isinstance(bootstrap.get("backend_url"), str) else backend_url

    repos = list_repos(mcp_cmd=mcp_cmd, backend_url=resolved_backend_url)
    for record in repos:
        if _repo_record_matches(record, repo):
            project_id = _project_id_from_record(record)
            if project_id:
                return {
                    "project_id": project_id,
                    "source": "list_repos",
                    "matched_repo": record,
                    "bootstrap": bootstrap,
                }

    projects = list_projects(mcp_cmd=mcp_cmd, backend_url=resolved_backend_url)
    for record in projects:
        if _project_record_matches(record, repo):
            project_id = _project_id_from_record(record)
            if project_id:
                return {
                    "project_id": project_id,
                    "source": "list_projects",
                    "matched_project": record,
                    "bootstrap": bootstrap,
                }

    if create_if_missing:
        created = create_project_if_tool_exists(mcp_cmd=mcp_cmd, repo=repo, backend_url=resolved_backend_url)
        if created is not None:
            project_id = _project_id_from_record(created)
            if project_id:
                return {
                    "project_id": project_id,
                    "source": "create_project",
                    "created_project": created,
                    "bootstrap": bootstrap,
                }

    if len(projects) == 1 and _truthy_env("CODEX_RVF_VK_SINGLE_PROJECT_FALLBACK"):
        project_id = _project_id_from_record(projects[0])
        if project_id:
            return {
                "project_id": project_id,
                "source": "single_project_fallback",
                "matched_project": projects[0],
                "bootstrap": bootstrap,
            }

    raise McpError(
        f"no Vibe-Kanban project matched repo {repo}; "
        "current MCP tools can list projects/repos but did not expose a usable create_project tool; "
        "set CODEX_RVF_VK_PROJECT_ID or leave exactly one Vibe project for single-project fallback"
    )


def create_issue(
    *,
    mcp_cmd: str,
    backend_url: str | None = None,
    project_id: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    tool_candidates = [
        os.environ.get("CODEX_RVF_VK_CREATE_ISSUE_TOOL", "").strip(),
        DEFAULT_CREATE_TOOL,
        "create_task",
        "create_card",
    ]
    arguments = {
        "project_id": project_id,
        "projectId": project_id,
        "title": title,
        "description": description,
    }
    def _call(client: McpClient, candidate: str | None) -> dict[str, Any]:
        tool = _find_tool(client.list_tools(), [item for item in tool_candidates if item])
        tool_name = str(tool["name"])
        payload = client.call_tool(tool_name, _tool_arguments(tool, arguments))
        issue_id = normalize_issue_id(payload)
        payload["tool"] = tool_name
        if candidate:
            payload["backend_url"] = candidate
        if issue_id:
            payload["issue_id"] = issue_id
        return payload

    return _with_mcp_client(mcp_cmd, backend_url=backend_url, callback=_call)


def update_issue(
    *,
    mcp_cmd: str,
    backend_url: str | None = None,
    project_id: str,
    issue_id: str,
    title: str | None,
    description: str,
    status: str,
) -> dict[str, Any]:
    tool_candidates = [
        os.environ.get("CODEX_RVF_VK_UPDATE_ISSUE_TOOL", "").strip(),
        DEFAULT_UPDATE_TOOL,
        "update_task",
        "update_card",
    ]
    arguments = {
        "project_id": project_id,
        "projectId": project_id,
        "issue_id": issue_id,
        "issueId": issue_id,
        "id": issue_id,
        "description": description,
        "status": normalize_issue_status(status),
    }
    if title:
        arguments["title"] = title
    def _call(client: McpClient, candidate: str | None) -> dict[str, Any]:
        tool = _find_tool(client.list_tools(), [item for item in tool_candidates if item])
        tool_name = str(tool["name"])
        payload = client.call_tool(tool_name, _tool_arguments(tool, arguments))
        payload["tool"] = tool_name
        if candidate:
            payload["backend_url"] = candidate
        payload.setdefault("issue_id", issue_id)
        return payload

    return _with_mcp_client(mcp_cmd, backend_url=backend_url, callback=_call)


def main() -> int:
    parser = argparse.ArgumentParser(description="最小 Vibe-Kanban MCP issue client。")
    parser.add_argument(
        "action",
        choices=[
            "create",
            "update",
            "create-workspace",
            "update-workspace",
            "resolve-project",
            "list-projects",
            "list-repos",
        ],
    )
    parser.add_argument("--mcp-cmd", default=os.environ.get("CODEX_RVF_VK_MCP_CMD", DEFAULT_MCP_CMD))
    parser.add_argument("--backend-url", default=os.environ.get("CODEX_RVF_VK_BACKEND_URL") or os.environ.get("VIBE_BACKEND_URL"))
    parser.add_argument("--start-cmd", default=os.environ.get("CODEX_RVF_VK_START_CMD", DEFAULT_START_CMD))
    parser.add_argument("--start-timeout", type=float, default=float(os.environ.get("CODEX_RVF_VK_START_TIMEOUT", DEFAULT_START_TIMEOUT_SECONDS)))
    parser.add_argument("--tmux-session", default=os.environ.get("CODEX_RVF_VK_TMUX_SESSION", DEFAULT_TMUX_SESSION))
    parser.add_argument("--repo")
    parser.add_argument("--start-if-needed", action="store_true")
    parser.add_argument("--create-if-missing", action="store_true")
    parser.add_argument("--project-id", default=os.environ.get("CODEX_RVF_VK_PROJECT_ID"))
    parser.add_argument("--issue-id")
    parser.add_argument("--workspace-id")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--status", default="running")
    args = parser.parse_args()

    try:
        if args.action == "list-projects":
            payload = {"projects": list_projects(mcp_cmd=args.mcp_cmd, backend_url=args.backend_url)}
        elif args.action == "list-repos":
            payload = {"repos": list_repos(mcp_cmd=args.mcp_cmd, backend_url=args.backend_url)}
        elif args.action == "resolve-project":
            if not args.repo:
                raise McpError("--repo is required for resolve-project")
            payload = resolve_project(
                mcp_cmd=args.mcp_cmd,
                repo=Path(args.repo),
                backend_url=args.backend_url,
                start_if_needed=args.start_if_needed,
                create_if_missing=args.create_if_missing,
                start_cmd=args.start_cmd,
                tmux_session=args.tmux_session,
                timeout_seconds=args.start_timeout,
            )
        elif args.action == "create-workspace":
            if not args.title:
                raise McpError("--title is required for create-workspace")
            if args.description is None:
                raise McpError("--description is required for create-workspace")
            payload = create_local_workspace(
                mcp_cmd=args.mcp_cmd,
                backend_url=args.backend_url,
                start_if_needed=args.start_if_needed,
                start_cmd=args.start_cmd,
                repo=Path(args.repo) if args.repo else None,
                tmux_session=args.tmux_session,
                timeout_seconds=args.start_timeout,
                title=args.title,
                description=args.description,
                status=args.status,
            )
        elif args.action == "update-workspace":
            if not args.workspace_id:
                raise McpError("--workspace-id is required for update-workspace")
            if not args.backend_url:
                raise McpError("--backend-url is required for update-workspace")
            if args.description is None:
                raise McpError("--description is required for update-workspace")
            payload = update_local_workspace(
                backend_url=args.backend_url,
                workspace_id=args.workspace_id,
                title=args.title,
                description=args.description,
                status=args.status,
            )
        elif args.action == "create":
            if not args.project_id:
                raise McpError("--project-id or CODEX_RVF_VK_PROJECT_ID is required")
            if not args.title:
                raise McpError("--title is required for create")
            if args.description is None:
                raise McpError("--description is required for create")
            payload = create_issue(
                mcp_cmd=args.mcp_cmd,
                backend_url=args.backend_url,
                project_id=args.project_id,
                title=args.title,
                description=args.description,
            )
        else:
            if not args.project_id:
                raise McpError("--project-id or CODEX_RVF_VK_PROJECT_ID is required")
            if not args.issue_id:
                raise McpError("--issue-id is required for update")
            if args.description is None:
                raise McpError("--description is required for update")
            payload = update_issue(
                mcp_cmd=args.mcp_cmd,
                backend_url=args.backend_url,
                project_id=args.project_id,
                issue_id=args.issue_id,
                title=args.title,
                description=args.description,
                status=args.status,
            )
    except Exception as exc:
        print(f"vibe-kanban MCP error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
