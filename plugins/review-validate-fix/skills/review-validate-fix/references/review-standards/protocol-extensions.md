# Protocol Extensions

本文定义 RVF 子代理可用的非完成态 request contract。reviewer request 通过 `$RVF_WRITE_REVIEW_RESULT` 写入 `$RVF_REVIEW_RESULT`；request 只表示“当前子代理需要主会话提供受控协助”，不是 review 结论，也不是 validate/fix 完成状态。

## 完成态保持不变

Reviewer 完成态 artifact：

- `kind: no_issues`
- `kind: issues`，每条 issue 含 `path`、`line`、`message`

Validate/fix 完成态由 `rvf_fix_attempt.py stop` 写入：

- `--status fixed`
- `--status false_positive`
- `--status elevated`
- `--status failed`

## 非完成态 request

```sh
python3 "$RVF_WRITE_REVIEW_RESULT" lock-request --out "$RVF_REVIEW_RESULT" \
  --name <stable-lock-name> --command <command> --reason <why>
```

命令需要 repo-scoped lock。

```sh
python3 "$RVF_WRITE_REVIEW_RESULT" standard-request --out "$RVF_REVIEW_RESULT" \
  --domain <simplification|security|performance> --scope <paths-or-issue> --reason <why>
```

需要主会话提供专项 standards subset 或确认该专项标准适用。

```sh
python3 "$RVF_WRITE_REVIEW_RESULT" measurement-request --out "$RVF_REVIEW_RESULT" \
  --metric <metric-or-signal> --command <command> --reason <why>
```

需要主会话运行、加锁、替换或驳回某个测量 / audit / reproduction 命令。

```sh
python3 "$RVF_WRITE_REVIEW_RESULT" subtask-request --out "$RVF_REVIEW_RESULT" \
  --type <read_only_investigation|security_check|performance_measurement|simplification_probe> \
  --scope <paths-or-issue> --reason <why>
```

需要主会话 spawn 一个受控子任务。默认由主会话 spawn，不由 requester 自行开新 agent。

```sh
python3 "$RVF_WRITE_REVIEW_RESULT" context-request --out "$RVF_REVIEW_RESULT" \
  --need <file|manifest|packet|prior-output|test-result> --reason <why>
```

需要主会话提供缺失上下文，避免 requester 扩大 scope 或猜测。

## Contract rules

- request artifact 不得和 `kind: no_issues` 混写。
- request artifact 不得和 issue 混写。
- request 不得和 validate/fix 完成状态混写；request 阶段不得运行 `rvf_fix_attempt.py stop`。
- request 可以有多条，但必须都在同一个 `kind: request` artifact 的 `requests` array 中。
- 主会话必须处理 request 后让 requester 重试完成态输出。
