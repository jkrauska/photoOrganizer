"""Live queue of source folders, processed one at a time by a long-running worker.

  photo-organizer-queue add PATH [PATH...]   add folders (any time, even while the worker runs)
  photo-organizer-queue list                 show the queue
  photo-organizer-queue remove ID            drop a queued item
  photo-organizer-queue worker --dest DIR    process items in order, then wait for more

Each item is a normal execute run (verified copy, then source delete; uncertain files stay).
`make stop` stops the current item gracefully and the worker exits; the item goes back to
'queued' and resumes when a worker starts again.
"""

import argparse
import datetime
import json
import os
import signal
import sys
import time

from photo_organizer.cli import run
from photo_organizer.dates import DEFAULT_EXIF_WORKERS, require_exiftool
from photo_organizer.db import DEFAULT_DB_PATH, RunLocked, init_db
from photo_organizer.constants import is_time_machine_path
from photo_organizer.scanner import is_within
from photo_organizer.state import DEFAULT_STOP_FILE, shutdown_requested, signal_handler

IDLE_POLL_SECONDS = 5


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def enqueue(conn, path, dest=None, keep_sources=False):
    """Queue one folder (copy-only if keep_sources, always for backups). Returns (queued_path_or_None, message)."""
    src = os.path.abspath(os.path.expanduser(path.strip())).rstrip('/') or '/'
    if not os.path.isdir(src):
        return None, f"not a folder: {src}"
    if dest and (is_within(src, dest) or is_within(dest, src)):
        return None, f"overlaps the store {dest}: {src}"
    if conn.execute("SELECT 1 FROM queue WHERE source = ? AND status IN ('queued', 'running')",
                    (src,)).fetchone():
        return None, f"already queued: {src}"
    keep = bool(keep_sources or is_time_machine_path(src))
    conn.execute("INSERT INTO queue (source, keep_sources) VALUES (?, ?)", (src, int(keep)))
    conn.commit()
    return src, f"queued{' (copy-only)' if keep else ''}: {src}"


def add(conn, paths, dest=None, keep_sources=False):
    added = []
    for p in paths:
        src, msg = enqueue(conn, p, dest, keep_sources)
        print(f"  {msg}")
        if src:
            added.append(src)
    return added


def list_queue(conn, limit=30):
    rows = conn.execute(
        "SELECT id, status, source, added_at, finished_at, result, keep_sources FROM queue ORDER BY "
        "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, id DESC LIMIT ?", (limit,)
    ).fetchall()
    for r in rows:
        tag = ' [copy-only]' if r['keep_sources'] else ''
        print(f"  #{r['id']:<4} {r['status']:<8} {r['source']}{tag}" + (f"  ({r['result']})" if r['result'] else ''))
    if not rows:
        print("  (empty)")


def _set_worker_state(conn, **state):
    state.update(pid=os.getpid(), heartbeat=_now())
    conn.execute("INSERT OR REPLACE INTO progress (key, value) VALUES ('worker', ?)", (json.dumps(state),))
    conn.commit()


def _other_run_active(conn):
    row = conn.execute("SELECT value FROM progress WHERE key = 'run'").fetchone()
    if not row:
        return False
    r = json.loads(row[0])
    return not r.get('finished') and r.get('pid') != os.getpid() and _pid_alive(r.get('pid'))


def _summary(totals):
    if totals.get('duplicate_kept') or totals.get('keep_sources'):
        parts = [f"{totals.get('done', 0):,} copied", f"{totals.get('duplicate_kept', 0):,} already in store"]
    else:
        parts = [f"{totals.get('done', 0):,} moved", f"{totals.get('duplicate_removed', 0):,} duplicates removed"]
    for key, label in (('review', 'left: no date'), ('source_kept', 'sources not removable'), ('error', 'errors')):
        if totals.get(key):
            parts.append(f"{totals[key]:,} {label}")
    return ', '.join(parts)


