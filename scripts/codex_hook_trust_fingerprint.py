#!/usr/bin/env python3
"""Codex `hooks.state` 信任指纹的本地复算 + 部署后核对。

## 这个模块解决什么

Codex 对每条 command hook 记一份**信任指纹**（`~/.codex/config.toml` 的
`[hooks.state."<key>"] trusted_hash`）。改了 hook 命令而没有重新信任 ⇒ 当前指纹与记录的不一致
⇒ 该 hook 被判 `Modified` 并**跳过**。`codex exec` 下这件事**完全静默**：display 里干脆不出现
该 hook 行，既无 warning 也无非零退出。实测（0.147.0）：改命令不重信任后 stdout+stderr 里
`hook` / `trust` / `/hooks` 命中**全为 0**。

于是 `/rvf-local-deploy` 有一个静默失效面：deploy 把新的 hook 命令写进去、一切报告成功，而
RVF 在 Codex 上从此一次都不执行——直到有人偶然发现「Codex 侧再没起过 RVF」。

本模块只做**核对与告警**，绝不改 live config：重新信任是一个信任决定，必须由人做。

## 指纹算法（2026-08-16 用本机实盘数据全量验证：15/15 + 3/3 + 3/3 精确匹配）

`trusted_hash` = `"sha256:" + hex(sha256(compact_canonical_json(identity)))`，其中

```
identity = {
    "event_name": <snake_case 事件名>,          # PostToolUse → post_tool_use
    "matcher":    <仅当 group 上有 matcher 时>,
    "hooks":      [ <该条 hook 对象原样，补齐默认值> ],
}
```

hook 对象**原样保留 hooks.json 里写的所有键**（含 `statusMessage` 这类附加字段——漏掉它会让
带状态文案的 hook 全部误判成 Modified），再补三个默认：`async=False` / `timeout=600` /
`type="command"`。对象键递归排序后 `json.dumps(..., separators=(",", ":"))`。

`hooks.state` 的键格式：`<source>:<snake_event>:<group_index>:<hook_index>`，其中 `<source>`
对 `CODEX_HOME/hooks.json` 是它的**绝对路径**，对插件 bundled 源是
`<plugin>@<marketplace>:<plugin 内相对路径>`。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

TRUST_STATUS_TRUSTED = "trusted"
"""全部被核对的 hook 指纹都与信任表一致。"""

TRUST_STATUS_UNTRUSTED_ENTRIES = "untrusted_entries"
"""至少一条 hook 的当前指纹与信任表不符（或压根没有信任条目）⇒ 它会被 Codex 静默跳过。"""

TRUST_STATUS_TRUST_TABLE_ABSENT = "trust_table_absent"
"""`config.toml` 里没有 `[hooks.state]`——Codex 还没信任过任何 hook。"""

TRUST_STATUS_UNAVAILABLE = "unavailable"
"""核对本身做不了，具体原因由 error_code 单独记录。**不是**「一切正常」。"""

TRUST_STATUS_CHECK_COVERAGE_INCOMPLETE = "check_coverage_incomplete"
"""信任表读到了，但核对**没覆盖到该覆盖的东西**：一条都没核到（``checked == 0``）、有源文件
缺失/不可读（``missing_sources``），或某个源在信任表里连一个键都没有
（``sources_without_any_trust_entry``）。

这是本模块**唯一要避免的失败模式**的落点：「一条都没核到」绝不能被呈现成「全部一致」。
"""

RETRUST_REMEDIATION = (
    "重新信任这些 hook：在 Codex TUI 里交互确认，或把下面列出的 current_fingerprint 手工写进 "
    "`~/.codex/config.toml` 对应 `[hooks.state.\"<key>\"]` 的 `trusted_hash`。"
    "注意 `codex plugin add`（重装）只刷新 cache 快照、**不**更新 trusted_hash。"
)

INCOMPLETE_COVERAGE_REMEDIATION = (
    "先把「核对不到」修掉再谈受信：确认传给核对的 hook 源路径就是宿主真正加载的那一份"
    "（插件 bundled 源在**已部署的 marketplace payload 根**下，不在 `$HOME/plugins/` 下），"
    "并确认 Codex 至少信任过一次该源——从没被信任过的 hook 在信任表里没有任何键，"
    "它一次都不会执行。"
)

RVF_PLUGIN_NAME = "review-validate-fix"

RVF_BUNDLED_HOOK_SOURCE_RELATIVE_PATHS = ("hooks/hooks.json",)
"""Codex 按约定发现的插件 bundled hook 源（相对 plugin payload 根）。

