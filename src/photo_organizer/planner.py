"""Compute destination paths for pending files, handling collisions and ARW+XMP pairing."""

import datetime
import os

from photo_organizer.dates import (
    _parse_exif_datetime,
    extract_date_from_dirpath,
    extract_date_from_filename,
)


def compute_dest_dir(dt, dest_root):
    """Compute destination directory from a date: DEST/YYYY/MM/YYYY-MM-DD/"""
    if dt is None:
        return os.path.join(dest_root, 'UNKNOWN_DATE')
    return os.path.join(
        dest_root,
        f"{dt.year:04d}",
        f"{dt.month:02d}",
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    )


def resolve_dest_path(dest_dir, filename, conn):
    """Resolve the full destination path, handling filename collisions.
    Uses DB only (no filesystem checks) to avoid network round-trips."""
    dest_path = os.path.join(dest_dir, filename)

    existing = conn.execute(
        "SELECT id FROM files WHERE dest_path = ?", (dest_path,)
    ).fetchone()
    if not existing:
        return dest_path

    # Add numeric suffix
    stem, ext = os.path.splitext(filename)
    for i in range(1, 10000):
        candidate = os.path.join(dest_dir, f"{stem}_{i}{ext}")
        existing = conn.execute(
            "SELECT id FROM files WHERE dest_path = ?", (candidate,)
        ).fetchone()
        if not existing:
            return candidate

    raise RuntimeError(f"Too many collisions for {filename} in {dest_dir}")


def plan_destinations(conn, dest_root, source_root, verbose=False, file_ids=None):
    """Compute destination paths for pending files. If file_ids given, only plan those."""
    query = ("SELECT id, filename, original_path, date_source, exif_creation_date, "
             "file_modified_date, group_key FROM files WHERE status = 'pending' AND dest_path IS NULL ")
    if file_ids:
        placeholders = ','.join('?' * len(file_ids))
        query += f"AND id IN ({placeholders}) "
        query += "ORDER BY group_key, filename"
        rows = conn.execute(query, file_ids).fetchall()
    else:
        query += "ORDER BY group_key, filename"
        rows = conn.execute(query).fetchall()

    # Group by group_key to handle ARW+XMP pairs
    groups = {}
    for row in rows:
        key = row['group_key']
        if key not in groups:
            groups[key] = []
        groups[key].append(dict(row))

    planned = 0
    total_to_plan = sum(len(m) for m in groups.values())
    for key, members in groups.items():
        # Find the best date from the group using DB data (no file I/O)
        best_date = None

        # Prefer ARW's EXIF date
        for m in members:
            ext = os.path.splitext(m['filename'])[1].lower()
            if ext == '.arw' and m['date_source'] == 'exif' and m['exif_creation_date']:
                best_date = _parse_exif_datetime(m['exif_creation_date'])
                break

        # Fall back to any member with an exif date
        if best_date is None:
            for m in members:
                if m['exif_creation_date']:
                    best_date = _parse_exif_datetime(m['exif_creation_date'])
                    if best_date:
                        break

        # Fall back to filename/dirname parsing (no file I/O needed)
        if best_date is None:
            for m in members:
                best_date = extract_date_from_filename(m['filename'])
                if best_date:
                    break

        if best_date is None:
            for m in members:
                best_date = extract_date_from_dirpath(m['original_path'], source_root)
                if best_date:
                    break

        # Last resort: file mtime from DB
        if best_date is None:
            for m in members:
                if m['file_modified_date']:
                    try:
                        best_date = datetime.date.fromisoformat(m['file_modified_date'][:10])
                    except (ValueError, TypeError):
                        pass
                    if best_date:
                        break

        dest_dir = compute_dest_dir(best_date, dest_root)

        for m in members:
            dest_path = resolve_dest_path(dest_dir, m['filename'], conn)
            conn.execute(
                "UPDATE files SET dest_path = ? WHERE id = ?",
                (dest_path, m['id'])
            )
            planned += 1

            if verbose:
                rel_dest = os.path.relpath(dest_path, dest_root)
                print(f"  {m['filename']} -> {rel_dest} ({m['date_source']})")

        if planned % 500 == 0:
            conn.commit()
            if total_to_plan > 0:
                pct = planned * 100 // total_to_plan
                print(f"\r  Planning {planned:,}/{total_to_plan:,} ({pct}%)...", end='', flush=True)

    conn.commit()
    print(f"\r  Planned destinations for {planned:,} files          ")
    return planned
