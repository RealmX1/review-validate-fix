#!/usr/bin/env python3
"""第三代 Codex rollout（``item_completed`` 信封）的解包与三代并存归属测试。

背景：2026-08 的 ``codex-tui`` 把原先散在 ``event_msg`` 的若干子类型搬进统一的
``payload.type == "item_completed"`` 信封（真内容在 ``payload.item``、由 ``item.type``
区分）。RVF 的编辑归属只认 ``patch_apply_end``，于是归属归零，fail-loud 守卫把整个
Stop hook loud-skip（run rvf-20260812T153919Z-dispatcher-ac459543）。

这些测试锁定三件事：
1. 信封解包原语对每种 item 的字段映射（含 ``text``/``Text`` 大小写混用、``file://`` cwd）。
2. **三代并存**下归属不重不漏——Codex 支持 resume / 切换模型 / 不同前端，同一天甚至同一
   session 内会出现不同代记录。
3. 刻意不翻译 ``UserMessage`` / ``AgentMessage``：这两类已有 ``response_item/message``
   兜底，再翻一遍会把旧世代的双份冗余搬过来（重复切分 / 重复计数）。

⚠️ 世代判定一律基于 payload/item 结构，**绝不能看 ``cli_version``**——实测同一版本号
（0.147.0）在 Codex Desktop 与 codex-tui 下走不同 schema。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.session_scope_allocation import session_change_manifest as manifest_module
from core.transcript import codex_rollout_item_envelope as envelope
from core.transcript.patch_parsing import synthesize_apply_patch_text_from_patch_apply_end

SESSION_MANIFEST = ROOT / "core" / "session_scope_allocation" / "session_change_manifest.py"


def _envelope_record(item: dict, *, timestamp: str = "2026-08-12T23:10:00Z") -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "item_completed", "thread_id": "th-1", "turn_id": "tu-1", "item": item},
    }


def _file_change_item(path: str, *, status: str = "completed", **change: object) -> dict:
    return {
        "type": "FileChange",
        "id": "exec-file-change-1",
        "changes": {path: dict(change)},
        "status": status,
        "stdout": "",
        "stderr": "",
    }


# ── 1. FileChange → patch_apply_end 形状映射（update / add / delete / move_path）


def test_file_change_update_maps_to_patch_apply_end_shape() -> None:
    item = _file_change_item(
        "/repo/src/foo.py",
        type="update",
        unified_diff="@@ -10,3 +10,5 @@\n unchanged\n-old\n+new\n+also new\n",
        move_path=None,
    )
    payload = envelope.legacy_patch_apply_end_payload_from_file_change_item(item)
    assert payload is not None
    assert payload["type"] == "patch_apply_end"
    assert payload["success"] is True
    assert payload["call_id"] == "exec-file-change-1"
    text = synthesize_apply_patch_text_from_patch_apply_end(payload)
    assert text is not None
    assert "*** Update File: /repo/src/foo.py" in text
    assert "+also new" in text


def test_file_change_add_and_delete_synthesize_mutation_lines() -> None:
    add = envelope.legacy_patch_apply_end_payload_from_file_change_item(
        _file_change_item("/repo/src/new.py", type="add", content="line one\nline two\n")
    )
    delete = envelope.legacy_patch_apply_end_payload_from_file_change_item(
        _file_change_item("/repo/src/gone.py", type="delete", content="bye\n")
    )
    add_text = synthesize_apply_patch_text_from_patch_apply_end(add)
    delete_text = synthesize_apply_patch_text_from_patch_apply_end(delete)
    assert "*** Add File: /repo/src/new.py" in add_text
    assert "+line one" in add_text and "+line two" in add_text
    assert "*** Delete File: /repo/src/gone.py" in delete_text
    assert "-bye" in delete_text


def test_file_change_move_path_precedes_hunks() -> None:
    """``*** Move to:`` 必须写在 hunk 之前——parse_apply_patch_details 是按行状态机，
    放在 unified_diff 之后会让 mutation hunk 先归到 source（RVF-001 F2）。"""
    payload = envelope.legacy_patch_apply_end_payload_from_file_change_item(
        _file_change_item(
            "/repo/src/old_name.py",
            type="update",
            unified_diff="@@ -1,1 +1,1 @@\n-a\n+b\n",
            move_path="/repo/src/new_name.py",
        )
    )
    text = synthesize_apply_patch_text_from_patch_apply_end(payload)
    assert text.index("*** Move to: /repo/src/new_name.py") < text.index("@@ -1,1 +1,1 @@")


# ── 2. 未完成的 FileChange 不产生归属


def test_file_change_not_completed_yields_no_ownership() -> None:
    payload = envelope.legacy_patch_apply_end_payload_from_file_change_item(
        _file_change_item(
            "/repo/src/foo.py", status="in_progress", type="update", unified_diff="@@ -1,1 +1,1 @@\n-a\n+b\n"
        )
    )
    assert payload is not None
    assert payload["success"] is False
    assert synthesize_apply_patch_text_from_patch_apply_end(payload) is None


# ── 3. UserMessage 文本与 text_elements（含 text/Text 大小写混用）


def test_user_message_item_flattens_text_and_elements() -> None:
    element = {"byte_range": {"start": 0, "end": 40}, "placeholder": "$review-validate-fix:review-validate-fix"}
    item = {
        "type": "UserMessage",
        "id": "um-1",
        "content": [
            {"type": "text", "text": "$review-validate-fix:review-validate-fix", "text_elements": [element]},
            {"type": "Text", "text": "second block", "text_elements": []},
        ],
    }
    text, elements = envelope.user_message_text_and_elements_from_user_message_item(item)
    assert text == "$review-validate-fix:review-validate-fix\nsecond block"
    assert elements == [element]


def test_agent_message_item_uses_capitalised_text_blocks() -> None:
    """``AgentMessage`` 的块是 ``{"type": "Text"}``（大写），按 type 判定必然漏读。"""
    item = {"type": "AgentMessage", "id": "am-1", "content": [{"type": "Text", "text": "hello"}], "phase": "commentary"}
    assert envelope.agent_message_text_from_item(item) == "hello"


# ── 4. CommandExecution：argv→cmd、file:// cwd 剥离


def test_command_execution_item_unwraps_shell_argv_and_file_uri_cwd() -> None:
    item = {
        "type": "CommandExecution",
        "id": "exec-1",
        "command": ["/bin/zsh", "-lc", "git status --short"],
        "cwd": "file:///repo/worktree",
        "exit_code": 0,
        "status": "completed",
    }
    assert envelope.shell_command_and_workdir_from_command_execution_item(item) == (
        "git status --short",
        "/repo/worktree",
    )


def test_command_execution_item_without_shell_wrapper_joins_argv() -> None:
    item = {"type": "CommandExecution", "id": "exec-2", "command": ["git", "status", "--short"], "cwd": "/repo"}
    command, workdir = envelope.shell_command_and_workdir_from_command_execution_item(item)
    assert command == "git status --short"
    assert workdir == "/repo"


# ── 5. 内嵌 JS 的 tools.exec_command 抽取：命中调用形态、不命中只读文本


def test_embedded_exec_command_extracted_from_js_object_literal() -> None:
    """实参普遍是**无引号 key 的 JS 对象字面量**（实测仅约 17% 是严格 JSON），
    ``json.loads`` 恢复率为 0——这是第二代 exec 归属全盘失效的第二个死因。"""
    strict_json = (
        'const r = await tools.exec_command({"cmd":"git status --short","workdir":"/repo",'
        '"yield_time_ms":10000}); text(r.output);'
    )
    bare_keys = 'const r = await tools.exec_command({cmd:"git log -5",workdir:"/repo"}); text(r.output);'
    assert envelope.embedded_exec_command_invocation_from_tool_input(strict_json) == ("git status --short", "/repo")
    assert envelope.embedded_exec_command_invocation_from_tool_input(bare_keys) == ("git log -5", "/repo")


def test_embedded_exec_command_ignores_read_only_mentions_and_interpolation() -> None:
    """只读 JS（提到 ``exec_command`` 但不调用）不得被当成一次执行；带 ``${}`` 插值的
    命令不是静态字面量，按「抽不到就放弃、不猜」返回 None。"""
    read_only = 'const t = ALL_TOOLS.filter(x => x.name === "exec_command"); text(JSON.stringify(t));'
    interpolated = "const r = await tools.exec_command({cmd:`git show ${sha}`,workdir:'/repo'});"
    assert envelope.embedded_exec_command_invocation_from_tool_input(read_only) is None
    assert envelope.embedded_exec_command_invocation_from_tool_input(interpolated) is None


def test_shell_invocation_source_distinguishes_three_generations() -> None:
    first = {"payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "ls", "workdir": "/repo"})}}
    second = {"payload": {"type": "custom_tool_call", "name": "exec", "input": 'await tools.exec_command({cmd:"ls",workdir:"/repo"});'}}
    third = _envelope_record({"type": "CommandExecution", "id": "e", "command": ["/bin/zsh", "-lc", "ls"], "cwd": "file:///repo"})
    assert manifest_module.shell_invocation_from_record(first) == (
        "ls", "/repo", manifest_module.SHELL_INVOCATION_SOURCE_EXEC_COMMAND_TOOL
    )
    assert manifest_module.shell_invocation_from_record(second) == (
        "ls", "/repo", manifest_module.SHELL_INVOCATION_SOURCE_EMBEDDED_JS
    )
    assert manifest_module.shell_invocation_from_record(third) == (
        "ls", "/repo", manifest_module.SHELL_INVOCATION_SOURCE_COMMAND_EXECUTION_ITEM
    )


# ── 6. 未知 item.type 被前向兼容告警捕获


def test_unrecognized_item_types_reported_and_known_ones_are_not() -> None:
    records = [
        _envelope_record({"type": "FileChange", "changes": {}, "status": "completed"}),
        _envelope_record({"type": "Plan", "text": "x"}),
        _envelope_record({"type": "SomeFourthGenerationItem", "id": "z"}),
        _envelope_record({"type": "AnotherNewThing"}),
        {"type": "event_msg", "payload": {"type": "patch_apply_end", "success": True, "changes": {}}},
    ]
    assert envelope.unrecognized_codex_item_types(records) == ["AnotherNewThing", "SomeFourthGenerationItem"]


# ── 7. 伪路径不得掩盖 fail-loud 守卫


def test_shell_operator_terminates_write_command_argument_scan(tmp_path: Path) -> None:
    """``rm -r /tmp/x && git diff …`` 里的 ``&&`` 曾被解析成一个名叫 ``&&`` 的「被改动路径」。
    这类伪路径不构成审查单元，却会让 ``owned_paths`` 非空，从而**掩盖 schema-drift 守卫**
    （判据是「有编辑证据 + dirty + 零归属」）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    candidates = manifest_module.command_path_candidates(
        repo, "rm -r /tmp/outside-the-repo && git diff --stat", repo
    )
    assert [c["path"] for c in candidates] == []


