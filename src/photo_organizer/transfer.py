"""Verified copy into the canonical store, then removal of the source.

A source file is deleted only after its bytes are proven to be on the destination storage:
- new content: copied in one pass (hashing as it writes), then read back from the NAS with the
  client cache bypassed and matched, then the source deleted;
- duplicate content: the canonical copy is re-read from the NAS and matched, then the source deleted.
Anything that fails verification keeps its source and is marked 'error'.
"""

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import humanize

from photo_organizer.constants import is_time_machine_path
from photo_organizer.db import NOW_SQL
from photo_organizer.hashing import LARGE_FILE, copy_and_hash, sha256_file
from photo_organizer.progress import set_progress
from photo_organizer.state import shutdown_requested


def recover_interrupted(conn):
    """Return rows left mid-transfer by a crash to 'pending', removing their possibly partial copy.
    The source is still in place for these rows, so the copy is simply redone."""
    rows = conn.execute(
        "SELECT id, dest_path FROM files WHERE status IN ('copying', 'copied')"
    ).fetchall()
    for r in rows:
        if r['dest_path'] and os.path.exists(r['dest_path']):
            os.unlink(r['dest_path'])
    conn.execute("UPDATE files SET status = 'pending' WHERE status IN ('copying', 'copied')")
    conn.commit()
    return len(rows)


SOURCE_KEPT = 'source not removed: '


def _has_time_machine_deny_acl(path):
    """Files copied out of a Time Machine backup keep its 'group:everyone deny ... delete' ACL."""
    out = subprocess.run(['ls', '-lde', path], capture_output=True, text=True).stdout
    return any('group:everyone deny' in line and 'delete' in line for line in out.splitlines()[1:])


def remove_source(path):
    """Delete a source whose bytes are verified in the store. Only the Time Machine deny ACL is
    stripped to allow this; any other permission problem is raised and the source stays."""
    if is_time_machine_path(path):
        raise PermissionError(f"refusing to delete inside a Time Machine backup: {path}")
    try:
        os.unlink(path)
    except PermissionError:
        if not _has_time_machine_deny_acl(path):
            raise
        subprocess.run(['chmod', '-N', path], check=True, capture_output=True)
        os.unlink(path)


def retry_source_removal(conn, sources):
    """Delete sources left behind by an earlier failed delete, after re-verifying their store copy."""
    placeholders = ','.join('?' * len(sources))
    rows = conn.execute(
        f"SELECT id, original_path, dest_path, sha256 FROM files WHERE status = 'done' "
        f"AND error_message LIKE '{SOURCE_KEPT}%' AND source_root IN ({placeholders})",
        sources,
    ).fetchall()
    removed = 0
    for r in rows:
        try:
            if os.path.exists(r['original_path']):
                if sha256_file(r['dest_path'], nocache=True) != r['sha256']:
                    continue
                remove_source(r['original_path'])
            conn.execute("UPDATE files SET error_message = NULL WHERE id = ?", (r['id'],))
            removed += 1
        except OSError as e:
            conn.execute("UPDATE files SET error_message = ? WHERE id = ?", (f"{SOURCE_KEPT}{e}", r['id']))
    conn.commit()
    return removed, len(rows)


COPY_ONLY = 'copy-only: source kept'


class LocalFS:
    """File operations through this Mac's mounts (SMB or local disk). NasFS mirrors this interface."""

    verifies_copy = False   # the copy must still be read back from the destination storage
    label = None

    exists = staticmethod(os.path.exists)
    getsize = staticmethod(os.path.getsize)
    unlink = staticmethod(os.unlink)
    remove_source = staticmethod(remove_source)

    ismount = staticmethod(os.path.ismount)
    rmdir = staticmethod(os.rmdir)
    rmtree = staticmethod(shutil.rmtree)

    @staticmethod
    def entries(path):
        """[(name, is_dir)] without following symlinks."""
        return [(e.name, e.is_dir(follow_symlinks=False)) for e in os.scandir(path)]

    @staticmethod
    def sha256(path, nocache=False, progress=None):
        return sha256_file(path, nocache=nocache, progress=progress)

    @staticmethod
    def copy_and_hash(src, dst, size, progress=None):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        return copy_and_hash(src, dst, progress=progress)

    def close(self):
        pass


