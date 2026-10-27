#!/usr/bin/env python3
"""宿主 hook 入口；错误只记录诊断，不续跑 Agent。"""
import argparse
import json
import sys
import _rvf_pyroot
from adapters import resolve_hook_host_adapter
from core.task_snapshot_review.hook_entrypoint import handle_hook


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", choices=("Stop", "UserPromptSubmit"), default="Stop")
    parser.add_argument("--host", choices=("codex", "claude_code", "kimi_code"), default="codex")
    args = parser.parse_args(argv)
    try:
        event = json.load(sys.stdin)
        payload = handle_hook(event, event_name=args.event, host_adapter=resolve_hook_host_adapter(args.host))
        if payload:
            print(json.dumps(payload, ensure_ascii=False))
    except Exception as error:
        # Kimi 的 stdout 会进入模型；诊断统一走 stderr，不构造继续行动消息。
        print(f"RVF hook 诊断：{type(error).__name__}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
