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


# 「宿主会真的注册」的最小合法 SKILL.md。首行/末行围栏 + 非空 name/description 是
# `parseSkillText()` 对**目录型 skill** 的硬要求，缺一份就被 `parseAndRegister()` 静默跳过。
HOST_REGISTRABLE_SKILL_MANIFEST_TEXT = (
    "---\nname: review-validate-fix\ndescription: RVF 入口。\n---\n\n# review-validate-fix\n"
)


def _build_kimi_home(
    root: Path,
    *,
    installed_registry: object,
    hook_entries: list | None,
    root_manifest_document: dict | None = None,
    declares_skills: bool = True,
    skill_manifest_text: str = HOST_REGISTRABLE_SKILL_MANIFEST_TEXT,
    manifest_extra_fields: dict | None = None,
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
    skill_directory = plugin_root / "skills" / "review-validate-fix"
    skill_directory.mkdir(parents=True, exist_ok=True)
    (skill_directory / "SKILL.md").write_text(skill_manifest_text, encoding="utf-8")
    if hook_entries is not None:
        manifest_document = {
            "name": "review-validate-fix",
            "description": "d",
            "hooks": hook_entries,
        }
        if declares_skills:
            manifest_document["skills"] = ["./skills/"]
        if manifest_extra_fields is not None:
            manifest_document.update(manifest_extra_fields)
        (plugin_root / ".kimi-plugin" / "plugin.json").write_text(
            json.dumps(manifest_document), encoding="utf-8"
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
        # 必须是**枚举**（local-path / zip-url / github），不是路径；`github` 与 `capabilities`
        # 在本地路径安装下 Kimi 自己也不写这两个键。fixture 一旦偏离宿主真实形状，
        # 下面那些测试就是在对着一个不存在的世界判绿。
        "source": "local-path",
        "enabled": True,
        "installedAt": "t",
        "updatedAt": "t",
        "originalSource": str(kimi_home / "payload" / "review-validate-fix"),
    }
    record.update(overrides)
    return record


def _run_activation_state_checker(kimi_home: Path) -> subprocess.CompletedProcess:
    """跑一次核对器并原样返回进程结果。

    需要单独暴露它，是因为「核对器**崩了**」与「核对器判了某一态」在 `_inspect` 里看起来一样
    （崩溃时 stdout 为空，`json.loads` 抛 JSONDecodeError）——而崩溃恰恰是本核对器作为部署 gate
    最不能出现的形态，必须能对 stderr 直接下断言。
    """
    return subprocess.run(
        [sys.executable, str(ACTIVATION_STATE_CHECKER), "--json", "--kimi-code-home", str(kimi_home)],
        capture_output=True,
        text=True,
    )


def _inspect(kimi_home: Path) -> tuple[dict, int]:
    completed = _run_activation_state_checker(kimi_home)
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


def test_source_written_as_a_path_is_rejected_by_the_host_listing_schema(tmp_path: Path) -> None:
    """`source` 写成路径 ⇒ 0.38.0 的 `/plugins` 面板整个打不开。

    这是 RVF 部署到 Kimi 后实际发生的故障：`pluginService.listPlugins` 的出站 zod 校验
    `source: enum([...])` 拒收路径字符串。它必须是**独立的一态**——因为 hook 派发走
    `enabledHooks()`、不过这道校验，报成「没注册」会把人引去查一条其实好着的链路。
    """
    kimi_home = _build_kimi_home(
        tmp_path / "bad-source", installed_registry=None, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    record = _registration_record(kimi_home, source=str(kimi_home / "payload" / "review-validate-fix"))
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [record]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "registry_record_rejected_by_host_schema", report
    assert exit_code == 5, report


def test_github_written_as_null_is_rejected_because_optional_is_not_nullable(
    tmp_path: Path,
) -> None:
    """zod 的 `.optional()` 接受「缺省」，**不**接受 `null`。"""
    kimi_home = _build_kimi_home(
        tmp_path / "null-github", installed_registry=None, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home, github=None)]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "registry_record_rejected_by_host_schema", report
    assert exit_code == 5, report


def test_non_string_source_is_a_violation_and_never_crashes_the_deployment_gate(
    tmp_path: Path,
) -> None:
    """`source` 是 list/dict 时必须**记为违规**，而不是让核对器自己抛 TypeError。

    成员测试 `x not in frozenset(...)` 对 unhashable 的 x 抛 `TypeError: unhashable type`。
    本脚本是部署 gate，崩溃 = gate 失效：stdout 变空、JSON 消费方直接炸，而退出码 1 会被读成
    not_registered/manifest_invalid ⇒ 把人引去查一条根本没坏的链路。
    """
    for label, malformed_source in (
        ("list", ["local-path"]),
        ("dict", {"kind": "local-path", "path": "/tmp/x"}),
    ):
        kimi_home = _build_kimi_home(
            tmp_path / f"unhashable-source-{label}",
            installed_registry=None,
            hook_entries=WELL_FORMED_HOOK_ENTRIES,
        )
        (kimi_home / "plugins" / "installed.json").write_text(
            json.dumps({"plugins": [_registration_record(kimi_home, source=malformed_source)]}),
            encoding="utf-8",
        )
        completed = _run_activation_state_checker(kimi_home)
        assert "TypeError" not in completed.stderr, (label, completed.stderr)
        assert completed.stdout, (label, completed.stderr)
        report = json.loads(completed.stdout)
        assert report["activation_state"] == "registry_record_rejected_by_host_schema", (
            label,
            report,
        )
        assert completed.returncode == 5, (label, report)


def test_empty_github_object_is_rejected_because_owner_repo_and_ref_are_required(
    tmp_path: Path,
) -> None:
    """`github` 只判 isinstance(dict) 是**差一格**：`pluginGithubMetadataSchema` 要求 owner/repo/ref。

    宿主逐字是 `object({owner: string(), repo: string(), ref: object({kind: enum([branch,tag,sha]),
    value: string()}), installedSha: string().optional()})` ⇒ `{}` 一样让 `/plugins` 打不开。
    """
    kimi_home = _build_kimi_home(
        tmp_path / "empty-github", installed_registry=None, hook_entries=WELL_FORMED_HOOK_ENTRIES
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home, github={})]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "registry_record_rejected_by_host_schema", report
    assert exit_code == 5, report


