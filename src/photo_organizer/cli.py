import argparse
import datetime
import os
import signal
import sys
import time

import humanize

from photo_organizer.cleanup import prune_empty_dirs
from photo_organizer.dates import DEFAULT_EXIF_WORKERS, require_exiftool
from photo_organizer.db import DEFAULT_DB_PATH, backup_db, init_db
from photo_organizer.planner import plan_destinations
from photo_organizer.progress import finish_run, set_progress, start_run
from photo_organizer.report import print_report
from photo_organizer.scanner import PriorCatalogs, is_within, iter_source_files, scan_batch
from photo_organizer.state import shutdown_requested, signal_handler
from photo_organizer.transfer import execute_transfers, recover_interrupted


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def run(sources, dest, db_path, execute=False, batch_size=5000, workers=4, limit=0,
        verbose=False, confirm=lambda prompt: True, exif_workers=DEFAULT_EXIF_WORKERS):
    """Scan sources, file trusted-date media into dest, delete verified sources. Returns totals."""
    conn = init_db(db_path)
    start_run(conn, sources, dest, execute)
    final_phase = 'stopped'
    try:
        recovered = recover_interrupted(conn)
        if recovered:
            print(f"Recovered {recovered:,} files interrupted mid-transfer")

        known = {r[0] for r in conn.execute("SELECT original_path FROM files")}
        file_iter = iter_source_files(sources, dest, known)
        prior = PriorCatalogs()
        totals = {'done': 0, 'duplicate_removed': 0, 'error': 0, 'review': 0}
        processed = 0
        batch_num = 0

        while not shutdown_requested():
            batch_num += 1
            set_progress(conn, batch=batch_num)
            want = min(batch_size, limit - processed) if limit else batch_size
            if limit and want <= 0:
                break

            pending = conn.execute(
                "SELECT id FROM files WHERE status = 'pending' LIMIT ?", (want,)
            ).fetchall() if execute else []
            if pending:
                batch_ids = [r['id'] for r in pending]
                print(f"\n--- Batch {batch_num} ({len(batch_ids):,} pending from earlier) ---")
                exhausted = False
            else:
                print(f"\n[{ts()}] --- Batch {batch_num}: scanning ---")
                batch_ids, exhausted = scan_batch(file_iter, conn, known, prior, want, verbose, exif_workers)
                if not batch_ids:
                    break

            _, review = plan_destinations(conn, dest, batch_ids, verbose)
            totals['review'] += review
            processed += len(batch_ids)

            if execute:
                size = conn.execute(
                    f"SELECT SUM(file_size) FROM files WHERE status = 'pending' AND dest_path IS NOT NULL "
                    f"AND id IN ({','.join('?' * len(batch_ids))})", batch_ids,
                ).fetchone()[0] or 0
                set_progress(conn, phase='waiting for confirmation')
                if not confirm(f"\n[{ts()}] Move {len(batch_ids) - review:,} files "
                               f"({humanize.naturalsize(size)}) and delete sources? [y/N] "):
                    print("Aborted.")
                    break
                counts = execute_transfers(conn, batch_ids, workers, verbose)
                for k, v in counts.items():
                    totals[k] += v
                print(f"  Batch {batch_num}: {counts['done']:,} moved | "
                      f"{counts['duplicate_removed']:,} duplicates removed | {counts['error']:,} errors")
                backup_db(conn, dest)

            if exhausted:
                break

        if execute:
            set_progress(conn, phase='pruning empty folders')
            pruned = sum(prune_empty_dirs(s, verbose) for s in sources)
            print(f"\nRemoved {pruned:,} emptied source folders")
        if not shutdown_requested():
            final_phase = 'finished'
        if execute:
            print(f"Catalog backed up to {backup_db(conn, dest)}")
        print_report(conn, dry_run=not execute)
        return totals
    finally:
        try:
            finish_run(conn, final_phase)
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="File photos from ad-hoc folders into DEST/YYYY/MM/YYYY-MM-DD/, deleting each "
                    "source only after its copy in DEST is verified. Files without a reliable date, "
                    "unhandled types, and errors are left where they are."
    )
    parser.add_argument('--source', action='append', required=True,
                        help='Source directory to empty into the store (repeatable)')
    parser.add_argument('--dest', required=True, help='Canonical store root (the fresh mount)')
    parser.add_argument('--db', default=DEFAULT_DB_PATH,
                        help=f'Catalog path on local disk (default: {DEFAULT_DB_PATH})')
    parser.add_argument('--execute', action='store_true',
                        help='Actually move files and delete sources (default is dry-run)')
    parser.add_argument('--yes', action='store_true', help='Skip confirmation prompts')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--limit', type=int, default=0, help='Process at most N files (0 = no limit)')
    parser.add_argument('--batch-size', type=int, default=5000)
    parser.add_argument('--workers', type=int, default=4, help='Parallel copy/verify threads')
    parser.add_argument('--exif-workers', type=int, default=DEFAULT_EXIF_WORKERS,
                        help=f'Parallel exiftool processes for date reads (default: {DEFAULT_EXIF_WORKERS})')
    args = parser.parse_args()

    require_exiftool()
    for d in args.source + [args.dest]:
        if not os.path.isdir(d):
            sys.exit(f"Error: directory not found: {d}")
    sources = [os.path.abspath(s) for s in args.source]
    dest = os.path.abspath(args.dest)
    for s in sources:
        if is_within(s, dest):
            sys.exit(f"Error: source {s} is inside the destination")
    if any(is_within(os.path.abspath(args.db), s) for s in sources):
        sys.exit("Error: the catalog must not live inside a source directory")

    print(f"Photo Organizer [{'EXECUTE: sources are DELETED after verified copy' if args.execute else 'DRY RUN'}]")
    for s in sources:
        print(f"  Source: {s}")
    print(f"  Dest:   {dest}")
    print(f"  DB:     {args.db}")
    print(f"  Batch:  {args.batch_size} files, {args.workers} workers"
          + (f", limit {args.limit}" if args.limit else ""))
    print()

    def confirm(prompt):
        return args.yes or input(prompt).strip().lower() in ('y', 'yes')

    if not confirm("Confirm settings are correct to continue? [y/N] "):
        sys.exit("Aborted.")

    signal.signal(signal.SIGINT, signal_handler)
    start = time.time()
    run(sources, dest, args.db, args.execute, args.batch_size, args.workers, args.limit,
        args.verbose, confirm, args.exif_workers)
    print(f"\nTotal time: {humanize.naturaldelta(time.time() - start)}")
