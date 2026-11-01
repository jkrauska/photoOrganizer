#!/usr/bin/env bash
# Run the full photo-analysis pipeline end to end.
#
# Usage:
#   scripts/analysis_pipeline.sh                       # process all unprocessed photos
#   scripts/analysis_pipeline.sh -n 50                 # limit analyze + enrich to 50 photos
#   scripts/analysis_pipeline.sh -n 5 --no-geocode     # quick test, skip Nominatim
#   scripts/analysis_pipeline.sh --skip-analyze        # cluster + enrich only (reuse existing JSONL)
#   scripts/analysis_pipeline.sh --data-dir DIR        # sidecar location (default: $PHOTO_ANALYSIS_DIR or ~/gemma)

set -eo pipefail

LIMIT_ARGS=()
GEOCODE_ARGS=()
DATA_ARGS=()
SKIP_ANALYZE=0
SKIP_CLUSTER=0
SKIP_ENRICH=0
SKIP_INDEX=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--limit)       LIMIT_ARGS=(-n "$2"); shift 2 ;;
    --no-geocode)     GEOCODE_ARGS=(--no-geocode); shift ;;
    --data-dir)       DATA_ARGS=(--data-dir "$2"); shift 2 ;;
    --skip-analyze)   SKIP_ANALYZE=1; shift ;;
    --skip-cluster)   SKIP_CLUSTER=1; shift ;;
    --skip-enrich)    SKIP_ENRICH=1; shift ;;
    --skip-index)     SKIP_INDEX=1; shift ;;
    -h|--help)        sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

echo "==> uv sync --extra analysis"
uv sync --quiet --extra analysis

if [[ $SKIP_ANALYZE -eq 0 ]]; then
  echo "==> Step 1/4: photo-analyze (Gemma + InsightFace)"
  uv run photo-analyze "${LIMIT_ARGS[@]}" "${DATA_ARGS[@]}"
fi

if [[ $SKIP_CLUSTER -eq 0 ]]; then
  echo "==> Step 2/4: photo-cluster-faces (DBSCAN)"
  uv run photo-cluster-faces "${DATA_ARGS[@]}"
fi

if [[ $SKIP_ENRICH -eq 0 ]]; then
  echo "==> Step 3/4: photo-enrich (exiftool + ffprobe + Nominatim)"
  uv run photo-enrich "${LIMIT_ARGS[@]}" "${GEOCODE_ARGS[@]}" "${DATA_ARGS[@]}"
fi

if [[ $SKIP_INDEX -eq 0 ]]; then
  echo "==> Step 4/4: photo-index (SQLite for Datasette)"
  uv run photo-index "${DATA_ARGS[@]}"
fi
