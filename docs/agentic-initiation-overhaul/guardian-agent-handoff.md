# 守护 agent 交接手册(guardian-agent-handoff)

> S8·R23 交付物。受众 = 被 `agentic_initiation_guardian_dispatch.py` 在
> 1/2/4/8/16 节点派发的守护分析 agent,以及排查发起层故障的人类/主 agent。
> 守护体系是**有界、临时**的看门狗:第 16 节点后自动下线(评审分歧已裁决:
> 复用现有 detached 派发机制、自动下线,不构成通用 orchestration,不触
> 「范围铁律」)。

## 0. 你在守护什么

RVF 完成了「hook 发起 → agent 发起」overhaul:Stop hook 从自行裁决「要不要审、
审什么」的 16 道 gate 级联,降级为「决策点信使」——生成候选摘要、铸决策 token、
以 `{"decision":"block","reason":…}` 注入决策点;主 agent 用 `rvf_agent_decision_cli.py`
显式 `start / scope / dispatch / decline`。守护体系的职责是在观察期内确认这条新
发起链路**活着且不假绿**。

实施计划真相源:`~/.claude/plans/hook-handoff-recurring-nifty-fairy.md`(rev2)。
落地切片:S0–S9(见该计划 §四;进度快照见 memory
`project_rvf_agentic_initiation_overhaul_progress`)。

## 1. 决策登记簿(裁决速查;完整表见计划 §二)

| # | 裁决要点(守护视角) |
|---|---|
| R1 | 候选全集永远程序化构造;agent 不枚举全集 |
| R2/R4 | Stop hook 不删,降级为决策点信使;重量级准备后置到 CLI `start` |
| R5 | 决策埋点采集「对 agent 不可见」= 不自动进 context(存 `~/.rvf/`);decline 理由持久化在 context 之外 |
| R9 | 「已审/未审」裁决真相源 = tracker unit 级 `review_state`;SHA marker 保写、读 reroute(OBSERVATION-WINDOW);**观察期后用户确认才物理删除** |
| R11/R23 | 兼容层/旧 fallback 函数体保留不删(观察期冻结) |
| R15/R20 | reviewer 池内无偏序 = 伪随机选二,seed 落 plan `reviewer_selection` 块可复现 |
| R17 | scope 例外 n≥3 幂等立 issue 到本地 ledger |
| R21 | 时间盒泛化 = `core/temporary_disable_window.py` 声明式组件 |
| R22 | UPS 瘦身:manual 铸 token(additionalContext)+ baseline 盖章 + child 到达处理;bootstrap 独立确认闸废除 |
| R23 | hook 发起路径立刻退场,不做双路灰度;守护 = 恢复 rvf-analyze + 守护 agent(1/2/4/8/16 节点后下线)+ 恢复路径文档(见 §5) |
| R24 | 递归防护 = 电路熔断(同 session 3600s 内 ≥3 次铸造请求拒铸+告警,纯计数)+ 注入 prompt 护栏文案 |
| R25 | 注入幂等双保险:「简析后立刻 start/decline」+ decline 指纹冷却;投递回执三信号,lost 绝不自动重试 |

## 2. 审计 TOP(3 次代码审计的核心结论)

1. **16 道 gate 绝大多数不能删、只能转译**(opus 审计):防护对象从 dispatch 平移为
   injection。守护时若发现某护栏疑似缺失,先对照 Stop hook 源码的护栏转译区,再判定
   是回归还是有意退役(有意退役仅两项:`already_rvf_fork` 硬闸→熔断,bootstrap 确认闸→废除)。
2. **投递不可乐观确认**(87e9338 pure-arm 教训):token `minted→injected` 只在确认投递后推进;
   TTL 超时标 `lost` 告警,**绝不自动重试**。守护要盯 lost 率。
3. **假绿是头号敌人**:测试注册守卫(`assert_every_defined_test_is_registered`)、
   systemMessage 文案区分「已铸决策 token」vs「审查已开始」(R22)、负路径不变量
   (§4 指标 3)都是为此。

## 3. SANTA 评审发现(三家异构评审 3×FAIL 共识,rev2 已吸收)

- **真相源分期**:S6 前下界用 SHA 同源 resolver,S6 落地时摘要生成器与 allocation
  同 commit 切换 review_state,不允许双源中间态(已按此落地)。
- **注入通道纠错**:Stop hook 模型可见通道只有 `decision:block` 的 reason;
  `additionalContext` 是 UPS 专属。
