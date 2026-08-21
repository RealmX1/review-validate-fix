"""在 ``~/.codex/config.toml`` 里把 RVF **plugin bundled** hook 注册整体停用。

## 为什么这份 bundled 注册在 Codex 上是纯负债

RVF 在 Codex 上有两条注册通道：

| 通道 | 路径形态 | Codex 上的实际作用 |
|---|---|---|
| ``~/.codex/hooks.json``（installer 烘焙） | ``~/.rvf/developer-plugin-marketplaces/…``，**不带版本号、跨部署不变** | ✅ 真正干活 |
| plugin bundled ``hooks/hooks.json`` | ``~/.codex/plugins/cache/<mkt>/<plugin>/<version>/``，**每次换 commit 换目录** | ❌ 恒定 no-op |

bundled 那份之所以恒定 no-op，是因为 ``hooks/_claude_hook_entry.py`` 的
``is_foreign_invocation`` 判定 Codex 后一律静默 ``return 0``——它只服务 Claude Code。
于是它在 Codex 侧提供的唯一东西就是**一条会悬空的版本化路径**：``codex plugin remove``
（每次部署都会对 canonical 跑一次）删掉整棵 payload 树后，已在运行的会话仍按启动时的注册
快照去 spawn 它。

## 隔离 CODEX_HOME 实测（codex-cli 0.147.0）

| 观察 | 结果 |
|---|---|
| bundled ``hooks/hooks.json`` 是否被 plugin loader 按约定加载 | ✅ 是（``[hooks.state]`` 里有对应条目、且 hook 真的执行） |
| ``codex exec`` 下未受信任的 hook | 静默跳过——必须先有 ``trusted_hash`` 才会跑 |
| ``[hooks.state."<key>"].enabled = false`` | ✅ **抑制该条 bundled hook**，同会话里 ``~/.codex/hooks.json`` 的 hook 照常执行 |
| ``codex plugin add`` / ``remove`` / 再 ``add`` 之后 | ``enabled = false`` **原样保留**（``plugin remove`` 本来就不清 ``[hooks.state]``） |

因为 ``enabled = false`` 跨 ``plugin add`` 存活，写入时机不敏感；installer 放在 ``plugin add``
之后写，纯粹是为了让「装完即抑制」这件事在阅读顺序上也成立。

## 为什么做文本级手术而不是整份重写 TOML

``config.toml`` 是 live 基础设施，里面有大量与 RVF 无关的用户配置。标准库没有 TOML writer，
整份重写等于用一个有损的往返把用户配置洗一遍。因此这里**只按 key 精确切块**、只动
``enabled`` 那一行，其余字节（含 ``trusted_hash``、注释、空行）原样保留。

## 文本级手术的两道安全网

文本级手术的代价是「认不出表头」会静默退化成「追加一个同名表」，而重复表会让 ``tomllib``
直接判整份 config 非法。既然写的是 live 用户配置，这里刻意加了两道网：

1. **表头识别按 TOML 语义而不是按字面量**：容忍行尾注释、单引号（literal string）键、
   点号与方括号周围的空白。
2. **写回前先用 ``tomllib`` 解析一遍新文本**，解析不过就整体放弃写入。抑制是**加固**，
   宁可不抑制，也不能把用户配置写坏——同理，一切读写失败都只**如实报进结果对象**、
   不外抛，绝不让加固动作把部署本身弄挂（``scripts/install_to_codex.py`` 的
   ``_bundled_plugin_hooks_document`` 读不到就返回空文档，正是这个取舍的另一半）。

写入本身走「同目录临时文件 + ``os.replace``」原子替换，因此不存在「先截断再写」的半截窗口：
进程在任何时刻被打断，``config.toml`` 要么是旧的完整内容、要么是新的完整内容。
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


BUNDLED_HOOKS_JSON_RELATIVE_PATH = "hooks/hooks.json"
"""Codex plugin loader 按约定加载的 bundled hooks 文件（也是 ``hooks.state`` 键里的 ``<source>`` 后半段）。"""

_HOOK_STATE_ENABLED_LINE = re.compile(r"^\s*enabled\s*=")


def _hook_state_table_header_pattern(hook_state_key: str) -> re.Pattern[str]:
    """匹配 ``[hooks.state."<key>"]`` 这一行表头的正则。

    刻意不做字面量相等比较：TOML 里同一个表头有多种合法写法，任何一种认不出来，
    调用方都会走进「该表不存在」分支去**追加一个同名表** ⇒ 重复表 ⇒ 整份 config 非法。
    这里容忍的三类差异是：

    - 行尾注释：``[hooks.state."<key>"]  # 手工标注``
    - 单引号（TOML literal string）键：``[hooks.state.'<key>']``
    - 点号 / 方括号周围的空白：``[ hooks.state . "<key>" ]``

    hook state key 本身由 :func:`bundled_plugin_hook_state_keys` 生成，只含
    ``@ : / . -`` 与字母数字，两种引号下的字面含义完全一致，不需要处理转义。
    """
    quoted_key = re.escape(hook_state_key)
    return re.compile(
        r"^\s*\[\s*hooks\s*\.\s*state\s*\.\s*"
        rf"(?:\"{quoted_key}\"|'{quoted_key}')"
        r"\s*\]\s*(?:#.*)?$"
    )


def _enabled_line_value(line: str) -> str:
    """取 ``enabled = <value>`` 里的值，忽略行尾注释。

    ``enabled`` 是布尔，值里不可能出现 ``#``，所以按第一个 ``#`` 截断是安全的。
    不忽略注释的话，``enabled = false  # 之前手工关的`` 会被误判成「还没关」，
    于是每次部署都把用户那条注释洗掉、并在回执里谎报「本次新关闭了一条」。
    """
    return line.split("=", 1)[1].split("#", 1)[0].strip()


def snake_case_hook_event_name(event_name: str) -> str:
    """``PostToolUse`` → ``post_tool_use``（Codex 的 ``hooks.state`` 键用 snake_case）。

    与 ``scripts/codex_hook_trust_fingerprint.py`` 的同名函数是两份独立实现：那份是
    deploy-time-only 的独立工具、不随 payload 分发，本模块则要跟着 ``core/`` 进 payload。
    ``tests/test_codex_bundled_plugin_hook_suppression.py`` 有等价性 gate 钉住两者不漂移。
    """
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event_name).lower()


def bundled_plugin_hook_state_keys(
    *,
    plugin_selector: str,
    bundled_hooks_document: Mapping[str, Any],
    bundled_hooks_relative_path: str = BUNDLED_HOOKS_JSON_RELATIVE_PATH,
) -> tuple[str, ...]:
    """枚举 bundled hooks 文档在 ``[hooks.state]`` 里对应的全部键。

    键格式：``<plugin>@<marketplace>:<plugin 内相对路径>:<snake_event>:<group>:<hook>``。
    刻意从文档本身枚举而不是硬编码事件名——bundled ``hooks.json`` 将来增删事件时，
    这里自动跟上，不会留下一条没被抑制的注册。
    """
    hooks = bundled_hooks_document.get("hooks")
    if not isinstance(hooks, Mapping):
        return ()
    keys: list[str] = []
    for event_name, groups in hooks.items():
        if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, Mapping):
                continue
            entries = group.get("hooks")
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                continue
            for hook_index in range(len(entries)):
                keys.append(
                    f"{plugin_selector}:{bundled_hooks_relative_path}"
                    f":{snake_case_hook_event_name(str(event_name))}"
                    f":{group_index}:{hook_index}"
                )
    return tuple(keys)


@dataclass(frozen=True)
class CodexBundledPluginHookSuppressionOutcome:
    """一次抑制写入的结果，供部署回执留痕。"""

    config_path: Path
    disabled_hook_state_keys: tuple[str, ...]
    already_disabled_hook_state_keys: tuple[str, ...]
    created_hook_state_keys: tuple[str, ...]
    config_written: bool
    config_write_failure_reason: str | None = None
    """抑制**没能落地**的原因；成功或本就无事可做时为 ``None``。

    抑制是加固、失败不阻断部署，所以失败只能靠这个字段被看见——它会进部署回执。
    ``None`` 之外的任何值都意味着「这台机器上 bundled hook 没被抑制」，此时三个 key
    元组一律为空，免得回执自称做了没做的事。
    """


def config_toml_text_with_hook_state_entries_disabled(
    config_toml_text: str, hook_state_keys: Sequence[str]
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """返回 ``(新文本, 本次改成 false 的键, 本来就是 false 的键, 新建块的键)``。

    纯函数、无 IO，便于逐条 gate。
    """
    lines = config_toml_text.splitlines(keepends=True)
    newly_disabled: list[str] = []
    already_disabled: list[str] = []
    created: list[str] = []

    for key in hook_state_keys:
        header = f'[hooks.state."{key}"]'
        header_pattern = _hook_state_table_header_pattern(key)
        header_index = next(
            (
                index
                for index, line in enumerate(lines)
                if header_pattern.match(line)
            ),
            None,
        )
        if header_index is None:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append("\n")
            lines.append(f"{header}\n")
            lines.append("enabled = false\n")
            created.append(key)
            continue
        block_end = len(lines)
        for index in range(header_index + 1, len(lines)):
            if lines[index].lstrip().startswith("["):
                block_end = index
                break
        enabled_index = next(
            (
                index
                for index in range(header_index + 1, block_end)
                if _HOOK_STATE_ENABLED_LINE.match(lines[index])
            ),
            None,
        )
        if enabled_index is None:
            lines.insert(header_index + 1, "enabled = false\n")
            newly_disabled.append(key)
            continue
        if _enabled_line_value(lines[enabled_index]) == "false":
            already_disabled.append(key)
            continue
        lines[enabled_index] = "enabled = false\n"
        newly_disabled.append(key)

    return "".join(lines), tuple(newly_disabled), tuple(already_disabled), tuple(created)


def _unparsable_config_toml_reason(config_toml_text: str) -> str | None:
    """新文本能被 ``tomllib`` 解析就返回 ``None``，否则返回该放弃写入的原因。

    ``tomllib`` 缺席（Python < 3.11）同样按「不可写」处理：校验不了就等于没有安全网，
    而这份文件是 live 用户配置——见模块 docstring 的取舍。
    """
    try:
        import tomllib  # noqa: PLC0415 - stdlib ≥3.11；缺席时按「无法校验」处理
    except ImportError:
        return "无 tomllib（Python < 3.11），无法在写回前校验 config.toml 合法性，放弃写入"
    try:
        tomllib.loads(config_toml_text)
    except ValueError as error:  # tomllib.TOMLDecodeError 是 ValueError 子类
        return f"改写后的 config.toml 无法被 tomllib 解析（放弃写入）：{error}"
    return None


def _atomically_replace_file_text(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace`` 原子替换，杜绝「先截断再写」的半截窗口。

    刻意沿用原文件的权限位：这是 live 用户配置，不该因为一次加固写入被改权限。
    任何失败都保证临时文件不残留。
    """
    original_mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".rvf-suppression-tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _suppression_not_applied(
    config_path: Path, config_write_failure_reason: str
) -> CodexBundledPluginHookSuppressionOutcome:
    """一次没落地的抑制：key 元组一律清空，只留失败原因。"""
    return CodexBundledPluginHookSuppressionOutcome(
        config_path=config_path,
        disabled_hook_state_keys=(),
        already_disabled_hook_state_keys=(),
        created_hook_state_keys=(),
        config_written=False,
        config_write_failure_reason=config_write_failure_reason,
    )


