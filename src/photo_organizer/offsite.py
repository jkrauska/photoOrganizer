"""Offsite copy of the store in S3 Glacier Deep Archive: one tar per year, or one per month for big
years (big months in parts: 2023-06.1, 2023-06.2, ...).

  photo-offsite plan    [--ark /Volumes/photoArk] [--only 1999,2021-07]   units, file counts, sizes
  photo-offsite upload  --bucket joel-photo-ark [--only ...] [--redo]    stream each tar to S3
  photo-offsite verify                       S3 still has every tar, at its size, in DEEP_ARCHIVE
  photo-offsite restore UNIT                 Bulk thaw of one tar (up to 48 h, readable for 7 days)
  photo-offsite check   UNIT                 download a thawed tar, compare its sha256 and size

Nothing is staged on disk: each tar is written straight into `aws s3 cp -`, hashing every file
as it is read and the tar stream as a whole. A unit is recorded in index.tsv only after S3 reports
the object at the exact size written, so a failed upload is simply redone by the next run.
Each tar gets a manifest (path, size, mtime, sha256 and the tar's S3 URI per file) and a line in
index.tsv, both kept locally and in S3 Standard, so "which tar has DSC01234.ARW?" needs no thaw.

AWS credentials: the profile in $AWS_PROFILE, else "photoark" (the upload-only user made by
scripts/offsite-setup.sh). Local state lives in ~/.photo-organizer/offsite/.
"""

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from photo_organizer.constants import SKIP_DIR_SUFFIXES, SKIP_NAMES

STATE_DIR = Path(os.environ.get('PHOTO_OFFSITE_DIR', '~/.photo-organizer/offsite')).expanduser()
DEFAULT_ARK = '/Volumes/photoArk'
PROFILE = os.environ.get('AWS_PROFILE', 'photoark')
# Largest tar. Years up to this are one tar; bigger ones get a tar per month, and a month bigger
# still (single days reach 240 GiB) is cut into parts NAME.1, NAME.2, ... in path order. Streamed
# uploads use the profile's 8 MB parts (fast here; 64 MB parts were 4x slower), and S3 allows 10,000
# of them, so no tar may pass 78 GiB. It also bounds a retry to ~2.5 h of upload.
UNIT_MAX_BYTES = 64 * 2**30
STALE_DAYS = 90
INDEX_FIELDS = ('unit', 's3_uri', 'source', 'files', 'bytes', 'sha256', 'uploaded_at')
CHUNK = 1 << 20
PROGRESS_SECONDS = 10


@dataclass
class Unit:
    name: str
    files: list = field(default_factory=list)   # (path relative to the ark, size)

    @property
    def size(self):
        return sum(s for _, s in self.files)


def walk(ark, rel):
    """(relative path, size) of every file under ark/rel, sorted; junk and NAS caches skipped."""
    out = []
    with os.scandir(os.path.join(ark, rel)) as it:
        for e in it:
            if e.name in SKIP_NAMES or e.name.startswith('._'):
                continue
            path = f'{rel}/{e.name}'
            if e.is_dir(follow_symlinks=False):
                if not e.name.startswith('.') and not e.name.endswith(SKIP_DIR_SUFFIXES):
                    out += walk(ark, path)
            elif e.is_file(follow_symlinks=False):
                out.append((path, e.stat().st_size))
    return sorted(out)


def plan_units(ark, only=None):
    """Units for every year folder in the ark (only: unit names or years to keep)."""
    units = []
    for year in sorted(d for d in os.listdir(ark) if re.fullmatch(r'\d{4}', d) and os.path.isdir(f'{ark}/{d}')):
        if only and not any(o.split('-')[0] == year for o in only):
            continue
        files = walk(ark, year)
        if sum(s for _, s in files) <= UNIT_MAX_BYTES:
            units.append(Unit(year, files))
            continue
        months = {}
        for path, size in files:
            parts = path.split('/')
            key = f'{year}-{parts[1]}' if len(parts) > 2 and re.fullmatch(r'\d\d', parts[1]) else f'{year}-misc'
            months.setdefault(key, Unit(key)).files.append((path, size))
        for month in sorted(months.values(), key=lambda u: u.name):
            units += split(month)
    if only:
        units = [u for u in units if {u.name, base(u.name), u.name.split('-')[0]} & only]
    return units


