"""Record each stored file's camera and lens (EXIF Make / Model / LensModel) in the catalog.

exiftool runs on the NAS itself (a copy of this Mac's, pure Perl, kept in ~/.photo-organizer/exiftool
on the NAS), so files are read from local disks there instead of over SMB. Files go to a few
long-lived `exiftool -stay_open` processes in small batches; each batch is reported as it lands.
Resumable: a file is read once (camera_read_at), whether or not it names a camera.

  photo-organizer-cameras run --dest /Volumes/photoArk [--batch 20] [--procs 4] [--limit N]
  photo-organizer-cameras stats
"""

import argparse
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from photo_organizer.db import DEFAULT_DB_PATH, NOW_SQL, init_db
from photo_organizer.state import shutdown_requested, signal_handler

NAS_DIR = '.photo-organizer/exiftool'           # under the ssh user's home on the NAS
LOCAL_EXIFTOOL = '/opt/homebrew/opt/exiftool/libexec'
TAGS = ('Make', 'Model', 'LensModel')
SKIP_EXTENSIONS = ('.xmp', '.aae', '.thm')       # sidecars name no camera

ROMAN = {'2': 'II', '3': 'III', '4': 'IV', '5': 'V', '6': 'VI'}
BRANDS = {'sony': 'Sony', 'apple': 'Apple', 'canon': 'Canon', 'nikon corporation': 'Nikon', 'nikon': 'Nikon',
          'olympus imaging corp.': 'Olympus', 'olympus corporation': 'Olympus', 'fujifilm': 'Fujifilm',
          'panasonic': 'Panasonic', 'samsung': 'Samsung', 'google': 'Google', 'gopro': 'GoPro', 'dji': 'DJI',
          'eastman kodak company': 'Kodak', 'ricoh imaging company, ltd.': 'Ricoh', 'leica camera ag': 'Leica'}


def camera_name(make, model):
    """Friendly camera name: 'ILCE-1' -> 'Sony A1', 'ILCE-7RM4' -> 'Sony A7R IV', 'iPhone 17 Pro' as is."""
    make, model = (make or '').strip(), re.sub(r'\s*\(V[\d.]+\)$', '', (model or '').strip())   # '(V03.30)': firmware
    if not model:
        return BRANDS.get(make.lower(), make) or None
    brand = BRANDS.get(make.lower(), make.title() if make.isupper() else make)
    if brand == 'Sony':
        m = re.fullmatch(r'ILCE-(\d+)([A-Z]*?)(?:M(\d))?', model)
        if m:
            return f"Sony A{m.group(1)}{m.group(2)}" + (f" {ROMAN.get(m.group(3), m.group(3))}" if m.group(3) else '')
        m = re.fullmatch(r'(?:SLT|DSLR|ILCA)-A(\d+)V?', model)
        if m:
            return f"Sony A{m.group(1)}"
        m = re.fullmatch(r'DSC-([A-Z]+\d+)(?:M(\d))?', model)
        if m:
            return f"Sony {m.group(1)}" + (f" {ROMAN.get(m.group(2), m.group(2))}" if m.group(2) else '')
    if brand == 'Apple':
        return model
    if model.lower().startswith(brand.lower()):          # 'NIKON D3100' -> 'Nikon D3100'
        return brand + model[len(brand):]
    return f"{brand} {model}" if brand else model


# --- exiftool on the NAS -----------------------------------------------------------------------

def _ssh(target, command, **kw):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', target, command], **kw)


def nas_exiftool_command(target):
    """The command that runs exiftool on the NAS, installing (or updating) its copy first."""
    lib, exe = f"$HOME/{NAS_DIR}/lib/perl5", f"$HOME/{NAS_DIR}/bin/exiftool"
    command = f'perl -I"{lib}" "{exe}"'
    want = subprocess.run(['exiftool', '-ver'], capture_output=True, text=True).stdout.strip()
    have = _ssh(target, f"{command} -ver 2>/dev/null", capture_output=True, text=True).stdout.strip()
    if have != want:
        print(f"Installing exiftool {want} on {target} (was {have or 'missing'})")
        tar = subprocess.Popen(['tar', '-czf', '-', '-C', LOCAL_EXIFTOOL, 'bin/exiftool', 'lib'],
                               stdout=subprocess.PIPE)
        _ssh(target, f'rm -rf "$HOME/{NAS_DIR}" && mkdir -p "$HOME/{NAS_DIR}" && tar -xzf - -C "$HOME/{NAS_DIR}"',
             stdin=tar.stdout, check=True)
        tar.wait()
    return command


