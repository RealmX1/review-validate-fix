# RVF 手动触发误判修复 + cline-kanban→my-kanban 注入迁移设计

> 状态：Track A（检测器修复）已实现并测试；Track B（注入侧）为**前瞻迁移 keep-in-mind**，本次不写代码。

## 1. 起因（已复现的 bug）

用户提交一段 **RVF land**（`/rvf-land` + 粘贴的 handoff 正文）后，日志出现：

```
RVF UPS：派发已就绪 · origin=post_user_prompt_manual · run=rvf-20260609T124718Z-user-prompt-submit-manual-412bcf4a · status=completed · token=…
```

这不符合预期：`rvf-land` 契约明确「不启动新的 RVF review」，但 UserPromptSubmit hook 走了
`post_user_prompt_manual` 路径——新建 manual prep、跑 `prepare_run` 到 completed，给一个**无关项目**
`~/.cline/worktrees/c46a7/ai-analysis` 完整 bootstrap 了 review packet（含把 `.env` 快照进 run dir），
并向 agent 注入「去 source review env 跑 review」的 additionalContext。

**根因**：`rvf_user_prompt_submit.py` 的 `detect_manual_trigger` 对**整段 prompt**（含粘贴的 handoff
正文）跑 `(?:^|\s)[\$/:]review-validate-fix\b`，命中了正文里行首/空白后的字面量。

这是一类 bug：所有吃 handoff 正文的姊妹 skill（`rvf-land` / `rvf-handoff-intake` / `rvf-reopen` /
`rvf-analyze`）都会被同一检测器误触发。

## 2. 两个关键约束（决定方案形状）

1. **位置锚定不够**：只认行首 `/review-validate-fix` 会引入假阴——用户输入框可能残留内容（例如上一条
   没发出去的草稿）把合法触发顶离行首。修复必须**内容/结构判定、位置无关**。
2. **RVF 没有任何 keystroke 自动化**（全仓 grep `osascript` / `send-keys` / `keystroke` / `cliclick`
   为空）。hook 是 stdin/stdout 子进程，**看不到也碰不到终端输入框**；对 `UserPromptSubmit` 而言
   「提交的 prompt 就是输入框内容」，不存在「另有 leftover」。因此「输入框冲突」只发生在**注入(push)
   侧**，不在检测侧。
3. 出问题的 run 落在 `~/.claude/rvf` → **Claude 会话**，那里 `codex_invoked_skill` 结构化读取不可用。
   故修复**必须靠文本判定**，结构化检测仅作 Codex 侧加成。

## 3. Track A — 检测器修复（已实现）

文件：`plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py`

**保留**现有「任意位置匹配」的 `RVF_MANUAL_TRIGGER_RE`（这样残留前缀的合法触发仍能命中，解决假阴），
新增 `_classify_manual_trigger(event, prompt) -> "manual" | "suppressed" | "none"`：触发字面量在场时，
若命中下列任一抑制信号（全部位置无关）则判 `suppressed`、不启动 review：

1. **前导姊妹命令**（`RVF_SIBLING_TRIGGER_RE`，纯文本，harness 无关）：prompt 开头（容忍前导空白）是
   `[$/:](rvf:|review-validate-fix:)?rvf-<name>`。主 skill 名 `review-validate-fix` 不以 `rvf-` 开头，
   故永不误吞真触发。
2. **粘贴的 handoff 正文**（`_looks_like_handoff_body`，纯文本，捕获「无前导命令的粘贴正文」）：复用
   `rvf_handoff_intake.RVF_RUN_RE`（run id 谓词）+ `parse_sections`（markdown 章节切分，无 IO），当
   prompt 含 RVF run id **且** 含至少一个 handoff 独有章节标题（`RVF_HANDOFF_SECTION_MARKERS`）时判定。
   run-id 与章节标题**叠加**，避免误伤「只顺嘴提了个 run id」的合法 review 请求。
