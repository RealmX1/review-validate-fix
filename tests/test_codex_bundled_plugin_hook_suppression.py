"""bundled hook 抑制的行为 gate。

抑制机制本身已在隔离 CODEX_HOME 下用真实 codex 0.147.0 会话实测过：
``[hooks.state."<key>"].enabled = false`` 会让该条 bundled hook 不执行，
而同会话里 ``~/.codex/hooks.json`` 的 hook 照常执行；该值跨
``plugin add`` / ``remove`` / 再 ``add`` 原样存活。本文件钉的是我们这一侧的写入正确性。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.codex_bundled_plugin_hook_suppression import (  # noqa: E402
    BUNDLED_HOOKS_JSON_RELATIVE_PATH,
    bundled_plugin_hook_state_keys,
    config_toml_text_with_hook_state_entries_disabled,
    disable_bundled_plugin_hooks_in_codex_config,
    snake_case_hook_event_name,
)

BUNDLED_HOOKS_JSON = ROOT / "plugins/review-validate-fix/hooks/hooks.json"
SELECTOR = "review-validate-fix@review-validate-fix-local-main"


def _trust_module():
    spec = importlib.util.spec_from_file_location(
        "codex_hook_trust_fingerprint_for_suppression_tests",
        ROOT / "scripts/codex_hook_trust_fingerprint.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snake_case_event_name_matches_the_deploy_time_fingerprint_tool() -> None:
    """两份独立实现必须逐字一致，否则抑制会写到一个 Codex 永远不看的键上。"""
    trust = _trust_module()
    for event_name in (
        "Stop",
        "UserPromptSubmit",
        "PostToolUse",
        "PreToolUse",
        "SessionStart",
        "SubagentStop",
        "PermissionRequest",
        "PreCompact",
    ):
        assert snake_case_hook_event_name(event_name) == trust.snake_case_event_name(
            event_name
        )


def test_hook_state_keys_cover_every_entry_of_the_real_bundled_document() -> None:
    document = json.loads(BUNDLED_HOOKS_JSON.read_text(encoding="utf-8"))
    keys = bundled_plugin_hook_state_keys(
        plugin_selector=SELECTOR, bundled_hooks_document=document
    )
    assert set(keys) == {
        f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:stop:0:0",
        f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:user_prompt_submit:0:0",
        f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:post_tool_use:0:0",
    }


def test_malformed_bundled_document_yields_no_keys() -> None:
    assert bundled_plugin_hook_state_keys(
        plugin_selector=SELECTOR, bundled_hooks_document={}
    ) == ()
    assert bundled_plugin_hook_state_keys(
        plugin_selector=SELECTOR, bundled_hooks_document={"hooks": "not-a-mapping"}
    ) == ()


def test_existing_entry_is_flipped_while_trusted_hash_and_neighbours_survive() -> None:
    key = f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:stop:0:0"
    original = (
        "model = \"gpt-5.6-sol\"\n"
        "\n"
        "[plugins.\"review-validate-fix@review-validate-fix-local-main\"]\n"
        "enabled = true\n"
        "\n"
        "[hooks.state]\n"
        "\n"
        f'[hooks.state."{key}"]\n'
        "enabled = true\n"
        'trusted_hash = "sha256:deadbeef"\n'
        "\n"
        '[hooks.state."/Users/x/.codex/hooks.json:stop:0:0"]\n'
        "enabled = true\n"
        'trusted_hash = "sha256:cafef00d"\n'
    )

    updated, disabled, already, created = (
        config_toml_text_with_hook_state_entries_disabled(original, [key])
    )

    assert disabled == (key,) and already == () and created == ()
    assert 'trusted_hash = "sha256:deadbeef"' in updated
    # 只动 bundled 那一块；plugins 表与 CODEX_HOME/hooks.json 那条必须原样。
    assert '[plugins."review-validate-fix@review-validate-fix-local-main"]\nenabled = true' in updated
    assert (
        '[hooks.state."/Users/x/.codex/hooks.json:stop:0:0"]\nenabled = true' in updated
    )
    block = updated.split(f'[hooks.state."{key}"]\n', 1)[1]
    assert block.startswith("enabled = false\n")


def test_entry_without_enabled_line_gets_one_inserted() -> None:
    key = f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:stop:0:0"
    original = f'[hooks.state."{key}"]\ntrusted_hash = "sha256:abc"\n'

    updated, disabled, already, created = (
        config_toml_text_with_hook_state_entries_disabled(original, [key])
    )

    assert disabled == (key,) and created == ()
    assert updated == (
        f'[hooks.state."{key}"]\nenabled = false\ntrusted_hash = "sha256:abc"\n'
    )


def test_missing_entry_is_created_and_rerun_is_idempotent() -> None:
    key = f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:stop:0:0"
    original = 'model = "gpt-5.6-sol"\n'

    once, disabled, already, created = (
        config_toml_text_with_hook_state_entries_disabled(original, [key])
    )
    assert created == (key,) and disabled == () and already == ()
    assert f'[hooks.state."{key}"]\nenabled = false\n' in once

    twice, disabled_again, already_again, created_again = (
        config_toml_text_with_hook_state_entries_disabled(once, [key])
    )
    assert twice == once
    assert already_again == (key,) and disabled_again == () and created_again == ()


def test_similar_but_different_key_is_not_touched() -> None:
    """键必须精确匹配：``…:stop:0:0`` 不得连带改掉 ``…:stop:0:1``。"""
    target = f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:stop:0:0"
    neighbour = f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:stop:0:1"
    original = (
        f'[hooks.state."{target}"]\nenabled = true\n\n'
        f'[hooks.state."{neighbour}"]\nenabled = true\n'
    )

    updated, _, _, _ = config_toml_text_with_hook_state_entries_disabled(
        original, [target]
    )

    assert f'[hooks.state."{neighbour}"]\nenabled = true\n' in updated
    assert updated.count("enabled = false") == 1


def test_disable_writes_config_and_is_a_no_op_without_config_file(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    document = json.loads(BUNDLED_HOOKS_JSON.read_text(encoding="utf-8"))

    absent = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home,
        plugin_selector=SELECTOR,
        bundled_hooks_document=document,
    )
    assert absent.config_written is False
    assert not (codex_home / "config.toml").exists(), "config.toml 不存在时不得凭空创建"

    (codex_home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    outcome = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home,
        plugin_selector=SELECTOR,
        bundled_hooks_document=document,
    )

    assert outcome.config_written is True
    assert len(outcome.created_hook_state_keys) == 3
    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert text.count("enabled = false") == 3
    assert text.startswith('model = "gpt-5.6-sol"\n')


def test_written_config_still_parses_as_toml(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.6-sol"\n\n[hooks.state]\n', encoding="utf-8"
    )
    disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home,
        plugin_selector=SELECTOR,
        bundled_hooks_document=json.loads(
            BUNDLED_HOOKS_JSON.read_text(encoding="utf-8")
        ),
    )

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))

    state = parsed["hooks"]["state"]
    assert len(state) == 3
    assert all(entry["enabled"] is False for entry in state.values())


# ---------------------------------------------------------------------------
# RVF-002：表头识别必须按 TOML 语义，不能按字面量
#
# 字面量比较（``line.strip() == '[hooks.state."<key>"]'``）认不出合法但形态不同的表头，
# 于是走进「不存在」分支去追加一个同名表 ⇒ 重复表 ⇒ tomllib 判整份 config 非法。
# 而这写的是 live ``~/.codex/config.toml``。下面每个形态都钉住「不得产生重复表」。
# ---------------------------------------------------------------------------

STOP_KEY = f"{SELECTOR}:{BUNDLED_HOOKS_JSON_RELATIVE_PATH}:stop:0:0"


def _assert_single_table_flipped_to_false(original: str, key: str) -> str:
    """跑一遍抑制并断言：表头只出现一次、值翻成 false、整份仍是合法 TOML。"""
    updated, disabled, already, created = (
        config_toml_text_with_hook_state_entries_disabled(original, [key])
    )

    assert created == (), "认出了既有表头就不该再追加一个同名表"
    assert disabled == (key,) and already == ()
    assert updated.count(key) == 1, f"出现重复表：\n{updated}"
    parsed = tomllib.loads(updated)
    assert parsed["hooks"]["state"][key]["enabled"] is False
    return updated


def test_header_with_trailing_comment_is_recognised_instead_of_duplicated() -> None:
    original = (
        f'[hooks.state."{STOP_KEY}"]  # 手工标注：先前排查时看过这条\n'
        "enabled = true\n"
        'trusted_hash = "sha256:deadbeef"\n'
    )

    updated = _assert_single_table_flipped_to_false(original, STOP_KEY)

    assert "# 手工标注：先前排查时看过这条" in updated, "用户的行尾注释必须原样保留"
    assert 'trusted_hash = "sha256:deadbeef"' in updated


def test_single_quoted_header_is_recognised_instead_of_duplicated() -> None:
    """TOML literal string 键：``[hooks.state.'<key>']`` 与双引号写法等价。"""
    original = f"[hooks.state.'{STOP_KEY}']\nenabled = true\n"

    updated = _assert_single_table_flipped_to_false(original, STOP_KEY)

    assert f"[hooks.state.'{STOP_KEY}']" in updated, "不该顺手把用户的引号风格改掉"


def test_header_with_whitespace_around_dots_is_recognised() -> None:
    """``[ hooks.state . "<key>" ]`` 也是合法 TOML 表头。"""
    original = f'[ hooks.state . "{STOP_KEY}" ]\nenabled = true\n'

    _assert_single_table_flipped_to_false(original, STOP_KEY)


def test_already_false_with_trailing_comment_counts_as_already_disabled() -> None:
    """``enabled = false  # …`` 是「已经关了」，不该被当成没关而每次部署重写一遍。"""
    original = f'[hooks.state."{STOP_KEY}"]\nenabled = false  # 之前手工关的\n'

    updated, disabled, already, created = (
        config_toml_text_with_hook_state_entries_disabled(original, [STOP_KEY])
    )

    assert already == (STOP_KEY,) and disabled == () and created == ()
    assert updated == original, "已经是 false 时必须一个字节都不动"


