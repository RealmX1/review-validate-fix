# `$rvf-analyze` 事后复盘 agent 操作手册

本 reference 描述如何对一次已 finalize 的 RVF run 跑 ``$rvf-analyze`` 复盘。
agent 的本职是**叙事补全 + 因果归属**——所有可机械抽取的事实都已经由
``scripts/rvf_analyze.py`` 写到 ``<run_dir>/artifacts/analysis/`` 下的
``summary.md`` 与 ``causality.json``，agent 只需在那里填空。

``rvf_run_finalize.finalize_run()`` 会在 RVF finish/finalize 后自动生成同一套
确定性 scaffold。用户显式跑 ``$rvf-analyze`` 时，脚本仍会重新 scaffold 一次，
然后由 agent 补全叙事与 ``candidate_patch_call_ids``。

> ⚠️ 与主 RVF 流程严格分离：``$rvf-analyze`` 与 ``$review-validate-fix`` 是
> 两套独立 mode。即便上下文里同时出现 review/validate/fix 叙事，本 agent 的
> 唯一任务是**复盘已 finalize 的 run**——不得启动新 review、不得改源码、
> 不得跑 validate/fix、不得生成 handoff.md。

## 入口与目标 run 解析

用户调用形式（任选其一）：
- ``$rvf-analyze``——默认走 ``state/latest.json``
- ``$rvf-analyze latest``——同上
- ``$rvf-analyze <run_id>``——例如 ``$rvf-analyze rvf-2026-05-04T10-12-31-abc``
- ``$rvf-analyze /abs/path/to/run_dir``——直接给 run_dir 路径

agent 只调一个脚本：

```bash
python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_analyze.py \
  [--run-id <id> | --run-dir <path> | --latest | <positional target>]
```

如果当前 cwd 不是仓库根，请使用脚本的绝对路径。

## 退出码语义（必须严格按此分支）

脚本 stdout 总是结构化 JSON。退出码语义：

| 退出码 | 含义 | agent 处理 |
|---|---|---|
| `0` | 已成功 scaffold ``analysis/summary.md`` 与 ``analysis/causality.json``。 | 进入"叙事补全"流程（见下） |
| `2` | classification == ``running``（run 看起来还在跑）。 | **询问用户**：是否仍要复盘？得到肯定后追加 ``--force`` 重入；否则结束 |
| `3` | classification 是 ``orphan_candidate`` 或 ``cancel_without_lock``，需要决策。 | **询问用户**："这个 run 看起来未完成，要现在 lazy finalize 一次再分析吗？"；用户答 yes → 追加 ``--auto-finalize-orphan`` 重入；用户答 no → 追加 ``--decline-finalize`` 重入 |
| `5` | 上轮调用传了 ``--auto-finalize-orphan`` 但 ``finalize_run`` 自己抛异常。 | **不要再次询问 lazy finalize 决策**。读 stdout 里 ``error`` 字段把失败原因转给用户；可建议用户 ``--decline-finalize`` 重入做降级分析，或排查 finalize_run 自身问题 |
| `4` | 解析 run_dir 失败（找不到目录 / latest pointer 缺失）。 | 反馈错误信息给用户，请其确认 ``--run-id`` / ``--run-dir`` 是否正确 |

agent 不得在退出码 2/3 上自作主张选择——必须用中文向用户提问。退出码 5 已经代表"用户上轮已经选过 lazy finalize"——只能转述错误并提议降级路径，不要再次发起同一个询问。
``--auto-finalize-orphan`` 会真的去拍 trajectory + workspace diff，可能保留
abandon 期间的杂质 record，是用户该决定是否接受的事。

## 工件路径

成功后两份产物：
- ``<run_dir>/artifacts/analysis/summary.md``——Markdown 叙事骨架。
- ``<run_dir>/artifacts/analysis/causality.json``——结构化 issue + patch 列表。

如果脚本走过 ``--auto-finalize-orphan`` / ``--decline-finalize`` / ``half_broken``
任一分支，``<run_dir>/artifacts/.interrupted`` 也会被写入，记录检测时刻、
classification 与用户决策。后续重入时该文件会被 classify 读出并体现在
``classification.has_interrupted_marker``——不要再次询问用户同一个问题。

## 复盘内容（agent 实际要做的）

### 1. ``summary.md``：补全 ``<!-- TODO(rvf-analyze): ... -->`` 占位

按节填写。各节已有的确定性事实是真理来源；你只在标注的 placeholder 位置
追加叙事。约束：
- 全部用中文。
- 每段叙事不超过 5 句；超长拆段。
- 引用具体证据时用 ``trajectory.jsonl`` 的 ``raw_ref.line`` 或
  ``causality.json::patches[].call_id`` / ``issues[].issue_id`` 当锚点，
  不要复制大段原文。