# ── 8/9. 端到端：三代并存的合成 rollout 走 build_manifest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "rvf@example.test")
    _git(path, "config", "user.name", "RVF Test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-q", "-m", "seed")
    return path


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _build_manifest(repo: Path, transcript: Path, output: Path) -> dict:
    subprocess.run(
        [
            sys.executable, str(SESSION_MANIFEST),
            "--repo", str(repo),
            "--transcript", str(transcript),
            "--output", str(output),
            "--no-tracker",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def test_third_generation_rollout_attributes_edits_without_schema_drift(tmp_path: Path) -> None:
    """本次故障的最小复现：纯第三代 rollout（零 ``patch_apply_end``、零 ``exec_command``）
    必须归属出编辑路径，且 ``schema_drift_suspected`` 为 False。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "seed.txt").write_text("seed\nedited\n", encoding="utf-8")
    transcript = _write_jsonl(
        tmp_path / "rollout.jsonl",
        [
            {"timestamp": "2026-08-12T23:00:00Z", "type": "session_meta", "payload": {"id": "S3", "originator": "codex-tui"}},
            _envelope_record(
                {
                    "type": "UserMessage",
                    "id": "um",
                    "content": [{"type": "text", "text": "edit seed.txt", "text_elements": []}],
                }
            ),
            # 编辑证据只以第二代 exec 内嵌 JS 出现（apply_patch 调用），真相源是 FileChange。
            {
                "timestamp": "2026-08-12T23:09:00Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call_x",
                    "input": 'const r = await tools.apply_patch("*** Begin Patch\\n***");',
                },
            },
            _envelope_record(
                _file_change_item(
                    str(repo / "seed.txt"),
                    type="update",
                    unified_diff="@@ -1,1 +1,2 @@\n seed\n+edited\n",
                    move_path=None,
                )
            ),
        ],
    )
    result = _build_manifest(repo, transcript, tmp_path / "manifest.json")
    assert result["schema_drift_suspected"] is False
    assert result["owned_dirty_paths"] == ["seed.txt"]
    assert result["unattributed_dirty_paths"] == []


def test_three_generations_coexisting_attribute_without_duplication(tmp_path: Path) -> None:
    """resume / 切换模型的混合 session：同一 transcript 里三代记录并存。归属必须不重不漏，
    且同一次 shell 执行不因第二代 ``exec`` 与第三代 ``CommandExecution`` 各记一次而重复。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "first.txt").write_text("one\n", encoding="utf-8")
    (repo / "second.txt").write_text("two\n", encoding="utf-8")
    (repo / "third.txt").write_text("three\n", encoding="utf-8")
    shared_command = "tee third.txt"
    transcript = _write_jsonl(
        tmp_path / "rollout.jsonl",
        [
            {"timestamp": "2026-08-12T23:00:00Z", "type": "session_meta", "payload": {"id": "MIX"}},
            # 第一代：apply_patch 工具调用
            {
                "timestamp": "2026-08-12T23:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "call_legacy",
                    "input": "*** Begin Patch\n*** Add File: first.txt\n@@\n+one\n*** End Patch\n",
                },
            },
            # 第二代：patch_apply_end（覆盖旧式未覆盖的新路径 → 必须照常摄入）
            {
                "timestamp": "2026-08-12T23:02:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "exec-gen2",
                    "success": True,
                    "changes": {str(repo / "second.txt"): {"type": "add", "content": "two\n"}},
                },
            },
            # 同一次 shell 执行的两代记录：第二代调用 + 第三代结果，只能算一次。
            {
                "timestamp": "2026-08-12T23:03:00Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call_exec",
                    "input": f'const r = await tools.exec_command({{cmd:"{shared_command}",workdir:"{repo}"}});',
                },
            },
            _envelope_record(
                {
                    "type": "CommandExecution",
                    "id": "exec-gen3",
                    "command": ["/bin/zsh", "-lc", shared_command],
                    "cwd": f"file://{repo}",
                    "exit_code": 0,
                    "status": "completed",
                },
                timestamp="2026-08-12T23:03:01Z",
            ),
        ],
    )
    result = _build_manifest(repo, transcript, tmp_path / "manifest.json")
    assert result["schema_drift_suspected"] is False
    assert result["owned_dirty_paths"] == ["first.txt", "second.txt", "third.txt"]
    shared = [event for event in result["command_events"] if event["cmd"] == shared_command]
    assert len(shared) == 1, "同一次 shell 执行的两代记录不得各登记一次"
    assert [c["path"] for c in result["command_path_candidates"]] == ["third.txt"]


