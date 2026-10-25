# 72dbd：canonical issue 主路径的冻结范围扩展与补证

## 缺陷与修复

原 stop 按 `issue_paths ∪ fix_allowlist` 判断是否扩展，land 却相对原始冻结合同判断。canonical issue 的主路径因此可以合法 stop/apply，却无法通过 land；再次传入 `--scope-expansion-path` 也不能补全。

本卡将修改权限与扩展记账分开，并让 stop、writer、receipt reader 共用分类逻辑。主路径的 canonical issue 是既有修改授权来源；普通主路径位于冻结范围外时，stop 将其加入 `expanded_paths` 与有效 `declared_paths`，另用 `canonical_issue_declared_paths` 记录来源。其他边界外路径仍须显式声明和理由，guarded 路径、validate-only 与工具托管路径的门禁保持有效。

`declared_paths_outside_attempt_boundary` 保留修改权限外扩的独立路径集合，用于补丁导出、告警和 apply 的 baseline blob 检查。自审中补强的生命周期回归先复现了“已审依赖声明后仍漏入补丁”的中间错误，再验证完整 apply/land 及并发漂移冲突，避免仅修记账而丢失真实修复。

历史 applied 事实通过正式 `check-evidence` / prepare / commit 补证。新增资格字段 `recovered_canonical_issue_scope_expansion_paths` 明确展示补出的路径。补证要求同 run/issue/attempt 身份、固定后已 applied 的结果、prepare 时登记的 issue 路径和实际 patch 同时成立；实际补丁解析包含 rename 两端。工件写入仍使用原 v2 扩展资格 receipt，嵌入原始快照及摘要，不改写原 attempt/result、patch 或 handoff，不扩大冻结合同路径集合。重复运行复用凭据，重启后不依赖 fixer 工作树。

没有添加依工具旧版本切换的兼容分支，也没有修改历史生产 run 来迁就 reader。正常 landing restamp 仍保留原始 reviewed contract。

## 原始样本只读核验

原 run：`rvf-review-start-2af6e3d18859ee08`。本卡使用当前函数重新核算原始工件，得到 4 条 fixed、0 误报、0 backlog；RVF-004 的 5 条路径全部满足扩展资格，补出遗漏的 `web-ui/src/components/board-card-session-activity.ts`。核验前后，全部资格输入的 SHA256 均相同。

原 Task 后来已经人工 land 并吸收其他 main 工作。对其当前工作树运行本卡的公开只读 check-evidence，已越过原 `scope_expansion_invalid`，随后因后续未属于该历史候选的落地 delta 返回 `landed_delta_escaped_reviewed_scope`。这项拒绝被保留，没有把当前树强行认作原历史候选，也没有代操作原 Task。

完整成功路径在隔离仓库重放：真实 prepare/start/stop/apply、正式 land、原工件字节保全、删除 fixer 工作树后的恢复及重复 land 均由自动化验证。详细验证结果与交付状态见本卡持久证据。

## 对等消费与边界

已有新格式 stop 记录直接包含完整 `expanded_paths` 与声明。历史补证 receipt 则保留原始遗漏快照，因此外部 `base-branch-sync` reader 还需要采用同源的主路径判据。旧 reader 的拒绝应保留；不能通过改写快照获得通过。

本卡只修改自己的 worktree；没有修改或部署 `my-ai-setup`，没有 push、合回 main 或安装共享工具。生产消费者是否已经可用，需要按实际安装版本另行核验。

## 持久证据

`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/task-72dbd-canonical-issue-scope-expansion-recovery/` 保存修前失败、修后回归、合同检查、未修改 HEAD 的基线对照和原始 run 的只读复核。

- `before-regression-result.json` / `.log`：修前两个真实主路径用例均因遗漏 expanded path 失败。
- `first-implementation-regression-result.json` / `.log`：83 passed，1 failed；失败为未改动的 SQLite v3 迁移测试缺少 leases 表。
- `unchanged-head-baseline-regression-result.json` / `.log`：在未修改的 `33739f4` 源码快照中复现同一 SQLite 错误。
- `corrected-targeted-regression-result.json` / `.log`：writer、资格、land 与初轮主路径恢复回归 111 passed；后续补强的修改权限用例另以最终回归结果为准。
- `original-run-read-only-qualification-recheck.json`：原始输入摘要、原 patch/facts 摘要和恢复路径。
- `original-run-public-read-only-check-evidence.json`：原 Task 当前树的正式拒绝结果，不冒充 eligible。

共享代码抽取后曾漏导入一个仍被 contract 校验使用的常量，产生了中间轮次的 NameError；该错误已修正，中间日志保留，后续结果单独记录。

## 最终验证与本地交付

- 新增 15 个端到端用例，覆盖新 stop 记录、原事件补证、首次补证前已有提交、重启、幂等、身份与补丁拒绝、修改权限和并发漂移保护。
- writer、资格与 land 的专项回归：111 passed；最终范围/修改权限回归：35 passed、1 deselected；已有提交补证专项：2 passed。各组存在重叠，不累加成测试总数。
- 完整 `bash scripts/check_skill_contracts.sh` 返回 0，共 65 组检查通过，包含最终 land/evidence 回归和真实 RVF 状态根的测试泄漏检查。仓库已有的 `64943` 迁移用例排除保持原样，本卡没有新增排除。
- 直接在任务 worktree 的首轮完整检查，在 21 组通过后，安装测试沿既有 state 符号链接反复复制 1.1 GB 本机历史状态；本卡终止自己的复制进程，保留 exit 143 与原日志，不把中止写成通过或产品失败。
- 随后在同仓派生的 `isolated-contract-validation-worktree` 中覆盖相同源码，隔离既有 state/cache 链接后完成全量检查。原始源码摘要、补充测试/未使用导入清理的输入变更和最终摘要对照均有持久记录；生产 state 未被修改。

本地交付采用源码自审和真实自动验证后的普通 Git 提交，不冒用来源 run 代表本卡完成了独立 RVF。自审见 `main-session-source-self-review.json`；最终提交、分支、工作区状态和验证结果见 `final-local-delivery-result.json`。没有生成虚构的 RVF 完成凭据，也没有 push、合回 main、安装或部署。

最终证据补充：

- `final-frozen-scope-and-modification-authority-regression-result.json` / `.log`：补丁完整性和 apply 漂移保护回归。
- `already-committed-candidate-recovery-regression-result.json` / `.log`：首次补证前已提交与尚未提交的两条恢复路径。
- `isolated-check-skill-contracts-result.json` / `.log` / `-timing.jsonl`：完整检查真实结果与逐组时间。
- `isolated-contract-validation-source-identity.json` 及输入变更记录：相同源码在派生 worktree 中核验的依据。
- `cross-repository-consumer-update-requirements.json`：外部 reader 的同源判据、保留门禁、验收用例与已观察的安装脚本摘要；本卡不宣称它已完成对等更新。