3. **Codex 结构化加成**（`_codex_sibling_skill_invoked`，仅 Codex，best-effort）：rollout
   `text_elements` 显式调用的是姊妹 skill（`rvf-*`）而非主 skill。`try/except` 包裹，永不阻断。

被抑制且本因触发字面量在场时，发一条 **user-facing** `systemMessage`
（`kind="suppressed_handoff_literal"`）：提示「检测到 review-validate-fix 字面量但识别为 handoff 正文 /
姊妹命令参数，未启动 review；如需手动 review，请单独发送 `$review-validate-fix`」。仅对用户可见、不进
模型上下文——这是用户「通知而非自注入」想法里 hook 真能做到的子集。

**导入安全**：`rvf_handoff_intake` 顶层只引 stdlib、无 IO，可安全用于检测热路径；**绝不**调
`build_payload`（它可能跑 git subprocess），只用 `RVF_RUN_RE` + `parse_sections`。缺省时降级为不做
handoff 识别。

测试：`tests/test_review_support_scripts.py::test_rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger`
（已注册进 `review_support_test_cases()`，否则静默不跑）。覆盖：四个姊妹命令 + handoff → 抑制；无前导
命令的纯 handoff 正文 → 抑制；裸触发 → 仍 manual；残留前缀 + 触发 → 仍 manual（假阴守卫）；含 run-id
但无 handoff 章节 → 仍 manual（防过度抑制）。

## 4. Track B — 注入/冲突：前瞻迁移 keep-in-mind（本次不写代码）

> 这是给 **cline-kanban → my-kanban** 迁移的设计契约，**写在这里 + 本任务 commit body**，供日后实现。

三 kanban 代际，勿混淆：**vibe-kanban**（废弃，勿重引）→ **cline-kanban**（当前，`kanban` CLI）→
**my-kanban**（新目标，**独立 sibling repo** `/Users/bominzhang/Documents/GitHub/my-kanban`，含 native
per-task prompt 存储；与前两者互不相同）。

### 迁移契约

- RVF 无 keystroke 自动化、hook 碰不到活输入框；「输入框冲突」只在**有东西要 push trigger** 时存在。
- 把 RVF 被推迟/注入的 dispatch trigger 落到 **my-kanban 的 native per-task prompt 存储**。trigger 停在
  该存储里供用户复核/提交——这就是用户「**粘贴但不按 Enter**」诉求的 durable 实现，RVF 由此**永不触碰活
  输入框**。my-kanban 实现侧「do some magic and put it there」。
- 该 native 存储概念在 my-kanban HEAD `1c1e6c2` 尚未按此名建好（前瞻）。

### 被否决的 stash 路线（记录原因，避免重提）

- **ctrl+s native stash**：会覆盖既有 stash；且输入框为空时 ctrl+s 会把 stash 反弹回填——用户已自否。
- **ctrl+g 外部编辑器 / 读终端尾行**：需不存在的 keystroke 自动化；Claude vs Codex 行为各异；且
  ctrl+g/「自动打开编辑器」正是 Phase A 已用 OS 通知替换掉的旧机制（见 handoff-notification overhaul）。

### 「等待时延长 prep TTL」实现备注

- `rvf_prep_file` 的 `expires_at` 不可变（`PROTECTED_UPDATE_FIELDS`），`DEFAULT_TTL_SECONDS=300`。
- 延长 = 用更长 `ttl_seconds` 同 token 重写（`write_prep_file`）。供 my-kanban 存储实现时参考：trigger
  停在 prompt 存储等待用户确认期间，prep 不应按默认 5 分钟过期。

### 同线原则

与 handoff-notification overhaul Phase B（cline-kanban 带按钮 toast）同一原则：**不自动打开 / 不自动
注入**，改为投递到 durable、用户可控的位置。

## 5. Out of scope（现在不做）

- 构建 my-kanban 的 native prompt 存储（独立 repo；前瞻）。
- 清扫误触发遗留的 stray run `rvf-20260609T124718Z-user-prompt-submit-manual-412bcf4a`（可选）。
