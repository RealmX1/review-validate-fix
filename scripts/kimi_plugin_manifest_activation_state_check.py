#!/usr/bin/env python3
"""独立判定「RVF 在 Kimi Code CLI 上的注册是否**真的生效**」（只读）。

**为什么不能读 `installed.json` 的 state**

Kimi 的 ``PluginManager.persist()`` 只写 9 个字段
（``id / root / source / enabled / installedAt / updatedAt / originalSource / capabilities / github``），
**不含 ``state`` / ``manifest`` / ``diagnostics``**——那三个是加载时由 ``materialize()`` 现场派生的
内存值。所以「手工塞一个 ``state: "ok"`` 进去」既写不进也读不出，而 ``enabledHooks()`` 的过滤条件
逐字是 ``record.enabled && record.state === "ok" && record.manifest !== undefined``。

⇒ **判定必须由本脚本自己复算**。这正是计划 §3 P-1 那条硬要求「部署校验必须能独立判定注册是否
真的生效，绝不以『安装器写完了』当成功」——本仓反复栽在这个形状上（回执写了 ≠ 事情成了）。

**为什么复算要做到 hook 条目粒度**

Kimi 的 manifest 校验对**单条** hook 不过 schema 的处置是 ``severity:"warn"`` + **静默丢弃那一条**，
而 record 的 ``state`` **仍然是 ``ok``**。于是「plugin 看起来完全健康，但 Stop hook 根本不存在」
是一个零报错的稳定状态——比整份报错危险得多。所以本脚本不满足于「plugin 在册且 enabled」，
必须逐条复刻 schema 校验，并断言 RVF 需要的两个事件（Stop / UserPromptSubmit）**都活着**。

**五态，且 ``check_coverage_incomplete`` 绝不呈现为「一切正常」**

判不出来时必须说判不出来。把「没查到问题」等同于「没有问题」，是这套部署校验最容易退化成的
假绿形状（同族先例：``kimi doctor config`` 完全不碰 plugin store，未装任何插件也 exit 0）。

⚠️ ``~/.kimi-code`` 是 live infra：本脚本**只读**，任何写操作都不属于它的职责。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

KIMI_CODE_HOME_ENVIRONMENT_VARIABLE = "KIMI_CODE_HOME"
DEFAULT_KIMI_CODE_HOME_RELATIVE_TO_USER_HOME = ".kimi-code"

KIMI_PLUGIN_MANIFEST_ROOT_RELATIVE_PATH = "kimi.plugin.json"
KIMI_PLUGIN_MANIFEST_DIRECTORY_RELATIVE_PATH = ".kimi-plugin/plugin.json"

REVIEW_VALIDATE_FIX_PLUGIN_IDENTIFIER = "review-validate-fix"
REQUIRED_HOOK_EVENT_NAMES = ("Stop", "UserPromptSubmit")

# 复刻 Kimi 的 hook 条目 schema：字段集合是 `.strict()` 的，多一个字段该条就被丢弃。
HOOK_ENTRY_REQUIRED_FIELD_NAMES = frozenset({"event", "command", "timeout"})
HOOK_ENTRY_OPTIONAL_FIELD_NAMES = frozenset({"matcher"})
HOOK_ENTRY_TIMEOUT_MINIMUM_SECONDS = 1
HOOK_ENTRY_TIMEOUT_MAXIMUM_SECONDS = 600

ACTIVATION_STATE_SOLE_REGISTRATION = "sole_registration"
ACTIVATION_STATE_NOT_REGISTERED = "not_registered"
ACTIVATION_STATE_DUPLICATE_REGISTRATION = "duplicate_registration"
ACTIVATION_STATE_MANIFEST_INVALID = "manifest_invalid"
ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE = "check_coverage_incomplete"

# 只有 sole_registration 放行。这是「正向证明」而非「排除法」——与 `2806755` 给 Codex 侧
# 定下的判据同源：必须等于 sole_registration 才算好，unavailable / 查不到都**不算**。
ACTIVATION_STATE_EXIT_CODES = {
    ACTIVATION_STATE_SOLE_REGISTRATION: 0,
    ACTIVATION_STATE_NOT_REGISTERED: 1,
    ACTIVATION_STATE_MANIFEST_INVALID: 1,
    ACTIVATION_STATE_DUPLICATE_REGISTRATION: 3,
    ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE: 4,
}


@dataclass
class KimiPluginActivationStateReport:
    activation_state: str
    kimi_code_home: str | None = None
    installed_registry_path: str | None = None
    matching_registration_count: int = 0
    effective_manifest_path: str | None = None
    shadowed_manifest_path: str | None = None
    live_hook_event_names: list[str] = field(default_factory=list)
    discarded_hook_entries: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)

    def as_json_document(self) -> dict[str, Any]:
        return {
            "activation_state": self.activation_state,
            "kimi_code_home": self.kimi_code_home,
            "installed_registry_path": self.installed_registry_path,
            "matching_registration_count": self.matching_registration_count,
            "effective_manifest_path": self.effective_manifest_path,
            "shadowed_manifest_path": self.shadowed_manifest_path,
            "live_hook_event_names": sorted(self.live_hook_event_names),
            "discarded_hook_entries": self.discarded_hook_entries,
            "diagnostics": self.diagnostics,
            "remediation": self.remediation,
        }


def resolve_kimi_code_home(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    environment = os.environ if environment is None else environment
    override = environment.get(KIMI_CODE_HOME_ENVIRONMENT_VARIABLE)
    if override:
        return Path(override).expanduser()
    base = home_directory if home_directory is not None else Path.home()
    return Path(base) / DEFAULT_KIMI_CODE_HOME_RELATIVE_TO_USER_HOME


def hook_entry_is_schema_valid(hook_entry: Any) -> tuple[bool, str | None]:
    """复刻 Kimi 对单条 hook 的 schema 校验；返回 (是否有效, 被丢弃的原因)。

    Kimi 对不合格条目**只 warn 并丢弃该条**，record 的 state 仍是 ok ⇒ 缺了这一条复算，
    「plugin 健康但 hook 不存在」就查不出来。
    """
    if not isinstance(hook_entry, dict):
        return False, "hook 条目不是 object"
    field_names = set(hook_entry)
    missing = HOOK_ENTRY_REQUIRED_FIELD_NAMES - field_names
    if missing:
        return False, f"缺必填字段 {sorted(missing)}"
    unexpected = field_names - HOOK_ENTRY_REQUIRED_FIELD_NAMES - HOOK_ENTRY_OPTIONAL_FIELD_NAMES
    if unexpected:
        # schema 是 .strict() 的：多写一个字段整条被丢弃，而不是被忽略。
        return False, f"存在 schema 不认识的字段 {sorted(unexpected)}（schema 为 strict）"
    if not isinstance(hook_entry.get("event"), str) or not hook_entry["event"]:
        return False, "event 不是非空字符串"
    if not isinstance(hook_entry.get("command"), str) or not hook_entry["command"]:
        return False, "command 不是非空字符串"
    timeout = hook_entry.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return False, "timeout 不是数字"
    if not (HOOK_ENTRY_TIMEOUT_MINIMUM_SECONDS <= timeout <= HOOK_ENTRY_TIMEOUT_MAXIMUM_SECONDS):
        return False, (
            f"timeout={timeout} 超出 "
            f"[{HOOK_ENTRY_TIMEOUT_MINIMUM_SECONDS}, {HOOK_ENTRY_TIMEOUT_MAXIMUM_SECONDS}]"
        )
    if "matcher" in hook_entry and not isinstance(hook_entry["matcher"], str):
        return False, "matcher 存在但不是字符串"
    return True, None


def resolve_effective_manifest_path(plugin_root: Path) -> tuple[Path | None, Path | None]:
    """返回 (生效的 manifest, 被遮蔽的 manifest)。

    Kimi 的 ``parseManifest()`` 先查 ``<root>/kimi.plugin.json``、再查
    ``<root>/.kimi-plugin/plugin.json``；**两个都在时 root 版生效**，dir 版被记为
    ``shadowedManifestPath``。RVF 投的是 dir 版，所以仓库根下一旦出现 root 版，
    我们的 manifest 会被**静默遮蔽**——必须报出来。
    """
    root_manifest = plugin_root / KIMI_PLUGIN_MANIFEST_ROOT_RELATIVE_PATH
    directory_manifest = plugin_root / KIMI_PLUGIN_MANIFEST_DIRECTORY_RELATIVE_PATH
    if root_manifest.is_file():
        return root_manifest, (directory_manifest if directory_manifest.is_file() else None)
    if directory_manifest.is_file():
        return directory_manifest, None
    return None, None


def inspect_kimi_plugin_activation_state(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
    plugin_identifier: str = REVIEW_VALIDATE_FIX_PLUGIN_IDENTIFIER,
) -> KimiPluginActivationStateReport:
    kimi_code_home = resolve_kimi_code_home(
        environment=environment, home_directory=home_directory
    )
    report = KimiPluginActivationStateReport(
        activation_state=ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE,
        kimi_code_home=str(kimi_code_home),
    )

    installed_registry_path = kimi_code_home / "plugins" / "installed.json"
    report.installed_registry_path = str(installed_registry_path)

    if not installed_registry_path.is_file():
        # Kimi 的 readInstalled() 对 ENOENT 返回空 registry ⇒ 这是「没装」而不是「查不了」。
        report.activation_state = ACTIVATION_STATE_NOT_REGISTERED
        report.diagnostics.append(
            f"{installed_registry_path} 不存在 ⇒ Kimi 从未注册过任何插件（RVF 亦未注册）。"
        )
        report.remediation.append(
            "运行 RVF 的 Kimi 安装步骤写入 installed.json，然后在 Kimi 里执行 /plugins reload。"
        )
        return report

    try:
        registry_document = json.loads(installed_registry_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # 非法 JSON 在 Kimi 侧是抛错（不是空 registry），但对本脚本而言是「判不出」。
        report.activation_state = ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE
        report.diagnostics.append(
            f"installed.json 无法解析（{type(exc).__name__}: {exc}）⇒ 本次核对**未能覆盖**注册状态，"
            "不得据此认为一切正常。Kimi 侧对非法 JSON 是抛错，插件会整体加载失败。"
        )
        report.remediation.append(
            f"修复 {installed_registry_path} 的 JSON，然后在 Kimi 里执行 /plugins reload。"
        )
        return report

    plugin_records = registry_document.get("plugins") if isinstance(registry_document, dict) else None
    if not isinstance(plugin_records, list):
        report.activation_state = ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE
        report.diagnostics.append(
            "installed.json 顶层缺少 plugins 数组 ⇒ 结构与取证到的形态不符，本次核对未能覆盖。"
        )
        return report

    matching = [
        record
        for record in plugin_records
        if isinstance(record, dict) and record.get("id") == plugin_identifier
    ]
    report.matching_registration_count = len(matching)

    if not matching:
        report.activation_state = ACTIVATION_STATE_NOT_REGISTERED
        report.diagnostics.append(
            f"installed.json 里没有 id={plugin_identifier!r} 的条目（共 {len(plugin_records)} 条注册）。"
        )
        report.remediation.append(
            "运行 RVF 的 Kimi 安装步骤，然后在 Kimi 里执行 /plugins reload。"
        )
        return report

    if len(matching) > 1:
        # 本仓在 Codex 侧栽过一模一样的跟头：陈旧的重复注册在会话中途被清理，
        # 此后每次 Stop 都去 spawn 一个不存在的脚本（已证 286 次无上界续跑）。
        report.activation_state = ACTIVATION_STATE_DUPLICATE_REGISTRATION
        report.diagnostics.append(
            f"id={plugin_identifier!r} 出现 {len(matching)} 次 ⇒ 重复注册。"
            "重复注册的危害不是「装了两份」，而是其中一份被清理时，仍持有旧快照的 live 会话"
            "会反复 spawn 已消失的 hook 脚本。"
        )
        report.remediation.append(
            f"手工编辑 {installed_registry_path} 只保留一条 canonical 注册，再 /plugins reload。"
        )
        return report

    record = matching[0]
    if record.get("enabled") is not True:
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append(
            f"注册在册但 enabled={record.get('enabled')!r} ⇒ enabledHooks() 会跳过它，hook 不会触发。"
        )
        report.remediation.append("在 Kimi 的 /plugins 面板里启用该插件，或把 enabled 改为 true。")
        return report

    plugin_root_value = record.get("root")
    if not isinstance(plugin_root_value, str) or not plugin_root_value:
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append("注册条目缺少 root 路径 ⇒ 无法定位 manifest。")
        return report

    plugin_root = Path(plugin_root_value).expanduser()
    if not plugin_root.is_dir():
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append(
            f"注册指向的 plugin root 不存在：{plugin_root} ⇒ manifest 无从加载，hook 不会触发。"
            "（注册的生命周期与磁盘的生命周期是两件事，这正是它们脱节的形状。）"
        )
        report.remediation.append("重新部署 RVF 到该路径，或更新注册条目的 root。")
        return report

    effective_manifest, shadowed_manifest = resolve_effective_manifest_path(plugin_root)
    report.effective_manifest_path = str(effective_manifest) if effective_manifest else None
    report.shadowed_manifest_path = str(shadowed_manifest) if shadowed_manifest else None

    if effective_manifest is None:
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append(
            f"{plugin_root} 下既无 {KIMI_PLUGIN_MANIFEST_ROOT_RELATIVE_PATH} 也无 "
            f"{KIMI_PLUGIN_MANIFEST_DIRECTORY_RELATIVE_PATH}。"
        )
        return report

    if shadowed_manifest is not None:
        report.diagnostics.append(
            f"⚠️ {shadowed_manifest} 被 {effective_manifest} 遮蔽（root 版优先）。"
            "RVF 投的是 dir 版，若 root 版不是 RVF 的，我们的 hook 声明**完全不生效**。"
        )

    try:
        manifest_document = json.loads(effective_manifest.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append(f"manifest 无法解析（{type(exc).__name__}: {exc}）。")
        return report

    hook_entries = manifest_document.get("hooks") if isinstance(manifest_document, dict) else None
    if hook_entries is None:
        hook_entries = []
    if not isinstance(hook_entries, list):
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append("manifest 的 hooks 不是数组。")
        return report

    for index, hook_entry in enumerate(hook_entries):
        valid, discard_reason = hook_entry_is_schema_valid(hook_entry)
        if valid:
            report.live_hook_event_names.append(hook_entry["event"])
        else:
            report.discarded_hook_entries.append(
                {
                    "index": index,
                    "event": hook_entry.get("event") if isinstance(hook_entry, dict) else None,
                    "discard_reason": discard_reason,
                }
            )

    missing_events = [
        event for event in REQUIRED_HOOK_EVENT_NAMES if event not in report.live_hook_event_names
    ]
    if missing_events:
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append(
            f"RVF 需要的 hook 事件 {missing_events} 没有活着的声明。"
            + (
                f" 被 schema 丢弃的条目：{report.discarded_hook_entries}"
                " —— Kimi 对单条不合格只 warn 并丢弃，plugin 的 state 仍是 ok，"
                "所以这是一个「看起来健康、实际 hook 不存在」的零报错状态。"
                if report.discarded_hook_entries
                else ""
            )
        )
        report.remediation.append(
            "修正 .kimi-plugin/plugin.json 的 hooks 条目（字段恰为 event/command/timeout[/matcher]，"
            f"timeout ∈ [{HOOK_ENTRY_TIMEOUT_MINIMUM_SECONDS}, {HOOK_ENTRY_TIMEOUT_MAXIMUM_SECONDS}]），"
            "重新部署后 /plugins reload。"
        )
        return report

    report.activation_state = ACTIVATION_STATE_SOLE_REGISTRATION
    report.diagnostics.append(
        "恰好一条 enabled 注册，manifest 可解析，"
        f"RVF 需要的 {list(REQUIRED_HOOK_EVENT_NAMES)} 两个 hook 均通过 schema 复算。"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "只读核对 RVF 在 Kimi Code CLI 上的注册是否真的生效（自行复算 state，"
            "不读 installed.json 里并不存在的 state 字段）。"
        )
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告。")
    parser.add_argument(
        "--kimi-code-home",
        default=None,
        help="覆盖 KIMI_CODE_HOME（测试用；生产走 env 或 ~/.kimi-code）。",
    )
    arguments = parser.parse_args(argv)

    environment = dict(os.environ)
    if arguments.kimi_code_home:
        environment[KIMI_CODE_HOME_ENVIRONMENT_VARIABLE] = arguments.kimi_code_home

    report = inspect_kimi_plugin_activation_state(environment=environment)

    if arguments.json:
        print(json.dumps(report.as_json_document(), ensure_ascii=False, indent=2))
    else:
        print(f"KIMI_PLUGIN_ACTIVATION_STATE={report.activation_state}")
        for diagnostic in report.diagnostics:
            print(f"  · {diagnostic}")
        for remediation in report.remediation:
            print(f"  → {remediation}")

    return ACTIVATION_STATE_EXIT_CODES.get(
        report.activation_state, ACTIVATION_STATE_EXIT_CODES[ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE]
    )


if __name__ == "__main__":
    raise SystemExit(main())