def test_third_generation_file_change_recovers_trajectory_artifact_refs(tmp_path: Path) -> None:
    """trajectory distill 侧：``patch_apply_end`` 分支在第三代死掉后，编辑不再产出
    ``artifact_refs``，rvf-analyze 的 issue↔patch 因果链在 codex-tui 会话**静默**全盲
    （不报错，所以比归属失明更隐蔽）。信封里的 FileChange 必须走同一条合成路径。"""
    sys.path.insert(0, str(ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix" / "scripts"))
    from adapters.codex.transcript import distill_codex_jsonl

    transcript = _write_jsonl(
        tmp_path / "rollout.jsonl",
        [
            {"timestamp": "2026-08-12T23:00:00Z", "type": "session_meta", "payload": {"id": "TRJ"}},
            _envelope_record(
                _file_change_item(
                    "src/foo.py",
                    type="update",
                    unified_diff="@@ -10,3 +10,5 @@\n unchanged\n-old\n+new\n+also new\n",
                    move_path=None,
                )
            ),
        ],
    )
    distilled, _index = distill_codex_jsonl(rollout_path=transcript, rollout_filename="rollout.jsonl", repo=None)
    patch_results = [
        item for item in distilled if item["kind"] == "tool_result" and item.get("tool") == "apply_patch"
    ]
    assert patch_results, "信封里的 FileChange 必须 distill 成 apply_patch tool_result"
    assert patch_results[0]["call_id"] == "exec-file-change-1"
    assert [ref["path"] for ref in patch_results[0]["artifact_refs"]] == ["src/foo.py"]
    assert patch_results[0]["artifact_refs"][0]["op"] == "edit"


def test_third_generation_user_messages_are_not_double_counted(tmp_path: Path) -> None:
    """第三代 transcript 里每个用户回合成对出现（``response_item`` + 信封 ``UserMessage``）。
    读消息的站点早有 ``response_item/message`` 兜底，故**刻意不翻译**信封那份——否则会把
    旧世代的双份冗余搬过来。这条测试守住那个决定。"""
    sys.path.insert(0, str(ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix" / "scripts"))
    from adapters.codex.transcript import distill_codex_jsonl

    repo = _init_repo(tmp_path / "repo")
    transcript = _write_jsonl(
        tmp_path / "rollout.jsonl",
        [
            {"timestamp": "2026-08-12T23:00:00Z", "type": "session_meta", "payload": {"id": "DUP"}},
            {
                "timestamp": "2026-08-12T23:00:01Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "text", "text": "do the thing"}]},
            },
            _envelope_record(
                {
                    "type": "UserMessage",
                    "id": "um",
                    "content": [{"type": "text", "text": "do the thing", "text_elements": []}],
                }
            ),
            {
                "timestamp": "2026-08-12T23:00:03Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "on it"}]},
            },
            _envelope_record({"type": "AgentMessage", "id": "am", "content": [{"type": "Text", "text": "on it"}]}),
        ],
    )
    distilled, _index = distill_codex_jsonl(rollout_path=transcript, rollout_filename="rollout.jsonl", repo=repo)
    assert [item["summary"] for item in distilled if item.get("role") == "user"] == ["do the thing"]
    assert [item["summary"] for item in distilled if item.get("role") == "assistant"] == ["on it"]


def test_bridge_recovers_invoked_skill_from_envelope_and_matches_vendored_on_legacy(tmp_path: Path) -> None:
    """显式 ``$skill`` 触发检测：vendored 迭代器只认第二代，对第三代信封全盲（失败形式是
    「什么都没发生」，比 loud-skip 更隐蔽）。桥接必须认回来，且对第二代与 vendored 逐项等价。"""
    scripts = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix" / "scripts"
    sys.path.insert(0, str(scripts))
    import codex_invoked_skill as vendored
    import codex_invoked_skill_rollout_schema_bridge as bridge

    element = {"byte_range": {"start": 0, "end": 40}, "placeholder": "$review-validate-fix:review-validate-fix"}
    third_generation = _write_jsonl(
        tmp_path / "gen3.jsonl",
        [
            _envelope_record(
                {
                    "type": "UserMessage",
                    "id": "um",
                    "content": [
                        {"type": "text", "text": "$review-validate-fix:review-validate-fix", "text_elements": [element]}
                    ],
                }
            )
        ],
    )
    second_generation = _write_jsonl(
        tmp_path / "gen2.jsonl",
        [
            {
                "timestamp": "2026-08-11T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "$review-validate-fix:review-validate-fix",
                    "text_elements": [element],
                },
            }
        ],
    )
    assert vendored.invoked_skills_from_transcript(third_generation, which="all") == []
    assert bridge.was_skill_invoked(third_generation, "review-validate-fix") is True
    legacy_vendored = [s.placeholder for s in vendored.invoked_skills_from_transcript(second_generation, which="all")]
    legacy_bridge = [s.placeholder for s in bridge.invoked_skills_from_transcript(second_generation, which="all")]
    assert legacy_bridge == legacy_vendored == ["$review-validate-fix:review-validate-fix"]
