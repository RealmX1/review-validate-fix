# cc088 现场解封 runbook（base-branch-sync × rvf-land 死锁的第一个真实卡点）

> 适用对象：cline-kanban 仓的 Kanban task `cc088`，worktree
> `~/.cline/worktrees/cc088/cline-kanban`（git-dir `…/cline-kanban/.git/worktrees/cline-kanban151`）。
> RVF run：`rvf-20260823T215549Z-agentic-initiation-start-752987bf`
> （run 目录在 `~/.rvf/user-local-data/run-logs/runs/<run-id>/`）。
>
> **本 runbook 不由 review-validate-fix 这个 task 执行。** cc088 是另一个 Kanban task 的 worktree，
> 改动它属于那个 task 的职责范围。这里只给出确定性步骤与判据。

## 0. 一句话结论

**代码修好不会自动解封 cc088，而且这一轮的 contract 已经作废、必须重新 prepare——不能靠 restamp 救。**
K1+K3 拆掉的是「拿冻结值判罪」那一族闸；cc088 还压着**两道设计正确的 fail-closed**，
它们要的是重新跑一轮，不是放宽判据。

## 1. 现场事实（全部实测，2026-08-26）

| 项 | 值 |
| --- | --- |
| worktree HEAD | `5633f629` |
| worktree 状态 | **dirty**（十余个 tracked 文件未提交） |
| contract 四个 tip | 全部 `f4c3a970` |
| contract `bootstrap_receipt_digest`（冻结） | `05b73ba8…` |
| 活 receipt digest（现算，与 sidecar 自洽） | `66e46fbc…` |
| receipt `absorbed_base_tip` | `5633f629` |
| receipt `task_tip_after_base_absorption` | `5633f629` |
| 当前 `main` | `d0304b47` |
| validation records | 4 条，前 3 条 `passed`，**第 4 条 `failed` / `exit_code=1`**；4 条共用 manifest `3afa23aa…` |

**这轮 run 的时间线**（由上表反推，是理解后面每一条判据的钥匙）：
prepare 发生在 base 还停在 `f4c3a970` 的时候，task HEAD 也正好是 `f4c3a970`（已吸收，合法开跑）；
review 跑完之后，**又发生了一次 base 吸收**，把 task HEAD 推到 `5633f629`。
⇒ **HEAD 从 `f4c3a970` 前移到 `5633f629` 的全部内容都是 base 吸收，一个 task 提交都没有。**

## 2. K1+K3 之后哪几格通了

| 格 | 判据 | 修复后 |
| --- | --- | --- |
| ②③ | rvf-land 侧门禁的吸收/冻结 tip 比较 | **已整体删除**（K1） |
| ④ | `optional_receipt` 的冻结 digest 严格相等 | 冻结 `05b73ba8…` ≠ 活 `66e46fbc…`，**今天必挂**；K3 换成「活文件 canonical + sidecar 自洽」后通过 |
| ⑤ | `validate_bootstrap_receipt_subject` 的 `absorbed_base_tip == reviewed_base_tip` | `5633f629` ≠ `f4c3a970`，**今天必挂**；K3 换祖先性后，实测 `f4c3a970 ⊑ 5633f629` 为真 ⇒ 通过 |
| ⑤b/⑥ | `task_tip_after_base_absorption ⊑ HEAD`、四 tip ⊑ HEAD | 本来就是祖先性，通过 |

## 3. 仍然挡着的两道闸——都**不是** bug

### 闸 A：validation record 第 4 条是 `failed`

`write_reviewed_task_candidate_evidence.py` 的 `validated_validation_records` 要求**每一条** record
都 `status == "passed" and exit_code == 0`，且其 `candidate_content_manifest_sha256` 等于**当前**内容摘要。

这是设计对的 fail-closed：出路存在（在吸收后的树上重跑验证）、成本有界、单调收敛。
**不要为它放宽任何判据。**

### 闸 B：落地 delta 逃出了被评审的 scope（K5 的配套闸）

K5 会在写 evidence 前把 `candidate_task_tip` 刷成落地后的 HEAD，但放开 tip 等号的同时补了一道
**落地 delta 收敛闸**：`git diff --name-only --no-renames <restamp 前的 tip> <HEAD>` 必须
⊆ `primary_files ∪ fix_allowlist ∪ task_change_paths ∪ background_files`。

cc088 实测：

```
$ git diff --name-only --no-renames f4c3a970 5633f629
AGENTS.md
CLAUDE.md
```

