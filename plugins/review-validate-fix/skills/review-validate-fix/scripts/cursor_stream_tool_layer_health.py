#!/usr/bin/env python3
"""cursor-agent stream-json「工具执行层」健康判定的纯逻辑（无副作用、仅 stdlib）。

把这段检测逻辑单独成模块，让两处零漂移地共用：
  - run_alternative_reviewer.py 的 ``CursorStreamActivityMonitor``（评审过程中实时快速失败）；
  - verify_cursor_tool_layer.py 这个「升级 cursor 前的冒烟 gate」（一次性验证新 build）。

背景：2026-06-24 一次 cursor-agent 自更新后的「首次运行 transient」把整条工具执行层打断
——10/10 工具调用全部 spawnError / Aborted，而 model 推理与 auth 均正常。当时 RVF 直到
~83s idle 才放弃。这里专门识别「cursor 自身工具执行器坏了」这一类失败（区别于「命令退出
非零」「文件不存在」等证明工具层正常工作的普通工具错误）。
"""
from __future__ import annotations

import json
from typing import Any

# cursor-agent 自身 tool-runtime 坏掉时的错误签名（小写子串匹配）。只匹配「执行器层」
# 失败，不含「命令退出非零」「文件不存在」这类正常工具错误（那些代表工具层是好的）。
CURSOR_TOOL_RUNTIME_FAILURE_SIGNATURES = (
    "aborted",
    "no exit status",
    "execution environment may need to be restarted",
)


def classify_cursor_tool_call_outcome(tool_call: object) -> str | None:
    """判定一条 cursor-agent ``tool_call``（subtype=completed）事件的工具层结果。

    返回值：
      - ``"runtime_failure"``：cursor 自身的工具执行器坏了——``result.spawnError`` 存在，
        或 ``result.error`` 的文本命中 :data:`CURSOR_TOOL_RUNTIME_FAILURE_SIGNATURES`
        （如 ``Aborted`` / ``no exit status``）。
      - ``"ok"``：工具层正常。**包含**「命令退出非零」「文件不存在」这类正常工具错误——
        它们恰恰证明工具层在工作，只是该次操作本身失败。
      - ``None``：不是可判定的工具结果（无法归类，既不计成功也不计失败）。

    刻意只读结构化的 ``result.spawnError`` / ``result.error`` 字段，绝不扫描命令 stdout
    或被读文件内容，避免「读到的文件里恰好含 'Aborted' 字样」造成误判。
    """
    if not isinstance(tool_call, dict):
        return None
    for value in tool_call.values():
        if not isinstance(value, dict):
            continue
        result = value.get("result")
        if not isinstance(result, dict):
            continue
        if "spawnError" in result:
            return "runtime_failure"
        error = result.get("error")
        if error is not None:
            if isinstance(error, dict):
                error_text = " ".join(str(item) for item in error.values())
            else:
                error_text = str(error)
            lowered = error_text.lower()
            if any(sig in lowered for sig in CURSOR_TOOL_RUNTIME_FAILURE_SIGNATURES):
                return "runtime_failure"
            return "ok"
        return "ok"
    return None


def summarize_cursor_stream_tool_layer(stream_text: str) -> dict[str, Any]:
    """扫描整段 cursor stream-json stdout，汇总工具执行层健康度。

    返回 ``{"tool_runtime_failures", "tool_successes", "completed_tool_calls",
    "healthy"}``。``healthy`` 的判据：有过至少一次成功工具调用、且零 runtime 失败
    （冒烟 gate 用最严判据；评审过程中的实时判据另见 ``CursorStreamActivityMonitor``）。
    """
    failures = 0
    successes = 0
    completed = 0
    for raw_line in stream_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "tool_call" or payload.get("subtype") != "completed":
            continue
        completed += 1
        outcome = classify_cursor_tool_call_outcome(payload.get("tool_call"))
        if outcome == "runtime_failure":
            failures += 1
        elif outcome == "ok":
            successes += 1
    return {
        "tool_runtime_failures": failures,
        "tool_successes": successes,
        "completed_tool_calls": completed,
        "healthy": successes > 0 and failures == 0,
    }
