# RVF 路径清单压缩展示

路径清单统一复用 `compact_path_tree_rendering.py`：以目录分段建树、合并没有分叉的目录链，
使用连接线和代码块保留结构。只操作传入的路径字符串，不访问文件系统。

参考 [Repomix 的文件树实现](https://github.com/yamadashy/repomix/blob/main/src/core/file/fileTreeGenerate.ts)。
[Rich.Tree](https://rich.readthedocs.io/en/stable/tree.html) 和
[anytree.RenderTree](https://anytree.readthedocs.io/en/stable/api/anytree.render.html) 提供通用树渲染，
但目录链压缩和 RVF 属性分组仍需实现，因此这里使用标准库，不新增运行时依赖。

## 展示规则

- `render_compact_path_tree(paths)` 返回纯文本，`render_compact_path_tree_block(paths)` 返回代码块。
- 目录以 `/` 结尾，同层目录优先、名称按字符串顺序排序，文件始终单列；空清单为 `(none)`。
  真实文件名 `(none)` 展示为 `\u0028none)`，用可逆转义避免与空集标记混淆。
- 相同清单内重复路径仅展示一次。调用方提供文件路径集合，并负责原有数量上限、范围和属性分组。
- 相对路径基准由调用方声明；绝对路径保留前导 `/`。不规范化大小写、`.`、`..` 或反斜线。
- 反斜线、引号、反引号、美元符、冒号、树连接线与不可打印字符使用 JSON 字符串转义（不含外层引号）；
  标签开头或空白后的斜线同样转义，普通目录分隔符保留。仅含转义的清单附带说明。
  这些转义防止叶子文件名、绝对路径或压缩目录链被 UPS 识别为手动 skill 调用，解码后保持原路径。
- 候选横幅按完整 `change_state`、`ownership` 分组；handoff intake 按原始两列 Git status 分组，
  用 JSON 引号显式保留 status 内的空格。组属性适用于全部叶子，不设隐式继承或叶子覆盖规则。
- 先按原有顺序选择前 N 个文件，再分组和排序。目录行不消耗文件配额；省略信息留在树外。

## 使用范围

| 入口 | 压缩内容 | 保留的边界 |
| --- | --- | --- |
| Stop / 手动触发决策横幅 | 候选路径及共享状态 | 同一正文渲染入口、前 40 文件、决策 token |
| review packet | primary、background、owned、unattributed、allocated、background-untracked 清单 | 每组的审查范围语义 |
| scope-of-work 骨架 | 前 40 个候选文件，共用一条填写提示 | 必须逐文件填写完整路径和 reasoning；stub、rerender gate |
| handoff intake 文本 | reviewed 与 unrelated 清单内各自按 Git status 分组 | 两类范围不合并 |
| scope diagnosis / bootstrap 确认摘要 | dirty-write / 前 10 个超阈路径 | 原诊断和确认行为 |
| analysis scaffold | reviewer artifact 引用、变更路径的 scope 分类 | run_dir / 目标仓库路径基准分别声明 |
| base intent context | 机械适配路径 | 自报归因分类不改变审查范围 |

JSON、scope contract、候选指纹、unit/hunk 证据、diff、命令参数以及逐文件内容标题保留完整路径。
本改动不改变候选计算、ownership 判定、hook audience 或投递通道，不提供旧展示格式兼容层。

## 实际示例与测量

以 5 个文件的候选清单为例，旧横幅这一段为 845 字符，当前渲染为 403 字符，减少 52.3%。
计数包含该段标题、状态、缩进和换行，不包含其余横幅。它是字符数比较，不代表 token 比例或模型准确率。

````text
  候选路径（相对目标 worktree；分组属性适用于组内全部文件）：已展示 5 / 5 个文件

change_state=uncommitted; ownership=unattributed（本组展示 5 文件）
```text
docs/investigations/codex-kanban-hook-registration-and-rvf-skill-loading-2026-09-07/evidence/
├── app-server-permission-summary.json
├── app-server-reload-summary.json
├── current-config-sanitized.json
├── current-hooks-metadata-mirror.json
└── current-rvf-plugin-load-errors.json
```
````

## 回归入口

`python3 -m pytest tests/test_compact_path_tree_rendering.py -q` 检查路径可还原性、混合状态、40/41
边界、特殊文件名防自触发、handoff 分组、bootstrap 配额以及临时隔离 Git 仓库中的 packet CLI。
该测试文件已加入 `scripts/check_skill_contracts.sh` 的插件契约检查。

现有 scope-of-work、base intent、analysis 测试更新展示断言；机器字段断言保留。
测试脚本加载器将 scripts 目录加入导入路径，与直接运行脚本时一致，确保单独执行测试也能导入共享渲染器。
真实 agent 的格式理解准确率比较保留在渲染处的 `EXPERIMENT_NODE:`，本次没有宣称已验证。