def split(unit):
    """The unit, or consecutive parts NAME.1, NAME.2, ... of at most UNIT_MAX_BYTES each."""
    if unit.size <= UNIT_MAX_BYTES:
        return [unit]
    parts, total = [Unit(f'{unit.name}.1')], 0
    for path, size in unit.files:
        if parts[-1].files and total + size > UNIT_MAX_BYTES:
            parts.append(Unit(f'{unit.name}.{len(parts) + 1}'))
            total = 0
        parts[-1].files.append((path, size))
        total += size
    return parts


def base(name):
    """The year or month a unit covers: 2023-06.2 -> 2023-06."""
    return name.split('.')[0]


def is_open(name, today):
    """The unit's year or month isn't over yet, so more photos may still land in it."""
    return base(name) in (str(today.year), f'{today:%Y-%m}', f'{today.year}-misc')


def read_index(state=STATE_DIR):
    """{unit: row}; a unit uploaded again has its latest line win."""
    path = Path(state) / 'index.tsv'
    if not path.exists():
        return {}
    with path.open(newline='') as f:
        return {r['unit']: r for r in csv.DictReader(f, delimiter='\t')}


def write_index(rows, state=STATE_DIR):
    path = Path(state) / 'index.tsv'
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', newline='') as f:
        w = csv.DictWriter(f, INDEX_FIELDS, delimiter='\t', lineterminator='\n')
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: r['unit']))
    tmp.replace(path)


def split_uri(uri):
    bucket, _, key = uri.removeprefix('s3://').partition('/')
    return bucket, key


