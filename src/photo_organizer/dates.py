"""Capture-date extraction: ExifTool for embedded metadata, then filename and folder patterns."""

import datetime
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from photo_organizer.constants import FILENAME_PATTERNS, VIDEO_EXTENSIONS

# Capture time in local time (EXIF), or with an explicit offset (Apple Keys / Sony XML).
LOCAL_OR_ZONED_TAGS = ('SubSecDateTimeOriginal', 'DateTimeOriginal', 'CreationDate', 'CreationDateValue')
# QuickTime container dates: UTC per spec, but many cameras write local time instead.
CONTAINER_TAGS = ('CreateDate', 'MediaCreateDate')
READ_TAGS = LOCAL_OR_ZONED_TAGS + CONTAINER_TAGS + ('Make', 'MajorBrand')
# Verified on this library: Sony SLT-A65V MP4 times match its JPEG times; iPhone/phone MOV/MP4 are UTC.
LOCAL_TIME_VIDEO_MAKERS = ('sony', 'canon', 'nikon', 'panasonic', 'fujifilm', 'olympus', 'om digital',
                           'pentax', 'ricoh', 'leica')
EXIFTOOL_CHUNK = 50
DEFAULT_EXIF_WORKERS = 8


def _plausible_year(y):
    return 1970 <= y <= datetime.date.today().year + 1


def parse_exif_datetime(val):
    """Parse '2013:01:18 15:21:17[.123][-07:00]' to a datetime (aware when an offset is present)."""
    if not val or not isinstance(val, str):
        return None
    m = re.match(r'(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?',
                 val.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(g) for g in m.groups()[:6])
    if not _plausible_year(y):
        return None
    tz = None
    if m.group(7):
        off = m.group(7)
        if off == 'Z':
            tz = datetime.timezone.utc
        else:
            sign = -1 if off[0] == '-' else 1
            digits = off[1:].replace(':', '')
            tz = datetime.timezone(sign * datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    try:
        return datetime.datetime(y, mo, d, hh, mm, ss, tzinfo=tz)
    except ValueError:
        return None


def require_exiftool():
    if not shutil.which('exiftool'):
        raise SystemExit("exiftool not found in PATH. Install with: brew install exiftool")


def _container_time_is_local(rec):
    maker = f"{rec.get('Make') or ''} {rec.get('MajorBrand') or ''}".lower()
    return any(m in maker for m in LOCAL_TIME_VIDEO_MAKERS)


def pick_capture_date(rec, is_video):
    """Best (datetime, raw_string) from one exiftool record (read without QuickTimeUTC), or None."""
    for tag in LOCAL_OR_ZONED_TAGS:
        dt = parse_exif_datetime(rec.get(tag))
        if dt:
            return dt, rec[tag]
    for tag in CONTAINER_TAGS:
        dt = parse_exif_datetime(rec.get(tag))
        if not dt:
            continue
        if is_video and dt.tzinfo is None and not _container_time_is_local(rec):
            # UTC per the QuickTime spec: convert to this machine's local zone (DST-aware).
            dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
        return dt, rec[tag]
    return None


def _read_chunk(chunk):
    cmd = ['exiftool', '-json', '-q', '-q', '-fast', '-charset', 'filename=utf8']
    cmd += [f'-{t}' for t in READ_TAGS]
    cmd += ['-@', '-']
    # Own session so Ctrl-C reaches only our handler; a killed read would make dated files look undated.
    result = subprocess.run(cmd, input='\n'.join(chunk), capture_output=True, text=True,
                            start_new_session=True)
    # exiftool exits 1 if some files are unreadable (the rest are still reported); a signal is fatal.
    if result.returncode < 0:
        raise RuntimeError(f"exiftool killed by signal {-result.returncode}")
    try:
        records = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        records = []
    out = {}
    for rec in records:
        is_video = os.path.splitext(rec['SourceFile'])[1].lower() in VIDEO_EXTENSIONS
        hit = pick_capture_date(rec, is_video)
        if hit:
            out[rec['SourceFile']] = hit
    return out


def read_exif_dates(paths, on_chunk=None, workers=DEFAULT_EXIF_WORKERS):
    """Return {path: (datetime, raw_string)} for files with a usable embedded capture date.

    Over SMB each file costs a network round trip or two, so several exiftool processes
    read disjoint chunks in parallel to keep requests in flight.
    """
    chunks = [paths[i:i + EXIFTOOL_CHUNK] for i in range(0, len(paths), EXIFTOOL_CHUNK)]
    out = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_read_chunk, c): len(c) for c in chunks}
        for fut in as_completed(futures):
            out.update(fut.result())
            done += futures[fut]
            if on_chunk:
                on_chunk(done, len(paths))
    return out


def date_from_filename(filename):
    for pattern in FILENAME_PATTERNS:
        m = pattern.search(filename)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if _plausible_year(y):
                    return datetime.date(y, mo, d)
            except ValueError:
                continue
    return None


def date_from_dirpath(filepath, source_root):
    """Date from the nearest 'YYYY-MM-DD' folder. Year- or month-only folders are too vague to file by."""
    parts = os.path.relpath(filepath, source_root).split(os.sep)[:-1]
    for part in reversed(parts):
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', part)
        if m:
            try:
                d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if _plausible_year(d.year):
                    return d
            except ValueError:
                pass
    return None
