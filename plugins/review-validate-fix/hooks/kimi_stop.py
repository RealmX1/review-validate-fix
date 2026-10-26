#!/usr/bin/env python3
"""宿主 hook 薄入口；使用共享快照流程。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/review-validate-fix/scripts"))
from task_snapshot_review_hook import main

if __name__ == "__main__":
    raise SystemExit(main(['--event', 'Stop', '--host', 'kimi_code']))