刻意写成显式常量而不是 ``rglob("hooks.json")``：payload 里还有别的同名文件时，猜出来的
label 会造出假告警。若 Codex 将来新增发现位置，扩这个元组——而不是让该源静默消失。
"""


def snake_case_event_name(event_name: str) -> str:
    """`PostToolUse` → `post_tool_use`（Codex 的 `hooks.state` 键用 snake_case 事件名）。"""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event_name).lower()


def _canonicalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalized(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalized(item) for item in value]
    return value


def hook_trust_fingerprint(
    *, event_name: str, group: Mapping[str, Any], hook: Mapping[str, Any]
) -> str:
    """复算某条 hook 的 Codex 信任指纹。"""
    normalized_hook = dict(hook)
    normalized_hook.setdefault("async", False)
    normalized_hook.setdefault("timeout", 600)
    normalized_hook.setdefault("type", "command")
    identity: dict[str, Any] = {
        "event_name": snake_case_event_name(event_name),
        "hooks": [normalized_hook],
    }
    if "matcher" in group:
        identity["matcher"] = group["matcher"]
    encoded = json.dumps(
        _canonicalized(identity), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class CodexConfigurationReadError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def read_codex_config_document(codex_home: Path) -> dict[str, Any]:
    """读取配置；把环境不足、文件缺失、读取失败和解析失败分别报告。"""
    config_path = Path(codex_home) / "config.toml"
    if sys.version_info < (3, 11):
        raise CodexConfigurationReadError("python_version_too_old", "读取 Codex TOML 配置需要 Python >=3.11。")
    try:
        import tomllib  # noqa: PLC0415
    except ImportError as exc:
        raise CodexConfigurationReadError("python_dependency_missing", "Python 环境缺少 tomllib。") from exc
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CodexConfigurationReadError("configuration_missing", f"Codex 配置文件不存在：{config_path}") from exc
    except OSError as exc:
        raise CodexConfigurationReadError("configuration_read_failed", f"Codex 配置文件读取失败：{config_path}: {exc}") from exc
    except UnicodeError as exc:
        raise CodexConfigurationReadError("configuration_parse_failed", f"Codex 配置不是有效 UTF-8：{config_path}: {exc}") from exc
    try:
        return tomllib.loads(config_text)
    except ValueError as exc:
        raise CodexConfigurationReadError("configuration_parse_failed", f"Codex TOML 配置解析失败：{config_path}: {exc}") from exc


def read_codex_hook_trust_table(codex_home: Path) -> dict[str, Any] | None:
    """读 `[hooks.state]`；配置有效但无该表时返回 None，读取错误保留分类。"""
    parsed = read_codex_config_document(codex_home)
    state = (parsed.get("hooks") or {}).get("state")
    return state if isinstance(state, dict) else None


CODEX_CONFIG_PLUGIN_REGISTRATION_TABLE = "plugins"
"""``[plugins."<selector>"]``——插件注册本身，重复 skill 清单与重复 hook 的直接来源。"""

CODEX_CONFIG_HOOK_TRUST_TABLE = "hooks.state"
"""``[hooks.state."<selector>:<相对路径>:<event>:<gi>:<hi>"]``——bundled hook 的信任条目。

