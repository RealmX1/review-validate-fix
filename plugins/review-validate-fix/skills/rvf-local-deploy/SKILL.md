---
name: rvf-local-deploy
description: 当在 review-validate-fix 仓库中，用户要求 deploy、local install、sync、发布到本机 Codex plugin cache，或从当前 checkout 配置本机 stable RVF plugin/Stop hook 时使用。
---

# RVF Local Deploy

本 skill 用于把本仓库的 canonical RVF plugin 部署到本机 Codex plugin 空间。它只适用于 `review-validate-fix` 仓库。

## Preconditions

- 从仓库根目录工作：`/Users/bominzhang/Documents/GitHub/review-validate-fix`。
- 把 `plugins/review-validate-fix/` 视为 canonical plugin payload source；此外 repo 顶层 `core/`（host-agnostic 核心）与 `adapters/`（各 host adapter）也是 canonical source，部署时由 installer 的 `deploy_payload` / `vendor_pyroot` vendored 进每个 payload 根（连同 `.rvf-pyroot` 哨兵）。
- 不要从 `~/plugins/review-validate-fix` 或 `~/.codex/plugins/cache/...` 部署；这些是安装产物。
- 先检查 `git status --short`。plugin/deployment scope 外的 background WIP 可以保持 dirty；但除非用户明确要求部署 dirty worktree，不要部署未提交的 plugin/runtime 改动。

## Default Deploy

安装前先运行 contract check：

```bash
python3 scripts/check_plugin_contracts.py
```

通过后安装 plugin 并刷新 stable Stop hook：

```bash
python3 scripts/install_to_codex.py --configure-stop-hook
```

这会更新本机 stable channel：

- `~/plugins/review-validate-fix`
- `~/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0`
- `~/.agents/plugins/marketplace.json`
- `~/.codex/config.toml`
- `~/.codex/hooks.json`

installer 还会写入部署日志，用于追踪「哪些 plugin 状态已经被部署」以及「部署时对应哪个 RVF trajectory / analysis run」：

- `~/plugins/review-validate-fix/skills/review-validate-fix/state/deployments/deployments.jsonl`
- `~/plugins/review-validate-fix/skills/review-validate-fix/state/deployments/latest-deployment.json`
- `~/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/review-validate-fix/state/deployments/deployments.jsonl`
- `~/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/review-validate-fix/state/deployments/latest-deployment.json`

每条记录应至少包含 source git HEAD/branch/status、runtime hash、安装目标、hook 选项，以及 `RVF_RUN_DIR` / latest RVF run pointer 中可解析出的 run summary 和 analysis artifact paths。

安装产物中的每个 `skills/*/SKILL.md` H1 heading 还会带 `deployed <commit-prefix>` stamp；source checkout 内的 canonical `SKILL.md` 不应带该 stamp。

## Post-Deploy Checks

验证 installed runtime，而不是只验证 source checkout：

```bash
test -f /Users/bominzhang/plugins/review-validate-fix/.codex-plugin/plugin.json
test -f /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/codex_stop_hook_router.py
python3 -m py_compile /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/codex_stop_hook_router.py
test -f /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/state/deployments/latest-deployment.json
test -f /Users/bominzhang/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/review-validate-fix/state/deployments/latest-deployment.json
rg -n "\\[deployed [0-9a-f]{12}(-dirty)?\\]" /Users/bominzhang/plugins/review-validate-fix/skills/*/SKILL.md
rg -n "\\[deployed [0-9a-f]{12}(-dirty)?\\]" /Users/bominzhang/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/*/SKILL.md
```

还要验证 **vendor-on-install** 的 vendored payload 真落地——这是 S1 引入的运行期不变量：部署后的 `trajectory_distill.py` 经 `_rvf_pyroot` 哨兵依赖 vendored 的 `core/` + `adapters/` + `.rvf-pyroot`。若 vendoring 静默失败，会出现「部署检查全过、运行期 `ModuleNotFoundError`」（正是 vendor-on-install 要消灭的漂移；部署前门 `check_plugin_contracts.py` 对此零感知）：

```bash
# vendored core/adapters + 哨兵已落进 payload 根
test -f /Users/bominzhang/plugins/review-validate-fix/.rvf-pyroot
test -f /Users/bominzhang/plugins/review-validate-fix/core/transcript/models.py
test -f /Users/bominzhang/plugins/review-validate-fix/core/subagents/models.py
test -f /Users/bominzhang/plugins/review-validate-fix/adapters/codex/subagent.py
test -f /Users/bominzhang/plugins/review-validate-fix/adapters/claude_code/subagent.py
# import-smoke：deployed facade 经哨兵 bootstrap 能 import vendored core（exit 0 = vendoring + bootstrap 健康）
python3 /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_distill.py -h >/dev/null
```