class TransferContext:
    def __init__(self, conn, keep_sources=False, fs=None):
        self.conn = conn
        self.fs = fs or LocalFS()
        self.keep_sources = keep_sources
        self.large = {}             # fid -> live state of a >= LARGE_FILE transfer, for the status page
        self.large_lock = threading.Lock()
        self.completed = 0
        self.db_lock = threading.Lock()
        self.inflight = {}          # sha256 -> Event set when that content's first copy finishes
        self.verified = {}          # canonical dest_path -> bool (NAS re-read result this run)
        self.verify_lock = threading.Lock()

    def set_status(self, fid, status, sha=None, message=None, dest_path=None):
        with self.db_lock:
            self.conn.execute(
                f"UPDATE files SET status = ?, sha256 = COALESCE(?, sha256), error_message = ?, updated_at = {NOW_SQL}, "
                "dest_path = COALESCE(?, dest_path) WHERE id = ?",
                (status, sha, message, dest_path, fid),
            )
            self.conn.commit()

    def track(self, fid, src, size):
        """Progress callback factory for a large file: phase -> callback(bytes_done)."""
        if size < LARGE_FILE:
            return lambda phase: None

        def phase_cb(phase):
            with self.large_lock:
                self.large[fid] = {'name': os.path.basename(src), 'source': src, 'size': size,
                                   'phase': phase, 'done': 0, 'started': time.time()}

            def cb(done):
                self.large[fid]['done'] = done
            return cb
        return phase_cb

    def untrack(self, fid):
        with self.large_lock:
            self.large.pop(fid, None)

    def large_snapshot(self):
        now = time.time()
        with self.large_lock:
            items = [dict(v) for v in self.large.values()]
        for v in items:
            elapsed = max(now - v.pop('started'), 0.001)
            v['bytes_per_s'] = v['done'] / elapsed
        return sorted(items, key=lambda v: v['name'])

    def canonical_ok(self, path, sha):
        with self.verify_lock:
            if path in self.verified:
                return self.verified[path]
        ok = self.fs.exists(path) and self.fs.sha256(path, nocache=True) == sha
        with self.verify_lock:
            self.verified[path] = ok
        return ok


def _remove_duplicate(ctx, fid, src, src_hash, canonical):
    if not ctx.canonical_ok(canonical, src_hash):
        ctx.set_status(fid, 'error', src_hash, f"canonical copy {canonical} failed verification; source kept")
        return 'error', f"canonical copy failed verification: {canonical}"
    if ctx.keep_sources:
        with ctx.db_lock:
            ctx.conn.execute(
                f"UPDATE files SET status = 'duplicate_kept', sha256 = ?, dest_path = NULL, updated_at = {NOW_SQL}, "
                "error_message = ? WHERE id = ?",
                (src_hash, f"duplicate of {canonical}; {COPY_ONLY}", fid),
            )
            ctx.conn.commit()
        return 'duplicate_kept', None
    ctx.fs.remove_source(src)
    with ctx.db_lock:
        # Release the planned destination name so it isn't reserved by a file that was never copied.
        ctx.conn.execute(
            f"UPDATE files SET status = 'duplicate_removed', sha256 = ?, dest_path = NULL, updated_at = {NOW_SQL}, "
            "error_message = ? WHERE id = ?",
            (src_hash, f"duplicate of {canonical}; source removed", fid),
        )
        ctx.conn.commit()
    return 'duplicate_removed', None


