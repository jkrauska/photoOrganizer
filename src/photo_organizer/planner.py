"""Compute destination paths: DEST/YYYY/MM/YYYY-MM-DD/<original filename>."""

import os
from collections import defaultdict

from photo_organizer.constants import RAW_EXTENSIONS, TRUSTED_DATE_SOURCES
from photo_organizer.db import NOW_SQL

SOURCE_RANK = {s: i for i, s in enumerate(TRUSTED_DATE_SOURCES)}


def compute_dest_dir(day, dest_root):
    """day is 'YYYY-MM-DD'."""
    return os.path.join(dest_root, day[:4], day[5:7], day[:10])


def resolve_dest_path(dest_dir, filename, conn, own_id):
    """Pick a destination path no other catalog row claims, adding _1, _2... on collision."""
    stem, ext = os.path.splitext(filename)
    for i in range(10000):
        candidate = os.path.join(dest_dir, filename if i == 0 else f"{stem}_{i}{ext}")
        taken = conn.execute(
            "SELECT 1 FROM files WHERE dest_path = ? AND id != ?", (candidate, own_id)
        ).fetchone()
        if not taken:
            return candidate
    raise RuntimeError(f"Too many collisions for {filename} in {dest_dir}")


def _best_day(members):
    """Most trustworthy capture day in a group, preferring RAW EXIF; None if nothing is trusted."""
    trusted = [m for m in members if m['date_source'] in SOURCE_RANK and m['taken_at']]
    if not trusted:
        return None
    trusted.sort(key=lambda m: (
        SOURCE_RANK[m['date_source']],
        os.path.splitext(m['filename'])[1].lower() not in RAW_EXTENSIONS,
    ))
    return trusted[0]['taken_at'][:10]


def plan_destinations(conn, dest_root, file_ids, verbose=False):
    """Plan destinations for pending files in file_ids; groups with no trusted date go to 'review'.
    Returns (planned, review)."""
    placeholders = ','.join('?' * len(file_ids))
    rows = conn.execute(
        f"SELECT id, filename, date_source, taken_at, group_key FROM files "
        f"WHERE status = 'pending' AND dest_path IS NULL AND id IN ({placeholders}) "
        f"ORDER BY group_key, filename",
        file_ids,
    ).fetchall()

    groups = defaultdict(list)
    for row in rows:
        groups[row['group_key']].append(row)

    planned = review = 0
    for members in groups.values():
        day = _best_day(members)
        if day is None:
            conn.executemany(
                f"UPDATE files SET status = 'review', error_message = ?, updated_at = {NOW_SQL} WHERE id = ?",
                [("no reliable capture date (only file mtime)", m['id']) for m in members],
            )
            review += len(members)
            continue

        dest_dir = compute_dest_dir(day, dest_root)
        for m in members:
            dest_path = resolve_dest_path(dest_dir, m['filename'], conn, m['id'])
            conn.execute("UPDATE files SET dest_path = ? WHERE id = ?", (dest_path, m['id']))
            planned += 1
            if verbose:
                print(f"  {m['filename']} -> {os.path.relpath(dest_path, dest_root)} ({m['date_source']})")

    conn.commit()
    print(f"  Planned {planned:,} files; {review:,} left in place for review (no reliable date)")
    return planned, review
