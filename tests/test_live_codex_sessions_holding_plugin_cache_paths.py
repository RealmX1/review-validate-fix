"""部署前 live Codex 会话检测的行为 gate。

这条判据是 2026-08-18 事故里**当时就摆在同一屏上、却没被采信**的那一条：``lsof`` 报 0/0
放行的同时，rollout 扫描已经报「活会话引用 legacy ×2」。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.live_codex_sessions_holding_plugin_cache_paths import (  # noqa: E402
    codex_plugin_cache_path_prefix,
    live_codex_session_warning_lines,
    live_codex_sessions_holding_plugin_cache_paths,
    plugin_cache_path_prefixes_for_selectors,
)

NOW = 1_787_065_145.0
PLUGIN_NAME = "review-validate-fix"
STALE_MARKETPLACE = "local-codex-plugins"


def _write_rollout(
    sessions_root: Path,
    *,
    session_id: str,
    prelude_skill_roots: list[str],
    modified_at: float,
    trailing_text_mentions: list[str] | None = None,
) -> Path:
    day_directory = sessions_root / "2026/08/18"
    day_directory.mkdir(parents=True, exist_ok=True)
    rollout_path = day_directory / f"rollout-{session_id}.jsonl"
    roots_markdown = "\n".join(
        f"- `r{index}` = `{root}`" for index, root in enumerate(prelude_skill_roots)
    )
    records = [
        {"type": "session_meta", "payload": {"session_id": session_id}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"### Skill roots\n{roots_markdown}\n"}
                ],
            },
        },
    ]
    # prelude 之后再灌满记录，把「agent 自己读到同名路径」的正文推出 prelude 窗口。
    for index in range(40):
        text = ""
        if trailing_text_mentions and index < len(trailing_text_mentions):
            text = trailing_text_mentions[index]
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                },
            }
        )
    rollout_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    os.utime(rollout_path, (modified_at, modified_at))
    return rollout_path


def _stale_skill_root(codex_home: Path) -> str:
    return (
        f"{codex_home}/plugins/cache/{STALE_MARKETPLACE}/{PLUGIN_NAME}/0.1.0/skills"
    )


def _prefixes(codex_home: Path) -> tuple[str, ...]:
    return (
        codex_plugin_cache_path_prefix(
            codex_home=codex_home,
            marketplace_name=STALE_MARKETPLACE,
            plugin_name=PLUGIN_NAME,
        ),
    )


def test_recent_session_holding_the_doomed_plugin_cache_path_is_reported(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T22-57-04-01a01560",
        prelude_skill_roots=[
            f"{codex_home}/skills",
            _stale_skill_root(codex_home),
        ],
        modified_at=NOW - 120,
    )

    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=_prefixes(codex_home),
        now_epoch_seconds=NOW,
    )

    assert len(findings) == 1
    assert findings[0].session_id == "2026-08-18T22-57-04-01a01560"
    assert findings[0].held_plugin_cache_paths == (_stale_skill_root(codex_home),)


def test_session_idle_beyond_the_activity_window_is_not_reported(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T09-00-00-oldsession",
        prelude_skill_roots=[_stale_skill_root(codex_home)],
        modified_at=NOW - 7200,
    )

    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=_prefixes(codex_home),
        now_epoch_seconds=NOW,
        recent_activity_window_seconds=3600,
    )

    assert findings == ()


def test_path_mentioned_only_in_agent_output_after_the_prelude_is_ignored(
    tmp_path: Path,
) -> None:
    """防的是本仓反复踩过的坑：agent 自己 ``cat``/``grep`` 出来的关键词不是注册证据。"""
    codex_home = tmp_path / ".codex"
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T23-30-25-cleansession",
        prelude_skill_roots=[f"{codex_home}/skills"],
        modified_at=NOW - 60,
        trailing_text_mentions=[
            f"我刚 ls 了 {_stale_skill_root(codex_home)}，它已经不存在了。"
        ],
    )

    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=_prefixes(codex_home),
        now_epoch_seconds=NOW,
    )

    assert findings == ()


def test_no_prefixes_or_missing_sessions_root_yields_no_findings(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    assert (
        live_codex_sessions_holding_plugin_cache_paths(
            codex_home=codex_home,
            plugin_cache_path_prefixes=(),
            now_epoch_seconds=NOW,
        )
        == ()
    )
    assert (
        live_codex_sessions_holding_plugin_cache_paths(
            codex_home=codex_home,
            plugin_cache_path_prefixes=_prefixes(codex_home),
            now_epoch_seconds=NOW,
        )
        == ()
    )


def test_sibling_plugin_sharing_a_name_prefix_is_not_reported(tmp_path: Path) -> None:
    """同一 marketplace 下 ``review-validate-fix-dashboard`` 不是 ``review-validate-fix`` 的子树。

    直接对 ``normpath`` 结果 ``startswith`` 会把末尾分隔符吃掉，于是这类**相似但不同**的
    插件路径被误判成命中。这条告警是给开发者看的判据，误报会稀释它的可信度。
    """
    codex_home = tmp_path / ".codex"
    sibling_skill_root = (
        f"{codex_home}/plugins/cache/{STALE_MARKETPLACE}/"
        f"{PLUGIN_NAME}-dashboard/0.1.0/skills"
    )
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T22-10-00-siblingplugin",
        prelude_skill_roots=[f"{codex_home}/skills", sibling_skill_root],
        modified_at=NOW - 90,
    )

    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=_prefixes(codex_home),
        now_epoch_seconds=NOW,
    )

    assert findings == ()


def test_sibling_plugin_is_excluded_while_the_real_subtree_is_still_reported(
    tmp_path: Path,
) -> None:
    """反向守卫：修掉误报不能顺手引入假阴性——真正的子路径仍必须命中。"""
    codex_home = tmp_path / ".codex"
    sibling_skill_root = (
        f"{codex_home}/plugins/cache/{STALE_MARKETPLACE}/"
        f"{PLUGIN_NAME}-dashboard/0.1.0/skills"
    )
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T22-20-00-bothplugins",
        prelude_skill_roots=[
            f"{codex_home}/skills",
            sibling_skill_root,
            _stale_skill_root(codex_home),
        ],
        modified_at=NOW - 90,
    )

    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=_prefixes(codex_home),
        now_epoch_seconds=NOW,
    )

    assert len(findings) == 1
    assert findings[0].held_plugin_cache_paths == (_stale_skill_root(codex_home),)


def test_prefix_written_without_a_trailing_separator_behaves_identically(
    tmp_path: Path,
) -> None:
    """前缀带/不带末尾分隔符两种写法必须等价：都命中真子树、都不命中同名前缀的兄弟插件。"""
    codex_home = tmp_path / ".codex"
    sibling_skill_root = (
        f"{codex_home}/plugins/cache/{STALE_MARKETPLACE}/"
        f"{PLUGIN_NAME}-dashboard/0.1.0/skills"
    )
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T22-30-00-noslashprefix",
        prelude_skill_roots=[sibling_skill_root, _stale_skill_root(codex_home)],
        modified_at=NOW - 45,
    )
    prefix_without_trailing_separator = (
        f"{codex_home}/plugins/cache/{STALE_MARKETPLACE}/{PLUGIN_NAME}"
    )
    assert not prefix_without_trailing_separator.endswith("/")
    assert _prefixes(codex_home)[0] == prefix_without_trailing_separator + "/"

    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=(prefix_without_trailing_separator,),
        now_epoch_seconds=NOW,
    )

    assert len(findings) == 1
    assert findings[0].held_plugin_cache_paths == (_stale_skill_root(codex_home),)


def test_skill_root_equal_to_the_doomed_tree_root_itself_is_reported(
    tmp_path: Path,
) -> None:
    """整棵树都会被 ``codex plugin remove`` 删掉，因此等于前缀目录本身也算持有。"""
    codex_home = tmp_path / ".codex"
    doomed_tree_root = f"{codex_home}/plugins/cache/{STALE_MARKETPLACE}/{PLUGIN_NAME}"
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T22-40-00-treeroot",
        prelude_skill_roots=[doomed_tree_root],
        modified_at=NOW - 20,
    )

    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=_prefixes(codex_home),
        now_epoch_seconds=NOW,
    )

    assert len(findings) == 1
    assert findings[0].held_plugin_cache_paths == (doomed_tree_root,)


def test_selectors_translate_into_plugin_cache_path_prefixes(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    prefixes = plugin_cache_path_prefixes_for_selectors(
        codex_home=codex_home,
        plugin_selectors=[
            f"{PLUGIN_NAME}@{STALE_MARKETPLACE}",
            f"{PLUGIN_NAME}@review-validate-fix-local-main",
            "malformed-without-marketplace",
            f"{PLUGIN_NAME}@{STALE_MARKETPLACE}",
        ],
    )

    assert prefixes == (
        f"{codex_home}/plugins/cache/{STALE_MARKETPLACE}/{PLUGIN_NAME}/",
        f"{codex_home}/plugins/cache/review-validate-fix-local-main/{PLUGIN_NAME}/",
    )


def test_warning_lines_are_empty_without_findings_and_name_the_session_otherwise(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    assert live_codex_session_warning_lines(()) == ()
    _write_rollout(
        codex_home / "sessions",
        session_id="2026-08-18T22-57-04-01a01560",
        prelude_skill_roots=[_stale_skill_root(codex_home)],
        modified_at=NOW - 30,
    )
    findings = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=codex_home,
        plugin_cache_path_prefixes=_prefixes(codex_home),
        now_epoch_seconds=NOW,
    )

    lines = live_codex_session_warning_lines(findings)

    assert lines
    assert any("2026-08-18T22-57-04-01a01560" in line for line in lines)