def test_github_ref_of_the_wrong_shape_is_rejected(tmp_path: Path) -> None:
    """owner/repo 齐了但 `ref` 形状不对，宿主一样拒收——嵌套一层不等于可以不校验。"""
    kimi_home = _build_kimi_home(
        tmp_path / "bad-github-ref",
        installed_registry=None,
        hook_entries=WELL_FORMED_HOOK_ENTRIES,
    )
    malformed_github = {
        "owner": "RealmX1",
        "repo": "review-validate-fix",
        # 宿主要的是 `{kind, value}` 这个 object，不是一个裸字符串。
        "ref": "main",
    }
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home, github=malformed_github)]}),
        encoding="utf-8",
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "registry_record_rejected_by_host_schema", report
    assert exit_code == 5, report


def test_original_source_written_as_null_is_rejected_because_optional_is_not_nullable(
    tmp_path: Path,
) -> None:
    """`originalSource` 在 `pluginSummarySchema` 里是 `string().optional()`，而安装器**每次都写**它。

    一旦落成 `null` 或非字符串，`/plugins` 会以与 `source` 写坏**完全相同的偏斜方式**打不开
    （面板全挂、hook 照常触发）。它是「源自持久化 record 且进入 pluginSummarySchema」的第三个
    字段——只校验 source 与 github 正是同一个「覆盖面差一格」的错误被复制了一遍。
    """
    kimi_home = _build_kimi_home(
        tmp_path / "null-original-source",
        installed_registry=None,
        hook_entries=WELL_FORMED_HOOK_ENTRIES,
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home, originalSource=None)]}),
        encoding="utf-8",
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "registry_record_rejected_by_host_schema", report
    assert exit_code == 5, report


