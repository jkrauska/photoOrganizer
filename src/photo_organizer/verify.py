"""Re-read every file in the store from the NAS (client cache bypassed) and check it against the catalog."""

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import humanize

from photo_organizer.db import DEFAULT_DB_PATH
from photo_organizer.hashing import sha256_file


def _check(dest_path, sha):
    try:
        return dest_path, 'ok' if sha256_file(dest_path, nocache=True) == sha else 'MISMATCH'
    except FileNotFoundError:
        return dest_path, 'MISSING'
    except OSError as e:
        return dest_path, f'UNREADABLE ({e})'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=DEFAULT_DB_PATH)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--source', help='Only files that came from this source root')
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    query = "SELECT dest_path, sha256, file_size FROM files WHERE status = 'done'"
    params = ()
    if args.source:
        query += " AND source_root = ?"
        params = (args.source.rstrip('/'),)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    total_bytes = sum(r[2] or 0 for r in rows)
    print(f"Verifying {len(rows):,} stored files ({humanize.naturalsize(total_bytes)}) from the NAS...")

    problems = []
    done = done_bytes = 0
    sizes = {r[0]: r[2] or 0 for r in rows}
    start = time.time()
    with ThreadPoolExecutor(args.workers) as pool:
        for fut in as_completed(pool.submit(_check, r[0], r[1]) for r in rows):
            path, result = fut.result()
            done += 1
            done_bytes += sizes[path]
            if result != 'ok':
                problems.append((path, result))
                print(f"\n  {result}: {path}")
            elapsed = max(time.time() - start, 1e-6)
            print(f"\r  {done:,}/{len(rows):,} | {done_bytes / elapsed / 1e6:.0f} MB/s | {len(problems)} problems",
                  end='', flush=True)
    print(f"\n{len(rows) - len(problems):,} verified OK, {len(problems):,} problems")
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
