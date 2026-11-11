#!/usr/bin/env bash
# Keep the analysis caught up while the queue worker adds photos to the store: every INTERVAL
# seconds, find faces in photos not yet scanned, re-cluster (names new faces) when any were
# found, then summarize day folders not yet summarized (the people found go into the summary).
# Each pass skips what is already done, so a pass with nothing new is quick. After the worker
# has stopped, one more pass catches its last files, then this exits.
#
# Usage: scripts/ark-follow.sh        (make ark-follow runs it in the background)
cd "$(dirname "$0")/.." || exit 1
DB=${ANALYSIS_DB:-$HOME/.photo-organizer/analysis.db}
INTERVAL=${INTERVAL:-600}
faces() { sqlite3 "$DB" "SELECT COUNT(*) FROM faces"; }
worker() { pgrep -f "photo-organizer-queue.* worker" > /dev/null; }
ark() { uv run --inexact --extra analysis photo-ark-analyze "$@"; }

pass() {
  local before
  before=$(faces)
  echo "== $(date '+%F %T') faces in new photos"
  ark run --no-vlm
  if [[ $(faces) != "$before" ]]; then
    echo "== $(date '+%F %T') $(( $(faces) - before )) new faces; clustering"
    while pgrep -f "photo-ark-analyze cluster" > /dev/null; do sleep 30; done
    ark cluster
  fi
  echo "== $(date '+%F %T') summaries of new days"
  ark days
}

echo "== $(date '+%F %T') following the store every ${INTERVAL}s while the queue worker runs"
while worker; do
  pass
  sleep "$INTERVAL"
done
pass
echo "== $(date '+%F %T') queue worker stopped; caught up, done"
