"""Copy the Apple Photos library into the store one year at a time, leaving the library untouched.

A source named 'photos:2016' catalogs that year's assets as ordinary pending rows, so the usual
plan / verified copy / dedup steps file them. It is always copy-only: nothing in the library is
written or deleted. Each asset contributes:
  - the original, byte for byte, under its real name (IMG_1234.HEIC), dated by Photos
    (which includes any date fixes made in Photos)
  - the RAW of a RAW+JPEG pair, the Live Photo video (IMG_1234.MOV), and the current edit
    (IMG_1234_edited.HEIC) when there is one
  - an XMP sidecar (IMG_1234.HEIC.xmp) with Photos' title, caption, keywords, people and GPS,
    so the original's bytes and hash stay unchanged
All of an asset's files share one group key, so they land in the same day folder. Assets whose
original is only in iCloud are left for review; they are retried on the next run of that year.
"""

import datetime
import os
import re

from photo_organizer.db import NOW_SQL

PREFIX = 'photos:'
SIDECAR_DIR = os.path.expanduser('~/.photo-organizer/photos-sidecars')
ICLOUD_ONLY = 'original only in iCloud; download it in Photos, then queue this year again'


def is_photos_source(source):
    return source.startswith(PREFIX)


def parse_source(source):
    """'photos:2016' -> 2016; ValueError for anything else."""
    m = re.fullmatch(re.escape(PREFIX) + r'(\d{4})', source.strip())
    if not m:
        raise ValueError(f"not an Apple Photos year: {source!r} (expected e.g. photos:2016)")
    return int(m.group(1))


def open_library(library=None):
    import osxphotos
    return osxphotos.PhotosDB(dbfile=library) if library else osxphotos.PhotosDB()


def library_years(db):
    """{year: asset count} for dated assets not in the trash, oldest first."""
    years = {}
    for p in db.photos():
        if p.date:
            years[p.date.year] = years.get(p.date.year, 0) + 1
    return dict(sorted(years.items()))


def render_xmp(p):
    from osxphotos.sidecars import xmp_sidecar
    return xmp_sidecar(p)


def _write_sidecar(p, sidecar_dir):
    path = os.path.join(sidecar_dir, p.uuid[:1], p.uuid + '.xmp')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = render_xmp(p)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(body)
    os.replace(tmp, path)
    return path


def asset_files(p, sidecar_dir):
    """[(kind, path, filename)] to file for one asset, original first. Empty if the original isn't local."""
    if not p.path or not os.path.exists(p.path):
        return []
    name = p.original_filename or os.path.basename(p.path)
    stem = os.path.splitext(name)[0]
    files = [('original', p.path, name)]
    if p.has_raw and p.path_raw and os.path.exists(p.path_raw):
        files.append(('raw', p.path_raw, stem + os.path.splitext(p.path_raw)[1].upper()))
    if p.live_photo and p.path_live_photo and os.path.exists(p.path_live_photo):
        files.append(('live', p.path_live_photo, stem + os.path.splitext(p.path_live_photo)[1].upper()))
    if p.hasadjustments and p.path_edited and os.path.exists(p.path_edited):
        files.append(('edited', p.path_edited, f"{stem}_edited{os.path.splitext(p.path_edited)[1].upper()}"))
    files.append(('xmp', _write_sidecar(p, sidecar_dir), name + '.xmp'))
    return files


def catalog_year(conn, source, db=None, sidecar_dir=None):
    """Add rows for the year's assets not cataloged yet. Returns (new pending ids, review count)."""
    year = parse_source(source)
    db = db or open_library()
    sidecar_dir = sidecar_dir or SIDECAR_DIR
    # iCloud-only assets from an earlier run get another chance (they may be downloaded now).
    conn.execute("DELETE FROM files WHERE source_root = ? AND status = 'review'", (source,))
    # Across years: an asset whose date was changed in Photos may have been filed under another year.
    known = {r[0] for r in conn.execute("SELECT original_path FROM files WHERE photos_uuid IS NOT NULL")}
    start = datetime.datetime(year, 1, 1)
    assets = [p for p in db.photos(from_date=start - datetime.timedelta(days=2),
                                   to_date=datetime.datetime(year + 1, 1, 1) + datetime.timedelta(days=2))
              if p.date and p.date.year == year]
    ids, review = [], 0
    for p in sorted(assets, key=lambda p: p.date):
        taken = p.date.replace(tzinfo=None).isoformat()     # local time where it was taken
        group = f"photos:{p.uuid}"
        if p.path in known:
            continue
        files = asset_files(p, sidecar_dir)
        if not files:
            if group not in known:
                conn.execute(
                    f"""INSERT INTO files (filename, original_path, source_root, taken_at, date_source,
                       group_key, photos_uuid, status, error_message, updated_at)
                       VALUES (?, ?, ?, ?, 'photos', ?, ?, 'review', ?, {NOW_SQL})""",
                    (p.original_filename or p.uuid, group, source, taken, group, p.uuid, ICLOUD_ONLY))
                review += 1
            continue
        for kind, path, filename in files:
            if path in known:
                continue
            st = os.stat(path)
            cur = conn.execute(
                """INSERT INTO files (filename, original_path, source_root, file_size, file_modified_date,
                   taken_at, date_source, group_key, photos_uuid, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'photos', ?, ?, 'pending')""",
                (filename, path, source, st.st_size, datetime.datetime.fromtimestamp(st.st_mtime).isoformat(),
                 taken, group, p.uuid))
            ids.append(cur.lastrowid)
    conn.commit()
    return ids, review


SIDECAR_MOVED = 'sidecar moved next to the stored copy of its photo'


def place_sidecars(conn, file_ids):
    """An original already in the store isn't copied again, so its just-copied XMP would sit beside
    nothing: move it next to the stored copy (as <stored name>.xmp). If that copy has a sidecar
    already, drop ours from the store (the library is untouched either way). Returns moved count."""
    placeholders = ','.join('?' * len(file_ids))
    rows = conn.execute(
        f"""SELECT x.id, x.dest_path, o.error_message FROM files x
            JOIN files o ON o.group_key = x.group_key AND o.filename || '.xmp' = x.filename
            WHERE x.id IN ({placeholders}) AND x.photos_uuid IS NOT NULL AND x.status = 'done'
              AND o.status IN ('duplicate_kept', 'duplicate_removed') AND o.error_message LIKE 'duplicate of %'""",
        file_ids).fetchall()
    moved = 0
    for r in rows:
        canonical = r['error_message'][len('duplicate of '):].split('; ')[0]
        target = canonical + '.xmp'
        if os.path.exists(target):
            os.unlink(r['dest_path'])
            conn.execute(f"UPDATE files SET status = 'duplicate_kept', dest_path = NULL, error_message = ?, "
                         f"updated_at = {NOW_SQL} WHERE id = ?",
                         (f"stored copy {canonical} already has a sidecar", r['id']))
        else:
            os.rename(r['dest_path'], target)
            conn.execute(f"UPDATE files SET dest_path = ?, error_message = ?, updated_at = {NOW_SQL} WHERE id = ?",
                         (target, SIDECAR_MOVED, r['id']))
            moved += 1
        try:
            os.rmdir(os.path.dirname(r['dest_path']))     # the day folder held only this sidecar
        except OSError:
            pass
    conn.commit()
    return moved