- 如果某节因为数据缺失（``half_broken`` 或 ``declined_finalize``）无法叙事，
  原句保留 placeholder + 加一行明确说明 "**数据不全**：…"，不要瞎编。

各节的叙事侧重：
- ``## 概览``——一句话回答"这个 run 干了什么、是否成功"。
- ``## 触发上下文 (pre-RVF)``——用户/上一阶段在 RVF 启动前在做什么；为什么
  会触发 RVF。如果是分叉场景指明父会话 id；同会话场景指出 cut 点 marker。
- ``## RVF 自身轨迹``——RVF 的 prepare → review → merge → validate_fix →
  handoff 各阶段实际发生了什么。引用 ``trajectory.index.json::kind_counts``。
- ``## Reviewer 发现``——每位 reviewer 提了哪些 REAL/NIT/ELEVATE issue，
  哪些被 validate-fix 处理、哪些被绕过。证据来自
  ``artifacts/reviewers/<id>/review-result.json``。
- ``## 工作区改动``——最终 ``workspace-diff.json`` 里改了什么文件，是否
  与 reviewer 建议一致。引用 ``changed_paths[].path``。
- ``## 待 LLM 补全的叙事``——这一节就是给你的"自由发挥"区，写一段对
  整个 run 的复盘判断（设计取舍、未解决的疑点、值得后续跟进的事项）。

### 2. ``causality.json``：填写 ``candidate_patch_call_ids``

这是 agent 唯一会**修改** schema 字段值的地方。读 ``issues[]``，对每个
issue 判断"哪些 patch 的 call_id 看起来是为了响应这条 issue"，把
``call_id`` 列表填到该 issue 的 ``candidate_patch_call_ids: []``。

判断启发式（优先级从高到低）：
1. patch 改动的 ``artifact_refs[].path`` 与 issue 提到的文件路径吻合。
2. patch 时间戳晚于 issue 发现时间。
3. ``trajectory.jsonl`` 中该 patch 之前的 ``message`` / ``reasoning``
   record 文本里出现了 issue 的关键词或 ``issue_id``。

允许列表为空（找不到对应 patch 时）；允许多对多。**不要伪造 call_id**——
只能用 ``causality.json::patches[].call_id`` 列表中已存在的值。

写回文件时：直接 read → 修改 ``issues[].candidate_patch_call_ids`` →
write back。其余字段（``schema_version`` / ``run_id`` / ``patches`` 等）
保持原样。原子写：``<path>.tmp`` + ``mv`` 或者使用 Python 标准套路；不要
让其他进程读到半文件。

### 3. 最终输出

agent 完成补全后只回复 1–3 句中文：
- 写到了哪两份文件（绝对路径）。
- 概括最值得用户注意的复盘结论（一句话）。

不要把 ``summary.md`` 全文复述到对话里——文件已经写好，让用户自己打开看。

## 边界情况

- ``--decline-finalize`` 路径上的 run 通常没有 ``trajectory/`` 与
  ``workspace-diff.json``。``causality.json::patches`` 会是空列表；
  ``summary.md`` 的多个节会标 "数据不全"。这是预期行为，不是 bug。
- ``half_broken``：``summary.json`` 都读不出来时，``causality.json``
  的 ``run_id`` 字段会是 ``null``。叙事章节几乎都得标 "数据不全"。
- 多 reviewer 时，``causality.json::issues[]`` 已按 ``reviewer_id``
  排序聚合；agent 不需要重排。
- 如果用户重复跑 ``$rvf-analyze`` 同一个 run，scaffold 会被覆盖。
  ``.interrupted`` 也会更新 ``written_at``。这是预期的——agent 每次都重新
  写一份完整 ``summary.md`` 与 ``causality.json``，不要尝试 merge 旧版本。

## 不做的事

- 不调用 ``finalize_run`` 直接（脚本会在 ``--auto-finalize-orphan`` 时调）。
- 不修改 ``run_dir`` 下除 ``analysis/`` 之外任何文件；尤其不要改
  ``summary.json`` / ``handoff.md`` / ``trajectory/`` / reviewer 产物。
- 不基于复盘结论自动开 PR、改源码或派生新 task。复盘是只读 + 写
  ``analysis/`` 两份新文件。
- 不读 ``~/.codex/sessions/`` 或仓库 git 历史——所有需要的内容已经被
  ``trajectory_capture`` 落到 ``run_dir/artifacts/trajectory/`` 里。