而该 contract 的 `base_delta_paths` 是 `['.gitignore']`——**是更早那一次吸收的记录**。
`AGENTS.md` / `CLAUDE.md` 既不在四个集合里，也不在 contract 记录的任何 base delta 里
⇒ 它们是**这份 contract 冻结之后才进入 worktree、且从未被任何一轮 RVF 记录过的字节**。

⇒ **S3 拦住是正确的，不是误报。** 这正是「rvf-land 可以落地 reviewer 从没看过的字节」那个洞
被 K5 补上之后应有的表现。

## 4. 解封步骤（确定性）

> 前提：`/rvf-local-deploy` 已把 K1+K3+K5 部署到本机（不部署不生效）。

1. **不要复用这一轮的 contract。** 见 §5 的复用/作废表。
2. 在 `~/.cline/worktrees/cc088/cline-kanban` 里，先把该 task 的未提交工作**按其自身流程收尾**
   （commit 或明确丢弃）。`write_reviewed_task_candidate_evidence.py` 硬性要求
   「rvf-land 提交之后 worktree 必须 clean」，dirty 状态下这一步之后的一切都跑不到。
3. **重新 prepare 一轮 RVF**：此时 base 已经是 `d0304b47`，先按 `AGENTS.md` marker 指定的
   `base-branch-sync`（`PARADIGM=ABSORB_ONLY`）把当前 local base 吸收进 task，
   再让 prepare 冻结一份**与当前 HEAD 一致**的新 contract。
   新 contract 的 `base_delta_paths` 会正确记录这一次吸收，`AGENTS.md` / `CLAUDE.md` 不再是孤儿字节。
4. **在吸收后的树上重跑验证，重建 `artifacts/validation-records.json`**：
   4 条 record 必须全部 `passed`，且 `candidate_content_manifest_sha256` 等于**重跑那一刻**的内容摘要。
   原来那条 `failed` 的 `handoff-validation-004` 必须查明失败原因并修好，**不得**靠删掉它来凑绿
   ——`validated_validation_records` 要求「至少一条」，但它检查的是**列出来的每一条**，
   删记录只会把「验证失败」变成「验证没跑过」，两者都不该被 evidence 认可。
5. 走完新一轮 review → rvf-land → `write_reviewed_task_candidate_evidence.py`
   → `base-branch-sync` 的 evidence-aware handback。

## 5. 旧工件：哪些复用、哪些作废

| 工件 | 处置 | 理由 |
| --- | --- | --- |
| `<git-dir>/base-branch-sync/local-base-into-task-worktree-bootstrap-receipt.json` | **复用**（且不要手改） | 它与 sidecar 自洽、`absorbed_base_tip` 是当前 HEAD 的祖先。K3 之后它不再因 digest 漂移被拒。新一轮吸收会正常重写它，走 `REUSED_IDENTICAL_RECEIPT` 幂等短路也正常 |
| run `…752987bf` 的 `scope.contract.json` | **作废** | 四个 tip 全停在 `f4c3a970`，而其后的 base 吸收带进了它没记录过的字节（§3 闸 B）。restamp 不能救——S3 会正确地拒绝 |
| run `…752987bf` 的 `artifacts/validation-records.json` | **作废** | 第 4 条 `failed`，且 4 条共用的 manifest `3afa23aa…` 对应的是吸收前的内容 |
| run `…752987bf` 的 `handoff.md` | **只作阅读材料，不作凭证** | 它描述的 review 结论仍有参考价值（哪些 issue 被修了），但它绑定的 contract 已作废 |
| `<git-dir>/rvf/evidence/` 下的 candidate evidence | 现场**不存在**，无需处置 | 这一轮从未写出过 evidence |

## 6. 复发判据

如果将来又出现「run 卡住、且 `absorbed_base_tip` 与 `reviewed_base_tip` 不等」，先分清是哪一类：

- **base 在 prepare 之后前移、但 task 没有再吸收** ⇒ K3 的祖先性直接放行，不需要任何人工介入；
- **base 在 prepare 之后前移、task 又吸收了一次**（cc088 这一类）⇒ 落地 delta 里会出现
  contract 没记录过的 base 字节，S3 正确拒绝 ⇒ **重新 prepare**，不要试图放宽 S3；
- **task 被 rewind 到 receipt 之前** ⇒ 报 `does not descend from the bootstrap receipt`，
  这是数据真的坏了，查 rewind 来源。
