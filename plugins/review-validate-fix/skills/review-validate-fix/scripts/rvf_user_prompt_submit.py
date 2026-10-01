#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kanban_followup_lock
import rvf_bootstrap_gate
import rvf_prep_file
import rvf_withdrawn_dispatch_tombstone
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from kanban_followup_trigger_transcript_presence import has_explicit_human_prompt_after
from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import (  # noqa: E402
    DISPATCH_PREP_REVIVE_MAX_AGE_ENV,
    DISPATCH_PREP_REVIVE_MAX_AGE_SECONDS,
)
from session_label import text_from_message_payload

try:
    # 纯文本 handoff 识别复用：`RVF_RUN_RE`（run id 谓词）+ `parse_sections`
    # （markdown 章节切分，无 IO）。仅用这两个无副作用函数判定「粘贴的是 handoff
    # 正文」，**绝不**调 `build_payload`（它可能跑 git subprocess）。顶层 import
    # 只引 stdlib，无 IO，可安全用于 UPS 检测热路径。缺省时降级为不做 handoff 识别。
    import rvf_handoff_intake
except Exception:  # pragma: no cover - handoff 识别可缺省降级
    rvf_handoff_intake = None

try:
    # Vendored single-file copy (see its header). Gives a structured "which skill
    # did the user explicitly invoke" read from the Codex rollout — catches forms
    # the anchored regex misses (notably the namespaced `$rvf:review-validate-fix`).
    import codex_invoked_skill
except Exception:  # pragma: no cover - stay resilient if the vendor is absent
    codex_invoked_skill = None


DISPATCH_TOKEN_RE = re.compile(r"\bRVF_DISPATCH=token=([0-9A-Fa-f]{16})\b")
# 发起决策点横幅的 needle。横幅经 cline-kanban 终端投递成为一条**真实 user message** 时，本
# hook 会 fire 并在 prompt 正文里读到它——**这就是 INV-A 的达成信号**：一条真实 user prompt
# 必然触发 UPS，所以 RVF 自己就能观测到「横幅确实成了 user message」，比查任何外部 ref 都可靠。
# 与 ``kanban_followup_trigger_transcript_presence.decision_token_literal`` 同源。
DECISION_POINT_TOKEN_RE = re.compile(r"\bRVF_DECISION_TOKEN=([0-9A-Fa-f]{16})\b")
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
CLINE_KANBAN_TASK_MARKER = "RVF_CLINE_KANBAN_TASK"
KANBAN_FOLLOWUP_MARKER = "RVF_KANBAN_FOLLOWUP_TRIGGER"
DISPATCH_ORIGIN_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fork", re.compile(rf"\b{re.escape(RVF_FORK_MARKER)}\b")),
    ("kanban-task", re.compile(rf"\b{re.escape(CLINE_KANBAN_TASK_MARKER)}\b")),
    ("kanban-followup", re.compile(rf"\b{re.escape(KANBAN_FOLLOWUP_MARKER)}\b")),
)
RVF_MANUAL_TRIGGERS = ("$review-validate-fix", "/review-validate-fix", ":review-validate-fix")
# Match a manual trigger only at line start or after whitespace, and only
# when the trailing token boundary is clean (\b). This avoids accidentally
# triggering on quoted/embedded literals that appear inside review packets,
# transcript excerpts, error stacks, or normal prose like
# "please document the /review-validate-fix tool".
# 负向先行断言 `(?!:rvf-)`：在检测层就区分主 skill 与 `rvf-*` 姊妹子 skill。所有
# 姊妹 skill 的命名空间形态都是 `…:rvf-<name>`（如 `/review-validate-fix:rvf-land`、
# `/review-validate-fix:rvf-local-deploy`），调用它们不应启动新 review；而主 skill
# 的裸形态 `/review-validate-fix` 与命名空间形态
# `/review-validate-fix:review-validate-fix`（后缀是 `:review-` 而非 `:rvf-`）仍命中。
# 本断言位置无关，根因级修复「子 skill 出现在 prompt 句中时误触发 manual review」。
RVF_MANUAL_TRIGGER_RE = re.compile(
    r"(?:^|\s)[\$/:]review-validate-fix(?!:rvf-)\b",
    re.MULTILINE,
)
# 前导姊妹命令抑制（位置锚定 prompt 开头，容忍前导空白）。`rvf-land` /
# `rvf-handoff-intake` / `rvf-reopen` / `rvf-analyze` 等姊妹 skill 明确「不启动新
# review」，却常把含 `review-validate-fix` 字面量的 handoff 正文作为参数粘贴进来。
# 主 skill 名是 `review-validate-fix`（不以 `rvf-` 开头），故本正则永不误吞真触发。
# 容忍命名空间前缀 `rvf:` / `review-validate-fix:`。
RVF_SIBLING_TRIGGER_RE = re.compile(
    r"\s*[\$/:](?:rvf:|review-validate-fix:)?rvf-[a-z0-9][a-z0-9-]*\b",
)
# handoff/finalization 正文里高度独有的 markdown 章节标题（归一化为小写后做子串
# 匹配）。与 RVF run id 共现时判定为「粘贴的 handoff 正文」。这些短语在「普通要求
# 跑一次 review」的 prompt 里几乎不会作为章节标题出现，叠加 run id 共现可避免误伤
# 只顺嘴提了个 run id 的合法 review 请求。
RVF_HANDOFF_SECTION_MARKERS = (
    "handoff intake hints",
    "deterministic intake hints",
    "validate/fix",
    "处理结果",
    "repo delta",
    "继续指引",
    "升级事项",
    "本次 review",
)
# manual 触发可内联指定 review scope：`/review-validate-fix scope: a.py b.py`。
# 取首个 `scope:`（行首或空白前缀，避免命中 `telescope:` 之类）之后直到行尾的
# 内容作为 primary 文件清单。大小写不敏感；建议把 `scope:` 放在该行末尾。
RVF_MANUAL_SCOPE_RE = re.compile(
    r"(?:^|\s)scope:\s*(.+)",
    re.IGNORECASE | re.MULTILINE,
)

# 仅供本进程 main() 在 stdout flush 成功后兑现投递回执；_render_hook_payload 不会
# 把该内部键暴露给 host。调试 --json 也会丢弃它且不推进 token（未实际注入）。
_RVF_PENDING_MANUAL_INJECTION_RECEIPT = "_rvf_pending_manual_injection_receipt"

# 同理（RVF-003）：带外决策点首个到达只在结果里登记「待发布」，由 main() 在 hook 输出 print+flush 成功后
# 才发布「首次注入已交付完毕」凭证；_render_hook_payload 不暴露它，调试 --json 丢弃它且不发布。
_RVF_PENDING_DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL = (
    "_rvf_pending_decision_point_banner_first_injection_handoff_completed_credential"
)


def _latest_user_message_from_transcript(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue
                if record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)
                        if text:
                            latest = text
    except OSError:
        return None
    return latest


def prompt_text_from_event(event: dict[str, Any]) -> tuple[str | None, str]:
    prompt = event.get("prompt")
    if isinstance(prompt, str):
        return prompt, "prompt"
    direct = event.get("last_user_message")
    if isinstance(direct, str):
        return direct, "last_user_message"
    for key in ("transcript_path", "conversation_path", "session_path"):
        value = event.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        message = _latest_user_message_from_transcript(Path(value).expanduser())
        if message:
            return message, key
    return None, "missing"


def dispatch_token_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = DISPATCH_TOKEN_RE.search(text)
    if match is None:
        return None
    return match.group(1).lower()


def decision_point_token_from_text(text: str | None) -> str | None:
    """从 prompt 正文里取出发起决策点 token 的字面量（没有则 None）。

    只是子串搜索：命中**不**说明这条 prompt 是边界标记落地，开发者 prompt 里也会引用它。判「标记落地」
    一律走 :func:`decision_point_token_when_prompt_is_exactly_one_out_of_band_boundary_marker`。
    """
    if not text:
        return None
    match = DECISION_POINT_TOKEN_RE.search(text)
    if match is None:
        return None
    return match.group(1).lower()


def decision_point_token_when_prompt_is_exactly_one_out_of_band_boundary_marker(
    event: dict[str, Any], prompt: str | None
) -> str | None:
    """这条 prompt **整条**就是一行带外边界标记（容许 host 的一层粘贴包裹）时返回它的 token，否则 None。

    只有这种独立消息才是机器投递 / rewind 重交送来的标记，才有资格进作废出口（block）、复工撤回、M10
    rewind 重交判定，以及 E1 到达记录里「本 token 自己的标记」那一类。开发者 prompt 里引用标记（粘日志 /
    handoff / 旧横幅，同一条里再附上自己的话）一律按开发者 prompt 处理（RVF-001）：子串口径会让作废出口
    把整条开发者正文 block 掉，或把一份有效征询当成 rewind 重交撤回。

    host 粘贴包裹经当前会话的 HostAdapter 剥（Claude Code 把 cline-kanban 的终端粘贴包成
    ``<pasted_content id=…>``，UPS 收到的 ``prompt`` 就是包好的形态；core 不写 host 字面量）。任何一步失败
    都按「不是标记」处理：漏认一条机器投递的标记只是正文没注入，交给 Stop 侧既有对账（补发 / 搁浅撤回）；
    反方向误认则会丢掉开发者的话。
    """
    if decision_point_token_from_text(prompt) is None:
        return None  # 快路径：连字面量都没有的 prompt 不必装配 HostAdapter
    try:
        import rvf_decision_token_store as decision_token_store  # noqa: PLC0415 - lazy, stdlib-only
    except Exception:  # noqa: BLE001
        return None
    candidate_texts: list[str | None] = [prompt]
    try:
        from host_adapter_resolver import resolve_current_session_host_adapter  # noqa: PLC0415

        candidate_texts.append(
            resolve_current_session_host_adapter(
                event
            ).user_prompt_text_inside_host_paste_envelope_when_whole_prompt_is_one_paste(event, prompt or "")
        )
    except Exception:  # noqa: BLE001 - 剥不了包裹只是少认一种形态
        pass
    for candidate_text in candidate_texts:
        token = decision_token_store.decision_token_of_text_that_is_exactly_one_out_of_band_boundary_marker(
            candidate_text
        )
        if token is not None:
            return token
    return None


def detect_origin_marker(text: str) -> str | None:
    for name, pattern in DISPATCH_ORIGIN_MARKERS:
        if pattern.search(text):
            return name
    return None


def detect_manual_trigger(text: str) -> bool:
    return bool(RVF_MANUAL_TRIGGER_RE.search(text))


def _leading_sibling_command(text: str | None) -> bool:
    """prompt 开头是否为 RVF 姊妹命令（``/rvf-land`` 等，位置锚定、纯文本、harness 无关）。"""
    if not text:
        return False
    return RVF_SIBLING_TRIGGER_RE.match(text) is not None


def _looks_like_handoff_body(text: str | None) -> bool:
    """prompt 是否「看起来是粘贴的 RVF handoff/finalization 正文」（位置无关，纯文本）。

    判据：含 RVF run id（``rvf_handoff_intake.RVF_RUN_RE``）**且** 含至少一个 handoff
    独有的 markdown 章节标题（经 ``parse_sections`` 切出的 heading 命中
    :data:`RVF_HANDOFF_SECTION_MARKERS`）。两条件叠加，避免误伤只顺嘴提了个 run id 的
    合法 review 请求。只用 stdlib-only、无 IO 的两个函数；``rvf_handoff_intake`` 缺省时
    直接返回 False（降级为不做识别）。
    """
    if rvf_handoff_intake is None or not text:
        return False
    try:
        if not rvf_handoff_intake.RVF_RUN_RE.search(text):
            return False
        sections = rvf_handoff_intake.parse_sections(text)
    except Exception:  # pragma: no cover - 识别失败永不阻断，按「非 handoff」处理
        return False
    for heading in sections:
        normalized = heading.strip().lower()
        if any(marker in normalized for marker in RVF_HANDOFF_SECTION_MARKERS):
            return True
    return False


def _codex_sibling_skill_invoked(event: dict[str, Any]) -> bool:
    """Codex 结构化加成：本轮显式调用的是 RVF 姊妹 skill（``rvf-*``）而非主 skill。

    仅 Codex rollout 有 ``text_elements``；Claude / 缺 transcript 时返回 False。
    best-effort：结构化读取异常绝不阻断。
    """
    if codex_invoked_skill is None:
        return False
    try:
        skills = codex_invoked_skill.invoked_skills_from_event(event)
    except Exception:  # pragma: no cover - 结构化读取永不阻断
        return False
    for skill in skills:
        name = getattr(skill, "name", "") or ""
        if name.startswith("rvf-"):
            return True
    return False


def _manual_trigger_suppressed(event: dict[str, Any], prompt: str) -> bool:
    """触发字面量在场，但应被抑制（识别为 handoff 正文 / 姊妹命令参数）。

    三条抑制信号任一命中即抑制，全部位置无关（不依赖字面量在行首）：
    1. 前导姊妹命令（``/rvf-land`` 等，纯文本）。
    2. 粘贴的 handoff 正文（run id + handoff 章节标题，纯文本，捕获无前导命令的形态）。
    3. Codex 结构化显式调用了姊妹 skill（仅 Codex，加成）。
    """
    if _leading_sibling_command(prompt):
        return True
    if _looks_like_handoff_body(prompt):
        return True
    if _codex_sibling_skill_invoked(event):
        return True
    return False


def _classify_manual_trigger(event: dict[str, Any], prompt: str) -> str:
    """判定本轮是否手动触发 review-validate-fix：``"manual"`` / ``"suppressed"`` / ``"none"``。

    结构化优先（Codex）：经 vendored ``codex_invoked_skill`` 从 rollout transcript 的
    ``user_message.text_elements`` 读取显式 ``$skill`` 调用——这能命中锚定正则漏掉的
    命名空间形态 ``$rvf:review-validate-fix``（``:review-validate-fix`` 前缀非词边界，
    旧正则 MISS）；显式调用主 skill 本身即真 manual，最高优先。否则回退
    :func:`detect_manual_trigger`（位置无关，保留以应对「输入框残留前缀把合法触发顶离行首」
    的假阴）：无字面量 → ``"none"``；有字面量但被 :func:`_manual_trigger_suppressed`
    识别为 handoff 正文 / 姊妹命令参数 → ``"suppressed"``；否则 → ``"manual"``。
    best-effort：结构化读取异常绝不阻断，直接回退文本判定。
    """
    if codex_invoked_skill is not None:
        try:
            if codex_invoked_skill.was_skill_invoked(event, "review-validate-fix"):
                return "manual"
        except Exception:  # pragma: no cover - 结构化读取永不阻断
            pass
    if not detect_manual_trigger(prompt):
        return "none"
    if _manual_trigger_suppressed(event, prompt):
        return "suppressed"
    return "manual"


def _review_validate_fix_manually_invoked(event: dict[str, Any], prompt: str) -> bool:
    """:func:`_classify_manual_trigger` 的 bool 投影（兼容既有调用方 / 测试）。"""
    return _classify_manual_trigger(event, prompt) == "manual"


def parse_manual_scope_directive(prompt: str | None) -> list[str]:
    """从 manual 触发串里解析内联 ``scope:`` 指令。

    语法：``/review-validate-fix scope: a.py b.py``——取首个 ``scope:`` 之后直到
    行尾的内容，按空白 / 逗号切分成 primary 文件清单，并去掉包裹引号。路径
    规范化（去 ``./`` 前缀、反斜杠归一、去重排序）交给下游 ``prepare_run`` 的
    ``normalized_scope_list``，本函数只负责切分。无 ``scope:`` 时返回空列表。

    注意：``scope:`` 取到行尾，故该行 ``scope:`` 之后的普通文字也会被当成文件；
    约定把 ``scope:`` 放在行末（或单独成行）。
    """
    if not prompt:
        return []
    match = RVF_MANUAL_SCOPE_RE.search(prompt)
    if match is None:
        return []
    tokens = (
        token.strip().strip("'\"")
        for token in re.split(r"[,\s]+", match.group(1).strip())
    )
    return [token for token in tokens if token]


def _resolve_cwd(event: dict[str, Any]) -> tuple[str, bool]:
    raw = event.get("cwd")
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), False
    return str(Path.cwd()), True


def _git_resolved_repo(cwd: str) -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            top = result.stdout.strip()
            if top:
                return top
    except (FileNotFoundError, OSError):
        return None
    return None


def _git_head_oid(repo: str) -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            head = result.stdout.strip()
            if head:
                return head
    except (FileNotFoundError, OSError):
        return None
    return None


def _extend_round_baseline_expiry_for_host_synthesized_turn(event: dict[str, Any]) -> str | None:
    """host 合成的回合：只续期本会话**活跃**的 round baseline marker，不前移 head（理由见
    ``round_baseline_marker.extend_round_baseline_marker_expiry_without_moving_baseline_head``）。

    键解析与 :func:`_capture_round_baseline` 同口径（task_id 优先 / session_id 回退）。best-effort：
    任何失败都返回 None，绝不阻断这条 prompt。
    """
    try:
        from core.session_scope_allocation import round_baseline_marker  # noqa: PLC0415 - lazy

        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        repo = _git_resolved_repo(cwd) or cwd
        try:
            from rvf_analyze_advisory import current_kanban_task_id  # noqa: PLC0415

            task_id = current_kanban_task_id(event)
        except Exception:
            task_id = None
        raw_session = event.get("session_id")
        session_id = raw_session.strip() if isinstance(raw_session, str) and raw_session.strip() else None
        extended_path = (
            round_baseline_marker.extend_round_baseline_marker_expiry_without_moving_baseline_head(
                task_id=task_id, session_id=session_id, repo=repo
            )
        )
        return str(extended_path) if extended_path is not None else None
    except Exception:
        return None


