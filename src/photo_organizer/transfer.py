"""Parallel copy-verify-delete file transfer with SHA256 verification."""

import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import humanize

from photo_organizer.hashing import sha256_file
from photo_organizer.state import shutdown_requested


def _transfer_one_file(src, dst, fid, filename, file_size, db_lock, conn):
    """Transfer a single file: hash, copy, verify, delete. Thread-safe."""
    try:
        src_hash = sha256_file(src)

        # Check for duplicates
        with db_lock:
            dup = conn.execute(
                "SELECT id, dest_path FROM files WHERE sha256 = ? AND status = 'done'",
                (src_hash,)
            ).fetchone()
            if dup:
                conn.execute(
                    "UPDATE files SET sha256 = ?, status = 'duplicate', "
                    "error_message = ? WHERE id = ?",
                    (src_hash, f"Duplicate of file at {dup['dest_path']}", fid)
                )
                conn.commit()
                return 'duplicate', file_size, None

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

        with db_lock:
            conn.execute("UPDATE files SET status = 'copied' WHERE id = ?", (fid,))

        dst_hash = sha256_file(dst)
        if src_hash != dst_hash:
            os.unlink(dst)
            with db_lock:
                conn.execute(
                    "UPDATE files SET status = 'error', sha256 = ?, "
                    "error_message = 'SHA256 mismatch after copy' WHERE id = ?",
                    (src_hash, fid)
                )
                conn.commit()
            return 'error', file_size, f"SHA256 mismatch for {filename}"

        os.unlink(src)
        with db_lock:
            conn.execute(
                "UPDATE files SET status = 'done', sha256 = ? WHERE id = ?",
                (src_hash, fid)
            )
            conn.commit()
        return 'done', file_size, None

    except Exception as e:
        with db_lock:
            conn.execute(
                "UPDATE files SET status = 'error', error_message = ? WHERE id = ?",
                (str(e), fid)
            )
            conn.commit()
        return 'error', file_size, f"{filename}: {e}"


def execute_transfers(conn, verbose=False, file_ids=None, workers=4):
    """Copy files with parallel workers. Verify SHA256, delete sources."""
    if file_ids:
        placeholders = ','.join('?' * len(file_ids))
        rows = conn.execute(
            f"SELECT id, original_path, dest_path, filename, file_size "
            f"FROM files WHERE status = 'pending' AND id IN ({placeholders}) ORDER BY dest_path",
            file_ids
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, original_path, dest_path, filename, file_size "
            "FROM files WHERE status = 'pending' ORDER BY dest_path"
        ).fetchall()

    total = len(rows)
    if total == 0:
        print("  No files to transfer.")
        return 0, 0, 0

    done = 0
    errors = 0
    duplicates = 0
    total_bytes = sum(r['file_size'] for r in rows)
    bytes_done = 0
    start_time = time.time()
    db_lock = threading.Lock()
    completed_count = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for row in rows:
            if shutdown_requested():
                break
            fut = pool.submit(
                _transfer_one_file,
                row['original_path'], row['dest_path'], row['id'],
                row['filename'], row['file_size'], db_lock, conn
            )
            futures[fut] = row

        for fut in as_completed(futures):
            if shutdown_requested():
                pool.shutdown(wait=False, cancel_futures=True)
                break

            row = futures[fut]
            completed_count += 1

            try:
                status, size, err_msg = fut.result()
            except Exception as e:
                errors += 1
                print(f"\n  ERROR: {row['filename']}: {e}")
                continue

            bytes_done += size
            if status == 'done':
                done += 1
            elif status == 'duplicate':
                duplicates += 1
                if verbose:
                    print(f"\n  DUPLICATE: {row['filename']}")
            elif status == 'error':
                errors += 1
                if err_msg:
                    print(f"\n  ERROR: {err_msg}")

            # Extract year/month from dest_path (e.g. .../2013/01/2013-01-04/file.jpg)
            dest = row['dest_path'] or ''
            parts = dest.split(os.sep)
            folder_label = ''
            for j, p in enumerate(parts):
                if re.match(r'^\d{4}$', p) and j + 1 < len(parts):
                    folder_label = f"{p}/{parts[j+1]}"
                    break

            elapsed = time.time() - start_time
            if elapsed > 0:
                rate_mb = (bytes_done / 1_048_576) / elapsed
                files_per_sec = completed_count / elapsed
                pct = bytes_done / total_bytes * 100 if total_bytes > 0 else 0
                print(f"\r  [{completed_count:,}/{total:,}] {pct:.1f}% | "
                      f"{humanize.naturalsize(bytes_done)}/{humanize.naturalsize(total_bytes)} | "
                      f"{rate_mb:.1f} MB/s | {files_per_sec:.1f} files/s | "
                      f"{folder_label:<7} | {workers}w   ",
                      end='', flush=True)

    print()
    return done, errors, duplicates
