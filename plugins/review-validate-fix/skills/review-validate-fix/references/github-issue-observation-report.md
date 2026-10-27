# GitHub 问题观察报告

主 Agent 先读取 `rvf collect-issues --list-repository owner/repository`，判断是否为同根因。问题身份应描述机制或行为，不使用文件行号或标题作为唯一身份；旧人工问题可显式给 `issue_number`。跨组件问题选主要修复仓库，一条 Issue。

```json
{
  "repository": "owner/repository",
  "problem_key": "workflow:background-wip-overwritten",
  "title": "应用修复时覆盖背景 WIP",
  "problem": "问题及成立依据",
  "impact": "受影响行为",
  "reproduction": "最小复现步骤",
  "evidence": "必要命令、代码或结果",
  "observation_id": "task-id:run-id:problem-observation-1",
  "observation": {
    "task_id": "来源任务",
    "run_id": "精确运行标识",
    "phenomenon": "本次现象",
    "validation": "实际验证结果",
    "repair": "修复情况，已修复也可收集"
  }
}
```

`repository` 省略时从 `--repo` 的 GitHub origin 推断。可选 `problem_key_aliases` 用于已知根因标识更名，`issue_number` 用于主 Agent 已核对的已有问题。批量报告使用 `{"reports":[...]}`。

同一 observation 重试沿用同一原件与 ID；观察内容改变则新建 observation。CLI 返回投递结果和持久原件路径。断网、缺少凭据及未知写入结果均保留材料；条件恢复后 `--flush` 先查询 Issue/评论再接续。不自动关闭、重新打开或派发任务。
