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