def test_well_formed_github_metadata_still_passes_so_the_replica_does_not_over_reject(
    tmp_path: Path,
) -> None:
    """负向对照：合规的 github 安装必须照常放行。

    收紧 schema 复算的对偶风险是**过拒**——把一条宿主明明列得出来的记录判成 rejected，
    同样是假信号。zod 的 `object()` 默认是 strip（对照 hook 条目那份逐字 `.strict()`），
    因此多余的键**不**构成违规。
    """
    kimi_home = _build_kimi_home(
        tmp_path / "good-github",
        installed_registry=None,
        hook_entries=WELL_FORMED_HOOK_ENTRIES,
    )
    well_formed_github = {
        "owner": "RealmX1",
        "repo": "review-validate-fix",
        "ref": {"kind": "branch", "value": "main"},
        "installedSha": "0" * 40,
    }
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps(
            {"plugins": [_registration_record(kimi_home, source="github", github=well_formed_github)]}
        ),
        encoding="utf-8",
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "sole_registration", report
    assert exit_code == 0, report


def test_manifest_without_skills_declaration_never_passes(tmp_path: Path) -> None:
    """漏声明 `skills` 是零报错状态：plugin `state: ok`、hook 正常、而所有入口不存在。

    Kimi 只从 `manifest.skills` 发现 skill（`roots.length === 0` 时直接 `return 0`），
    **不会**去扫 `skills/` 目录——即便该目录就在那儿、装满了 SKILL.md。
    """
    kimi_home = _build_kimi_home(
        tmp_path / "no-skills",
        installed_registry=None,
        hook_entries=WELL_FORMED_HOOK_ENTRIES,
        declares_skills=False,
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home)]}), encoding="utf-8"
    )
    report, exit_code = _inspect(kimi_home)
    assert report["activation_state"] == "manifest_invalid", report
    assert exit_code == 1, report
    assert report["discovered_skill_directory_names"] == [], report


# ---------------------------------------------------------------------------
# 「有 SKILL.md」≠「宿主注册了这个 skill」
#
# 宿主 `discoverSkills()` 逐份走 `parseSkillFromFile()` → `parseSkillText()`，frontmatter 不合格
# 就抛错，而 `parseAndRegister()` 的 catch 把错**静默吞掉**并跳过该 skill；`/plugins` 面板与
# `GET /api/v1/plugins` 的 `skillCount` 逐字是 `(await discoverSkills({roots})).length`——同一条路径。
# 所以只数 SKILL.md 文件个数的核对器，会对「一份只含无效 SKILL.md 的部署」判 sole_registration/0，
# 而宿主 skillCount 是 0：那正是这套部署校验存在要消灭的假绿形状，只不过换了一层皮。
#
# 注意本文件上方 `HOST_REGISTRABLE_SKILL_MANIFEST_TEXT` 的由来：这些测试写出来之前，fixture 里
# 那份 SKILL.md 内容是 `"# review-validate-fix"`——一份宿主铁定拒收的文件。也就是说整套
# 「健康部署」测试当时都跑在一个宿主看不见任何 skill 的世界上，而核对器判 sole_registration/0。
# ---------------------------------------------------------------------------


def _inspect_with_skill_manifest(
    tmp_path: Path, label: str, skill_manifest_text: str, **build_kwargs
) -> tuple[dict, int]:
    kimi_home = _build_kimi_home(
        tmp_path / label,
        installed_registry=None,
        hook_entries=WELL_FORMED_HOOK_ENTRIES,
        skill_manifest_text=skill_manifest_text,
        **build_kwargs,
    )
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [_registration_record(kimi_home)]}), encoding="utf-8"
    )
    return _inspect(kimi_home)


