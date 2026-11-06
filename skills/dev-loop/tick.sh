#!/usr/bin/env bash
# One headless dev-loop pass for cron. Only starts Claude when devq.py finds new work for you.
#   */10 * * * * bash /path/to/dev-loop/tick.sh >> ~/.local/state/merge-scheduler-dev/tick.log 2>&1
# Interactive alternative: in a Claude session, `/loop 5m /dev-loop`.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${MERGE_SCHEDULER_DEV_STATE:-$HOME/.local/state/merge-scheduler-dev}"
mkdir -p "$STATE"
exec 9>"$STATE/tick.lock"; flock -n 9 || { echo "$(date -u +%FT%TZ) previous pass still running"; exit 0; }

python3 "$HERE/tools/devq.py" --json > "$STATE/queue.json"
SIG="$(python3 - "$STATE/queue.json" <<'EOF'
import json, sys
rows = json.load(open(sys.argv[1]))["rows"]
print(" ".join(sorted(f"{r['key']}@{r['sha']}:{r['bucket']}" for r in rows if r["bucket"] not in ("ask", "hint", "waiting"))))
EOF
)"
if [[ -z "$SIG" ]] || [[ "$SIG" == "$(cat "$STATE/last-sig" 2>/dev/null)" ]]; then
  echo "$(date -u +%FT%TZ) nothing new"; exit 0
fi
echo "$SIG" > "$STATE/last-sig"
claude -p "/dev-loop" ${CLAUDE_FLAGS:-}
