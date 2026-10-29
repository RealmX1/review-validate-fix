# Reviewer 账户资格与零推理健康门禁

## 实现合同

用户在任务 `ef4ad` 批准实施《RVF 外审健康门禁修复与统一账户管理方向》。本轮交付统一候选、fallback、正式派发前的资格判断；共享负面证据；正式调用成功的一小时复用；原 run 的有界自动恢复。

实施分为三个可独立验证的切片：共享资格与证据、调度与正式派发、原生补证与所属执行恢复。验收使用隔离 HOME、配置目录、真实 Git worktree、替身 CLI 和本地 HTTP 服务，不向已知不可用账户发送真实推理。

Magpie 的部署、登录、刷新、切号及 Cline Kanban 账户管理 UI 属于后续跨仓库设计。本轮仅消费一个与厂商无关的缓存状态文件，并用第三方管理系统替身验证身份绑定与陈旧额度；没有安装或调用 Magpie。历史调查 run、安装副本和用户全局配置不作修改。

## 资格判断

`reviewer_account_eligibility.py` 提供相同的判断，应用于 `default_reviewer_configurations`、`_start_role` 和 `execute_role`。正式派发前在角色锁内再次核对身份、账户状态、原截止时间及 Stop。

| 状态 | 证据与行为 |
| --- | --- |
| `available` | 同配置、凭据来源、账户、路由及模型范围内的正式调用成功，未满 3,600 秒且没有更新的负面事实。 |
| `unavailable` | 明确未登录、绑定的认证/封禁/额度故障、配置指定的代理 `/health` 返回 503。排除此配置，其他席及 fallback 继续。 |
| `unknown` | 无证据、接口不可达、元数据 429/500、陈旧额度、只能证明本地已登录。允许有界正式调用，不能标记健康通过。 |

共享记录正在被另一进程更新时有界等待；仍无法取得锁则保持 `unknown`、`can_dispatch=false`，交给机器等待恢复，不解释为账户封禁。共享存储损坏时保留原件的摘要命名副本，重建未知状态；存储不可写时保留未知及具体错误类型。原 run 的原生输出和 outcome 可以幂等补齐共享证据。

## 零推理检查

默认 Claude 使用 `claude auth status --json`。只接受与正式命令相同 CLI 的本地认证检查；`loggedIn=false` 排除，`loggedIn=true` 仍不足以验证远端 OAuth。另支持同 CLI 的 `codex login status`。

现有 `health_command: ["curl", "-fsS", "http://127.0.0.1:8080/health"]` 覆盖继续生效。只接受已核验的 GET `/health` 路径和只读参数；实际调用禁用 curlrc、限制响应大小及总时限、不跟随重定向。503 是该配置声明的路由不可用；200 只能证明路由可达；429、其他错误或请求异常保持未知。

其他健康命令统一记录 `inference_or_unverified_health_command_skipped`，不执行。内置 Kimi/OMP 的模型请求型健康 prompt 已移除；动态选模只依赖正式命令与各自的非推理模型配置查询。旧配置中的模型健康命令可以被解析，但不会获准执行。

每次状态查询最多 5 秒，并消耗原角色剩余时限。普通状态缓存 30 秒；明确不可用状态 300 秒后复核。复核次数按原 run 剩余时限计算并持久保存；原截止时间到达后不再恢复。查询进程受所属执行登记和进程组清理保护。

## 身份、缓存与故障记忆

共享记录位于 `~/.rvf/user-local-data/runtime-state/reviewer-account-availability/`，遵循 `RVF_USER_LOCAL_DATA_ROOT`。每个身份独立加锁，使用现有原子写入机制。记录保存摘要、状态、来源路径、下一次检查时间、恢复预算与事件历史，不保存凭据正文或健康命令原始输出。

身份覆盖正式 CLI 可执行文件及摘要、命令范围、有效认证/路由环境、配置声明的账户/凭据来源/路由标识，以及 `credential_evidence_paths` 的内容摘要。内置 Codex/Claude 同时跟踪认证文件和关联路由配置。会话与回合 ID 不参与账户身份，普通机器环境变化不重置认证预算。