def test_mixed_header_shapes_survive_a_real_end_to_end_write(tmp_path: Path) -> None:
    """三条真实 key 各用一种表头写法，写完必须仍是合法 TOML 且没有重复表。"""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    document = json.loads(BUNDLED_HOOKS_JSON.read_text(encoding="utf-8"))
    keys = bundled_plugin_hook_state_keys(
        plugin_selector=SELECTOR, bundled_hooks_document=document
    )
    assert len(keys) == 3
    shapes = (
        f'[hooks.state."{keys[0]}"]  # 行尾注释\n',
        f"[hooks.state.'{keys[1]}']\n",
        f'[ hooks.state . "{keys[2]}" ]\n',
    )
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.6-sol"\n\n'
        + "\n".join(f'{shape}enabled = true\ntrusted_hash = "sha256:{i}"\n'
                    for i, shape in enumerate(shapes)),
        encoding="utf-8",
    )

    outcome = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home,
        plugin_selector=SELECTOR,
        bundled_hooks_document=document,
    )

    assert outcome.config_written is True
    assert outcome.config_write_failure_reason is None
    assert set(outcome.disabled_hook_state_keys) == set(keys)
    assert outcome.created_hook_state_keys == (), "既有表头都认出来了就不该新建任何表"
    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    state = tomllib.loads(text)["hooks"]["state"]
    assert len(state) == 3
    assert all(entry["enabled"] is False for entry in state.values())
    assert all(text.count(key) == 1 for key in keys)


