"""Kimi Code CLI 的会话磁盘布局解析（host-特定，只读）。

## 布局（实测，本机 42 份 wire.jsonl / 23 个 session 目录）

```
<kimi_code_home>/sessions/
  wd_<workdir-basename>_<hash>/          ← 按工作目录分桶；<hash> 不可从 workDir 反推
    session_<uuid>/
      state.json                          ← workDir / lastPrompt / agents 都在这里
      agents/<agent_id>/wire.jsonl        ← 每个 agent 一份；主 agent 的 id 是 "main"
      logs/kimi-code.log
```

两条**会咬人**的事实：

1. **`session_id` 本身已经带 `session_` 前缀**（Kimi 生成的就是 ``session_<uuid>``）。
   再拼一次前缀会得到 ``session_session_<uuid>``，找不到目录且没有任何报错。本模块用
   :func:`normalized_session_directory_name` 统一归一，两种写法都收。
2. **`wd_<basename>_<hash>` 里的 hash 不可从工作目录反推**，所以无法由 ``(workDir, session_id)``
   直接拼出路径——必须要么查索引、要么扫桶。

## `KIMI_CODE_HOME` 是**生产** override，不是测试钩子

Kimi 自身解析 home 的顺序是「显式 ``homeDir`` 参数 → 环境变量 ``KIMI_CODE_HOME`` → ``~/.kimi-code``」。
只处理默认 home 会在用户设了该变量时**同时**装错插件、读错 transcript，而两者都静默。
本模块因此把它当第一优先级读，测试注入只是顺带受益。

## 为什么把 `session_index.jsonl` 当快路径而不是真相源

``<kimi_code_home>/session_index.jsonl`` 每行是 ``{sessionId, sessionDir, workDir}``，正好是
本模块需要的映射。本机实测它是**完整**的（23 条索引 ↔ 23 个磁盘目录，0 缺失）。

但它终究是**派生**数据，而 ``sessions/`` 目录树才是真相；Kimi 何时、以何种原子性写它，
我们没有取证。因此本模块：**先查索引（快），未命中或指向的路径已不存在时回落扫描（准）**。
两条路径的一致性由 :func:`_self_check` 在真实数据上断言。

> 早先的设计文档写着「``session_layout`` 不读 ``session_index.jsonl``」，理由是「已实测该文件
> 不完整」。该理由在 2026-08-23 复核时**不成立**（当时本机根本没有这个文件，属 UNVERIFIABLE；
> 现在有了，且完整）。把它降级为快路径而非禁用，是在「快」与「不依赖未验证不变量」之间取的中点。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

KIMI_CODE_HOME_ENVIRONMENT_VARIABLE = "KIMI_CODE_HOME"
"""Kimi 自己的生产 override。优先级高于 ``~/.kimi-code``。"""

DEFAULT_KIMI_CODE_HOME_DIRECTORY_NAME = ".kimi-code"
SESSIONS_DIRECTORY_NAME = "sessions"
SESSION_INDEX_FILE_NAME = "session_index.jsonl"
SESSION_DIRECTORY_NAME_PREFIX = "session_"
WORKING_DIRECTORY_BUCKET_DIRECTORY_NAME_PREFIX = "wd_"
SESSION_STATE_FILE_NAME = "state.json"
AGENTS_DIRECTORY_NAME = "agents"
AGENT_WIRE_TRANSCRIPT_FILE_NAME = "wire.jsonl"
MAIN_AGENT_IDENTIFIER = "main"
"""``state.json.agents`` 里主 agent 的键，实测恒为 ``"main"``（其 ``type`` 字段也是 ``"main"``）。"""


def resolved_kimi_code_home_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """按 Kimi 自身的优先级解析 home：``KIMI_CODE_HOME`` → ``~/.kimi-code``。"""
    env = os.environ if environment is None else environment
    override = (env.get(KIMI_CODE_HOME_ENVIRONMENT_VARIABLE) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_KIMI_CODE_HOME_DIRECTORY_NAME


def kimi_code_sessions_root_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    return resolved_kimi_code_home_directory(environment) / SESSIONS_DIRECTORY_NAME


def normalized_session_directory_name(session_id: str) -> str:
    """把 ``<uuid>`` 与 ``session_<uuid>`` 两种写法都归一成磁盘上的目录名。

    Kimi 生成的 session id 本身已含前缀；调用方（hook event、CLI 参数）两种都可能给。
    不归一的后果是拼出 ``session_session_<uuid>``——找不到目录，且**没有任何报错**。
    """
    trimmed = (session_id or "").strip()
    if not trimmed:
        return ""
    if trimmed.startswith(SESSION_DIRECTORY_NAME_PREFIX):
        return trimmed
    return f"{SESSION_DIRECTORY_NAME_PREFIX}{trimmed}"


def _session_directory_from_index(
    session_directory_name: str, environment: Mapping[str, str] | None
) -> Path | None:
    """快路径：查 ``session_index.jsonl``。索引缺失 / 损坏 / 指向不存在的路径都返回 None。"""
    index_path = resolved_kimi_code_home_directory(environment) / SESSION_INDEX_FILE_NAME
    try:
        raw = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if normalized_session_directory_name(str(entry.get("sessionId") or "")) != (
            session_directory_name
        ):
            continue
        session_directory = entry.get("sessionDir")
        if not session_directory:
            continue
        candidate = Path(str(session_directory))
        # 索引可能落后于磁盘（会话被删、home 被搬走）——指向不存在的路径就当没命中。
        if candidate.is_dir():
            return candidate
    return None


def _session_directory_by_scanning_buckets(
    session_directory_name: str, environment: Mapping[str, str] | None
) -> Path | None:
    """慢路径（真相源）：扫 ``sessions/wd_*/<session_dir>``。"""
    sessions_root = kimi_code_sessions_root_directory(environment)
    if not sessions_root.is_dir():
        return None
    try:
        buckets = sorted(sessions_root.iterdir())
    except OSError:
        return None
    for bucket in buckets:
        if not bucket.is_dir():
            continue
        if not bucket.name.startswith(WORKING_DIRECTORY_BUCKET_DIRECTORY_NAME_PREFIX):
            continue
        candidate = bucket / session_directory_name
        if candidate.is_dir():
            return candidate
    return None


def resolve_session_directory(
    session_id: str, environment: Mapping[str, str] | None = None
) -> Path | None:
    """定位 ``session_<uuid>/`` 目录：先查索引（快），未命中回落扫桶（准）。"""
    session_directory_name = normalized_session_directory_name(session_id)
    if not session_directory_name:
        return None
    from_index = _session_directory_from_index(session_directory_name, environment)
    if from_index is not None:
        return from_index
    return _session_directory_by_scanning_buckets(session_directory_name, environment)


def resolve_agent_wire_transcript_path(
    session_id: str,
    agent_id: str = MAIN_AGENT_IDENTIFIER,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """定位某个 agent 的 ``wire.jsonl``。文件不存在时返回 None（不造假路径）。

    **不存在也返回 None 是刻意的**：调用方（hook shim）要靠它判断「能不能合成
    ``transcript_path``」。返回一个不存在的路径会让 ``detect_transcript_format`` 拿到 None
    并兜底 Codex ⇒ RVF 在 Kimi 上发错线材而静默失声。
    """
    session_directory = resolve_session_directory(session_id, environment)
    if session_directory is None:
        return None
    candidate = (
        session_directory
        / AGENTS_DIRECTORY_NAME
        / (agent_id or MAIN_AGENT_IDENTIFIER)
        / AGENT_WIRE_TRANSCRIPT_FILE_NAME
    )
    return candidate if candidate.is_file() else None


def read_session_state(
    session_id: str, environment: Mapping[str, str] | None = None
) -> dict | None:
    """读 ``state.json``。它是 shim 归一 ``cwd`` 与 ``last_user_message`` 的唯一来源。

    实测字段：``workDir``（真实工作目录，Stop 事件本身不带）、``lastPrompt``（最后一条用户
    输入）、``agents``（``{<id>: {homedir, type, parentAgentId[, swarmItem]}}``——**没有 ``role``**）、
    ``title`` / ``isCustomTitle`` / ``createdAt`` / ``updatedAt`` / ``custom``。
    """
    session_directory = resolve_session_directory(session_id, environment)
    if session_directory is None:
        return None
    try:
        raw = (session_directory / SESSION_STATE_FILE_NAME).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _self_check() -> None:
    """runnable check：布局解析在**合成**目录树上必须正确；live 数据存在时再交叉验证一次。

    主断言刻意不依赖本机是否装过 Kimi——gate 要在任何机器上都能跑出真结论，而不是在没有
    ``~/.kimi-code`` 的机器上静默变成 vacuous truth。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "kimi-home"
        session_id = "session_11111111-2222-3333-4444-555555555555"
        bucket = home / SESSIONS_DIRECTORY_NAME / "wd_someproject_deadbeef00"
        session_directory = bucket / session_id
        (session_directory / AGENTS_DIRECTORY_NAME / MAIN_AGENT_IDENTIFIER).mkdir(
            parents=True
        )
        wire = (
            session_directory
            / AGENTS_DIRECTORY_NAME
            / MAIN_AGENT_IDENTIFIER
            / AGENT_WIRE_TRANSCRIPT_FILE_NAME
        )
        wire.write_text(
            json.dumps({"type": "metadata", "protocol_version": "1.4"}) + "\n",
            encoding="utf-8",
        )
        (session_directory / SESSION_STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "workDir": "/some/work/dir",
                    "lastPrompt": "hello",
                    "agents": {
                        MAIN_AGENT_IDENTIFIER: {
                            "homedir": str(session_directory),
                            "type": "main",
                            "parentAgentId": None,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        env = {KIMI_CODE_HOME_ENVIRONMENT_VARIABLE: str(home)}

        assert resolved_kimi_code_home_directory(env) == home
        assert kimi_code_sessions_root_directory(env) == home / SESSIONS_DIRECTORY_NAME

        # 前缀归一：带与不带 `session_` 必须解析到同一个目录。
        bare = session_id[len(SESSION_DIRECTORY_NAME_PREFIX) :]
        assert normalized_session_directory_name(bare) == session_id
        assert normalized_session_directory_name(session_id) == session_id
        assert normalized_session_directory_name("") == ""
        assert resolve_session_directory(bare, env) == session_directory
        assert resolve_session_directory(session_id, env) == session_directory

        # 无索引时必须靠扫桶就能解析（索引只是快路径，不是必需品）。
        assert not (home / SESSION_INDEX_FILE_NAME).exists()
        assert resolve_agent_wire_transcript_path(session_id, environment=env) == wire

        state = read_session_state(session_id, env)
        assert state is not None and state["workDir"] == "/some/work/dir"
        assert state["lastPrompt"] == "hello"
        assert "role" not in state["agents"][MAIN_AGENT_IDENTIFIER], (
            "实测 Kimi 的 agents[<id>] 没有 role 字段；若哪天有了，transcript 归因要重新设计。"
        )

        # 索引存在且正确时走快路径，结果必须与扫桶一致。
        (home / SESSION_INDEX_FILE_NAME).write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "sessionDir": str(session_directory),
                    "workDir": "/some/work/dir",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert resolve_session_directory(session_id, env) == session_directory

        # 索引指向不存在的路径 ⇒ 必须回落扫桶，而不是返回一个坏路径。
        (home / SESSION_INDEX_FILE_NAME).write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "sessionDir": str(home / "gone" / session_id),
                    "workDir": "/some/work/dir",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert resolve_session_directory(session_id, env) == session_directory, (
            "索引落后于磁盘时必须回落扫桶——返回坏路径会让 transcript 探测拿到 None 而兜底 Codex，"
            "RVF 于是在 Kimi 上发错线材并静默失声。"
        )

        # 损坏的索引不得让整条解析抛异常。
        (home / SESSION_INDEX_FILE_NAME).write_text("{ not json\n\n", encoding="utf-8")
        assert resolve_session_directory(session_id, env) == session_directory

        # 未知 session / 不存在的 agent ⇒ None（不造假路径）。
        assert resolve_session_directory("session_does-not-exist", env) is None
        assert resolve_agent_wire_transcript_path(session_id, "no-such-agent", env) is None
        assert read_session_state("session_does-not-exist", env) is None

    # 交叉验证：若本机确有 Kimi 会话，索引快路径与扫桶慢路径必须给出同一答案。
    live_root = kimi_code_sessions_root_directory({})
    cross_checked = 0
    if live_root.is_dir():
        for bucket in sorted(live_root.iterdir()):
            if not bucket.is_dir() or not bucket.name.startswith(
                WORKING_DIRECTORY_BUCKET_DIRECTORY_NAME_PREFIX
            ):
                continue
            for candidate in sorted(bucket.iterdir()):
                if not candidate.is_dir() or not candidate.name.startswith(
                    SESSION_DIRECTORY_NAME_PREFIX
                ):
                    continue
                indexed = _session_directory_from_index(candidate.name, {})
                scanned = _session_directory_by_scanning_buckets(candidate.name, {})
                assert scanned == candidate, (candidate, scanned)
                if indexed is not None:
                    assert indexed == scanned, (
                        f"{candidate.name}: 索引给出 {indexed}，扫桶给出 {scanned}"
                    )
                    cross_checked += 1
    print(
        "kimi session_layout self-check OK"
        + (f"（另交叉验证 {cross_checked} 个 live 会话）" if cross_checked else "（本机无 live 会话，仅合成断言）")
    )


if __name__ == "__main__":
    _self_check()
