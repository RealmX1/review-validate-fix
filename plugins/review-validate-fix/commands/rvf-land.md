---
description: 收尾同一 worktree 中 future-self 已应用的 RVF 工作：吃下粘贴的 RVF handoff、sanity-check、必要时最小修正、验证并提交（不自动运行 base-branch-sync）。
argument-hint: "<粘贴的 RVF handoff / finalization 正文，或 handoff 路径>"
---

# RVF Land

Input: $ARGUMENTS

把这次调用当作显式触发 bundled `rvf-land` skill。不要依赖 Claude Code 从 plugin manifest 自动加载该 skill。

开始工作流前，先解析 bundled skill 目录并读取其 `SKILL.md`：

```bash
python3 - <<'PY'
from pathlib import Path
import os

candidates = []
root = os.environ.get("CLAUDE_PLUGIN_ROOT")
if root:
    candidates.append(Path(root) / "skills" / "rvf-land")
candidates.append(Path.home() / ".claude" / "plugins" / "cache" / "review-validate-fix-local" / "review-validate-fix" / "0.1.0" / "skills" / "rvf-land")
candidates.append(Path.home() / ".claude" / "local-marketplaces" / "review-validate-fix" / "plugins" / "review-validate-fix" / "skills" / "rvf-land")

for candidate in candidates:
    skill = candidate / "SKILL.md"
    if skill.is_file():
        print(candidate)
        break
else:
    raise SystemExit("rvf-land bundled skill not found")
PY
```

然后读取 `<printed-skill-dir>/SKILL.md` 并按其内容作为权威实现执行。`$ARGUMENTS` 是用户粘贴的 RVF handoff / finalization 正文，或 handoff 文件路径；为空时停止并要求用户提供。本命令到提交为止结束，不自动运行 `/base-branch-sync`。
