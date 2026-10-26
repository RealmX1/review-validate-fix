#!/usr/bin/env python3
"""检查当前插件的可导入源码、manifest 与运行入口。"""
import ast
import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full',action='store_true',help='运行当前 manifest 与自动化验收')
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for directory in ('core','adapters','plugins','scripts'):
        for source in (root / directory).rglob('*.py'):
            ast.parse(source.read_text(), filename=str(source))
    plugin = root / 'plugins/review-validate-fix'
    for path in (plugin/'.codex-plugin/plugin.json', plugin/'.claude-plugin/plugin.json', plugin/'.kimi-plugin/plugin.json', plugin/'hooks/hooks.json'):
        json.loads(path.read_text())
    for name in ('rvf_command_line_shortcut_entrypoint.py','execute_snapshot_review_role.py','task_snapshot_review_hook.py'):
        assert (plugin/'skills/review-validate-fix/scripts'/name).is_file(), name
    print('插件源码与入口检查通过')
    if arguments.full:
        subprocess.run(['bash',str(root/'scripts/sync-manifest.sh')],check=True,cwd=root)
        return subprocess.run([sys.executable,'-m','pytest','-q','tests'],cwd=root).returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