def aws_env():
    """Keys exported in the shell would outrank the profile, so admin credentials could do the upload."""
    env = {k: v for k, v in os.environ.items()
           if k not in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_CREDENTIAL_EXPIRATION')}
    return {**env, 'AWS_PROFILE': PROFILE}


def aws(*args):
    """stdout of one AWS CLI call; raises with its stderr on failure."""
    p = subprocess.run(['aws', *args], env=aws_env(), capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or f'aws {args[0]} {args[1]} failed')
    return p.stdout


class HashWriter:
    """File-like sink that hashes and counts what passes through it."""

    def __init__(self, out):
        self.out, self.sha, self.bytes = out, hashlib.sha256(), 0

    def write(self, b):
        self.sha.update(b)
        self.bytes += len(b)
        self.out.write(b)
        return len(b)


class HashReader:
    def __init__(self, f):
        self.f, self.sha = f, hashlib.sha256()

    def read(self, n=-1):
        b = self.f.read(n)
        self.sha.update(b)
        return b


def progress_path(name, state=STATE_DIR):
    return Path(state) / 'uploading' / f'{name}.json'


def read_uploading(state=STATE_DIR):
    """{unit: progress} for uploads whose process is still running; a killed upload leaves its file behind."""
    live = {}
    for path in (Path(state) / 'uploading').glob('*.json'):
        try:
            p = json.loads(path.read_text())
            os.kill(p['pid'], 0)
        except (OSError, ValueError, KeyError):
            continue
        live[p['unit']] = p
    return live


def upload_unit(unit, ark, bucket, state=STATE_DIR, log=print):
    """Stream one tar to Deep Archive, then record its manifest and index line (locally and in S3).
    While it runs, uploading/UNIT.json holds its pid and how far it got, for the Offsite tab."""
    progress = progress_path(unit.name, state)
    progress.parent.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now().isoformat(timespec='seconds')

    def report(files_done, bytes_done):
        tmp = progress.with_suffix('.tmp')
        tmp.write_text(json.dumps({'unit': unit.name, 'pid': os.getpid(), 'started_at': started,
                                   'files': len(unit.files), 'bytes': unit.size,
                                   'files_done': files_done, 'bytes_done': bytes_done}))
        tmp.replace(progress)

    report(0, 0)
    try:
        return stream_unit(unit, ark, bucket, state, log, report)
    finally:
        progress.unlink(missing_ok=True)


def stream_unit(unit, ark, bucket, state, log, report):
    key = f'tars/{unit.name}.tar'
    # Sizes the parts (10,000 max) for the classic client, so an overestimate is fine; CRT ignores it
    # and uses the profile's multipart_chunksize (scripts/offsite-setup.sh).
    expected = sum(1536 + -(-s // 512) * 512 for _, s in unit.files) + 20480
    # Own session: Ctrl-C must not reach the CLI, which could then complete a truncated tar.
    proc = subprocess.Popen(['aws', 's3', 'cp', '-', f's3://{bucket}/{key}', '--storage-class', 'DEEP_ARCHIVE',
                             '--expected-size', str(expected), '--only-show-errors'],
                            stdin=subprocess.PIPE, env=aws_env(), start_new_session=True)
    out = HashWriter(proc.stdin)
    rows, done, logged_step = [], 0, 0
    t0 = reported = time.monotonic()
    try:
        tar = tarfile.open(fileobj=out, mode='w|', format=tarfile.PAX_FORMAT, copybufsize=CHUNK)
        for i, (rel, _) in enumerate(unit.files, 1):
            with open(os.path.join(ark, rel), 'rb') as f:
                st = os.fstat(f.fileno())
                info = tarfile.TarInfo(rel)
                info.size, info.mtime, info.mode = st.st_size, st.st_mtime, 0o644
                reader = HashReader(f)
                tar.addfile(info, reader)
                rows.append((rel, st.st_size, int(st.st_mtime), reader.sha.hexdigest()))
            done += st.st_size
            if time.monotonic() - reported >= PROGRESS_SECONDS:
                report(i, done)
                reported = time.monotonic()
            # One line per 10% of the unit's bytes; a file that crosses several steps logs only the last.
            step = done * 10 // max(unit.size, 1)
            if step > logged_step and step < 10:
                logged_step = step
                secs = max(time.monotonic() - t0, 1e-3)
                eta = dt.datetime.now() + dt.timedelta(seconds=secs * (unit.size - done) / done)
                log(f'{step * 10}% complete ({done * 8 / secs / 1e6:.0f}Mb/s) estimated completion time {eta:%H:%M}')
        tar.close()
        proc.stdin.close()
    except BaseException:
        # Killed before stdin reaches EOF, so S3 never completes the object; the partial
        # multipart upload is removed by the bucket's 7-day lifecycle rule.
        proc.kill()
        proc.wait()
        raise
    if proc.wait():
        raise RuntimeError(f'{unit.name}: aws s3 cp exited {proc.returncode}')
    head = json.loads(aws('s3api', 'head-object', '--bucket', bucket, '--key', key))
    if head['ContentLength'] != out.bytes or head.get('StorageClass') != 'DEEP_ARCHIVE':
        raise RuntimeError(f"{unit.name}: S3 has {head['ContentLength']} bytes in {head.get('StorageClass')}, "
                           f"wrote {out.bytes}")

    manifest = Path(state) / 'manifests' / f'{unit.name}.tsv.gz'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(manifest, 'wt', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(('path', 'size', 'mtime', 'sha256', 'tar'))
        w.writerows(row + (f's3://{bucket}/{key}',) for row in rows)
    aws('s3', 'cp', str(manifest), f's3://{bucket}/manifests/{manifest.name}', '--only-show-errors')
    index = read_index(state)
    index[unit.name] = {'unit': unit.name, 's3_uri': f's3://{bucket}/{key}', 'source': ark, 'files': len(rows),
                        'bytes': out.bytes, 'sha256': out.sha.hexdigest(),
                        'uploaded_at': dt.datetime.now().isoformat(timespec='seconds')}
    write_index(index, state)
    aws('s3', 'cp', str(Path(state) / 'index.tsv'), f's3://{bucket}/index.tsv', '--only-show-errors')
    return index[unit.name]


def bucket_region(bucket):
    # LocationConstraint is null for us-east-1.
    return json.loads(aws('s3api', 'get-bucket-location', '--bucket', bucket))['LocationConstraint'] or 'us-east-1'


def verify(state=STATE_DIR, lister=None, region_of=bucket_region):
    """List the bucket and compare it with index.tsv; the result is saved to verify.json and returned.
    Each unit is ok, missing, size (wrong length), class (not DEEP_ARCHIVE) or no-manifest.
    Also records each bucket's region and the storage class S3 reports for every tar and manifest."""
    index = read_index(state)
    result = {'checked_at': dt.datetime.now().isoformat(timespec='seconds'), 'error': None, 'units': {}, 'extra': [],
              'buckets': {}, 'classes': {}}
    try:
        for bucket in sorted({split_uri(r['s3_uri'])[0] for r in index.values()}):
            result['buckets'][bucket] = {'region': region_of(bucket)}
            listing = lister(bucket) if lister else json.loads(aws(
                's3api', 'list-objects-v2', '--bucket', bucket, '--output', 'json',
                '--query', 'Contents[].{k: Key, s: Size, c: StorageClass}') or 'null') or []
            objects = {o['k']: o for o in listing}
            expected = set()
            for name, r in index.items():
                b, key = split_uri(r['s3_uri'])
                if b != bucket:
                    continue
                manifest = f'manifests/{name}.tsv.gz'
                expected |= {key, manifest}
                o = objects.get(key)
                result['units'][name] = ('missing' if not o else 'size' if o['s'] != int(r['bytes'])
                                         else 'class' if o['c'] != 'DEEP_ARCHIVE'
                                         else 'no-manifest' if manifest not in objects else 'ok')
                result['classes'][name] = {'tar': o and o['c'], 'manifest': (objects.get(manifest) or {}).get('c')}
            result['buckets'][bucket]['index_class'] = (objects.get('index.tsv') or {}).get('c')
            result['extra'] += [f's3://{bucket}/{k}' for k in sorted(objects)
                                if k not in expected and k != 'index.tsv']
    except (RuntimeError, OSError, ValueError) as e:
        result['error'] = str(e)
    Path(state).mkdir(parents=True, exist_ok=True)
    (Path(state) / 'verify.json').write_text(json.dumps(result, indent=1))
    return result


def read_verify(state=STATE_DIR):
    path = Path(state) / 'verify.json'
    return json.loads(path.read_text()) if path.exists() else None


def manifest_paths(name, state=STATE_DIR):
    path = Path(state) / 'manifests' / f'{name}.tsv.gz'
    if not path.exists():
        return set()
    with gzip.open(path, 'rt', newline='') as f:
        return {row[0] for row in csv.reader(f, delimiter='\t')}


def catalog_paths(catalog, ark):
    """Relative paths of every file the organizer has stored in the ark."""
    with sqlite3.connect(f'file:{catalog}?mode=ro', uri=True, timeout=30) as conn:
        return [p[len(ark) + 1:] for (p,) in conn.execute(
            "SELECT dest_path FROM files WHERE status = 'done' AND substr(dest_path, 1, ?) = ?",
            (len(ark) + 1, ark + '/'))]


def unit_of(rel, groups):
    """The backed-up year or month (a key of groups) a stored file belongs in, or None."""
    parts = rel.split('/')
    sub = f'{parts[0]}-{parts[1]}' if len(parts) > 2 else f'{parts[0]}-misc'
    return sub if sub in groups else parts[0] if parts[0] in groups else None


def summary(catalog=None, state=STATE_DIR, today=None):
    """Everything the Offsite tab shows: a red/yellow/green light with its reasons, one row per
    unit, and the years the catalog has that aren't backed up at all."""
    today = today or dt.date.today()
    index, last = read_index(state), read_verify(state) or {}
    classes = last.get('classes', {})
    units = {name: {'unit': name, 'files': int(r['files']), 'bytes': int(r['bytes']), 'uploaded_at': r['uploaded_at'],
                    's3_uri': r['s3_uri'], 'status': last.get('units', {}).get(name, 'unverified'),
                    'tar_class': classes.get(name, {}).get('tar'),
                    'manifest_class': classes.get(name, {}).get('manifest'), 'new_files': 0}
             for name, r in index.items()}
    seen = last.get('buckets', {})
    buckets = [{'bucket': b, 'region': seen.get(b, {}).get('region'), 'index_class': seen.get(b, {}).get('index_class'),
                'units': sum(split_uri(u['s3_uri'])[0] == b for u in units.values()),
                'bytes': sum(u['bytes'] for u in units.values() if split_uri(u['s3_uri'])[0] == b)}
               for b in sorted({split_uri(u['s3_uri'])[0] for u in units.values()})]
    missing_years = {}
    if catalog and os.path.exists(catalog):
        ark = next(iter(index.values()))['source'] if index else DEFAULT_ARK
        groups = {}
        for name in sorted(index, key=lambda n: (base(n), int(n.partition('.')[2] or 0))):
            groups.setdefault(base(name), []).append(name)
        backed = {b: set().union(*(manifest_paths(n, state) for n in names)) for b, names in groups.items()}
        for rel in catalog_paths(catalog, ark):
            if not re.fullmatch(r'\d{4}', rel.split('/')[0]):
                continue
            b = unit_of(rel, groups)
            if b:
                units[groups[b][-1]]['new_files'] += rel not in backed[b]
            else:
                missing_years[rel[:4]] = missing_years.get(rel[:4], 0) + 1

    uploading = read_uploading(state)
    for name, p in uploading.items():
        if name not in units:
            units[name] = {'unit': name, 'files': p['files'], 'bytes': p['bytes'], 'uploaded_at': None,
                           's3_uri': None, 'status': 'in progress', 'tar_class': None, 'manifest_class': None,
                           'new_files': 0}
        units[name]['progress'] = p
    bad = [u['unit'] for u in units.values() if u['status'] not in ('ok', 'unverified', 'in progress')]
    reasons, light = [], 'green'
    if bad:
        light = 'red'
        reasons.append(f"S3 doesn't match the index for {', '.join(bad)}")
    if last and last.get('extra'):
        reasons.append(f"{len(last['extra'])} object(s) in S3 that the index doesn't list")
    if not index:
        reasons.append('nothing backed up yet')
    elif not last:
        reasons.append('never verified')
    elif last.get('error'):
        reasons.append(f"last verify couldn't reach S3: {last['error']}")
    else:
        age = (today - dt.date.fromisoformat(last['checked_at'][:10])).days
        if age > STALE_DAYS:
            reasons.append(f'last verified {age} days ago')
        unverified = [n for n, u in units.items() if u['status'] == 'unverified']
        if unverified:
            reasons.append(f"uploaded since the last verify: {', '.join(unverified)}")
    drifted = [f"{n} (+{u['new_files']})" for n, u in units.items() if u['new_files']]
    if drifted:
        reasons.append(f"files added after backup: {', '.join(drifted)}")
    if light == 'green' and reasons:
        light = 'yellow'
    return {'light': light, 'reasons': reasons, 'verified_at': last.get('checked_at'), 'buckets': buckets,
            'units': sorted(units.values(), key=lambda u: u['unit']),
            'not_backed_up': [{'year': y, 'files': n, 'open': is_open(y, today),
                               'uploading': any(u[:4] == y for u in uploading)}
                              for y, n in sorted(missing_years.items())]}


def restore(name, state=STATE_DIR, days=7):
    bucket, key = split_uri(read_index(state)[name]['s3_uri'])
    aws('s3api', 'restore-object', '--bucket', bucket, '--key', key, '--restore-request',
        json.dumps({'Days': days, 'GlacierJobParameters': {'Tier': 'Bulk'}}))


def check(name, state=STATE_DIR):
    """(ok, message): stream a thawed tar from S3 and compare its sha256 and size with the index."""
    r = read_index(state)[name]
    bucket, key = split_uri(r['s3_uri'])
    head = json.loads(aws('s3api', 'head-object', '--bucket', bucket, '--key', key))
    if 'ongoing-request="false"' not in head.get('Restore', ''):
        return False, f"{name} isn't thawed yet ({head.get('Restore') or 'no restore requested'})"
    proc = subprocess.Popen(['aws', 's3', 'cp', r['s3_uri'], '-', '--only-show-errors'],
                            stdout=subprocess.PIPE, env=aws_env())
    sha, n = hashlib.sha256(), 0
    while b := proc.stdout.read(CHUNK):
        sha.update(b)
        n += len(b)
    if proc.wait():
        return False, f'download failed (aws exited {proc.returncode})'
    if n != int(r['bytes']) or sha.hexdigest() != r['sha256']:
        return False, f"{name}: got {n} bytes sha256 {sha.hexdigest()}, index has {r['bytes']} {r['sha256']}"
    return True, f'{name}: {n} bytes, sha256 matches'


def gib(n):
    return f'{n / 2**30:,.2f} GiB'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('plan', 'upload'):
        p = sub.add_parser(name)
        p.add_argument('--ark', default=DEFAULT_ARK)
        p.add_argument('--only', type=lambda s: set(filter(None, s.split(','))),
                       help='comma-separated units or years, e.g. 1999,2021-07')
    up = sub.choices['upload']
    up.add_argument('--bucket', required=True)
    up.add_argument('--redo', action='store_true', help='upload the --only units again even if already backed up')
    sub.add_parser('verify')
    for name in ('restore', 'check'):
        sub.add_parser(name).add_argument('unit')
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.cmd in ('plan', 'upload'):
        ark = args.ark.rstrip('/')
        index, today = read_index(), dt.date.today()
        units = plan_units(ark, args.only)
        if args.cmd == 'plan':
            for u in units:
                note = 'backed up' if u.name in index else 'not finished yet' if is_open(u.name, today) else ''
                print(f'{u.name:<10} {len(u.files):>8,} files {gib(u.size):>14}  {note}')
            print(f'{len(units)} units, {sum(len(u.files) for u in units):,} files, {gib(sum(u.size for u in units))}')
            return
        if args.redo and not args.only:
            raise SystemExit('--redo needs --only')
        todo = [u for u in units if (u.name not in index or args.redo)
                and (not is_open(u.name, today) or (args.only and u.name in args.only))]
        print(f'{len(todo)} unit(s) to upload, {gib(sum(u.size for u in todo))}')
        for u in todo:
            start = dt.datetime.now()
            print(f'{u.name}: {len(u.files):,} files, {gib(u.size)}')
            r = upload_unit(u, ark, args.bucket)
            secs = max((dt.datetime.now() - start).total_seconds(), 1)
            print(f"{u.name}: done, {gib(int(r['bytes']))} in {secs:.0f}s "
                  f"({int(r['bytes']) * 8 / secs / 1e6:.0f} Mbps), sha256 {r['sha256'][:16]}")
    elif args.cmd == 'verify':
        r = verify()
        bad = {n: s for n, s in r['units'].items() if s != 'ok'}
        print(f"{len(r['units']) - len(bad)}/{len(r['units'])} units ok")
        for n, s in bad.items():
            print(f'  {n}: {s}')
        for k in r['extra']:
            print(f'  not in index: {k}')
        if r['error']:
            print(f"error: {r['error']}")
        sys.exit(1 if bad or r['error'] else 0)
    elif args.cmd == 'restore':
        restore(args.unit)
        print(f'Bulk thaw of {args.unit} requested; check back in up to 48 hours: photo-offsite check {args.unit}')
    else:
        ok, msg = check(args.unit)
        print(msg)
        sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
