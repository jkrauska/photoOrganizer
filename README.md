# Photo Organizer

Sorts and moves photos from a source drive into a `YEAR/MONTH/YYYY-MM-DD/` directory structure. Tracks all files in a SQLite database with SHA256 verification.

## Setup

```bash
uv sync
```

## Usage

```bash
# Dry run (default) - scan and plan, no files moved
uv run photo-organizer

# Dry run with verbose output
uv run photo-organizer -v

# Limit to first 100 files (for testing)
uv run photo-organizer --limit 100

# Actually move files (prompts for confirmation)
uv run photo-organizer --execute

# Resume an interrupted run
uv run photo-organizer --execute --resume
```

## Photo Analysis (Gemma 4 + InsightFace)

Analyzes the local Apple Photos library on-device (Apple Silicon, MLX + CoreML):
Gemma 4 (`mlx-community/gemma-4-31b-it-4bit` via `mlx-vlm`) writes a caption,
keywords, scene, subjects, setting and visible text per photo, and InsightFace
`buffalo_l` detects faces and emits 512-d embeddings that are clustered into people.

```bash
uv sync --extra analysis
brew install exiftool ffmpeg

scripts/analysis_pipeline.sh -n 5 --no-geocode   # quick test
scripts/analysis_pipeline.sh                     # full run, resumable

uv run photo-face-browser                        # label face clusters at http://localhost:8000/
uv run photo-index                               # rebuild photos.db after labeling
uv run datasette ~/gemma/photos.db -o            # faceted + full-text + map browsing
```

Steps (each is its own command and resumable, since everything is keyed by Photos UUID):

1. `photo-analyze` → `photo_analysis.jsonl` (Gemma fields + face embeddings; `--model` to swap VLMs)
2. `photo-cluster-faces` → `face_clusters.json` (DBSCAN, cosine; tune `--eps` / `--min-samples`)
3. `photo-enrich` → `metadata.jsonl` (exiftool, ffprobe, Nominatim reverse-geocode at ≤1 req/s)
4. `photo-index` → `photos.db` (SQLite + FTS5; dropped and rebuilt from the sidecars each run)

`photo-face-browser` writes `face_labels.json` (cluster or per-face → name). It binds to
`127.0.0.1` by default; `--host 0.0.0.0` exposes it on the LAN with no auth.

### Data directory

All sidecars live in one directory, `$PHOTO_ANALYSIS_DIR` or `~/gemma` by default
(override per command with `--data-dir`). That data is personal and is gitignored, so
it never lands in this repo. Note that `photos.db` here is unrelated to the organizer's
`photo_organizer.db`: it covers the Apple Photos library keyed by UUID, not the NAS tree.

### Constraints

- Python 3.11 only (`mlx-vlm` wheels).
- `pillow-heif` is required, or every HEIC iPhone photo is silently skipped.
- RAW files are analyzed through Apple's largest JPEG derivative (`analysis_path`).
- Gated Hugging Face models need `HF_TOKEN` exported (e.g. via a gitignored `.envrc`).

## Layout

Code lives under `src/photo_organizer/` as a proper package:

- `dates.py` — EXIF/filename/dirname date extraction
- `hashing.py` — SHA256 file hashing
- `db.py` — SQLite schema/connection
- `scanner.py` — source directory walk + cataloging
- `planner.py` — destination path computation, collision handling, ARW+XMP pairing
- `transfer.py` — parallel copy/verify/delete
- `report.py` — summary reporting
- `cli.py` — argument parsing and the batch-processing loop
- `state.py` — graceful-shutdown flag shared across modules
- `analysis/` — `analyze`, `cluster`, `enrich`, `index`, `face_browser`, plus `paths` for the data directory

## How It Works

1. **Scan** - Walks the source directory, extracts dates from EXIF/filenames/metadata, records everything in SQLite
2. **Plan** - Computes destination paths (`YEAR/MONTH/YYYY-MM-DD/`), handles filename collisions and ARW+XMP pairing
3. **Execute** - Copies each file, verifies SHA256 matches, then deletes the source. Asks for confirmation first.
4. **Report** - Summary of what was done

### Date Extraction Priority

1. EXIF DateTimeOriginal (JPG via Pillow, ARW via TIFF struct parser)
2. EXIF DateTimeDigitized / DateTime
3. Filename patterns (`2013-01-18 15.21.17.jpg`, `20170520-140151.jpg`, etc.)
4. Parent directory names (`2017-06-17/`, `2017/05/`)
5. File modification time (last resort)

### File Pairing

ARW and XMP files sharing the same base name are grouped together and placed in the same destination folder.

## Defaults

- **Source:** `/Volumes/Extreme SSD`
- **Dest:** `/Volumes/fileshares/photosCleanup2026`
- **DB:** `<dest>/photo_organizer.db`

Override with `--source`, `--dest`, `--db` flags.

## Inspecting the Database

```bash
# Check status counts
sqlite3 /Volumes/fileshares/photosCleanup2026/photo_organizer.db \
  "SELECT status, count(*) FROM files GROUP BY status"

# Check for errors
sqlite3 /Volumes/fileshares/photosCleanup2026/photo_organizer.db \
  "SELECT filename, error_message FROM files WHERE status='error'"

# See date source distribution
sqlite3 /Volumes/fileshares/photosCleanup2026/photo_organizer.db \
  "SELECT date_source, count(*) FROM files GROUP BY date_source"
```
