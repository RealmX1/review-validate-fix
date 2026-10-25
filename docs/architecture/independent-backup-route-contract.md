# 独立备用评审与收尾路线

本实现消费 task 73564 在 2026-10-25 获准的实施计划。正常 RVF 的质量／授权义务继续有效；备用路线将派发、结果 writer/checker、validation wrapper、land 登记与业务事实分开。它不签发正常 RVF receipt，不更改原失败，也不承担 Monitoring→Issue 的分类／计数／建卡核心。

## 实现合同

- 能力：正常基础设施失效后，由执行 Agent 以原始双评审、问题处置、真实验证和 Git 事实继续已授权交付。
- 范围：RVF／land／同步 Skill 的自包含正文；候选快照、持久阶段材料；正常写入接管协作；Kanban 自动收尾抑制观察。
- 非目标：第二套自动 reviewer 引擎、新的资格 schema、自动部署／push／Done、修改原 token 或 receipt、代替用户的价值取舍。
- 验收：同输入并行新上下文双评审；按缺失阶段接续；接管互斥；结果未知先对账；崩溃后不重复提交；共同 base-ref 锁和 WIP 保护；渲染／分类服务不可用不影响已交付正文。

## 互斥的两个顺序

正常写入先原子落盘 `normal-mutations/<id>.json`，然后检查 `active-owner` 和已消费原 run。检查通过才进入副作用区间。备用方先原子发布 `active-owner`，然后等待既有 reservation 清空。于是正常写入要么先登记、被备用方等待，要么后检查到接管、根本不执行。

完整身份预先写入 Git 元数据内的独立 generation，再以不可覆盖的 symlink 原子发布。空目录、损坏记录或 dangling symlink 同样阻止正常写入。仅创建 marker 不代表备用方已经可写；`status=waiting_for_normal_mutations` 时只能补证。

外部正常命令在独立进程组中启动：先持久记录进程组，再经管道屏障允许子命令执行。Python 装饰器作用域中的 Git／验证命令也走此协议；并行线程复制各自上下文，多个子组累积登记，嵌套来源 reservation 同步保留子组。父 wrapper 消失不等于子命令已退出；对账同时检查全部组内活进程。异常 reservation 保留，不能按 TTL 偷取。Node 原生调用的未知结果须从原生控制面补证，不能用 Git HEAD 未变推断调用没有发生。

dispatcher 的 reservation 覆盖派发、等待、reroute 与结果处理。已经登记的有限在途调用可以结束，此时接管仍为 waiting，不能启动备用写入；dispatcher 结束后原 run 才可接续。脱离原 dispatcher 的只读 reviewer 结果仍归原 run，不能自动覆盖备用材料或宣称缺腿已完成。

## 公开接口与记录边界

`independent_backup_route.py` 仅依赖标准库与 Git，提供 `status`、`claim`、`adopt-manual-ownership`、`freeze-candidate`、`record-stage`、`reconcile-normal-mutation`、`finish`。`run-normal-mutation` 和装饰器供正常写入方登记副作用；内部 launcher 必须验证真实父 reservation，单独传内部 flag 不能绕过接管。

控制目录由 `git rev-parse --absolute-git-dir` 定位，为每个 worktree 独立；base-ref 锁继续采用原 common-dir／base ref 协议。普通业务材料是 Markdown、原始结果、patch 和命令日志；控制 JSON 是辅助器内部元数据，不是新的审查输出 schema。

阶段材料明确标记 `attestation_origin=executing_agent`。Agent 仍须读取两份真实完成结果、区分 request／未完成、核对去来源处置和实际验证。辅助器只核对记录和 Git 基本事实，不把文件存在包装成机器认证的评审成功。

完成结果先刷盘，再登记原 run 已消费，最后原子归档接管 marker。完成重试复用原结果；只有当前 HEAD、工作树与 index 内容仍相同才抑制旧 Stop 路线，新内容不被隐藏。stage fact 保留内容寻址历史；原始材料复制至持久目录，候选 Git 对象保留固定 ref，不能依赖临时目录或将被回收的 worktree。

## 跨仓交付

共享指引事实源为 RVF 的 `references/independent-backup-route-instructions.md`。更新时运行 `scripts/render_independent_backup_route_skill_sections.py`，生成 RVF／land／别名正文；显式传同步 Skill 路径时，生成其正文、独立辅助器副本和来源摘要。`--check` 只检查交付副本一致性，业务运行时不调用生成器。

`my-ai-setup` 的正常同步入口与 optimistic handback 登记 reservation；base 侧 landing 同时在源 task 的稳定 Git 目录登记 reservation，提交变化不会改变接管身份。第五个既有参数之后可补来源 worktree；未提供时从当前 tip 查明现存 worktree，无可核对来源则等待补证。它们不改变原 base-ref 锁、Tier 1、freshness、WIP 和远端授权边界。

`cline-kanban` 在自动收尾协调器和独立 recovery supervisor 恢复／投递前登记相同 reservation，识别 active marker 与已消费原 run。抑制事实写入可选 `independentBackupRouteSuppressionObservation`；原 `paused/failed/completed` 历史与执行结果不改成备用成功。观察写入失败不遗留未启动的业务 reservation。记录用于部署后只读观察，不能作授权凭据。

## 验收与未部署边界

隔离测试覆盖真实 Git 候选与 index 保全、在途接管、身份损坏／dangling marker、同身份恢复、阶段／证据缺失、父进程退出但子组存活，以及正常 land／同步和 Node 协调器回归。独立新上下文 Agent 的行为前测使用 synthetic reviewer／授权材料，只证明隔离工作流可执行，不是生产 RVF 审查证据。

安装 payload、运行时实际 native restore／投递与机器重启后的正式进程接续，必须分别通过部署后验证。源码／隔离测试通过不等于已经安装、部署、同步到 main 或完成 PDV。
