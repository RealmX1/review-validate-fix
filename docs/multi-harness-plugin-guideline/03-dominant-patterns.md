# 03 · 三种主流架构模式

> 核验后总结，按"当前生态实际使用频度 + 工程复杂度 + 维护成本"综合排序。
>
> 结论：**Pattern A 是当前默认选择**。Pattern B 适合"主 host + 其它 best-effort"的项目。Pattern C 适合需要面向公开 marketplace 同时保留私有安装路径的项目。

---

## Pattern A · 多 manifest in-repo + 共享 source tree

### 形状

```
repo-root/
├── .claude-plugin/
│   └── plugin.json          # Claude Code 原生 manifest
├── .codex-plugin/
│   └── plugin.json          # Codex 原生 manifest
├── .opencode/
│   └── plugin.json          # OpenCode 原生 manifest（如支持）
├── .cursor-plugin/
│   └── plugin.json          # Cursor 原生 manifest（如支持）
├── skills/                  # 共享：所有 host 都从这里加载 skill
│   ├── review/SKILL.md
│   └── validate/SKILL.md
├── commands/                # 共享：slash command 文档
├── agents/                  # 共享：sub-agent 定义
├── adapters/
│   ├── claude_code/
│   │   ├── hooks/
│   │   ├── permissions/
│   │   └── settings/
│   ├── codex/
│   │   ├── hooks/           # 注意：当前不会被 Codex runtime 加载，见 04
│   │   └── config_overlay/
│   └── opencode/
└── scripts/
    └── sync-manifest.sh     # 保持各 manifest 的 version/description 一致
```

### 核心权衡

- ✅ 每个 host 都是 **Native** 安装体验：用户用各 host 自带的 `plugin install` 命令直接拉本仓库，立即可用。
- ✅ skills/commands/agents 共享一棵树，逻辑只写一次。
- ✅ 不引入"自研中间格式" → 不需要维护编译器、不需要为某 host 解析自家 DSL。
- ⚠ manifest 漂移：N 份 manifest 中的 version / description / icon 等字段必须保持同步，需要 `scripts/sync-manifest.sh` 或 CI 校验。
- ⚠ Codex 当前不会扫插件根的 `hooks/`，必须由 installer 或 skill 文档要求用户手动把 hook 接到 `~/.codex/hooks.json`（详见 [`04`](04-anti-patterns.md) 与 [`05`](05-adapter-contract.md)）。

### 实际样本

- `obra/superpowers`（Claude Code + Codex 双 manifest，无 universal manifest，无 compiler）。
- `EveryInc/compound-engineering-plugin`（Pattern A，与 Superpowers 同形）。
- 准 Pattern A：`affaan-m/everything-claude-code` —— 它有 `.claude-plugin/plugin.json`，但其它 host 的"清单"通过 installer 的 profile 在目标位置生成；可视作 Pattern A + installer 增强。

### 何时选

- 想让"多个 host 都把这个插件视为原生公民"。
- 不想引入自研 manifest DSL。
- 团队人数能承担 N 份 manifest 的同步。

---

## Pattern B · 单源标准 + 翻译器

### 形状

```
repo-root/
├── source-of-truth/         # 某一个 host 的原生格式（通常是 Claude Code）
│   └── .claude-plugin/plugin.json
├── translator/              # 把 source-of-truth 翻译成其它 host 格式
│   ├── to_codex.py
│   └── to_opencode.py
├── dist/                    # 翻译产物（通常被 .gitignore，或在 release 时生成）
│   ├── codex/
│   └── opencode/
└── skills/                  # 共享
```

或者一种更轻量的形态：**直接采用 `agentskills.io` 标准**作为单源，所有兼容 client 直接消费同一份 SKILL.md。

### 核心权衡

- ✅ 单源 → 单一事实来源 → 不会漂移。
- ✅ 翻译器是"工具仓"，可独立测试、可被多个项目复用。
- ⚠ 派生产物的"原生体验"打折：被翻译过去的 hook、subagent 调用方式等可能在目标 host 上行为不同甚至无法执行。
- ⚠ 翻译器需要持续维护：每当任一 host 更新 manifest schema，翻译器都要跟。
- ⚠ 若选用 `agentskills.io`：runtime 层（hook / subagent）仍需各 host 各自处理；它只把 skill **文档**协议层标准化了。

### 实际样本

- cc-plugin-to-codex 类转换器（见 [`02-verified-landscape.md`](02-verified-landscape.md) D 节）。
- 直接生产 `agentskills.io` 兼容 SKILL.md 的项目（见 [`02`](02-verified-landscape.md) E 节）。

### 何时选

- 有明确的"主 host"，其它 host 只需 best-effort 兼容。
- 不想维护 N 份 manifest，但能接受派生产物体验打折。
- 项目以 skill 文档为主、几乎不用 hook（这时 `agentskills.io` 单源是最优解）。

---

## Pattern C · Marketplace + 手动 installer 双轨

### 形状

```
repo-root/
├── manifests/
│   ├── claude-marketplace/  # 投放到公开 marketplace 的版本
│   └── self-hosted/         # 私有/团队自有渠道的版本
├── install.sh               # 手动渠道，可 --profile / --target
├── skills/
└── adapters/
```

### 核心权衡

- ✅ 同时获得 marketplace 的发现性 + installer 的灵活性。
- ✅ 私有版本可以包含 marketplace 不接受的字段（敏感配置、内部 hook 路径等）。
- ⚠ 双轨：发布流程更复杂，需要 CI 区分 release 类型。
- ⚠ 用户认知成本：要解释"marketplace 装的 vs `install.sh` 装的有何区别"。

### 实际样本

- `affaan-m/everything-claude-code`（installer 显式给出 profile/target，矩阵化兼容性，可视作 Pattern C 的"installer 重心"变体）。

### 何时选

- 需要进 marketplace（如 Claude Code plugin index），同时保留企业/私有用户的高级安装入口。
- 团队有能力维护两条发布通路。

---

## 选择决策矩阵

| 维度 | Pattern A | Pattern B | Pattern C |
|---|---|---|---|
| 主目标 | 多 host 都 Native | 单源驱动多 host | marketplace + 自托管并存 |
| 是否需自研 DSL | ❌ | ❌（直接复用 `agentskills.io`）/ ✅（自研翻译器） | ❌ |
| 体验一致性 | 高 | 中（派生 host 体验打折） | 高 |
| Codex hook 现状下能否原生用 | ❌（需 installer fallback） | ❌（同上） | ❌（同上） |
| 维护成本 | manifest sync | 翻译器维护 | 双发布通路 |
| 上线门槛 | 低 | 中 | 高 |
| **推荐默认** | ✅ | 退而求其次 | 仅在需要 marketplace 时 |

---

## 三种模式与 RVF 的关系

- RVF 当前事实上已经走在 **Pattern A** 路径：仓库内既有 Claude Code 侧的 plugin 结构，又通过 Stop hook 调度 Codex；只是 manifest/adapter 分工尚不显式。
- [`06-rvf-application.md`](06-rvf-application.md) 给出把 RVF 显式对齐 Pattern A 的最小动作。
- 不建议 RVF 走 Pattern B：因为 RVF 重度依赖 hook 与 subagent，单源 + 翻译器无法吃掉 host 之间 hook 运行时差异。
- 不建议 RVF 走 Pattern C：目前没有进公开 marketplace 的需求。
