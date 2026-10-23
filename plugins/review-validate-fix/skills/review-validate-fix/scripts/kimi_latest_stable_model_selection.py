"""通过 Kimi 官方 OAuth 刷新接口为一次评审选择稳定标准/Pro 模型。"""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


KIMI_MANAGED_PROVIDER_ID = "managed:kimi-code"


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

    def remaining_seconds() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Kimi 官方模型目录刷新超时")
        return remaining

    kimi_home = Path(env.get("KIMI_CODE_HOME") or Path(env.get("HOME") or Path.home()) / ".kimi-code")
    process = None
    # 目录查询不在评审仓中启动，避免加载该仓项目配置或创建会话工件。
    with tempfile.TemporaryDirectory(prefix="rvf-kimi-model-catalog-") as working_directory:
        try:
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

            token = (kimi_home / "server.token").read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError("Kimi 本地模型服务未提供认证 token")
            # loopback 请求不经过用户 HTTP 代理，也不跟随重定向携带认证信息。
            class RejectRedirects(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs):
                    return None

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirects())

            def request_catalog(path: str, *, method: str = "GET") -> dict[str, Any]:
                request = urllib.request.Request(
                    base_url + path, data=b"{}" if method == "POST" else None, method=method,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                )
                try:
                    with opener.open(request, timeout=remaining_seconds()) as response:
                        payload = json.load(response)
                except Exception as exc:
                    # 异常正文可能包含凭证或服务输出，只记录异常类型。
                    raise ValueError(f"Kimi 本地模型接口请求失败: {type(exc).__name__}") from None
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
                # 即使父进程已退出，也回收仍属于本次服务进程组的子进程。
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS 对刚退出、已无可发信号成员的进程组可能返回 EPERM。
                    if process.poll() is None:
                        process.terminate()
                if process.poll() is None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()
