#!/usr/bin/env python3
"""cursor-agent「工具执行层」冒烟 gate —— 升级 / 切换 cursor build 前的一次性验证。

为什么存在：cursor-agent 的 ``status`` / ``--version`` 只验证 auth 与版本号，**不验证工具
执行层**。2026-06-24 一次自更新后的「首次运行 transient」让 model/auth 全正常、却 10/10
工具调用 spawnError/Aborted——这种坏 build 能轻松骗过 ``cursor-agent status`` 这类健康检查。

本 gate 真正让 cursor-agent 在 headless 下跑一个 trivial 的「shell + read」任务，解析它自己的
stream-json，确认工具执行器确实能 spawn 子进程、能读文件。**这是「未来升级 cursor-agent 前
必须先跑」的测试**：过了才信任新 build / 才解除 alternative-reviewer.cursor.json 里的应急 pin
（应急 pin = 把该 config 的 command[0] 改成 known-good 版本的【绝对路径】；见该配置文件注释）。

要验证某个【具体】build，用 `--cursor-bin <该版本 cursor-agent 的绝对路径>`：cursor-agent 的
版本 wrapper 从自身 realpath 解析版本目录，故按绝对路径调用即锁定该版本。注意：本机 install 下
环境变量 `CURSOR_AGENT_BIN` 【不生效】（实测设了仍跑 PATH build），故本 gate 不读它、也别靠它选版本。

检测逻辑与评审过程中的实时快速失败（CursorStreamActivityMonitor）共用
cursor_stream_tool_layer_health.py，零漂移。

退出码：
  0  工具层健康（≥1 次成功工具调用且零 runtime 失败）
  1  工具层坏掉（出现 spawnError / Aborted 等 runtime 失败，或一次成功都没有）
  2  harness 错误（无法启动 cursor-agent / 超时 / 没有任何已完成的工具调用可判定）

用法：
  python3 verify_cursor_tool_layer.py                 # 用 PATH 上的 cursor-agent（RVF 默认用的那个）
  python3 verify_cursor_tool_layer.py --cursor-bin /abs/path/to/versions/<good>/cursor-agent
  python3 verify_cursor_tool_layer.py --json          # 机器可读结果
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cursor_stream_tool_layer_health import summarize_cursor_stream_tool_layer

EXIT_HEALTHY = 0
EXIT_TOOL_LAYER_BROKEN = 1
EXIT_HARNESS_ERROR = 2

PROBE_PROMPT = (
    "Do exactly two tool actions and then stop:\n"
    "1. Run the shell command: printf 'CURSOR-TOOL-LAYER-OK\\n'\n"
    '2. Read the file ./package.json and report the value of its "name" field.\n'
    "Then reply in one line: SHELL=<what the shell printed> NAME=<the name field>. "
    "Do not write any files."
)
PROBE_PACKAGE_JSON = '{ "name": "cursor-tool-layer-probe", "version": "1.0.0" }\n'


def run_probe(cursor_bin: str, timeout_seconds: float) -> tuple[int, str, str]:
    """在隔离临时目录里 headless 跑一次 cursor-agent 探针。返回 (returncode, stdout, stderr)。"""
    command = [
        cursor_bin,
        "-p",
        "--output-format",
        "stream-json",
        "--force",
        "--trust",
        "--sandbox",
        "disabled",
    ]
    with tempfile.TemporaryDirectory(prefix="cursor-tool-layer-smoke-") as tmp:
        workdir = Path(tmp)
        (workdir / "package.json").write_text(PROBE_PACKAGE_JSON, encoding="utf-8")
        completed = subprocess.run(
            command,
            input=PROBE_PROMPT,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--cursor-bin",
        default="cursor-agent",
        help="cursor-agent 可执行文件（默认 PATH 上的 cursor-agent；验证具体版本请传该版本的绝对路径）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="探针整体超时秒数（默认 120）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    def emit(verdict: str, exit_code: int, summary: dict | None, detail: str) -> int:
        if args.json:
            print(
                json.dumps(
                    {
                        "verdict": verdict,
                        "exit_code": exit_code,
                        "cursor_bin": args.cursor_bin,
                        "summary": summary,
                        "detail": detail,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"[cursor tool-layer gate] {verdict}: {detail}")
            if summary is not None:
                print(
                    f"  completed_tool_calls={summary['completed_tool_calls']} "
                    f"successes={summary['tool_successes']} "
                    f"runtime_failures={summary['tool_runtime_failures']}"
                )
        return exit_code

    try:
        returncode, stdout, stderr = run_probe(args.cursor_bin, args.timeout)
    except FileNotFoundError:
        return emit("HARNESS_ERROR", EXIT_HARNESS_ERROR, None, f"找不到 cursor-agent 可执行文件: {args.cursor_bin}")
    except subprocess.TimeoutExpired:
        return emit(
            "HARNESS_ERROR",
            EXIT_HARNESS_ERROR,
            None,
            f"cursor-agent 探针超过 {args.timeout:g}s 未返回（可能工具层悬挂，建议人工排查）",
        )

    summary = summarize_cursor_stream_tool_layer(stdout)
    if summary["completed_tool_calls"] == 0:
        snippet = (stderr.strip() or stdout.strip())[:400]
        return emit(
            "HARNESS_ERROR",
            EXIT_HARNESS_ERROR,
            summary,
            f"stream 中没有任何已完成的工具调用可供判定（cursor rc={returncode}）。输出片段: {snippet!r}",
        )
    if summary["healthy"]:
        return emit("HEALTHY", EXIT_HEALTHY, summary, "工具执行层正常：shell 能 spawn、文件能读")
    return emit(
        "TOOL_LAYER_BROKEN",
        EXIT_TOOL_LAYER_BROKEN,
        summary,
        "工具执行层坏掉：出现 spawnError/Aborted 等 runtime 失败或零成功——不要信任此 build",
    )


if __name__ == "__main__":
    raise SystemExit(main())