def test_skill_md_without_frontmatter_is_not_counted_because_the_host_skips_it(
    tmp_path: Path,
) -> None:
    """issue 的原形：一份只含无效 SKILL.md 的部署，宿主 skillCount 为 0，核对器必须判红。"""
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path, "no-frontmatter", "# review-validate-fix\n"
    )
    assert report["activation_state"] == "manifest_invalid", report
    assert exit_code == 1, report
    assert report["discovered_skill_directory_names"] == [], report
    # 诊断必须点名「文件在那儿但宿主不注册」，而不是含糊的「找不到 SKILL.md」——
    # 后者会把人引去查文件是否存在，而文件恰恰就在那儿。
    assert len(report["host_skipped_skill_manifests"]) == 1, report
    assert "Missing frontmatter" in report["host_skipped_skill_manifests"][0], report


def test_missing_required_frontmatter_field_is_not_counted(tmp_path: Path) -> None:
    """`name`/`description` 缺任意一个，宿主抛 `Missing required frontmatter field` 并跳过。"""
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path, "no-description", "---\nname: review-validate-fix\n---\n\nbody\n"
    )
    assert report["activation_state"] == "manifest_invalid", report
    assert exit_code == 1, report
    assert report["discovered_skill_directory_names"] == [], report
    assert "description" in report["host_skipped_skill_manifests"][0], report


def test_unsupported_skill_type_is_not_counted(tmp_path: Path) -> None:
    """`type` 只接受 prompt/inline/flow/reference；其余抛 UnsupportedSkillTypeError。"""
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path,
        "bad-type",
        "---\nname: review-validate-fix\ndescription: d\ntype: agent\n---\n",
    )
    assert report["activation_state"] == "manifest_invalid", report
    assert exit_code == 1, report
    assert report["discovered_skill_directory_names"] == [], report


def test_empty_type_value_is_not_counted_because_yaml_null_is_not_undefined(
    tmp_path: Path,
) -> None:
    """宿主判缺省用的是 `type === void 0`，而 YAML 的 `type:` 空值是 `null`——两者不等。"""
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path, "null-type", "---\nname: review-validate-fix\ndescription: d\ntype:\n---\n"
    )
    assert report["activation_state"] == "manifest_invalid", report
    assert exit_code == 1, report


def test_supported_type_still_passes(tmp_path: Path) -> None:
    """收紧不能把合法部署一起判死：显式写 `type: prompt` 依然要放行。"""
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path, "prompt-type", "---\nname: review-validate-fix\ndescription: d\ntype: prompt\n---\n"
    )
    assert report["activation_state"] == "sole_registration", report
    assert exit_code == 0, report
    assert report["discovered_skill_directory_names"] == ["review-validate-fix"], report


def test_frontmatter_beyond_the_replicas_reach_degrades_instead_of_passing(
    tmp_path: Path,
) -> None:
    """判不出来时必须说判不出来，绝不当成没问题。

    本核对器刻意**不**内嵌 YAML 引擎（部署机上没有任何带 PyYAML 的解释器），所以块标量这类
    形状超出它敢判的范围。此时正确的出口是 `check_coverage_incomplete`——把「没查到问题」
    等同于「没有问题」，就是在更深一层重造同一个假绿。
    """
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path,
        "block-scalar",
        "---\nname: review-validate-fix\ndescription: |\n  多行说明\n---\n",
    )
    assert report["activation_state"] == "check_coverage_incomplete", report
    assert exit_code == 4, report
    assert report["discovered_skill_directory_names"] == [], report
    assert len(report["host_registrability_undecidable_skill_manifests"]) == 1, report
    # 降级必须同时给出「以宿主自己的数字为准」的终验出路，否则这条 exit 4 是个死胡同。
    assert any("skillCount" in remediation for remediation in report["remediation"]), report


