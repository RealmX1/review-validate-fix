---
description: 失败再入：用户实测判断「早先实现本身未达成原始目标」时，按最近一次已 RVF 的实现 run 武装一次性 rescope state，使主 agent 修复后的新一轮 RVF 全量重审「该实现 ∪ 本次 fix」。不要求粘贴 handoff。
argument-hint: "[可选：粘贴的 RVF handoff 正文 / handoff 路径，或 target run id]"
---

# RVF Reopen

Input: $ARGUMENTS

把这次调用当作显式触发 bundled `rvf-reopen` skill。不要依赖 Claude Code 从 plugin manifest 自动加载该 skill。

开始工作流前，先解析 bundled skill 目录并读取其 `SKILL.md`：

```bash
python3 - <<'PY'
from pathlib import Path
import os

candidates = []
root = os.environ.get("CLAUDE_PLUGIN_ROOT")
if root:
    candidates.append(Path(root) / "skills" / "rvf-reopen")
candidates.append(Path.home() / ".claude" / "plugins" / "cache" / "review-validate-fix-local" / "review-validate-fix" / "0.1.0" / "skills" / "rvf-reopen")
candidates.append(Path.home() / ".claude" / "local-marketplaces" / "review-validate-fix" / "plugins" / "review-validate-fix" / "skills" / "rvf-reopen")

for candidate in candidates:
    skill = candidate / "SKILL.md"
    if skill.is_file():
        print(candidate)
        break
else:
    raise SystemExit("rvf-reopen bundled skill not found")
PY
```

然后读取 `<printed-skill-dir>/SKILL.md` 并按其内容作为权威实现执行。`$ARGUMENTS` 可选（粘贴的 RVF handoff 正文 / 路径，或显式 target run id）；为空时本命令仍可工作——skill 会经 `rvf_rescope.py arm` 从 tracker 解析最近一次已 RVF 的实现 run。本命令只武装 rescope state，不直接启动新的 RVF review，也不提交。