任一 vendored 校验失败 = 部署损坏（vendoring / bootstrap 没生效），不要当成功收尾；重跑 `install_to_codex.py` 或排查 `deploy_payload` / `vendor_pyroot`。

如果部署的是具体功能改动，还要检查 installed 版本中的相关文件。示例：

```bash
rg -n "SCHEMA_VERSION|ANALYSIS_SCHEMA_VERSION" \
  /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/diff_tracker.py \
  /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/analysis_artifacts.py
```

## 实际部署后：汇总本次新投入使用的 commit

仅在**确实执行了安装**（installer 跑成功、向 deploy log 追加了新条目）后做；在 Failure Gate 处中止、根本没安装时跳过本节。

目的：最终回复不仅要说「部署到了哪个 HEAD」，还要明确「这次部署相对**上一次部署**，新投入使用了哪些 commit」——这是用户每次实际部署都想在收尾看到的 delta。

数据源是 deploy log `deployments.jsonl`：installer 每次安装都向它 append 一条含 `source.head` 的记录。本次安装写入的是**最后一行**，上一次部署是**倒数第二行**；两者的 `source.head` 做 `git log` 区间即得本次新增 commit：

```bash
JSONL=/Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/state/deployments/deployments.jsonl
python3 - "$JSONL" <<'PY'
import json, sys, subprocess, pathlib
lines = [l for l in pathlib.Path(sys.argv[1]).read_text().splitlines() if l.strip()]
heads = [json.loads(l).get("source", {}).get("head") for l in lines]
new = heads[-1]
prev = heads[-2] if len(heads) >= 2 else None
print("本次部署 HEAD:", (new or "?")[:12])
if not prev:
    print("（首次记录的部署，无前序部署可对比）"); raise SystemExit
print("上次部署 HEAD:", prev[:12])
fwd = subprocess.run(["git", "log", "--oneline", "--no-decorate", f"{prev}..{new}"], capture_output=True, text=True)
back = subprocess.run(["git", "log", "--oneline", "--no-decorate", f"{new}..{prev}"], capture_output=True, text=True)
if fwd.returncode or back.returncode:
    print("（区间计算失败：前序 HEAD 可能已不在当前 repo——按 describe/head 直述，不做区间）"); raise SystemExit
fwd_lines = [x for x in fwd.stdout.splitlines() if x.strip()]
back_lines = [x for x in back.stdout.splitlines() if x.strip()]
if not fwd_lines and not back_lines:
    print("本次与上一次部署为同一 HEAD，无新增 commit。")
else:
    if fwd_lines:
        print(f"新投入使用 {len(fwd_lines)} 个 commit（上次 → 本次）：")
        for x in fwd_lines: print("  +", x)
    if back_lines:
        print(f"⚠️ 另有 {len(back_lines)} 个 commit 不在本次 HEAD 上（回滚/分叉，本次相对上次回退了它们）：")
        for x in back_lines: print("  -", x)
PY
```

把这段输出里「新投入使用的 commit 列表」（及回滚/分叉警告，如有）原样纳入最终回复。三种边界都要如实呈现：首次部署无基线、与上次同一 HEAD 无新增、以及回滚/分叉时哪些 commit 被回退。

## 重启 Kanban listener（如适用）

本 skill 只部署 plugin 文件，不重启任何运行进程。但若某次部署后确实需要重启 RVF 所拥有/复用的 Kanban listener（tmux 会话 `cline-kanban` / `cline-kanban-<port>`），**不要按进程名杀**（`pkill -f kanban` / `killall node` 会误杀同机并存的其它 kanban/mkanban/node listener），**也不要盲目重建 tmux 会话**（会丢失富交互 PATH）。正确做法：按端口反查唯一 PID，用 `kill -9 <pid>` 让 `while true` 监督脚本（`run-cline-kanban-<port>-service.sh`）原地重拉，PATH/cwd 经存活的监督进程继承。

完整步骤、监督脚本退出码契约（130/143 永久停服、其它码 5s 重拉），以及 `kill -9 <pid>` 只命中单个 PID（不杀子进程/不杀进程组；子 agent 经 PTY 挂断二次退出，需 `pgrep -P` 单独核验）的机制说明，见：

`~/.claude/skills/cline-kanban-local-deploy/references/kanban-runtime-upgrades.md` 的 "Preferred restart: supervised kill-by-PID" 一节。

## Failure Gates

遇到以下情况时停止，不要安装：

- `scripts/check_plugin_contracts.py` fails.
- 当前 checkout 不是预期 source repo。
- 部署会复制无关的未提交 plugin/runtime 改动。
- 用户要求 stable deployment，但 checkout 不在预期 branch/tag/commit。

最终回复中说明 installer 输出摘要、post-deploy checks、deploy log 路径、**本次相对上一次部署新投入使用的 commit 列表**（见「实际部署后：汇总本次新投入使用的 commit」一节；仅实际安装后给出），以及哪些 dirty paths 被有意保留未动。
