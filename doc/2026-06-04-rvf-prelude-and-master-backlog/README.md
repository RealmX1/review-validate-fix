# RVF Prelude 复核 + 全项目 Master Backlog 重排

本目录是一次综合分析的产出：(1) 复核 RVF "prelude"（dispatch 后、派 reviewer 前的准备步骤）里的 token/时间浪费，正面回答「哪些可程序化、哪些是不可替代 reasoning」；(2) 把这些发现并入全项目既有/新增 backlog，统一重排优先级（范围 B）。

- **分析基准 commit**：local `main` `75ef235`（2026-06-04）
- **生成日期**：2026-06-07
- **范围**：B（全项目 master backlog 重排）
- **性质**：纯文档，不改动代码；所有条目留待后续单独执行

## 文件索引

| 文件 | 内容 |
|---|---|
| [`prelude-waste-analysis.md`](prelude-waste-analysis.md) | 分析正文：main 工作核对 + 旧结论复核 + 4 项新发现 + 与 backlog 对齐 + **每轮固定浪费量级排序**（最大单项=451 行 review-env.sh）+ **不可程序化 reasoning 侧** |
| [`master-backlog.md`](master-backlog.md) | 全项目 master backlog 总表（T0–T4，6 列）+ 验证手段小节 + 已 Done 对账 + 新发现/归并对照 |

## 数据来源 doc（main `75ef235`）

- `docs/global-tracker-finishing-handoff.md` — tracker/lease 收尾（5 slice，#1 landed / #2 partial / #3–5 remaining）
- `docs/workflow-plugin-design-system.md` — Gap 1–5 / Slice A–E
- `docs/rvf-dispatch-flow-overhaul-plan.md` — dispatch/prep（Slice A–I 已 land）
- `docs/agent-codebase-navigation-infrastructure-review.md` — 导航基础设施现状（2×P0 + 5×P1 + 3×P2 + 1×P3）【`84f1617` 新增】
- `docs/log/2026-05-28-claude-code-cross-harness-adaptation-handoff.md` — cross-harness 缺口 A1/A2/B/C + ✅G【`84f1617` 新增】
- `docs/log/2026-05-30-cross-harness-plugin-S0-S4-implementation-log.md` — A1/A2/B/C 闭合对照表（§3），证明上述缺口已 Done

## 关键结论速览

- prelude 旧分析主轴整体仍成立；两点修正：「agent 过度自证」是 run-variable 单次行为（非每轮固定成本）；触发器过宽限定到 Claude/正则回退路径。
- **最大单项固定浪费 = review-env.sh 451 行载体错配**（修复路径 = Gap1 结构化 env sidecar，T1）。
- **唯一正确性 bug = Cline Kanban task prompt 未闭合代码块围栏**（T0 首项止血）。
- cross-harness A1/A2/B/C 经对账**已全部闭合**，移出活跃 backlog。

## 后续（未执行，待用户确认）

- 候选迁移（仅建议，**不擅自动 `docs/` 正式文档**）：把 `doc/phase-b-deferred/workspace-diff-tracker-integration-…md` 收编进同一 dated 约定；或为 `docs/` 既有 plan/report 引入索引（呼应 master-backlog 里 nav-index T4 项）。