def _mark_new_turn_observed_for_deferred_until_condition_declines(event: dict[str, Any]) -> bool:
    """给本会话所有延后型谢绝记录打「出现了新回合」标志（闸 2c 据此放行重新征询）。

    会话身份只取 ``event["session_id"]``（与 snooze 基线重置同口径）；best-effort，失败永不阻断 prompt
    ——少打一次标志只让重新征询晚一个回合。返回是否真的打了至少一条。
    """
    try:
        import rvf_decision_token_store  # noqa: PLC0415 - lazy, stdlib-only

        raw_session_id = event.get("session_id")
        if not (isinstance(raw_session_id, str) and raw_session_id.strip()):
            return False
        return (
            rvf_decision_token_store.mark_new_turn_observed_for_deferred_until_condition_declines_in_session(
                session_id=raw_session_id.strip()
            )
            > 0
        )
    except Exception:
        return False


def _host_synthesized_user_prompt_submit_turn_kind(event: dict[str, Any], prompt: str) -> str | None:
    """这条 UPS 是否 host 自己合成的回合（经**当前会话**的 HostAdapter 判定；core 不写 host 字面量）。

    判定失败一律按「开发者 prompt」处理（返回 None）＝改动前的行为：误判成合成会吞掉一条真实的
    开发者 prompt（手动触发失效、round baseline 不前移），代价远比少排除一条通知大。
    """
    try:
        from host_adapter_resolver import resolve_current_session_host_adapter  # noqa: PLC0415

        host_adapter = resolve_current_session_host_adapter(event)
        turn_kind = host_adapter.host_synthesized_user_prompt_submit_turn_kind(event, prompt)
    except Exception:  # noqa: BLE001 - 判定失败退回改动前的行为
        return None
    return turn_kind if isinstance(turn_kind, str) and turn_kind else None


def _agent_turn_identifier_from_hook_event(event: dict[str, Any]) -> str | None:
    """这条 UPS 所属 agent 回合的标识（经当前会话的 HostAdapter 取；取不到为 None = 无此证据）。"""
    try:
        from host_adapter_resolver import resolve_current_session_host_adapter  # noqa: PLC0415

        identifier = resolve_current_session_host_adapter(
            event
        ).agent_turn_identifier_shared_by_hook_events_of_one_turn(event)
    except Exception:  # noqa: BLE001 - 取不到只是少一份证据
        return None
    return identifier if isinstance(identifier, str) and identifier else None


def _record_user_prompt_submit_arrival_for_decision_point_agent_resumed_work_evidence(
    event: dict[str, Any],
    prompt: str,
    *,
    host_synthesized_turn_kind: str | None,
    decision_point_token_when_prompt_is_exactly_one_out_of_band_boundary_marker: str | None,
) -> list[dict[str, Any]]:
    """把这次 UPS 到达记进会话的到达记录（决策点「铸 token 之后 agent 是否复工」的证据 E1），
    返回**本次之前**的到达记录。

    必须早于合成回合的短路：后台任务完成通知恰恰是最主要的复工信号。种类只分判据需要的那几类；边界标记
    记下它带的 token，「到达时那枚 token 是否已作废（会被挡下、不开回合）」由判据侧按 token 历史判定——
    这里刻意**不读** token：UPS 热路径上多一次锁外读，会改变首次到达那段锁内竞态的形状。best-effort：
    任何失败返回空列表，绝不阻断 prompt。

    「是不是边界标记」用调用方按**整条独立标记**判出的 token（RVF-001）：开发者 prompt 里引用某枚 token 的
    标记是一次真实的开发者发话，必须记成 ``developer_prompt``——记成那枚 token 的标记会被判据当作「本 token
    自己的标记」跳过，复工证据就丢了。
    """
    try:
        import decision_point_agent_resumed_work_after_token_mint_evidence as resumed_work_evidence  # noqa: PLC0415

        decision_token: str | None = None
        if host_synthesized_turn_kind is not None:
            arrival_kind = (
                resumed_work_evidence.USER_PROMPT_SUBMIT_ARRIVAL_KIND_HOST_SYNTHESIZED_TURN_PREFIX
                + host_synthesized_turn_kind
            )
        elif dispatch_token_from_text(prompt) is not None or detect_origin_marker(prompt) is not None:
            arrival_kind = resumed_work_evidence.USER_PROMPT_SUBMIT_ARRIVAL_KIND_RVF_MACHINE_PROMPT
        else:
            decision_token = decision_point_token_when_prompt_is_exactly_one_out_of_band_boundary_marker
            arrival_kind = (
                resumed_work_evidence.USER_PROMPT_SUBMIT_ARRIVAL_KIND_DEVELOPER_PROMPT
                if decision_token is None
                else resumed_work_evidence.USER_PROMPT_SUBMIT_ARRIVAL_KIND_DECISION_POINT_BOUNDARY_MARKER
            )
        session_id = event.get("session_id")
        return resumed_work_evidence.record_user_prompt_submit_arrival_and_return_previous_arrivals(
            session_id=session_id if isinstance(session_id, str) else None,
            user_prompt_submit_arrival_kind=arrival_kind,
            decision_token=decision_token,
            agent_turn_identifier=_agent_turn_identifier_from_hook_event(event),
        )
    except Exception:  # noqa: BLE001
        return []


def _capture_round_baseline(event: dict[str, Any], prompt: str | None) -> dict[str, Any] | None:
    """Record HEAD at this genuine user prompt as the lower bound for the next
    RVF round's committed-change detection (see ``round_baseline_marker``).

    Best-effort and strictly non-blocking: any failure (no cwd, not a git repo,
    detached/empty HEAD, IO error) returns None and the next Stop simply falls
    back to dirty-only behaviour. Keyed task_id-first / session_id-fallback to
    mirror how the Stop hook reads it. Only called for genuine user prompts —
    RVF's own injected dispatch-token / origin-marker prompts are not round
    boundaries and must not move the baseline.
    """
    try:
        from core.session_scope_allocation import round_baseline_marker  # noqa: PLC0415 - lazy, off the hot import path

        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        repo = _git_resolved_repo(cwd) or cwd
        head = _git_head_oid(repo)
        if not head:
            return None
        try:
            from rvf_analyze_advisory import current_kanban_task_id  # noqa: PLC0415

            task_id = current_kanban_task_id(event)
        except Exception:
            task_id = None
        raw_session = event.get("session_id")
        session_id = raw_session.strip() if isinstance(raw_session, str) and raw_session.strip() else None
        marker_path = round_baseline_marker.write_round_baseline_marker(
            task_id=task_id,
            session_id=session_id,
            baseline_head=head,
            repo=repo,
            prompt_excerpt=prompt or "",
        )
        if marker_path is None:
            return None
        return {"baseline_head": head, "marker_path": str(marker_path)}
    except Exception:
        return None


def _run_shared_workflow(
    *,
    record: rvf_prep_file.PrepFileRecord,
    user_prompt_excerpt: str | None,
    timeout_seconds: float,
    extra_primary_files: list[str] | None = None,
) -> dict[str, Any]:
    """Import prepare_review_run lazily to avoid pulling reviewable_unit_diff_tracker on early-exit paths."""
    import prepare_review_run  # noqa: PLC0415 - intentional lazy import

    return prepare_review_run.prepare_run_from_prep_file(
        record,
        timeout_seconds=timeout_seconds,
        user_prompt_excerpt=user_prompt_excerpt,
        extra_primary_files=extra_primary_files,
    )


def _existing_shared_workflow_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    rvf_run = payload.get("rvf_run")
    if not isinstance(rvf_run, dict):
        return None
    state = rvf_run.get("shared_workflow_state")
    if isinstance(state, dict):
        return state
    return None