def disable_bundled_plugin_hooks_in_codex_config(
    *,
    codex_home: Path,
    plugin_selector: str,
    bundled_hooks_document: Mapping[str, Any],
    bundled_hooks_relative_path: str = BUNDLED_HOOKS_JSON_RELATIVE_PATH,
) -> CodexBundledPluginHookSuppressionOutcome:
    """把该 plugin 的 bundled hook 注册在 ``config.toml`` 里全部置为 ``enabled = false``。

    ``config.toml`` 不存在时**不创建**：没有 config 就说明这台机器上还没有 Codex 注册，
    凭空造一个 ``[hooks.state]`` 只会留下无主的孤儿条目。

    **本函数不为任何可预期的读写失败抛异常。** 它被 ``install_to_codex.py`` 放在
    ``codex plugin add`` **之后**调用，抛出去就会把一次已经改过 live 状态的部署打断在半路、
    并让 rollback 重跑同一段（有把 ``rollback_failed`` 也一起点着的风险）。抑制是加固，
    失败一律落到 ``config_write_failure_reason`` 上进部署回执。

    只捕获 ``OSError`` / ``ValueError``（权限、瞬态 IO、非 UTF-8 内容、非法 TOML）——
    ``NameError``/``TypeError`` 这类自身 bug 仍然照常炸出来，不被这层降级掩盖。
    """
    config_path = Path(codex_home) / "config.toml"
    keys = bundled_plugin_hook_state_keys(
        plugin_selector=plugin_selector,
        bundled_hooks_document=bundled_hooks_document,
        bundled_hooks_relative_path=bundled_hooks_relative_path,
    )
    if not keys or not config_path.is_file():
        return CodexBundledPluginHookSuppressionOutcome(
            config_path=config_path,
            disabled_hook_state_keys=(),
            already_disabled_hook_state_keys=(),
            created_hook_state_keys=(),
            config_written=False,
        )
    try:
        original = config_path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        return _suppression_not_applied(config_path, f"读不到 {config_path}：{error}")
    updated, newly_disabled, already_disabled, created = (
        config_toml_text_with_hook_state_entries_disabled(original, keys)
    )
    if updated == original:
        return CodexBundledPluginHookSuppressionOutcome(
            config_path=config_path,
            disabled_hook_state_keys=newly_disabled,
            already_disabled_hook_state_keys=already_disabled,
            created_hook_state_keys=created,
            config_written=False,
        )
    unparsable_reason = _unparsable_config_toml_reason(updated)
    if unparsable_reason is not None:
        return _suppression_not_applied(config_path, unparsable_reason)
    try:
        _atomically_replace_file_text(config_path, updated)
    except (OSError, ValueError) as error:
        return _suppression_not_applied(config_path, f"写不回 {config_path}：{error}")
    return CodexBundledPluginHookSuppressionOutcome(
        config_path=config_path,
        disabled_hook_state_keys=newly_disabled,
        already_disabled_hook_state_keys=already_disabled,
        created_hook_state_keys=created,
        config_written=True,
    )
