# Reviewer 额度耗尽检测 + cooldown 自动回退实现日志

日期：2026-06-22

设计稿：`~/.claude/plans/recent-rbf-runs-have-rippling-horizon.md`（评审后定稿，含 D1–D4 / R-a–R-e / F1–F2 / O1–O4 折叠项与两项用户决策）。
Kanban worktree：`/Users/bominzhang/.cline/worktrees/98331/review-validate-fix`。

## 目标

最近多轮 RVF run 反复失败：被选中的 **Codex reviewer 用完额度（usage limit / 订阅配额耗尽）**，每次需人工换备援重跑。
根因——dispatcher 的可用性判定**只看 auth、不看额度**：`probe_available()` 跑 `--preflight`（本质 `codex login status`），
额度耗尽时仍返回 0 → codex 始终进 `available` → `route()` 选中 → `codex exec` 真跑才撞额度失败 → 无自动备援、跨轮反复复发。

目标：dispatcher 能识别「某 harness / 其背后订阅是否还有额度做评审」，识别到耗尽则**自动回退**，回退**复用现有 `available`-集 + `route()` 机制**
（与 cursor R4 缺席降级同构），不新增「派发前主动查额度」探测。

## 已确认决策

1. **双层回退**：① 轮内 reroute（撞额度那路当场换备援补上，保持恰好两路）；② 跨轮 cooldown（`~/.rvf` 记 TTL，后续轮 probe 跳过）。
2. **仅失败签名检测**：只在 reviewer 真跑挂时从输出识别额度签名；不新增主动探测。
3. **Cooldown TTL = 默认 1h + 解析 reset hint**（用户决策）：能从错误文本解析到 provider 重置提示则以它记 `expires_at`，否则回落 1h；env `RVF_HARNESS_LIMIT_COOLDOWN_TTL_SECONDS` 覆盖默认。
4. **主 harness 盲区 = fail-close + 响亮信号**（用户决策）：external 补不上且主/兜底 harness 自身在 cooldown / 刚撞额度时，明确 fail-close 并发响亮 warning，**不**伪装 R3 in-harness mimic。

## web 查证（实现前先抓真相，落实 D1/D4）

`codex exec --json` 输出 JSONL，额度耗尽时**最可靠信号是 `turn.failed`（带 error details）或顶层 `error` 事件**，文案形如 "You've hit your usage limit."。
**关键陷阱**：exec 模式下 `rate_limits` 字段恒为 `null`（API 不为 exec session 下发 `x-codex-*` headers）→ 绝不能靠 `token_count`/`turn.completed` 的 rate_limits 判额度，只能靠 error 事件文案签名。
来源：openai/codex issue #14728、#12299，及 Codex non-interactive 文档。

## GitNexus impact（编辑前，CLAUDE.md 强制）

`execute_plan` / `probe_available` / `route` / `extract_codex_json_result` 四个目标符号 upstream impact 均 **LOW / epistemic: exact**：
每个仅被本文件 `main()` 直接调用、无跨模块外部调用方；改动又是 additive（新 `fallbacks` key、新函数、新模块）→ blast radius 受控，无 HIGH/CRITICAL。
（索引覆盖的是 sibling worktree c45d2 / Documents-GitHub，本 worktree 98331 未单独索引；同仓库 worktree call-graph 结构一致，结果做方向性参考。）

## 实现切片（文件级）

- **新模块 `scripts/harness_limit_cooldown.py`**：`~/.rvf/harness-limit-cooldown/` 下带 TTL 的 best-effort 冷却标记
  （env `RVF_HARNESS_LIMIT_COOLDOWN_ROOT` 覆盖；env 路径视作「直接就是 cooldown 目录」、显式 `root=` 参数则追加 SUBDIR）。
  `DEFAULT_TTL_SECONDS=3600`；`parse_reset_hint()` 解析 "try again in 4h" / "retry in 30 minutes" / "Retry-After: 120" / "resets at <ISO>"
  （夹到 [60s, 7d]）；`record()` 取 `reset_hint or ttl or 默认`、重复命中取更晚 expires（延长不缩短）、原子 tmp+replace last-writer-wins；
  `active()` / `active_harnesses()` 读前 lazy `sweep_expired()`。**不**抄 kanban lock 的 O_EXCL/takeover 重型机制（hint 非 mutex）。
- **`scripts/run_alternative_reviewer.py`（检测）**：
  - 常量 `EXTERNAL_REVIEWER_USAGE_LIMIT_EXIT_CODE=125`、`EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG`、收紧的 `USAGE_LIMIT_SIGNATURES`（D4：去裸 `429`，需 `http/status 429` 或与 `too many requests` 共现）。
  - `looks_like_usage_limit(text, *, signatures=None)`（返回命中短语或 None）。
  - `extract_codex_json_result`（D1）：识别 `error`/`turn.failed`/`thread.error` 事件（顶层 / `event_msg` / `item.completed` 嵌套），命中额度签名**立即 raise** `CodexJsonOutputError("usage_limit_exhausted", …)`，优先于任何已累积 assistant 文本；非额度类错误不 raise。
  - `main()` 收尾（D2/D3）：先记 `subprocess_returncode`（翻码前）；**文本兜底扫描只在 `subprocess_returncode != 0 and output_error_reason is None and not timed_out` 时**触发（绑 raw stderr/stdout、绝不绑 normalize 后正文；天然排除「returncode 0 → review-result 校验失败翻成 1」路径）；命中后统一规整为退出码 125 + flag + summary `output_error_reason`/`output_error_message`，并加 `reviewer_usage_limit_exhausted` reason_code 分支。
