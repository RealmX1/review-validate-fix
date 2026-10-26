# 用户把 elevated 问题交给其他 Task

用户的真实委派可以使原问题不再阻断原候选收尾；问题仍保持 elevated。委派不授予 commit、base 同步或 push 权限，用户 Stop 与其他门禁继续有效。Agent 自行建卡、handoff 转述和普通 `role=user` 标签均不能代替用户授权。

```sh
rvf elevated-issue-delegation inspect --repo <worktree> --run-dir <精确run>
rvf elevated-issue-delegation record --repo <worktree> --run-dir <精确run> --request-file <持久请求.json>
rvf elevated-issue-delegation verify --repo <worktree> --run-dir <精确run>
rvf elevated-issue-delegation resume --repo <worktree> --run-dir <精确run> --request-file <持久请求.json>
```

请求样例（schema_version 1）：

```json
{
  "schema_version": 1,
  "action": "delegate",
  "run_id": "rvf-example",
  "issue_id": "RVF-002",
  "follow_up": {
    "project_path": "/absolute/main-repository",
    "workspace_id": "target-project",
    "task_id": "target-task",
    "task_created_at": 1792950703909
  },
  "target_receipt_path": "/persistent/task-get-receipt.json",
  "authorization": {
    "transcript_path": "/home/user/.codex/sessions/native-session.jsonl",
    "session_id": "source-session",
    "native_turn_id": "source-turn",
    "native_user_message_id": "source-message",
    "message_text": "将 RVF-002 委派给 target-task 解决。",
    "mode": "direct_delegation"
  }
}
```

`target_receipt_path` 来自显式目标的 `kanban task get`，需含稳定 project/task/createdAt 及 workspace 身份。缺失时自动尝试同一稳定目标的控制面查询。接受后的归档快照不再依赖目标可查询、标题未变或任务已完成。

原生上下文须包含该唯一 run ID，可以来自原生工具日志或前文；用户不需要在批准时重输。其他 run 中同名 issue 的决定不能被复用。

用户先批准建议方案、再由建卡回执确定目标时，mode 使用 `approve_proposed_delegation`；另填 `proposal_context_quote`（紧邻用户消息之前、包含 issue ID 的原生提案原文）及 `target_binding_quote`（至少八个字符，同时出现在提案和目标 task prompt 的具体方案文字）。建立跟进任务的意图可以来自该已批准提案或用户消息，因此“可以”能批准明确的委派提案；否定、拒绝或执行禁止不能算批准。当前实现核验 Codex TUI 的原生 `UserMessage`、user-triggered turn 与 user provenance，并归档事件时前缀；支持原生 `Text` 和分段文本。未支持的宿主格式保留机器等待或无效状态，不伪造授权。

撤销使用 `action=revoke`，绑定原生用户明确撤销该 issue 的新消息；无需 target 回执。重新委派仍使用 delegate。所有事件追加保存，原 handoff、attempt/result 和旧失败记录保持不变。

撤销按对应动作核验：`不要撤销 RVF-002 的委派。` 会拒绝；`不要继续委派，撤销 RVF-002 的委派。` 可以授权撤销。每条历史事件都必须有真实用户授权，较新的合法委派不能使先前的否定撤销变成合法。

退出码 0 包含已接受和已终止的旧请求；`superseded` 不产生新处置或执行资格，`accepted/reused=true/superseded=true` 仅复用历史接受回执。当前资格以 inspect/verify 或 Land 的完整核验为准；10 为等待机器证据，11 为证据无效，12 为明确禁止（包含唯一执行者已被其他路线接管）。record/resume 首次持久绑定候选、attempt、scope 与 handoff，等待期间变化不得自动重绑旧决定；也保存输入指纹、失败历史和恢复条件。同输入最多三次自动补证，原生记录、回执、查询实现或其他绑定输入变化后重新核验。后续已授权的 `rvf land commit` 自动尝试恢复待补证委派；`land check-evidence` 始终只读。已提交结果先对账，再执行缺失阶段。

canonical issue、attempt/result、reviewer 结果与轮转、scope、validation、handoff/summary 和 evidence 的正式写入口共用 `artifacts/elevated-issue-delegation/mutation.lock`，与消费者 CAS 互斥。锁忙时最多等待 30 秒，释放后自动继续；超时保留等待及失败记录，锁变为可用后恢复预算自动重开。Land 在锁外完成验证重放，再持锁复核、提交和发布证据。更新冻结工件必须经过正式 writer，不能直接覆盖文件。

含委派 evidence 必须是 v3，声明 `user_delegated_elevated_issue_disposition_v1`。例如总数 2、fixed 1、elevated 1、user_delegated 1、blocking_elevated 0；结果不得写成全部已修。旧消费者拒绝 v3，应使用支持该能力的消费者；不得删除证据或降级成全绿 v1。v3 的范围扩展继续执行完整扩展校验。公开结果分别报告审查完成、问题未解决及跟进归属、Land 实际结果、base 同步实际结果。

同一原生用户事件只代表一次决定；旧消息重放不会越过较新撤销，较新重新委派需要新的真实用户消息。不可变前缀不足以证明先后时进入机器等待，恢复指纹包含账本。历史 v3 指针按冻结账本前缀核验，允许合法新回执替换旧指针；当前读取仍检查最新 canonical 资格。
