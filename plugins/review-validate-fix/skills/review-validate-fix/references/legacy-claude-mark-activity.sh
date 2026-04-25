#!/bin/bash
# Tiny helper: called by PostToolUse on Write|Edit. Appends a monotonic tick
# to a session-keyed activity file. stop-review-validate-fix.sh uses this to dedup:
# skip dispatch when there have been no Write/Edit ticks since the last
# dispatch (prevents wasted Opus reviews when the main session's turn did
# not actually modify files — e.g. it just reported "Review clean").

set -uo pipefail

SID=$(jq -r '.session_id // "default"' 2>/dev/null | tr -dc 'A-Za-z0-9_-')
[ -z "$SID" ] && exit 0

STATE_DIR="$HOME/.claude/hooks/state"
mkdir -p "$STATE_DIR"

printf '%s\n' "$(date +%s%N)" >> "$STATE_DIR/${SID}.activity"
exit 0