- **`scripts/dispatch_reviewers.py`（回退）**：
  - `route()` plan 加 `"fallbacks": []`（additive，schema_version 仍 1）。
  - `probe_available(cooldown_active=...)`：真实 probe 跳过冷却中的 harness（`assume_available` 路径不应用，D-O4）。
  - `execute_plan(registry, main_harness, available, max_fallback_rounds=2)`（reactive）：对「125 + flag/summary 佐证」（D3 双条件、`_leg_usage_limit`）的每路 ① 记 cooldown（带解析出的 reset hint）；② 对 `A' = available − cooled` 重调 `route()` 取替换候选（R-c 单源、`_reroute_candidates`）；③ in-place 替换失败 slot、`-fb<slot>` 后缀 + `_dedupe_reviewer_id` 防碰撞（R-a/R-b）、保留失败 leg artifact、记 `fallbacks`；④ bounded 多轮（替换也 125 则再来，R-d）；⑤ 补不上 → fail-close（缺一条合法 leg 即判 `status: failed`）+ `main_harness_usage_limit_exhausted` / `all_reviewers_usage_limit_exhausted`（F1/F2/R-e），**绝不**置伪 `needs_last_resort_fallback`。旧调用方不传 registry/main_harness 时退化为「无 reroute」。
  - `main()`：真实 probe 前读 `active_harnesses()` 传入 probe；route 后对被冷却的 enabled harness 发 `harness_limit_cooldown_active` warning；A 全被 cooldown 清空 → `all_harnesses_usage_limited` error fail-close（O1：`dispatch_executed` event 加 `fallbacks` / `cooldown_recorded`）。
- **契约/文档（O2/O3）**：`scripts/check_skill_contracts.sh` 字面量纳入 `harness_limit_cooldown_active` / `main_harness_usage_limit_exhausted` / `all_harnesses_usage_limited` / `fallbacks` / 新模块函数 / `usage_limit_exhausted` / `looks_like_usage_limit`；`review-merge-policy.md` 新增「额度耗尽检测 + 自动回退」节，说明 `fallbacks` 语义与 fail-close 不伪装 R3。
- **测试（注册进 `review_support_test_cases()`，规避未注册静默不跑陷阱）**：9 个新用例 + `plan_artifact_schema` 加 `fallbacks` 断言：codex_json error 检测 / stderr 文本检测 / 两个 no-false-positive（成功正文含 rate limit、rc0→invalid-result 含 429）/ cooldown 单元（record/active/sweep/parse_reset_hint）/ reroute / id-collision(-fb1/-fb2) / probe 排除 cooldown（子进程真实 probe + warning）/ fail-close 主 harness 耗尽。

## 设计要点与坑

- **D2 守卫是防误报核心**：用「翻码前的 `subprocess_returncode != 0`」作为文本扫描闸，恰好把「模型评审正文（可能讨论 429/rate limit）经 returncode-0→invalid 翻成 1」的路径挡在外面——这正是 `..._no_false_positive_invalid_result` 回归用例所验证的。
- **125 + flag 双条件**对齐 timeout 的 124 + flag 先例：dispatch 不只看裸 125，须叠加 stderr flag 或 summary `output_error_reason` 佐证（`reviewer.summary.json` 为 unique 命名，dispatch 按 glob 取 mtime 最新一份）。
- **reset hint 数据流解耦**：`run_alternative_reviewer` 只把错误文案 snippet 写进 summary `output_error_message`，由 dispatch 调 `parse_reset_hint` 解析——`run_alternative_reviewer` 不依赖 cooldown 模块。
- **env vs 显式 root 的 SUBDIR 差异**（沿用 kanban lock 约定）：execute_plan/子进程经 env 读写（env 路径不加 SUBDIR），单元测试用显式 `root=`（加 SUBDIR）；测试以 `_with_cooldown_env` 上下文把 env 指到 tmp 并在退出还原，避免污染真实 `~/.rvf`。

## 验证

- `bash scripts/check_skill_contracts.sh` → 契约检查通过。
- 9 个新用例 + `plan_artifact_schema` + 5 个既有 dispatch/routing 用例：全绿、无回归。
- 全量 `python3 tests/test_review_support_scripts.py` 结果见 commit 说明。

## AGENTS.md 合规

纯特性，无通过改动主程序达成的 backward-compatibility 临时改动；新参数（execute_plan 的 registry/main_harness/available）以 optional 默认 None 退化兼容旧调用方，属正常 API 演进而非临时桥接，无需 `dev_backward_compatibility/` 记录。
