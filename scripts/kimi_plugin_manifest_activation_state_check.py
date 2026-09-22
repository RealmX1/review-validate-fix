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
import re
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

# Kimi 的 `installed.json` record 里 `source` 是**安装来源种类的枚举**（路径归 `originalSource`），
# `github` 只对 kind=="github" 有值。0.38.0 起 `pluginService.listPlugins` 的出站 zod 校验逐字是
# `source: enum(["local-path","zip-url","github"])` 与 `github: <object>.optional()`；写成路径、或把
# `github` 写成 `null`（`.optional()` 接受缺省、**不**接受 null），整个 `/plugins` 面板就报
# "output validation failed" 打不开。
#
# ⚠️ 这一层与 hook 是**解耦**的：`enabledHooks()` 走的是 `materialize()`→`recordFrom()`，那条路径对
# `source` 不做任何枚举校验（`source: input.source ?? "local-path"` 原样透传）⇒ record 写坏时
# hook 照常派发、只有 UI 全挂。所以它必须是**独立的一态**，不能折进 not_registered/manifest_invalid：
# 报成「没注册」会把人引去查 hook，而 hook 恰恰是好的。
KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KINDS = frozenset({"local-path", "zip-url", "github"})

# `pluginGithubMetadataSchema` 的逐字形状（0.38.0，`packages/kap-server/src/protocol/rest-plugin.ts`）：
#   object({owner: string(), repo: string(),
#           ref: object({kind: enum(["branch","tag","sha"]), value: string()}),
#           installedSha: string().optional()})
# ⇒ 只判 `isinstance(dict)` 是**差一格**：`{}` 在本地看着像 object，到宿主那边照样 output
# validation failed。注意这层 `object()` 是 zod 默认的 **strip**（对照 hook 条目那份逐字
# `.strict()`）⇒ 多余的键会被剥掉而不是判失败，复刻时**不得**把多余键记为违规。
KIMI_PLUGIN_REGISTRY_GITHUB_METADATA_REQUIRED_STRING_FIELD_NAMES = ("owner", "repo")
KIMI_PLUGIN_REGISTRY_GITHUB_REF_KINDS = frozenset({"branch", "tag", "sha"})

# RVF 在 Kimi 上的入口全部是 skill（`/review-validate-fix` 等 6 个），而 Kimi 发现 plugin skill 的
# **唯一**入口是 manifest 的 `skills` 字段：`countDiscoveredPluginSkills()` 逐字是
# `(manifest?.skills ?? []).map(...)`，`roots.length === 0` 时直接 `return 0`——**不会**去扫
# `skills/` 目录。（对照：`agents` 在字段缺省时**会**自动收 `agents/`，skills 没有这个待遇；
# 唯一 fallback 是 plugin 根上放一份 `SKILL.md`，RVF 没有。）
# ⇒ 漏声明 `skills` 是一个零报错状态：plugin `state: ok`、hook 正常、而所有 skill 不存在。
REQUIRED_MINIMUM_DISCOVERABLE_SKILL_COUNT = 1

# 两条命令缺一不可：`/plugins reload` 只把 installed.json 重新 materialize 进**进程级**
# PluginManager；skill 是**会话级**的（`this.skillsReady = this.loadSkills()` 只在 session 构造时跑
# 一次），必须再 `/reload` 或 `/new` 重建会话。Kimi 自己的 TUI 常量逐字是
# `PLUGIN_RELOAD_HINT = "Run /new or /reload to apply plugin changes."`
MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT = (
    "在 Kimi 里先执行 /plugins reload（刷新进程级注册表），再执行 /reload 或 /new（重建会话，"
    "skill 只在会话构造时加载一次）——只做前者会看到「插件在册、Skills 为空」"
)
SKILL_MANIFEST_FILE_NAME = "SKILL.md"

# ⚠️ 「有 SKILL.md」**不等于**「宿主注册了这个 skill」。0.38.0 的 `discoverSkills()` 对每个候选
# 走 `parseAndRegister()` → `parseSkillFromFile()` → `parseSkillText()`，后者对**目录型 skill**
# （basename 恰为 SKILL.md）逐字做这几件事，任何一条不过就 throw，而 `parseAndRegister()` 的
# catch 把 SkillParseError / UnsupportedSkillTypeError **静默吞掉并跳过该 skill**：
#   1. 首行 trim 后必须恰为 "---"，否则 `Missing frontmatter in <path>`；
#   2. 必须有第二条 "---" 围栏，否则 `Missing closing frontmatter fence`；
#   3. 围栏之间必须能被 js-yaml 4.1.1 `load()` 解析，且结果必须是 mapping
#      （`isRecord()` = object && !null && !Array）；
#   4. `type` 必须落在 {undefined, prompt, inline, flow, reference}——注意宿主用的是 `=== void 0`，
#      所以 YAML 的 `null`（`type:` 空值/ `~`）**不是** undefined，会抛 UnsupportedSkillTypeError；
#   5. `name` 与 `description` 都必须是 `nonEmptyString()`（typeof === "string" && trim() !== ""）。
# 而 `/plugins` 面板与 `GET /api/v1/plugins` 的 `skillCount` 逐字是
# `(await discoverSkills({ roots })).length`——同一条路径。⇒ 只数 SKILL.md 文件个数的复刻器，会对
# 「一份只含无效 SKILL.md 的部署」判绿，而宿主 skillCount 是 0：正是本核对器存在要消灭的那个假绿。
SKILL_FRONTMATTER_FENCE_LINE = "---"
HOST_SUPPORTED_SKILL_TYPE_VALUES = frozenset({"prompt", "inline", "flow", "reference"})
HOST_REQUIRED_DIRECTORY_SKILL_FRONTMATTER_FIELD_NAMES = ("name", "description")

