"""Kimi 插件激活态核对（S-B8 / D-e）的五态闸。

**为什么这个核对必须自己复算**

Kimi 的 ``persist()`` 只落 9 个字段，``state`` / ``manifest`` / ``diagnostics`` 都是加载时
派生的内存值——磁盘上根本没有 ``state``。而 ``enabledHooks()`` 的过滤条件逐字是
``record.enabled && record.state === "ok" && record.manifest !== undefined``。
所以「读 installed.json 看 state 是不是 ok」这条最自然的实现路径，检的是一个不存在的字段。

**为什么要复算到 hook 条目粒度**

单条 hook 不过 schema 时，Kimi 的处置是 ``severity:"warn"`` + 静默丢弃那一条，而 record 的
``state`` **仍然是 ok**。于是「plugin 看起来完全健康、但 Stop hook 根本不存在」是一个零报错的
稳定状态。本测试的 ``badhook`` 与 ``shadow`` 两例就是这个形状。

**为什么 ``check_coverage_incomplete`` 必须与「正常」区分**

判不出来时说判不出来。把「没查到问题」等同于「没有问题」，是这套校验最容易退化成的假绿——
同族先例：``kimi doctor config`` 完全不碰 plugin store，未装任何插件也 exit 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
ACTIVATION_STATE_CHECKER = (
    REPOSITORY_ROOT / "scripts/kimi_plugin_manifest_activation_state_check.py"
)

WELL_FORMED_HOOK_ENTRIES = [
    {"event": "Stop", "command": 'python3 "$KIMI_PLUGIN_ROOT/hooks/kimi_stop.py"', "timeout": 115},
    {
        "event": "UserPromptSubmit",
        "command": 'python3 "$KIMI_PLUGIN_ROOT/hooks/kimi_user_prompt_submit.py"',
        "timeout": 85,
    },
]


def _build_kimi_home(
    root: Path,
    *,
    installed_registry: object,
    hook_entries: list | None,
    root_manifest_document: dict | None = None,
) -> Path:
    kimi_home = root
    (kimi_home / "plugins").mkdir(parents=True, exist_ok=True)
    if installed_registry is not None:
        (kimi_home / "plugins" / "installed.json").write_text(
            installed_registry
            if isinstance(installed_registry, str)
            else json.dumps(installed_registry),
            encoding="utf-8",
        )
    plugin_root = kimi_home / "payload" / "review-validate-fix"
    (plugin_root / ".kimi-plugin").mkdir(parents=True, exist_ok=True)
    if hook_entries is not None:
        (plugin_root / ".kimi-plugin" / "plugin.json").write_text(
            json.dumps(
                {"name": "review-validate-fix", "description": "d", "hooks": hook_entries}
            ),
            encoding="utf-8",
        )
    if root_manifest_document is not None:
        (plugin_root / "kimi.plugin.json").write_text(
            json.dumps(root_manifest_document), encoding="utf-8"
        )
    return kimi_home


def _registration_record(kimi_home: Path, **overrides) -> dict:
    record = {
        "id": "review-validate-fix",
        "root": str(kimi_home / "payload" / "review-validate-fix"),
        "source": "local",
        "enabled": True,
        "installedAt": "t",
        "updatedAt": "t",
        "originalSource": "local",
        "capabilities": [],
        "github": None,
    }
    record.update(overrides)
    return record


def _inspect(kimi_home: Path) -> tuple[dict, int]:
    completed = subprocess.run(
        [sys.executable, str(ACTIVATION_STATE_CHECKER), "--json", "--kimi-code-home", str(kimi_home)],
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout), completed.returncode


def test_healthy_single_registration_is_the_only_state_that_exits_zero(tmp_path: Path) -> None:
    kimi_home = _build_kimi_home(
        tmp_path / "sole", installed_registry=None, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home)]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "sole_registration", report
    assert exit_code == 0, report
    assert sorted(report["live_hook_event_names"]) == ["Stop", "UserPromptSubmit"], report


def test_missing_registry_reports_not_registered_rather_than_a_coverage_gap(tmp_path: Path) -> None:
    kimi_home = _build_kimi_home(
        tmp_path / "absent", installed_registry={"plugins": []}, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "not_registered", report
    assert exit_code == 1, report


def test_duplicate_registration_is_its_own_state(tmp_path: Path) -> None:
    """重复注册的危害不是「装了两份」。

    本仓在 Codex 侧栽过一模一样的跟头：陈旧的重复注册在会话运行到一半时被清理，此后每次 Stop
    都去 spawn 一个已经不存在的脚本——实测 286 次无上界续跑，最后靠人工 kill。
    """
    kimi_home = _build_kimi_home(
        tmp_path / "dup", installed_registry=None, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps(
            {"plugins": [_registration_record(kimi_home), _registration_record(kimi_home)]}
        ),
        encoding="utf-8",
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "duplicate_registration", report
    assert exit_code == 3, report
    assert report["matching_registration_count"] == 2, report


def test_one_bad_hook_entry_is_caught_even_though_kimi_would_keep_the_plugin_healthy(
    tmp_path: Path,
) -> None:
    """多写一个字段 ⇒ 该条被 strict schema 丢弃 ⇒ Stop hook 消失，而 Kimi 侧 state 仍是 ok。"""
    hook_entries = [dict(WELL_FORMED_HOOK_ENTRIES[0], extraField="x"), WELL_FORMED_HOOK_ENTRIES[1]]
    kimi_home = _build_kimi_home(tmp_path / "badhook", installed_registry=None, hook_entries=hook_entries)
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home)]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "manifest_invalid", report
    assert exit_code == 1, report
    assert report["live_hook_event_names"] == ["UserPromptSubmit"], report
    assert report["discarded_hook_entries"][0]["event"] == "Stop", report


def test_out_of_range_timeout_discards_the_entry(tmp_path: Path) -> None:
    hook_entries = [dict(WELL_FORMED_HOOK_ENTRIES[0], timeout=601), WELL_FORMED_HOOK_ENTRIES[1]]
    kimi_home = _build_kimi_home(tmp_path / "timeout", installed_registry=None, hook_entries=hook_entries)
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home)]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "manifest_invalid", report
    assert "601" in json.dumps(report["discarded_hook_entries"], ensure_ascii=False), report


def test_root_manifest_shadowing_our_dir_manifest_is_reported(tmp_path: Path) -> None:
    """``<root>/kimi.plugin.json`` 优先于 ``.kimi-plugin/plugin.json``。

    RVF 投的是 dir 版，所以 root 版一旦出现，我们的 hook 声明被**静默架空**。
    """
    kimi_home = _build_kimi_home(
        tmp_path / "shadow",
        installed_registry=None,
        hook_entries=WELL_FORMED_HOOK_ENTRIES,
        root_manifest_document={"name": "other", "description": "d", "hooks": []},
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home)]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "manifest_invalid", report
    assert report["shadowed_manifest_path"], report
    assert any("遮蔽" in diagnostic for diagnostic in report["diagnostics"]), report


def test_unparsable_registry_is_coverage_incomplete_not_a_clean_bill_of_health(
    tmp_path: Path,
) -> None:
    kimi_home = _build_kimi_home(
        tmp_path / "badjson", installed_registry="{not json", hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "check_coverage_incomplete", report
    assert exit_code == 4, report
    assert any("未能覆盖" in diagnostic for diagnostic in report["diagnostics"]), report


def test_disabled_or_missing_plugin_root_never_passes(tmp_path: Path) -> None:
    """注册的生命周期与磁盘的生命周期是两件事——脱节时必须报出来。"""
    kimi_home = _build_kimi_home(
        tmp_path / "disabled", installed_registry=None, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home, enabled=False)]}), encoding="utf-8"
    )
    disabled_report, disabled_exit = _inspect(kimi_home)
    assert disabled_report["activation_state"] == "manifest_invalid", disabled_report
    assert disabled_exit != 0

    vanished_home = _build_kimi_home(
        tmp_path / "vanished", installed_registry=None, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    (vanished_home / "plugins" / "installed.json").write_text(
        json.dumps(
            {"plugins": [_registration_record(vanished_home, root=str(tmp_path / "gone"))]}
        ),
        encoding="utf-8",
    )
    vanished_report, vanished_exit = _inspect(vanished_home)
    assert vanished_report["activation_state"] == "manifest_invalid", vanished_report
    assert vanished_exit != 0
