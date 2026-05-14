---
description: Run the RVF double-review, validate/fix, and handoff workflow
argument-hint: [optional RVF mode or scope]
---

# Review Validate Fix

Input: $ARGUMENTS

Treat this as an explicit invocation of the bundled `review-validate-fix` skill. Do not rely on Claude Code automatically loading that skill from the plugin manifest.

Before starting the workflow, resolve the bundled skill directory and read its `SKILL.md`:

```bash
python3 - <<'PY'
from pathlib import Path
import os

candidates = []
root = os.environ.get("CLAUDE_PLUGIN_ROOT")
if root:
    candidates.append(Path(root) / "skills" / "review-validate-fix")
candidates.append(Path.home() / ".claude" / "plugins" / "cache" / "review-validate-fix-local" / "review-validate-fix" / "0.1.0" / "skills" / "review-validate-fix")
candidates.append(Path.home() / ".claude" / "local-marketplaces" / "review-validate-fix" / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix")

for candidate in candidates:
    skill = candidate / "SKILL.md"
    if skill.is_file():
        print(candidate)
        break
else:
    raise SystemExit("review-validate-fix bundled skill not found")
PY
```

Then read `<printed-skill-dir>/SKILL.md` and follow it as the authoritative implementation. Use the current repository as the target unless `$ARGUMENTS` supplies a different path, mode, or review scope. Use only the active implementation bundled in this plugin: the installed RVF skill plus the Claude Code `hooks/stop.py` wrapper, which delegates to the current RVF scripts bundled in this plugin.