**实测（codex-cli 0.147.0）：`codex plugin remove <selector>` 不清理这张表**——它会删掉
plugin cache 树与 ``[plugins."<selector>"]``，却把该 selector 的信任条目原样留下。所以「只在
这张表里出现」的 selector 必须给出**手工删除**的指引，而不是又建议跑一遍 remove。
"""


def rvf_plugin_selector_registration_sources_in_codex_config(
    config_document: Mapping[str, Any] | None,
) -> dict[str, tuple[str, ...]]:
    """`config.toml` 里每个 ``review-validate-fix@<marketplace>`` selector 出现在哪几张表。

    两张表都要扫，且必须分辨来源——因为两者的清理手段不同（见
    ``CODEX_CONFIG_HOOK_TRUST_TABLE``）。只做识别，不做判断：调用方决定「多于一个 selector」
    该是告警还是阻断。
    """
    if not isinstance(config_document, Mapping):
        return {}
    selector_prefix = f"{RVF_PLUGIN_NAME}@"
    sources: dict[str, list[str]] = {}

    def record(selector: str, table_name: str) -> None:
        tables = sources.setdefault(selector, [])
        if table_name not in tables:
            tables.append(table_name)

    plugins_table = config_document.get(CODEX_CONFIG_PLUGIN_REGISTRATION_TABLE)
    if isinstance(plugins_table, Mapping):
        for key in plugins_table:
            if isinstance(key, str) and key.startswith(selector_prefix):
                record(key, CODEX_CONFIG_PLUGIN_REGISTRATION_TABLE)
    hooks_table = config_document.get("hooks")
    trust_table = hooks_table.get("state") if isinstance(hooks_table, Mapping) else None
    if isinstance(trust_table, Mapping):
        for key in trust_table:
            if not isinstance(key, str) or not key.startswith(selector_prefix):
                continue
            # key = "<plugin>@<marketplace>:<相对路径>:<event>:<gi>:<hi>"
            record(key.split(":", 1)[0], CODEX_CONFIG_HOOK_TRUST_TABLE)
    return {
        selector: tuple(sources[selector]) for selector in sorted(sources)
    }


def rvf_plugin_selectors_in_codex_config(
    config_document: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """`config.toml` 里出现过的所有 ``review-validate-fix@<marketplace>`` selector（去重排序）。"""
    return tuple(
        rvf_plugin_selector_registration_sources_in_codex_config(config_document)
    )


def verify_codex_hook_trust(
    *,
    codex_home: Path,
    hook_sources: Iterable[tuple[str, Path]],
) -> dict[str, Any]:
    """核对给定 hook 源的当前指纹与 Codex 信任表是否一致。

    ``hook_sources`` 是 ``(source_label, hooks_json_path)`` 序列；``source_label`` 必须与
    `hooks.state` 键里的 `<source>` 段逐字一致（``CODEX_HOME/hooks.json`` 用绝对路径，插件
    bundled 源用 ``<plugin>@<marketplace>:<相对路径>``）。

    只读、幂等、绝不改 live config。
    """
    try:
        trust_table = read_codex_hook_trust_table(codex_home)
    except CodexConfigurationReadError as exc:
        return {
            "status": TRUST_STATUS_UNAVAILABLE,
            "checked": 0,
            "untrusted": [],
            "error_code": exc.error_code,
            "note": str(exc),
        }
    if trust_table is None:
        return {
            "status": TRUST_STATUS_UNAVAILABLE,
            "checked": 0,
            "untrusted": [],
            "error_code": "configuration_trust_table_missing",
            "note": f"Codex 配置中缺少 [hooks.state]：{Path(codex_home) / 'config.toml'}",
        }

    checked = 0
    untrusted: list[dict[str, Any]] = []
    missing_sources: list[str] = []
    sources_without_any_trust_entry: list[dict[str, Any]] = []
    for source_label, hooks_json_path in hook_sources:
        path = Path(hooks_json_path)
        if not path.is_file():
            missing_sources.append(f"{source_label} -> {path}")
            continue
        try:
            hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
        except (OSError, ValueError, AttributeError):
            missing_sources.append(f"{source_label} -> {path}（不可读 / 非法 JSON）")
            continue
        # 源文件在，但信任表里连一个以它为前缀的键都没有 = 这个源**从没被信任过**。
        # 最危险的正是这一种：新装 / 换了 marketplace label 的 bundled hook，Codex 一次都不会
        # 执行它，而按 key 逐条查表只会得到「没有条目」，不会告诉你「整个源都没登记过」。
        source_key_prefix = f"{source_label}:"
        if not any(
            isinstance(key, str) and key.startswith(source_key_prefix) for key in trust_table
        ):
            sources_without_any_trust_entry.append(
                {"source": source_label, "path": str(path)}
            )
        for event_name, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for group_index, group in enumerate(groups):
                for hook_index, hook in enumerate(group.get("hooks") or []):
                    checked += 1
                    key = (
                        f"{source_label}:{snake_case_event_name(event_name)}"
                        f":{group_index}:{hook_index}"
                    )
                    current = hook_trust_fingerprint(
                        event_name=event_name, group=group, hook=hook
                    )
                    recorded = (trust_table.get(key) or {}).get("trusted_hash")
                    if recorded == current:
                        continue
                    untrusted.append(
                        {
                            "key": key,
                            "source": source_label,
                            "event_name": event_name,
                            "current_fingerprint": current,
                            "recorded_fingerprint": recorded,
                            "reason": (
                                "no_trust_entry" if recorded is None else "fingerprint_mismatch"
                            ),
                        }
                    )

    # 覆盖面不完整 = 本次核对的结论**不能**被读成「一切正常」，无论 untrusted 是不是空的。
    coverage_incomplete = bool(
        checked == 0 or missing_sources or sources_without_any_trust_entry
    )
    if not trust_table:
        status = TRUST_STATUS_TRUST_TABLE_ABSENT
    elif untrusted:
        status = TRUST_STATUS_UNTRUSTED_ENTRIES
    elif coverage_incomplete:
        status = TRUST_STATUS_CHECK_COVERAGE_INCOMPLETE
    else:
        status = TRUST_STATUS_TRUSTED
    result: dict[str, Any] = {
        "status": status,
        "checked": checked,
        "untrusted": untrusted,
        "missing_sources": missing_sources,
        "sources_without_any_trust_entry": sources_without_any_trust_entry,
        "coverage_incomplete": coverage_incomplete,
    }
    coverage_notes: list[str] = []
    if checked == 0:
        coverage_notes.append(
            "一条 hook 都没被核对到（checked=0）。这**不是**「全部一致」——"
            "要么传进来的 hook 源一个都不存在，要么源里根本没有 hook。"
        )
    if missing_sources:
        coverage_notes.append(
            f"{len(missing_sources)} 个 hook 源缺失 / 不可读，它们完全没进核对。"
        )
    if sources_without_any_trust_entry:
        labels = "、".join(
            entry["source"] for entry in sources_without_any_trust_entry
        )
        coverage_notes.append(
            f"{len(sources_without_any_trust_entry)} 个 hook 源在信任表里**一个键都没有**"
            f"（{labels}）——即从没被信任过，Codex 会一次都不执行它们。"
        )
    if untrusted:
        result["remediation"] = RETRUST_REMEDIATION
        # 全灭 = 大概率是本模块的指纹复算跟 Codex 漂了，而不是每条 hook 各自坏了。
        # 明说出来，免得把一个工具问题读成 N 个部署问题。
        if checked and len(untrusted) == checked and not sources_without_any_trust_entry:
            coverage_notes.insert(
                0,
                "被核对的每一条都不匹配。比起「所有 hook 同时失信」，更可能是 Codex 改了指纹算法"
                "而本模块没跟上——请先用 `codex` 源码或 TUI 交互信任一条来校准，再判断部署。",
            )
    elif coverage_incomplete:
        result["remediation"] = INCOMPLETE_COVERAGE_REMEDIATION
    if coverage_notes:
        result["note"] = " ".join(coverage_notes)
    return result


def rvf_codex_hook_sources(
    *,
    codex_home: Path,
    deployed_plugin_root: Path | None,
    trust_table: Mapping[str, Any] | None,
    deployed_plugin_selector: str | None = None,
) -> list[tuple[str, Path]]:
    """列出与 RVF 相关的 hook 源：``CODEX_HOME/hooks.json`` + 每个插件 bundled 源。

    bundled 源从**两个方向**收集，缺一不可：

    ① **信任表反查**——已登记过的 label（``<plugin>@<marketplace>:<相对路径>``）不去猜，
       同一个 plugin 可以经多个 marketplace 登记（本机实测就有两个）。

    ② **磁盘发现**——只靠 ①，一个**从没被信任过**的 bundled hook 在信任表里没有任何键，
       于是整个源都不会进核对，也不会出现在 ``untrusted`` 里。那恰恰是最危险的一种
       （新装 = Codex 一次都不会执行它）。所以 payload 根下实际存在的 bundled 源要用
       ``deployed_plugin_selector`` 造出它**本该有的** label 补进来，让它以
       ``no_trust_entry`` 的形式被看见，而不是静默消失。

    ``deployed_plugin_selector`` 形如 ``review-validate-fix@<marketplace>``，应当取自安装器
    回执（``plugin_selector``）——不要在这里硬拼 marketplace 名。
    """
    sources: list[tuple[str, Path]] = []
    home_hooks = Path(codex_home) / "hooks.json"
    if home_hooks.is_file():
        sources.append((str(home_hooks.resolve()), home_hooks))
    if deployed_plugin_root is None:
        return sources
    plugin_root = Path(deployed_plugin_root)
    seen_labels: set[str] = set()
    if isinstance(trust_table, Mapping):
        for key in trust_table:
            if not isinstance(key, str) or key.startswith("/"):
                continue
            # key = "<plugin>@<marketplace>:<相对路径>:<event>:<gi>:<hi>"
            parts = key.split(":")
            if len(parts) < 5:
                continue
            label = ":".join(parts[:-3])
            relative = parts[-4]
            if not label.startswith(f"{RVF_PLUGIN_NAME}@") or label in seen_labels:
                continue
            seen_labels.add(label)
            bundled = plugin_root / relative
            if bundled.is_file():
                sources.append((label, bundled))
    if deployed_plugin_selector:
        for relative in RVF_BUNDLED_HOOK_SOURCE_RELATIVE_PATHS:
            label = f"{deployed_plugin_selector}:{relative}"
            if label in seen_labels:
                continue
            bundled = plugin_root / relative
            if bundled.is_file():
                seen_labels.add(label)
                sources.append((label, bundled))
    return sources


def _self_check() -> None:
    """runnable check：指纹算法的三条已知性质（含 statusMessage 参与、默认值补齐）。"""
    group = {"matcher": "*", "hooks": [{"command": "echo hi", "timeout": 5, "type": "command"}]}
    hook = group["hooks"][0]
    baseline = hook_trust_fingerprint(event_name="PostToolUse", group=group, hook=hook)
    assert baseline.startswith("sha256:") and len(baseline) == len("sha256:") + 64

    # ① 事件名参与：同一条 hook 挂不同事件下指纹不同。
    assert hook_trust_fingerprint(event_name="Stop", group=group, hook=hook) != baseline
    # ② matcher 参与：group 上有没有 matcher 会改变指纹。
    assert (
        hook_trust_fingerprint(
            event_name="PostToolUse", group={"hooks": [hook]}, hook=hook
        )
        != baseline
    )
    # ③ statusMessage 参与：漏掉它会让所有带状态文案的 hook 被误判成 Modified
    #    （本机实盘验证时正是这一项让 15 条里的 6 条对不上）。
    assert (
        hook_trust_fingerprint(
            event_name="PostToolUse", group=group, hook={**hook, "statusMessage": "x"}
        )
        != baseline
    )
    # ④ 默认值补齐是幂等的：显式写出默认值与省略它们必须同指纹。
    assert (
        hook_trust_fingerprint(
            event_name="PostToolUse",
            group=group,
            hook={**hook, "async": False},
        )
        == baseline
    )
    assert snake_case_event_name("PostToolUse") == "post_tool_use"
    assert snake_case_event_name("Stop") == "stop"
    print("codex_hook_trust_fingerprint self-check OK")


if __name__ == "__main__":
    _self_check()
