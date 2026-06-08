# AI Agentic 工作流设计反模式 — 演示 + 评估工具包

一份关于「业界公认的 AI agentic 工作流设计反模式」的自包含演示，外加一个可批注的 Markdown 镜像，供你按既有认知评估/校准这份清单。校准完成后，将用最终清单审计本 RVF 仓库（Phase B）。

## 怎么用

1. **看演示**：浏览器直接打开 `presentation.html`（自包含、离线可开、无外部依赖）。
   - `←` / `→` / 空格 翻页 · `O` 总览跳转 · `F` 全屏 · 底部进度条/页码。
   - 每张卡片：定义 / 为何有害 / 症状（如何识别）/ 缓解（正确做法）/ 来源；右上角徽标是**证据强度**。
2. **做评估**：打开 `anti-patterns.annotated.md`，在每张卡片底部的 `annot` 区块里逐条标 ☐同意 / ☐存疑 / ☐补充，写下你的既有认知或反驳。
   - 顶部有「速览与判定表」可做快速纵览。
   - **重跑生成脚本会保留你写在 `annot` 区块内的内容**（按反模式 id merge）。

## 证据强度三档（卡片右上角徽标 / md 中标注）

- `跨多源公认`：多个独立权威来源（Anthropic / 学术 arXiv / Google SRE / Microsoft / OWASP 等）一致。
- `较公认`：真实且常见，但来源相对集中（多为实践博客或单一分类法）。
- `新兴/单源观点`：单一来源或较新，需你独立判断。

> 注：这一档是**编辑判定**，用于帮你分轻重。初次由 workflow 的逐卡 verify agent 各自标注时，因每个判者独立且倾向「确认上调」，曾全部塌缩到 `跨多源公认`（这本身是 LLM-as-judge 偏差的一个实例）；随后做了一次**整体相对校准**恢复区分度。你尽可在 md 里覆盖。

## 内容真相源与再生成链（如需增删/修改反模式）

```
_cards/<id>.json          ← 每个反模式一份（内容真相源，来自 workflow）
   │  python3 assemble_data.py
   ▼
antipatterns-data.json    ← 聚合 + 排序 + 组元数据（中间产物）
   │  python3 build_html.py
   ▼
presentation.html         ← 自包含演示；内嵌 JSON 数据岛为唯一渲染源
   │  python3 generate_markdown.py
   ▼
anti-patterns.annotated.md ← 可批注镜像（重跑保留 annot 区块）
```

- **改某条内容**：编辑 `_cards/<id>.json` → 依次重跑三脚本。
- **加一条反模式**：在 `_cards/` 新增 `<id>.json`（字段见任一现有卡片；`group_id` 须为 `g1`–`g8` 之一，`order` 决定排序）→ 重跑三脚本。
- **删一条**：删除对应 `_cards/<id>.json` → 重跑三脚本。
- ⚠️ **不要手改 `antipatterns-data.json`**：它由 `assemble_data.py` 从 `_cards/` 生成，重跑会被覆盖丢失；唯一真相源是 `_cards/<id>.json`。（`generate_markdown.py` 头注里「改 antipatterns-data.json」的旧表述以本 README 为准。）
- 字段：`id, name_cn, name_en, group_id, order, definition, why_harmful, symptoms[], mitigation, sources[{label,url}], evidence_strength`。

## 文件清单

| 文件 | 作用 |
|---|---|
| `presentation.html` | 自包含 HTML 演示（**主交付物**，浏览器打开即用） |
| `anti-patterns.annotated.md` | 可批注评估清单（**你在此评估**） |
| `antipatterns-data.json` | 聚合后的结构化数据（中间产物） |
| `_cards/*.json` | 每个反模式一份的内容真相源 |
| `assemble_data.py` / `build_html.py` / `generate_markdown.py` | 再生成脚本（stdlib-only） |

## 下一步（Phase B）

你校准完 `anti-patterns.annotated.md`（增删/标注）并告知后，我会用你确认的最终清单逐条审计 RVF：每个反模式给出 `状态（真问题/部分暴露/已justified设计/历史已修复/不适用）+ file:line 证据 + 严重度 + 建议`，产出 `rvf-audit.md`。
