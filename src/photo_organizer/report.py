import humanize


def print_report(conn, dry_run=False):
    """Print a summary report."""
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    status_counts = conn.execute(
        "SELECT status, COUNT(*) as cnt, SUM(file_size) as total_size "
        "FROM files GROUP BY status ORDER BY status"
    ).fetchall()

    for row in status_counts:
        size_str = humanize.naturalsize(row['total_size'] or 0)
        print(f"  {row['status']:<12} {row['cnt']:>8,} files  ({size_str})")

    # Date source distribution
    print("\nDate sources:")
    sources = conn.execute(
        "SELECT date_source, COUNT(*) as cnt FROM files GROUP BY date_source ORDER BY cnt DESC"
    ).fetchall()
    for row in sources:
        print(f"  {row['date_source']:<12} {row['cnt']:>8,} files")

    # Errors
    error_rows = conn.execute(
        "SELECT filename, original_path, error_message FROM files WHERE status = 'error'"
    ).fetchall()
    if error_rows:
        print(f"\nErrors ({len(error_rows)}):")
        for row in error_rows:
            print(f"  {row['filename']}: {row['error_message']}")

    # Unknown dates
    unknown = conn.execute(
        "SELECT COUNT(*) as cnt FROM files WHERE dest_path LIKE '%UNKNOWN_DATE%'"
    ).fetchone()
    if unknown and unknown['cnt'] > 0:
        print(f"\nFiles with unknown date: {unknown['cnt']}")

    if dry_run:
        print(f"\n[DRY RUN] No files were moved. Run with --execute to proceed.")

    # Destination directory count
    dirs = conn.execute(
        "SELECT COUNT(DISTINCT substr(dest_path, 1, instr(dest_path, '/' || filename) - 1)) "
        "as cnt FROM files WHERE dest_path IS NOT NULL"
    ).fetchone()
    if dirs:
        print(f"\nDestination directories: {dirs['cnt']:,}")
