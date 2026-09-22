#!/usr/bin/env python3
"""Codex `hooks.state` 信任指纹核对（部署后的静默失效面）。

Codex 对每条 command hook 记信任指纹；改了命令而不重新信任 ⇒ 该 hook 被判 Modified 并**跳过**，
且 `codex exec` 下**完全静默**（实测 0.147.0：stdout+stderr 里 `hook`/`trust`/`/hooks` 命中全为 0）。
于是 `/rvf-local-deploy` 会报告一切成功，而 RVF 在 Codex 侧从此一次都不执行。

本 gate 钉死两件事：

* **TRUST-1** —— 指纹算法能复算出与 Codex 一致的值。算法的**四条已知性质**（事件名参与、
  matcher 参与、`statusMessage` 参与、默认值补齐幂等）由模块自检钉死；本文件另外钉死
  「命令一改，指纹就变」这条核心判据。
* **TRUST-2** —— 核对结果**不可假绿**：核对不了要报 `unavailable` 而不是 `trusted`；
  指纹不符 / 无信任条目都要如实列出，并给出重新信任的指引。
* **TRUST-3**（RVF-006）—— **「一条都没核到」不得被呈现成「全部一致」**。`checked == 0`、
  源缺失、以及「源文件在但信任表里一个对应键都没有」（= 从没被信任过，最危险的一种）
  都必须落到非 `trusted` 状态，并各自有可见结论。
* **TRUST-4**（RVF-005）—— bundled hook 源的位置取自**安装器回执**的真实 payload 根，
  不按 `$HOME/plugins/<plugin>` 硬拼；定位不到就报「覆盖不完整」，绝不静默报 trusted。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_hook_trust_fingerprint as trust  # noqa: E402
import rvf_local_deploy  # noqa: E402

HOOKS_JSON = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": 'python3 "/plugins/rvf/hooks/stop.py"',
                        "timeout": 120,
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'python3 "/plugins/rvf/hooks/post_tool_use.py"',
                        "timeout": 30,
                        "statusMessage": "记录工具完成",
                    }
                ],
            }
        ],
    }
}


def _write_sources(tmp_path: Path, hooks: dict) -> tuple[Path, Path]:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(json.dumps(hooks, ensure_ascii=False, indent=2), encoding="utf-8")
    return codex_home, hooks_path


def _all_fingerprints(hooks: dict) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for event_name, groups in hooks["hooks"].items():
        for group_index, group in enumerate(groups):
            for hook_index, hook in enumerate(group["hooks"]):
                key = (
                    f"{trust.snake_case_event_name(event_name)}:{group_index}:{hook_index}"
                )
                fingerprints[key] = trust.hook_trust_fingerprint(
                    event_name=event_name, group=group, hook=hook
                )
    return fingerprints


def _write_config(codex_home: Path, hooks_path: Path, fingerprints: dict[str, str]) -> None:
    lines = []
    for suffix, digest in fingerprints.items():
        lines.append(f'[hooks.state."{hooks_path.resolve()}:{suffix}"]')
        lines.append(f'trusted_hash = "{digest}"')
    (codex_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_matching_fingerprints_report_trusted(tmp_path: Path) -> None:
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    _write_config(codex_home, hooks_path, _all_fingerprints(HOOKS_JSON))

    result = trust.verify_codex_hook_trust(
        codex_home=codex_home, hook_sources=[(str(hooks_path.resolve()), hooks_path)]
    )
    assert result["status"] == trust.TRUST_STATUS_TRUSTED, result
    assert result["checked"] == 2
    assert result["untrusted"] == []


def test_changing_a_hook_command_without_retrusting_is_reported(tmp_path: Path) -> None:
    """TRUST-1 的核心判据：命令一改，指纹就变——deploy 必须能看见这件事。"""
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    _write_config(codex_home, hooks_path, _all_fingerprints(HOOKS_JSON))

    changed = json.loads(json.dumps(HOOKS_JSON))
    changed["hooks"]["Stop"][0]["hooks"][0]["command"] = 'python3 "/plugins/rvf/hooks/stop.py" --new'
    hooks_path.write_text(json.dumps(changed, ensure_ascii=False, indent=2), encoding="utf-8")

    result = trust.verify_codex_hook_trust(
        codex_home=codex_home, hook_sources=[(str(hooks_path.resolve()), hooks_path)]
    )
    assert result["status"] == trust.TRUST_STATUS_UNTRUSTED_ENTRIES, result
    assert [entry["reason"] for entry in result["untrusted"]] == ["fingerprint_mismatch"]
    assert result["untrusted"][0]["key"].endswith(":stop:0:0")
    assert result["untrusted"][0]["current_fingerprint"].startswith("sha256:")
    assert "重新信任" in result["remediation"]
    # 只有被改的那条失信；没动的那条仍然受信（否则这条断言无法区分「真检出」与「全灭」）。
    assert result["checked"] == 2


def test_a_hook_with_no_trust_entry_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    fingerprints = _all_fingerprints(HOOKS_JSON)
    fingerprints.pop("stop:0:0")
    _write_config(codex_home, hooks_path, fingerprints)

    result = trust.verify_codex_hook_trust(
        codex_home=codex_home, hook_sources=[(str(hooks_path.resolve()), hooks_path)]
    )
    assert result["status"] == trust.TRUST_STATUS_UNTRUSTED_ENTRIES
    assert [entry["reason"] for entry in result["untrusted"]] == ["no_trust_entry"]
    assert result["untrusted"][0]["recorded_fingerprint"] is None


def test_missing_config_reports_unavailable_not_trusted(tmp_path: Path) -> None:
    """TRUST-2：核对不了必须说「核对不了」。报 trusted 就是把假绿写进部署回执。"""
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    result = trust.verify_codex_hook_trust(
        codex_home=codex_home, hook_sources=[(str(hooks_path.resolve()), hooks_path)]
    )
    assert result["status"] == trust.TRUST_STATUS_UNAVAILABLE, result
    assert result["status"] != trust.TRUST_STATUS_TRUSTED
    assert "不是" in result["note"]


def test_all_mismatched_is_flagged_as_probably_an_algorithm_drift(tmp_path: Path) -> None:
    """全灭时要明说「更可能是本模块跟 Codex 漂了」，免得把工具问题读成 N 个部署问题。"""
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    _write_config(
        codex_home, hooks_path, {key: "sha256:" + "0" * 64 for key in _all_fingerprints(HOOKS_JSON)}
    )
    result = trust.verify_codex_hook_trust(
        codex_home=codex_home, hook_sources=[(str(hooks_path.resolve()), hooks_path)]
    )
    assert len(result["untrusted"]) == result["checked"] == 2
    assert "指纹算法" in result["note"]


def test_missing_hook_source_is_recorded_rather_than_silently_skipped(tmp_path: Path) -> None:
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    _write_config(codex_home, hooks_path, _all_fingerprints(HOOKS_JSON))
    result = trust.verify_codex_hook_trust(
        codex_home=codex_home,
        hook_sources=[
            (str(hooks_path.resolve()), hooks_path),
            ("review-validate-fix@nowhere:hooks/hooks.json", tmp_path / "missing.json"),
        ],
    )
    assert any("nowhere" in entry for entry in result["missing_sources"]), result


def test_plugin_bundled_sources_are_derived_from_the_trust_table_not_guessed(
    tmp_path: Path,
) -> None:
    """同一 plugin 可经多个 marketplace 登记（本机实测就有两个）——label 只能反查，不能猜。"""
    plugin_root = tmp_path / "plugins" / "review-validate-fix"
    (plugin_root / "hooks").mkdir(parents=True)
    (plugin_root / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    table = {
        "review-validate-fix@local-codex-plugins:hooks/hooks.json:stop:0:0": {"trusted_hash": "x"},
        "review-validate-fix@other-market:hooks/hooks.json:stop:0:0": {"trusted_hash": "y"},
        "/somewhere/hooks.json:stop:0:0": {"trusted_hash": "z"},
        "some-other-plugin@m:hooks/hooks.json:stop:0:0": {"trusted_hash": "w"},
    }
    sources = trust.rvf_codex_hook_sources(
        codex_home=codex_home, deployed_plugin_root=plugin_root, trust_table=table
    )
    labels = [label for label, _ in sources]
    assert "review-validate-fix@local-codex-plugins:hooks/hooks.json" in labels
    assert "review-validate-fix@other-market:hooks/hooks.json" in labels
    assert not any("some-other-plugin" in label for label in labels)


def test_checking_nothing_is_never_reported_as_trusted(tmp_path: Path) -> None:
    """TRUST-3 的核心判据：信任表非空、但一条 hook 都没核到 ⇒ 绝不能是 `trusted`。

    这正是本模块**唯一要避免的失败模式**：`checked=0, untrusted=[]` 曾经落进 `trusted`，
    于是 deploy 印出「全部一致（核对 0 条）」——把「一条都没核到」呈现成「一切正常」。
    """
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[hooks.state."review-validate-fix@m:hooks/hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:' + "0" * 64 + '"\n',
        encoding="utf-8",
    )

    result = trust.verify_codex_hook_trust(codex_home=codex_home, hook_sources=[])
    assert result["checked"] == 0
    assert result["status"] != trust.TRUST_STATUS_TRUSTED, result
    assert result["status"] == trust.TRUST_STATUS_CHECK_COVERAGE_INCOMPLETE
    assert result["coverage_incomplete"] is True
    assert "checked=0" in result["note"]
    assert result["remediation"]


def test_missing_hook_source_downgrades_status_not_just_a_side_note(tmp_path: Path) -> None:
    """TRUST-3：有源缺失时即便其余全对也不算 trusted——缺的那些等价于未受信。"""
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    _write_config(codex_home, hooks_path, _all_fingerprints(HOOKS_JSON))

    result = trust.verify_codex_hook_trust(
        codex_home=codex_home,
        hook_sources=[
            (str(hooks_path.resolve()), hooks_path),
            ("review-validate-fix@nowhere:hooks/hooks.json", tmp_path / "missing.json"),
        ],
    )
    assert result["checked"] == 2
    assert result["untrusted"] == []
    assert result["status"] != trust.TRUST_STATUS_TRUSTED, result
    assert result["status"] == trust.TRUST_STATUS_CHECK_COVERAGE_INCOMPLETE


def test_a_source_with_no_trust_entry_at_all_gets_its_own_visible_conclusion(
    tmp_path: Path,
) -> None:
    """TRUST-3：源文件在、信任表里却一个对应键都没有 = **从没被信任过**。

    这是最危险的一种（新装 / 换 marketplace label ⇒ Codex 一次都不会执行它），逐条查表只会
    得到 N 条 `no_trust_entry`，说不出「整个源都没登记过」。它需要一条独立结论。
    """
    codex_home, hooks_path = _write_sources(tmp_path, HOOKS_JSON)
    bundled = tmp_path / "payload" / "hooks" / "hooks.json"
    bundled.parent.mkdir(parents=True)
    bundled.write_text(json.dumps(HOOKS_JSON, ensure_ascii=False), encoding="utf-8")
    _write_config(codex_home, hooks_path, _all_fingerprints(HOOKS_JSON))

    result = trust.verify_codex_hook_trust(
        codex_home=codex_home,
        hook_sources=[
            (str(hooks_path.resolve()), hooks_path),
            ("review-validate-fix@never-trusted:hooks/hooks.json", bundled),
        ],
    )
    never_trusted = result["sources_without_any_trust_entry"]
    assert [entry["source"] for entry in never_trusted] == [
        "review-validate-fix@never-trusted:hooks/hooks.json"
    ], result
    assert result["status"] != trust.TRUST_STATUS_TRUSTED
    assert result["coverage_incomplete"] is True
    assert "一个键都没有" in result["note"]


def test_a_never_trusted_bundled_source_is_discovered_on_disk_not_only_reverse_looked_up(
    tmp_path: Path,
) -> None:
    """TRUST-3 的叠加面：只从信任表反查 ⇒ 从没被信任过的 bundled hook 整个源都不会进核对。"""
    plugin_root = tmp_path / "marketplace" / "plugins" / "review-validate-fix"
    (plugin_root / "hooks").mkdir(parents=True)
    (plugin_root / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    # 信任表里完全没有该 plugin 的键（= 从没被信任过）。
    reverse_lookup_only = trust.rvf_codex_hook_sources(
        codex_home=codex_home,
        deployed_plugin_root=plugin_root,
        trust_table={"/somewhere/hooks.json:stop:0:0": {"trusted_hash": "z"}},
    )
    assert reverse_lookup_only == []

    with_disk_discovery = trust.rvf_codex_hook_sources(
        codex_home=codex_home,
        deployed_plugin_root=plugin_root,
        trust_table={"/somewhere/hooks.json:stop:0:0": {"trusted_hash": "z"}},
        deployed_plugin_selector="review-validate-fix@review-validate-fix-local-main",
    )
    assert [label for label, _ in with_disk_discovery] == [
        "review-validate-fix@review-validate-fix-local-main:hooks/hooks.json"
    ]
    # 已登记的 label 不因磁盘发现而重复。
    registered = trust.rvf_codex_hook_sources(
        codex_home=codex_home,
        deployed_plugin_root=plugin_root,
        trust_table={
            "review-validate-fix@review-validate-fix-local-main:hooks/hooks.json:stop:0:0": {
                "trusted_hash": "x"
            }
        },
        deployed_plugin_selector="review-validate-fix@review-validate-fix-local-main",
    )
    assert len(registered) == 1


def test_deployed_plugin_payload_root_comes_from_the_installer_receipt(
    tmp_path: Path,
) -> None:
    """TRUST-4（RVF-005）：payload 根取自回执，而不是硬拼 `$HOME/plugins/<plugin>`。"""
    marketplace_root = tmp_path / ".rvf/developer-plugin-marketplaces/rvf-local-main"
    payload_root = marketplace_root / "plugins" / "review-validate-fix"
    (payload_root / "hooks").mkdir(parents=True)
    (payload_root / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")

    resolved, selector, note = (
        rvf_local_deploy._deployed_plugin_payload_root_from_installer_receipt(
            {
                "active_marketplace_root": str(marketplace_root),
                "plugin_selector": "review-validate-fix@rvf-local-main",
            }
        )
    )
    assert resolved == payload_root
    assert selector == "review-validate-fix@rvf-local-main"
    assert note is None
    # 硬拼的历史路径不参与：它是这条缺陷本身。
    assert resolved != tmp_path / "plugins" / "review-validate-fix"


def test_unresolvable_payload_root_is_reported_as_incomplete_not_trusted(
    tmp_path: Path,
) -> None:
    """TRUST-4：定位不到 payload 根 ⇒ bundled 源一条没核到 ⇒ 必须非 trusted。"""
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        '[hooks.state."review-validate-fix@m:hooks/hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:' + "0" * 64 + '"\n',
        encoding="utf-8",
    )

    observation = rvf_local_deploy._observe_codex_hook_trust(home, installer_receipt=None)
    assert observation["status"] != trust.TRUST_STATUS_TRUSTED, observation
    assert observation["status"] == trust.TRUST_STATUS_CHECK_COVERAGE_INCOMPLETE
    assert observation["coverage_incomplete"] is True
    assert observation["deployed_plugin_payload_root"] is None
    assert "回执" in observation["note"]


def test_only_trusted_status_is_printed_as_everything_is_fine(capsys) -> None:
    """TRUST-3/4：`_print_text` 的每一个非 trusted 分支都不得读成「一切正常」。"""
    not_fine_statuses = [
        trust.TRUST_STATUS_UNTRUSTED_ENTRIES,
        trust.TRUST_STATUS_TRUST_TABLE_ABSENT,
        trust.TRUST_STATUS_CHECK_COVERAGE_INCOMPLETE,
        trust.TRUST_STATUS_UNAVAILABLE,
    ]
    for status in not_fine_statuses:
        rvf_local_deploy._print_codex_hook_trust_observation(
            {"status": status, "checked": 0, "untrusted": [], "note": "n"}
        )
        printed = capsys.readouterr().out
        assert "全部一致" not in printed, (status, printed)
        assert "⚠️" in printed, (status, printed)

    rvf_local_deploy._print_codex_hook_trust_observation(
        {"status": trust.TRUST_STATUS_TRUSTED, "checked": 3, "untrusted": []}
    )
    printed = capsys.readouterr().out
    assert "全部一致" in printed
    assert "⚠️" not in printed


def _write_config_text(codex_home: Path, text: str) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(text, encoding="utf-8")


def test_rvf_selectors_are_collected_from_both_plugins_and_hooks_state(
    tmp_path: Path,
) -> None:
    """两张表都要扫：`[plugins.*]` 是注册本身，`[hooks.state.*]` 会留下解除注册后的残迹。"""
    codex_home = tmp_path / "codex-home"
    _write_config_text(
        codex_home,
        '\n'.join(
            [
                '[plugins."review-validate-fix@review-validate-fix-local-main"]',
                "enabled = true",
                '[plugins."ponytail@local-codex-plugins"]',
                "enabled = true",
                '[hooks.state."review-validate-fix@local-codex-plugins'
                ':hooks/hooks.json:stop:0:0"]',
                'trusted_hash = "sha256:deadbeef"',
                "",
            ]
        ),
    )

    document = trust.read_codex_config_document(codex_home)
    selectors = trust.rvf_plugin_selectors_in_codex_config(document)

    assert selectors == (
        "review-validate-fix@local-codex-plugins",
        "review-validate-fix@review-validate-fix-local-main",
    )


def test_rvf_selector_scan_ignores_similarly_named_plugins(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    _write_config_text(
        codex_home,
        '[plugins."review-validate-fix-dashboard@somewhere"]\nenabled = true\n',
    )

    document = trust.read_codex_config_document(codex_home)

    assert trust.rvf_plugin_selectors_in_codex_config(document) == ()


def test_second_rvf_registration_in_codex_config_blocks_rather_than_observes(
    tmp_path: Path,
) -> None:
    """config.toml 里的第二份注册必须判成阻断态，而不是又一条 advisory 观测。

    既有的 `_observe_codex_hook_trust` 是刻意非阻断的（重新信任是人的决定）；沿用那个语义正是
    本机双份 RVF 烂了三周的原因。
    """
    home = tmp_path / "home"
    _write_config_text(
        home / ".codex",
        '\n'.join(
            [
                '[plugins."review-validate-fix@review-validate-fix-local-main"]',
                "enabled = true",
                '[plugins."review-validate-fix@local-codex-plugins"]',
                "enabled = true",
                "",
            ]
        ),
    )

    uniqueness = rvf_local_deploy._sole_review_validate_fix_registration_in_codex_config(
        home,
        installer_receipt={
            "plugin_selector": "review-validate-fix@review-validate-fix-local-main"
        },
    )

    assert uniqueness["status"] == "multiple_registrations"
    assert uniqueness["extra_selectors"] == ["review-validate-fix@local-codex-plugins"]
    assert (
        "codex plugin remove review-validate-fix@local-codex-plugins --json"
        in uniqueness["remediation"]
    )


def test_hook_trust_only_residue_is_flagged_with_manual_cleanup_not_plugin_remove(
    tmp_path: Path,
) -> None:
    """只剩 `[hooks.state]` 残留时，指引必须是手删，不能是 `codex plugin remove`。

    实测（codex-cli 0.147.0，隔离 HOME）：``codex plugin remove <selector>`` 会删掉 plugin
    cache 树与 ``[plugins."<selector>"]``，却把该 selector 的 ``[hooks.state.*]`` 条目原样留
    下。对这种残留建议跑 remove，等于给了一条已被证明无效的命令。
    """
    home = tmp_path / "home"
    _write_config_text(
        home / ".codex",
        '\n'.join(
            [
                '[plugins."review-validate-fix@review-validate-fix-local-main"]',
                "enabled = true",
                '[hooks.state."review-validate-fix@local-codex-plugins'
                ':hooks/hooks.json:stop:0:0"]',
                'trusted_hash = "sha256:deadbeef"',
                "",
            ]
        ),
    )

    uniqueness = rvf_local_deploy._sole_review_validate_fix_registration_in_codex_config(
        home,
        installer_receipt={
            "plugin_selector": "review-validate-fix@review-validate-fix-local-main"
        },
    )

    assert uniqueness["status"] == "multiple_registrations"
    assert uniqueness["registration_sources"][
        "review-validate-fix@local-codex-plugins"
    ] == [trust.CODEX_CONFIG_HOOK_TRUST_TABLE]
    assert "手工删除" in uniqueness["remediation"]
    # 不得给出「跑一遍 remove」这条已被证明无效的指令（说明性提及该命令名是可以的）。
    assert (
        "codex plugin remove review-validate-fix@local-codex-plugins --json"
        not in uniqueness["remediation"]
    )


def test_sole_rvf_registration_in_codex_config_is_accepted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_config_text(
        home / ".codex",
        '[plugins."review-validate-fix@review-validate-fix-local-main"]\nenabled = true\n',
    )

    uniqueness = rvf_local_deploy._sole_review_validate_fix_registration_in_codex_config(
        home,
        installer_receipt={
            "plugin_selector": "review-validate-fix@review-validate-fix-local-main"
        },
    )

    assert uniqueness["status"] == "sole_registration"


def test_unreadable_codex_config_is_never_reported_as_sole_registration(
    tmp_path: Path,
) -> None:
    """核不到 ≠ 唯一。这条守的是与 hook 信任观测同一个失败模式。"""
    uniqueness = rvf_local_deploy._sole_review_validate_fix_registration_in_codex_config(
        tmp_path / "home-without-codex",
        installer_receipt={
            "plugin_selector": "review-validate-fix@review-validate-fix-local-main"
        },
    )

    assert uniqueness["status"] == "unavailable"
    assert uniqueness["status"] != "sole_registration"


def test_missing_installer_receipt_selector_never_reports_sole_registration(
    tmp_path: Path,
) -> None:
    """canonical selector 一律取自回执；取不到就如实说核不了，不得硬拼 marketplace 名。"""
    home = tmp_path / "home"
    _write_config_text(
        home / ".codex",
        '[plugins."review-validate-fix@local-codex-plugins"]\nenabled = true\n',
    )

    uniqueness = rvf_local_deploy._sole_review_validate_fix_registration_in_codex_config(
        home,
        installer_receipt={},
    )

    assert uniqueness["status"] == "unavailable"


def test_codex_config_without_canonical_registration_is_not_reported_as_sole_registration(
    tmp_path: Path,
) -> None:
    """一个 RVF selector 都没有 ⇒ 「没有第二份」，但 canonical 也不在册。

    旧实现只排除已识别的多注册状态，于是这种 config 会落到 `sole_registration`，把「canonical
    根本没注册」谎报成「唯一」。认证要的是**正向证明**：canonical 确实出现在插件注册表里。
    """
    home = tmp_path / "home"
    _write_config_text(
        home / ".codex",
        '[plugins."ponytail@local-codex-plugins"]\nenabled = true\n',
    )

    uniqueness = rvf_local_deploy._sole_review_validate_fix_registration_in_codex_config(
        home,
        installer_receipt={
            "plugin_selector": "review-validate-fix@review-validate-fix-local-main"
        },
    )

    assert uniqueness["status"] == (
        rvf_local_deploy.CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS
    )
    assert uniqueness["status"] != rvf_local_deploy.CODEX_SOLE_REGISTRATION_STATUS
    assert "review-validate-fix@review-validate-fix-local-main" in uniqueness["note"]


def test_canonical_selector_only_in_hook_trust_table_is_not_reported_as_sole_registration(
    tmp_path: Path,
) -> None:
    """canonical 只剩 `[hooks.state]` 残迹 ⇒ 插件没注册，只是信任条目还在，同样不算在册。"""
    home = tmp_path / "home"
    _write_config_text(
        home / ".codex",
        '[hooks.state."review-validate-fix@review-validate-fix-local-main'
        ':hooks/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:deadbeef"\n',
    )

    uniqueness = rvf_local_deploy._sole_review_validate_fix_registration_in_codex_config(
        home,
        installer_receipt={
            "plugin_selector": "review-validate-fix@review-validate-fix-local-main"
        },
    )

    assert uniqueness["status"] == (
        rvf_local_deploy.CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS
    )


def test_only_sole_registration_is_printed_as_fine(capsys) -> None:
    for status in (
        "multiple_registrations",
        "unavailable",
        rvf_local_deploy.CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS,
    ):
        rvf_local_deploy._print_codex_registration_uniqueness(
            {
                "status": status,
                "selectors": ["review-validate-fix@a", "review-validate-fix@b"],
                "remediation": "codex plugin remove review-validate-fix@b --json",
                "note": "n",
            }
        )
        printed = capsys.readouterr().out
        assert "⚠️" in printed, (status, printed)
        assert "唯一" not in printed.replace("唯一性", ""), (status, printed)

    rvf_local_deploy._print_codex_registration_uniqueness(
        {
            "status": "sole_registration",
            "selectors": ["review-validate-fix@review-validate-fix-local-main"],
        }
    )
    printed = capsys.readouterr().out
    assert "唯一" in printed
    assert "⚠️" not in printed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
