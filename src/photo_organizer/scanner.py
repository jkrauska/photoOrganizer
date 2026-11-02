"""Walk source directories and catalog files into the database."""

import datetime
import os
import sqlite3

from photo_organizer.constants import (
    ALL_EXTENSIONS, MEDIA_EXTENSIONS, PRIOR_CATALOG_NAME, SIDECAR_EXTENSIONS,
    SKIP_DIR_SUFFIXES, SKIP_NAMES,
)
from photo_organizer.dates import (
    DEFAULT_EXIF_WORKERS, date_from_dirpath, date_from_filename, read_exif_dates,
)
from photo_organizer.cameras import camera_name
from photo_organizer.db import BACKUP_NAME, NOW_SQL
from photo_organizer.progress import set_progress
from photo_organizer.state import shutdown_requested


def should_skip(name):
    return name in SKIP_NAMES or name.startswith('._')


def should_skip_dir(name):
    return should_skip(name) or name.lower().endswith(SKIP_DIR_SUFFIXES)


def compute_group_key(dirpath, filename):
    """Key pairing a media file with its sidecars: same folder, same stem (IMG.xmp or IMG.ARW.xmp)."""
    stem, ext = os.path.splitext(filename)
    if ext.lower() in SIDECAR_EXTENSIONS:
        inner_stem, inner_ext = os.path.splitext(stem)
        if inner_ext.lower() in MEDIA_EXTENSIONS:
            stem = inner_stem
    return os.path.join(dirpath, stem).lower()


def is_within(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class PriorCatalogs:
    """Read-only lookups into catalogs left at a source root by earlier runs of this tool.

    A file an earlier run filed by mtime sits in a dated folder, so without this its
    folder name would be mistaken for a real date.
    """

    def __init__(self):
        self._catalogs = {}     # source_root -> {dest_path: row} (or None without a catalog)

    def _load(self, source_root):
        """Read the whole catalog once: it usually sits on the NAS and has no dest_path index,
        so a query per file rescanned the table over SMB (minutes for a few thousand files)."""
        for name in (PRIOR_CATALOG_NAME, BACKUP_NAME):
            path = os.path.join(source_root, name)
            if os.path.exists(path):
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                try:
                    return {r['dest_path']: r for r in conn.execute(
                        "SELECT dest_path, original_path, date_source FROM files "
                        "WHERE status = 'done' AND dest_path IS NOT NULL")}
                finally:
                    conn.close()
        return None

    def lookup(self, source_root, filepath):
        if source_root not in self._catalogs:
            self._catalogs[source_root] = self._load(source_root)
        catalog = self._catalogs[source_root]
        return catalog.get(filepath) if catalog else None


class PushbackIter:
    def __init__(self, it):
        self._it = iter(it)
        self._pushed = []

    def __iter__(self):
        return self

    def __next__(self):
        return self._pushed.pop() if self._pushed else next(self._it)

    def push_back(self, item):
        self._pushed.append(item)


def iter_source_files(source_roots, dest_root, known_paths):
    """Yield (source_root, dirpath, filename) for every file not yet in the catalog, folder by folder."""
    return PushbackIter(_walk(source_roots, dest_root, known_paths))


def _walk(source_roots, dest_root, known_paths):
    for root in source_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if not should_skip_dir(d) and not is_within(os.path.join(dirpath, d), dest_root)
            )
            for fname in sorted(filenames):
                if should_skip(fname) or fname in (PRIOR_CATALOG_NAME, BACKUP_NAME):
                    continue
                if os.path.join(dirpath, fname) in known_paths:
                    continue
                yield root, dirpath, fname


def _resolve_date(filepath, source_root, exif_hit, prior):
    """Return (taken_at, raw_exif, date_source, origin_path)."""
    if exif_hit:
        dt, raw = exif_hit
        return dt.isoformat(), raw, 'exif', prior['original_path'] if prior else None
    filename = os.path.basename(filepath)
    d = date_from_filename(filename)
    if d:
        return d.isoformat(), None, 'filename', prior['original_path'] if prior else None
    if prior:
        # The folder this file sits in was chosen by an earlier run; trust it only as much as that run did.
        if prior['date_source'] in ('exif', 'filename', 'dirname'):
            d = date_from_dirpath(filepath, source_root)
            if d:
                return d.isoformat(), None, 'dirname', prior['original_path']
        return None, None, 'mtime', prior['original_path']
    d = date_from_dirpath(filepath, source_root)
    if d:
        return d.isoformat(), None, 'dirname', None
    return None, None, 'mtime', None


