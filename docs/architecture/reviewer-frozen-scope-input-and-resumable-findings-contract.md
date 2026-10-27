# Reviewer 冻结范围输入与可恢复结果契约

本任务按批准计划收敛 reviewer 协议，保留候选枚举、`review_highwater`、`round_baseline`、双审选路及额度池规则。真实缺陷评测机制由独立 Backlog 任务 `98bed` 研究。

## 正式 Scope 与输入

分配器记录 unit 的实际来源，分别保留“历史基线 → 原 HEAD”及“原 HEAD → dirty 候选”的 hunk 内容、blob 身份和摘要。分配和渲染使用相同内容身份；不能用相同 hunk 行号替代内容核验，也不能在基线较早时退化为整个文件的累计 diff。

工作区原始内容摘要单独用于枚举后的漂移核验；候选内容身份和 blob 摘要使用 Git 按路径应用 clean/行尾规范化后的内容。符号链接以链接文本为原始内容和 Git blob，不解引用目标，也不应用普通文件的 clean filter。两类摘要不能混用，正式候选仍须核对冻结 blob。

packet 正文只呈现分配内容一次。全仓历史 diff、完整 blob 和机器审计按需读取；新增文件不重复内联。生成物、二进制及超大源码仍在原 Scope 内，已知生成关系仅帮助 reviewer 选择查证方法。

冻结索引按内容摘要命名，绑定候选、Scope、lease 和 unit。索引、patch、前后 blob 都保存摘要；重渲保存新索引，旧索引继续可核对。投影恢复失败时保留原证据及真实缺口，不能用扩大范围掩盖缺证据。

最终 prompt 默认预算为 **128 KiB**，包括模板、环境说明与代码。超限自动改成简短入口和文件引用。`read_frozen_review_evidence.py` 默认每页读取 **32 KiB 原始字节**，返回字节 offset、总长、下一页位置及 base64，支持超长单行和二进制。读取时核对摘要及候选身份。

## 发现、补证与完成

保留 `issues`、`no_issues`、`request` 三种结果。`kind=request` 可同时携带已发现的 `issues[]`；追加 issue 不解决请求，也不把该腿变成完成态。

请求内容身份与进度分开，重复请求及 issue 不重复累计。`respond-request` 持久保存响应，reviewer 处理后以 `resolve-request` 记录解决依据；所有请求解决后用 `complete` 完成评审。完成结果保留 findings 和已解决请求历史，禁止把已记录的问题改成 clean。误报由 validate/fix 判定。

完成事实共同要求：合法 canonical 结果、正常执行退出、无待解决请求、冻结证据可核验。展示端同时呈现发现与补证；merge、candidate qualification、finalize、tracker 完成及高水位推进均不能把部分 findings 当作双审完成。

## 普通入口恢复

`rvf redispatch-reviewers` 或绑定同一 run 的普通 `dispatch_reviewers.py --execute` 自动选择未完成腿。完成腿保留原结果；同腿 canonical 不被移走，尝试前另存检查点。每腿退出后持久记录执行结果，进程重启后复用精确计划及输入绑定。

执行锁由 dispatcher、supervisor 和真实 reviewer 继承。强杀父进程时，只要真实 reviewer 仍在途就继续持锁；新的入口保存 `dispatch_execution_lock_available` 等待条件，不重复派发。原尝试正常结束后，可凭绑定尝试身份、run、候选输入和结果摘要的持久回执补齐未写回的 plan；不能用最新文件时间或父进程消失推断完成。

runner 自动查找可读取材料、标准子集和既有命令锁。请求通过 `--target` 指定确切文件时，保存可重验的 `file_readable` 条件及内容摘要；锁争用保存 `command_lock_available`，使用内核锁事实重验，不删除他人锁。默认有界等待 5 秒，`RVF_REVIEWER_REQUEST_CONDITION_WAIT_SECONDS` 可设为 0–30 秒；条件出现后在当前 runner 内接续，超出窗口则由普通恢复入口再次重验。

`recovery` 保存条件、已尝试动作和次数。同一条件最多自动重试一次；输入超限改用引用，从同腿持久检查点启动新尝试，仍失败沿既有替补路径处理。条件或执行身份变化后重新取得恢复资格。锁和执行计划防止重复派发；每次恢复重验 Stop、授权、候选绑定及在途执行。只有新权限、真实价值取舍或人独有信息进入人工决策。

## 健康探针复用

选路与正式派发使用同一缓存，指纹包含实际解析的模型、有效配置、执行身份、CLI 身份和探针模式。默认成功 TTL 仍为 1800 秒，失败 TTL 仍为 300 秒。动态选模改变时失效；恢复时只预检相关未完成腿，替补确有需要时才扩展预检。

健康检查返回结构化原因。额度预检写入原 cooldown；输入超限、探针在途或探针进程证据缺失保持待恢复状态，不能当作 harness 不可用或直接启动兜底。ledger 记录缓存命中及失效原因、输入字节与恢复次数。诊断遵守现有 `developer_only` 契约。

有效的额度缓存可以补齐丢失的 cooldown，恢复时沿用原观察时点及到期时间，缓存命中不会延长冷却。provider 的重置条件到期后，该证据失效并重新预检；成功、失败 TTL 的默认值保持不变。

| 状态 | dispatcher 退出码 | 后续行为 |
| --- | ---: | --- |
| `completed` | 0 | 继续现有合并流程 |
| `failed` | 1 | 保留失败与部分发现，沿现有替补或恢复路径处理 |
| `blocked_on_reviewer_request` | 3 | 等待补证或 reviewer 确认处理 |
| `waiting_for_reviewer_probe_evidence` | 5 | 等待机器证据条件变化，普通入口重验 |

## 验证入口

- `tests/test_reviewer_frozen_evidence_and_bounded_input.py`：混合来源、同坐标不同内容、预算、字节分页及生成关系。
- `tests/test_reviewer_findings_with_pending_requests.py`：发现与补证共存、显式解决、证据缺口不隐藏发现。
- `tests/test_reviewer_ordinary_resume_end_to_end.py`：真实隔离 CLI 的中断、跨进程续跑、重复入口、自动条件等待、Stop 和一次输入恢复。
- `tests/test_reviewer_shared_health_probe.py`：预检与正式派发的实际模型调用计数、动态身份、额度及输入原因。
- `scripts/replay_frozen_review_packet_scope_and_prompt_budget.py`：只读重放固定历史包，核对原始摘要、全部分配 unit、证据文件及最终输入预算。

三份实际重放的原包分别约 1.77、4.51、5.19 MB，全部 226 个分配 unit 保留可读取证据；最终 prompt 均在 128 KiB 内。具体输出和插件契约检查记录保存在本任务持久交接目录。
