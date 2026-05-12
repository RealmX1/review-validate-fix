---
name: rvf-tracker-dashboard-session
description: Use in the review-validate-fix repository when the user asks to update, restart, refresh, host, or fix the RVF diff tracker / tracker dashboard GUI session, especially when a browser page such as localhost/127.0.0.1:8765 shows an old interface, refused connection, or stale runtime. Keep the same port by default so the developer only needs to refresh the existing browser tab.
---

# RVF Tracker Dashboard Session

Dev-only source helper. This skill is kept under `dev-only/` so it is not
installed into the stable RVF plugin runtime.

This skill manages the local RVF tracker dashboard web server. It is for the
dashboard served by `tracker_dashboard.py`, not for Cline Kanban or old
Vibe-Kanban processes.

## Rules

- Keep the existing dashboard port by default. Do not move from `8765` to
  another port unless the user explicitly asks or the original port cannot be
  recovered.
- Use tmux for a persistent server. Do not use bare foreground commands,
  `nohup ... &`, or ad hoc background jobs for the final hosted dashboard.
- Prefer the installed stable runtime:
  `/Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/tracker_dashboard.py`.
- Use the source checkout only when the user explicitly asks for a dev/source
  dashboard:
  `/Users/bominzhang/Documents/GitHub/review-validate-fix/plugins/review-validate-fix/skills/review-validate-fix/scripts/tracker_dashboard.py`.
- Stop only processes that are clearly `tracker_dashboard.py` instances for
  the target port. Do not kill unrelated listeners.
- If using the in-app browser, refresh or navigate the existing tab after the
  server is restarted; the expected UX is that the developer keeps the same
  URL and presses refresh.

## Workflow

1. Identify the intended port.

   Use the user's current URL if available. Otherwise inspect existing tracker
   listeners and default to `8765`.

   ```bash
   lsof -nP -iTCP:8765 -sTCP:LISTEN || true
   ps aux | rg 'tracker_dashboard.py|8765|8766' | rg -v 'rg '
   ```

2. Inspect the current listener before stopping it.

   Capture the PID, command, repo path, log root, and whether it is installed
   stable or source/dev runtime. If the process is not `tracker_dashboard.py`,
   stop and report the conflict instead of killing it.

   ```bash
   lsof -nP -iTCP:<port> -sTCP:LISTEN || true
   ps -p <pid> -o pid,ppid,stat,etime,command
   ```

3. Stop stale dashboard sessions on that same port.

   Prefer killing the tmux session if the process is in a known dashboard tmux
   session. If no tmux session owns it, kill only the verified dashboard PID.

   ```bash
   tmux list-sessions | rg 'rvf-tracker-dashboard' || true
   tmux kill-session -t rvf-tracker-dashboard-latest 2>/dev/null || true
   kill <verified-dashboard-pid> 2>/dev/null || true
   ```

4. Start latest installed stable dashboard in tmux on the same port.

   Keep the repo and log root explicit. The default target repo for this
   repository is the main checkout.

   ```bash
   tmux new-session -d -s rvf-tracker-dashboard-latest \
     'cd /Users/bominzhang/Documents/GitHub/review-validate-fix && exec python3 /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/tracker_dashboard.py --repo /Users/bominzhang/Documents/GitHub/review-validate-fix --log-root /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/state --port <port> --no-open --poll-seconds 5'
   ```

   If the user is intentionally viewing another worktree, preserve that repo
   path but still use the installed stable script and state root unless they
   ask for dev/source runtime.

5. Verify the same URL is serving the new session.

   ```bash
   lsof -nP -iTCP:<port> -sTCP:LISTEN
   curl -s http://127.0.0.1:<port>/api/snapshot | python3 -m json.tool | sed -n '1,80p'
   tmux list-sessions | rg 'rvf-tracker-dashboard-latest'
   ```

   The snapshot must show the expected `repo.repo_path` and installed
   `repo.tracker_dir` under
   `/Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/state/diff-tracker`.

6. Refresh the browser.

   If Browser is available and the user is looking at the in-app browser, open
   or refresh the same URL, for example `http://127.0.0.1:8765/`. Do not tell
   the user to switch to a new port when the same port was recoverable.

## Response

Report:

- the preserved URL;
- tmux session name;
- new listener PID;
- script path and repo path used;
- whether any stale dashboard PID was stopped;
- the verification result from `/api/snapshot`.

Mention any unrelated dirty worktree paths only if they are relevant to the
dashboard update.