BATCH_BYTES = 50 << 30      # a batch also ends after this much data, so huge videos don't make hours-long batches


def scan_batch(file_iter, conn, known_paths, prior, limit, verbose=False, exif_workers=DEFAULT_EXIF_WORKERS,
               max_bytes=BATCH_BYTES):
    """Catalog about `limit` new media/sidecar files (or `max_bytes` of them) from file_iter, ending at a
    folder boundary so a photo and its sidecars are never split across batches. A folder holding more
    than twice `limit` files is split between photos instead (sidecars share their photo's stem and
    sort next to it).

    Unhandled file types are recorded as 'ignored' (left in place) and don't count toward the limit.
    Returns (inserted_ids, exhausted).
    """
    batch = []
    batch_bytes = 0
    ignored = 0
    exhausted = True
    set_progress(conn, phase='scanning', scanned_in_batch=0)
    for item in file_iter:
        if (len(batch) + ignored) % 500 == 499:
            set_progress(conn, scanned_in_batch=len(batch) + ignored, current_dir=item[1])
        root, dirpath, fname = item
        if batch and (len(batch) >= limit or batch_bytes >= max_bytes or shutdown_requested()) and (
                dirpath != batch[-1][1] or (len(batch) >= 2 * limit and
                                            compute_group_key(dirpath, fname) != compute_group_key(dirpath, batch[-1][2]))):
            file_iter.push_back(item)
            exhausted = False
            break
        filepath = os.path.join(dirpath, fname)
        ext = os.path.splitext(fname)[1].lower()
        try:
            st = os.stat(filepath)
        except OSError as e:
            if verbose:
                print(f"  [error] Can't stat {filepath}: {e}")
            continue
        known_paths.add(filepath)
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()

        if ext not in ALL_EXTENSIONS:
            conn.execute(
                """INSERT INTO files (filename, original_path, source_root, file_size,
                   file_modified_date, status, error_message)
                   VALUES (?, ?, ?, ?, ?, 'ignored', ?)""",
                (fname, filepath, root, st.st_size, mtime, f"unhandled file type {ext or '(none)'}"),
            )
            ignored += 1
            continue

        batch.append((root, dirpath, fname, filepath, ext, st.st_size, mtime))
        batch_bytes += st.st_size

    set_progress(conn, phase='reading dates', scanned_in_batch=len(batch) + ignored,
                 dates_read=0, dates_total=0)
    cameras = {}
    exif = read_exif_dates(
        [b[3] for b in batch if b[4] in MEDIA_EXTENSIONS],
        on_chunk=lambda done, total: set_progress(conn, dates_read=done, dates_total=total),
        workers=exif_workers, cameras=cameras,
    )

    ids = []
    for root, dirpath, fname, filepath, ext, size, mtime in batch:
        taken_at, raw, source, origin = _resolve_date(filepath, root, exif.get(filepath),
                                                      prior.lookup(root, filepath))
        cur = conn.execute(
            """INSERT INTO files (filename, original_path, source_root, origin_path, file_size,
               file_modified_date, taken_at, exif_creation_date, date_source, group_key, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (fname, filepath, root, origin, size, mtime, taken_at, raw, source,
             compute_group_key(dirpath, fname)),
        )
        ids.append(cur.lastrowid)
        if ext in MEDIA_EXTENSIONS:     # read just now; a file naming no camera isn't read again
            make, model, lens = cameras.get(filepath, (None, None, None))
            conn.execute(
                f"UPDATE files SET camera_make = ?, camera_model = ?, lens_model = ?, camera = ?, "
                f"camera_read_at = {NOW_SQL} WHERE id = ?",
                (make, model, lens, camera_name(make, model) if make or model else None, cur.lastrowid))
    conn.commit()

    parts = [f"{len(ids):,} new"]
    if ignored:
        parts.append(f"{ignored:,} unhandled types left in place")
    print(f"  Scanned: {', '.join(parts)}")
    return ids, exhausted
