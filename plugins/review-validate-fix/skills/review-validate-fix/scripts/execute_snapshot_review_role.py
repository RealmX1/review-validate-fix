#!/usr/bin/env python3
"""等待独立角色进程并保存实际结果；重启后由原 run 接续。"""
import sys
from pathlib import Path
import _rvf_pyroot
from core.task_snapshot_review.role_process_execution import execute_role

if __name__ == "__main__":
    execute_role(Path(sys.argv[1]).resolve(),inherited_lock_descriptor=int(sys.argv[2]) if len(sys.argv)>2 else None)