HOST_SKILL_REGISTRABILITY_REGISTRABLE = "registrable"
HOST_SKILL_REGISTRABILITY_SKIPPED_BY_HOST = "skipped_by_host"
HOST_SKILL_REGISTRABILITY_UNDECIDABLE = "undecidable_without_yaml_engine"

# js-yaml 4.1.1 的 core schema 隐式标量：这些 plain scalar **不是** string，于是宿主的
# `nonEmptyString()` 返回 undefined。`name: true` / `name: 3` / `type:` 都因此被跳过。
YAML_CORE_SCHEMA_NULL_PLAIN_SCALARS = frozenset({"~", "null", "Null", "NULL"})
YAML_CORE_SCHEMA_BOOLEAN_PLAIN_SCALARS = frozenset(
    {"true", "True", "TRUE", "false", "False", "FALSE"}
)
_YAML_TOP_LEVEL_SIMPLE_KEY_LINE = re.compile(
    r"^(?P<key>[A-Za-z0-9_][A-Za-z0-9_.\-]*)[ \t]*:(?P<value>(?:[ \t].*)?)$"
)
# 十进制分支允许前导零，与 js-yaml 的 `resolveYamlInteger()` 一致（"0755" 在它那儿是整数 755，
# 只有 0b/0x/0o 走特殊分支）。⚠️ 这两条正则**互相兜底**而非各管一段：下面的 float 模式的
# `[0-9][0-9_]*` 分支本身就覆盖了全部十进制串，所以「非字符串」这个结论不依赖本条正则写得多准；
# 保留它只是为了让进制字面量（0x/0o/0b）也被认出来。
_YAML_CORE_SCHEMA_INTEGER = re.compile(
    r"^[-+]?(?:0b[01_]+|0x[0-9a-fA-F_]+|0o[0-7_]+|[0-9][0-9_]*)$"
)
_YAML_CORE_SCHEMA_FLOAT = re.compile(
    r"^(?:[-+]?(?:[0-9][0-9_]*)(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?"
    r"|\.[0-9_]+(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)
_YAML_TIMESTAMP_LOOKALIKE = re.compile(r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ].*)?$")
# 单行 plain scalar 里出现 ": " 或以 ":" 收尾，在 block context 里是 YAML 语法错误
# （"mapping values are not allowed here"）⇒ 宿主整份 frontmatter 解析失败并跳过该 skill。
# 本复刻器不敢替 js-yaml 下这个判断，一律降级为「判不出」。
_YAML_PLAIN_SCALAR_AMBIGUOUS_COLON = re.compile(r":(?:[ \t]|$)")
_YAML_PLAIN_SCALAR_FORBIDDEN_LEADING_INDICATORS = tuple("&*!|>%@`?")

# 复刻器**永远**只是复刻。留一条指向宿主自己那个数字的终验路径，是这份脚本诚实性的一部分。
TERMINAL_HOST_SKILL_COUNT_VERIFICATION_HINT = (
    "终验以宿主自己的数字为准：起 `kimi web` 后 `GET /api/v1/plugins`，看该插件的 `skillCount`"
    "（它逐字就是 `countDiscoveredPluginSkills()` → `(await discoverSkills({roots})).length`）"
)

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
ACTIVATION_STATE_REGISTRY_RECORD_REJECTED_BY_HOST_SCHEMA = (
    "registry_record_rejected_by_host_schema"
)

