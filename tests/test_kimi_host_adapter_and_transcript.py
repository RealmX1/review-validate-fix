#!/usr/bin/env python3
"""行为 gate：Kimi Code CLI host adapter + wire transcript 蒸馏。

护的不变量：

* **KIMI-1（Protocol）** —— ``KimiCodeCliHostAdapter`` 满足 ``HostAdapter``，6 个方法各自
  返回约定值；出口线材是 ``hook_specific_output_permission_decision_deny``。
* **KIMI-2（origin 区分）** —— ``context.append_message`` 的 ``role`` 实测**全是** ``user``，
  真正区分来源的是 ``origin.kind``：``user`` 才是用户说的，``injection`` /
  ``system_trigger`` / ``skill_activation`` / ``background_task`` 都是 harness 写的。把它们
  当成用户发言会污染 RVF 的 scope 归属（「用户说了什么」是归属的基础），故非 user 来源必须
  落 ``phase_marker`` 而不是 ``message``。判据是**正向**的 ``kind == "user"``（负向判据挡不住
  将来新增的来源），且**会话命名与蒸馏必须共用同一个判据**——各写一份会漂移，会话名会退化
  成 system-reminder 正文或子代理派发 prompt。``turn.prompt`` 也带自己的 ``origin``，同样要过
  这道闸。
* **KIMI-3（think 的正文字段）** —— ``content.part`` 里 ``think`` 的正文在 **``part.think``**，
  不在 ``part.text``。按 ``part["text"]`` 一把抓会让所有推理内容静默变成空串（实测 think 占
  ``content.part`` 的 67%）。
* **KIMI-4（写型工具的参数名）** —— Kimi 的 ``Write`` / ``Edit`` 用 **``path``**，不是 Claude
  的 ``file_path``。照抄 Claude 的键名会让 ``artifact_refs`` 恒为空——**静默的**，因为缺键
  只是取不到值、不会抛。
* **KIMI-5（未知 record 可见而不炸）** —— 新版本 Kimi 新增 record type 时，蒸馏必须继续跑完
  并把它计进 ``unknown_record_type_counts``。静默丢弃会让蒸馏悄悄变得不完整；抛异常又会让
  整条链路因一个无关事件而断掉。
* **KIMI-6（session 布局）** —— ``session_id`` 自带 ``session_`` 前缀（再拼一次会找不到目录
  且无报错）；``KIMI_CODE_HOME`` 是生产 override；``session_index.jsonl`` 只是快路径，索引
  失效时必须回落扫桶。
* **KIMI-7（子代理名册）** —— Kimi 的子代理来自 ``state.json`` 的 ``agents`` 名册（不是
  Codex 的 spawn 事件、也不是 Claude 的 ``subagents/`` glob）。主 agent 必须被跳过——把它当
  成自己的子代理会造成蒸馏自嵌套。跳过用**双判据**（``type=="main"`` 或 ``parentAgentId``
  为空），任一字段缺失时另一个仍挡得住。
* **KIMI-8（子代理派发）** —— ``subagent_capture`` 的三处 host 派发必须各自选到 Kimi 的实现；
  漏接一处会静默退回 Codex 的实现去解析 Kimi 的数据。
* **KIMI-9（主轨迹派发）** —— KIMI-8 只覆盖**子代理**轨迹。``trajectory_capture`` 另有两处
  独立的 host 派发（主轨迹蒸馏、RVF-起点 cut），漏接同样静默：Codex 的解析器读 Kimi wire
  不抛，只返回 **0 条 / None**。cut 还多一层陷阱——Kimi 的 ``time`` 是 **epoch 毫秒整数**，
  照抄另两家的「只认 ISO 串」会让 ``since_timestamp`` 把全部候选滤掉。
* **KIMI-10（父会话 context 渲染）** —— ``rvf_parent_context.render_lines`` 的渲染器分派同理；
  漏接会让 Kimi 父会话的 context blob 静默渲染成空行，child agent 只是拿不到背景。
* **KIMI-11（harness 身份 token）** —— ``round_baseline_marker`` 的 key 里带 harness token。
  同一个 harness 在不同信号层（host 探测值 / hook 注入的 agent id / kanban 的 ``AI_AGENT``）
  必须归一成**同一个**串；裂开就是写者写 A、读者读 B，marker 凭空消失 ⇒ 漏审，且零报错。
* **KIMI-12（来源标签短前缀）** —— 父会话来源标签的 host 短前缀不得是两宿主二分。

KIMI-9/10/11/12 共享同一个形状：**一处 ``if host == HOST_CLAUDE: … else: <Codex 实现>``**。
它在新 host 上永远不报错，只是安静地拿 Codex 的解析器去读别家的数据。所以每一处都配一条
**结构性** gate：逐 ``KNOWN_HOST_IDENTITIES`` 断言分派各有其人且两两不同——加第四个 host 却
忘了补分支时当场炸，而不是等线上发现「RVF 在那个 host 上什么都没审到」。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix" / "scripts"


def _run_in_repo_context(body: str) -> dict:
    """在子进程里跑一段用 adapters.kimi 的代码，返回它 print 的 JSON。

    与既有 host 判定 gate 同口径：不把 ``core`` / ``adapters`` import 进 pytest 进程，
    避免测试之间经模块级状态互相污染。
    """
    script = (
        f"import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n" + body
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def _write_wire(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _distill(wire: Path) -> dict:
    return _run_in_repo_context(
        "from pathlib import Path\n"
        "from adapters.kimi.transcript import distill_kimi_wire_jsonl\n"
        f"d, i = distill_kimi_wire_jsonl(rollout_path=Path({str(wire)!r}), "
        f"rollout_filename={wire.name!r}, repo=None)\n"
        "print(json.dumps({'distilled': d, 'index': i}, ensure_ascii=False))\n"
    )


METADATA = {"type": "metadata", "protocol_version": "1.4", "created_at": 1787371449575}


# ── KIMI-1：Protocol 与 6 个方法 ──────────────────────────────────────────────


def test_adapter_satisfies_host_adapter_protocol_and_returns_expected_values() -> None:
    result = _run_in_repo_context(
        "from adapters.kimi.kimi_code_cli_host_adapter import KimiCodeCliHostAdapter\n"
        "from core.host_adapter.host_adapter_protocol import HostAdapter\n"
        "a = KimiCodeCliHostAdapter()\n"
        "print(json.dumps({\n"
        "  'is_protocol': isinstance(a, HostAdapter),\n"
        "  'display': a.host_display_name(),\n"
        "  'deep_link': a.session_deep_link('session_abc'),\n"
        "  'kanban': a.cline_kanban_agent_id(),\n"
        "  'skip_mode': a.detect_host_skip_mode({}),\n"
        "  'quoted_none': a.parent_transcript_quoted_session_name(None),\n"
        "  'channel': a.stop_hook_agent_directive_channel(),\n"
        "}))\n"
    )
    assert result["is_protocol"] is True
    assert result["display"] == "Kimi Code CLI"
    assert result["deep_link"] is None, (
        "`kimi -S <id>` 是 CLI 参数不是 URI scheme；深链消费者要的是可点击链接"
    )
    assert result["kanban"] == "kimi"
    assert result["skip_mode"] is None
    assert result["quoted_none"] is None
    assert result["channel"] == "hook_specific_output_permission_decision_deny", (
        "发错线材在 Kimi 上不报错，只会静默 allow —— 这条只能靠 gate 钉"
    )


def test_quoted_session_name_reads_the_first_user_prompt(tmp_path: Path) -> None:
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        # 实测 turn.prompt 恒带自己的 origin（52/52），故 fixture 也必须带——
        # 不带 origin 的 turn.prompt 在真实 wire 里不存在。
        [
            METADATA,
            {
                "type": "turn.prompt",
                "origin": {"kind": "user"},
                "input": [{"type": "text", "text": "帮我看下这段"}],
            },
        ],
    )
    result = _run_in_repo_context(
        "from pathlib import Path\n"
        "from adapters.kimi.kimi_code_cli_host_adapter import KimiCodeCliHostAdapter\n"
        f"print(json.dumps({{'name': KimiCodeCliHostAdapter()"
        f".parent_transcript_quoted_session_name(Path({str(wire)!r}))}}))\n"
    )
    assert result["name"] == '"帮我看下这段"'


def test_quoted_session_name_is_none_for_a_session_that_never_got_input(tmp_path: Path) -> None:
    """实测本机 42 份 wire 里有 6 份是「已初始化、零输入」——它们的正确答案是 None，
    不该硬凑一个名字出来。"""
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [METADATA, {"type": "config.update", "profileName": "agent"}],
    )
    result = _run_in_repo_context(
        "from pathlib import Path\n"
        "from adapters.kimi.kimi_code_cli_host_adapter import KimiCodeCliHostAdapter\n"
        f"print(json.dumps({{'name': KimiCodeCliHostAdapter()"
        f".parent_transcript_quoted_session_name(Path({str(wire)!r}))}}))\n"
    )
    assert result["name"] is None


# ── KIMI-2：origin.kind 区分真实输入与系统注入 ────────────────────────────────


def test_user_message_and_injection_are_not_conflated(tmp_path: Path) -> None:
    wire = _write_wire(
        tmp_path / "wire.jsonl",
        [
            METADATA,
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "origin": {"kind": "user"},
                    "content": [{"type": "text", "text": "真实用户输入"}],
                },
            },
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "origin": {"kind": "injection", "variant": "todo_list_reminder"},
                    "content": [{"type": "text", "text": "<system-reminder>…</system-reminder>"}],
                },
            },
        ],
    )
    out = _distill(wire)
    kinds = out["index"]["kind_counts"]
    assert kinds.get("message") == 1, (
        f"injection 被当成了用户发言：{kinds}。role 实测全是 'user'，区分来源的是 origin.kind；"
        "混为一谈会污染 RVF 的 scope 归属。"
    )
    assert kinds.get("phase_marker") == 1, kinds
    markers = [r for r in out["distilled"] if r["kind"] == "phase_marker"]
    assert markers[0]["marker"] == "injection:todo_list_reminder", markers
    messages = [r for r in out["distilled"] if r["kind"] == "message"]
    assert messages[0]["summary"] == "真实用户输入"


def test_turn_prompt_is_not_double_counted(tmp_path: Path) -> None:
    """``turn.prompt`` 与 origin.kind=='user' 的 append_message 是同一条输入的两次记录。"""
    wire = _write_wire(
        tmp_path / "wire.jsonl",
        [
            METADATA,
            {"type": "turn.prompt", "input": [{"type": "text", "text": "同一条输入"}]},
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "origin": {"kind": "user"},
                    "content": [{"type": "text", "text": "同一条输入"}],
                },
            },
        ],
    )
    out = _distill(wire)
    assert out["index"]["kind_counts"].get("message") == 1, out["index"]
    assert out["index"]["unknown_record_type_counts"].get("turn.prompt") == 1, out["index"]


NON_USER_ORIGINS = [
    {"kind": "injection", "variant": "permission_mode"},
    {"kind": "injection", "variant": "todo_list_reminder"},
    {"kind": "system_trigger"},
    {"kind": "skill_activation"},
    {"kind": "background_task"},
]
"""本机 48 份真实 wire 里实测到的**全部**非 user origin.kind。

