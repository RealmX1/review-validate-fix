# RVF 审计问题在吸收本地 main 后的复核

本轮依据用户要求，吸收本地 `main` 后复核原六项审计并修复仍存在的问题。固定吸收点为 `1ca058ed42d38833926a352826e2049156a2322e`，整合提交为 `a3d5ef3707d28304630298e8d6f296ebb09caaba`。主仓库 WIP 保持原样；本轮不交回 base、不推送、不部署。

| 原问题 | 当前结论 | 本轮处置 |
| --- | --- | --- |
| 1. Codex 安装目标与 Kanban 独立 home | 当前会话的配置 home 独立，但 `plugins` 与全局 home 指向同一物理目录，不能据此认定插件缓存装错。 | 保留 Kanban 的配置隔离设计，不改全局配置及默认传播策略。 |
| 2. 明确撤销后另开 OS session 的后代继续执行 | main 已补齐所属后代的观察和撤销，并区分前台等待中断与明确取消。 | 复验现有进程回归；整合 skill 中用户要求的 `continue` 前核验、接续及安全重放规则。 |
| 3. 脱敏递归豁免通用字段 | `name/type/status` 等字段的通用豁免仍存在，不能将其记作已修复。 | 按用户此前安排暂缓，不扩大本轮实现范围。 |
| 4. 同名 run 的 Kanban 登记碰撞 | 仍能复现：同一 task 内两个不同父目录下的 `automatic-review` 共享 basename 幂等键，后者被拒绝。 | 登记键使用规范化精确 run 目录的摘要；同目录重试、软链别名复用同一执行，不修改冻结 `run_id` 或历史 run。 |
| 5. merge 时新增任务文件漏提醒 | 仍能复现：旧任务内容撤回，仅保留 merge 自身新增文件时，默认 `diff-tree` 漏掉该路径。 | 使用 combined merge diff 发现相对所有父提交都改变的路径；保持现有完整净 diff 和 rename 跟踪，不把单纯导入的 base 文件算作任务新增。 |
| 6. Kimi external agent 与主会话 hook | 两个独立 Task 的提交均已进入固定 main。external 派发不再校验未执行的 health command；健康配置已有账户资格模块消费者。hook 已修复原生 prompt 内容块及同文本新用户消息的补证身份。 | 复用 `02a84`、`4beef` 的实现和原生证据，并跑当前源码相关回归；不重复开发，也不把历史加载未知或模型自主选择发起 RVF 写成已证明。 |

## 两项修复的边界

Kanban 的幂等键以 task 为作用域，同一目录的重试必须稳定，不同目录则必须独立。因此在所属执行适配器入口统一规范化目录，再生成登记键；不增加全局会话注册表、配置开关或旧版本迁移分支。

merge 检测仅用于决定是否存在需要提醒的任务路径。combined diff 不将只来自某个父提交的导入内容认作 merge 自身的任务改动。实际送审材料继续使用固定基线到候选的完整净 diff；文件、内容和任务绑定门禁保持有效。

## 验证与证据

新增回归在修复前为 `3 failed, 1 passed`，修复后为 `4 passed`。最初测试把纯 base 合并的真实 `unknown` 误写成必须 `absent`；已修正为只要求不认领任务改动并保持 hook 静默，原失败记录仍保留。

新增测试覆盖不同目录同名 run、同一 run 重试和软链别名、merge 自身新增文件触发提醒，以及纯 base 导入保持静默。现有回归另外覆盖 Kimi reviewer/fixer 正式派发、hook 宿主输出、固定起点、完成去重和显式撤销 detached 后代。

上述相关回归在本轮整合后的源码上共 `129 passed in 91.83s`；插件源码与入口契约检查通过。

原生 Kimi 证据见 [Kimi 主会话 hook 验证](2026-10-29-kimi-native-main-session-hook-input.md)。该证据证明上下文送达同一原生主会话，不证明真实外部模型一定选择发起 RVF，也不代表本轮部署了安装副本。

本 Task 的命令原件、同步故障恢复、前后回归与最终评审保存在持久交接目录：

`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/review-validate-fix/43e9b/2026-11-05-local-main-audit-remediation/`

同仓任务盘点的实时 `task list` 因超时降级为持久看板状态，采集 244 个同仓 Task；已知 Kimi Task 另用 `task get` 实时核对，均无未合入的实现。本轮没有改动或合并任何 sibling worktree。
