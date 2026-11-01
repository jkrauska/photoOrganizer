import argparse
import datetime
import os
import signal
import sys
import time

import humanize

from photo_organizer.cleanup import prune_empty_dirs
from photo_organizer.constants import is_time_machine_path
from photo_organizer.dates import DEFAULT_EXIF_WORKERS, require_exiftool
from photo_organizer.db import DEFAULT_DB_PATH, RunLocked, acquire_run_lock, backup_db, init_db
from photo_organizer.planner import plan_destinations
from photo_organizer.progress import finish_run, set_progress, start_run
from photo_organizer.report import print_report
from photo_organizer.scanner import PriorCatalogs, is_within, iter_source_files, scan_batch
from photo_organizer.state import DEFAULT_STOP_FILE, configure_stop_file, shutdown_requested, signal_handler
from photo_organizer.nas import detect as detect_nas
from photo_organizer.transfer import execute_transfers, recover_interrupted, retry_source_removal


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def run(sources, dest, db_path, execute=False, batch_size=5000, workers=4, limit=0,
        verbose=False, confirm=lambda prompt: True, exif_workers=DEFAULT_EXIF_WORKERS,
        stop_file=DEFAULT_STOP_FILE, keep_sources=False, nas=True):
    """Scan sources, file trusted-date media into dest, delete verified sources. Returns totals,
    with 'stopped' set when a stop was requested. Raises RunLocked if another run holds the catalog."""
    # Backups are imported copy-only, whatever was asked.
    keep_sources = keep_sources or any(is_time_machine_path(s) for s in sources)
    lock_fd = acquire_run_lock(db_path)
    configure_stop_file(stop_file)
    conn = init_db(db_path)
    start_run(conn, sources, dest, execute)
    final_phase = 'stopped'
    fs = None
    try:
        recovered = recover_interrupted(conn)
        if recovered:
            print(f"Recovered {recovered:,} files interrupted mid-transfer")
        if execute and not keep_sources:
            removed, kept = retry_source_removal(conn, sources)
            if kept:
                print(f"Removed {removed:,} of {kept:,} sources left behind by earlier failed deletes")

        transfer = None
        if execute:
            note = None
            if nas:
                fs, note = detect_nas(sources, dest)
            if fs:
                transfer = 'NAS ' + fs.target.split('@')[-1]
                print("Transfers run on the NAS: " + ', '.join(f"{a} = {b}" for a, b in fs.mapping))
            else:
                transfer = 'Mac' + (f" ({note})" if note else '')
                if note:
                    print(f"  {note}")
            set_progress(conn, transfer=transfer)

        known = {r[0] for r in conn.execute("SELECT original_path FROM files")}
        file_iter = iter_source_files(sources, dest, known)
        prior = PriorCatalogs()
        totals = {'done': 0, 'duplicate_removed': 0, 'duplicate_kept': 0, 'source_kept': 0, 'error': 0, 'review': 0}
        processed = 0
        batch_num = 0

        while not shutdown_requested():
            batch_num += 1
            set_progress(conn, batch=batch_num)
            want = min(batch_size, limit - processed) if limit else batch_size
            if limit and want <= 0:
                break

            # Only this run's sources: an earlier dry run over other folders must not move here.
            pending = conn.execute(
                f"SELECT id FROM files WHERE status = 'pending' "
                f"AND source_root IN ({','.join('?' * len(sources))}) ORDER BY id LIMIT ?",
                (*sources, want),
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
                action = "Copy (sources kept)" if keep_sources else "Move"
                tail = "" if keep_sources else " and delete sources"
                if not confirm(f"\n[{ts()}] {action} {len(batch_ids) - review:,} files "
                               f"({humanize.naturalsize(size)}){tail}? [y/N] "):
                    print("Aborted.")
                    break
                counts = execute_transfers(conn, batch_ids, workers, verbose, keep_sources, fs)
                for k, v in counts.items():
                    totals[k] += v
                if keep_sources:
                    print(f"  Batch {batch_num}: {counts['done']:,} copied | "
                          f"{counts['duplicate_kept']:,} already in store | {counts['error']:,} errors")
                else:
                    print(f"  Batch {batch_num}: {counts['done']:,} moved | "
                          f"{counts['duplicate_removed']:,} duplicates removed | "
                          f"{counts['source_kept']:,} sources not removable | {counts['error']:,} errors")
                backup_db(conn, dest)

            if exhausted:
                break

        stopped = shutdown_requested()
        if execute and not stopped and not keep_sources:
            set_progress(conn, phase='pruning empty folders')
            pruned = sum(prune_empty_dirs(s, verbose, fs) for s in sources)
            print(f"\nRemoved {pruned:,} emptied source folders")
        if stopped:
            print("\nStopped on request; rerun the same command to resume.")
        else:
            final_phase = 'finished'
        if execute:
            print(f"Catalog backed up to {backup_db(conn, dest)}")
        print_report(conn, dry_run=not execute)
        totals['stopped'] = stopped
        totals['keep_sources'] = keep_sources
        totals['transfer'] = transfer
        return totals
    finally:
        if fs:
            fs.close()
        try:
            finish_run(conn, final_phase)
        finally:
            conn.close()
            os.close(lock_fd)


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
    parser.add_argument('--keep-sources', action='store_true',
                        help='Copy-only: verify copies but never delete or prune sources '
                             '(always on for Time Machine backups)')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--limit', type=int, default=0, help='Process at most N files (0 = no limit)')
    parser.add_argument('--batch-size', type=int, default=5000)
    parser.add_argument('--workers', type=int, default=4, help='Parallel copy/verify threads')
    parser.add_argument('--stop-file', default=DEFAULT_STOP_FILE,
                        help=f'Create this file to stop gracefully (default: {DEFAULT_STOP_FILE})')
    parser.add_argument('--exif-workers', type=int, default=DEFAULT_EXIF_WORKERS,
                        help=f'Parallel exiftool processes for date reads (default: {DEFAULT_EXIF_WORKERS})')
    parser.add_argument('--no-nas', action='store_true',
                        help='Always transfer through the Mac, even between shares of one NAS')
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

    keep = args.keep_sources or any(is_time_machine_path(s) for s in sources)
    mode = ('EXECUTE: copy-only, sources kept' if keep else 'EXECUTE: sources are DELETED after verified copy')
    print(f"Photo Organizer [{mode if args.execute else 'DRY RUN'}]")
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
    signal.signal(signal.SIGTERM, signal_handler)
    start = time.time()
    try:
        run(sources, dest, args.db, args.execute, args.batch_size, args.workers, args.limit,
            args.verbose, confirm, args.exif_workers, args.stop_file, args.keep_sources,
            nas=not args.no_nas)
    except RunLocked as e:
        sys.exit(f"Error: {e}. Wait for it to finish, or add this folder to the queue (make queue SRC=...).")
    print(f"\nTotal time: {humanize.naturaldelta(time.time() - start)}")