凭据来源无法观察的配置保持 `unknown`，不把上一条调用成功提升为可复用的账户健康通过；仍允许有界执行。此类配置可提供精确的 `credential_evidence_paths` 或绑定的管理系统状态，避免账户切换后误用旧成功。

内置 Claude 的 `credential_identity_source=claude_secure_storage` 在 macOS 额外跟踪生产 Keychain 服务的凭据摘要，匹配 Claude 2.1.311 的 `Claude Code-credentials` 及自定义配置目录后缀规则；凭据值只在内存中计算摘要。Linux 使用认证文件。其他凭据存储应通过明确的证据路径或后续管理系统状态提供变更信号。

正式原生错误立即否定旧成功记录。认证和明确封禁作用于账户；额度作用于显式模型、动态默认模型族或配置声明的额度池。普通网络故障不写成认证/封禁。正式成功与评审完成分开：成功调用不能把缺少有效结果的 reviewer 标成完成。

认证/额度失败冷却 300 秒后，未变化身份最多允许一次额外正式尝试。尝试权在模型派发前原子认领，重启和并发 controller 不能重复认领。明确封禁没有定时正式重试。凭据身份变化或更新且范围匹配的账户恢复事实可以重新取得资格；额度恢复还需要独立的新鲜额度证据。

旧调用晚到的成功不能覆盖期间出现的新失败。对同一个 invocation 的重复补证不增加故障次数。worker 退出但留下结构化原生错误或正常终止事实时，可以补齐账户证据；退出码缺失时不凭 stderr 猜测失败，也不伪造原 outcome。

## 管理系统缓存状态文件

reviewer 配置可声明以下字段；同样允许通过本地 reviewer override 设置：

```json
{
  "account_identity": "provider/account-identifier",
  "credential_source_identity": "manager/credential-binding-identifier",
  "route_identity": "manager/route-identifier",
  "account_status_evidence_path": "/persistent/account-manager/reviewer-account-status.json"
}
```

文件内容示例：

```json
{
  "account_identity": "provider/account-identifier",
  "credential_source_identity": "manager/credential-binding-identifier",
  "route_identity": "manager/route-identifier",
  "status": "available",
  "observed_at": 1793303000,
  "stale": false,
  "quota": {
    "remaining": 12,
    "observed_at": 1793303000,
    "reset_at": 1793306600,
    "stale": false
  }
}
```

三个身份必须精确匹配。时间使用 epoch 秒。状态最多有效 60 秒，可用 `valid_until` 进一步缩短。支持 `available`、`authentication`、`account_banned`、`usage_limit`；其他值、缺失文件、错误身份及陈旧状态均保持未知。只有读取状态文件，不触发 vendor 查询或模型请求。

状态按模型细分时，配置与状态文件还须提供相同的 `model_scope_identity`；不同模型范围的记录保持未知。账户级状态可省略此字段。

额度单独按采集时间、`stale` 及 `reset_at` 判断；已过期窗口不能据此判断剩余额度。正式成功缓存不会延长额度有效期。已确认的负面事实会留在共享记录，不因元数据文件后来陈旧就消失；账户与凭据绑定改变、更新的恢复事实或有界正式恢复才能解除。

## 所属执行恢复与验收

业务结果沿用 `machine_evidence_pending`、`resume_same_run`、`evidence_paths`、`retry_not_before_epoch_ms` 和 `maximum_unchanged_retries`，由现有 Cline Kanban 所属执行机制接续。观察路径包含共享记录、凭据文件、配置、缓存状态文件及当前资格判断。仅当前未完成阶段提供恢复时间，不把历史尝试的旧时间当作新恢复资格。下次唤醒也受原角色截止时间限制。

自动验收覆盖代理 503、元数据 429/500、免费登录成功后正式认证失败、跨 run 排除、一小时到期、身份/Keychain 变化、陈旧额度、原生补证、并发恢复认领、多个重启 controller、Stop、到期与已完成席保护。完整命令为 `PYTEST_ADDOPTS='--tb=short' bash scripts/check_skill_contracts.sh`。

免费检查仍可能漏掉首次正式调用故障，因此必须同时保留有界 fallback 和跨 run 负面证据。Magpie 的实际账号映射、切号、凭据生命周期及部署兼容性仍需后续隔离验证。
