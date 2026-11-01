import os
from collections import Counter

import humanize

STATUS_LABELS = {
    'done': 'moved into store',
    'duplicate_removed': 'duplicate, source removed',
    'pending': 'pending',
    'review': 'LEFT IN PLACE: no reliable date',
    'ignored': 'LEFT IN PLACE: unhandled type',
    'error': 'LEFT IN PLACE: error',
}


def print_report(conn, dry_run=False):
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    for row in conn.execute(
        "SELECT status, COUNT(*) AS cnt, SUM(file_size) AS size FROM files GROUP BY status ORDER BY cnt DESC"
    ):
        label = STATUS_LABELS.get(row['status'], row['status'])
        print(f"  {label:<36} {row['cnt']:>9,}  ({humanize.naturalsize(row['size'] or 0)})")

    print("\nDate sources (filed files):")
    for row in conn.execute(
        "SELECT date_source, COUNT(*) AS cnt FROM files WHERE status NOT IN ('ignored') "
        "GROUP BY date_source ORDER BY cnt DESC"
    ):
        print(f"  {row['date_source'] or '-':<12} {row['cnt']:>9,}")

    exts = Counter(
        os.path.splitext(r['filename'])[1].lower() or '(none)'
        for r in conn.execute("SELECT filename FROM files WHERE status = 'ignored'")
    )
    if exts:
        print("\nUnhandled types left in place:")
        for ext, n in exts.most_common(15):
            print(f"  {ext:<12} {n:>9,}")

    errors = conn.execute(
        "SELECT original_path, error_message FROM files WHERE status = 'error' LIMIT 25"
    ).fetchall()
    if errors:
        print(f"\nErrors (first {len(errors)}):")
        for row in errors:
            print(f"  {row['original_path']}: {row['error_message']}")

    print("\nList what was left in place:")
    print("  sqlite3 <catalog> \"SELECT original_path, error_message FROM files "
          "WHERE status IN ('review','ignored','error')\"")
    if dry_run:
        print("\n[DRY RUN] No files were moved or deleted. Run with --execute to proceed.")