class StayOpen:
    """One `exiftool -stay_open` on the NAS: send a batch of paths, get one JSON record per file."""

    def __init__(self, target, command):
        self.proc = subprocess.Popen(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=30', target,
             f"{command} -stay_open True -@ -"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', start_new_session=True)   # Ctrl-C stops between batches

    def read(self, paths, tags=TAGS, options=()):
        # No -q: it also suppresses the {ready} line that ends each batch.
        args = ['-json', '-fast', *options, '-charset', 'filename=utf8', *[f'-{t}' for t in tags], *paths,
                '-execute']
        self.proc.stdin.write('\n'.join(args) + '\n')
        self.proc.stdin.flush()
        out = []
        for line in self.proc.stdout:
            if line.strip() == '{ready}':
                break
            out.append(line)
        else:
            raise RuntimeError("exiftool on the NAS exited")
        text = ''.join(out).strip()
        return {r['SourceFile']: r for r in json.loads(text)} if text else {}

    def close(self):
        try:
            self.proc.stdin.write('-stay_open\nFalse\n')
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.proc.kill()


# --- backfill ----------------------------------------------------------------------------------

def unread(conn, limit=0):
    skip = ' AND '.join(f"lower(dest_path) NOT LIKE '%{e}'" for e in SKIP_EXTENSIONS)
    return conn.execute(
        f"SELECT id, dest_path FROM files WHERE status = 'done' AND dest_path IS NOT NULL "
        f"AND camera_read_at IS NULL AND {skip} ORDER BY id" + (f" LIMIT {int(limit)}" if limit else '')
    ).fetchall()


def record(conn, rows, found):
    """Store what exiftool reported for rows ([(id, nas_path)]); files it couldn't read stay unnamed."""
    for fid, nas_path in rows:
        r = found.get(nas_path, {})
        make, model, lens = (str(r[t]).strip() if r.get(t) not in (None, '') else None for t in TAGS)
        conn.execute(
            f"UPDATE files SET camera_make = ?, camera_model = ?, lens_model = ?, camera = ?, "
            f"camera_read_at = {NOW_SQL} WHERE id = ?",
            (make, model, lens, camera_name(make, model) if (make or model) else None, fid))


def backfill(conn, fs, batch=20, procs=4, limit=0, readers=None, out=print):
    """Read camera tags for stored files not read yet. readers: StayOpen-likes (tests).
    Returns (files read, files naming a camera)."""
    todo = [(r['id'], fs.remote(r['dest_path'])) for r in unread(conn, limit)]
    if not todo:
        out("Every stored file's camera is already recorded.")
        return 0, 0
    if readers is None:
        command = nas_exiftool_command(fs.target)
        readers = [StayOpen(fs.target, command) for _ in range(max(1, min(procs, len(todo) // batch + 1)))]
    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    lock = threading.Lock()
    state = {'next': 0, 'done': 0, 'named': 0, 'cameras': collections.Counter(), 'error': None}
    start = time.time()
    out(f"Reading camera tags of {len(todo):,} files on the NAS: {len(readers)} exiftool processes, "
        f"{batch} files per batch")

    def work(reader):
        while not shutdown_requested() and state['error'] is None:
            with lock:
                if state['next'] >= len(batches):
                    return
                rows = batches[state['next']]
                state['next'] += 1
            try:
                found = reader.read([p for _, p in rows])
            except Exception as e:
                state['error'] = e
                return
            with lock:
                record(conn, rows, found)
                conn.commit()
                names = collections.Counter(
                    camera_name(found[p].get('Make'), found[p].get('Model')) for _, p in rows
                    if p in found and (found[p].get('Make') or found[p].get('Model')))
                state['done'] += len(rows)
                state['named'] += sum(names.values())
                state['cameras'].update(names)
                rate = state['done'] / max(time.time() - start, 1e-6)
                eta = (len(todo) - state['done']) / rate if rate else 0
                top = ', '.join(f"{n} ×{c}" for n, c in names.most_common(3)) or 'no camera tags'
                out(f"  [{state['done']:,}/{len(todo):,}] {rate:.0f} files/s, ~{eta / 60:.0f} min left | {top}")

    threads = [threading.Thread(target=work, args=(r,), daemon=True) for r in readers]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for r in readers:
            r.close()
    if state['error'] is not None:
        raise state['error']
    out(f"Read {state['done']:,} files, {state['named']:,} name a camera. Most common: "
        + ', '.join(f"{n} ({c:,})" for n, c in state['cameras'].most_common(6)))
    return state['done'], state['named']


def stats(conn):
    rows = conn.execute(
        "SELECT camera, COUNT(*) n, MIN(substr(taken_at, 1, 4)) first, MAX(substr(taken_at, 1, 4)) last "
        "FROM files WHERE status = 'done' AND camera_read_at IS NOT NULL GROUP BY camera ORDER BY n DESC").fetchall()
    for r in rows:
        print(f"  {r['n']:>8,}  {r['camera'] or '(no camera tag)':<32} {r['first'] or ''}–{r['last'] or ''}")
    left = len(unread(conn))
    print(f"  {left:,} stored files not read yet" if left else "  every stored file has been read")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_run = sub.add_parser('run', help='Read camera tags of stored files on the NAS (resumable)')
    p_run.add_argument('--dest', required=True, help='root of the canonical store')
    p_run.add_argument('--batch', type=int, default=20, help='Files per batch (one progress line each)')
    p_run.add_argument('--procs', type=int, default=4, help='Parallel exiftool processes on the NAS')
    p_run.add_argument('--limit', type=int, default=0)
    sub.add_parser('stats', help='Files per camera')
    args = parser.parse_args()
    conn = init_db(args.db)
    if args.cmd == 'stats':
        stats(conn)
        return
    from photo_organizer.nas import detect
    fs, note = detect([], os.path.abspath(args.dest))
    if not fs:
        sys.exit(f"Error: the store isn't reachable on its NAS over ssh ({note})")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        backfill(conn, fs, args.batch, args.procs, args.limit, out=lambda s: print(s, flush=True))
    finally:
        fs.close()
        conn.close()
    if shutdown_requested():
        print("Stopped; run again to continue.")


if __name__ == '__main__':
    main()