def _claim_dest(ctx, fid, planned, src_hash):
    """Final destination name, chosen under db_lock among files actually copied or copying.
    Planned-but-uncopied rows don't reserve names, so a duplicate's winner keeps the clean name.
    An existing file is reused only if it is byte-identical (src_hash known); otherwise skipped."""
    dest_dir = os.path.dirname(planned)
    filename = ctx.conn.execute("SELECT filename FROM files WHERE id = ?", (fid,)).fetchone()[0]
    stem, ext = os.path.splitext(filename)
    for i in range(10000):
        candidate = os.path.join(dest_dir, filename if i == 0 else f"{stem}_{i}{ext}")
        if ctx.conn.execute(
            "SELECT 1 FROM files WHERE dest_path = ? AND status IN ('done', 'copying') AND id != ?",
            (candidate, fid),
        ).fetchone():
            continue
        if ctx.fs.exists(candidate) and (not src_hash or ctx.fs.sha256(candidate, nocache=True) != src_hash):
            continue  # stray file not from this catalog; never overwrite it
        return candidate
    raise RuntimeError(f"Too many collisions for {filename} in {dest_dir}")


def _could_be_duplicate(ctx, fid, size):
    """Identical content needs identical size; with no other cataloged file of this size,
    the separate pre-copy hash (used for duplicate detection) can be skipped."""
    return ctx.conn.execute(
        "SELECT 1 FROM files WHERE file_size = ? AND id != ? AND status IN ('pending', 'copying', 'done') LIMIT 1",
        (size, fid),
    ).fetchone() is not None


def _copy_verify_delete(ctx, fid, src, dst, expected, size):
    """Copy src to dst (unless an identical dst already exists), verify from the NAS, delete src."""
    fs = ctx.fs
    phase = ctx.track(fid, src, size)
    copied = False
    if fs.exists(dst):
        written = expected      # _claim_dest only hands back an existing file that matched expected
    else:
        try:
            written = fs.copy_and_hash(src, dst, size, progress=phase('copying'))
        except FileExistsError:
            raise               # someone else's file appeared at dst: never remove it
        except BaseException:
            try:
                if fs.exists(dst):
                    fs.unlink(dst)
            except OSError:
                pass            # recover_interrupted removes it on the next run
            raise
        copied = True
        if expected and written != expected:
            fs.unlink(dst)
            ctx.set_status(fid, 'error', expected, "source changed during copy; source kept")
            return 'error', f"source changed during copy: {src}"
    # NasFS.copy_and_hash already hashed the copy as stored on the NAS; anything else is read back.
    if not (copied and fs.verifies_copy) and \
            fs.sha256(dst, nocache=True, progress=phase('verifying on NAS')) != written:
        fs.unlink(dst)
        ctx.set_status(fid, 'error', written, "copy read back from destination did not match; source kept")
        return 'error', f"destination verification failed: {dst}"
    ctx.set_status(fid, 'done', written, COPY_ONLY if ctx.keep_sources else None)
    with ctx.verify_lock:
        ctx.verified[dst] = True
    if ctx.keep_sources:
        return 'done', None
    try:
        fs.remove_source(src)
    except OSError as e:
        # The copy is verified and stays 'done'; a later run retries the delete.
        ctx.set_status(fid, 'done', written, f"{SOURCE_KEPT}{e}")
        return 'source_kept', f"copied and verified, but source not removed: {src}: {e}"
    return 'done', None


