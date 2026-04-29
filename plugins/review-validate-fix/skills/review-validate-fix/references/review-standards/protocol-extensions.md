# Protocol Extensions

本文定义 RVF 子代理可用的非完成态 request contract。request 只表示“当前子代理需要主会话提供受控协助”，不是 review 结论，也不是 validate/fix verdict。

## 完成态保持不变

Reviewer 完成态：

- `NO_ISSUES`
- 编号 `路径:行号` issue list

Validate/fix 完成态：

- `REAL`
- `FALSE_POSITIVE`
- `ELEVATE`

## 非完成态 request

```text
RVF_LOCK_REQUEST name=<stable-lock-name> command=<command> reason=<why>
```

命令需要 repo-scoped lock。

```text
RVF_STANDARD_REQUEST domain=<simplification|security|performance> reason=<why> scope=<paths-or-issue>
```

需要主会话提供专项 standards subset 或确认该专项标准适用。

```text
RVF_MEASUREMENT_REQUEST metric=<metric-or-signal> command=<command> reason=<why>
```

需要主会话运行、加锁、替换或驳回某个测量 / audit / reproduction 命令。

```text
RVF_SUBTASK_REQUEST type=<read_only_investigation|security_check|performance_measurement|simplification_probe> scope=<paths-or-issue> reason=<why>
```

需要主会话 spawn 一个受控子任务。默认由主会话 spawn，不由 requester 自行开新 agent。

```text
RVF_CONTEXT_REQUEST need=<file|manifest|packet|prior-output|test-result> reason=<why>
```

需要主会话提供缺失上下文，避免 requester 扩大 scope 或猜测。

## Contract rules

- request 不得和 `NO_ISSUES` 混写。
- request 不得和 issue list 混写。
- request 不得和 validate/fix verdict 混写。
- request 可以有多行，但每行都必须是 `RVF_*_REQUEST`。
- 主会话必须处理 request 后让 requester 重试完成态输出。
