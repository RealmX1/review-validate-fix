#!/usr/bin/env python3
"""S1 vendor-on-install 产物冒烟测试（**测产物而非源**）。

装进 tmp HOME 后验证：
1. 部署 payload 自包含——``.rvf-pyroot`` 哨兵 + vendored ``core/`` + ``adapters/``
   都在 payload 根，且不带 ``__pycache__`` / ``.pyc``。
2. 干净子解释器里、只把 payload 根加进 ``sys.path``（cwd 切到 tmp、去掉
   PYTHONPATH，杜绝 repo 顶层泄漏），``import core.transcript.models`` 成功且
   确实来自 payload。
3. 以脚本方式跑 vendored facade（模拟部署后 Stop hook 的调用形态：脚本自身 dir
   自动入 path + 哨兵 bootstrap）能做最小 distill。

捕捉「repo 测试全绿、生产 ModuleNotFoundError」的 vendoring 漂移。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_to_codex.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("rvf_install_for_vendored_test", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deploy_into(home: Path) -> Path:
    module = _load_installer()
    original_home = module.Path.home
    original_argv = sys.argv
    module.Path.home = classmethod(lambda cls: home)
    sys.argv = [
        "install_to_codex.py",
        "--plugin-parent",
        str(home / "plugins"),
        "--skip-claude-plugin",
    ]
    try:
        rc = module.main()
    finally:
        module.Path.home = original_home
        sys.argv = original_argv
    assert rc == 0, f"installer main() returned {rc}"
    return home / "plugins" / "review-validate-fix"


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def test_vendored_payload_is_self_contained_and_core_imports() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        dst = _deploy_into(home)

        assert (dst / ".rvf-pyroot").is_file()
        assert (dst / "core" / "transcript" / "models.py").is_file()
        assert (dst / "core" / "transcript" / "io.py").is_file()
        assert (dst / "adapters" / "codex" / "transcript.py").is_file()
        assert (dst / "adapters" / "claude_code" / "transcript.py").is_file()
        assert not list((dst / "core").rglob("__pycache__"))
        assert not list((dst / "core").rglob("*.pyc"))
        assert not list((dst / "adapters").rglob("*.pyc"))

        code = (
            "import sys;"
            f"sys.path.insert(0, r'{dst}');"
            "import core.transcript.models as m;"
            f"assert m.__file__.startswith(r'{dst}'), m.__file__;"
            "assert hasattr(m, 'TranscriptRecord') and hasattr(m, 'NormalizedTranscript');"
            "print('ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(home),
            env=_clean_env(),
        )
        assert proc.returncode == 0, f"vendored core import failed: {proc.stderr}"
        assert "ok" in proc.stdout


def test_vendored_facade_runs_minimal_distill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        dst = _deploy_into(home)
        facade = dst / "skills" / "review-validate-fix" / "scripts" / "trajectory_distill.py"
        assert facade.is_file()

        rollout = home / "r.jsonl"
        rollout.write_text(
            '{"timestamp":"t0","type":"session_meta","payload":{"id":"s"}}\n'
            '{"timestamp":"t1","type":"response_item","payload":'
            '{"type":"reasoning","summary":[{"type":"text","text":"hi"}]}}\n',
            encoding="utf-8",
        )
        out = home / "trajectory.jsonl"
        proc = subprocess.run(
            [sys.executable, str(facade), "--rollout", str(rollout), "--output", str(out)],
            capture_output=True,
            text=True,
            cwd=str(home),
            env=_clean_env(),
        )
        assert proc.returncode == 0, f"vendored facade distill failed: {proc.stderr}"
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        kinds = [json.loads(line)["kind"] for line in lines]
        assert kinds == ["phase_marker", "reasoning"]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