def _claude_projects_root() -> Path:
    """Root holding Claude Code 的 per-project transcript 目录。

    尊重 ``CLAUDE_CONFIG_DIR``（Claude Code 把整棵 ``~/.claude`` 树迁到那里），
    否则回落 ``~/.claude``。transcript 落在
    ``<root>/projects/<cwd-slug>/<session-id>.jsonl``。
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = (
        Path(base.strip()).expanduser()
        if isinstance(base, str) and base.strip()
        else Path("~/.claude").expanduser()
    )
    return root / "projects"


def _claude_project_slug(cwd: str) -> str:
    """Claude Code 的 project 目录 slug：cwd 里每个 ``/`` 与 ``.`` → ``-``。"""
    return re.sub(r"[/.]", "-", cwd)


def _resolve_child_transcript_path(
    event: dict[str, Any], *, child_session_id: str
) -> tuple[Path | None, dict[str, Any]]:
    """确定性解析被 dispatch 的 child agent transcript 路径。

    返回 ``(path, info)``。即使文件尚未落盘也给出 child transcript 位置——child
    的*首条* UserPromptSubmit 时 host 已为 transcript 命名但可能还没写出。
    ``capture_run`` 会在 capture 时（child 自身 Stop，那时文件必存在）重新
    ``.is_file()`` 校验，因此记录一个"尚未存在但即将存在"的路径是安全的，且
    严格优于 ``None``（旧行为让持久 ``origin.json`` 对 child 拓扑失明）。

    解析顺序（fail-safe，绝不臆造路径）：
      1. *declared* —— host 在 hook payload 里上报的 ``transcript_path`` /
         ``conversation_path`` / ``session_path``（Claude 与 Codex 均会带），
         即便尚未落盘也采用。
      2. *derived* —— 仅 Claude，且无 declared 路径时：重建
         ``<claude-projects>/<cwd-slug>/<session-id>.jsonl``，仅当该 project
         目录已存在（与 flush 无关的 Claude 信号）才采用，否则返回 ``None``。
    """
    info: dict[str, Any] = {}
    raw = (
        event.get("transcript_path")
        or event.get("conversation_path")
        or event.get("session_path")
    )
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw.strip()).expanduser().resolve()
        info["transcript_source"] = "declared"
        info["child_transcript_exists"] = candidate.is_file()
        return candidate, info

    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        project_dir = _claude_projects_root() / _claude_project_slug(cwd.strip())
        if project_dir.is_dir():
            derived = (project_dir / f"{child_session_id}.jsonl").resolve()
            info["transcript_source"] = "derived"
            info["child_transcript_exists"] = derived.is_file()
            return derived, info
        info["transcript_source"] = "derive_skipped_no_project_dir"
        info["derive_candidate_dir"] = str(project_dir)
        return None, info

    info["transcript_source"] = "unavailable"
    return None, info


def _backfill_child_session(
    record: rvf_prep_file.PrepFileRecord,
    event: dict[str, Any],
    *,
    prep_root: str | Path | None,
) -> tuple[rvf_prep_file.PrepFileRecord, dict[str, Any]]:
    """Self-backfill the dispatched task agent's session into the prep + origin.

    When the UserPromptSubmit hook fires *inside* a dispatched task agent whose
    session differs from the recorded origin — Cline Kanban flow-2-branch /
    flow-2-inplace *and* flow-1-self-rising / kanban-followup (each runs in its
    own Claude session) — the parent Stop hook only knows its own transcript, so
    ``trajectory_capture.capture_run`` would slice the wrong conversation. Here
    we record the task agent's ``child_session_id`` / ``child_transcript_path``
    into:

    1. the prep payload — ledger trail + idempotency;
    2. the persistent ``origin.json`` — the channel ``capture_run`` reads
       long after the short-TTL prep file has been swept.

    Conservative + idempotent: acts only when the current session id is present
    and differs from ``origin_session_id`` (so same-session manual / followup
    dispatch is untouched). Returns the possibly-updated prep record + a debug
    dict (also emitted as a prep diagnostic).

    Ordering assumption: the parent Stop hook writes ``origin.json`` and spawns
    the task agent *before* the task agent can submit a token-bearing prompt, so
    by the time this runs the parent's ``origin.json`` is fully written; the
    merge here only adds child keys and preserves parent keys. A corrupt/partial
    read still fails closed (caught → ``origin_write_error`` → capture falls back
    to parent transcript, no regression).
    """
    debug: dict[str, Any] = {"backfilled": False}
    child_session_id = event.get("session_id")
    if not isinstance(child_session_id, str) or not child_session_id.strip():
        debug["skip_reason"] = "no_child_session_id"
        return record, debug
    child_session_id = child_session_id.strip()
    origin_session_id = record.payload.get("origin_session_id")
    if not isinstance(origin_session_id, str) or not origin_session_id.strip():
        debug["skip_reason"] = "no_origin_session_id"
        return record, debug
    if child_session_id == origin_session_id.strip():
        debug["skip_reason"] = "same_session"
        return record, debug

    child_transcript, transcript_info = _resolve_child_transcript_path(
        event, child_session_id=child_session_id
    )
    child_transcript_str = str(child_transcript) if child_transcript is not None else None
    debug.update(
        {
            "child_session_id": child_session_id,
            "child_transcript_path": child_transcript_str,
            **transcript_info,
        }
    )

    if (
        record.payload.get("child_session_id") != child_session_id
        or record.payload.get("child_transcript_path") != child_transcript_str
    ):
        try:
            record = rvf_prep_file.update_prep_file(
                record,
                {
                    "child_session_id": child_session_id,
                    "child_transcript_path": child_transcript_str,
                },
            )
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            debug["prep_update_error"] = f"{type(exc).__name__}: {exc}"

    origin_path_raw = record.payload.get("origin_metadata_path")
    origin_path: Path | None = None
    if isinstance(origin_path_raw, str) and origin_path_raw.strip():
        origin_path = Path(origin_path_raw).expanduser()
    else:
        rvf_run = record.payload.get("rvf_run")
        if isinstance(rvf_run, dict):
            run_dir = rvf_run.get("run_dir")
            if isinstance(run_dir, str) and run_dir.strip():
                origin_path = Path(run_dir).expanduser() / "artifacts" / "origin.json"

    if origin_path is not None and origin_path.is_file():
        try:
            origin_payload = json.loads(origin_path.read_text(encoding="utf-8"))
            if not isinstance(origin_payload, dict):
                origin_payload = {}
            if (
                origin_payload.get("child_session_id") != child_session_id
                or origin_payload.get("child_transcript_path") != child_transcript_str
            ):
                origin_payload["child_session_id"] = child_session_id
                origin_payload["child_transcript_path"] = child_transcript_str
                # Reuse rvf_prep_file's atomic writer (O_EXCL tmp + random
                # suffix + replace + failure cleanup) rather than a weaker
                # ad-hoc one — single source of truth for atomic JSON IO.
                rvf_prep_file._atomic_write_json(origin_path, origin_payload)
            debug["backfilled"] = True
            debug["origin_path"] = str(origin_path)
        except (OSError, json.JSONDecodeError) as exc:
            debug["origin_write_error"] = f"{type(exc).__name__}: {exc}"
    else:
        debug["skip_reason"] = "origin_metadata_unavailable"
        if origin_path is not None:
            debug["origin_missing"] = str(origin_path)

    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_child_session_backfill",
                "status": "ok" if debug.get("backfilled") else "skipped",
                **{key: value for key, value in debug.items() if key != "backfilled"},
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass
    return record, debug


def arm_kanban_followup_lock_on_delivery(
    event: dict[str, Any],
    record: rvf_prep_file.PrepFileRecord,
    *,
    prep_root: str | Path | None = None,
) -> str | None:
    """投递确认时才 arm kanban-followup 锁，且**只落到 awaiting 态**（delivery-confirmed arm）。

    这是把锁的 arm 从 Stop hook（dispatch 时乐观预 arm）移到这里的核心。本 hook 只有在
    Cline Kanban 注入的 follow-up trigger **真正成为一个 prompt**（投递落地）时才会 fire，
    所以在此 arm 才能保证锁不为「投递静默失败」的那一轮存在。若投递静默失败（例如 /compact 在
    注入 turn 落地前重置了会话），本 hook 不 fire → 不 arm → 不会留下纯 TTL 锁空转、挡住后续
    自动 dispatch（治本 squat）。

    但**「prompt 被提交」仍不等于「agent 接手」**：本 hook 在提交那一刻 fire，严格早于模型产出
    任何 token；提交后这条 prompt 还可能被同回合的中断冲掉（enqueue→dequeue，从未成为
    transcript 条目），或落地后被用户 rewind 撤销。历史实现在此直接写 ``in_progress``，于是
    Stop 读侧会对一个从未看到过这条 prompt 的 agent 发「继续收尾这一轮」的 re-engage nudge
    （实测 run ``rvf-20260808T090747Z-stop-hook-ea36796d``）。故这里只落
    ``delivered_awaiting_agent_decision``：仍满足互斥去重，但读侧不发 nudge；升格成
    ``in_progress`` 由 Stop 读侧按「trigger 是否仍在当前 transcript 祖先链上」判定。

    锁主键是 task_id（task-path），故 ``target_kanban_task_id`` 的一致性是关键——它取自 prep
    payload（权威，与 Stop hook 旧 arm 用的同一来源）。run/repo/cwd 同样取自 prep。
    best-effort：缺 task_id 则不 arm；任何异常都不阻断本次 prompt（只记 diagnostic）。
    锁根路径走 ``kanban_followup_lock`` 的默认解析（``RVF_KANBAN_FOLLOWUP_LOCK_ROOT``
    env 或 ``~/.rvf``），与 Stop hook 读/清侧一致。

    锁主键是 task_id（task-path），故 ``target_kanban_task_id`` 的一致性是关键——它取自 prep
    payload（权威，与 Stop hook 旧 arm 用的同一来源）。run/repo/cwd 同样取自 prep。
    best-effort：缺 task_id 则不 arm；任何异常都不阻断本次 prompt（只记 diagnostic）。
    锁根路径走 ``kanban_followup_lock`` 的默认解析（``RVF_KANBAN_FOLLOWUP_LOCK_ROOT``
    env 或 ``~/.rvf``），与 Stop hook 读/清侧一致。
    """
    payload = record.payload
    task_id = payload.get("target_kanban_task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    task_id = task_id.strip()
    rvf_run = payload.get("rvf_run")
    rvf_run = rvf_run if isinstance(rvf_run, dict) else {}
    session_id = payload.get("origin_session_id") or event.get("session_id")
    repo = payload.get("origin_repo")
    cwd = payload.get("origin_cwd") or event.get("cwd")
    # 投递落地的会话身份 + transcript 路径：读侧要用它们判定「本次 Stop 与这把锁是不是同一个
    # 会话」，不同会话则不做祖先链对账（保守）。历史 payload 这三个字段全是 None。
    delivered_session_id = event.get("session_id")
    delivered_transcript_path = event.get("transcript_path")
    try:
        marker_path = kanban_followup_lock.write_marker(
            task_id=task_id,
            session_id=session_id if isinstance(session_id, str) and session_id.strip() else None,
            run_id=str(rvf_run.get("run_id") or ""),
            run_dir=str(rvf_run.get("run_dir") or ""),
            repo=repo if isinstance(repo, str) else None,
            cwd=cwd if isinstance(cwd, str) else None,
            delivered_session_id=(
                delivered_session_id
                if isinstance(delivered_session_id, str) and delivered_session_id.strip()
                else None
            ),
            delivered_transcript_path=(
                delivered_transcript_path
                if isinstance(delivered_transcript_path, str) and delivered_transcript_path.strip()
                else None
            ),
            dispatch_token=record.token,
            state=kanban_followup_lock.STATE_DELIVERED_AWAITING_AGENT_DECISION,
        )
    except Exception as exc:  # best-effort：绝不阻断 prompt
        try:
            rvf_prep_file.append_diagnostic(
                root=prep_root,
                token=record.token,
                record={
                    "event": "user_prompt_submit_kanban_followup_arm_failed",
                    "status": "warning",
                    "error": f"{type(exc).__name__}: {exc}",
                    "kanban_task_id": task_id,
                },
            )
        except (OSError, rvf_prep_file.PrepFileError):
            pass
        return None
    marker_str = str(marker_path) if marker_path is not None else None
    # 投递落地即解除 pending（按 token 防误清）：Stop 在「未确认投递」时写了 pending marker，
    # 本 hook fire 证明注入的 follow-up trigger 真的成为了 prompt → 清掉 pending，使下一次 Stop
    # 不会把它误判为静默丢投而重投。best-effort：清 pending 失败绝不阻断本次 prompt。
    try:
        removed_pending = kanban_followup_lock.clear_pending_marker(
            task_id=task_id, token=record.token
        )
    except Exception:
        removed_pending = []
    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_kanban_followup_armed",
                "status": "armed",
                "kanban_task_id": task_id,
                "run_id": rvf_run.get("run_id"),
                "marker_path": marker_str,
                "marker_state": kanban_followup_lock.STATE_DELIVERED_AWAITING_AGENT_DECISION,
                "removed_pending_marker_paths": removed_pending,
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass
    return marker_str


def stamp_kanban_followup_bootstrap_gate(
    event: dict[str, Any],
    record: rvf_prep_file.PrepFileRecord,
    result_state: dict[str, Any] | None,
    *,
    prep_root: str | Path | None = None,
) -> str | None:
    """prepare 判定「等 local base bootstrap」时，把 gate 就地盖进刚 arm 的 in-progress 锁。

    借的是本 hook 的时序：锁在**同一个进程**里先 arm（投递确认）、prepare 才跑（见
    ``inspect_user_prompt_submit``），所以 prepare 一返回就能把等待态写回同一把锁——不必新开
    marker 族、也不必让 Stop 读侧去反查 prep 文件。锁的 ``state`` 同时从 ``in_progress`` 改成
    ``waiting_bootstrap``，Stop 读侧据此改发定向指令（跑同步 skill）而不是通用「去收尾 handoff」。

    best-effort：无锁 / 无 gate / 写失败一律返回 None，绝不阻断本次 prompt。
    """
    gate = rvf_bootstrap_gate.gate_of(result_state)
    if gate is None:
        return None
    payload = record.payload
    task_id = payload.get("target_kanban_task_id")
    task_id = task_id.strip() if isinstance(task_id, str) and task_id.strip() else None
    session_id = payload.get("origin_session_id") or event.get("session_id")
    session_id = session_id if isinstance(session_id, str) and session_id.strip() else None
    try:
        marker = kanban_followup_lock.read_marker(
            task_id=task_id,
            session_id=session_id,
            # 锁 key 含仓身份（``kanban_followup_lock.marker_paths``）：锚点取 prep payload 的
            # origin_repo / origin_cwd，与同进程刚 arm 那次写侧同源，故必然解析到同一 identity。
            repo=payload.get("origin_repo") or payload.get("origin_cwd") or event.get("cwd"),
        )
        if marker is None:
            return None
        # 锁刚在同一进程 arm 成 ``delivered_awaiting_agent_decision``（agent 还没被确认收到这条
        # trigger）。此时**只盖 gate、不改 state**：改成 waiting_bootstrap 会把「收没收到」这一
        # 维信息抹掉，读侧于是又会对一个从未看到 prompt 的 agent 发定向 re-engage。真正的升格由
        # Stop 读侧在确认 trigger 仍在 transcript 祖先链上时做（届时按有无 gate 落到
        # waiting_bootstrap / in_progress）。
        keep_awaiting = (
            kanban_followup_lock.marker_state(marker)
            == kanban_followup_lock.STATE_DELIVERED_AWAITING_AGENT_DECISION
        )
        if not kanban_followup_lock.stamp_bootstrap_gate(
            marker,
            gate=gate,
            state=None if keep_awaiting else kanban_followup_lock.STATE_WAITING_BOOTSTRAP,
        ):
            return None
        marker_path = marker.get("_marker_path")
    except Exception:  # best-effort：绝不阻断 prompt
        return None
    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_kanban_followup_bootstrap_gate_stamped",
                "status": kanban_followup_lock.marker_state(marker),
                "kanban_task_id": task_id,
                "marker_path": marker_path,
                "bootstrap_phase": gate.get("phase"),
                "required_sync_skill": gate.get("required_sync_skill"),
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass
    return marker_path if isinstance(marker_path, str) else None


def _run_artifacts_present(payload: dict[str, Any]) -> bool:
    """best-effort：prep payload 指向的 run artifacts（``rvf_run.run_dir``）是否仍在磁盘。

    FU-1 过期再生成的守卫——只复活 run_dir 仍存在的过期 prep，不复活已被清理 / 真过期的
    run。任何结构异常或 OSError → False（保守不复活）。
    """
    if not isinstance(payload, dict):
        return False
    rvf_run = payload.get("rvf_run")
    if not isinstance(rvf_run, dict):
        return False
    run_dir = rvf_run.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return False
    try:
        return Path(run_dir).is_dir()
    except OSError:
        return False


def _dispatch_prep_revive_max_age_seconds() -> float:
    raw = os.environ.get(DISPATCH_PREP_REVIVE_MAX_AGE_ENV)
    if raw is None or not raw.strip():
        return float(DISPATCH_PREP_REVIVE_MAX_AGE_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DISPATCH_PREP_REVIVE_MAX_AGE_SECONDS)
    return max(0.0, value)


def _dispatch_prep_revive_staleness_veto(
    payload: dict[str, Any],
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """该过期 prep 是否**老到不该复活**？返回否决理由 dict，或 None（可以复活）。

    两条判据满足其一即否决：

    1. 距**首次**派发（``originally_created_at``，缺则 ``created_at``）超过上界（默认 15min）；
    2. 派发之后这份 transcript 里出现过显式的人类 prompt——会话已经走远，那条 trigger 属于
       上一个语境。

    判据 2 只认显式信号（``promptSource: typed`` / ``origin.kind: human``），拿不到就当作
    「没有否决证据」，靠判据 1 兜底。任何异常一律不否决（best-effort，宁可保持既有行为）。
    """
    created_raw = payload.get("originally_created_at")
    if not isinstance(created_raw, str) or not created_raw.strip():
        created_raw = payload.get("created_at")
    if not isinstance(created_raw, str) or not created_raw.strip():
        return None
    try:
        created_at = rvf_prep_file.parse_timestamp(created_raw)
    except Exception:
        return None
    created_epoch = created_at.astimezone(timezone.utc).timestamp()
    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = max(0.0, reference_now.timestamp() - created_epoch)
    max_age = _dispatch_prep_revive_max_age_seconds()
    if max_age > 0 and age_seconds > max_age:
        return {
            "reason": "prep_older_than_revive_max_age",
            "prep_age_seconds": round(age_seconds, 3),
            "revive_max_age_seconds": max_age,
            "originally_created_at": created_raw,
        }

    transcript_path = event.get("transcript_path")
    try:
        # 探测拿不到结论（无路径 / 不可读 / 该 harness 没有显式人类输入标记）时返回 False，
        # 即「没有否决证据」——判据 1 的年龄上界仍在兜底。
        found_human, _probe_error = has_explicit_human_prompt_after(
            transcript_path=transcript_path,
            after_epoch_seconds=created_epoch,
        )
    except Exception:  # best-effort：探测失败绝不否决
        return None
    if found_human:
        return {
            "reason": "human_prompt_after_dispatch",
            "prep_age_seconds": round(age_seconds, 3),
            "revive_max_age_seconds": max_age,
            "originally_created_at": created_raw,
            "transcript_path": transcript_path if isinstance(transcript_path, str) else None,
        }
    return None


def _bootstrap_waiting_additional_context_text(gate: dict[str, Any]) -> str:
    """bootstrap 等待态注入的 ``additionalContext`` 正文（model-facing，喂进主 agent 上下文）。

    这是入口 B 的 self-injection checkpoint：Stop 侧的 ``reengage_decision`` 要等 agent 干停
    才能送达，而这里在 follow-up trigger 落地的**同一个 turn** 就把指引交给 agent，省掉一整轮
    空转。步骤文案与入口 A 共用 ``rvf_bootstrap_gate.bootstrap_waiting_agent_steps``（单一来源）。
    """
    steps = rvf_bootstrap_gate.bootstrap_waiting_agent_steps(gate)
    return "\n".join(
        [
            "=== RVF: 本轮 review 尚未开始（等 local base bootstrap）===",
            f"local base `{gate.get('local_base_ref')}` 还没被吸收进本 task worktree，"
            "RVF 已把本轮挂起为**可恢复的等待态**（run 与 scope 均未创建、reviewer 未派发）。",
            *steps,
            "④ 完成后本轮会自动接续 prepare 并进入 review——请**不要**输出 handoff、"
            "也不要另开一轮 RVF。",
        ]
    )


def _manual_decision_additional_context_text(
    *,
    summary: dict[str, Any],
    token: str,
    newly_minted: bool,
    inline_scope: list[str] | None = None,
    overrode_agent_declined_cooldown: bool = False,
    overrode_agent_declined_session_scope_snooze: bool = False,
    overrode_agent_deferred_until_condition_decline: bool = False,
    overridden_deferred_until_condition_decline_reason: str | None = None,
) -> str:
    """manual 决策点注入的 ``additionalContext`` 正文（model-facing，喂进主 agent 上下文）。

    复用 Stop hook 的 ``_render_decision_point_injection_prompt`` 作**单一真相源**——渲染候选摘要 +
    ``RVF_DECISION_TOKEN`` + rvf-agent ``start/scope/dispatch/decline`` 样例命令 + 「简析后立刻
    start/decline」指引 + R24 递归护栏文案；仅在前面加一行 manual 触发前缀，末尾附用户内联 ``scope:``
    提示。Stop 引擎 import 失败时降级为精简自包含提示（token + 关键命令），绝不阻断。

    ``overrode_agent_declined_cooldown=True`` 时额外插一句：本次手动触发**推翻了你早前对同一批改动
    的谢绝**。这句不能省——否则 agent 拿到 token 后极可能照上次判断再谢绝一次，用户白打一遍。该参数
    只反映「是否命中并推翻了 agent 的谢绝」，与冷却记录能否被物理清除无关（清除失败只影响后续自动
    路径，对本轮 agent 无意义），故这段文案不提「冷却已清除」，恒为真话。

    ``overrode_agent_declined_session_scope_snooze=True`` 同理，对应闸 2b：agent 早前在**本会话**对这片
    工作范围谢绝过（此后改动还在继续，所以内容指纹已变、冷却不命中，只剩 snooze 挡着）。T3 之后这是
    最常见的被推翻形态，两者任一为真都要告知；措辞按实际命中的那一种写，不把「范围」说成「同一批改动」。

    ``overrode_agent_deferred_until_condition_decline=True`` 对应闸 2c：agent 早前说过「等 X 之后再审」。
    用户显式触发就是「现在就审」，所以要引用那条延后理由，提醒 agent 不要以同一条件再延后一次。
    """
    lines: list[str] = []
    if newly_minted:
        lines.append("你手动触发了 /review-validate-fix。已为本 worktree 铸一枚发起决策 token；")
    else:
        lines.append(
            "你手动触发了 /review-validate-fix；本候选指纹已有一枚活跃决策 token 在等待（幂等复用）；"
        )
    if overrode_agent_declined_cooldown or overrode_agent_declined_session_scope_snooze:
        overridden_decline_description = (
            "你早前对**同一批改动**谢绝过一次 RVF"
            if overrode_agent_declined_cooldown
            else "你早前已在**本会话**对这片工作范围谢绝过 RVF（此后改动还在继续）"
        )
        lines.append(
            f"⚠️ 注意：{overridden_decline_description}，用户这次的显式触发已推翻该谢绝。"
            "请不要以「上次已判断过不必审」为由再次 decline——除非你有本次新出现的理由。"
        )
    if overrode_agent_deferred_until_condition_decline:
        quoted_reason = (
            f"「{overridden_deferred_until_condition_decline_reason}」"
            if overridden_deferred_until_condition_decline_reason
            else "某个条件满足之后"
        )
        lines.append(
            f"⚠️ 注意：你早前在本会话把审查延后到{quoted_reason}；用户这次的显式触发表示现在就审。"
            "请不要以同一个条件再延后一次——除非你有本次新出现的理由。"
        )
    lines.append("请简要评估后用下面的 `rvf` 命令**显式** start（发起）或 decline（谢绝）。")
    lines.append("")
    try:
        import stop_review_validate_fix as stop_hook  # noqa: PLC0415 - 惰性、仅 manual 触发

        lines.append(
            stop_hook._render_decision_point_injection_prompt(
                summary=summary, token=token, fork_context=None
            )
        )
    except Exception:  # pragma: no cover - 渲染降级永不阻断
        lines.append(f"RVF_DECISION_TOKEN={token}")
        lines.append(f"候选文件数: {summary.get('candidate_path_count')}")
        lines.append(f"发起: rvf start --token {token} --reason \"<一句话理由>\"")
        lines.append(
            f"谢绝: rvf decline --token {token} --decline-kind "
            "<structural_not_needed|deferred_until_condition> --reason \"<一句话理由>\""
        )
    if inline_scope:
        lines.append("")
        lines.append(
            "用户在触发串里内联指定了 scope: "
            + ", ".join(inline_scope)
            + "。start（acknowledged 态）后可用 `rvf scope include/exclude` 据此收窄。"
        )
    return "\n".join(lines)


def _handle_manual_decision_point(
    event: dict[str, Any],
    prompt: str,
    *,
    base_payload: dict[str, Any],
    payload: dict[str, Any],
    prep_root: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """manual ``/review-validate-fix`` 触发 → 铸决策 token → ``additionalContext`` 注入决策点（S5·R22）。

    与 Stop hook 的 ``decision_point_injection_decision`` 对偶（同 S2 候选摘要 + S1 铸造 + S4
    注入尝试 ledger），差别在交付通道与生效闸数：UPS 用**合法专属**的
    ``hookSpecificOutput.additionalContext`` 把决策提示喂进主 agent 上下文（Stop 只能用
    decision/block）。manual 是用户显式意图：候选非空时 minted / deduplicated 均 surface
    additionalContext（附 token + rvf-agent CLI 样例命令）。

    **闸二/闸三对本路径不适用**：铸造时传 ``initiated_by_explicit_user_request=True``——命中 declined
    冷却即视为「用户推翻 agent 早前的谢绝」并照常铸（该告知与冷却记录能否清除无关；清除成功还会把
    这批改动整体放回可审状态，清除失败则仅剩后续自动路径仍被挡，文案如实分叉），铸造
    频率熔断整段跳过（它防的是 agent 递归自发起，手动触发按定义来自用户）。因此本函数不再有
    ``declined_cooldown`` / ``circuit_breaker_tripped`` 分支；除已知 outcome 外一律走 mint anomaly 兜底。

    负路径不变量（计划 §五.3）：候选非空的每种结局都写 injection-attempt ledger；成功投递路径
    由 :func:`_confirm_manual_decision_delivery` 在 stdout flush 后写回执，避免尚未 emit 就把 token /
    ledger 乐观推进。候选为空则只出 systemMessage、不写 ledger（与 Stop hook 无候选路径一致）。

    落盘根走 ``$RVF_HOME``/相应 env（S1/S4 默认解析），测试靠 ``RVF_HOME`` 隔离，绝不写真实
    ``~/.rvf``。整段 best-effort，任何异常都收敛成结构化结果 + systemMessage，不阻断本次 prompt。
    """
    # 惰性 import：仅 manual 触发（罕见）才拉起 S1/S2/S4 与 Stop 引擎（renderer 复用），
    # 不污染普通 prompt 的检测热路径。S2 摘要生成器的默认 resolver 本就会惰性 import Stop 引擎，
    # 故复用 renderer 不引入额外 import 成本。
    import rvf_decision_token_store as decision_token_store  # noqa: PLC0415
    import stop_decision_candidate_summary  # noqa: PLC0415
    import agentic_initiation_injection_attempt_ledger as injection_attempt_ledger  # noqa: PLC0415

    now_dt = decision_token_store.parse_timestamp(now) if now else None

    cwd, _cwd_inferred = _resolve_cwd(event)
    repo = _git_resolved_repo(cwd) or cwd
    raw_session = event.get("session_id")
    event_session_id = raw_session.strip() if isinstance(raw_session, str) and raw_session.strip() else None

    manual_payload: dict[str, Any] = {**payload, "dispatch_origin": "post_user_prompt_manual"}
    inline_scope = parse_manual_scope_directive(prompt)

    # ① 候选摘要（S2；纯 git+tracker 缓存，含 committed-but-unreviewed）。best-effort：
    # 构造失败 → 只出 systemMessage，不铸 token、不写 ledger。ledger=None → 跳过 kanban
    # workspace 守卫（manual 无 RunLedger，与旧 manual 路径不做守卫一致）。
    try:
        summary = stop_decision_candidate_summary.build_candidate_summary(
            event=event, cwd=cwd, repo=repo, ledger=None
        )
    except Exception as exc:  # pragma: no cover - 摘要构造永不阻断
        return {
            **manual_payload,
            "status": "manual_decision_summary_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "systemMessage": _trigger_system_message(
                kind="manual_summary_failed", dispatch_origin="post_user_prompt_manual"
            ),
        }

    # transcript 路径随摘要进 token payload：manual 触发同样会走 `rvf-agent start`，而 start 的
    # scope 口径（session-owned + tracker lease vs 退化的 manual-all-uncommitted）取决于它拿不拿得到
    # transcript。与 Stop hook 用**同一个** stamper，避免两条发起入口各盖各的名字。
    try:
        import stop_review_validate_fix as stop_hook  # noqa: PLC0415 - 惰性、仅 manual 触发

        stop_hook.stamp_origin_transcript_path_on_candidate_summary(summary, event)
    except Exception:  # noqa: BLE001 - 加料失败退化成「无 transcript」，由 start 侧显式降级承接
        pass

    worktree = str(summary.get("repo") or repo)
    candidate_count = int(summary.get("candidate_path_count") or 0)
    fingerprint = str(summary.get("candidate_fingerprint") or "")
    has_committed = bool(summary.get("has_committed_but_unreviewed"))
    summary_session_id = summary.get("session_id") or event_session_id

    # ② 无候选：manual 显式触发但本 worktree 自上次 review 以来无可审改动 → 只出 systemMessage
    # （不写 injection ledger，保持 canary 分母干净，与 Stop hook 无候选路径一致）。
    if candidate_count <= 0:
        return {
            **manual_payload,
            "status": "manual_decision_no_candidates",
            "candidate_path_count": 0,
            "systemMessage": _trigger_system_message(
                kind="manual_no_candidates",
                dispatch_origin="post_user_prompt_manual",
                detail=f"repo={worktree}",
            ),
        }

    # ③ 铸 token。`initiated_by_explicit_user_request=True` ⇒ 只剩闸一（去重幂等）生效：
    # declined 冷却命中即清除后继续铸，铸造频率熔断整段跳过（两者都是自动路径的保护闸，
    # 对用户显式意图是误伤）。异常兜底写 ledger。
    try:
        mint = decision_token_store.mint_decision_token(
            worktree=worktree,
            session_id=summary_session_id or "",
            candidate_fingerprint=fingerprint,
            candidate_summary=summary,
            now=now_dt,
            initiated_by_explicit_user_request=True,
            explicit_user_request_provenance=(
                decision_token_store.EXPLICIT_USER_REQUEST_PROVENANCE_DEVELOPER_MANUAL_TRIGGER_VIA_USER_PROMPT_SUBMIT
            ),
        )
    except Exception as exc:  # pragma: no cover - 铸造异常兜底
        injection_attempt_ledger.record_injection_attempt(
            worktree=worktree,
            session_id=summary_session_id,
            candidate_fingerprint=fingerprint,
            candidate_path_count=candidate_count,
            has_committed_but_unreviewed=has_committed,
            mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_MINT_ANOMALY,
            injected=False,
            note=f"{type(exc).__name__}: {exc}",
        )
        return {
            **manual_payload,
            "status": "manual_decision_mint_anomaly",
            "error": f"{type(exc).__name__}: {exc}",
            "systemMessage": _trigger_system_message(
                kind="manual_mint_anomaly", dispatch_origin="post_user_prompt_manual"
            ),
        }

    outcome = mint.outcome

    # ④ minted / deduplicated：surface 决策点 additionalContext（manual 显式意图，两者均注入）。
    if (
        outcome
        in (
            decision_token_store.MINT_OUTCOME_MINTED,
            decision_token_store.MINT_OUTCOME_DEDUPLICATED,
        )
        and mint.token
    ):
        token = mint.token
        newly_minted = outcome == decision_token_store.MINT_OUTCOME_MINTED
        # 「用户推翻了 agent 的谢绝」由**命中**冷却即证成，与 unlink 成败解耦：清除失败只让后续
        # 自动路径仍被挡，绝不能吞掉这次必发的告知（否则 agent 会照上次判断再 decline 一次）。
        overrode_cooldown = mint.explicit_user_request_overrode_agent_declined_cooldown
        cooldown_record_still_blocks_automatic_path = (
            mint.declined_cooldown_record_still_blocks_automatic_path
        )
        # 闸 2b 的同构两事实（M7）：与闸二字段**合并**判断是否告知 agent，各自如实写进用户可见文案。
        overrode_session_scope_snooze = (
            mint.explicit_user_request_overrode_agent_declined_session_scope_snooze
        )
        session_scope_snooze_record_still_blocks_automatic_path = (
            mint.declined_session_scope_snooze_record_still_blocks_automatic_path
        )
        additional_context = _manual_decision_additional_context_text(
            summary=summary,
            token=token,
            newly_minted=newly_minted,
            inline_scope=inline_scope,
            overrode_agent_declined_cooldown=overrode_cooldown,
            overrode_agent_declined_session_scope_snooze=overrode_session_scope_snooze,
            overrode_agent_deferred_until_condition_decline=(
                mint.explicit_user_request_overrode_agent_deferred_until_condition_decline
            ),
            overridden_deferred_until_condition_decline_reason=(
                mint.overridden_deferred_until_condition_decline_reason
            ),
        )
        detail = f"候选 {candidate_count} 文件"
        if overrode_cooldown:
            # 两支都必须与磁盘事实一致：清不掉时绝不谎称「已清除」，并把「自动路径仍被挡」说明白。
            detail += "；已推翻 agent 早前对同一批改动的谢绝"
            detail += (
                "，但谢绝冷却记录清除失败，后续自动 Stop 可能仍被它挡住"
                if cooldown_record_still_blocks_automatic_path
                else "，并已清除对应的谢绝冷却记录"
            )
        if overrode_session_scope_snooze:
            detail += "；已推翻 agent 早前在本会话对这片工作范围的谢绝（snooze）"
            detail += (
                "，但 snooze 记录清除失败，后续自动 Stop 可能仍被它静默"
                if session_scope_snooze_record_still_blocks_automatic_path
                else "，并已清除该 snooze"
            )
        if mint.explicit_user_request_overrode_agent_deferred_until_condition_decline:
            detail += "；已推翻 agent 早前在本会话的延后型谢绝（「等条件满足再审」）"
        manual_payload.update(
            {
                "status": "manual_decision_injected" if newly_minted else "manual_decision_deduplicated",
                "token": token,
                "candidate_path_count": candidate_count,
                "candidate_fingerprint": fingerprint,
                "has_committed_but_unreviewed": has_committed,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": additional_context,
                },
                "systemMessage": _trigger_system_message(
                    kind="manual_decision_injected" if newly_minted else "manual_decision_deduplicated",
                    dispatch_origin="post_user_prompt_manual",
                    token=token,
                    detail=detail,
                ),
                _RVF_PENDING_MANUAL_INJECTION_RECEIPT: {
                    "token": token,
                    "worktree": worktree,
                    "session_id": summary_session_id,
                    "candidate_fingerprint": fingerprint,
                    "candidate_path_count": candidate_count,
                    "has_committed_but_unreviewed": has_committed,
                    "mint_outcome": (
                        injection_attempt_ledger.INJECTION_OUTCOME_INJECTED
                        if newly_minted
                        else injection_attempt_ledger.INJECTION_OUTCOME_DEDUPLICATED_PENDING
                    ),
                },
            }
        )
        if inline_scope:
            manual_payload["manual_scope_files"] = inline_scope
        return manual_payload

    # ⑤ 走到这里说明 outcome 既非 minted/deduplicated、也拿不到 token。闸二被清除、闸三被跳过后
    # 这条路径已无正常来源，只可能是 store 侧新增了未知 outcome —— 按铸造异常兜底（写 ledger +
    # developer-facing systemMessage），保住「候选非空的每种结局都写 injection ledger」的负路径不变量。
    injection_attempt_ledger.record_injection_attempt(
        worktree=worktree,
        session_id=summary_session_id,
        candidate_fingerprint=fingerprint,
        candidate_path_count=candidate_count,
        has_committed_but_unreviewed=has_committed,
        mint_outcome=injection_attempt_ledger.INJECTION_OUTCOME_MINT_ANOMALY,
        injected=False,
        note=mint.message or f"未识别的铸造 outcome: {outcome}",
    )
    return {
        **manual_payload,
        "status": "manual_decision_mint_anomaly",
        "candidate_path_count": candidate_count,
        "error": f"未识别的铸造 outcome: {outcome}",
        "systemMessage": _trigger_system_message(
            kind="manual_mint_anomaly", dispatch_origin="post_user_prompt_manual"
        ),
    }


def _confirm_manual_decision_delivery(
    result: dict[str, Any], *, now: str | None = None
) -> None:
    """stdout 已 flush 后推进 manual token，并写与本次真实投递一致的 ledger 回执。"""
    receipt = result.pop(_RVF_PENDING_MANUAL_INJECTION_RECEIPT, None)
    if not isinstance(receipt, dict):
        return
    try:
        import rvf_decision_token_store as decision_token_store  # noqa: PLC0415
        import agentic_initiation_injection_attempt_ledger as injection_attempt_ledger  # noqa: PLC0415

        token = receipt.get("token")
        if not isinstance(token, str) or not token:
            return
        now_dt = decision_token_store.parse_timestamp(now) if now else None
        # dedup 可能命中 minted（上次在 emit 前中断）/injected/acknowledged。前两者可
        # 合法或幂等推进；后者及终态会拒绝回退，仍不影响「本次已交付」ledger 事实。
        try:
            decision_token_store.mark_injected(token, now=now_dt)
        except decision_token_store.DecisionTokenError:
            pass
        injection_attempt_ledger.record_injection_attempt(
            worktree=str(receipt.get("worktree") or ""),
            session_id=receipt.get("session_id"),
            candidate_fingerprint=str(receipt.get("candidate_fingerprint") or ""),
            candidate_path_count=int(receipt.get("candidate_path_count") or 0),
            has_committed_but_unreviewed=bool(
                receipt.get("has_committed_but_unreviewed")
            ),
            mint_outcome=str(receipt.get("mint_outcome") or ""),
            decision_token=token,
            injected=True,
        )
    except Exception:  # noqa: BLE001 - 投递后的可观测性回执永不反向拖红 UPS
        pass


def _decision_point_delivery_intent_task_id_for_banner(
    decision_token_payload: dict[str, Any], event: dict[str, Any]
) -> str | None:
    """投递意图按 kanban task 落盘：优先铸造时记在 token 上的那个 task，拿不到再看本事件所在 task。"""
    detail = decision_token_payload.get("out_of_band_delivery_detail")
    task_id = detail.get("kanban_task_id") if isinstance(detail, dict) else None
    if isinstance(task_id, str) and task_id:
        return task_id
    try:
        from rvf_analyze_advisory import current_kanban_task_id  # noqa: PLC0415

        task_id = current_kanban_task_id(event)
    except Exception:  # noqa: BLE001 - 取不到 task 只意味着这份观测记不下
        return None
    return task_id if isinstance(task_id, str) and task_id else None


def _record_first_arrival_observation_on_delivery_intent(
    result: dict[str, Any],
    *,
    task_id: str | None,
    decision_token: str,
    decision_point_banner_body_text_injected: bool,
    outcome: str,
) -> None:
    """把「UPS 首次观测到标记时发生了什么」写进投递意图，供搁浅对账判定该不该冲销熔断额度（M9）。

    best-effort：写不成只让对账按「UPS 没处理过」计费（多退避一次），绝不阻断这条 prompt。
    """
    try:
        recorded = kanban_followup_lock.record_user_prompt_submit_first_arrival_observation_on_decision_point_delivery_intent(
            task_id=task_id,
            decision_token=decision_token,
            decision_point_banner_body_text_injected=decision_point_banner_body_text_injected,
            outcome=outcome,
        )
    except Exception:  # noqa: BLE001
        recorded = False
    result["decision_point_delivery_intent_first_arrival_observation_recorded"] = bool(recorded)


# RVF-001：「首次注入已交付完毕」凭证。M10 把「锁外读到 injected 的重复到达」判为「用户 rewind 后重交」，
# 但锁外读到 injected 排除不了并发：首个到达刚在锁内提交 minted → injected、还没把正文交付完时，同一
# token 的另一次到达读到的同样是 injected——把它当 rewind 撤回，agent 随后拿到正文，``rvf start`` 却必然
# 因 withdrawn 硬失败。
#
# 故凭证只能在正文**真的交给 host 之后**才出现（RVF-003）：赢得迁移的那次到达在 inspect 阶段只登记
# 「待发布」（``_RVF_PENDING_DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL``），由
# ``main`` 在 hook 输出 print+flush 成功之后调
# ``_publish_decision_point_banner_first_injection_handoff_completed_credential_after_hook_output_flushed``
# 发布。早先在 inspect 的最后一步就发布：「发布 → ``main`` 写 stdout」之间插进来的并发到达会读到凭证、把
# 一枚 agent 马上就要拿到正文的 token 撤回。发布本身先把完整内容写进同目录临时文件、再 ``os.replace`` 到
# 最终路径：最终路径只随完整内容一起出现，写失败不留下最终路径（``O_EXCL`` 直接建最终路径时，内容没写完
# 甚至写失败，``is_file()`` 都已是 True）。凭证只增不删（token 文件被清扫之后才作为孤儿回收），所以
# 「先查凭证、再撤回」不存在凭证被收回的窗口：读到它 ⇒ 首次注入早已交付；读不到 ⇒ 一律按并发到达处置、
# 不撤回。stdout 破裂 / 发布失败 / 赢家在发布前崩溃 ⇒ 凭证缺席，之后的重交不撤回、token 到期静默
# expired——退回 M10 之前的行为，方向安全（漏撤回只是多等一次 TTL，误撤回是让已交付的决策硬失败）。
#
# 三家 host 都经 core ``main`` 写 stdout：Codex 直调本脚本，flush 即交给 host；Claude / Kimi 的 hook shim
# （``hooks/_claude_hook_entry.py`` / ``hooks/_kimi_hook_entry.py``）以子进程跑本脚本、在它退出后把捕获的
# stdout 原样（Kimi 经线材翻译）转写给 host。shim 是 stdlib-only、对 token 无感的同步转写，不碰锁也不碰
# token store；剩下的「本进程退出 → shim 转写」与 manual 回执（``_confirm_manual_decision_delivery``）
# 「flush 即交付」的既有契约同一边界，不再把发布下推进 shim。
#
# 凭证放在 token 文件旁、以 ``.`` 开头且不以 ``.json`` 结尾：token store 的 ``*.json`` 枚举（清扫、
# 按会话遍历、测试计数）看不见它，形状与 payload 读-改-写锁文件相同；发布用的临时文件同样如此。
DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL_FILENAME_SUFFIX = (
    ".decision-point-banner-first-injection-handoff-completed.credential"
)
DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL_PUBLISHING_TEMPORARY_FILENAME_INFIX = (
    ".publishing-temporary-"
)


def _decision_point_banner_first_injection_handoff_completed_credential_path(
    decision_token_store: Any, decision_token: str
) -> Path:
    token_path = decision_token_store.decision_token_path(decision_token)
    return token_path.with_name(
        f".{token_path.name}{DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL_FILENAME_SUFFIX}"
    )


def _decision_point_banner_first_injection_handoff_completed_credential_exists(
    decision_token_store: Any, decision_token: str
) -> bool:
    """凭证在场 ⇔ 首次注入早已交付完毕。读不到（含路径算不出 / I/O 报错）一律 False ⇒ 不撤回。"""
    try:
        return _decision_point_banner_first_injection_handoff_completed_credential_path(
            decision_token_store, decision_token
        ).is_file()
    except Exception:  # noqa: BLE001 - 查不清就按「未交付完毕」保守处置
        return False


def _sweep_orphaned_decision_point_banner_first_injection_handoff_completed_credentials(
    decision_token_directory: Path,
) -> None:
    """回收对应 token 文件已被清扫的凭证（token 文件没了，M10 也就不会再读到它），防止每枚 token 永久堆积一个。

    发布进程在改名前被杀留下的临时文件（名字以凭证名开头）一并按同一判据回收。
    """
    suffix = DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL_FILENAME_SUFFIX
    for credential_or_publishing_temporary_path in decision_token_directory.glob(f".*{suffix}*"):
        token_file_name = credential_or_publishing_temporary_path.name[1:].split(suffix, 1)[0]
        if not (decision_token_directory / token_file_name).exists():
            credential_or_publishing_temporary_path.unlink(missing_ok=True)


def _record_decision_point_banner_first_injection_handoff_completed_credential(
    decision_token_store: Any, decision_token: str
) -> bool:
    """原子发布「首次注入已交付完毕」凭证：完整内容先写同目录临时文件，再 ``os.replace`` 到最终路径。

    只能在正文已交给 host 之后调用（见上方常量注释）。best-effort，永不抛异常：写不成的后果只是之后的
    重交不会被判为 rewind，绝不反过来拖红这条 prompt；写失败既不留下最终路径，也不残留临时文件。
    """
    publishing_temporary_path: Path | None = None
    try:
        credential_path = _decision_point_banner_first_injection_handoff_completed_credential_path(
            decision_token_store, decision_token
        )
        publishing_temporary_path = credential_path.with_name(
            f"{credential_path.name}"
            f"{DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL_PUBLISHING_TEMPORARY_FILENAME_INFIX}"
            f"{os.getpid()}-{os.urandom(6).hex()}"
        )
        file_descriptor = os.open(
            str(publishing_temporary_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "decision_token": decision_token,
                    "first_injection_handoff_completed_at": datetime.now(timezone.utc).isoformat(),
                },
                handle,
            )
        # 原子改名：最终路径要么不存在，要么已带完整内容。已存在时覆盖成等价内容，路径从不缺席（只增不删）。
        os.replace(publishing_temporary_path, credential_path)
        publishing_temporary_path = None
    except Exception:  # noqa: BLE001
        return False
    finally:
        if publishing_temporary_path is not None:
            try:
                publishing_temporary_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 - 残留临时文件由孤儿回收兜底
                pass
    try:
        _sweep_orphaned_decision_point_banner_first_injection_handoff_completed_credentials(
            credential_path.parent
        )
    except Exception:  # noqa: BLE001 - 回收孤儿纯属清洁，失败不影响凭证本身
        pass
    return True


def _publish_decision_point_banner_first_injection_handoff_completed_credential_after_hook_output_flushed(
    result: dict[str, Any],
) -> None:
    """``main`` 在 hook 输出 print+flush 成功之后调用：兑现首个到达登记的凭证发布（RVF-003）。

    没有登记（非首个到达 / 非决策点标记）即 no-op。best-effort，永不抛异常：发布失败 ⇒ 凭证缺席 ⇒
    之后的重交不撤回（方向安全）。
    """
    pending = result.pop(_RVF_PENDING_DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL, None)
    if not isinstance(pending, dict):
        return
    decision_token = pending.get("decision_token")
    if not isinstance(decision_token, str) or not decision_token:
        return
    try:
        import rvf_decision_token_store as decision_token_store  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        result["decision_point_banner_first_injection_handoff_completed_credential_recorded"] = False
        return
    result["decision_point_banner_first_injection_handoff_completed_credential_recorded"] = (
        _record_decision_point_banner_first_injection_handoff_completed_credential(
            decision_token_store, decision_token
        )
    )


def _void_decision_point_boundary_marker_additional_context_text(
    decision_token: str, *, void_reason_for_agent: str
) -> str:
    """作废标记落地时喂给模型的说明（model-facing）。**不含决策正文**，也不驱动 agent 做任何 RVF 动作。"""
    return (
        f"[RVF] 刚才那一行「RVF 发起决策点」边界标记（RVF_DECISION_TOKEN={decision_token}）已作废，无需处理。\n"
        f"- 原因：{void_reason_for_agent}\n"
        "- 不要对这枚 token 执行 rvf start 或 rvf decline（会硬失败），也不必向开发者询问它。\n"
        "- 手上有正在进行的工作就继续；没有就直接结束本回合。需要审查时 RVF 会在之后的 Stop 重新征询。"
    )


def _apply_void_decision_point_boundary_marker_hard_neutralization(
    result: dict[str, Any],
    *,
    decision_token: str,
    void_reason_for_agent: str,
    void_reason_for_developer: str,
    system_message: str,
) -> dict[str, Any]:
    """作废的边界标记落地：照 ``_apply_withdrawn_dispatch_hard_neutralization`` 三管齐下（**唯一**的作废出口）。

    * ``decision:block`` —— host 挡掉这次提交：空闲时不开出一个琐碎回合（也就不会再触发一次 Stop），回合
      中途并入时直接丢弃（2026-10-02 取证：Claude Code 对排队消息的 block 生效，``dropped_by_hook``）；
    * ``additionalContext`` —— 挡不住时（不认 block 的 host）模型至少看到「已作废、无需处理」，不会照
      「未随附即为重复提交」那句去猜、更不会反问开发者要正文；
    * ``systemMessage`` —— 给开发者看。

    它是 prompt 抑制，不驱动 agent：说明文本只叫 agent 忽略这一行。
    """
    result["continue"] = True
    result["decision"] = "block"
    result["reason"] = (
        f"RVF 发起决策点标记已作废（token={decision_token}，{void_reason_for_developer}），已挡下这次提交。"
    )
    result["hookSpecificOutput"] = {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": _void_decision_point_boundary_marker_additional_context_text(
            decision_token, void_reason_for_agent=void_reason_for_agent
        ),
    }
    result["systemMessage"] = system_message
    result["decision_point_boundary_marker_void_on_arrival_hard_neutralized"] = True
    return result


def _withdraw_decision_point_on_boundary_marker_arrival_because_agent_resumed_work(
    result: dict[str, Any],
    *,
    decision_token_store: Any,
    decision_token: str,
    delivery_intent_task_id: str | None,
    resumed_work_verdict: Any,
) -> dict[str, Any]:
    """标记到达时判定 agent 在铸 token 之后已复工：CAS 撤回（锁内仍须是 minted）、不注入正文、走作废出口。

    CAS 输了（锁内已不是 minted：并发到达的另一次 UPS 抢先把正文注入了）⇒ 这一次就是重复到达，走重复到达
    出口。撤回因别的原因失败（token 文件缺失 / 状态机拒绝）⇒ 复工证据仍在，照样**不注入**，意图留给对账。
    """
    try:
        decision_token_store.mark_withdrawn(
            decision_token,
            reason="decision_point_boundary_marker_arrived_after_agent_resumed_work",
            withdrawal_cause=(
                decision_token_store
                .DECISION_POINT_WITHDRAWAL_CAUSE_AGENT_RESUMED_WORK_BEFORE_DECISION_POINT_DELIVERY_LANDED
            ),
            withdraw_only_if_state_inside_lock_is=frozenset({decision_token_store.STATE_MINTED}),
        )
    except decision_token_store.DecisionTokenWithdrawalPreconditionNotMetError as exc:
        result["decision_point_banner_resubmitted_without_body_reinjection"] = True
        result["decision_token_withdrawal_because_agent_resumed_work_lost_race"] = str(exc)
        result["systemMessage"] = _trigger_system_message(
            kind="decision_point_banner_resubmitted_without_body_reinjection",
            token=decision_token,
            status="concurrent_arrival_already_injected",
        )
        return result
    except decision_token_store.DecisionTokenError as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        result["decision_token_withdrawal_because_agent_resumed_work_error"] = error_text
        result["decision_token_advanced_to_injected"] = False
        result["decision_point_delivery_intent_left_for_reconciliation"] = True
        result["systemMessage"] = _trigger_system_message(
            kind="decision_point_banner_arrived_after_agent_resumed_work_but_withdrawal_failed",
            token=decision_token,
            detail=error_text,
        )
        _record_first_arrival_observation_on_delivery_intent(
            result,
            task_id=delivery_intent_task_id,
            decision_token=decision_token,
            decision_point_banner_body_text_injected=False,
            outcome=f"agent_resumed_work_before_marker_arrival_but_withdrawal_failed:{type(exc).__name__}",
        )
        return result
    result["decision_token_withdrawn_because_agent_resumed_work_before_marker_arrival"] = True
    result["decision_token_advanced_to_injected"] = False
    # 先把作废出口放进输出，再做可能抛异常的记账（与首次注入那一支「先输出、后记账」同理）。
    _apply_void_decision_point_boundary_marker_hard_neutralization(
        result,
        decision_token=decision_token,
        void_reason_for_agent=(
            "RVF 铸出这份决策点之后你已经复工，它承载的候选范围已经过时；RVF 会在你这一回合结束时按"
            "当时的改动重新评估"
        ),
        void_reason_for_developer="铸出之后 agent 已复工，已撤回",
        system_message=_trigger_system_message(
            kind="decision_point_banner_withdrawn_on_arrival_because_agent_resumed_work",
            token=decision_token,
            detail=(
                f"证据={getattr(resumed_work_verdict, 'evidence_source', None)} "
                f"{json.dumps(dict(getattr(resumed_work_verdict, 'evidence_detail', {}) or {}), ensure_ascii=False)}"
            ),
        ),
    )
    _record_first_arrival_observation_on_delivery_intent(
        result,
        task_id=delivery_intent_task_id,
        decision_token=decision_token,
        decision_point_banner_body_text_injected=False,
        outcome="withdrawn_agent_resumed_work_before_marker_arrival",
    )
    if delivery_intent_task_id:
        result["cleared_decision_point_delivery_intent_paths"] = (
            kanban_followup_lock.clear_decision_point_delivery_intent(
                task_id=delivery_intent_task_id, decision_token=decision_token
            )
        )
    return result


def _observe_delivered_decision_point_banner(
    event: dict[str, Any],
    decision_token: str,
    *,
    payload: dict[str, Any],
    user_prompt_submit_arrivals_recorded_before_this_arrival: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """带外投递的边界标记**确实成了一条 user message**——INV-A 达成，在此结算。

    粘贴通道只送一行无指令的边界标记（见 Stop 侧 ``_render_decision_point_out_of_band_boundary_marker``）；
    决策正文在投递前已写进 token payload，**由这里以 ``additionalContext`` 注入**。正文不走粘贴，
    是因为 Claude Code 把粘贴包成 ``<pasted_content>`` 并规定其中的指令只有在用户亲口要求时才执行
    ——粘贴块里的「由你决定」无法替自己授权，agent 会反过来问开发者。hook 注入的来源标注如实。

    **只有整条 prompt 就是那一行标记**（容许 host 的一层粘贴包裹，见
    ``decision_point_token_when_prompt_is_exactly_one_out_of_band_boundary_marker``）才会走到这里。下面的
    block / 撤回 / M10 每一支都会丢掉或作废这条提交，开发者在自己的话里引用标记绝不能进来（RVF-001）。

    它做四件事：

    1. 把 token 推进 ``injected``（``mark_injected`` 的语义 = 「agent 确实拿到了这份正文」；
       同步自注入由 Stop hook 当场推进，带外的推进点就是**这里**）；
    2. 清掉那份带外投递意图（它的使命——覆盖「送出去了没有」的崩溃窗口——到此结束）；
    3. 以 ``hookSpecificOutput.additionalContext`` 注入正文（model-facing；各 host 的线材由 hook
       shim 翻译，例如 Kimi 映射到顶层 ``message``）；
    4. 落一条诊断 + 一条 user-facing ``systemMessage``。

    **正文只在 token 首次 ``minted → injected`` 时注入。** 实测 Claude Code 双 ESC rewind 到标记
    之前会把标记放回输入框；用户再回车 = 同一个 token 第二次到达。rewind 的本意就是去掉这次征询，
    所以重复到达一律不注入，只走 developer_only。

    **各条出口的文案必须互不借用**（每一步都可能不成立，出口断言只能说到那一步真做成的事）：

    * token 已达终态（withdrawn / dispatched / declined）⇒ 不推进、不清意图、**不注入正文**，走作废出口
      （``_apply_void_decision_point_boundary_marker_hard_neutralization``：block + 「已作废、无需处理」的
      additionalContext + systemMessage）。只出 systemMessage 不够：agent 只看到一行「未随附即为重复提交」，
      空闲时还会为它白开一个回合；
    * token 仍是 minted、但**铸出之后 agent 已复工**（判据见
      ``decision_point_agent_resumed_work_after_token_mint_evidence``；``user_prompt_submit_arrivals_recorded_before_this_arrival``
      是本次到达**之前**的会话到达记录）⇒ CAS 撤回（锁内仍须是 minted，成因
      ``agent_resumed_work_before_decision_point_delivery_landed``）、清意图、走作废出口，**不注入正文**；
      同一回合结束时的 Stop 会按当时的工作树重新评估。CAS 输了 ⇒ 并发到达的另一次已注入，按重复到达处置；
    * 首次到达但 payload 里没有正文 ⇒ **不推进**（agent 并没拿到正文）、不清意图，交给对账；
    * 迁移（``claim_first_minted_to_injected_transition_exclusively``，锁内原子判定首次到达）抛 ``DecisionTokenError``（TTL 已过 / token 缺失 / 状态机拒绝）⇒
      **未推进**，同样不清意图、不注入，走专用文案并保留异常类型；
    * 非首次到达（token 已是 injected / acknowledged；含并发到达时在锁内输掉迁移权的那一次）⇒
      不推进、不注入，如实报一句；其中**锁外就看到 injected、且「首次注入已交付完毕」凭证在场**的
      那一次，按「用户 rewind 掉了征询、又把放回输入框的标记重交」处置：``mark_withdrawn(rewound_by_user)``
      （M10）。凭证缺席 ⇒ 首个到达可能刚在锁内提交迁移、还没交付完正文，这次是并发到达而不是 rewind，
      不撤回（RVF-001，见 ``DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL_FILENAME_SUFFIX``）。
      rewind 之后正文已不在 agent 上下文里，这枚 token 再没有人能据以决策；不撤回的话，标记落在
      祖先链上会让 rewind 对账永远判它「在链上」，同一指纹被静默去重直到 20 分钟后静默过期，
      与 SKILL.md「当轮作废、下次重问」相悖。acknowledged（agent 已 start）不撤——那是在途的一轮；
    * 只有首次到达且真的推进成功，才注入正文并输出「推进为 injected」这句断言。

    首次到达的每一种结局都写进投递意图（``user_prompt_submit_first_arrival_observation``）：搁浅
    对账只凭「正文确实注入过」这份正向证据冲销熔断额度；标记落地而这份记录缺失，本身就说明 UPS
    没处理它（M9）。成功那一支**先设置输出、再清意图**：两步之间抛出的异常会被外层吞掉，顺序反了
    就会留下「token 已 injected、正文却没注入」的组合（L-F3d）。

    推进发生在 stdout 打印之前：若随后 stdout 破裂，token 已是 injected 而 agent 没拿到正文，
    到期按「看到了没决定」静默 expired。这个窗口只在 hook 进程输出管道断裂时出现，与 manual 路径
    「flush 后再推进」相比的代价是少一次 lost 告警，换来推进失败时绝不注入正文。

    **它绝不 arm 在途锁。** in-progress marker 里 ``decision_token`` 的语义是「agent 已在决策点上
    **显式接受**」，而 awaiting 锁一旦在祖先链上发现 needle 就会把状态升格 ``in_progress`` ⇒ 会对
    一个**从未接受**的 agent 发出「继续收尾这一轮」的 nudge。``rvf-agent start`` 仍然是 arm 接受
    凭证的**唯一**入口。

    整段 best-effort：任何异常都收敛成结构化字段，绝不阻断这条 prompt——横幅已经在 agent 眼前了，
    因为记账失败而 block 它是纯粹的自伤。
    """
    result: dict[str, Any] = {
        **payload,
        "status": "decision_point_banner_delivered",
        "decision_token": decision_token,
        "dispatch_origin": "cline_kanban_decision_point_banner",
    }
    try:
        import rvf_decision_token_store as decision_token_store  # noqa: PLC0415

        lookup = decision_token_store.read_decision_token(decision_token)
        result["decision_token_status"] = lookup.status
        result["decision_token_state_before_observation"] = lookup.state
        payload_data = lookup.payload if isinstance(lookup.payload, dict) else {}
        # 已被撤回 / 已决策的 token 落地了（迟到标记）：**不推进、不清意图、不注入正文**，走作废出口。
        # 撤回的权威仍是 token store 自身（``rvf-agent start`` 读到终态会硬失败）；作废出口只负责不让这一行
        # 白开一个回合、不让 agent 对着它猜。
        if lookup.state in (
            decision_token_store.STATE_WITHDRAWN,
            decision_token_store.STATE_DISPATCHED,
            decision_token_store.STATE_DECLINED,
        ):
            result["decision_token_terminal_on_arrival"] = True
            void_reason_for_agent = {
                decision_token_store.STATE_WITHDRAWN: "这份决策点已被 RVF 撤回",
                decision_token_store.STATE_DISPATCHED: "这份决策点已经发起过审查",
                decision_token_store.STATE_DECLINED: "这份决策点已经被谢绝",
            }[lookup.state]
            return _apply_void_decision_point_boundary_marker_hard_neutralization(
                result,
                decision_token=decision_token,
                void_reason_for_agent=void_reason_for_agent,
                void_reason_for_developer=f"token 已是 {lookup.state}",
                system_message=_trigger_system_message(
                    kind="decision_point_banner_terminal_on_arrival",
                    token=decision_token,
                    status=str(lookup.state),
                ),
            )
        decision_point_body_text_for_agent = payload_data.get(
            decision_token_store.DECISION_POINT_BANNER_BODY_TEXT_FOR_AGENT_PAYLOAD_KEY
        )
        # 只有 injected / acknowledged 能证明「此前已经到达过一次」。expired / lost / 查无此 token
        # 是迟到或异常，不是重复提交——它们照旧落进下面锁内迁移的「未能推进」出口。
        token_already_arrived_before = lookup.state in (
            decision_token_store.STATE_INJECTED,
            decision_token_store.STATE_ACKNOWLEDGED,
        )
        if token_already_arrived_before:
            # 同一个 token 第二次到达（rewind 后标记被放回输入框再回车，或用户手抄了标记）：
            # 首次到达时已经注入过正文并清过意图，正文绝不再注入——连锁内迁移也不调，
            # 否则 acknowledged / 已过 TTL 的 token 会落进「本该推进但没推成」那条不相干的文案。
            result["decision_point_banner_resubmitted_without_body_reinjection"] = True
            if lookup.state == decision_token_store.STATE_INJECTED and not (
                _decision_point_banner_first_injection_handoff_completed_credential_exists(
                    decision_token_store, decision_token
                )
            ):
                # RVF-001：锁外读到 injected 排除不了并发——首个到达可能刚在锁内提交 minted → injected、
                # 还没把正文交付完。凭证缺席就不能判为 rewind：不撤回，只报重复到达。
                result[
                    "decision_point_banner_first_injection_handoff_not_yet_completed_so_token_not_withdrawn"
                ] = True
            elif lookup.state == decision_token_store.STATE_INJECTED:
                # M10：还停在 injected（agent 没 start 也没 decline）、且首次注入早已交付完毕（凭证在场，
                # 只增不删）⇒ 这是 rewind 之后的重交，按用户 rewind 处置、当轮作废。
                try:
                    # 锁外读到的是 injected；锁内必须仍是 injected 才撤（CAS）。这两次读之间 agent 可能刚好
                    # ``start``：``acknowledged → withdrawn`` 在状态机上合法，照撤就会把一轮已接受的 run 撤掉。
                    decision_token_store.mark_withdrawn(
                        decision_token,
                        reason="decision_point_boundary_marker_resubmitted_after_user_rewind",
                        withdrawal_cause=(
                            decision_token_store.DECISION_POINT_WITHDRAWAL_CAUSE_REWOUND_BY_USER
                        ),
                        withdraw_only_if_state_inside_lock_is=frozenset(
                            {decision_token_store.STATE_INJECTED}
                        ),
                    )
                    result["decision_token_withdrawn_after_marker_resubmission"] = True
                    result["systemMessage"] = _trigger_system_message(
                        kind="decision_point_banner_resubmitted_after_rewind_token_withdrawn",
                        token=decision_token,
                        status=str(lookup.state),
                    )
                    return result
                except decision_token_store.DecisionTokenError as exc:
                    # 撤回没成（例如刚被 agent 抢先 start / decline）：退回「只报重复到达」。
                    result["decision_token_withdrawal_after_marker_resubmission_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
            result["systemMessage"] = _trigger_system_message(
                kind="decision_point_banner_resubmitted_without_body_reinjection",
                token=decision_token,
                status=str(lookup.state),
            )
            return result

        delivery_intent_task_id = _decision_point_delivery_intent_task_id_for_banner(
            payload_data, event
        )

        if lookup.state == decision_token_store.STATE_MINTED:
            # 先问「铸出之后 agent 复工了没有」：复工 ⇒ 这份决策点的范围与时机都已过时，正文一律不注入，
            # 不论 payload 里有没有正文。判据只读、不抛；拿不到证据就维持改动前的行为。
            import decision_point_agent_resumed_work_after_token_mint_evidence as resumed_work_evidence  # noqa: PLC0415

            raw_session_id = event.get("session_id")
            raw_transcript_path = event.get("transcript_path")
            resumed_work_verdict = (
                resumed_work_evidence.classify_whether_agent_resumed_work_after_decision_token_mint(
                    decision_token=decision_token,
                    decision_token_payload=payload_data,
                    current_hook_event_session_id=(
                        raw_session_id if isinstance(raw_session_id, str) else None
                    ),
                    current_hook_event_agent_turn_identifier=_agent_turn_identifier_from_hook_event(event),
                    agent_turn_identifier_of_the_stop_event_that_minted_this_token=payload_data.get(
                        decision_token_store.DECISION_TOKEN_PAYLOAD_KEY_AGENT_TURN_IDENTIFIER_OF_THE_STOP_EVENT_THAT_MINTED_THIS_TOKEN
                    ),
                    transcript_path=(
                        raw_transcript_path if isinstance(raw_transcript_path, str) else None
                    ),
                    user_prompt_submit_arrivals_recorded_for_session=(
                        user_prompt_submit_arrivals_recorded_before_this_arrival
                    ),
                )
            )
            result.update(resumed_work_verdict.as_ledger_fields())
            if resumed_work_verdict.agent_resumed_work_after_token_mint:
                return _withdraw_decision_point_on_boundary_marker_arrival_because_agent_resumed_work(
                    result,
                    decision_token_store=decision_token_store,
                    decision_token=decision_token,
                    delivery_intent_task_id=delivery_intent_task_id,
                    resumed_work_verdict=resumed_work_verdict,
                )

        if lookup.state == decision_token_store.STATE_MINTED and not (
            isinstance(decision_point_body_text_for_agent, str)
            and decision_point_body_text_for_agent.strip()
        ):
            # agent 此刻只拿到一行无指令的标记，并没有拿到正文 ⇒ 不推进（injected 的语义是「拿到了
            # 正文」），意图留给对账（到期撤回并重新征询）。
            result["decision_token_advanced_to_injected"] = False
            result["decision_point_banner_body_text_missing"] = True
            result["decision_point_delivery_intent_left_for_reconciliation"] = True
            _record_first_arrival_observation_on_delivery_intent(
                result,
                task_id=delivery_intent_task_id,
                decision_token=decision_token,
                decision_point_banner_body_text_injected=False,
                outcome="banner_body_text_missing_from_decision_token_payload",
            )
            result["systemMessage"] = _trigger_system_message(
                kind="decision_point_banner_arrived_but_body_text_missing",
                token=decision_token,
                status=str(lookup.state),
            )
            return result
        try:
            # 上面锁外的读只是快路径：同一 token 的两次 UPS 可能并发到达、都读到 minted。「首次到达」
            # 的真相必须在 token 排他锁内与迁移一起原子地判定——只有亲手执行了 minted → injected
            # 的那一次才注入正文；输家走下面「重复到达」的出口（developer_only，只出 systemMessage）。
            claim_outcome = (
                decision_token_store.claim_first_minted_to_injected_transition_exclusively(
                    decision_token
                )
            )
        except decision_token_store.DecisionTokenError as exc:
            # 没能推进（TTL 已过 ⇒ ``DecisionTokenExpiredError``、token 文件缺失 ⇒
            # ``DecisionTokenNotFoundError``、状态机拒绝 ⇒ ``DecisionTokenStateError``）。
            #
            # 历史实现把异常记进 ``decision_token_error`` 就继续往下走，出口仍是那条逐字写着
            # 「决策 token 推进为 injected」的 systemMessage ⇒ 磁盘 state 其实还停在 minted、
            # ``rvf-agent start`` 实际会硬失败，开发者却被告知已经推进了。构造上完全可达：
            # 带外横幅异步到达，回执 ``delivered_queued_behind_active_agent_turn`` 排在一个长回合
            # 之后就能越过 1200s TTL。
            #
            # 故这里**提前返回**，走一条如实说明「未推进」的出口，并且**不清投递意图**：意图是
            # 后续对账（stranded 撤回 / 重新征询）唯一的抓手，token 没能推进时清掉它等于把善后
            # 线索也一起删了。
            result["decision_token_error"] = f"{type(exc).__name__}: {exc}"
            result["decision_token_advanced_to_injected"] = False
            result["decision_point_delivery_intent_left_for_reconciliation"] = True
            _record_first_arrival_observation_on_delivery_intent(
                result,
                task_id=delivery_intent_task_id,
                decision_token=decision_token,
                decision_point_banner_body_text_injected=False,
                outcome=f"decision_token_not_advanced_to_injected:{type(exc).__name__}",
            )
            result["systemMessage"] = _trigger_system_message(
                kind="decision_point_banner_arrived_but_token_not_advanced",
                token=decision_token,
                status=str(lookup.status),
                detail=f"{type(exc).__name__}: {exc}",
            )
            return result
        if not claim_outcome.this_call_won_first_minted_to_injected_transition:
            # 锁内发现另一次到达已抢先推进（并发到达的输家）：与上面锁外判定出的重复到达同一出口，
            # 不注入正文、不清意图（赢家负责清）。
            result["decision_token_state"] = claim_outcome.record.state
            result["decision_point_banner_resubmitted_without_body_reinjection"] = True
            result["decision_point_banner_concurrent_arrival_lost_first_injection_claim"] = True
            result["systemMessage"] = _trigger_system_message(
                kind="decision_point_banner_resubmitted_without_body_reinjection",
                token=decision_token,
                status=claim_outcome.state_observed_inside_lock_before_claim,
            )
            return result
        result["decision_token_state"] = claim_outcome.record.state
        result["decision_token_advanced_to_injected"] = True
        # 先把正文与告知放进输出，再做任何可能抛异常的记账：token 已推进为 injected，此后哪一步
        # 失败都只能少一条记账，绝不能让 agent 拿不到正文（L-F3d）。
        result["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": decision_point_body_text_for_agent,
        }
        result["decision_point_banner_body_text_injected_as_additional_context"] = True
        result["systemMessage"] = _trigger_system_message(
            kind="decision_point_banner_delivered",
            token=decision_token,
            detail="边界标记已作为一条真实 user message 到达（可 rewind）；决策正文已随本 hook 注入",
        )
        # RVF-001 / RVF-003：「首次注入已交付完毕」凭证这里只登记、不发布——正文此刻还只在结果里，没交给
        # host。由 main() 在 hook 输出 print+flush 成功之后发布；记账抛异常也照样登记（正文已在输出里）。
        result[_RVF_PENDING_DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL] = {
            "decision_token": decision_token,
        }
        # 清意图之前先记下「正文已注入」：清除若失败，残留的意图如实带着这份正向证据。
        _record_first_arrival_observation_on_delivery_intent(
            result,
            task_id=delivery_intent_task_id,
            decision_token=decision_token,
            decision_point_banner_body_text_injected=True,
            outcome="banner_body_text_injected_as_additional_context",
        )
        if delivery_intent_task_id:
            cleared = kanban_followup_lock.clear_decision_point_delivery_intent(
                task_id=delivery_intent_task_id, decision_token=decision_token
            )
            result["cleared_decision_point_delivery_intent_paths"] = cleared
    except Exception as exc:  # noqa: BLE001 - 记账永不阻断这条 prompt
        result["decision_token_observation_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _withdrawn_dispatch_additional_context_text(
    token: str, tombstone: dict[str, Any]
) -> str:
    """被撤回的投递落地时，喂给模型的**权威**禁令正文（model-facing）。"""
    reason = tombstone.get("reason") or "unspecified"
    claimed_at = tombstone.get("claimed_at") or "<unknown>"
    return (
        "[RVF] 这条 `$review-validate-fix` 触发**已被 RVF 撤回，不要执行它。**\n"
        f"- dispatch token：`{token}`\n"
        f"- 撤回时间：{claimed_at}\n"
        f"- 撤回原因：{reason}\n"
        "\n"
        "它是一条**迟到落地**的旧派发：RVF 在它落地之前就判定该轮投递未送达并已收尾"
        "（释放了 tracker lease、清了锁）。现在执行它会跑出一轮无锁、无 lease、无 scope 契约的 "
        "review，并可能与已经重新发起的那一轮并发撞车。\n"
        "\n"
        "正确做法：**忽略这条触发**，继续你原本的工作。若确实需要 review，等下一次 Stop "
        "由 RVF 重新发起，或由用户显式触发。"
    )


def _apply_withdrawn_dispatch_hard_neutralization(
    token: str, payload: dict[str, Any], tombstone: dict[str, Any]
) -> dict[str, Any]:
    """把 payload 就地改写成「硬中和」的 hook 结果并返回它（**唯一**的中和出口）。

    三管齐下而不是只写一句提示：``decision=block``（host 支持时直接挡掉这次提交）、
    ``additionalContext``（模型可见的权威禁令，挡不住时至少让模型看到）、``systemMessage``
    （用户可见）。只靠注入正文自觉是不可靠的——本次事故本身就是「agent 被拽去跑一轮它从未
    接受的 RVF」，同一类模型合规假设不能再用第二次。

    刻意抽成公共出口：撤回可能在「查 tombstone」与「抢 consumed」之间才胜出（TOCTOU），
    那条晚一步发现的路径必须走**同一条**硬中和，不能只记一行诊断就放行。
    """
    payload.update(
        {
            "status": "dispatch_withdrawn",
            "token": token,
            "workflow_started": False,
            "dispatch_withdrawn_reason": tombstone.get("reason"),
            "dispatch_withdrawn_at": tombstone.get("claimed_at"),
        }
    )
    payload["continue"] = True
    payload["decision"] = "block"
    payload["reason"] = (
        f"RVF 已撤回 dispatch token {token} 对应的那轮 review"
        f"（原因：{tombstone.get('reason') or 'unspecified'}）；这条迟到落地的触发不应被执行。"
    )
    payload["hookSpecificOutput"] = {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": _withdrawn_dispatch_additional_context_text(token, tombstone),
    }
    payload["systemMessage"] = (
        f"RVF: 已撤回的 review 派发迟到落地（token={token}，"
        f"撤回原因={tombstone.get('reason') or 'unspecified'}），已中和、未启动 review。"
    )
    return payload


def _withdrawn_dispatch_neutralization(
    token: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """token 已被撤回 → 返回一份「硬中和」的 hook 结果；否则 None。

    best-effort：tombstone 机制不可用（token 形状怪、文件系统报错）一律返回 None 放行。
    """
    try:
        tombstone = rvf_withdrawn_dispatch_tombstone.dispatch_token_was_withdrawn(token)
    except Exception:
        return None
    if tombstone is None:
        return None
    return _apply_withdrawn_dispatch_hard_neutralization(token, payload, tombstone)


def _claim_dispatch_token_consumed(
    token: str, payload: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any] | None:
    """在消费 token **之前**抢下 ``consumed`` 终态；输给 ``withdrawn`` 则返回硬中和结果。

    抢到 = 这条投递赢了这场竞态，后续任何超时逻辑都不得再把它当「未投递」撤回。

    抢不到且既有态是 ``withdrawn``：撤回是在上一步「查 tombstone」之后才胜出的（TOCTOU 窗口），
    这条投递**已经作废**。此时只把它记进诊断字段是不够的——主流程会照常消费 prep、arm
    kanban-followup 锁、启动 workflow，正好跑出这套机制要根治的那一形态。故这里返回与
    ``_withdrawn_dispatch_neutralization`` **同一条**硬中和出口，由调用方直接返回。

    其余情况一律返回 None 放行：``applicable=False``（token 形状不合法 / 文件系统报错）说明
    这道机制此刻用不了，应按无 tombstone 处理——它是止血机制，不是安全边界，绝不能堵死 prompt。
    """
    try:
        claim = rvf_withdrawn_dispatch_tombstone.claim_dispatch_token_terminal_state(
            token,
            state=rvf_withdrawn_dispatch_tombstone.STATE_CONSUMED,
            reason="user_prompt_submit_delivery_landed",
            details={
                "session_id": event.get("session_id"),
                "transcript_path": event.get("transcript_path"),
            },
        )
    except Exception:
        return None
    if not claim.applicable:
        return None
    payload["dispatch_token_terminal_state_claimed"] = claim.won
    if not claim.won and claim.existing_state:
        payload["dispatch_token_terminal_state_existing"] = claim.existing_state
    if (
        not claim.won
        and claim.existing_state == rvf_withdrawn_dispatch_tombstone.STATE_WITHDRAWN
    ):
        tombstone = (
            claim.existing_payload if isinstance(claim.existing_payload, dict) else {}
        )
        return _apply_withdrawn_dispatch_hard_neutralization(token, payload, tombstone)
    return None


def inspect_user_prompt_submit(
    event: dict[str, Any],
    *,
    prep_root: str | Path | None = None,
    now: str | None = None,
    shared_workflow_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    prompt, prompt_source = prompt_text_from_event(event)
    base_payload: dict[str, Any] = {
        "continue": True,
        "workflow_started": False,
        "prompt_source": prompt_source,
    }
    # S5（R22）：bootstrap 独立确认闸已废除。旧路径在此拦截用户 yes/no 以 resume 一次被
    # bootstrap-size 阻断的 dispatch；overhaul 后重量级准备后置到 rvf-agent CLI `start`，
    # 发起决策统一走「铸决策 token → agent 显式 start/decline」，不再需要 UPS 侧确认闸。
    if prompt is None:
        return {**base_payload, "status": "no_prompt"}

    # host 自己合成的回合（Claude：后台 Bash / 后台子代理完成时的 ``<task-notification>``）不是开发者
    # prompt，而下面每一道判定都预设「这是一条人或 RVF 发来的 prompt」：
    #   * 再入预算复位 / snooze 基线重置 / round baseline 前移 / park 资格——四处共用「是不是开发者
    #     真实 prompt」这一个判据，合成回合对四者都应视为**上一回合的延续**（逐项理由见下方各处注释
    #     与对应测试）；
    #   * dispatch token / origin marker / 决策点 token / 手动触发——合成正文会原样带回子代理与后台
    #     命令读写过的文本，在本仓里常含 RVF 自己的字面量，照跑检测器会把一句引用当成一次投递或手动触发。
    # 所以在任何检测器之前整段短路。只做一件事：续期活跃的 round baseline（不前移），免得长时间自主
    # 工作的会话掉回 dirty-only。对开发者与 agent 都静默（developer_only 且无话可说）。
    host_synthesized_turn_kind = _host_synthesized_user_prompt_submit_turn_kind(event, prompt)
    # 「整条就是一行带外边界标记」只判一次，E1 到达记录与下面的决策点分支共用（RVF-001：开发者 prompt 里
    # 引用标记不算标记落地）。合成回合不跑任何 RVF 字面量检测器。
    decision_point_token_of_standalone_boundary_marker_prompt = (
        decision_point_token_when_prompt_is_exactly_one_out_of_band_boundary_marker(event, prompt)
        if host_synthesized_turn_kind is None
        else None
    )
    # 唯一早于合成回合短路的副作用：记下这次到达（决策点复工判据 E1，任务通知正是最主要的复工信号）。
    user_prompt_submit_arrivals_recorded_before_this_arrival = (
        _record_user_prompt_submit_arrival_for_decision_point_agent_resumed_work_evidence(
            event,
            prompt,
            host_synthesized_turn_kind=host_synthesized_turn_kind,
            decision_point_token_when_prompt_is_exactly_one_out_of_band_boundary_marker=(
                decision_point_token_of_standalone_boundary_marker_prompt
            ),
        )
    )
    if host_synthesized_turn_kind is not None:
        synthesized_payload: dict[str, Any] = {
            **base_payload,
            "status": "host_synthesized_turn_is_not_a_developer_prompt",
            "host_synthesized_user_prompt_submit_turn_kind": host_synthesized_turn_kind,
        }
        extended_round_baseline_marker_path = (
            _extend_round_baseline_expiry_for_host_synthesized_turn(event)
        )
        if extended_round_baseline_marker_path is not None:
            synthesized_payload["round_baseline_expiry_extended_without_moving_head_marker_path"] = (
                extended_round_baseline_marker_path
            )
        # 唯一的例外：对「延后型谢绝」而言合成回合**就是**新回合——后台测试跑完、后台子代理写完，
        # 正是 agent 最常写下的延后条件。
        if _mark_new_turn_observed_for_deferred_until_condition_declines(event):
            synthesized_payload["deferred_until_condition_declines_marked_new_turn_observed"] = True
        return synthesized_payload

    token = dispatch_token_from_text(prompt)
    origin_marker = detect_origin_marker(prompt) if token is None else None
    # 发起决策点横幅经带外投递落地成的那条 user message。它与 dispatch token / origin marker
    # 一样是 **RVF 自己产生的机器正文**，不是用户的工作轮边界——故它同样要抑制 manual 触发判定、
    # 轮 baseline 前移与 PostToolUse park 资格（见下）。只认**整条**就是那一行标记的 prompt（RVF-001）：
    # 开发者在自己的话里引用标记，照常是一条开发者 prompt，下面每道「真实 prompt」判定都照跑。
    decision_point_token = (
        decision_point_token_of_standalone_boundary_marker_prompt
        if token is None and origin_marker is None
        else None
    )
    manual_decision = (
        _classify_manual_trigger(event, prompt)
        if token is None and origin_marker is None and decision_point_token is None
        else "none"
    )
    is_manual = manual_decision == "manual"

    # PostToolUse-park 资格：在回合开头标记「本回合是否该在首次编辑时 park 父 Kanban 卡片」。
    # 仅「非 token、非 kanban-followup marker、非 manual、且在 kanban task」的回合（=Turn 1 实现
    # 回合）eligible；Turn 2（被注入 followup 唤回、带 token+marker，是终态「待人审」回合）与手动
    # 回合 eligible=false，绝不 park。best-effort、纯副作用：永不阻断 prompt。
    try:
        from rvf_analyze_advisory import current_kanban_task_id  # noqa: PLC0415

        park_session_id = event.get("session_id")
        # 仅 kanban task 内的会话才写资格标记（task 外 PostToolUse 本就早退、不读资格 → 不写省 clutter）。
        if (
            isinstance(park_session_id, str)
            and park_session_id.strip()
            and current_kanban_task_id(event)
        ):
            import rvf_post_tool_use  # noqa: PLC0415 - lazy, stdlib-only, off hot import path

            rvf_post_tool_use.mark_park_eligibility(
                park_session_id.strip(),
                eligible=(
                    token is None
                    and origin_marker is None
                    and decision_point_token is None
                    and not is_manual
                ),
            )
    except Exception:
        pass

    # Stop 侧「跨 turn 再入预算」的复位路径 ②：出现了真实的人类回合 ⇒ 连续驱动 agent 的
    # 链条已被打破。判据与上面的 park 资格、下面的 baseline 前移**同源**——dispatch token /
    # origin marker / decision-point token 三者都是 RVF 自己产生的机器正文，不是用户轮边界；
    # 拿它们复位等于让 RVF 自己的注入把自己的保险丝按掉，那道闸就白加了。
    #
    # ⚠️ 会话身份的解析口径与 Stop 侧不完全相同：Stop 用 `session_id_from_event()`（event 缺
    # `session_id` 时回退到 transcript 路径推导），这里只取 `event["session_id"]`。不一致的后果
    # 一律是**少复位或复位错 bucket ⇒ 预算偏严**，不会造成假放行，故刻意不为此把那个巨型模块
    # import 进 UPS 的热路径。
    #
    # best-effort、纯副作用：复位失败只让预算偏严，永不阻断这条 prompt。
    if token is None and origin_marker is None and decision_point_token is None:
        try:
            from core.rvf_user_local_data_paths import (  # noqa: PLC0415
                resolved_rvf_session_hook_state_dir,
            )
            from core.stop_hook_reentry_budget import (  # noqa: PLC0415
                reset_stop_hook_reentry_budget_on_user_prompt_submit,
            )

            reentry_budget_session_id = event.get("session_id")
            if isinstance(reentry_budget_session_id, str) and reentry_budget_session_id.strip():
                reset_stop_hook_reentry_budget_on_user_prompt_submit(
                    session_identifier=reentry_budget_session_id.strip(),
                    state_directory=resolved_rvf_session_hook_state_dir(),
                )
        except Exception:
            pass

    # 闸 2b（declined 会话级 snooze）：开发者发来了真实 prompt ⇒ **不解除** snooze，只把本会话各
    # snooze 的「范围扩大」对比基线重置为发话那一刻（开发者若转去做新工作，出现新路径且新增量超阈值
    # 即重新征询；纯提问 / 继续打磨同一批文件仍保持安静）。判据与上面的再入预算复位**同源**——RVF 自己
    # 的三类机器正文（dispatch token / origin marker / 带外横幅）都不是开发者发话。按会话整目录处理
    # （不依赖 Stop 侧的 worktree 字符串逐字对齐）；会话身份口径同上：只取 ``event["session_id"]``，
    # 对不上只会少重置（基线停在 decline 那一刻，更容易解除），不会误伤。best-effort、纯副作用：失败永不
    # 阻断这条 prompt。
    #
    # 手动触发（``is_manual``）跳过：它马上会以「用户显式请求」清掉本会话本 worktree 的 snooze，先重置
    # 一次基线只会在 lifecycle ledger 里留下一条紧接着就被解除的噪声。host 合成的回合在更上面已整段短路。
    if token is None and origin_marker is None and decision_point_token is None and not is_manual:
        try:
            import rvf_decision_token_store  # noqa: PLC0415 - lazy, stdlib-only

            snooze_session_id = event.get("session_id")
            if isinstance(snooze_session_id, str) and snooze_session_id.strip():
                rvf_decision_token_store.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
                    session_id=snooze_session_id.strip()
                )
        except Exception:
            pass

    # 闸 2c（延后型谢绝）：开发者发话是新回合，放行「等 X 之后再审」的重新征询。判据与上面同源：RVF 自己
    # 的三类机器正文不算回合边界（带外横幅本身落地时 agent 才刚要决定，拿它放行会立刻自问自答）。
    if token is None and origin_marker is None and decision_point_token is None:
        _mark_new_turn_observed_for_deferred_until_condition_declines(event)

    diagnostic_session_keys = ("cwd", "hook_event_name", "session_id", "agent_id", "agent_type")

    if token is None and origin_marker is None and decision_point_token is None:
        # developer_only 诊断（只进结构化结果，不打印、不驱动 agent）：开发者 prompt 里引用了某枚决策 token，
        # 按开发者 prompt 原样放行，没有当作边界标记落地（RVF-001）。记进 base_payload，各个出口都带得到。
        quoted_decision_point_token = decision_point_token_from_text(prompt)
        if quoted_decision_point_token is not None:
            base_payload[
                "decision_point_token_literal_quoted_inside_developer_prompt_not_treated_as_boundary_marker"
            ] = quoted_decision_point_token

    record: rvf_prep_file.PrepFileRecord | None = None
    dispatch_origin: str | None = None
    payload: dict[str, Any] = {**base_payload}

    # 仅对「真正的用户 prompt」记录本轮 baseline（dispatch token / origin marker 是
    # RVF 自注入的机器 prompt，不是用户轮边界，不得前移 baseline）。纯副作用 +
    # best-effort：失败即降级为今日 dirty-only 行为。
    if token is None and origin_marker is None and decision_point_token is None:
        round_baseline_debug = _capture_round_baseline(event, prompt)
        if round_baseline_debug:
            payload["round_baseline"] = round_baseline_debug

    if decision_point_token is not None:
        # 带外投递的发起横幅落地了 —— INV-A 达成。只推进 token + 清投递意图，**绝不 arm 在途锁**
        # （arm 的唯一入口仍是 `rvf-agent start`，即 agent 显式接受之后）。
        return _observe_delivered_decision_point_banner(
            event,
            decision_point_token,
            payload=payload,
            user_prompt_submit_arrivals_recorded_before_this_arrival=(
                user_prompt_submit_arrivals_recorded_before_this_arrival
            ),
        )

    if token is not None:
        # ① 先问「这条投递还算数吗」，再谈消费它。
        #
        # 撤回竞态：RVF 可能早已判定这条投递丢了并撤回/重投，而它**随后**才落地（实测迟到过
        # 49 分钟）。落地时它是完全可执行的——注入正文自包含，agent 会照跑一次无锁、无 lease、
        # 无 scope 契约的 RVF，还可能与新一轮并发撞车。历史设计想靠「撤回时删 prep」止血，但
        # 那结构上不可判定：撤回删 prep 与正常消费 prep，留下的都是「文件缺失」。
        #
        # 故这里读的是**正向 tombstone**，且中和是 hook 强制的，不是靠正文自觉：block 提交
        # （host 支持时）+ 模型可见的权威 additionalContext + 用户可见 systemMessage 三管齐下。
        withdrawal = _withdrawn_dispatch_neutralization(token, payload)
        if withdrawal is not None:
            return withdrawal
        # ② 抢下 ``consumed``：此后任何超时逻辑都不得再把这条投递当「未投递」撤回。
        #    抢不到而态是 consumed 属幂等重入；抢不到而态是 withdrawn 说明撤回是在 ① 之后才
        #    胜出的（TOCTOU 窗口）——那一步返回硬中和结果，这里必须**直接返回**它，绝不能带
        #    着一条已作废的投递继续去消费 prep / arm 锁 / 起 workflow。
        withdrawal_after_claim = _claim_dispatch_token_consumed(token, payload, event)
        if withdrawal_after_claim is not None:
            return withdrawal_after_claim
        lookup_now = rvf_prep_file.parse_timestamp(now) if now else None
        lookup = rvf_prep_file.read_prep_file(token, root=prep_root, now=lookup_now)
        payload.update(
            {
                "status": lookup.status,
                "token": token,
                "prep_file_path": str(lookup.path),
            }
        )
        diagnostic: dict[str, Any] = {
            "event": "user_prompt_submit_dispatch_probe",
            "status": lookup.status,
            "workflow_started": False,
            "prep_file_path": str(lookup.path),
            "prompt_source": prompt_source,
        }
        if lookup.error:
            diagnostic["error"] = lookup.error
            payload["error"] = lookup.error
        for key in diagnostic_session_keys:
            value = event.get(key)
            if isinstance(value, str) and value:
                diagnostic[key] = value
        try:
            diag_path = rvf_prep_file.append_diagnostic(root=prep_root, token=token, record=diagnostic)
            payload["diagnostic_path"] = str(diag_path)
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            payload["diagnostic_error"] = str(exc)
        if (
            lookup.status == "expired"
            and lookup.payload is not None
            and _run_artifacts_present(lookup.payload)
        ):
            # FU-1（方案 B）：prep 已过 TTL，但其 run artifacts 仍在 → 就地续期、记一条
            # revived 诊断、沿用既有 valid 派发路径，而非静默丢整轮 followup。run_dir 不在
            # （run 已清理 / 真过期）则不复活，继续走下面的 dispatch_no_prep 早返回。
            #
            # 但复活必须有时效上界：一条**会话早已走远**的旧派发被原地唤醒，等于把一次从未被
            # agent 看到、且用户已 rewind 掉的 RVF 重新塞回来。两条否决判据满足其一即不复活。
            stale_veto = _dispatch_prep_revive_staleness_veto(
                lookup.payload, event, now=lookup_now
            )
            if stale_veto is not None:
                payload["prep_revive_declined"] = stale_veto
                try:
                    rvf_prep_file.append_diagnostic(
                        root=prep_root,
                        token=token,
                        record={
                            "event": "dispatch_prep_stale_not_revived",
                            "status": "declined",
                            "workflow_started": False,
                            "prep_file_path": str(lookup.path),
                            "prompt_source": prompt_source,
                            **stale_veto,
                        },
                    )
                except (OSError, rvf_prep_file.PrepFileError):
                    pass
                payload["systemMessage"] = _trigger_system_message(
                    kind="dispatch_no_prep", token=token, status="expired_too_stale_to_revive"
                )
                return payload
            try:
                revived = rvf_prep_file.revive_prep_file(lookup, root=prep_root, now=lookup_now)
            except (OSError, rvf_prep_file.PrepFileError):
                revived = None
            if revived is not None:
                lookup = rvf_prep_file.PrepFileLookup(
                    status="valid",
                    token=revived.token,
                    path=revived.path,
                    payload=revived.payload,
                )
                payload["status"] = "valid"
                payload["prep_revived"] = True
                try:
                    rvf_prep_file.append_diagnostic(
                        root=prep_root,
                        token=token,
                        record={
                            "event": "user_prompt_submit_dispatch_prep_revived",
                            "status": "revived",
                            "workflow_started": False,
                            "prep_file_path": str(revived.path),
                            "prompt_source": prompt_source,
                        },
                    )
                except (OSError, rvf_prep_file.PrepFileError):
                    pass
        if lookup.status != "valid" or lookup.payload is None:
            # dispatch token 在场但 prep 缺失 / 过期不可再生 / 不可读：给用户一条可见诊断行
            # （user-facing systemMessage，不进模型上下文），磁盘 diagnostics 已另记。
            payload["systemMessage"] = _trigger_system_message(
                kind="dispatch_no_prep", token=token, status=lookup.status
            )
            return payload
        record = rvf_prep_file.PrepFileRecord(
            token=lookup.token, path=lookup.path, payload=dict(lookup.payload)
        )
        dispatch_origin = str(lookup.payload.get("dispatch_origin") or "stop_hook")
        record, child_debug = _backfill_child_session(record, event, prep_root=prep_root)
        if child_debug.get("backfilled"):
            payload["child_session_id"] = child_debug.get("child_session_id")
            payload["child_transcript_path"] = child_debug.get("child_transcript_path")
        # 投递确认即 arm kanban-followup in-progress 锁：只有 Cline Kanban 注入的 follow-up
        # trigger 真正成为 prompt（即本 hook fire）时才上锁，替代 Stop hook 旧的「dispatch
        # 即乐观预 arm」。投递静默失败则本 hook 不 fire → 不 arm → 无 squat。
        if KANBAN_FOLLOWUP_MARKER in prompt:
            armed_marker_path = arm_kanban_followup_lock_on_delivery(
                event, record, prep_root=prep_root
            )
            if armed_marker_path:
                payload["kanban_followup_in_progress_marker_path"] = armed_marker_path
    elif origin_marker is not None:
        # Marker without token is an inconsistent state: dispatch should always set token.
        try:
            diag_path = rvf_prep_file.append_diagnostic(
                root=prep_root,
                token=rvf_prep_file.generate_token(),
                record={
                    "event": "user_prompt_submit_dispatch_probe",
                    "status": "dispatch_marker_without_token",
                    "origin_marker": origin_marker,
                    "prompt_source": prompt_source,
                    **{
                        key: event.get(key)
                        for key in diagnostic_session_keys
                        if isinstance(event.get(key), str) and event.get(key)
                    },
                },
            )
            payload["diagnostic_path"] = str(diag_path)
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            payload["diagnostic_error"] = str(exc)
        return {
            **payload,
            "status": "dispatch_marker_without_token",
            "origin_marker": origin_marker,
            # 自注入近失（marker 在场却无 token）：user-facing 诊断行。
            "systemMessage": _trigger_system_message(
                kind="marker_without_token", marker=origin_marker
            ),
        }
    elif is_manual:
        # S5（R22）：manual 触发不再预置 packet/prepare（旧 `_create_manual_prep_file` +
        # shared-workflow 预置逻辑已退役至 rvf-agent CLI `start`）。改为与 Stop hook 决策点
        # `decision_point_injection_decision` 对偶——铸决策 token（S1；用户显式触发只受闸一去重约束）并经 UPS 专属的
        # `additionalContext` 交付「候选摘要（S2）+ token + rvf-agent start/scope/dispatch/decline
        # 样例命令 + 简析后立刻 start/decline 指引」，由主 agent 显式发起或谢绝。此分支自成终局，
        # 提前返回，不再落入下方 token 派发的 shared-workflow 块。
        return _handle_manual_decision_point(
            event,
            prompt,
            base_payload=base_payload,
            payload=payload,
            prep_root=prep_root,
            now=now,
        )
    elif manual_decision == "suppressed":
        # 触发字面量在场但识别为 handoff 正文 / 姊妹命令参数：不新建 manual prep、
        # 不派发 review。给用户一条可见诊断行（user-facing systemMessage，不进模型
        # 上下文），说明为何「看似带 review-validate-fix 字面量却没启动 review」，
        # 并指明如何真正手动触发。这是用户「通知而非自注入」想法里 hook 真能做到的子集。
        return {
            **base_payload,
            "status": "manual_trigger_suppressed",
            "systemMessage": _trigger_system_message(kind="suppressed_handoff_literal"),
        }
    else:
        return {**base_payload, "status": "no_token"}

    assert record is not None  # narrow type for mypy/readers

    existing_state = _existing_shared_workflow_state(record.payload)
    if existing_state is not None and existing_state.get("status") == "completed":
        payload["workflow_started"] = False
        payload["shared_workflow_state"] = existing_state
        try:
            diag_path = rvf_prep_file.append_diagnostic(
                root=prep_root,
                token=record.token,
                record={
                    "event": "user_prompt_submit_shared_workflow_skipped",
                    "status": "already_completed",
                    "prep_file_path": str(record.path),
                    "dispatch_origin": dispatch_origin,
                    "prompt_source": prompt_source,
                },
            )
            payload.setdefault("diagnostic_path", str(diag_path))
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            payload["diagnostic_error"] = str(exc)
        # 该 prep 已 RVF 过、本次未重跑：user-facing 可见行（token 派发 + manual 皆可达）。
        payload["systemMessage"] = _trigger_system_message(
            kind="already_completed",
            dispatch_origin=dispatch_origin,
            token=record.token,
            run_id=(record.payload.get("rvf_run") or {}).get("run_id"),
            status=existing_state.get("status"),
        )
        return payload

    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_shared_workflow_started",
                "status": "started",
                "started_at": started_at,
                "prep_file_path": str(record.path),
                "dispatch_origin": dispatch_origin,
                "prompt_source": prompt_source,
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass

    try:
        # S5 后本块只由 token 派发路径（Stop hook / kanban 注入的 child 到达）到达；manual 已在
        # 上方自成终局。故不再传 manual 内联 scope（inline scope 现由 rvf-agent CLI scope 阶段处理）。
        result_state = _run_shared_workflow(
            record=record,
            user_prompt_excerpt=prompt[:2000] if prompt else None,
            timeout_seconds=shared_workflow_timeout_seconds,
        )
    except Exception as exc:
        result_state = {
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            new_rvf_run = dict(record.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = result_state
            rvf_prep_file.update_prep_file(record, {"rvf_run": new_rvf_run})
        except (OSError, rvf_prep_file.PrepFileError):
            pass

    payload["workflow_started"] = result_state.get("status") == "completed"
    payload["shared_workflow_state"] = result_state
    # prepare 卡在 pre-review local-base bootstrap 门：把等待 gate 写回本进程刚 arm 的
    # in-progress 锁（state → waiting_bootstrap），使 Stop 读侧能区分「等 bootstrap」与
    # 「正在 review」，并在锁 TTL 到期时复活本轮而不是清锁重开。
    waiting_gate = rvf_bootstrap_gate.gate_of(result_state)
    waiting_marker_path = stamp_kanban_followup_bootstrap_gate(
        event, record, result_state, prep_root=prep_root
    )
    if waiting_marker_path:
        payload["kanban_followup_waiting_bootstrap_marker_path"] = waiting_marker_path
    if waiting_gate is not None:
        # 模型可见的 self-injection checkpoint（``additionalContext``，非 user-only 的
        # ``systemMessage``）：本轮 review 还没开始，agent 需要先跑同步 skill 把 local base
        # 吸收进本 worktree、解决冲突、拿到 receipt，之后本轮会自动接续。
        payload["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _bootstrap_waiting_additional_context_text(waiting_gate),
        }
    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_shared_workflow_finished",
                "status": result_state.get("status"),
                "prep_file_path": str(record.path),
                "dispatch_origin": dispatch_origin,
                "error": result_state.get("error"),
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass
    # token 派发路径（child 到达跑 shared workflow）：给用户一条可见 systemMessage。
    # S5 后 manual 已不再落入本块（manual 走 additionalContext 决策点注入并自成终局），
    # 故此处只服务 token 派发，不再产出 manual 的 additionalContext。
    _state_status = result_state.get("status") if isinstance(result_state, dict) else None
    payload["systemMessage"] = _trigger_system_message(
        kind={
            "completed": "success",
            rvf_bootstrap_gate.WAITING_STATUS: "waiting_local_base_bootstrap",
        }.get(str(_state_status), "failed"),
        dispatch_origin=dispatch_origin,
        token=record.token,
        run_id=(record.payload.get("rvf_run") or {}).get("run_id"),
        status=_state_status,
    )
    return payload


def _trigger_system_message(
    *,
    kind: str,
    dispatch_origin: str | None = None,
    token: str | None = None,
    run_id: str | None = None,
    status: str | None = None,
    marker: str | None = None,
    detail: str | None = None,
) -> str:
    """构造一行 **user-facing** 的 RVF 触发状态串（用作 ``systemMessage``）。

    受众语义（已从 Claude Code 2.1.x bundle 字面实证）：``systemMessage`` =
    "Display a message to the user (all hooks)" —— **只对用户可见、不注入模型
    上下文**。因此每个 RVF 触发都用它给用户留一条可见痕迹，而不污染 agent
    的上下文。manual 决策点另用 ``hookSpecificOutput.additionalContext``
    （"Text injected into model context"）把候选摘要 + 决策 token + rvf-agent CLI
    样例命令喂给同会话 agent（见 ``_manual_decision_additional_context_text``）——
    这是「选择性 agent 可见」。

    ``kind``（token 派发路径 = Stop hook / kanban 注入的 child 到达；manual 决策点 = 用户
    手动 ``/review-validate-fix`` 铸决策 token）：
    - ``success`` / ``failed``：**token 派发路径**的 child shared-workflow 就绪 / 未就绪
      （S5 后 manual 不再走 prepare，故不再有「manual prepare 成功」口径）。
    - ``already_completed``：同一 prep 已 RVF 过，本次未重跑（token 派发路径）。
    - ``dispatch_no_prep``：prompt 带 dispatch token 但 prep 缺失 / 过期 / 不可读。
    - ``marker_without_token``：自注入 origin marker 却无 token（不一致态）。
    - ``suppressed_handoff_literal``：检测到字面量但识别为 handoff 正文 / 姊妹命令参数，未启动 review。
    - ``manual_decision_injected``：manual 触发**已铸决策 token，等待 agent 决策**（区分旧「审查已开始」）。
    - ``manual_decision_deduplicated``：manual 触发命中同键活跃决策 token（幂等复用），未重铸。
    - ``manual_decision_no_candidates``：manual 触发但本 worktree 自上次 review 以来无可审候选。
    - ``manual_summary_failed`` / ``manual_mint_anomaly``：manual 决策点摘要构造 / 铸造异常兜底。
      （declined 冷却 / 铸造熔断对 manual 路径不适用，故无对应 kind——见
      :func:`_handle_manual_decision_point`。）
    - ``decision_point_banner_delivered``：带外投递的边界标记**已成为一条真实 user message**
      （INV-A 达成，token 推进 injected，决策正文已随本 hook 以 additionalContext 注入）。
    - ``decision_point_banner_resubmitted_without_body_reinjection``：同一个 token 的标记第二次到达
      （典型是 rewind 后标记被放回输入框再回车），**不再注入正文**。
    - ``decision_point_banner_resubmitted_after_rewind_token_withdrawn``：同上，且 token 还停在
      injected ⇒ 已按用户 rewind 当轮作废（M10）。
    - ``decision_point_banner_arrived_but_body_text_missing``：标记首次到达，但 token payload 里没有
      决策正文 ⇒ 未推进、未注入，投递意图留给对账。
    - ``decision_point_banner_terminal_on_arrival``：一份已达终态（withdrawn / dispatched /
      declined）的横幅迟到落地，未推进任何状态，作废出口已挡下这次提交。
    - ``decision_point_banner_withdrawn_on_arrival_because_agent_resumed_work``：标记到达时判定 agent
      在铸 token 之后已复工 ⇒ 撤回、不注入正文、挡下提交。
    - ``decision_point_banner_arrived_after_agent_resumed_work_but_withdrawal_failed``：同上判定成立，
      但撤回本身失败 ⇒ 不注入正文、意图留给对账。
    - ``decision_point_banner_arrived_but_token_not_advanced``：横幅落地了但锁内首次迁移
      抛了 ``DecisionTokenError``（TTL 已过 / token 缺失 / 状态机拒绝）⇒ **未推进**。与上一条
      刻意分开：那条是「本来就不该推进」，这条是「本该推进但没推成」，善后不同（后者保留投递
      意图交给对账）。两者都**绝不**能借用 ``decision_point_banner_delivered`` 那句断言性文案。
    """
    run_ref = run_id or "—"
    origin = dispatch_origin or "—"
    tok = token or "—"

    def notice(*, status_value: str, message: str, extra_fields: tuple[str, ...] = ()) -> str:
        lines = [
            "=== RVF USER-PROMPT HOOK ===",
            f"RVF UPS: {message}",
            f"status={status_value}",
            f"origin={origin}",
            f"run={run_ref}",
            f"token={tok}",
        ]
        lines.extend(extra_fields)
        return "\n".join(lines)

    detail_fields: tuple[str, ...] = (f"detail={detail}",) if detail else ()

    if kind == "success":
        return notice(status_value=status or "completed", message="派发已就绪")
    if kind == "failed":
        return notice(status_value=status or "—", message="派发已跑但未就绪")
    if kind == "waiting_local_base_bootstrap":
        return notice(
            status_value=status or rvf_bootstrap_gate.WAITING_STATUS,
            message=(
                "本轮 review 尚未开始：task worktree 还没吸收 local base，"
                "已进入可恢复的 bootstrap 等待态（agent 已收到同步指引）"
            ),
            extra_fields=detail_fields,
        )
    if kind == "already_completed":
        return notice(
            status_value="already_completed",
            message="该 prep 已 RVF 过、本次未重跑",
        )
    if kind == "dispatch_no_prep":
        return notice(
            status_value=status or "unavailable",
            message="检测到 dispatch token 但 prep 不可用，未跑",
            extra_fields=("detail=prep 缺失 / 过期 / 不可读",),
        )
    if kind == "marker_without_token":
        return notice(
            status_value="marker_without_token",
            message="自注入 marker 无 token（不一致态），未派发",
            extra_fields=(f"marker={marker or '—'}",),
        )
    if kind == "suppressed_handoff_literal":
        return notice(
            status_value="suppressed_handoff_literal",
            message=(
                "检测到 review-validate-fix 字面量，但识别为 handoff 正文 / 姊妹命令参数，"
                "未启动 review；如需手动 review，请单独发送 $review-validate-fix / /review-validate-fix"
            ),
        )
    if kind == "manual_decision_injected":
        return notice(
            status_value="manual_decision_injected",
            message="已铸决策 token，等待 agent 决策（非自动审查已开始）",
            extra_fields=detail_fields,
        )
    if kind == "manual_decision_deduplicated":
        return notice(
            status_value="manual_decision_deduplicated",
            message="同键活跃决策 token 已在等待，未重铸（幂等），等待 agent 决策",
            extra_fields=detail_fields,
        )
    if kind == "manual_no_candidates":
        return notice(
            status_value="manual_decision_no_candidates",
            message="手动触发但本 worktree 自上次 review 以来无可审候选改动，未铸 token",
            extra_fields=detail_fields,
        )
    if kind == "manual_summary_failed":
        return notice(
            status_value="manual_decision_summary_failed",
            message="手动触发但候选摘要构造失败，未铸 token（best-effort，不阻断）",
            extra_fields=detail_fields,
        )
    if kind == "manual_mint_anomaly":
        return notice(
            status_value="manual_decision_mint_anomaly",
            message="手动触发但决策 token 铸造异常，未注入（best-effort，不阻断）",
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_delivered":
        return notice(
            status_value="decision_point_banner_delivered",
            message=(
                "带外投递的 RVF 发起决策点边界标记已作为一条真实 user message 到达，"
                "决策正文已注入 agent 上下文，决策 token 推进为 injected（等待 agent 显式 start / decline）"
            ),
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_resubmitted_without_body_reinjection":
        return notice(
            status_value=status or "resubmitted",
            message=(
                "RVF 发起决策点边界标记再次到达（常见于 rewind 后标记被放回输入框再提交），"
                "决策正文只在首次到达时注入，本次未注入、未改变任何状态"
            ),
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_resubmitted_after_rewind_token_withdrawn":
        return notice(
            status_value="withdrawn_rewound_by_user",
            message=(
                "RVF 发起决策点边界标记再次到达（rewind 后标记被放回输入框再提交）：决策正文只在首次到达时"
                "注入，本次未注入；这枚 token 已按「用户 rewind 掉了征询」当轮作废（不计冷却、不计熔断），"
                "下一次 Stop 会重新征询一次"
            ),
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_arrived_but_body_text_missing":
        return notice(
            status_value=status or "body_text_missing",
            message=(
                "RVF 发起决策点边界标记已到达，但 token 里没有决策正文，未注入、决策 token "
                "未推进；投递意图**未清除**，留给后续对账处置（到期撤回并重新征询）"
            ),
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_terminal_on_arrival":
        return notice(
            status_value=status or "terminal",
            message=(
                "一份**已作废**的 RVF 发起决策点横幅迟到落地（token 已达终态），"
                "未推进任何状态、未注入决策正文；已挡下这次提交（host 不认 block 时 agent 只会看到「已作废、"
                "无需处理」）；rvf start 会硬失败"
            ),
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_withdrawn_on_arrival_because_agent_resumed_work":
        return notice(
            status_value="withdrawn_agent_resumed_work_before_marker_arrival",
            message=(
                "RVF 发起决策点边界标记到达时，agent 在铸出这枚 token 之后**已经复工**（候选范围与时机都已"
                "过时）：已撤回该 token（不计熔断）、未注入决策正文，并挡下这次提交；这一回合结束时的 Stop "
                "会按当时的改动重新评估"
            ),
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_arrived_after_agent_resumed_work_but_withdrawal_failed":
        return notice(
            status_value="agent_resumed_work_but_withdrawal_failed",
            message=(
                "RVF 发起决策点边界标记到达时 agent 已复工，但撤回该 token **失败**（详见 detail）；"
                "未注入决策正文，投递意图**未清除**，留给后续对账处置"
            ),
            extra_fields=detail_fields,
        )
    if kind == "decision_point_banner_arrived_but_token_not_advanced":
        return notice(
            status_value=status or "not_advanced",
            message=(
                "带外投递的 RVF 发起决策点横幅已作为一条真实 user message 到达，但"
                "**决策 token 未能推进**（TTL 已过 / token 文件缺失 / 状态机拒绝，详见 detail）；"
                "token 仍停在原状态，rvf start 会硬失败。投递意图**未清除**，"
                "留给后续对账处置（到期撤回并重新征询）"
            ),
            extra_fields=detail_fields,
        )
    return notice(status_value=kind, message=kind)


def _render_hook_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """把 inspect 结果折成 hook-mode 要打印的合并 JSON（或 ``None`` = 静默）。

    ``systemMessage``（user-facing —— 每个 RVF 触发都对用户可见、不注入模型
    上下文）与 ``hookSpecificOutput.additionalContext``（model-facing —— manual
    决策点把候选摘要 + 决策 token + rvf-agent CLI 样例命令喂给同会话 agent）可在
    同一 JSON **共存**。改自旧的互斥 elif（旧逻辑里 systemMessage 会顶掉 manual 的
    additionalContext）。二者皆无时返回 ``None``（普通 prompt → 不打印 → 静默）。
    抽成纯函数便于直接单测。
    """
    out: dict[str, Any] = {}
    system_message = result.get("systemMessage")
    if isinstance(system_message, str) and system_message:
        out["continue"] = bool(result.get("continue", True))
        out["systemMessage"] = system_message
    if "hookSpecificOutput" in result:
        out["hookSpecificOutput"] = result["hookSpecificOutput"]
        out.setdefault("continue", True)
    # ``decision: block`` 只由两条硬中和产出：「已撤回的投递迟到落地」与「作废的决策点边界标记落地」。
    # host 支持时直接挡掉这次提交，是三管齐下里最硬的那一管。不支持该键的 host 会忽略它，此时 additionalContext 的
    # 权威禁令与 systemMessage 仍在（故这一管缺席只降级、不失效）。
    if result.get("decision") == "block":
        out["decision"] = "block"
        reason = result.get("reason")
        if isinstance(reason, str) and reason:
            out["reason"] = reason
        out.setdefault("continue", True)
    return out or None


def read_event_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "RVF UserPromptSubmit hook: detect dispatch tokens / origin markers / manual triggers, "
            "and run the shared prepare entry when applicable."
        )
    )
    parser.add_argument("--prep-root", default=None, help="Override RVF prep file root for tests or local diagnostics.")
    parser.add_argument("--now", default=None, help="Override current UTC timestamp for deterministic tests.")
    parser.add_argument(
        "--shared-workflow-timeout-seconds",
        type=float,
        default=60.0,
        help="Hard timeout for in-process shared prepare execution (seconds).",
    )
    parser.add_argument("--json", action="store_true", help="Emit detector result JSON. Actual hook mode stays silent.")
    args = parser.parse_args()

    result = inspect_user_prompt_submit(
        read_event_stdin(),
        prep_root=args.prep_root,
        now=args.now,
        shared_workflow_timeout_seconds=args.shared_workflow_timeout_seconds,
    )
    if args.json:
        result.pop(_RVF_PENDING_MANUAL_INJECTION_RECEIPT, None)
        result.pop(_RVF_PENDING_DECISION_POINT_BANNER_FIRST_INJECTION_HANDOFF_COMPLETED_CREDENTIAL, None)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    out = _render_hook_payload(result)
    if out is not None:
        # print+flush 成功才算 additionalContext 已交给 host；若 stdout 在此破裂，
        # token 保持 minted、ledger 不伪记 injected，后续 manual dedup 可重投；
        # 带外决策点的「首次注入已交付完毕」凭证也不发布（之后的重交不撤回）。
        print(json.dumps(out, ensure_ascii=False), flush=True)
        _confirm_manual_decision_delivery(result, now=args.now)
        _publish_decision_point_banner_first_injection_handoff_completed_credential_after_hook_output_flushed(
            result
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