def test_root_skill_fallback_hides_subdirectory_skills_from_the_host(tmp_path: Path) -> None:
    """`rootSkillFallback` 置真 ⇒ 宿主 `scanMode: "root-skill-only"`，子目录 skill 一个都不收。

    不复刻这条同样会造出「复验说 OK 而宿主是空」：skills/ 下每个子目录都有合法 SKILL.md，
    而宿主只看 skills/SKILL.md（不存在）⇒ skillCount 为 0。
    """
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path,
        "root-skill-only",
        HOST_REGISTRABLE_SKILL_MANIFEST_TEXT,
        manifest_extra_fields={"rootSkillFallback": True},
    )
    assert report["activation_state"] == "manifest_invalid", report
    assert exit_code == 1, report
    assert report["discovered_skill_directory_names"] == [], report


def test_valid_frontmatter_is_a_positive_proof_not_a_file_count(tmp_path: Path) -> None:
    """放行必须是正向证明：这个数是「宿主会注册的 skill」，不是「SKILL.md 文件个数」。"""
    report, exit_code = _inspect_with_skill_manifest(
        tmp_path, "valid", HOST_REGISTRABLE_SKILL_MANIFEST_TEXT
    )
    assert report["activation_state"] == "sole_registration", report
    assert exit_code == 0, report
    assert report["discovered_skill_directory_names"] == ["review-validate-fix"], report
    assert report["host_skipped_skill_manifests"] == [], report
    assert report["host_registrability_undecidable_skill_manifests"] == [], report


def test_the_shipped_rvf_payload_is_actually_registrable_by_the_host(tmp_path: Path) -> None:
    """闸门收紧之后，仓库里**真实发布**的那 6 份 SKILL.md 必须仍然逐份可注册。

    这条测试的作用是双向的：既拒绝「收紧到把自家发布物也判死」，也拒绝「复刻器判得太松」——
    它读的是真文件，发布物哪天 frontmatter 写坏了，这里当场红。
    """
    shipped_skills_directory = REPOSITORY_ROOT / "plugins/review-validate-fix/skills"
    shipped_skill_manifests = sorted(shipped_skills_directory.glob("*/SKILL.md"))
    assert shipped_skill_manifests, "发布物里一份 SKILL.md 都没有，fixture 前提已经不成立"

    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    import kimi_plugin_manifest_activation_state_check as checker

    for shipped_skill_manifest in shipped_skill_manifests:
        verdict, reason = checker.decide_host_skill_registrability(shipped_skill_manifest)
        assert verdict == checker.HOST_SKILL_REGISTRABILITY_REGISTRABLE, (
            shipped_skill_manifest,
            verdict,
            reason,
        )


def test_non_string_scalar_names_are_not_counted(tmp_path: Path) -> None:
    """`nonEmptyString()` 要求 `typeof === "string"`，YAML 隐式标量都不是字符串。

    `0755` 这一条是自审时补上的：js-yaml 的 `resolveYamlInteger()` 对带前导零的十进制也返回
    true（只有 0b/0x/0o 走特殊分支），把它当字符串就又是一个「复验说 OK 而宿主是空」。
    """
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    import kimi_plugin_manifest_activation_state_check as checker

    for non_string_scalar in ("true", "TRUE", "false", "null", "~", "42", "0755", "-1.5e3", ".inf"):
        skill_manifest = tmp_path / non_string_scalar.replace(".", "_") / "SKILL.md"
        skill_manifest.parent.mkdir(parents=True, exist_ok=True)
        skill_manifest.write_text(
            f"---\nname: {non_string_scalar}\ndescription: d\n---\n", encoding="utf-8"
        )
        verdict, reason = checker.decide_host_skill_registrability(skill_manifest)
        assert verdict == checker.HOST_SKILL_REGISTRABILITY_SKIPPED_BY_HOST, (
            non_string_scalar,
            verdict,
            reason,
        )