def worker(conn, dest, db_path, stop_file, exit_when_empty=False, **run_kwargs):
    """Process queued folders in order; wait for more when the queue is empty."""
    conn.execute("UPDATE queue SET status = 'queued' WHERE status = 'running'")  # crash recovery
    conn.commit()
    print(f"Queue worker {os.getpid()} → {dest}  (stop: make stop / Ctrl-C)")
    while True:
        if shutdown_requested() or os.path.exists(stop_file):
            if os.path.exists(stop_file):
                os.unlink(stop_file)
            print("Stop requested; worker exiting.")
            break
        item = conn.execute(
            "SELECT id, source, keep_sources FROM queue WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if item is None:
            if exit_when_empty:
                break
            _set_worker_state(conn, state='idle: waiting for queued folders')
            time.sleep(IDLE_POLL_SECONDS)
            continue
        if _other_run_active(conn):
            _set_worker_state(conn, state='waiting: another run is active', next=item['source'])
            time.sleep(IDLE_POLL_SECONDS)
            continue
        problem = ('folder not found' if not os.path.isdir(item['source'])
                   else 'overlaps the store' if is_within(item['source'], dest) or is_within(dest, item['source'])
                   else None)
        if problem:
            conn.execute("UPDATE queue SET status = 'failed', finished_at = datetime('now'), result = ? "
                         "WHERE id = ?", (problem, item['id']))
            conn.commit()
            continue

        conn.execute("UPDATE queue SET status = 'running', started_at = datetime('now') WHERE id = ?", (item['id'],))
        conn.commit()
        _set_worker_state(conn, state='running', current=item['source'], item=item['id'])
        print(f"\n=== Queue #{item['id']}: {item['source']} ===")
        try:
            totals = run([item['source']], dest, db_path, execute=True, stop_file=stop_file,
                         keep_sources=bool(item['keep_sources']), **run_kwargs)
        except RunLocked:
            conn.execute("UPDATE queue SET status = 'queued' WHERE id = ?", (item['id'],))
            conn.commit()
            _set_worker_state(conn, state='waiting: another run is active', next=item['source'])
            time.sleep(IDLE_POLL_SECONDS)
            continue
        except Exception as e:
            conn.execute("UPDATE queue SET status = 'failed', finished_at = datetime('now'), result = ? "
                         "WHERE id = ?", (f"{type(e).__name__}: {e}", item['id']))
            conn.commit()
            print(f"Queue #{item['id']} failed: {e}")
            continue

        if totals.get('stopped'):
            conn.execute("UPDATE queue SET status = 'queued', result = 'stopped; resumes next time' WHERE id = ?",
                         (item['id'],))
            conn.commit()
            print("Stopped; worker exiting. The item stays queued.")
            break
        conn.execute("UPDATE queue SET status = 'done', finished_at = datetime('now'), result = ? WHERE id = ?",
                     (_summary(totals), item['id']))
        conn.commit()
    _set_worker_state(conn, state='stopped', stopped=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_add = sub.add_parser('add')
    p_add.add_argument('paths', nargs='+')
    p_add.add_argument('--keep-sources', action='store_true',
                       help='Copy-only: never delete the originals (always on for Time Machine backups)')
    sub.add_parser('list')
    p_rm = sub.add_parser('remove')
    p_rm.add_argument('id', type=int)
    p_w = sub.add_parser('worker')
    p_w.add_argument('--dest', required=True)
    p_w.add_argument('--batch-size', type=int, default=5000)
    p_w.add_argument('--workers', type=int, default=4)
    p_w.add_argument('--exif-workers', type=int, default=DEFAULT_EXIF_WORKERS)
    p_w.add_argument('--stop-file', default=DEFAULT_STOP_FILE)
    p_w.add_argument('--exit-when-empty', action='store_true')
    args = parser.parse_args()

    conn = init_db(args.db)
    if args.cmd == 'add':
        add(conn, args.paths, keep_sources=args.keep_sources)
        list_queue(conn)
    elif args.cmd == 'list':
        list_queue(conn)
    elif args.cmd == 'remove':
        n = conn.execute("DELETE FROM queue WHERE id = ? AND status = 'queued'", (args.id,)).rowcount
        conn.commit()
        print("removed" if n else "not found (only queued items can be removed)")
    elif args.cmd == 'worker':
        require_exiftool()
        if not os.path.isdir(args.dest):
            sys.exit(f"Error: destination not found: {args.dest}")
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        worker(conn, os.path.abspath(args.dest), args.db, args.stop_file, args.exit_when_empty,
               batch_size=args.batch_size, workers=args.workers, exif_workers=args.exif_workers)
    conn.close()


if __name__ == '__main__':
    main()