# ---------------------------------------------------------------------------
# RVF-003：写 live 用户配置的失败必须如实进结果对象，而不是外抛打断部署
# ---------------------------------------------------------------------------


def test_config_that_cannot_be_parsed_is_never_written(tmp_path: Path) -> None:
    """写回前的 tomllib 校验：解析不过就整体放弃，宁可不抑制也不能写坏用户配置。"""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    broken = "这不是 TOML ][ \n"
    config_path.write_text(broken, encoding="utf-8")

    outcome = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home,
        plugin_selector=SELECTOR,
        bundled_hooks_document=json.loads(
            BUNDLED_HOOKS_JSON.read_text(encoding="utf-8")
        ),
    )

    assert outcome.config_written is False
    assert outcome.config_write_failure_reason is not None
    assert "tomllib" in outcome.config_write_failure_reason
    assert outcome.disabled_hook_state_keys == ()
    assert outcome.created_hook_state_keys == ()
    assert config_path.read_text(encoding="utf-8") == broken, "失败必须原样保留用户配置"


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root 无视权限位，权限类断言在 root 下没有意义",
)
def test_unwritable_config_directory_is_reported_instead_of_raising(
    tmp_path: Path,
) -> None:
    """只读父目录：函数不得抛异常（它跑在 ``codex plugin add`` 之后，抛了就断在半路）。"""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    original = 'model = "gpt-5.6-sol"\n'
    config_path.write_text(original, encoding="utf-8")
    codex_home.chmod(0o500)
    try:
        outcome = disable_bundled_plugin_hooks_in_codex_config(
            codex_home=codex_home,
            plugin_selector=SELECTOR,
            bundled_hooks_document=json.loads(
                BUNDLED_HOOKS_JSON.read_text(encoding="utf-8")
            ),
        )
        assert outcome.config_written is False
        assert outcome.config_write_failure_reason is not None
        assert str(config_path) in outcome.config_write_failure_reason
        assert outcome.disabled_hook_state_keys == ()
        assert outcome.already_disabled_hook_state_keys == ()
        assert outcome.created_hook_state_keys == ()
        assert config_path.read_text(encoding="utf-8") == original
        assert [entry.name for entry in codex_home.iterdir()] == ["config.toml"], (
            "失败路径不得留下临时文件"
        )
    finally:
        codex_home.chmod(0o700)


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root 无视权限位，权限类断言在 root 下没有意义",
)
def test_read_only_config_file_never_raises_and_keeps_its_mode(tmp_path: Path) -> None:
    """``chmod 444`` 的 config.toml：无论走成还是走不成，都不许抛异常、不许留半截文件。

    注意实际语义：原子替换只需要**父目录**可写，所以 444 的文件仍会被成功替换；
    这正是我们要的（抑制照常生效），因此这里额外钉住「权限位不被顺手改掉」。
    """
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    original = 'model = "gpt-5.6-sol"\n'
    config_path.write_text(original, encoding="utf-8")
    config_path.chmod(0o444)

    outcome = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home,
        plugin_selector=SELECTOR,
        bundled_hooks_document=json.loads(
            BUNDLED_HOOKS_JSON.read_text(encoding="utf-8")
        ),
    )

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o444, "不得顺手改用户配置的权限位"
    assert [entry.name for entry in codex_home.iterdir()] == ["config.toml"]
    text = config_path.read_text(encoding="utf-8")
    if outcome.config_written:
        assert outcome.config_write_failure_reason is None
        assert len(tomllib.loads(text)["hooks"]["state"]) == 3
    else:
        assert outcome.config_write_failure_reason is not None
        assert text == original


