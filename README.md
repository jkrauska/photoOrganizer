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

Built for macOS: it reads Apple Photos libraries, uses ExifTool for dates, and the analysis
side needs Apple Silicon. The organizer itself only needs Python 3.11 and `exiftool`.

## Setup

```bash
uv sync
brew install exiftool
cp config.mk.example config.mk   # then put your own SOURCES and DEST in it
```

`config.mk` is gitignored, so your paths stay out of the repo. Everything it sets can also
be passed on the command line (`make run SOURCES=... DEST=...`).

## Usage

The Makefile wraps the common runs:

```bash
make inventory              # dry run over all sources
make run LIMIT=200          # move + verify + delete, prompting per batch (YES=1 to skip prompts)
make status                 # live page at http://localhost:8077/
make enrich                 # Gemma 4 + InsightFace pipeline (Apple Photos library)
make help                   # all targets and variables
```

Logs land in `~/.photo-organizer/logs/`. Under the hood:

```bash
# 1. Inventory (dry run): catalog every file, read dates, plan destinations. Nothing moves.
uv run photo-organizer --source /Volumes/Backup/Pictures --source /Volumes/OldLaptop/Photos \
    --dest /Volumes/photoArk

# 2. Watch progress (images/s, MB/s, ETA, per-source counts, errors) in the browser.
uv run photo-organizer-status

# 3. Execute: move + verify + delete sources, batch by batch (--yes skips the per-batch prompt).
uv run photo-organizer --source ... --dest /Volumes/photoArk --execute
```

Point `--dest` at a disk with room for everything; the store is referred to below as the
"ark" and nothing outside it is ever written.

Running the inventory first makes the ETA cover the whole job, since every file is
already cataloged as pending. Runs are resumable: rerun the same command after a crash or
Ctrl-C (press twice to force quit); files interrupted mid-copy are cleaned up and redone.

A destination folder written by an older run of this tool can be used as a
source. Its `photo_organizer.db` is read to recover each file's original path and to send
files that run dated only by mtime to `review` instead of trusting their folder.

### Apple Photos library

The local Photos library is imported a year at a time as queue items named `photos:YYYY`,
always copy-only: nothing in the library is written or deleted.

```bash
make queue-photos                    # every year in the library, oldest first
make queue-photos YEARS="2010 2016"  # just these years (or type photos:2016 in the status page)
```

Each asset files its original byte for byte under its real name, dated by Photos (including
date fixes made there). It also files the RAW of a RAW+JPEG pair, the Live Photo video
(`IMG_1234.MOV`), the current edit (`IMG_1234_edited.HEIC`) and an XMP sidecar
(`IMG_1234.HEIC.xmp`: title, caption, keywords, people, GPS). Content already in the store is
not copied again, and its sidecar goes next to the stored copy. Assets only in iCloud are
left for review and retried when the year is queued again. Rows keep the Photos ID in
`photos_uuid`. Re-running a year copies only assets added since.

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
make browse                                      # faceted + full-text + map browsing (Datasette)
```

Steps (each is its own command and resumable, since everything is keyed by Photos UUID):

1. `photo-analyze` → `photo_analysis.jsonl` (Gemma fields + face embeddings; `--model` to swap VLMs)
2. `photo-cluster-faces` → `face_clusters.json` (DBSCAN, cosine; tune `--eps` / `--min-samples`)
3. `photo-enrich` → `metadata.jsonl` (exiftool, ffprobe, Nominatim reverse-geocode at ≤1 req/s)
4. `photo-index` → `photos.db` (SQLite + FTS5; dropped and rebuilt from the sidecars each run)

`photo-face-browser` writes `face_labels.json` (cluster or per-face → name). It binds to
`127.0.0.1` by default; `--host 0.0.0.0` exposes it on the LAN with no auth.

### Data directory

All sidecars live in one directory, `$PHOTO_ANALYSIS_DIR` or `~/.photo-organizer/analysis`
by default (override per command with `--data-dir`). Captions, face embeddings and names are
personal, so they stay outside the repo and the patterns that match them are gitignored.
Note that `photos.db` here is separate from the organizer's catalog: it covers the Apple
Photos library keyed by UUID, not the store tree.

### Constraints

- Python 3.11 only (`mlx-vlm` wheels).
- `pillow-heif` is required, or every HEIC iPhone photo is silently skipped.
- RAW files are analyzed through Apple's largest JPEG derivative (`analysis_path`).
- Gated Hugging Face models need `HF_TOKEN` exported (e.g. via a gitignored `.envrc`).
- Reverse-geocoding needs a contact for the Nominatim User-Agent, as its
  [usage policy](https://operations.osmfoundation.org/policies/nominatim/) requires: export
  `PHOTO_ORGANIZER_CONTACT` (your real email or a project URL) or pass `--contact`, else run
  with `--no-geocode`. Nominatim answers 403 to placeholder contacts, which shows up as a
  `_error` location on every geotagged photo.

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
- `analysis/ark*`, `days`, `shuffle`, `timelapse`, `face_finder`, `screenshots`: the same
  analysis over the store itself, and the pages `ui.py` serves

## Privacy

Nothing personal is committed: the catalog, the analysis database, captions, face
embeddings, face names and crops all live under `~/.photo-organizer/` and are gitignored,
and local disk paths belong in `config.mk`. Everything runs on your machine except
reverse-geocoding, which sends only rounded coordinates to Nominatim and can be turned off
with `--no-geocode`. The web pages bind to `127.0.0.1` unless you pass `--host`; on the LAN
they have no authentication, so only do that on a network you trust.

## License

MIT, see [LICENSE](LICENSE).
