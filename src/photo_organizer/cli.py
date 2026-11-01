import argparse
import datetime
import os
import signal
import sys
import time

import humanize

from photo_organizer.constants import DEFAULT_DEST, DEFAULT_SOURCE
from photo_organizer.dates import HAS_PILLOW
from photo_organizer.db import init_db
from photo_organizer.planner import plan_destinations
from photo_organizer.report import print_report
from photo_organizer.scanner import scan_source
from photo_organizer.state import shutdown_requested, signal_handler
from photo_organizer.transfer import execute_transfers


def main():
    parser = argparse.ArgumentParser(
        description="Photo Organizer - Sort and move photos into YEAR/MONTH/YYYY-MM-DD structure."
    )
    parser.add_argument('--execute', action='store_true',
                        help='Actually move files (default is dry-run)')
    parser.add_argument('--source', default=DEFAULT_SOURCE,
                        help=f'Source directory (default: {DEFAULT_SOURCE})')
    parser.add_argument('--dest', default=DEFAULT_DEST,
                        help=f'Destination directory (default: {DEFAULT_DEST})')
    parser.add_argument('--db', default=None,
                        help='SQLite database path (default: <dest>/photo_organizer.db)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show detailed progress')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from previous run')
    parser.add_argument('--limit', type=int, default=0,
                        help='Limit total number of files to process (0 = no limit)')
    parser.add_argument('--batch-size', type=int, default=5000,
                        help='Files per batch (scan, plan, transfer in chunks; default: 5000)')
    parser.add_argument('--workers', type=int, default=4,
                        help='Number of parallel transfer threads (default: 4)')

    args = parser.parse_args()

    # Validate paths
    if not os.path.isdir(args.source):
        print(f"Error: Source directory not found: {args.source}")
        sys.exit(1)
    if not os.path.isdir(args.dest):
        print(f"Error: Destination directory not found: {args.dest}")
        sys.exit(1)

    db_path = args.db or os.path.join(args.dest, 'photo_organizer.db')

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"Photo Organizer [{mode}]")
    print(f"  Source: {args.source}")
    print(f"  Dest:   {args.dest}")
    print(f"  DB:     {db_path}")
    if args.limit:
        print(f"  Limit:  {args.limit} files")
    print(f"  Batch:  {args.batch_size} files per batch")
    print(f"  Workers: {args.workers} threads")
    if not HAS_PILLOW:
        print("  Warning: Pillow not installed. JPG EXIF extraction disabled.")
    print()

    answer = input("Confirm settings are correct to continue? [y/N] ")
    if answer.strip().lower() not in ('y', 'yes'):
        print("Aborted.")
        sys.exit(0)
    print()

    conn = init_db(db_path)

    # Install graceful shutdown handler now that we're past prompts
    signal.signal(signal.SIGINT, signal_handler)

    def ts():
        return datetime.datetime.now().strftime("%H:%M:%S")

    def timed(label):
        """Context manager that prints step with elapsed time."""
        class Timer:
            def __enter__(self):
                self.start = time.time()
                print(f"[{ts()}] {label}...")
                return self
            def __exit__(self, *args):
                elapsed = time.time() - self.start
                print(f"[{ts()}] {label} done ({elapsed:.1f}s)")
        return Timer()

    batch_num = 0
    total_done = 0
    total_errors = 0
    total_duplicates = 0
    files_remaining = args.limit if args.limit else float('inf')
    run_start = time.time()

    while not shutdown_requested():
        batch_num += 1
        batch_limit = min(args.batch_size, int(files_remaining)) if args.limit else args.batch_size

        # Phase 1: Check for existing pending files first, scan only if none
        pending_rows = conn.execute(
            "SELECT id FROM files WHERE status = 'pending' LIMIT ?", (batch_limit,)
        ).fetchall()

        if pending_rows:
            batch_ids = [r['id'] for r in pending_rows]
            print(f"\n--- Batch {batch_num} ({len(batch_ids):,} pending in DB) ---")
        else:
            # No pending files — scan for new ones
            print(f"\n--- Batch {batch_num} (scanning up to {batch_limit:,} new files) ---")
            with timed("Scanning"):
                scanned, batch_ids = scan_source(args.source, conn, verbose=args.verbose, limit=batch_limit)
            if scanned == 0:
                print("No more files to scan.")
                break

        # Phase 2: Plan destinations for this batch only
        with timed("Planning"):
            plan_destinations(conn, args.dest, args.source, verbose=args.verbose, file_ids=batch_ids)

        # Phase 3: Execute this batch only
        if args.execute:
            total_size = conn.execute(
                f"SELECT SUM(file_size) as s FROM files WHERE status = 'pending' AND id IN ({','.join('?' * len(batch_ids))})",
                batch_ids
            ).fetchone()['s'] or 0
            answer = input(f"\n[{ts()}] Ready to move {len(batch_ids):,} files ({humanize.naturalsize(total_size)}). Proceed? [y/N] ")
            if answer.strip().lower() not in ('y', 'yes'):
                print("Aborted.")
                break
            with timed("Transferring"):
                done, errors, duplicates = execute_transfers(conn, verbose=args.verbose, file_ids=batch_ids, workers=args.workers)
            total_done += done
            total_errors += errors
            total_duplicates += duplicates
            print(f"  Batch {batch_num}: {done:,} done | {errors:,} errors | {duplicates:,} duplicates")
        else:
            print(f"[{ts()}] Skipped transfer (dry-run mode)")

        if args.limit:
            files_remaining -= len(batch_ids)
            if files_remaining <= 0:
                break

    # Final report
    total_elapsed = time.time() - run_start
    if args.execute and batch_num > 1:
        print(f"\n  TOTAL: {total_done:,} done | {total_errors:,} errors | {total_duplicates:,} duplicates")
    print_report(conn, dry_run=not args.execute)
    print(f"\nTotal time: {humanize.naturaldelta(total_elapsed)}")

    conn.close()
