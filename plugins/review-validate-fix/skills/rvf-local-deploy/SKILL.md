---
name: rvf-local-deploy
description: 当在 review-validate-fix 仓库中，用户要求 deploy、local install、sync、发布到本机 Codex plugin cache，或从当前 checkout 配置本机 stable RVF plugin/Stop hook 时使用。
---

# RVF Local Deploy

本 skill 是稳定 trampoline。即使当前内容来自过期的 installed cache，也只委托固定 canonical
checkout 中的 runner；不要从 installed plugin/cache 反向部署，也不要在本文件复制部署实现。

## Phase Zero

确认用户明确要求本机部署后，运行：

```bash
python3 /Users/bominzhang/Documents/GitHub/review-validate-fix/scripts/rvf_local_deploy.py --json
```

runner 负责 canonical source 门禁、并发锁、部署、双 hook、installed-runtime 校验与 commit
delta。默认拒绝 deploy scope 的未提交改动；只有用户明确授权 dirty deployment 后，才可重跑并
追加 `--allow-dirty`。禁止在 canonical runner 失败后降级执行 installed payload 中的脚本。

## Completion

仅当 runner 退出 0 且 JSON `status` 为 `ok` 才报告部署成功。最终回复包含 canonical HEAD、
installed before/after、是否曾 stale、部署目标和 verification 中的 commit delta。

退出 2 表示 canonical source/dirty 门禁失败；退出 3 表示已有部署 writer。两者都必须停止，
不得绕过。此 skill 不自动重启 Cline Kanban listener，也不 pull/fetch 远端。
