"""Walk the source directory and catalog files into the database."""

import datetime
import os

from photo_organizer.constants import ALL_EXTENSIONS, SKIP_NAMES
from photo_organizer.dates import extract_date
from photo_organizer.state import shutdown_requested


def should_skip(name):
    """Check if a file/directory should be skipped."""
    if name in SKIP_NAMES:
        return True
    if name.startswith('._'):
        return True
    return False


def compute_group_key(filename):
    """Compute a grouping key for file pairing (ARW + XMP)."""
    stem = os.path.splitext(filename)[0]
    return stem.lower()


def scan_source(source_root, conn, verbose=False, limit=0):
    """Walk the source directory and catalog all files into the database.
    Returns (new_count, list_of_inserted_ids)."""
    new_count = 0
    skipped = 0
    already_seen = 0
    done = False
    inserted_ids = []

    for dirpath, dirnames, filenames in os.walk(source_root):
        if done:
            break
        # Filter out hidden/system directories
        dirnames[:] = sorted(d for d in dirnames if not should_skip(d))

        for fname in sorted(filenames):
            if shutdown_requested() or (limit and new_count >= limit):
                done = True
                break

            if should_skip(fname):
                skipped += 1
                continue

            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALL_EXTENSIONS:
                skipped += 1
                if verbose:
                    print(f"  [skip] Unknown extension: {fname}")
                continue

            filepath = os.path.join(dirpath, fname)

            # Check if already in DB (don't count toward limit)
            existing = conn.execute(
                "SELECT id FROM files WHERE original_path = ?", (filepath,)
            ).fetchone()
            if existing:
                already_seen += 1
                continue

            # Get file metadata
            try:
                stat = os.stat(filepath)
                file_size = stat.st_size
                mtime = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
            except OSError as e:
                if verbose:
                    print(f"  [error] Can't stat {filepath}: {e}")
                continue

            # Extract date
            dt, exif_str, date_source = extract_date(filepath, source_root)

            cursor = conn.execute(
                """INSERT INTO files
                   (filename, original_path, file_size, file_modified_date,
                    exif_creation_date, date_source, group_key, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (fname, filepath, file_size, mtime,
                 exif_str, date_source, compute_group_key(fname))
            )
            inserted_ids.append(cursor.lastrowid)
            new_count += 1

            if new_count % 100 == 0:
                conn.commit()
                if limit:
                    pct = new_count * 100 // limit
                    print(f"\r  Scanned {new_count:,}/{limit:,} new files ({pct}%)...", end='', flush=True)
                else:
                    print(f"\r  Scanned {new_count:,} new files...", end='', flush=True)

    conn.commit()
    parts = [f"{new_count:,} new"]
    if already_seen:
        parts.append(f"{already_seen:,} already in DB")
    if skipped:
        parts.append(f"{skipped:,} skipped")
    print(f"\r  Scanned: {', '.join(parts)}          ")
    return new_count, inserted_ids
