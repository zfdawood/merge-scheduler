#!/usr/bin/env bash
# One merge-gate tick for cron: apply the schedule, and only if it raised events, hand them to Claude.
#   */5 * * * * bash /path/to/merge-gate/tick.sh >> ~/.local/state/merge-scheduler/tick.log 2>&1
# Optional env: GH_TOKEN, MERGE_SCHEDULER_CONFIG, MERGE_SCHEDULER_STATE, CLAUDE_FLAGS (e.g. a permission mode).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${MERGE_SCHEDULER_STATE:-$HOME/.local/state/merge-scheduler}"
mkdir -p "$STATE"
exec 9>"$STATE/tick.lock"; flock -n 9 || { echo "$(date -u +%FT%TZ) previous tick still running"; exit 0; }

EVENTS="$STATE/events-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 "$HERE/tools/gate.py" --apply --events "$EVENTS" | tee "$STATE/last-schedule.md" | sed -n '/^## Actions/,$p'

if [[ "$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$EVENTS")" != "0" ]]; then
  claude -p "/merge-gate handle the events in $EVENTS" ${CLAUDE_FLAGS:-}
else
  rm -f "$EVENTS"
fi