# 只有 sole_registration 放行。这是「正向证明」而非「排除法」——与 `2806755` 给 Codex 侧
# 定下的判据同源：必须等于 sole_registration 才算好，unavailable / 查不到都**不算**。
ACTIVATION_STATE_EXIT_CODES = {
    ACTIVATION_STATE_SOLE_REGISTRATION: 0,
    ACTIVATION_STATE_NOT_REGISTERED: 1,
    ACTIVATION_STATE_MANIFEST_INVALID: 1,
    ACTIVATION_STATE_DUPLICATE_REGISTRATION: 3,
    ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE: 4,
    ACTIVATION_STATE_REGISTRY_RECORD_REJECTED_BY_HOST_SCHEMA: 5,
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
    declared_skill_directory_paths: list[str] = field(default_factory=list)
    # 只装**宿主会真的注册**的那些（frontmatter 已复算），不是「有 SKILL.md 的目录」。
    discovered_skill_directory_names: list[str] = field(default_factory=list)
    host_skipped_skill_manifests: list[str] = field(default_factory=list)
    host_registrability_undecidable_skill_manifests: list[str] = field(default_factory=list)
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
            "declared_skill_directory_paths": self.declared_skill_directory_paths,
            "discovered_skill_directory_names": sorted(self.discovered_skill_directory_names),
            "host_skipped_skill_manifests": self.host_skipped_skill_manifests,
            "host_registrability_undecidable_skill_manifests": (
                self.host_registrability_undecidable_skill_manifests
            ),
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


def registry_record_github_metadata_violations_against_host_schema(
    github_metadata: Any,
) -> list[str]:
    """复刻 `pluginGithubMetadataSchema`；返回违规描述列表（空列表 = 这份 metadata 过得了校验）。

    `.optional()` 的语义是「可以缺省」，不是「可以为 null」——写 null 是硬失败。
    而「是个 object」远不等于「过得了校验」：owner / repo / ref 都是必备，`ref` 还得是
    `{kind ∈ branch|tag|sha, value}` 这个 object 而不是一个裸 ref 字符串。
    """
    if not isinstance(github_metadata, dict):
        return [
            f"github={github_metadata!r} 既不是 object 也不是缺省"
            "（本地路径安装应当**不写这个键**，而不是写 null）"
        ]

    violations: list[str] = []
    for required_field_name in KIMI_PLUGIN_REGISTRY_GITHUB_METADATA_REQUIRED_STRING_FIELD_NAMES:
        if not isinstance(github_metadata.get(required_field_name), str):
            violations.append(
                f"github.{required_field_name}={github_metadata.get(required_field_name)!r} "
                "不是字符串（`pluginGithubMetadataSchema` 里它是必备 string ⇒ `{}` 这种空 object "
                "在本地看着像 object，到宿主那边照样打不开面板）"
            )

    github_ref = github_metadata.get("ref")
    if not isinstance(github_ref, dict):
        violations.append(
            f"github.ref={github_ref!r} 不是 object（必备，形如 "
            "{kind: branch|tag|sha, value: <字符串>}）"
        )
    else:
        github_ref_kind = github_ref.get("kind")
        # 先判类型再做成员测试：对 frozenset 用 unhashable 值做 `in` 会抛 TypeError。
        if (
            not isinstance(github_ref_kind, str)
            or github_ref_kind not in KIMI_PLUGIN_REGISTRY_GITHUB_REF_KINDS
        ):
            violations.append(
                f"github.ref.kind={github_ref_kind!r} 不在枚举 "
                f"{sorted(KIMI_PLUGIN_REGISTRY_GITHUB_REF_KINDS)} 里"
            )
        if not isinstance(github_ref.get("value"), str):
            violations.append(f"github.ref.value={github_ref.get('value')!r} 不是字符串")

    if "installedSha" in github_metadata and not isinstance(
        github_metadata["installedSha"], str
    ):
        violations.append(
            f"github.installedSha={github_metadata['installedSha']!r} 既不是字符串也不是缺省"
        )

    return violations


def registry_record_violations_against_host_listing_schema(record: Mapping[str, Any]) -> list[str]:
    """复刻 Kimi 0.38.0 `pluginService.listPlugins` 的**出站** zod 校验；返回违规描述列表。

    覆盖面判据是「**既由 `persist()` 落盘、又进入 `pluginSummarySchema`** 的字段」，逐字数下来
    恰好三个：`source` / `originalSource` / `github`（其余 summary 字段——`displayName`、`state`、
    各种 count——都是加载时现场派生的，磁盘上写什么都改不了它们）。用「安装器实际会写哪些字段」
    当判据会漏：安装器每次都写 `originalSource`，它一旦落成 null，`/plugins` 就以与 `source`
    写坏**完全相同的偏斜方式**打不开，而本核对器却判 sole_registration / exit 0。

    返回空列表表示这条 record 能被 `/plugins` 正常列出。
    """
    violations: list[str] = []

    source_kind = record.get("source")
    if not isinstance(source_kind, str):
        # 必须先判类型：`record["source"]` 若是 list/dict，直接对 frozenset 做成员测试会抛
        # `TypeError: unhashable type`。本脚本是部署 gate，崩溃就等于 gate 失效——stdout 变空、
        # JSON 消费方直接炸，退出码还会被读成 not_registered/manifest_invalid，把人引去查一条
        # 根本没坏的链路。任何非字符串一律记为违规。
        violations.append(
            f"source={source_kind!r} 不是字符串 ⇒ 过不了 "
            f"enum({sorted(KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KINDS)})"
            "（它是**来源种类**不是路径；路径应放 originalSource）"
        )
    elif source_kind not in KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KINDS:
        violations.append(
            f"source={source_kind!r} 不在枚举 {sorted(KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KINDS)} 里"
            "（它是**来源种类**不是路径；路径应放 originalSource）"
        )

    # `originalSource: string().optional()`：缺省合法，null / 非字符串是硬失败。
    if "originalSource" in record and not isinstance(record["originalSource"], str):
        violations.append(
            f"originalSource={record['originalSource']!r} 既不是字符串也不是缺省"
            "（安装器**每次都写**这个键，写坏的后果与 source 写坏一模一样：面板全挂、hook 照常触发）"
        )

    if "github" in record:
        violations.extend(
            registry_record_github_metadata_violations_against_host_schema(record["github"])
        )

    return violations


def scan_frontmatter_top_level_string_fields(
    frontmatter_text: str, tracked_field_names: tuple[str, ...]
) -> tuple[dict[str, str | None], str | None]:
    """在**不引入 YAML 引擎**的前提下，判定 tracked 顶层键是否是「非空字符串」。

    返回 ``(fields, undecidable_reason)``：

    * key 不在 ``fields`` ⇒ frontmatter 里没有这个顶层键（宿主侧就是 ``undefined``）；
    * value 为 ``None``   ⇒ 键在，但值**不是**非空字符串（宿主 ``nonEmptyString()`` ⇒ undefined）；
    * value 为 ``str``    ⇒ 键在且是非空字符串（已 trim，与宿主 ``nonEmptyString()`` 同口径）。

    ⚠️ 本仓的部署机不存在任何带 PyYAML 的解释器（``/usr/bin`` 3.9.6、Homebrew 3.14.7、
    ``~/.local`` 3.12.12 实测都没有 ``yaml``），而这份核对器必须能在裸 python3 上跑。手搓一个
    「差不多的 YAML 解析器」会在更深一层重造「复刻器 ≠ 宿主」的同一个假绿。所以这里的纪律是：
    **只判敢判的，判不出就说判不出**——任何本函数没有十足把握的 YAML 形状都返回
    ``undecidable_reason``，调用方据此降级为 ``check_coverage_incomplete``，绝不判绿。
    于是本函数的不精确只会换来**假红（噪声）**，永远换不来假绿。
    """
    fields: dict[str, str | None] = {}
    pending_top_level_key: str | None = None
    if frontmatter_text == "":
        # `yamlText === ""` ⇒ 宿主直接给 `data = {}`：合法 mapping，但 name/description 全缺。
        return fields, None
    for line in frontmatter_text.split("\n"):
        if line.strip() == "":
            continue
        if line[0] in " \t":
            # 缩进行：属于上一个顶层键。若那是我们追踪的键，它的值可能是多行 plain scalar、
            # 嵌套 mapping，也可能整份就此变成 YAML 语法错误——三者宿主结论不同，判不出。
            if pending_top_level_key in tracked_field_names:
                return fields, (
                    f"顶层键 {pending_top_level_key!r} 后面跟了缩进块／多行标量，"
                    "不引 YAML 引擎判不出它到底是不是非空字符串"
                )
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # 注释不打断「上一个顶层键」的归属。
        key_match = _YAML_TOP_LEVEL_SIMPLE_KEY_LINE.match(line)
        if key_match is None:
            return fields, f"frontmatter 里有本复刻器不认识的顶层结构：{stripped!r}"
        key = key_match.group("key")
        if key in fields:
            # js-yaml 逐字 `throwError(state, "duplicated mapping key")` ⇒ 整份 frontmatter 解析失败。
            return fields, f"顶层键 {key!r} 重复 ⇒ js-yaml 抛 duplicated mapping key（本复刻器不替它下判断）"
        pending_top_level_key = key
        if key not in tracked_field_names:
            continue
        raw_value = key_match.group("value").strip()
        resolved, undecidable_reason = _resolve_single_line_yaml_scalar_as_non_empty_string(raw_value)
        if undecidable_reason is not None:
            return fields, f"顶层键 {key!r} 的值判不出：{undecidable_reason}"
        fields[key] = resolved
    return fields, None


def _resolve_single_line_yaml_scalar_as_non_empty_string(
    raw_value: str,
) -> tuple[str | None, str | None]:
    """把单行 YAML 标量解析成「非空字符串或不是」。返回 ``(value_or_None, undecidable_reason)``。"""
    if raw_value == "":
        # 值为空：要么是 null（下一行是新顶层键／文件结束），要么后面跟缩进块。后一种会被调用方
        # 的 pending-key 规则拦成「判不出」；此处这一支只可能是 null ⇒ 不是字符串。
        return None, None
    if raw_value[0] in _YAML_PLAIN_SCALAR_FORBIDDEN_LEADING_INDICATORS:
        return None, f"以 YAML 指示符 {raw_value[0]!r} 开头（锚点／别名／标签／块标量／指令）"
    if raw_value[0] in "{[":
        # flow 集合：无论跨不跨行都**不是**字符串；跨行的话后续缩进行会另行触发判不出。
        return None, None
    if raw_value.startswith("- ") or raw_value == "-":
        return None, "看起来是序列条目"
    if raw_value[0] in "\"'":
        quote = raw_value[0]
        if len(raw_value) < 2 or not raw_value.endswith(quote):
            return None, "引号标量没有在同一行闭合"
        if quote == '"' and "\\" in raw_value:
            return None, "双引号标量里有转义序列（可能解析成纯空白）"
        return (raw_value[1:-1].strip() or None), None
    comment_match = re.search(r"(?:^|[ \t])#", raw_value)
    plain = (raw_value[: comment_match.start()] if comment_match else raw_value).strip()
    if plain == "":
        return None, None
    if _YAML_PLAIN_SCALAR_AMBIGUOUS_COLON.search(plain):
        return None, "plain scalar 里有 ': ' 或以 ':' 收尾（block context 下是 YAML 语法错误）"
    if plain in YAML_CORE_SCHEMA_NULL_PLAIN_SCALARS:
        return None, None
    if plain in YAML_CORE_SCHEMA_BOOLEAN_PLAIN_SCALARS:
        return None, None
    if _YAML_CORE_SCHEMA_INTEGER.match(plain) or _YAML_CORE_SCHEMA_FLOAT.match(plain):
        return None, None
    if _YAML_TIMESTAMP_LOOKALIKE.match(plain):
        return None, "长得像 YAML timestamp，解析结果可能是 Date 而不是字符串"
    return plain, None


def decide_host_skill_registrability(skill_manifest_path: Path) -> tuple[str, str]:
    """判定宿主 ``discoverSkills()`` 会不会真的注册这份 ``SKILL.md``。返回 ``(verdict, reason)``。

    verdict ∈ {``registrable``, ``skipped_by_host``, ``undecidable_without_yaml_engine``}。
    逐条对齐 0.38.0 的 ``parseSkillText()``（校验清单见本文件顶部 ``SKILL_FRONTMATTER_FENCE_LINE``
    上方的注释）；``parseAndRegister()`` 会把抛出的错**静默吞掉**，所以每一条不过都等于「这个
    skill 在宿主那边不存在」。
    """
    try:
        text = skill_manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return (
            HOST_SKILL_REGISTRABILITY_SKIPPED_BY_HOST,
            f"读不出来（{error}）⇒ 宿主 parseSkillFromFile 抛 SkillParseError 并跳过",
        )
    lines = re.split(r"\r?\n", text)
    if (lines[0].strip() if lines else "") != SKILL_FRONTMATTER_FENCE_LINE:
        return (
            HOST_SKILL_REGISTRABILITY_SKIPPED_BY_HOST,
            f"首行不是 {SKILL_FRONTMATTER_FENCE_LINE!r} ⇒ 宿主 `Missing frontmatter`",
        )
    closing_fence_index = next(
        (
            index
            for index, line in enumerate(lines)
            if index > 0 and line.strip() == SKILL_FRONTMATTER_FENCE_LINE
        ),
        None,
    )
    if closing_fence_index is None:
        return (
            HOST_SKILL_REGISTRABILITY_SKIPPED_BY_HOST,
            "frontmatter 没有闭合围栏 ⇒ 宿主 `Missing closing frontmatter fence`",
        )
    frontmatter_text = "\n".join(lines[1:closing_fence_index]).strip()
    tracked = ("type",) + HOST_REQUIRED_DIRECTORY_SKILL_FRONTMATTER_FIELD_NAMES
    fields, undecidable_reason = scan_frontmatter_top_level_string_fields(frontmatter_text, tracked)
    if undecidable_reason is not None:
        return HOST_SKILL_REGISTRABILITY_UNDECIDABLE, undecidable_reason
    if "type" in fields and fields["type"] not in HOST_SUPPORTED_SKILL_TYPE_VALUES:
        # 宿主用 `type === void 0` 判缺省，YAML 的 null **不是** undefined ⇒ 写了 `type:` 空值同样抛。
        return (
            HOST_SKILL_REGISTRABILITY_SKIPPED_BY_HOST,
            f"type={fields['type']!r} 不在 {sorted(HOST_SUPPORTED_SKILL_TYPE_VALUES)} 内 "
            "⇒ 宿主 UnsupportedSkillTypeError",
        )
    for required_field_name in HOST_REQUIRED_DIRECTORY_SKILL_FRONTMATTER_FIELD_NAMES:
        if fields.get(required_field_name) is None:
            return (
                HOST_SKILL_REGISTRABILITY_SKIPPED_BY_HOST,
                f"frontmatter 缺少非空的 {required_field_name!r} ⇒ 宿主 "
                "`Missing required frontmatter field`",
            )
    return HOST_SKILL_REGISTRABILITY_REGISTRABLE, "frontmatter 通过宿主 parseSkillText 的全部校验"


@dataclass
class HostSkillDiscoveryReplication:
    """``discover_skill_directory_names()`` 的复算结果。"""

    resolved_skill_root_directories: list[str] = field(default_factory=list)
    host_registrable_skill_directory_names: list[str] = field(default_factory=list)
    host_skipped_skill_manifests: list[str] = field(default_factory=list)
    host_registrability_undecidable_skill_manifests: list[str] = field(default_factory=list)
    declaration_errors: list[str] = field(default_factory=list)


def discover_skill_directory_names(
    plugin_root: Path,
    declared_skill_entries: Any,
    *,
    root_skill_fallback_enabled: bool = False,
) -> HostSkillDiscoveryReplication:
    """复刻 Kimi 的 skill 发现，并**逐份判定宿主会不会真的注册**。

    复刻 `resolveDirListField("skills", …)` + `walkSkillDir` 的顶层行为：条目必须以 ``./`` 开头、
    解析后必须落在 plugin root 内、必须是目录；随后该目录下每个含 ``SKILL.md`` 的子目录是一个
    **候选**，目录自身的 ``SKILL.md`` 也是一个候选；每个候选再过 `decide_host_skill_registrability()`。

    ⚠️ `resolveDirListField` 对 ``./`` 前缀与越界的处置是 ``severity:"error"``，会让整个 record 的
    ``state`` 翻成 ``error`` ⇒ **连 hook 一起停摆**。所以声明 skills 写错比不写更危险，必须复算。

    **为什么必须解析 frontmatter，而不是把这个数改名叫「候选 SKILL.md 数」了事**

    「降级并诚实命名」那条路留下的是同一个可执行事实：一份只含无效 ``SKILL.md`` 的部署仍然
    ``exit 0``，只是报告上多一句免责声明。而本脚本的判据是**正向证明**（见文件头「只有
    sole_registration 放行」），改名不产生任何正向证据，读者照样会把 exit 0 读成「宿主看得见这些
    skill」。所以这里选「向宿主靠拢」：把宿主会跳过的候选真的踢出计数，让那个部署当场判红。

    **诚实性怎么保住**：复刻器永远只是复刻，所以 (a) 判不出的候选一律进
    ``host_registrability_undecidable_skill_manifests``，调用方据此降级为
    ``check_coverage_incomplete`` 而**不是**判绿；(b) 报告始终附
    ``TERMINAL_HOST_SKILL_COUNT_VERIFICATION_HINT`` 指向宿主自己的 ``skillCount``。

    **已知的剩余不等价**（全部是「本复刻器数得比宿主少」的方向 ⇒ 只可能假红、不可能假绿）：
    宿主还会把 skills 目录下的扁平 ``<name>.md`` 当 skill（且**不**要求 name/description），
    还会向下递归到 ``MAX_SKILL_SCAN_DEPTH = 8`` 层收集嵌套 skill；本复刻器都不数。反过来，
    所有会让本复刻器**多数**的路径都已堵上：frontmatter 无效（本函数）、``rootSkillFallback``
    只认根 SKILL.md（``root_skill_fallback_enabled``）、同一目录被声明两次（root 去重）。
    宿主按 ``name.toLowerCase()`` 去重则最多把 N 压到 1，压不到 0，对「≥1」这条闸无影响。
    """
    replication = HostSkillDiscoveryReplication()

    if declared_skill_entries is None:
        return replication

    if isinstance(declared_skill_entries, str):
        entries = [declared_skill_entries]
    elif isinstance(declared_skill_entries, list) and all(
        isinstance(entry, str) for entry in declared_skill_entries
    ):
        entries = list(declared_skill_entries)
    else:
        replication.declaration_errors.append(
            '"skills" 必须是 string 或 string[]（Kimi 判 severity=error ⇒ 整个插件停摆）'
        )
        return replication

    def classify_candidate(skill_manifest_path: Path, skill_directory_name: str) -> None:
        verdict, reason = decide_host_skill_registrability(skill_manifest_path)
        if verdict == HOST_SKILL_REGISTRABILITY_REGISTRABLE:
            replication.host_registrable_skill_directory_names.append(skill_directory_name)
        elif verdict == HOST_SKILL_REGISTRABILITY_UNDECIDABLE:
            replication.host_registrability_undecidable_skill_manifests.append(
                f"{skill_manifest_path} — {reason}"
            )
        else:
            replication.host_skipped_skill_manifests.append(f"{skill_manifest_path} — {reason}")

    plugin_root_resolved = plugin_root.resolve()
    already_scanned_root_directories: set[Path] = set()
    for entry in entries:
        if not entry.startswith("./"):
            replication.declaration_errors.append(
                f'"skills" 条目必须以 "./" 开头（得到 {entry!r}）⇒ severity=error'
            )
            continue
        candidate = (plugin_root / entry).resolve()
        if candidate != plugin_root_resolved and plugin_root_resolved not in candidate.parents:
            replication.declaration_errors.append(
                f'"skills" 条目解析到插件之外（{entry!r}）⇒ severity=error'
            )
            continue
        if not candidate.is_dir():
            replication.declaration_errors.append(
                f'"skills" 条目不是目录（{entry!r}）⇒ Kimi warn 并跳过该条'
            )
            continue
        replication.resolved_skill_root_directories.append(str(candidate))
        # 宿主 `pushProvidedRoot()` 按 realpath 去重 root；同一目录声明两次只扫一次。
        if candidate in already_scanned_root_directories:
            continue
        already_scanned_root_directories.add(candidate)
        if (candidate / SKILL_MANIFEST_FILE_NAME).is_file():
            classify_candidate(candidate / SKILL_MANIFEST_FILE_NAME, candidate.name)
        if root_skill_fallback_enabled:
            # `scanMode: manifest.rootSkillFallback ? "root-skill-only" : undefined`：置真时宿主
            # **只**看根上那一份 SKILL.md，子目录里的 skill 一个都不收。不复刻这条同样会造出
            # 「复验说 OK 而宿主是空」。
            continue
        for child in sorted(candidate.iterdir()):
            # ⚠️ 宿主的 `entry === "node_modules" || entry.startsWith(".")` 过滤只作用于**递归**
            # （`subdirs`），`directorySkills.add(entry)` 发生在那句 `continue` **之前**——所以
            # `.hidden/SKILL.md` 宿主照收。这里跟着收，否则复刻器会漏数。
            if (child / SKILL_MANIFEST_FILE_NAME).is_file():
                classify_candidate(child / SKILL_MANIFEST_FILE_NAME, child.name)

    return replication


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
            f"运行 RVF 的 Kimi 安装步骤写入 installed.json，然后{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
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
            f"修复 {installed_registry_path} 的 JSON，然后{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
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
            f"运行 RVF 的 Kimi 安装步骤，然后{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
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
            f"手工编辑 {installed_registry_path} 只保留一条 canonical 注册，然后{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
        )
        return report

    record = matching[0]

    registry_record_violations = registry_record_violations_against_host_listing_schema(record)
    if registry_record_violations:
        report.activation_state = ACTIVATION_STATE_REGISTRY_RECORD_REJECTED_BY_HOST_SCHEMA
        report.diagnostics.append(
            "注册条目不满足 Kimi `pluginService.listPlugins` 的出站 schema："
            + "；".join(registry_record_violations)
            + "。后果是**偏斜**的：`/plugins` 面板整个报 output validation failed 打不开，"
            "而 hook 派发走 enabledHooks()、不过这道校验 ⇒ Stop/UserPromptSubmit 很可能仍在正常触发。"
            "别按「插件没装上」去排查。"
        )
        report.remediation.append(
            f"用修好的安装器重写该条注册（source 写枚举 local-path、originalSource 写路径字符串、"
            f"不写 github 键），然后"
            f"{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
        )
        return report

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
            f"重新部署后，{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
        )
        return report

    declared_skill_entries = (
        manifest_document.get("skills") if isinstance(manifest_document, dict) else None
    )
    discovery = discover_skill_directory_names(
        plugin_root,
        declared_skill_entries,
        root_skill_fallback_enabled=bool(
            manifest_document.get("rootSkillFallback")
            if isinstance(manifest_document, dict)
            else False
        ),
    )
    resolved_skill_directories = discovery.resolved_skill_root_directories
    discovered_skill_names = discovery.host_registrable_skill_directory_names
    skill_declaration_errors = discovery.declaration_errors
    report.declared_skill_directory_paths = resolved_skill_directories
    report.discovered_skill_directory_names = discovered_skill_names
    report.host_skipped_skill_manifests = discovery.host_skipped_skill_manifests
    report.host_registrability_undecidable_skill_manifests = (
        discovery.host_registrability_undecidable_skill_manifests
    )

    if skill_declaration_errors:
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        report.diagnostics.append(
            f"manifest 的 skills 声明有错：{skill_declaration_errors}。"
            "标了 severity=error 的那几种会把整条 record 的 state 翻成 error ⇒ "
            "**连 Stop/UserPromptSubmit hook 一起停摆**，比不声明更严重。"
        )
        report.remediation.append(
            'skills 每个条目必须以 "./" 开头、指向 plugin root 内的真实目录，'
            f"改完重新部署，然后{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
        )
        return report

    if len(discovered_skill_names) < REQUIRED_MINIMUM_DISCOVERABLE_SKILL_COUNT:
        if discovery.host_registrability_undecidable_skill_manifests:
            # 判不出来时必须说判不出来：候选存在、但本复刻器不敢替 js-yaml 断言它的 frontmatter，
            # 于是「宿主到底看不看得见」是未知，**不是**「没问题」。
            report.activation_state = ACTIVATION_STATE_CHECK_COVERAGE_INCOMPLETE
            report.diagnostics.append(
                f"没能正向证明宿主会注册到至少 {REQUIRED_MINIMUM_DISCOVERABLE_SKILL_COUNT} 个 skill："
                f"有 {len(discovery.host_registrability_undecidable_skill_manifests)} 份 "
                f"{SKILL_MANIFEST_FILE_NAME} 的 frontmatter 超出本复刻器敢判的范围 "
                f"{discovery.host_registrability_undecidable_skill_manifests}。"
                "本核对器刻意不内嵌 YAML 引擎（部署机没有 PyYAML），判不出就降级，绝不当成没问题。"
            )
            report.remediation.append(
                f"把这些 {SKILL_MANIFEST_FILE_NAME} 的 frontmatter 收敛成简单的顶层 "
                "`name:` / `description:` 单行标量，或者直接用宿主的数字终验——"
                f"{TERMINAL_HOST_SKILL_COUNT_VERIFICATION_HINT}。"
            )
            return report
        report.activation_state = ACTIVATION_STATE_MANIFEST_INVALID
        if discovery.host_skipped_skill_manifests:
            report.diagnostics.append(
                f"skills 目录下有 {SKILL_MANIFEST_FILE_NAME}，但宿主**一个都不会注册**"
                f"（要求至少 {REQUIRED_MINIMUM_DISCOVERABLE_SKILL_COUNT} 个）："
                f"{discovery.host_skipped_skill_manifests}。"
                "这是「文件在那儿 ≠ skill 存在」的形状：`discoverSkills()` 逐份走 "
                "`parseSkillFromFile()`，frontmatter 不合格就被 `parseAndRegister()` 的 catch "
                "静默吞掉，plugin 的 state 仍是 ok、hook 也照常触发，而 `/plugins` 面板的 "
                "skillCount 是 0、RVF 的全部入口都不存在。"
            )
            report.remediation.append(
                f"给每份 {SKILL_MANIFEST_FILE_NAME} 补上合法 frontmatter（首行与结尾各一条 "
                f"`{SKILL_FRONTMATTER_FENCE_LINE}` 围栏，中间至少有非空的 "
                f"{list(HOST_REQUIRED_DIRECTORY_SKILL_FRONTMATTER_FIELD_NAMES)}，"
                f"`type` 若写则须 ∈ {sorted(HOST_SUPPORTED_SKILL_TYPE_VALUES)}），重新部署，然后"
                f"{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
            )
            return report
        report.diagnostics.append(
            f"manifest 的 skills={declared_skill_entries!r} 发现不到任何 {SKILL_MANIFEST_FILE_NAME}"
            f"（要求至少 {REQUIRED_MINIMUM_DISCOVERABLE_SKILL_COUNT} 个）⇒ RVF 在 Kimi 上的全部入口"
            "（/review-validate-fix 等）都不存在。这是一个**零报错**状态："
            "Kimi 只从 manifest.skills 发现 skill，字段缺省时直接返回 0、**不会**去扫 skills/ 目录"
            "（对照 agents 有目录兜底、skills 没有），而 plugin 的 state 仍是 ok、hook 也照常触发。"
        )
        report.remediation.append(
            '在 .kimi-plugin/plugin.json 里声明 "skills": ["./skills/"]，重新部署，然后'
            f"{MANUAL_ACTIVATION_COMMAND_SEQUENCE_HINT}。"
        )
        return report

    report.activation_state = ACTIVATION_STATE_SOLE_REGISTRATION
    report.diagnostics.append(
        "恰好一条 enabled 注册，注册条目通过宿主 listPlugins schema 复算，manifest 可解析，"
        f"RVF 需要的 {list(REQUIRED_HOOK_EVENT_NAMES)} 两个 hook 均通过 schema 复算，"
        f"并从 {resolved_skill_directories} 正向证明宿主会注册 {len(discovered_skill_names)} 个 skill"
        f"（frontmatter 已按宿主 parseSkillText 复算，不是数 {SKILL_MANIFEST_FILE_NAME} 文件个数）。"
    )
    if discovery.host_skipped_skill_manifests or (
        discovery.host_registrability_undecidable_skill_manifests
    ):
        # 放行判据（≥1 个已被正向证明）已经成立，但同目录里还有宿主会跳过／本复刻器判不出的候选，
        # 必须说出来——否则这条 exit 0 会被读成「这些 SKILL.md 宿主全看得见」。
        report.diagnostics.append(
            "注意：本次放行只证明了「至少一个 skill 宿主会注册」，**不**等于宿主 skillCount 等于"
            f"上面这个数。宿主会跳过的候选：{discovery.host_skipped_skill_manifests}；"
            f"本复刻器判不出的候选：{discovery.host_registrability_undecidable_skill_manifests}。"
        )
        report.remediation.append(f"要核对确切数目，{TERMINAL_HOST_SKILL_COUNT_VERIFICATION_HINT}。")
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