``injection`` 只是其中一类：``system_trigger``（子代理派发 prompt，19 条）、
``skill_activation``（2 条）、``background_task``（后台任务完成通知，2 条）同样是
harness 写的、不是用户说的。判据必须是**正向**的 ``origin.kind == "user"``，
而不是「不等于 injection」——后者会把新出现的 harness 来源默认算成用户发言。
"""


@pytest.mark.parametrize("origin", NON_USER_ORIGINS, ids=lambda o: o["kind"] + ":" + str(o.get("variant")))
def test_session_naming_and_distillation_share_one_user_input_criterion(
    tmp_path: Path, origin: dict
) -> None:
    """会话命名与蒸馏必须用**同一个**判据判「这段文本是不是用户说的」。

    两处各写一份判据必然漂移：蒸馏认 ``origin.kind``、命名只认 ``role``（实测恒为
    ``user``）时，会话名会变成 system-reminder 正文或子代理派发 prompt。
    """
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [
            METADATA,
            {
                "type": "turn.prompt",
                "origin": origin,
                "input": [{"type": "text", "text": "harness 写的正文，不是用户说的"}],
            },
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "origin": origin,
                    "content": [{"type": "text", "text": "harness 写的正文，不是用户说的"}],
                },
            },
        ],
    )
    naming = _run_in_repo_context(
        "from pathlib import Path\n"
        "from adapters.kimi.kimi_code_cli_host_adapter import KimiCodeCliHostAdapter\n"
        "from adapters.kimi.transcript import first_user_prompt_text\n"
        f"p = Path({str(wire)!r})\n"
        "print(json.dumps({'text': first_user_prompt_text(p),\n"
        "  'name': KimiCodeCliHostAdapter().parent_transcript_quoted_session_name(p)}))\n"
    )
    assert naming["text"] is None, (
        f"origin.kind={origin['kind']} 的 harness 正文被当成了用户输入：{naming['text']!r}"
    )
    assert naming["name"] is None, naming["name"]
    kinds = _distill(wire)["index"]["kind_counts"]
    assert not kinds.get("message"), f"蒸馏把 origin.kind={origin['kind']} 算成了用户发言：{kinds}"
    assert kinds.get("phase_marker") == 1, kinds


def test_quoted_session_name_skips_a_leading_injection(tmp_path: Path) -> None:
    """注入排在 ``turn.prompt`` 之前时，会话名仍必须是**用户**那条。

    只认 ``role`` 会让会话名变成 ``<system-reminder>…`` 正文——role 实测恒为 ``user``，
    它区分不出来源。
    """
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [
            METADATA,
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "origin": {"kind": "injection", "variant": "permission_mode"},
                    "content": [{"type": "text", "text": "<system-reminder>Auto permission mode</system-reminder>"}],
                },
            },
            {
                "type": "turn.prompt",
                "origin": {"kind": "user"},
                "input": [{"type": "text", "text": "帮我把 Kimi 接进 RVF"}],
            },
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "origin": {"kind": "user"},
                    "content": [{"type": "text", "text": "帮我把 Kimi 接进 RVF"}],
                },
            },
        ],
    )
    result = _run_in_repo_context(
        "from pathlib import Path\n"
        "from adapters.kimi.kimi_code_cli_host_adapter import KimiCodeCliHostAdapter\n"
        f"print(json.dumps({{'name': KimiCodeCliHostAdapter()"
        f".parent_transcript_quoted_session_name(Path({str(wire)!r}))}}))\n"
    )
    assert result["name"] == '"帮我把 Kimi 接进 RVF"', result["name"]


# ── KIMI-3 / KIMI-4：think 正文字段 与 写型工具参数名 ─────────────────────────


def test_think_part_body_comes_from_the_think_field_not_text(tmp_path: Path) -> None:
    wire = _write_wire(
        tmp_path / "wire.jsonl",
        [
            METADATA,
            {
                "type": "context.append_loop_event",
                "event": {"type": "content.part", "part": {"type": "think", "think": "推理正文"}},
            },
            {
                "type": "context.append_loop_event",
                "event": {"type": "content.part", "part": {"type": "text", "text": "回答正文"}},
            },
        ],
    )
    out = _distill(wire)
    by_kind = {r["kind"]: r for r in out["distilled"]}
    assert by_kind["reasoning"]["summary"] == "推理正文", (
        "think 的正文在 part.think —— 按 part['text'] 取会静默变成空串"
    )
    assert by_kind["message"]["summary"] == "回答正文"
    assert by_kind["message"]["role"] == "assistant"


def test_empty_content_parts_are_dropped_not_emitted_as_blank_records(tmp_path: Path) -> None:
    """实测 593 条 content.part 里有 101 条正文为空——它们不该变成空 record。"""
    wire = _write_wire(
        tmp_path / "wire.jsonl",
        [
            METADATA,
            {
                "type": "context.append_loop_event",
                "event": {"type": "content.part", "part": {"type": "think", "think": "   "}},
            },
        ],
    )
    out = _distill(wire)
    assert out["index"]["record_count"] == 0, out
    assert out["index"]["unknown_record_type_counts"].get("context.append_loop_event") == 1


@pytest.mark.parametrize(
    ("tool", "args", "expected_op"),
    [
        ("Write", {"path": "/repo/new.py", "content": "x"}, "create"),
        ("Edit", {"path": "/repo/old.py", "old_string": "a", "new_string": "b"}, "edit"),
    ],
)
def test_write_tools_use_path_argument_not_file_path(
    tmp_path: Path, tool: str, args: dict, expected_op: str
) -> None:
    """Kimi 用 ``path``；照抄 Claude 的 ``file_path`` 会让 artifact_refs 恒为空且不报错。"""
    wire = _write_wire(
        tmp_path / "wire.jsonl",
        [
            METADATA,
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "tool.call",
                    "name": tool,
                    "args": args,
                    "toolCallId": "tool_1",
                },
            },
        ],
    )
    out = _distill(wire)
    call = [r for r in out["distilled"] if r["kind"] == "tool_call"][0]
    assert call["tool"] == tool
    assert call["call_id"] == "tool_1"
    assert call["artifact_refs"] == [
        {"path": args["path"], "lines": None, "op": expected_op}
    ], call


def test_tool_call_and_result_pair_through_tool_call_id(tmp_path: Path) -> None:
    wire = _write_wire(
        tmp_path / "wire.jsonl",
        [
            METADATA,
            {
                "type": "context.append_loop_event",
                "event": {"type": "tool.call", "name": "Bash", "args": {"command": "ls"},
                          "toolCallId": "tool_42"},
            },
            {
                "type": "context.append_loop_event",
                "event": {"type": "tool.result", "toolCallId": "tool_42",
                          "result": {"output": "a\nb"}},
            },
        ],
    )
    out = _distill(wire)
    call = [r for r in out["distilled"] if r["kind"] == "tool_call"][0]
    result = [r for r in out["distilled"] if r["kind"] == "tool_result"][0]
    assert call["call_id"] == result["call_id"] == "tool_42"
    assert result["summary"] == "a\nb"


# ── KIMI-5：未知 record 可见而不炸 ────────────────────────────────────────────


def test_unknown_record_types_are_counted_and_never_raise(tmp_path: Path) -> None:
    wire = _write_wire(
        tmp_path / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "1.5"},
            {"type": "a.brand.new.event.from.a.future.kimi", "payload": {"whatever": 1}},
            {"type": "context.append_loop_event", "event": {"type": "step.begin", "step": 1}},
            {
                "type": "context.append_message",
                "message": {"role": "user", "origin": {"kind": "user"},
                            "content": [{"type": "text", "text": "仍要被抽出来"}]},
            },
        ],
    )
    out = _distill(wire)
    assert out["index"]["kind_counts"].get("message") == 1, (
        "遇到未知 record 后必须继续跑完，而不是丢掉后面的内容"
    )
    unknown = out["index"]["unknown_record_type_counts"]
    assert unknown.get("a.brand.new.event.from.a.future.kimi") == 1, unknown
    assert unknown.get("metadata") == 1, unknown
    assert unknown.get("context.append_loop_event") == 1, unknown


def test_malformed_lines_do_not_break_distillation(tmp_path: Path) -> None:
    wire = tmp_path / "wire.jsonl"
    wire.write_text(
        json.dumps(METADATA) + "\n"
        "{ not json\n"
        "\n"
        + json.dumps(
            {"type": "context.append_message",
             "message": {"role": "user", "origin": {"kind": "user"},
                         "content": [{"type": "text", "text": "活下来了"}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    out = _distill(wire)
    assert out["index"]["kind_counts"].get("message") == 1, out["index"]


# ── KIMI-6：session 布局 ──────────────────────────────────────────────────────


def test_session_layout_self_check_passes() -> None:
    """``session_layout`` 的 self-check 覆盖前缀归一 / 索引快路径 / 索引失效回落 / 生产
    override —— 它同时被契约 gate 以 ``run_step`` 执行，这里再钉一次防止 gate 条目被删。"""
    completed = subprocess.run(
        [sys.executable, str(ROOT / "adapters" / "kimi" / "session_layout.py")],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "self-check OK" in completed.stdout, completed.stdout


# ── KIMI-7：子代理名册 ────────────────────────────────────────────────────────


def _write_session_with_agents(root: Path, agents: dict) -> Path:
    """造一个 ``<session_dir>/agents/<id>/wire.jsonl`` 布局，返回主 agent 的 wire 路径。"""
    session_dir = root / "wd_proj_hash" / "session_abc"
    for agent_id in agents:
        _write_wire(session_dir / "agents" / agent_id / "wire.jsonl", [METADATA])
    (session_dir / "state.json").write_text(
        json.dumps(
            {
                "workDir": "/some/repo",
                "lastPrompt": "hi",
                "agents": {
                    agent_id: {**meta, "homedir": str(session_dir / "agents" / agent_id)}
                    for agent_id, meta in agents.items()
                },
            }
        ),
        encoding="utf-8",
    )
    return session_dir / "agents" / "main" / "wire.jsonl"


def _resolve_subagents(main_wire: Path) -> dict:
    return _run_in_repo_context(
        "from pathlib import Path\n"
        "from adapters.kimi.subagent import resolve_subagents\n"
        f"found = resolve_subagents(main_rollout_path=Path({str(main_wire)!r}))\n"
        "print(json.dumps([{'agent_id': s.agent_id, 'role': s.role, 'call_id': s.call_id,\n"
        "  'ts': s.ts, 'prompt': s.prompt, 'nickname': s.nickname,\n"
        "  'transcript': str(t) if t else None} for s, t in found]))\n"
    )


def test_subagents_come_from_the_state_json_roster(tmp_path: Path) -> None:
    main_wire = _write_session_with_agents(
        tmp_path,
        {
            "main": {"type": "main", "parentAgentId": None},
            "agent-0": {"type": "sub", "parentAgentId": "main", "swarmItem": "分析 A"},
            "agent-1": {"type": "sub", "parentAgentId": "main"},
        },
    )
    found = _resolve_subagents(main_wire)
    assert [f["agent_id"] for f in found] == ["agent-0", "agent-1"], found
    assert all(f["transcript"] is not None for f in found), found
    assert found[0]["prompt"] == "分析 A", "AgentSwarm 派生的 swarmItem 就是派生 prompt"
    assert found[1]["prompt"] is None, "普通 Agent 派生的不带 swarmItem，应为 None 而非空串"


def test_main_agent_is_never_reported_as_its_own_subagent(tmp_path: Path) -> None:
    """把主 agent 当子代理会造成蒸馏自嵌套。"""
    main_wire = _write_session_with_agents(
        tmp_path,
        {"main": {"type": "main", "parentAgentId": None},
         "agent-0": {"type": "sub", "parentAgentId": "main"}},
    )
    found = _resolve_subagents(main_wire)
    assert "main" not in [f["agent_id"] for f in found], found


@pytest.mark.parametrize(
    "main_meta",
    [
        {"type": "main"},                       # 缺 parentAgentId 字段
        {"parentAgentId": None},                # 缺 type 字段
        {"type": "main", "parentAgentId": None},
    ],
    ids=["no-parent-field", "no-type-field", "both"],
)
def test_main_agent_skip_survives_a_missing_field(tmp_path: Path, main_meta: dict) -> None:
    """双判据的意义：任一字段缺失时，另一个仍要挡住主 agent。"""
    main_wire = _write_session_with_agents(
        tmp_path, {"main": main_meta, "agent-0": {"type": "sub", "parentAgentId": "main"}}
    )
    found = _resolve_subagents(main_wire)
    assert [f["agent_id"] for f in found] == ["agent-0"], (main_meta, found)


def test_spawn_fields_without_a_kimi_counterpart_stay_none(tmp_path: Path) -> None:
    """``call_id`` / ``role`` / ``ts`` 在 Kimi 名册里没有对应物 —— 宁可 None 不可假精确。

    消费者无法分辨「真的没有」与「编出来的」；``type`` 是层级（main/sub）不是语义角色，
    塞进 ``role`` 会让跨 host 的 role 统计变成垃圾。
    """
    main_wire = _write_session_with_agents(
        tmp_path,
        {"main": {"type": "main", "parentAgentId": None},
         "agent-0": {"type": "sub", "parentAgentId": "main"}},
    )
    spawn = _resolve_subagents(main_wire)[0]
    assert spawn["call_id"] is None and spawn["role"] is None and spawn["ts"] is None, spawn
    assert spawn["nickname"] == "agent-0"


def test_unreadable_roster_returns_empty_instead_of_raising(tmp_path: Path) -> None:
    """子代理发现是尽力而为的观测，失败不该让整条 capture 链路断掉。"""
    session_dir = tmp_path / "wd_x_y" / "session_z"
    main_wire = _write_wire(session_dir / "agents" / "main" / "wire.jsonl", [METADATA])
    (session_dir / "state.json").write_text("{ not json", encoding="utf-8")
    assert _resolve_subagents(main_wire) == []


def test_wrong_shaped_path_is_not_guessed_at(tmp_path: Path) -> None:
    """路径不是 ``<session>/agents/<id>/wire.jsonl`` 形状时返回空，而不是猜一个会话目录。"""
    stray = _write_wire(tmp_path / "somewhere" / "wire.jsonl", [METADATA])
    assert _resolve_subagents(stray) == []


# ── KIMI-8：subagent_capture 的三处 host 派发 ─────────────────────────────────


def test_subagent_capture_dispatches_all_three_seams_to_kimi() -> None:
    """漏接任一处都会静默退回 Codex 的实现去解析 Kimi 的数据。"""
    result = _run_in_repo_context(
        "import subagent_capture as sc\n"
        "from core.host_adapter import HOST_CLAUDE, HOST_CODEX, HOST_KIMI_CODE_CLI\n"
        "from pathlib import Path\n"
        "print(json.dumps({\n"
        "  'distill': {str(h): sc._host_distill(h).__name__\n"
        "              for h in (HOST_CODEX, HOST_CLAUDE, HOST_KIMI_CODE_CLI, None)},\n"
        "  'originator_kimi': sc._host_originator(HOST_KIMI_CODE_CLI, Path('/nonexistent')),\n"
        "  'subagents_kimi': sc._resolve_subagents(HOST_KIMI_CODE_CLI,\n"
        "      main_rollout_path=Path('/nonexistent/agents/main/wire.jsonl'),\n"
        "      original_transcript=None, sessions_root=None),\n"
        "}))\n"
    )
    assert result["distill"]["kimi_code_cli"] == "distill_kimi_wire_jsonl", result["distill"]
    assert result["distill"]["codex"] == "distill_codex_jsonl", result["distill"]
    assert result["distill"]["claude_code"] == "distill_claude_jsonl", result["distill"]
    assert result["distill"]["None"] == "distill_codex_jsonl", "host 未知仍兜底 Codex"
    assert result["originator_kimi"] is None, "Kimi 无 Codex 式 originator 概念"
    assert result["subagents_kimi"] == [], "路径不存在时返回空而不是抛"


# ── KIMI-9：主轨迹派发（trajectory_capture）──────────────────────────────────
#
# subagent_capture 的三处派发（KIMI-8）只覆盖**子代理**轨迹。主轨迹另有两处独立的两宿主
# if/else —— 蒸馏与 RVF-起点 cut —— 漏接同样是静默的：Codex 的解析器读 Kimi wire 不会抛，
# 只会返回 0 条 / None。


def _kimi_user_message(text: str, time_ms: int) -> dict:
    """``origin.kind == "user"`` 的真实用户输入（``time`` 实测是 **epoch 毫秒整数**）。"""
    return {
        "type": "context.append_message",
        "time": time_ms,
        "message": {
            "role": "user",
            "origin": {"kind": "user"},
            "content": [{"type": "text", "text": text}],
        },
    }


def _kimi_injection(text: str, time_ms: int, variant: str = "todo_list_reminder") -> dict:
    """harness 注入的系统提醒——``role`` 同样是 ``user``，只有 ``origin.kind`` 能区分。"""
    return {
        "type": "context.append_message",
        "time": time_ms,
        "message": {
            "role": "user",
            "origin": {"kind": "injection", "variant": variant},
            "content": [{"type": "text", "text": text}],
        },
    }


def _kimi_assistant_text(text: str, time_ms: int) -> dict:
    return {
        "type": "context.append_loop_event",
        "time": time_ms,
        "event": {"type": "content.part", "part": {"type": "text", "text": text}},
    }


def _kimi_tool_call(name: str, args: dict, call_id: str, time_ms: int) -> dict:
    return {
        "type": "context.append_loop_event",
        "time": time_ms,
        "event": {"type": "tool.call", "name": name, "args": args, "toolCallId": call_id},
    }


def _kimi_tool_result(output: str, call_id: str, time_ms: int) -> dict:
    return {
        "type": "context.append_loop_event",
        "time": time_ms,
        "event": {"type": "tool.result", "toolCallId": call_id, "result": {"output": output}},
    }


def _capture_run(*, run_dir: Path, transcript: Path, session_id: str = "session_abc") -> dict:
    """子进程里跑 ``trajectory_capture.capture_run``，回报 summary + 主轨迹产物计数。"""
    return _run_in_repo_context(
        "from pathlib import Path\n"
        "import trajectory_capture as tc\n"
        f"run_dir = Path({str(run_dir)!r})\n"
        "(run_dir / 'artifacts').mkdir(parents=True, exist_ok=True)\n"
        "summary = tc.capture_run(run_dir=run_dir, event={"
        f"'transcript_path': {str(transcript)!r}, 'session_id': {session_id!r}"
        "})\n"
        "rvf = run_dir / 'artifacts' / 'trajectory' / 'rvf'\n"
        "traj = rvf / 'trajectory.jsonl'\n"
        "raw = traj.read_text(encoding='utf-8') if traj.is_file() else ''\n"
        "lines = [l for l in raw.splitlines() if l.strip()]\n"
        "print(json.dumps({\n"
        "  'summary': summary,\n"
        "  'trajectory_record_count': len(lines),\n"
        "  'trajectory_sources': sorted({json.loads(l).get('source') for l in lines}),\n"
        "  'post_manifest': json.loads((rvf / 'rollout.manifest.json').read_text()),\n"
        "  'post_rollout_line_count': sum(\n"
        "      1 for l in (rvf / 'rollout.jsonl').read_text(encoding='utf-8').splitlines()\n"
        "      if l.strip()) if (rvf / 'rollout.jsonl').is_file() else 0,\n"
        "}, ensure_ascii=False))\n"
    )


def test_kimi_main_trajectory_is_distilled_by_the_kimi_distiller(tmp_path: Path) -> None:
    """Codex distiller 读 Kimi wire 不会抛——只会静默蒸出 **0 条**，无任何 gate 会报警。"""
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [
            METADATA,
            _kimi_user_message("帮我改这个文件", 1787371449620),
            _kimi_assistant_text("好的，我先读一下", 1787371450000),
            _kimi_tool_call("Edit", {"path": "a/b.py"}, "tc-1", 1787371451000),
            _kimi_tool_result("ok", "tc-1", 1787371452000),
        ],
    )
    result = _capture_run(run_dir=tmp_path / "rvf-run", transcript=wire)
    assert result["post_manifest"]["host"] == "kimi_code_cli", result["post_manifest"]
    assert result["trajectory_record_count"] > 0, (
        "Kimi 会话的主 trajectory 被蒸成 0 条 —— 正是本切片要杜绝的静默失声"
    )
    assert result["trajectory_sources"] == ["kimi_code_cli"], result["trajectory_sources"]


def test_kimi_rvf_start_marker_is_found_in_a_wire_transcript(tmp_path: Path) -> None:
    """cut 找不到 ⇒ 整份会话全归 post、pre 为空，同样是静默降级。"""
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [
            METADATA,
            _kimi_user_message("先做点别的", 1787371449000),
            _kimi_assistant_text("好", 1787371449100),
            _kimi_user_message("$review-validate-fix 请审", 1787371450000),
            _kimi_assistant_text("开始 review", 1787371450100),
        ],
    )
    result = _capture_run(run_dir=tmp_path / "rvf-run", transcript=wire)
    assert result["summary"]["post_rvf_source_kind"] == "same-session-slice", result["summary"]
    assert result["summary"]["pre_rvf_source_kind"] == "same-session-slice", result["summary"]


def test_kimi_rvf_start_marker_survives_the_since_timestamp_filter(tmp_path: Path) -> None:
    """``time`` 是 **epoch 毫秒整数**，不是 ISO 串。

    照抄另两家的 ``record.get("timestamp")``（要求 ``str``）会让每条候选都被
    ``since_timestamp`` 过滤掉 ⇒ cut 永远找不到，**且不报错**。同一会话内连着跑两次 RVF 时
    ``since_timestamp`` 必然非空，所以这条路径是常态而不是边角。
    """
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [
            METADATA,
            _kimi_user_message("$review-validate-fix 第一次", 1787371449000),
            _kimi_assistant_text("第一次 review", 1787371449100),
            _kimi_user_message("$review-validate-fix 第二次", 1787371460000),
            _kimi_assistant_text("第二次 review", 1787371460100),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    # prepare 时刻落在两次触发之间（1787371449000 = 04:04:09Z，1787371460000 = 04:04:20Z）
    # ⇒ 只有第二次触发够格当本 run 的 cut。
    (run_dir / "summary.json").write_text(
        json.dumps({"timestamp": "2026-08-22T04:04:15Z"}), encoding="utf-8"
    )
    result = _capture_run(run_dir=run_dir, transcript=wire)
    assert result["summary"]["post_rvf_source_kind"] == "same-session-slice", result["summary"]
    cut = result["post_manifest"]["cut"]
    assert cut["marker_matched"] == "$review-validate-fix", cut
    # 第二次触发之后只剩 2 条 record（触发行 + 回复行）⇒ post 必须正好是这两行；
    # 若 since_timestamp 把第二次也滤掉、cut 落回第一次，这里会是 4。
    assert result["post_rollout_line_count"] == 2, result


def test_every_known_host_identity_has_its_own_main_trajectory_dispatch() -> None:
    """结构性 gate：加第四个 host 却忘了补分支时当场炸，而不是静默落进 Codex 兜底。"""
    result = _run_in_repo_context(
        "import trajectory_capture as tc\n"
        "from core.host_adapter import KNOWN_HOST_IDENTITIES\n"
        "print(json.dumps({\n"
        "  'distill': {h: tc._host_distill(h).__name__ for h in sorted(KNOWN_HOST_IDENTITIES)},\n"
        "  'find_start': {h: tc._host_find_rvf_start(h).__name__\n"
        "                 for h in sorted(KNOWN_HOST_IDENTITIES)},\n"
        "  'distill_unknown': tc._host_distill(None).__name__,\n"
        "  'find_start_unknown': tc._host_find_rvf_start(None).__name__,\n"
        "}))\n"
    )
    assert len(set(result["distill"].values())) == len(result["distill"]), result["distill"]
    assert len(set(result["find_start"].values())) == len(result["find_start"]), result["find_start"]
    assert result["distill"]["kimi_code_cli"] == "distill_kimi_wire_jsonl", result["distill"]
    assert result["find_start"]["kimi_code_cli"] == "find_rvf_start_in_kimi_wire_jsonl", (
        result["find_start"]
    )
    assert result["distill_unknown"] == "distill_codex_jsonl", "host 未知仍兜底 Codex"
    assert result["find_start_unknown"] == "find_rvf_start_in_jsonl", "host 未知仍兜底 Codex"


# ── KIMI-10：父会话 context 渲染（rvf_parent_context）────────────────────────


def _render_lines(wire: Path) -> list[str]:
    return _run_in_repo_context(
        "from pathlib import Path\n"
        "import rvf_parent_context as rpc\n"
        f"print(json.dumps(rpc.render_lines(Path({str(wire)!r})), ensure_ascii=False))\n"
    )


def test_kimi_parent_context_renders_instead_of_going_silently_empty(tmp_path: Path) -> None:
    """Codex 渲染器读 Kimi wire 只会逐条返回 None ⇒ context blob 静默变成空。"""
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [
            METADATA,
            _kimi_user_message("帮我改这个文件", 1787371449620),
            _kimi_assistant_text("好的", 1787371450000),
            _kimi_tool_call("Edit", {"path": "a/b.py"}, "tc-1", 1787371451000),
            _kimi_tool_result("done", "tc-1", 1787371452000),
        ],
    )
    lines = _render_lines(wire)
    assert lines, "Kimi 父会话 context 渲染成空行 —— 已实测复现"
    assert "user: 帮我改这个文件" in lines, lines
    assert "assistant: 好的" in lines, lines
    assert any(line.startswith("call Edit(") for line in lines), lines
    assert "result: done" in lines, lines


def test_kimi_parent_context_keeps_reasoning_and_drops_harness_injections(tmp_path: Path) -> None:
    """``role`` 全是 ``user``，只有 ``origin.kind`` 能区分真实输入与系统提醒。

    把注入当成用户发言会污染 child 读到的「用户说了什么」，与 Codex/Claude 渲染器丢弃
    system/developer boilerplate 同口径。
    """
    wire = _write_wire(
        tmp_path / "s" / "agents" / "main" / "wire.jsonl",
        [
            METADATA,
            _kimi_injection("<system-reminder>你的 todo 还没更新</system-reminder>", 1787371449000),
            _kimi_user_message("真正的用户输入", 1787371449620),
            {
                "type": "context.append_loop_event",
                "time": 1787371450000,
                "event": {"type": "content.part", "part": {"type": "think", "think": "先读文件"}},
            },
        ],
    )
    lines = _render_lines(wire)
    assert "user: 真正的用户输入" in lines, lines
    assert "reasoning: 先读文件" in lines, "Kimi 的 think 是明文，应当像 Claude 一样保留"
    assert not any("system-reminder" in line for line in lines), lines


def test_every_known_host_identity_has_its_own_parent_context_renderer() -> None:
    """结构性 gate：渲染器分派同样必须逐 host 各有其人。"""
    result = _run_in_repo_context(
        "import rvf_parent_context as rpc\n"
        "from core.host_adapter import KNOWN_HOST_IDENTITIES\n"
        "print(json.dumps({\n"
        "  'renderer': {h: rpc._host_record_renderer(h).__name__\n"
        "               for h in sorted(KNOWN_HOST_IDENTITIES)},\n"
        "  'unknown': rpc._host_record_renderer(None).__name__,\n"
        "}))\n"
    )
    assert len(set(result["renderer"].values())) == len(result["renderer"]), result["renderer"]
    assert result["renderer"]["kimi_code_cli"] == "_render_kimi_record", result["renderer"]
    assert result["unknown"] == "_render_codex_record", "host 未知仍兜底 Codex"


# ── KIMI-11：harness 身份 token 必须逐层收敛 ─────────────────────────────────


def test_kimi_harness_identity_token_converges_across_every_signal_layer() -> None:
    """``round_baseline_marker`` 的 key 里带 harness token；两层归一成不同串 = marker 消失。

    Claude 的三种写法（``claude_code`` / ``claude`` / ``claude-code_2-1-231_agent``）都折进
    ``claude-code``。Kimi 曾经没有这条：host 探测值 ``kimi_code_cli`` 折成 ``kimi-code-cli``、
    hook shim 注入的 agent id ``kimi`` 折成 ``kimi``、kanban 的 ``AI_AGENT=kimi_…_agent`` 又折成
    第三个串 —— 写者写 A、读者读 B，窗口下界凭空消失（⇒ 漏审），且**没有任何报错**。
    """
    result = _run_in_repo_context(
        "from core.host_adapter.parent_harness_identity import (\n"
        "  normalize_parent_harness_identity as n, PARENT_HARNESS_IDENTITY_KIMI_CODE_CLI as K)\n"
        "print(json.dumps({\n"
        "  'constant': K,\n"
        "  'from_host_kind': n('kimi_code_cli'),\n"
        "  'from_kanban_agent_id': n('kimi'),\n"
        "  'from_ai_agent_descriptor': n('kimi_1-0-0_agent'),\n"
        "  'claude_unchanged': [n('claude_code'), n('claude'), n('claude-code_2-1-231_agent')],\n"
        "  'codex_unchanged': [n('codex'), n('codex-cli')],\n"
        "  'cursor_unchanged': n('cursor-agent'),\n"
        "}))\n"
    )
    assert result["constant"] == "kimi-code-cli", result
    assert (
        result["from_host_kind"]
        == result["from_kanban_agent_id"]
        == result["from_ai_agent_descriptor"]
        == result["constant"]
    ), result
    # 对偶回归闸：既有三家的归一结果一个字符都不许动。
    assert result["claude_unchanged"] == ["claude-code"] * 3, result
    assert result["codex_unchanged"] == ["codex-cli"] * 2, result
    assert result["cursor_unchanged"] == "cursor-cli", result


# ── KIMI-12：父会话来源标签的 host 短前缀 ────────────────────────────────────


def test_session_reference_label_prefix_is_not_two_host_binary() -> None:
    """Kimi 会话的来源标签此前被渲染成 ``Codex <session-ref>``。

    只影响人类可读标签、不选线材，但它同样是一处「Claude else Codex」的两宿主二分，
    留着就是下一次「新 host 静默穿成 Codex」的模板。
    """
    result = _run_in_repo_context(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        "path = Path(sys.path[0]) / 'stop_review_validate_fix.py'\n"
        "spec = importlib.util.spec_from_file_location('rvf_stop_for_prefix_gate', path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['rvf_stop_for_prefix_gate'] = module\n"
        "spec.loader.exec_module(module)\n"
        "from core.host_adapter import KNOWN_HOST_IDENTITIES\n"
        "table = module.SESSION_REFERENCE_LABEL_PREFIX_BY_HOST\n"
        "print(json.dumps({\n"
        "  'table': table,\n"
        "  'missing_hosts': sorted(set(KNOWN_HOST_IDENTITIES) - set(table)),\n"
        "  'unknown_fallback': table.get(None, 'Codex'),\n"
        "}))\n"
    )
    assert result["missing_hosts"] == [], result
    assert result["table"]["kimi_code_cli"] == "Kimi", result["table"]
    assert len(set(result["table"].values())) == len(result["table"]), result["table"]
    assert result["unknown_fallback"] == "Codex", "host 未知仍兜底 Codex"


def test_subagent_capture_cli_accepts_every_known_host_identity(tmp_path: Path) -> None:
    """库侧三处派发认识 Kimi，CLI 却把它挡在 argparse ``choices`` 外，是同一处遗漏的尾巴。

    行为断言而不是扫字面量：argparse 拒绝非法 choice 时以 **2** 退出并在 stderr 写
    ``invalid choice``，这里逐 host 真跑一次 CLI。
    """
    main_wire = _write_wire(tmp_path / "s" / "agents" / "main" / "wire.jsonl", [METADATA])
    script = (
        ROOT
        / "plugins"
        / "review-validate-fix"
        / "skills"
        / "review-validate-fix"
        / "scripts"
        / "subagent_capture.py"
    )
    known_hosts = _run_in_repo_context(
        "from core.host_adapter import KNOWN_HOST_IDENTITIES\n"
        "print(json.dumps(sorted(KNOWN_HOST_IDENTITIES)))\n"
    )
    assert "kimi_code_cli" in known_hosts, known_hosts

    for host in known_hosts:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--main-rollout",
                str(main_wire),
                "--out-dir",
                str(tmp_path / "out" / host),
                "--host",
                host,
            ],
            capture_output=True,
            text=True,
        )
        assert "invalid choice" not in completed.stderr, (host, completed.stderr)
        assert completed.returncode == 0, (host, completed.returncode, completed.stderr)
