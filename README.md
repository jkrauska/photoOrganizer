# Photo Organizer

Empties ad-hoc photo dumps into one canonical store laid out as `YYYY/MM/YYYY-MM-DD/`,
tracking every file in a SQLite catalog. A source file is **deleted only after** its bytes
are proven to be in the store: either its copy was re-hashed and matched, or (for a
duplicate) the existing canonical copy was re-hashed from disk and matched.

Anything uncertain stays exactly where it was:

| Left in place | Why |
|---|---|
| `review` | no trustworthy capture date (EXIF, filename, or a `YYYY-MM-DD` folder) — only file mtime |
| `ignored` | file type not handled (catalogs, previews, documents, ...) |
| `error` | any verification failure; the source is never deleted |

Never entered: NAS recycle bins and snapshots (`@Recycle`, `@Recently-Snapshot`, `#recycle`,
`@eaDir`, ...) and app bundles whose insides must not move (`.photoslibrary`, `.cocatalog`,
`.lrdata`, ...). Folders left empty (or holding only `.DS_Store`-style junk) are pruned.

## Setup

```bash
uv sync
brew install exiftool
```

## Usage

```bash
# 1. Inventory (dry run): catalog every file, read dates, plan destinations. Nothing moves.
uv run photo-organizer --source /Volumes/fileshares/photos --source /Volumes/Photowork \
    --dest /Volumes/<fresh-mount>

# 2. Watch progress (images/s, MB/s, ETA, per-source counts, errors) in the browser.
uv run photo-organizer-status

# 3. Execute: move + verify + delete sources, batch by batch (--yes skips the per-batch prompt).
uv run photo-organizer --source ... --dest /Volumes/<fresh-mount> --execute
```

Running the inventory first makes the ETA cover the whole job, since every file is
already cataloged as pending. Runs are resumable: rerun the same command after a crash or
Ctrl-C (press twice to force quit); files interrupted mid-copy are cleaned up and redone.

A destination folder written by an older run (e.g. `photosCleanup2026`) can be used as a
source. Its `photo_organizer.db` is read to recover each file's original path and to send
files that run dated only by mtime to `review` instead of trusting their folder.

### Dates

1. ExifTool: `SubSecDateTimeOriginal`, `DateTimeOriginal`, `CreateDate`, `MediaCreateDate`
   (videos read with `-api QuickTimeUTC`, so they keep local time + offset). Stored as
   `taken_at` with time and offset when known.
2. Filename patterns (`2013-01-18 15.21.17.jpg`, `20170520-140151.jpg`, `IMG_20170520_140151.jpg`).
3. The nearest `YYYY-MM-DD` folder. Year-only or month-only folders are too vague and go to review.

A photo and its sidecars (`IMG.xmp`, `IMG.ARW.xmp`, `.aae`, `.thm`) are grouped by folder +
stem and always filed together, using the best date in the group (RAW EXIF preferred).

### Catalog

The live catalog is local, `~/.photo-organizer/catalog.db` (SQLite over SMB is unreliable),
and is backed up to `<dest>/photo_organizer_catalog.db` after every batch.

```bash
sqlite3 ~/.photo-organizer/catalog.db "SELECT status, count(*) FROM files GROUP BY status"
sqlite3 ~/.photo-organizer/catalog.db \
  "SELECT original_path, error_message FROM files WHERE status IN ('review','ignored','error')"
```

### Tests

```bash
uv run pytest
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
it never lands in this repo. Note that `photos.db` here is separate from the organizer's
catalog: it covers the Apple Photos library keyed by UUID, not the NAS tree.

### Constraints

- Python 3.11 only (`mlx-vlm` wheels).
- `pillow-heif` is required, or every HEIC iPhone photo is silently skipped.
- RAW files are analyzed through Apple's largest JPEG derivative (`analysis_path`).
- Gated Hugging Face models need `HF_TOKEN` exported (e.g. via a gitignored `.envrc`).

## Layout

Code lives under `src/photo_organizer/`:

- `cli.py`: arguments and the batch loop (`run()`)
- `scanner.py`: source walk, NAS/bundle skips, sidecar grouping, prior-catalog lookups
- `dates.py`: ExifTool batch date reads, filename and folder patterns
- `planner.py`: destination folders and review routing
- `transfer.py`: verified copy, duplicate removal, crash recovery
- `cleanup.py`: pruning emptied source folders
- `db.py`: catalog schema, migrations, backup to the destination
- `progress.py` / `status.py`: live run state and the status web page
- `report.py`, `hashing.py`, `state.py`, `constants.py`
- `analysis/`: `analyze`, `cluster`, `enrich`, `index`, `face_browser`, plus `paths` for the data directory