def test_write_is_an_atomic_replacement_not_an_in_place_truncation(
    tmp_path: Path,
) -> None:
    """原子写的可观测判据：inode 变了 ⇒ 是 ``os.replace`` 换文件，不是就地截断重写。

    就地 ``write_text`` 会保留同一个 inode，并且在「已截断、未写完」之间存在半截窗口。
    """
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    inode_before = config_path.stat().st_ino

    outcome = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home,
        plugin_selector=SELECTOR,
        bundled_hooks_document=json.loads(
            BUNDLED_HOOKS_JSON.read_text(encoding="utf-8")
        ),
    )

    assert outcome.config_written is True
    assert config_path.stat().st_ino != inode_before, (
        "inode 没变说明是就地重写，存在「先截断再写」的半截窗口"
    )
    assert [entry.name for entry in codex_home.iterdir()] == ["config.toml"], (
        "成功路径不得留下临时文件"
    )


def test_successful_outcome_reports_no_failure_reason(tmp_path: Path) -> None:
    """成功与「本来就没事可做」两条路径上，失败原因必须是 ``None``。"""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    document = json.loads(BUNDLED_HOOKS_JSON.read_text(encoding="utf-8"))

    absent = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home, plugin_selector=SELECTOR, bundled_hooks_document=document
    )
    assert absent.config_write_failure_reason is None

    (codex_home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    written = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home, plugin_selector=SELECTOR, bundled_hooks_document=document
    )
    assert written.config_written is True
    assert written.config_write_failure_reason is None

    rerun = disable_bundled_plugin_hooks_in_codex_config(
        codex_home=codex_home, plugin_selector=SELECTOR, bundled_hooks_document=document
    )
    assert rerun.config_written is False
    assert rerun.config_write_failure_reason is None
    assert len(rerun.already_disabled_hook_state_keys) == 3