def transfer_one(ctx, fid, src, planned):
    try:
        if not ctx.fs.exists(src):
            ctx.set_status(fid, 'error', None, "source missing")
            return 'error', f"source missing: {src}"
        size = ctx.fs.getsize(src)

        with ctx.db_lock:
            fast = not _could_be_duplicate(ctx, fid, size)
            if fast:
                dst = _claim_dest(ctx, fid, planned, None)
                ctx.conn.execute("UPDATE files SET status = 'copying', dest_path = ? WHERE id = ?", (dst, fid))
                ctx.conn.commit()
        if fast:
            return _copy_verify_delete(ctx, fid, src, dst, None, size)

        src_hash = ctx.fs.sha256(src, progress=ctx.track(fid, src, size)('hashing source'))
        while True:
            with ctx.db_lock:
                dup = ctx.conn.execute(
                    "SELECT dest_path FROM files WHERE sha256 = ? AND status = 'done' AND id != ?",
                    (src_hash, fid),
                ).fetchone()
                waiter = ctx.inflight.get(src_hash) if not dup else None
                if not dup and not waiter:
                    ctx.inflight[src_hash] = threading.Event()
                    dst = _claim_dest(ctx, fid, planned, src_hash)
                    ctx.conn.execute(
                        "UPDATE files SET status = 'copying', sha256 = ?, dest_path = ? WHERE id = ?",
                        (src_hash, dst, fid),
                    )
                    ctx.conn.commit()
            if dup:
                return _remove_duplicate(ctx, fid, src, src_hash, dup['dest_path'])
            if waiter:
                waiter.wait()      # identical content is being copied by another worker
                continue
            break

        try:
            return _copy_verify_delete(ctx, fid, src, dst, src_hash, size)
        finally:
            with ctx.db_lock:
                ctx.inflight.pop(src_hash).set()

    except Exception as e:
        ctx.set_status(fid, 'error', None, str(e))
        return 'error', f"{src}: {e}"
    finally:
        ctx.untrack(fid)


def execute_transfers(conn, file_ids, workers=4, verbose=False, keep_sources=False, fs=None):
    """Transfer the batch's planned files. Returns {status: count}."""
    placeholders = ','.join('?' * len(file_ids))
    rows = conn.execute(
        f"SELECT id, original_path, dest_path, filename, file_size FROM files "
        f"WHERE status = 'pending' AND dest_path IS NOT NULL AND id IN ({placeholders}) "
        f"ORDER BY dest_path",
        file_ids,
    ).fetchall()
    counts = {'done': 0, 'duplicate_removed': 0, 'duplicate_kept': 0, 'source_kept': 0, 'error': 0}
    if not rows:
        print("  No files to transfer.")
        return counts

    ctx = TransferContext(conn, keep_sources, fs)
    total = len(rows)
    total_bytes = sum(r['file_size'] or 0 for r in rows)
    bytes_done = completed = 0
    start = time.time()
    phase = f"transferring {ctx.fs.label}" if ctx.fs.label else 'transferring'
    set_progress(conn, ctx.db_lock, phase=phase, batch_done=0, batch_total=total, large=[])

    # Big files can run for minutes without anything finishing; keep the status page live meanwhile.
    beat_stop = threading.Event()

    def heartbeat():
        while not beat_stop.wait(2):
            set_progress(conn, ctx.db_lock, batch_done=ctx.completed, large=ctx.large_snapshot())

    beat = threading.Thread(target=heartbeat, name='progress-heartbeat', daemon=True)
    beat.start()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for row in rows:
            if shutdown_requested():
                break
            futures[pool.submit(transfer_one, ctx, row['id'], row['original_path'], row['dest_path'])] = row

        for fut in as_completed(futures):
            row = futures[fut]
            status, err = fut.result()
            counts[status] += 1
            completed += 1
            ctx.completed = completed
            bytes_done += row['file_size'] or 0
            if err:
                print(f"\n  ERROR: {err}")
            elif verbose and status == 'duplicate_removed':
                print(f"\n  DUPLICATE removed: {row['original_path']}")
            elapsed = time.time() - start
            if elapsed > 0:
                pct = bytes_done / total_bytes * 100 if total_bytes else 100
                print(f"\r  [{completed:,}/{total:,}] {pct:.1f}% | "
                      f"{humanize.naturalsize(bytes_done)}/{humanize.naturalsize(total_bytes)} | "
                      f"{bytes_done / 1_048_576 / elapsed:.1f} MB/s | {workers}w   ",
                      end='', flush=True)
            if shutdown_requested():
                set_progress(conn, ctx.db_lock, phase='stopping: finishing in-flight files')
                pool.shutdown(wait=True, cancel_futures=True)
                break
    beat_stop.set()
    beat.join()
    set_progress(conn, ctx.db_lock, batch_done=completed, large=[])
    print()
    return counts