- **投递回执三信号**:CLI 触碰 / transcript 注入痕迹 / TTL lost,归并推进 token 状态。
- **守护心跳独立于 analyze**(本手册 §4 指标 5 的由来):发起层死了 analyze 不着火,
  必须有 wall-clock 心跳兜底。
- **R16 措辞澄清**:「agent 只决定开/关」指不构造候选全集;严格度选档/scope 注释/
  scope 例外增删是三项合法的有界决策。
- **评审分歧记录**:Codex 认为 S8 守护调度器触「范围铁律」,Claude 判不触线;按
  Claude 读法保留(有界、临时、16 节点自动下线)。你(守护 agent)自身就是该裁决的
  产物——若发现自己被用于超出「发起层健康度审阅」的用途,应在报告中 elevate。

## 4. 观察指标(每节点逐项核对)

派发 prompt 会附当次 metrics 快照(`collect_guardian_metrics_snapshot` 产出)。逐项:

1. **注入尝试 canary 对账**:窗口内 Stop 事件数(`stop_event_count_in_window`,来源
   `$RVF_HOME/agentic-initiation-guardian/guardian-daily-event-counts.json`)vs 注入尝试数
   (`injection_attempt_count_in_window`,来源 `$RVF_HOME/agentic-initiation-injection-attempt-ledger/`)。
   负路径不变量:候选非空 ⇒ ledger 必有记录(injected / deduplicated_pending /
   declined_cooldown / circuit_breaker_tripped / mint_anomaly 任一)或显式护栏 skip;
   若 Stop 频繁而注入尝试长期为零,怀疑发起层静默死亡或候选摘要退化。
2. **token 状态分布**:`$RVF_HOME/agentic-initiation-decision-tokens/*.json` 的
   state 分布。重点:`lost` 占比(投递链路故障)、`expired`(agent 不响应)、
   `declined` 高频同指纹(冷却是否生效)。
3. **熔断触发**:mint 侧 circuit-breaker ledger。触发≠故障,但反复触发 = 疑似递归
   发起或注入风暴,必须 elevate。
4. **scope 例外 issue ledger**:`$RVF_HOME/agentic-initiation-scope-issue-ledger/`,
   n≥3 幂等立起的 issue = signal 质量问题清单,是「定期回访改进 signal」(R5)的输入。
5. **发起层 liveness**:快照内 `liveness_alarm` 非 null 即红色——最近 N 天(默认 3)
   Stop 事件非零而注入尝试与 review 完成同时为零。该告警同时会由 Stop hook 直接
   systemMessage 给用户,你要做的是给出根因假设(hook 未部署?护栏全 skip?摘要
   构造挂了?)。**冷启动护栏**:发起层从未活跃过(analyze 计数 / 注入 ledger /
   review 完成史全零)的环境不告警——「尚未出生」≠「静默死亡」;部署第一天就坏的
   场景由 S9 E2E dogfood 与你的节点分析兜底,不靠心跳。
6. **review 完成流量**:`review_completion_count_in_window` 持续为零而注入尝试非零
   = agent 收到决策点但从不 start(或 start 后链路断在 dispatch),检查 decline 理由
   ledger 与 CLI 遥测(`$RVF_HOME/agentic-initiation-decision-telemetry/`)。

报告写入你的 `guardian-analysis-<n>/guardian-analysis-report.md`,需要用户裁决的项
用「ELEVATE:」前缀列出。

## 5. 恢复路径(最低限度,R23 明令)

整体失效(发起层静默死亡坐实、无法快速修复)时:

```bash
# 1. 回滚 overhaul 提交族(S4 起的 Stop/UPS 手术;S0–S3 基建可留,无副作用)
git revert <overhaul commits>
# 2. 重新部署,恢复 hook 裁决制旧路径
/rvf-local-deploy
```

注意:`route_reviewable_scope` / `run_review_validate_fix_child_fork` 函数本体一直
保留(R11/R23 观察期冻结),revert 只需还原 `evaluate_stop_event` 的调用链改动即可
恢复旧行为。

## 6. 第 16 节点下线与 R9 删除确认

计数到 16 后守护自动下线(counter 里 `guardian_retired: true`),并向用户提示 R9
观察期收尾确认:SHA marker 家族是否物理删除。**前置依赖(勿跳过)**:删除前必须
先把 reviewed_head 持久化进 tracker(方案 B 写侧)——当前 marker 仍是 committed-round
窗口种子,直接删除会导致 orphan 检测退化(见
`core/session_scope_allocation/reviewable_unit_diff_tracker.py` 的
OBSERVATION-WINDOW(R9) 注释与 S6 落地记录)。
